"""Opt-in Firefox checks for notebook actions; all HTTP responses are local fixtures.

Run: python3 tests/browser_mlops_notebook.py --webdriver http://127.0.0.1:4445
Uses the repository JavaScript without a production login or database.
"""
from __future__ import annotations

import argparse
import base64
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/style.css"><style>body{margin:20px;background:#101c2b;color:#e6eef8;font:16px sans-serif}.mlops-notebook-guide{max-width:950px}</style>
<main class="mlops-notebook-guide"><h2>Follow the pipeline in Jupyter</h2><p>Inspect saved production evidence and offline snapshot checks.</p><div id="actions"></div><div id="other"></div></main>
<script>
window.fixtureErrors=[];window.requests=[];window.tabs=[];window.fixture={status:200,url:'/notebooks/lab/tree/test.ipynb',delay:false};
addEventListener('error',e=>fixtureErrors.push(e.message));addEventListener('unhandledrejection',e=>fixtureErrors.push(String(e.reason)));
window.open=()=>{if(fixture.blocked)return null;const t={closed:false,location:{replace(url){t.url=url}},close(){t.closed=true}};tabs.push(t);return t};
window.fetch=async(url,options)=>{requests.push({url,options});if(fixture.delay)await new Promise(resolve=>window.resolveImport=resolve);if(fixture.network)throw new TypeError('Network request failed');return new Response(JSON.stringify(fixture.status===200?{url:fixture.url}:{detail:{message:'<'+'script>unsafe<'+'/script> Dataset not found'}}),{status:fixture.status,headers:{'Content-Type':'application/json'}})};
</script><script src="/diagnostics.js"></script>'''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        paths = {'/': (HTML.encode(), 'text/html'),
                 '/diagnostics.js': ((ROOT / 'frontend/js/mlops-diagnostics.js').read_bytes(), 'text/javascript'),
                 '/style.css': ((ROOT / 'frontend/css/admin-ml-ops.css').read_bytes(), 'text/css')}
        if self.path not in paths:
            self.send_error(404)
            return
        body, kind = paths[self.path]
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def run(base):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(path, body=None, method=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                value = json.load(response)['value']
        except urllib.error.HTTPError as error:
            raise AssertionError(error.read().decode()) from error
        if isinstance(value, dict) and value.get('error'):
            raise AssertionError(value)
        return value

    sid = call('/session', {'capabilities': {'alwaysMatch': {'browserName': 'firefox', 'moz:firefoxOptions': {'args': ['-headless']}}}})['sessionId']
    prefix = '/session/' + sid

    def js(source):
        return call(prefix + '/execute/sync', {'script': source, 'args': []})

    def wait(source):
        for _ in range(100):
            if js(source):
                return
            time.sleep(.03)
        raise AssertionError('Timed out: ' + source + ' errors=' + str(js('return fixtureErrors')) + ' body=' + str(js('return document.body.innerText')))

    try:
        call(prefix + '/window/rect', {'width': 1280, 'height': 850})
        call(prefix + '/url', {'url': 'http://127.0.0.1:' + str(server.server_port) + '/'})
        assert js("return MLOpsDiagnostics.contextFor({model_id:'model',job_id:'stale',dataset_id:'stale'})") == {'model_id': 'model'}
        assert js("return MLOpsDiagnostics.contextFor({job_id:'job',dataset_id:'stale'})") == {'job_id': 'job'}
        assert js("return MLOpsDiagnostics.contextFor({model_type:'behavior_anomaly_model'})") == {'model_type': 'behavior_anomaly_model'}
        for value in ['https://attacker.invalid/notebooks/lab/tree/test.ipynb', '//attacker.invalid/a.ipynb', '/notebooks/lab/tree/../api/test.ipynb', '/notebooks/lab/tree/test.ipynb?token=secret', 'javascript:alert(1)']:
            assert js('return MLOpsDiagnostics.safeNotebookUrl(' + json.dumps(value) + ')') is None
        js("MLOpsDiagnostics.updatePanel(document.querySelector('#actions'),{model_type:'behavior_anomaly_model',model_id:'chosen',dataset_id:'stale'});document.querySelector('#actions button').click()")
        wait("return document.querySelector('#actions .mlops-notebook-ready')")
        request = js('return requests[0]')
        assert request['url'] == '/notebooks/vas/import'
        assert request['options']['method'] == 'POST'
        assert request['options']['credentials'] == 'same-origin'
        assert request['options']['headers']['X-Requested-With'] == 'XMLHttpRequest'
        assert json.loads(request['options']['body']) == {'model_type': 'behavior_anomaly_model', 'model_id': 'chosen'}
        assert js('return tabs[0].opener') is None
        assert js('return tabs[0].url').endswith('/notebooks/lab/tree/test.ipynb')
        assert 'model_id=chosen' in js("return document.querySelector('#actions a[download]').href")
        js("MLOpsDiagnostics.updatePanel(document.querySelector('#actions'),{model_type:'behavior_anomaly_model',model_id:'chosen'})")
        assert js("return !!document.querySelector('#actions .mlops-notebook-ready')"), 'status refresh preserves the prepared link'
        print('PASS primary lineage, safe URLs, authenticated import, download, new tab and stable refresh')

        js("fixture.blocked=true;MLOpsDiagnostics.updatePanel(document.querySelector('#actions'),{job_id:'job'});document.querySelector('#actions button').click()")
        wait("return document.querySelector('#actions .mlops-notebook-ready')")
        assert js('return tabs.length') == 1
        assert js("return document.querySelector('#actions .mlops-notebook-ready').rel") == 'noopener noreferrer'
        js("fixture.blocked=false;fixture.status=404;MLOpsDiagnostics.updatePanel(document.querySelector('#actions'),{dataset_id:'missing'});document.querySelector('#actions button').click()")
        wait("return document.querySelector('#actions .mlops-notebook-status').textContent.includes('Dataset not found')")
        assert js("return document.querySelectorAll('#actions script').length") == 0
        assert js('return tabs.at(-1).closed') is True
        assert js("return document.querySelector('#actions button').disabled") is False
        assert js("return !!document.querySelector('#actions a[download]')")
        js("fixture.status=401;document.querySelector('#actions button').click()")
        wait("return !document.querySelector('#actions button').disabled")
        assert js('return tabs.at(-1).closed')
        js("fixture.status=200;fixture.url='https://attacker.invalid/unsafe.ipynb';document.querySelector('#actions button').click()")
        wait("return document.querySelector('#actions .mlops-notebook-status').textContent.includes('invalid link')")
        assert js('return tabs.at(-1).closed')
        print('PASS popup fallback, missing data, expired session, escaped error content and unsafe response rejection')

        js("fixture.url='/notebooks/lab/tree/ready.ipynb';fixture.delay=true;MLOpsDiagnostics.updatePanel(document.querySelector('#actions'),{job_id:'one'});MLOpsDiagnostics.updatePanel(document.querySelector('#other'),{job_id:'one'});window.before=requests.length;document.querySelector('#actions button').click();document.querySelector('#other button').click()")
        assert js('return requests.length-before') == 1
        js('resolveImport()')
        wait("return document.querySelectorAll('.mlops-notebook-ready').length===2")
        js("fixture.delay=false;fixture.network=true;document.querySelector('#actions button').click()")
        wait("return document.querySelector('#actions .mlops-notebook-status').textContent.includes('Network request failed')")
        assert js('return tabs.at(-1).closed')
        call(prefix + '/window/rect', {'width': 390, 'height': 850})
        assert js('return document.documentElement.scrollWidth <= innerWidth')
        assert js('return fixtureErrors') == []
        output = Path('/tmp/vas-notebook-ui-mobile.png')
        output.write_bytes(base64.b64decode(call(prefix + '/screenshot')))
        js("document.querySelector('#other').replaceChildren(MLOpsDiagnostics.render({job_id:'legacy',status:'failed',details:{stage_history:[{stage:'training',duration_seconds:0},{stage:'evaluating'}]}}))")
        assert js("return [...document.querySelectorAll('#other .mlops-diagnostic-status')].map(node=>node.textContent)") == ['completed', 'failed']
        js("document.querySelector('#other').replaceChildren(MLOpsDiagnostics.render({job_id:'complete',status:'completed',details:{stage_history:[{stage:'registering'}]}}))")
        assert js("return document.querySelector('#other .mlops-diagnostic-status').textContent") == 'completed'
        js("document.querySelector('#other').replaceChildren(MLOpsDiagnostics.render({job_id:'nested',status:'running',details:{preparation:{status:'completed',snapshots_written:10},dataset_diagnostics:{stage_history:[{stage:'validation',status:'completed',duration_seconds:1}]},training_diagnostics:{stage_history:[{stage:'loading_dataset',duration_seconds:2},{stage:'training'}]},stage_history:[{stage:'stale_top_level'}]}}))")
        assert js("return [...document.querySelectorAll('#other .mlops-diagnostic-status')].map(node=>node.textContent)") == ['completed', 'completed', 'completed', 'running']
        assert 'stale top level' not in js("return document.querySelector('#other').textContent")
        assert 'Dataset · validation' in js("return document.querySelector('#other').textContent")
        assert 'Training · training' in js("return document.querySelector('#other').textContent")
        js("document.querySelector('#other').replaceChildren(MLOpsDiagnostics.render({job_id:'empty',status:'failed',details:{}}))")
        assert 'No recorded stage history' in js("return document.querySelector('#other').textContent")
        print('PASS legacy and nested stage status, zero-second completion, missing evidence and no duplicate stale history')
        print('PASS duplicate import coalescing, network failure, mobile layout and no JavaScript errors')
        print('Screenshot: ' + str(output))
    finally:
        call(prefix, method='DELETE')
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--webdriver', default='http://127.0.0.1:4445')
    run(parser.parse_args().webdriver.rstrip('/'))
