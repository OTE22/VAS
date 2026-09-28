"""Notebook exports remain read-only, correctly scoped, and credential-free."""
import asyncio
from datetime import datetime
import json
import sys
from types import ModuleType, SimpleNamespace as Row
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, text
from conftest import engine, session, settings
from backend.ml import notebook_evidence as evidence
from db_models import Base, BackgroundTaskHistory

run = asyncio.run
FAMILY = 'behavior_anomaly_model'
DATASET_ID = uuid.UUID('aaaaaaaa-1111-4111-8111-111111111111')
MODEL_ID = uuid.UUID('bbbbbbbb-2222-4222-8222-222222222222')
OTHER_ID = uuid.UUID('cccccccc-3333-4333-8333-333333333333')


def dataset(**overrides):
    data = dict(id=DATASET_ID, definition_name='behavior_anomaly_person',
                feature_set_version='secintel-features-v2', storage_path=None, build_job_id=None,
                kind='unsupervised', quality_report={}, parquet_sha256='a' * 64)
    return Row(**{**data, **overrides})


def model(**overrides):
    data = dict(id=MODEL_ID, model_type=FAMILY, dataset_id=DATASET_ID, training_job_id='mltrain-test',
                training_config={'algorithm': 'isolation_forest'}, quality_gates={'passed': True},
                evaluation_report={'holdout_rows': 12}, version=2)
    return Row(**{**data, **overrides})


def task(**overrides):
    data = dict(job_id='mltrain-test', task_type='ml_training', status='completed',
                details={'model_type': FAMILY}, result={}, payload={}, error_code=None,
                request_id='request-fixture', started_at=None, completed_at=None, progress_percent=100)
    return Row(**{**data, **overrides})


def snapshot(identifier=7):
    return Row(id=identifier, entity_type='person', as_of_timestamp=datetime(2026, 1, 1),
               event_timestamp=datetime(2026, 1, 1), feature_set_version='secintel-features-v2',
               features={'visits': 2}, unavailable_features={}, source_row_counts={'appearance': 2},
               computation_run_id='features-test', features_checksum='d' * 64)


class FakeDB:
    def __init__(self, *, datasets=(), models=(), tasks=(), snapshots=()):
        self.datasets = {str(row.id): row for row in datasets}
        self.models = {str(row.id): row for row in models}
        self.tasks = {row.job_id: row for row in tasks}
        self.snapshots = list(snapshots)
        self.queries = []
        self.gets = []

    async def get(self, cls, key):
        self.gets.append((cls.__name__, key))
        return (self.models if cls.__name__ == 'MLModel' else self.datasets).get(str(key))

    async def execute(self, statement):
        cls = statement.column_descriptions[0]['entity'].__name__
        params = statement.compile().params
        self.queries.append((cls, statement, params))
        if cls == 'BackgroundTaskHistory':
            value = self.tasks.get(params.get('job_id_1'))
        elif cls == 'MLModel':
            value = next((row for row in self.models.values()
                          if row.training_job_id == params.get('training_job_id_1')), None)
        elif cls == 'MLFeatureSnapshot':
            return Row(scalars=lambda: Row(all=lambda: self.snapshots))
        else:
            raise AssertionError('Unexpected evidence query: ' + cls)
        return Row(scalar_one_or_none=lambda: value)


@pytest.fixture
def isolated_exports(monkeypatch, tmp_path):
    # The evidence builder itself is real. Replace only large neighboring services
    # so these tests cannot import startup, execute a job, or contact production.
    dataset_module = ModuleType('backend.ml.dataset_builder')
    dataset_module.serialize_dataset = lambda row: dict(vars(row))
    registry_module = ModuleType('backend.ml.registry_service')
    registry_module.serialize_model_row = lambda row: dict(vars(row))
    service_module = ModuleType('backend.ml.service_deployment')
    service_module.service_status = AsyncMock(return_value={
        'generated_at': '2026-01-01T00:00:00Z', 'items': [
            {'model_type': FAMILY, 'state': 'not_deployed', 'destination': {'url': '/admin/security-intelligence'}}]})
    feature_module = ModuleType('backend.ml.feature_store')
    feature_module.feature_store = Row(get_definitions_for_feature_set=AsyncMock(return_value=[
        {'feature_name': 'visits', 'definition_version': '1'}]))
    for name, module in [('backend.ml.dataset_builder', dataset_module),
                         ('backend.ml.registry_service', registry_module),
                         ('backend.ml.service_deployment', service_module),
                         ('backend.ml.feature_store', feature_module)]:
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(settings, 'ML_ARTIFACT_DIR', str(tmp_path), raising=False)
    return Row(root=tmp_path, service=service_module, features=feature_module)


