"""Opt-in real ML runs against a disposable DB; never import in normal tests.

The caller must provide isolated container networking, no production mounts,
and an explicit output volume. Fixtures are synthetic, not accuracy evidence.
"""
import asyncio, json, logging, os, sys, time, uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0, '/app')
from config import settings
assert os.environ.get('VAS_SERVICE_VALIDATION') == '1', 'Explicit opt-in required'
assert urlsplit(settings.DATABASE_URL).hostname == 'notebook-validation-db', 'Disposable DB only'
assert urlsplit(settings.DATABASE_URL).path == '/notebook_validation', 'Disposable DB only'
OUT = Path('/validation-output'); assert Path(settings.ML_ARTIFACT_DIR) == OUT/'artifacts'
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from db_connection import db_manager
from db_models import (Base, Identity, IdentityType, IdentityAppearance, IdentityRelationship,
    Pipeline, MLFeatureDefinition, MLFeatureSnapshot, MLLabel, MLDataset, MLModel, BackgroundTaskHistory)
from backend.ml.feature_store import FEATURE_INVENTORY
from backend.ml.model_specs import MODEL_SPECS
from backend.ml.registry_service import validate_artifact, preprocess_feature_vector, score_with_payload
from backend.ml.dataset_builder import build_dataset, _code_version
from backend.ml.dataset_definitions import get_definition
from backend.ml.trainer import run_training_job, try_acquire_training
from backend.core.task_history import task_history_manager
from backend.ml.notebook_evidence import build_notebook_evidence
from backend.ml.debug_notebook import build_pipeline_debug_notebook
from backend.ml.service_deployment import deploy_service, stop_service

logging.basicConfig(level=logging.WARNING)

def save(name, data):
    (OUT/name).write_text(json.dumps(data,default=str,indent=2))

