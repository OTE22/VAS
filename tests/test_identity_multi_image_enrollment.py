"""
Multi-image enrollment: one identity, many images, one embedding per image.
===========================================================================
Run inside the api container:

    docker exec face_recognition_api python -m pytest \
        tests/test_identity_multi_image_enrollment.py -v

These pin the defects found in POST /api/upload-person and the behaviour that
replaced them:

  UP-001  the `else` was attached to `if embedding is not None:` instead of
          `if identity_service:`, making the identity-service-unavailable
          branch unreachable — a missing service fell through to success
  UP-002  the broad exception handler returned HTTP 200 + success=True when
          the database write failed, so the UI reported people who did not exist
  UP-003  the photo was written to its final path BEFORE the transaction, so
          failures left orphan files
  UP-004  is_face_image=True fabricated eye/nose/mouth coordinates from the
          image centre instead of detecting real landmarks
  UP-005  identity lookup was exact and case-sensitive, so "Chandler" and
          "chandler" became two people
"""

import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.request
import uuid as uuid_module

import pytest

from conftest import run_on_shared_loop as run_async

BASE = "http://localhost:8000"
FACES_DIR = "/app/storage/faces"
SERVICE_SRC = "/app/backend/core/enrollment_service.py"
ROUTE_SRC = "/app/backend/routes/upload.py"
# The detector call and the padded retry now live here, shared with search.
EXTRACTION_SRC = "/app/backend/core/face_extraction.py"

# Fixtures live OUTSIDE FACES_DIR, in the repository, so the tests do not
# depend on production enrollment data. (They previously read live display-name
# folders such as storage/faces/trump/, which the storage purge removes.)
FIXTURES = "/app/tests/fixtures/faces"
# Real pixels with genuinely detectable faces and distinct checksums —
# synthetic images do not survive real face detection.
FACE_A = f"{FIXTURES}/face_a.jpg"
FACE_B = f"{FIXTURES}/face_b.jpg"
FACE_C = f"{FIXTURES}/face_c.png"
# A genuinely PRE-CROPPED face (112x112): the plain detector finds nothing in
# it, which is exactly the case is_face_image=True exists for.
CROPPED_FACE = f"{FIXTURES}/cropped_face.jpg"
# Two people in one frame — must be refused, never silently enrolled as one.
TWO_FACES = f"{FIXTURES}/two_faces.jpg"

