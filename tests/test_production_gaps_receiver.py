"""Receiver-side regression tests for the VMS production-gap fixes.

Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_production_gaps_receiver.py -v

Covers:
  * image-free event_id deduplication (the dedup check now runs BEFORE the
    no-images early return) - idempotent deduplication WITHIN the dedup TTL,
    never "exactly-once processing"
  * WEBHOOK_DEDUP_TTL_SECONDS covers the sender's worst-case retry horizon
  * expected-nginx body-limit guard: helper parse, settings-route 422
"""

import json
import time
import urllib.error
import urllib.request
import uuid

import pytest

BASE = "http://localhost:8000"


def _http(method, path, body=None, *, token=None, csrf=True, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if csrf:
        request.add_header("X-Requested-With", "XMLHttpRequest")
    if path.startswith("/webhook") or path.startswith("/api/webhook"):
        try:
            from config import settings
            from backend.security.webhook_auth import header_name, parse_keys
            keys = parse_keys(getattr(settings, "WEBHOOK_API_KEYS", ""))
            if keys:
                request.add_header(header_name(settings), keys[0])
        except Exception:
            pass
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, _json(raw)
    except urllib.error.HTTPError as exc:
        return exc.code, _json(exc.read())


def _json(raw):
    try:
        return json.loads(raw.decode())
    except Exception:                                          # noqa: BLE001
        return {}


def _imagefree_event(event_id=None):
    payload = {
        "pipeline_id": "gap-test",
        "pipeline_name": "gap-test-cam",
        "results": {"num_detections": 1,
                    "predictions": [{"class_name": "person", "confidence": 0.9}]},
    }
    if event_id is not None:
        payload["event_id"] = event_id
    return payload


# ------------------------------------------------ image-free event_id dedup

def test_imagefree_event_id_dedups_within_ttl():
    """Same event_id retried within the TTL -> acknowledged as duplicate, not
    re-processed. This is exactly what a VMS webhook retry looks like."""
    eid = uuid.uuid4().hex
    s1, b1 = _http("POST", "/webhook/gap-test", _imagefree_event(eid))
    s2, b2 = _http("POST", "/webhook/gap-test", _imagefree_event(eid))
    assert s1 == 200 and b1.get("status") == "ok", (s1, b1)
    assert s2 == 200 and b2.get("status") == "duplicate", (
        f"retry of the same event_id must dedup, got {s2} {b2}")
    assert b2.get("queued") == 0 and b2.get("dropped") == 0


def test_different_event_ids_processed_normally():
    s1, b1 = _http("POST", "/webhook/gap-test", _imagefree_event(uuid.uuid4().hex))
    s2, b2 = _http("POST", "/webhook/gap-test", _imagefree_event(uuid.uuid4().hex))
    assert (s1, b1.get("status")) == (200, "ok")
    assert (s2, b2.get("status")) == (200, "ok"), "a NEW event must never be deduped"


def test_keyless_imagefree_payload_never_falsely_dedups():
    """No event_id/request_id/frame_id and no image -> job_key is None -> no
    dedup, exactly the pre-change behaviour."""
    s1, b1 = _http("POST", "/webhook/gap-test", _imagefree_event())
    s2, b2 = _http("POST", "/webhook/gap-test", _imagefree_event())
    assert (s1, b1.get("status")) == (200, "ok")
    assert (s2, b2.get("status")) == (200, "ok")


def test_same_event_id_after_ttl_expiry_processes_again(monkeypatch):
    """TTL-bounded semantics, asserted at the dedup primitive with a short
    monkeypatched TTL (waiting out the real 600s live is impractical): after
    expiry the same key is processed again - documented, expected behaviour.
    That is why the claim is 'idempotent deduplication within the configured
    dedup TTL', never exactly-once."""
    from config import settings
    from backend.routes import webhook as wh
    monkeypatch.setattr(settings, "WEBHOOK_DEDUP_TTL_SECONDS", 1)
    key = f"gap-test:event_id:{uuid.uuid4().hex}"
    assert wh._dedup_is_duplicate(key) is False   # first sighting recorded
    assert wh._dedup_is_duplicate(key) is True    # inside TTL -> duplicate
    time.sleep(1.2)
    assert wh._dedup_is_duplicate(key) is False   # expired -> processed again


def test_dedup_ttl_covers_sender_retry_horizon():
    """Receiver half of the cross-repo contract: VMS's worst-case same-event
    retry horizon is ~360s (6 attempts x 35s + 5 waits x Retry-After<=30);
    the sender's suite asserts horizon x1.5 <= 600. This side must therefore
    keep the TTL at >= 600."""
    from config import settings
    assert int(settings.WEBHOOK_DEDUP_TTL_SECONDS) >= 600, (
        f"WEBHOOK_DEDUP_TTL_SECONDS={settings.WEBHOOK_DEDUP_TTL_SECONDS} no longer covers "
        f"the VMS retry horizon (~360s + margin) - retried event_ids would be re-processed")


def test_dedup_ttl_effective_value_in_the_live_app_covers_the_horizon():
    """The assertion above reads THIS process's settings, built from the
    container environment — but WEBHOOK_DEDUP_TTL_SECONDS is also a
    runtime-mutable setting. A stored admin override in the `settings` table
    is hydrated over the env at boot, so the LIVE app can be running a
    different value than the env says.

    That is not hypothetical: this exact drift shipped. The compose file was
    raised 60 -> 600, the container env said 600, the sibling test passed —
    and the running app hydrated 60 from a DB row seeded under the old value
    (`Applied WEBHOOK_DEDUP_TTL_SECONDS=60 to runtime (mode=next_request)` in
    the boot log). Retried camera frames were being re-processed as new
    sightings while every env-level check was green.

    So this test asks the LIVE app what it will actually use."""
    token = _admin_token()
    status, body = _http("GET", "/api/settings/WEBHOOK_DEDUP_TTL_SECONDS",
                         token=token)
    assert status == 200, f"settings endpoint returned {status}: {body}"
    effective = int(body["effective_value"])
    assert effective >= 600, (
        f"the RUNNING app's effective WEBHOOK_DEDUP_TTL_SECONDS is {effective} "
        f"(stored={body.get('stored_value')}, env={body.get('env_value')}, "
        f"source={body.get('source')}). A stored settings-table override is "
        f"undercutting the deployed configuration — fix it via "
        f"PUT /api/settings/WEBHOOK_DEDUP_TTL_SECONDS, not by editing env, "
        f"which hydration will overrule again on the next boot.")


def _admin_token():
    data = json.dumps({"username": "admin", "password": "admin123"}).encode()
    request = urllib.request.Request(
        BASE + "/api/auth/login", data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())["access_token"]


# ------------------------------------------------ expected nginx body limit

def test_expected_nginx_webhook_body_limit_parses():
    from backend.core.runtime_settings import expected_nginx_webhook_body_limit_mb
    limit = expected_nginx_webhook_body_limit_mb()
    assert limit == 25, (
        f"expected nginx webhook body limit parsed as {limit!r}; the baked nginx.conf "
        f"webhook location should declare client_max_body_size 25m")


def test_receiver_limit_within_expected_nginx_limit():
    from config import settings
    from backend.core.runtime_settings import expected_nginx_webhook_body_limit_mb
    limit = expected_nginx_webhook_body_limit_mb()
    assert limit is not None
    assert int(settings.WEBHOOK_MAX_BODY_MB) <= limit, (
        f"WEBHOOK_MAX_BODY_MB={settings.WEBHOOK_MAX_BODY_MB} exceeds the expected nginx "
        f"limit {limit}m - nginx would 413 bodies the receiver claims to accept")


@pytest.fixture()
def admin_token():
    status, body = _http("POST", "/api/auth/login",
                         {"username": "admin", "password": "admin123"}, csrf=False)
    if status != 200:
        pytest.skip(f"admin login unavailable in this deployment ({status})")
    token = body.get("access_token") or body.get("token")
    assert token, "login returned no bearer token"
    return token


def test_settings_route_rejects_oversized_body_limit(admin_token):
    """Editing WEBHOOK_MAX_BODY_MB above the expected nginx limit must 422 with
    a message naming both values - nginx would reject those bodies first."""
    status, body = _http("PUT", "/api/settings/WEBHOOK_MAX_BODY_MB",
                         {"value": "200"}, token=admin_token)
    assert status == 422, f"oversized limit must be refused, got {status} {body}"
    detail = json.dumps(body)
    assert "expected nginx" in detail and "200" in detail and "25" in detail, detail


def test_settings_route_accepts_value_within_limit(admin_token):
    from config import settings
    current = str(int(settings.WEBHOOK_MAX_BODY_MB))
    status, body = _http("PUT", "/api/settings/WEBHOOK_MAX_BODY_MB",
                         {"value": current}, token=admin_token)
    assert status == 200, f"in-range value must be accepted, got {status} {body}"
