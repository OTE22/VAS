"""Admin-session gateway. VAS credentials never reach notebook kernels."""
import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from tornado import httpclient, httputil, ioloop, web, websocket

PREFIX = '/notebooks/'
API = os.environ.get('VAS_AUTH_URL', 'http://face_recognition:8000/api/ml/notebook-access')
UPSTREAM = 'http://notebook:8888'
MAX_EXPORT_BYTES = 8 * 1024 * 1024
MODEL_TYPES = frozenset({
    'behavior_anomaly_model', 'coappearance_anomaly_model', 'social_graph_anomaly_model',
    'threat_ranking_model', 'tabular_regression_model',
})
TOKEN = Path(os.environ['JUPYTER_TOKEN_FILE']).read_text().strip()
if len(TOKEN) < 32:
    raise RuntimeError('Notebook service token is missing')
HOP = {'connection', 'upgrade', 'keep-alive', 'transfer-encoding', 'te', 'trailer',
       'proxy-authorization', 'proxy-authenticate', 'content-length'}


def same_origin(request):
    origin = request.headers.get('Origin') or request.headers.get('Referer')
    if not origin:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme == 'https' and parsed.netloc == request.host


async def access(cookie):
    if not cookie:
        return 401
    try:
        response = await httpclient.AsyncHTTPClient().fetch(httpclient.HTTPRequest(
            API, headers={'Cookie': cookie}, request_timeout=8, follow_redirects=False), raise_error=False)
        return 204 if response.code == 204 else response.code if response.code in (401, 403) else 503
    except Exception:
        return 503


def upstream_headers(request, ws=False):
    headers = httputil.HTTPHeaders()
    for key in ('Content-Type', 'Accept', 'Range', 'If-None-Match', 'If-Modified-Since'):
        if key in request.headers:
            headers[key] = request.headers[key]
    headers['Authorization'] = 'token ' + TOKEN
    headers['Host'] = request.host
    headers['X-Forwarded-Proto'] = 'https'
    if ws:
        headers['Origin'] = 'https://' + request.host
    return headers


class Proxy(web.RequestHandler):
    async def prepare(self):
        # Do not allow browser credentials or tokens in URLs to leak upstream.
        if self.get_query_argument('token', None) is not None:
            raise web.HTTPError(400, reason='URL tokens are not supported')
        if not self.request.path.startswith(PREFIX):
            raise web.HTTPError(404)
        status = await access(self.request.headers.get('Cookie', ''))
        if status != 204:
            if status == 401 and self.request.method == 'GET' and 'text/html' in self.request.headers.get('Accept', ''):
                self.redirect('/signin')
                return
            raise web.HTTPError(status)
        if self.request.method not in ('GET', 'HEAD') and not same_origin(self.request):
            raise web.HTTPError(403, reason='Same-origin request required')

    async def forward(self):
        if self._finished:
            return
        try:
            result = await httpclient.AsyncHTTPClient().fetch(httpclient.HTTPRequest(
                UPSTREAM + self.request.uri, method=self.request.method,
                headers=upstream_headers(self.request),
                body=self.request.body if self.request.method in ('POST', 'PUT', 'PATCH') or self.request.body else None,
                request_timeout=120, follow_redirects=False), raise_error=False)
        except Exception:
            raise web.HTTPError(503, reason='Notebook workspace is starting or unavailable')
        self.set_status(result.code)
        self.clear_header('Content-Type')
        for key, value in result.headers.get_all():
            # Jupyter login cookies are unnecessary; only VAS authenticates browsers.
            if key.lower() not in HOP | {'set-cookie', 'server'}:
                self.add_header(key, value)
        self.set_header('Cache-Control', 'no-store')
        if self.request.method != 'HEAD' and result.code not in (204, 304):
            self.write(result.body)
        self.finish()

    get = post = put = patch = delete = head = forward


