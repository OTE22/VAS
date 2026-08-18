"""
Security-intelligence algorithm validation on seeded, realistic data
====================================================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_intel_algorithms.py -v

Before this file the suite asserted response SHAPES but zero numeric
correctness — every headline bug it now pins (225% co-appearance, the
arithmetic-mean-of-clock-hours anomaly, first-hop-only trajectories, the
loitering busy-loop) shipped with green tests. Each scenario seeds
controllable identities/appearances via SQL (the pattern from
test_dashboard_system), exercises the REAL endpoint or service, and cleans up
in teardown. Seeded appearance rows deliberately leave end_time NULL — that
is what live data looks like (34/34 rows at audit time) and it exercises the
COALESCE overlap fix.
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

BASE = "http://localhost:8000"
ROUTES_PATH = "/app/backend/routes/intelligence.py"
SERVICE_PATH = "/app/backend/core/security_intelligence_service.py"

CAM = "pytest-intel-cam-{}"  # all seeded pipelines share this prefix
NAME_PREFIX = "pytest-intel-"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


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
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"_raw": raw.decode(errors="replace")}


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         {"username": "admin", "password": "admin123"})
    assert status == 200, f"admin login failed: {body}"
    return body["access_token"]


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


# ---------------------------------------------------------------------------
# Seeding — one realistic dataset for the whole module
# ---------------------------------------------------------------------------

def _seed_dataset():
    """Seed every scenario at once; returns the id map."""
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        now = datetime.utcnow()
        ids = {}

        async with db_manager.get_session() as db:
            # --- cameras (with coordinates where the scenario needs them) ---
            cams = {
                CAM.format("a"): (33.5000, 44.4000),   # anomaly / co-appearance home camera
                CAM.format("b"): (33.5009, 44.4000),   # ~100 m north of a (correlation pair)
                CAM.format("rapid1"): (33.6000, 44.5000),
                CAM.format("rapid2"): (33.6090, 44.5000),  # ~1 km north of rapid1
                CAM.format("ta"): (None, None),
                CAM.format("tb"): (None, None),
                CAM.format("tc"): (None, None),
                CAM.format("group"): (None, None),
            }
            for cam, (lat, lng) in cams.items():
                await db.execute(sa_text(
                    "INSERT INTO pipelines (pipeline_id, created_at, is_active, latitude, longitude) "
                    "VALUES (:p, now(), 1, :lat, :lng) ON CONFLICT (pipeline_id) DO NOTHING"),
                    {"p": cam, "lat": lat, "lng": lng})

            async def ident(name):
                return str((await db.execute(sa_text(
                    "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
                    " last_seen_at, created_at, updated_at, appearances_count) "
                    "VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, :ts, :ts, now(), now(), 0) "
                    "RETURNING id"), {"n": NAME_PREFIX + name, "ts": now})).scalar())

            async def appear(identity_id, cam, ts):
                # end_time stays NULL on purpose — mirrors live data.
                await db.execute(sa_text(
                    "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
                    "VALUES (:i, :p, :ts, now())"), {"i": identity_id, "p": cam, "ts": ts})

            # --- 1. circular anomaly: midnight-straddler (23:00/01:00 regular) ---
            # Baseline strictly OLDER than the 7-day recent window.
            ids["night_regular"] = await ident("night-regular")
            for d in range(8, 16):
                day = now - timedelta(days=d)
                hour = 23 if d % 2 == 0 else 1
                await appear(ids["night_regular"], CAM.format("a"),
                             day.replace(hour=hour, minute=0, second=0, microsecond=0))
            # Recent rows at the same hours, same camera: NOT anomalous.
            for d in (1, 2, 3):
                day = now - timedelta(days=d)
                hour = 23 if d % 2 == 0 else 1
                await appear(ids["night_regular"], CAM.format("a"),
                             day.replace(hour=hour, minute=0, second=0, microsecond=0))

            # --- 2. circular anomaly: 09:00 regular with one 03:00 outlier ---
            ids["day_regular"] = await ident("day-regular")
            for d in range(8, 16):
                day = now - timedelta(days=d)
                await appear(ids["day_regular"], CAM.format("a"),
                             day.replace(hour=9, minute=0, second=0, microsecond=0))
            outlier_day = now - timedelta(days=2)
            await appear(ids["day_regular"], CAM.format("a"),
                         outlier_day.replace(hour=3, minute=0, second=0, microsecond=0))
            await appear(ids["day_regular"], CAM.format("a"),
                         (now - timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0))

            # --- 3. co-appearance percentage bound ---
            # Target T: 4 appearances. Companion O: THREE overlapping rows per
            # T appearance (raw event count 12 > 4 windows — the exact shape
            # that produced 225% live).
            ids["pct_target"] = await ident("pct-target")
            ids["pct_companion"] = await ident("pct-companion")
            for h in range(4):
                t = now - timedelta(days=1, hours=h)
                await appear(ids["pct_target"], CAM.format("a"), t)
                for m in (1, 2, 3):
                    await appear(ids["pct_companion"], CAM.format("a"), t + timedelta(minutes=m))

            # --- 4. patterns: group of 3 in one 5-minute bucket ---
            bucket = (now - timedelta(hours=5)).replace(minute=10, second=0, microsecond=0)
            ids["group"] = []
            for n in range(3):
                gid = await ident(f"group-{n}")
                ids["group"].append(gid)
                await appear(gid, CAM.format("group"), bucket + timedelta(seconds=30 * n))

            # --- 5. patterns: off-hours regular (03:00-04:00 window rows) ---
            ids["night_owl"] = await ident("night-owl")
            base_night = (now - timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)
            for m in (0, 20, 40):
                await appear(ids["night_owl"], CAM.format("a"), base_night + timedelta(minutes=m))

            # --- 6. patterns: rapid movement, 1 km in 60 s (~60 km/h) ---
            ids["runner"] = await ident("runner")
            t_rapid = now - timedelta(hours=3)
            await appear(ids["runner"], CAM.format("rapid1"), t_rapid)
            await appear(ids["runner"], CAM.format("rapid2"), t_rapid + timedelta(seconds=60))

            # --- 7. trajectory: three A->B->C sessions on separate days ---
            ids["walker"] = await ident("walker")
            for d in (2, 3, 4):
                t0 = (now - timedelta(days=d)).replace(hour=10, minute=0, second=0, microsecond=0)
                await appear(ids["walker"], CAM.format("ta"), t0)
                await appear(ids["walker"], CAM.format("tb"), t0 + timedelta(minutes=5))
                await appear(ids["walker"], CAM.format("tc"), t0 + timedelta(minutes=10))

            # --- 8. correlation pair on cameras ~100 m apart ---
            ids["corr_a"] = await ident("corr-a")
            ids["corr_b"] = await ident("corr-b")
            t_corr = (now - timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
            # B appears 3 min after every A appearance; plus one B row BEFORE
            # any A row (must never match — sequences are strictly A-then-B).
            await appear(ids["corr_b"], CAM.format("b"), t_corr - timedelta(minutes=5))
            for h in range(3):
                a_time = t_corr + timedelta(hours=h)
                await appear(ids["corr_a"], CAM.format("a"), a_time)
                await appear(ids["corr_b"], CAM.format("b"), a_time + timedelta(minutes=3))

            await db.commit()
        return ids
    return run_async(_run())


def _cleanup_dataset():
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            await db.execute(sa_text(
                "DELETE FROM threat_assessments WHERE subject_id IN "
                "(SELECT id::text FROM identities WHERE display_name LIKE :p) "
                "OR person_id IN (SELECT id FROM identities WHERE display_name LIKE :p)"),
                {"p": NAME_PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identity_relationships WHERE identity_id_1 IN "
                "(SELECT id FROM identities WHERE display_name LIKE :p) OR identity_id_2 IN "
                "(SELECT id FROM identities WHERE display_name LIKE :p)"),
                {"p": NAME_PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identity_appearances WHERE identity_id IN "
                "(SELECT id FROM identities WHERE display_name LIKE :p)"),
                {"p": NAME_PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identities WHERE display_name LIKE :p"),
                {"p": NAME_PREFIX + "%"})
            await db.execute(sa_text(
                "DELETE FROM identity_appearances WHERE pipeline_id LIKE 'pytest-intel-cam-%'"))
            await db.execute(sa_text(
                "DELETE FROM pipelines WHERE pipeline_id LIKE 'pytest-intel-cam-%'"))
            await db.commit()
    run_async(_run())


@pytest.fixture(scope="module")
def seeded():
    _cleanup_dataset()  # a crashed previous run must not poison this one
    ids = _seed_dataset()
    yield ids
    _cleanup_dataset()


# ---------------------------------------------------------------------------
# Circular statistics — the arithmetic-mean bug, pinned numerically
# ---------------------------------------------------------------------------

def test_circular_mean_handles_midnight_straddle():
    """23:00 and 01:00 average to midnight — NOT to noon. The arithmetic mean
    (the old implementation) says 12.0 and flags this person's every visit."""
    from backend.core.security_intelligence_service import circular_hour_stats
    mean, std = circular_hour_stats([23.0, 1.0, 23.0, 1.0, 23.0, 1.0])
    assert min(mean, 24.0 - mean) < 0.01, f"circular mean must be ~0/24, got {mean}"
    assert 0.9 < std < 1.2, f"std of a +/-1h schedule must be ~1h, got {std}"


