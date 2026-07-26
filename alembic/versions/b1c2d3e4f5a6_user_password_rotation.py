"""user password rotation tracking

Adds the columns that let a bootstrapped administrator be forced through a
password change on first login, so a seeded credential cannot silently become
a permanent one.

server_default is deliberate: existing rows backfill to false, and any code
path that still INSERTs without naming the column keeps working.

Revision ID: b1c2d3e4f5a6
Revises: a5c6d7e8f9b0
"""

from alembic import op
import sqlalchemy as sa


revision = "b1c2d3e4f5a6"
down_revision = "a5c6d7e8f9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}

    if "must_change_password" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "must_change_password",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    if "password_changed_at" not in columns:
        op.add_column(
            "users",
            sa.Column("password_changed_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}

    if "password_changed_at" in columns:
        op.drop_column("users", "password_changed_at")
    if "must_change_password" in columns:
        op.drop_column("users", "must_change_password")
