"""Filtered cards use matching sightings; fallback evidence remains visible."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from conftest import run_on_shared_loop
from db_connection import db_manager
from db_models import Identity, IdentityType, IdentityStatus, IdentityAppearance, IdentityEmbedding, Pipeline


def test_filtered_cards_and_fallback_camera_counts(monkeypatch):
    from backend.routes.identities import list_unknown_identities
    from backend.core.redis_cache import redis_cache_service
    from backend.auth.auth_service import AuthService
    monkeypatch.setattr(redis_cache_service, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(redis_cache_service, 'set', AsyncMock(return_value=True))

    async def run():
        await db_manager.init_db()
        async with db_manager.get_session() as db:
            camera, hidden = 'window-' + uuid4().hex, 'hidden-' + uuid4().hex
            seen = Identity(type=IdentityType.UNKNOWN, status=IdentityStatus.ACTIVE,
                            last_seen_at=datetime(2026, 1, 3, 12), appearances_count=2)
            fallback = Identity(type=IdentityType.UNKNOWN, status=IdentityStatus.ACTIVE,
                                last_seen_at=datetime(2026, 1, 1, 12), appearances_count=0)
            db.add_all([seen, fallback, Pipeline(pipeline_id=camera), Pipeline(pipeline_id=hidden)])
            await db.flush()
            db.add_all([
                IdentityAppearance(identity_id=seen.id, pipeline_id=camera,
                                   start_time=datetime(2026, 1, 1, 12)),
                IdentityAppearance(identity_id=seen.id, pipeline_id=camera,
                                   start_time=datetime(2026, 1, 3, 12)),
                IdentityEmbedding(identity_id=fallback.id, pipeline_id=camera, faiss_index_type='unknown'),
                IdentityEmbedding(identity_id=fallback.id, pipeline_id=hidden, faiss_index_type='unknown'),
            ])
            await db.flush()
            monkeypatch.setattr(AuthService, 'get_user_pipelines', AsyncMock(return_value=[camera]))
            try:
                async def query(pipeline=None):
                    return await list_unknown_identities(page=1, page_size=20,
                        date_from='2026-01-01T00:00:00Z', date_to='2026-01-02T00:00:00Z',
                        pipeline_id=pipeline, status_filter=None, min_appearances=None,
                        show_all=True, db=db, current_user=SimpleNamespace(id=0, role='user'))
                for body in [await query(), await query(camera)]:
                    rows = {row['id']: row for row in body['identities']}
                    assert body['total'] == 2
                    assert body['pipeline_totals'] == {camera: 2}
                    event = rows[str(seen.id)]['camera_events'][camera]
                    assert event['timestamp'] == '2026-01-01T12:00:00Z'
                    assert event['appearances_count'] == 1
                    legacy = rows[str(fallback.id)]
                    assert legacy['pipeline_evidence_only'] is True
                    assert legacy['camera_events'] == {}
                    assert legacy['pipeline_ids'] == [camera]
            finally:
                await db.rollback()
    run_on_shared_loop(run())
