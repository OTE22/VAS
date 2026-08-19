"""One definition of pipeline membership, and cache invalidation that hits.

Two defects that shared a shape: code written against an assumption nobody
re-checked against the data.

1. The priority rule "appearances, else embeddings, else Face->Detection"
   existed THREE times — twice in the unknown-faces listing and once in
   `auth_service.check_identity_access`. Three hand-written copies of an
   authorization rule is a drift waiting to happen, and the failure is
   invisible: an identity listed to a user that the detail view then refuses,
   or worse, the reverse. It now lives once, in
   `backend.core.identity_pipelines`.

2. `invalidate_unknown_cache(user_id=N)` built the glob
   `cache:unknown:*:user_N:*` while real keys are `cache:unknown:user_N:hash`
   — one segment too many, so it matched NOTHING and cleared nothing. It went
   unseen because every caller passes None and takes the global path.

Every assertion here is checked against the database or against Redis itself,
not against what an endpoint claims.
"""

import json
import urllib.error
import urllib.request

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

BASE = "http://localhost:8000"
MARK = "pytest-pipeauth"
CAM_A = f"{MARK}-cam-a"
CAM_B = f"{MARK}-cam-b"
CAM_C = f"{MARK}-cam-c"


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


async def _cache():
    from backend.core.redis_cache import redis_cache_service
    if not getattr(redis_cache_service, "_initialized", False):
        await redis_cache_service.initialize()
    return redis_cache_service


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
            for table in ("identity_appearances", "identity_embeddings"):
                await db.execute(sa_text(
                    f"DELETE FROM {table} WHERE identity_id IN "
                    "(SELECT id FROM identities WHERE display_name LIKE :m)"),
                    {"m": MARK + "%"})
            # faces first (FK to detections), then the detections themselves —
            # both are tied to this module's cameras, nothing else uses them.
            await db.execute(sa_text(
                "DELETE FROM faces WHERE detection_id IN "
                "(SELECT id FROM detections WHERE pipeline_id LIKE :m)"),
                {"m": MARK + "%"})
            await db.execute(sa_text(
                "DELETE FROM detections WHERE pipeline_id LIKE :m"), {"m": MARK + "%"})
            await db.execute(sa_text(
                "DELETE FROM identities WHERE display_name LIKE :m"), {"m": MARK + "%"})
            await db.execute(sa_text(
                "DELETE FROM user_pipeline_access WHERE pipeline_id LIKE :m"),
                {"m": MARK + "%"})
            await db.execute(sa_text(
                "DELETE FROM users WHERE username LIKE :m"), {"m": MARK + "%"})
            await db.execute(sa_text(
                "DELETE FROM pipelines WHERE pipeline_id LIKE :m"), {"m": MARK + "%"})
            await db.commit()
    run_async(_run())


