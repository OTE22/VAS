"""Extension point: reuse canonical prediction evidence when production exists."""
from sqlalchemy import select, func


async def production_inference_ready(db, minimum):
    from db_models import MLModel, MLPrediction
    count = (await db.execute(select(func.count()).select_from(MLPrediction).join(MLModel, MLModel.id == MLPrediction.model_id)
        .where(MLModel.stage == "production", MLPrediction.actual_mode_used.in_(["ml", "hybrid"]),
               MLPrediction.fallback_reason.is_(None)))).scalar() or 0
    return count >= minimum
