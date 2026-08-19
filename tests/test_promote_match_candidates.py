"""Promote match suggestions: reuse, read-only-ness, and the two explicit exits.

    docker exec face_recognition_api python -m pytest tests/test_promote_match_candidates.py -v

Promoting an unknown face used to be a blind act: the modal offered a name field
and nothing else, so the same person could be promoted twice and the duplicate
was only discovered later. GET /api/admin/unknown/{id}/match-candidates answers
"who might this already be?" first.

The properties worth pinning are the ones that are cheap to break silently:

  * it REUSES find_similar_identities/build_candidate_rows rather than growing a
    second similarity implementation that could drift from recognition;
  * looking changes NOTHING — no rows, no commit, no SearchHistory, no
    watchlist alerts, and above all no automatic merge;
  * every failure is a 200 with a warning, because an administrator whose
    snapshot is missing must still be able to promote the face as a new person;
  * only KNOWN identities are ever offered, and never the face's own identity.

House pattern from test_promote_merge_integrity.py: HTTP against the live app,
direct SQL for ground truth, a qa_ prefix and module cleanup.
"""

import inspect
import io
import json
import os
import shutil
import urllib.error
import urllib.request
import uuid as uuid_module

import pytest

from conftest import run_on_shared_loop as run_async

BASE = "http://localhost:8000"
FIXTURES = "/app/tests/fixtures/faces"
FACE_A = f"{FIXTURES}/face_a.jpg"
FACE_B = f"{FIXTURES}/face_b.jpg"
# A genuine stranger. face_b is NOT one: measured against face_a it scores
# 0.4299, just over the 0.40 ENROLL_CANDIDATE_MIN floor, so it legitimately
# matches and cannot prove that a different snapshot was used.
FACE_STRANGER = f"{FIXTURES}/face_c.png"
STORAGE = "/app/storage"
FACES_DIR = f"{STORAGE}/faces"

TEST_PREFIX = "qa_pmc_"
QA_PIPELINE = "qa-pmc-cam"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _multipart(fields, files):
    boundary = "----qapmc" + uuid_module.uuid4().hex
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
            except Exception:                                  # noqa: BLE001
                return response.status, {"_raw": raw.decode(errors="replace")}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except Exception:                                      # noqa: BLE001
            return exc.code, {"_raw": raw.decode(errors="replace")}


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


def _seed_unknown(name, source_fixture=FACE_A, *, absolute=True, corrupt=False,
                  missing=False):
    """An ACTIVE unknown with a real snapshot file on disk.

    Writes the snapshot under STORAGE/<pipeline>/unknown/ exactly as the
    detection pipeline does, so the endpoint exercises the same ABSOLUTE
    best_snapshot_path shape that production carries — the shape that
    pending_absolute_path alone mishandles.
    """
    folder = os.path.join(STORAGE, QA_PIPELINE, "unknown")
    os.makedirs(folder, exist_ok=True)
    filename = f"{TEST_PREFIX}{uuid_module.uuid4().hex}.jpg"
    path = os.path.join(folder, filename)

    if not missing:
        with open(path, "wb") as handle:
            handle.write(b"not-an-image" if corrupt else _read(source_fixture))

    stored = path if absolute else f"storage/{QA_PIPELINE}/unknown/{filename}"

    identity_id = str(_sql(
        "INSERT INTO identities (id, type, status, display_name, best_snapshot_path, "
        " first_seen_at, last_seen_at, created_at, updated_at, appearances_count) "
        "VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, :p, now(), now(), "
        "        now(), now(), 1) RETURNING id",
        {"n": name, "p": stored}, fetch="scalar"))
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
        "DELETE FROM faces WHERE identity_id = :i",
        "DELETE FROM identities WHERE id = :i",
    ):
        _sql(statement, {"i": identity_id})
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
    folder = os.path.join(STORAGE, QA_PIPELINE)
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(scope="module", autouse=True)
def _clean_module():
    _cleanup_prefix()
    yield
    _cleanup_prefix()


def _candidates(token, identity_id):
    return _http("GET", f"/api/admin/unknown/{identity_id}/match-candidates",
                 token=token)


def _handler_source():
    from backend.routes import identities as routes
    return inspect.getsource(routes.unknown_match_candidates)


