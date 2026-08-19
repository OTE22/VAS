"""Unknown Faces Center: filtering, pagination and authorization.

The reported symptom was "APPLY FILTERS does not filter correctly on date or on
pipeline". It was four defects wearing one coat:

  * the pipeline filter ran in PYTHON, over rows SQL had already paginated, so
    a camera whose faces were not among the newest `page_size` identities came
    back empty while `total` claimed hundreds;
  * `date_to` was compared with `<=` against a bare `YYYY-MM-DD`, i.e. midnight,
    so From = To = today matched nothing at all;
  * a `<input type="date">` value is the operator's LOCAL day and was compared
    against UTC-stored instants, misplacing everything within the browser's
    offset of midnight;
  * Next never re-fetched, so the wrong answer could not even be paged past.

These tests are BEHAVIOURAL: they seed rows, call the endpoint and assert what
comes back. The two source-level checks at the end are supplementary guards
against a careless re-ordering; they are not evidence that anything works.
"""

import json
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

BASE = "http://localhost:8000"

# Everything this module creates carries this marker and nothing else does.
MARK = "pytest-unkfilt"
CAM_OLD = "pytest-unkfilt-old"     # faces deliberately older than one page
CAM_NEW = "pytest-unkfilt-new"     # faces that crowd out the first page
CAM_BOTH = "pytest-unkfilt-both"   # an identity seen on two cameras
DAY = datetime(2026, 6, 15, 12, 0, 0)   # fixed; never "now"


def _http(method, path, body=None, token=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except Exception:                                      # noqa: BLE001
            payload = {}
        return exc.code, payload


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


def _drop_unknown_cache():
    async def _run():
        from backend.core.redis_cache import redis_cache_service
        if not getattr(redis_cache_service, "_initialized", False):
            await redis_cache_service.initialize()
        await redis_cache_service.invalidate_unknown_cache()
    run_async(_run())


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         {"username": "admin", "password": "admin123"})
    assert status == 200, f"admin login failed: {body}"
    return body["access_token"]


def _cleanup():
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            await db.execute(sa_text(
                "DELETE FROM identity_appearances WHERE identity_id IN "
                "(SELECT id FROM identities WHERE display_name LIKE :m)"), {"m": MARK + "%"})
            await db.execute(sa_text(
                "DELETE FROM identity_embeddings WHERE identity_id IN "
                "(SELECT id FROM identities WHERE display_name LIKE :m)"), {"m": MARK + "%"})
            await db.execute(sa_text(
                "DELETE FROM identities WHERE display_name LIKE :m"), {"m": MARK + "%"})
            await db.execute(sa_text(
                "DELETE FROM user_pipeline_access WHERE pipeline_id LIKE :m"), {"m": MARK + "%"})
            await db.execute(sa_text(
                "DELETE FROM users WHERE username LIKE :m"), {"m": MARK + "%"})
            await db.execute(sa_text(
                "DELETE FROM pipelines WHERE pipeline_id LIKE :m"), {"m": MARK + "%"})
            await db.commit()
    run_async(_run())


