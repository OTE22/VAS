"""Versioned pipeline configurations and durable MLflow synchronization.

Revision ID: fcc3d4e5f6a7
Revises: fbb2c3d4e5f6
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "fcc3d4e5f6a7"
down_revision = "fbb2c3d4e5f6"
branch_labels = depends_on = None


def upgrade():
    op.create_table("ml_pipeline_versions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("configuration", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Integer()),
        sa.UniqueConstraint("name", "version", name="uq_ml_pipeline_version"))
    op.create_table("ml_tracking_runs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("model_id", pg.UUID(as_uuid=True), sa.ForeignKey("ml_models.id", ondelete="SET NULL"), unique=True),
        sa.Column("run_id", sa.String(64)),
        sa.Column("registered_name", sa.String(128)),
        sa.Column("registered_version", sa.String(32)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("manifest", pg.JSONB(), nullable=False),
        sa.Column("last_error", sa.String(500)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))


def downgrade():
    # Explicitly destructive; operators must export evidence before downgrade.
    op.drop_table("ml_tracking_runs")
    op.drop_table("ml_pipeline_versions")
