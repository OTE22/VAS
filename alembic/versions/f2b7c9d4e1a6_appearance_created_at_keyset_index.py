"""Keyset index for the ML feature collector.

The collector reads identity_appearances with exactly one shape:

    WHERE created_at > :window_start [AND (created_at, id) > :cursor]
    ORDER BY created_at, id LIMIT :batch

Without an index on that ordering every batch is a full parallel
sequential scan plus a sort (measured: 6 265 buffers / 160-190 ms per
batch at 275 k rows, growing linearly with the table) - a 300 k backlog is
15 full scans. A composite (created_at, id) btree serves the range scan in
index order with no sort; a plain (created_at) index would still sort on
the id tie-break that keyset pagination depends on.

Built CONCURRENTLY so a live table is never write-locked while the index
is created (the migration runs at application start-up).

Revision ID: f2b7c9d4e1a6
Revises: e6a1c4d8b2f7
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f2b7c9d4e1a6"
down_revision: Union[str, None] = "e6a1c4d8b2f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX = "idx_appearance_created_at_id"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
                   "ON identity_appearances (created_at, id)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
