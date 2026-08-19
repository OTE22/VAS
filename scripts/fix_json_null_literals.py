"""Rewrite stored JSON `null` literals to SQL NULL.

    docker exec face_recognition_api python3 /app/scripts/fix_json_null_literals.py [--apply]

A JSON/JSONB column holding the literal `null` is not the same as an empty
column: `IS NULL` is false for it and `IS NOT NULL` is true, so every SQL
filter, count and dashboard reads it as a value that is present. The writers
now store SQL NULL (see the JSONB subclass in db_models.py); this repairs the
rows written before that.

Discovers the affected columns rather than hard-coding them, so it stays
correct as the schema changes. Dry-run by default: it reports what it would
change and touches nothing without --apply.

Only ever rewrites `null` to NULL. It cannot damage a real value: the WHERE
clause matches the literal four-character JSON null and nothing else.
"""
import argparse
import asyncio
import sys

sys.path.insert(0, "/app")


async def scan(db, sa_text):
    """(table, column, count) for every JSON/JSONB column holding JSON null."""
    columns = (await db.execute(sa_text("""
        SELECT table_name, column_name
          FROM information_schema.columns
         WHERE table_schema = 'public' AND data_type IN ('json', 'jsonb')
         ORDER BY table_name, column_name"""))).all()

    affected = []
    for table, column in columns:
        n = (await db.execute(sa_text(
            f'SELECT count(*) FROM "{table}" WHERE "{column}"::text = \'null\''
        ))).scalar()
        if n:
            affected.append((table, column, n))
    return affected


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="perform the update (default: report only)")
    args = parser.parse_args()

    from db_connection import db_manager
    from sqlalchemy import text as sa_text

    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()

    async with db_manager.get_session() as db:
        affected = await scan(db, sa_text)

        if not affected:
            print("No JSON null literals found. Nothing to do.")
            return 0

        total = sum(n for _, _, n in affected)
        print(f"{len(affected)} column(s), {total} row(s) holding JSON null:")
        for table, column, n in affected:
            print(f"  {table}.{column}: {n}")

        if not args.apply:
            print("\nDry run. Re-run with --apply to rewrite these to SQL NULL.")
            return 0

        changed = 0
        for table, column, _ in affected:
            result = await db.execute(sa_text(
                f'UPDATE "{table}" SET "{column}" = NULL '
                f'WHERE "{column}"::text = \'null\''))
            changed += result.rowcount or 0
        await db.commit()
        print(f"\nRewrote {changed} value(s) to SQL NULL.")

        # Prove it, rather than trusting the rowcount.
        remaining = await scan(db, sa_text)
        if remaining:
            print("STILL PRESENT (unexpected):")
            for table, column, n in remaining:
                print(f"  {table}.{column}: {n}")
            return 1
        print("Verified: no JSON null literals remain.")
    return 0


sys.exit(asyncio.run(main()))
