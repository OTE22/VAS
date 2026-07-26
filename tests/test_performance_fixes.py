
"""
Performance-overhaul regression tests
=====================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_performance_fixes.py -v

Covers the acceptance-critical behaviors of the event-loop overhaul:
  * /health/live answers fast (loop alive) and /health/ready is structured
  * webhook returns 202 + job_id quickly, dedups repeats, rejects oversized bodies
  * queue enforces its bound at add() (real backpressure)
  * login stays responsive (bcrypt off-loop)
"""

import asyncio
import base64
import io
import json
import struct
import time
import urllib.request
import urllib.error

import pytest

BASE = "http://localhost:8000"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _http(method: str, path: str, body: dict = None, headers: dict = None,
          raw_body: bytes = None, timeout: float = 30.0):
    """Tiny stdlib HTTP client returning (status, parsed_json_or_None, elapsed_s)."""
    url = BASE + path
    data = raw_body if raw_body is not None else (
        json.dumps(body).encode() if body is not None else None
    )
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        payload = e.read()
        status = e.code
    except urllib.error.URLError:
        # Server refused mid-upload (e.g. 413 sent before the body finished
        # uploading, then the connection was closed) — report as status -1.
        payload = b""
        status = -1
    elapsed = time.time() - start
    try:
        parsed = json.loads(payload)
    except Exception:
        parsed = None
    return status, parsed, elapsed


