"""Pending enrollment decisions — the claim ticket for a two-phase upload

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-08-03

Enrollment used to mint a new identity UUID for any name it had not seen
before, with no similarity check anywhere in the flow: uploading a second photo
of an already-enrolled person under a new spelling produced two UUIDs holding
two embeddings of one face, and recognition then answered with whichever vector
scored higher. This table is what lets the server stop and ask instead.

An upload that looks like somebody we already have is parked here — file on
disk under STORAGE_DIR/pending, row here — and NOTHING durable is written until
an administrator confirms: no identity, no identity_images row, no
identity_embeddings row, no gallery folder, no vector-index entry.

WHY POSTGRES AND NOT REDIS. Redis is optional in this deployment —
backend/lifespan.py continues startup when it is unreachable and logs the cache
as non-critical — so a Redis-only store would silently disable the whole review
gate on a running system, and the in-process fallback used by
backend/auth/auth_security.py is per-worker. A claim ticket that can evaporate
is worse than no ticket at all: the upload it guards is already on disk.

token_hash holds SHA-256 of the token, never the token. The raw value is
returned to the client exactly once and is not recoverable from this table.

The row is CONSUMED BY DELETE (`DELETE ... WHERE token_hash = :h AND user_id =
:u AND expires_at > :now RETURNING *`), which is what makes one-time use
race-free without a flag or a lock: a second concurrent confirm matches zero
rows. `candidates` freezes the list actually offered, so a confirmation cannot
name an identity that was never shown.

The two model-version stamps are not decoration. The embedding is recomputed
from the same bytes at confirmation; if the recognition or detection weights
changed between the two phases, the vector being approved is not the vector the
candidates were ranked against, and the decision must be refused rather than
silently applied under new models.

Downgrade drops the table. That loses claim tickets, never data: every pending
row is a 15-minute intent to enroll that an administrator can simply repeat by
uploading again, and the files they reference are removed by the sweeper.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'd6e7f8a9b0c1'
down_revision = 'c5d6e7f8a9b0'
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table):
    return _inspector().has_table(table)


def _has_index(table, name):
    if not _has_table(table):
        return False
    return name in {ix["name"] for ix in _inspector().get_indexes(table)}


def upgrade() -> None:
    if not _has_table("pending_enrollments"):
        op.create_table(
            "pending_enrollments",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),

            # SHA-256 of the upload token. The token itself is never stored.
            sa.Column("token_hash", sa.String(64), nullable=False),

            # The decision is bound to the administrator who uploaded it: nobody
            # else may approve, reject or even observe it.
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False),

            # display_name is what the operator typed; display_name_key is its
            # normalized/casefolded form, bound so a confirmation cannot quietly
            # retarget the upload at a different person than the one reviewed.
            sa.Column("display_name", sa.String(255), nullable=False),
            sa.Column("display_name_key", sa.String(255), nullable=False),

            # Relative, e.g. storage/pending/<random>.jpg — same convention as
            # identity_images.storage_path. Outside the gallery by construction.
            sa.Column("storage_path", sa.String(512), nullable=False),
            sa.Column("original_filename", sa.String(255), nullable=True),

            # SHA-256 of the ORIGINAL bytes: rebound at confirmation so the file
            # that gets enrolled is provably the file that was reviewed.
            sa.Column("file_checksum", sa.String(64), nullable=False),
            sa.Column("content_type", sa.String(100), nullable=True),
            sa.Column("file_size", sa.Integer(), nullable=True),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),

            # Replayed verbatim at confirmation. is_face_image selects the
            # padded-retry branch in the detector, so re-running detection
            # without it could refuse a photo the review phase accepted.
            sa.Column("is_face_image", sa.Boolean(), nullable=False,
                      server_default=sa.false()),

            # Which models produced the embedding that ranked the candidates.
            sa.Column("embedding_model_version", sa.String(64), nullable=True),
            sa.Column("detection_model_version", sa.String(64), nullable=True),

            # 'strong' | 'uncertain'. No row is written when nothing matched —
            # that upload enrolls directly, exactly as before this change.
            sa.Column("decision", sa.String(16), nullable=False),
            sa.Column("top_similarity", sa.Float(), nullable=True),

            # The exact-checksum hit, when one exists on ANOTHER identity.
            # Deterministic evidence, computed before approximate similarity.
            sa.Column("checksum_match_identity_id",
                      postgresql.UUID(as_uuid=True), nullable=True),

            # The frozen list actually offered to the administrator.
            sa.Column("candidates", postgresql.JSONB(), nullable=False),

            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
        )

    if not _has_index("pending_enrollments", "uq_pending_enrollment_token"):
        op.create_index("uq_pending_enrollment_token", "pending_enrollments",
                        ["token_hash"], unique=True)
    if not _has_index("pending_enrollments", "idx_pending_enrollment_expires"):
        op.create_index("idx_pending_enrollment_expires", "pending_enrollments",
                        ["expires_at"])
    if not _has_index("pending_enrollments", "idx_pending_enrollment_user"):
        op.create_index("idx_pending_enrollment_user", "pending_enrollments",
                        ["user_id"])


def downgrade() -> None:
    for index_name in ("uq_pending_enrollment_token",
                       "idx_pending_enrollment_expires",
                       "idx_pending_enrollment_user"):
        if _has_index("pending_enrollments", index_name):
            op.drop_index(index_name, table_name="pending_enrollments")
    if _has_table("pending_enrollments"):
        op.drop_table("pending_enrollments")
