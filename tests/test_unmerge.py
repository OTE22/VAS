"""Unmerge: reverse ONE pair merge, using only what that merge recorded.

    docker exec face_recognition_api python -m pytest tests/test_unmerge.py -v

Merge became reversible when it started recording provenance. Reversing it is
where the danger is, because provenance describes the world as the merge left
it — not as it is now. Between a merge and its reversal an administrator can
re-point a photo, promote one to primary, delete one, or merge the winner
again. Restoring blindly would overwrite all of that.

So the operation is built as two halves with a hard line between them: verify
everything, then mutate. Every refusal is raised from the verification half,
which is what makes "no partial changes on refusal" structural rather than a
promise. These tests assert the refusals AND that nothing moved.

The three properties worth stating plainly, each pinned below:

  * restoration reads the recorded id lists, never "everything the winner
    owns" — otherwise it would steal every row the winner gained after;
  * the reversal marker and the restoration commit in ONE transaction, so a
    second unmerge is refused from durable state rather than from a guess;
  * files are unlinked only AFTER the commit, and only files the merge itself
    created (created_by_merge). A rollback must never leave the database
    pointing at a file that is already gone.

House pattern from test_promote_merge_integrity.py: HTTP against the live app,
direct SQL for ground truth, qa_ prefix + module cleanup.
"""

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
FACES_DIR = "/app/storage/faces"
SNAP_DIR = "/app/storage/qa-unmerge"

TEST_PREFIX = "qa_unmrg_"
QA_PIPELINE = "qa-unmrg-cam"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _multipart(fields, files):
    boundary = "----qaunmrg" + uuid_module.uuid4().hex
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
    """A KNOWN identity with a real gallery: one image, marked primary."""
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


def _make_unknown(name):
    identity_id = str(_sql(
        "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
        " last_seen_at, created_at, updated_at, appearances_count) "
        "VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, now(), now(), now(), "
        "        now(), 0) RETURNING id", {"n": name}, fetch="scalar"))
    _sql("INSERT INTO pipelines (pipeline_id, total_detections, is_active, "
         " created_at, updated_at) VALUES (:p, 0, 1, now(), now()) "
         "ON CONFLICT (pipeline_id) DO NOTHING", {"p": QA_PIPELINE})
    _add_appearance(identity_id)
    return identity_id


def _add_appearance(identity_id):
    _sql("INSERT INTO pipelines (pipeline_id, total_detections, is_active, "
         " created_at, updated_at) VALUES (:p, 0, 1, now(), now()) "
         "ON CONFLICT (pipeline_id) DO NOTHING", {"p": QA_PIPELINE})
    new_id = _sql(
        "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, "
        " created_at) VALUES (:i, :p, now(), now()) RETURNING id",
        {"i": identity_id, "p": QA_PIPELINE}, fetch="scalar")
    _sql("UPDATE identities SET appearances_count = "
         "  (SELECT count(*) FROM identity_appearances WHERE identity_id = :i) "
         "WHERE id = :i", {"i": identity_id})
    return new_id


def _add_embedding(identity_id):
    import numpy as np
    vector = np.random.rand(512).astype("float32")
    vector /= np.linalg.norm(vector)
    literal = "[" + ",".join(f"{x:.6f}" for x in vector) + "]"
    return _sql(
        "INSERT INTO identity_embeddings (identity_id, pipeline_id, embedding, "
        " faiss_index_type, vector_index_sync_state, embedding_model_version, "
        " created_at) "
        "VALUES (:i, :p, CAST(:v AS vector), 'unknown', 'pending', 'w600k_r50', now()) "
        "RETURNING id", {"i": identity_id, "p": QA_PIPELINE, "v": literal},
        fetch="scalar")