def _handler_names():
    """Every name the handler actually CALLS or references, via the AST.

    Substring scanning is wrong here and the repo already knows it: the
    handler's own docstring explains why it avoids search_multi_face and
    db.commit, so a text search finds both and reports the opposite of the
    truth. Parsing means only real code counts.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(_handler_source()))
    called, attributes = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
        if isinstance(node, ast.Attribute):
            attributes.add(node.attr)
        if isinstance(node, ast.alias):
            called.add((node.asname or node.name).split(".")[-1])
    return called, attributes


def _db_snapshot():
    """Counts + max(updated_at) across everything the lookup could touch."""
    row = _sql(
        "SELECT (SELECT count(*) FROM identities), "
        "       (SELECT count(*) FROM identity_embeddings), "
        "       (SELECT count(*) FROM identity_appearances), "
        "       (SELECT count(*) FROM identity_images), "
        "       (SELECT count(*) FROM search_history), "
        "       (SELECT count(*) FROM watchlist_alerts), "
        "       (SELECT count(*) FROM identity_merges), "
        "       (SELECT max(updated_at) FROM identities)")
    return tuple(row[0])


# ---------------------------------------------------------------------------
# 1-2. reuse, and the stored snapshot as the input
# ---------------------------------------------------------------------------

def test_the_endpoint_reuses_the_shared_matching_helpers():
    """No second similarity implementation.

    Source-level rather than a patch: the route runs inside gunicorn, a
    different process from pytest, so monkeypatching here could not observe it.
    """
    called, _attributes = _handler_names()
    for helper in ("find_similar_identities", "build_candidate_rows",
                   "classify_match", "prepare_upload"):
        assert helper in called, f"{helper} is not actually called by the handler"
    # The two things it must NOT do: reimplement the vector query, or call the
    # search service that writes history rows and commits.
    assert "search_similar_embeddings" not in called
    assert "search_multi_face" not in called


def test_the_stored_best_snapshot_is_what_gets_matched(token):
    """Swap the file the identity points at and the answer must follow it."""
    known_a = _enroll(token, TEST_PREFIX + "person_a", FACE_A)
    unknown = _seed_unknown(TEST_PREFIX + "seed_a", FACE_A)
    try:
        status, body = _candidates(token, unknown)
        assert status == 200, body
        assert any(c["identity_id"] == known_a for c in body["candidates"]), body

        # Repoint the SAME identity at a different face; the match must change.
        folder = os.path.join(STORAGE, QA_PIPELINE, "unknown")
        other = os.path.join(folder, f"{TEST_PREFIX}{uuid_module.uuid4().hex}.png")
        with open(other, "wb") as handle:
            handle.write(_read(FACE_STRANGER))
        _sql("UPDATE identities SET best_snapshot_path = :p WHERE id = :i",
             {"p": other, "i": unknown})

        status, body = _candidates(token, unknown)
        assert status == 200, body
        assert not any(c["identity_id"] == known_a for c in body["candidates"]), (
            "the result did not follow best_snapshot_path — a cached or "
            "hard-coded image is being matched")
    finally:
        _delete_identity(unknown)
        _delete_identity(known_a)


def test_a_relative_snapshot_path_resolves_too(token):
    """Both stored shapes must work: absolute pipeline paths AND relative ones."""
    known = _enroll(token, TEST_PREFIX + "relpath", FACE_A)
    unknown = _seed_unknown(TEST_PREFIX + "seed_rel", FACE_A, absolute=False)
    try:
        status, body = _candidates(token, unknown)
        assert status == 200, body
        assert body["warning"] is None, body["warning"]
        assert any(c["identity_id"] == known for c in body["candidates"]), body
    finally:
        _delete_identity(unknown)
        _delete_identity(known)


# ---------------------------------------------------------------------------
# 3, 6, 7. what may appear in the list
# ---------------------------------------------------------------------------

def test_only_known_identities_are_offered(token):
    """An unknown twin scores near 1.0 and must still never be suggested."""
    known = _enroll(token, TEST_PREFIX + "known_only", FACE_A)
    subject = _seed_unknown(TEST_PREFIX + "subject", FACE_A)
    twin = _seed_unknown(TEST_PREFIX + "twin", FACE_A)
    # Give the twin a real embedding so it is genuinely searchable.
    _sql("INSERT INTO identity_embeddings (identity_id, pipeline_id, embedding, "
         "       faiss_index_type, embedding_model_version, created_at) "
         "SELECT :twin, :p, e.embedding, 'unknown', e.embedding_model_version, now() "
         "FROM identity_embeddings e WHERE e.identity_id = :known LIMIT 1",
         {"twin": twin, "known": known, "p": QA_PIPELINE})
    try:
        status, body = _candidates(token, subject)
        assert status == 200, body
        returned = {c["identity_id"] for c in body["candidates"]}
        assert twin not in returned, "an UNKNOWN identity was offered as a match"
        assert known in returned, body
    finally:
        _delete_identity(twin)
        _delete_identity(subject)
        _delete_identity(known)


def test_the_face_is_never_offered_as_its_own_match(token):
    """The subject has its own embedding here, so without the exclusion it
    would rank first at ~1.0."""
    known = _enroll(token, TEST_PREFIX + "selfexcl_known", FACE_A)
    subject = _seed_unknown(TEST_PREFIX + "selfexcl", FACE_A)
    _sql("INSERT INTO identity_embeddings (identity_id, pipeline_id, embedding, "
         "       faiss_index_type, embedding_model_version, created_at) "
         "SELECT :subj, :p, e.embedding, 'known', e.embedding_model_version, now() "
         "FROM identity_embeddings e WHERE e.identity_id = :known LIMIT 1",
         {"subj": subject, "known": known, "p": QA_PIPELINE})
    try:
        status, body = _candidates(token, subject)
        assert status == 200, body
        assert all(c["identity_id"] != subject for c in body["candidates"]), body
    finally:
        _delete_identity(subject)
        _delete_identity(known)


def test_many_embeddings_of_one_person_collapse_to_one_candidate(token):
    """Otherwise a well-photographed person fills every slot and hides the rest."""
    known = _enroll(token, TEST_PREFIX + "collapse", FACE_A)
    for _ in range(4):
        _sql("INSERT INTO identity_embeddings (identity_id, pipeline_id, embedding, "
             "       faiss_index_type, embedding_model_version, created_at) "
             "SELECT :k, :p, e.embedding, 'known', e.embedding_model_version, now() "
             "FROM identity_embeddings e WHERE e.identity_id = :k LIMIT 1",
             {"k": known, "p": QA_PIPELINE})
    subject = _seed_unknown(TEST_PREFIX + "collapse_subj", FACE_A)
    try:
        status, body = _candidates(token, subject)
        assert status == 200, body
        ids = [c["identity_id"] for c in body["candidates"]]
        assert ids.count(known) <= 1, f"one identity appeared {ids.count(known)} times"
    finally:
        _delete_identity(subject)
        _delete_identity(known)


def test_results_are_ordered_and_bounded_by_config(token):
    """Descending similarity, never more than ENROLL_MAX_CANDIDATES, never
    below ENROLL_CANDIDATE_MIN, and the thresholds are echoed from config."""
    from config import settings

    subject = _seed_unknown(TEST_PREFIX + "bounds", FACE_A)
    known_ids = [_enroll(token, f"{TEST_PREFIX}bounds_{i}", FACE_A) for i in range(3)]
    try:
        status, body = _candidates(token, subject)
        assert status == 200, body

        scores = [c["similarity"] for c in body["candidates"]]
        assert scores == sorted(scores, reverse=True), scores
        assert len(body["candidates"]) <= int(settings.ENROLL_MAX_CANDIDATES)
        assert all(s >= float(settings.ENROLL_CANDIDATE_MIN) for s in scores), scores

        thresholds = body["thresholds"]
        assert thresholds["candidate_min"] == float(settings.ENROLL_CANDIDATE_MIN)
        assert thresholds["strong_min"] == float(settings.ENROLL_STRONG_MATCH_MIN)
        assert thresholds["max_candidates"] == int(settings.ENROLL_MAX_CANDIDATES)
        assert thresholds["candidate_pool"] == int(settings.ENROLL_CANDIDATE_POOL)
    finally:
        _delete_identity(subject)
        for identity_id in known_ids:
            _delete_identity(identity_id)


def test_the_frontend_hard_codes_no_threshold():
    """Thresholds live in config.py and are displayed, never decided, client-side."""
    with open("/app/frontend/js/admin-unknown.js", encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("async function loadPromoteCandidates")
    end = source.index("async function mergeIntoKnownCandidate")
    block = source[start:end]
    for banned in ("0.4", "0.75", "0.35", "SIMILARITY_THRESHOLD"):
        assert banned not in block, f"threshold {banned!r} hard-coded in the modal JS"


# ---------------------------------------------------------------------------
# 5, 11. looking changes nothing
# ---------------------------------------------------------------------------

def test_viewing_suggestions_mutates_no_database_row(token):
    known = _enroll(token, TEST_PREFIX + "readonly", FACE_A)
    subject = _seed_unknown(TEST_PREFIX + "readonly_subj", FACE_A)
    try:
        before = _db_snapshot()
        for _ in range(3):
            status, body = _candidates(token, subject)
            assert status == 200, body
        after = _db_snapshot()
        assert before == after, (
            f"the lookup changed database state:\n  before={before}\n  after ={after}")
    finally:
        _delete_identity(subject)
        _delete_identity(known)


def test_the_handler_never_commits():
    """Structural. A commit here would turn opening a modal into a mutation,
    and would also finalize whatever the caller's transaction was holding."""
    called, _attributes = _handler_names()
    for writer in ("commit", "add", "delete", "flush"):
        assert writer not in called, (
            f"the read-only handler calls .{writer}() — opening a modal would mutate")