@pytest.fixture(scope="module")
def world():
    """One identity per priority tier, plus users with different access.

    tier1  appearances on CAM_A  (and an embedding on CAM_B that must be IGNORED)
    tier2  no appearances, embedding on CAM_B
    tier3  neither, only a Face -> Detection on CAM_C
    """
    _cleanup()

    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        made = {}
        async with db_manager.get_session() as db:
            for cam in (CAM_A, CAM_B, CAM_C):
                await db.execute(sa_text(
                    "INSERT INTO pipelines (pipeline_id, created_at, is_active) "
                    "VALUES (:p, now(), 1) ON CONFLICT (pipeline_id) DO NOTHING"),
                    {"p": cam})

            async def identity(name):
                return str((await db.execute(sa_text(
                    "INSERT INTO identities (id, type, status, display_name,"
                    " first_seen_at, last_seen_at, created_at, updated_at,"
                    " appearances_count) VALUES (gen_random_uuid(), 'UNKNOWN',"
                    " 'ACTIVE', :n, now(), now(), now(), now(), 1) RETURNING id"),
                    {"n": name})).scalar())

            # tier 1 — appearances win outright over its own embedding
            made["tier1"] = await identity(f"{MARK}-tier1")
            await db.execute(sa_text(
                "INSERT INTO identity_appearances (identity_id, pipeline_id,"
                " start_time, created_at) VALUES (:i, :p, now(), now())"),
                {"i": made["tier1"], "p": CAM_A})
            await db.execute(sa_text(
                "INSERT INTO identity_embeddings (identity_id, pipeline_id,"
                " faiss_index_type, created_at) VALUES (:i, :p, 'unknown', now())"),
                {"i": made["tier1"], "p": CAM_B})

            # tier 2 — no appearances, so the embedding speaks
            made["tier2"] = await identity(f"{MARK}-tier2")
            await db.execute(sa_text(
                "INSERT INTO identity_embeddings (identity_id, pipeline_id,"
                " faiss_index_type, created_at) VALUES (:i, :p, 'unknown', now())"),
                {"i": made["tier2"], "p": CAM_B})

            # tier 3 — only a detected face carries a camera
            made["tier3"] = await identity(f"{MARK}-tier3")
            det = (await db.execute(sa_text(
                "INSERT INTO detections (pipeline_id, timestamp)"
                " VALUES (:p, now()) RETURNING id"), {"p": CAM_C})).scalar()
            await db.execute(sa_text(
                "INSERT INTO faces (detection_id, identity_id, name, similarity)"
                " VALUES (:d, :i, 'Unknown', 0.0)"),
                {"d": det, "i": made["tier3"]})

            for suffix, cams in (("a", [CAM_A]), ("b", [CAM_B]),
                                 ("c", [CAM_C]), ("none", [])):
                uid = (await db.execute(sa_text(
                    "INSERT INTO users (username, email, password_hash, role,"
                    " is_active, can_use_chatbot, created_at) VALUES (:u, :e, 'x',"
                    " 'user', true, false, now()) RETURNING id"),
                    {"u": f"{MARK}-user-{suffix}",
                     "e": f"{MARK}-{suffix}@example.invalid"})).scalar()
                for cam in cams:
                    await db.execute(sa_text(
                        "INSERT INTO user_pipeline_access (user_id, pipeline_id,"
                        " granted_at) VALUES (:u, :p, now())"), {"u": uid, "p": cam})
                made[f"user_{suffix}"] = uid
            await db.commit()
        return made

    data = run_async(_run())

    async def _clear_cache():
        cache = await _cache()
        await cache.invalidate_unknown_cache()
    run_async(_clear_cache())

    yield data
    _cleanup()
    run_async(_clear_cache())


# ---------------------------------------------------------------------------
# The relation itself, against the database
# ---------------------------------------------------------------------------

def test_the_relation_resolves_each_tier_from_the_database(world):
    """Priority, not union: tier1 has an embedding on CAM_B and must still
    resolve to CAM_A alone."""
    async def _run():
        from db_connection import db_manager
        from backend.core.identity_pipelines import pipelines_for
        await _ensure_db()
        async with db_manager.get_session() as db:
            return await pipelines_for(
                db, [world["tier1"], world["tier2"], world["tier3"]])

    resolved = {str(k): v for k, v in run_async(_run()).items()}
    assert resolved.get(world["tier1"]) == {CAM_A}, (
        f"appearances must win outright: {resolved.get(world['tier1'])}")
    assert resolved.get(world["tier2"]) == {CAM_B}
    assert resolved.get(world["tier3"]) == {CAM_C}


def test_one_query_resolves_a_whole_page(world):
    """The helper exists so membership is not re-derived per identity."""
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import event
        from backend.core.identity_pipelines import pipelines_for
        await _ensure_db()
        counter = {"n": 0}

        def _count(conn, cursor, statement, params, context, executemany):
            counter["n"] += 1

        engine = db_manager.engine.sync_engine
        event.listen(engine, "before_cursor_execute", _count)
        try:
            async with db_manager.get_session() as db:
                await pipelines_for(
                    db, [world["tier1"], world["tier2"], world["tier3"]])
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        return counter["n"]

    assert run_async(_run()) == 1, "membership for a page must cost ONE query"


# ---------------------------------------------------------------------------
# Listing and access check must agree — the drift the duplication invited
# ---------------------------------------------------------------------------

