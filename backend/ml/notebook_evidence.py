"""Read-only, bounded notebook evidence tied to an explicit model/run/dataset.

No extraction, training, registry transitions, artifact unpickling or database
credentials are exposed. Notebook exports are snapshots, not a live DB client.
"""
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import re
import uuid
from sqlalchemy import select
from backend.ml.model_specs import MODEL_SPECS, get_model_spec

ALLOWED_JOBS = {'ml_training', 'ml_dataset_build', 'ml_feature_computation'}
JOB_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
PRIVATE_KEYS = re.compile(r'password|secret|token|credential|authorization|cookie|(^|_)(url|uri|dsn|path|hostname|worker_name|created_by|requested_by)(_|$)', re.I)


class NotebookEvidenceError(ValueError):
    def __init__(self, code, message, status=422):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def safe_evidence(value, depth=0):
    """Defense in depth over explicitly selected structured fields only."""
    if depth > 16:
        return '[inspection depth limit]'
    if isinstance(value, dict):
        result = {str(k): safe_evidence(v, depth+1) for k, v in list(value.items())[:1000]
                  if not PRIVATE_KEYS.search(str(k))}
        if len(value) > 1000:
            result['_inspection_truncated'] = True
        return result
    if isinstance(value, (list, tuple)):
        return [safe_evidence(v, depth+1) for v in value[:1000]] + (['[inspection item limit]'] if len(value)>1000 else [])
    if isinstance(value, datetime):
        return value.isoformat() + ('Z' if value.tzinfo is None else '')
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        if '://' in value or value.startswith(('/', '\\')):
            return '[external address or absolute path omitted]'
        return value[:8000]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        import math
        return value if math.isfinite(value) else None
    return str(value)[:200]


def _subset(obj, names):
    return {key: obj[key] for key in names if key in obj}


def _id(value):
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        raise NotebookEvidenceError('INVALID_REFERENCE', 'Choose a valid model or dataset identifier.')


def artifact_reference(row):
    """Return only an approved relative Parquet reference, never host paths."""
    from config import settings
    if row is None or not row.storage_path:
        return False, None, None
    try:
        root = Path(settings.ML_ARTIFACT_DIR).resolve()
        path = Path(row.storage_path).resolve()
        if not path.is_relative_to(root/'datasets') or path.suffix != '.parquet':
            return False, None, None
        return path.is_file(), path.relative_to(root).as_posix(), path
    except (OSError, ValueError):
        return False, None, None


def _snapshot_ids(path):
    # Read only three lineage references, not the full training dataset.
    import pyarrow.parquet as pq
    if path.stat().st_size > 256 * 1024 * 1024:
        return []
    with pq.ParquetFile(path, thrift_string_size_limit=1_000_000,
                        thrift_container_size_limit=100_000) as source:
        if 'snapshot_id' not in source.schema.names:
            return []
        batch = next(source.iter_batches(batch_size=3, columns=['snapshot_id']), None)
        return [int(row['snapshot_id']) for row in batch.to_pylist() if row['snapshot_id'] is not None] if batch is not None else []


