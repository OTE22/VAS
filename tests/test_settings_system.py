"""
Settings-system tests
=====================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_settings_system.py -v

Proves the settings overhaul contract:
  * values 0 / false survive round-trips (no truthiness destruction)
  * typed validation rejects bad values, enforces min/max/enum (422)
  * saved values are NOT reverted by subsequent GETs (the old killer bug)
  * stored vs effective vs source are distinguished; env divergence visible
  * immediate settings apply to the running process; restart-required ones
    honestly report applied=false
  * responses carry Cache-Control: no-store
  * retention dry-run deletes nothing; real run deletes only expired rows,
    tolerates missing files, keeps recent records; runs cannot overlap
  * sensitive values are masked everywhere
"""

import asyncio
import json
import time
import urllib.request
import urllib.error

import pytest

BASE = "http://localhost:8000"


def _http(method, path, body=None, token=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # resp.headers is an HTTPMessage — .get() is case-insensitive
            return resp.status, json.loads(resp.read() or b"{}"), resp.headers
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except Exception:
            payload = {}
        return e.code, payload, e.headers


from conftest import run_on_shared_loop as run_async  # noqa: E402
# asyncpg is loop-bound: ALL test modules must drive the pooled engine from
# the single shared loop defined in conftest.py.


async def _ensure_db():
    """The test process is separate from the server — init the DB engine once."""
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


@pytest.fixture(scope="module")
def token():
    status, body, _ = _http("POST", "/api/auth/login",
                            {"username": "admin", "password": "admin123"})
    assert status == 200, f"admin login failed: {body}"
    return body["access_token"]


def _get_setting(token, key):
    status, body, headers = _http("GET", f"/api/settings/{key}", token=token)
    assert status == 200, f"GET {key} failed: {body}"
    return body, headers


def _put_setting(token, key, value, reason="pytest"):
    return _http("PUT", f"/api/settings/{key}",
                 {"value": str(value), "change_reason": reason}, token=token)


# ---------------------------------------------------------------------------
# Typed parsing (unit level — direct import)
# ---------------------------------------------------------------------------

def test_typed_parse_preserves_zero_and_false():
    from backend.core.runtime_settings import typed_parse
    assert typed_parse("MAX_PHOTOS_PER_PERSON", "0") == 0
    assert typed_parse("SAVE_UNKNOWN_FACES", "false") is False
    assert typed_parse("SAVE_UNKNOWN_FACES", "0") is False
    assert typed_parse("IDENTITY_ENRICH_MIN_QUALITY", "0.0") == 0.0


def test_typed_parse_floats():
    from backend.core.runtime_settings import typed_parse
    assert typed_parse("SIMILARITY_THRESHOLD", "0.45") == 0.45


def test_typed_parse_rejects_garbage():
    from backend.core.runtime_settings import typed_parse, SettingValidationError
    with pytest.raises(SettingValidationError):
        typed_parse("SIMILARITY_THRESHOLD", "not-a-number")
    with pytest.raises(SettingValidationError):
        typed_parse("SAVE_UNKNOWN_FACES", "maybe")
    with pytest.raises(SettingValidationError):
        typed_parse("CACHE_TTL", "")  # required, empty not allowed


def test_typed_parse_min_max_enum():
    from backend.core.runtime_settings import typed_parse, SettingValidationError
    with pytest.raises(SettingValidationError):
        typed_parse("SIMILARITY_THRESHOLD", "1.5")   # > max 1.0
    with pytest.raises(SettingValidationError):
        typed_parse("DATA_RETENTION_DAYS", "0")      # < min 1
    with pytest.raises(SettingValidationError):
        typed_parse("LOG_LEVEL", "NOISY")            # not in enum
    assert typed_parse("LOG_LEVEL", "WARNING") == "WARNING"


# ---------------------------------------------------------------------------
# API contract
# ---------------------------------------------------------------------------

def test_no_store_headers(token):
    status, _, headers = _http("GET", "/api/settings", token=token)
    assert status == 200
    assert "no-store" in headers.get("Cache-Control", "")
    status, _, headers = _http("GET", "/api/settings/audit/log?limit=1", token=token)
    assert status == 200
    assert "no-store" in headers.get("Cache-Control", "")


def test_settings_payload_has_typed_schema(token):
    body, _ = _get_setting(token, "DATA_RETENTION_DAYS")
    for field in ("stored_value", "effective_value", "default_value", "value_type",
                  "unit", "minimum", "maximum", "source", "overridden",
                  "apply_mode", "requires_restart", "last_changed_at"):
        assert field in body, f"missing field {field}"
    assert body["value_type"] == "integer"
    assert body["unit"] == "days"
    assert body["apply_mode"] == "next_job_run"


def test_put_survives_get_no_revert(token):
    """THE regression test for the config->DB revert bug."""
    original, _ = _get_setting(token, "CACHE_TTL")
    try:
        status, body, _ = _put_setting(token, "CACHE_TTL", 1234)
        assert status == 200, body
        # GET twice (the page does this immediately after save)
        for _ in range(2):
            current, _ = _get_setting(token, "CACHE_TTL")
            assert current["stored_value"] == 1234, "stored value was reverted!"
            assert current["effective_value"] == 1234, "effective value not applied"
            assert current["source"] == "database"
    finally:
        _put_setting(token, "CACHE_TTL", original["effective_value"] or 3600)


def test_integer_zero_round_trip(token):
    original, _ = _get_setting(token, "WEBHOOK_DEDUP_TTL_SECONDS")
    try:
        status, body, _ = _put_setting(token, "WEBHOOK_DEDUP_TTL_SECONDS", 0)
        assert status == 200, body
        current, _ = _get_setting(token, "WEBHOOK_DEDUP_TTL_SECONDS")
        assert current["stored_value"] == 0            # NOT '(empty)', NOT None
        assert current["effective_value"] == 0
    finally:
        _put_setting(token, "WEBHOOK_DEDUP_TTL_SECONDS", original["effective_value"] if original["effective_value"] is not None else 60)


def test_boolean_false_round_trip_and_applies(token):
    original, _ = _get_setting(token, "SAVE_UNKNOWN_FACES")
    try:
        status, body, _ = _put_setting(token, "SAVE_UNKNOWN_FACES", "false")
        assert status == 200, body
        assert body["applied"] is True
        current, _ = _get_setting(token, "SAVE_UNKNOWN_FACES")
        assert current["stored_value"] is False
        assert current["effective_value"] is False     # running process updated
    finally:
        _put_setting(token, "SAVE_UNKNOWN_FACES", str(original["effective_value"]).lower())


def test_invalid_values_rejected_422(token):
    status, body, _ = _put_setting(token, "SIMILARITY_THRESHOLD", "abc")
    assert status == 422, body
    status, body, _ = _put_setting(token, "SIMILARITY_THRESHOLD", "3.0")
    assert status == 422, body
    status, body, _ = _put_setting(token, "LOG_LEVEL", "SHOUTING")
    assert status == 422, body


def test_restart_required_reports_not_applied(token):
    original, _ = _get_setting(token, "QUEUE_WORKERS")
    try:
        status, body, _ = _put_setting(token, "QUEUE_WORKERS", 14)
        assert status == 200, body
        assert body["applied"] is False
        assert body["restart_required"] is True
        assert "restart" in body["message"].lower()
    finally:
        _put_setting(token, "QUEUE_WORKERS", original["stored_value"] or original["default_value"])


def test_env_override_visibility(token):
    """SIMILARITY_THRESHOLD is set in docker-compose env (0.4). An admin edit
    must win AND expose the divergence."""
    original, _ = _get_setting(token, "SIMILARITY_THRESHOLD")
    try:
        status, body, _ = _put_setting(token, "SIMILARITY_THRESHOLD", "0.5")
        assert status == 200, body
        current, _ = _get_setting(token, "SIMILARITY_THRESHOLD")
        assert current["stored_value"] == 0.5
        assert current["effective_value"] == 0.5
        assert current["source"] == "database"
        if current["env_value"] is not None:
            assert current["overridden"] is True
            assert current["env_value"] == 0.4
    finally:
        _put_setting(token, "SIMILARITY_THRESHOLD", "0.4")


def test_sensitive_values_masked(token):
    body, _ = _get_setting(token, "JWT_SECRET_KEY")
    assert body["stored_value"] == "***HIDDEN***"
    assert body["effective_value"] == "***HIDDEN***"
    assert body["value"] == "***HIDDEN***"


def test_audit_log_records_action(token):
    _put_setting(token, "CACHE_TTL", 3601, reason="audit-action-test")
    _put_setting(token, "CACHE_TTL", 3600, reason="audit-action-restore")
    status, body, _ = _http("GET", "/api/settings/audit/log?setting_key=CACHE_TTL&limit=2", token=token)
    assert status == 200
    assert len(body["logs"]) >= 1
    assert body["logs"][0]["action"] in ("value_applied", "value_saved")


# ---------------------------------------------------------------------------
# JS-level guards (source assertions)
# ---------------------------------------------------------------------------

def test_frontend_double_submit_guard_and_no_store():
    src = open("frontend/js/admin-settings.js", encoding="utf-8").read()
    assert "saveInFlight" in src, "double-submit guard missing"
    assert "cache: 'no-store'" in src
    assert "credentials: 'include'" in src
    assert "onclick=" not in src, "inline onclick strings must not exist"


# ---------------------------------------------------------------------------
# Retention: status, dry run, real run (with seeded backdated data)
# ---------------------------------------------------------------------------

def test_retention_status_endpoint(token):
    status, body, _ = _http("GET", "/api/admin/retention/status", token=token)
    assert status == 200, body
    assert "DATA_RETENTION_DAYS" in body["settings"]
    assert body["settings"]["DATA_RETENTION_DAYS"]["apply_mode"] == "next_job_run"
    assert "running" in body
    # Flattened monitor-page contract
    for key in ("enabled", "stored_retention_days", "effective_retention_days",
                "source", "apply_mode", "cleanup_interval_seconds", "currently_running"):
        assert key in body, f"status missing flattened key {key}"


def _run_retention_job(token, dry_run, timeout=120):
    """POST /run (202 + job) then poll the task until it finishes."""
    body = {"dry_run": dry_run}
    if not dry_run:
        body["confirmation"] = "DELETE_EXPIRED_DATA"
    status, resp, _ = _http("POST", "/api/admin/retention/run", body, token=token)
    assert status == 202, f"run not accepted: {resp}"
    assert resp["accepted"] is True and resp["job_id"].startswith("retention-")
    deadline = time.time() + timeout
    while time.time() < deadline:
        s, task, _ = _http("GET", f"/api/tasks/{resp['task_id']}", token=token)
        assert s == 200, task
        if task["status"] in ("completed", "failed", "cancelled"):
            assert task["status"] == "completed", f"retention job did not complete: {task}"
            return task["result"]
        time.sleep(1)
    raise AssertionError("retention job did not finish in time")


def _seed_backdated_detection(days_old=400, with_file=True, missing_file=False):
    """Insert a backdated detection + face (+ optional temp image file)."""
    import os
    from datetime import datetime, timedelta

    async def _seed():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        path = None
        if with_file:
            os.makedirs("/app/storage/retention-test", exist_ok=True)
            path = f"/app/storage/retention-test/old_{time.time()}.jpg"
            if not missing_file:
                with open(path, "wb") as f:
                    f.write(b"\xff\xd8\xff\xe0" + b"0" * 128 + b"\xff\xd9")
        async with db_manager.get_session() as db:
            await db.execute(sa_text(
                "INSERT INTO pipelines (pipeline_id, created_at, is_active) "
                "VALUES ('retention-test', now(), 1) ON CONFLICT (pipeline_id) DO NOTHING"))
            det_id = (await db.execute(sa_text(
                "INSERT INTO detections (uuid, pipeline_id, timestamp) "
                "VALUES (:u, 'retention-test', :ts) RETURNING id"),
                {"u": f"ret-test-{time.time()}",
                 "ts": datetime.utcnow() - timedelta(days=days_old)})).scalar()
            await db.execute(sa_text(
                "INSERT INTO faces (detection_id, name, similarity, face_image_path) "
                "VALUES (:d, 'Unknown', 0.0, :p)"), {"d": det_id, "p": path})
            await db.commit()
            return det_id, path

    return run_async(_seed())


def _detection_exists(det_id):
    async def _check():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            return (await db.execute(sa_text(
                "SELECT count(*) FROM detections WHERE id=:i"), {"i": det_id})).scalar()
    return run_async(_check())


def test_retention_dry_run_deletes_nothing(token):
    det_id, path = _seed_backdated_detection(days_old=400)
    try:
        body = _run_retention_job(token, dry_run=True)
        assert body["dry_run"] is True
        assert body["candidate_rows"] >= 1
        assert body["rows_deleted"] == 0
        assert _detection_exists(det_id) == 1, "dry run must not delete rows"
        import os
        assert os.path.exists(path), "dry run must not delete files"
    finally:
        pass  # cleaned by the real-run test below or retention itself


def test_retention_real_run_deletes_expired_keeps_recent(token):
    import os
    old_id, old_path = _seed_backdated_detection(days_old=400)
    missing_id, _ = _seed_backdated_detection(days_old=399, with_file=True, missing_file=True)
    recent_id, _ = _seed_backdated_detection(days_old=0, with_file=False)

    body = _run_retention_job(token, dry_run=False)
    assert body["dry_run"] is False
    assert body["status"] == "completed"
    assert body["rows_deleted"] >= 2
    assert body["missing_files"] >= 1, "missing file must be counted, not crash the run"

    assert _detection_exists(old_id) == 0, "expired detection must be deleted"
    assert not os.path.exists(old_path), "expired file must be deleted"
    assert _detection_exists(missing_id) == 0, "missing file must not abort cleanup"
    assert _detection_exists(recent_id) == 1, "recent record must survive"

    # cleanup the recent seeded row + pipeline (cascades)
    async def _cleanup():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            await db.execute(sa_text("DELETE FROM pipelines WHERE pipeline_id='retention-test'"))
            await db.commit()
    run_async(_cleanup())


def test_retention_runs_cannot_overlap():
    """In-process lock: a second run while one is in progress is skipped."""
    from backend.core.data_retention import DataRetentionManager

    async def scenario():
        rm = DataRetentionManager()
        async with rm._run_lock:
            return await rm.cleanup_old_data(dry_run=True)

    result = run_async(scenario())
    assert result["status"] == "skipped"
    assert result["reason"] == "already_running"


# ---------------------------------------------------------------------------
# Hydration (settings survive restart)
# ---------------------------------------------------------------------------

def test_hydrate_applies_admin_values():
    """A DB value differing from env/default is applied to the runtime object."""
    from backend.core import runtime_settings
    from config import settings as cfg

    async def scenario():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        original = cfg.MAX_PHOTOS_PER_PERSON
        async with db_manager.get_session() as db:
            await db.execute(sa_text(
                "UPDATE settings SET value='7' WHERE key='MAX_PHOTOS_PER_PERSON'"))
            await db.commit()
            try:
                applied = await runtime_settings.hydrate_from_db(db)
                assert applied >= 1
                assert cfg.MAX_PHOTOS_PER_PERSON == 7
            finally:
                await db.execute(sa_text(
                    "UPDATE settings SET value=:v WHERE key='MAX_PHOTOS_PER_PERSON'"),
                    {"v": str(original).lower() if isinstance(original, bool) else str(original)})
                await db.commit()
                setattr(cfg, 'MAX_PHOTOS_PER_PERSON', original)

    run_async(scenario())


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
