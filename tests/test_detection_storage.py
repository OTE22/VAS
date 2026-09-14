"""New and historical layouts, using temporary files and rollback-only DB writes."""
import asyncio
import importlib
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np
import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import db_models as m
from config import settings
from backend.core import detection_storage as storage
from backend.core.storage_references import unreferenced_files, retire_snapshot


def test_atomic_crop_layout_and_invalid_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'STORAGE_DIR', str(tmp_path))
    frame, face = uuid4(), uuid4()
    image = np.full((112, 112, 3), 120, dtype=np.uint8)
    path = Path(storage.write_crop(image, '../camera/name', frame, face, datetime(2026, 9, 13)))
    assert path.relative_to(tmp_path).parts == ('detections', '2026', '09', '13',
        storage.camera_key('../camera/name'), str(frame), str(face) + '.jpg')
    assert cv2.imread(str(path)).shape == image.shape
    assert not list(tmp_path.rglob('*.part.jpg'))
    with pytest.raises(ValueError):
        storage.local_path('../escape.jpg')
    (tmp_path / 'link').symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        storage.local_path('link/escape.jpg')
    monkeypatch.setattr(cv2, 'imencode', lambda *a: (False, None))
    with pytest.raises(OSError):
        storage.write_crop(image, 'camera', uuid4(), uuid4(), datetime.utcnow())
    assert len(list(tmp_path.rglob('*.jpg'))) == 1


@pytest.mark.parametrize('enabled,unknown_enabled,name,limit,existing,expected', [
    (False, True, 'Unknown', 1, 0, False),
    (True, False, 'Unknown', 1, 0, False),
    (True, True, 'Unknown', 0, 3, True),
    (True, True, 'Known person', 0, 0, False),
    (True, True, 'Known person', 1, 1, False),
    (True, True, 'Known person', 2, 1, True),
])
def test_save_settings_preserved(tmp_path, monkeypatch, enabled, unknown_enabled, name, limit, existing, expected):
    monkeypatch.setattr(settings, 'STORAGE_DIR', str(tmp_path))
    monkeypatch.setattr(settings, 'SAVE_IMAGES', enabled)
    monkeypatch.setattr(settings, 'SAVE_UNKNOWN_FACES', unknown_enabled)
    monkeypatch.setattr(settings, 'MAX_PHOTOS_PER_PERSON', limit)
    monkeypatch.setattr(storage, 'stored_camera_photos', AsyncMock(return_value=set(range(existing))))
    @asynccontextmanager
    async def session():
        yield None
    result = asyncio.run(storage.save_camera_crop(np.zeros((112, 112, 3), dtype=np.uint8),
        pipeline_id='camera', capture_id=uuid4(), face_id=uuid4(), captured_at=datetime.utcnow(),
        identity_id=uuid4(), name=name, session_factory=session))
    assert bool(result) == expected
    assert len(list(tmp_path.rglob('*.jpg'))) == int(expected)


@pytest.mark.parametrize('layout', ['historical', 'organized'])
def test_dependencies_across_layouts(tmp_path, monkeypatch, layout):
    asyncio.run(_exercise(tmp_path, monkeypatch, layout))


