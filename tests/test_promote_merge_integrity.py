"""Promote/merge integrity: audit, gallery ownership, provenance, honest logs.

    docker exec face_recognition_api python -m pytest tests/test_promote_merge_integrity.py -v

What this file pins, and why it did not exist before:

  * Successful promotions were NEVER audited — log_promote() was defined and
    called from nowhere; only unexpected exceptions wrote a row.
  * Merge re-parented appearances/embeddings/faces but NOT identity_images, so
    winner-owned embeddings pointed at loser-owned image rows forever.
  * remove_from_vector_index ran AFTER the re-parent, matched zero rows,
    removed nothing — and logged "Removed N vector(s)".
  * Neither operation invalidated the Unknown-list cache (TTL 30 hours).
  * The re-parent UPDATEs kept no provenance, so no unmerge can ever exist.

House pattern from test_identity_merge_smoke.py: HTTP against the live app,
direct SQL for ground truth, qa_ prefix + module cleanup.
"""

import io
import json
import os
import urllib.error
import urllib.request
import uuid as uuid_module

import pytest

from conftest import run_on_shared_loop as run_async

BASE = "http://localhost:8000"
FIXTURES = "/app/tests/fixtures/faces"
FACE_A = f"{FIXTURES}/face_a.jpg"
FACE_B = f"{FIXTURES}/face_b.jpg"
FACES_DIR = "/app/storage/faces"

TEST_PREFIX = "qa_pmint_"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _multipart(fields, files):
    boundary = "----qapmint" + uuid_module.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f"--{boundary}\r\n".encode())
        out.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.write(f"{value}\r\n".encode())
    for name, (filename, payload, content_type) in files.items():
        out.write(f"--{boundary}\r\n".encode())
        out.write((f'Content-Disposition: form-data; name="{name}"; '
                   f'filename="{filename}"\r\n').encode())
        out.write(f"Content-Type: {content_type}\r\n\r\n".encode())
        out.write(payload)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def _http(method, path, *, body=None, token=None, fields=None, files=None,
          timeout=180):
    data = None
    headers = {}
    if files is not None:
        data, content_type = _multipart(fields or {}, files)
        headers["Content-Type"] = content_type
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw or b"{}")
            except Exception:
                return response.status, {"_raw": raw.decode(errors="replace")}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except Exception:
            return exc.code, {"_raw": raw.decode(errors="replace")}


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         body={"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


def _enroll(token, name, fixture_path):
    """Create a KNOWN identity from a fixture, answering the decision gate."""
    kind = "image/png" if fixture_path.endswith(".png") else "image/jpeg"
    status, body = _http("POST", "/api/upload-person", token=token,
                         fields={"person_name": name},
                         files={"photo": (os.path.basename(fixture_path),
                                          _read(fixture_path), kind)})
    if status == 202 and body.get("decision_required"):
        status, body = _http("POST", "/api/enrollment/confirm", token=token,
                             body={"action": "create_new", "display_name": name,
                                   "upload_token": body["upload_token"],
                                   "confirm_create_new": True})
    assert status == 200 and body.get("success"), body
    return body["identity_id"]


def _sql(statement, params=None, fetch="all"):
    from sqlalchemy import text

    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            result = await db.execute(text(statement), params or {})
            if not result.returns_rows:
                value = result.rowcount
            elif fetch == "scalar":
                value = result.scalar()
            else:
                value = result.all()
            await db.commit()
            return value
    return run_async(_run())


QA_PIPELINE = "qa-pmint-cam"


def _make_unknown(name):
    """SQL-seed an ACTIVE unknown identity the Unknown list will actually show.

    The list endpoint SKIPS identities with no resolvable pipeline ("completely
    remove them"), so a bare identities row is invisible — it needs at least an
    appearance. The pipeline row is created too, so this also works on a fresh
    isolated database.
    """
    identity_id = str(_sql(
        "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
        " last_seen_at, created_at, updated_at, appearances_count) "
        "VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, now(), now(), now(), now(), 1) "
        "RETURNING id", {"n": name}, fetch="scalar"))
    _sql("INSERT INTO pipelines (pipeline_id, created_at, is_active) "
         "VALUES (:p, now(), 1) ON CONFLICT (pipeline_id) DO NOTHING",
         {"p": QA_PIPELINE})
    _sql("INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
         "VALUES (:i, :p, now(), now())", {"i": identity_id, "p": QA_PIPELINE})
    return identity_id


def _delete_identity(identity_id):
    for statement in (
        "UPDATE identities SET merged_into_id = NULL WHERE merged_into_id = :i",
        "DELETE FROM identity_merges WHERE from_identity_id = :i OR to_identity_id = :i",
        "DELETE FROM identity_audit_log WHERE identity_id = :i OR related_identity_id = :i",
        "DELETE FROM identity_embeddings WHERE identity_id = :i",
        "DELETE FROM identity_images WHERE identity_id = :i",
        "DELETE FROM identity_appearances WHERE identity_id = :i",
        "DELETE FROM identities WHERE id = :i",
    ):
        _sql(statement, {"i": identity_id})
    import shutil
    folder = os.path.join(FACES_DIR, str(identity_id))
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)