def workflow_export_url(payload):
    """Only identifiers are accepted: clients cannot supply destinations or code."""
    allowed = {'model_type', 'model_id', 'dataset_id', 'job_id'}
    if not isinstance(payload, dict) or not payload or set(payload) - allowed:
        raise web.HTTPError(400, reason='Choose a service, model, dataset or job')
    values = {}
    for key, value in payload.items():
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise web.HTTPError(400, reason='Notebook selection is invalid')
        if key == 'model_type':
            if value not in MODEL_TYPES:
                raise web.HTTPError(400, reason='Unknown model service')
        elif key in ('model_id', 'dataset_id'):
            try:
                value = str(uuid.UUID(value))
            except ValueError:
                raise web.HTTPError(400, reason='Model and dataset IDs must be UUIDs')
        elif not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
            raise web.HTTPError(400, reason='Job identifier is invalid')
        values[key] = value
    if not values:
        raise web.HTTPError(400, reason='Choose a service, model, dataset or job')
    origin = urlsplit(API)
    if origin.scheme not in ('http', 'https') or not origin.netloc or origin.username or origin.password:
        raise web.HTTPError(503, reason='Notebook API connection is unavailable')
    return f'{origin.scheme}://{origin.netloc}/api/ml/workflow-notebook?' + urlencode(values)


def prepare_notebook(body):
    """Validate the bounded generated document and remove any saved execution."""
    if len(body) > MAX_EXPORT_BYTES:
        raise web.HTTPError(502, reason='Notebook export exceeds the workspace import limit')
    try:
        notebook = json.loads(body)
    except (ValueError, UnicodeError):
        raise web.HTTPError(502, reason='Notebook export is not a valid document')
    if not isinstance(notebook, dict) or notebook.get('nbformat') != 4:
        raise web.HTTPError(502, reason='Notebook export format is unsupported')
    cells = notebook.get('cells')
    if not isinstance(cells, list) or not 1 <= len(cells) <= 500:
        raise web.HTTPError(502, reason='Notebook export contains invalid cells')
    if not isinstance(notebook.get('metadata', {}), dict):
        raise web.HTTPError(502, reason='Notebook export metadata is invalid')
    for cell in cells:
        if not isinstance(cell, dict) or cell.get('cell_type') not in ('markdown', 'code', 'raw'):
            raise web.HTTPError(502, reason='Notebook export contains an unsupported cell')
        source = cell.get('source', '')
        if not isinstance(source, str) and not (isinstance(source, list) and all(isinstance(line, str) for line in source)):
            raise web.HTTPError(502, reason='Notebook export contains invalid source text')
        metadata = cell.get('metadata', {})
        if not isinstance(metadata, dict):
            raise web.HTTPError(502, reason='Notebook export cell metadata is invalid')
        cell['metadata'] = {**metadata, 'trusted': False}
        if cell['cell_type'] == 'code':
            cell['outputs'] = []
            cell['execution_count'] = None
    notebook.setdefault('metadata', {}).pop('widgets', None)
    return notebook


class WorkflowImport(Proxy):
    """Import a server-generated notebook without exposing either credential."""
    def write_error(self, status_code, **kwargs):
        self.set_header('Content-Type', 'application/json')
        self.set_header('Cache-Control', 'no-store')
        self.finish({'error': {'code': 'NOTEBOOK_IMPORT_FAILED', 'message': self._reason}})

    async def post(self):
        if self._finished:
            return
        if len(self.request.body) > 4096:
            raise web.HTTPError(400, reason='Notebook selection is too large')
        if self.request.headers.get('Content-Type', '').split(';', 1)[0].strip() != 'application/json':
            raise web.HTTPError(415, reason='Notebook selection must be JSON')
        try:
            payload = json.loads(self.request.body)
        except (ValueError, UnicodeError):
            raise web.HTTPError(400, reason='Notebook selection must be valid JSON')
        export_url = workflow_export_url(payload)
        client = httpclient.AsyncHTTPClient(force_instance=True, max_body_size=MAX_EXPORT_BYTES)
        try:
            try:
                response = await client.fetch(httpclient.HTTPRequest(
                    export_url, headers={'Cookie': self.request.headers.get('Cookie', ''),
                                         'Accept': 'application/x-ipynb+json'},
                    request_timeout=60, follow_redirects=False), raise_error=False)
            except Exception:
                raise web.HTTPError(503, reason='Notebook export is unavailable; retry from ML Ops')
            if response.code != 200:
                status = response.code if response.code in (400, 401, 403, 404, 409, 422, 429) else 503
                reasons = {401: 'Sign in again to open the notebook', 403: 'Administrator access is required',
                           404: 'The selected model, dataset or job is no longer available',
                           409: 'The selected evidence is not compatible',
                           422: 'Choose a compatible service and evidence selection',
                           429: 'Too many notebook requests; try again shortly'}
                raise web.HTTPError(status, reason=reasons.get(status, 'Notebook export is unavailable; retry from ML Ops'))
            notebook = prepare_notebook(response.body)
            # Each request gets an unpredictable fresh name. Check before writing so
            # even a forced UUID collision cannot replace a saved user notebook.
            filename = 'vas-workflow-' + uuid.uuid4().hex + '.ipynb'
            contents_url = UPSTREAM + PREFIX + 'api/contents/' + filename
            headers = upstream_headers(self.request)
            headers['Content-Type'] = 'application/json'
            try:
                existing = await client.fetch(httpclient.HTTPRequest(
                    contents_url + '?content=0', headers=headers,
                    request_timeout=15, follow_redirects=False), raise_error=False)
                if existing.code != 404:
                    if existing.code == 200:
                        raise web.HTTPError(409, reason='Notebook name already exists; retry to create a new copy')
                    raise web.HTTPError(503, reason='Notebook workspace is unavailable')
                saved = await client.fetch(httpclient.HTTPRequest(
                    contents_url, method='PUT', headers=headers,
                    body=json.dumps({'type': 'notebook', 'format': 'json', 'content': notebook}),
                    request_timeout=60, follow_redirects=False), raise_error=False)
            except web.HTTPError:
                raise
            except Exception:
                raise web.HTTPError(503, reason='Notebook workspace is unavailable; retry from ML Ops')
            if saved.code not in (200, 201):
                raise web.HTTPError(503, reason='Notebook workspace could not save the new notebook')
            self.set_status(201)
            self.set_header('Cache-Control', 'no-store')
            self.finish({'url': PREFIX + 'lab/tree/' + filename, 'filename': filename})
        finally:
            client.close()

    async def get(self):
        raise web.HTTPError(405, reason='Use the Open workflow notebook action in ML Ops')

    put = patch = delete = head = get


