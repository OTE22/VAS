"""Gateway authorization tests with synthetic credentials and no application DB."""
import importlib.util
import json
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


class ImportValidationTests(unittest.TestCase):
    def test_source_is_constrained_to_configured_api_authority(self):
        url = gateway.workflow_export_url({'model_type': 'behavior_anomaly_model', 'job_id': 'mltrain_ab-123'})
        self.assertEqual(url, 'http://face_recognition:8000/api/ml/workflow-notebook?model_type=behavior_anomaly_model&job_id=mltrain_ab-123')
        for payload in ({}, [], {'url': 'https://attacker.invalid'}, {'model_type': 'made_up'},
                        {'job_id': '../escape'}, {'job_id': 'x' * 129}, {'dataset_id': 'bad'},
                        {'model_id': 1}, {'model_type': None}):
            with self.subTest(payload=payload), self.assertRaises(gateway.web.HTTPError):
                gateway.workflow_export_url(payload)

    def test_uuid_is_canonicalized(self):
        url = gateway.workflow_export_url({'dataset_id': 'ABCDEF0123456789ABCDEF0123456789'})
        self.assertIn('dataset_id=abcdef01-2345-6789-abcd-ef0123456789', url)

    def test_saved_execution_is_removed(self):
        data = {'nbformat': 4, 'metadata': {'widgets': {'untrusted': True}}, 'cells': [
            {'cell_type': 'code', 'source': 'print(1)', 'metadata': {'trusted': True},
             'execution_count': 9, 'outputs': [{'output_type': 'stream', 'text': 'old'}]}]}
        result = gateway.prepare_notebook(json.dumps(data).encode())
        self.assertEqual(result['cells'][0]['outputs'], [])
        self.assertIsNone(result['cells'][0]['execution_count'])
        self.assertFalse(result['cells'][0]['metadata']['trusted'])
        self.assertNotIn('widgets', result['metadata'])

    def test_invalid_and_oversized_exports_fail(self):
        for body in (b'not-json', b'{}', b'null', b'{"nbformat":4,"cells":[]}',
                     b'{"nbformat":4,"cells":[{"cell_type":"code","source":42}]}',
                     b'x' * (gateway.MAX_EXPORT_BYTES + 1)):
            with self.subTest(size=len(body)), self.assertRaises(gateway.web.HTTPError):
                gateway.prepare_notebook(body)


