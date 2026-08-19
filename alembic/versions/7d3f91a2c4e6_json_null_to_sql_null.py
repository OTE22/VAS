"""Stored JSON `null` literals become SQL NULL.

A JSON/JSONB column holding the four-character literal `null` is not an empty
column, and SQL does not treat the two alike:

    WHERE col IS NULL      -- FALSE for a stored JSON null
    WHERE col IS NOT NULL  -- TRUE  for a stored JSON null

so a column meaning "nothing here" reads back as a present value to every
filter, count and dashboard that asks in SQL. 474 values across ten columns
were in that state.

The clearest evidence it was never intended: two sibling tables in the same
subsystem, same column name, same 241 rows — `ml_predictions.missing_features`
held 241 SQL NULLs while `ml_shadow_comparisons.missing_features` held 241
JSON nulls. The inference writer omitted the key when empty and left a comment
warning about the literal; the shadow writer wrote
`result.missing_features or None`, and passing Python None to a JSONB column is
what stores it.

Two halves, because the rows come from two places:

  * writers  — `db_models.JSONB` now sets `none_as_null=True`, so no column
               declaration and no writer has to remember;
  * seeding  — `d4e5f6a7b8c9` seeds ml_feature_definitions through its own
               `sa.column(..., postgresql.JSONB)`, outside the ORM, and 20 of
               the frozen rows carry `readiness_requirements: None`. That
               migration is applied history and is not edited; this one runs
               after it, so a database built from scratch also ends up clean.

Discovers the columns instead of listing them, so it repairs whatever a given
database actually holds. Only ever rewrites the literal — no real value can
match `::text = 'null'`.

Revision ID: 7d3f91a2c4e6
Revises: f6a7b8c9d0e1
"""
from alembic import op

revision = '7d3f91a2c4e6'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None

_FIND = """
    SELECT table_name, column_name
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND data_type IN ('json', 'jsonb')
     ORDER BY table_name, column_name
"""


def upgrade() -> None:
    bind = op.get_bind()
    for table, column in bind.exec_driver_sql(_FIND).fetchall():
        bind.exec_driver_sql(
            f'UPDATE "{table}" SET "{column}" = NULL '
            f"WHERE \"{column}\"::text = 'null'")


def downgrade() -> None:
    """No inverse.

    SQL NULL and JSON null are distinguishable going in, but once the literals
    are gone there is nothing recording which columns used to hold them —
    restoring them would mean guessing, and would put back the exact defect
    this removes. The column types still permit a JSON null for any writer that
    asks for one explicitly (`sqlalchemy.JSON.NULL`), so nothing is lost.
    """
