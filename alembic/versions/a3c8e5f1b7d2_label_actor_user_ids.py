"""Reliable reviewer identity on labels.

ml_labels recorded its creator and reviewer as usernames only. A username is
unique at a time but is not an identity across renames/re-creation, so a
future separation-of-duties policy (ML_EVIDENCE_REQUIRE_INDEPENDENT_REVIEW)
and the self_reviewed evidence population need the user ids. Nullable:
rows written before this revision keep their usernames and are compared by
username; CLI/seed writers have no user id.

Revision ID: a3c8e5f1b7d2
Revises: f2b7c9d4e1a6
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3c8e5f1b7d2"
down_revision: Union[str, None] = "f2b7c9d4e1a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ml_labels", sa.Column("created_by_user_id", sa.Integer(), nullable=True))
    op.add_column("ml_labels", sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("ml_labels", "reviewed_by_user_id")
    op.drop_column("ml_labels", "created_by_user_id")
