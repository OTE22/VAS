"""ADD PERSON — end-to-end audit: scenarios + database-fill correctness.

Audit-only. Changes no product code; asserts observed behaviour and observed
SQL, and records evidence to /app/logs/audit/add-person/evidence.json.

Named `audit_*` so a normal `pytest tests/` run does not collect it. Run it
explicitly (pytest collects an explicitly-passed path regardless of the
python_files pattern):

    docker exec -w /app <api> python -m pytest tests/audit_add_person_matrix.py -v

WHAT THIS COVERS, AND WHAT IT DELIBERATELY DOES NOT
The existing suites already prove the happy paths and the review gate:
  tests/test_identity_multi_image_enrollment.py  — create/extend/duplicate,
      no-face, cropped-face, two-faces, rollback, auth
  tests/test_enrollment_decision_gate.py         — the 202 decision flow,
      token single-use/expiry/cross-admin, parked-upload atomicity
Those are run as part of this audit rather than duplicated here. This module
covers what nothing covers today:
  * every COLUMN written by an enrollment (the "is the database filled
    correctly" question), not merely row counts
  * oversized file, non-image bytes, and name edge cases
  * concurrency: same new name, same photo, and two first-photos in parallel
  * the specific data-correctness suspicions found by reading the code
    (fabricated sighting timestamps, permanently-NULL quality, missing audit row)

Everything it creates is prefixed qa_addaudit_ and removed on teardown, per the
repo convention that a test may delete only rows it created.
"""
import hashlib
import io
import json
import os
import uuid as uuid_module
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import run_on_shared_loop as run_async

import urllib.error
import urllib.request

BASE = "http://localhost:8000"
FIXTURES = "/app/tests/fixtures/faces"
STORAGE_DIR = "/app/storage"
FACES_DIR = f"{STORAGE_DIR}/faces"

FACE_A = f"{FIXTURES}/face_a.jpg"
FACE_B = f"{FIXTURES}/face_b.jpg"
FACE_C = f"{FIXTURES}/face_c.png"
CROPPED_FACE = f"{FIXTURES}/cropped_face.jpg"
TWO_FACES = f"{FIXTURES}/two_faces.jpg"

TEST_PREFIX = "qa_addaudit_"
EVIDENCE_DIR = "/app/logs/audit/add-person"

# Collected as the run proceeds and written out at teardown, so the report can
# quote observed values instead of restating assertions.
EVIDENCE = {}


def _record(name, payload):
    EVIDENCE[name] = payload
    return payload


# ---------------------------------------------------------------------------
# helpers (same shapes as tests/test_identity_multi_image_enrollment.py)
# ---------------------------------------------------------------------------

def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _multipart(fields, files):
    boundary = "----qaboundary" + uuid_module.uuid4().hex
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