class WorkflowImportTests(AsyncHTTPTestCase):
    def get_app(self):
        return gateway.make_app()

    @staticmethod
    def response(code, document=None):
        return SimpleNamespace(code=code, body=json.dumps(document or {}).encode())

    def request_import(self, payload=None, responses=None, headers=None):
        notebook = {'nbformat': 4, 'nbformat_minor': 5, 'metadata': {}, 'cells': [
            {'cell_type': 'code', 'metadata': {}, 'source': ['print("debug")'],
             'outputs': [{'old': 'output'}], 'execution_count': 1}]}
        fake = SimpleNamespace(fetch=AsyncMock(side_effect=responses or [
            self.response(200, notebook), self.response(404), self.response(201)]), close=Mock())
        factory = Mock(return_value=fake)
        client_module = SimpleNamespace(AsyncHTTPClient=factory, HTTPRequest=httpclient.HTTPRequest)
        request_headers = {'Content-Type': 'application/json', 'Origin': self.get_url('').replace('http:', 'https:'),
                           'Cookie': 'access_token=synthetic-private-browser-session'}
        request_headers.update(headers or {})
        with patch.object(gateway, 'access', AsyncMock(return_value=204)), patch.object(gateway, 'httpclient', client_module):
            result = self.fetch('/notebooks/vas/import', method='POST', body=json.dumps(payload or {
                'model_type': 'behavior_anomaly_model'}), headers=request_headers, raise_error=False)
        return result, fake, factory

    def test_import_only_saves_new_notebook_and_never_starts_a_kernel(self):
        result, client, factory = self.request_import()
        self.assertEqual(result.code, 201)
        data = json.loads(result.body)
        self.assertRegex(data['filename'], r'^vas-workflow-[0-9a-f]{32}\.ipynb$')
        self.assertEqual(data['url'], '/notebooks/lab/tree/' + data['filename'])
        self.assertEqual(result.headers['Cache-Control'], 'no-store')
        self.assertEqual(client.fetch.await_count, 3)
        export, probe, save = [call.args[0] for call in client.fetch.call_args_list]
        self.assertEqual(export.headers['Cookie'], 'access_token=synthetic-private-browser-session')
        self.assertNotIn('Authorization', export.headers)
        self.assertNotIn('Cookie', save.headers)
        self.assertTrue(save.headers['Authorization'].startswith('token '))
        self.assertEqual(save.method, 'PUT')
        self.assertNotIn('/kernels', save.url)
        saved = json.loads(save.body)
        self.assertEqual(saved['content']['cells'][0]['outputs'], [])
        self.assertNotIn('synthetic-private-browser-session', str(saved))
        self.assertNotIn(gateway.TOKEN, str(saved))
        factory.assert_called_once_with(force_instance=True, max_body_size=gateway.MAX_EXPORT_BYTES)
        client.close.assert_called_once()

    def test_import_requires_admin_session(self):
        for status in (401, 403, 503):
            with self.subTest(status=status), patch.object(gateway, 'access', AsyncMock(return_value=status)):
                result = self.fetch('/notebooks/vas/import', method='POST', body='{}')
                self.assertEqual(result.code, status)
                self.assertEqual(json.loads(result.body)['error']['code'], 'NOTEBOOK_IMPORT_FAILED')

    def test_cross_origin_import_is_rejected(self):
        result, client, _ = self.request_import(headers={'Origin': 'https://attacker.invalid'})
        self.assertEqual(result.code, 403)
        client.fetch.assert_not_called()

    def test_invalid_selection_never_fetches_api(self):
        result, client, _ = self.request_import({'url': 'https://attacker.invalid/payload.ipynb'})
        self.assertEqual(result.code, 400)
        client.fetch.assert_not_called()

    def test_import_requires_json_content_type(self):
        result, client, _ = self.request_import(headers={'Content-Type': 'text/plain'})
        self.assertEqual(result.code, 415)
        client.fetch.assert_not_called()

    def test_existing_notebook_is_never_overwritten(self):
        notebook = {'nbformat': 4, 'cells': [{'cell_type': 'markdown', 'source': 'sample'}]}
        result, client, _ = self.request_import(responses=[self.response(200, notebook), self.response(200)])
        self.assertEqual(result.code, 409)
        self.assertEqual(client.fetch.await_count, 2)
        client.close.assert_called_once()

    def test_export_errors_do_not_create_workspace_files_or_leak_api_body(self):
        for status in (401, 403, 404, 409, 422, 429, 500):
            with self.subTest(status=status):
                result, client, _ = self.request_import(responses=[self.response(status, {'detail': 'sensitive traceback'})])
                self.assertEqual(result.code, status if status != 500 else 503)
                self.assertNotIn(b'sensitive traceback', result.body)
                self.assertEqual(client.fetch.await_count, 1)

    def test_workspace_failure_is_actionable(self):
        notebook = {'nbformat': 4, 'cells': [{'cell_type': 'markdown', 'source': 'sample'}]}
        for responses in ([self.response(200, notebook), self.response(500)],
                          [self.response(200, notebook), self.response(404), self.response(500)]):
            result, client, _ = self.request_import(responses=responses)
            self.assertEqual(result.code, 503)
            self.assertIn('Notebook workspace', json.loads(result.body)['error']['message'])
            client.close.assert_called_once()

    def test_get_import_does_not_create_notebook(self):
        with patch.object(gateway, 'access', AsyncMock(return_value=204)):
            result = self.fetch('/notebooks/vas/import')
        self.assertEqual(result.code, 405)


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