def _tiny_jpeg_b64() -> str:
    """A minimal valid JPEG (magic bytes pass validate_image_content)."""
    try:
        import numpy as np
        import cv2
        img = np.full((64, 64, 3), 128, dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", img)
        assert ok
        return base64.b64encode(buf.tobytes()).decode()
    except ImportError:
        # bare-bones JPEG header + EOI; enough for magic-byte validation
        return base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9").decode()


def _webhook_payload(request_id=None):
    payload = {
        "location_name": "PerfTest Cam",
        "images": [_tiny_jpeg_b64()],
        "predictions": [],
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


# ---------------------------------------------------------------------------
# health endpoints
# ---------------------------------------------------------------------------

def test_health_live_exists_and_fast():
    latencies = []
    for _ in range(20):
        status, body, elapsed = _http("GET", "/health/live", timeout=5)
        assert status == 200
        assert body["status"] == "alive"
        latencies.append(elapsed)
    latencies.sort()
    p99 = latencies[int(len(latencies) * 0.99) - 1]
    # generous in-test bound; the load script asserts the strict 100ms target
    assert p99 < 0.5, f"/health/live p99 {p99:.3f}s too slow - loop may be blocked"


def test_health_ready_structured():
    status, body, _ = _http("GET", "/health/ready", timeout=10)
    assert status in (200, 503)
    assert body["status"] in ("ready", "degraded", "not_ready")
    assert "database" in body["components"]
    assert "models" in body["components"]
    for comp in body["components"].values():
        assert "healthy" in comp and "required" in comp


def test_legacy_health_still_works():
    status, body, _ = _http("GET", "/health", timeout=30)
    assert status == 200
    assert "status" in body


# ---------------------------------------------------------------------------
# webhook contract
# ---------------------------------------------------------------------------

def test_webhook_returns_202_with_job_id_quickly():
    # unique request_id so the content-hash dedup from earlier runs can't match
    status, body, elapsed = _http(
        "POST", "/webhook/perf-test-pipeline",
        _webhook_payload(f"fresh-{time.time()}"), timeout=10,
    )
    assert status == 202, f"expected 202, got {status}: {body}"
    assert body["status"] == "queued"
    assert body.get("job_id")
    assert body["pipeline_id"] == "perf-test-pipeline"
    assert elapsed < 1.0, f"webhook acceptance took {elapsed:.3f}s (must be <1s)"


def test_webhook_duplicate_not_requeued():
    rid = f"dup-test-{int(time.time())}"
    status1, body1, _ = _http("POST", "/webhook/perf-test-pipeline", _webhook_payload(rid))
    status2, body2, _ = _http("POST", "/webhook/perf-test-pipeline", _webhook_payload(rid))
    assert status1 == 202
    assert status2 == 200
    assert body2["status"] == "duplicate"
    assert body2["queued"] == 0


def test_webhook_oversized_body_413():
    # bigger than WEBHOOK_MAX_BODY_MB (25MB default). The app answers 413 from
    # the Content-Length header BEFORE reading the body, so the client may see
    # either the 413 response or a connection reset mid-upload (-1). Both prove
    # the oversized request was rejected without buffering 26MB.
    big = b'{"images": ["' + b"A" * (26 * 1024 * 1024) + b'"]}'
    status, _, _ = _http("POST", "/webhook/perf-test-pipeline", raw_body=big, timeout=60)
    assert status in (413, -1), f"oversized body was not rejected (status {status})"


def test_webhook_no_images_ok():
    status, body, _ = _http("POST", "/webhook/perf-test-pipeline", {"predictions": []})
    assert status == 200
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# queue backpressure (unit-level, direct import)
# ---------------------------------------------------------------------------

def test_queue_enforces_bound_at_add():
    from backend.core.processing_queue import ProcessingQueue

    async def scenario():
        q = ProcessingQueue(max_size=3, max_concurrent=10)
        results = []
        for i in range(5):
            results.append(await q.add({"pipeline_id": "bp-test", "image_b64": "x"}))
        return results, q.pending_items

    results, pending = asyncio.new_event_loop().run_until_complete(scenario())
    assert results[:3] == [True, True, True]
    assert results[3] is False and results[4] is False, "add() must reject beyond max_size"
    assert pending == 3


def test_queue_get_releases_capacity():
    from backend.core.processing_queue import ProcessingQueue

    async def scenario():
        q = ProcessingQueue(max_size=2, max_concurrent=10)
        await q.add({"pipeline_id": "bp2", "image_b64": "x"})
        await q.add({"pipeline_id": "bp2", "image_b64": "x"})
        assert await q.add({"pipeline_id": "bp2", "image_b64": "x"}) is False
        await q.flush_batches()  # not yet timed out - batch may stay buffered
        # force-flush by aging the batch timestamps
        for pid in list(q._batch_timestamps):
            q._batch_timestamps[pid] -= 10
        await q.flush_batches()
        item = await q.get()  # dequeue frees capacity
        assert item["is_batch"]
        return await q.add({"pipeline_id": "bp2", "image_b64": "x"})

    result = asyncio.new_event_loop().run_until_complete(scenario())
    assert result is True, "capacity must free up after a worker dequeues the batch"


# ---------------------------------------------------------------------------
# webhook dedup helpers (unit-level)
# ---------------------------------------------------------------------------

def test_job_key_prefers_sender_id():
    from backend.routes.webhook import _job_key
    key = _job_key({"request_id": "abc"}, "pipe1", ["xxxx"])
    assert key == "pipe1:request_id:abc"


def test_job_key_content_hash_fallback():
    from backend.routes.webhook import _job_key
    k1 = _job_key({}, "pipe1", ["imagedata123"])
    k2 = _job_key({}, "pipe1", ["imagedata123"])
    k3 = _job_key({}, "pipe1", ["differentimg"])
    assert k1 == k2
    assert k1 != k3


def test_dedup_ttl_set():
    from backend.routes import webhook as wh
    key = f"unit-test-{time.time()}"
    assert wh._dedup_is_duplicate(key) is False
    assert wh._dedup_is_duplicate(key) is True


# ---------------------------------------------------------------------------
# auth stays responsive
# ---------------------------------------------------------------------------

def test_login_latency():
    status, body, elapsed = _http(
        "POST", "/api/auth/login",
        {"username": "admin", "password": "admin123"}, timeout=15,
    )
    assert status == 200, f"login failed: {status} {body}"
    assert body.get("access_token")
    assert elapsed < 3.0, f"login took {elapsed:.2f}s"


def test_light_api_responsive():
    status, body, _ = _http(
        "POST", "/api/auth/login",
        {"username": "admin", "password": "admin123"},
    )
    assert status == 200
    token = body["access_token"]
    status, _, elapsed = _http(
        "GET", "/api/pipelines", headers={"Authorization": f"Bearer {token}"}, timeout=10
    )
    assert status == 200
    assert elapsed < 2.0, f"/api/pipelines took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# SQL agent isolation (config presence; a live LLM query is too heavy for CI)
# ---------------------------------------------------------------------------

def test_sql_agent_concurrency_config():
    from sql_agent.api import routes as sqlr
    assert sqlr.SQL_AGENT_MAX_CONCURRENT >= 1
    assert sqlr._sql_agent_semaphore._value <= sqlr.SQL_AGENT_MAX_CONCURRENT
    assert sqlr._USER_AGENTS_MAX == 10


def test_sql_agent_db_hardening():
    import inspect
    from sql_agent.database import DatabaseManager
    src = inspect.getsource(DatabaseManager.get_connection)
    assert "statement_timeout" in src
    assert "default_transaction_read_only" in src


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