async def build_notebook_evidence(db, *, model_type=None, model_id=None, dataset_id=None, job_id=None):
    from db_models import MLModel, MLDataset, BackgroundTaskHistory, MLFeatureSnapshot
    from backend.ml.dataset_builder import serialize_dataset
    from backend.ml.registry_service import serialize_model_row
    from backend.ml.dataset_definitions import get_definition, feature_set_limitations
    from backend.ml.service_deployment import service_status
    from backend.ml.feature_store import feature_store

    if model_type and model_type not in MODEL_SPECS:
        raise NotebookEvidenceError('UNKNOWN_SERVICE', 'Choose one of the configured ML services.')
    if not any((model_type, model_id, dataset_id, job_id)):
        raise NotebookEvidenceError('CONTEXT_REQUIRED', 'Choose a service, model, dataset or run.')
    if job_id and not JOB_ID.fullmatch(job_id):
        raise NotebookEvidenceError('INVALID_REFERENCE', 'Choose a valid ML run identifier.')
    model = await db.get(MLModel, _id(model_id)) if model_id else None
    if model_id and model is None:
        raise NotebookEvidenceError('MODEL_NOT_FOUND', 'The selected model no longer exists.', 404)
    if model:
        if model_type and model_type != model.model_type:
            raise NotebookEvidenceError('CONTEXT_MISMATCH', 'The model belongs to another service.')
        if dataset_id and _id(dataset_id) != model.dataset_id:
            raise NotebookEvidenceError('CONTEXT_MISMATCH', 'The selected dataset did not train this model.')
        if job_id and job_id != model.training_job_id:
            raise NotebookEvidenceError('CONTEXT_MISMATCH', 'The selected run did not train this model.')
        model_type, dataset_id, job_id = model.model_type, model.dataset_id, model.training_job_id
    task = (await db.execute(select(BackgroundTaskHistory).where(BackgroundTaskHistory.job_id == job_id))).scalar_one_or_none() if job_id else None
    if job_id and task is None and model is None:
        raise NotebookEvidenceError('JOB_NOT_FOUND', 'The selected ML run no longer exists.', 404)
    if task and task.task_type not in ALLOWED_JOBS:
        raise NotebookEvidenceError('JOB_NOT_SUPPORTED', 'Choose a feature preparation, dataset build or training run.', 422)
    details, result, payload = ((task.details or {}), (task.result or {}), (task.payload or {})) if task else ({}, {}, {})
    if task:
        family = details.get('model_type') or payload.get('model_type')
        if model_type and family and family != model_type:
            raise NotebookEvidenceError('CONTEXT_MISMATCH', 'The run belongs to another service.')
        model_type = model_type or family
        task_dataset = result.get('dataset_id') or details.get('dataset_id') or payload.get('dataset_id')
        if dataset_id and task_dataset and _id(dataset_id) != _id(task_dataset):
            raise NotebookEvidenceError('CONTEXT_MISMATCH', 'The dataset does not match the selected run.')
        dataset_id = dataset_id or task_dataset
        if model is None and task.task_type == 'ml_training':
            model = (await db.execute(select(MLModel).where(MLModel.training_job_id == job_id)
                     .order_by(MLModel.created_at.desc()).limit(1))).scalar_one_or_none()
            if model:
                if model_type and model_type != model.model_type:
                    raise NotebookEvidenceError('CONTEXT_MISMATCH', 'The registered model and run disagree on their service.')
                if dataset_id and _id(dataset_id) != model.dataset_id:
                    raise NotebookEvidenceError('CONTEXT_MISMATCH', 'The registered model and run disagree on their dataset.')
                model_type, dataset_id = model.model_type, model.dataset_id
    dataset = await db.get(MLDataset, _id(dataset_id)) if dataset_id else None
    if dataset_id and dataset is None:
        raise NotebookEvidenceError('DATASET_NOT_FOUND', 'The linked dataset no longer exists.', 404)
    if dataset and not model_type:
        families = [key for key, spec in MODEL_SPECS.items() if spec.dataset_definition == dataset.definition_name
                    and spec.feature_set_version == dataset.feature_set_version]
        if len(families) == 1:
            model_type = families[0]
    if model_type and model_type not in MODEL_SPECS:
        raise NotebookEvidenceError('UNKNOWN_SERVICE', 'The recorded model service is unsupported.')
    spec = get_model_spec(model_type) if model_type else None
    if dataset and spec and model_type != 'tabular_regression_model':
        # Historical feature versions are inspectable for a linked model, but
        # another family's dataset must never be silently relabelled.
        if dataset.definition_name and dataset.definition_name != spec.dataset_definition:
            raise NotebookEvidenceError('CONTEXT_MISMATCH', 'The dataset belongs to another service.')
        known_schema = dataset.feature_set_version in {s.feature_set_version for s in MODEL_SPECS.values() if s.feature_set_version}
        if not model and known_schema and dataset.feature_set_version != spec.feature_set_version:
            raise NotebookEvidenceError('CONTEXT_MISMATCH', 'The saved feature schema belongs to another service.')
    build_task = task if task and task.task_type == 'ml_dataset_build' else None
    if dataset and dataset.build_job_id:
        build_task = (task if task and task.job_id == dataset.build_job_id else
            (await db.execute(select(BackgroundTaskHistory).where(BackgroundTaskHistory.job_id == dataset.build_job_id))).scalar_one_or_none())
    build_details = (build_task.details or {}) if build_task else {}
    diagnostics = build_details.get('dataset_diagnostics') or (build_details if build_details.get('diagnostics_version') == 1 else {})
    diagnostics = _subset(diagnostics, ('diagnostics_version','stage','stage_history','failure','configuration','dataset_id'))
    training = details.get('training_diagnostics') or (_subset(details, ('stage','stage_history','resource_usage')) if task and task.task_type == 'ml_training' and not details.get('diagnostics_version') else {})
    model_data = serialize_model_row(model) if model else {}
    model_data = {k:v for k,v in model_data.items() if k not in ('notes','rejection_reason','shadow_approval')}
    training = dict(training)
    training['configuration'] = model_data.get('training_config') or _subset(payload, ('model_type','algorithm','dataset_id','seed','hyperparameters','pipeline','run_options','prepare_features','sampling_policy'))
    for key in ('evaluation_report', 'quality_gates'):
        training['evaluation' if key == 'evaluation_report' else key] = model_data.get(key)
    training['reproducibility'] = (model_data.get('training_config') or {}).get('reproducibility')
    if task and task.error_code and task.task_type == 'ml_training':
        training['failure'] = {'code':task.error_code,'log_reference':task.job_id}
    preparation = details.get('preparation') or (result if task and task.task_type == 'ml_feature_computation' else None)
    if preparation is None and payload.get('prepare_features') and payload.get('dataset_id'):
        preparation = {'status':'reused', 'note':'The run selected an immutable saved dataset; fresh feature preparation was skipped.'}
    available, relative, path = artifact_reference(dataset)
    query = select(MLFeatureSnapshot)
    lineage_scope = 'not_available'
    if available:
        try:
            ids = _snapshot_ids(path)
        except (OSError, ValueError, TypeError):
            ids = []
        query = query.where(MLFeatureSnapshot.id.in_(ids))
        lineage_scope = 'first_three_saved_dataset_rows' if ids else 'dataset_lineage_unavailable'
    elif dataset:
        query = None  # Never substitute unrelated current samples for a missing saved artifact.
    elif task and task.task_type == 'ml_feature_computation':
        query = query.where(MLFeatureSnapshot.computation_run_id == task.job_id)
        if spec and spec.feature_set_version:
            query = query.where(MLFeatureSnapshot.entity_type == spec.entity_type,
                                MLFeatureSnapshot.feature_set_version == spec.feature_set_version)
        lineage_scope = 'selected_collection_run'
    elif spec and spec.feature_set_version:
        query = query.where(MLFeatureSnapshot.entity_type == spec.entity_type,
                            MLFeatureSnapshot.feature_set_version == spec.feature_set_version)
        lineage_scope = 'latest_family_examples_not_training_membership'
    else:
        query = None
    snapshots = (await db.execute(query.order_by(MLFeatureSnapshot.id.desc()).limit(3))).scalars().all() if query is not None else []
    feature_set = dataset.feature_set_version if dataset else spec.feature_set_version if spec else None
    definitions = await feature_store.get_definitions_for_feature_set(db, feature_set) if feature_set else []
    # These definitions are current inspection references. Dataset replay uses
    # only frozen debug_contract definitions, never these as historical truth.
    from backend.ml.build_provenance import inspect_provenance
    export_identity = inspect_provenance()
    services = await service_status(db)
    service = next((s for s in services['items'] if s['model_type'] == model_type), None)
    if service:
        service = {**service, 'captured_at': services['generated_at'],
                   'scope_note':'Current service status at export time; not historical training state.'}
    data = {
        'job': {k:getattr(task,k) for k in ('job_id','task_type','status','error_code','request_id','started_at','completed_at','progress_percent')} if task else None,
        'dataset': serialize_dataset(dataset) if dataset else None,
        'diagnostics': diagnostics, 'artifact_available': available, 'artifact_relative': relative,
        'pipeline': {'exported_at':datetime.now(timezone.utc).isoformat(), 'model_type':model_type,
            'export_code_version':export_identity,
            'model_spec':asdict(spec) if spec else None,
            'dataset_definition':get_definition(spec.dataset_definition).to_manifest() if spec else None,
            'preparation':preparation, 'training':training, 'model':model_data or None, 'service':service,
            'feature_lineage':{'scope':lineage_scope,
                'integrity_note':'Lineage previews are inspection evidence; the notebook verifies the entire saved artifact before replay.', 'samples':[{
                'id':r.id,'entity_type':r.entity_type,'as_of':r.as_of_timestamp,'event_timestamp':r.event_timestamp,
                'feature_set_version':r.feature_set_version,'features':r.features,'missingness':r.unavailable_features,
                'source_row_counts':r.source_row_counts,'computation_run_id':r.computation_run_id,
                'features_checksum':r.features_checksum} for r in snapshots],
                'definitions':definitions,'definitions_scope':'Current definitions at export; frozen validation definitions remain in dataset.quality_report.debug_contract.'},
            'limitations':feature_set_limitations(feature_set) if feature_set else []}}
    return safe_evidence(data)
