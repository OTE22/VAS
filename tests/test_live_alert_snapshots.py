"""Snapshot access and photo-cap regression checks without a live database."""
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import numpy as np
from fastapi import HTTPException
from config import settings
from backend.core import detection_storage as storage
from backend.core.live_alert_service import live_alert_service
from backend.routes.live_alerts import trigger_snapshot


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_and_admin_can_read_actual_image_but_other_users_cannot(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, 'STORAGE_DIR', folder):
            path = Path(folder) / 'snapshot.jpg'
            path.write_bytes(b'actual-event-image')
            trigger = SimpleNamespace(snapshot_path=str(path), alert=SimpleNamespace(created_by=7))
            with patch.object(live_alert_service, 'get_trigger_with_alert', AsyncMock(return_value=trigger)):
                for user in ({'id':7, 'role':'user'}, {'id':8, 'role':'admin'}):
                    response = await trigger_snapshot(uuid4(), object(), user)
                    self.assertEqual(Path(response.path).read_bytes(), b'actual-event-image')
                    self.assertEqual(response.headers['cache-control'], 'private, no-store')
                with self.assertRaises(HTTPException) as error:
                    await trigger_snapshot(uuid4(), object(), {'id':8, 'role':'user'})
                self.assertEqual(error.exception.status_code, 403)

    async def test_missing_expired_and_outside_storage_images_are_not_served(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, 'STORAGE_DIR', folder):
            for path in (None, str(Path(folder)/'missing.jpg'), '/etc/passwd', '../escape.jpg'):
                trigger = SimpleNamespace(snapshot_path=path, alert=SimpleNamespace(created_by=7))
                with patch.object(live_alert_service, 'get_trigger_with_alert', AsyncMock(return_value=trigger)):
                    with self.assertRaises(HTTPException) as error:
                        await trigger_snapshot(uuid4(), object(), {'id':7, 'role':'user'})
                    self.assertEqual(error.exception.status_code,404)

    async def test_photo_cap_allows_only_eligible_auto_capture_alert(self):
        @asynccontextmanager
        async def session():
            yield object()
        for eligible, auto_capture, save_enabled, expected in (
            (True,True,True,True), (False,True,True,False),
            (True,False,True,False), (True,True,False,False),
        ):
            with self.subTest(eligible=eligible,auto_capture=auto_capture,save_enabled=save_enabled):
                with tempfile.TemporaryDirectory() as folder, \
                     patch.object(settings,'STORAGE_DIR',folder), \
                     patch.object(settings,'SAVE_IMAGES',save_enabled), \
                     patch.object(settings,'MAX_PHOTOS_PER_PERSON',1), \
                     patch.object(settings,'LIVE_ALERTS_ENABLED',True), \
                     patch.object(storage,'stored_camera_photos',AsyncMock(return_value={'old.jpg'})), \
                     patch.object(live_alert_service,'get_active_alerts_for_identity',AsyncMock(return_value=[SimpleNamespace(auto_capture_snapshot=auto_capture)])), \
                     patch.object(live_alert_service,'_should_alert_trigger',AsyncMock(return_value=eligible)):
                    result = await storage.save_camera_crop(np.zeros((112,112,3),dtype=np.uint8),
                        pipeline_id='camera',capture_id=uuid4(),face_id=uuid4(),
                        captured_at=datetime.utcnow(),identity_id=uuid4(),name='Person',
                        session_factory=session,similarity=.95)
                    self.assertEqual(bool(result),expected)
                    self.assertEqual(len(list(Path(folder).rglob('*.jpg'))),int(expected))
