"""
One image-search audit path (plan §7).

    /api/search/by-image  and  /api/search/advanced  both write EXACTLY ONE
    search_history row per call, through the single writer
    backend/core/search_audit.record_image_search. /search/by-image keeps its
    bare-array body and returns the row id additively in the X-Search-Id
    header; the row is keyed by that id and carries the request facts.

    docker exec face_recognition_api python -m pytest tests/test_search_audit_parity.py -q
"""
import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.request
import uuid

import pytest

from test_unmerge import _sql

BASE = "http://localhost:8000"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FACE = "/app/tests/fixtures/faces/face_a.jpg"


def _multipart(fields, image_bytes):
    boundary = "----qasrch" + uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f"--{boundary}\r\n".encode())
        out.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    out.write(f"--{boundary}\r\n".encode())
    out.write(b'Content-Disposition: form-data; name="image"; filename="q.jpg"\r\nContent-Type: image/jpeg\r\n\r\n')
    out.write(image_bytes)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def _post(path, token, fields, image_bytes):
    data, ctype = _multipart(fields, image_bytes)
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), json.loads(exc.read() or b"{}")


@pytest.fixture(scope="module")
def token():
    req = urllib.request.Request(BASE + "/api/auth/login",
                                 data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


@pytest.fixture(scope="module")
def image():
    with open(FACE, "rb") as fh:
        return fh.read()


def _row(search_id):
    rows = _sql("SELECT search_type::text, scope, top_k, filters, input_image_hash, results_count, "
                "watchlist_alerts_count, user_id, ip_address FROM search_history WHERE id = CAST(:i AS uuid)",
                {"i": search_id})
    return rows[0] if rows else None


def _count():
    return _sql("SELECT count(*) FROM search_history", fetch="scalar")


def test_by_image_writes_exactly_one_row_and_returns_the_id_in_a_header(token, image):
    before = _count()
    status, headers, body = _post("/api/search/by-image", token,
                                  {"scope": "both", "top_k": "5", "pipeline_id": "qa-audit-cam"}, image)
    assert status == 200, body
    assert isinstance(body, list), "body stays the bare result array (frontend contract)"
    sid = headers.get("X-Search-Id") or headers.get("x-search-id")
    assert sid, headers
    assert _count() == before + 1
    row = _row(sid)
    assert row is not None, "the header id IS the search_history row id"
    stype, scope, top_k, filters, img_hash, results_count, alerts, user_id, ip = row
    assert stype == "SINGLE" and scope == "both" and top_k == 5
    assert (filters or {}).get("pipeline_id") == "qa-audit-cam", filters
    assert img_hash == hashlib.sha256(image).hexdigest(), "hash of the bytes; bytes never stored"
    assert results_count == len(body) and alerts == 0 and user_id is not None
    _sql("DELETE FROM search_history WHERE id = CAST(:i AS uuid)", {"i": sid})


def test_advanced_search_writes_exactly_one_row_with_the_same_writer(token, image):
    before = _count()
    status, _headers, body = _post("/api/search/advanced", token,
                                   {"scope": "both", "top_k": "5", "check_watchlist": "true"}, image)
    assert status == 200, body
    sid = body["search_id"]
    assert _count() == before + 1
    row = _row(sid)
    # search_type SINGLE/MULTI = face count in the upload (one face here); the
    # hash is the SAME convention as /search/by-image: sha256 of the upload bytes
    assert row is not None and row[0] in ("SINGLE", "MULTI") and row[1] == "both" and row[2] == 5, row
    assert row[4] == hashlib.sha256(image).hexdigest(), row
    _sql("DELETE FROM search_history WHERE id = CAST(:i AS uuid)", {"i": sid})


def test_by_image_with_zero_results_still_writes_one_row(token, image):
    """A search that ran and found nothing is still a search (results_count 0)."""
    before = _count()
    status, headers, body = _post("/api/search/by-image", token,
                                  {"scope": "both", "top_k": "5", "date_from": "2000-01-01T00:00:00Z",
                                   "date_to": "2000-01-02T00:00:00Z"}, image)
    assert status == 200 and body == [], body
    sid = headers.get("X-Search-Id") or headers.get("x-search-id")
    assert _count() == before + 1 and _row(sid)[5] == 0
    _sql("DELETE FROM search_history WHERE id = CAST(:i AS uuid)", {"i": sid})


def test_by_image_failure_writes_no_row(token):
    before = _count()
    status, _h, body = _post("/api/search/by-image", token, {"scope": "both"}, b"not-an-image")
    assert status in (400, 422), (status, body)
    assert _count() == before


def test_search_history_has_exactly_one_writer():
    """`SearchHistory(` construction / INSERT INTO search_history exists only in
    backend/core/search_audit.py; the three call sites delegate."""
    offenders = []
    for root, _d, files in os.walk(f"{REPO}/backend"):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, REPO).replace("\\", "/")
            src = open(path, encoding="utf-8", errors="replace").read()
            if rel == "backend/core/search_audit.py":
                assert "SearchHistory(" in src
                continue
            if re.search(r"\bSearchHistory\(", src) or re.search(r"insert\s*\(\s*SearchHistory\b", src) \
                    or re.search(r"INSERT INTO search_history", src, re.I):
                offenders.append(rel)
    assert offenders == [], offenders
    callers = sorted(rel for rel in ("backend/routes/identities.py", "backend/core/advanced_search.py",
                                     "backend/core/batch_search_service.py")
                     if "record_image_search(" in open(f"{REPO}/{rel}", encoding="utf-8").read())
    assert callers == ["backend/core/advanced_search.py", "backend/core/batch_search_service.py",
                       "backend/routes/identities.py"], callers
