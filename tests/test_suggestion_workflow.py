"""Suggestion review regressions; database fixtures always roll back."""
import asyncio
import uuid
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import patch, AsyncMock
import pytest
from sqlalchemy import select
from fastapi import HTTPException
import db_models as m
from test_merge_reliability import sandbox, vector
from backend.core.face_quality import QUALITY_SCORER_VERSION
from backend.core.suggestion_review import complete_groups
from config import settings


def test_grouping_rejects_chain_and_model_mismatch():
    samples = {name: {'qa': [vector(angle)]} for name, angle in [('a',0),('b',30),('c',60)]}
    groups = complete_groups(samples, .8)
    assert groups[0][0] == ['a','b']
    assert all(len(group) == 2 for group, _ in groups)
    assert complete_groups({'a': {'qa': [vector(0)]}, 'b': {'other': [vector(0)]}}, .8) == []
    assert complete_groups({'a': {}, 'b': {}}, .8) == []


@pytest.mark.parametrize('status', [m.MergeSuggestionStatus.APPROVED, m.MergeSuggestionStatus.REJECTED, m.MergeSuggestionStatus.INVALIDATED])
def test_reviewed_suggestion_cannot_be_rejected(status):
    async def run():
        from backend.routes.identities import reject_merge_suggestion
        async with sandbox() as (db, service, admin):
            suggestion = m.MergeSuggestion(identity_ids=[], confidence=.8, status=status)
            db.add(suggestion); await db.flush()
            identifier = suggestion.id
            await db.commit()
            with pytest.raises(HTTPException) as error:
                await reject_merge_suggestion(identifier, db, NS(id=admin, role='admin', username='qa'))
            assert error.value.status_code == 409
            current = (await db.execute(select(m.MergeSuggestion).where(m.MergeSuggestion.id == identifier))).scalar_one()
            assert current.status == status
    asyncio.run(run())


def test_approval_records_original_pair_feedback_before_embeddings_move():
    async def run():
        import backend.routes.identities as route
        async with sandbox() as (db, service, admin):
            people = [m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE, appearances_count=i+1) for i in range(3)]
            db.add_all(people); await db.flush()
            ids = [p.id for p in people]
            for p in people:
                db.add(m.IdentityEmbedding(identity_id=p.id, embedding=vector(0), embedding_model_version='qa',
                    quality=.8, quality_scorer_version=QUALITY_SCORER_VERSION, faiss_index_type='unknown'))
            suggestion = m.MergeSuggestion(identity_ids=[str(i) for i in ids], confidence=.8)
            db.add(suggestion); await db.flush()
            with patch.object(route, 'get_identity_service', return_value=service), patch.object(route, 'request_snapshot', AsyncMock()), patch('backend.core.merge_notifications.publish_merge', AsyncMock()):
                result = await route.approve_merge_suggestion(suggestion.id, None, None, db, NS(id=admin, role='admin', username='qa'))
            assert result['success']
            feedback = (await db.execute(select(m.SimilarityTrainingData).where(m.SimilarityTrainingData.identity_id_1.in_(ids)))).scalars().all()
            assert len(feedback) == 3 and all(row.label == 1 for row in feedback)
            assert sorted(row.appearances_diff for row in feedback) == [1,1,2]
            owners = (await db.execute(select(m.IdentityEmbedding.identity_id).where(m.IdentityEmbedding.identity_id.in_(ids)))).scalars().all()
            assert owners == [ids[0]] * 3
    asyncio.run(run())


def test_camera_suggestions_route_uses_verified_groups():
    async def run():
        from backend.routes.identities import get_merge_suggestions_for_pipeline
        async with sandbox() as (db, service, admin):
            camera = m.Pipeline(pipeline_id='suggestion-qa-' + uuid.uuid4().hex)
            people = [m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE) for _ in range(3)]
            db.add_all([camera, *people]); await db.flush()
            for i, person in enumerate(people):
                db.add(m.IdentityAppearance(identity_id=person.id, pipeline_id=camera.pipeline_id, start_time=datetime(2026,9,13,10)))
                db.add(m.IdentityEmbedding(identity_id=person.id, embedding=vector(0),
                    embedding_model_version='qa' if i < 2 else 'different', faiss_index_type='unknown'))
            await db.flush()
            with patch.object(settings, 'CLUSTER_MIN_SAMPLES', 2):
                result = await get_merge_suggestions_for_pipeline(camera.pipeline_id, None, db, NS(id=admin, role='admin', username='qa'))
            assert len(result) == 1
            assert set(result[0]['identity_ids']) == {str(p.id) for p in people[:2]}
            assert result[0]['confidence'] == pytest.approx(1)
            assert 'safe to approve' not in result[0]['recommendation']
    asyncio.run(run())
