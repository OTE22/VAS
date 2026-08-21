"""Suspicious-pattern detectors: the cases the happy-path tests do not cover.

Run explicitly:

    docker exec -w /app <api> python -m pytest tests/audit_suspicious_patterns.py -v

tests/test_intel_algorithms.py proves each detector FIRES on a textbook case
with the derived confidence. Correctness also means NOT firing: a detector
that flags nearby cameras, two-person "groups" or daytime activity is worse
than none, because every false alarm costs an operator's attention. And the
off-hours rule is defined in LOCAL camera time, which nothing exercised.

Every scenario seeds its own rows under its own camera ids, so other data in
the database cannot leak into a bucket, and the module removes them.
"""
import uuid as uuid_module
from datetime import datetime, timedelta

import pytest

from conftest import run_on_shared_loop as run_async

PREFIX = "qa_patt_"
CAM = "qa-patt-{}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sql(statement, params=None, fetch="none"):
    from sqlalchemy import text
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            result = await db.execute(text(statement), params or {})
            if fetch == "scalar":
                return result.scalar()
            if fetch == "all":
                return [dict(r._mapping) for r in result]
            await db.commit()
            return None
    return run_async(_run())


def _camera(name, lat=None, lng=None, timezone=None):
    cam = CAM.format(name) + "-" + uuid_module.uuid4().hex[:6]
    _sql("""INSERT INTO pipelines (pipeline_id, created_at, is_active, latitude, longitude, timezone)
            VALUES (:p, now(), 1, :lat, :lng, :tz) ON CONFLICT (pipeline_id) DO NOTHING""",
         {"p": cam, "lat": lat, "lng": lng, "tz": timezone})
    return cam


def _identity(label):
    return _sql("""INSERT INTO identities (id, type, status, display_name, first_seen_at,
                        last_seen_at, created_at, updated_at, appearances_count)
                   VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, now(), now(), now(), now(), 0)
                   RETURNING id::text""",
                {"n": PREFIX + label + "-" + uuid_module.uuid4().hex[:6]}, fetch="scalar")


def _appear(identity_id, cam, ts):
    _sql("""INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at)
            VALUES (CAST(:i AS uuid), :p, :ts, now())""", {"i": identity_id, "p": cam, "ts": ts})


def _detect(days_back=30, min_group_size=3):
    """The real service, on the real database."""
    async def _run():
        from db_connection import db_manager
        from backend.core.security_intelligence_service import security_intelligence_service
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            report = await security_intelligence_service.detect_suspicious_patterns(
                db, days_back=days_back, min_group_size=min_group_size)
            return report.patterns
    return run_async(_run())


def _for(patterns, ptype, identity_id):
    return [p for p in patterns if p.pattern_type == ptype and identity_id in p.identities_involved]


def _cleanup():
    _sql("DELETE FROM identity_appearances WHERE pipeline_id LIKE 'qa-patt-%'")
    _sql("DELETE FROM identity_appearances WHERE identity_id IN "
         "(SELECT id FROM identities WHERE display_name LIKE :p)", {"p": PREFIX + "%"})
    _sql("DELETE FROM identities WHERE display_name LIKE :p", {"p": PREFIX + "%"})
    _sql("DELETE FROM pipelines WHERE pipeline_id LIKE 'qa-patt-%'")


