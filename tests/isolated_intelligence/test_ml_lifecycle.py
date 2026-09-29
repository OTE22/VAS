"""Lifecycle regressions using only disposable PostgreSQL and generated rows."""
import asyncio
from datetime import datetime, timedelta
import uuid

import pytest
from sqlalchemy import text
from conftest import engine, session, settings
from db_models import Base, MLModel, MLFeatureSnapshot, MLPrediction, MLShadowComparison
from backend.ml.drift_service import drift_service
from backend.ml.model_specs import get_model_spec
from backend.ml.dataset_builder import _select_supervised_features

run = asyncio.run


@pytest.fixture
def database():
    if engine is None:
        pytest.skip('Disposable database required')
    settings.ML_DRIFT_MIN_SAMPLES = 2
    settings.ML_DRIFT_PSI_WARNING = .1
    settings.ML_DRIFT_PSI_CRITICAL = .25
    async def setup():
        async with engine.begin() as db:
            await db.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            await db.run_sync(Base.metadata.create_all)
            await db.execute(text('TRUNCATE ml_models, ml_feature_snapshots CASCADE'))
    run(setup())


def model(family='behavior_anomaly_model', version=1):
    spec = get_model_spec(family)
    return MLModel(id=uuid.uuid4(), model_type=family, version=version,
        stage='validated', algorithm=spec.default_algorithm, artifact_name='fixture',
        artifact_path='/tmp/never-loaded', artifact_hash='a'*64,
        feature_set_version=spec.feature_set_version, feature_names=['visits', 'gap'])


def snapshots(row, *, days, schema=None, entity_type=None, missing=False):
    spec = get_model_spec(row.model_type)
    return [MLFeatureSnapshot(entity_id=str(uuid.uuid4()), entity_type=entity_type or spec.entity_type,
        feature_set_version=schema or row.feature_set_version,
        as_of_timestamp=datetime.utcnow() - timedelta(days=days, seconds=i),
        computed_at=datetime.utcnow() - timedelta(days=days, seconds=i),
        features={'visits': float(i), **({} if missing else {'gap': float(i + 1)})}) for i in range(3)]


def predictions(row, *, days, score=0.1, fallback=None):
    return [MLPrediction(id=uuid.uuid4(), subject_id=str(uuid.uuid4()), model_id=row.id,
        model_type=row.model_type, model_version_label='v'+str(row.version),
        requested_mode='shadow', actual_mode_used='shadow', behavioral_anomaly_score=score,
        as_of_timestamp=datetime.utcnow() - timedelta(days=days),
        created_at=datetime.utcnow() - timedelta(days=days, seconds=i),
        idempotency_key=str(uuid.uuid4()), fallback_reason=fallback) for i in range(3)]


@pytest.mark.parametrize('family', ['behavior_anomaly_model', 'coappearance_anomaly_model', 'social_graph_anomaly_model'])
def test_data_drift_uses_model_entity_schema_and_features(database, family):
    async def exercise():
        async with session() as db:
            row = model(family)
            db.add(row)
            await db.flush()
            for days in (2, 10):
                db.add_all(snapshots(row, days=days))
                db.add_all(snapshots(row, days=days, schema='unrelated-schema'))
                db.add_all(snapshots(row, days=days, entity_type='pipeline'))
            await db.flush()
            report = await drift_service.run_data_drift(db, model_id=row.id)
            assert report['sample_count'] == report['baseline_sample_count'] == 3
            assert report['insufficient_data'] is False
            assert report['metrics']['worst_psi'] == 0
            assert set(report['metrics']['features']) == {'visits', 'gap'}
            assert report['metrics']['entity_type'] == get_model_spec(family).entity_type
    run(exercise())


def test_missing_feature_cannot_disappear_from_drift_report(database):
    async def exercise():
        async with session() as db:
            row = model()
            db.add(row)
            await db.flush()
            db.add_all(snapshots(row, days=10) + snapshots(row, days=2, missing=True))
            await db.flush()
            report = await drift_service.run_data_drift(db, model_id=row.id)
            assert report['sample_count'] == 3
            assert report['assessment_status'] == 'insufficient_data'
            feature = report['metrics']['features']['gap']
            assert feature['current_missing_rate'] == 1
            assert feature['baseline_missing_rate'] == 0
            assert feature['psi'] is None
    run(exercise())