async def _exercise(tmp_path, monkeypatch, layout):
    monkeypatch.setattr(settings, 'STORAGE_DIR', str(tmp_path))
    monkeypatch.setattr(settings, 'WATCHLIST_ENABLED', False)
    monkeypatch.setattr(settings, 'LIVE_ALERTS_ENABLED', False)
    service_module = importlib.import_module('backend.core.identity_service')
    monkeypatch.setattr(service_module, 'identity_service', service_module.IdentityService(None))
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(bind=connection, join_transaction_mode='create_savepoint', expire_on_commit=False) as db:
                    person = m.Identity(type=m.IdentityType.UNKNOWN, status=m.IdentityStatus.ACTIVE, display_name='Storage QA')
                    pipeline = m.Pipeline(pipeline_id='storage-qa-' + uuid4().hex)
                    db.add_all([person, pipeline]); await db.flush()
                    vector = np.ones(512, dtype=np.float32) / np.sqrt(512)
                    embedding = m.IdentityEmbedding(identity_id=person.id, pipeline_id=pipeline.pipeline_id, embedding=vector)
                    db.add(embedding); await db.flush()
                    image = np.full((112, 112, 3), 100, dtype=np.uint8)
                    if layout == 'historical':
                        path = tmp_path / pipeline.pipeline_id / 'storageqa' / 'old.jpg'
                        path.parent.mkdir(parents=True)
                        assert cv2.imwrite(str(path), image)
                    else:
                        path = Path(storage.write_crop(image, pipeline.pipeline_id, uuid4(), uuid4(), datetime.utcnow()))
                    from backend.core.detection_evidence import persist_detection
                    outcome = await persist_detection(db, detection_data={
                        'pipeline_id': pipeline.pipeline_id,
                        'detection': {'pipeline_id': pipeline.pipeline_id, 'timestamp': datetime.utcnow()},
                        'faces': [{'name': 'Storage QA', 'identity_id': person.id, 'similarity': .95,
                            'label_state': 'auto_unknown', 'face_image_path': str(path), 'quality': .8,
                            'quality_scorer': 'fq1', '_embedding_id': embedding.id, '_event_id': uuid4().hex}],
                    })
                    face = (await db.execute(select(m.Face).where(m.Face.identity_id == person.id))).scalar_one()
                    await db.refresh(embedding)
                    assert embedding.detection_id == face.detection_id
                    assert face.face_image_path == str(path)
                    appearance = (await db.execute(select(m.IdentityAppearance).where(m.IdentityAppearance.identity_id == person.id))).scalar_one()
                    assert appearance.best_snapshot_path == str(path)
                    assert await storage.stored_camera_photos(db, person.id, pipeline.pipeline_id, 'Storage QA') == {str(path)}
                    # Renames have no effect on DB-backed file discovery.
                    assert await storage.stored_camera_photos(db, person.id, pipeline.pipeline_id, 'Renamed') == {str(path)}
                    pairs = {(person.id, pipeline.pipeline_id)}
                    assert (await storage.camera_photo_fallbacks(db, pairs))[next(iter(pairs))] == str(path)
                    from backend.routes.identities import _find_best_image_from_storage_for_identity
                    found = await _find_best_image_from_storage_for_identity(person, str(tmp_path), db)
                    assert storage.local_path(found) == path
                    from backend.utils.path_utils import normalize_storage_path
                    assert normalize_storage_path(str(path), str(tmp_path), check_exists=True).startswith('storage/')
                    # Retiring a detection must preserve a profile/appearance image.
                    assert await unreferenced_files(db, [str(path)], {'faces': m.Face.id == face.id}) == []
                    # An alert remains an independent reference after sightings expire.
                    watchlist = m.Watchlist(name='Storage QA ' + uuid4().hex)
                    db.add(watchlist); await db.flush()
                    entry = m.WatchlistEntry(watchlist_id=watchlist.id, identity_id=person.id)
                    db.add(entry); await db.flush()
                    alert = m.WatchlistAlert(watchlist_entry_id=entry.id, triggered_by='detection',
                        snapshot_path='storage/' + path.relative_to(tmp_path).as_posix())
                    db.add(alert); await db.flush()
                    await db.execute(delete(m.Face).where(m.Face.id == face.id))
                    assert (await storage.camera_photo_fallbacks(db, pairs))[next(iter(pairs))] == str(path)
                    assert await storage.stored_camera_photos(db, person.id, pipeline.pipeline_id, 'Renamed') == {str(path)}
                    assert await retire_snapshot(db, appearance) == 0
                    assert path.exists()
                    assert await retire_snapshot(db, person) == 0
                    assert path.exists()
                    assert await unreferenced_files(db, [str(path)]) == []
                    await db.delete(alert); await db.flush()
                    assert await unreferenced_files(db, [str(path)]) == [str(path)]
                    # Enrollment galleries never become scheduled-cleanup candidates.
                    gallery = tmp_path / 'faces' / str(person.id) / 'original.jpg'
                    gallery.parent.mkdir(parents=True); gallery.write_bytes(b'original')
                    person.best_snapshot_path = 'storage/' + gallery.relative_to(tmp_path).as_posix()
                    assert await retire_snapshot(db, person) == 0
                    assert person.best_snapshot_path is not None and gallery.exists()
                    # Last-reference removal deletes the crop, never its vector.
                    person.best_snapshot_path = str(path)
                    assert await retire_snapshot(db, person) == 1
                    assert not path.exists() and gallery.exists()
                    assert (await db.execute(select(m.IdentityEmbedding.id).where(m.IdentityEmbedding.id == embedding.id))).scalar_one() == embedding.id
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
