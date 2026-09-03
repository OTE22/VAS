"""Transactional decision-mode command service.

The database is the source of truth. A successful command commits the setting
and its audit event before changing process-local runtime state. If runtime
application unexpectedly fails, a compensating transaction restores the
previous durable mode and records the failure.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ml.audit import ml_audit


async def change_decision_mode(
    db: AsyncSession,
    *,
    target_mode: str,
    action: str,
    actor_username: str,
    actor_user_id: Optional[int],
    reason: str,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    from config import settings as app_settings
    from db_models import Setting
    from backend.core.runtime_settings import apply_to_runtime

    previous_mode = str(app_settings.ML_DECISION_MODE)
    row = (await db.execute(
        select(Setting).where(Setting.key == "ML_DECISION_MODE").with_for_update()
    )).scalar_one_or_none()
    if row is None:
        row = Setting(
            key="ML_DECISION_MODE", value=target_mode, value_type="string",
            category="ml_ops", is_sensitive=False, is_readonly=False,
            description=("rules is the production-safe default; shadow needs an approved "
                         "model; hybrid/ml remain governed by readiness gates"),
            created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
        )
        db.add(row)
    else:
        row.value = target_mode
        row.updated_at = datetime.utcnow()

    await ml_audit(
        db, action=action, actor_username=actor_username,
        actor_user_id=actor_user_id, object_type="ml_config",
        object_id="ML_DECISION_MODE", before={"mode": previous_mode},
        after={"mode": target_mode}, reason=reason, ip_address=ip_address,
    )
    await db.commit()

    try:
        applied = apply_to_runtime("ML_DECISION_MODE", target_mode)
        if not applied:
            raise RuntimeError("ML_DECISION_MODE was committed but is not dynamically applicable")
    except Exception as exc:
        # Keep durable and effective state aligned. A crash between the first
        # commit and this compensation is recovered by startup hydration,
        # which applies the committed target rather than silently diverging.
        restore = (await db.execute(
            select(Setting).where(Setting.key == "ML_DECISION_MODE").with_for_update()
        )).scalar_one()
        restore.value = previous_mode
        restore.updated_at = datetime.utcnow()
        await ml_audit(
            db, action="mode_apply_failed", actor_username=actor_username,
            actor_user_id=actor_user_id, object_type="ml_config",
            object_id="ML_DECISION_MODE", before={"mode": target_mode},
            after={"mode": previous_mode}, reason=str(exc)[:200],
            ip_address=ip_address,
        )
        await db.commit()
        raise

    try:
        from backend.ml import metrics as ml_metrics
        await ml_metrics.refresh_state(db, reason=action)
    except Exception:
        pass
    return {"success": True, "mode": target_mode, "previous_mode": previous_mode}

