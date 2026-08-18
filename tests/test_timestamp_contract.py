"""Every frontend-facing timestamp is timezone-aware.

    docker exec face_recognition_api python -m pytest tests/test_timestamp_contract.py -v

The dashboard printed `[TIME] Legacy naive timestamp received — interpreting as
UTC` on every page load: the WebSocket `initial_data` path and several REST
producers emitted bare `.isoformat()` (no `Z`, no offset) while the realtime
`new_detection` path already appended `Z`. Two conventions, one wire.

A naive string is not merely untidy — JavaScript parses a zoneless date-time as
LOCAL time (ES spec), so every admin page using raw `new Date(value)` rendered
those timestamps shifted by the browser's UTC offset, and `admin-unknown.js`
sorted naive initial values against aware live values in one list.

These tests assert BEHAVIOUR — they read real HTTP and WebSocket payloads and
walk them RECURSIVELY, because a custom serializer can produce a naive string
that no source-level grep would catch. The source scans at the end are a
secondary guard, not the primary evidence.

Storage stays naive-UTC on purpose; see backend/utils/time_utils.py.
"""

import json
import os
import re
import urllib.error
import urllib.request
import uuid as uuid_module
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import run_on_shared_loop as run_async

BASE = "http://localhost:8000"

# The frontend's own gate, copied verbatim from dashboard.js:133. If a value
# fails this, parseLegacyNaiveUtcTimestamp() runs and the warning fires.
AWARE_RE = re.compile(r"([zZ]|[+-]\d{2}:?\d{2})$")

# A timestamp-ish JSON key. Deliberately broad: the point is to catch fields
# nobody remembered to list.
TIMESTAMP_KEY_RE = re.compile(
    r"(^|_)(timestamp|time|at|date)$|_at$|_time$|_timestamp$|^timestamp$", re.I)

# Values that look like a date-time but are NOT wire timestamps.
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