def test_circular_distance_wraps_at_24():
    from backend.core.security_intelligence_service import circular_hour_distance
    assert circular_hour_distance(3, 9) == 6.0
    assert circular_hour_distance(23, 1) == 2.0
    assert circular_hour_distance(0, 12) == 12.0


def test_night_regular_produces_no_off_schedule_anomalies(token, seeded):
    """The midnight-straddler at their usual hours: the old arithmetic-mean
    baseline (mean=12h) flagged every one of these visits."""
    status, body = _http(
        "GET", f"/api/security/anomalies/{seeded['night_regular']}?days_back=7",
        token=token)
    assert status == 200, body
    assert body["baseline"]["sufficient"] is True, body["baseline"]
    off_schedule = [a for a in body["items"] if a["anomaly_type"] == "off_schedule"]
    assert off_schedule == [], (
        f"23:00/01:00 activity against a 23:00/01:00 baseline is NOT anomalous: {off_schedule}")


def test_day_regular_03_outlier_is_flagged(token, seeded):
    """03:00 against a solid 09:00 baseline: distance 6h. The old rule
    required diff > 6 (strictly), so this genuine outlier was MISSED."""
    status, body = _http(
        "GET", f"/api/security/anomalies/{seeded['day_regular']}?days_back=7",
        token=token)
    assert status == 200, body
    off_schedule = [a for a in body["items"] if a["anomaly_type"] == "off_schedule"]
    assert len(off_schedule) >= 1, "the 03:00 outlier must be flagged"
    flagged_hours = {round(a["deviation"]["actual_hour"]) for a in off_schedule}
    assert 3 in flagged_hours, f"expected the 03:00 visit flagged, got hours {flagged_hours}"
    for a in off_schedule:
        assert 0 <= a["risk_score"] <= 100
        assert a["deviation"]["sigma_threshold"] > 0


