"""Covering index for the Advanced Search camera filter.

Why this migration exists
-------------------------
`/api/search/advanced` accepts a `pipeline_id` filter that was never applied to
any query — it was threaded through the service and written to
`SearchHistory.filters`, and that was all. Implementing it adds a correlated
EXISTS over `identity_appearances`:

    EXISTS (SELECT 1 FROM identity_appearances a
             WHERE a.identity_id = identities.id
               AND a.pipeline_id = :pipeline_id)

The outer query pins a small candidate set (`identities.id IN (...)`, bounded by
the vector-index fan-out), so the planner drives from `identities` and
semi-joins. The existing indexes do not serve that shape well:

  * `idx_appearance_pipeline` is (pipeline_id, start_time) — no `identity_id`,
    so it can only be used by driving from the camera side, which would
    materialise every appearance row for that camera: the wrong plan.
  * `idx_appearance_identity_start` is (identity_id, start_time) — usable as the
    driver, but `pipeline_id` is not in it, so every candidate needs a heap
    fetch to test the predicate. Worst case that is an identity's entire
    appearance history, for an identity that then fails the filter — the common
    case whenever a camera is selected.

(identity_id, pipeline_id) lets the semi-join answer from the index alone and
stop at the first matching row.

Reversible: creates/drops one index, no data change.
"""

from alembic import op

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None

_INDEX = "idx_appearance_identity_pipeline"
_TABLE = "identity_appearances"


def upgrade() -> None:
    # IF NOT EXISTS: this index is also declared in db_models.__table_args__,
    # so a database created by metadata.create_all() already has it.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_INDEX} ON {_TABLE} (identity_id, pipeline_id)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
