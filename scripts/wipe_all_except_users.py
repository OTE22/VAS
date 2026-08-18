"""
Delete every row in the database except the `users` table.

    # dry run first — this is the default, nothing is changed
    docker exec -w /app face_recognition_api python scripts/wipe_all_except_users.py

    # then, for real
    docker stop face_recognition_api
    docker compose -f docker/docker-compose.cpu.yml run --rm --no-deps \
        --entrypoint python face_recognition \
        scripts/wipe_all_except_users.py --apply --yes-i-understand
    docker start face_recognition_api

⚠️  READ THIS BEFORE RUNNING WITH --apply
=========================================
Preserving ONLY `users` is more destructive than the usual cleanup, and two of
the tables it removes are not "data" — the application needs them to boot:

  * `alembic_version` — the recorded schema revision. Empty it and the startup
    preflight can no longer confirm the schema matches the code; production
    compose pins MIGRATIONS_EXPECTED_HEAD and will refuse to start (exit 78).
    Recover with:  alembic stamp head
  * `organizations` + `workspaces` + `workspace_members` — the default
    workspace row. Without it `conversation_service.get_default_workspace_id`
    raises, and every POST /api/v1/conversations answers 500. Nothing re-seeds
    it: the row was created by migration d3e4f5a6b7c8, which will not re-run.

So the honest summary: `--apply` with the defaults leaves you with accounts and
a schema, and an application that will not fully start until you stamp the
revision and re-create the default workspace.

If what you actually want is "clear the demo data but keep the system usable",
use `--keep-boot-rows`, which additionally preserves those tables plus the
migration-seeded ML configuration. That is
the same set `scripts/wipe_operational_data.py` protects, and it is the option
to reach for unless you specifically need a bare database.

Design
------
* **The truncate list is DERIVED from the live schema**, never hard-coded, so a
  table added by a future migration is included automatically instead of being
  silently skipped.
* **One TRUNCATE, no CASCADE.** CASCADE would follow foreign keys out of the
  list and could delete a table this script promised to keep. Without it,
  PostgreSQL refuses the statement instead — a loud failure beats a silent
  over-delete. The FK check below reports the problem before anything runs.
* **Dry run by default.** `--apply --yes-i-understand` is required to write.
* Sequences are reset (`RESTART IDENTITY`) so a fresh database starts at id 1.

This is a development/demo tool. Do not point it at production.
"""

import argparse
import asyncio
import os
import shutil
import sys

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Always kept: the table the caller asked to protect.
ALWAYS_PRESERVE = {"users"}

# Kept only with --keep-boot-rows. These are what the application reads at
# startup rather than operational data; see the warning above.
BOOT_ROW_TABLES = {
    "alembic_version",
    "organizations",
    "workspaces",
    "workspace_members",
    # Migration-seeded ML configuration, NOT operational data. An earlier
    # version of this set omitted these two; a --keep-boot-rows wipe then
    # emptied them and 20 ML tests failed downstream (KeyError
    # 'off_hours_ratio_30d', 404 'Unknown model type') because the feature
    # inventory and the four retraining policies were gone and nothing at
    # startup re-seeds them. Recovery, should it ever happen again:
    #   re-apply the ML migrations (they seed the definitions + the four policies)
    #   (see alembic/versions/b8c9d0e1f2a3_ml_pipeline.py).
    "ml_feature_definitions",
    "ml_retraining_policies",
}

# Everything directly under storage/ is wiped EXCEPT these names.
#
# Derived rather than listed, for the same reason the table list is derived from
# the live schema: the camera pipelines each own a directory named after their
# pipeline id (a UUID), so any hard-coded list is wrong the moment a new camera
# appears. Those folders hold the bulk of the images — the per-person crops and
# the `unknown/` snapshots — and an earlier version of this script missed all of
# them because it only knew about storage/faces.
STORAGE_ROOT = "storage"
STORAGE_KEEP = {
    # Regression fixtures, not data. Deleting these breaks the quality suite.
    "qa-quality-regression",
}

# Generated artifacts outside storage/. Each is rebuilt or re-seeded on demand.
EXTRA_WIPE_DIRS = (
    "models/ml/candidates",       # trained model candidates
    "models/ml/datasets",         # training datasets
    # The SQL agent / chatbot knowledge base. Safe to remove: SQLKnowledgeBase
    # calls _auto_initialize_seed_examples() in its constructor, so an empty
    # store re-seeds itself on the next startup. Any examples LEARNED beyond the
    # built-in seeds are lost, which is the point of a wipe.
    "sql_agent/chromadb_data",
    "chromadb_data",
)

