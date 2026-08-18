"""Enrollment asks before it duplicates a person.

    docker exec face_recognition_api python -m pytest \
        tests/test_enrollment_decision_gate.py -v

THE DEFECT. Name lookup in enrollment is TEXTUAL. On its own that meant any
spelling the system had not seen minted a fresh identity UUID — no similarity
check existed anywhere in `enroll_image`, which never called a vector search at
all. So a second photo of an already-enrolled person, uploaded as "Jon Smith"
instead of "John Smith", became a SECOND identity holding a second embedding of
the same face. Recognition then answered with whichever vector scored higher,
non-deterministically, and the upload returned HTTP 200 with a green toast.
Worse, `identity_created=True` SKIPPED the checksum duplicate check entirely,
so even byte-identical bytes re-enrolled under the new name.

WHAT REPLACED IT. A name-based upload that matches somebody we already have is
parked and returned as a decision (HTTP 202) instead of being enrolled. The
administrator answers: add to that person, create a new person anyway, or
cancel.

WHAT THIS IS NOT. It is not a block. Every outcome stays reachable, including
"create a new person anyway" for real lookalikes and twins. And it does NOT
stand between an identity and its second photo: different images of one person
under one UUID is the supported case and is asserted below.

ATOMICITY IS THE HEADLINE ASSERTION. While an upload is parked, nothing durable
exists for it — no identity, no identity_images row, no identity_embeddings
row, no gallery folder, no vector-index entry — and the tests below check all
of them, not just the first.
"""

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
PENDING_DIR = "/app/storage/pending"
INCOMING_DIR = "/app/storage/faces/.incoming"
REVIEW_SRC = "/app/backend/routes/enrollment_review.py"

FIXTURES = "/app/tests/fixtures/faces"
# face_a and face_b are two DIFFERENT photos of the SAME person — the case the
# gate exists for, and the case that must still be allowed onto one UUID.
FACE_A = f"{FIXTURES}/face_a.jpg"
FACE_B = f"{FIXTURES}/face_b.jpg"
# A different person entirely.
FACE_C = f"{FIXTURES}/face_c.png"
CROPPED_FACE = f"{FIXTURES}/cropped_face.jpg"
TWO_FACES = f"{FIXTURES}/two_faces.jpg"

TEST_PREFIX = "qa_gate_"
SECOND_ADMIN = TEST_PREFIX + "other_admin"
SECOND_ADMIN_PASSWORD = "Gate-Test-Pw-8f3a2b7c!"


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
    boundary = "----qagate" + uuid_module.uuid4().hex
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
          headers=None, timeout=180):
    data = None
    headers = dict(headers or {})
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
            if fetch == "scalar":
                return result.scalar()
            if fetch == "none":
                await db.commit()
                return None
            return result.all()
    return run_async(_run())


def _unique(suffix):
    return f"{TEST_PREFIX}{suffix}_{uuid_module.uuid4().hex[:8]}"


def _upload(token, name, fixture, is_face_image=False):
    kind = "image/png" if fixture.endswith(".png") else "image/jpeg"
    return _http("POST", "/api/upload-person", token=token,
                 fields={"person_name": name,
                         "is_face_image": str(is_face_image).lower()},
                 files={"photo": (os.path.basename(fixture),
                                  _read(fixture), kind)})


def _confirm(token, **payload):
    return _http("POST", "/api/enrollment/confirm", token=token, body=payload)


def _enroll_new(token, name, fixture, is_face_image=False):
    """Create an identity, answering the review prompt when it appears."""
    status, body = _upload(token, name, fixture, is_face_image)
    if status == 202 and body.get("decision_required"):
        status, body = _confirm(token, action="create_new", display_name=name,
                                upload_token=body["upload_token"],
                                confirm_create_new=True)
    assert status == 200 and body.get("success"), body
    return body["identity_id"]


def _pending_count():
    """How many tickets exist BECAUSE OF this module.

    Every call site already meant this — they were written against a table
    that only ever held their own rows. Counting the whole table would answer
    a different question the moment the database holds anything else.
    """
    return len(_owned_pending_ids())


def _ticket_alive(upload_token):
    """Is THIS ticket still claimable?

    Asked by token hash rather than by counting rows: a test that enrolls a
    second person mid-flow legitimately creates and consumes tickets of its
    own, and a global count would read those instead of the one under test.
    """
    import hashlib

    return _sql("SELECT count(*) FROM pending_enrollments WHERE token_hash = :h",
                {"h": hashlib.sha256(upload_token.encode()).hexdigest()},
                fetch="scalar") == 1


# ---------------------------------------------------------------------------
# Ownership: this module may read, modify and delete ONLY what it created
#
# The cleanup here used to be `DELETE FROM pending_enrollments` with no WHERE,
# and two tests issued table-wide UPDATEs. On an empty database that is
# invisible; against a system with real parked uploads it destroys an
# administrator's review queue. "The table is currently empty" is not a safety
# property — it is a coincidence that expires the moment someone uses the app.
#
# Tickets are created over HTTP, so the test never sees their primary keys and
# cannot simply remember its own inserts. Instead the ids present BEFORE the
# module runs are captured once; everything that appears afterwards is ours.
# Same for staged files under PENDING_DIR.
#
# This is the rule _ticket_alive already followed and explained — ask about the
# row you created, never about the table.
# ---------------------------------------------------------------------------

_PREEXISTING_PENDING_IDS: set = set()
_PREEXISTING_PENDING_FILES: set = set()


def _capture_pending_baseline():
    """Snapshot what already existed. Called once, before any test runs."""
    global _PREEXISTING_PENDING_IDS, _PREEXISTING_PENDING_FILES
    _PREEXISTING_PENDING_IDS = {
        row[0] for row in _sql("SELECT id FROM pending_enrollments")}
    _PREEXISTING_PENDING_FILES = set(
        os.listdir(PENDING_DIR)) if os.path.isdir(PENDING_DIR) else set()


def _owned_pending_ids():
    """Primary keys of the tickets THIS module created."""
    return [row[0] for row in _sql("SELECT id FROM pending_enrollments")
            if row[0] not in _PREEXISTING_PENDING_IDS]


def _pending_files():
    """Staged files this module created — never the ones it found."""
    if not os.path.isdir(PENDING_DIR):
        return []
    return sorted(name for name in os.listdir(PENDING_DIR)
                  if name not in _PREEXISTING_PENDING_FILES)


def _incoming_files():
    return sorted(os.listdir(INCOMING_DIR)) if os.path.isdir(INCOMING_DIR) else []


def _counts(identity_id):
    images = _sql("SELECT count(*) FROM identity_images WHERE identity_id = :i",
                  {"i": identity_id}, fetch="scalar")
    embeddings = _sql("SELECT count(*) FROM identity_embeddings WHERE identity_id = :i",
                      {"i": identity_id}, fetch="scalar")
    primaries = _sql("SELECT count(*) FROM identity_images "
                     "WHERE identity_id = :i AND is_primary",
                     {"i": identity_id}, fetch="scalar")
    return images, embeddings, primaries


def _cleanup_prefix():
    rows = _sql("SELECT id FROM identities WHERE display_name LIKE :p",
                {"p": TEST_PREFIX + "%"})
    for row in rows:
        identity_id = str(row[0])
        _sql("DELETE FROM identity_embeddings WHERE identity_id = :i",
             {"i": identity_id}, fetch="none")
        _sql("DELETE FROM identity_images WHERE identity_id = :i",
             {"i": identity_id}, fetch="none")
        _sql("DELETE FROM identities WHERE id = :i", {"i": identity_id}, fetch="none")
        folder = os.path.join(FACES_DIR, identity_id)
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
    _delete_owned_pending()
    _sql("DELETE FROM users WHERE username = :u", {"u": SECOND_ADMIN}, fetch="none")


