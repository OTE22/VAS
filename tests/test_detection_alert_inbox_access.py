"""Read/write authorization and severity validation without external services."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from backend.routes import watchlists, live_alerts
from backend.auth.auth_service import get_current_user
from db_connection import get_db


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(watchlists.router)
        self.client = TestClient(self.app)
        async def fake_db():
            yield object()
        self.app.dependency_overrides[get_db] = fake_db
        self.url = '/api/detection-alerts/inbox'
        self.ack = self.url + '/00000000-0000-0000-0000-000000000001/acknowledge'
        self.body = {'source':'live','latest_id':'00000000-0000-0000-0000-000000000001'}

    def user(self, role):
        self.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=7, username='test', email='test@example.invalid', role=role, is_active=True)

    def test_missing_auth_cannot_read_or_acknowledge(self):
        self.assertIn(self.client.get(self.url).status_code, [401,403])
        self.assertIn(self.client.post(self.ack,json=self.body).status_code, [401,403])

    def test_nonadmin_cannot_read_acknowledge_or_fetch_snapshots(self):
        self.user('user')
        self.assertEqual(self.client.get(self.url).status_code,403)
        self.assertEqual(self.client.post(self.ack,json=self.body).status_code,403)
        self.assertEqual(self.client.get('/api/detection-alerts/live/00000000-0000-0000-0000-000000000001/snapshot').status_code,403)

    def test_admin_read_is_not_cached(self):
        self.user('admin')
        with patch.object(watchlists.detection_alert_inbox,'list_inbox',AsyncMock(return_value={'items':[], 'total':0})):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['cache-control'],'no-store')

    def test_ack_requires_csrf_and_passes_authenticated_actor(self):
        self.user('admin')
        with patch.object(watchlists.detection_alert_inbox,'acknowledge_episode',AsyncMock(return_value=2)) as ack, patch.object(watchlists,'_broadcast_change',AsyncMock()):
            self.assertEqual(self.client.post(self.ack,json=self.body).status_code,403)
            ack.assert_not_called()
            response=self.client.post(self.ack,json=self.body,headers={'X-Requested-With':'XMLHttpRequest'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(ack.call_args.args[-1],7)

    def test_invalid_source_and_stale_ack_are_rejected(self):
        self.user('admin')
        headers={'X-Requested-With':'XMLHttpRequest'}
        self.assertEqual(self.client.post(self.ack,json={**self.body,'source':'users'},headers=headers).status_code,422)
        with patch.object(watchlists.detection_alert_inbox,'acknowledge_episode',AsyncMock(return_value=0)):
            self.assertEqual(self.client.post(self.ack,json=self.body,headers=headers).status_code,409)

    def test_update_omission_preserves_severity_and_invalid_values_rejected(self):
        self.assertNotIn('alert_level',live_alerts.UpdateLiveAlertRequest(name='Rename').model_dump(exclude_unset=True))
        for invalid in [None,'urgent','CRITICAL']:
            with self.assertRaises(ValidationError):
                live_alerts.UpdateLiveAlertRequest(alert_level=invalid)
        self.assertEqual(live_alerts.CreateLiveAlertRequest(name='Test',identity_id='id').alert_level,'warning')