def _add_face(identity_id):
    _sql("INSERT INTO pipelines (pipeline_id, total_detections, is_active, "
         " created_at, updated_at) VALUES (:p, 0, 1, now(), now()) "
         "ON CONFLICT (pipeline_id) DO NOTHING", {"p": QA_PIPELINE})
    detection_id = _sql(
        "INSERT INTO detections (pipeline_id, timestamp) VALUES (:p, now()) "
        "RETURNING id", {"p": QA_PIPELINE}, fetch="scalar")
    return _sql(
        "INSERT INTO faces (detection_id, name, similarity, identity_id) "
        "VALUES (:d, 'qa_unmerge', 0.9, :i) RETURNING id",
        {"d": detection_id, "i": identity_id}, fetch="scalar")


def _place_snapshot(fixture_path):
    """Copy a fixture under STORAGE_DIR and return its normalized relative path.

    The merge's snapshot-adoption path only looks at files inside the storage
    root — pending_absolute_path refuses anything else — so an unknown's
    best_snapshot_path has to live there to be adoptable at all.
    """
    os.makedirs(SNAP_DIR, exist_ok=True)
    name = f"{uuid_module.uuid4().hex}{os.path.splitext(fixture_path)[1]}"
    shutil.copy2(fixture_path, os.path.join(SNAP_DIR, name))
    return f"storage/qa-unmerge/{name}"


def _merge(token, from_id, to_id):
    return _http("POST", "/api/admin/identities/merge", token=token,
                 body={"from_identity_id": from_id, "to_identity_id": to_id,
                       "notes": "qa_unmrg", "decision": "merge_existing",
                       "confirm_merge_risk": True})


def _unmerge(token, merge_id, **kwargs):
    return _http("POST", f"/api/admin/identities/merges/{merge_id}/unmerge",
                 token=token, body=kwargs or {})


def _merge_id(from_id):
    return _sql("SELECT id FROM identity_merges WHERE from_identity_id = :i "
                "ORDER BY id DESC LIMIT 1", {"i": from_id}, fetch="scalar")


def _provenance(merge_id):
    raw = _sql("SELECT provenance FROM identity_merges WHERE id = :m",
               {"m": merge_id}, fetch="scalar")
    return raw if isinstance(raw, dict) else json.loads(raw or "{}")


def _reason(body):
    detail = body.get("detail")
    return detail.get("reason") if isinstance(detail, dict) else detail


def _abs(relative_path):
    return "/app/" + str(relative_path).lstrip("/")


# ---------------------------------------------------------------------------
# ground-truth snapshot: everything an unmerge could possibly disturb
# ---------------------------------------------------------------------------

def _state(*identity_ids):
    ids = [str(i) for i in identity_ids]
    def rows(sql):
        return [tuple(str(c) for c in r) for r in _sql(sql, {"ids": ids})]
    folders = []
    for identity_id in ids:
        folder = os.path.join(FACES_DIR, identity_id)
        if os.path.isdir(folder):
            folders += [os.path.join(identity_id, f) for f in sorted(os.listdir(folder))]
    return {
        "identities": rows(
            "SELECT id, status::text, coalesce(merged_into_id::text,''), "
            "       appearances_count, coalesce(best_snapshot_path,'') "
            "FROM identities WHERE id = ANY(CAST(:ids AS uuid[])) ORDER BY id"),
        "appearances": rows(
            "SELECT id, identity_id FROM identity_appearances "
            "WHERE identity_id = ANY(CAST(:ids AS uuid[])) ORDER BY id"),
        "embeddings": rows(
            "SELECT id, identity_id, coalesce(image_id::text,'') "
            "FROM identity_embeddings "
            "WHERE identity_id = ANY(CAST(:ids AS uuid[])) ORDER BY id"),
        "faces": rows(
            "SELECT id, identity_id FROM faces "
            "WHERE identity_id = ANY(CAST(:ids AS uuid[])) ORDER BY id"),
        "images": rows(
            "SELECT id, identity_id, storage_path, is_primary FROM identity_images "
            "WHERE identity_id = ANY(CAST(:ids AS uuid[])) ORDER BY id"),
        "files": sorted(folders),
    }


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------

