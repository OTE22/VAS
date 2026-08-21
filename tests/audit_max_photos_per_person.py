"""MAX_PHOTOS_PER_PERSON — does it work, with different values?

Audit-only; changes no product code. Run explicitly:

    docker exec -w /app <api> python -m pytest tests/audit_max_photos_per_person.py -v

WHAT THE SETTING ACTUALLY IS
Declared config.py:553 (default 1), registered dynamic in
backend/core/runtime_settings.py:106 with range 0..1000, and consumed in
exactly ONE place: backend/services/image_processing.py:699 — the CAMERA
path. There it caps how many face-crop FILES are written to disk for a KNOWN
person inside storage/<pipeline_id>/<name>/, and it is deliberately skipped
for unknown faces (`apply_threshold = not is_unknown`, :703).

It is NOT consulted anywhere in the enrollment/upload path: adding photos via
ADD PERSON is bounded by the hard-coded MAX_IMAGES_PER_IDENTITY = 1000
(backend/core/enrollment_service.py:102).

So "does it work with different numbers" has three separate answers, and this
module measures each one:
  1. the setting mechanics   — does a new value persist and reach the process?
  2. the upload path         — does the cap bind ADD PERSON? (it should not,
                               but the name strongly implies it does)
  3. the camera path         — what the cap does when it binds, including what
                               it writes to the database at the limit
"""
import json
import os
import uuid as uuid_module

import pytest

from conftest import run_on_shared_loop as run_async

from audit_add_person_matrix import (  # reuse one harness, do not fork it
    BASE, FACE_A, FACE_B, FACE_C, CROPPED_FACE, EVIDENCE_DIR,
    _http, _read, _sql, _upload, _identity_ids_for, _record, EVIDENCE,
)

SETTING = "MAX_PHOTOS_PER_PERSON"
TEST_PREFIX = "qa_maxphotos_"


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         body={"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


@pytest.fixture(scope="module", autouse=True)
def _seed_settings_rows(token):
    """Settings rows are created lazily by sync_settings_from_config(), which
    only runs on GET /api/settings|/categories. Until someone loads the list,
    the per-key GET and PUT both answer 404 — so seed once up front."""
    status, listing = _http("GET", "/api/settings", token=token)
    assert status == 200, listing
    return listing


def _get_setting(token):
    status, body = _http("GET", f"/api/settings/{SETTING}", token=token)
    assert status == 200, body
    return body


def _put_setting(token, value):
    return _http("PUT", f"/api/settings/{SETTING}", token=token,
                 body={"value": str(value), "change_reason": "audit"},
                 headers={"X-Requested-With": "XMLHttpRequest"})


@pytest.fixture(scope="module", autouse=True)
def _restore_setting(token, _seed_settings_rows):
    """Record the original value and put it back, whatever the tests do."""
    original = _get_setting(token).get("stored_value")
    yield
    if original is not None:
        _put_setting(token, original)
    import shutil
    for identity_id in _identity_ids_for(TEST_PREFIX):
        _sql("DELETE FROM identity_audit_log WHERE identity_id = CAST(:i AS uuid)"
             "   OR related_identity_id = CAST(:i AS uuid)",
             {"i": identity_id}, fetch="none")
        _sql("DELETE FROM identity_embeddings WHERE identity_id = CAST(:i AS uuid)",
             {"i": identity_id}, fetch="none")
        _sql("DELETE FROM identity_images WHERE identity_id = CAST(:i AS uuid)",
             {"i": identity_id}, fetch="none")
        _sql("DELETE FROM identities WHERE id = CAST(:i AS uuid)",
             {"i": identity_id}, fetch="none")
        folder = os.path.join("/app/storage/faces", identity_id)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    with open(os.path.join(EVIDENCE_DIR, "evidence_max_photos.json"), "w") as handle:
        json.dump(EVIDENCE, handle, indent=2, default=str)


# ---------------------------------------------------------------------------
# 1. Setting mechanics — does a new value persist AND reach the running process?
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [0, 1, 2, 5, 25, 1000])
def test_accepted_values_persist_and_apply(token, value):
    status, body = _put_setting(token, value)
    assert status == 200, (status, body)

    after = _get_setting(token)
    _record(f"max_photos_set_{value}", {"put": body, "get": after})

    assert str(after["stored_value"]) == str(value), after
    # effective_value is read off the SERVING process, so this is the only
    # evidence that the change reached the running application.
    assert str(after["effective_value"]) == str(value), (
        f"{SETTING} was stored as {value} but the running process still reports "
        f"{after['effective_value']} — saved is not applied")
    assert after.get("apply_mode") in ("immediate", "next_request", "next_job_run"), (
        f"{SETTING} is registered dynamic; apply_mode={after.get('apply_mode')}")


@pytest.mark.parametrize("value", [-1, 1001, 99999])
def test_out_of_range_values_are_refused(token, value):
    """The registry declares 0..1000; anything outside must be refused."""
    status, body = _put_setting(token, value)
    _record(f"max_photos_reject_{value}", {"status": status, "body": body})
    assert status == 422, (
        f"{SETTING}={value} is outside the declared 0..1000 range but was "
        f"accepted with status {status}: {body}")