TEST_PREFIX = "qa_multiimg_"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _source(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _code_only(source):
    """Strip comments and docstrings so contract scans read code, not prose."""
    import ast
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    return ast.unparse(tree)


def _multipart(fields, files):
    """Build a multipart/form-data body (no third-party dependency)."""
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
          fields=None, files=None, timeout=180):
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


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         body={"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


def _delete_identity(identity_id):
    from sqlalchemy import text
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(text("DELETE FROM identity_embeddings WHERE identity_id = :i"),
                             {"i": identity_id})
            await db.execute(text("DELETE FROM identity_images WHERE identity_id = :i"),
                             {"i": identity_id})
            await db.execute(text("DELETE FROM identities WHERE id = :i"), {"i": identity_id})
            await db.commit()
    run_async(_run())
    folder = os.path.join(FACES_DIR, str(identity_id))
    if os.path.isdir(folder):
        for entry in os.listdir(folder):
            try:
                os.remove(os.path.join(folder, entry))
            except OSError:
                pass
        try:
            os.rmdir(folder)
        except OSError:
            pass


def _cleanup_prefix():
    """Remove every identity this module created, by name prefix."""
    from sqlalchemy import text
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            rows = (await db.execute(
                text("SELECT id FROM identities WHERE display_name LIKE :p"),
                {"p": TEST_PREFIX + "%"})).all()
            return [str(r[0]) for r in rows]
    for identity_id in run_async(_run()):
        _delete_identity(identity_id)


@pytest.fixture(scope="module", autouse=True)
def _clean_module():
    _cleanup_prefix()
    yield
    _cleanup_prefix()


def _upload_legacy(token, name, image_path, is_face_image=False, filename=None,
                   on_decision="create_new"):
    """Enroll by name, answering the review prompt if one comes back.

    These fixtures deliberately reuse one face across many names — face_a.jpg
    alone is enrolled under a dozen different names in this module, and
    face_a/face_b are two photos of the SAME person — so the decision gate
    (`202 decision_required`) fires on most of them. Answering it here keeps
    every downstream assertion in this file about what enrollment DOES rather
    than about which phase answered.

    Tolerates BOTH outcomes on purpose. Whether a given upload sees a match
    depends on what other modules left behind, which depends on collection
    order; a helper that assumed one or the other would fail intermittently.
    """
    payload = _read(image_path)
    status, body = _http("POST", "/api/upload-person", token=token,
                         fields={"person_name": name,
                                 "is_face_image": str(is_face_image).lower()},
                         files={"photo": (filename or os.path.basename(image_path),
                                          payload, "image/jpeg")})
    if status == 202 and body.get("decision_required"):
        return _http("POST", "/api/enrollment/confirm", token=token,
                     headers={"X-Requested-With": "XMLHttpRequest"},
                     body={"action": on_decision,
                           "display_name": name,
                           "upload_token": body["upload_token"],
                           # The prompt exists because this looks like somebody
                           # we have; these tests mean to create a new record
                           # anyway, which is the answer that needs saying twice.
                           "confirm_create_new": True})
    return status, body


def _add_image(token, identity_id, image_path, headers=None):
    payload = _read(image_path)
    return _http("POST", f"/api/identities/{identity_id}/images", token=token,
                 headers=headers or {"X-Requested-With": "XMLHttpRequest"},
                 files={"photo": (os.path.basename(image_path), payload, "image/jpeg")})


def _db_counts(identity_id):
    from sqlalchemy import text
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            images = (await db.execute(
                text("SELECT count(*) FROM identity_images WHERE identity_id=:i"),
                {"i": identity_id})).scalar()
            embeddings = (await db.execute(
                text("SELECT count(*) FROM identity_embeddings WHERE identity_id=:i"),
                {"i": identity_id})).scalar()
            linked = (await db.execute(
                text("SELECT count(*) FROM identity_embeddings e JOIN identity_images m "
                     "ON e.image_id = m.id WHERE e.identity_id=:i AND m.identity_id=:i"),
                {"i": identity_id})).scalar()
            primaries = (await db.execute(
                text("SELECT count(*) FROM identity_images "
                     "WHERE identity_id=:i AND is_primary"),
                {"i": identity_id})).scalar()
            return images, embeddings, linked, primaries
    return run_async(_run())


# ---------------------------------------------------------------------------
# UP-001 / UP-002 / UP-004: the control-flow and honesty defects, at source
# ---------------------------------------------------------------------------

def test_identity_service_branch_is_reachable_and_not_tied_to_embedding():
    """UP-001. The unavailable-service branch must hang off the service check.

    The old shape put `else:` on `if embedding is not None:`, and because the
    embedding was already validated non-None the branch could never run.
    """
    code = _code_only(_source(SERVICE_SRC))
    assert "service = _identity_service()" in code
    # The refusal is raised from the service check, unconditionally reachable.
    guard = re.search(r"if service is None:\s*(.+?)raise EnrollmentError", code, re.S)
    assert guard, "the identity-service check no longer raises when unavailable"
    assert "service_unavailable" in code
    # And it is NOT nested inside an embedding-is-None branch.
    assert "if embedding is not None:" not in code, (
        "enrollment still branches on `embedding is not None` — the validated "
        "embedding makes that branch dead code (UP-001)")


def test_no_failure_path_returns_http_200_success():
    """UP-002. A broad handler must never convert failure into success."""
    route_code = _code_only(_source(ROUTE_SRC))
    # No handler may pair a 200 status with success: True while handling an error.
    assert not re.search(r"except\s+Exception[\s\S]{0,600}?status_code=200[\s\S]{0,200}?"
                         r'"success":\s*True', route_code), (
        "an exception handler still returns HTTP 200 with success=True (UP-002)")
    service_code = _code_only(_source(SERVICE_SRC))
    # The service converts unexpected failures into a refusal, not a result.
    assert "database_update_failed" in service_code
    assert re.search(r"except Exception[\s\S]{0,800}?raise EnrollmentError", service_code), (
        "unexpected exceptions are not converted into an explicit refusal")


def test_landmarks_are_never_fabricated():
    """UP-004. No synthetic eye/nose/mouth coordinates anywhere.

    The detection itself moved to backend/core/face_extraction.py, shared with
    the search endpoints — enrollment was the only path that refused to
    fabricate, and leaving the logic here is what let the others drift. The
    invariant is unchanged; it is scanned over the union of the three files.
    """
    code = (_code_only(_source(SERVICE_SRC)) + _code_only(_source(ROUTE_SRC))
            + _code_only(_source(EXTRACTION_SRC)))
    for token in ("center_x", "center_y"):
        assert token not in code, f"fabricated landmark coordinate {token!r} still present"
    # Landmarks come from the detector, both in normal and cropped-face mode.
    assert code.count(".detector.detect") >= 2, (
        "cropped-face mode must still run a real detection")
    assert "no_landmarks" in code


def test_raw_exception_text_never_reaches_the_client():
    route_code = _code_only(_source(ROUTE_SRC))
    assert "str(e)" not in route_code and "str(exc)" not in route_code, (
        "route interpolates raw exception text into a response")


# ---------------------------------------------------------------------------
# Pure-function contracts
# ---------------------------------------------------------------------------

def test_name_normalization_collapses_case_and_whitespace():
    """UP-005. These three spellings are one person."""
    from backend.core.enrollment_service import name_lookup_key, normalize_display_name

    assert name_lookup_key("Ali Abbass") == name_lookup_key("ali abbass")
    assert name_lookup_key("ALI   ABBASS") == name_lookup_key("Ali Abbass")
    assert name_lookup_key("  Ali  Abbass  ") == "ali abbass"
    # Display case is preserved even though lookup ignores it.
    assert normalize_display_name("  Ali   Abbass ") == "Ali Abbass"


def test_storage_folder_is_the_uuid_and_rejects_traversal():
    from backend.core.enrollment_service import identity_folder

    identity_id = uuid_module.uuid4()
    folder = identity_folder(identity_id)
    assert os.path.basename(folder) == str(identity_id)
    assert "storage" in folder.replace("\\", "/")
    for hostile in ("../../etc", "a/../../b", "..", "not-a-uuid"):
        with pytest.raises(Exception):
            identity_folder(hostile)


def test_relative_storage_path_refuses_paths_outside_storage():
    from backend.core.enrollment_service import (EnrollmentError,
                                                 relative_storage_path)

    inside = relative_storage_path(f"{FACES_DIR}/x/image_001.jpg")
    assert inside.startswith("storage/")
    assert not os.path.isabs(inside)
    with pytest.raises(EnrollmentError):
        relative_storage_path("/etc/passwd")


def test_generated_filenames_are_server_controlled():
    from backend.core.enrollment_service import next_image_filename

    folder = f"/tmp/qa_names_{uuid_module.uuid4().hex}"
    os.makedirs(folder, exist_ok=True)
    try:
        assert next_image_filename(folder, ".jpg") == "image_001.jpg"
        open(os.path.join(folder, "image_001.jpg"), "wb").close()
        assert next_image_filename(folder, ".jpg") == "image_002.jpg"
    finally:
        for entry in os.listdir(folder):
            os.remove(os.path.join(folder, entry))
        os.rmdir(folder)


def test_embedding_validation_rejects_bad_vectors():
    import numpy as np

    from backend.core.enrollment_service import (EXPECTED_EMBEDDING_DIM,
                                                 EnrollmentError,
                                                 validate_embedding_vector)

    good = np.ones(EXPECTED_EMBEDDING_DIM, dtype=np.float32)
    assert validate_embedding_vector(good).size == EXPECTED_EMBEDDING_DIM

    with pytest.raises(EnrollmentError):          # None
        validate_embedding_vector(None)
    with pytest.raises(EnrollmentError):          # wrong dimension
        validate_embedding_vector(np.ones(128, dtype=np.float32))
    with pytest.raises(EnrollmentError):          # zero norm
        validate_embedding_vector(np.zeros(EXPECTED_EMBEDDING_DIM, dtype=np.float32))
    nan = np.ones(EXPECTED_EMBEDDING_DIM, dtype=np.float32)
    nan[0] = np.nan
    with pytest.raises(EnrollmentError):          # non-finite
        validate_embedding_vector(nan)


def test_saved_embedding_record_validation_rejects_wrong_owner_and_null():
    from backend.core.enrollment_service import (EnrollmentError,
                                                 validate_saved_embedding)

    class Rec:
        def __init__(self, identity_id, embedding=None, image_id=None):
            self.identity_id = identity_id
            self.embedding = embedding
            self.image_id = image_id

    identity_id = uuid_module.uuid4()
    with pytest.raises(EnrollmentError):                    # no record at all
        validate_saved_embedding(None, identity_id, None)
    with pytest.raises(EnrollmentError):                    # belongs elsewhere
        validate_saved_embedding(Rec(uuid_module.uuid4(), [0.1] * 512),
                                 identity_id, None)
    with pytest.raises(EnrollmentError):                    # null vector
        validate_saved_embedding(Rec(identity_id, None), identity_id, None)
    with pytest.raises(EnrollmentError):                    # wrong image link
        validate_saved_embedding(Rec(identity_id, [0.1] * 512, image_id=999),
                                 identity_id, 1)


# ---------------------------------------------------------------------------
# Live enrollment
# ---------------------------------------------------------------------------

def test_first_upload_creates_identity_image_and_embedding(token):
    name = TEST_PREFIX + "first"
    status, body = _upload_legacy(token, name, FACE_A)
    assert status == 200, body
    assert body["success"] is True, body
    assert body["identity_created"] is True
    assert body["image_created"] is True
    assert body["embedding_created"] is True
    assert body["is_primary"] is True, "the first photo becomes primary"

    identity_id = body["identity_id"]
    images, embeddings, linked, primaries = _db_counts(identity_id)
    assert (images, embeddings, linked, primaries) == (1, 1, 1, 1), (
        f"expected 1 image/1 embedding/linked/1 primary, got "
        f"{images}/{embeddings}/{linked}/{primaries}")

    # The folder is the UUID, not the display name.
    assert os.path.isdir(os.path.join(FACES_DIR, identity_id))
    assert not os.path.isdir(os.path.join(FACES_DIR, name))


def test_second_image_extends_the_same_identity(token):
    """One identity -> many images -> one embedding each."""
    name = TEST_PREFIX + "multi"
    status, first = _upload_legacy(token, name, FACE_A)
    assert status == 200 and first["success"], first
    identity_id = first["identity_id"]

    status, second = _add_image(token, identity_id, FACE_B)
    assert status == 201, second
    assert second["success"] is True
    assert second["identity_created"] is False, "must not create a second person"
    assert second["image_created"] is True
    assert second["embedding_created"] is True
    assert second["is_primary"] is False, "a newer photo must not steal primary"
    assert second["image_id"] != first["image_id"]

    images, embeddings, linked, primaries = _db_counts(identity_id)
    assert images == 2, f"expected 2 images, got {images}"
    assert embeddings == 2, f"expected 2 embeddings, got {embeddings}"
    assert linked == 2, "every embedding must reference its source image"
    assert primaries == 1, "exactly one primary image"

    # Both files live in the one UUID folder, server-named.
    folder = os.path.join(FACES_DIR, identity_id)
    files = sorted(os.listdir(folder))
    assert len(files) == 2, files
    assert all(re.fullmatch(r"image_\d{3}\.(jpg|png|webp|bmp)", f) for f in files), files


def test_additional_uploads_never_create_duplicate_identities(token):
    """Same person by name, three spellings — still one identity."""
    base = TEST_PREFIX + "casefold"
    status, first = _upload_legacy(token, base, FACE_A)
    assert status == 200 and first["success"], first
    identity_id = first["identity_id"]

    status, second = _upload_legacy(token, base.upper(), FACE_B)
    assert status == 200, second
    assert second["success"] is True
    assert second["identity_created"] is False, "capitalization created a duplicate"
    assert second["identity_id"] == identity_id

    status, third = _upload_legacy(token, "  " + base + "   ", FACE_C)
    assert status == 200, third
    assert third["identity_created"] is False, "whitespace created a duplicate"
    assert third["identity_id"] == identity_id

    images, embeddings, _linked, primaries = _db_counts(identity_id)
    assert images == 3 and embeddings == 3, f"{images} images / {embeddings} embeddings"
    assert primaries == 1


def test_duplicate_checksum_is_idempotent(token):
    name = TEST_PREFIX + "dup"
    status, first = _upload_legacy(token, name, FACE_A)
    assert status == 200 and first["success"], first
    identity_id = first["identity_id"]

    status, again = _add_image(token, identity_id, FACE_A)
    assert status == 200, again
    assert again["success"] is True
    assert again["duplicate"] is True
    assert again["image_created"] is False
    assert again["embedding_created"] is False
    assert again["image_id"] == first["image_id"], "must reference the existing image"

    images, embeddings, _linked, _primaries = _db_counts(identity_id)
    assert images == 1, "a duplicate upload created a second image row"
    assert embeddings == 1, "a duplicate upload created a second embedding"

    folder = os.path.join(FACES_DIR, identity_id)
    assert len(os.listdir(folder)) == 1, "a duplicate upload wrote a second file"


def test_ambiguous_display_name_returns_409(token):
    """Two active people, one normalized name -> the caller must choose."""
    from sqlalchemy import text
    from db_connection import db_manager

    name = TEST_PREFIX + "twin"
    status, first = _upload_legacy(token, name, FACE_A)
    assert status == 200 and first["success"], first

    # Second identity with the same normalized name, created directly so the
    # (now-fixed) endpoint cannot be the thing that makes the collision.
    twin_id = str(uuid_module.uuid4())

    async def _seed():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            await db.execute(text("""
                INSERT INTO identities (id, type, status, display_name,
                                        first_seen_at, last_seen_at,
                                        appearances_count, created_at, updated_at)
                VALUES (:i, 'KNOWN', 'ACTIVE', :n, now(), now(), 0, now(), now())
            """), {"i": twin_id, "n": name.upper()})
            await db.commit()
    run_async(_seed())

    try:
        status, body = _upload_legacy(token, name, FACE_B)
        assert status == 409, f"ambiguous name must refuse, got {status}: {body}"
        assert body["success"] is False
        assert body["error"] == "ambiguous_identity"
        assert len(body.get("candidates", [])) >= 2, body
        # The refusal names the choices without leaking anything else.
        assert all("identity_id" in c and "display_name" in c
                   for c in body["candidates"])
    finally:
        _delete_identity(twin_id)


def test_renaming_a_person_does_not_move_their_folder(token):
    from sqlalchemy import text
    from db_connection import db_manager

    name = TEST_PREFIX + "rename"
    status, body = _upload_legacy(token, name, FACE_A)
    assert status == 200 and body["success"], body
    identity_id = body["identity_id"]
    folder = os.path.join(FACES_DIR, identity_id)
    before = sorted(os.listdir(folder))

    async def _rename():
        async with db_manager.get_session() as db:
            await db.execute(text("UPDATE identities SET display_name=:n WHERE id=:i"),
                             {"n": TEST_PREFIX + "renamed_away", "i": identity_id})
            await db.commit()
    run_async(_rename())

    assert os.path.isdir(folder), "the UUID folder moved when the person was renamed"
    assert sorted(os.listdir(folder)) == before
    assert not os.path.isdir(os.path.join(FACES_DIR, TEST_PREFIX + "renamed_away"))


def test_only_one_primary_image_and_admin_can_change_it(token):
    name = TEST_PREFIX + "primary"
    status, first = _upload_legacy(token, name, FACE_A)
    assert status == 200 and first["success"], first
    identity_id = first["identity_id"]
    status, second = _add_image(token, identity_id, FACE_B)
    assert status == 201, second

    # The database itself forbids a second primary.
    from sqlalchemy import text
    from db_connection import db_manager
    from sqlalchemy.exc import IntegrityError

    async def _force_second_primary():
        async with db_manager.get_session() as db:
            await db.execute(text("UPDATE identity_images SET is_primary=true WHERE id=:i"),
                             {"i": second["image_id"]})
            await db.commit()
    with pytest.raises(IntegrityError):
        run_async(_force_second_primary())

    # The supported path works and moves the flag atomically.
    status, promoted = _http(
        "PUT", f"/api/identities/{identity_id}/images/{second['image_id']}/primary",
        token=token, headers={"X-Requested-With": "XMLHttpRequest"})
    assert status == 200, promoted
    assert promoted["success"] is True and promoted["is_primary"] is True

    _images, _embeddings, _linked, primaries = _db_counts(identity_id)
    assert primaries == 1, "promotion left more than one primary"

    status, listing = _http("GET", f"/api/identities/{identity_id}/images", token=token)
    assert status == 200, listing
    flags = {img["image_id"]: img["is_primary"] for img in listing["images"]}
    assert flags[second["image_id"]] is True
    assert flags[first["image_id"]] is False


def test_no_face_image_is_rejected_and_leaves_nothing_behind(token):
    """A failure before persistence must leave no identity, row or file."""
    import numpy as np
    import cv2

    blank = np.full((256, 256, 3), 127, dtype=np.uint8)
    ok, buffer = cv2.imencode(".jpg", blank)
    assert ok
    name = TEST_PREFIX + "noface"

    status, body = _http("POST", "/api/upload-person", token=token,
                         fields={"person_name": name, "is_face_image": "false"},
                         files={"photo": ("blank.jpg", buffer.tobytes(), "image/jpeg")})
    assert status == 400, body
    assert body["success"] is False
    assert body["error"] == "no_face"

    from sqlalchemy import text
    from db_connection import db_manager

    async def _count():
        async with db_manager.get_session() as db:
            return (await db.execute(
                text("SELECT count(*) FROM identities WHERE display_name=:n"),
                {"n": name})).scalar()
    assert run_async(_count()) == 0, "a rejected upload created an identity"

    # No temp file survived either.
    incoming = os.path.join(FACES_DIR, ".incoming")
    leftovers = os.listdir(incoming) if os.path.isdir(incoming) else []
    assert not leftovers, f"temporary files left behind: {leftovers}"


def test_cropped_face_mode_still_requires_real_landmarks(token):
    """is_face_image=True may relax framing, never invent landmarks."""
    import numpy as np
    import cv2

    blank = np.full((200, 200, 3), 200, dtype=np.uint8)
    ok, buffer = cv2.imencode(".jpg", blank)
    assert ok
    name = TEST_PREFIX + "cropped"

    status, body = _http("POST", "/api/upload-person", token=token,
                         fields={"person_name": name, "is_face_image": "true"},
                         files={"photo": ("crop.jpg", buffer.tobytes(), "image/jpeg")})
    assert status == 400, body
    assert body["success"] is False
    assert body["error"] in ("no_landmarks", "no_face"), body
    assert body["error"] == "no_landmarks", (
        "cropped-face mode must report the landmark failure explicitly")


def test_pre_cropped_face_enrolls_via_real_landmarks(token):
    """is_face_image=True on a genuinely cropped face succeeds — and it does so
    by DETECTING landmarks (padded retry), never by fabricating them.

    The plain detector finds nothing in this 112x112 crop, so a pass here can
    only come from the retry path finding real keypoints.
    """
    import cv2

    from backend.core.enrollment_service import detect_face_landmarks

    image = cv2.imread(CROPPED_FACE)
    assert image is not None
    # Baseline: without the cropped-face mode there is no detection at all.
    from backend.core import model_manager
    if not model_manager._initialized:
        model_manager.initialize()
    _b, plain = model_manager.detector.detect(image, max_num=1)
    assert plain is None or len(plain) == 0, (
        "fixture is no longer a hard crop; the test would prove nothing")

    _bbox, landmarks = detect_face_landmarks(image, is_face_image=True)
    assert landmarks is not None and len(landmarks) == 5, (
        "cropped-face mode did not produce five real landmarks")

    # End to end through the endpoint.
    name = TEST_PREFIX + "cropok"
    status, body = _upload_legacy(token, name, CROPPED_FACE, is_face_image=True)
    assert status == 200, body
    assert body["success"] is True and body["embedding_created"] is True


def test_cropped_face_records_its_source_type(token):
    """A pre-cropped upload uses the same folder and workflow; only the audit
    field distinguishes it."""
    name = TEST_PREFIX + "croppedmeta"
    status, body = _upload_legacy(token, name, CROPPED_FACE, is_face_image=True)
    assert status == 200, body
    assert body["success"] is True and body["embedding_created"] is True
    identity_id = body["identity_id"]

    status, listing = _http("GET", f"/api/identities/{identity_id}/images", token=token)
    assert status == 200, listing
    image = listing["images"][0]
    assert image["source_type"] == "cropped_face", image
    # Same UUID folder, same server-generated name — no separate workflow.
    assert re.fullmatch(r"image_\d{3}\.(jpg|png|webp|bmp)", image["filename"]), image
    assert os.path.isdir(os.path.join(FACES_DIR, identity_id))

    # A normal (non-cropped) upload is recorded distinctly.
    status, second = _add_image(token, identity_id, FACE_A)
    assert status == 201, second
    status, listing = _http("GET", f"/api/identities/{identity_id}/images", token=token)
    kinds = {img["image_id"]: img["source_type"] for img in listing["images"]}
    assert kinds[second["image_id"]] == "upload", kinds


def test_multiple_faces_are_rejected(token):
    """Two people in one frame must never enroll as one person — otherwise the
    stored signature could belong to the wrong face."""
    name = TEST_PREFIX + "twofaces"
    for cropped in (False, True):
        status, body = _upload_legacy(token, name, TWO_FACES, is_face_image=cropped)
        assert status == 400, (cropped, status, body)
        assert body["success"] is False
        assert body["error"] == "multiple_faces", body
        assert "2 faces" in body["message"], body

    from sqlalchemy import text
    from db_connection import db_manager

    async def _count():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            return (await db.execute(
                text("SELECT count(*) FROM identities WHERE display_name=:n"),
                {"n": name})).scalar()
    assert run_async(_count()) == 0, "a multi-face upload created an identity"


def test_commit_failure_after_file_move_removes_the_final_image(token, monkeypatch):
    """The narrow window that would otherwise strand a file.

    os.replace() puts the image at its final path immediately BEFORE commit. If
    the commit then fails, rollback must delete that final file as well as the
    rows — otherwise storage accumulates images no database row references.
    """
    from backend.core import enrollment_service
    from backend.core.identity_index_pgvector import get_pgvector_index
    from backend.core.identity_service import IdentityService
    from db_connection import db_manager

    name = TEST_PREFIX + "commitfail"
    moved_to = {}

    real_replace = enrollment_service.os.replace

    def spy_replace(src, dst):
        real_replace(src, dst)
        moved_to["path"] = dst          # capture the final location
    monkeypatch.setattr(enrollment_service.os, "replace", spy_replace)

    monkeypatch.setattr(enrollment_service, "_identity_service",
                        lambda: IdentityService(None, pgvector_index=get_pgvector_index()))

    class CommitBoom(Exception):
        pass

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            original_commit = db.commit

            async def exploding_commit():
                raise CommitBoom("simulated commit failure after the file move")
            db.commit = exploding_commit
            try:
                with pytest.raises(enrollment_service.EnrollmentError) as excinfo:
                    await enrollment_service.enroll_image(
                        db,
                        image_bytes=_read(FACE_A),
                        original_filename="face_a.jpg",
                        content_type="image/jpeg",
                        person_name=name,
                    )
                return excinfo.value
            finally:
                db.commit = original_commit
    error = run_async(_run())

    assert error.code == "database_update_failed"
    assert "simulated commit failure" not in error.message

    # The move DID happen, and the file was cleaned up afterwards.
    assert moved_to.get("path"), "the test never exercised the post-move window"
    assert not os.path.exists(moved_to["path"]), (
        f"final image survived a failed commit: {moved_to['path']}")

    incoming = os.path.join(FACES_DIR, ".incoming")
    assert not (os.listdir(incoming) if os.path.isdir(incoming) else []), \
        "temporary file left behind"

    from sqlalchemy import text

    async def _rows():
        async with db_manager.get_session() as db:
            identities = (await db.execute(
                text("SELECT count(*) FROM identities WHERE display_name=:n"),
                {"n": name})).scalar()
            images = (await db.execute(text(
                "SELECT count(*) FROM identity_images m JOIN identities i "
                "ON i.id = m.identity_id WHERE i.display_name = :n"),
                {"n": name})).scalar()
            return identities, images
    identities, images = run_async(_rows())
    assert identities == 0, "a failed commit left an identity behind"
    assert images == 0, "a failed commit left an image row behind"


def test_database_failure_returns_failure_and_removes_temp_file(token, monkeypatch):
    """UP-002 + UP-003 at the service boundary: an exception during persistence
    must roll back, clean up, and surface as an explicit refusal."""
    from backend.core import enrollment_service
    from db_connection import db_manager

    name = TEST_PREFIX + "dbfail"

    class Boom(Exception):
        pass

    class ExplodingService:
        use_pgvector = True

        async def save_embedding(self, **_kwargs):
            raise Boom("simulated database failure")

    monkeypatch.setattr(enrollment_service, "_identity_service",
                        lambda: ExplodingService())

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            with pytest.raises(enrollment_service.EnrollmentError) as excinfo:
                await enrollment_service.enroll_image(
                    db,
                    image_bytes=_read(FACE_A),
                    original_filename="image1.jpg",
                    content_type="image/jpeg",
                    person_name=name,
                    actor_user_id=None,
                )
            return excinfo.value
    error = run_async(_run())

    assert error.code == "database_update_failed"
    assert error.status_code == 500
    assert "simulated database failure" not in error.message, (
        "raw exception text leaked into the user-facing message")

    # No identity, no temp file, no final file.
    from sqlalchemy import text

    async def _count():
        async with db_manager.get_session() as db:
            return (await db.execute(
                text("SELECT count(*) FROM identities WHERE display_name=:n"),
                {"n": name})).scalar()
    assert run_async(_count()) == 0, "a failed enrollment left an identity behind"

    incoming = os.path.join(FACES_DIR, ".incoming")
    leftovers = os.listdir(incoming) if os.path.isdir(incoming) else []
    assert not leftovers, f"temporary files left behind: {leftovers}"


def test_identity_service_unavailable_returns_503_under_pgvector(monkeypatch):
    """UP-001: the branch that could never run now runs."""
    from backend.core import enrollment_service
    from db_connection import db_manager

    monkeypatch.setattr(enrollment_service, "_identity_service", lambda: None)

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            with pytest.raises(enrollment_service.EnrollmentError) as excinfo:
                await enrollment_service.enroll_image(
                    db,
                    image_bytes=_read(FACE_A),
                    original_filename="image1.jpg",
                    content_type="image/jpeg",
                    person_name=TEST_PREFIX + "nosvc",
                )
            return excinfo.value
    error = run_async(_run())
    assert error.code == "service_unavailable"
    assert error.status_code == 503


def _real_pgvector_service():
    """A real IdentityService writing to pgvector.

    The pytest process never ran the app lifespan, so the module-level
    identity_service singleton is None here; the server has one. Build the same
    object the lifespan builds (FAISS index omitted — the pgvector write path
    does not use it).
    """
    from backend.core.identity_index_pgvector import get_pgvector_index
    from backend.core.identity_service import IdentityService

    return IdentityService(None, pgvector_index=get_pgvector_index())


def test_index_sync_failure_keeps_the_database_enrollment(token, monkeypatch):
    """PostgreSQL is authoritative; the vector index is a cache.

    This used to force the legacy `_sync_faiss` (the display-name FaceDatabase
    write) to fail. That whole chain was deleted; the one surviving sync
    mechanism is `sync_pending_embedding`, so the failure is injected there —
    which is also the more honest test, since it is the code that actually runs.
    """
    from backend.core import enrollment_service
    from backend.core.identity_service import IdentityService
    from db_connection import db_manager

    monkeypatch.setattr(enrollment_service, "_identity_service",
                        _real_pgvector_service)

    async def _sync_always_fails(self, *_args, **_kwargs):
        raise RuntimeError("injected index-sync failure")

    monkeypatch.setattr(IdentityService, "sync_pending_embedding",
                        _sync_always_fails)
    name = TEST_PREFIX + "faissfail"

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            return await enrollment_service.enroll_image(
                db,
                image_bytes=_read(FACE_A),
                original_filename="image1.jpg",
                content_type="image/jpeg",
                person_name=name,
            )
    result = run_async(_run())

    assert result.image_created is True and result.embedding_created is True
    assert "vector_index_sync_pending" in result.warnings

    images, embeddings, linked, _primaries = _db_counts(result.identity_id)
    assert (images, embeddings, linked) == (1, 1, 1), (
        "an index-sync failure rolled back valid PostgreSQL data")

    from sqlalchemy import text

    async def _vector_state():
        """Sync state is per-VECTOR now.

        It used to be asserted on identity_images.faiss_sync_state — a column
        that was written and never read, duplicating state that belongs to the
        embedding. The authoritative field is
        identity_embeddings.vector_index_sync_state.
        """
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT e.vector_index_sync_state, e.embedding IS NOT NULL "
                "FROM identity_embeddings e WHERE e.identity_id = :i"),
                {"i": result.identity_id})).first()
    state, has_vector = run_async(_vector_state())
    assert has_vector is True, (
        "the vector must be committed to PostgreSQL regardless of index sync")
    assert state in ("synced", "pending", "failed"), f"illegal sync state {state!r}"