@pytest.fixture(scope="module", autouse=True)
def _clean_module():
    # FIRST, before anything can create a ticket: everything already in the
    # table belongs to somebody else and is off-limits for the whole module.
    _capture_pending_baseline()
    _cleanup_prefix()
    yield
    _cleanup_prefix()


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         body={"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


@pytest.fixture(scope="module")
def other_token(token):
    """A SECOND administrator, for the cross-user authorization tests.

    POST /api/users deliberately refuses to create administrators — promotion
    is not something that endpoint does (see backend/routes/users.py) — so the
    account is created as an analyzer through the real API and then promoted
    directly. That is test-harness setup, not a code path under test: what is
    being tested is that one administrator cannot approve another's upload,
    which needs two accounts that BOTH clear `require_role(["admin"])`.
    """
    from backend.auth.password import hash_password

    # Inserted directly with role='admin' rather than created through
    # POST /api/users and promoted afterwards. That two-step dance produced a
    # token still carrying role='analyzer' — the account is minted at one
    # moment and the JWT at another, and the promotion did not reliably land in
    # between. Creating the row in its final state removes the window entirely.
    _sql("DELETE FROM users WHERE username = :u", {"u": SECOND_ADMIN}, fetch="none")
    _sql("""INSERT INTO users (username, email, password_hash, full_name, role,
                               is_active, can_use_chatbot, created_at, updated_at)
            VALUES (:u, :e, :p, 'Gate Test Second Admin', 'admin',
                    true, false, now(), now())""",
         {"u": SECOND_ADMIN, "e": f"{SECOND_ADMIN}@example.invalid",
          "p": hash_password(SECOND_ADMIN_PASSWORD)}, fetch="none")

    stored_role = _sql("SELECT role FROM users WHERE username = :u",
                       {"u": SECOND_ADMIN}, fetch="scalar")
    assert stored_role == "admin", f"second admin was stored as {stored_role!r}"

    status, body = _http("POST", "/api/auth/login",
                         body={"username": SECOND_ADMIN,
                               "password": SECOND_ADMIN_PASSWORD})
    assert status == 200, f"the second administrator cannot log in: {status} {body}"

    # Fail here, not deep inside a test: a token carrying the wrong role turns
    # the cross-user assertions into a confusing 403 about role, when what they
    # test is that one ADMIN cannot touch another admin's upload.
    import base64
    claims = body["access_token"].split(".")[1]
    claims += "=" * (-len(claims) % 4)
    role = json.loads(base64.urlsafe_b64decode(claims)).get("role")
    assert role == "admin", f"the second admin's token carries role={role!r}"
    return body["access_token"]


class _no_match_for:
    """Make a fixture face genuinely unmatched, then put the world back.

    Every fixture face in this repository is ALREADY enrolled by an ambient
    demo identity, so "upload a face nobody has" is not a state that exists
    here. Rather than assume one, this creates it: every searchable identity
    that would match the face is set INACTIVE for the duration and restored
    afterwards. INACTIVE is outside the searchable set (ACTIVE + PROMOTED),
    which `test_an_inactive_identity_is_not_offered` proves independently.
    """

    def __init__(self, fixture):
        self.fixture = fixture
        self.restore = []

    def __enter__(self):
        from backend.core.enrollment_service import prepare_upload
        from backend.core.vector_index.access import search_similar_embeddings
        from config import settings
        from db_connection import db_manager

        prepared = prepare_upload(_read(self.fixture),
                                  original_filename=os.path.basename(self.fixture))

        async def _run():
            if not getattr(db_manager, "_initialized", False):
                await db_manager.init_db()
            async with db_manager.get_session() as db:
                # Deliberately NOT find_similar_identities: that caps at
                # ENROLL_MAX_CANDIDATES, and a holder left ACTIVE beyond the cap
                # is exactly the one that would make these tests flaky.
                hits = await search_similar_embeddings(
                    db, prepared.embedding_normalized, top_k=500,
                    threshold=float(settings.ENROLL_CANDIDATE_MIN),
                    identity_type="known")
                return sorted({str(hit["identity_id"]) for hit in hits})
        ranked = run_async(_run())

        for identity_id in ranked:
            row = _sql("SELECT status::text FROM identities WHERE id = :i",
                       {"i": identity_id}, fetch="scalar")
            if row is None:
                continue
            self.restore.append((identity_id, row))
            _sql("UPDATE identities SET status = 'INACTIVE' WHERE id = :i",
                 {"i": identity_id}, fetch="none")
        return self

    def __exit__(self, *exc):
        for identity_id, status in self.restore:
            _sql("UPDATE identities SET status = :s WHERE id = :i",
                 {"s": status, "i": identity_id}, fetch="none")
        return False


def _delete_owned_pending():
    """Remove this module's tickets and staged files. Nothing else.

    Deletes by explicit primary key rather than by predicate, so there is no
    expression that could ever widen to somebody else's row.
    """
    owned = _owned_pending_ids()
    if owned:
        _sql("DELETE FROM pending_enrollments WHERE id = ANY(:ids)",
             {"ids": owned}, fetch="none")
    for name in _pending_files():
        try:
            os.remove(os.path.join(PENDING_DIR, name))
        except OSError:
            pass


def _clear_pending():
    _delete_owned_pending()


@pytest.fixture(autouse=True)
def _isolate_pending():
    """One test, one claim ticket.

    Several tests below deliberately leave a ticket alive — that is the point
    of a recoverable refusal — so without this the table accumulates and any
    assertion phrased as "exactly one pending row" would read someone else's.
    """
    _clear_pending()
    yield
    _clear_pending()


@pytest.fixture
def parked(token):
    """A deterministic world with one upload awaiting a decision.

    Yields (owner_id, attempted_name, decision_body) where `owner` is the ONLY
    searchable identity matching either fixture face, so "the right person was
    recommended" is a stable assertion.

    That isolation is necessary, not decorative: this repository's fixtures are
    shared, and face_a alone is already held by several ambient identities. Left
    active they fill the candidate list — all at the same similarity — and which
    ones survive the cap is arbitrary.

    The upload is face_b against an owner holding face_a: two DIFFERENT photos
    of one person, scoring 0.4299, which is the UNCERTAIN band. That is the
    case that used to mint a second UUID in silence, and the one where adding
    the photo to the existing person is a real state change rather than a
    byte-identical duplicate.
    """
    with _no_match_for(FACE_A):
        owner = _enroll_new(token, _unique("owner"), FACE_A)
        name = _unique("rival")
        status, body = _upload(token, name, FACE_B)
        assert status == 202, f"expected a decision, got {status}: {body}"
        assert body.get("decision_required") is True, body
        yield owner, name, body


@pytest.fixture
def parked_strong(token):
    """The same, but the upload is byte-identical, so similarity is 1.0.

    Yields (owner_id, attempted_name, decision_body) in the STRONG band.
    """
    with _no_match_for(FACE_A):
        owner = _enroll_new(token, _unique("sowner"), FACE_A)
        name = _unique("srival")
        status, body = _upload(token, name, FACE_A)
        assert status == 202, f"expected a decision, got {status}: {body}"
        assert body["match_confidence"] == "strong", body
        yield owner, name, body


# ---------------------------------------------------------------------------
# 1. No match: behaviour is byte-for-byte what it was before the gate
# ---------------------------------------------------------------------------

def test_an_unmatched_upload_enrolls_directly_with_the_legacy_payload(token):
    """The gate must be invisible when there is nothing to ask about."""
    before_rows, before_files = _pending_count(), _pending_files()
    name = _unique("solo")
    with _no_match_for(FACE_C):
        status, body = _upload(token, name, FACE_C)

    assert status == 200, body
    assert body.get("decision_required") is None, (
        "an unmatched upload must not raise a decision")
    assert body["identity_created"] is True
    for field in ("success", "message", "filename", "identity_id",
                  "identity_created", "image_created", "embedding_created",
                  "is_primary", "total_identities", "total_faces", "backend"):
        assert field in body, f"legacy response lost the {field!r} field"

    assert _pending_count() == before_rows, "an unmatched upload parked a ticket"
    assert _pending_files() == before_files, "an unmatched upload left a file"


def test_two_photos_of_one_person_score_above_the_candidate_floor():
    """The calibration this whole flow depends on, measured rather than assumed.

    face_a and face_b are two different photos of the same person. If their
    similarity fell below ENROLL_CANDIDATE_MIN, uploading the second under a
    new name would sail past the gate and silently mint the duplicate identity
    — the exact defect being fixed — while recognition, whose own bar is
    SIMILARITY_THRESHOLD, would still call them one person and report either
    name at random.

    Measured value at the time of writing: 0.4299, against a floor of 0.40.
    Unrelated fixture faces score below 0.05, so the margin above is large.
    """
    import numpy as np

    from backend.core.enrollment_service import prepare_upload
    from config import settings

    a = prepare_upload(_read(FACE_A), original_filename="face_a.jpg")
    b = prepare_upload(_read(FACE_B), original_filename="face_b.jpg")
    same_person = float(np.dot(a.embedding_normalized, b.embedding_normalized))

    assert same_person >= float(settings.ENROLL_CANDIDATE_MIN), (
        f"two photos of one person score {same_person:.4f}, below the "
        f"{settings.ENROLL_CANDIDATE_MIN} floor — the duplicate they would "
        "create is invisible to the review gate")
    assert float(settings.ENROLL_CANDIDATE_MIN) <= float(settings.SIMILARITY_THRESHOLD), (
        "the candidate floor is above recognition's own match threshold, so "
        "faces recognition treats as one person enroll as two without asking")

    c = prepare_upload(_read(FACE_C), original_filename="face_c.png")
    different = float(np.dot(a.embedding_normalized, c.embedding_normalized))
    assert different < float(settings.ENROLL_CANDIDATE_MIN), (
        f"two different people score {different:.4f}, at or above the floor — "
        "the gate would interrupt unrelated enrollments")


# ---------------------------------------------------------------------------
# 2 + 3. A match returns a decision, and NOTHING durable is created
# ---------------------------------------------------------------------------

def test_a_strong_match_recommends_the_correct_identity(token, parked_strong):
    owner, name, body = parked_strong

    assert body["recommended_action"] == "add_to_existing", body
    assert body["match_confidence"] == "strong", body
    assert body["candidate_identities"][0]["identity_id"] == owner, (
        f"the wrong person was recommended: {body['candidate_identities']}")
    assert body["candidate_identities"][0]["similarity"] >= 0.75
    assert _sql("SELECT count(*) FROM identities WHERE display_name = :n",
                {"n": name}, fetch="scalar") == 0, "an identity was created"


def test_an_uncertain_match_returns_the_candidates_for_review(token):
    """Two people already on file, each holding a photo of the same person.

    Both score in the uncertain band against a third photo, so the response
    must carry BOTH for the administrator to choose between — this is the case
    the server must not resolve on its own.
    """
    with _no_match_for(FACE_A):
        first = _enroll_new(token, _unique("twinA"), FACE_A)
        # A second record of the same face. Reaching this state requires the
        # explicit override, which is what makes it a deliberate duplicate.
        name = _unique("twinB")
        status, body = _upload(token, name, FACE_A)
        assert status == 202, body
        status, created = _confirm(token, action="create_new", display_name=name,
                                   upload_token=body["upload_token"],
                                   confirm_create_new=True)
        assert status == 200, created
        second = created["identity_id"]

        status, decision = _upload(token, _unique("twinC"), FACE_B)
        assert status == 202, decision
        assert decision["match_confidence"] == "uncertain", decision
        assert decision["recommended_action"] == "review", decision

        offered = {c["identity_id"] for c in decision["candidate_identities"]}
        assert {first, second} <= offered, (
            f"both possible people must be offered, got {offered}")
        for candidate in decision["candidate_identities"]:
            assert 0.40 <= candidate["similarity"] < 0.75, candidate


def test_a_decision_creates_nothing_durable(token, parked):
    owner, name, body = parked

    assert body["success"] is False, (
        "success must be false: no person was enrolled")
    candidates = body["candidate_identities"]
    assert candidates, "a decision with no candidates is unanswerable"
    assert any(c["identity_id"] == owner for c in candidates), (
        f"the person holding the same face was not offered: {candidates}")
    top = candidates[0]
    for field in ("identity_id", "display_name", "similarity", "preview_image",
                  "confidence_band"):
        assert field in top, f"candidate is missing {field!r}"
    from config import settings
    assert top["similarity"] >= float(settings.ENROLL_CANDIDATE_MIN), top
    assert top["preview_image"], "candidate has no preview image"
    assert body["upload_token"], "no upload token was issued"
    assert body["expires_at"], "no expiry was published"

    # --- ATOMICITY: none of the five durable artifacts may exist -------------
    assert _sql("SELECT count(*) FROM identities WHERE display_name = :n",
                {"n": name}, fetch="scalar") == 0, "an identity was created"
    # The parked bytes live in the pending store and NOWHERE under the gallery.
    # (Asserting "no image row has this checksum" would be wrong: these fixtures
    # are shared, and another identity may legitimately already hold this file.)
    parked_files = _pending_files()
    assert len(parked_files) == 1, parked_files
    for root, _dirs, files in os.walk(FACES_DIR):
        assert not any(f in parked_files for f in files), (
            f"the parked upload reached the gallery at {root}")

    # The embedding count for the owner is unchanged: one, from face_a.
    images, embeddings, _ = _counts(owner)
    assert (images, embeddings) == (1, 1), (
        f"the parked upload leaked rows onto the candidate: {images}/{embeddings}")
    assert _ticket_alive(body["upload_token"])
    assert len(_pending_files()) == 1, _pending_files()


def test_the_parked_upload_never_reaches_the_gallery(token, parked):
    """FACES_DIR holds one folder per identity UUID and nothing else."""
    _owner, _name, _body = parked
    for entry in os.listdir(FACES_DIR):
        if entry.startswith("."):
            continue                      # .incoming, the staging area
        uuid_module.UUID(entry)           # raises if a stray folder appeared


def test_the_parked_upload_adds_no_vector_anywhere(token):
    """No embedding row means nothing to index, so count the rows themselves.

    Written as its own flow rather than on the shared fixture because the only
    honest assertion is a before/after of the whole table: the parked photo has
    no identity to scope a query to, which is the entire point.
    """
    with _no_match_for(FACE_A):
        _owner = _enroll_new(token, _unique("novec"), FACE_A)
        before = _sql("SELECT count(*) FROM identity_embeddings", fetch="scalar")
        images_before = _sql("SELECT count(*) FROM identity_images", fetch="scalar")

        status, body = _upload(token, _unique("novec_rival"), FACE_B)
        assert status == 202, body

        assert _sql("SELECT count(*) FROM identity_embeddings",
                    fetch="scalar") == before, (
            "parking an upload created a vector row")
        assert _sql("SELECT count(*) FROM identity_images",
                    fetch="scalar") == images_before, (
            "parking an upload created an image row")


# ---------------------------------------------------------------------------
# 4. add_to_existing — and NO automatic merge
# ---------------------------------------------------------------------------

def test_add_to_existing_attaches_the_photo_and_merges_nothing(token, parked):
    owner, _name, body = parked
    before = _sql("SELECT status::text, merged_into_id FROM identities "
                  "WHERE id = :i", {"i": owner})[0]
    staged_before = set(_pending_files())

    status, confirmed = _confirm(token, action="add_to_existing",
                                 identity_id=owner,
                                 upload_token=body["upload_token"])
    assert status == 200, confirmed
    assert confirmed["identity_id"] == owner
    assert confirmed["identity_created"] is False, (
        "add_to_existing must not create an identity")
    assert confirmed["image_created"] is True
    assert confirmed["embedding_created"] is True
    assert confirmed["decision_applied"] == "add_to_existing"

    images, embeddings, primaries = _counts(owner)
    assert (images, embeddings, primaries) == (2, 2, 1), (
        f"expected two images/embeddings and one primary, got "
        f"{images}/{embeddings}/{primaries}")

    after = _sql("SELECT status::text, merged_into_id FROM identities "
                 "WHERE id = :i", {"i": owner})[0]
    assert after[0] == before[0], "the identity's status changed"
    assert after[1] is None, "an automatic merge was performed"

    assert not _ticket_alive(body["upload_token"]), (
        "the claim ticket outlived its decision")
    assert not (staged_before & set(_pending_files())), (
        "the parked file was not removed")


def test_two_different_photos_of_one_person_live_under_one_uuid(token, parked):
    """The supported case, stated as its own assertion.

    The gate exists to stop a SECOND identity, never to stop a second photo.
    """
    owner, _name, body = parked
    status, _ = _confirm(token, action="add_to_existing", identity_id=owner,
                         upload_token=body["upload_token"])
    assert status == 200

    paths = [row[0] for row in _sql(
        "SELECT storage_path FROM identity_images WHERE identity_id = :i "
        "ORDER BY created_at", {"i": owner})]
    assert len(paths) == 2, paths
    assert len(set(paths)) == 2, "the two photos share a storage path"
    assert all(owner in path for path in paths), (
        f"a photo landed outside the identity's own folder: {paths}")
    folder = os.path.join(FACES_DIR, owner)
    assert len(os.listdir(folder)) == 2, os.listdir(folder)


# ---------------------------------------------------------------------------
# 5. create_new anyway — reachable, but not with one click on a strong match
# ---------------------------------------------------------------------------

def test_create_new_on_a_strong_match_needs_a_second_confirmation(
        token, parked_strong):
    _owner, name, body = parked_strong
    status, refused = _confirm(token, action="create_new", display_name=name,
                               upload_token=body["upload_token"])
    assert status == 409, refused
    assert refused["error"] == "create_new_needs_confirmation", refused
    assert refused["requires_confirmation"] is True
    assert refused["confirm_field"] == "confirm_create_new"
    # Recoverable: the ticket survives so the administrator can answer again.
    assert _ticket_alive(body["upload_token"]), (
        "a recoverable refusal consumed the ticket")


def test_the_administrator_can_reject_the_recommendation_and_create_a_new_uuid(
        token, parked):
    owner, name, body = parked
    status, created = _confirm(token, action="create_new", display_name=name,
                               upload_token=body["upload_token"],
                               confirm_create_new=True)
    assert status == 200, created
    assert created["identity_created"] is True
    assert created["identity_id"] != owner, "no new identity was created"

    statuses = dict(_sql(
        "SELECT id::text, status::text FROM identities WHERE id IN (:a, :b)",
        {"a": owner, "b": created["identity_id"]}))
    assert set(statuses.values()) == {"ACTIVE"}, statuses
    merged = _sql("SELECT count(*) FROM identities "
                  "WHERE id IN (:a, :b) AND merged_into_id IS NOT NULL",
                  {"a": owner, "b": created["identity_id"]}, fetch="scalar")
    assert merged == 0, "an automatic merge was performed"


# ---------------------------------------------------------------------------
# 6. cancel
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("via", ["confirm", "cancel_endpoint"])
def test_cancelling_leaves_no_trace(token, parked, via):
    _owner, name, body = parked
    upload_token = body["upload_token"]
    # Snapshot THIS upload's staged file. Asserting the directory is globally
    # empty reads other tests' work, which made this fail intermittently on
    # collection order rather than on behaviour.
    staged_before = set(_pending_files())

    if via == "confirm":
        status, out = _confirm(token, action="cancel", upload_token=upload_token)
    else:
        status, out = _http("POST", "/api/enrollment/cancel", token=token,
                            body={"upload_token": upload_token})
    assert status == 200, out
    assert out["cancelled"] is True
    assert out["identity_created"] is False

    assert not _ticket_alive(upload_token), "the cancelled ticket survived"
    assert not (staged_before & set(_pending_files())), (
        "the cancelled upload's staged file was left on disk")
    assert _sql("SELECT count(*) FROM identities WHERE display_name = :n",
                {"n": name}, fetch="scalar") == 0

    status, reused = _confirm(token, action="cancel", upload_token=upload_token)
    assert status == 410, reused


# ---------------------------------------------------------------------------
# 7-9. Token lifecycle: one-time, user-bound, expiring
# ---------------------------------------------------------------------------

def test_a_token_cannot_be_used_twice(token, parked):
    owner, _name, body = parked
    upload_token = body["upload_token"]

    status, _ = _confirm(token, action="add_to_existing", identity_id=owner,
                         upload_token=upload_token)
    assert status == 200

    status, reused = _confirm(token, action="add_to_existing", identity_id=owner,
                              upload_token=upload_token)
    assert status == 410, reused
    assert reused["error"] == "pending_upload_not_found"
    images, embeddings, _ = _counts(owner)
    assert (images, embeddings) == (2, 2), (
        f"the replayed token enrolled a third copy: {images}/{embeddings}")


def test_another_administrator_cannot_approve_someone_elses_upload(
        token, other_token, parked):
    owner, _name, body = parked

    status, refused = _confirm(other_token, action="add_to_existing",
                               identity_id=owner,
                               upload_token=body["upload_token"])
    assert status == 410, refused
    assert refused["error"] == "pending_upload_not_found", (
        "a foreign token must be indistinguishable from a missing one")
    assert _ticket_alive(body["upload_token"]), (
        "a foreign attempt consumed the owner's ticket")

    # The rightful owner is unaffected.
    status, ok = _confirm(token, action="add_to_existing", identity_id=owner,
                          upload_token=body["upload_token"])
    assert status == 200, ok


def test_an_expired_token_is_rejected_and_swept(token, parked):
    owner, _name, body = parked
    _sql("UPDATE pending_enrollments SET expires_at = now() - interval '1 hour' "
         "WHERE id = ANY(:ids)", {"ids": _owned_pending_ids()}, fetch="none")
    staged_before = set(_pending_files())
    assert staged_before, "the parked upload has no staged file"

    status, refused = _confirm(token, action="add_to_existing", identity_id=owner,
                               upload_token=body["upload_token"])
    assert status == 410, refused
    assert refused["error"] == "pending_upload_not_found"
    # The sweep runs at the top of the endpoint, so the row and its file are
    # already gone by the time the refusal is written.
    assert not _ticket_alive(body["upload_token"]), (
        "an expired ticket was left in the table")
    assert not (staged_before & set(_pending_files())), (
        "an expired ticket's file was left on disk")


# ---------------------------------------------------------------------------
# 10. Exact-file duplicates stay non-destructive, and are found across identities
# ---------------------------------------------------------------------------

def test_the_exact_same_file_produces_a_duplicate_notice(token):
    owner = _enroll_new(token, _unique("dup"), FACE_A)
    before = len(os.listdir(os.path.join(FACES_DIR, owner)))

    status, body = _http("POST", f"/api/identities/{owner}/images", token=token,
                         files={"photo": ("face_a.jpg", _read(FACE_A), "image/jpeg")})
    assert status == 200, body
    assert body.get("duplicate") is True, body
    assert body["image_created"] is False
    assert body["embedding_created"] is False

    images, embeddings, _ = _counts(owner)
    assert (images, embeddings) == (1, 1), "a duplicate created a second row"
    assert len(os.listdir(os.path.join(FACES_DIR, owner))) == before, (
        "a duplicate wrote a second file")


def test_an_exact_file_already_on_another_identity_is_reported_as_evidence(token):
    """Deterministic duplicate evidence, computed before any similarity score."""
    _owner = _enroll_new(token, _unique("crossdup"), FACE_A)
    status, body = _upload(token, _unique("crossdup_other"), FACE_A)

    assert status == 202, body
    reported = body["duplicate_of_identity_id"]
    assert reported, (
        f"the identity already holding this exact file was not reported: {body}")

    # Any searchable identity holding these exact bytes is a correct answer;
    # several may, and the fixtures are shared with other suites.
    holders = {str(row[0]) for row in _sql(
        "SELECT m.identity_id FROM identity_images m "
        "JOIN identities i ON i.id = m.identity_id "
        "WHERE m.file_checksum = ("
        "  SELECT file_checksum FROM pending_enrollments "
        "   WHERE id = ANY(:ids) LIMIT 1) "
        "AND i.status::text IN ('ACTIVE', 'PROMOTED')", {"ids": _owned_pending_ids()})}
    assert reported in holders, (
        f"reported {reported}, which does not hold this file: {holders}")
    # The duplicate fact is request-local evidence surfaced to the caller; the
    # ticket freezes only the OFFERED candidates (re-verified live at confirm).
    # The write-only checksum_match_identity_id column was removed (c2d3e4f5a6b7):
    # nothing ever read it.
    cols = {r[0] for r in _sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'pending_enrollments'")}
    assert "checksum_match_identity_id" not in cols, "dead column must stay removed"


# ---------------------------------------------------------------------------
# 11. Refusals write nothing anywhere
# ---------------------------------------------------------------------------

def test_a_photo_with_two_faces_parks_nothing(token):
    """Detection runs before the gate, so a refusal costs nothing anywhere."""
    before_files = _pending_files()
    status, body = _upload(token, _unique("refused"), TWO_FACES)
    assert status == 400, body
    assert body["error"] == "multiple_faces", body
    assert body.get("decision_required") is None
    assert _pending_files() == before_files, "a refused photo was parked"
    assert _incoming_files() == [], "a refused photo was left in .incoming"


def test_a_photo_with_no_face_parks_nothing(token):
    import numpy as np
    try:
        import cv2
    except ImportError:                                   # pragma: no cover
        pytest.skip("cv2 unavailable")
    blank = cv2.imencode(".jpg", np.full((256, 256, 3), 200, np.uint8))[1].tobytes()

    before_files = _pending_files()
    status, body = _http("POST", "/api/upload-person", token=token,
                         fields={"person_name": _unique("blank"),
                                 "is_face_image": "false"},
                         files={"photo": ("blank.jpg", blank, "image/jpeg")})
    assert status == 400, body
    assert body["error"] == "no_face", body
    assert _pending_files() == before_files, "a faceless photo was parked"
    assert _incoming_files() == [], "a faceless photo was left in .incoming"


# ---------------------------------------------------------------------------
# 12. State that changes BETWEEN the two phases
# ---------------------------------------------------------------------------

def test_an_identity_deactivated_between_review_and_confirmation_is_refused(
        token, parked):
    owner, _name, body = parked
    _sql("UPDATE identities SET status = 'INACTIVE' WHERE id = :i",
         {"i": owner}, fetch="none")
    try:
        status, refused = _confirm(token, action="add_to_existing",
                                   identity_id=owner,
                                   upload_token=body["upload_token"])
        assert status == 409, refused
        assert refused["error"] == "identity_not_active", refused
        assert _ticket_alive(body["upload_token"]), (
        "a recoverable refusal consumed the ticket")
    finally:
        _sql("UPDATE identities SET status = 'ACTIVE' WHERE id = :i",
             {"i": owner}, fetch="none")


def test_an_identity_merged_between_review_and_confirmation_is_refused(
        token, parked):
    """A merged record's rows belong to its successor; writing here strands them."""
    owner, _name, body = parked
    survivor = _enroll_new(token, _unique("survivor"), FACE_C)
    _sql("UPDATE identities SET merged_into_id = :s WHERE id = :i",
         {"s": survivor, "i": owner}, fetch="none")
    try:
        status, refused = _confirm(token, action="add_to_existing",
                                   identity_id=owner,
                                   upload_token=body["upload_token"])
        assert status == 409, refused
        assert refused["error"] == "identity_not_active", refused
        assert _ticket_alive(body["upload_token"])
    finally:
        _sql("UPDATE identities SET merged_into_id = NULL WHERE id = :i",
             {"i": owner}, fetch="none")


def test_an_identity_deleted_between_review_and_confirmation_is_refused(
        token, parked):
    owner, _name, body = parked
    _sql("DELETE FROM identity_embeddings WHERE identity_id = :i",
         {"i": owner}, fetch="none")
    _sql("DELETE FROM identity_images WHERE identity_id = :i",
         {"i": owner}, fetch="none")
    _sql("DELETE FROM identities WHERE id = :i", {"i": owner}, fetch="none")

    status, refused = _confirm(token, action="add_to_existing", identity_id=owner,
                               upload_token=body["upload_token"])
    assert status == 404, refused
    assert refused["error"] == "identity_not_found", refused


def test_losing_permission_between_review_and_confirmation_is_refused(
        token, other_token, parked):
    """Authorization is re-read from the LIVE user row, not from the token."""
    owner, _name, _body = parked
    name = _unique("permloss")
    status, decision = _upload(other_token, name, FACE_B)
    assert status == 202, f"the second admin's upload was not parked: {decision}"

    _sql("UPDATE users SET role = 'observer' WHERE username = :u",
         {"u": SECOND_ADMIN}, fetch="none")
    try:
        status, refused = _confirm(other_token, action="add_to_existing",
                                   identity_id=decision["candidate_identities"][0]["identity_id"],
                                   upload_token=decision["upload_token"])
        # require_role rejects at the door, or the live re-read inside the route
        # does (identity_forbidden). Either way the write does not happen — and
        # the token that opened the review is still cryptographically valid, so
        # this is precisely the case a token-only check would have missed.
        assert status == 403, refused
        assert _sql("SELECT count(*) FROM identities WHERE display_name = :n",
                    {"n": name}, fetch="scalar") == 0
    finally:
        _sql("UPDATE users SET role = 'admin' WHERE username = :u",
             {"u": SECOND_ADMIN}, fetch="none")


def test_a_model_change_between_review_and_confirmation_is_refused(token, parked):
    """The embedding is recomputed at confirmation.

    Under different weights that vector is not the one the candidates were
    ranked against, so the similarity the administrator saw no longer describes
    the comparison being made.
    """
    owner, _name, body = parked
    _sql("UPDATE pending_enrollments SET embedding_model_version = :v "
         "WHERE id = ANY(:ids)",
         {"v": "some_other_model_v9", "ids": _owned_pending_ids()}, fetch="none")

    status, refused = _confirm(token, action="add_to_existing", identity_id=owner,
                               upload_token=body["upload_token"])
    assert status == 409, refused
    assert refused["error"] == "model_version_changed", refused
    images, embeddings, _ = _counts(owner)
    assert (images, embeddings) == (1, 1), "the stale-model decision still enrolled"


def test_the_ticket_records_the_models_that_produced_its_embedding(token, parked):
    from backend.core.enrollment_service import (detection_model_version,
                                                 embedding_model_version)

    row = _sql("SELECT embedding_model_version, detection_model_version, "
               "       is_face_image, display_name_key, file_checksum, "
               "       user_id, created_at, expires_at "
               "FROM pending_enrollments WHERE id = ANY(:ids)",
               {"ids": _owned_pending_ids()}, fetch="all")[0]
    assert row[0] == embedding_model_version(), row
    assert row[1] == detection_model_version(), row
    assert row[2] is False
    assert row[3] == row[3].casefold(), "the name key was not normalized"
    assert re.fullmatch(r"[0-9a-f]{64}", row[4]), "checksum is not a sha-256 hex"
    assert row[5] is not None, "the ticket is not bound to a user"
    assert row[7] > row[6], "expiry is not after creation"


# ---------------------------------------------------------------------------
# A choice that was never offered
# ---------------------------------------------------------------------------

def test_an_identity_that_was_not_offered_cannot_be_chosen(token, parked):
    _owner, _name, body = parked
    outsider = _enroll_new(token, _unique("outsider"), FACE_C)

    status, refused = _confirm(token, action="add_to_existing",
                               identity_id=outsider,
                               upload_token=body["upload_token"])
    assert status == 409, refused
    assert refused["error"] == "identity_not_offered", refused
    images, embeddings, _ = _counts(outsider)
    assert (images, embeddings) == (1, 1), (
        "a photo was attached to a person who was never suggested")
    assert _ticket_alive(body["upload_token"]), (
        "a recoverable refusal consumed the ticket")


# ---------------------------------------------------------------------------
# Search scope: ACTIVE and PROMOTED
# ---------------------------------------------------------------------------

def test_a_promoted_identity_is_searched_and_can_be_chosen(token):
    """PROMOTED is half of the searchable set, so it must also be selectable.

    Offering a candidate and then refusing it is a dead end: the administrator
    is shown the right answer and cannot act on it.
    """
    with _no_match_for(FACE_A):
        owner = _enroll_new(token, _unique("promoted"), FACE_A)
        _sql("UPDATE identities SET status = 'PROMOTED' WHERE id = :i",
             {"i": owner}, fetch="none")

        status, body = _upload(token, _unique("promoted_rival"), FACE_B)
        assert status == 202, f"a PROMOTED identity was not searched: {body}"
        offered = [c["identity_id"] for c in body["candidate_identities"]]
        assert owner in offered, f"the PROMOTED identity was not offered: {offered}"

        status, confirmed = _confirm(token, action="add_to_existing",
                                     identity_id=owner,
                                     upload_token=body["upload_token"])
        assert status == 200, confirmed
        images, embeddings, _ = _counts(owner)
        assert (images, embeddings) == (2, 2), (
            "the photo did not attach to the PROMOTED identity")


def test_an_inactive_identity_is_not_offered(token):
    """The searchable set is ACTIVE + PROMOTED; INACTIVE is neither."""
    owner = _enroll_new(token, _unique("inactive"), FACE_A)
    _sql("UPDATE identities SET status = 'INACTIVE' WHERE id = :i",
         {"i": owner}, fetch="none")
    try:
        status, body = _upload(token, _unique("inactive_rival"), FACE_B)
        offered = [c["identity_id"] for c in body.get("candidate_identities", [])]
        assert owner not in offered, "an INACTIVE identity was offered as a match"
    finally:
        _sql("UPDATE identities SET status = 'ACTIVE' WHERE id = :i",
             {"i": owner}, fetch="none")


# ---------------------------------------------------------------------------
# Ordering: name resolution happens BEFORE the gate
# ---------------------------------------------------------------------------

def test_an_existing_name_is_never_sent_for_review(token):
    """The operator already answered "who is this?" by typing a known name."""
    name = _unique("known")
    owner = _enroll_new(token, name, FACE_A)

    status, body = _upload(token, name, FACE_B)
    assert status == 200, f"a known name was sent for review: {body}"
    assert body["identity_id"] == owner
    assert body["identity_created"] is False
    images, _embeddings, _ = _counts(owner)
    assert images == 2


# ---------------------------------------------------------------------------
# Token secrecy
# ---------------------------------------------------------------------------

def test_the_raw_token_is_never_stored(token, parked):
    _owner, _name, body = parked
    stored = _sql("SELECT token_hash FROM pending_enrollments "
                  "WHERE id = ANY(:ids)", {"ids": _owned_pending_ids()},
                  fetch="scalar")
    assert re.fullmatch(r"[0-9a-f]{64}", stored), stored
    assert stored != body["upload_token"], "the raw token was stored verbatim"

    import hashlib
    assert stored == hashlib.sha256(
        body["upload_token"].encode()).hexdigest(), (
        "the stored hash does not correspond to the issued token")


def test_the_decision_response_leaks_no_paths_or_vectors(token, parked):
    _owner, _name, body = parked
    blob = json.dumps(body)
    for needle in ("/app/", "storage/pending/", "storage/faces/.incoming"):
        assert needle not in blob, f"the decision response leaked {needle!r}"
    assert '"embedding"' not in blob, "the decision response exposed a vector"


# ---------------------------------------------------------------------------
# Contract scans
# ---------------------------------------------------------------------------

def test_the_confirm_route_delegates_to_the_shared_service():
    """The third enroll_image call site, held to the same rule as the other two.

    tests/test_identity_multi_image_enrollment.py pins upload.py at exactly two
    call sites. That count is what stops a parallel enrollment implementation
    from growing there, so the confirmation path lives in its own module — and
    inherits the same scan rather than escaping it.
    """
    code = _code_only(_source(REVIEW_SRC))
    assert code.count("await enroll_image(") == 1, (
        "the confirm route must delegate exactly once to the shared service")
    for forbidden in ("get_embedding", "detector.detect", "face_db.add_face",
                      "center_x", "center_y"):
        assert forbidden not in code, (
            f"the confirm route re-implements {forbidden!r} instead of delegating")


def test_the_confirm_route_never_merges_identities():
    """Requirement, machine-checked: no automatic merge, ever."""
    code = _code_only(_source(REVIEW_SRC))
    assert "merge_identities" not in code
    assert "merged_into_id =" not in code


def test_the_review_routes_never_leak_exception_text():
    code = _code_only(_source(REVIEW_SRC))
    assert "str(e)" not in code and "str(exc)" not in code, (
        "raw exception text must never reach the client")


def test_a_decision_is_not_reported_as_a_server_failure():
    """202, not 4xx/5xx.

    A review prompt is the workflow working. Routed through the error path it
    would be counted as a client error by every dashboard and alert watching
    this endpoint, and operators would chase a steady stream of "errors" from a
    feature behaving exactly as designed.
    """
    from backend.routes.upload import HTTP_DECISION_REQUIRED

    assert 200 <= HTTP_DECISION_REQUIRED < 300, HTTP_DECISION_REQUIRED


def test_the_pending_store_is_outside_the_gallery():
    """FACES_DIR holds identity UUID folders; a parked upload has no identity."""
    from config import settings

    faces = os.path.abspath(settings.FACES_DIR)
    pending = os.path.abspath(settings.PENDING_UPLOAD_DIR)
    assert os.path.commonpath([faces, pending]) != faces, (
        "the pending store is inside the gallery")
    assert os.path.dirname(pending) == os.path.abspath(settings.STORAGE_DIR), (
        "the pending store must be a sibling of faces/, on the same filesystem")


# ---------------------------------------------------------------------------
# The dialog the administrator actually answers
# ---------------------------------------------------------------------------

MODAL_JS = "/app/frontend/js/upload-modal.js"
MODAL_HTML = "/app/frontend/components/upload-modal.html"


def _js_code_only(source):
    """Drop // comments and /* */ blocks.

    This file explains in its own comments which APIs it deliberately avoids,
    and a raw text scan cannot tell an explanation from a call.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return "\n".join(
        re.sub(r"//.*$", "", line) for line in without_blocks.splitlines())


def test_the_modal_treats_202_as_a_decision_not_an_error():
    """The branch must come BEFORE the generic failure handling.

    Reached second, a decision would be rendered as "Upload Failed" — which is
    both wrong and the opposite of what the operator needs to do next.
    """
    source = _js_code_only(_source(MODAL_JS))
    decision = source.index("decision_required")
    failure = source.index("!response.ok || !data.success")
    assert decision < failure, (
        "the decision branch runs after the error branch, so a review prompt "
        "would be shown as an upload failure")
    assert "response.status === 202" in source


def test_the_modal_offers_all_three_answers():
    html = _source(MODAL_HTML)
    js = _source(MODAL_JS)
    for action in ("enrollmentAddToExisting", "enrollmentCreateNew",
                   "enrollmentCancel"):
        assert action in js, f"{action} is not implemented"
    for markup in ('data-action="enrollmentCreateNew"',
                   'data-action="enrollmentCancel"'):
        assert markup in html, f"the panel is missing {markup}"
    assert "enrollmentAddToExisting" in js, (
        "the per-candidate action must be attached to the rendered rows")


def test_every_decision_action_is_registered():
    """Actions.register is the only way a data-action becomes invocable.

    tests/test_advanced_search_page.py enforces this globally; asserting it
    here too keeps the failure next to the code that would cause it.
    """
    js = _source(MODAL_JS)
    registration = js[js.index("Actions.register({"):]
    for action in ("enrollmentAddToExisting", "enrollmentCreateNew",
                   "enrollmentCancel"):
        assert re.search(rf"^\s*{action}:", registration, re.M), (
            f"{action} has a data-action but is not registered")


def test_closing_the_dialog_cancels_the_upload():
    """An abandoned review must not leave a file and a live token behind."""
    js = _js_code_only(_source(MODAL_JS))
    close = js[js.index("function closeUploadModal()"):]
    close = close[:close.index("\n}")]
    assert "cancelPendingUpload" in close, (
        "closing the modal leaves the parked upload to expire on its own")


def test_candidate_names_are_not_interpolated_into_html():
    """display_name is operator-supplied text and is rendered with textContent."""
    js = _js_code_only(_source(MODAL_JS))
    builder = js[js.index("function buildCandidateRow("):]
    builder = builder[:builder.index("\n    return row;")]
    assert "innerHTML" not in builder, (
        "candidate rows build markup from a display name with innerHTML")
    assert "textContent" in builder


def test_a_duplicate_confirm_is_not_presented_as_an_addition():
    """A duplicate is a successful no-op, and must not look like an add.

    THE REPORTED BUG. Re-uploading a photo an identity already holds correctly
    stores nothing — the checksum rule refuses to write a byte-identical
    image_002.jpg. But the modal rendered that outcome with the same green tick
    as a real addition, so operators confirmed "Add to this person" and then
    went looking under storage/faces/<uuid>/ for a file the server had rightly
    declined to write. Three such confirmations were logged before anyone
    questioned the tick rather than the backend.
    """
    js = _js_code_only(_source(MODAL_JS))

    # Both success paths must branch: the review flow AND the direct upload.
    assert js.count("data.image_created === false") >= 2, (
        "a duplicate is still reported as an addition on at least one path")
    assert js.count("data.duplicate === true") >= 2

    # And the branch must not reuse the green success banner.
    for marker in ("'Already On File'", '"Already On File"'):
        if marker in js:
            break
    else:
        raise AssertionError("no distinct duplicate notice is shown")
    assert "'info'" in js, (
        "the duplicate notice reuses the success styling; showUploadAlert needs "
        "a neutral state or the tick still says an image was stored")


def test_the_alert_has_a_neutral_state_distinct_from_success():
    """Neutral must not be green-with-a-tick, or the fix is cosmetic only."""
    js = _js_code_only(_source(MODAL_JS))
    palette = js[js.index("_ALERT_PALETTE"):]
    palette = palette[:palette.index("};") + 2]
    for state in ("error", "info", "success"):
        assert state in palette, f"the alert palette has no {state!r} state"
    assert "✅" in palette and "ℹ️" in palette, (
        "info and success must not share an icon")


def test_the_candidate_holding_the_exact_file_is_marked():
    """The dead end is visible BEFORE the click, not discovered after it."""
    js = _js_code_only(_source(MODAL_JS))
    assert "duplicate_of_identity_id" in js, (
        "the modal ignores the server's deterministic duplicate evidence")
    builder = js[js.index("function buildCandidateRow("):]
    builder = builder[:builder.index("\n    return row;")]
    assert "holdsThisFile" in builder
    assert "Already has this photo" in builder, (
        "the candidate that already holds this file is not labelled")


def test_the_duplicate_candidates_add_action_is_withdrawn_not_relabelled():
    """"Add to this person" must not be offered where it would store nothing.

    Relabelling alone is not enough: the click would still reach the server,
    still succeed, and still add nothing. The action itself is withdrawn —
    no data-action, and `disabled`, which actions.js also honours.
    """
    js = _js_code_only(_source(MODAL_JS))
    builder = js[js.index("function buildCandidateRow("):]
    builder = builder[:builder.index("\n    return row;")]

    duplicate_branch = builder[builder.index("if (holdsThisFile)"):]
    duplicate_branch = duplicate_branch[:duplicate_branch.index("} else {")]
    assert "choose.disabled = true" in duplicate_branch, (
        "the duplicate candidate's button is still clickable")
    assert "Add to this person" not in duplicate_branch, (
        "the duplicate candidate still offers to add")
    assert "dataset.action" not in duplicate_branch, (
        "the duplicate candidate still carries the add action")

    # ...and the add action IS still wired for every other candidate.
    other_branch = builder[builder.index("} else {"):]
    assert "Add to this person" in other_branch
    assert "enrollmentAddToExisting" in other_branch, (
        "a different photo of the same person can no longer be added")


def test_a_busy_reset_cannot_re_enable_the_withdrawn_action():
    """setDecisionBusy(false) re-enables buttons after a recoverable refusal.

    Without an exemption it would hand back the very action that was withdrawn.
    """
    js = _js_code_only(_source(MODAL_JS))
    assert "permanentlyDisabled" in js, (
        "nothing distinguishes a withdrawn action from a busy one")
    busy = js[js.index("function setDecisionBusy("):]
    busy = busy[:busy.index("\n}")]
    assert "permanentlyDisabled" in busy, (
        "setDecisionBusy re-enables the withdrawn duplicate action")


def test_the_duplicate_notice_names_the_person_and_promises_nothing():
    js = _js_code_only(_source(MODAL_JS))
    assert "This exact image is already stored for" in js, (
        "the neutral duplicate notice is missing")
    assert "Nothing will be added" in js, (
        "the notice does not say that nothing will be added")
    assert "${duplicateName}" in js or "duplicateName" in js, (
        "the notice does not name the person")

    html = _source(MODAL_HTML)
    assert 'id="enrollmentDuplicateText"' in html, (
        "the notice has no element to write the person's name into")
    # Neutral, never a success tick.
    notice = html[html.index('id="enrollmentDuplicateNotice"'):]
    notice = notice[:notice.index("</div>")]
    assert "check" not in notice.lower(), "the duplicate notice uses a success tick"


def test_the_server_recommendation_is_suppressed_for_a_duplicate():
    """The API recommends add_to_existing for a strong match.

    When the strong match is the identity that already holds these exact bytes,
    that recommendation is wrong and must not reach the operator.
    """
    js = _js_code_only(_source(MODAL_JS))
    assert "duplicateName" in js
    show = js[js.index("function showEnrollmentDecision("):]
    show = show[:show.index("\n}")]
    assert "duplicateName" in show and "data.message" in show, (
        "the server's add recommendation is shown verbatim even for a duplicate")
    assert "!holdsThisFile" in show, (
        "the duplicate holder can still be flagged as the recommended match")


def test_the_duplicate_response_carries_what_the_modal_branches_on(token, parked):
    """Behavioural half: the flags the fix depends on are really returned."""
    owner, _name, body = parked

    # First confirm attaches the photo for real.
    status, added = _confirm(token, action="add_to_existing", identity_id=owner,
                             upload_token=body["upload_token"])
    assert status == 200, added
    assert added["image_created"] is True
    assert added.get("duplicate") is not True

    # Re-upload the SAME file to the SAME identity via the direct route.
    status, again = _http("POST", f"/api/identities/{owner}/images", token=token,
                          files={"photo": ("face_b.jpg", _read(FACE_B), "image/jpeg")})
    assert status == 200, again
    assert again["duplicate"] is True, again
    assert again["image_created"] is False, (
        "image_created must be false so the UI can tell nothing was stored")

    images, embeddings, _ = _counts(owner)
    assert (images, embeddings) == (2, 2), (
        f"the duplicate wrote a third row: {images}/{embeddings}")
    assert len(os.listdir(os.path.join(FACES_DIR, owner))) == 2, (
        "the duplicate wrote a second copy of the same bytes")


def test_the_second_confirmation_is_asked_once_and_then_carried():
    js = _source(MODAL_JS)
    assert "requires_confirmation" in js, (
        "the modal ignores the server's request for a second confirmation")
    assert "confirm_create_new: true" in js, (
        "the second attempt must carry the confirmation flag or it loops")


# ---------------------------------------------------------------------------
# Threshold and pool validation (fail startup, never silently corrected)
# ---------------------------------------------------------------------------

def _violations(**overrides):
    from types import SimpleNamespace

    from backend.security.config_guard import collect_violations

    base = dict(ENROLL_STRONG_MATCH_MIN=0.75, ENROLL_CANDIDATE_MIN=0.40,
                ENROLL_CANDIDATE_POOL=25, ENROLL_MAX_CANDIDATES=5,
                SIMILARITY_THRESHOLD=0.40,
                ENVIRONMENT="development", STORAGE_DIR="/app/storage",
                JWT_ALGORITHM="HS256", AUTH_COOKIE_SAMESITE="lax")
    base.update(overrides)
    return {v.code for v in collect_violations(SimpleNamespace(**base), env={})}


def test_a_valid_threshold_pair_raises_nothing():
    assert not {code for code in _violations() if code.startswith("ENROLL_")}


def test_inverted_thresholds_fail_startup():
    codes = _violations(ENROLL_CANDIDATE_MIN=0.9, ENROLL_STRONG_MATCH_MIN=0.5)
    assert "ENROLL_THRESHOLDS_INVERTED" in codes, codes


def test_out_of_range_thresholds_fail_startup():
    assert "ENROLL_THRESHOLD_OUT_OF_RANGE" in _violations(ENROLL_STRONG_MATCH_MIN=1.5)
    assert "ENROLL_THRESHOLD_OUT_OF_RANGE" in _violations(ENROLL_CANDIDATE_MIN=-0.1)


def test_a_non_numeric_threshold_fails_startup():
    assert "ENROLL_THRESHOLD_NOT_A_NUMBER" in _violations(
        ENROLL_STRONG_MATCH_MIN="high")


def test_a_pool_no_larger_than_the_display_count_fails_startup():
    """The pool truncates BEFORE per-identity collapsing.

    One person with several photos would otherwise fill it and hide every other
    candidate — the operator reviews a list missing the right answer.
    """
    codes = _violations(ENROLL_CANDIDATE_POOL=5, ENROLL_MAX_CANDIDATES=5)
    assert "ENROLL_CANDIDATE_POOL_TOO_SMALL" in codes, codes
    assert "ENROLL_CANDIDATE_POOL_TOO_SMALL" in _violations(
        ENROLL_CANDIDATE_POOL=3, ENROLL_MAX_CANDIDATES=5)


def test_showing_no_candidates_fails_startup():
    assert "ENROLL_CANDIDATE_COUNT_INVALID" in _violations(ENROLL_MAX_CANDIDATES=0)


def test_a_candidate_floor_above_recognitions_own_bar_is_reported():
    """The calibration trap, caught at startup.

    Between SIMILARITY_THRESHOLD and a higher ENROLL_CANDIDATE_MIN sits a band
    where recognition says "same person" and enrollment says "nothing to ask
    about" — so the duplicate is created silently and recognition then reports
    either name for that face. Advisory rather than fatal: it is a deliberate
    calibration an operator may want, but it must never pass unremarked.
    """
    from types import SimpleNamespace

    from backend.security.config_guard import collect_violations

    codes = _violations(ENROLL_CANDIDATE_MIN=0.60, SIMILARITY_THRESHOLD=0.40)
    assert "ENROLL_CANDIDATE_MIN_ABOVE_RECOGNITION" in codes, codes

    severities = {v.code: v.severity for v in collect_violations(
        SimpleNamespace(ENROLL_STRONG_MATCH_MIN=0.75, ENROLL_CANDIDATE_MIN=0.60,
                        ENROLL_CANDIDATE_POOL=25, ENROLL_MAX_CANDIDATES=5,
                        SIMILARITY_THRESHOLD=0.40, ENVIRONMENT="development",
                        STORAGE_DIR="/app/storage", JWT_ALGORITHM="HS256",
                        AUTH_COOKIE_SAMESITE="lax"), env={})}
    assert severities["ENROLL_CANDIDATE_MIN_ABOVE_RECOGNITION"] == "warn"


def test_the_shipped_configuration_does_not_trip_its_own_rules():
    """The defaults must be a configuration this guard accepts."""
    from backend.security.config_guard import collect_violations
    from config import settings

    codes = {v.code for v in collect_violations(settings, env={})
             if v.code.startswith("ENROLL_")}
    assert not codes, f"the shipped enrollment settings are self-inconsistent: {codes}"


def test_the_thresholds_are_not_clamped_at_read_time():
    """Silently correcting an inverted pair would hide it forever.

    classify_match must report what the configuration actually says, so the
    startup guard stays the single place that refuses it.
    """
    import ast
    import inspect
    import textwrap

    from backend.core import enrollment_service

    # AST, not text: this function's own docstring explains why it does not
    # clamp, and a raw scan cannot tell an explanation from a call.
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(enrollment_service.classify_match)))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not ({"max", "min"} & called), (
        "classify_match clamps the thresholds instead of letting the startup "
        "guard refuse an invalid pair")


def test_the_bands_partition_similarity(token):
    from backend.core.enrollment_service import classify_match
    from config import settings

    strong = float(settings.ENROLL_STRONG_MATCH_MIN)
    floor = float(settings.ENROLL_CANDIDATE_MIN)
    assert classify_match(strong) == "strong"
    assert classify_match(1.0) == "strong"
    assert classify_match(floor) == "uncertain"
    assert classify_match((floor + strong) / 2) == "uncertain"
    assert classify_match(floor - 0.01) == "none"
    assert classify_match(None) == "none"


# ---------------------------------------------------------------------------
# Cleanup isolation
#
# This module's teardown used to be `DELETE FROM pending_enrollments` with no
# WHERE clause, run before AND after every test in the file. Against an empty
# table that is invisible. Against a real system it silently destroys an
# administrator's entire review queue — uploads parked precisely because a
# human still has to decide about them.
#
# The invariant, stated as a test rather than as a convention: a regression
# test may delete only data that the test itself created.
# ---------------------------------------------------------------------------

def _insert_sentinel_ticket():
    """A row standing in for legitimate development data.

    Written directly rather than through the API because it must NOT be
    attributable to this module — it plays the part of a ticket that was
    already there when the suite started.
    """
    marker = "sentinel_" + uuid_module.uuid4().hex
    admin_id = _sql("SELECT id FROM users WHERE username = 'admin'", fetch="scalar")
    return _sql(
        "INSERT INTO pending_enrollments (token_hash, user_id, display_name, "
        " display_name_key, storage_path, file_checksum, is_face_image, "
        " decision, candidates, created_at, expires_at) "
        "VALUES (:h, :u, :n, :k, :p, :c, false, 'uncertain', '[]'::jsonb, "
        "        now(), now() + interval '7 days') RETURNING id",
        {"h": uuid_module.uuid4().hex * 2, "u": admin_id,
         "n": marker, "k": marker.lower(),
         "p": f"storage/pending/{marker}.jpg", "c": marker},
        fetch="scalar"), marker


def test_cleanup_touches_only_rows_this_module_created(token):
    """Seed a pre-existing ticket, run a full park-and-clean cycle, and prove
    the sentinel is still there, column for column."""
    sentinel_id, marker = _insert_sentinel_ticket()
    # It was NOT here when the module started, so teach the baseline that it
    # belongs to somebody else — exactly the state a real parked upload is in.
    _PREEXISTING_PENDING_IDS.add(sentinel_id)
    columns = ("token_hash, user_id, display_name, display_name_key, "
               "storage_path, file_checksum, decision, created_at, expires_at")
    before = _sql(f"SELECT {columns} FROM pending_enrollments WHERE id = :i",
                  {"i": sentinel_id})[0]

    try:
        with _no_match_for(FACE_A):
            _enroll_new(token, _unique("iso_owner"), FACE_A)
            status, body = _upload(token, _unique("iso_rival"), FACE_B)
            assert status == 202 and body.get("decision_required"), body

        assert _pending_count() == 1, (
            "the module should own exactly the one ticket it just parked")

        _clear_pending()

        assert _pending_count() == 0, "the module's own ticket was not cleaned up"
        after = _sql(f"SELECT {columns} FROM pending_enrollments WHERE id = :i",
                     {"i": sentinel_id})
        assert after, (
            "cleanup deleted a ticket this module did not create — an "
            "administrator's review queue would be gone")
        assert after[0] == before, (
            f"the pre-existing ticket was modified: {before} -> {after[0]}")
    finally:
        _PREEXISTING_PENDING_IDS.discard(sentinel_id)
        _sql("DELETE FROM pending_enrollments WHERE id = :i",
             {"i": sentinel_id}, fetch="none")


def test_no_unscoped_delete_or_update_survives_in_this_module():
    """The rule, enforced against the source so it cannot quietly come back.

    Parsed rather than grepped: a substring scan trips over this docstring.
    """
    import ast

    source = open(__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    # Skip this function's own body: the patterns it matches on are themselves
    # string literals beginning "DELETE FROM", so a naive scan reports itself.
    guard = "test_no_unscoped_delete_or_update_survives_in_this_module"
    scanned = [node for node in tree.body
               if not (isinstance(node, ast.FunctionDef) and node.name == guard)]
    literals = [child.value
                for node in scanned for child in ast.walk(node)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)]
    offenders = []
    for text in literals:
        flat = " ".join(text.split())
        upper = flat.upper()
        if not (upper.startswith("DELETE FROM") or upper.startswith("UPDATE ")):
            continue
        if "WHERE" not in upper:
            offenders.append(flat[:80])
    assert not offenders, (
        f"unscoped mutations would reach pre-existing development data: {offenders}")