def test_a_strong_match_still_merges_nothing(token):
    """The whole point: a 90%+ match is a suggestion, never an action."""
    known = _enroll(token, TEST_PREFIX + "nostrongauto", FACE_A)
    subject = _seed_unknown(TEST_PREFIX + "nostrongauto_subj", FACE_A)
    try:
        status, body = _candidates(token, subject)
        assert status == 200, body
        assert body["match_band"] == "strong", body["match_band"]

        row = _sql("SELECT type, status, merged_into_id FROM identities WHERE id = :i",
                   {"i": subject})[0]
        assert str(row[0]).upper().endswith("UNKNOWN"), row
        assert str(row[1]).upper().endswith("ACTIVE"), row
        assert row[2] is None, "a merge happened without anyone asking for it"
        assert _sql("SELECT count(*) FROM identity_merges WHERE from_identity_id = :i",
                    {"i": subject}, fetch="scalar") == 0
    finally:
        _delete_identity(subject)
        _delete_identity(known)


# ---------------------------------------------------------------------------
# 9-10. failure still leaves a way forward
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["missing", "corrupt", "faceless", "none"])
def test_unusable_snapshots_warn_and_still_allow_promote_new(token, kind):
    """A 4xx here would strand the administrator: they could neither see
    matches nor create the person."""
    if kind == "missing":
        subject = _seed_unknown(TEST_PREFIX + "bad_missing", missing=True)
    elif kind == "corrupt":
        subject = _seed_unknown(TEST_PREFIX + "bad_corrupt", corrupt=True)
    elif kind == "faceless":
        subject = _seed_unknown(TEST_PREFIX + "bad_faceless", FACE_A)
        blank = os.path.join(STORAGE, QA_PIPELINE, "unknown",
                             f"{TEST_PREFIX}blank_{uuid_module.uuid4().hex}.jpg")
        import numpy as np
        try:
            import cv2
            cv2.imwrite(blank, np.full((200, 200, 3), 240, dtype=np.uint8))
        except Exception:                                      # noqa: BLE001
            pytest.skip("cv2 unavailable to synthesize a faceless image")
        _sql("UPDATE identities SET best_snapshot_path = :p WHERE id = :i",
             {"p": blank, "i": subject})
    else:
        subject = _seed_unknown(TEST_PREFIX + "bad_none")
        _sql("UPDATE identities SET best_snapshot_path = NULL WHERE id = :i",
             {"i": subject})

    try:
        status, body = _candidates(token, subject)
        assert status == 200, f"{kind}: expected a warning, got {status}: {body}"
        assert body["candidates"] == [], body
        assert body["warning"], f"{kind}: no warning explaining why"
        assert body["match_band"] == "none"

        # The fallback path must still work.
        status, promoted = _http(
            "POST", f"/api/admin/unknown/{subject}/promote", token=token,
            body={"display_name": TEST_PREFIX + f"after_{kind}", "person_code": None,
                  "decision": "create_new"})
        assert status == 200 and promoted.get("success"), promoted
        assert promoted["identity"]["id"] == subject, "promote created a different identity"
    finally:
        _delete_identity(subject)