async def fixture():
    async with db_manager.engine.begin() as conn:
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        await conn.run_sync(Base.metadata.create_all)
    async with db_manager.get_session() as db:
        if (await db.execute(select(func.count()).select_from(Identity))).scalar():
            return
        for item in FEATURE_INVENTORY:
            db.add(MLFeatureDefinition(**item))
        for camera in range(4):
            db.add(Pipeline(pipeline_id=f'validation-camera-{camera}', timezone='UTC'))
        await db.flush()
        now = datetime.utcnow().replace(microsecond=0)
        base = now-timedelta(days=110)
        people=[]
        for i in range(120):
            person=uuid.uuid5(uuid.NAMESPACE_DNS, 'isolated-notebook-person-'+str(i))
            anchor=base+timedelta(days=28*(i//40)+(i%40)*.2)
            people.append((person,anchor))
            db.add(Identity(id=person,type=IdentityType.UNKNOWN,first_seen_at=anchor,last_seen_at=anchor+timedelta(days=24)))
        await db.flush()
        for i,(person,anchor) in enumerate(people):
            for j in range(8):
                start=anchor+timedelta(days=j*3,hours=i%5,minutes=(i*j)%43)
                db.add(IdentityAppearance(identity_id=person,pipeline_id=f'validation-camera-{(i+j)%4}',
                    start_time=start,end_time=start+timedelta(seconds=30+i*3+j*11),created_at=start))
            # Reviewed outcomes are a simulated workflow state in this disposable DB.
            # They must never be copied into production or cited as real accuracy evidence.
            db.add(MLLabel(subject_id=str(person),person_id=person,label='positive' if i%2 else 'negative',
                label_kind='manual',label_definition_version='v1',source='validation_fixture_review',
                event_time=anchor+timedelta(days=25),review_status='reviewed',status='active',
                created_by='isolated-author',created_by_user_id=1,idempotency_key='validation-'+str(person),reviewed_by='isolated-reviewer',reviewed_by_user_id=2,reviewed_at=now))
        for group in range(3):
            for i in range(40):
                for offset in (1,2,3+(i%4)):
                    j=(i+offset)%40
                    a,b=sorted((people[group*40+i][0],people[group*40+j][0]))
                    # Ring offsets are unique for each unordered pair in this fixture.
                    db.add(IdentityRelationship(identity_id_1=a,identity_id_2=b,
                        co_appearance_count=5+(i*7+offset)%41,co_appearance_percentage=15+(i*3)%80,
                        common_pipelines=[f'validation-camera-{c}' for c in range(1+i%4)],
                        first_co_appearance=base+timedelta(days=28*group),
                        last_co_appearance=base+timedelta(days=28*group+24+i*.1),
                        calculated_at=base+timedelta(days=28*group+31)))
        await db.commit()
    from backend.ml.collector import run_collection
    from backend.ml.relational_feature_service import collect_relational_snapshots
    async with db_manager.get_session() as db:
        stats=await run_collection(db,run_id='validation-collection',full_rebuild=False)
        save('collection.json',stats)
        for group in range(3):
            # Only cache observations whose calculated_at precedes this cutoff are read.
            result=await collect_relational_snapshots(db,as_of=base+timedelta(days=28*group+32),run_id=f'validation-relational-{group}')
            await db.commit()
            save(f'relational-{group}.json',result)
    save('fixture.json',{'synthetic':True,'people':120,'appearances':960,'relationships':360,
        'base':base.isoformat(),'purpose':'mechanical validation only; no accuracy/capacity conclusion'})

async def one(family, attempt):
    spec=MODEL_SPECS[family]
    job=f'validation-{list(MODEL_SPECS).index(family)}-{attempt}'
    await task_history_manager.create_job(job, 'ml_training', 'Isolated validation '+family)
    result={'family':family,'job_id':job,'synthetic':True}
    started=time.monotonic()
    try:
        dataset_id=None
        pipeline=None
        if family=='tabular_regression_model':
            async with db_manager.get_session() as db:
                built=await build_dataset(db,name='validation-regression',kind='unsupervised',definition=get_definition(spec.dataset_definition))
            assert built['status']=='built', built
            dataset_id=built['dataset_id']
            pipeline={'model_type':family,'algorithm':'xgboost_regressor','target':'appearance_count_30d',
                'features':['distinct_pipelines_30d','days_since_first_seen','days_since_last_seen','appearance_count_7d']}
        async with db_manager.get_session() as db:
            row=(await db.execute(select(BackgroundTaskHistory).where(BackgroundTaskHistory.job_id==job))).scalar_one()
            row.payload={'model_type':family,'algorithm':spec.default_algorithm,'dataset_id':dataset_id,'seed':42,'pipeline':pipeline}
        assert try_acquire_training(job) is None
        await run_training_job(job,model_type=family,algorithm=spec.default_algorithm,dataset_id=dataset_id,seed=42,pipeline=pipeline)
        async with db_manager.get_session() as db:
            task=(await db.execute(select(BackgroundTaskHistory).where(BackgroundTaskHistory.job_id==job))).scalar_one()
            result['training_status']=task.status
            result['failure']={'code':task.error_code,'message':task.error_message} if task.error_code else None
            model=(await db.execute(select(MLModel).where(MLModel.training_job_id==job))).scalar_one_or_none()
            if model:
                result.update(model_id=str(model.id),stage=model.stage,engineering=(model.training_config or {}).get('engineering_gate'),
                    scientific=(model.training_config or {}).get('scientific_gate'),code_version=model.code_version,
                    evaluation=model.evaluation_report,quality=model.quality_gates)
                payload=validate_artifact(model.artifact_path,expected_hash=model.artifact_hash,
                    expected_feature_names=model.feature_names,expected_dependencies=model.dependency_versions)
                sample={name:value for name,value in payload['imputation_medians'].items()}
                import numpy as np
                vector,missing=preprocess_feature_vector(payload,sample)
                scores=score_with_payload(payload,np.array([vector]))
                assert len(scores)==1 and np.isfinite(scores[0])
                result['artifact_reload_score']=float(scores[0])
                if family!='tabular_regression_model':
                    try:
                        result['deployment']=await deploy_service(db,family,model_id=str(model.id),artifact_hash=model.artifact_hash,
                            reason='Disposable mechanical validation; not production evidence',actor='validation-fixture',actor_id=2)
                    except Exception as exc:
                        await db.rollback(); result['deployment_error']={'type':type(exc).__name__,'message':str(exc)}
                else:
                    try:
                        await deploy_service(db,family,model_id=str(model.id),artifact_hash=model.artifact_hash,
                            reason='Expected offline-only refusal',actor='validation-fixture',actor_id=2)
                    except Exception as exc:
                        result['expected_offline_refusal']=getattr(exc,'code',type(exc).__name__)
                        await db.rollback()
                model_ref=result['model_id']
            else: model_ref=None
        if model_ref and 'deployment' in result:
            async with db_manager.get_session() as db:
                people=(await db.execute(select(Identity.id).order_by(Identity.first_seen_at.desc()).limit(3))).scalars().all()
                if family=='behavior_anomaly_model':
                    from backend.ml.inference_service import inference_service
                    predicted=await inference_service.predict_identity(db,str(people[0]))
                    result['consumer']=vars(predicted)
                    assert predicted.ok, result['consumer']
                elif family=='threat_ranking_model':
                    from backend.ml.model_scoring_service import rank_identities
                    result['consumer']=await rank_identities(db,[str(x) for x in people])
                    assert result['consumer']['scored']==len(people),result['consumer']
                else:
                    from backend.ml.model_scoring_service import score_relational_subject
                    edge=(await db.execute(select(IdentityRelationship).order_by(IdentityRelationship.calculated_at.desc()).limit(1))).scalar_one()
                    result['consumer']=await score_relational_subject(db,model_type=family,
                        identity_id=str(edge.identity_id_1),related_identity_id=str(edge.identity_id_2) if family=='coappearance_anomaly_model' else None,
                        model_id=model_ref,consumer='security_intelligence')
                    assert np.isfinite(result['consumer']['score']), result['consumer']
        async with db_manager.get_session() as db:
            evidence=await build_notebook_evidence(db,model_id=model_ref) if model_ref else await build_notebook_evidence(db,job_id=job)
            evidence['pipeline']['validation_fixture']={'synthetic':True,'not_production_accuracy_evidence':True}
            save(f'{family}-evidence.json',evidence)
            notebook=build_pipeline_debug_notebook(evidence)
            # Artefacts are mounted read-only at /artifacts in the independent Jupyter runner.
            save(f'notebooks/{family}.ipynb',notebook)
        if 'deployment' in result:
            async with db_manager.get_session() as db:
                result['stop']=await stop_service(db,family,model_id=model_ref,
                    reason='Finish isolated validation',actor='validation-fixture',actor_id=2)
                stopped=(await db.execute(select(MLModel).where(MLModel.id==uuid.UUID(model_ref)))).scalar_one()
                assert stopped.stage=='archived', 'Stop did not archive the selected model'
                from backend.ml.registry_service import registry_service
                stage='approved' if family=='threat_ranking_model' else 'shadow'
                assert await registry_service.get_stage_model(db,family,stage) is None, 'Stopped model remains bound'
        if family=='tabular_regression_model':
            assert result.get('expected_offline_refusal')=='OFFLINE_ONLY', result
        result['status']='passed' if result['training_status']=='completed' and model_ref and (family=='tabular_regression_model' or 'consumer' in result) else 'failed'
    except Exception as exc:
        import traceback
        result.update(status='failed',exception=type(exc).__name__,message=str(exc),traceback=traceback.format_exc())
    result['duration_seconds']=round(time.monotonic()-started,2)
    save(f'{family}-result.json',result)
    print(json.dumps({key:result.get(key) for key in ('family','status','training_status','failure','engineering','deployment_error','exception','message')}),flush=True)
    return result

async def main():
    db_manager.engine=create_async_engine(settings.DATABASE_URL,echo=False)
    db_manager.session_maker=async_sessionmaker(db_manager.engine,expire_on_commit=False,autoflush=False)
    db_manager._initialized=True
    try:
        await fixture()
        attempt=uuid.uuid4().hex[:8]
        outcomes=[]
        for family in MODEL_SPECS: outcomes.append(await one(family,attempt))
        save('service-results.json',outcomes)
        print('ALL_SERVICE_RUNS_FINISHED',flush=True)
    finally: await db_manager.close_db()

if __name__=='__main__': asyncio.run(main())
