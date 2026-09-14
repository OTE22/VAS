"""One-off probe: does a face filed under the wrong person answer with that
person's name? Uses AI-generated faces (logs/net-fixtures), so nobody real is
enrolled. Skips when the photos are absent.

Run:  sudo scripts/run_regression_isolated.sh tests/test_net_graft_probe.py -q -s
"""
import io
import os
import uuid

import pytest

from test_e2e_api_sweep import _browser_cookie, _http, _sql, _exec

NET = "/app/logs/net-fixtures"


def _photo(name):
    with open(os.path.join(NET, name), "rb") as handle:
        return handle.read()


def _padded(name):
    """The generated portraits are tight crops the detector cannot use. Put the
    face in the middle of a plain canvas three times its size: a scene."""
    from PIL import Image
    face = Image.open(io.BytesIO(_photo(name))).convert("RGB")
    w, h = face.size
    canvas = Image.new("RGB", (w * 3, h * 3), (110, 110, 110))
    canvas.paste(face, (w, h))
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _say(step, status, note):
    print(f"  {status:>5}  {step:<52} {note}")


def _search(browser, name):
    """Search by image the way the Search page does. Returns (status, rows)."""
    for payload in (_photo(name), _padded(name)):
        status, body, _ = _http("POST", "/api/search/by-image", headers=browser, timeout=300,
                                fields={"scope": "known", "top_k": "5"},
                                files={"image": (name, payload, "image/jpeg")})
        if status == 200:
            break
    rows = body if isinstance(body, list) else (body.get("results") or body.get("matches") or [])
    hits = []
    for r in rows[:5]:
        if isinstance(r, dict):
            hits.append((r.get("display_name") or r.get("identity_name") or r.get("name"),
                         r.get("similarity") if r.get("similarity") is not None else r.get("score")))
    if status != 200:
        return status, [(str(body)[:90], None)]
    return status, hits


def test_a_grafted_face_answers_with_the_persons_name():
    need = {"net_a.jpg", "net_b.jpg", "net_c.jpg"}
    if not os.path.isdir(NET) or not need <= set(os.listdir(NET)):
        pytest.skip("download three faces into logs/net-fixtures to repeat this")
    cookie = _browser_cookie()
    browser = {"Cookie": cookie, "X-Requested-With": "XMLHttpRequest"}
    karim = "Karim " + uuid.uuid4().hex[:6]
    karim_id = None
    print(f"\n--- {karim} is enrolled with face A; faces B and C are strangers ---")
    try:
        # 1. Karim exists, with face A only
        status, body, _ = _http("POST", "/api/upload-person", headers=browser, timeout=300,
                                fields={"person_name": karim, "is_face_image": "true"},
                                files={"photo": ("net_a.jpg", _photo("net_a.jpg"), "image/jpeg")})
        if status == 202 and body.get("decision_required"):
            status, body, _ = _http("POST", "/api/enrollment/confirm", headers=browser, timeout=300,
                                    body={"upload_token": body["upload_token"], "action": "create_new",
                                          "display_name": karim, "confirm_create_new": True})
        assert status in (200, 201), body
        karim_id = str(_sql("SELECT id FROM identities WHERE display_name=:n", {"n": karim})[0][0])
        _say("Karim created with face A", status, "")

        # 2. BEFORE: who does the system say face B is?
        status, hits = _search(browser, "net_b.jpg")
        _say("search with face B, BEFORE the graft", status, f"{hits or 'no known person matched'}")

        # 3. THE GRAFT: face B added to Karim through add-image (his id, not his name)
        status, body, _ = _http("POST", f"/api/identities/{karim_id}/images", headers=browser,
                                timeout=300, fields={"is_face_image": "true"},
                                files={"photo": ("net_b.jpg", _photo("net_b.jpg"), "image/jpeg")})
        _say("add-image: face B filed under Karim", status, body.get("message") or body.get("error") or "")

        # 4. AFTER: who does the system say face B is now?
        status, hits = _search(browser, "net_b.jpg")
        _say("search with face B, AFTER the graft", status, f"{hits or 'no known person matched'}")

        # 5. THE NAME PATH: the Add Person dialog, typing Karim's name, face C
        status, body, _ = _http("POST", "/api/upload-person", headers=browser, timeout=300,
                                fields={"person_name": karim, "is_face_image": "true"},
                                files={"photo": ("net_c.jpg", _photo("net_c.jpg"), "image/jpeg")})
        gate = "a decision gate was raised" if status == 202 else "no gate, filed directly"
        _say("upload-person: existing name + face C", status, gate)

        # 6. and who is face C now?
        status, hits = _search(browser, "net_c.jpg")
        _say("search with face C, AFTER the name path", status, f"{hits or 'no known person matched'}")

        images, vectors = _sql("SELECT (SELECT count(*) FROM identity_images WHERE identity_id=:i), "
                               "(SELECT count(*) FROM identity_embeddings WHERE identity_id=:i)",
                               {"i": karim_id})[0]
        _say("Karim's record now holds", 200, f"photos={images} vectors={vectors} (three different faces)")
    finally:
        if karim_id:
            for statement in ("DELETE FROM identity_embeddings WHERE identity_id=:i",
                              "DELETE FROM identity_images WHERE identity_id=:i",
                              "DELETE FROM identities WHERE id=:i"):
                try:
                    _exec(statement, {"i": karim_id})
                except Exception:
                    pass