# ---------------------------------------------------------------------------
# 8-9. the two explicit exits
# ---------------------------------------------------------------------------

def test_choosing_a_match_runs_the_existing_merge_workflow(token):
    """Unknown = loser, known = winner, and the known person keeps its type —
    which is why this branch leaves the Known count unchanged."""
    known = _enroll(token, TEST_PREFIX + "mergewin", FACE_A)
    subject = _seed_unknown(TEST_PREFIX + "mergelose", FACE_A)
    try:
        status, body = _candidates(token, subject)
        assert status == 200 and body["candidates"], body
        target = body["candidates"][0]["identity_id"]
        assert target == known, body

        known_before = _sql("SELECT count(*) FROM identities WHERE type = 'KNOWN'",
                            fetch="scalar")

        status, merged = _http("POST", "/api/admin/identities/merge", token=token,
                               body={"from_identity_id": subject,
                                     "to_identity_id": target,
                                     "notes": "qa_pmc suggestion merge",
                                     "decision": "merge_existing",
                       "confirm_merge_risk": True})
        assert status == 200, merged

        loser = _sql("SELECT status, merged_into_id FROM identities WHERE id = :i",
                     {"i": subject})[0]
        assert str(loser[0]).upper().endswith("MERGED"), loser
        assert str(loser[1]) == str(target), loser

        winner_type = _sql("SELECT type FROM identities WHERE id = :i",
                           {"i": target}, fetch="scalar")
        assert str(winner_type).upper().endswith("KNOWN"), winner_type

        known_after = _sql("SELECT count(*) FROM identities WHERE type = 'KNOWN'",
                           fetch="scalar")
        assert known_after == known_before, "the Known population changed on a merge"

        # Provenance from the earlier integrity work must still be recorded.
        assert _sql("SELECT count(*) FROM identity_merges "
                    "WHERE from_identity_id = :i AND provenance IS NOT NULL",
                    {"i": subject}, fetch="scalar") == 1
    finally:
        _delete_identity(subject)
        _delete_identity(known)