# ---------------------------------------------------------------------------
# Security + response hygiene
# ---------------------------------------------------------------------------

def test_upload_endpoints_reject_anonymous_and_non_admin():
    payload = _read(FACE_A)
    for path in ("/api/upload-person",
                 f"/api/identities/{uuid_module.uuid4()}/images"):
        status, _ = _http("POST", path,
                          fields={"person_name": "x", "is_face_image": "false"},
                          files={"photo": ("a.jpg", payload, "image/jpeg")})
        assert status in (401, 403), f"{path} answered {status} anonymously"


def test_new_image_endpoints_enforce_csrf_for_cookie_clients():
    from fastapi import HTTPException
    from starlette.requests import Request as StarletteRequest

    from backend.routes.upload import require_upload_csrf

    def make(headers):
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return StarletteRequest({"type": "http", "method": "POST", "path": "/x",
                                 "headers": raw, "query_string": b""})

    with pytest.raises(HTTPException) as exc:
        require_upload_csrf(make({}))
    assert exc.value.status_code == 403
    require_upload_csrf(make({"x-requested-with": "XMLHttpRequest"}))
    require_upload_csrf(make({"authorization": "Bearer x"}))


def test_responses_never_contain_absolute_paths(token):
    name = TEST_PREFIX + "paths"
    status, body = _upload_legacy(token, name, FACE_A)
    assert status == 200 and body["success"], body
    identity_id = body["identity_id"]

    status, listing = _http("GET", f"/api/identities/{identity_id}/images", token=token)
    assert status == 200

    for payload in (body, listing):
        blob = json.dumps(payload)
        for needle in ("/app/", "storage/faces/.incoming", "C:\\\\", "/etc/"):
            assert needle not in blob, f"response leaked {needle!r}: {blob[:300]}"
        assert "\"embedding\"" not in blob, "response exposed a vector"