def _http(method, path, *, body=None, token=None, headers=None,
          fields=None, files=None, timeout=300):
    data = None
    hdrs = dict(headers or {})
    if files is not None:
        data, content_type = _multipart(fields or {}, files)
        hdrs["Content-Type"] = content_type
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in hdrs.items():
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
    """Direct SQL on the shared loop (asyncpg is loop-bound)."""
    from sqlalchemy import text
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            result = await db.execute(text(statement), params or {})
            if fetch == "all":
                return [dict(row._mapping) for row in result]
            if fetch == "one":
                row = result.first()
                return dict(row._mapping) if row else None
            if fetch == "scalar":
                return result.scalar()
            await db.commit()
            return None

    return run_async(_run())


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         body={"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


def _upload(token, name, path=FACE_A, is_face_image=False, filename=None,
            on_decision="create_new", payload_override=None,
            content_type="image/jpeg"):
    """Enroll by name through the real endpoint, answering the review gate.

    Returns (status, body). When the gate parks the upload (202) the answer
    given by `on_decision` is submitted and ITS response is returned, so
    callers see the final outcome.
    """
    payload = payload_override if payload_override is not None else _read(path)
    status, body = _http(
        "POST", "/api/upload-person", token=token,
        fields={"person_name": name, "is_face_image": str(is_face_image).lower()},
        files={"photo": (filename or os.path.basename(path), payload, content_type)})
    if status == 202 and body.get("decision_required") and on_decision:
        answer = {"action": on_decision, "upload_token": body["upload_token"]}
        if on_decision == "create_new":
            answer["display_name"] = body.get("display_name", name)
            answer["confirm_create_new"] = True
        elif on_decision == "add_to_existing":
            answer["identity_id"] = body["candidate_identities"][0]["identity_id"]
        return _http("POST", "/api/enrollment/confirm", token=token, body=answer,
                     headers={"X-Requested-With": "XMLHttpRequest"})
    return status, body


def _identity_ids_for(prefix):
    rows = _sql("SELECT id::text AS id FROM identities WHERE display_name LIKE :p",
                {"p": prefix + "%"})
    return [row["id"] for row in rows]


def _cleanup_prefix():
    import shutil
    for identity_id in _identity_ids_for(TEST_PREFIX):
        # identity_audit_log FKs identities in both directions with no ON
        # DELETE action (purge_face_storage.py:63 calls these the RESTRICT
        # four), so the audit rows enrollment now writes must go first.
        _sql("DELETE FROM identity_audit_log WHERE identity_id = CAST(:i AS uuid)"
             "   OR related_identity_id = CAST(:i AS uuid)",
             {"i": identity_id}, fetch="none")
        _sql("DELETE FROM identity_embeddings WHERE identity_id = CAST(:i AS uuid)",
             {"i": identity_id}, fetch="none")
        _sql("DELETE FROM identity_images WHERE identity_id = CAST(:i AS uuid)",
             {"i": identity_id}, fetch="none")
        _sql("DELETE FROM identities WHERE id = CAST(:i AS uuid)",
             {"i": identity_id}, fetch="none")
        folder = os.path.join(FACES_DIR, identity_id)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(scope="module", autouse=True)
def _clean_module():
    _cleanup_prefix()
    yield
    _cleanup_prefix()
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    with open(os.path.join(EVIDENCE_DIR, "evidence.json"), "w") as handle:
        json.dump(EVIDENCE, handle, indent=2, default=str)


def _unique(label):
    return f"{TEST_PREFIX}{label}_{uuid_module.uuid4().hex[:8]}"


def _counts():
    """Rows in the tables an enrollment must NOT touch."""
    return {
        table: _sql(f"SELECT count(*) FROM {table}", fetch="scalar")
        for table in ("faces", "detections", "identity_appearances")
    }


# ---------------------------------------------------------------------------
# §3 — is the database filled correctly?
# ---------------------------------------------------------------------------

def test_enrollment_writes_every_column_correctly(token):
    """The core question: after one upload, is every row correct?"""
    name = _unique("cols")
    untouched_before = _counts()

    status, body = _upload(token, name, FACE_A)
    assert status == 200, body
    identity_id = body["identity_id"]

    identity = _sql(
        """SELECT id::text AS id, type::text AS type, status::text AS status,
                  display_name, person_code, person_code_key,
                  first_seen_at, last_seen_at, appearances_count,
                  best_snapshot_path, merged_into_id, created_at, updated_at
             FROM identities WHERE id = CAST(:i AS uuid)""",
        {"i": identity_id}, fetch="one")

    image = _sql(
        """SELECT id, storage_path, original_filename, file_checksum, content_type,
                  file_size, width, height, is_primary, source_type,
                  processing_status, failure_reason, quality_score,
                  quality_scorer_version, created_by
             FROM identity_images WHERE identity_id = CAST(:i AS uuid)""",
        {"i": identity_id}, fetch="one")

    embedding = _sql(
        """SELECT id, image_id, detection_id, pipeline_id::text AS pipeline_id,
                  quality, quality_scorer_version, faiss_index_type,
                  embedding_model_version, vector_index_sync_state,
                  vector_dims(embedding) AS dims,
                  (embedding <-> embedding) AS self_distance,
                  sqrt(( SELECT sum(v*v) FROM unnest(embedding::real[]) AS v)) AS l2_norm
             FROM identity_embeddings WHERE identity_id = CAST(:i AS uuid)""",
        {"i": identity_id}, fetch="one")

    _record("db_fill_single_enrollment",
            {"identity": identity, "image": image, "embedding": embedding})

    # --- identities -------------------------------------------------------
    assert identity["type"].upper().endswith("KNOWN"), identity
    assert identity["status"].upper().endswith("ACTIVE"), identity
    assert identity["display_name"] == name
    assert identity["merged_into_id"] is None
    assert identity["best_snapshot_path"] == image["storage_path"], (
        "best_snapshot_path must point at the primary image")

    # --- identity_images --------------------------------------------------
    assert image["storage_path"] == f"storage/faces/{identity_id}/image_001.jpg", image
    assert "\\" not in image["storage_path"], "path must be forward-slashed"
    assert image["is_primary"] is True
    assert image["processing_status"] == "completed", image
    assert image["source_type"] == "upload", image
    assert image["failure_reason"] is None
    assert image["created_by"] is not None, "the acting admin must be recorded"
    assert image["file_size"] == len(_read(FACE_A))
    assert image["width"] and image["height"], "decoded dimensions must be stored"

    # the checksum must match the bytes actually on disk
    absolute = os.path.join(STORAGE_DIR, image["storage_path"].split("storage/", 1)[1])
    assert os.path.isfile(absolute), f"file missing on disk: {absolute}"
    on_disk = hashlib.sha256(_read(absolute)).hexdigest()
    assert image["file_checksum"] == on_disk, "checksum disagrees with the stored file"
    assert on_disk == hashlib.sha256(_read(FACE_A)).hexdigest(), (
        "stored bytes differ from the uploaded bytes")

    # --- identity_embeddings ---------------------------------------------
    assert embedding["image_id"] == image["id"], "embedding must be linked to its image"
    assert embedding["detection_id"] is None, "an enrollment is not a detection"
    assert embedding["pipeline_id"] is None, "an enrollment is not a camera sighting"
    assert embedding["dims"] == 512, embedding
    assert abs(float(embedding["l2_norm"]) - 1.0) < 1e-3, (
        f"embedding must be unit-norm, got {embedding['l2_norm']}")
    assert embedding["faiss_index_type"] == "known", embedding
    assert embedding["embedding_model_version"], "model version must be recorded"
    assert embedding["vector_index_sync_state"] == "synced", embedding

    # --- tables an enrollment must not touch ------------------------------
    assert _counts() == untouched_before, (
        "enrollment wrote to faces/detections/identity_appearances")


def test_enrolled_person_is_actually_findable(token):
    """Stored is not the same as indexed: prove the vector path can find them."""
    name = _unique("findable")
    status, body = _upload(token, name, FACE_A)
    assert status == 200, body
    identity_id = body["identity_id"]

    status, search = _http(
        "POST", "/api/search/by-image", token=token,
        fields={"threshold": "0.3", "limit": "10"},
        files={"image": ("probe.jpg", _read(FACE_A), "image/jpeg")})
    _record("search_reachability", {"status": status, "body": search})
    assert status == 200, search

    blob = json.dumps(search)
    assert identity_id in blob, (
        "a person enrolled seconds ago is not returned by a search for their own photo")


# ---------------------------------------------------------------------------
# §4 — data-correctness suspicions
# ---------------------------------------------------------------------------

def test_enrollment_does_not_fabricate_a_camera_sighting(token):
    """SUSPICION 1+2: upload sets last_seen_at and leaves appearances_count 0.

    last_seen_at means "when a camera last saw this person". An upload is not a
    sighting. This test pins a real camera last_seen_at, then enrolls a second
    photo and checks whether the upload overwrote it.
    """
    name = _unique("sighting")
    status, body = _upload(token, name, FACE_A)
    assert status == 200, body
    identity_id = body["identity_id"]

    # Pretend a camera saw them a week ago.
    _sql("""UPDATE identities
               SET last_seen_at = now() - interval '7 days',
                   first_seen_at = now() - interval '30 days',
                   appearances_count = 42
             WHERE id = CAST(:i AS uuid)""", {"i": identity_id}, fetch="none")
    before = _sql("""SELECT first_seen_at, last_seen_at, appearances_count
                       FROM identities WHERE id = CAST(:i AS uuid)""",
                  {"i": identity_id}, fetch="one")

    # A second, different photo of the same person.
    status, body2 = _upload(token, name, FACE_B, on_decision="add_to_existing")
    assert status in (200, 201), body2

    after = _sql("""SELECT first_seen_at, last_seen_at, appearances_count
                      FROM identities WHERE id = CAST(:i AS uuid)""",
                 {"i": identity_id}, fetch="one")
    _record("sighting_timestamps", {"before": before, "after": after})

    assert after["last_seen_at"] == before["last_seen_at"], (
        f"uploading a photo overwrote the camera last_seen_at: "
        f"{before['last_seen_at']} -> {after['last_seen_at']}")


def test_enrollment_records_face_quality(token):
    """SUSPICION 3: quality_score is passed as None, so nothing is ever scored."""
    name = _unique("quality")
    status, body = _upload(token, name, FACE_A)
    assert status == 200, body
    identity_id = body["identity_id"]

    row = _sql("""SELECT m.quality_score, m.quality_scorer_version,
                         e.quality AS embedding_quality
                    FROM identity_images m
                    JOIN identity_embeddings e ON e.image_id = m.id
                   WHERE m.identity_id = CAST(:i AS uuid)""",
               {"i": identity_id}, fetch="one")
    _record("quality_columns", row)

    assert row["quality_score"] is not None, (
        "identity_images.quality_score is NULL: no quality is measured at "
        "enrollment, so the quality gate cannot reject a blurry face")
    assert row["embedding_quality"] is not None, (
        "identity_embeddings.quality is NULL for enrolled photos")


def test_creating_a_person_is_audited(token):
    """SUSPICION 4: merge and promote write identity_audit_log; create does not."""
    name = _unique("audit")
    before = _sql("SELECT count(*) FROM identity_audit_log", fetch="scalar")
    status, body = _upload(token, name, FACE_A)
    assert status == 200, body
    after = _sql("SELECT count(*) FROM identity_audit_log", fetch="scalar")

    rows = _sql("""SELECT action_type, identity_id::text AS identity_id
                     FROM identity_audit_log
                    WHERE identity_id = CAST(:i AS uuid)""",
                {"i": body["identity_id"]})
    _record("audit_rows_on_create",
            {"count_before": before, "count_after": after, "rows": rows})

    assert rows, (
        "creating a person from a photo writes no identity_audit_log row, "
        "although merging and promoting one do")


# ---------------------------------------------------------------------------
# §2 scenarios 8-10 — input validation nothing covers today
# ---------------------------------------------------------------------------

def test_oversized_file_is_rejected(token):
    """Scenario 8: > MAX_FILE_SIZE (10 MB). No existing test covers this."""
    name = _unique("toobig")
    oversized = _read(FACE_A) + b"\0" * (11 * 1024 * 1024)
    status, body = _upload(token, name, payload_override=oversized,
                           filename="huge.jpg", on_decision=None)
    _record("oversized_file", {"status": status, "bytes": len(oversized), "body": body})

    assert status == 400, (status, body)
    assert body.get("error") == "file_too_large", body
    assert not _identity_ids_for(name), "a rejected upload created an identity"


@pytest.mark.parametrize("label,payload,filename,content_type,expected", [
    ("text_renamed_jpg", b"this is definitely not an image", "evil.jpg",
     "image/jpeg", "invalid_image"),
    ("svg", b'<svg xmlns="http://www.w3.org/2000/svg"><text>x</text></svg>',
     "evil.svg", "image/svg+xml", "invalid_image"),
    ("empty", b"", "empty.jpg", "image/jpeg", "empty_file"),
    ("real_jpeg_wrong_content_type", None, "note.txt", "text/plain",
     "invalid_file_type"),
])
def test_non_image_payloads_are_rejected(token, label, payload, filename,
                                         content_type, expected):
    """Scenario 9: non-image bytes and content-type mismatches."""
    name = _unique(f"badfile_{label}")
    body_bytes = _read(FACE_A) if payload is None else payload
    status, body = _upload(token, name, payload_override=body_bytes,
                           filename=filename, content_type=content_type,
                           on_decision=None)
    _record(f"non_image_{label}", {"status": status, "body": body})

    assert status == 400, (status, body)
    assert body.get("error") == expected, body
    assert not _identity_ids_for(name), "a rejected upload created an identity"


@pytest.mark.parametrize("label,person_name,expect_ok", [
    ("empty", "", False),
    ("whitespace_only", "   ", False),
    ("single_char", "a", False),
    ("too_long", "x" * 300, False),
    ("collapses_whitespace", "  Bob   Smith  ", True),
    ("html", "<script>alert(1)</script>", True),
    ("sql_ish", "Robert'); DROP TABLE identities;--", True),
    ("unicode", "علي عباس", True),
])
def test_person_name_validation(token, label, person_name, expect_ok):
    """Scenario 10: name edge cases, including that hostile names store safely."""
    prefixed = person_name if not expect_ok else f"{TEST_PREFIX}{label}_{person_name}"
    status, body = _upload(token, prefixed, FACE_A, on_decision="create_new")
    _record(f"name_{label}", {"sent": prefixed, "status": status, "body": body})

    if not expect_ok:
        assert status in (400, 422), (status, body)
        # The modal reads data.error / data.message. FastAPI's own 422 envelope
        # carries neither, so such a rejection renders as the bare string
        # "HTTP error! status: 422" (frontend/js/upload-modal.js:342).
        assert body.get("error") == "invalid_name", (
            f"name {person_name!r} was rejected with status {status} but in an "
            f"envelope the upload modal cannot render: {body}")
        return

    assert status in (200, 201), (status, body)
    stored = _sql("SELECT display_name FROM identities WHERE id = CAST(:i AS uuid)",
                  {"i": body["identity_id"]}, fetch="scalar")

    if label == "collapses_whitespace":
        assert stored == f"{TEST_PREFIX}{label}_ Bob Smith".replace("_ ", "_ ").strip() \
            or "  " not in stored, f"whitespace was not collapsed: {stored!r}"
    else:
        # hostile names must round-trip verbatim, proving they are data not code
        assert person_name in stored, (person_name, stored)

    # the table is still there and healthy, i.e. nothing was interpreted
    assert _sql("SELECT count(*) FROM identities", fetch="scalar") > 0


# ---------------------------------------------------------------------------
# §2 scenarios 14-16 — concurrency, entirely uncovered today
# ---------------------------------------------------------------------------

def test_concurrent_same_new_name_does_not_create_duplicate_people(token):
    """Scenario 14 / SUSPICION 5: display_name has no unique constraint."""
    # The race only exists on the path that CREATES a person. If the face is
    # already on file the review gate answers 202 for both callers and nothing
    # is created, which would pass this test while proving nothing — so start
    # from a clean slate with a face that matches no one.
    _cleanup_prefix()
    name = _unique("race_name")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_upload, token, name, FACE_C, False, None, None)
                   for _ in range(2)]
        results = [future.result() for future in futures]

    ids = _identity_ids_for(name)
    statuses = [s for s, _ in results]
    _record("concurrent_same_name",
            {"results": [{"status": s, "body": b} for s, b in results],
             "identities_created": ids})

    if not ids:
        pytest.skip(f"race not exercised: neither upload reached the create "
                    f"path (statuses={statuses})")

    assert len(ids) <= 1, (
        f"two parallel uploads of the same new name created {len(ids)} separate "
        f"identities; every later upload for that name now fails with 409 "
        f"ambiguous_identity: {ids}")