def _cleanup_prefix():
    rows = _sql("SELECT id FROM identities WHERE display_name LIKE :p",
                {"p": TEST_PREFIX + "%"})
    for (identity_id,) in rows:
        _delete_identity(str(identity_id))
    _sql("DELETE FROM identity_appearances WHERE pipeline_id = :p", {"p": QA_PIPELINE})
    _sql("DELETE FROM pipelines WHERE pipeline_id = :p", {"p": QA_PIPELINE})


@pytest.fixture(scope="module", autouse=True)
def _clean_module():
    _cleanup_prefix()
    yield
    _cleanup_prefix()


def _merge(token, from_id, to_id):
    return _http("POST", "/api/admin/identities/merge", token=token,
                 body={"from_identity_id": from_id, "to_identity_id": to_id,
                       "notes": "qa_pmint integrity",
                       "decision": "merge_existing",
                       "confirm_merge_risk": True})


def _abs_storage(relative_path):
    """storage/faces/... -> /app/storage/faces/..."""
    return "/app/" + str(relative_path)


UNKNOWN_LIST = "/api/admin/unknown?page=1&page_size=100"


# ---------------------------------------------------------------------------
# PROMOTE
# ---------------------------------------------------------------------------

def test_successful_promote_writes_an_audit_row(token):
    """Proof 1 (+ person_code metadata). log_promote() existed and was called
    from NOWHERE — a successful promotion left no audit trace at all."""
    unknown_id = _make_unknown(TEST_PREFIX + "audit-probe")
    status, body = _http(
        "POST", f"/api/admin/unknown/{unknown_id}/promote", token=token,
        body={"display_name": TEST_PREFIX + "Promoted One",
              "person_code": "QA-CODE-77", "decision": "create_new"})
    assert status == 200 and body.get("success"), body

    rows = _sql(
        "SELECT action_type, success, username, action_details, notes "
        "FROM identity_audit_log WHERE identity_id = :i AND action_type='promote'",
        {"i": unknown_id})
    assert rows, "successful promotion wrote no audit row"
    action_type, success, username, details, notes = rows[0]
    assert success is True
    assert username == "admin"
    details = details if isinstance(details, dict) else json.loads(details or "{}")
    assert details.get("display_name") == TEST_PREFIX + "Promoted One"
    # person_code's one honest use: audit metadata. It was previously accepted
    # and silently discarded.
    assert "QA-CODE-77" in (notes or ""), notes


def test_promoted_identity_disappears_from_the_unknown_list(token):
    """Proof 3. The list is cached for 30 HOURS; without post-commit
    invalidation the stale page keeps showing the promoted face."""
    unknown_id = _make_unknown(TEST_PREFIX + "vanish-probe")

    status, body = _http("GET", UNKNOWN_LIST, token=token)
    assert status == 200
    assert unknown_id in json.dumps(body), \
        "seeded unknown identity should appear in the list (cache now warm)"

    status, body = _http(
        "POST", f"/api/admin/unknown/{unknown_id}/promote", token=token,
        body={"display_name": TEST_PREFIX + "Promoted Vanish",
              "decision": "create_new"})
    assert status == 200, body

    status, body = _http("GET", UNKNOWN_LIST, token=token)
    assert status == 200
    assert unknown_id not in json.dumps(body), \
        "promoted identity still served from the stale Unknown cache"


def test_repromoting_a_known_identity_is_refused_and_audited(token):
    """Proofs 2 + 16 (promote half). The refusal is the controlled response —
    and it must leave an audit row, which it previously did not."""
    unknown_id = _make_unknown(TEST_PREFIX + "repromote-probe")
    status, _body = _http(
        "POST", f"/api/admin/unknown/{unknown_id}/promote", token=token,
        body={"display_name": TEST_PREFIX + "Promoted Twice",
              "decision": "create_new"})
    assert status == 200

    status, body = _http(
        "POST", f"/api/admin/unknown/{unknown_id}/promote", token=token,
        body={"display_name": TEST_PREFIX + "Promoted Again",
              "decision": "create_new"})
    assert status == 400, body
    detail = json.dumps(body)
    assert "not unknown" in detail.lower()

    rows = _sql(
        "SELECT success, error_message FROM identity_audit_log "
        "WHERE identity_id = :i AND action_type='promote' AND success = false",
        {"i": unknown_id})
    assert rows, "refused promotion left no audit row"
    assert "not unknown" in (rows[0][1] or "").lower()


# ---------------------------------------------------------------------------
# MERGE — ownership, files, provenance
# ---------------------------------------------------------------------------

