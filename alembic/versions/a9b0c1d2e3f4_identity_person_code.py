"""Identity person_code — the organisation's own identifier for a person

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-08-06

The promote endpoint has accepted a `person_code` since it was written, logged
it, and thrown it away: there was no column to put it in. Operators typed a
badge number into a field that did nothing, and the only trace was a line in the
audit note. This gives it a home.

Two columns, matching the display/key pair PendingEnrollment already uses:
`person_code` is what the operator typed, `person_code_key` is its uppercased
form. Codes are read aloud and retyped, so emp-001 and EMP-001 are the same
code; storing only the display form would let both exist and destroy the code's
only useful property — that it identifies exactly one person.

WHY A PARTIAL UNIQUE INDEX. Most identities have no code, and every unknown face
certainly does not. A plain UNIQUE index would permit that (PostgreSQL treats
NULLs as distinct), but `WHERE person_code_key IS NOT NULL` states the rule
instead of relying on that behaviour: codes are unique when present, and absence
is not a value that can collide. It also keeps the index to the rows that have
one rather than carrying an entry per identity.

Both columns are nullable and no backfill is attempted — there is nothing to
backfill from. Existing identities keep NULL and stay valid.

Downgrade drops the columns, losing every recorded code. That is data, not
derived state: it cannot be reconstructed from anything else in the schema.
"""

import sqlalchemy as sa
from alembic import op

revision = 'a9b0c1d2e3f4'
down_revision = 'f8a9b0c1d2e3'
branch_labels = None
depends_on = None

INDEX_NAME = "uq_identity_person_code_key"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table, column):
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _has_index(table, name):
    return name in {ix["name"] for ix in _inspector().get_indexes(table)}


def upgrade() -> None:
    if not _has_column("identities", "person_code"):
        op.add_column("identities",
                      sa.Column("person_code", sa.String(100), nullable=True))
    if not _has_column("identities", "person_code_key"):
        op.add_column("identities",
                      sa.Column("person_code_key", sa.String(100), nullable=True))

    if not _has_index("identities", INDEX_NAME):
        # Partial: unique among rows that HAVE a code, absent for those that
        # do not. Creating it will fail loudly if conflicting codes already
        # exist, which is the correct outcome — silently keeping duplicates
        # would leave a lookup key that matches two people.
        op.create_index(INDEX_NAME, "identities", ["person_code_key"],
                        unique=True,
                        postgresql_where=sa.text("person_code_key IS NOT NULL"))


def downgrade() -> None:
    if _has_index("identities", INDEX_NAME):
        op.drop_index(INDEX_NAME, table_name="identities")
    for column in ("person_code_key", "person_code"):
        if _has_column("identities", column):
            op.drop_column("identities", column)