@pytest.fixture(scope="module", autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


NOW = datetime.utcnow().replace(second=0, microsecond=0)
# A quiet afternoon hour, far from every off-hours window used below.
DAY = (NOW - timedelta(days=2)).replace(hour=14, minute=10)


# ===========================================================================
# Rapid movement — must NOT fire for a normal walk
# ===========================================================================

def test_nearby_cameras_do_not_trigger_rapid_movement():
    """Two cameras 5 m apart, 30 s apart: walking speed -> not suspicious."""
    a = _camera("near-a", 33.5000, 44.4000)
    b = _camera("near-b", 33.50004, 44.4000)      # ~4.5 m north
    person = _identity("walker")
    _appear(person, a, DAY)
    _appear(person, b, DAY + timedelta(seconds=30))

    hits = _for(_detect(), "rapid_movement", person)
    assert not hits, (
        f"a 5 m camera hop in 30 s was flagged as rapid movement "
        f"(implied speed ~0.5 km/h): {[h.evidence for h in hits]}")


def test_same_camera_reappearance_is_not_rapid_movement():
    a = _camera("same", 33.5, 44.4)
    person = _identity("lingerer")
    _appear(person, a, DAY)
    _appear(person, a, DAY + timedelta(seconds=10))
    assert not _for(_detect(), "rapid_movement", person)


def test_without_coordinates_the_time_rule_alone_fires():
    """Documented fallback: no coordinates -> any sub-window camera hop fires.
    Pinned so the degraded behaviour is a known, visible limitation."""
    a = _camera("nocoord-a")
    b = _camera("nocoord-b")
    person = _identity("hopper")
    _appear(person, a, DAY)
    _appear(person, b, DAY + timedelta(seconds=30))

    hits = _for(_detect(), "rapid_movement", person)
    assert hits, "time-only fallback should fire when cameras have no coordinates"
    assert hits[0].evidence.get("implied_speed_kmh") is None


def test_far_cameras_at_implausible_speed_fire():
    a = _camera("far-a", 33.6000, 44.5000)
    b = _camera("far-b", 33.6090, 44.5000)        # ~1 km
    person = _identity("sprinter")
    _appear(person, a, DAY)
    _appear(person, b, DAY + timedelta(seconds=60))
    hits = _for(_detect(), "rapid_movement", person)
    assert hits and 40 <= hits[0].evidence["implied_speed_kmh"] <= 80, hits


# ===========================================================================
# Group activity — thresholds, severity, recurrence, bucket edges
# ===========================================================================

def test_group_below_threshold_is_not_a_group():
    cam = _camera("duo")
    people = [_identity(f"duo-{i}") for i in range(2)]
    for i, p in enumerate(people):
        _appear(p, cam, DAY + timedelta(seconds=10 * i))
    hits = [p for p in _detect(min_group_size=3)
            if p.pattern_type == "group_activity" and set(people) & set(p.identities_involved)]
    assert not hits, "two people are not a group when min_group_size is 3"


def test_group_of_five_is_high_severity():
    cam = _camera("five")
    people = [_identity(f"five-{i}") for i in range(5)]
    for i, p in enumerate(people):
        _appear(p, cam, DAY + timedelta(seconds=10 * i))
    hits = [p for p in _detect() if p.pattern_type == "group_activity"
            and set(people).issubset(set(p.identities_involved))]
    assert hits and hits[0].severity == "high", hits


def test_recurring_group_earns_the_recurrence_bonus():
    """Same trio in two different buckets: 0.25 + 0.25 + 0.25 = 0.75."""
    cam = _camera("recur")
    trio = [_identity(f"recur-{i}") for i in range(3)]
    for bucket in (DAY, DAY + timedelta(hours=1)):
        for i, p in enumerate(trio):
            _appear(p, cam, bucket + timedelta(seconds=5 * i))
    hits = [p for p in _detect() if p.pattern_type == "group_activity"
            and set(trio) == set(p.identities_involved)]
    assert len(hits) == 2, f"expected one pattern per bucket, got {len(hits)}"
    assert all(h.evidence["group_recurrence"] == 2 for h in hits)
    assert all(h.confidence == pytest.approx(0.75) for h in hits), [h.confidence for h in hits]


def test_group_straddling_a_clock_boundary_is_caught():
    """v2 used fixed 5-minute clock buckets: three people at 14:09:50 /
    14:10:10 / 14:10:20 were split 1+2 across buckets and never reported.
    v3 slides the window from each sighting, so "within five minutes of each
    other" is judged against the sightings, not the clock."""
    cam = _camera("edge")
    trio = [_identity(f"edge-{i}") for i in range(3)]
    boundary = DAY.replace(minute=10)              # a multiple of 5
    _appear(trio[0], cam, boundary - timedelta(seconds=10))
    _appear(trio[1], cam, boundary + timedelta(seconds=10))
    _appear(trio[2], cam, boundary + timedelta(seconds=20))
    hits = [p for p in _detect() if p.pattern_type == "group_activity"
            and set(trio).issubset(set(p.identities_involved))]
    assert len(hits) == 1, f"a group straddling a clock boundary must be reported once, got {len(hits)}"
    assert hits[0].evidence["window_minutes"] == 5


def test_people_more_than_five_minutes_apart_are_not_a_group():
    """The sliding window must not over-reach: A at :00, B at :04, C at :08
    never share a 5-minute window, even though each is within 5 min of the
    next (no gap-chaining)."""
    cam = _camera("chain")
    trio = [_identity(f"chain-{i}") for i in range(3)]
    for i, p in enumerate(trio):
        _appear(p, cam, DAY + timedelta(minutes=4 * i))
    hits = [p for p in _detect() if p.pattern_type == "group_activity"
            and set(trio).issubset(set(p.identities_involved))]
    assert not hits, "A/B/C spanning 8 minutes were chained into one group"


def test_a_departing_member_does_not_spawn_a_second_pattern():
    """{A,B,C,D} together, then A leaves and {B,C,D} linger: ONE occurrence,
    not a 4-group followed by a spurious 3-group subset."""
    cam = _camera("depart")
    four = [_identity(f"depart-{i}") for i in range(4)]
    for i, p in enumerate(four):
        _appear(p, cam, DAY + timedelta(seconds=10 * i))
    for p in four[1:]:                                   # B, C, D again 3 min later
        _appear(p, cam, DAY + timedelta(minutes=3))
    hits = [p for p in _detect() if p.pattern_type == "group_activity"
            and set(p.identities_involved) <= set(four)]
    assert len(hits) == 1, [h.identities_involved for h in hits]
    assert set(hits[0].identities_involved) == set(four)


def test_same_group_hours_apart_counts_as_two_occurrences():
    cam = _camera("twice")
    trio = [_identity(f"twice-{i}") for i in range(3)]
    for when in (DAY, DAY + timedelta(hours=3)):
        for i, p in enumerate(trio):
            _appear(p, cam, when + timedelta(seconds=15 * i))
    hits = [p for p in _detect() if p.pattern_type == "group_activity"
            and set(trio) == set(p.identities_involved)]
    assert len(hits) == 2
    assert all(h.evidence["group_recurrence"] == 2 for h in hits)


# ===========================================================================
# Unusual timing — local time, inclusivity, wrap-around, minimum count
# ===========================================================================

def test_two_off_hours_sightings_are_not_enough():
    cam = _camera("two-night")
    person = _identity("two-night")
    night = (NOW - timedelta(days=1)).replace(hour=3, minute=0)
    _appear(person, cam, night)
    _appear(person, cam, night + timedelta(minutes=20))
    assert not _for(_detect(), "unusual_timing", person), "min_occurrences is 3"


def test_off_hours_is_judged_in_the_cameras_local_time():
    """03:00 UTC on a UTC+3 camera is 06:00 local -> NOT off-hours.
    00:00 UTC on that camera is 03:00 local -> off-hours."""
    cam = _camera("beirut", timezone="Asia/Beirut")        # UTC+3 in August
    commuter = _identity("commuter")                        # 06:00 local
    night = _identity("night-local")                        # 03:00 local
    for m in (0, 20, 40):
        _appear(commuter, cam, (NOW - timedelta(days=1)).replace(hour=3, minute=m))
        _appear(night, cam, (NOW - timedelta(days=1)).replace(hour=0, minute=m))

    patterns = _detect()
    assert not _for(patterns, "unusual_timing", commuter), (
        "03:00 UTC on an Asia/Beirut camera is 06:00 local and must not be off-hours")
    hit = _for(patterns, "unusual_timing", night)
    assert hit, "00:00 UTC on an Asia/Beirut camera is 03:00 local and must be off-hours"
    assert "Asia/Beirut" in hit[0].evidence["timezones"], hit[0].evidence


def test_camera_without_timezone_falls_back_to_the_site_default():
    from config import settings
    cam = _camera("no-tz")                                  # timezone NULL
    person = _identity("utc-night")
    for m in (0, 20, 40):
        _appear(person, cam, (NOW - timedelta(days=1)).replace(hour=3, minute=m))
    hit = _for(_detect(), "unusual_timing", person)
    assert hit, "with DEFAULT_SITE_TIMEZONE=UTC, 03:00 UTC must be off-hours"
    assert str(settings.DEFAULT_SITE_TIMEZONE) in hit[0].evidence["timezones"]


def test_window_bounds_are_inclusive_and_05_59_counts():
    cam = _camera("edge-hour")
    person = _identity("five-fifty-nine")
    for d in (1, 2, 3):
        _appear(person, cam, (NOW - timedelta(days=d)).replace(hour=5, minute=59))
    assert _for(_detect(), "unusual_timing", person), "05:59 is inside 02:00-05:59"

    late = _identity("six-oclock")
    for d in (1, 2, 3):
        _appear(late, cam, (NOW - timedelta(days=d)).replace(hour=6, minute=0))
    assert not _for(_detect(), "unusual_timing", late), "06:00 is outside the window"


def test_midnight_wrapping_window_is_honoured():
    """22:00-04:59 configured: 23:30 must count, 12:00 must not."""
    from config import settings
    from backend.core import runtime_settings
    prev_start, prev_end = settings.PATTERN_OFF_HOURS_START, settings.PATTERN_OFF_HOURS_END
    assert runtime_settings.apply_to_runtime("PATTERN_OFF_HOURS_START", 22)
    assert runtime_settings.apply_to_runtime("PATTERN_OFF_HOURS_END", 4)
    try:
        cam = _camera("wrap")
        late = _identity("late-night")
        noon = _identity("noon")
        for d in (1, 2, 3):
            _appear(late, cam, (NOW - timedelta(days=d)).replace(hour=23, minute=30))
            _appear(noon, cam, (NOW - timedelta(days=d)).replace(hour=12, minute=0))
        patterns = _detect()
        assert _for(patterns, "unusual_timing", late), "23:30 is inside 22:00-04:59"
        assert not _for(patterns, "unusual_timing", noon), "12:00 is outside 22:00-04:59"
    finally:
        runtime_settings.apply_to_runtime("PATTERN_OFF_HOURS_START", prev_start)
        runtime_settings.apply_to_runtime("PATTERN_OFF_HOURS_END", prev_end)


def test_days_back_excludes_older_activity():
    cam = _camera("old")
    person = _identity("ancient")
    for m in (0, 20, 40):
        _appear(person, cam, (NOW - timedelta(days=45)).replace(hour=3, minute=m))
    assert not _for(_detect(days_back=30), "unusual_timing", person)
    assert _for(_detect(days_back=60), "unusual_timing", person)


# ===========================================================================
# Per-camera scope
# ===========================================================================

def _detect_scoped(pipeline_id, days_back=30, min_group_size=3):
    async def _run():
        from db_connection import db_manager
        from backend.core.security_intelligence_service import security_intelligence_service
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            return await security_intelligence_service.detect_suspicious_patterns(
                db, days_back=days_back, min_group_size=min_group_size,
                pipeline_id=pipeline_id)
    return run_async(_run())


def test_camera_scope_keeps_only_that_cameras_patterns():
    cam_a = _camera("scope-a")
    cam_b = _camera("scope-b")
    trio_a = [_identity(f"scope-a-{i}") for i in range(3)]
    trio_b = [_identity(f"scope-b-{i}") for i in range(3)]
    for i, p in enumerate(trio_a):
        _appear(p, cam_a, DAY + timedelta(seconds=10 * i))
    for i, p in enumerate(trio_b):
        _appear(p, cam_b, DAY + timedelta(seconds=10 * i))

    report = _detect_scoped(cam_a)
    groups = [p for p in report.patterns if p.pattern_type == "group_activity"]
    assert any(set(trio_a) == set(p.identities_involved) for p in groups), "camera A's group missing"
    assert not any(set(trio_b) & set(p.identities_involved) for p in groups), (
        "camera B's group leaked into a scan scoped to camera A")
    assert report.pipeline_id == cam_a
    assert report.scope_note and "rapid movement" in report.scope_note.lower()


def test_camera_scope_cannot_produce_rapid_movement():
    """Two cameras are needed to move between; a one-camera scan is told so."""
    a = _camera("scope-far-a", 33.6000, 44.5000)
    b = _camera("scope-far-b", 33.6090, 44.5000)
    person = _identity("scope-sprinter")
    _appear(person, a, DAY)
    _appear(person, b, DAY + timedelta(seconds=60))
    assert _for(_detect(), "rapid_movement", person), "unscoped scan must still fire"
    assert not _for(_detect_scoped(a).patterns, "rapid_movement", person)


def test_scope_is_applied_before_the_scan_cap():
    """A quiet camera must not be starved by a busy one: the cap budgets the
    scoped camera alone. Proven structurally — the filter is in the SQL."""
    import inspect
    from backend.core.security_intelligence_service import SecurityIntelligenceService
    src = inspect.getsource(SecurityIntelligenceService.detect_suspicious_patterns)
    scan = src.split("scan_limit = int(settings.PATTERN_SCAN_LIMIT)", 1)[1]
    scan = scan.split(".limit(scan_limit)", 1)[0]
    assert "IdentityAppearance.pipeline_id == pipeline_id" in scan, (
        "the camera filter must be part of the capped query, not applied afterwards")


def test_unknown_pipeline_is_a_404_on_the_route():
    import json, urllib.request, urllib.error
    req = urllib.request.Request("http://localhost:8000/api/auth/login",
                                 data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    token = json.loads(urllib.request.urlopen(req, timeout=60).read())["access_token"]
    req = urllib.request.Request(
        "http://localhost:8000/api/security/patterns?days_back=7&pipeline_id=no-such-camera-xyz",
        headers={"Authorization": f"Bearer {token}"})
    try:
        urllib.request.urlopen(req, timeout=60)
        status = 200
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 404, status
