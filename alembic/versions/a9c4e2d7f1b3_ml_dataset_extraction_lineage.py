"""ML dataset extraction lineage + training configuration lineage.

Datasets were reproducible but their EXTRACTION was not auditable: the
builder selected `ORDER BY as_of ASC LIMIT 100000` and recorded neither the
cap nor how many rows it silently left out. Models recorded an incomplete
`hyperparameters` dict and no code revision.

Additive, nullable columns only — no existing row is rewritten, and rows
built before this revision are reported at read time as having used the
legacy extraction policy (never back-filled to pretend otherwise):

ml_datasets
  definition_name / definition_version   which typed dataset definition built it
  extraction                              {policy_version, candidate_rows,
                                           selected_rows, excluded_rows, cap,
                                           sampling_policy, ordering, time_range}
  parquet_sha256                          hash of the Parquet FILE bytes (the
                                           logical `checksum` stays the
                                           canonical-row fingerprint)
  manifest_path                           server-only sidecar manifest

ml_models
  training_config                         complete algorithm configuration
                                           actually used (seed, every
                                           hyperparameter, dataset id)
  code_version                            git revision of the trainer

Revision ID: a9c4e2d7f1b3
Revises: d8f2b6c1e4a7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a9c4e2d7f1b3"
down_revision: Union[str, None] = "d8f2b6c1e4a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ml_datasets", sa.Column("definition_name", sa.String(128), nullable=True))
    op.add_column("ml_datasets", sa.Column("definition_version", sa.String(64), nullable=True))
    op.add_column("ml_datasets", sa.Column("extraction", JSONB, nullable=True))
    op.add_column("ml_datasets", sa.Column("parquet_sha256", sa.String(64), nullable=True))
    op.add_column("ml_datasets", sa.Column("manifest_path", sa.Text, nullable=True))
    op.add_column("ml_models", sa.Column("training_config", JSONB, nullable=True))
    op.add_column("ml_models", sa.Column("code_version", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("ml_models", "code_version")
    op.drop_column("ml_models", "training_config")
    op.drop_column("ml_datasets", "manifest_path")
    op.drop_column("ml_datasets", "parquet_sha256")
    op.drop_column("ml_datasets", "extraction")
    op.drop_column("ml_datasets", "definition_version")
    op.drop_column("ml_datasets", "definition_name")
