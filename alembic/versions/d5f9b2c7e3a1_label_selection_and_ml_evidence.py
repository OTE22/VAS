"""Review-selection metadata on labels.

Future mapping validation may deliberately review more unusual / highly
unusual predictions. A reviewed subset selected that way must never be read
as natural prevalence, so the label records HOW it was selected:
{method, band, sampling_probability, reason, selected_at}. NULL = recorded
without explicit selection metadata (treated as natural, and said so).

Revision ID: d5f9b2c7e3a1
Revises: c3e8a1f5d7b2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "d5f9b2c7e3a1"
down_revision: Union[str, None] = "c3e8a1f5d7b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ml_labels", sa.Column("selection", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("ml_labels", "selection")