def test_promote_as_new_preserves_the_same_identity(token):
    """Same row, same id, same embeddings and appearances — never a second person."""
    subject = _seed_unknown(TEST_PREFIX + "newperson", FACE_A)
    try:
        before = _sql("SELECT (SELECT count(*) FROM identity_appearances "
                      "        WHERE identity_id = :i), "
                      "       (SELECT count(*) FROM identity_embeddings "
                      "        WHERE identity_id = :i), "
                      "       (SELECT first_seen_at FROM identities WHERE id = :i)",
                      {"i": subject})[0]
        total_before = _sql("SELECT count(*) FROM identities", fetch="scalar")

        status, body = _http("POST", f"/api/admin/unknown/{subject}/promote",
                             token=token,
                             body={"display_name": TEST_PREFIX + "newperson_named",
                                   "person_code": None, "decision": "create_new"})
        assert status == 200 and body.get("success"), body
        assert body["identity"]["id"] == subject

        row = _sql("SELECT type, status, display_name FROM identities WHERE id = :i",
                   {"i": subject})[0]
        assert str(row[0]).upper().endswith("KNOWN"), row
        assert str(row[1]).upper().endswith("PROMOTED"), row
        assert row[2] == TEST_PREFIX + "newperson_named", row

        after = _sql("SELECT (SELECT count(*) FROM identity_appearances "
                     "        WHERE identity_id = :i), "
                     "       (SELECT count(*) FROM identity_embeddings "
                     "        WHERE identity_id = :i), "
                     "       (SELECT first_seen_at FROM identities WHERE id = :i)",
                     {"i": subject})[0]
        assert tuple(after) == tuple(before), (
            f"promotion disturbed history: before={tuple(before)} after={tuple(after)}")
        assert _sql("SELECT count(*) FROM identities", fetch="scalar") == total_before, (
            "promotion created a second identity")
    finally:
        _delete_identity(subject)


# ---------------------------------------------------------------------------
# 12-13. nothing else moved
# ---------------------------------------------------------------------------

def test_existing_search_defaults_are_untouched():
    """scope already defaulted to 'both'; this feature must not have narrowed it
    for the pages that depend on it."""
    from backend.core.advanced_search import AdvancedSearchService

    signature = inspect.signature(AdvancedSearchService.search_multi_face)
    assert signature.parameters["scope"].default == "both"
    assert signature.parameters["check_watchlist"].default is True

    with open("/app/backend/routes/advanced_search.py", encoding="utf-8") as handle:
        route_source = handle.read()
    assert 'scope: str = Form(default="both"' in route_source


def test_the_new_ui_is_csp_safe_and_registered():
    """Dynamically rendered rows: inline handlers are blocked, so the merge
    button must dispatch through the Actions registry."""
    with open("/app/frontend/js/admin-unknown.js", encoding="utf-8") as handle:
        source = handle.read()
    assert "mergeIntoKnownCandidate: (el) =>" in source, "action not registered"
    assert "data-action', 'mergeIntoKnownCandidate'" in source.replace('"', "'")

    start = source.index("function buildCandidateRow")
    end = source.index("async function mergeIntoKnownCandidate")
    block = source[start:end]
    assert "onclick" not in block, "inline onclick is CSP-blocked on this page"
    assert "innerHTML" not in block, "candidate names are operator-supplied text"


