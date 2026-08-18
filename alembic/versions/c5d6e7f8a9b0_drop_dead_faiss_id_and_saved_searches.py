"""Drop the dead positional faiss_id and the never-shipped saved_searches table

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-08-03

identity_embeddings.faiss_id was the positional handle into the retired
name-keyed FAISS service. Every surviving writer stored NULL (identity_service,
identity_index_pgvector), the FlatFaissIndex backend keys vectors by the
embedding row's own id — never by position — and the only reader left was a
debug endpoint counting NULLs. A column that can only ever be NULL carries no
information; it goes, together with its two indexes
(idx_embedding_faiss_id, idx_embedding_faiss_id_type), both of which indexed
an all-NULL column. faiss_index_type STAYS: its name is a relic but its data
still partitions the known/unknown search spaces.

saved_searches never acquired a writer: no route, no service, no frontend
reference anywhere in the repository. The SavedSearch ORM model is deleted in
the same commit, so leaving the table would guarantee schema/model drift. The
`searchtype` enum it referenced is SHARED with search_history and is NOT
dropped.

Both directions carry idempotent guards. Downgrade recreates the structures
EMPTY — a downgrade may undo a schema change, never invent data. Rows that
lived in saved_searches before this migration are not recoverable through
downgrade (there were none on any known deployment).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'c5d6e7f8a9b0'
down_revision = 'b4c5d6e7f8a9'
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table):
    return _inspector().has_table(table)


def _has_column(table, column):
    return column in {col["name"] for col in _inspector().get_columns(table)}


def _has_index(table, name):
    return name in {ix["name"] for ix in _inspector().get_indexes(table)}


def upgrade() -> None:
    # Order matters: indexes first, then the column they reference.
    for index_name in ("idx_embedding_faiss_id", "idx_embedding_faiss_id_type",
                       "ix_identity_embeddings_faiss_id"):  # legacy autogen alias
        if _has_index("identity_embeddings", index_name):
            op.drop_index(index_name, table_name="identity_embeddings")

    if _has_column("identity_embeddings", "faiss_id"):
        op.drop_column("identity_embeddings", "faiss_id")

    if _has_table("saved_searches"):
        op.drop_table("saved_searches")


def downgrade() -> None:
    if not _has_column("identity_embeddings", "faiss_id"):
        op.add_column("identity_embeddings",
                      sa.Column("faiss_id", sa.Integer(), nullable=True))
    if not _has_index("identity_embeddings", "idx_embedding_faiss_id"):
        op.create_index("idx_embedding_faiss_id", "identity_embeddings",
                        ["faiss_id"])
    if not _has_index("identity_embeddings", "idx_embedding_faiss_id_type"):
        op.create_index("idx_embedding_faiss_id_type", "identity_embeddings",
                        ["faiss_id", "faiss_index_type"])

    if not _has_table("saved_searches"):
        op.create_table(
            "saved_searches",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("search_type",
                      postgresql.ENUM(name="searchtype", create_type=False),
                      nullable=True),
            sa.Column("scope", sa.String(20), nullable=True),
            sa.Column("top_k", sa.Integer(), nullable=True),
            sa.Column("min_quality", sa.Float(), nullable=True),
            sa.Column("filters", postgresql.JSONB(), nullable=True),
            sa.Column("exclude_identity_ids", postgresql.JSONB(), nullable=True),
            sa.Column("exclude_watchlist_ids", postgresql.JSONB(), nullable=True),
            sa.Column("embedding_hash", sa.String(64), nullable=True),
            sa.Column("use_count", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        op.create_index("idx_saved_search_user", "saved_searches", ["user_id"])