def test_legacy_endpoint_and_new_endpoint_share_one_implementation():
    """No parallel enrollment logic: both routes call the same service.

    The COUNT is the point, not the number: it is what stops a third
    enrollment implementation from growing here. A third call site does exist,
    in backend/routes/enrollment_review.py — phase two of a name-based upload —
    and it is held to the same rule by
    tests/test_enrollment_decision_gate.py::test_the_confirm_route_delegates_to_the_shared_service.
    Keeping it in its own module is what let this assertion stay at two.
    """
    code = _code_only(_source(ROUTE_SRC))
    assert code.count("await enroll_image(") == 2, (
        "expected exactly two call sites (legacy + add-image) into the shared service")
    # The route must not re-implement detection or embedding.
    for forbidden in ("get_embedding", "detector.detect", "face_db.add_face"):
        assert forbidden not in code, (
            f"route re-implements enrollment step {forbidden!r} instead of delegating")


def test_legacy_response_keeps_the_fields_the_frontend_reads(token):
    """upload-modal.js reads success/message/total_faces and the error codes."""
    name = TEST_PREFIX + "compat"
    status, body = _upload_legacy(token, name, FACE_A)
    assert status == 200, body
    for field in ("success", "message", "filename", "identity_id",
                  "identity_created", "total_identities", "total_faces", "backend"):
        assert field in body, f"legacy response lost the {field!r} field"
    assert isinstance(body["total_faces"], int)
    # filename is a basename, never a path
    assert "/" not in body["filename"] and "\\" not in body["filename"]


