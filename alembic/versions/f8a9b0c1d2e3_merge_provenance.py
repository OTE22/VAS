"""Merge provenance — record what a merge moved, so an unmerge can exist

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-08-06

A merge soft-deletes the loser (status=MERGED, merged_into_id=winner) and then
re-parents its appearances, embeddings, faces and — as of this change — gallery
images onto the winner with plain UPDATEs. Those UPDATEs overwrite identity_id
in place: before this column, nothing recorded WHICH rows had belonged to the
loser, so no future unmerge could ever know what to give back.

One nullable JSONB column on the table that already records who merged what,
when, and why. The shape written by identity_service._consolidate_loser_assets:

    {
      "appearance_ids": [...],
      "embedding_ids":  [...],
      "face_ids":       [...],
      "images": [
        {"id": 205,
         "original_path": "storage/faces/<loser>/image_001.jpg",
         "new_path":      "storage/faces/<winner>/image_003.jpg",
         "file_copied":   true,
         "was_primary":   false,
         "deduplicated_into": null}
      ],
      "loser_type": "unknown",
      "loser_display_name": "..."
    }

Deliberately NOT a new table: identity_merges already carries winner, loser,
acting admin, timestamp and notes, and one merge produces one provenance blob.
Rows written before this migration keep provenance NULL — honest, since nothing
was recorded for them and inventing it retroactively would be a lie.

This migration records; it does not restore. There is no unmerge endpoint yet,
and downgrade simply drops the column (losing only the recordings, never the
merged data itself).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'f8a9b0c1d2e3'
down_revision = 'e7f8a9b0c1d2'
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table):
    return _inspector().has_table(table)


def _has_column(table, column):
    if not _has_table(table):
        return False
    return column in {c["name"] for c in _inspector().get_columns(table)}


def upgrade() -> None:
    if _has_table("identity_merges") and not _has_column("identity_merges", "provenance"):
        op.add_column(
            "identity_merges",
            sa.Column("provenance", postgresql.JSONB(), nullable=True),
        )


def downgrade() -> None:
    if _has_column("identity_merges", "provenance"):
        op.drop_column("identity_merges", "provenance")