# Never touched, and worth naming so nobody "helpfully" adds them later:
#   ~/.cache/chroma/onnx_models  — 167 MB of DOWNLOADED MODEL, not data.
#                                  Deleting it forces a slow refetch.
#   logs/                        — diagnostic history. Removing it destroys the
#                                  record of what happened; use --include-logs
#                                  if you really want that.
LOG_DIR = "logs"

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def _all_tables(db) -> set:
    from sqlalchemy import text as sa_text
    rows = (await db.execute(sa_text(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).all()
    return {row[0] for row in rows}


async def _row_counts(db, tables) -> dict:
    """Per-table row counts, so the dry run reports real numbers."""
    from sqlalchemy import text as sa_text
    counts = {}
    for table in sorted(tables):
        try:
            counts[table] = (await db.execute(
                sa_text(f'SELECT count(*) FROM "{table}"'))).scalar() or 0
        except Exception:                                      # noqa: BLE001
            counts[table] = -1          # unreadable; reported, never guessed
    return counts


async def _blocking_foreign_keys(db, preserve, truncate) -> list:
    """Preserved tables that reference a truncated one.

    This is the exact condition that makes TRUNCATE-without-CASCADE fail.
    Reporting it up front turns a confusing database error into a sentence.
    """
    from sqlalchemy import text as sa_text
    rows = (await db.execute(sa_text("""
        SELECT tc.table_name, ccu.table_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
    """))).all()
    return sorted({
        f"{child} -> {parent}"
        for child, parent in rows
        if child in preserve and parent in truncate and child != parent
    })


def _files_under(relative_dir):
    absolute = os.path.join(REPO, relative_dir)
    if not os.path.isdir(absolute):
        return []
    found = []
    for root, _dirs, names in os.walk(absolute):
        found.extend(os.path.join(root, name) for name in names)
    return found


def _wipe_targets(include_logs: bool):
    """Relative directories to empty, derived from what is actually on disk.

    Everything under storage/ except STORAGE_KEEP — which picks up each
    camera's pipeline-id folder without anyone having to list them — plus the
    generated artifact directories elsewhere in the repo.
    """
    targets = []

    storage_absolute = os.path.join(REPO, STORAGE_ROOT)
    if os.path.isdir(storage_absolute):
        for entry in sorted(os.listdir(storage_absolute)):
            if entry in STORAGE_KEEP:
                continue
            if os.path.isdir(os.path.join(storage_absolute, entry)):
                targets.append(f"{STORAGE_ROOT}/{entry}")

    for relative in EXTRA_WIPE_DIRS:
        if os.path.isdir(os.path.join(REPO, relative)):
            targets.append(relative)

    if include_logs and os.path.isdir(os.path.join(REPO, LOG_DIR)):
        targets.append(LOG_DIR)

    return targets


IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def _count_images(paths):
    return sum(1 for path in paths
               if os.path.splitext(path)[1].lower() in IMAGE_SUFFIXES)


async def main(args) -> int:
    from db_connection import db_manager
    from sqlalchemy import text as sa_text

    await db_manager.init_db()

    preserve = set(ALWAYS_PRESERVE)
    if args.keep_boot_rows:
        preserve |= BOOT_ROW_TABLES
    preserve |= {name.strip() for name in (args.keep or []) if name.strip()}

    async with db_manager.get_session() as db:
        database = (await db.execute(sa_text("SELECT current_database()"))).scalar()
        tables = await _all_tables(db)

        unknown = preserve - tables
        if unknown:
            print(f"❌ asked to preserve tables that do not exist: {sorted(unknown)}")
            return 2

        truncate = sorted(tables - preserve)
        counts = await _row_counts(db, truncate)
        total_rows = sum(value for value in counts.values() if value > 0)
        blocking = await _blocking_foreign_keys(db, preserve, set(truncate))

        wipe_dirs = _wipe_targets(args.include_logs)
        files = []
        for relative in wipe_dirs:
            files.extend(_files_under(relative))
        kept_files = []
        for relative in sorted(STORAGE_KEEP):
            kept_files.extend(_files_under(f"{STORAGE_ROOT}/{relative}"))

        print("=" * 70)
        print(f"database           : {database}")
        print(f"schema tables      : {len(tables)}")
        print(f"preserved          : {len(preserve)} -> {', '.join(sorted(preserve))}")
        print(f"to truncate        : {len(truncate)} tables, {total_rows} rows")
        print(f"files to delete    : {len(files)} "
              f"({_count_images(files)} images) under {len(wipe_dirs)} directories")
        if kept_files:
            print(f"fixtures preserved : {STORAGE_ROOT}/"
                  f"{', '.join(sorted(STORAGE_KEEP))} ({len(kept_files)} files)")
        print("=" * 70)

        if args.verbose:
            print("\n  tables:")
            for table in truncate:
                if counts.get(table):
                    print(f"    {table:45s} {counts[table]:>8}")
            print("\n  directories:")
            for relative in wipe_dirs:
                found = _files_under(relative)
                print(f"    {relative:45s} {len(found):>8} file(s)")

        if blocking:
            print()
            print("❌ TRUNCATE would fail: a preserved table references a truncated one.")
            for pair in blocking:
                print(f"     {pair}")
            print("   Add the referenced table to --keep, or widen the preserve set.")
            return 3

        print("\n✅ FK check           : OK — no preserved table references a truncated one")

        if not args.keep_boot_rows:
            missing_boot = BOOT_ROW_TABLES & set(truncate)
            if missing_boot:
                print()
                print("⚠️  WARNING — the application will NOT fully start after this:")
                if "alembic_version" in missing_boot:
                    print("     • alembic_version will be empty; the startup preflight")
                    print("       cannot verify the schema. Recover: alembic stamp head")
                if {"workspaces", "organizations"} & missing_boot:
                    print("     • the default workspace row is removed; conversation")
                    print("       endpoints will answer 500 and nothing re-seeds it.")
                print("     Re-run with --keep-boot-rows to avoid both.")

        if not (args.apply and args.yes_i_understand):
            print("\nDRY RUN — nothing was changed. "
                  "Re-run with --apply --yes-i-understand.")
            return 0

        if not args.no_prompt:
            # `docker exec` without -it gives this process no terminal, so
            # input() would raise EOFError and bury the real problem in a
            # traceback. Say what to do instead. Checked BEFORE prompting so a
            # non-interactive run never half-starts.
            if not sys.stdin.isatty():
                print()
                print("❌ No terminal attached, so the confirmation prompt cannot be")
                print("   answered. Nothing was changed. Either:")
                print("     • add --no-prompt to skip the confirmation, or")
                print("     • run with a TTY:  docker exec -it ...")
                return 4

            print()
            answer = input(f'Type the database name ("{database}") to proceed: ')
            if answer.strip() != database:
                print("aborted.")
                return 1

        # One statement: PostgreSQL truncates them together, so there is no
        # window in which half the tables are empty.
        quoted = ", ".join(f'"{table}"' for table in truncate)
        await db.execute(sa_text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY"))
        await db.commit()
        print(f"\n✅ Truncated {len(truncate)} tables ({total_rows} rows -> 0)")

    deleted = 0
    images = _count_images(files)
    for path in files:
        try:
            os.remove(path)
            deleted += 1
        except OSError as exc:
            print(f"   could not delete {path}: {exc}")
    # Remove the now-empty directory trees too. A camera's pipeline folder is
    # recreated on its next frame, so leaving hundreds of empty UUID directories
    # behind would just be litter.
    for relative in wipe_dirs:
        absolute = os.path.join(REPO, relative)
        if not os.path.isdir(absolute):
            continue
        if relative.startswith(f"{STORAGE_ROOT}/"):
            shutil.rmtree(absolute, ignore_errors=True)
        else:
            for entry in os.listdir(absolute):
                candidate = os.path.join(absolute, entry)
                if os.path.isdir(candidate):
                    shutil.rmtree(candidate, ignore_errors=True)
    print(f"✅ Deleted {deleted} file(s) ({images} images)")
    if kept_files:
        print(f"✅ Preserved {STORAGE_ROOT}/{', '.join(sorted(STORAGE_KEEP))} "
              f"({len(kept_files)} fixture files)")

    if not args.keep_boot_rows:
        print("\nNext steps, because the boot rows were removed:")
        print("   docker exec -w /app/alembic face_recognition_api alembic stamp head")
        print("   (and re-create the default workspace, or restore from a dump)")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="actually truncate; without it this is a dry run")
    parser.add_argument("--yes-i-understand", action="store_true",
                        help="required alongside --apply")
    parser.add_argument("--no-prompt", action="store_true",
                        help="skip the typed database-name confirmation")
    parser.add_argument("--keep-boot-rows", action="store_true",
                        help="also preserve alembic_version, organizations, workspaces, "
                             "workspace_members and the migration-seeded ML config "
                             "so the app still starts and ML suites keep their seeds")
    parser.add_argument("--keep", action="append", metavar="TABLE",
                        help="preserve an additional table (repeatable)")
    parser.add_argument("--include-logs", action="store_true",
                        help="also empty logs/ (destroys the diagnostic record "
                             "of what happened; off by default)")
    parser.add_argument("--verbose", action="store_true",
                        help="list every table and its row count")
    parsed = parser.parse_args()

    from utils.logging import setup_logging
    setup_logging()

    raise SystemExit(asyncio.run(main(parsed)))