# ---------------------------------------------------------------------------
# Enrollment atomicity under VECTOR_BACKEND=faiss
#
# The FAISS branch of save_embedding commits the vector itself — required by the
# vector-index contract (persist -> COMMIT -> index). Called from enroll_image
# that commit landed BEFORE os.replace, splitting enrollment across two
# transactions: the rollback had nothing left to undo, so a later failure left a
# committed identity_images row pointing at a file that never landed.
#
# Two things these tests must do, and why:
#   * call enroll_image DIRECTLY, never through _upload_legacy. The HTTP helper
#     talks to the gunicorn process; monkeypatching in the pytest process would
#     not reach it, and the test would silently exercise pgvector instead.
#   * select the FAISS branch by CONSTRUCTION. `use_pgvector` is read from a
#     module constant captured at import, so patching settings.VECTOR_BACKEND
#     would not reach it either.
# ---------------------------------------------------------------------------

def _faiss_service(tmpdir=None, index=None):
    """An IdentityService wired to a real FlatFaissIndex, FAISS branch active."""
    from backend.core.identity_service import IdentityService
    from backend.core.vector_index import FlatFaissIndex

    if index is None:
        index = FlatFaissIndex(tmpdir, model_version="test")
    service = IdentityService(None, pgvector_index=None, vector_index=index)
    service.use_pgvector = False
    return service


