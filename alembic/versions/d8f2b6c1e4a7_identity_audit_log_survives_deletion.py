"""identity_audit_log survives the deletion of the identity it describes.

`identity_audit_log.user_id` has always been ON DELETE SET NULL: deleting the
operator must not delete the record of what they did. The two identity columns
had no ON DELETE at all, so the opposite was true of the subject — an identity
with any audit history could not be deleted until its audit rows were deleted
first, which means the usual way to satisfy the constraint is to destroy the
evidence.

That was survivable while only promote/merge/unmerge wrote audit rows. Now that
creating a person is audited too (the accountability gap this series fixes),
every enrolled person carries audit history, and the constraint would turn a
routine deletion into an error or into "delete the audit trail first".

SET NULL keeps the row and drops only the pointer, matching user_id. The audit
row still names the identity: `action_details.identity_id` carries the id as
text, so a deleted person remains traceable in the log after the FK is gone —
the same tombstone shape used for deleted users.

Not CASCADE: cascading would delete the audit rows along with the identity,
which is precisely the outcome an audit log exists to prevent.

Revision ID: d8f2b6c1e4a7
Revises: 7d3f91a2c4e6
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d8f2b6c1e4a7"
down_revision: Union[str, None] = "7d3f91a2c4e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = ("identity_id", "related_identity_id")


def upgrade() -> None:
    for column in COLUMNS:
        name = f"identity_audit_log_{column}_fkey"
        op.drop_constraint(name, "identity_audit_log", type_="foreignkey")
        op.create_foreign_key(name, "identity_audit_log", "identities",
                              [column], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    for column in COLUMNS:
        name = f"identity_audit_log_{column}_fkey"
        op.drop_constraint(name, "identity_audit_log", type_="foreignkey")
        op.create_foreign_key(name, "identity_audit_log", "identities",
                              [column], ["id"])