def _scenario_gallery(token, tag):
    """KNOWN loser WITH its own gallery, merged into a KNOWN winner.

    Exercises the moved-image path: the row changes owner, the file is copied
    into the winner's folder, and the primary flag is cleared.
    """
    loser = _enroll(token, TEST_PREFIX + tag + "_loser", FACE_A)
    winner = _enroll(token, TEST_PREFIX + tag + "_winner", FACE_B)
    _add_appearance(loser)
    _add_embedding(loser)
    _add_face(loser)
    status, body = _merge(token, loser, winner)
    assert status == 200, body
    return loser, winner, _merge_id(loser)


def _scenario_adopt(token, tag, *, winner_owns_the_bytes=False):
    """UNKNOWN loser with only a best_snapshot_path, merged into a KNOWN winner.

    With winner_owns_the_bytes the adoption dedups onto the winner's existing
    row (created_by_merge False); otherwise the merge creates the row and the
    file itself (created_by_merge True).
    """
    winner = _enroll(token, TEST_PREFIX + tag + "_winner", FACE_B)
    loser = _make_unknown(TEST_PREFIX + tag + "_loser")
    source = FACE_B if winner_owns_the_bytes else FACE_A
    relative = _place_snapshot(source)
    _sql("UPDATE identities SET best_snapshot_path = :p WHERE id = :i",
         {"p": relative, "i": loser})
    _add_embedding(loser)
    _add_face(loser)
    status, body = _merge(token, loser, winner)
    assert status == 200, body
    merge_id = _merge_id(loser)
    adopted = _provenance(merge_id).get("adopted_snapshot")
    assert adopted, f"scenario needs an adoption; provenance said {_provenance(merge_id)}"
    assert adopted["created_by_merge"] is (not winner_owns_the_bytes), adopted
    return loser, winner, merge_id, adopted


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

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
    for (identity_id,) in _sql(
            "SELECT id FROM identities WHERE display_name LIKE :p",
            {"p": TEST_PREFIX + "%"}):
        _delete_identity(str(identity_id))
    _sql("DELETE FROM faces WHERE detection_id IN "
         "  (SELECT id FROM detections WHERE pipeline_id = :p)", {"p": QA_PIPELINE})
    _sql("DELETE FROM detections WHERE pipeline_id = :p", {"p": QA_PIPELINE})
    _sql("DELETE FROM identity_appearances WHERE pipeline_id = :p", {"p": QA_PIPELINE})
    _sql("DELETE FROM identity_embeddings WHERE pipeline_id = :p", {"p": QA_PIPELINE})
    _sql("DELETE FROM pipelines WHERE pipeline_id = :p", {"p": QA_PIPELINE})
    if os.path.isdir(SNAP_DIR):
        shutil.rmtree(SNAP_DIR, ignore_errors=True)


@pytest.fixture(scope="module", autouse=True)
def _clean_module():
    _cleanup_prefix()
    yield
    _cleanup_prefix()


@pytest.fixture(autouse=True)
def _clean_each():
    yield
    _cleanup_prefix()


# ===========================================================================
# 1. the happy path
# ===========================================================================

def test_unmerge_restores_the_loser_and_everything_the_merge_moved(token):
    loser, winner, merge_id = _scenario_gallery(token, "basic")

    merged = _sql("SELECT status::text, merged_into_id::text FROM identities "
                  "WHERE id = :i", {"i": loser})[0]
    assert merged == ("MERGED", winner), merged

    status, body = _unmerge(token, merge_id)
    assert status == 200, body
    assert body["success"] is True

    restored = _sql("SELECT status::text, merged_into_id FROM identities "
                    "WHERE id = :i", {"i": loser})[0]
    assert restored == ("ACTIVE", None), (
        f"the source identity came back as {restored}, not ACTIVE and unmerged")

    provenance = _provenance(merge_id)
    for table, key in (("identity_appearances", "appearance_ids"),
                       ("identity_embeddings", "embedding_ids"),
                       ("faces", "face_ids")):
        ids = provenance[key]
        assert ids, f"scenario recorded no {key}"
        back = _sql(f"SELECT count(*) FROM {table} WHERE id = ANY(:ids) "
                    f"AND identity_id = :i",
                    {"ids": ids, "i": loser}, fetch="scalar")
        assert back == len(ids), (
            f"{back}/{len(ids)} {table} rows returned to the source identity")

    assert _sql("SELECT count(*) FROM identity_images WHERE identity_id = :i",
                {"i": loser}, fetch="scalar") == 1, "the gallery did not come back"