def test_anomaly_envelope_reports_baseline(token, seeded):
    status, body = _http(
        "GET", f"/api/security/anomalies/{seeded['night_regular']}?days_back=7",
        token=token)
    assert status == 200
    for key in ("items", "total", "truncated", "baseline", "recent_count", "algorithm_version"):
        assert key in body, f"anomaly envelope missing {key}"
    assert body["algorithm_version"] == "anomaly-context-v3"
    for key in ("sufficient", "samples", "window_start", "window_end"):
        assert key in body["baseline"]
    assert body["baseline"]["samples"] >= 5


def test_insufficient_baseline_is_not_silence(token, seeded):
    """A fresh identity (companion has only recent rows) must say
    'insufficient baseline', never an empty list that reads as 'normal'."""
    status, body = _http(
        "GET", f"/api/security/anomalies/{seeded['pct_companion']}?days_back=7",
        token=token)
    assert status == 200, body
    assert body["baseline"]["sufficient"] is False
    assert body["items"] == []


# ---------------------------------------------------------------------------
# Co-appearance percentage — bounded 0..100 (was 225 live)
# ---------------------------------------------------------------------------

def test_co_appearance_percentage_is_bounded(token, seeded):
    """3 companion rows per target window: raw event count (12) exceeds the
    target's appearance count (4). The old numerator used the raw count and
    reported 300%. The share of target appearances with company is 100%."""
    status, body = _http(
        "GET", f"/api/identities/{seeded['pct_target']}/related?min_co_appearances=2",
        token=token)
    assert status == 200, body
    items = body["items"]
    assert items, "the companion must be detected as related"
    for r in items:
        pct = r["co_appearance_percentage"]
        assert 0 <= pct <= 100, f"percentage out of bounds: {pct}"
    companion = [r for r in items if r["identity_id"] == seeded["pct_companion"]]
    assert companion, f"companion missing from related list: {items}"
    assert companion[0]["co_appearance_percentage"] == pytest.approx(100.0), (
        "every target appearance had company — the share is exactly 100%")


# ---------------------------------------------------------------------------
# Suspicious patterns — envelope + derived confidences on seeded data
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def patterns_body(token, seeded):
    status, body = _http("GET", "/api/security/patterns?days_back=7", token=token, timeout=120)
    assert status == 200, body
    return body


def test_patterns_envelope_shape(patterns_body):
    for key in ("items", "total", "truncated", "scanned_rows", "analysis_window", "algorithm_version"):
        assert key in patterns_body, f"patterns envelope missing {key}"
    assert patterns_body["algorithm_version"] == "patterns-v2"
    assert patterns_body["truncated"] is False
    assert patterns_body["scanned_rows"] >= 1
    assert patterns_body["analysis_window"]["days_back"] == 7


def test_group_activity_detected_with_derived_confidence(patterns_body, seeded):
    groups = [p for p in patterns_body["items"]
              if p["pattern_type"] == "group_activity"
              and set(seeded["group"]).issubset(set(p["identities_involved"]))]
    assert groups, "the seeded 3-identity bucket must be detected"
    g = groups[0]
    # size == min_size (3), one bucket: 0.25 + (3/6)/2 + 0 = 0.5 exactly —
    # derived from the data, not the old hardcoded 0.8.
    assert g["confidence"] == pytest.approx(0.5), g
    assert g["evidence"]["group_size"] == 3
    assert "group_recurrence" in g["evidence"]


def test_unusual_timing_detected_with_share_based_confidence(patterns_body, seeded):
    timings = [p for p in patterns_body["items"]
               if p["pattern_type"] == "unusual_timing"
               and p["identities_involved"] == [seeded["night_owl"]]]
    assert timings, "the seeded 03:00 regular must be detected"
    t = timings[0]
    # All 3 of the identity's appearances are off-hours: share 1.0 ->
    # confidence 0.4 + 0.6*1.0 = 1.0 (old hardcoded value was 0.7).
    assert t["confidence"] == pytest.approx(1.0), t
    assert t["evidence"]["off_hours_share"] == pytest.approx(1.0)
    assert t["evidence"]["off_hour_appearances"] == 3