def test_merge_moves_gallery_ownership_and_records_provenance(token):
    """Proofs 5, 6, 9, 17. The heart of the change: identity_images was NOT in
    the re-parenting list, so the winner's embeddings pointed at rows owned by
    the soft-deleted loser."""
    winner = _enroll(token, TEST_PREFIX + "gall-winner", FACE_A)
    loser = _enroll(token, TEST_PREFIX + "gall-loser", FACE_B)

    status, body = _merge(token, loser, winner)
    assert status == 200 and body.get("success"), body

    # Proof 5: winner owns everything.
    counts = _sql(
        "SELECT "
        " (SELECT count(*) FROM identity_images WHERE identity_id = :w),"
        " (SELECT count(*) FROM identity_images WHERE identity_id = :l),"
        " (SELECT count(*) FROM identity_embeddings WHERE identity_id = :w),"
        " (SELECT count(*) FROM identity_embeddings WHERE identity_id = :l)",
        {"w": winner, "l": loser})[0]
    assert counts[0] == 2, f"winner should own both images, owns {counts[0]}"
    assert counts[1] == 0, f"loser still owns {counts[1]} image row(s)"
    assert counts[2] == 2 and counts[3] == 0

    # Proof 6: no winner embedding references a loser-owned image.
    orphans = _sql(
        "SELECT count(*) FROM identity_embeddings e "
        "JOIN identity_images i ON e.image_id = i.id "
        "WHERE e.identity_id = :w AND i.identity_id != :w",
        {"w": winner}, fetch="scalar")
    assert orphans == 0, f"{orphans} winner embedding(s) point at foreign images"

    # Proof 9: every winner gallery file is readable and non-empty.
    for (path,) in _sql(
            "SELECT storage_path FROM identity_images WHERE identity_id = :w",
            {"w": winner}):
        absolute = _abs_storage(path)
        assert os.path.isfile(absolute), f"gallery file missing: {path}"
        assert os.path.getsize(absolute) > 0, f"gallery file empty: {path}"

    # The loser's ORIGINAL file and folder survive — merge copies, never moves.
    loser_folder = os.path.join(FACES_DIR, loser)
    assert os.path.isdir(loser_folder)
    assert any(name.startswith("image_") for name in os.listdir(loser_folder))

    # Proof 17: provenance names the moved rows and both paths.
    prov = _sql(
        "SELECT provenance FROM identity_merges "
        "WHERE from_identity_id = :l AND to_identity_id = :w",
        {"l": loser, "w": winner}, fetch="scalar")
    assert prov, "merge wrote no provenance"
    prov = prov if isinstance(prov, dict) else json.loads(prov)
    assert prov["embedding_ids"], "moved embeddings not recorded"
    assert prov["loser_display_name"] == TEST_PREFIX + "gall-loser"
    assert len(prov["images"]) == 1
    image_record = prov["images"][0]
    assert image_record["file_copied"] is True
    assert image_record["original_path"].startswith(f"storage/faces/{loser}/")
    assert image_record["new_path"].startswith(f"storage/faces/{winner}/")
    assert image_record["deduplicated_into"] is None


def test_merge_never_overwrites_a_winner_file(token):
    """Proof 10. Both sides own image_001.jpg; the copy must land in the next
    free slot, byte-identical originals intact."""
    winner = _enroll(token, TEST_PREFIX + "coll-winner", FACE_A)
    loser = _enroll(token, TEST_PREFIX + "coll-loser", FACE_B)

    winner_before = {name: os.path.getsize(os.path.join(FACES_DIR, winner, name))
                     for name in os.listdir(os.path.join(FACES_DIR, winner))}
    assert "image_001.jpg" in winner_before

    status, body = _merge(token, loser, winner)
    assert status == 200, body

    winner_after = {name: os.path.getsize(os.path.join(FACES_DIR, winner, name))
                    for name in os.listdir(os.path.join(FACES_DIR, winner))}
    # The original is untouched, and the copy occupied a NEW name.
    assert winner_after["image_001.jpg"] == winner_before["image_001.jpg"]
    assert len(winner_after) == len(winner_before) + 1


def test_merge_deduplicates_identical_photos(token):
    """Proof 7. Same bytes on both sides: moving the loser's row would violate
    uq_identity_image_checksum, so its embeddings re-point at the winner's copy
    and the duplicate row stays behind on the soft-deleted loser."""
    winner = _enroll(token, TEST_PREFIX + "dup-winner", FACE_A)
    loser = _enroll(token, TEST_PREFIX + "dup-loser", FACE_A)

    winner_image = _sql(
        "SELECT id FROM identity_images WHERE identity_id = :w",
        {"w": winner}, fetch="scalar")

    status, body = _merge(token, loser, winner)
    assert status == 200, body

    counts = _sql(
        "SELECT "
        " (SELECT count(*) FROM identity_images WHERE identity_id = :w),"
        " (SELECT count(*) FROM identity_images WHERE identity_id = :l)",
        {"w": winner, "l": loser})[0]
    assert counts[0] == 1, "dedup must not create a second winner image row"
    assert counts[1] == 1, "the duplicate row stays on the loser"

    # Every winner embedding references the winner's own image.
    foreign = _sql(
        "SELECT count(*) FROM identity_embeddings e "
        "JOIN identity_images i ON e.image_id = i.id "
        "WHERE e.identity_id = :w AND i.identity_id != :w",
        {"w": winner}, fetch="scalar")
    assert foreign == 0

    prov = _sql(
        "SELECT provenance FROM identity_merges "
        "WHERE from_identity_id = :l AND to_identity_id = :w",
        {"l": loser, "w": winner}, fetch="scalar")
    prov = prov if isinstance(prov, dict) else json.loads(prov)
    assert prov["images"][0]["deduplicated_into"] == winner_image
    assert prov["images"][0]["file_copied"] is False


