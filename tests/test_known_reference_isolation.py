"""Synthetic regression for camera-reference drift; isolated PostgreSQL only."""
from types import SimpleNamespace
import hashlib
import importlib
import numpy as np
import pytest
from sqlalchemy import select, func, update
from conftest import run_on_shared_loop as run
from test_person_history_retention import isolated
from config import settings
from db_models import (Identity, IdentityImage, IdentityEmbedding, IdentityType,
                       IdentityStatus, Pipeline, User)
from backend.core.identity_service import IdentityService
from backend.core.identity_index_pgvector import IdentityIndexPgVector
from backend.core.enrollment_service import ensure_image_reference


def vector(angle):
    v = np.zeros(512, dtype=np.float32)
    v[:2] = [np.cos(np.deg2rad(angle)), np.sin(np.deg2rad(angle))]
    return v


def service():
    s = IdentityService(pgvector_index=IdentityIndexPgVector())
    s.use_pgvector = True
    s.embedding_model_version = 'reference-test-v1'
    return s


async def seed(db, folder, svc, angle=0):
    person = Identity(type=IdentityType.KNOWN, status=IdentityStatus.ACTIVE,
                      display_name='Synthetic enrolled person')
    db.add(person); await db.flush()
    path = folder / 'approved.jpg'; payload=b'approved synthetic photo'; path.write_bytes(payload)
    im = IdentityImage(identity_id=person.id, storage_path=str(path),
                       file_checksum=hashlib.sha256(payload).hexdigest(),
                       source_type='upload', processing_status='completed', is_primary=True)
    db.add(im); await db.flush()
    row = IdentityEmbedding(identity_id=person.id, image_id=im.id, embedding=vector(angle),
                            embedding_model_version=svc.embedding_model_version, faiss_index_type='known')
    db.add(row); await db.flush()
    return person, im, row


@pytest.mark.parametrize('backend', ['pgvector', 'faiss'])
def test_camera_matches_remain_history_not_live_references(isolated, monkeypatch, backend):
    sessions, folder = isolated
    monkeypatch.setattr(settings, 'SIMILARITY_THRESHOLD', .8)
    monkeypatch.setattr(settings, 'IDENTITY_AUTO_ENRICH_ENABLED', False)
    async def exercise():
        svc = service()
        async with sessions() as db:
            db.add(Pipeline(pipeline_id='synthetic-camera')); await db.flush()
            person, im, anchor = await seed(db, folder, svc)
            if backend == 'faiss':
                from backend.core.vector_index.flat_faiss import FlatFaissIndex
                svc.use_pgvector = False
                svc.vector_index = FlatFaissIndex(storage_dir=str(folder/'faiss'), dim=512, model_version=svc.embedding_model_version)
            # A retained unrelated camera observation must not be a live known
            # match even though it has cosine 1.0 to itself and a known owner.
            observed = await svc.save_embedding(person, vector(90), None, 'synthetic-camera', .9, db,
                                                defer_commit=True)
            await db.commit()
            assert observed.faiss_index_type == 'known_observation'
            # The low-level Known API is safe by default as well.
            assert await svc.pgvector_index.search_known(vector(90), db, threshold=.8,
                model_version=svc.embedding_model_version) == []
            assert await svc._maybe_enrich_identity(person, vector(90), 1., .9, None, 'synthetic-camera', db) is None
            assert await svc.search_vector_index(db, vector(90), threshold=.8, index_type='known') == []
            good = await svc.search_vector_index(db, vector(0), threshold=.8, index_type='known')
            assert good[0][0] == str(person.id)
            history = await svc.pgvector_index.search_known(vector(90), db, threshold=.8, include_inactive=True, recognition_only=False)
            assert history[0][0] == str(person.id)
            assert await db.scalar(select(func.count(IdentityEmbedding.id))) == 2
            # Model drift and deleted references cannot silently use camera history.
            svc.embedding_model_version = 'different-model'
            assert await svc.search_vector_index(db, vector(0), threshold=.8) == []
            svc.embedding_model_version = 'reference-test-v1'
            await db.delete(im); await db.flush()
            assert await svc.search_vector_index(db, vector(0), threshold=.8) == []
            assert await db.scalar(select(func.count(IdentityEmbedding.id))) == 2
    run(exercise())


