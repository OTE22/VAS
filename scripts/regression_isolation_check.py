"""
Regression isolation assertions — run INSIDE the regression API container
before pytest starts (scripts/run_regression_isolated.sh). Any failure aborts
the regression before a single test executes. Nothing here relies on naming
conventions alone: every claim is checked against the live process.

    python scripts/regression_isolation_check.py --expect-id <id> \
        --expect-db face_recognition_regression --dev-db face_recognition

The stack this guards is SELF-CONTAINED: its own PostgreSQL, Redis, Martin and
nginx on a private network. So the strongest assertion available is no longer
"the dev services are not being written to" — it is that the dev services are
not REACHABLE. A name that does not resolve cannot be touched by a test, by a
mistake in a fixture, or by a library that reads an environment variable
somebody forgot to override.
"""
import argparse
import asyncio
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


HOST_BIND_FSTYPES = ("9p", "fuse.grpcfuse", "virtiofs", "fakeowner", "cifs", "nfs", "nfs4")

# Development containers, by their container_name in docker-compose.cpu.yml.
# None of these may resolve from inside the regression network. `face_recognition`,
# `redis`, `postgres`, `martin` and `nginx` are deliberately NOT here: they are
# the regression stack's own aliases, and the runner separately proves they
# point at this project's containers.
DEV_CONTAINERS = ("face_recognition_db", "face_recognition_redis", "face_recognition_api",
                  "face_recognition_martin", "face_recognition_nginx")


def _mount_info(path: str):
    """(source, fstype) of the longest mount point containing `path`."""
    best = ("", "", "")
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                src, mnt, fstype = parts[0], parts[1], parts[2]
                if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best[1]):
                    best = (src, mnt, fstype)
    except FileNotFoundError:
        pass
    return best[0], best[2]


def _resolves(name: str):
    try:
        return socket.gethostbyname(name)
    except socket.gaierror:
        return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-id", required=True)
    # The regression database carries the SAME name as production, because
    # init-db.sql creates pgvector inside `face_recognition` specifically.
    # Isolation is by server, not by name — which is why §3 below is the
    # assertion that matters.
    ap.add_argument("--expect-db", default="face_recognition")
    ap.add_argument("--dev-redis-host", default="redis")     # kept for call compatibility
    ap.add_argument("--dev-db", default="face_recognition")
    args = ap.parse_args()

    from config import settings
    from urllib.parse import urlparse
    problems = []

    # 1. environment marker + not production
    if os.environ.get("REGRESSION_ISOLATION_ID") != args.expect_id:
        problems.append("REGRESSION_ISOLATION_ID marker missing/mismatched — this is not the regression container")
    if str(settings.ENVIRONMENT).strip().lower() in ("production", "prod"):
        problems.append("ENVIRONMENT is production")

    # 2. database: settings + live current_database()
    db_url = urlparse(settings.DATABASE_URL.replace("+asyncpg", ""))
    if db_url.path.lstrip("/") != args.expect_db:
        problems.append(f"DATABASE_URL points at {db_url.path.lstrip('/')!r}, expected {args.expect_db!r}")
    if db_url.hostname in DEV_CONTAINERS:
        problems.append(f"DATABASE_URL points at the development server {db_url.hostname!r}")
    try:
        from db_connection import db_manager
        from sqlalchemy import text
        await db_manager.init_db()
        async with db_manager.get_session() as db:
            current = (await db.execute(text("SELECT current_database()"))).scalar()
        if current != args.expect_db:
            problems.append(f"live current_database() is {current!r}, expected {args.expect_db!r}")
    except Exception as exc:
        problems.append(f"database check failed: {exc}")

    # 3. NO DEVELOPMENT SERVICE IS REACHABLE. The decisive assertion: on a
    #    private network these names do not exist, so nothing in the suite can
    #    read or write the developer's database, cache, tiles or files even by
    #    accident.
    reachable = {name: ip for name in DEV_CONTAINERS if (ip := _resolves(name))}
    if reachable:
        problems.append(f"development containers are reachable from the regression "
                        f"stack: {reachable} — this run is not isolated")

    # 4. redis is this stack's own instance
    r_url = urlparse(settings.REDIS_URL)
    if r_url.hostname not in ("redis", "redis_regression"):
        problems.append(f"REDIS_URL host is {r_url.hostname!r}, expected the regression redis")
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(settings.REDIS_URL)
        await client.ping()
        info = await client.info("server")
        await client.aclose()
        if not info:
            problems.append("the regression redis answered PING but reported no server info")
    except Exception as exc:
        problems.append(f"redis check failed: {exc}")

    # 5. map data under test is the FIXTURE tree, with its own ledger. If this
    #    ever points at map-data/, the suite is grading the developer's
    #    archives instead of the code.
    root = str(settings.MAP_DATA_DIR).rstrip("/")
    if not root.endswith("map-data-test"):
        problems.append(f"MAP_DATA_DIR is {root!r}, not the immutable fixture tree")
    if not str(settings.MAP_CONTENT_LEDGER_PATH).startswith(root):
        problems.append("the content ledger is outside the map tree it authorizes")

    # 6. storage + ML artifacts + database dir must be docker VOLUMES of this
    #    stack, never the dev host bind mounts (which appear as 9p/drvfs/fuse
    #    host filesystems here). The runner additionally proves it end to end
    #    with a marker file that must not surface in the host directories.
    for label, path in (("storage", str(settings.STORAGE_DIR)),
                        ("ml_artifacts", str(settings.ML_ARTIFACT_DIR)),
                        ("database_dir", "/app/database")):
        abs_path = path if os.path.isabs(path) else os.path.join("/app", path)
        src, fstype = _mount_info(abs_path)
        if fstype in HOST_BIND_FSTYPES or src.startswith(("C:", "/run/desktop", "/host_mnt")):
            problems.append(f"{label} at {abs_path} is a HOST bind mount ({src} {fstype}) — not isolated")
        if not fstype:
            problems.append(f"{label} at {abs_path}: mount not found")

    if problems:
        print("REGRESSION ISOLATION FAILED:\n  - " + "\n  - ".join(problems))
        return 1
    print(f"regression isolation OK: db={args.expect_db} redis={r_url.hostname} "
          f"map={root} storage/ml on regression volumes; "
          f"no development container resolves")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
