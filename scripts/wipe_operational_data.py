"""
Wipe all operational data, keep the accounts and the migration-seeded rows.

    # from the host, with the API stopped:
    docker stop face_recognition_api
    docker exec -w /app face_recognition_db_or_api \
        python scripts/wipe_operational_data.py --apply --yes-i-understand
    docker start face_recognition_api

Standing rule: run this after every full regression pass. The suite leaves
fixture rows behind — ~12 `qa_multiimg_*` identities, a `qa_gate_other_admin`
user, enrolled faces, search history — and that residue accumulates until the
ambient-data assertions in tests/test_upload_match_integrity.py and
tests/test_conversation_domain.py fail on the NEXT run for reasons unrelated to
any code change. Cleaning after each run keeps those tests meaningful.

Design
------
* **The truncate list is DERIVED from the live schema**, never hard-coded: every
  table except the preserve set below. A table added by a future migration is
  therefore wiped automatically instead of being silently missed by a stale list.
* **One TRUNCATE, no CASCADE.** PostgreSQL refuses to truncate a table that
  something outside the list references, so omitting CASCADE turns "the list is
  wrong" into a loud error rather than a silently wider wipe. The FK graph is
  checked explicitly first, so the failure is explained before it happens.
* **Dry-run is the default.** `--apply` additionally requires
  `--yes-i-understand`. There is no backup step — this is unrecoverable.

Why these tables are preserved
------------------------------
`alembic_version` is the schema pointer, not data; emptying it makes Alembic
believe no migration ever ran. The other five hold rows inserted by migrations
that are already stamped and will never re-run, and no application code
re-creates them:

* `organizations` + `workspaces` — `conversation_service.get_default_workspace_id`
  raises RuntimeError without the default workspace row, and both
  /api/conversations handlers call it unconditionally. Deleting these kills the
  chatbot for every user with no recovery path.
* `ml_retraining_policies` — /api/ml/retraining-policy/{type} 404s for all four
  model types; no auto-create.
* `ml_feature_definitions` — seeded ONLY by Alembic (frozen literal in migration
  d4e5f6a7b8c9); boot verifies FEATURE_INVENTORY against the table and fails
  closed if they differ, so wiping them would stop the API from starting.

`users` is never a foreign-key CHILD, so keeping it blocks nothing. RESTART
IDENTITY touches only truncated tables, so user ids never shift — which matters
because identity_audit_log.user_id is a NOT NULL FK to the `system` account.

Known, accepted consequence
---------------------------
`risk_model_versions` is NOT preserved. Its 3 migration-seeded rows
(identity_threat / network_node / movement_map, all risk-engine-v1) are gone for
good, so tests/test_risk_platform.py::test_migration_applied_and_indexed fails
permanently (`assert 0 >= 3`). Risk scoring still works: `risk_engine.get_model`
falls back to the compiled DEFAULT_MODELS at the same version.

This is the owner's explicit decision. They were offered the one-line restore
("turns the last test green, no behaviour change") and declined it. Do not add
this table to PRESERVE, do not re-seed it, and do not re-raise the failing test
as a bug — it is expected output, not a regression.
"""

import argparse
import asyncio
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text as sa_text  # noqa: E402

# Kept intentionally. Every entry has a reason in the module docstring above;
# do not add to this set without one.
PRESERVE = {
    "alembic_version",
    "users",
    "organizations",
    "workspaces",
    "workspace_members",
    "ml_retraining_policies",
    "ml_feature_definitions",
}

# Directories emptied alongside the rows that point at them. Nothing in the
# codebase reconciles orphaned files against missing rows, so this is the only
# thing that removes them.
WIPE_DIRS = (
    "storage/faces",
    "storage/pending",
    "storage/debug/cropped",
    "storage/debug/webhook_images",
    "models/ml/candidates",
    "models/ml/datasets",
)

# Accounts the suite creates and never cleans up. `users` is preserved WHOLESALE,
# so without this they survive every wipe and accumulate — and because cleaning
# now runs after every regression pass, the suite re-creates them on each cycle.
# Removed only AFTER the truncate: by then every other table referencing
# `users` is empty, leaving `workspace_members` as the single FK to clear first.
FIXTURE_USERS = ("qa_gate_other_admin",)

# Real accounts. A bug in FIXTURE_USERS must never be able to remove these.
PROTECTED_USERS = ("admin", "OTE22", "system")