def test_the_promote_modal_markup_is_extended_not_replaced():
    """One modal, both exits — a second modal would let the two paths drift."""
    with open("/app/frontend/admin/unknown.html", encoding="utf-8") as handle:
        html = handle.read()
    assert html.count('id="promote-modal"') == 1
    assert 'id="promote-candidates"' in html
    assert 'id="promote-candidates-warning"' in html
    assert 'id="promote-name"' in html, "the create-new path must remain"
    # The page's cache-buster is pinned by test_dashboard_system.py.
    assert "?v=unknown-9" in html


# ---------------------------------------------------------------------------
# The promote press itself: candidates first, mutations never
# ---------------------------------------------------------------------------

def _js_function(name, source=None):
    """The body of one top-level JS function, by brace matching."""
    if source is None:
        with open("/app/frontend/js/admin-unknown.js", encoding="utf-8") as handle:
            source = handle.read()
    start = source.index("function " + name + "(")
    depth, opened, index = 0, False, start
    while index < len(source):
        if source[index] == "{":
            depth, opened = depth + 1, True
        elif source[index] == "}":
            depth -= 1
            if opened and depth == 0:
                return source[start:index + 1]
        index += 1
    raise AssertionError("could not delimit " + name)


def test_pressing_promote_requests_candidates_before_any_mutation():
    """Opening the modal is a LOOK. It may fetch suggestions and nothing else —
    no promote, no merge, no write of any kind."""
    body = _js_function("promoteIdentityModal")
    assert "loadPromoteCandidates(" in body, "promote does not request candidates"
    for mutating in ("/promote", "/merge", "method: 'POST'"):
        assert mutating not in body, "opening the modal reaches " + repr(mutating)


def test_the_candidate_request_is_a_get():
    """A GET is what makes 'looking changes nothing' visible from the outside."""
    body = _js_function("loadPromoteCandidates")
    assert "match-candidates" in body
    # fetch(url, {credentials}) with no method is a GET; any override is not.
    assert "method:" not in body, "the candidate lookup overrides the HTTP method"
    assert "body:" not in body, "the candidate lookup sends a request body"


# ---------------------------------------------------------------------------
# The decision, recorded
# ---------------------------------------------------------------------------

def _latest_details(action_type, identity_id):
    rows = _sql("SELECT action_details FROM identity_audit_log "
                "WHERE action_type = :a AND (identity_id = :i OR related_identity_id = :i) "
                "ORDER BY id DESC LIMIT 1", {"a": action_type, "i": identity_id})
    if not rows:
        return {}
    details = rows[0][0]
    return json.loads(details) if isinstance(details, str) else (details or {})


def test_promote_new_records_the_create_new_decision(token):
    subject = _seed_unknown(TEST_PREFIX + "dec_new", FACE_A)
    try:
        status, body = _http("POST", "/api/admin/unknown/" + subject + "/promote",
                             token=token,
                             body={"display_name": TEST_PREFIX + "dec_new_named",
                                   "person_code": None, "decision": "create_new"})
        assert status == 200 and body.get("success"), body
        details = _latest_details("promote", subject)
        assert details.get("decision") == "create_new", details
    finally:
        _delete_identity(subject)


def test_a_suggested_merge_records_the_merge_existing_decision(token):
    known = _enroll(token, TEST_PREFIX + "dec_win", FACE_A)
    subject = _seed_unknown(TEST_PREFIX + "dec_lose", FACE_A)
    try:
        status, body = _http("POST", "/api/admin/identities/merge", token=token,
                             body={"from_identity_id": subject,
                                   "to_identity_id": known,
                                   "notes": "qa_pmc decision merge",
                                   "decision": "merge_existing",
                       "confirm_merge_risk": True})
        assert status == 200, body
        details = _latest_details("merge", subject)
        assert details.get("decision") == "merge_existing", details
    finally:
        _delete_identity(subject)
        _delete_identity(known)


