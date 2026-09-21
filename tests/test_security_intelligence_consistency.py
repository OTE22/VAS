import asyncio, os
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from config import settings
from db_models import Identity, IdentityType, IdentityStatus, Pipeline, IdentityAppearance, IdentityRelationship, RelationshipStrength, ThreatAssessmentRecord
from backend.core.security_intelligence_service import security_intelligence_service
from backend.routes import intelligence as routes

@pytest.mark.skipif(not os.environ.get('REGRESSION_ISOLATION_ID'), reason='Disposable database only')
def test_windowed_network_and_deduplicated_threat(monkeypatch):
    asyncio.run(exercise(monkeypatch))

async def exercise(monkeypatch):
    engine=create_async_engine(settings.DATABASE_URL)
    Session=async_sessionmaker(engine,expire_on_commit=False)
    ids=sorted([uuid4(),uuid4()]); pid='sec_audit_'+uuid4().hex
    now=datetime.utcnow()
    monkeypatch.setattr(settings, 'RELATED_IDENTITY_MIN_CO_APPEARANCES', 1)
    try:
        async with Session() as db:
            db.add(Pipeline(pipeline_id=pid))
            db.add_all([Identity(id=i,type=IdentityType.KNOWN,status=IdentityStatus.ACTIVE,display_name='audit') for i in ids])
            await db.flush()
            for i in ids:
                for days in [0,10,20]:
                    db.add(IdentityAppearance(identity_id=i,pipeline_id=pid,start_time=now-timedelta(days=days)))
            db.add(IdentityRelationship(identity_id_1=ids[0],identity_id_2=ids[1],co_appearance_count=3,co_appearance_percentage=100,relationship_strength=RelationshipStrength.STRONG,first_co_appearance=now-timedelta(days=20),last_co_appearance=now,common_pipelines=[pid]))
            await db.commit()
            graph=await security_intelligence_service.build_social_network(db,identity_ids=[str(ids[0])],days_back=1)
            assert graph.edges[0].co_appearances==1
            assert all(n.appearances_count==1 for n in graph.nodes)
            assert graph.edges[0].first_seen_together >= now-timedelta(days=1)
            db.add(IdentityAppearance(identity_id=ids[0],pipeline_id=pid,start_time=now-timedelta(hours=2)))
            await db.execute(delete(IdentityRelationship).where(IdentityRelationship.identity_id_1==ids[0]))
            await db.commit()
            uncached=await security_intelligence_service.build_social_network(db,identity_ids=[str(ids[1])],days_back=1)
            assert uncached.edges[0].co_appearances == graph.edges[0].co_appearances
            assert uncached.edges[0].co_appearance_percentage == 50
            forward=await security_intelligence_service.build_social_network(db,identity_ids=[str(ids[0])],days_back=1)
            assert forward.edges[0].co_appearance_percentage == 50
            from backend.ml.decision_service import decision_service
            version='audit-'+uuid4().hex
            def outcome(score):
                assessment=SimpleNamespace(identity_id=str(ids[0]),display_name='audit',overall_risk_score=score,risk_factors=[],threat_level='low' if score==10 else 'critical',severity='low' if score==10 else 'critical',confidence=.5,recommendations=[],last_assessed=now,algorithm_version=version,engine=None)
                return SimpleNamespace(assessment=assessment,actual_mode_used='rules',provenance=None,prediction_id=None,shadow_planned=False,decision_record={})
            monkeypatch.setattr(decision_service,'decide',AsyncMock(side_effect=[outcome(10),outcome(90)]))
            first=await routes.get_threat_assessment(str(ids[0]),db=db,current_user={'id':1})
            second=await routes.get_threat_assessment(str(ids[0]),db=db,current_user={'id':1})
            assert first['persisted'] and second['deduplicated']
            assert first['assessment_id']==second['assessment_id']
            stored=(await db.execute(select(ThreatAssessmentRecord).where(ThreatAssessmentRecord.person_id==ids[0]))).scalar_one()
            assert second['overall_risk_score']==stored.total_risk_score==10
            assert second['severity']==stored.severity=='low'
            assert second['last_assessed']==first['last_assessed']
            assert second['engine']['total_score']==10
            assert second['recommendations']==[]

    finally:
        async with Session() as db:
            await db.execute(delete(ThreatAssessmentRecord).where(ThreatAssessmentRecord.person_id.in_(ids)))
            await db.execute(delete(Identity).where(Identity.id.in_(ids)))
            await db.execute(delete(Pipeline).where(Pipeline.pipeline_id==pid))
            await db.commit()
        await engine.dispose()