def test_merge_preserves_a_single_primary_image(token):
    """Proof 8. uq_identity_image_one_primary is DB-enforced; both sides enter
    the merge with a primary and exactly one may leave it."""
    winner = _enroll(token, TEST_PREFIX + "prim-winner", FACE_A)
    loser = _enroll(token, TEST_PREFIX + "prim-loser", FACE_B)

    status, body = _merge(token, loser, winner)
    assert status == 200, body

    primaries = _sql(
        "SELECT count(*) FROM identity_images WHERE identity_id = :w AND is_primary",
        {"w": winner}, fetch="scalar")
    assert primaries == 1, f"winner has {primaries} primary images"


def test_missing_source_file_degrades_without_losing_the_row(token):
    """Proof 11. When the loser's file cannot be copied, the ROW still moves —
    keeping its old path, which stays readable because the loser folder is
    never deleted — and provenance says file_copied=false."""
    winner = _enroll(token, TEST_PREFIX + "miss-winner", FACE_A)
    loser = _enroll(token, TEST_PREFIX + "miss-loser", FACE_B)

    # Point the loser's row at a file that does not exist.
    _sql("UPDATE identity_images SET storage_path = :p WHERE identity_id = :l",
         {"p": f"storage/faces/{loser}/definitely_missing.jpg", "l": loser})

    status, body = _merge(token, loser, winner)
    assert status == 200, body

    row = _sql(
        "SELECT identity_id, storage_path FROM identity_images "
        "WHERE storage_path LIKE :p",
        {"p": f"storage/faces/{loser}/definitely_missing%"})
    assert row and str(row[0][0]) == winner, \
        "the uncopyable image row must still be re-parented to the winner"

    loser_folder = os.path.join(FACES_DIR, loser)
    assert os.path.isdir(loser_folder), "loser folder must survive copy failure"

    prov = _sql(
        "SELECT provenance FROM identity_merges "
        "WHERE from_identity_id = :l AND to_identity_id = :w",
        {"l": loser, "w": winner}, fetch="scalar")
    prov = prov if isinstance(prov, dict) else json.loads(prov)
    assert prov["images"][0]["file_copied"] is False
    assert prov["images"][0]["new_path"] is None


def test_db_rollback_restores_ownership_and_cleanup_removes_copies(token):
    """Proof 12. Consolidation inside a transaction that rolls back leaves the
    loser exactly as it was; the copied file — the one thing rollback cannot
    reach — is removed by the route's cleanup helper."""
    winner = _enroll(token, TEST_PREFIX + "roll-winner", FACE_A)
    loser = _enroll(token, TEST_PREFIX + "roll-loser", FACE_B)

    async def _run():
        import uuid as _uuid

        from sqlalchemy import select

        from backend.core.identity_service import IdentityService
        from db_connection import db_manager
        from db_models import Identity

        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()

        copied: list = []
        async with db_manager.get_session() as db:
            loser_row = (await db.execute(
                select(Identity).where(Identity.id == _uuid.UUID(loser))
            )).scalar_one()
            service = object.__new__(IdentityService)  # the method uses no state
            winner_row = (await db.execute(
                select(Identity).where(Identity.id == _uuid.UUID(winner))
            )).scalar_one()
            provenance = await IdentityService._consolidate_loser_assets(
                service, db, loser_row, winner_row, copied)
            assert provenance["images"], "consolidation saw no images"
            assert copied, "a file should have been copied before the failure"
            await db.rollback()
        return copied

    copied = run_async(_run())
    assert all(os.path.isfile(p) for p in copied), "copy should exist pre-cleanup"

    # DB: rollback restored the loser's ownership completely.
    still_owned = _sql(
        "SELECT count(*) FROM identity_images WHERE identity_id = :l",
        {"l": loser}, fetch="scalar")
    assert still_owned == 1, "rollback did not restore the loser's image row"

    # Files: the route's helper removes what rollback cannot.
    from backend.routes.identities import _cleanup_copied_files
    _cleanup_copied_files(copied)
    assert all(not os.path.isfile(p) for p in copied), "cleanup left copies behind"

    # And the loser's original file was never touched.
    loser_folder = os.path.join(FACES_DIR, loser)
    assert any(name.startswith("image_") for name in os.listdir(loser_folder))


# ---------------------------------------------------------------------------
# MERGE — visibility, idempotency, honesty
# ---------------------------------------------------------------------------

