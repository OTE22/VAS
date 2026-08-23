"""Request-id correlation on the ML audit log.

Every ML-Ops call writes a [MLOPS_CALL] log line carrying the request id,
and every job row (background_task_history) can carry one - but the
durable ml_audit_log row could not be joined to either. This adds the
nullable column (filled from the request context by the writer; NULL for
CLI / background actions) and an index for lookups by request.

Revision ID: e6a1c4d8b2f7
Revises: d5f9b2c7e3a1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e6a1c4d8b2f7"
down_revision: Union[str, None] = "d5f9b2c7e3a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ml_audit_log", sa.Column("request_id", sa.String(length=64), nullable=True))
    op.create_index("idx_ml_audit_request", "ml_audit_log", ["request_id"])


def downgrade() -> None:
    op.drop_index("idx_ml_audit_request", table_name="ml_audit_log")
    op.drop_column("ml_audit_log", "request_id")