# Regression fixtures, not data. Deleting these breaks the quality suite.
PRESERVE_DIRS = ("storage/qa-quality-regression",)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def _plan(db):
    """(tables_to_truncate, blockers) derived from the LIVE schema."""
    tables = {r[0] for r in (await db.execute(sa_text(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'"))).all()}

    unknown = PRESERVE - tables
    if unknown:
        raise SystemExit(
            f"REFUSING: preserve-list names absent from the schema: {sorted(unknown)}. "
            "The list is stale — fix it before wiping anything.")

    truncate = sorted(tables - PRESERVE)

    fks = (await db.execute(sa_text("""
        SELECT tc.table_name AS child, ccu.table_name AS parent
        FROM information_schema.table_constraints tc
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema='public'
    """))).all()

    # The condition that makes TRUNCATE-without-CASCADE fail: a table we are
    # KEEPING that references a table we are TRUNCATING.
    blockers = sorted({(c, p) for c, p in fks if c not in truncate and p in truncate})
    return truncate, blockers


async def _drop_fixture_users(db):
    """Remove suite-created accounts. Returns the usernames actually deleted.

    Guarded by PROTECTED_USERS so a bad edit to FIXTURE_USERS cannot delete a
    real account — `system` in particular is a NOT NULL FK target for
    identity_audit_log.user_id, and losing it is unrecoverable.
    """
    doomed = [u for u in FIXTURE_USERS if u not in PROTECTED_USERS]
    if not doomed:
        return []

    rows = (await db.execute(sa_text(
        "SELECT id, username FROM users WHERE username = ANY(:names)"),
        {"names": list(doomed)})).all()
    if not rows:
        return []

    ids = [r[0] for r in rows]
    # The only surviving FK to `users` once the truncate has run.
    await db.execute(sa_text(
        "DELETE FROM workspace_members WHERE user_id = ANY(:ids)"), {"ids": ids})
    await db.execute(sa_text(
        "DELETE FROM users WHERE id = ANY(:ids)"), {"ids": ids})
    await db.commit()
    return [r[1] for r in rows]


async def _counts(db, tables):
    total = 0
    for table in tables:
        total += (await db.execute(sa_text(f'SELECT count(*) FROM "{table}"'))).scalar() or 0
    return total


def _file_plan():
    """(paths_to_delete, kept) — resolved against the repo, existence-checked."""
    doomed = []
    for rel in WIPE_DIRS:
        base = os.path.join(REPO, rel)
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            doomed.append(os.path.join(base, entry))
    kept = [os.path.join(REPO, rel) for rel in PRESERVE_DIRS
            if os.path.isdir(os.path.join(REPO, rel))]
    return doomed, kept


def _count_files(paths):
    n = 0
    for path in paths:
        if os.path.isfile(path):
            n += 1
        else:
            for _root, _dirs, names in os.walk(path):
                n += len(names)
    return n


async def main(args) -> int:
    from db_connection import db_manager
    await db_manager.init_db()

    async with db_manager.get_session() as db:
        truncate, blockers = await _plan(db)
        before = await _counts(db, truncate)
        doomed, kept = _file_plan()
        file_count = _count_files(doomed)

        print(f"schema tables      : {len(truncate) + len(PRESERVE)}")
        print(f"preserved          : {len(PRESERVE)} -> {', '.join(sorted(PRESERVE))}")
        print(f"to truncate        : {len(truncate)} tables, {before} rows")
        print(f"files to delete    : {file_count} under {len(WIPE_DIRS)} directories")
        print(f"fixtures preserved : {', '.join(PRESERVE_DIRS)}")

        if blockers:
            print("\nREFUSING — a preserved table references a table marked for truncation:")
            for child, parent in blockers:
                print(f"  {child} -> {parent}")
            print("TRUNCATE would fail (or need CASCADE, which could widen the wipe).")
            return 2
        print("\nFK check           : OK — no preserved table references a truncated one")

        if not args.apply:
            print("\nDRY RUN — nothing was changed. "
                  "Re-run with --apply --yes-i-understand.")
            return 0
        if not args.yes_i_understand:
            print("\nREFUSING: --apply also requires --yes-i-understand.")
            return 1
        if not args.no_prompt:
            if input(f"\nDelete {before} rows and {file_count} files? [type DELETE] ") != "DELETE":
                print("Aborted.")
                return 1

        quoted = ", ".join(f'"{t}"' for t in truncate)
        await db.execute(sa_text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY"))
        await db.commit()

        after = await _counts(db, truncate)
        if after:
            print(f"\nFAILED: {after} rows survived the truncate.")
            return 3
        print(f"\n✅ Truncated {len(truncate)} tables ({before} rows -> 0)")

        dropped = await _drop_fixture_users(db)
        if dropped:
            print(f"✅ Removed fixture users: {', '.join(dropped)}")
        else:
            print("✅ No fixture users present")

    # Files only after the transaction has committed: a rolled-back wipe with
    # the images already gone is the worse of the two failure modes.
    deleted = 0
    for path in doomed:
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
                deleted += 1
            else:
                deleted += _count_files([path])
                shutil.rmtree(path)
        except Exception as exc:                                   # noqa: BLE001
            print(f"  ! could not remove {path}: {exc}")
    print(f"✅ Deleted {deleted} file(s)")
    for path in kept:
        print(f"✅ Preserved {os.path.relpath(path, REPO)} "
              f"({_count_files([path])} fixture files)")

    print("\nStart the API again; settings re-seed from config.py on the next "
          "GET /api/settings.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="actually wipe; without it this is a dry run")
    parser.add_argument("--yes-i-understand", action="store_true",
                        help="required alongside --apply; there is no backup")
    parser.add_argument("--no-prompt", action="store_true",
                        help="skip the interactive confirmation")
    sys.exit(asyncio.run(main(parser.parse_args())))