def test_merged_identity_disappears_from_the_unknown_list(token):
    """Proof 4 — same 30h-stale-cache reasoning as the promote variant."""
    winner = _enroll(token, TEST_PREFIX + "unk-winner", FACE_A)
    loser = _make_unknown(TEST_PREFIX + "unk-loser")

    status, body = _http("GET", UNKNOWN_LIST, token=token)
    assert status == 200
    assert loser in json.dumps(body), "seeded unknown should be listed"

    status, body = _merge(token, loser, winner)
    assert status == 200, body

    status, body = _http("GET", UNKNOWN_LIST, token=token)
    assert status == 200
    assert loser not in json.dumps(body), \
        "merged identity still served from the stale Unknown cache"


def test_rerunning_a_merge_returns_409_naming_the_winner(token):
    """Proof 16 (merge half). A completed merge re-submitted is not an error to
    retry — the 409 names the winner so callers can tell 'done' from 'wrong'.
    Merging INTO a merged identity is refused the same way (chain prevention)."""
    winner = _enroll(token, TEST_PREFIX + "rerun-winner", FACE_A)
    loser = _enroll(token, TEST_PREFIX + "rerun-loser", FACE_B)
    third = _enroll(token, TEST_PREFIX + "rerun-third", FACE_A)

    status, body = _merge(token, loser, winner)
    assert status == 200, body

    # Same merge again → 409 naming the winner.
    status, body = _merge(token, loser, winner)
    assert status == 409, (status, body)
    assert winner in json.dumps(body), "the 409 must name the winning identity"

    # Merging into the dead loser → 409 (no chains).
    status, body = _merge(token, third, loser)
    assert status == 409, (status, body)

    # Nothing was duplicated by the re-runs: exactly one merge record.
    merges = _sql(
        "SELECT count(*) FROM identity_merges "
        "WHERE from_identity_id = :l AND to_identity_id = :w",
        {"l": loser, "w": winner}, fetch="scalar")
    assert merges == 1


def test_search_does_not_return_the_merged_loser(token):
    """Proof 15. Search behavior unchanged: the loser is excluded by status,
    embedding IDs stay valid and resolve to the winner."""
    winner = _enroll(token, TEST_PREFIX + "srch-winner", FACE_A)
    loser = _enroll(token, TEST_PREFIX + "srch-loser", FACE_B)
    status, body = _merge(token, loser, winner)
    assert status == 200, body

    status, body = _http("POST", "/api/search/by-image", token=token,
                         fields={"scope": "both", "top_k": "10"},
                         files={"image": ("face_b.jpg", _read(FACE_B), "image/jpeg")})
    assert status == 200, body
    assert loser not in json.dumps(body), "merged loser must never be searchable"


def test_cache_invalidation_happens_only_after_commit():
    """Proof 13, structurally: in every mutating route, the first cache
    invalidation call comes AFTER the first commit, and the service layer
    (which runs inside the transaction) never invalidates at all."""
    import inspect

    # Direct module import, not `from backend.routes import identities`:
    # the package __init__ uses guarded imports, so its ATTRIBUTE can be None
    # in a process where an optional dependency failed — the module itself
    # still imports fine.
    import backend.routes.identities as routes_module

    for handler_name in ("promote_unknown_to_known", "merge_identities",
                         "merge_multiple_identities", "approve_merge_suggestion"):
        source = inspect.getsource(getattr(routes_module, handler_name))
        invalidate_at = source.find("invalidate_unknown_cache")
        assert invalidate_at != -1, f"{handler_name} never invalidates the cache"
        commit_at = source.find("await db.commit()")
        assert commit_at != -1 and commit_at < invalidate_at, \
            f"{handler_name} invalidates before committing"

    # Plain file read, not inspect.getsource(module): another test in this
    # process can leave sys.modules['backend.core.identity_service'] poisoned,
    # and the file on disk is the artifact under scrutiny anyway.
    with open("/app/backend/core/identity_service.py", encoding="utf-8") as handle:
        service_source = handle.read()
    assert "invalidate_unknown_cache" not in service_source, \
        "the service layer must not invalidate mid-transaction"


def test_merge_logs_reparenting_not_removal():
    """Proof 14. The old FAISS branch logged 'Removed N vector(s)' for a call
    that matched zero rows. The claim must be gone from every merge path."""
    with open("/app/backend/core/identity_service.py", encoding="utf-8") as handle:
        source = handle.read()
    assert "no vector deletion was required" in source
    for lie in ("Removed {removed} vector", "Removed {removed} vectors"):
        assert lie not in source, f"the false removal log survives: {lie!r}"