def test_rapid_movement_gated_by_implied_speed(patterns_body, seeded):
    rapids = [p for p in patterns_body["items"]
              if p["pattern_type"] == "rapid_movement"
              and p["identities_involved"] == [seeded["runner"]]]
    assert rapids, "1 km in 60 s must be detected as rapid movement"
    r = rapids[0]
    speed = r["evidence"]["implied_speed_kmh"]
    assert 40 <= speed <= 80, f"~60 km/h expected, got {speed}"
    # margin 1-60/300=0.8 -> 0.3+0.5*0.8+0.2 (geometry corroboration) = 0.9
    assert r["confidence"] == pytest.approx(0.9), r


def test_patterns_single_bounded_scan_in_source():
    """Perf contract: ONE bounded appearance scan feeds all three detectors
    (previously three full-table scans, one non-sargable)."""
    src = _read(SERVICE_PATH)
    body = src.split("async def detect_suspicious_patterns", 1)[1].split("\n    def _detect_group_activity", 1)[0]
    # The docstring DESCRIBES the old extract('hour') bug — strip it before
    # scanning, or the explanation reads as the violation.
    code = body.split('"""', 2)[-1]
    assert code.count("select(IdentityAppearance)") == 1, "patterns must issue exactly one appearance scan"
    assert "PATTERN_SCAN_LIMIT" in code and ".limit(" in code
    assert "extract(" not in code, "the non-sargable extract('hour') scan must be gone"


def test_patterns_endpoint_perf_smoke(token, seeded):
    started = time.monotonic()
    status, _ = _http("GET", "/api/security/patterns?days_back=7", token=token, timeout=120)
    elapsed = time.monotonic() - started
    assert status == 200
    assert elapsed < 2.0, f"patterns took {elapsed:.2f}s on a small dataset"


# ---------------------------------------------------------------------------
# Trajectory — multi-hop learning (query mid-route)
# ---------------------------------------------------------------------------

def test_trajectory_predicts_mid_route(token, seeded):
    """Three A->B->C sessions; standing at B the model must predict C. The
    v1 model only learned sessions whose FIRST camera matched the query, so
    it returned 'Insufficient Evidence' for every mid-route query."""
    status, body = _http(
        "GET", "/api/intelligence/trajectory/predict"
               f"?identity_id={seeded['walker']}&current_camera={CAM.format('tb')}",
        token=token)
    assert status == 200, body
    assert body["model_version"] == "trajectory-v2"
    assert body["insufficient_evidence"] is False, (
        "mid-route queries must not read as insufficient evidence")
    predicted = {p["camera_id"]: p for p in body["predictions"]}
    assert CAM.format("tc") in predicted, f"B must predict C: {predicted.keys()}"
    assert predicted[CAM.format("tc")]["probability"] == pytest.approx(1.0), (
        "B->C is the only observed transition out of B")


def test_trajectory_from_route_start_still_works(token, seeded):
    status, body = _http(
        "GET", "/api/intelligence/trajectory/predict"
               f"?identity_id={seeded['walker']}&current_camera={CAM.format('ta')}",
        token=token)
    assert status == 200, body
    predicted = {p["camera_id"] for p in body["predictions"]}
    assert CAM.format("tb") in predicted


# ---------------------------------------------------------------------------
# Correlation — two-pointer sweep equals brute force on the seed
# ---------------------------------------------------------------------------

def test_correlation_matches_brute_force(seeded):
    """The O(A+B) sweep must produce exactly the pairs the O(A*B) definition
    produces: B strictly after A, within the time window, on a nearby camera."""
    result = {}

    async def _run():
        from db_connection import db_manager
        from backend.core.activity_correlation import activity_correlation_analyzer
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            score, sequences, meta = await activity_correlation_analyzer.calculate_correlation(
                db, seeded["corr_a"], seeded["corr_b"], days_back=30)
            rows_a = (await db.execute(sa_text(
                "SELECT start_time FROM identity_appearances WHERE identity_id = :i ORDER BY start_time"),
                {"i": seeded["corr_a"]})).scalars().all()
            rows_b = (await db.execute(sa_text(
                "SELECT start_time FROM identity_appearances WHERE identity_id = :i ORDER BY start_time"),
                {"i": seeded["corr_b"]})).scalars().all()
        result.update(score=score, sequences=sequences, meta=meta,
                      rows_a=rows_a, rows_b=rows_b)
    run_async(_run())

    # Brute force over every (a, b) pair. Cameras a<->b are ~100 m apart —
    # inside the 500 m nearby threshold; window is the configured 10 minutes.
    from config import settings
    window = timedelta(minutes=getattr(settings, "MULTI_CAMERA_TIME_WINDOW_MINUTES", 10))
    expected = set()
    for a_time in result["rows_a"]:
        for b_time in result["rows_b"]:
            if a_time < b_time < a_time + window:
                expected.add((a_time, b_time))

    actual = {(s["from_time"], s["to_time"]) for s in result["sequences"]}
    assert actual == expected, f"sweep={sorted(actual)} brute={sorted(expected)}"
    assert len(expected) == 3, "seed defines exactly 3 A-then-B sequences"
    assert result["meta"]["truncated"] is False
    # Full participation, one modal camera pair, zero travel-time variance:
    # the documented formula gives participation 6/7 * (0.7 + 0.3*1.0).
    participation = (3 + 3) / (len(result["rows_a"]) + len(result["rows_b"]))
    assert result["score"] == pytest.approx(participation, abs=0.01)


