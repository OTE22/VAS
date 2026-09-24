import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

class AccessTests(unittest.TestCase):
    def test_session_revocation_and_permissions(self):
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        from backend.routes.ml_ops import router, ML_MANAGE
        from backend.auth.auth_service import AuthService
        app=FastAPI(); app.include_router(router)
        app.dependency_overrides[ML_MANAGE]=lambda:SimpleNamespace(username='fixture')
        with TestClient(app) as client, patch.object(AuthService,'decode_token',return_value={'jti':'fixture'}):
            self.assertEqual(client.get('/api/ml/notebook-access').status_code,401)
            client.cookies.set('access_token','synthetic')
            with patch('backend.auth.auth_security.is_token_revoked',AsyncMock(return_value=False)):
                result=client.get('/api/ml/notebook-access')
                self.assertEqual(result.status_code,204)
                self.assertEqual(result.content,b'')
            with patch('backend.auth.auth_security.is_token_revoked',AsyncMock(return_value=True)):
                self.assertEqual(client.get('/api/ml/notebook-access').status_code,401)
            with patch('backend.auth.auth_security.is_token_revoked',AsyncMock(side_effect=OSError('offline'))):
                self.assertEqual(client.get('/api/ml/notebook-access').status_code,503)
            async def denied():raise HTTPException(403,'Admin required')
            app.dependency_overrides[ML_MANAGE]=denied
            self.assertEqual(client.get('/api/ml/notebook-access').status_code,403)

class SharedRevocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_store_required(self):
        from backend.auth.auth_security import is_token_revoked
        with patch('backend.auth.auth_security._redis', AsyncMock(return_value=None)):
            with self.assertRaises(RuntimeError):
                await is_token_revoked('fixture', require_shared_store=True)
        client = SimpleNamespace(exists=AsyncMock(side_effect=OSError('offline')))
        with patch('backend.auth.auth_security._redis', AsyncMock(return_value=client)):
            with self.assertRaises(OSError):
                await is_token_revoked('fixture', require_shared_store=True)
