"""Gateway authorization tests with synthetic credentials and no application DB."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tornado import httpclient, httputil
from tornado.testing import AsyncHTTPTestCase

ROOT = Path(__file__).resolve().parents[1]
secret = tempfile.NamedTemporaryFile(mode='w', delete=False)
secret.write('synthetic-notebook-token-' + 'a' * 32); secret.close()
os.environ['JUPYTER_TOKEN_FILE'] = secret.name
spec = importlib.util.spec_from_file_location('notebook_gateway', ROOT / 'docker/notebook/gateway.py')
gateway = importlib.util.module_from_spec(spec); spec.loader.exec_module(gateway)


class GatewayTests(AsyncHTTPTestCase):
    def get_app(self):
        return gateway.make_app()

    def test_anonymous_page_redirects_to_vas_signin(self):
        with patch.object(gateway, 'access', AsyncMock(return_value=401)):
            r = self.fetch('/notebooks/lab', headers={'Accept':'text/html'}, follow_redirects=False)
        self.assertEqual(r.code, 302); self.assertEqual(r.headers['Location'], '/signin')

    def test_non_admin_and_session_outages_fail_closed(self):
        for status in (403,503):
            with patch.object(gateway, 'access', AsyncMock(return_value=status)):
                self.assertEqual(self.fetch('/notebooks/api/kernels').code, status)

    def test_cross_origin_mutation_refused_even_with_valid_session(self):
        with patch.object(gateway, 'access', AsyncMock(return_value=204)):
            r = self.fetch('/notebooks/api/kernels', method='POST', body='{}', headers={'Origin':'https://attacker.invalid'})
        self.assertEqual(r.code,403)

    def test_query_token_is_never_forwarded(self):
        with patch.object(gateway, 'access', AsyncMock(return_value=204)):
            self.assertEqual(self.fetch('/notebooks/lab?token=private').code,400)

    def test_service_headers_exclude_vas_credentials(self):
        request=SimpleNamespace(host='vas.example',headers={'Cookie':'access_token=private','Authorization':'Bearer private','Accept':'application/json'})
        headers=gateway.upstream_headers(request)
        self.assertNotIn('Cookie',headers)
        self.assertNotIn('private',str(headers))
        self.assertTrue(headers['Authorization'].startswith('token '))


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_kernel_closes_when_session_ends(self):
        socket=SimpleNamespace(request=SimpleNamespace(headers={'Cookie':'expired'}), close=Mock(), upstream=SimpleNamespace(close=Mock()))
        with patch.object(gateway,'access',AsyncMock(return_value=401)):
            await gateway.KernelSocket.recheck(socket)
        socket.close.assert_called_once(); socket.upstream.close.assert_called_once()

    async def test_empty_delete_reaches_upstream_without_invalid_body(self):
        handler=SimpleNamespace(_finished=False,
            request=SimpleNamespace(uri='/notebooks/api/kernels/fixture', method='DELETE',
                body=b'', host='vas.example', headers={}),
            set_status=Mock(), clear_header=Mock(), add_header=Mock(), set_header=Mock(), write=Mock(), finish=Mock())
        result=SimpleNamespace(code=204, headers=httputil.HTTPHeaders(), body=b'')
        with patch.object(gateway.httpclient.AsyncHTTPClient,'fetch',AsyncMock(return_value=result)) as fetch:
            await gateway.Proxy.forward(handler)
        self.assertIsNone(fetch.call_args.args[0].body)
        handler.set_status.assert_called_once_with(204)
        handler.write.assert_not_called()

    async def test_gateway_auth_outage_is_not_anonymous_access(self):
        with patch.object(gateway.httpclient.AsyncHTTPClient,'fetch',AsyncMock(side_effect=OSError('offline'))):
            self.assertEqual(await gateway.access('session'),503)


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_private_header_authenticates_not_cookie_or_query(self):
        spec=importlib.util.spec_from_file_location('gateway_identity',ROOT/'docker/notebook/gateway_identity.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        provider=module.GatewayIdentityProvider(token='')
        self.assertTrue(provider.auth_enabled)
        self.assertFalse(provider.login_available)
        self.assertEqual(provider.token,'')
        handler=SimpleNamespace(request=SimpleNamespace(headers={}))
        self.assertIsNone(await provider.get_user_token(handler))
        handler.request.headers['Authorization']='token '+Path(secret.name).read_text()
        self.assertEqual((await provider.get_user_token(handler)).username,'vas-admin')
        self.assertIsNone(provider.get_user_cookie(handler))


def tearDownModule():
    Path(secret.name).unlink(missing_ok=True)


if __name__ == '__main__': unittest.main()
