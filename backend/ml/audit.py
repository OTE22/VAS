"""
ML admin audit — durable rows + structured log lines.

Immutable by convention: the application contains no update or delete path
for ml_audit_log rows and retention deliberately excludes the table. The
only deletes in the repository are the two guarded development data
generators (scripts/seed_ml_ops_demo.py --remove, scripts/generate_synthetic_year.py
--remove), which remove exactly the audit rows they themselves created and
refuse production databases. The
writer follows the identity-audit rule: it joins the caller's transaction
(flush, not commit) and swallows its own failures — audit must never break
the operation it records. Never log embeddings, tokens, passwords, or
artifact paths.
"""

import logging
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.ml.constants import LOG_MLOPS_AUDIT

logger = logging.getLogger(__name__)


def _current_request_id() -> Optional[str]:
    """Request id of the HTTP call in progress (None for CLI/background), so
    an audit row joins to its [MLOPS_CALL] line and task-history row."""
    try:
        from utils.logging import request_id_var
        value = request_id_var.get()
        return value[:64] if value and value != "-" else None
    except Exception:
        return None


async def ml_audit(db: AsyncSession, *, action: str,
                   actor_user_id: Optional[int] = None,
                   actor_username: Optional[str] = None,
                   object_type: Optional[str] = None,
                   object_id: Optional[str] = None,
                   before: Optional[Dict[str, Any]] = None,
                   after: Optional[Dict[str, Any]] = None,
                   reason: Optional[str] = None,
                   ip_address: Optional[str] = None) -> None:
    """Write one audit row (flush, not commit) + one [MLOPS_AUDIT] line."""
    logger.info(
        "%s action=%s user_id=%s username=%s object=%s:%s reason=%s",
        LOG_MLOPS_AUDIT, action, actor_user_id, actor_username,
        object_type, object_id, (reason or "")[:200],
    )
    try:
        from db_models import MLAuditLog
        db.add(MLAuditLog(
            action=action[:64],
            object_type=(object_type or None),
            object_id=(str(object_id)[:64] if object_id is not None else None),
            actor_user_id=actor_user_id,
            actor_username=(actor_username or None),
            before=before,
            after=after,
            reason=reason,
            ip_address=(ip_address or None),
            request_id=_current_request_id(),
        ))
        await db.flush()
    except Exception:
        # Audit failure must never break the audited operation.
        logger.warning("%s row write failed for action=%s (operation continues)",
                       LOG_MLOPS_AUDIT, action, exc_info=True)