def test_correlation_endpoint_reports_truncation_flag(token, seeded):
    status, body = _http(
        "GET", "/api/intelligence/correlation/calculate"
               f"?identity_a={seeded['corr_a']}&identity_b={seeded['corr_b']}",
        token=token)
    assert status == 200, body
    assert body["truncated"] is False
    assert body["correlation_score"] > 0
    assert body["insufficient_evidence"] is False
    assert "does not prove causation" in body["note"]


# ---------------------------------------------------------------------------
# Threat assessment — versioned, real counts
# ---------------------------------------------------------------------------

def test_threat_carries_algorithm_version(token, seeded):
    status, body = _http(
        "GET", f"/api/security/threat/{seeded['day_regular']}", token=token, timeout=120)
    assert status == 200, body
    assert body["algorithm_version"] == "risk-engine-v1"
    assert 0 <= body["overall_risk_score"] <= 100
    assert isinstance(body["risk_factors"], list) and body["risk_factors"]
    for factor in body["risk_factors"]:
        assert "factor" in factor and "score" in factor and "description" in factor


def test_threat_activity_factor_reads_real_count():
    """The identities row seeds appearances_count=0 while 11 appearance rows
    exist (live drift: joey 34 vs 3). The activity factor must COUNT rows,
    never read the drifted counter."""
    src = _read(SERVICE_PATH)
    body = src.split("async def assess_threat", 1)[1].split("async def _calculate_relationships", 1)[0]
    assert "func.count(IdentityAppearance.id)" in body, "activity factor must COUNT appearance rows"
    assert "identity.appearances_count" not in body, "the drifted counter is back in the threat rubric"


def test_threat_reports_insufficient_baseline_honestly(token, seeded):
    """day_regular's whole history is younger than the threat rubric's 30-day
    anomaly window, so its anomaly baseline is empty. The assessment must say
    so via a zero-scored factor — not imply 'no anomalies'."""
    status, body = _http(
        "GET", f"/api/security/threat/{seeded['day_regular']}", token=token, timeout=120)
    assert status == 200
    honesty = [f for f in body["risk_factors"]
               if "Insufficient baseline" in (f.get("description") or "")]
    assert honesty, f"expected the zero-scored baseline-honesty factor: {body['risk_factors']}"
    assert honesty[0]["score"] == 0.0


# ---------------------------------------------------------------------------
# Loitering / map analytics — correct cluster walk (was a busy-loop)
# ---------------------------------------------------------------------------

def _mov(lat, lng, minutes, base=None):
    base = base or datetime(2026, 7, 30, 12, 0)
    return {"coordinates": {"lat": lat, "lng": lng},
            "timestamp": (base + timedelta(minutes=minutes)).isoformat()}


def test_loitering_emits_one_pattern_per_dwell_cluster():
    """Two separate dwell spots => two patterns with real durations. The old
    while-loop never advanced past a valid point (bounded busy-loop) and its
    emit block sat outside the loop, so at most one pattern ever emerged."""
    from backend.core.security_analysis import SecurityMapAnalyzer
    track = (
        [_mov(33.5000, 44.4000 + i * 0.00001, i) for i in range(8)] +      # dwell A ~7 min
        [_mov(33.5200, 44.4200, 10)] +                                     # transit
        [_mov(33.5400, 44.4400 + i * 0.00001, 12 + i) for i in range(9)]   # dwell B ~8 min
    )
    patterns = SecurityMapAnalyzer.detect_loitering(track, radius_meters=50, min_duration_minutes=5)
    assert len(patterns) == 2, f"one pattern per dwell cluster, got {len(patterns)}"
    durations = sorted(
        int((p.end_time - p.start_time).total_seconds() / 60) for p in patterns)
    assert durations == [7, 8], f"durations must be real minutes: {durations}"


def test_loitering_survives_invalid_leading_rows():
    """All-invalid prefixes used to leave the start variables unbound
    (NameError swallowed by a blanket except -> silently zero patterns)."""
    from backend.core.security_analysis import SecurityMapAnalyzer
    junk = [{"coordinates": None}, {"nope": 1}, {"coordinates": {"lat": "x", "lng": "y"}}]
    track = junk + [_mov(33.5, 44.4 + i * 0.00001, i) for i in range(8)]
    patterns = SecurityMapAnalyzer.detect_loitering(track)
    assert len(patterns) == 1
    assert patterns[0].pattern_type == "loitering"


