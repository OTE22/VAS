"""Per-assessment decision provenance.

An assessment persisted only `decision_mode` (= the mode that EXECUTED:
"shadow" in shadow, "rules" when a gated mode fell back), so a HYBRID/ML
request that fell back was indistinguishable from a plain RULES run, and
nothing recorded which input the anomaly signal came from, which mapping
policy turned it into risk points, or why a fallback happened. Truthful
history needs these persisted WITH the assessment — never reconstructed
from today's configuration.

threat_assessments (all nullable, additive; NULL on rows before this
revision = "not recorded"):
  requested_mode          the administrator's configured mode at the time
  anomaly_signal_source   rules | ml — which input fed behavioral_anomalies
  signal_mapping_version  the validated ML->risk policy used (ml only)
  fallback_reason         stable FallbackReason code when ML could not serve

`decision_mode` keeps its meaning (executed mode).

Revision ID: c3e8a1f5d7b2
Revises: b7d2f4a9c6e1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3e8a1f5d7b2"
down_revision: Union[str, None] = "b7d2f4a9c6e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("threat_assessments", sa.Column("requested_mode", sa.String(16), nullable=True))
    op.add_column("threat_assessments", sa.Column("anomaly_signal_source", sa.String(8), nullable=True))
    op.add_column("threat_assessments", sa.Column("signal_mapping_version", sa.String(64), nullable=True))
    op.add_column("threat_assessments", sa.Column("fallback_reason", sa.String(64), nullable=True))


def downgrade() -> None:
    for column in ("fallback_reason", "signal_mapping_version", "anomaly_signal_source", "requested_mode"):
        op.drop_column("threat_assessments", column)