def test_opt_in_enrichment_cannot_chain_away_from_approved_photo(isolated, monkeypatch):
    sessions, folder = isolated
    monkeypatch.setattr(settings, 'SIMILARITY_THRESHOLD', .8)
    monkeypatch.setattr(settings, 'IDENTITY_ENRICH_MIN_SIMILARITY', .8)
    monkeypatch.setattr(settings, 'IDENTITY_AUTO_ENRICH_ENABLED', True)
    async def exercise():
        svc = service()
        async with sessions() as db:
            db.add(Pipeline(pipeline_id='synthetic-camera')); await db.flush()
            person, image, anchor = await seed(db, folder, svc)
            # 30 degrees = .866 to approved anchor, .5 at 60 degrees.
            learned = await svc._maybe_enrich_identity(person, vector(30), .866, .9, None, 'synthetic-camera', db)
            assert learned is not None
            assert (await db.get(IdentityEmbedding, learned)).faiss_index_type == 'known_enriched'
            assert await svc._maybe_enrich_identity(person, vector(60), .866, .9, None, 'synthetic-camera', db) is None
            assert await svc._maybe_enrich_identity(person, vector(25), .9, None, None, 'synthetic-camera', db) is None
            # A forged/stale enriched row without anchor support cannot match.
            db.add(IdentityEmbedding(identity_id=person.id, pipeline_id='synthetic-camera',
                embedding=vector(90), embedding_model_version=svc.embedding_model_version,
                faiss_index_type='known_enriched'))
            await db.flush()
            assert await svc.search_vector_index(db, vector(90), threshold=.99) == []
            assert await svc.search_vector_index(db, vector(30), threshold=.99)
            monkeypatch.setattr(settings, 'IDENTITY_AUTO_ENRICH_ENABLED', False)
            assert await svc.search_vector_index(db, vector(30), threshold=.99) == []
    run(exercise())


def test_reference_requires_completed_image_same_owner_current_model(isolated):
    sessions, folder = isolated
    async def exercise():
        svc=service()
        async with sessions() as db:
            person, im, anchor = await seed(db, folder, svc)
            im.processing_status='failed'; await db.flush()
            assert await svc.search_vector_index(db, vector(0), threshold=.8) == []
            im.processing_status='completed'
            other=Identity(type=IdentityType.KNOWN); db.add(other); await db.flush()
            im.identity_id=other.id; await db.flush()
            assert await svc.search_vector_index(db, vector(0), threshold=.8) == []
    run(exercise())


def prepare_stub(monkeypatch):
    import backend.core.enrollment_service as enrollment
    monkeypatch.setattr(enrollment, 'prepare_upload', lambda *args, **kwargs: SimpleNamespace(
        embedding_normalized=vector(0), quality=.9, quality_scorer_version='synthetic'))


def test_explicit_image_repair_is_idempotent_and_keeps_observations(isolated, monkeypatch):
    sessions, folder=isolated
    prepare_stub(monkeypatch)
    async def exercise():
        svc=service()
        async with sessions() as db:
            person, image, old = await seed(db, folder, svc, angle=90)
            old.image_id=None; old.faiss_index_type='known_observation'; await db.flush()
            ref=await ensure_image_reference(db, person, image, svc)
            assert ref.id != old.id
            again=await ensure_image_reference(db, person, image, svc)
            assert again.id == ref.id
            assert await db.scalar(select(func.count(IdentityEmbedding.id))) == 2
            assert await svc.search_vector_index(db, vector(90), threshold=.8) == []
            assert await svc.search_vector_index(db, vector(0), threshold=.8)
    run(exercise())


def test_future_promotion_uses_only_selected_photo_and_preserves_cluster(isolated, monkeypatch):
    sessions, folder=isolated
    prepare_stub(monkeypatch)
    async def exercise():
        svc=service()
        async with sessions() as db:
            user=User(username='synthetic-admin', email='synthetic@example.invalid', password_hash='not-a-login', role='admin')
            db.add(user); await db.flush()
            path=folder/'selected.jpg'; path.write_bytes(b'synthetic selected photo')
            person=Identity(type=IdentityType.UNKNOWN, status=IdentityStatus.ACTIVE,
                            best_snapshot_path=str(path))
            db.add(person); await db.flush()
            for angle in [0,45,90]:
                db.add(IdentityEmbedding(identity_id=person.id, embedding=vector(angle),
                    embedding_model_version=svc.embedding_model_version, faiss_index_type='unknown'))
            await db.flush()
            promoted=await svc.promote_unknown_to_known(person.id,'Synthetic promoted',user.id,db,copied_files=[])
            assert promoted.id == person.id and promoted.type == IdentityType.KNOWN
            assert await db.scalar(select(func.count(IdentityEmbedding.id))) == 4
            assert await db.scalar(select(func.count(IdentityEmbedding.id)).where(IdentityEmbedding.image_id.isnot(None))) == 1
            assert await svc.search_vector_index(db, vector(90), threshold=.8) == []
            assert await svc.search_vector_index(db, vector(0), threshold=.8)
            await db.rollback()
    run(exercise())


