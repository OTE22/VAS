"""secintel-features-v2: point-in-time identity type + exact last-seen.

Two defects of the frozen v1 feature set are fixed under a NEW feature-set
version (backend/ml/constants.FEATURE_SET_VERSION = "secintel-features-v2"):

  is_unknown_identity   v1 read the identity's CURRENT type at compute time,
                        so an identity promoted or merged after a snapshot's
                        as_of was labelled with hindsight. v2 reconstructs the
                        type AS OF the snapshot from the promote/merge audit
                        trail (identity_audit_log) — computation
                        `is_unknown_identity_as_of`.
  days_since_last_seen  v1 took the last row of a LIMIT-5000 ascending read of
                        the 90-day window, stale for busy identities. v2 uses
                        an exact MAX(start_time) < as_of aggregate —
                        computation `days_since_last_seen_exact`.

Definitions are frozen literals seeded only by Alembic (see d4e5f6a7b8c9).
This revision DEACTIVATES the two v1 definition rows (kept for the record —
v1 snapshots/datasets/models keep referencing them) and inserts the two v2
rows. Every other definition is unchanged and stays at version 1; the SET
version moves to v2 because the windowed builders now also refuse
(`appearance_window_truncated`) instead of silently undercounting when the
90-day window exceeds the row cap.

Nothing existing is rewritten: v1 snapshots, datasets, models, thresholds
and shadow comparisons keep their v1 label and remain valid for v1. A v2
model must be trained on v2 snapshots and shadow-approved explicitly.

Revision ID: b7d2f4a9c6e1
Revises: a9c4e2d7f1b3
"""
import uuid as _uuid
from datetime import datetime as _dt
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d2f4a9c6e1"
down_revision: Union[str, None] = "a9c4e2d7f1b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

V2_DEFINITIONS = [
    {"name": "days_since_last_seen", "version": 2, "entity_type": "person",
     "value_type": "float", "window": None, "source": "identity_appearances",
     "computation": "days_since_last_seen_exact", "params": {},
     "leakage_class": "safe", "is_active": True,
     "description": "Gap between MAX(start_time) < as_of and as_of (days), exact"},
    {"name": "is_unknown_identity", "version": 2, "entity_type": "person",
     "value_type": "float", "window": None, "source": "identities+identity_audit_log",
     "computation": "is_unknown_identity_as_of", "params": {},
     "leakage_class": "safe", "is_active": True,
     "description": "1.0 when the identity was UNKNOWN as of the snapshot (promote/merge "
                    "audit trail); known-since-creation identities are 0.0"},
]
SUPERSEDED_V1 = {
    "days_since_last_seen": "Gap between the last pre-as_of appearance and as_of (days) "
                            "(SUPERSEDED by v2: stale beyond the 5000-row window cap)",
    "is_unknown_identity": "Identity type at compute time (SUPERSEDED by v2: point-in-time violation)",
}


def _table():
    return sa.table(
        "ml_feature_definitions",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String), sa.column("version", sa.Integer),
        sa.column("entity_type", sa.String), sa.column("value_type", sa.String),
        sa.column("window", sa.String), sa.column("source", sa.String),
        sa.column("computation", sa.String), sa.column("params", postgresql.JSONB),
        sa.column("leakage_class", sa.String),
        sa.column("readiness_requirements", postgresql.JSONB),
        sa.column("description", sa.Text), sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime), sa.column("deactivated_at", sa.DateTime),
    )


def upgrade() -> None:
    bind = op.get_bind()
    defs = _table()
    for name, description in SUPERSEDED_V1.items():
        bind.execute(
            defs.update().where(sa.and_(defs.c.name == name, defs.c.version == 1))
            .values(is_active=False, deactivated_at=_dt.utcnow(), description=description))
    for row in V2_DEFINITIONS:
        # readiness_requirements is left unset: SQL NULL, never a JSON null literal
        bind.execute(postgresql.insert(defs).values(
            id=_uuid.uuid4(), created_at=_dt.utcnow(), **row
        ).on_conflict_do_nothing(index_elements=["name", "version"]))


def downgrade() -> None:
    """Re-activates the v1 rows and removes the v2 rows. v2 snapshots are
    left in place (they carry their own feature_set_version label)."""
    bind = op.get_bind()
    defs = _table()
    for row in V2_DEFINITIONS:
        bind.execute(defs.delete().where(sa.and_(defs.c.name == row["name"], defs.c.version == 2)))
    for name in SUPERSEDED_V1:
        bind.execute(defs.update().where(sa.and_(defs.c.name == name, defs.c.version == 1))
                     .values(is_active=True, deactivated_at=None))
