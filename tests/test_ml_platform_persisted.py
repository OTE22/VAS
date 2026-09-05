"""Opt-in real PostgreSQL + MLflow workflow, isolated from user tables and files.

ML_PLATFORM_VERIFY_POSTGRES=1 python -m pytest tests/test_ml_platform_persisted.py
Creates a random schema, sets search_path to that schema ONLY, applies the new
migration there, and drops only that schema. No application startup/live APIs.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import importlib.util
import os
from pathlib import Path
import uuid
from unittest.mock import patch, AsyncMock
import pytest

@pytest.mark.skipif(os.environ.get("ML_PLATFORM_VERIFY_POSTGRES") != "1", reason="Explicit isolated PostgreSQL verification opt-in required")
def test_persisted_selection_training_registration_and_promotion(tmp_path):
    asyncio.run(workflow(tmp_path))

async def workflow(tmp_path):
    import httpx
    from fastapi import FastAPI, HTTPException
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from config import settings
    from db_connection import db_manager
    from db_models import Base, MLDataset, MLModel, MLFeatureDefinition, MLAuditLog, BackgroundTaskHistory, MLPipelineVersion, MLTrackingRun, MLModelThreshold
    from backend.routes import ml_ops
    from backend.ml.dataset_builder import _atomic_parquet_write, dataset_fingerprint
    from backend.ml.trainer import run_training_job
    from backend.ml.mlflow_tracking import sync_job, client
    from backend.core.task_history import task_history_manager
    schema = "mlverify_" + uuid.uuid4().hex
    assert schema.startswith("mlverify_") and len(schema) == 41 and schema.replace('_', '').isalnum()
    control = create_async_engine(settings.DATABASE_URL)
    engine = None
    try:
        async with control.begin() as conn:
            await conn.execute(text('CREATE SCHEMA "' + schema + '"'))
        engine = create_async_engine(settings.DATABASE_URL, connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as conn:
            assert (await conn.execute(text('SELECT current_schema()'))).scalar() == schema
            tables = [c.__table__ for c in (MLDataset, MLModel, MLModelThreshold, MLFeatureDefinition, MLAuditLog, BackgroundTaskHistory)]
            await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
            def migrate(sync):
                from alembic.migration import MigrationContext
                from alembic.operations import Operations
                path = Path(__file__).parents[1] / 'alembic/versions/fcc3d4e5f6a7_ml_platform_integrations.py'
                spec = importlib.util.spec_from_file_location('verify_migration', path); module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                with Operations.context(MigrationContext.configure(sync)):
                    module.upgrade()
            await conn.run_sync(migrate)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        @asynccontextmanager
        async def session():
            async with sessions() as db: yield db
        async def db_dependency():
            async with sessions() as db: yield db
        async def admin(): return {"id": 1, "username": "isolated-admin", "role": "admin"}
        async def no_limit(): pass
        app = FastAPI(); app.include_router(ml_ops.router)
        app.dependency_overrides[ml_ops.get_db] = db_dependency
        app.dependency_overrides[ml_ops._ML_MANAGE_CAPABILITY] = admin
        # No Redis or live limiter state is involved; CSRF and authorization wrappers remain real.
        for route in ml_ops.router.routes:
            for dependency in getattr(getattr(route, 'dependant', None), 'dependencies', []):
                if dependency.name == '_rl': app.dependency_overrides[dependency.call] = no_limit
        with patch.object(db_manager, 'get_session', session), patch.object(settings, 'ML_ARTIFACT_DIR', str(tmp_path)), patch.object(settings, 'MLFLOW_ENABLED', True), patch.object(settings, 'MLFLOW_TRACKING_URI', ''), patch.object(settings, 'XGBOOST_ENABLED', True), patch.object(settings, 'OPTUNA_ENABLED', True), patch.object(settings, 'SHAP_ENABLED', True), patch('backend.ml.metrics.refresh_state', new=AsyncMock()), patch('backend.ml.registry_service.RegistryService._bump_version_key', new=AsyncMock()):
            names = ['appearance_count_7d', 'appearance_count_30d']
            rows = [{"entity_id": str(uuid.uuid4()), "as_of": datetime(2025,1,1), "features": {names[0]: float(i % 20), names[1]: float(3 * (i % 20) + 1)}, "snapshot_id": i+1, "split": 'train' if i < 60 else 'val' if i < 75 else 'test', "label": None} for i in range(90)]
            checksum = dataset_fingerprint(rows); file = tmp_path / 'dataset.parquet'
            _atomic_parquet_write(rows, str(file), expected_checksum=checksum)
            original_bytes = file.read_bytes(); digest = hashlib.sha256(original_bytes).hexdigest(); dataset_id = uuid.uuid4()
            async with sessions() as db:
                db.add(MLDataset(id=dataset_id, name='isolated-synthetic', version=1, kind='unsupervised', feature_set_version='secintel-features-v2', checksum=checksum, parquet_sha256=digest, storage_path=str(file), row_count=90, status='built', quality_report={"passed": True}, split_config={"method":"fixture", "seed":42}))
                for name in names: db.add(MLFeatureDefinition(name=name, version=1, entity_type='person', source='synthetic-test', computation='appearance_count', params={}, leakage_class='safe'))
                await db.commit()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://isolated.test', headers={'X-Requested-With':'XMLHttpRequest'}) as api:
                async def get(path):
                    r = await api.get(path); assert r.status_code == 200, r.text; return r.json()
                config = {"name":"isolated-regression", "configuration":{"model_type":"tabular_regression_model", "algorithm":"xgboost_regressor", "target":names[1], "features":[names[0]], "metrics":["mae","rmse","r2"]}}
                denied = await api.post('/api/ml/pipelines', json=config, headers={'X-Requested-With':''}); assert denied.status_code == 403
                r = await api.post('/api/ml/pipelines', json=config); assert r.status_code == 201, r.text; pipeline = r.json()
                r = await api.post('/api/ml/pipelines', json=config); assert r.status_code == 201 and r.json()['version'] == 2
                assert len((await get('/api/ml/pipelines'))['items']) == 2
                explorer = await get('/api/ml/datasets/' + str(dataset_id) + '/explorer'); assert explorer['total_rows'] == 90
                await get('/api/ml/datasets/' + str(dataset_id) + '/validation-report')
                body = {'pipeline_id':pipeline['id'], 'dataset_id':str(dataset_id), 'seed':42, 'hyperparameters':{'n_estimators':20}, 'run_options':{'shap':True, 'optuna':{'enabled':True,'trials':2,'timeout_seconds':30,'search_space':{'max_depth':{'type':'int','low':2,'high':3}}}}}
                r = await api.post('/api/ml/training-jobs', json=body); assert r.status_code == 202, r.text; job_id = r.json()['job_id']
                async with sessions() as db:
                    task = (await db.execute(select(BackgroundTaskHistory).where(BackgroundTaskHistory.job_id == job_id))).scalar_one(); payload = task.payload
                    assert payload['pipeline']['version'] == 1
                await run_training_job(job_id, model_type=payload['model_type'], algorithm=payload['algorithm'], dataset_id=payload['dataset_id'], seed=payload['seed'], hyperparameters=payload['hyperparameters'], pipeline=payload['pipeline'], run_options=payload['run_options'])
                job = await task_history_manager.get_task_by_job_id(job_id); assert job['status'] == 'completed', job
                assert (await sync_job(job_id))['status'] == 'synchronized'
                model_id = job['result']['model_id']; model = await get('/api/ml/models/' + model_id)
                assert model['stage'] == 'validated', model
                assert model['training_config']['feature_names'] == [names[0]]
                assert model['training_config']['reproducibility']['dataset']['parquet_sha256'] == digest
                assert model['evaluation_report']['splits']['test']['rmse'] < model['evaluation_report']['splits']['test']['baseline_rmse']
                assert model['tracking']['status'] == 'synchronized', model['tracking']
                r = await api.post('/api/ml/models/' + model_id + '/promote', json={'reason':'reviewed isolated evidence','artifact_checksum':'0'*64}); assert r.status_code == 409
                r = await api.post('/api/ml/models/' + model_id + '/promote', json={'reason':'reviewed isolated evidence','artifact_checksum':model['artifact_hash']}); assert r.status_code == 200, r.text
                synced = await sync_job(job_id); assert synced['status'] == 'synchronized', synced
                assert str(client().get_model_version_by_alias(synced['registered_name'], 'approved').version) == synced['registered_version']
                await get('/api/ml/models/' + model_id + '/explanations?sample=1')
                r = await api.get('/api/ml/models/' + model_id + '/explanations/download/shap-global.png'); assert r.status_code == 200 and r.content.startswith(b'\x89PNG')
                r = await api.get('/api/ml/models/' + model_id + '/explanations/download/credentials'); assert r.status_code == 404
                # An outage records a retryable failure, retaining the local artifact and approval.
                with patch('backend.ml.mlflow_tracking.client', side_effect=RuntimeError('synthetic outage')):
                    assert (await sync_job(job_id))['status'] == 'failed'
                assert (await get('/api/ml/models/' + model_id))['stage'] == 'approved'
                assert (await sync_job(job_id))['status'] == 'synchronized'
                async def forbidden(): raise HTTPException(403, 'Permission denied')
                app.dependency_overrides[ml_ops._ML_MANAGE_CAPABILITY] = forbidden
                assert (await api.get('/api/ml/experiments')).status_code == 403
                async with sessions() as db:
                    actions = (await db.execute(select(MLAuditLog.action))).scalars().all()
                    assert 'pipeline_version_created' in actions and 'model_approved' in actions and 'mlflow_sync' in actions
                assert file.read_bytes() == original_bytes
    finally:
        if engine: await engine.dispose()
        # The identifier is generated above, never supplied by a user or derived from existing tables.
        async with control.begin() as conn: await conn.execute(text('DROP SCHEMA IF EXISTS "' + schema + '" CASCADE'))
        await control.dispose()