def test_the_restored_status_is_the_recorded_one_not_a_guess(token):
    """A PROMOTED person merged away must come back PROMOTED, not ACTIVE."""
    loser, winner, _ = _scenario_gallery(token, "promoted")
    # rewind: undo the merge state, set PROMOTED, merge again
    _sql("UPDATE identities SET status = 'PROMOTED', merged_into_id = NULL "
         "WHERE id = :i", {"i": loser})
    _sql("DELETE FROM identity_merges WHERE from_identity_id = :i", {"i": loser})
    _sql("UPDATE identity_images SET identity_id = :l WHERE identity_id = :w "
         "AND source_type IS DISTINCT FROM 'upload_primary_marker' "
         "AND id = (SELECT min(id) FROM identity_images WHERE identity_id = :w)",
         {"l": loser, "w": winner})

    status, body = _merge(token, loser, winner)
    assert status == 200, body
    merge_id = _merge_id(loser)
    assert _provenance(merge_id)["loser_status"].upper() == "PROMOTED"

    status, body = _unmerge(token, merge_id)
    assert status == 200, body
    assert _sql("SELECT status::text FROM identities WHERE id = :i",
                {"i": loser}, fetch="scalar") == "PROMOTED", (
        "the source came back ACTIVE — a guessed status would silently demote "
        "a promoted person or resurrect a retired one")


# ===========================================================================
# 2. what the merge created vs what the winner already owned
# ===========================================================================

def test_an_image_the_merge_created_is_removed_row_and_file(token):
    loser, winner, merge_id, adopted = _scenario_adopt(token, "created")
    absolute = _abs(adopted["new_path"])
    assert os.path.isfile(absolute), "scenario precondition: the file exists"

    status, body = _unmerge(token, merge_id)
    assert status == 200, body

    assert _sql("SELECT count(*) FROM identity_images WHERE id = :i",
                {"i": adopted["image_id"]}, fetch="scalar") == 0, (
        "the row this merge created survived the reversal")
    assert not os.path.isfile(absolute), (
        "the file this merge created survived the reversal")
    assert body["adopted_image_removed"]["image_id"] == adopted["image_id"]
    assert body["files_deleted"] == 1


def test_an_image_the_winner_already_owned_is_never_deleted(token):
    """created_by_merge=False. The merge only pointed at this row; the winner
    owned it before and must still own it after."""
    loser, winner, merge_id, adopted = _scenario_adopt(
        token, "preowned", winner_owns_the_bytes=True)
    image_id = adopted["image_id"]
    before = _sql("SELECT identity_id::text, storage_path, is_primary "
                  "FROM identity_images WHERE id = :i", {"i": image_id})[0]

    status, body = _unmerge(token, merge_id)
    assert status == 200, body

    after = _sql("SELECT identity_id::text, storage_path, is_primary "
                 "FROM identity_images WHERE id = :i", {"i": image_id})
    assert after, "an image the winner owned BEFORE the merge was deleted"
    assert after[0] == before, f"{before} became {after[0]}"
    assert os.path.isfile(_abs(before[1])), "the winner's own file was unlinked"
    assert body["adopted_image_removed"] is None
    assert body["files_deleted"] == 0


