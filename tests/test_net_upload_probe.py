"""One-off probe: a photo from the internet through BOTH enrollment endpoints.

Answers a direct question: the modal posts to /api/upload-person, which is
marked deprecated and points at POST /api/identities/{id}/images. Does the
replacement actually replace it? The photos are AI-generated faces from
thispersondoesnotexist.com, so no real person's biometrics are enrolled.

Run:  sudo scripts/run_regression_isolated.sh tests/test_net_upload_probe.py -q -s
"""

import os
import uuid

import pytest

from test_e2e_api_sweep import (_browser_cookie, _http, _sql, _exec)

NET = os.path.join("/app", "logs", "net-fixtures")
RESULTS = []


def _photo(name):
    with open(os.path.join(NET, name), "rb") as handle:
        return handle.read()


def _say(step, status, note):
    RESULTS.append((step, status, note))
    print(f"  {status:>5}  {step:<58} {note}")


def _counts(identity_id):
    images = _sql("SELECT count(*) FROM identity_images WHERE identity_id=:i",
                  {"i": identity_id})[0][0]
    vectors = _sql("SELECT count(*) FROM identity_embeddings WHERE identity_id=:i",
                   {"i": identity_id})[0][0]
    return images, vectors


def test_probe_both_enrollment_endpoints():
    if not os.path.isdir(NET) or not os.listdir(NET):
        pytest.skip("no downloaded photos; re-fetch them into logs/net-fixtures to repeat this")
    cookie = _browser_cookie()
    browser = {"Cookie": cookie, "X-Requested-With": "XMLHttpRequest"}
    name = "Net Person " + uuid.uuid4().hex[:6]
    identity_id = None
    print(f"\n--- a photo from the internet, enrolled as {name!r} ---")

    try:
        # 1. THE DEPRECATED ENDPOINT: create a person from a photo.
        # A portrait downloaded from the internet is a TIGHT CROP, and the
        # detector wants a scene it can find a face inside. The endpoint says so
        # and offers the remedy, which is the same checkbox the modal shows, so
        # do what an operator would do and tick it.
        status, body, _ = _http("POST", "/api/upload-person", headers=browser, timeout=300,
                                fields={"person_name": name, "is_face_image": "false"},
                                files={"photo": ("net_a.jpg", _photo("net_a.jpg"), "image/jpeg")})
        _say("DEPRECATED  first try, 'this is a face image' UNTICKED", status,
             body.get("error") or "accepted")
        if status == 400 and body.get("error") == "no_face":
            status, body, _ = _http("POST", "/api/upload-person", headers=browser, timeout=300,
                                    fields={"person_name": name, "is_face_image": "true"},
                                    files={"photo": ("net_a.jpg", _photo("net_a.jpg"), "image/jpeg")})
        if status == 202 and body.get("decision_required"):
            status, body, _ = _http("POST", "/api/enrollment/confirm", headers=browser, timeout=300,
                                    body={"upload_token": body["upload_token"], "action": "create_new",
                                          "display_name": name})
        _say("DEPRECATED  retried with it TICKED", status,
             body.get("message") or body.get("error") or "created")
        assert status in (200, 201), body
        rows = _sql("SELECT id FROM identities WHERE display_name=:n", {"n": name})
        assert rows, "no identity was created"
        identity_id = str(rows[0][0])
        images, vectors = _counts(identity_id)
        _say("            rows after the first upload", 200, f"images={images} vectors={vectors}")

        # 2. THE SAME PHOTO AGAIN, same endpoint
        status, body, _ = _http("POST", "/api/upload-person", headers=browser, timeout=300,
                                fields={"person_name": name, "is_face_image": "true"},
                                files={"photo": ("net_a.jpg", _photo("net_a.jpg"), "image/jpeg")})
        first = status
        if status == 202 and body.get("decision_required"):
            status, body, _ = _http("POST", "/api/enrollment/confirm", headers=browser, timeout=300,
                                    body={"upload_token": body["upload_token"],
                                          "action": "add_to_existing", "identity_id": identity_id})
        _say(f"DEPRECATED  the identical photo again (gate answered {first})", status,
             body.get("message") or body.get("error") or "accepted")
        images, vectors = _counts(identity_id)
        _say("            rows after the repeat", 200, f"images={images} vectors={vectors}")

        # 3. THE REPLACEMENT ENDPOINT, same photo
        status, body, _ = _http("POST", f"/api/identities/{identity_id}/images",
                                headers=browser, timeout=300, fields={"is_face_image": "true"},
                                files={"photo": ("net_a.jpg", _photo("net_a.jpg"), "image/jpeg")})
        _say("REPLACEMENT POST /api/identities/{id}/images  (same photo)", status,
             body.get("message") or body.get("error") or "accepted")

        # 4. THE REPLACEMENT ENDPOINT, a DIFFERENT photo of nobody
        status, body, _ = _http("POST", f"/api/identities/{identity_id}/images",
                                headers=browser, timeout=300, fields={"is_face_image": "true"},
                                files={"photo": ("net_b.jpg", _photo("net_b.jpg"), "image/jpeg")})
        _say("REPLACEMENT POST /api/identities/{id}/images  (a second face)", status,
             body.get("message") or body.get("error") or "accepted")
        images, vectors = _counts(identity_id)
        _say("            rows after both replacement calls", 200,
             f"images={images} vectors={vectors}")

        # 5. CAN THE REPLACEMENT CREATE A PERSON AT ALL?
        status, body, _ = _http("POST", f"/api/identities/{uuid.uuid4()}/images",
                                headers=browser, timeout=300, fields={"is_face_image": "true"},
                                files={"photo": ("net_b.jpg", _photo("net_b.jpg"), "image/jpeg")})
        _say("REPLACEMENT can it create a NEW person? (unknown id)", status,
             body.get("message") or body.get("error") or "accepted")

        # what is on disk and in the vector rows
        for path, primary, checksum in _sql(
                "SELECT storage_path, is_primary, left(file_checksum, 12) "
                "FROM identity_images WHERE identity_id=:i ORDER BY id", {"i": identity_id}):
            _say(f"            file {os.path.basename(path)}", 200,
                 f"primary={primary} checksum={checksum}.. exists={os.path.exists('/app/' + path)}")
        for vid, part, model, state in _sql(
                "SELECT id, faiss_index_type, embedding_model_version, vector_index_sync_state "
                "FROM identity_embeddings WHERE identity_id=:i ORDER BY id", {"i": identity_id}):
            _say(f"            vector {vid}", 200, f"{part} / {model} / {state}")
    finally:
        if identity_id:
            for statement in ("DELETE FROM identity_embeddings WHERE identity_id=:i",
                              "DELETE FROM identity_images WHERE identity_id=:i",
                              "DELETE FROM identities WHERE id=:i"):
                try:
                    _exec(statement, {"i": identity_id})
                except Exception:
                    pass