def test_concurrent_identical_photo_is_reported_as_duplicate_not_500(token):
    """Scenario 15 / SUSPICION 6: uq_identity_image_checksum -> generic 500."""
    name = _unique("race_dup")
    status, body = _upload(token, name, FACE_A)
    assert status == 200, body
    identity_id = body["identity_id"]

    def _add():
        return _http("POST", f"/api/identities/{identity_id}/images", token=token,
                     fields={"is_face_image": "false"},
                     files={"photo": ("face_b.jpg", _read(FACE_B), "image/jpeg")})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result()
                   for future in [pool.submit(_add) for _ in range(2)]]
    _record("concurrent_identical_photo",
            [{"status": s, "body": b} for s, b in results])

    statuses = sorted(status for status, _ in results)
    assert 500 not in statuses, (
        f"racing the same photo returned a 500 instead of the friendly "
        f"duplicate response: {results}")


def test_concurrent_uploads_do_not_overwrite_each_others_files(token):
    """Scenario 16 / SUSPICION 7: next_image_filename scans the directory
    without a lock, so two uploads can pick the same image_NNN name and
    os.replace silently overwrites one of them."""
    name = _unique("race_file")
    status, body = _upload(token, name, FACE_A)
    assert status == 200, body
    identity_id = body["identity_id"]

    # next_image_filename() builds "image_%03d<ext>" and tests membership in the
    # directory listing, so the ordinal is PER EXTENSION: a .jpg and a .png can
    # both be image_001 and never collide. To exercise the real collision both
    # uploads must land on the same extension, hence the .jpg filename on both.
    def _add(path, filename):
        return _http("POST", f"/api/identities/{identity_id}/images", token=token,
                     fields={"is_face_image": "false"},
                     files={"photo": (filename, _read(path), "image/jpeg")})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in
                   [pool.submit(_add, FACE_B, "b.jpg"),
                    pool.submit(_add, FACE_C, "c.jpg")]]

    rows = _sql("""SELECT storage_path, file_checksum FROM identity_images
                    WHERE identity_id = CAST(:i AS uuid) ORDER BY id""",
                {"i": identity_id})
    paths = [row["storage_path"] for row in rows]
    _record("concurrent_file_naming",
            {"results": [{"status": s, "body": b} for s, b in results], "rows": rows})

    assert len(paths) == len(set(paths)), (
        f"two identity_images rows point at the same file — one photo was "
        f"overwritten: {paths}")

    for row in rows:
        absolute = os.path.join(STORAGE_DIR,
                                row["storage_path"].split("storage/", 1)[1])
        assert os.path.isfile(absolute), f"row points at a missing file: {absolute}"
        actual = hashlib.sha256(_read(absolute)).hexdigest()
        assert actual == row["file_checksum"], (
            f"{row['storage_path']} on disk does not match its stored checksum — "
            f"it was overwritten by a concurrent upload")