def test_a_merge_without_a_decision_is_refused_before_it_mutates(token):
    """The optional-decision compatibility mode is gone. A caller that omits it
    is rejected at the schema, so nothing is merged and nothing is audited."""
    known = _enroll(token, TEST_PREFIX + "nodec_win", FACE_A)
    subject = _seed_unknown(TEST_PREFIX + "nodec_lose", FACE_A)
    try:
        status, body = _http("POST", "/api/admin/identities/merge", token=token,
                             body={"from_identity_id": subject,
                                   "to_identity_id": known,
                                   "notes": "qa_pmc no decision"})
        assert status == 422, f"omitted decision was accepted: {status} {body}"

        # and critically: nothing happened
        row = _sql("SELECT status, merged_into_id FROM identities WHERE id = :i",
                   {"i": subject})[0]
        assert str(row[0]).upper().endswith("ACTIVE"), row
        assert row[1] is None, "a refused merge still moved the identity"
        assert _sql("SELECT count(*) FROM identity_merges WHERE from_identity_id = :i",
                    {"i": subject}, fetch="scalar") == 0
        assert _latest_details("merge", subject) == {}, "a refused merge was audited"
    finally:
        _delete_identity(subject)
        _delete_identity(known)


def test_a_promotion_without_a_decision_is_refused_before_it_mutates(token):
    subject = _seed_unknown(TEST_PREFIX + "nodec_promote", FACE_A)
    try:
        status, body = _http("POST", "/api/admin/unknown/" + subject + "/promote",
                             token=token,
                             body={"display_name": TEST_PREFIX + "nodec_named"})
        assert status == 422, f"omitted decision was accepted: {status} {body}"
        row = _sql("SELECT type, status FROM identities WHERE id = :i",
                   {"i": subject})[0]
        assert str(row[0]).upper().endswith("UNKNOWN"), row
        assert str(row[1]).upper().endswith("ACTIVE"), row
    finally:
        _delete_identity(subject)


@pytest.mark.parametrize("bogus", [
    "manual_merge",
    "delete_everything",
    "merge",
    "",
    "1; DROP TABLE users",
    None,
])
def test_invalid_decisions_are_refused_with_422_and_change_nothing(token, bogus):
    """Refused, not silently dropped. Dropping let a caller believe something
    was recorded that was not; a 422 says so, and says it before any mutation."""
    suffix = str(abs(hash(str(bogus))) % 9999)
    known = _enroll(token, TEST_PREFIX + "bogus_win_" + suffix, FACE_A)
    subject = _seed_unknown(TEST_PREFIX + "bogus_lose_" + suffix, FACE_A)
    try:
        body = {"from_identity_id": subject, "to_identity_id": known,
                "notes": "qa_pmc bogus decision"}
        if bogus is not None:
            body["decision"] = bogus
        status, response = _http("POST", "/api/admin/identities/merge",
                                 token=token, body=body)
        assert status == 422, f"{bogus!r} was accepted: {status} {response}"

        row = _sql("SELECT status, merged_into_id FROM identities WHERE id = :i",
                   {"i": subject})[0]
        assert str(row[0]).upper().endswith("ACTIVE"), (bogus, row)
        assert row[1] is None, (bogus, "a refused merge still moved the identity")
        assert _latest_details("merge", subject) == {}, (bogus, "audited anyway")
    finally:
        _delete_identity(subject)
        _delete_identity(known)


@pytest.mark.parametrize("raw,expected", [
    (" CREATE_NEW ", "create_new"),
    ("merge_existing", "merge_existing"),
    ("Merge_Existing", "merge_existing"),
])
def test_valid_decisions_are_normalized_then_stored(token, raw, expected):
    """Case and surrounding whitespace are operator typing, not forgery."""
    known = _enroll(token, TEST_PREFIX + "norm_win_" + expected[:4], FACE_A)
    subject = _seed_unknown(TEST_PREFIX + "norm_lose_" + expected[:4], FACE_A)
    try:
        if expected == "create_new":
            status, body = _http(
                "POST", "/api/admin/unknown/" + subject + "/promote", token=token,
                body={"display_name": TEST_PREFIX + "norm_named",
                      "person_code": None, "decision": raw})
            assert status == 200, body
            assert _latest_details("promote", subject).get("decision") == expected
        else:
            status, body = _http("POST", "/api/admin/identities/merge", token=token,
                                 body={"from_identity_id": subject,
                                       "to_identity_id": known,
                                       "notes": "qa_pmc normalize",
                                       "decision": raw,
                                       # the seeded unknown has no embeddings, so
                                       # the compatibility gate reports
                                       # "unavailable" — this test is about
                                       # decision normalization, not the gate
                                       "confirm_merge_risk": True})
            assert status == 200, body
            assert _latest_details("merge", subject).get("decision") == expected
    finally:
        _delete_identity(subject)
        _delete_identity(known)