def test_rows_the_winner_gained_after_the_merge_stay_with_the_winner(token):
    """Restoration reads the recorded id lists. Anything acquired afterwards is
    invisible to it — which is the whole reason it must not select by owner."""
    loser, winner, merge_id = _scenario_gallery(token, "postgain")

    later_appearance = _add_appearance(winner)
    later_embedding = _add_embedding(winner)
    later_face = _add_face(winner)

    status, body = _unmerge(token, merge_id)
    assert status == 200, body

    for table, row_id in (("identity_appearances", later_appearance),
                          ("identity_embeddings", later_embedding),
                          ("faces", later_face)):
        owner = _sql(f"SELECT identity_id::text FROM {table} WHERE id = :r",
                     {"r": row_id}, fetch="scalar")
        assert owner == winner, (
            f"{table} row {row_id}, acquired AFTER the merge, was handed to the "
            f"source identity — restoration is selecting by owner, not by id")


# ===========================================================================
# 3. refusals — each asserts the reason AND that nothing moved
# ===========================================================================

def _refusal_leaves_everything_alone(token, merge_id, loser, winner, reason,
                                     http_status=409):
    before = _state(loser, winner)
    status, body = _unmerge(token, merge_id)
    assert status == http_status, (status, body)
    assert _reason(body) == reason, body
    assert _state(loser, winner) == before, (
        f"a refusal ({reason}) still changed state")


def test_a_merge_without_provenance_is_refused(token):
    loser, winner, merge_id = _scenario_gallery(token, "noprov")
    _sql("UPDATE identity_merges SET provenance = NULL WHERE id = :m",
         {"m": merge_id})
    _refusal_leaves_everything_alone(token, merge_id, loser, winner,
                                     "provenance_missing")


def test_a_merge_without_a_recorded_loser_status_is_refused(token):
    """Restoring to a guessed ACTIVE would resurrect a retired identity."""
    loser, winner, merge_id = _scenario_gallery(token, "nostatus")
    _sql("UPDATE identity_merges SET provenance = provenance - 'loser_status' "
         "WHERE id = :m", {"m": merge_id})
    _refusal_leaves_everything_alone(token, merge_id, loser, winner,
                                     "provenance_missing_loser_status")


def test_a_batch_merge_row_is_refused(token):
    """Multi-merge applies target-level effects with no per-source record, so
    one source cannot be reversed in isolation."""
    loser, winner, merge_id = _scenario_gallery(token, "batch")
    _sql("UPDATE identity_merges SET provenance = "
         "  jsonb_set(provenance, '{multi_merge}', '{\"batch_size\": 3}'::jsonb) "
         "WHERE id = :m", {"m": merge_id})
    _refusal_leaves_everything_alone(token, merge_id, loser, winner,
                                     "multi_merge_unsupported")


def test_an_unknown_merge_id_is_a_404(token):
    status, body = _unmerge(token, 987654321)
    assert status == 404, (status, body)
    assert _reason(body) == "merge_not_found", body


def test_a_source_whose_state_moved_on_is_refused_with_its_own_reason(token):
    """loser_not_merged means something else changed the row — a different
    situation from already_unmerged, and it must not share its message."""
    loser, winner, merge_id = _scenario_gallery(token, "notmerged")
    _sql("UPDATE identities SET status = 'INACTIVE' WHERE id = :i", {"i": loser})
    before = _state(loser, winner)
    status, body = _unmerge(token, merge_id)
    assert status == 409, (status, body)
    assert _reason(body) == "loser_not_merged", body
    assert _state(loser, winner) == before


# ---- gallery conflict detection -------------------------------------------

def test_a_moved_image_repointed_after_the_merge_is_refused(token):
    loser, winner, merge_id = _scenario_gallery(token, "repointed")
    image_id = _provenance(merge_id)["images"][0]["id"]
    _sql("UPDATE identity_images SET storage_path = :p WHERE id = :i",
         {"p": "storage/faces/somewhere-else/image_009.jpg", "i": image_id})
    _refusal_leaves_everything_alone(token, merge_id, loser, winner,
                                     "post_merge_gallery_conflict")