def test_backtracking_emits_once_per_revisit():
    from backend.core.security_analysis import SecurityMapAnalyzer
    # A -> B -> C -> A -> B -> C -> A : four revisits, the last A having TWO
    # prior A-visits (the old code emitted once per prior match: 5 total).
    spots = [(33.5000, 44.4000), (33.5200, 44.4200), (33.5400, 44.4400)]
    track = [_mov(*spots[i % 3], minutes=5 * i) for i in range(7)]
    patterns = SecurityMapAnalyzer.detect_backtracking(track)
    assert len(patterns) == 4, f"one emission per revisit, got {len(patterns)}"


def test_point_in_polygon_ignores_junk_vertices():
    """A junk vertex mid-list used to leave the previous-vertex pointer
    stale, testing phantom edges and inverting containment."""
    from backend.core.security_analysis import SecurityMapAnalyzer
    square = [[33.0, 44.0], ["bad"], [33.0, 45.0], [34.0, 45.0], [34.0, 44.0]]
    assert SecurityMapAnalyzer._point_in_polygon(33.5, 44.5, square) is True
    assert SecurityMapAnalyzer._point_in_polygon(35.0, 44.5, square) is False


def test_loitering_reports_real_duration_not_severity():
    """A loitering pattern must carry the MEASURED span in minutes.

    The Folium popup that this used to inspect printed `severity` (a 1-10
    score) under a "Duration:" label. The renderer is gone; the number is now
    computed in the analysis module and travels as data, so assert it there.
    """
    src = _read("/app/backend/core/security_analysis.py")
    assert "duration_minutes = (end_time - anchor_time).total_seconds() / 60" in src, (
        "loitering duration must be measured from the timestamps")
    assert "duration_minutes" in src and "severity" in src, "both fields exist and are distinct"

    from backend.core.security_analysis import SecurityMapAnalyzer
    import inspect
    body = inspect.getsource(SecurityMapAnalyzer.detect_loitering)
    assert "duration_minutes=" in body or "duration_minutes" in body


# ---------------------------------------------------------------------------
# Operational envelope: timeouts, metrics, single-flight, versions
# ---------------------------------------------------------------------------

def test_timeout_wrapper_present_on_heavy_endpoints():
    src = _read(ROUTES_PATH)
    # The bound comes from INTEL_QUERY_TIMEOUT_SECONDS, read per call — it used
    # to be a module-level literal that no operator could change.
    assert "INTEL_QUERY_TIMEOUT_SECONDS" in src
    assert "asyncio.wait_for" in src
    # Helper definition + five wrapped call sites, each labelled with its
    # feature name for the metrics series.
    flat = " ".join(src.split())
    for feature in ("network", "patterns", "anomalies", "threat", "correlation"):
        assert f'_bounded_intel_call( "{feature}"' in flat, f"heavy endpoint {feature} not wrapped"
    assert src.count("_bounded_intel_call(") >= 6


def test_intel_metrics_exposed(token, seeded):
    # Touch one instrumented endpoint, then look for the series.
    _http("GET", "/api/security/patterns?days_back=7", token=token, timeout=120)
    req = urllib.request.Request(BASE + "/metrics")
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode()
    assert "fr_intel_requests_total" in text, "intel request counter missing from /metrics"
    assert "fr_intel_duration_seconds" in text, "intel duration histogram missing from /metrics"
    assert 'feature="patterns"' in text


def test_legacy_threshold_learn_releases_single_flight(token):
    """The sync endpoint now holds the SAME guard as the job path. Two
    sequential calls must both succeed — a leaked guard would 409 forever."""
    headers = {"X-Requested-With": "XMLHttpRequest"}
    status1, body1 = _http("POST", "/api/intelligence/thresholds/learn",
                           body={}, token=token, headers=headers, timeout=120)
    if status1 == 409:
        pytest.skip(f"a real threshold job is running: {body1}")
    assert status1 == 200, body1
    status2, body2 = _http("POST", "/api/intelligence/thresholds/learn",
                           body={}, token=token, headers=headers, timeout=120)
    assert status2 == 200, f"guard not released after sync completion: {body2}"


def test_legacy_threshold_guard_in_source():
    src = _read(ROUTES_PATH)
    section = src.split("DEPRECATED synchronous variant", 1)[1].split("async def _run_threshold_job", 1)[0]
    assert "_try_acquire_threshold_job" in section, "sync endpoint must hold the single-flight guard"
    assert "_release_threshold_job" in section
    assert "finally:" in section, "the guard must be released on every path"


def test_capabilities_report_algorithm_versions(token):
    status, body = _http("GET", "/api/security/capabilities", token=token)
    assert status == 200, body
    caps = body["capabilities"]
    assert caps["anomaly_detection"]["algorithm_version"] == "anomaly-context-v3"
    assert caps["pattern_detection"]["algorithm_version"] == "patterns-v2"
    assert caps["threat_assessment"]["algorithm_version"] == "risk-engine-v1"
    assert caps["network_analysis"]["risk_score_version"]
    assert caps["trajectory_prediction"]["model_version"] == "trajectory-v2"