def test_provenance_is_recorded_for_multi_merge(token):
    """Proof 17 for the merge-multiple path — one provenance blob per source."""
    a = _enroll(token, TEST_PREFIX + "multi-a", FACE_A)
    b = _enroll(token, TEST_PREFIX + "multi-b", FACE_B)

    status, body = _http("POST", "/api/admin/identities/merge-multiple",
                         token=token,
                         body={"identity_ids": [a, b], "target_identity_id": a,
                               "notes": "qa_pmint multi"})
    assert status == 200 and body.get("success"), body

    prov = _sql(
        "SELECT provenance FROM identity_merges "
        "WHERE from_identity_id = :l AND to_identity_id = :w",
        {"l": b, "w": a}, fetch="scalar")
    assert prov, "merge-multiple wrote no provenance"
    prov = prov if isinstance(prov, dict) else json.loads(prov)
    assert prov["embedding_ids"]
    assert prov["images"] and prov["images"][0]["file_copied"] is True


# ---------------------------------------------------------------------------
# MERGE SNAPSHOT ADOPTION — the loser's best snapshot joins the winner's
# gallery, gated on THAT FILE's quality
# ---------------------------------------------------------------------------
#
# Unknowns have no identity_images rows, so before this feature an
# unknown->known merge left the winner's profile without the very photo that
# justified the merge. Adoption considers exactly ONE candidate —
# best_snapshot_path — and only when the exact file passes
# settings.IDENTITY_QUALITY_THRESHOLD_KNOWN. The quality is measured from the
# file (or a stored identity_images row for that path), never borrowed from
# "an" embedding of the same identity.

QA_SNAP_DIR = os.path.join("/app/storage", QA_PIPELINE, "unknown")


def _write_snapshot(payload, suffix=".jpg"):
    os.makedirs(QA_SNAP_DIR, exist_ok=True)
    path = os.path.join(QA_SNAP_DIR, f"{TEST_PREFIX}snap_{uuid_module.uuid4().hex}{suffix}")
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


def _blurred_beyond_detection():
    """A frame no detector path can find a face in — the unscorable case."""
    import cv2
    import numpy as np
    img = cv2.imread(FACE_A)
    blur = cv2.GaussianBlur(img, (61, 61), 25)
    ok, buf = cv2.imencode(".jpg", blur)
    assert ok
    return buf.tobytes()


def _seed_unknown_with_snapshot(name, snapshot_path, *, embedding_quality=None):
    """ACTIVE unknown pointing at snapshot_path, optionally with a seeded
    embedding whose quality is deliberately DIFFERENT from the file's."""
    identity_id = str(_sql(
        "INSERT INTO identities (id, type, status, display_name, best_snapshot_path, "
        " first_seen_at, last_seen_at, created_at, updated_at, appearances_count) "
        "VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, :p, now(), now(), "
        "        now(), now(), 1) RETURNING id",
        {"n": name, "p": snapshot_path}, fetch="scalar"))
    _sql("INSERT INTO pipelines (pipeline_id, created_at, is_active) "
         "VALUES (:p, now(), 1) ON CONFLICT (pipeline_id) DO NOTHING",
         {"p": QA_PIPELINE})
    _sql("INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
         "VALUES (:i, :p, now(), now())", {"i": identity_id, "p": QA_PIPELINE})
    if embedding_quality is not None:
        # Borrow a real vector, stamp a DIFFERENT quality on it. If adoption
        # ever reads this instead of the file, the A/B test below fails.
        _sql("INSERT INTO identity_embeddings (identity_id, pipeline_id, embedding, "
             "       faiss_index_type, embedding_model_version, quality, created_at) "
             "SELECT :i, :p, e.embedding, 'unknown', e.embedding_model_version, :q, now() "
             "FROM identity_embeddings e LIMIT 1",
             {"i": identity_id, "p": QA_PIPELINE, "q": embedding_quality})
    return identity_id


def _merge_prov(loser_id):
    rows = _sql("SELECT provenance FROM identity_merges WHERE from_identity_id = :i "
                "ORDER BY id DESC LIMIT 1", {"i": loser_id})
    if not rows or rows[0][0] is None:
        return {}
    value = rows[0][0]
    return json.loads(value) if isinstance(value, str) else value


def _gallery(identity_id):
    return _sql("SELECT id, storage_path, is_primary, source_type, quality_score, "
                "       quality_scorer_version FROM identity_images "
                "WHERE identity_id = :i ORDER BY id", {"i": identity_id})


