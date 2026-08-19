"""A JSON column meaning "nothing" must hold SQL NULL, not the literal `null`.

They are not interchangeable in the database:

    WHERE col IS NULL      -- FALSE for a stored JSON null
    WHERE col IS NOT NULL  -- TRUE  for a stored JSON null

so a column that means "no value" reads back as a present value to every SQL
filter, count and dashboard. 474 rows across ten columns were in that state.

The clearest proof it was accidental rather than intended: two sibling tables
in the same subsystem, same column name, same 241 rows —
`ml_predictions.missing_features` held 241 SQL NULLs while
`ml_shadow_comparisons.missing_features` held 241 JSON nulls. The inference
writer omitted the key when empty (and left a comment warning about exactly
this); the shadow writer wrote `result.missing_features or None`, and passing
Python None to a JSONB column is what stores the literal.

Fixed on the TYPE (`db_models.JSONB`, none_as_null=True) so no column
declaration and no writer has to remember.
"""

import pytest

from conftest import run_on_shared_loop as run_async  # asyncpg is loop-bound

MARK = "pytest-jsonnull"


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


def test_the_jsonb_type_maps_none_to_sql_null():
    """Every mapped JSON/JSONB column, not a sample of them."""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.dialects.postgresql import JSONB as PGJSONB
    import db_models

    offenders = []
    for mapper in db_models.Base.registry.mappers:
        table = mapper.local_table
        if table is None:
            continue
        for column in table.columns:
            if isinstance(column.type, PGJSONB) and not column.type.none_as_null:
                offenders.append(f"{table.name}.{column.name}")
    assert not offenders, (
        "these JSONB columns still store the JSON literal null for a Python "
        f"None: {offenders}")


def test_writing_none_stores_sql_null_not_the_literal():
    """The behaviour itself, through the ORM and read back in SQL."""
    async def _run():
        from db_connection import db_manager
        from db_models import SearchHistory, SearchType
        from sqlalchemy import text as sa_text
        import uuid

        await _ensure_db()
        row_id = uuid.uuid4()
        async with db_manager.get_session() as db:
            db.add(SearchHistory(
                id=row_id, user_id=None, search_type=SearchType.SINGLE,
                scope="both", watchlist_alerts_count=0,
                filters=None, exclude_identity_ids=None,
                input_quality_scores=None))
            await db.commit()

            # coalesce: for a SQL NULL, `col::text = 'null'` is itself NULL,
            # not false — three-valued logic, and the whole point of the bug.
            probe = (await db.execute(sa_text("""
                SELECT (filters IS NULL) AS is_sql_null,
                       coalesce(filters::text = 'null', false) AS is_json_null,
                       (exclude_identity_ids IS NULL),
                       (input_quality_scores IS NULL)
                  FROM search_history WHERE id = :i"""), {"i": row_id})).first()

            await db.execute(sa_text("DELETE FROM search_history WHERE id = :i"),
                             {"i": row_id})
            await db.commit()
        return probe

    is_sql_null, is_json_null, excl_null, quality_null = run_async(_run())
    assert is_sql_null is True, "None was stored as the JSON literal null"
    assert is_json_null is False
    assert excl_null is True and quality_null is True


def test_the_shadow_writer_no_longer_stores_the_literal():
    """The exact line that produced 241 of the 474: `x or None` into JSONB."""
    with open("/app/backend/ml/shadow_service.py", encoding="utf-8") as handle:
        source = handle.read()
    assert "missing_features=result.missing_features or None" in source, (
        "the writer changed shape; re-check that it still cannot store a JSON "
        "null now that the type, not the call site, is what guarantees it")
    # It is safe BECAUSE of the type, so assert the type is what makes it safe.
    from db_models import MLShadowComparison
    assert MLShadowComparison.__table__.c.missing_features.type.none_as_null is True


def test_no_json_null_literals_remain_anywhere():
    """Whole-database sweep: the repaired state, and a guard against a new
    writer or an import reintroducing them."""
    async def _run():
        from db_connection import db_manager
        from sqlalchemy import text as sa_text
        await _ensure_db()
        found = []
        async with db_manager.get_session() as db:
            columns = (await db.execute(sa_text("""
                SELECT table_name, column_name FROM information_schema.columns
                 WHERE table_schema='public' AND data_type IN ('json','jsonb')
                 ORDER BY 1,2"""))).all()
            for table, column in columns:
                n = (await db.execute(sa_text(
                    f'SELECT count(*) FROM "{table}" WHERE "{column}"::text = '
                    "'null'"))).scalar()
                if n:
                    found.append(f"{table}.{column}={n}")
        return found

    found = run_async(_run())
    assert not found, f"JSON null literals present: {found}"


def test_the_repair_script_is_committed_and_dry_by_default():
    """It rewrites data, so it must not act without being asked."""
    import ast
    path = "/app/scripts/fix_json_null_literals.py"
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    ast.parse(source)
    assert '"--apply"' in source, "no explicit opt-in flag"
    assert "if not args.apply:" in source, "the script is not dry-run by default"
    # It must only ever match the literal, never a real value.
    assert source.count("::text = \\'null\\'") + source.count('::text = \'null\'') >= 1
