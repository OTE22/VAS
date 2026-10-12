"""Unknown matching and timeline regressions using synthetic, isolated data only."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import numpy as np
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from conftest import run_on_shared_loop as run
from test_person_history_retention import isolated
from test_known_reference_isolation import vector
from config import settings
from db_models import Identity, IdentityType, IdentityEmbedding, IdentityAppearance, Pipeline, Detection, Face
from backend.core.identity_service import IdentityService
from backend.core.identity_index_pgvector import IdentityIndexPgVector
from backend.core.appearance_events import review_unknown_associations


@pytest.mark.parametrize('backend', ['pgvector', 'faiss'])
def test_live_matching_cannot_chain_through_later_observations(isolated, monkeypatch, backend):
    sessions, folder = isolated
    monkeypatch.setattr(settings, 'UNKNOWN_SIMILARITY_THRESHOLD', .35)
    async def exercise():
        svc = IdentityService(pgvector_index=IdentityIndexPgVector())
        svc.use_pgvector = backend == 'pgvector'
        if backend == 'faiss':
            from backend.core.vector_index.flat_faiss import FlatFaissIndex
            svc.vector_index = FlatFaissIndex(storage_dir=str(folder/'index'), dim=512, model_version=svc.embedding_model_version)
        async with sessions() as db:
            db.add(Pipeline(pipeline_id='synthetic-camera')); await db.flush()
            first = await svc.find_or_create_identity(vector(0), 'synthetic-camera', None, db, .9)
            await db.commit()
            assert first.is_new_identity
            initial = await db.get(IdentityEmbedding, first.embedding_id)
            assert initial.embedding is not None
            assert initial.embedding_model_version == svc.embedding_model_version
            middle = await svc.find_or_create_identity(vector(60), 'synthetic-camera', None, db, .9)
            assert middle.identity.id == first.identity.id and not middle.is_new_identity
            await svc.save_embedding(first.identity, vector(60), None, 'synthetic-camera', .9, db, defer_commit=True)
            await db.commit()
            # B matches A and C matches B, but C MUST NOT be assigned to A.
            last = await svc.find_or_create_identity(vector(120), 'synthetic-camera', None, db, .9)
            assert last.is_new_identity and last.identity.id != first.identity.id
            await db.commit()
            good = await svc.find_or_create_identity(vector(10), 'synthetic-camera', None, db, .9)
            assert good.identity.id == first.identity.id and not good.is_new_identity
            history = await svc.pgvector_index.search_unknown(vector(60), db, threshold=.99, recognition_only=False)
            assert history[0][0] == str(first.identity.id)
            assert await db.scalar(select(func.count(IdentityEmbedding.id))) == 3
            assert await svc.pgvector_index.search_unknown(vector(0), db, threshold=.99, model_version='different-model') == []
    run(exercise())


async def sightings(db, folder):
    db.add(Pipeline(pipeline_id='synthetic-camera'))
    identity = Identity(type=IdentityType.UNKNOWN)
    db.add(identity); await db.flush()
    apps=[]; embeddings=[]
    for n, angle in enumerate([0, 60, 120]):
        path = folder/f'synthetic-{n}.jpg'; path.write_bytes(b'synthetic fixture')
        det=Detection(pipeline_id='synthetic-camera'); db.add(det); await db.flush()
        db.add(Face(identity_id=identity.id, detection_id=det.id, name='Unknown', similarity=.5, face_image_path=str(path)))
        app=IdentityAppearance(identity_id=identity.id, pipeline_id=det.pipeline_id, detection_id=det.id,
            start_time=datetime(2026, 1, 1)+timedelta(minutes=n), best_snapshot_path=str(path))
        emb=IdentityEmbedding(identity_id=identity.id, detection_id=det.id, pipeline_id=det.pipeline_id,
            embedding=vector(angle), embedding_model_version=IdentityService._resolve_model_version())
        db.add_all([app,emb]); await db.flush(); apps.append(app); embeddings.append(emb)
    return identity,apps,embeddings


def test_clicked_sighting_changes_context_without_rewriting_history(isolated, monkeypatch):
    sessions,folder=isolated
    monkeypatch.setattr(settings,'UNKNOWN_SIMILARITY_THRESHOLD',.35)
    async def exercise():
        async with sessions() as db:
            person,apps,embeddings=await sightings(db,folder)
            original = [(a.id,a.identity_id,a.best_snapshot_path) for a in apps]
            result,meta=await review_unknown_associations(db,person.id,apps)
            assert [result[a.id]['association_status'] for a in apps] == ['selected','consistent','review_required']
            assert meta['included_count']==2 and meta['review_count']==1
            result,meta=await review_unknown_associations(db,person.id,apps,apps[2].id)
            assert [result[a.id]['association_status'] for a in apps] == ['review_required','consistent','selected']
            from backend.routes.identities import get_identity_details, IdentityDetail
            payload=await get_identity_details(str(person.id),db,SimpleNamespace(role='admin'),appearance_id=apps[2].id)
            response=IdentityDetail(**payload)
            assert len(response.appearances)==3 and response.appearances_count==3
            assert response.association_review['reference_appearance_id']==apps[2].id
            with pytest.raises(HTTPException) as err:
                await get_identity_details(str(person.id),db,SimpleNamespace(role='admin'),appearance_id=999999)
            assert err.value.status_code==404
            assert [(a.id,a.identity_id,a.best_snapshot_path) for a in apps]==original
            assert await db.scalar(select(func.count(IdentityEmbedding.id)))==3
    run(exercise())


@pytest.mark.parametrize('problem', ['missing', 'model', 'ambiguous_vector', 'ambiguous_face', 'wrong_photo', 'wrong_camera', 'zero'])
def test_uncertain_vector_links_do_not_enter_journey(isolated, problem):
    sessions,folder=isolated
    async def exercise():
        async with sessions() as db:
            person,apps,embeddings=await sightings(db,folder)
            row=embeddings[1]
            if problem=='missing': row.embedding=None
            elif problem=='model': row.embedding_model_version='old-model'
            elif problem=='ambiguous_vector':
                db.add(IdentityEmbedding(identity_id=person.id, detection_id=row.detection_id, pipeline_id=row.pipeline_id,
                    embedding=vector(60), embedding_model_version=row.embedding_model_version))
            elif problem=='ambiguous_face':
                db.add(Face(identity_id=person.id,detection_id=row.detection_id,name='Unknown',similarity=.5))
            elif problem=='wrong_photo': apps[1].best_snapshot_path=str(folder/'unrelated.jpg')
            elif problem=='wrong_camera': row.pipeline_id=None
            elif problem=='zero': row.embedding=np.zeros(512,dtype=np.float32)
            await db.flush()
            result,meta=await review_unknown_associations(db,person.id,apps,apps[0].id)
            assert result[apps[1].id]['association_status']=='unverified'
            # A selected image with no reliable vector must not borrow a different reference.
            result,meta=await review_unknown_associations(db,person.id,apps,apps[1].id)
            assert not meta['reference_available']
            assert meta['included_count']==1
            assert all(result[a.id]['association_status']=='unverified' for a in (apps[0],apps[2]))
    run(exercise())


def test_missing_portrait_never_borrows_shared_unknown_folder(isolated):
    sessions,folder=isolated
    async def exercise():
        async with sessions() as db:
            person=Identity(type=IdentityType.UNKNOWN); db.add(person); await db.flush()
            other=folder/'synthetic-camera/unknown/unrelated.jpg'; other.parent.mkdir(parents=True); other.write_bytes(b'synthetic')
            from backend.routes.identities import _find_best_image_from_storage_for_identity
            assert await _find_best_image_from_storage_for_identity(person,str(folder),db) is None
            assert other.exists()
    run(exercise())