def test_promotion_without_usable_photo_refuses_before_renaming(isolated):
    sessions, _ = isolated
    async def exercise():
        from backend.core.promotion_review import PromotionError
        svc=service()
        async with sessions() as db:
            person=Identity(type=IdentityType.UNKNOWN, status=IdentityStatus.ACTIVE)
            db.add(person); await db.flush()
            db.add(IdentityEmbedding(identity_id=person.id, embedding=vector(0),
                                    embedding_model_version=svc.embedding_model_version))
            await db.flush()
            with pytest.raises(PromotionError, match='no saved photo'):
                await svc.promote_unknown_to_known(person.id,'Synthetic',0,db)
            assert person.type == IdentityType.UNKNOWN and person.display_name is None
    run(exercise())


def test_live_ingest_and_enrollment_review_ignore_contaminated_history(isolated, monkeypatch):
    sessions, folder = isolated
    monkeypatch.setattr(settings, 'SIMILARITY_THRESHOLD', .8)
    monkeypatch.setattr(settings, 'IDENTITY_AUTO_ENRICH_ENABLED', False)
    import backend.core.enrollment_service as enrollment
    monkeypatch.setattr(enrollment, 'embedding_model_version', lambda: 'reference-test-v1')
    async def exercise():
        svc = service()
        async with sessions() as db:
            db.add(Pipeline(pipeline_id='synthetic-camera')); await db.flush()
            person, image, ref = await seed(db, folder, svc)
            await svc.save_embedding(person, vector(90), None, 'synthetic-camera', .9, db)
            result = await svc.find_or_create_identity(vector(90), 'synthetic-camera', None, db, .9)
            assert result.identity.id != person.id and result.identity.type == IdentityType.UNKNOWN
            good = await svc.find_or_create_identity(vector(0), 'synthetic-camera', None, db, .9)
            assert good.identity.id == person.id and not good.is_new_identity
            assert await enrollment.find_similar_identities(db, vector(90)) == []
            assert (await enrollment.find_similar_identities(db, vector(0)))[0][0] == str(person.id)
    run(exercise())


def test_reference_repair_rejects_changed_photo_without_writing(isolated, monkeypatch):
    sessions, folder = isolated
    prepare_stub(monkeypatch)
    async def exercise():
        from backend.core.enrollment_service import EnrollmentError
        svc = service()
        async with sessions() as db:
            person, image, previous = await seed(db, folder, svc)
            previous.image_id = None; await db.flush()
            (folder / 'approved.jpg').write_bytes(b'different photo')
            with pytest.raises(EnrollmentError, match='changed'):
                await ensure_image_reference(db, person, image, svc)
            assert await db.scalar(select(func.count(IdentityEmbedding.id))) == 1
    run(exercise())


def test_loader_links_only_registered_approved_uploads(isolated):
    sessions, folder = isolated
    async def exercise():
        from backend.core.identity_loader import IdentityLoader
        svc = service(); loader = IdentityLoader(svc, None)
        async with sessions() as db:
            person, image, row = await seed(db, folder, svc)
            row.image_id = None; await db.flush()
            image.source_type = 'promotion'; await db.flush()
            await loader._link_loaded_reference(db, person, row.id, str(folder/'approved.jpg'))
            assert row.image_id is None  # Legacy promotion needs explicit review.
            image.source_type = 'upload'; await db.flush()
            await loader._link_loaded_reference(db, person, row.id, str(folder/'approved.jpg'))
            assert row.image_id == image.id
    run(exercise())


def test_clustering_tolerates_multiple_existing_group_suggestions(isolated, monkeypatch):
    sessions, folder = isolated
    async def exercise():
        from datetime import datetime
        from unittest.mock import AsyncMock
        from backend.core.identity_clustering import IdentityClusteringService
        from db_models import IdentityAppearance, MergeSuggestion, MergeSuggestionStatus
        async with sessions() as db:
            db.add(Pipeline(pipeline_id='synthetic-camera'))
            people = [Identity(type=IdentityType.UNKNOWN, status=IdentityStatus.ACTIVE,
                               appearances_count=2) for _ in range(3)]
            db.add_all(people); await db.flush()
            for person in people[:2]:
                db.add(IdentityAppearance(identity_id=person.id, pipeline_id='synthetic-camera',
                                           start_time=datetime.utcnow()))
            for members in [people[:2], people]:
                db.add(MergeSuggestion(identity_ids=[str(p.id) for p in members],
                    status=MergeSuggestionStatus.PENDING, confidence=.9))
            await db.flush()
            cluster=IdentityClusteringService()
            monkeypatch.setattr(cluster, '_verify_face_similarity', AsyncMock(return_value=(True,.99)))
            await cluster._create_hybrid_suggestions(people[:2],db)
            assert await db.scalar(select(func.count(MergeSuggestion.id))) == 2
    run(exercise())