def build(db=None, **selection):
    return run(evidence.build_notebook_evidence(db or FakeDB(), **selection))


@pytest.mark.parametrize('selection,code,status', [
    ({}, 'CONTEXT_REQUIRED', 422), ({'model_type': 'fictional'}, 'UNKNOWN_SERVICE', 422),
    ({'job_id': '../secret'}, 'INVALID_REFERENCE', 422), ({'job_id': 'x' * 129}, 'INVALID_REFERENCE', 422),
    ({'model_id': 'bad'}, 'INVALID_REFERENCE', 422), ({'dataset_id': 'bad'}, 'INVALID_REFERENCE', 422),
    ({'model_id': str(MODEL_ID)}, 'MODEL_NOT_FOUND', 404),
    ({'dataset_id': str(DATASET_ID)}, 'DATASET_NOT_FOUND', 404),
    ({'job_id': 'missing'}, 'JOB_NOT_FOUND', 404)])
def test_missing_invalid_or_unsupported_selection_is_actionable(isolated_exports, selection, code, status):
    with pytest.raises(evidence.NotebookEvidenceError) as exc:
        build(**selection)
    assert (exc.value.code, exc.value.status) == (code, status)


def test_model_selection_canonicalizes_and_links_exact_run_and_dataset(isolated_exports):
    db = FakeDB(models=[model()], datasets=[dataset()], tasks=[task()])
    value = build(db, model_id=str(MODEL_ID).upper().replace('-', ''))
    assert value['pipeline']['model']['id'] == str(MODEL_ID)
    assert value['dataset']['id'] == str(DATASET_ID)
    assert value['job']['job_id'] == 'mltrain-test'
    assert value['pipeline']['model_type'] == FAMILY
    assert value['pipeline']['training']['evaluation'] == {'holdout_rows': 12}
    assert db.gets[0] == ('MLModel', MODEL_ID)


@pytest.mark.parametrize('selection', [
    {'model_type': 'social_graph_anomaly_model'}, {'dataset_id': str(OTHER_ID)}, {'job_id': 'wrong-run'}])
def test_explicit_model_links_cannot_be_overridden(isolated_exports, selection):
    with pytest.raises(evidence.NotebookEvidenceError) as exc:
        build(FakeDB(models=[model()]), model_id=MODEL_ID, **selection)
    assert exc.value.code == 'CONTEXT_MISMATCH'


def test_training_job_discovers_its_registered_model(isolated_exports):
    db = FakeDB(models=[model()], datasets=[dataset()], tasks=[task(result={'dataset_id': str(DATASET_ID)})])
    value = build(db, job_id='mltrain-test')
    assert value['pipeline']['model']['id'] == str(MODEL_ID)
    assert value['dataset']['id'] == str(DATASET_ID)


def test_unrelated_background_job_is_rejected(isolated_exports):
    db = FakeDB(tasks=[task(task_type='retention')])
    with pytest.raises(evidence.NotebookEvidenceError) as exc:
        build(db, job_id='mltrain-test')
    assert exc.value.code == 'JOB_NOT_SUPPORTED'


def test_dataset_derives_family_and_recorded_build_diagnostics(isolated_exports):
    saved = dataset(build_job_id='dataset-test')
    build_run = task(job_id='dataset-test', task_type='ml_dataset_build', details={
        'diagnostics_version': 1, 'stage': 'validating', 'stage_history': [{'stage': 'extracting', 'status': 'completed'}]})
    value = build(FakeDB(datasets=[saved], tasks=[build_run]), dataset_id=DATASET_ID)
    assert value['pipeline']['model_type'] == FAMILY
    assert value['diagnostics']['stage'] == 'validating'
    assert value['pipeline']['model'] is None


def test_missing_historical_job_does_not_erase_model_lineage(isolated_exports):
    value = build(FakeDB(models=[model()], datasets=[dataset()]), model_id=MODEL_ID)
    assert value['job'] is None
    assert value['pipeline']['model']['training_job_id'] == 'mltrain-test'
    assert value['pipeline']['training']['configuration']['algorithm'] == 'isolation_forest'


def test_legacy_dataset_of_another_known_schema_cannot_be_relabelled(isolated_exports):
    db = FakeDB(datasets=[dataset(definition_name=None, feature_set_version='coappearance-features-v1')])
    with pytest.raises(evidence.NotebookEvidenceError) as exc:
        build(db, dataset_id=DATASET_ID, model_type=FAMILY)
    assert exc.value.code == 'CONTEXT_MISMATCH'