@pytest.fixture(scope="module")
def seeded():
    """A dataset shaped to expose the defects.

    CAM_NEW gets 30 identities newer than everything on CAM_OLD, so CAM_OLD's
    faces cannot appear in the first page of an unfiltered listing — which is
    exactly the case the old post-pagination filter got wrong.
    """
    _cleanup()

    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        made = {"old": [], "new": [], "both": [], "tied": [],
                "embedding_only": None, "face_only": None}
        async with db_manager.get_session() as db:
            for cam in (CAM_OLD, CAM_NEW, CAM_BOTH):
                await db.execute(sa_text(
                    "INSERT INTO pipelines (pipeline_id, created_at, is_active) "
                    "VALUES (:p, now(), 1) ON CONFLICT (pipeline_id) DO NOTHING"), {"p": cam})

            async def mk(name, seen, appearances=1):
                ident = (await db.execute(sa_text(
                    "INSERT INTO identities (id, type, status, display_name, first_seen_at,"
                    " last_seen_at, created_at, updated_at, appearances_count) VALUES "
                    "(gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, :ts, :ts, now(), now(), :c) "
                    "RETURNING id"), {"n": name, "ts": seen, "c": appearances})).scalar()
                return str(ident)

            async def appear(ident, cam, seen):
                await db.execute(sa_text(
                    "INSERT INTO identity_appearances (identity_id, pipeline_id, start_time,"
                    " created_at) VALUES (:i, :p, :ts, now())"),
                    {"i": ident, "p": cam, "ts": seen})

            # 30 OLD (a week back) and 30 NEW (today) — one full page of NEW
            # sits in front of every OLD row.
            for i in range(30):
                ts = DAY - timedelta(days=7, minutes=i)
                ident = await mk(f"{MARK}-old-{i:02d}", ts, appearances=2)
                await appear(ident, CAM_OLD, ts)
                made["old"].append(ident)
            for i in range(30):
                ts = DAY - timedelta(minutes=i)
                ident = await mk(f"{MARK}-new-{i:02d}", ts, appearances=3)
                await appear(ident, CAM_NEW, ts)
                made["new"].append(ident)

            # Seen on BOTH cameras: the fan-out case for the statistics.
            for i in range(4):
                ts = DAY - timedelta(days=1, minutes=i)
                ident = await mk(f"{MARK}-both-{i:02d}", ts, appearances=5)
                await appear(ident, CAM_OLD, ts)
                await appear(ident, CAM_BOTH, ts)
                made["both"].append(ident)

            # Five identities sharing ONE last_seen_at, for the tie test.
            tie_ts = DAY - timedelta(days=3)
            for i in range(5):
                ident = await mk(f"{MARK}-tied-{i:02d}", tie_ts)
                await appear(ident, CAM_OLD, tie_ts)
                made["tied"].append(ident)

            # Tier 2: an identity with NO appearance, only an embedding.
            ts = DAY - timedelta(days=2)
            ident = await mk(f"{MARK}-embonly", ts)
            await db.execute(sa_text(
                "INSERT INTO identity_embeddings (identity_id, pipeline_id, faiss_index_type,"
                " created_at) VALUES (:i, :p, 'unknown', now())"), {"i": ident, "p": CAM_BOTH})
            made["embedding_only"] = ident

            await db.commit()
        return made

    data = run_async(_run())
    _drop_unknown_cache()
    yield data
    _cleanup()
    _drop_unknown_cache()


def _list(token, **params):
    params.setdefault("show_all", "true")
    params.setdefault("page", 1)
    params.setdefault("page_size", 100)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    status, body = _http("GET", f"/api/admin/unknown?{query}", token=token)
    assert status == 200, body
    return body


def _marked(payload):
    return [i for i in payload["identities"]
            if (i.get("display_name") or "").startswith(MARK)]


# ---------------------------------------------------------------------------
# Pipeline filtering
# ---------------------------------------------------------------------------

def test_pipeline_filter_returns_only_that_pipeline(token, seeded):
    body = _list(token, pipeline_id=CAM_NEW)
    rows = _marked(body)
    assert rows, "the filter returned none of the seeded faces"
    foreign = [i["id"] for i in rows if CAM_NEW not in i["pipeline_ids"]]
    assert not foreign, f"identities without {CAM_NEW} came back: {foreign}"
    assert {i["id"] for i in rows} == set(seeded["new"])