@pytest.mark.parametrize("value", ["abc", "", "1.5"])
def test_non_integer_values_are_refused(token, value):
    status, body = _put_setting(token, value)
    _record(f"max_photos_reject_type_{value or 'empty'}", {"status": status, "body": body})
    assert status == 422, (
        f"{SETTING}={value!r} is not an integer but was accepted "
        f"with status {status}: {body}")


# ---------------------------------------------------------------------------
# 2. The upload path — does the cap bind ADD PERSON?
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cap", [1, 2])
def test_cap_governs_camera_capture_not_uploads(token, cap):
    """The cap must NOT limit Add Person — and that is deliberate.

    The audit found that a setting named MAX_PHOTOS_PER_PERSON does not bind
    the upload path. Enforcing it there was considered and rejected: the
    default is 1, multi-image enrollment is a supported and encouraged feature
    (that is what identity_images exists for), so applying the camera cap to
    uploads would stop every admin on default configuration from adding a
    second photo of anyone. The setting was documented as camera-only instead
    (backend/routes/settings.py custom_descriptions).

    This pins that decision from both ends: uploads stay uncapped, and the
    admin UI has to keep saying so — see
    test_cap_is_documented_as_camera_only.
    """
    status, body = _put_setting(token, cap)
    assert status == 200, body
    assert str(_get_setting(token)["effective_value"]) == str(cap)

    name = f"{TEST_PREFIX}cap{cap}_{uuid_module.uuid4().hex[:8]}"
    status, first = _upload(token, name, FACE_A, on_decision="create_new")
    assert status in (200, 201), first
    identity_id = first["identity_id"]

    # Add further photos by id, so name resolution and the review gate cannot
    # confuse the result. Each is a genuinely different image.
    extras = [FACE_B, FACE_C, CROPPED_FACE]
    outcomes = []
    for index, fixture in enumerate(extras, start=2):
        status, body = _http(
            "POST", f"/api/identities/{identity_id}/images", token=token,
            fields={"is_face_image": str(fixture == CROPPED_FACE).lower()},
            files={"photo": (f"extra_{index}.jpg", _read(fixture), "image/jpeg")})
        outcomes.append({"n": index, "status": status,
                         "error": body.get("error"), "message": body.get("message")})

    stored = _sql("""SELECT count(*) FROM identity_images
                      WHERE identity_id = CAST(:i AS uuid)""",
                  {"i": identity_id}, fetch="scalar")
    _record(f"upload_cap_{cap}",
            {"cap": cap, "outcomes": outcomes, "images_stored": stored})

    assert stored > cap, (
        f"{SETTING}={cap} now limits uploaded photos ({stored} stored). That is "
        f"a behaviour change, not a fix: with the default of 1 it would stop "
        f"admins adding a second photo of anyone. If the cap really should "
        f"apply to uploads, it needs its own setting and a default that does "
        f"not break multi-image enrollment. Outcomes: {outcomes}")
    assert all(o["status"] in (200, 201) for o in outcomes), outcomes


# ---------------------------------------------------------------------------
# 3. The camera path — what the cap does where it IS consumed
# ---------------------------------------------------------------------------

def test_camera_path_log_matches_what_it_stores():
    """At the cap, discarding the path is CORRECT — claiming otherwise was not.

    The branch sets `face_filename = None` so no path is stored for a file that
    was never written; that is right. The defect was the log line above it,
    which read "Path saved to DB: <path>, but file NOT saved to disk" — telling
    an operator a row holds a path when the column receives NULL.

    Source-level because the behavioural half needs a live camera pipeline.
    """
    source = _read("/app/backend/services/image_processing.py").decode()
    window = source[source.index("max_photos_per_person = settings.MAX_PHOTOS_PER_PERSON"):]
    window = window[:2200]

    branch = window[window.index("if apply_threshold and len(existing_images)"):]
    branch = branch[:branch.index("else:")]
    # Comments only — NOT _repo_scan.strip_comments_and_docstrings, which also
    # blanks string literals, and the log message under test IS a string
    # literal. Without this the comment explaining the old wording matches the
    # old wording, and the test grades prose instead of code.
    code = "\n".join(line for line in branch.splitlines()
                     if not line.strip().startswith("#"))
    _record("camera_path_threshold_branch", code)
    branch = code

    assert "face_filename = None" in branch, (
        "the cap no longer discards the path; a path would now be stored for a "
        "file that was never written")
    assert "Path saved to DB" not in branch, (
        "the log still claims a path was stored while the branch stores NULL "
        "(backend/services/image_processing.py, threshold branch)")


def test_cap_is_documented_as_camera_only():
    """The admin UI files this setting under 'storage' with no hint that it
    governs camera capture only and does nothing to uploaded photos."""
    descriptions = _read("/app/backend/routes/settings.py").decode()
    # The key appears first in the category map; the admin-facing text is the
    # custom_descriptions entry, so look for the quoted key followed by a colon
    # and a string rather than the first mention.
    marker = descriptions.find(f'"{SETTING}": "')
    assert marker != -1, (
        f"{SETTING} has no custom_descriptions entry, so the admin UI shows the "
        f"bare field description with no hint that it is camera-only")
    blurb = descriptions[marker:marker + 500]
    _record("max_photos_ui_description", blurb[:400])

    assert "camera" in blurb.lower() or "pipeline" in blurb.lower(), (
        f"{SETTING} is presented to admins without saying it applies only to "
        f"camera captures; an admin setting it to 1 would reasonably expect "
        f"ADD PERSON to stop at one photo, which it does not")