def test_prediction_drift_and_shadow_health_isolate_model_and_time(database):
    async def exercise():
        async with session() as db:
            row, other = model(), model(version=2)
            db.add_all([row, other])
            await db.flush()
            for target in (row, other):
                before = predictions(target, days=10)
                current = predictions(target, days=2, score=.9 if target is other else .1)
                future = predictions(target, days=-2)
                db.add_all(before + current + future)
                await db.flush()
                for pred in current + future:
                    db.add(MLShadowComparison(prediction_id=pred.id, model_id=target.id,
                        subject_id=pred.subject_id, rule_threat_score=0, rule_threat_severity='low',
                        operational_disagreement='anomaly_only' if target is other else 'neither',
                        ml_failed=target is other, ml_latency_ms=1, created_at=pred.created_at))
            await db.flush()
            report = await drift_service.run_prediction_drift(db, model_id=row.id)
            assert report['sample_count'] == report['baseline_sample_count'] == 3
            assert report['insufficient_data'] is False
            assert report['metrics']['anomaly_score_psi'] == 0
            assert report['metrics']['operational_disagreement'] == {'neither': 3}
            assert report['metrics']['shadow_failure_rate'] == 0
    run(exercise())


def test_missing_family_telemetry_is_explicit(database):
    async def exercise():
        async with session() as db:
            row = model('coappearance_anomaly_model')
            db.add(row)
            await db.flush()
            report = await drift_service.run_prediction_drift(db, model_id=row.id)
            assert report['insufficient_data'] is True
            assert report['metrics']['score_source_supported'] is False
            assert report['metrics']['unavailable_reason']
    run(exercise())


def test_truncation_and_nonfinite_prediction_are_not_healthy_evidence(database, monkeypatch):
    import backend.ml.drift_service as module
    monkeypatch.setattr(module, 'DRIFT_SAMPLE_LIMIT', 2)
    async def exercise():
        async with session() as db:
            row = model()
            db.add(row)
            await db.flush()
            db.add_all(snapshots(row, days=10) + snapshots(row, days=2))
            db.add_all(predictions(row, days=10) + predictions(row, days=2, score=float('nan')))
            await db.flush()
            data = await drift_service.run_data_drift(db, model_id=row.id)
            pred = await drift_service.run_prediction_drift(db, model_id=row.id)
            assert data['insufficient_data'] and pred['insufficient_data']
            assert data['metrics']['truncated']['current']
            assert pred['metrics']['invalid_scores']['current'] == 2
            assert pred['sample_count'] == 0
    run(exercise())


def test_fallback_scores_do_not_enter_prediction_distribution(database):
    async def exercise():
        async with session() as db:
            row = model()
            db.add(row)
            await db.flush()
            db.add_all(predictions(row, days=10) + predictions(row, days=2)
                       + predictions(row, days=2, score=.9, fallback='PREDICTION_FAILED'))
            await db.flush()
            report = await drift_service.run_prediction_drift(db, model_id=row.id)
            assert report['sample_count'] == 3
            assert report['metrics']['prediction_volume']['current'] == 6
            assert report['metrics']['fallback_rate'] == .5
            assert report['metrics']['anomaly_score_psi'] == 0
    run(exercise())


@pytest.mark.parametrize('strategy', ['temporal', 'temporal_group'])
def test_sparse_feature_selection_never_uses_heldout_values(strategy):
    rows = [dict(entity_id=str(i), as_of=datetime(2025, 1, 1) + timedelta(days=i),
                 features={'stable': i, 'heldout_only': None if i < 6 else i,
                           'train_only': i if i < 6 else None}) for i in range(10)]
    definitions = [{'name': name} for name in rows[0]['features']]
    args = dict(strategy=strategy, val_fraction=.2, holdout_fraction=.2)
    selected, names, policy = _select_supervised_features(rows, definitions, **args)
    assert policy['training_rows'] == 6
    assert policy['excluded_sparse_features'] == {'heldout_only': 1.0}
    assert [d['name'] for d in names] == ['stable', 'train_only']
    assert all('heldout_only' not in row['features'] for row in selected)
    assert 'heldout_only' in rows[0]['features']  # original inspection data preserved
    for row in rows[6:]:
        row['features'] = {'heldout_only': 9000}
    _, changed_names, changed_policy = _select_supervised_features(rows, definitions, **args)
    assert changed_names == names and changed_policy == policy


def test_null_and_absent_features_share_training_only_imputation():
    from backend.ml.trainer import _assemble_matrix
    rows = [{'features': {'visits': value, 'all_null': None}} for value in [0., 2., 4., None]]
    names, medians, matrix_of = _assemble_matrix(rows)
    assert names == ['visits'] and medians == {'visits': 2.}
    assert matrix_of(rows).tolist() == [[0.], [2.], [4.], [2.]]
    # Held-out values never change the persisted median; null and absent agree.
    assert matrix_of([{'features': {'visits': 10000}}, {'features': {}}, {'features': {'visits': None}}]).tolist() == [[10000.], [2.], [2.]]


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), True, '2'])
def test_invalid_training_feature_fails_explicitly(bad):
    from backend.ml.trainer import _assemble_matrix
    from backend.ml.registry_service import RegistryError
    with pytest.raises(RegistryError) as error:
        _assemble_matrix([{'features': {'visits': bad}}])
    assert error.value.code == 'INVALID_FEATURE_VALUE'