def _listing_ids(user_id, role="user"):
    async def _run():
        from db_connection import db_manager
        from backend.routes.identities import list_unknown_identities
        await _ensure_db()

        class Principal:
            def __init__(self, uid):
                self.id = uid
                self.role = role

        async with db_manager.get_session() as db:
            body = await list_unknown_identities(
                page=1, page_size=100, date_from=None, date_to=None,
                pipeline_id=None, status_filter=None, min_appearances=None,
                show_all=True, db=db, current_user=Principal(user_id))
        return {i["id"] for i in body["identities"]}
    return run_async(_run())


def _access(user_id, identity_id, role="user"):
    async def _run():
        from db_connection import db_manager
        from backend.auth.auth_service import check_identity_access
        await _ensure_db()

        class Principal:
            def __init__(self, uid):
                self.id = uid
                self.role = role

        async with db_manager.get_session() as db:
            return await check_identity_access(identity_id, Principal(user_id), db)
    return run_async(_run())


@pytest.mark.parametrize("user_key,visible_key", [
    ("user_a", "tier1"),   # appearances on CAM_A
    ("user_b", "tier2"),   # embedding on CAM_B
    ("user_c", "tier3"),   # Face -> Detection on CAM_C
])
def test_listing_and_access_check_agree_on_every_tier(world, user_key, visible_key):
    """The two consumers of the rule must reach the same verdict.

    user_b is the sharp case: tier1 HAS an embedding on CAM_B, but appearances
    outrank it, so tier1 is NOT on CAM_B. A union-based copy would leak it into
    both the listing and the access check.
    """
    async def _clear():
        cache = await _cache()
        await cache.invalidate_unknown_cache()
    run_async(_clear())

    uid = world[user_key]
    listed = _listing_ids(uid)
    for tier in ("tier1", "tier2", "tier3"):
        identity_id = world[tier]
        expected = tier == visible_key
        assert (identity_id in listed) is expected, (
            f"{user_key}: {tier} listed={identity_id in listed}, expected {expected}")
        assert _access(uid, identity_id) is expected, (
            f"{user_key}: check_identity_access({tier})={not expected}, "
            f"disagrees with the listing")


def test_a_user_with_no_pipelines_sees_and_reaches_nothing(world):
    async def _clear():
        cache = await _cache()
        await cache.invalidate_unknown_cache()
    run_async(_clear())

    uid = world["user_none"]
    assert _listing_ids(uid) == set()
    for tier in ("tier1", "tier2", "tier3"):
        assert _access(uid, world[tier]) is False


def test_admin_reaches_every_tier(world, token):
    async def _clear():
        cache = await _cache()
        await cache.invalidate_unknown_cache()
    run_async(_clear())

    status, body = _http(
        "GET", "/api/admin/unknown?show_all=true&page=1&page_size=100", token=token)
    assert status == 200, body
    listed = {i["id"] for i in body["identities"]}
    for tier in ("tier1", "tier2", "tier3"):
        assert world[tier] in listed, f"admin cannot see {tier}"


def test_the_card_shows_exactly_what_filtering_by_it_returns(world, token):
    """The correspondence an operator actually checks."""
    async def _clear():
        cache = await _cache()
        await cache.invalidate_unknown_cache()
    run_async(_clear())

    status, body = _http(
        "GET", "/api/admin/unknown?show_all=true&page=1&page_size=100", token=token)
    assert status == 200, body
    cards = {i["id"]: i["pipeline_ids"] for i in body["identities"]}

    for cam in (CAM_A, CAM_B, CAM_C):
        run_async(_clear())
        status, filtered = _http(
            "GET",
            f"/api/admin/unknown?show_all=true&page=1&page_size=100&pipeline_id={cam}",
            token=token)
        assert status == 200, filtered
        returned = {i["id"] for i in filtered["identities"]}
        expected = {ident for ident, cams in cards.items() if cam in cams}
        assert returned == expected, (
            f"{cam}: filter returned {returned}, cards claim {expected}")


# ---------------------------------------------------------------------------
# Cache invalidation, verified against Redis
# ---------------------------------------------------------------------------

def _redis_keys(pattern):
    async def _run():
        cache = await _cache()
        if not cache._enabled or not cache.redis_client:
            pytest.skip("redis disabled in this deployment")
        found = []
        async for key in cache.redis_client.scan_iter(match=pattern):
            found.append(key.decode() if isinstance(key, bytes) else key)
        return found
    return run_async(_run())