def test_merge_adoption_scores_the_exact_snapshot_file(token):
    """The A/B proof. One loser, a seeded embedding at quality 0.95, and a
    best_snapshot_path pointing at an UNSCORABLE frame: if adoption borrowed the
    embedding's quality it would adopt; scoring the exact file refuses. Repoint
    the SAME identity at a sharp frame and it adopts — with the FILE's measured
    quality in provenance, not the embedding's 0.95."""
    unscorable = _write_snapshot(_blurred_beyond_detection())
    sharp = _write_snapshot(_read(FACE_A))

    winner = _enroll(token, TEST_PREFIX + "abwin", FACE_B)

    # --- B: unscorable file, high-quality embedding --------------------------
    loser_b = _seed_unknown_with_snapshot(TEST_PREFIX + "ab_b", unscorable,
                                          embedding_quality=0.95)
    try:
        status, body = _http("POST", "/api/admin/identities/merge", token=token,
                             body={"from_identity_id": loser_b,
                                   "to_identity_id": winner,
                                   "notes": "qa ab-b", "decision": "merge_existing",
                       "confirm_merge_risk": True})
        assert status == 200, body
        prov = _merge_prov(loser_b)
        assert prov.get("adopted_snapshot") is None, (
            "an unscorable snapshot was adopted — the embedding's 0.95 quality "
            "was used instead of the file's")
        assert prov.get("snapshot_adoption", {}).get("reason") == \
            "unavailable_snapshot_metadata", prov.get("snapshot_adoption")

        # --- A: sharp file, same identity shape ------------------------------
        gallery_before = len(_gallery(winner))
        loser_a = _seed_unknown_with_snapshot(TEST_PREFIX + "ab_a", sharp,
                                              embedding_quality=0.95)
        try:
            status, body = _http("POST", "/api/admin/identities/merge", token=token,
                                 body={"from_identity_id": loser_a,
                                       "to_identity_id": winner,
                                       "notes": "qa ab-a", "decision": "merge_existing",
                       "confirm_merge_risk": True})
            assert status == 200, body
            prov = _merge_prov(loser_a)
            adopted = prov.get("adopted_snapshot")
            assert adopted is not None, "a sharp above-threshold snapshot was refused"
            assert adopted["created_by_merge"] is True
            # The recorded quality is the FILE's (~0.766), not the seeded 0.95.
            assert adopted["quality_score"] < 0.9, adopted
            assert len(_gallery(winner)) == gallery_before + 1
        finally:
            _delete_identity(loser_a)
    finally:
        _delete_identity(loser_b)
        _delete_identity(winner)


def test_adopted_row_persists_score_and_scorer_version(token):
    """The new row carries quality_score AND quality_scorer_version — the
    path<->quality link that was structurally missing for unknowns."""
    from backend.core.face_quality import QUALITY_SCORER_VERSION

    sharp = _write_snapshot(_read(FACE_A))
    winner = _enroll(token, TEST_PREFIX + "perswin", FACE_B)
    loser = _seed_unknown_with_snapshot(TEST_PREFIX + "pers_lose", sharp)
    try:
        status, body = _http("POST", "/api/admin/identities/merge", token=token,
                             body={"from_identity_id": loser, "to_identity_id": winner,
                                   "notes": "qa persist", "decision": "merge_existing",
                       "confirm_merge_risk": True})
        assert status == 200, body

        rows = [r for r in _gallery(winner) if r[3] == "merge"]
        assert len(rows) == 1, rows
        row = rows[0]
        assert row[4] is not None and 0.0 < float(row[4]) <= 1.0, row
        assert row[5] == QUALITY_SCORER_VERSION, row
        assert row[2] is False, "the adopted image displaced the winner's primary"
        assert row[1].startswith("storage/faces/"), row
        # loser's original evidence untouched
        assert os.path.isfile(sharp)
        # adoption adds an image, never a vector
        assert _sql("SELECT count(*) FROM identity_embeddings WHERE identity_id=:i",
                    {"i": loser}, fetch="scalar") == 0  # re-parented away, not duplicated
    finally:
        _delete_identity(loser)
        _delete_identity(winner)


def test_below_threshold_snapshot_is_not_adopted():
    """Direct service call with a patched threshold ABOVE the file's real score:
    the merge machinery must refuse the image. Proves the gate reads
    settings.IDENTITY_QUALITY_THRESHOLD_KNOWN at decision time — no constant."""
    from config import settings

    sharp = _write_snapshot(_read(FACE_A))          # scores ~0.766

    async def _run():
        from backend.core.identity_service import IdentityService
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        # Constructed directly: the app-startup singleton does not exist in
        # the pytest process, and these methods need only db + settings.
        service = IdentityService()

        async with db_manager.get_session() as db:
            winner_id = (await db.execute(sa_text(
                "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
                " last_seen_at, created_at, updated_at, appearances_count) "
                "VALUES (gen_random_uuid(), 'KNOWN', 'ACTIVE', :n, now(), now(), now(), "
                "        now(), 0) RETURNING id"),
                {"n": TEST_PREFIX + "thr_win"})).scalar()
            loser_id = (await db.execute(sa_text(
                "INSERT INTO identities (id, type, status, display_name, best_snapshot_path, "
                " first_seen_at, last_seen_at, created_at, updated_at, appearances_count) "
                "VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, :p, now(), now(), "
                "        now(), now(), 0) RETURNING id"),
                {"n": TEST_PREFIX + "thr_lose", "p": sharp})).scalar()
            await db.commit()

            from db_models import Identity
            from sqlalchemy import select
            winner = (await db.execute(select(Identity).where(Identity.id == winner_id))).scalar_one()
            loser = (await db.execute(select(Identity).where(Identity.id == loser_id))).scalar_one()

            original = settings.IDENTITY_QUALITY_THRESHOLD_KNOWN
            try:
                object.__setattr__(settings, "IDENTITY_QUALITY_THRESHOLD_KNOWN", 0.99)
                prov_high = await service._consolidate_loser_assets(db, loser, winner, [])
                object.__setattr__(settings, "IDENTITY_QUALITY_THRESHOLD_KNOWN", 0.10)
                prov_low = await service._consolidate_loser_assets(db, loser, winner, [])
            finally:
                object.__setattr__(settings, "IDENTITY_QUALITY_THRESHOLD_KNOWN", original)
            await db.rollback()   # nothing from this probe persists
            return str(winner_id), str(loser_id), prov_high, prov_low

    winner_id, loser_id, prov_high, prov_low = run_async(_run())
    try:
        assert prov_high["adopted_snapshot"] is None, (
            "a 0.99 threshold still adopted a ~0.77 file")
        assert prov_high["snapshot_adoption"]["reason"] == "below_face_quality_threshold"
        assert prov_high["snapshot_adoption"]["threshold"] == 0.99
        assert prov_low["adopted_snapshot"] is not None, (
            "a 0.10 threshold refused a ~0.77 file")
    finally:
        _delete_identity(loser_id)
        _delete_identity(winner_id)