def test_linked_historical_model_keeps_its_older_dataset_inspectable(isolated_exports):
    db = FakeDB(models=[model()], datasets=[dataset(feature_set_version='legacy-secintel-v1')])
    value = build(db, model_id=MODEL_ID)
    assert value['dataset']['feature_set_version'] == 'legacy-secintel-v1'
    assert value['pipeline']['model_type'] == FAMILY


@pytest.mark.parametrize('task_type', ['ml_feature_computation', 'ml_dataset_build', 'ml_training'])
def test_failure_is_assigned_to_training_only_for_training_jobs(isolated_exports, task_type):
    run_row = task(task_type=task_type, status='failed', error_code='INSUFFICIENT_DATA')
    value = build(FakeDB(tasks=[run_row]), job_id='mltrain-test')
    assert value['job']['error_code'] == 'INSUFFICIENT_DATA'
    assert ('failure' in value['pipeline']['training']) == (task_type == 'ml_training')


@pytest.mark.parametrize('run_changes,selection', [
    ({'details': {'model_type': 'social_graph_anomaly_model'}}, {'model_type': FAMILY}),
    ({'result': {'dataset_id': str(OTHER_ID)}}, {'dataset_id': str(DATASET_ID)}),
    ({'details': {'model_type': 'unsupported_recorded_family'}}, {})])
def test_job_context_disagreement_is_never_silently_relabelled(isolated_exports, run_changes, selection):
    with pytest.raises(evidence.NotebookEvidenceError) as exc:
        build(FakeDB(tasks=[task(**run_changes)]), job_id='mltrain-test', **selection)
    assert exc.value.code in ('CONTEXT_MISMATCH', 'UNKNOWN_SERVICE')


def test_missing_snapshot_never_substitutes_latest_family_rows(isolated_exports):
    db = FakeDB(datasets=[dataset(storage_path=str(isolated_exports.root/'datasets'/'missing.parquet'))],
                snapshots=[snapshot()])
    value = build(db, dataset_id=DATASET_ID)
    assert value['artifact_available'] is False
    assert value['pipeline']['feature_lineage']['samples'] == []
    assert not any(cls == 'MLFeatureSnapshot' for cls, _, _ in db.queries)


def test_service_only_examples_are_explicitly_not_training_membership(isolated_exports):
    db = FakeDB(snapshots=[snapshot()])
    value = build(db, model_type=FAMILY)
    assert value['pipeline']['feature_lineage']['scope'] == 'latest_family_examples_not_training_membership'
    assert value['pipeline']['service']['captured_at'] == '2026-01-01T00:00:00Z'
    _, query, params = next(item for item in db.queries if item[0] == 'MLFeatureSnapshot')
    assert params['entity_type_1'] == 'person'
    assert params['feature_set_version_1'] == 'secintel-features-v2'
    assert query._limit_clause.value == 3


def test_collection_examples_are_scoped_to_selected_run(isolated_exports):
    feature_run = task(job_id='features-test', task_type='ml_feature_computation',
                       result={'snapshots_written': 2})
    db = FakeDB(tasks=[feature_run], snapshots=[snapshot()])
    value = build(db, job_id='features-test')
    assert value['pipeline']['feature_lineage']['scope'] == 'selected_collection_run'
    params = next(params for cls, _, params in db.queries if cls == 'MLFeatureSnapshot')
    assert params['computation_run_id_1'] == 'features-test'


def test_dataset_lineage_queries_only_ids_from_saved_snapshot(isolated_exports, monkeypatch):
    path = isolated_exports.root/'datasets'/'saved.parquet'
    path.parent.mkdir(); path.write_bytes(b'synthetic artifact; parser is stubbed')
    monkeypatch.setattr(evidence, '_snapshot_ids', lambda source: [7, 9])
    db = FakeDB(datasets=[dataset(storage_path=str(path))], snapshots=[snapshot()])
    value = build(db, dataset_id=DATASET_ID)
    assert value['artifact_relative'] == 'datasets/saved.parquet'
    assert value['pipeline']['feature_lineage']['scope'] == 'first_three_saved_dataset_rows'
    params = next(params for cls, _, params in db.queries if cls == 'MLFeatureSnapshot')
    assert params['id_1'] == [7, 9]


