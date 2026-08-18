"""Align create_all-era residue with the migration-defined schema.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9

Databases that were bootstrapped by `Base.metadata.create_all()` (every
database before revision 000_baseline existed) carry a few column defaults that
differ from what the migrations that OWN those tables define — because
`CREATE TABLE IF NOT EXISTS` in those revisions was skipped when create_all had
already made the table from the ORM (which declares Python-side defaults only).
The migration path is authoritative, so this revision brings existing databases
to the migration-defined shape; a fresh `alembic upgrade head` database already
has it (tests/test_migration_schema_parity.py proves the two are identical).

  deleted_users.user_id     — the ORIGINAL users.id, always supplied explicitly:
                              no serial default (b0c1d2e3f4a5 defines INTEGER PK)
  deleted_users.deleted_at  — DEFAULT now()          (b0c1d2e3f4a5)
  similarity_model_registry.created_at DEFAULT now(), model_type DEFAULT
  'merge_similarity', status DEFAULT 'candidate'     (a5c6d7e8f9b0)
  duplicate create_all-era indexes (ORM `index=True` names ix_<table>_<col>)
  on columns whose migration already defines the index under its own name:
  ix_deleted_users_username / ix_deleted_users_deleted_at (b0c1d2e3f4a5 owns
  idx_deleted_users_*), ix_similarity_model_registry_created_at /
  _training_job_id / _id (a5c6d7e8f9b0 owns idx_model_registry_*; id is the PK)

All statements are idempotent; no rows are touched.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE deleted_users ALTER COLUMN user_id DROP DEFAULT")
    op.execute("DROP SEQUENCE IF EXISTS deleted_users_user_id_seq")
    op.execute("ALTER TABLE deleted_users ALTER COLUMN deleted_at SET DEFAULT now()")
    op.execute("ALTER TABLE similarity_model_registry ALTER COLUMN created_at SET DEFAULT now()")
    op.execute("ALTER TABLE similarity_model_registry ALTER COLUMN model_type SET DEFAULT 'merge_similarity'")
    op.execute("ALTER TABLE similarity_model_registry ALTER COLUMN status SET DEFAULT 'candidate'")
    for name in ("ix_deleted_users_username", "ix_deleted_users_deleted_at",
                 "ix_similarity_model_registry_created_at", "ix_similarity_model_registry_training_job_id",
                 "ix_similarity_model_registry_id"):
        op.execute(f"DROP INDEX IF EXISTS {name}")


def downgrade() -> None:
    op.execute("ALTER TABLE similarity_model_registry ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TABLE similarity_model_registry ALTER COLUMN model_type DROP DEFAULT")
    op.execute("ALTER TABLE similarity_model_registry ALTER COLUMN created_at DROP DEFAULT")
    op.execute("ALTER TABLE deleted_users ALTER COLUMN deleted_at DROP DEFAULT")
    # the serial default is not restored: user_id is the original users.id by design;
    # the dropped duplicate indexes are not recreated: their columns stay indexed
    # under the migration-defined names.
