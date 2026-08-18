"""Record which scorer produced each stored quality value

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-08-02

The detection path computed `quality` from only two of the four terms
`compute_quality_score` weights — and from the wrong inputs at that: `face_size`
came from the UPSTREAM PERSON bounding box, not a face box, so the size term
saturated and the score collapsed to `0.3 + 0.2 * upstream_confidence`. Any
confidence below 1.0 therefore produced < 0.5, which the KNOWN threshold
(default 0.5) rejected outright. Known people were not accumulating embeddings
from camera detections at all, and the values that were stored carried no
image-quality information — they were a linear re-encoding of detector
confidence.

The scorer now measures blur, lighting, absolute face resolution and pose from
the real SCRFD face crop, so the numbers mean something different than before.

**Old values are not converted.** No monotone map exists from
`0.3 + 0.2 * confidence` to a blur/lighting/pose score, so any conversion would
be invented data. The column added here records provenance instead, exactly as
`identity_embeddings.embedding_model_version` already does: NULL means "produced
by the legacy scorer, semantics unknown". Cross-row comparisons (the
best-snapshot choice in `create_appearance`, the clustering quality feature) are
scoped to a single scorer version so old and new values are never ranked against
each other.

Nullable with no default and no backfill — a value invented here would be
indistinguishable from a measured one forever after.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b4c5d6e7f8a9'
down_revision = 'a3b4c5d6e7f8'
branch_labels = None
depends_on = None

_COLUMN = 'quality_scorer_version'
_TABLES = ('identity_embeddings', 'identity_images')


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {row[0] for row in bind.execute(sa.text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :t"), {"t": table})}


def upgrade() -> None:
    for table in _TABLES:
        if not _has_column(table, _COLUMN):
            op.add_column(table, sa.Column(_COLUMN, sa.String(32), nullable=True))

    # Rows that predate this migration keep NULL. Deliberately no UPDATE here:
    # stamping them with the current version would claim they were measured by a
    # scorer that did not exist when they were written.


def downgrade() -> None:
    """Drops only the provenance column.

    `quality` itself is never touched in either direction — a downgrade may undo
    a schema change, never a measurement.
    """
    for table in _TABLES:
        if _has_column(table, _COLUMN):
            op.drop_column(table, _COLUMN)