def test_pipeline_filter_reaches_faces_older_than_the_first_page(token, seeded):
    """THE reported bug.

    Every CAM_OLD face is older than all 30 CAM_NEW ones, so none of them are
    in the newest page. Filtering in Python after the LIMIT returned an empty
    page here while `total` still counted them.
    """
    unfiltered = _list(token, page_size=25)
    first_page_ids = {i["id"] for i in unfiltered["identities"]}
    assert not (first_page_ids & set(seeded["old"])), (
        "fixture no longer isolates the case: an OLD face reached page 1")

    body = _list(token, pipeline_id=CAM_OLD, page_size=25)
    returned = {i["id"] for i in _marked(body)}
    assert returned, "filtering by a camera with only older faces returned nothing"
    assert returned <= set(seeded["old"]) | set(seeded["both"]) | set(seeded["tied"])
    # and the count agrees with what the filter actually matches
    expected = len(seeded["old"]) + len(seeded["both"]) + len(seeded["tied"])
    assert body["total"] >= expected, (body["total"], expected)


def test_pipeline_membership_follows_the_three_source_priority(token, seeded):
    """An identity with no appearances is placed by its embedding, and
    filtering by that camera returns it — the card and the filter agree."""
    body = _list(token, pipeline_id=CAM_BOTH)
    ids = {i["id"] for i in _marked(body)}
    assert seeded["embedding_only"] in ids, (
        "an identity whose only pipeline evidence is an embedding was not "
        "matched by its camera")
    card = next(i for i in _marked(body) if i["id"] == seeded["embedding_only"])
    assert card["pipeline_ids"] == [CAM_BOTH], card["pipeline_ids"]


def test_an_identity_on_two_cameras_is_returned_by_either(token, seeded):
    for cam in (CAM_OLD, CAM_BOTH):
        ids = {i["id"] for i in _marked(_list(token, pipeline_id=cam))}
        assert set(seeded["both"]) <= ids, f"{cam} lost the two-camera identities"


# ---------------------------------------------------------------------------
# Date bounds
# ---------------------------------------------------------------------------

def test_from_equals_to_covers_that_whole_day(token, seeded):
    """From = To = one calendar day used to match nothing, because `<=` against
    a bare date is midnight."""
    day = (DAY - timedelta(days=7)).strftime("%Y-%m-%d")
    body = _list(token, date_from=day, date_to=day)
    rows = _marked(body)
    assert rows, f"a whole-day filter on {day} returned nothing"
    for row in rows:
        assert row["last_seen_at"][:10] == day, row["last_seen_at"]


def test_date_only_and_instant_upper_bounds_differ(token, seeded):
    day = (DAY - timedelta(days=7)).strftime("%Y-%m-%d")
    whole_day = _list(token, date_from=day, date_to=day)
    # An instant at midday cuts the same day in half; it must NOT gain a day.
    instant = _list(token, date_from=day, date_to=f"{day}T00:30:00Z")
    assert instant["total"] < whole_day["total"], (
        "a full-ISO date_to was widened to the whole day")


@pytest.mark.parametrize("raw,end,expected", [
    ("2026-08-18", False, datetime(2026, 8, 18, 0, 0)),
    ("2026-08-18", True, datetime(2026, 8, 19, 0, 0)),        # whole day
    ("2026-08-18T21:00:00Z", True, datetime(2026, 8, 18, 21, 0)),
    ("2026-08-18T21:00:00+03:00", True, datetime(2026, 8, 18, 18, 0)),
    ("2026-08-18T21:00:00-05:00", True, datetime(2026, 8, 19, 2, 0)),
])
def test_filter_bounds_normalize_to_naive_utc(raw, end, expected):
    """The columns are naive UTC. An offset must be CONVERTED, never stripped —
    stripping +03:00 would move the bound three hours."""
    from backend.routes.identities import _parse_filter_bound
    got = _parse_filter_bound(raw, end=end)
    assert got == expected, f"{raw} -> {got}, expected {expected}"
    assert got.tzinfo is None, "a tz-aware bound cannot be compared with the column"


