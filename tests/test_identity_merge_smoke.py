"""Merge + deletion smoke tests — the two real identity-removal paths.

    docker exec face_recognition_api python -m pytest tests/test_identity_merge_smoke.py -v

There is deliberately NO `DELETE /api/identities/{id}` endpoint: an identity
leaves the system by being MERGED into another (loser keeps its row, status
MERGED, `merged_into_id` set) or by RETENTION. Despite that, merge had zero
API-level tests before this file — the only coverage was a frontend
page-structure assertion.

These double as post-rebuild smoke tests: after a demo-data wipe and fresh
enrollment, this file proves enrollment → merge → search behave end to end,
and its module cleanup leaves no residue (every identity it creates carries
the qa_ prefix).
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
FACE_C = f"{FIXTURES}/face_c.png"
FACES_DIR = "/app/storage/faces"

TEST_PREFIX = "qa_mergesmoke_"


# ---------------------------------------------------------------------------
# helpers (house pattern from test_identity_multi_image_enrollment.py)
# ---------------------------------------------------------------------------

def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _multipart(fields, files):
    boundary = "----qamerge" + uuid_module.uuid4().hex
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
    """Create an identity from a fixture, answering the review prompt if raised.

    This module needs two SEPARATE identities to merge, and it builds them from
    face_a.jpg and face_b.jpg — two photos of the SAME person. That is exactly
    the case the enrollment decision gate stops and asks about, so the answer
    here is always "create a new person anyway": these tests are about merging
    two records, which presupposes two records.
    """
    kind = "image/png" if fixture_path.endswith(".png") else "image/jpeg"
    status, body = _http("POST", "/api/upload-person", token=token,
                         fields={"person_name": name},
                         files={"photo": (os.path.basename(fixture_path),
                                          _read(fixture_path), kind)})
    if status == 202 and body.get("decision_required"):
        # Bearer clients are CSRF-exempt (require_upload_csrf), so no
        # X-Requested-With header is needed here.
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
                value = result.rowcount          # DELETE / UPDATE
            elif fetch == "scalar":
                value = result.scalar()
            else:
                value = result.all()
            await db.commit()
            return value
    return run_async(_run())


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


@pytest.fixture(scope="module", autouse=True)
def _clean_module():
    _cleanup_prefix()
    yield
    _cleanup_prefix()


def _search_by_image(token, fixture_path, top_k=5):
    kind = "image/png" if fixture_path.endswith(".png") else "image/jpeg"
    return _http("POST", "/api/search/by-image", token=token,
                 fields={"scope": "both", "top_k": str(top_k)},
                 files={"image": (os.path.basename(fixture_path),
                                  _read(fixture_path), kind)})


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def test_merge_endpoint_requires_authentication():
    status, _body = _http("POST", "/api/admin/identities/merge",
                          body={"from_identity_id": str(uuid_module.uuid4()),
                                "to_identity_id": str(uuid_module.uuid4()),
                                "decision": "merge_existing",
                       "confirm_merge_risk": True})
    assert status in (401, 403)


def test_merge_moves_embeddings_and_marks_the_loser(token):
    winner = _enroll(token, TEST_PREFIX + "winner", FACE_A)
    loser = _enroll(token, TEST_PREFIX + "loser", FACE_B)

    status, body = _http("POST", "/api/admin/identities/merge", token=token,
                         body={"from_identity_id": loser,
                               "to_identity_id": winner,
                               "notes": "merge smoke test",
                               "decision": "merge_existing",
                       "confirm_merge_risk": True})
    assert status == 200, body

    merged_into = _sql("SELECT merged_into_id FROM identities WHERE id = :i",
                       {"i": loser}, fetch="scalar")
    assert str(merged_into) == winner, (
        "the loser must record who absorbed it, not be row-deleted")

    loser_status = _sql("SELECT status FROM identities WHERE id = :i",
                        {"i": loser}, fetch="scalar")
    assert str(loser_status).upper().endswith("MERGED")

    loser_vectors = _sql(
        "SELECT count(*) FROM identity_embeddings WHERE identity_id = :i",
        {"i": loser}, fetch="scalar")
    winner_vectors = _sql(
        "SELECT count(*) FROM identity_embeddings WHERE identity_id = :i",
        {"i": winner}, fetch="scalar")
    assert loser_vectors == 0, "the loser still owns embeddings after merge"
    assert winner_vectors == 2, (
        f"the winner should own both embeddings, has {winner_vectors}")


def test_search_returns_the_winner_never_the_merged_loser(token):
    winner = _enroll(token, TEST_PREFIX + "searchwin", FACE_C)
    loser = _enroll(token, TEST_PREFIX + "searchlose", FACE_A)

    status, _ = _http("POST", "/api/admin/identities/merge", token=token,
                      body={"from_identity_id": loser,
                            "to_identity_id": winner,
                            "decision": "merge_existing",
                       "confirm_merge_risk": True})
    assert status == 200

    # Probe with the LOSER's enrollment photo: its embedding now belongs to
    # the winner, so the winner must come back and the loser must not.
    status, results = _search_by_image(token, FACE_A)
    assert status == 200, results
    returned = {r.get("identity_id") for r in results}
    assert loser not in returned, "a merged identity is still searchable"
    assert winner in returned, (
        "the winner did not inherit the loser's searchability")


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def test_deletion_removes_rows_files_and_searchability(token):
    """No DELETE endpoint exists, so deletion is the retention/maintenance
    path: FK-ordered row removal plus the gallery folder. This asserts the
    system is actually clean afterwards — rows, files, and the index."""
    victim = _enroll(token, TEST_PREFIX + "todelete", FACE_B)
    folder = os.path.join(FACES_DIR, victim)
    assert os.path.isdir(folder) and os.listdir(folder), (
        "enrollment must have written the gallery folder")

    _delete_identity(victim)

    remaining = _sql(
        "SELECT "
        " (SELECT count(*) FROM identities WHERE id = :i)"
        " + (SELECT count(*) FROM identity_embeddings WHERE identity_id = :i)"
        " + (SELECT count(*) FROM identity_images WHERE identity_id = :i)",
        {"i": victim}, fetch="scalar")
    assert remaining == 0, "deletion left database rows behind"
    assert not os.path.isdir(folder), "deletion left the gallery folder behind"

    status, results = _search_by_image(token, FACE_B)
    assert status == 200, results
    assert victim not in {r.get("identity_id") for r in results}, (
        "a deleted identity is still returned by vector search")


# ---------------------------------------------------------------------------
# Post-rebuild invariants
# ---------------------------------------------------------------------------

def test_every_active_identity_has_at_least_one_embedding():
    """After a wipe + fresh enrollment there must be no orphan identities —
    a person without a vector can never be recognized or searched."""
    orphans = _sql(
        "SELECT count(*) FROM identities i "
        "WHERE i.status = 'ACTIVE' AND NOT EXISTS "
        " (SELECT 1 FROM identity_embeddings e WHERE e.identity_id = i.id)",
        fetch="scalar")
    assert orphans == 0, f"{orphans} active identit(ies) own no embedding"


def test_faces_dir_holds_only_uuid_folders():
    if not os.path.isdir(FACES_DIR):
        return
    for entry in os.listdir(FACES_DIR):
        if entry.startswith("."):
            continue  # .incoming staging area
        full = os.path.join(FACES_DIR, entry)
        assert os.path.isdir(full), f"loose file in the gallery: {entry}"
        uuid_module.UUID(entry)  # raises on a display-name folder


def test_schema_head_is_current():
    head = _sql("SELECT version_num FROM alembic_version", fetch="scalar")
    assert head == "c5e7f9a1b3d4", head