def test_promote_refuses_a_merge_decision(token):
    """'Promote this as a merge' is not a coherent instruction."""
    subject = _seed_unknown(TEST_PREFIX + "wrongbranch", FACE_A)
    try:
        status, body = _http("POST", "/api/admin/unknown/" + subject + "/promote",
                             token=token,
                             body={"display_name": TEST_PREFIX + "wrongbranch_named",
                                   "person_code": None,
                                   "decision": "merge_existing",
                       "confirm_merge_risk": True})
        assert status == 422, body
        row = _sql("SELECT type FROM identities WHERE id = :i", {"i": subject})[0]
        assert str(row[0]).upper().endswith("UNKNOWN"), row
    finally:
        _delete_identity(subject)


def test_the_decision_enum_has_exactly_two_members():
    from backend.utils.identity_audit import (ALLOWED_DECISIONS, PromotionDecision,
                                              coerce_decision)

    assert set(ALLOWED_DECISIONS) == {"create_new", "merge_existing"}
    assert {m.value for m in PromotionDecision} == ALLOWED_DECISIONS

    assert coerce_decision(" CREATE_NEW ") == "create_new"
    assert coerce_decision("Merge_Existing") == "merge_existing"
    for bogus in (None, "", "nonsense", "merge", 12345, "'; --"):
        try:
            coerce_decision(bogus)
        except ValueError:
            continue
        raise AssertionError(f"{bogus!r} was accepted")


def test_no_repository_caller_omits_the_decision():
    """Every promote/merge request body in the repo must declare one."""
    import glob
    import os

    offenders = []
    for path in (glob.glob("/app/frontend/js/*.js") + glob.glob("/app/tests/*.py")
                 + glob.glob("/app/backend/routes/*.py") + glob.glob("/app/scripts/**/*.py",
                                                                    recursive=True)):
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        for index, line in enumerate(lines):
            if "identities/merge'" in line or 'identities/merge"' in line \
                    or "/promote`" in line or "/promote\"" in line:
                window = "\n".join(lines[index:index + 12])
                # Source-scanning assertions in THIS file quote the endpoints as
                # string literals; they issue no request, so they are not callers.
                if "assert" in window or "for mutating in" in window:
                    continue
                if ("body" in window or "JSON.stringify" in window) \
                        and "decision" not in window:
                    offenders.append(f"{os.path.basename(path)}:{index + 1}")
    assert not offenders, f"callers with no decision: {offenders}"


# ---------------------------------------------------------------------------
# "None of these"
# ---------------------------------------------------------------------------

def test_the_none_of_these_button_exists_and_is_csp_safe():
    with open("/app/frontend/admin/unknown.html", encoding="utf-8") as handle:
        html = handle.read()
    assert 'id="promote-none-of-these"' in html
    assert "NONE OF THESE" in html
    button_markup = html[html.index('id="promote-none-of-these"'):][:600]
    assert "onclick" not in button_markup, "inline onclick is CSP-blocked"

    with open("/app/frontend/js/admin-unknown.js", encoding="utf-8") as handle:
        js = handle.read()
    assert "getElementById('promote-none-of-these')" in js
    assert "addEventListener('click', chooseNoneOfThese)" in js


def test_none_of_these_switches_to_create_new_without_mutating():
    """It collapses the suggestions and focuses the name field. No request."""
    body = _js_function("chooseNoneOfThese")
    assert "promote-candidates-section" in body, "the candidate section is not collapsed"
    assert "promote-name" in body and ".focus()" in body, "the name field is not focused"
    for mutating in ("fetch(", "/promote", "/merge"):
        assert mutating not in body, "choosing 'none of these' reaches " + repr(mutating)
    assert "style.display = 'flex'" not in body, "it opened another modal"


def test_the_button_is_hidden_when_there_is_nothing_to_reject():
    """With no candidates the modal is already in create-new mode."""
    body = _js_function("loadPromoteCandidates")
    assert "setNoneOfTheseVisible(true)" in body
    assert "setNoneOfTheseVisible(false)" in body


def test_the_empty_state_uses_the_required_wording():
    body = _js_function("loadPromoteCandidates")
    assert "No similar known person was found." in body, (
        "the required empty-state wording is missing")
    assert "No enrolled person matches this face" not in body, "old wording still present"