def _enroll_direct(db, name, image_path=None):
    from backend.core import enrollment_service
    return enrollment_service.enroll_image(
        db,
        image_bytes=_read(image_path or FACE_A),
        original_filename="face_a.jpg",
        content_type="image/jpeg",
        person_name=name,
    )


def test_enrollment_is_atomic_under_faiss(monkeypatch, tmp_path):
    """A failed commit must leave NO identity, NO image row and NO embedding row.

    Fails before the fix: save_embedding's own commit had already made all three
    durable by the time the outer commit blew up.
    """
    from sqlalchemy import text
    from backend.core import enrollment_service
    from db_connection import db_manager

    name = TEST_PREFIX + "faissatomic"
    moved_to = {}
    real_replace = enrollment_service.os.replace

    def spy_replace(src, dst):
        real_replace(src, dst)
        moved_to["path"] = dst
    monkeypatch.setattr(enrollment_service.os, "replace", spy_replace)
    monkeypatch.setattr(enrollment_service, "_identity_service",
                        lambda: _faiss_service(str(tmp_path / "idx")))

    class CommitBoom(Exception):
        pass

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            original_commit = db.commit

            async def exploding_commit():
                raise CommitBoom("simulated commit failure after the file move")
            db.commit = exploding_commit
            try:
                with pytest.raises(enrollment_service.EnrollmentError) as excinfo:
                    await _enroll_direct(db, name)
                return excinfo.value
            finally:
                db.commit = original_commit
    error = run_async(_run())
    assert error.code == "database_update_failed"

    assert moved_to.get("path"), "the test never exercised the post-move window"
    assert not os.path.exists(moved_to["path"]), (
        "final image survived a failed commit: " + str(moved_to["path"]))

    async def _rows():
        async with db_manager.get_session() as db:
            identities = (await db.execute(
                text("SELECT count(*) FROM identities WHERE display_name=:n"),
                {"n": name})).scalar()
            images = (await db.execute(text(
                "SELECT count(*) FROM identity_images m JOIN identities i "
                "ON i.id = m.identity_id WHERE i.display_name = :n"),
                {"n": name})).scalar()
            embeddings = (await db.execute(text(
                "SELECT count(*) FROM identity_embeddings e JOIN identities i "
                "ON i.id = e.identity_id WHERE i.display_name = :n"),
                {"n": name})).scalar()
            return identities, images, embeddings
    identities, images, embeddings = run_async(_rows())
    assert identities == 0, "a failed commit left an identity behind under faiss"
    assert images == 0, "a failed commit left an image row behind under faiss"
    assert embeddings == 0, "a failed commit left an embedding row behind under faiss"