def test_network_reports_risk_rubric_version(token):
    status, body = _http("GET", "/api/security/network?days_back=7", token=token, timeout=120)
    assert status == 200, body
    assert body["risk_score_version"], "the network response must label its risk rubric"


def test_threshold_confidence_is_dispersion_based():
    """samples/50 was a sample count dressed up as a confidence. The v2 number
    must rank a tight small sample above a scattered large one."""
    src = _read("/app/backend/core/threshold_learner.py")
    assert "samples / 50" not in src and "/ 50.0" not in src, "the old samples/50 confidence is back"
    assert "spread_minutes" in src and "p95_minutes" in src
    assert "dispersion_term" in src and "sample_term" in src


def test_new_tunables_registered():
    from backend.core.runtime_settings import SETTINGS_REGISTRY
    for key in ("ANOMALY_DEVIATION_SIGMA", "ANOMALY_MIN_BASELINE_SAMPLES",
                "PATTERN_SCAN_LIMIT", "PATTERN_MAX_PER_TYPE",
                "PATTERN_OFF_HOURS_START", "PATTERN_OFF_HOURS_END",
                "PATTERN_RAPID_MIN_SPEED_KMH", "NETWORK_FALLBACK_MAX_IDENTITIES",
                "MULTI_CAMERA_DISTANCE_METERS", "RELATED_IDENTITY_TIME_WINDOW_MINUTES"):
        assert key in SETTINGS_REGISTRY, f"{key} missing from SETTINGS_REGISTRY"
        assert SETTINGS_REGISTRY[key].apply_mode in ("immediate", "next_request"), key


# ---------------------------------------------------------------------------
# Regression pins for the adversarial-review findings
# ---------------------------------------------------------------------------

def test_loitering_gap_guard_splits_separate_visits():
    """Same lobby camera at 09:00/09:02 today and 08:55 tomorrow used to read
    as ONE 1435-minute severity-10 dwell (coordinates are the CAMERA's fixed
    lat/lng, and the walk had no time-gap guard)."""
    from backend.core.security_analysis import SecurityMapAnalyzer
    track = [
        _mov(33.5, 44.4, 0),        # day 1, 09:00-equivalent
        _mov(33.5, 44.4, 2),        # +2 min — same visit
        _mov(33.5, 44.4, 1435),     # next day — a NEW visit, not a 24h dwell
    ]
    patterns = SecurityMapAnalyzer.detect_loitering(track, radius_meters=50, min_duration_minutes=5)
    assert patterns == [], (
        f"a 2-minute visit repeated next day is not a day-long dwell: "
        f"{[(p.description, p.severity) for p in patterns]}")
    # A genuine continuous dwell still emits.
    dwell = [_mov(33.5, 44.4 + i * 0.00001, i) for i in range(8)]
    assert len(SecurityMapAnalyzer.detect_loitering(dwell)) == 1


def test_backtracking_pingpong_still_detected():
    """A,B,A,B,A oscillation ('casing'): the most recent same-spot visit is
    only 2 stops back (non-qualifying), but the older qualifying visit must
    still be consulted — a naive dedup break silenced the track entirely."""
    from backend.core.security_analysis import SecurityMapAnalyzer
    a, b = (33.5000, 44.4000), (33.5030, 44.4000)  # ~330 m apart
    track = [_mov(*(a if i % 2 == 0 else b), minutes=5 * i) for i in range(5)]
    patterns = SecurityMapAnalyzer.detect_backtracking(track)
    assert len(patterns) == 1, f"the A@4 return (gap 4 to A@0) must emit: {len(patterns)}"
    assert "4 stops earlier" in patterns[0].description
    # A pure dwell (consecutive same-spot rows) is NOT backtracking.
    dwell = [_mov(33.5, 44.4, 5 * i) for i in range(5)]
    assert SecurityMapAnalyzer.detect_backtracking(dwell) == []


def test_isolated_nodes_reach_the_api(token, seeded):
    """The service computes isolated = requested ids with no edges, but the
    route used to intersect that with edge-derived kept_ids — two provably
    disjoint sets — so the response always said []."""
    walker = seeded["walker"]
    status, body = _http(
        "GET", f"/api/security/network?identity_ids={walker}&days_back=7",
        token=token, timeout=120)
    assert status == 200, body
    assert walker in body["isolated_nodes"], (
        f"an explicitly requested identity with no co-appearances must be "
        f"reported isolated: {body['isolated_nodes']}")