# ---------------------------------------------------------------------------
# §2 scenario 18 — the CSRF posture of the identity-minting endpoint
# ---------------------------------------------------------------------------

def test_cookie_upload_without_csrf_header_is_refused(token):
    """Scenario 18 / SUSPICION 13: /api/upload-person carries no CSRF
    dependency by design, while the newer per-identity route does. Documented
    deliberate at backend/routes/upload.py:307 — this pins the actual exposure.
    """
    status, login = _http("POST", "/api/auth/login",
                          body={"username": "admin", "password": "admin123"})
    assert status == 200, login

    # A cookie-authenticated, cross-site-shaped POST: no Authorization header,
    # no X-Requested-With.
    cookie = login.get("access_token")
    name = _unique("csrf")
    data, content_type = _multipart(
        {"person_name": name, "is_face_image": "false"},
        {"photo": ("face_a.jpg", _read(FACE_A), "image/jpeg")})
    request = urllib.request.Request(BASE + "/api/upload-person", data=data,
                                     method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("Cookie", f"access_token={cookie}")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            status, body = response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        status, body = exc.code, {"_raw": exc.read().decode(errors="replace")}

    _record("csrf_cookie_no_header", {"status": status, "body": body})

    assert status == 403, (
        "a cookie-authenticated POST with no X-Requested-With header created a "
        f"person (status {status}); the identity-minting endpoint has no CSRF "
        "protection while POST /api/identities/{id}/images does")
