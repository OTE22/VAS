"""The `system` audit principal — the actor for machine-initiated actions.

`identity_audit_log.user_id` is NOT NULL and a foreign key to `users.id`, so
automated work — vector index rebuild, reconciliation, corruption recovery, the
configured-fallback event — needs a real row to attribute to. Without it those
INSERTs are rejected and the operations that most need a durable record produce
none (`backend/core/vector_index/manager.py` logs "no 'system' principal" and
skips the audit row).

Attributing machine actions to the human `admin` account was the alternative and
was rejected when the principal was introduced: it writes a false audit record,
and afterwards nobody can tell a 3am automated rebuild from something an
administrator actually did. See
`alembic/versions/a3b4c5d6e7f8_system_audit_principal.py`.

THIS ACCOUNT CANNOT AUTHENTICATE, by three independent mechanisms:

  * `is_active=False` is checked both on login and on every token validation
    (`backend/auth/auth_service.py`);
  * `password_hash` is the literal "!", which is not a bcrypt hash, so
    verification cannot succeed for any input — there is no password that works;
  * `role="system"` is not in the `Role` enum, so `canonical_role()` resolves it
    to the LEAST privileged role and `require_role(["admin"])` grants it nothing.

Relationship to the migration
-----------------------------
The migration keeps its own copy of these literals on purpose. An applied
migration is a frozen historical record and must never import live application
code — otherwise editing this module would retroactively change what a past
migration did. This module is authoritative for RUNTIME; the migration is
authoritative for HISTORY. That is the standard Alembic separation, not
duplicated business logic.
"""

import logging
from typing import Optional, Tuple

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db_models import User

logger = logging.getLogger(__name__)

SYSTEM_USERNAME = "system"
SYSTEM_EMAIL = "system@localhost.invalid"
SYSTEM_ROLE = "system"
SYSTEM_FULL_NAME = "Automated system actions"
# Not a bcrypt hash. bcrypt verification against it cannot succeed, so the
# account has no password rather than a weak one.
UNUSABLE_PASSWORD_HASH = "!"


def _normalize(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def is_system_principal(user) -> bool:
    """Whether `user` is the protected audit principal.

    Matches on EITHER username or role, case- and whitespace-insensitively. Both
    are checked because the two can be edited independently: a half-tampered row
    whose role was changed to "user" is still the principal and must still be
    protected, otherwise the guard could be lifted by the very edit it exists to
    prevent.

    Accepts an ORM User or anything with `username` / `role` attributes; returns
    False for None so callers need no separate null check.
    """
    if user is None:
        return False
    return (_normalize(getattr(user, "username", None)) == SYSTEM_USERNAME
            or _normalize(getattr(user, "role", None)) == SYSTEM_ROLE)


async def get_system_principal(db: AsyncSession) -> Optional[User]:
    """The principal, or None when it is missing."""
    return (await db.execute(
        select(User).where(User.username == SYSTEM_USERNAME)
    )).scalar_one_or_none()


async def ensure_system_principal(db: AsyncSession) -> Tuple[User, bool]:
    """Create the principal if absent, and force it back to canonical state.

    Returns `(user, created)`. Idempotent: safe to call repeatedly, and safe in
    production — every statement is keyed on `username = 'system'`, so no other
    account is read or written.

    `ON CONFLICT (username) DO NOTHING` makes a duplicate impossible even under
    concurrent calls.

    The UPDATE re-asserts EVERY security property, not merely the two flags the
    migration forces. A repair that only reset `is_active` and
    `can_use_chatbot` would leave a tampered row holding a real bcrypt hash and
    an ordinary role — dormant, but armed the moment anyone flips `is_active`
    again. Restoring "most of" a security invariant is how an account named
    `system` quietly becomes a usable login.
    """
    inserted = (await db.execute(
        text("""
            INSERT INTO users (username, email, password_hash, full_name, role,
                               is_active, can_use_chatbot, created_at, updated_at,
                               must_change_password, permissions_version)
            VALUES (:username, :email, :password_hash, :full_name, :role,
                    false, false, NOW(), NOW(), false, 1)
            ON CONFLICT (username) DO NOTHING
            RETURNING id
        """).bindparams(
            username=SYSTEM_USERNAME,
            email=SYSTEM_EMAIL,
            password_hash=UNUSABLE_PASSWORD_HASH,
            full_name=SYSTEM_FULL_NAME,
            role=SYSTEM_ROLE,
        )
    )).first()
    created = inserted is not None

    await db.execute(
        text("""
            UPDATE users
               SET email           = :email,
                   password_hash   = :password_hash,
                   role            = :role,
                   is_active       = false,
                   can_use_chatbot = false,
                   -- Not a security property, but a leftover block reason makes
                   -- the admin table render the principal as BLOCKED, which
                   -- reads as an incident rather than as normal protected
                   -- state. Cleared so the row displays as designed.
                   blocked_reason  = NULL,
                   blocked_at      = NULL,
                   updated_at      = NOW()
             WHERE username = :username
        """).bindparams(
            username=SYSTEM_USERNAME,
            email=SYSTEM_EMAIL,
            password_hash=UNUSABLE_PASSWORD_HASH,
            role=SYSTEM_ROLE,
        )
    )
    await db.commit()

    user = await get_system_principal(db)
    if user is None:                                    # pragma: no cover
        raise RuntimeError(
            "system principal missing immediately after ensure_system_principal")

    # The ORM may hold a stale copy from before the raw UPDATE above.
    await db.refresh(user)

    if created:
        logger.warning(
            "[SYSTEM_PRINCIPAL] created the 'system' audit principal (id=%s); "
            "machine-initiated audit rows can be written again", user.id)
    else:
        logger.info(
            "[SYSTEM_PRINCIPAL] 'system' principal already present (id=%s); "
            "canonical security properties re-asserted", user.id)
    return user, created
