"""Offline portrait/representative regressions. SQLite is in-memory only."""
import uuid
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np
from sqlalchemy import create_engine, text

from backend.core.detection_storage import portrait_from_frame
from backend.core.identity_service import IdentityService
from db_models import IdentityType


class PortraitTests(unittest.TestCase):
    def test_original_pixels_and_margin_are_preserved_without_upscaling(self):
        frame = np.arange(300 * 400 * 3, dtype=np.uint8).reshape(300, 400, 3)
        actual = portrait_from_frame(frame, [100, 100, 200, 200])
        np.testing.assert_array_equal(actual, frame[80:220, 80:220])
        self.assertFalse(np.shares_memory(actual, frame))
        self.assertEqual(portrait_from_frame(frame, [10, 10, 20, 20]).shape, (14, 14, 3))

    def test_frame_edge_clamps_without_black_padding(self):
        frame = np.full((100, 120, 3), 180, np.uint8)
        actual = portrait_from_frame(frame, [0, 0, 50, 80])
        self.assertEqual(actual.shape, (96, 60, 3))
        self.assertTrue((actual == 180).all())

    def test_large_portrait_has_bounded_payload_and_preserves_aspect(self):
        frame = np.full((2160, 3840, 3), 180, np.uint8)
        actual = portrait_from_frame(frame, [500, 500, 1500, 1000])
        self.assertEqual(actual.shape, (320, 640, 3))
        self.assertTrue((actual == 180).all())

    def test_invalid_boxes_fail_without_accidental_negative_slices(self):
        frame = np.ones((100, 100, 3), np.uint8)
        for box in ([20, 20, 10, 30], [0, 0, float('nan'), 20],
                    [-100, -100, -20, -20], [120, 0, 130, 20]):
            with self.subTest(box=box), self.assertRaises(ValueError):
                portrait_from_frame(frame, box)


class RepresentativeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Use the real SQLAlchemy query against tiny isolated tables. No
        # application database/schema creation or connections are involved.
        self.engine = create_engine('sqlite://')
        self.connection = self.engine.connect()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.connection.close)
        for ddl in (
            'CREATE TABLE faces (identity_id TEXT, detection_id INTEGER, face_image_path TEXT)',
            'CREATE TABLE identity_embeddings (identity_id TEXT, detection_id INTEGER, quality FLOAT, quality_scorer_version TEXT)',
            'CREATE TABLE identity_images (id INTEGER, identity_id TEXT, is_primary BOOLEAN)',
        ):
            self.connection.execute(text(ddl))
        self.iid = uuid.uuid4()
        self.service = IdentityService.__new__(IdentityService)
        self.db = SimpleNamespace(execute=AsyncMock(side_effect=self.connection.execute),
                                  add=lambda row: None, flush=AsyncMock())
        self.identity = SimpleNamespace(id=self.iid, type=IdentityType.UNKNOWN,
            display_name='Test', best_snapshot_path='old.jpg', appearances_count=1)

    def seed(self, path, detection, quality, scorer='full-v1', iid=None):
        args = dict(i=(iid or self.iid).hex, d=detection, p=path, q=quality, s=scorer)
        self.connection.execute(text('INSERT INTO faces VALUES (:i,:d,:p)'), args)
        self.connection.execute(text('INSERT INTO identity_embeddings VALUES (:i,:d,:q,:s)'), args)

    async def appearance(self, quality, scorer='full-v1'):
        return await self.service.create_appearance(identity=self.identity, pipeline_id='camera',
            track_id=None, start_time=datetime(2026, 10, 5), best_snapshot_path='new.jpg',
            db=self.db, quality_score=quality, quality_scorer_version=scorer, similarity=1.)

    async def test_better_photo_replaces_old_even_when_new_embedding_already_exists(self):
        self.seed('old.jpg', 1, .4)
        self.seed('new.jpg', 2, .8)
        self.seed('another.jpg', 3, .99)
        await self.appearance(.8)
        self.assertEqual(self.identity.best_snapshot_path, 'new.jpg')

    async def test_worse_and_equal_photos_preserve_representative_but_keep_event_photo(self):
        self.seed('old.jpg', 1, .8)
        for quality in (.3, .8):
            event = await self.appearance(quality)
            self.assertEqual(self.identity.best_snapshot_path, 'old.jpg')
            self.assertEqual(event.best_snapshot_path, 'new.jpg')

    async def test_other_identity_in_same_detection_does_not_supply_score(self):
        self.seed('old.jpg', 1, .4)
        self.seed('other.jpg', 1, .99, iid=uuid.uuid4())
        self.assertAlmostEqual(await self.service._camera_snapshot_quality(
            self.db, self.iid, 'old.jpg', 'full-v1'), .4)

    async def test_scorer_versions_are_not_mixed(self):
        self.seed('old.jpg', 1, .99, scorer='legacy-v1')
        self.assertIsNone(await self.service._camera_snapshot_quality(self.db, self.iid, 'old.jpg', 'full-v1'))
        await self.appearance(.8)
        self.assertEqual(self.identity.best_snapshot_path, 'new.jpg')

    async def test_known_enrollment_remains_authoritative(self):
        self.identity.type = IdentityType.KNOWN
        self.connection.execute(text('INSERT INTO identity_images VALUES (1,:i,1)'), {'i': self.iid.hex})
        event = await self.appearance(1.)
        self.assertEqual(self.identity.best_snapshot_path, 'old.jpg')
        self.assertEqual(event.best_snapshot_path, 'new.jpg')


class IngestPortraitTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_and_live_photos_use_original_frame_coordinates(self):
        import base64
        from contextlib import asynccontextmanager
        from backend.services import image_processing as processing
        from backend.core import detection_storage
        from config import settings
        frame = np.full((400, 500, 3), 170, np.uint8)
        frame[95:200, 70:180] = [50, 100, 150]
        aligned = np.zeros((112, 112, 3), np.uint8)
        crop_result = dict(error=None, stage='done', kpss=np.zeros((1, 5, 2)),
            bboxes=np.array([[50, 60, 130, 150, .99]]), face_bbox=[50, 60, 130, 150],
            aligned_face=aligned, embedding=np.ones(512) / np.sqrt(512),
            quality=.8, quality_scorer='full-v1', acceptance={'accepted': True})
        person = SimpleNamespace(id=uuid.uuid4(), type=IdentityType.UNKNOWN, display_name='Unknown')
        resolution = SimpleNamespace(identity=person, is_new_identity=True, similarity=.8,
            embedding_id=42, identity_created=True)
        service = SimpleNamespace(find_or_create_identity=AsyncMock(return_value=resolution),
            quality_threshold=.1, use_pgvector=True, pgvector_index=None)
        @asynccontextmanager
        async def session():
            yield SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        writer = SimpleNamespace(add_detection=AsyncMock())
        with patch.object(processing, 'db_manager', SimpleNamespace(get_session=session)), \
             patch.object(processing, 'ensure_pipeline_registered', AsyncMock()), \
             patch.object(processing, 'resolve_display_name', AsyncMock(return_value='Test')), \
             patch.object(processing, '_decode_frame_sync', return_value=frame), \
             patch.object(processing, '_process_crop_sync', return_value=crop_result), \
             patch.object(processing, 'batch_writer', writer), \
             patch.object(processing, 'FACE_TRACKING_ENABLED', False), \
             patch.object(settings, 'SAVE_CROPPED_IMAGES', False), \
             patch.object(settings, 'SKIP_UNKNOWN_FACES', False), \
             patch('backend.core.identity_service.identity_service', service), \
             patch.object(detection_storage, 'save_camera_crop', AsyncMock(return_value='/portrait.jpg')) as save:
            output = await processing.process_image_async(image_bytes=b'frame',
                predictions=[{'bbox': [40, 50, 300, 350], 'class_name': 'person', 'confidence': .99}],
                pipeline_id='portrait-test', use_batch_write=True, send_realtime_updates=False)
        # Crop-local face coordinates must be translated by (40, 50).
        expected = portrait_from_frame(frame, [90, 110, 170, 200])
        np.testing.assert_array_equal(save.call_args.args[0], expected)
        live = cv2.imdecode(np.frombuffer(base64.b64decode(output['faces'][0]['image']), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(live.shape, expected.shape)
        self.assertLess(np.abs(live.astype(float) - expected).mean(), 3)
        self.assertEqual(writer.add_detection.call_args.args[0]['faces'][0]['face_image_path'], '/portrait.jpg')
        np.testing.assert_array_equal(service.find_or_create_identity.call_args.kwargs['embedding'], crop_result['embedding'])
        self.assertFalse(aligned.any())  # the recognition crop is untouched