def test_a_moved_image_deleted_after_the_merge_is_refused(token):
    """Refuse, rather than restore the rows that DO remain."""
    loser, winner, merge_id = _scenario_gallery(token, "deleted")
    image_id = _provenance(merge_id)["images"][0]["id"]
    _sql("UPDATE identity_embeddings SET image_id = NULL WHERE image_id = :i",
         {"i": image_id})
    _sql("DELETE FROM identity_images WHERE id = :i", {"i": image_id})
    _refusal_leaves_everything_alone(token, merge_id, loser, winner,
                                     "post_merge_gallery_conflict")


def test_a_moved_image_now_owned_by_a_third_party_is_refused(token):
    loser, winner, merge_id = _scenario_gallery(token, "thirdparty")
    stranger = _make_unknown(TEST_PREFIX + "thirdparty_stranger")
    image_id = _provenance(merge_id)["images"][0]["id"]
    _sql("UPDATE identity_images SET identity_id = :s WHERE id = :i",
         {"s": stranger, "i": image_id})
    before = _state(loser, winner)
    status, body = _unmerge(token, merge_id)
    assert status == 409, (status, body)
    assert _reason(body) == "post_merge_gallery_conflict", body
    assert _state(loser, winner) == before
    assert _sql("SELECT identity_id::text FROM identity_images WHERE id = :i",
                {"i": image_id}, fetch="scalar") == stranger, (
        "the third party's image was taken from them")


def test_a_moved_image_promoted_to_primary_after_the_merge_is_refused(token):
    """The merge demoted every moved row unconditionally, so a primary flag
    here is an administrator's later decision."""
    loser, winner, merge_id = _scenario_gallery(token, "promoted_img")
    image_id = _provenance(merge_id)["images"][0]["id"]
    _sql("UPDATE identity_images SET is_primary = false WHERE identity_id = :w",
         {"w": winner})
    _sql("UPDATE identity_images SET is_primary = true WHERE id = :i",
         {"i": image_id})
    _refusal_leaves_everything_alone(token, merge_id, loser, winner,
                                     "post_merge_gallery_conflict")


def test_restoring_a_primary_is_refused_when_the_source_already_holds_one(token):
    """uq_identity_image_one_primary permits at most one. Refuse rather than
    demote whatever the source now has."""
    loser, winner, merge_id = _scenario_gallery(token, "twoprimary")
    assert any(r["was_primary"] for r in _provenance(merge_id)["images"]), (
        "scenario precondition: the merge moved the source's primary")
    _sql("INSERT INTO identity_images (identity_id, storage_path, file_checksum, "
         " is_primary, source_type, processing_status, created_at, updated_at) "
         "VALUES (:i, :p, :c, true, 'upload', 'completed', now(), now())",
         {"i": loser, "p": f"storage/faces/{loser}/image_099.jpg",
          "c": "qa_unmrg_" + uuid_module.uuid4().hex[:24]})

    before = _state(loser, winner)
    status, body = _unmerge(token, merge_id)
    assert status == 409, (status, body)
    assert _reason(body) == "post_merge_gallery_conflict", body
    assert _state(loser, winner) == before
    assert _sql("SELECT count(*) FROM identity_images WHERE identity_id = :i "
                "AND is_primary", {"i": loser}, fetch="scalar") == 1, (
        "the one-primary-per-identity constraint was violated or the existing "
        "primary was demoted")


def test_the_happy_path_still_restores_after_all_those_checks(token):
    """Proof the verification is not vacuously refusing everything."""
    loser, winner, merge_id = _scenario_gallery(token, "notvacuous")
    status, body = _unmerge(token, merge_id)
    assert status == 200, body
    assert _sql("SELECT status::text FROM identities WHERE id = :i",
                {"i": loser}, fetch="scalar") == "ACTIVE"
    assert body["restored"]["appearances"] >= 1
    assert body["restored_images"] == 1