class KernelSocket(websocket.WebSocketHandler):
    upstream = None
    check_timer = None

    def check_origin(self, origin):
        return same_origin(self.request)

    async def prepare(self):
        if self.get_query_argument('token', None) is not None:
            raise web.HTTPError(400)
        status = await access(self.request.headers.get('Cookie', ''))
        if status != 204:
            raise web.HTTPError(status)
        if not same_origin(self.request):
            raise web.HTTPError(403)

    def select_subprotocol(self, subprotocols):
        # Jupyter's binary v1 protocol is relayed without interpreting messages.
        return 'v1.kernel.websocket.jupyter.org' if 'v1.kernel.websocket.jupyter.org' in subprotocols else None

    async def open(self, *args):
        try:
            request = httpclient.HTTPRequest(UPSTREAM.replace('http:', 'ws:') + self.request.uri,
                                             headers=upstream_headers(self.request, ws=True), request_timeout=15)
            self.upstream = await websocket.websocket_connect(request,
                subprotocols=[self.selected_subprotocol] if self.selected_subprotocol else None,
                max_message_size=32 * 1024 * 1024)
            self.check_timer = ioloop.PeriodicCallback(self.recheck, 10000)
            self.check_timer.start()
            asyncio.create_task(self.read_upstream())
        except Exception:
            self.close(1011, 'Notebook kernel unavailable')

    async def recheck(self):
        if await access(self.request.headers.get('Cookie', '')) != 204:
            self.close(1008, 'Admin session ended. Sign in again.')
            if self.upstream:
                self.upstream.close()

    async def read_upstream(self):
        try:
            while self.upstream:
                message = await self.upstream.read_message()
                if message is None:
                    break
                await self.write_message(message, binary=isinstance(message, bytes))
        except Exception:
            pass
        finally:
            self.close()

    async def on_message(self, message):
        if self.upstream:
            await self.upstream.write_message(message, binary=isinstance(message, bytes))

    def on_close(self):
        if self.check_timer:
            self.check_timer.stop()
        if self.upstream:
            self.upstream.close()


def make_app():
    return web.Application([
        (r'/notebooks/vas/import', WorkflowImport),
        (r'/notebooks/(?:api/kernels/[^/]+/channels|api/events/subscribe)', KernelSocket),
        (r'/notebooks/.*', Proxy),
    ], websocket_max_message_size=32 * 1024 * 1024, log_function=lambda handler: None)


if __name__ == '__main__':
    make_app().listen(8080, max_buffer_size=32 * 1024 * 1024)
    ioloop.IOLoop.current().start()
