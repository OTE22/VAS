"""First-administrator bootstrap.

Replaces the previous inline block in backend/lifespan.py, which hardcoded a
well-known default password and then wrote it in cleartext to stdout and to the
bind-mounted log file.

Rules now:
  * an administrator already exists            -> do nothing
  * no administrator, bootstrap secret given   -> create, force rotation, audit
  * no administrator, no secret, production    -> fail startup
  * no administrator, no secret, development   -> warn and skip

Nothing in this module logs, returns, or accepts a password value. `_audit_line`
takes no password argument by signature, so a future edit cannot casually add
one.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import List, Optional

logger = logging.getLogger(__name__)

# Postgres advisory lock key for the bootstrap section. Distinct from
# data_retention's RETENTION_ADVISORY_LOCK_KEY (823451).
BOOTSTRAP_ADVISORY_LOCK_KEY = 823_452


class BootstrapAdminError(RuntimeError):
    """The deployment requires an administrator and cannot create one."""


class BootstrapOutcome(str, Enum):
    EXISTS = "exists"
    CREATED = "created"
    SKIPPED = "skipped"
    RACED = "raced"


def _assess_password(password: str) -> List[str]:
    """Reasons a bootstrap password is unacceptable. Never echoes the value."""
    from backend.security.config_guard import assess_admin_password

    return assess_admin_password(password)


def _audit_line(*, username: str, source: str, rotation_required: bool) -> str:
    """Audit text for administrator creation.

    Takes no password parameter, by design — the provenance is auditable, the
    value is not.
    """
    return (
        f"[AUDIT] bootstrap administrator created: username={username} "
        f"credential_source={source} rotation_required={rotation_required}"
    )


def _is_production(cfg) -> bool:
    return str(getattr(cfg, "ENVIRONMENT", "")).strip().lower() in ("production", "prod")


async def ensure_bootstrap_admin(db_manager, *, cfg=None) -> BootstrapOutcome:
    """Create the first administrator when, and only when, none exists."""
    if cfg is None:
        from config import settings as cfg

    from sqlalchemy import func, select, text as sa_text

    from backend.security.secrets import resolve_secret
    from db_models import User

    production = _is_production(cfg)

    if not bool(getattr(cfg, "BOOTSTRAP_ADMIN_ENABLED", True)):
        logger.info("Bootstrap administrator disabled by configuration")
        return BootstrapOutcome.SKIPPED

    async with db_manager.get_session() as db:
        existing_admin = (
            await db.execute(
                select(User.id).where(User.role == "admin", User.is_active.is_(True)).limit(1)
            )
        ).scalar_one_or_none()

        if existing_admin is not None:
            logger.info(
                "Bootstrap administrator: an admin account already exists (id=%s) — no action",
                existing_admin,
            )
            return BootstrapOutcome.EXISTS

        # HUMAN accounts only. The `system` audit principal is seeded by
        # migration a3b4c5d6e7f8 on every fresh database (role='system',
        # inactive, unusable hash): it is a machine actor, not a user, and its
        # presence must not make a fresh database look like a damaged one —
        # counting it here made a fresh production bootstrap impossible.
        from backend.services.system_principal import SYSTEM_ROLE, SYSTEM_USERNAME
        user_count = (await db.execute(
            select(func.count()).select_from(User).where(
                func.lower(User.username) != SYSTEM_USERNAME,
                func.lower(User.role) != SYSTEM_ROLE))).scalar_one()

    # Human users exist but none is an active admin. That is a damaged
    # deployment, not a fresh one — creating another admin here would be a
    # privilege escalation path, so refuse in production.
    if user_count:
        message = (
            f"{user_count} user account(s) exist but none is an active administrator; "
            "refusing to auto-create one"
        )
        if production:
            raise BootstrapAdminError(message)
        logger.warning("Bootstrap administrator: %s", message)
        return BootstrapOutcome.SKIPPED

    secret = resolve_secret(
        getattr(cfg, "BOOTSTRAP_ADMIN_PASSWORD", "") or None,
        getattr(cfg, "BOOTSTRAP_ADMIN_PASSWORD_FILE", "") or None,
        name="BOOTSTRAP_ADMIN_PASSWORD",
    )

    if not secret:
        message = (
            "no administrator account exists and no bootstrap credential was supplied; "
            "set BOOTSTRAP_ADMIN_PASSWORD_FILE (preferred) or BOOTSTRAP_ADMIN_PASSWORD "
            "and restart"
        )
        if production:
            raise BootstrapAdminError(message)
        logger.warning("Bootstrap administrator: %s", message)
        return BootstrapOutcome.SKIPPED

    reasons = _assess_password(secret)
    if reasons:
        message = "bootstrap administrator password is unacceptable: " + "; ".join(reasons)
        if production:
            raise BootstrapAdminError(message)
        logger.warning("Bootstrap administrator: %s (allowed in development)", message)

    username = str(getattr(cfg, "BOOTSTRAP_ADMIN_USERNAME", "admin") or "admin").strip()
    email = str(getattr(cfg, "BOOTSTRAP_ADMIN_EMAIL", "") or f"{username}@example.com").strip()
    rotation_required = bool(getattr(cfg, "BOOTSTRAP_ADMIN_REQUIRE_ROTATION", True))
    source = "file" if getattr(cfg, "BOOTSTRAP_ADMIN_PASSWORD_FILE", "") else "environment"

    from backend.auth.password import hash_password

    password_hash = hash_password(secret)

    async with db_manager.get_session() as db:
        # Transaction-scoped advisory lock: several workers or replicas run this
        # concurrently, and the lock releases on commit or rollback either way.
        await db.execute(
            sa_text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": BOOTSTRAP_ADVISORY_LOCK_KEY},
        )

        # Re-check inside the lock — another process may have won the race.
        raced = (
            await db.execute(
                select(User.id).where(User.role == "admin", User.is_active.is_(True)).limit(1)
            )
        ).scalar_one_or_none()
        if raced is not None:
            logger.info("Bootstrap administrator: created concurrently by another process")
            return BootstrapOutcome.RACED

        db.add(User(
            username=username,
            email=email,
            password_hash=password_hash,
            full_name="System Administrator",
            role="admin",
            can_use_chatbot=True,
            is_active=True,
            must_change_password=rotation_required,
        ))
        await db.commit()

    logger.warning(_audit_line(
        username=username, source=source, rotation_required=rotation_required
    ))
    if rotation_required:
        logger.warning(
            "The bootstrap administrator must change its password on first login."
        )
    return BootstrapOutcome.CREATED