# ===========================================================================
# 4. idempotency — the durable reversal marker
# ===========================================================================

def test_a_second_unmerge_is_refused_and_changes_absolutely_nothing(token):
    loser, winner, merge_id = _scenario_gallery(token, "idem")
    status, body = _unmerge(token, merge_id)
    assert status == 200, body

    after_first = _state(loser, winner)
    status, body = _unmerge(token, merge_id)
    assert status == 409, (status, body)
    assert _reason(body) == "already_unmerged", body
    message = body["detail"]["message"]
    assert "admin" in message and "reversed" in message, (
        f"the refusal should say when and by whom: {message}")
    assert _state(loser, winner) == after_first, (
        "the second unmerge attempt changed state")

    assert _sql("SELECT count(*) FROM identity_audit_log WHERE "
                "action_type = 'unmerge' AND action_details ->> 'merge_id' = :m",
                {"m": str(merge_id)}, fetch="scalar") == 1, (
        "a second unmerge audit row was written for the same merge")


def test_the_marker_outranks_the_source_identitys_current_status(token):
    """loser.status is not the idempotency test. Change it independently and
    the second attempt must still report already_unmerged, not a confusing
    state error."""
    loser, winner, merge_id = _scenario_gallery(token, "markerwins")
    assert _unmerge(token, merge_id)[0] == 200

    # An administrator retires the restored identity afterwards.
    _sql("UPDATE identities SET status = 'INACTIVE' WHERE id = :i", {"i": loser})

    status, body = _unmerge(token, merge_id)
    assert status == 409, (status, body)
    assert _reason(body) == "already_unmerged", (
        f"the marker lost to a status check: {body}")


def test_both_the_merge_and_its_reversal_stay_readable(token):
    loser, winner, merge_id = _scenario_gallery(token, "trail")
    assert _unmerge(token, merge_id)[0] == 200

    assert _sql("SELECT count(*) FROM identity_merges WHERE id = :m",
                {"m": merge_id}, fetch="scalar") == 1, (
        "the merge record was deleted; history must stay immutable")
    assert _provenance(merge_id).get("appearance_ids") is not None, (
        "the merge's provenance was edited")

    rows = _sql("SELECT action_type, success, username, identity_id::text, "
                "       related_identity_id::text, action_details "
                "FROM identity_audit_log "
                "WHERE action_type IN ('merge','unmerge') "
                "  AND (identity_id = :w OR related_identity_id = :w) "
                "ORDER BY created_at", {"w": winner})
    kinds = [r[0] for r in rows]
    assert "merge" in kinds and "unmerge" in kinds, (
        f"the trail should show both sides of the story: {kinds}")

    reversal = [r for r in rows if r[0] == "unmerge"][0]
    assert reversal[1] is True and reversal[2] == "admin"
    assert reversal[3] == winner and reversal[4] == loser
    details = reversal[5] if isinstance(reversal[5], dict) else json.loads(reversal[5])
    assert details["merge_id"] == str(merge_id), (
        "the marker key must be a plain string under 'merge_id' — the "
        "idempotency check reads action_details ->> 'merge_id'")
    assert details["restored"]["appearances"] >= 1


# ===========================================================================
# 5. transaction and filesystem safety
# ===========================================================================