def test_faiss_enrollment_never_indexes_before_commit(monkeypatch, tmp_path):
    """The contract in the other direction.

    Catches a naive drop-the-commit fix: indexing an uncommitted row strands a
    phantom entry under a key a rollback then erases. Ordering is observed with
    a commit counter rather than a second database session, because index.add()
    runs synchronously ON the event loop — probing the database from inside it
    would deadlock.
    """
    from backend.core import enrollment_service
    from backend.core.vector_index import FlatFaissIndex
    from db_connection import db_manager

    name = TEST_PREFIX + "faissorder"
    commits = {"count": 0}
    seen_at = []

    index = FlatFaissIndex(str(tmp_path / "idx"), model_version="test")
    real_add = index.add

    def spy_add(key, vector, **kwargs):
        seen_at.append(commits["count"])
        return real_add(key, vector, **kwargs)
    index.add = spy_add

    monkeypatch.setattr(enrollment_service, "_identity_service",
                        lambda: _faiss_service(index=index))

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            original_commit = db.commit

            async def counting_commit():
                result = await original_commit()
                commits["count"] += 1
                return result
            db.commit = counting_commit
            try:
                return await _enroll_direct(db, name)
            finally:
                db.commit = original_commit
    result = run_async(_run())

    assert result.identity_created, "enrollment did not create the identity"
    assert seen_at, "the index was never asked to add the vector"
    for observed in seen_at:
        assert observed >= 1, (
            "index.add() ran before the enrollment transaction committed — "
            "a rollback would have stranded a phantom index entry")