def test_a_local_day_from_an_offset_browser_lands_on_that_day(token, seeded):
    """What the page now sends: local midnight, and local midnight of the next
    day as an exclusive upper bound."""
    tz = timezone(timedelta(hours=3))                      # e.g. Beirut
    local_day = (DAY - timedelta(days=7)).date()
    start = datetime(local_day.year, local_day.month, local_day.day, tzinfo=tz)
    body = _list(token,
                 date_from=start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                 date_to=(start + timedelta(days=1)).astimezone(timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    assert _marked(body), "the operator's own day came back empty"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def test_total_and_total_pages_are_the_same_on_every_page(token, seeded):
    first = _list(token, page=1, page_size=10)
    second = _list(token, page=2, page_size=10)
    assert first["total"] == second["total"], "total changed between pages"
    assert first["total_pages"] == second["total_pages"]
    assert first["total_pages"] > 1, "fixture should span several pages"


def test_page_two_returns_different_identities(token, seeded):
    first = {i["id"] for i in _list(token, page=1, page_size=10)["identities"]}
    second = {i["id"] for i in _list(token, page=2, page_size=10)["identities"]}
    assert second, "page 2 was empty"
    assert not (first & second), "page 2 repeated rows from page 1"


def test_pagination_is_deterministic_under_tied_timestamps(token, seeded):
    """Five identities share one last_seen_at. Without the `id` tiebreak,
    PostgreSQL may order them differently per call and rows repeat or vanish."""
    def walk():
        pages, page = [], 1
        while True:
            body = _list(token, page=page, page_size=7)
            pages += [i["id"] for i in body["identities"]]
            if page >= body["total_pages"]:
                return pages, body["total"]
            page += 1

    first_walk, total = walk()
    second_walk, _ = walk()

    assert len(first_walk) == len(set(first_walk)), "an identity appeared on two pages"
    assert len(first_walk) == total, (
        f"walked {len(first_walk)} rows but total says {total} — rows were lost")
    assert first_walk == second_walk, "ordering changed between identical requests"
    assert set(seeded["tied"]) <= set(first_walk), "tied identities were dropped"


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def test_statistics_are_not_inflated_by_multi_camera_identities(token, seeded):
    """`both` identities have 2 appearances rows each. Computing the stats in
    one join would count their appearances_count twice."""
    body = _list(token, pipeline_id=CAM_BOTH)
    rows = _marked(body)
    expected_appearances = sum(i["appearances_count"] for i in rows)
    assert body["stats"]["total_appearances"] == expected_appearances, (
        "total_appearances does not match the sum over the matched identities")
    assert body["stats"]["total_unknown"] == body["total"]
    assert body["stats"]["active_cameras"] >= 1


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

class _Principal:
    """Just the two attributes the route reads.

    Deliberately not a loaded ORM User: mutating `user.role` on a session-bound
    instance would flush a real role change into the database.
    """

    def __init__(self, user_id, role):
        self.id = user_id
        self.role = role


def _as_user(user_id, role, **params):
    """Call the route in-process as a specific principal."""
    async def _run():
        from db_connection import db_manager
        from backend.routes.identities import list_unknown_identities
        await _ensure_db()
        async with db_manager.get_session() as db:
            return await list_unknown_identities(
                page=params.get("page", 1), page_size=params.get("page_size", 100),
                date_from=None, date_to=None,
                pipeline_id=params.get("pipeline_id"), status_filter=None,
                min_appearances=None, show_all=True, db=db,
                current_user=_Principal(user_id, role))
    return run_async(_run())


@pytest.fixture(scope="module")
def restricted_user(seeded):
    """A non-admin who can see CAM_OLD and nothing else."""
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        async with db_manager.get_session() as db:
            uid = (await db.execute(sa_text(
                "INSERT INTO users (username, email, password_hash, role, is_active,"
                " can_use_chatbot, created_at) "
                "VALUES (:u, :e, 'x', 'user', true, false, now()) RETURNING id"),
                {"u": f"{MARK}-user", "e": f"{MARK}@example.invalid"})).scalar()
            await db.execute(sa_text(
                "INSERT INTO user_pipeline_access (user_id, pipeline_id, granted_at) "
                "VALUES (:u, :p, now())"), {"u": uid, "p": CAM_OLD})
            blind = (await db.execute(sa_text(
                "INSERT INTO users (username, email, password_hash, role, is_active,"
                " can_use_chatbot, created_at) "
                "VALUES (:u, :e, 'x', 'user', true, false, now()) RETURNING id"),
                {"u": f"{MARK}-blind", "e": f"{MARK}-blind@example.invalid"})).scalar()
            await db.commit()
            return uid, blind
    return run_async(_run())


def test_non_admin_sees_a_full_page_not_a_partly_emptied_one(restricted_user, seeded):
    """The access filter used to run after the LIMIT, so a user whose cameras
    were sparse in the newest rows got a page with holes in it."""
    _drop_unknown_cache()
    uid, _ = restricted_user
    body = _as_user(uid, "user", page_size=10)
    assert body["total"] > 10, "fixture should give this user more than one page"
    assert len(body["identities"]) == 10, (
        f"asked for 10, got {len(body['identities'])} — rows were dropped after "
        "pagination")
    for row in body["identities"]:
        assert CAM_OLD in row["pipeline_ids"], row["pipeline_ids"]


def test_non_admin_with_no_pipelines_sees_nothing(restricted_user):
    _drop_unknown_cache()
    _, blind = restricted_user
    body = _as_user(blind, "user")
    assert body["total"] == 0 and body["identities"] == []


def test_non_admin_naming_an_inaccessible_pipeline_gets_nothing(restricted_user, seeded):
    """Fail closed: an out-of-scope camera must not widen the result."""
    _drop_unknown_cache()
    uid, _ = restricted_user
    body = _as_user(uid, "user", pipeline_id=CAM_NEW)
    assert body["total"] == 0, "an inaccessible camera returned rows"
    assert body["identities"] == []


def test_cache_keys_separate_authorization_scopes(restricted_user):
    """Two principals must never share an entry, and a permission change must
    change the key at once rather than after the 30-hour TTL."""
    async def _run():
        from backend.core.redis_cache import redis_cache_service
        key = redis_cache_service.get_unknown_cache_key
        base = dict(page=1, page_size=25)
        admin_key = await key(user_id=1, filters={"schema": "v2", "role": "admin",
                                                  "scope": None}, **base)
        user_key = await key(user_id=1, filters={"schema": "v2", "role": "user",
                                                 "scope": [CAM_OLD]}, **base)
        widened = await key(user_id=1, filters={"schema": "v2", "role": "user",
                                                "scope": [CAM_OLD, CAM_NEW]}, **base)
        other_user = await key(user_id=2, filters={"schema": "v2", "role": "user",
                                                   "scope": [CAM_OLD]}, **base)
        return admin_key, user_key, widened, other_user

    admin_key, user_key, widened, other_user = run_async(_run())
    assert len({admin_key, user_key, widened, other_user}) == 4, (
        "cache keys collide across role, scope or user")


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

def test_query_count_does_not_grow_with_page_size(token, seeded):
    """The removed code ran up to three queries PER IDENTITY, twice over.

    The claim under test is architectural — a bounded set of SQL operations
    rather than work per row — so this asserts the shape of the growth, not an
    exact number, which would break on any unrelated query being added.
    """
    async def _run(page_size):
        from db_connection import db_manager
        from db_models import User
        from sqlalchemy import event, select as sa_select
        from backend.routes.identities import list_unknown_identities
        from backend.core.redis_cache import redis_cache_service
        await _ensure_db()
        if not getattr(redis_cache_service, "_initialized", False):
            await redis_cache_service.initialize()
        await redis_cache_service.invalidate_unknown_cache()

        counter = {"n": 0}

        def _count(conn, cursor, statement, params, context, executemany):
            counter["n"] += 1

        engine = db_manager.engine.sync_engine
        event.listen(engine, "before_cursor_execute", _count)
        try:
            async with db_manager.get_session() as db:
                user = (await db.execute(
                    sa_select(User).where(User.role == "admin").limit(1))).scalars().first()
                body = await list_unknown_identities(
                    page=1, page_size=page_size, date_from=None, date_to=None,
                    pipeline_id=None, status_filter=None, min_appearances=None,
                    show_all=True, db=db, current_user=user)
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        return counter["n"], len(body["identities"])

    small_queries, small_rows = run_async(_run(5))
    large_queries, large_rows = run_async(_run(50))

    assert large_rows > small_rows, "fixture did not produce a larger page"
    assert large_queries <= small_queries + 2, (
        f"{small_rows} identities cost {small_queries} queries but {large_rows} "
        f"cost {large_queries} — query count is growing with the row count")


# ---------------------------------------------------------------------------
# Supplementary source guards (NOT evidence — the tests above are)
# ---------------------------------------------------------------------------

def test_source_applies_pagination_after_the_filters():
    with open("/app/backend/routes/identities.py", encoding="utf-8") as handle:
        source = handle.read()
    body = source.split("async def list_unknown_identities")[1].split("\nasync def ")[0]
    scope_at = body.index("pipeline_scope_predicate(pipeline_id, user_pipelines)")
    offset_at = body.index(".offset(offset)")
    assert scope_at < offset_at, "pagination is applied before the pipeline scope"
    assert "Identity.id.desc()" in body, "the ORDER BY lost its deterministic tiebreak"


def test_source_has_one_effective_pipeline_definition():
    """Exactly one definition, REPO-WIDE — the point is that no consumer can
    hand-write the priority again. It previously existed three times: twice in
    the listing and once in auth_service.check_identity_access."""
    import os
    definitions = []
    for root, _dirs, names in os.walk("/app/backend"):
        if "__pycache__" in root:
            continue
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                if "def effective_pipelines(" in handle.read():
                    definitions.append(os.path.relpath(path, "/app"))
    assert definitions == ["backend/core/identity_pipelines.py"], definitions

    with open("/app/backend/routes/identities.py", encoding="utf-8") as handle:
        source = handle.read()
    body = source.split("async def list_unknown_identities")[1].split("\nasync def ")[0]
    # The three per-identity pipeline lookups the route used to run inside its
    # loop. Matching these exact shapes, not any mention of the tables: the
    # route still contains an IdentityAppearance query for SNAPSHOT repair,
    # which is about images and is none of this test's business.
    for removed in ("select(IdentityAppearance.pipeline_id).where(",
                    "select(IdentityEmbedding.pipeline_id).where(",
                    "select(Detection.pipeline_id).join("):
        assert removed not in body, (
            f"the route re-implements pipeline lookup ({removed}) instead of "
            "reading the effective-pipeline relation")
    assert "await pipelines_for(" in body, (
        "the per-page lookup must come from the one shared relation")


def test_frontend_pagination_always_refetches():
    with open("/app/frontend/js/admin-unknown.js", encoding="utf-8") as handle:
        source = handle.read()
    assert "allPipelineGroups.length > 0" not in source, (
        "Next still re-slices groups in memory instead of fetching")
    assert "function resetPagination" in source
    assert "localDayToUtcInstant" in source
    assert "startswith(" not in source, "Python method name in JavaScript"


def test_frontend_groups_under_the_filtered_camera_only():
    """A person on three cameras carries all three in `pipeline_ids`. Grouping
    by each of them drew groups for cameras the operator had filtered out, so a
    correct backend answer still looked like the filter had been ignored.
    Behavioural proof lives in scripts/dev/unknown_filter_probe.js, which
    drives the real page; this only guards the mechanism."""
    with open("/app/frontend/js/admin-unknown.js", encoding="utf-8") as handle:
        source = handle.read()
    assert "const only = currentFilters.pipeline_id || null;" in source
    assert ".filter(pipelineId => !only || pipelineId === only)" in source, (
        "grouping no longer restricts to the selected camera")