def test_pattern_cap_keeps_newest_and_reports_truncation(seeded):
    """With the per-type cap forced to 1, the survivor of each type must be
    the NEWEST of that type and the report must say truncated — the in-
    detector caps used to keep the OLDEST items and stay invisible."""
    from config import settings as app_settings

    async def _detect():
        from db_connection import db_manager
        from backend.core.security_intelligence_service import security_intelligence_service
        await _ensure_db()
        async with db_manager.get_session() as db:
            return await security_intelligence_service.detect_suspicious_patterns(
                db, days_back=7, min_group_size=3)

    full = run_async(_detect())
    newest_by_type = {}
    for p in full.patterns:
        cur = newest_by_type.get(p.pattern_type)
        if cur is None or p.first_detected > cur.first_detected:
            newest_by_type[p.pattern_type] = p
    multi_types = {t for t in newest_by_type
                   if sum(1 for p in full.patterns if p.pattern_type == t) > 1}

    original = app_settings.PATTERN_MAX_PER_TYPE
    app_settings.PATTERN_MAX_PER_TYPE = 1
    try:
        capped = run_async(_detect())
    finally:
        app_settings.PATTERN_MAX_PER_TYPE = original

    per_type = {}
    for p in capped.patterns:
        per_type.setdefault(p.pattern_type, []).append(p)
    for ptype, plist in per_type.items():
        assert len(plist) == 1, f"cap=1 violated for {ptype}"
        assert plist[0].first_detected == newest_by_type[ptype].first_detected, (
            f"{ptype}: the cap must keep the NEWEST pattern, not the oldest")
    if multi_types:
        assert capped.truncated is True, (
            "clipping by the per-type cap must be visible in the truncated flag")


def test_zero_std_floor_does_not_crash(seeded):
    """ANOMALY_MIN_STD_HOURS=0 is a registry-sanctioned live value; against a
    zero-variance baseline the old ratio divided by zero and both the
    anomalies AND threat endpoints 500ed."""
    from config import settings as app_settings

    async def _detect():
        from db_connection import db_manager
        from backend.core.security_intelligence_service import security_intelligence_service
        await _ensure_db()
        async with db_manager.get_session() as db:
            return await security_intelligence_service.detect_anomalies(
                db, seeded["day_regular"], days_back=7)

    original = app_settings.ANOMALY_MIN_STD_HOURS
    app_settings.ANOMALY_MIN_STD_HOURS = 0.0
    try:
        report = run_async(_detect())   # must not raise
    finally:
        app_settings.ANOMALY_MIN_STD_HOURS = original
    off = [a for a in report.anomalies if a.anomaly_type == "off_schedule"]
    assert off, "the 03:00 outlier must still be flagged with the floor at 0"
    for a in off:
        assert 0 <= a.risk_score <= 90

    src = _read("/app/backend/core/time_context.py")
    assert 'if threshold > 0 else float("inf")' in src, (
        "the zero-threshold division guard is gone")


def test_capped_queries_are_deterministic_in_source():
    """Every LIMIT that bounds an analysis scan must pair with an ORDER BY
    (newest first) — LIMIT without ORDER BY returns planner-dependent rows."""
    intel = _read("/app/backend/core/intelligence_service.py")
    for chunk in intel.split(".limit(1000)")[:-1]:
        assert chunk.rstrip().endswith("start_time.desc())"), (
            "a capped co-appearance query lost its ORDER BY")
    traj = _read("/app/backend/core/trajectory_predictor.py")
    assert "order_by(IdentityAppearance.start_time.desc()).limit(TRAJECTORY_MAX_APPEARANCES)" in traj, (
        "the trajectory history cap must keep the NEWEST rows")
    svc = _read(SERVICE_PATH)
    assert ".order_by(IdentityAppearance.start_time.desc())\n            .limit(recent_limit)" in svc or \
           "start_time.desc())\n            .limit(recent_limit)" in svc, (
        "the anomaly recent-window cap must keep the NEWEST rows")


def test_threat_fallback_requires_minimum_co_appearances():
    src = _read(SERVICE_PATH)
    body = src.split("async def assess_threat", 1)[1].split("async def _calculate_relationships", 1)[0]
    assert "having(func.count() >= min_co)" in body, (
        "the live connection fallback must apply the same per-partner minimum "
        "as the relationship cache — otherwise one-off passersby inflate the threat band")


def test_off_hours_window_documented_as_inclusive():
    cfg = _read("/app/config.py")
    assert "INCLUSIVE whole hour" in cfg, "PATTERN_OFF_HOURS_END doc must match the inclusive code"
    src = _read(SERVICE_PATH)
    assert ':00-{hour_end:02d}:59' in src, "emitted window labels must show the real inclusive span"


def test_frontend_truncation_note_survives_empty_results():
    js = _read("/app/frontend/js/admin-security-intelligence.js")
    empty_branch = js.split("function renderPatterns", 1)[1].split("const cards", 1)[0]
    assert "buildTruncationNote" in empty_branch, (
        "a truncated scan with zero patterns must still show the partial-scan note")


def test_frontend_stops_polling_untracked_jobs():
    js = _read("/app/frontend/js/admin-security-intelligence.js")
    poll = js.split("async function pollThresholdJob", 1)[1].split("async function learnThresholds", 1)[0]
    assert "err.status === 404" in poll, (
        "a 404 from the job-status endpoint must stop the poll with a clear message")


def test_job_status_covers_sync_guard_ids():
    src = _read(ROUTES_PATH)
    section = src.split("async def get_threshold_job", 1)[1].split("@router.get", 1)[0]
    assert "_threshold_job_running()" in section and '"synthetic": True' in section, (
        "polling the sync guard's job id must report 'running', not 404")
