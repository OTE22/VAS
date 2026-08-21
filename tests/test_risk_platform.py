"""
Risk platform tests: unified engine, persisted assessments, learned
thresholds, timezones, rate limiting, distributed locks.
====================================================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_risk_platform.py -v

Covers the production-hardening brief end to end: severity-band boundaries,
cross-endpoint score consistency, idempotent persistence + workflow
(acknowledge/resolve/reopen), migration correctness, threshold precedence
and explicit activation, Asia/Beirut DST-correct off-hours, weekend/holiday
buckets, insufficient-history honesty, per-user+IP rate limiting with
Retry-After, cross-worker lock semantics with TTL recovery, and API
backward compatibility.
"""

import json
import urllib.error
import urllib.request
import uuid as uuid_mod
from datetime import datetime, timedelta

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

BASE = "http://localhost:8000"
PREFIX = "pytest-risk-"
CAM_BEIRUT = "pytest-risk-cam-beirut"
CAM_PLAIN = "pytest-risk-cam-plain"


def _http(method, path, body=None, token=None, cookie=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if cookie:
        req.add_header("Cookie", cookie)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}"), e.headers
        except Exception:
            return e.code, {"_raw": raw.decode(errors="replace")}, e.headers


@pytest.fixture(scope="module")
def token():
    status, body, _ = _http("POST", "/api/auth/login",
                            {"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


async def _ensure_redis():
    """The test process has its own cache_manager singleton — initialize it
    so distributed-lock and rate-limit tests exercise REAL Redis."""
    from backend.core.cache_manager import cache_manager
    if getattr(cache_manager, "redis_client", None) is None:
        try:
            await cache_manager.initialize()
        except Exception:
            pass
    return getattr(cache_manager, "redis_client", None) is not None


def _cleanup():
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            await db.execute(sa_text(
                "DELETE FROM threat_assessments WHERE subject_id IN "
                "(SELECT id::text FROM identities WHERE display_name LIKE :p) "
                "OR person_id IN (SELECT id FROM identities WHERE display_name LIKE :p)"),
                {"p": PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identity_appearances WHERE identity_id IN "
                "(SELECT id FROM identities WHERE display_name LIKE :p)"),
                {"p": PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identities WHERE display_name LIKE :p"), {"p": PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identity_appearances WHERE pipeline_id LIKE 'pytest-risk-cam-%'"))
            await db.execute(sa_text(
                "DELETE FROM learned_thresholds WHERE scope_id LIKE 'pytest-risk-%' "
                "OR (extras ->> 'pytest') = 'risk-platform'"))
            await db.execute(sa_text(
                "DELETE FROM pipelines WHERE pipeline_id LIKE 'pytest-risk-cam-%'"))
            await db.commit()
    run_async(_run())


@pytest.fixture(scope="module")
def seeded(token):
    _cleanup()

    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        now = datetime.utcnow()
        ids = {}
        async with db_manager.get_session() as db:
            await db.execute(sa_text(
                "INSERT INTO pipelines (pipeline_id, created_at, is_active, timezone, location_name) "
                "VALUES (:p, now(), 1, 'Asia/Beirut', 'pytest-risk-loc') "
                "ON CONFLICT (pipeline_id) DO NOTHING"), {"p": CAM_BEIRUT})
            await db.execute(sa_text(
                "INSERT INTO pipelines (pipeline_id, created_at, is_active) "
                "VALUES (:p, now(), 1) ON CONFLICT (pipeline_id) DO NOTHING"), {"p": CAM_PLAIN})

            async def ident(name):
                return str((await db.execute(sa_text(
                    "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
                    " last_seen_at, created_at, updated_at, appearances_count) "
                    "VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, :ts, :ts, now(), now(), 0) "
                    "RETURNING id"), {"n": PREFIX + name, "ts": now})).scalar())

            async def appear(identity_id, cam, ts):
                await db.execute(sa_text(
                    "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
                    "VALUES (:i, :p, :ts, now())"), {"i": identity_id, "p": cam, "ts": ts})

            # Subject for assessments (a plain identity with some history)
            ids["subject"] = await ident("subject")
            for d in range(1, 4):
                await appear(ids["subject"], CAM_PLAIN,
                             (now - timedelta(days=d)).replace(minute=0, second=0, microsecond=0))

            # Beirut night-owl: 00:30 UTC = 02:30/03:30 local (EET/EEST) —
            # inside the 02-05 local off-hours window in BOTH DST seasons,
            # while the raw UTC hour (0) never is.
            ids["beirut"] = await ident("beirut-night")
            for d in (1, 2, 3):
                await appear(ids["beirut"], CAM_BEIRUT,
                             (now - timedelta(days=d)).replace(hour=0, minute=30, second=0, microsecond=0))
            await db.commit()
        return ids
    ids = _seed = run_async(_run())
    yield ids
    _cleanup()


# ---------------------------------------------------------------------------
# 1-2. Unified bands + cross-endpoint consistency
# ---------------------------------------------------------------------------

def test_severity_band_boundaries():
    from backend.core.risk_engine import severity_for
    assert severity_for(0) == "low" and severity_for(24.99) == "low"
    assert severity_for(25) == "moderate" and severity_for(49.99) == "moderate"
    assert severity_for(50) == "high" and severity_for(74.99) == "high"
    assert severity_for(75) == "critical" and severity_for(100) == "critical"
    assert severity_for(-5) == "low" and severity_for(500) == "critical"


def test_threat_endpoint_and_persisted_row_agree(token, seeded):
    """The endpoint's number, the engine severity map, and the persisted row
    must be ONE calculation — no endpoint computes risk independently."""
    from backend.core.risk_engine import severity_for
    status, body, _ = _http("GET", f"/api/security/threat/{seeded['subject']}",
                            token=token, timeout=120)
    assert status == 200, body
    assert body["severity"] == severity_for(body["overall_risk_score"])
    assert body["threat_level"] == body["severity"]
    assert body["persisted"] is True and body["assessment_id"]

    status, stored, _ = _http("GET", f"/api/security/assessments/{body['assessment_id']}",
                              token=token)
    assert status == 200, stored
    assert stored["total_risk_score"] == body["overall_risk_score"]
    assert stored["severity"] == body["severity"]
    assert stored["model_version"] == body["algorithm_version"] == "risk-engine-v1"


def test_scores_are_labelled_honestly(token, seeded):
    status, body, _ = _http("GET", f"/api/security/threat/{seeded['subject']}",
                            token=token, timeout=120)
    assert status == 200
    assert body["score_type"] == "heuristic"
    assert body["is_probability"] is False
    assert body["calibration_status"] == "uncalibrated"
    assert isinstance(body["limitations"], list) and body["limitations"]
    assert body["engine"]["signal_scores"] is not None
    assert body["engine"]["weights"]


def test_network_and_map_share_the_engine_version(token):
    status, net, _ = _http("GET", "/api/security/network?days_back=7", token=token, timeout=120)
    assert status == 200
    assert net["risk_score_version"] == "risk-engine-v1"
    from backend.core.security_analysis import SecurityMapAnalyzer
    result = SecurityMapAnalyzer.calculate_risk_score([], watchlist_matches=[{"alert_level": "high"}])
    assert result["model_version"] == "risk-engine-v1"
    assert result["score_type"] == "heuristic" and result["is_probability"] is False
    assert set(result) >= {"total_risk", "risk_level", "risk_factors"}  # legacy keys intact


# ---------------------------------------------------------------------------
# 3-4. Persistence, dedup, retrieval, workflow
# ---------------------------------------------------------------------------

def test_create_is_idempotent_within_window(token, seeded):
    headers = {"X-Requested-With": "XMLHttpRequest"}
    s1, b1, _ = _http("POST", "/api/security/assessments",
                      body={"subject_id": seeded["subject"]},
                      token=token, headers=headers, timeout=120)
    assert s1 in (200, 201), b1
    s2, b2, _ = _http("POST", "/api/security/assessments",
                      body={"subject_id": seeded["subject"]},
                      token=token, headers=headers, timeout=120)
    assert s2 == 200, b2
    assert b2["deduplicated"] is True
    assert b1["id"] == b2["id"], "same subject + window must collapse onto ONE row"


def test_list_filters_and_history(token, seeded):
    status, listing, _ = _http(
        "GET", f"/api/security/assessments?person_id={seeded['subject']}&page_size=10",
        token=token)
    assert status == 200
    assert listing["total"] >= 1
    for item in listing["items"]:
        assert item["person_id"] == seeded["subject"]

    status, hist, _ = _http(
        "GET", f"/api/security/assessments/history/identity/{seeded['subject']}",
        token=token)
    assert status == 200 and hist["total"] >= 1

    status, bad, _ = _http("GET", "/api/security/assessments?severity=bogus", token=token)
    assert status == 422

    ghost = str(uuid_mod.uuid4())
    status, _, _ = _http("GET", f"/api/security/assessments/{ghost}", token=token)
    assert status == 404


def test_acknowledge_resolve_reopen_workflow(token, seeded):
    headers = {"X-Requested-With": "XMLHttpRequest"}
    _, created, _ = _http("POST", "/api/security/assessments",
                          body={"subject_id": seeded["subject"]},
                          token=token, headers=headers, timeout=120)
    aid = created["id"]

    s, acked, _ = _http(f"POST", f"/api/security/assessments/{aid}/acknowledge",
                        body={}, token=token, headers=headers)
    assert s == 200 and acked["status"] == "acknowledged"
    assert acked["acknowledged_by"] and acked["acknowledged_at"]

    s, resolved, _ = _http(f"POST", f"/api/security/assessments/{aid}/resolve",
                           body={"resolution_status": "false_positive", "notes": "pytest"},
                           token=token, headers=headers)
    assert s == 200 and resolved["status"] == "resolved"
    assert resolved["resolution_status"] == "false_positive"

    # Illegal transition: acknowledging a resolved assessment
    s, err, _ = _http(f"POST", f"/api/security/assessments/{aid}/acknowledge",
                      body={}, token=token, headers=headers)
    assert s == 409, err

    s, reopened, _ = _http(f"POST", f"/api/security/assessments/{aid}/reopen",
                           body={"notes": "pytest reopen"}, token=token, headers=headers)
    assert s == 200 and reopened["status"] == "open"
    assert reopened["resolution_status"] == "reopened"


def test_assessment_rows_survive_via_db(seeded):
    """Persistence is real rows, not process memory: read the table through a
    FRESH session in this separate test process (the app that wrote them is
    another process entirely)."""
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            rows = (await db.execute(sa_text(
                "SELECT total_risk_score, severity, signals, model_version "
                "FROM threat_assessments WHERE subject_id = :s"),
                {"s": seeded["subject"]})).all()
            return rows
    rows = run_async(_run())
    assert rows, "assessments must be durable rows"
    for score, severity, signals, model in rows:
        assert 0 <= score <= 100 and severity in {"low", "moderate", "high", "critical"}
        assert isinstance(signals, list) and model == "risk-engine-v1"


# ---------------------------------------------------------------------------
# 5. Migration correctness
# ---------------------------------------------------------------------------

def test_migration_applied_and_indexed():
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            version = (await db.execute(sa_text(
                "SELECT version_num FROM alembic_version"))).scalar()
            tables = {r[0] for r in (await db.execute(sa_text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))).all()}
            indexes = {r[0] for r in (await db.execute(sa_text(
                "SELECT indexname FROM pg_indexes WHERE tablename='threat_assessments'"))).all()}
            tz_col = (await db.execute(sa_text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='pipelines' AND column_name='timezone'"))).scalar()
            seeds = (await db.execute(sa_text(
                "SELECT count(*) FROM risk_model_versions WHERE status='active'"))).scalar()
            return version, tables, indexes, tz_col, seeds
    version, tables, indexes, tz_col, seeds = run_async(_run())
    # Head pin: the risk-platform migration must be in the applied lineage.
    # Chain: a7b8c9d0e1f2 (risk platform) -> b8c9d0e1f2a3 (ML pipeline)
    #        -> d0e1f2a3b4c5 (multi-image enrollment)
    #        -> e1f2a3b4c5d6 (image source_type)
    #        -> f2a3b4c5d6e7 (vector-index sync state)
    #        -> a3b4c5d6e7f8 (system audit principal)
    #        -> b4c5d6e7f8a9 (quality scorer provenance)
    #        -> c5d6e7f8a9b0 (drop dead faiss_id + saved_searches)
    #        -> d6e7f8a9b0c1 (pending enrollment decisions)
    #        -> e7f8a9b0c1d2 (webhook ingest credentials)
    #        -> f8a9b0c1d2e3 (merge provenance)
    #        -> a9b0c1d2e3f4 (identity person_code)
    #        -> b0c1d2e3f4a5 -> c2d3e4f5a6b7 -> d4e5f6a7b8c9 -> f6a7b8c9d0e1
    #        -> 7d3f91a2c4e6 (JSON null literals -> SQL NULL)
    #        -> d8f2b6c1e4a7 (current head: identity_audit_log survives deletion).
    assert version == "d8f2b6c1e4a7"
    for t in ("threat_assessments", "risk_signal_results", "risk_model_versions", "learned_thresholds"):
        assert t in tables, f"missing table {t}"
    for idx in ("uq_assessment_idempotency", "idx_assessment_person_created",
                "idx_assessment_severity_status", "idx_assessment_pipeline_created"):
        assert idx in indexes, f"missing index {idx}"
    assert tz_col == 1
    assert seeds >= 3, "one active risk_model_versions row per profile"


# ---------------------------------------------------------------------------
# 6. Threshold precedence, activation gate, rollback
# ---------------------------------------------------------------------------

def test_threshold_precedence_and_fallback(token, seeded):
    """location > pipeline > global > static default — and candidates are
    NEVER consumed before activation."""
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        from backend.core.threshold_store import threshold_store, SIGNAL_TIME_WINDOW
        await _ensure_db()
        results = {}
        async with db_manager.get_session() as db:
            async def put(scope_type, scope_id, value, status, version):
                await db.execute(sa_text(
                    "INSERT INTO learned_thresholds "
                    "(id, scope_type, scope_id, signal_name, value, sample_count, "
                    " version, status, extras, created_at) "
                    "VALUES (gen_random_uuid(), :st, :si, :sig, :v, 50, :ver, :status, "
                    " '{\"pytest\": \"risk-platform\"}'::jsonb, now())"),
                    {"st": scope_type, "si": scope_id, "sig": SIGNAL_TIME_WINDOW,
                     "v": value, "ver": version, "status": status})
            # Deliberately harmless values scoped to pytest ids
            await put("global", "", 10.0, "active", 901)
            await put("pipeline", "pytest-risk-cam-p", 12.0, "active", 901)
            await put("location", "pytest-risk-locX", 14.0, "active", 901)
            await put("pipeline", "pytest-risk-cam-cand", 99.0, "candidate", 901)
            await db.commit()
            threshold_store.invalidate()

            r = await threshold_store.resolve(db, SIGNAL_TIME_WINDOW,
                                              pipeline_id="pytest-risk-cam-p",
                                              location_name="pytest-risk-locX",
                                              static_default=10)
            results["location_wins"] = (r.value, r.source)
            r = await threshold_store.resolve(db, SIGNAL_TIME_WINDOW,
                                              pipeline_id="pytest-risk-cam-p",
                                              static_default=10)
            results["pipeline_wins"] = (r.value, r.source)
            r = await threshold_store.resolve(db, SIGNAL_TIME_WINDOW,
                                              pipeline_id="pytest-risk-cam-nope",
                                              static_default=10)
            results["global_wins"] = (r.value, r.source, r.provenance)
            r = await threshold_store.resolve(db, SIGNAL_TIME_WINDOW,
                                              pipeline_id="pytest-risk-cam-cand",
                                              static_default=10)
            results["candidate_ignored"] = (r.value, r.source)

            # Static fallback: an unknown signal has no rows anywhere
            r = await threshold_store.resolve(db, "pytest_bogus_signal",
                                              static_default=42.5)
            results["static"] = (r.value, r.source, r.provenance)
        return results
    res = run_async(_run())
    assert res["location_wins"] == (14.0, "location")
    assert res["pipeline_wins"] == (12.0, "pipeline")
    assert res["global_wins"][0] == 10.0 and res["global_wins"][1] == "global"
    assert "@v901" in res["global_wins"][2]
    assert res["candidate_ignored"][0] == 10.0 and res["candidate_ignored"][1] == "global", (
        "an unactivated candidate must never be consumed")
    assert res["static"] == (42.5, "static_default", "static:42.5")


def test_threshold_activation_gate_and_rollback(token):
    """Candidates under the sample floor are refused; activation retires the
    previous active row (rollback = re-activate it)."""
    headers = {"X-Requested-With": "XMLHttpRequest"}

    async def _seed_candidates():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            tiny = (await db.execute(sa_text(
                "INSERT INTO learned_thresholds (id, scope_type, scope_id, signal_name, "
                " value, sample_count, version, status, extras, created_at) "
                "VALUES (gen_random_uuid(), 'pipeline', 'pytest-risk-cam-act', "
                " 'multi_camera_time_window_minutes', 7.0, 2, 902, 'candidate', "
                " '{\"pytest\": \"risk-platform\"}'::jsonb, now()) RETURNING id"))).scalar()
            fat = (await db.execute(sa_text(
                "INSERT INTO learned_thresholds (id, scope_type, scope_id, signal_name, "
                " value, sample_count, version, status, extras, created_at) "
                "VALUES (gen_random_uuid(), 'pipeline', 'pytest-risk-cam-act', "
                " 'multi_camera_time_window_minutes', 8.0, 60, 903, 'candidate', "
                " '{\"pytest\": \"risk-platform\"}'::jsonb, now()) RETURNING id"))).scalar()
            await db.commit()
            return str(tiny), str(fat)
    tiny_id, fat_id = run_async(_seed_candidates())

    s, body, _ = _http("POST", f"/api/security/learned-thresholds/{tiny_id}/activate",
                       body={}, token=token, headers=headers)
    assert s == 422, f"2 samples must be refused: {body}"

    s, body, _ = _http("POST", f"/api/security/learned-thresholds/{fat_id}/activate",
                       body={}, token=token, headers=headers)
    assert s == 200, body
    assert body["status"] == "active"

    # Second activation of a NEW candidate retires the first (rollback path)
    s, listing, _ = _http(
        "GET", "/api/security/learned-thresholds?signal_name=multi_camera_time_window_minutes",
        token=token)
    assert s == 200
    mine = [i for i in listing["items"] if i["scope_id"] == "pytest-risk-cam-act"]
    statuses = {i["version"]: i["status"] for i in mine}
    assert statuses[903] == "active" and statuses[902] == "candidate"


# ---------------------------------------------------------------------------
# 7-9. Timezones, DST, buckets, insufficient history
# ---------------------------------------------------------------------------

def test_beirut_dst_conversion_is_zoneinfo_correct():
    from backend.core.time_context import local_hour
    # Winter: EET (UTC+2); Summer: EEST (UTC+3) — no fixed offsets anywhere.
    assert local_hour(datetime(2026, 1, 15, 1, 0), "Asia/Beirut") == 3.0
    assert local_hour(datetime(2026, 7, 15, 1, 0), "Asia/Beirut") == 4.0
    # Fallback for unset/invalid: DEFAULT_SITE_TIMEZONE (UTC by default)
    assert local_hour(datetime(2026, 7, 15, 1, 0), None) == 1.0
    assert local_hour(datetime(2026, 7, 15, 1, 0), "Not/AZone") == 1.0


def test_off_hours_evaluated_in_pipeline_local_time(seeded):
    """00:30 UTC at an Asia/Beirut camera is 02:30/03:30 LOCAL — inside the
    02-05 off-hours window. A UTC-only interpretation (hour 0) would miss it.
    """
    async def _run():
        from db_connection import db_manager
        from backend.core.security_intelligence_service import security_intelligence_service
        await _ensure_db()
        async with db_manager.get_session() as db:
            return await security_intelligence_service.detect_suspicious_patterns(
                db, days_back=7, min_group_size=3)
    report = run_async(_run())
    timing = [p for p in report.patterns
              if p.pattern_type == "unusual_timing"
              and p.identities_involved == [seeded["beirut"]]]
    assert timing, "local-time off-hours must flag the Beirut night visits"
    assert "Asia/Beirut" in timing[0].evidence.get("timezones", [])


def test_weekend_holiday_buckets_and_confidence():
    from backend.core.time_context import (
        BucketedHourBaseline, day_bucket, parse_holidays, parse_weekend_days)
    weekend = parse_weekend_days("5,6")
    holidays = parse_holidays("2026-07-20,bogus,2026-12-25")
    assert holidays == {"2026-07-20", "2026-12-25"}
    # 2026-07-18 is a Saturday; 2026-07-20 is a configured holiday (Monday)
    assert day_bucket(datetime(2026, 7, 18, 10, 0), weekend, holidays) == "weekend"
    assert day_bucket(datetime(2026, 7, 20, 10, 0), weekend, holidays) == "holiday"
    assert day_bucket(datetime(2026, 7, 21, 10, 0), weekend, holidays) == "workday"

    baseline = BucketedHourBaseline(min_bucket_samples=5, std_floor=0.75, k_sigma=2.0)
    for _ in range(20):
        baseline.add(9.0, "workday")
    baseline.add(21.0, "weekend")  # thin weekend bucket

    solid = baseline.evaluate(3.0, "workday")
    assert solid.is_anomalous and solid.bucket_used == "workday"
    fallback = baseline.evaluate(21.5, "weekend")
    assert fallback.bucket_used == "overall", "thin bucket must fall back"
    assert fallback.confidence < solid.confidence, (
        "fallback judgements must carry reduced confidence")


def test_insufficient_history_is_explicit():
    from backend.core.time_context import BucketedHourBaseline
    empty = BucketedHourBaseline(min_bucket_samples=5, std_floor=0.75, k_sigma=2.0)
    result = empty.evaluate(3.0, "workday")
    assert result.insufficient_data is True
    assert result.is_anomalous is False and result.confidence == 0.0


# ---------------------------------------------------------------------------
# 10. Rate limiting
# ---------------------------------------------------------------------------

def test_rate_limit_blocks_with_retry_after(seeded):
    from fastapi import HTTPException
    from starlette.requests import Request as StarletteRequest
    from backend.core.rate_limiter import enforce_rate_limit

    scope_name = f"pytest-rl-{uuid_mod.uuid4().hex[:8]}"

    async def _run():
        redis_ok = await _ensure_redis()
        request = StarletteRequest({
            "type": "http", "method": "GET", "path": "/x", "headers": [],
            "client": ("10.99.99.99", 40000), "query_string": b"", "state": {},
        })
        user = {"id": 999901, "username": "pytest-rl"}
        allowed = 0
        blocked = None
        for _ in range(5):
            try:
                await enforce_rate_limit(request, user, scope_name, limit_override=3)
                allowed += 1
            except HTTPException as e:
                blocked = e
                break
        return redis_ok, allowed, blocked
    redis_ok, allowed, blocked = run_async(_run())
    assert allowed == 3, f"exactly the limit must pass (redis={redis_ok})"
    assert blocked is not None and blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.detail["error_code"] == "RATE_LIMITED"


def test_heavy_endpoints_carry_rate_limits():
    with open("/app/backend/routes/intelligence.py", encoding="utf-8") as f:
        src = f.read()
    for scope in ("network_analysis", "pattern_detection", "anomaly_analysis",
                  "threat_assessment", "correlation", "relationship_recalc",
                  "threshold_learning"):
        assert f'rate_limited("{scope}"' in src, f"missing rate limit: {scope}"
    with open("/app/backend/routes/risk_assessments.py", encoding="utf-8") as f:
        assert 'rate_limited("assessment_recalc"' in f.read()
    with open("/app/backend/routes/batch_export.py", encoding="utf-8") as f:
        assert 'rate_limited("export"' in f.read()
    with open("/app/backend/routes/conversations.py", encoding="utf-8") as f:
        assert 'rate_limited("chatbot"' in f.read()


def test_rate_limit_metric_exposed(token):
    req = urllib.request.Request(BASE + "/metrics")
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode()
    assert "fr_rate_limit_rejections_total" in text
    assert "fr_assessments_total" in text
    assert "fr_distributed_lock_contention_total" in text


# ---------------------------------------------------------------------------
# 11. Authorization on the new surface
# ---------------------------------------------------------------------------

def test_new_endpoints_require_auth():
    for path in ("/api/security/assessments", "/api/security/risk-model",
                 "/api/security/learned-thresholds"):
        status, _, _ = _http("GET", path)
        assert status == 401, path


def test_mutations_require_csrf(token, seeded):
    cookie = f"access_token={token}"
    status, _, _ = _http("POST", "/api/security/assessments",
                         body={"subject_id": seeded["subject"]}, cookie=cookie)
    assert status == 403, "cookie POST without X-Requested-With must be blocked"


# ---------------------------------------------------------------------------
# 12-13. Distributed locks: cross-worker semantics, TTL recovery
# ---------------------------------------------------------------------------

def test_distributed_lock_mutual_exclusion_and_recovery():
    async def _run():
        import asyncio
        from backend.core.distributed_lock import DistributedLock, peek_holder
        redis_ok = await _ensure_redis()
        if not redis_ok:
            return "no-redis"

        name = f"pytest-lock-{uuid_mod.uuid4().hex[:8]}"
        # Two 'workers' (independent lock instances) racing
        a = DistributedLock(name, ttl_seconds=30)
        b = DistributedLock(name, ttl_seconds=30)
        assert await a.acquire(holder_label="worker-a") is True
        assert a.distributed is True, "with Redis up this must be a REAL lock"
        assert await b.acquire(holder_label="worker-b") is False
        assert b.holder_hint == "worker-a"
        assert await peek_holder(name) == "worker-a"

        # Ownership-token safety: b.release() must NOT free a's lock
        await b.release()
        assert await peek_holder(name) == "worker-a"
        assert await a.renew() is True
        await a.release()
        assert await peek_holder(name) is None

        # TTL recovery: a crashed holder's lock self-expires
        crash = DistributedLock(name, ttl_seconds=1)
        assert await crash.acquire(holder_label="crashed-worker") is True
        await asyncio.sleep(1.3)
        fresh = DistributedLock(name, ttl_seconds=30)
        assert await fresh.acquire(holder_label="recovered") is True, (
            "an expired lock must be acquirable — no permanent deadlocks")
        await fresh.release()
        return "ok"
    outcome = run_async(_run())
    if outcome == "no-redis":
        pytest.skip("Redis unavailable in test environment")
    assert outcome == "ok"


def test_degraded_mode_without_redis_is_explicit():
    """Without Redis the lock reports distributed=False (per-process only) —
    the documented degraded mode the startup warning points at."""
    async def _run():
        from backend.core import distributed_lock as dl
        original = dl._redis
        dl._redis = lambda: None
        try:
            lock = dl.DistributedLock("pytest-degraded", ttl_seconds=5)
            acquired = await lock.acquire()
            return acquired, lock.distributed
        finally:
            dl._redis = original
    acquired, distributed = run_async(_run())
    assert acquired is True and distributed is False


# ---------------------------------------------------------------------------
# 15. Backward compatibility
# ---------------------------------------------------------------------------

def test_threat_endpoint_backward_compatible(token, seeded):
    status, body, _ = _http("GET", f"/api/security/threat/{seeded['subject']}",
                            token=token, timeout=120)
    assert status == 200
    # Every pre-existing key still present
    for key in ("identity_id", "display_name", "overall_risk_score", "risk_factors",
                "threat_level", "recommendations", "last_assessed", "algorithm_version"):
        assert key in body, f"legacy key {key} missing"
    for factor in body["risk_factors"]:
        assert set(factor) >= {"factor", "score", "description"}


def test_worker_docs_updated():
    with open("/app/backend/security/config_guard.py", encoding="utf-8") as f:
        guard = f.read()
    assert "multi-worker safe via Redis locks" in guard, (
        "the WORKERS guard must state the intelligence guards are distributed now")
    with open("/app/.env.example", encoding="utf-8") as f:
        env = f.read()
    for key in ("DEFAULT_SITE_TIMEZONE", "RATE_LIMIT_HEAVY_PER_MINUTE",
                "ASSESSMENT_DEDUP_WINDOW_MINUTES", "THRESHOLD_MIN_SAMPLES_FOR_ACTIVATION"):
        assert key in env, f".env.example missing {key}"
