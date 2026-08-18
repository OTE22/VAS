"""Webhook ingest credentials — named tokens issued to external senders

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-08-05

Before this table there was exactly one way for an external system to obtain an
ingest credential: a human ran `openssl rand -base64 48 > secrets/webhook_api_keys`,
read the file, and sent the string out of band. Nothing in the application could
generate it, nothing could display it — WEBHOOK_API_KEYS is in no settings
category, so no Setting row is ever seeded and GET /api/settings/WEBHOOK_API_KEYS
is a 404 — and nothing recorded that the handover happened. Every camera and
every third party shared one flat key set, so a request could not be attributed
to a sender and revoking one sender meant rotating the whole fleet.

token_hash holds SHA-256 of the token, never the token. The raw value is shown
to the administrator exactly once and is not recoverable from this table.

REVOCATION IS DELETE. There is deliberately no revoked_at and no expires_at: a
flag would create a second "unusable" state the verifier must tell apart from
"absent", and any distinction the verifier can make is a distinction the 401 can
leak. Deleting the row collapses both into one state, so a revoked credential is
indistinguishable from a token that was never valid.

created_by_user_id is ON DELETE SET NULL, NOT CASCADE — the deliberate
divergence from pending_enrollments. A pending enrollment is meaningless without
its uploader; an ingest credential belongs to a camera fleet. Cascading would
mean that deleting a departing employee's account silently blacks out every
camera they provisioned, discovered as an outage rather than as a decision.
created_by_username is denormalized so attribution survives that SET NULL.

The environment credentials (WEBHOOK_API_KEYS / WEBHOOK_AUTH_TOKEN) keep working
alongside this table as the break-glass path, and config_guard still requires one
in production. Startup therefore never depends on this table, and a database
outage cannot lock every camera out.

DOWNGRADE REVOKES EVERY ISSUED CREDENTIAL. Unlike the pending-enrollment
downgrade above it, which loses only 15-minute claim tickets an administrator can
repeat, this one takes every external sender offline until each is re-issued a
new token by hand. The environment key is what keeps ingest alive through it.
"""

import sqlalchemy as sa
from alembic import op

revision = 'e7f8a9b0c1d2'
down_revision = 'd6e7f8a9b0c1'
branch_labels = None
depends_on = None

_INDEXES = (
    # The verification lookup and the uniqueness guarantee are the same index:
    # two credentials can never share a token.
    ("uq_webhook_credential_token", ["token_hash"], True),
    # Uniqueness on the NORMALIZED key, so "Acme VMS" and "acme  vms" cannot
    # both exist and leave a log line ambiguous about which one authenticated.
    ("uq_webhook_credential_name", ["name_key"], True),
    ("idx_webhook_credential_created", ["created_at"], False),
)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table):
    return _inspector().has_table(table)


def _has_index(table, name):
    if not _has_table(table):
        return False
    return name in {ix["name"] for ix in _inspector().get_indexes(table)}


def upgrade() -> None:
    if not _has_table("webhook_credentials"):
        op.create_table(
            "webhook_credentials",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("name_key", sa.String(100), nullable=False),
            sa.Column("created_by_user_id", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("created_by_username", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.now()),
            # Nullable and advisory: written by a throttled batch flush, so it
            # lags real use by up to one cache TTL. "Never used" is only
            # meaningful once the credential is older than that window.
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
        )

    for name, columns, unique in _INDEXES:
        if not _has_index("webhook_credentials", name):
            op.create_index(name, "webhook_credentials", columns, unique=unique)


def downgrade() -> None:
    for name, _columns, _unique in _INDEXES:
        if _has_index("webhook_credentials", name):
            op.drop_index(name, table_name="webhook_credentials")
    if _has_table("webhook_credentials"):
        op.drop_table("webhook_credentials")