def test_faiss_enrollment_survives_a_failed_index_sync(monkeypatch, tmp_path):
    """A dead index must not fail a committed enrollment.

    The vector is authoritative in PostgreSQL and the index is a cache, so the
    upload succeeds, the row stays reconcilable, and reconciliation repairs it.
    """
    from sqlalchemy import text
    from backend.core import enrollment_service
    from backend.core.vector_index import FlatFaissIndex
    from backend.core.vector_index.reconcile import pending_backlog, reconcile
    from db_connection import db_manager

    name = TEST_PREFIX + "faissdead"
    index = FlatFaissIndex(str(tmp_path / "idx"), model_version="test")
    real_add = index.add

    def exploding_add(key, vector, **kwargs):
        raise RuntimeError("simulated index failure")
    index.add = exploding_add

    monkeypatch.setattr(enrollment_service, "_identity_service",
                        lambda: _faiss_service(index=index))

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            return await _enroll_direct(db, name)
    result = run_async(_run())

    assert result.identity_created is True, "a cache write failure failed the enrollment"
    assert "vector_index_sync_pending" in (result.warnings or []), (
        "a failed index sync was not reported as a warning: " + str(result.warnings))

    images, embeddings, linked, primaries = _db_counts(result.identity_id)
    assert (images, embeddings) == (1, 1), "the enrollment was not durable"

    async def _state():
        async with db_manager.get_session() as db:
            return (await db.execute(
                text("SELECT id, vector_index_sync_state FROM identity_embeddings "
                     "WHERE identity_id=:i"), {"i": result.identity_id})).all()
    rows = run_async(_state())
    assert rows, "no embedding row"
    emb_id, state = int(rows[0][0]), rows[0][1]
    assert state in ("pending", "failed"), (
        "a failed sync must leave the row reconcilable, got " + repr(state))

    # Reconciliation repairs it once the index works again.
    index.add = real_add

    async def _reconcile():
        async with db_manager.get_session() as db:
            backlog = await pending_backlog(db)
            await reconcile(index, db)
            after = (await db.execute(
                text("SELECT vector_index_sync_state FROM identity_embeddings "
                     "WHERE id=:i"), {"i": emb_id})).scalar()
            return backlog, after
    backlog, after = run_async(_reconcile())

    assert backlog >= 1, "the pending vector was not counted in the backlog"
    assert index.contains(emb_id), "reconciliation did not add the missing vector"
    assert after == "synced", "reconciliation left the row " + repr(after)


def test_save_embedding_default_still_commits_under_faiss(tmp_path):
    """Non-deferred callers (identity_loader, backfill scripts) are unchanged.

    Asserts from a SEPARATE session that the row is durable before this one
    commits — i.e. save_embedding really did commit on its own.
    """
    import uuid as uuid_module
    from datetime import datetime

    import numpy as np
    from sqlalchemy import text
    from db_connection import db_manager
    from db_models import Identity, IdentityStatus, IdentityType

    service = _faiss_service(str(tmp_path / "idx"))
    ident = uuid_module.uuid4()
    vector = np.zeros(512, dtype=np.float32)
    vector[0] = 1.0

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            now = datetime.utcnow()
            db.add(Identity(id=ident, type=IdentityType.KNOWN,
                            status=IdentityStatus.ACTIVE,
                            display_name=TEST_PREFIX + "defaultcommit",
                            first_seen_at=now, last_seen_at=now))
            await db.commit()

            identity = await db.get(Identity, ident)
            record = await service.save_embedding(
                identity=identity, embedding=vector, detection_id=None,
                pipeline_id=None, quality_score=None, db=db)
            emb_id = record.id

            # Independent session: only a real commit is visible from here.
            async with db_manager.get_session() as probe:
                visible = (await probe.execute(
                    text("SELECT count(*) FROM identity_embeddings WHERE id=:i"),
                    {"i": emb_id})).scalar()
            return emb_id, visible

    emb_id, visible = run_async(_run())
    assert visible == 1, (
        "save_embedding(defer_commit=False) no longer commits under faiss — "
        "identity_loader and the backfill script depend on that")


def test_deferred_save_leaves_the_row_pending_and_unindexed(tmp_path):
    """The deferred contract, asserted directly rather than through enrollment."""
    import uuid as uuid_module
    from datetime import datetime

    import numpy as np
    from sqlalchemy import text
    from db_connection import db_manager
    from db_models import Identity, IdentityStatus, IdentityType

    service = _faiss_service(str(tmp_path / "idx"))
    ident = uuid_module.uuid4()
    vector = np.zeros(512, dtype=np.float32)
    vector[1] = 1.0

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            now = datetime.utcnow()
            db.add(Identity(id=ident, type=IdentityType.KNOWN,
                            status=IdentityStatus.ACTIVE,
                            display_name=TEST_PREFIX + "deferred",
                            first_seen_at=now, last_seen_at=now))
            await db.commit()

            identity = await db.get(Identity, ident)
            record = await service.save_embedding(
                identity=identity, embedding=vector, detection_id=None,
                pipeline_id=None, quality_score=None, db=db,
                defer_commit=True)

            # Not committed: an independent session must not see it yet.
            async with db_manager.get_session() as probe:
                visible = (await probe.execute(
                    text("SELECT count(*) FROM identity_embeddings WHERE id=:i"),
                    {"i": record.id})).scalar()

            indexed_before = service.vector_index.contains(record.id)
            state_before = record.vector_index_sync_state

            await db.commit()
            synced = await service.sync_pending_embedding(record.id, vector, db)
            indexed_after = service.vector_index.contains(record.id)
            return visible, indexed_before, state_before, synced, indexed_after

    visible, indexed_before, state_before, synced, indexed_after = run_async(_run())
    assert visible == 0, "defer_commit=True committed anyway"
    assert indexed_before is False, (
        "the vector was indexed before its transaction committed — a rollback "
        "would have stranded a phantom entry")
    assert state_before == "pending", "deferred row was left " + repr(state_before)
    assert synced == "synced"
    assert indexed_after is True, "sync_pending_embedding did not index the vector"


def test_defer_commit_is_inert_under_pgvector(token):
    """The live backend is provably unchanged, through the real HTTP path."""
    name = TEST_PREFIX + "pgvinert"
    status, result = _upload_legacy(token, name, FACE_A)
    assert status == 200 and result.get("success") is True, (status, result)
    identity_id = result["identity_id"]

    images, embeddings, linked, primaries = _db_counts(identity_id)
    assert (images, embeddings, linked, primaries) == (1, 1, 1, 1), (
        "pgvector enrollment changed shape: "
        + str((images, embeddings, linked, primaries)))
    assert not result.get("warnings"), "unexpected warnings: " + str(result.get("warnings"))