def test_unscorable_snapshot_does_not_fail_the_merge(token):
    """Corrupt bytes: scoring fails, the merge still completes, nothing adopted."""
    corrupt = _write_snapshot(b"definitely-not-a-jpeg")
    winner = _enroll(token, TEST_PREFIX + "corr_win", FACE_B)
    loser = _seed_unknown_with_snapshot(TEST_PREFIX + "corr_lose", corrupt)
    try:
        gallery_before = len(_gallery(winner))
        status, body = _http("POST", "/api/admin/identities/merge", token=token,
                             body={"from_identity_id": loser, "to_identity_id": winner,
                                   "notes": "qa corrupt", "decision": "merge_existing",
                       "confirm_merge_risk": True})
        assert status == 200, body
        assert len(_gallery(winner)) == gallery_before
        prov = _merge_prov(loser)
        assert prov.get("adopted_snapshot") is None
        assert prov.get("snapshot_adoption", {}).get("reason") == \
            "unavailable_snapshot_metadata"
        loser_status = _sql("SELECT status FROM identities WHERE id=:i",
                            {"i": loser}, fetch="scalar")
        assert str(loser_status).upper().endswith("MERGED")
    finally:
        _delete_identity(loser)
        _delete_identity(winner)


def test_deduplicated_snapshot_neither_duplicates_nor_overwrites(token):
    """The winner already owns byte-identical content: no new row, no new file,
    created_by_merge=false, and the existing row's metadata is untouched."""
    winner = _enroll(token, TEST_PREFIX + "dedup_win", FACE_A)
    before = _gallery(winner)
    assert len(before) == 1
    original_row = before[0]

    duplicate = _write_snapshot(_read(FACE_A))       # byte-identical to the enrolled photo
    loser = _seed_unknown_with_snapshot(TEST_PREFIX + "dedup_lose", duplicate)
    try:
        status, body = _http("POST", "/api/admin/identities/merge", token=token,
                             body={"from_identity_id": loser, "to_identity_id": winner,
                                   "notes": "qa dedup", "decision": "merge_existing",
                       "confirm_merge_risk": True})
        assert status == 200, body

        after = _gallery(winner)
        assert len(after) == 1, "dedup still created a second gallery row"
        assert after[0] == original_row, (
            f"the pre-existing row was modified:\n  before={original_row}\n  after ={after[0]}")

        prov = _merge_prov(loser)
        adopted = prov.get("adopted_snapshot")
        assert adopted is not None and adopted["created_by_merge"] is False, adopted
        assert adopted["image_id"] == original_row[0]

        # The pre-existing file must still exist (nothing staged it for rollback).
        assert os.path.isfile("/app/" + original_row[1])
    finally:
        _delete_identity(loser)
        _delete_identity(winner)


def test_a_loser_with_a_real_gallery_gets_no_extra_adoption(token):
    """A KNOWN loser with identity_images keeps today's consolidation exactly;
    best_snapshot fallback must not add an extra image on top."""
    winner = _enroll(token, TEST_PREFIX + "gal_win", FACE_A)
    loser = _enroll(token, TEST_PREFIX + "gal_lose", FACE_B)
    try:
        status, body = _http("POST", "/api/admin/identities/merge", token=token,
                             body={"from_identity_id": loser, "to_identity_id": winner,
                                   "notes": "qa gallery-loser", "decision": "merge_existing",
                       "confirm_merge_risk": True})
        assert status == 200, body
        prov = _merge_prov(loser)
        assert prov.get("adopted_snapshot") is None
        assert prov.get("snapshot_adoption", {}).get("reason") == "loser_has_gallery"
        # exactly the loser's own gallery moved — one 'upload' row each
        moved = [r for r in _gallery(winner) if r[3] == "upload"]
        assert len(moved) == 2
    finally:
        _delete_identity(loser)
        _delete_identity(winner)