def test_a_rollback_restores_nothing_and_deletes_no_file(token):
    """The marker and the restoration are ONE transaction.

    Driven in-process so the transaction can be rolled back deliberately — the
    HTTP route always commits. This is the property that matters: if the commit
    fails, the loser stays merged, the winner keeps every row, no file is
    unlinked, and no marker exists to block a later retry.
    """
    loser, winner, merge_id, adopted = _scenario_adopt(token, "rollback")
    absolute = _abs(adopted["new_path"])
    before = _state(loser, winner)
    assert os.path.isfile(absolute)

    staged: list = []

    async def _run():
        from sqlalchemy import text as sa_text
        from db_connection import db_manager
        from backend.core.identity_service import IdentityService
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            try:
                admin_id = (await db.execute(sa_text(
                    "SELECT id FROM users WHERE username = 'admin'"))).scalar()
                await IdentityService().unmerge_identity(
                    merge_id, user_id=admin_id, username="admin", db=db,
                    files_to_delete=staged)
                # everything is pending in this transaction; throw it away
                await db.rollback()
            except Exception:
                await db.rollback()
                raise
    run_async(_run())

    assert staged, "the adopted file should have been STAGED for deletion"
    assert os.path.isfile(absolute), (
        "a staged file was unlinked before the commit — a rollback would leave "
        "the database pointing at a file that no longer exists")
    assert _state(loser, winner) == before, "the rollback did not undo the writes"
    assert _sql("SELECT count(*) FROM identity_audit_log WHERE "
                "action_type = 'unmerge' AND action_details ->> 'merge_id' = :m",
                {"m": str(merge_id)}, fetch="scalar") == 0, (
        "a reversal marker survived a rolled-back unmerge, so a legitimate "
        "retry would be refused as already_unmerged")

    # and the retry the marker would otherwise have blocked still works
    status, body = _unmerge(token, merge_id)
    assert status == 200, body


def test_a_file_another_row_still_references_is_not_deleted(token):
    """The row goes; the file stays, because something else points at it."""
    loser, winner, merge_id, adopted = _scenario_adopt(token, "shared")
    absolute = _abs(adopted["new_path"])
    other = _make_unknown(TEST_PREFIX + "shared_other")
    _sql("INSERT INTO identity_images (identity_id, storage_path, file_checksum, "
         " is_primary, source_type, processing_status, created_at, updated_at) "
         "VALUES (:i, :p, :c, false, 'upload', 'completed', now(), now())",
         {"i": other, "p": adopted["new_path"],
          "c": "qa_unmrg_" + uuid_module.uuid4().hex[:24]})

    status, body = _unmerge(token, merge_id)
    assert status == 200, body
    assert body["files_deleted"] == 0, "a file another row references was deleted"
    assert os.path.isfile(absolute), "the shared file was unlinked"
    assert _sql("SELECT count(*) FROM identity_images WHERE id = :i",
                {"i": adopted["image_id"]}, fetch="scalar") == 0, (
        "the adopted row should still be removed")


def test_deleting_the_adopted_primary_is_reported_not_back_filled(token):
    """Removing the winner's only primary is legal — at most one, not exactly
    one — and choosing a replacement is an administrator's call."""
    loser, winner, merge_id, adopted = _scenario_adopt(token, "lastprimary")
    _sql("UPDATE identity_images SET is_primary = false WHERE identity_id = :w "
         "AND id <> :a", {"w": winner, "a": adopted["image_id"]})
    _sql("UPDATE identity_images SET is_primary = true WHERE id = :a",
         {"a": adopted["image_id"]})
    _sql("UPDATE identity_merges SET provenance = jsonb_set("
         "  provenance, '{adopted_snapshot,became_primary}', 'true'::jsonb) "
         "WHERE id = :m", {"m": merge_id})

    status, body = _unmerge(token, merge_id)
    assert status == 200, body
    assert body["target_has_primary_image"] is False, (
        "the response must say the target now has no primary image")
    assert _sql("SELECT count(*) FROM identity_images WHERE identity_id = :w "
                "AND is_primary", {"w": winner}, fetch="scalar") == 0, (
        "a replacement primary was chosen automatically")


# ===========================================================================
# 6. authorization
# ===========================================================================

def test_unmerge_requires_authentication():
    status, _ = _unmerge(None, 1)
    assert status == 401, (
        "an unauthenticated caller reached the unmerge endpoint")