def _seed_cache_entry(user_id, marker):
    async def _run():
        cache = await _cache()
        key = await cache.get_unknown_cache_key(
            user_id=user_id, page=1, page_size=25, filters={"marker": marker})
        await cache.set(key, {"marker": marker}, ttl=300)
        return key
    return run_async(_run())


def test_the_key_layout_is_what_the_invalidation_globs_assume():
    """The bug was a glob written against a key format that never existed."""
    key = _seed_cache_entry(4242, "layout")
    assert key.startswith("cache:unknown:user_4242:"), key
    assert key.count(":") == 3, f"unexpected key shape: {key}"


def test_per_user_invalidation_clears_that_user_and_only_that_user():
    """Previously cleared nothing at all: the glob had a segment too many."""
    mine = _seed_cache_entry(4242, "mine")
    theirs = _seed_cache_entry(4343, "theirs")
    assert _redis_keys(mine) and _redis_keys(theirs), "seeding failed"

    async def _invalidate():
        cache = await _cache()
        return await cache.invalidate_unknown_cache(user_id=4242)
    deleted = run_async(_invalidate())

    assert deleted >= 1, (
        f"invalidate_unknown_cache(user_id=4242) deleted {deleted} keys — "
        "the pattern matches nothing")
    assert _redis_keys(mine) == [], "the targeted user's entry survived"
    assert _redis_keys(theirs) != [], "another user's entry was destroyed"

    async def _cleanup():
        cache = await _cache()
        await cache.invalidate_unknown_cache(user_id=4343)
    run_async(_cleanup())


def test_dashboard_invalidation_has_the_same_shape():
    """The identical bug sat in the dashboard invalidation."""
    async def _run():
        cache = await _cache()
        key = await cache.get_dashboard_cache_key(
            user_id=4444, pipeline_ids=["x"], display_hours=24)
        await cache.set(key, {"marker": "dash"}, ttl=300)
        return key
    key = run_async(_run())
    assert key.startswith("cache:dashboard:user_4444:"), key

    async def _invalidate():
        cache = await _cache()
        return await cache.invalidate_dashboard_cache(user_id=4444)
    assert run_async(_invalidate()) >= 1, "dashboard per-user invalidation is a no-op"
    assert _redis_keys(key) == []


def test_global_invalidation_still_clears_everyone():
    a = _seed_cache_entry(5151, "a")
    b = _seed_cache_entry(5252, "b")
    assert _redis_keys(a) and _redis_keys(b)

    async def _invalidate():
        cache = await _cache()
        return await cache.invalidate_unknown_cache()
    run_async(_invalidate())
    assert _redis_keys(a) == [] and _redis_keys(b) == []


def test_user_id_zero_is_not_treated_as_absent():
    """`if user_id:` would send user 0 down the global path and wipe everyone."""
    zero = _seed_cache_entry(0, "zero")
    other = _seed_cache_entry(6161, "other")

    async def _invalidate():
        cache = await _cache()
        return await cache.invalidate_unknown_cache(user_id=0)
    run_async(_invalidate())

    assert _redis_keys(other) != [], (
        "invalidating user 0 cleared another user — truthiness bug")

    async def _cleanup():
        cache = await _cache()
        await cache.invalidate_unknown_cache()
    run_async(_cleanup())
    assert _redis_keys(zero) == []


# ---------------------------------------------------------------------------
# Supplementary source guard
# ---------------------------------------------------------------------------

def test_no_module_rebuilds_the_priority_by_hand():
    """auth_service held the third copy. Nothing may hand-roll it again."""
    import os
    offenders = []
    for root, _dirs, names in os.walk("/app/backend"):
        if "__pycache__" in root or root.endswith("core"):
            continue
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            # The tell-tale of a rebuilt fallback chain: selecting pipeline_id
            # off the appearance table AND off the embedding table in one file.
            if ("select(IdentityAppearance.pipeline_id)" in source
                    and "select(IdentityEmbedding.pipeline_id)" in source):
                offenders.append(os.path.relpath(path, "/app"))
    assert not offenders, f"hand-written pipeline priority in: {offenders}"