def test_artifact_reference_refuses_traversal_wrong_format_and_symlink_escape(isolated_exports, tmp_path):
    root = isolated_exports.root
    (root/'datasets').mkdir()
    outside = root/'outside.parquet'; outside.write_bytes(b'x')
    (root/'datasets'/'escape.parquet').symlink_to(outside)
    for path in (outside, root/'datasets'/'..'/'outside.parquet', root/'datasets'/'escape.parquet', root/'datasets'/'data.pkl'):
        assert evidence.artifact_reference(dataset(storage_path=str(path))) == (False, None, None)
    inside = root/'datasets'/'saved.parquet'; inside.write_bytes(b'x')
    assert evidence.artifact_reference(dataset(storage_path=str(inside))) == (True, 'datasets/saved.parquet', inside)


def test_structured_redaction_retains_numbers_and_lineage_but_drops_sensitive_values():
    safe = evidence.safe_evidence({'password': 'sentinel', 'nested': {'access_token': 'sentinel',
        'api_url': 'https://private', 'credential_path': '/private', 'dataset_id': DATASET_ID,
        'value': float('nan'), 'count': 3}, 'notes': 'https://private', 'artifact': '/private/path',
        'timestamp': datetime(2026, 1, 1)})
    serialized = json.dumps(safe)
    assert 'sentinel' not in serialized and 'https://private' not in serialized and '/private' not in serialized
    assert safe['nested'] == {'dataset_id': str(DATASET_ID), 'value': None, 'count': 3}
    assert safe['timestamp'] == '2026-01-01T00:00:00Z'


def test_nested_phase_evidence_and_reused_dataset_are_preserved(isolated_exports):
    run_row = task(payload={'prepare_features': True, 'dataset_id': str(DATASET_ID)}, details={
        'model_type': FAMILY, 'training_diagnostics': {'stage': 'evaluating', 'stage_history': [{'stage': 'fit'}]},
        'dataset_diagnostics': {'diagnostics_version': 1, 'stage': 'built'}})
    value = build(FakeDB(tasks=[run_row], datasets=[dataset(build_job_id='mltrain-test')]), job_id='mltrain-test')
    assert value['pipeline']['preparation']['status'] == 'reused'
    assert value['pipeline']['training']['stage'] == 'evaluating'
    assert value['diagnostics']['stage'] == 'built'


@pytest.mark.skipif(engine is None, reason='Disposable PostgreSQL not supplied')
def test_progress_merge_preserves_other_phases_while_default_replace_is_unchanged():
    """Real PostgreSQL JSONB merge across committed sessions, never a live DSN."""
    from backend.core.task_history import task_history_manager
    job_id = 'notebook-progress-' + uuid.uuid4().hex[:8]
    async def check():
        async with engine.begin() as connection:
            await connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            await connection.run_sync(Base.metadata.create_all)
        try:
            async with session() as db:
                db.add(BackgroundTaskHistory(job_id=job_id, task_type='ml_training',
                    task_name='Notebook phase preservation fixture', status='running',
                    details={'model_type': FAMILY, 'preparation': {'status': 'prepared', 'snapshots_written': 11}}))
            await task_history_manager.update_progress(job_id, 30, details={
                'dataset_diagnostics': {'stage': 'built', 'stage_history': [{'stage': 'validation', 'status': 'completed'}]}},
                merge_details=True)
            await task_history_manager.update_progress(job_id, 70, details={
                'training_diagnostics': {'stage': 'fitting', 'stage_history': [{'stage': 'fitting'}]}}, merge_details=True)
            async with session() as db:
                row = (await db.execute(select(BackgroundTaskHistory).where(BackgroundTaskHistory.job_id == job_id))).scalar_one()
                assert row.progress_percent == 70
                assert row.details['preparation']['snapshots_written'] == 11
                assert row.details['dataset_diagnostics']['stage_history'][0]['status'] == 'completed'
                assert row.details['training_diagnostics']['stage'] == 'fitting'
                assert row.details['model_type'] == FAMILY
            await task_history_manager.update_progress(job_id, 80, details={'stage': 'legacy-replace'})
            async with session() as db:
                row = (await db.execute(select(BackgroundTaskHistory).where(BackgroundTaskHistory.job_id == job_id))).scalar_one()
                assert row.details == {'stage': 'legacy-replace'}
                assert row.progress_percent == 80
            await task_history_manager.update_progress(job_id, 90)
            async with session() as db:
                row = (await db.execute(select(BackgroundTaskHistory).where(BackgroundTaskHistory.job_id == job_id))).scalar_one()
                assert row.details == {'stage': 'legacy-replace'} and row.progress_percent == 90
        finally:
            async with session() as db:
                await db.execute(delete(BackgroundTaskHistory).where(BackgroundTaskHistory.job_id == job_id))
    run(check())
