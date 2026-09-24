"""Admin-session gateway. VAS credentials never reach notebook kernels."""
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit

from tornado import httpclient, httputil, ioloop, web, websocket

PREFIX = '/notebooks/'
API = os.environ.get('VAS_AUTH_URL', 'http://face_recognition:8000/api/ml/notebook-access')
UPSTREAM = 'http://notebook:8888'
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
        (r'/notebooks/(?:api/kernels/[^/]+/channels|api/events/subscribe)', KernelSocket),
        (r'/notebooks/.*', Proxy),
    ], websocket_max_message_size=32 * 1024 * 1024, log_function=lambda handler: None)


if __name__ == '__main__':
    make_app().listen(8080, max_buffer_size=32 * 1024 * 1024)
    ioloop.IOLoop.current().start()
