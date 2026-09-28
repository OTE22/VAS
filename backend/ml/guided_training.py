"""Preflight and worker preparation for the service-first training workflow.

Uses the existing training queue/runner and immutable dataset builder. Preparation
never activates a model or changes decision mode.
"""
import uuid
from sqlalchemy import select
from backend.ml.model_specs import get_model_spec


class GuidedTrainingRefusal(ValueError):
    def __init__(self, code, message, **details):
        self.code, self.message, self.details = code, message, details
        super().__init__(message)


async def preflight_guided_training(db, *, model_type, dataset_id=None):
    """Cheap eligibility checks; artifact and sample gates run again in worker."""
    from config import settings
    from backend.ml.job_service import ml_worker_health
    worker = await ml_worker_health(db, lease_seconds=settings.ML_JOB_LEASE_SECONDS)
    if worker['status'] != 'healthy':
        raise GuidedTrainingRefusal('ML_WORKER_UNAVAILABLE',
            'The training worker is unavailable. Restore worker health, then try again.', status_code=503)
    spec = get_model_spec(model_type)
    if model_type == 'tabular_regression_model':
        raise GuidedTrainingRefusal('ADVANCED_CONFIGURATION_REQUIRED',
            'Numeric experiments need an explicit target and saved dataset. Use Advanced tools to configure this experiment.')
    if spec.dataset_kind == 'supervised':
        from backend.ml.labeling_service import labeling_service
        stats = await labeling_service.label_stats(db)
        if not stats['supervised_gate_open']:
            raise GuidedTrainingRefusal('INSUFFICIENT_REVIEWED_LABELS',
                'Review enough positive and negative outcome labels before training this service.',
                label_readiness=labeling_service.supervised_refusal(stats))
    if dataset_id:
        from db_models import MLDataset
        try:
            identifier = uuid.UUID(str(dataset_id))
        except (ValueError, TypeError, AttributeError):
            raise GuidedTrainingRefusal('DATASET_NOT_FOUND', 'Select a saved dataset or choose Prepare new data.')
        row = (await db.execute(select(MLDataset).where(MLDataset.id == identifier))).scalar_one_or_none()
        if row is None:
            raise GuidedTrainingRefusal('DATASET_NOT_FOUND', 'The selected dataset no longer exists. Refresh and choose again.')
        if row.status != 'built':
            raise GuidedTrainingRefusal('DATASET_NOT_READY', 'The selected dataset is not ready for training. Inspect its validation report or prepare new data.')
        if row.kind != spec.dataset_kind or row.feature_set_version != spec.feature_set_version:
            raise GuidedTrainingRefusal('DATASET_SERVICE_MISMATCH', 'This dataset belongs to a different service or feature version. Choose compatible data or prepare new data.')
        if row.definition_name and row.definition_name != spec.dataset_definition:
            raise GuidedTrainingRefusal('DATASET_SERVICE_MISMATCH', 'This dataset was prepared for another service. Choose compatible data or prepare new data.')


async def prepare_training_inputs(job_id, payload):
    """Incremental feature preparation in the leased training child process.

    A supplied dataset is immutable: do not compute newer inputs for an
    experiment explicitly reusing it. The trainer verifies its hashes/schema.
    """
    if not payload.get('prepare_features') or payload.get('dataset_id'):
        return {'status': 'reused' if payload.get('dataset_id') else 'not_requested'}
    from db_connection import db_manager
    from backend.core.task_history import task_history_manager
    from backend.ml.collector import run_collection
    model_type = payload['model_type']
    async def progress(percent):
        await task_history_manager.update_progress(job_id, 1 + int(max(0, min(100, percent)) * .03),
            details={'stage': 'preparing_features', 'model_type': model_type,
                     'guided_workflow': True, 'preparation_percent': percent})
    await progress(0)
    async with db_manager.get_session() as db:
        result = await run_collection(db, run_id=job_id, full_rebuild=False, progress_cb=progress)
    if result.get('cancelled') or result.get('status') == 'cancelled':
        raise GuidedTrainingRefusal('PREPARATION_CANCELLED', 'Preparation was cancelled. Review job status before starting again.')
    if result.get('status') in ('failed', 'busy') or result.get('current_state_pending', 0):
        raise GuidedTrainingRefusal('FEATURE_PREPARATION_INCOMPLETE',
            'Feature preparation did not finish. Inspect feature collection status and retry before training.')
    await progress(100)
    return {'status': 'prepared', 'snapshots_written': result.get('snapshots_written', 0),
            'snapshots_reused': result.get('snapshots_deduplicated', 0)}