def _http(method, path, body=None, token=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw or b"{}")
            except Exception:
                return response.status, {"_raw": raw.decode(errors="replace")}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except Exception:
            return exc.code, {"_raw": raw.decode(errors="replace")}


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         {"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


# ---------------------------------------------------------------------------
# Recursive payload walker — nested fields count, not just top level
# ---------------------------------------------------------------------------

def collect_timestamp_strings(payload, path="$"):
    """Every (json_path, value) that looks like a wire timestamp.

    Walks dicts AND lists to any depth: `initial_data.data[3].faces[0]
    .last_seen_at` is exactly the kind of field that drifted naive while the
    envelope looked fine.
    """
    found = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{path}.{key}"
            if isinstance(value, str) and TIMESTAMP_KEY_RE.search(key):
                found.append((child, value))
            else:
                found.extend(collect_timestamp_strings(value, child))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(collect_timestamp_strings(value, f"{path}[{index}]"))
    return found


def assert_aware(json_path, value):
    """A wire timestamp must carry a zone; a date-only value must stay a date."""
    if DATE_ONLY_RE.match(value):
        # "2026-08-03" is a DATE. It has no instant and must not be promoted
        # to midnight UTC — JS parses date-only as UTC already, and inventing
        # a time would fabricate precision the server never had.
        return
    if not ISO_DATETIME_RE.match(value):
        return          # not a timestamp at all (ids, names, enum values)
    assert AWARE_RE.search(value), (
        f"{json_path} = {value!r} is naive — dashboard.js would route it "
        "through parseLegacyNaiveUtcTimestamp() and print the [TIME] warning")
    assert not value.endswith("+00:00Z"), (
        f"{json_path} = {value!r} has a double suffix (a '+ \"Z\"' site was "
        "handed an already-aware datetime)")


def assert_payload_aware(payload, label):
    checked = 0
    for json_path, value in collect_timestamp_strings(payload, label):
        assert_aware(json_path, value)
        checked += 1
    return checked


# ---------------------------------------------------------------------------
# iso_utc / utc_now units
# ---------------------------------------------------------------------------

def test_naive_is_treated_as_utc_with_microseconds():
    from backend.utils.time_utils import iso_utc

    assert iso_utc(datetime(2026, 1, 2, 3, 4, 5, 123456)) == \
        "2026-01-02T03:04:05.123456Z"


def test_aware_offset_is_converted_to_the_same_instant():
    from backend.utils.time_utils import iso_utc

    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    assert iso_utc(aware) == "2026-01-01T07:00:00Z"


def test_aware_conversion_crossing_a_date_boundary():
    """The case a `.replace(tzinfo=utc)` implementation gets wrong.

    01:30 at +05:30 is 20:00 the PREVIOUS day in UTC. Relabelling the
    wall-clock reading would report the wrong day; astimezone() converts the
    instant. The mirror case (23:00 at -05:00 -> 04:00 the NEXT day) is
    checked too, so a sign error cannot pass.
    """
    from backend.utils.time_utils import iso_utc

    east = datetime(2026, 3, 15, 1, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert iso_utc(east) == "2026-03-14T20:00:00Z", "date moved the wrong way"

    west = datetime(2026, 3, 15, 23, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert iso_utc(west) == "2026-03-16T04:00:00Z"


def test_aware_utc_input_has_no_double_suffix():
    from backend.utils.time_utils import iso_utc

    result = iso_utc(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))
    assert result == "2026-01-01T00:00:00Z"
    assert "+00:00" not in result


def test_reparsing_our_own_output_is_stable():
    """iso_utc(parse(iso_utc(x))) == iso_utc(x) — no duplicate offset or suffix.

    This is the round trip a client makes when it echoes a timestamp back, and
    the shape that produced "...+00:00Z" in the old `+ "Z"` implementations.
    """
    from backend.utils.time_utils import iso_utc

    for original in (datetime(2026, 6, 1, 10, 20, 30),                       # naive
                     datetime(2026, 6, 1, 10, 20, 30, tzinfo=timezone.utc),  # aware UTC
                     datetime(2026, 6, 1, 10, 20, 30,
                              tzinfo=timezone(timedelta(hours=-3)))):        # aware offset
        once = iso_utc(original)
        reparsed = datetime.fromisoformat(once.replace("Z", "+00:00"))
        twice = iso_utc(reparsed)
        assert twice == once, f"{original!r}: {once!r} -> {twice!r}"
        assert twice.count("Z") == 1 and "+00:00" not in twice


def test_none_passes_through():
    from backend.utils.time_utils import iso_utc

    assert iso_utc(None) is None


@pytest.mark.parametrize("bad", [
    date(2026, 8, 3),          # a DATE must stay a date string
    "2026-08-03T00:00:00Z",    # already serialized
    1754208000,                # epoch
    object(),
])
def test_non_datetime_input_is_refused(bad):
    """No silent stringification: a date would become a fabricated midnight,
    and a str would get double-wrapped."""
    from backend.utils.time_utils import iso_utc

    with pytest.raises(TypeError):
        iso_utc(bad)


def test_utc_now_is_aware_utc():
    from backend.utils.time_utils import utc_now

    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_helper_never_relabels_an_aware_datetime():
    """`.replace(tzinfo=utc)` on an aware value keeps the wall clock and moves
    the instant — the exact bug this helper must not contain."""
    import ast
    import inspect

    from backend.utils import time_utils

    source = inspect.getsource(time_utils.iso_utc)
    tree = ast.parse(source.lstrip())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "replace"
                and any(kw.arg == "tzinfo" for kw in node.keywords)):
            pytest.fail("iso_utc() calls .replace(tzinfo=...) — use astimezone()")


# ---------------------------------------------------------------------------
# Redis serializer catch-all
# ---------------------------------------------------------------------------

def test_redis_serializer_makes_datetimes_aware_and_leaves_dates_alone():
    from backend.core.websocket_manager import _serialize_for_redis

    assert _serialize_for_redis(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05Z"

    aware = datetime(2026, 1, 2, 5, 4, 5, tzinfo=timezone(timedelta(hours=2)))
    assert _serialize_for_redis(aware) == "2026-01-02T03:04:05Z"

    # A date is not an instant. datetime subclasses date, so the branch order
    # in _serialize_for_redis decides this.
    assert _serialize_for_redis(date(2026, 8, 3)) == "2026-08-03"


def test_redis_serializer_reaches_nested_datetimes():
    from backend.core.websocket_manager import _serialize_for_redis

    payload = {"data": [{"faces": [{"last_seen_at": datetime(2026, 1, 1, 0, 0)}]}]}
    serialized = _serialize_for_redis(payload)
    assert serialized["data"][0]["faces"][0]["last_seen_at"].endswith("Z")


# ---------------------------------------------------------------------------
# Live payloads — REST
# ---------------------------------------------------------------------------

def _sql(statement, params=None, fetch=None):
    from sqlalchemy import text

    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            result = await db.execute(text(statement), params or {})
            value = None
            if fetch == "scalar" and result.returns_rows:
                value = result.scalar()
            elif fetch == "all" and result.returns_rows:
                value = result.all()
            await db.commit()
            return value
    return run_async(_run())


@pytest.fixture(scope="module")
def seeded_detection():
    """A detection + face so /api/detections is not vacuously empty.

    The demo-data wipe left the detections table empty; asserting over an empty
    list would prove nothing.
    """
    pipeline_id = "pytest-ts-cam"
    _sql("INSERT INTO pipelines (pipeline_id, created_at, is_active) "
         "VALUES (:p, now(), 1) ON CONFLICT (pipeline_id) DO NOTHING",
         {"p": pipeline_id})
    detection_id = _sql(
        "INSERT INTO detections (uuid, pipeline_id, timestamp, processing_time_ms) "
        "VALUES (gen_random_uuid()::text, :p, now(), 12.5) RETURNING id",
        {"p": pipeline_id}, fetch="scalar")
    _sql("INSERT INTO faces (detection_id, name, similarity) "
         "VALUES (:d, :n, 0.99)",
         {"d": detection_id, "n": "pytest-ts-person"})
    yield {"pipeline_id": pipeline_id, "detection_id": detection_id}
    _sql("DELETE FROM faces WHERE detection_id = :d", {"d": detection_id})
    _sql("DELETE FROM detections WHERE id = :d", {"d": detection_id})
    _sql("DELETE FROM detections WHERE pipeline_id = :p", {"p": pipeline_id})
    _sql("DELETE FROM pipelines WHERE pipeline_id = :p", {"p": pipeline_id})


def test_dashboard_rest_payloads_are_fully_aware(token, seeded_detection):
    """Behavioural: walk the real responses the dashboard fetches."""
    pipeline_id = seeded_detection["pipeline_id"]
    endpoints = [
        "/api/dashboard/config",
        "/api/dashboard/pipelines",
        "/api/stats",
        "/api/detections?limit=50",
        f"/api/detections/{pipeline_id}?limit=50",
    ]
    total = 0
    for path in endpoints:
        status, body = _http("GET", path, token=token)
        assert status == 200, (path, status, body)
        total += assert_payload_aware(body, path)
    assert total >= 5, f"only {total} timestamps inspected — walker missed fields"


def test_seeded_detection_timestamp_is_aware(token, seeded_detection):
    """The specific field dashboard.js's REST fallback parses."""
    status, body = _http(
        "GET", f"/api/detections/{seeded_detection['pipeline_id']}?limit=50",
        token=token)
    assert status == 200, body
    rows = body if isinstance(body, list) else body.get("detections", [])
    assert rows, "seeded detection missing — the assertion would be vacuous"
    for row in rows:
        assert_aware("detections[].timestamp", row["timestamp"])


def test_admin_rest_payloads_are_fully_aware(token):
    """The pages that parse with a raw `new Date()` and would silently render
    local-shifted times: identities, audit, live alerts, retention, search
    history, watchlists."""
    endpoints = [
        "/api/admin/identities?limit=10",
        "/api/admin/unknown?page=1&page_size=10",
        "/api/admin/audit/log?limit=10",
        "/api/live-alerts",
        "/api/admin/retention/status",
        "/api/admin/watchlists",
        "/api/search/history?limit=10",
    ]
    inspected = 0
    for path in endpoints:
        status, body = _http("GET", path, token=token)
        if status in (404, 403):
            continue          # endpoint not mounted in this build
        assert status == 200, (path, status, body)
        inspected += assert_payload_aware(body, path)
    assert inspected >= 1, "no admin timestamps inspected at all"


# ---------------------------------------------------------------------------
# Live payloads — WebSocket
# ---------------------------------------------------------------------------

def _invalidate_dashboard_caches():
    """Force a freshly BUILT initial_data message.

    websocket.py caches the serialized message in Redis (TTL up to 30 h) and
    replays it verbatim. Without this, a pre-fix cached blob would make the
    test assert the old format and fail for the wrong reason.
    """
    from backend.core.redis_cache import redis_cache_service

    async def _run():
        await redis_cache_service.initialize()
        await redis_cache_service.invalidate_dashboard_cache()
        await redis_cache_service.invalidate_unknown_cache()
    run_async(_run())


def _receive_initial_data(token, timeout=20):
    import asyncio

    websockets = pytest.importorskip("websockets")

    async def _run():
        url = f"ws://localhost:8000/ws?token={token}"
        async with websockets.connect(url, open_timeout=10) as socket:
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                message = json.loads(raw)
                if message.get("type") == "initial_data":
                    return message
            return None
    return run_async(_run())


def test_websocket_initial_data_timestamps_are_aware(token, seeded_detection):
    """The reported bug, end to end.

    Every timestamp in the message is walked recursively — the envelope, each
    `data[].timestamp`, and each `faces[].last_seen_at`.
    """
    _invalidate_dashboard_caches()
    message = _receive_initial_data(token)
    assert message is not None, "no initial_data message received within timeout"

    assert_aware("initial_data.timestamp", message["timestamp"])
    checked = assert_payload_aware(message, "initial_data")
    assert checked >= 1, "walker found no timestamps in initial_data"


def test_websocket_cached_replay_keeps_the_aware_format(token):
    """Cache entries written AFTER deployment carry the new representation.

    Self-sufficient rather than order-dependent: invalidate, connect once to
    force a fresh BUILD (which writes the cache), then connect again to get the
    REPLAY. The second message travelled through Redis
    (_serialize_for_redis + json.dumps/loads), so this proves the round trip
    does not degrade the format.

    A pre-deployment entry would still be naive — that is exactly why
    dashboard.js keeps parseLegacyNaiveUtcTimestamp, and why deployment should
    call invalidate_all(); see the ops note in Docs/36.
    """
    _invalidate_dashboard_caches()
    first = _receive_initial_data(token)
    assert first is not None, "no initial_data on the fresh-build path"

    second = _receive_initial_data(token)
    assert second is not None, "no initial_data on the cached path"
    assert_aware("cached initial_data.timestamp", second["timestamp"])
    checked = assert_payload_aware(second, "cached initial_data")
    assert checked >= 1, "walker found no timestamps in the cached message"


# ---------------------------------------------------------------------------
# Source guards (secondary — the behavioural tests above are the evidence)
# ---------------------------------------------------------------------------

WIRE_PRODUCERS = [
    "/app/backend/routes/websocket.py",
    "/app/backend/routes/detections.py",
    "/app/backend/routes/audit.py",
    "/app/backend/routes/live_alerts.py",
    "/app/backend/routes/batch_export.py",
    "/app/backend/routes/retention.py",
    "/app/backend/routes/intelligence.py",
    "/app/backend/routes/identities.py",
    "/app/backend/core/background_task_notifier.py",
]


def test_wire_producers_have_no_bare_isoformat():
    """A bare `.isoformat()` in these files emits a naive string.

    Legal shapes: `iso_utc(...)`, a local helper delegating to it, or the
    explicit `.isoformat() + "Z"` on a known-naive value.
    """
    offenders = []
    for path in WIRE_PRODUCERS:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for match in re.finditer(r"\.isoformat\(\)", source):
            tail = source[match.end():match.end() + 12]
            if re.match(r'\s*\+\s*"Z"', tail):
                continue
            line = source[:match.start()].count("\n") + 1
            offenders.append(f"{os.path.basename(path)}:{line}")
    assert not offenders, f"bare .isoformat() at wire producers: {offenders}"


def test_websocket_route_has_no_isoformat_at_all():
    with open("/app/backend/routes/websocket.py", encoding="utf-8") as handle:
        source = handle.read()
    assert ".isoformat()" not in source, (
        "websocket.py must serialize exclusively through iso_utc()")


def test_db_comparison_sites_still_use_naive_utcnow():
    """The other half of the contract: an aware value bound against a naive
    TIMESTAMP column is a TypeError (or worse, a silent shift)."""
    with open("/app/backend/routes/websocket.py", encoding="utf-8") as handle:
        source = handle.read()
    assert "cutoff_time = datetime.utcnow() - timedelta" in source, (
        "the initial-data cutoff comparison must stay naive")
    assert "utc_now() -" not in source and "- utc_now()" not in source, (
        "aware arithmetic against naive DB values")


def test_dashboard_keeps_its_legacy_compatibility_gate():
    """Requirement: the frontend parser stays for pre-deployment cache entries
    and any producer not yet migrated. New payloads must simply never reach it."""
    with open("/app/frontend/js/dashboard.js", encoding="utf-8") as handle:
        source = handle.read()
    assert "parseLegacyNaiveUtcTimestamp" in source
    assert "function parseServerTimestamp" in source


def test_ml_ops_form_no_longer_stamps_z_onto_local_input():
    with open("/app/frontend/js/admin-ml-ops.js", encoding="utf-8") as handle:
        source = handle.read()
    assert "toISOString()" in source
    assert "+ ':00' : eventRaw) + 'Z'" not in source, (
        "a datetime-local value is the analyst's LOCAL wall clock; appending "
        "'Z' relabels it as UTC")


def test_no_bare_local_datetime_now_in_backend():
    """`datetime.now()` reads the SERVER's local clock. Every value here is
    UTC-labelled, filename-bound, or compared against naive-UTC DB values."""
    offenders = []
    for root, _dirs, names in os.walk("/app/backend"):
        if "__pycache__" in root:
            continue
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for match in re.finditer(r"datetime\.now\(\s*\)", source):
                line = source[:match.start()].count("\n") + 1
                offenders.append(f"{os.path.relpath(path, '/app')}:{line}")
    assert not offenders, f"bare datetime.now() (server-local): {offenders}"
