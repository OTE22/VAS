const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function harness() {
    const nodes = new Map();
    const events = {};
    const pending = [];
    const timers = new Map();
    let timerId = 0;
    function element() {
        return {textContent: '', innerHTML: '', checked: true, style: {},
            classList: {toggle() {}}, addEventListener() {}};
    }
    const document = {
        hidden: false,
        getElementById(id) { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); },
        createElement() {
            const node = element();
            Object.defineProperty(node, 'innerHTML', {get() {
                return node.textContent.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
            }});
            return node;
        },
        addEventListener(name, callback) { events[name] = callback; }
    };
    const context = vm.createContext({document, window: {addEventListener() {}},
        URLSearchParams, AbortController, Date, console,
        setTimeout(fn) {timers.set(++timerId, fn); return timerId;},
        clearTimeout(id) {timers.delete(id);},
        fetch(url, options) { return new Promise(resolve => pending.push({url, options, resolve})); }
    });
    vm.runInContext(fs.readFileSync('frontend/js/admin-logs.js', 'utf8'), context);
    return {context, nodes, document, pending, timers,
        run: code => vm.runInContext(code, context),
        reply(index, data) { pending[index].resolve({ok: true, headers: {get: () => 'application/json'}, json: async () => data}); }
    };
}

test('older response cannot overwrite newer resource data', async () => {
    const h = harness();
    const first = h.run("request('logs', '/old', data => $('logs-container').textContent = data.value)");
    const second = h.run("request('logs', '/new', data => $('logs-container').textContent = data.value)");
    assert.equal(h.pending[0].options.signal.aborted, true);
    h.reply(1, {value: 'new'}); await second;
    h.reply(0, {value: 'old'}); await first;
    assert.equal(h.nodes.get('logs-container').textContent, 'new');
});

test('partial pagination never claims a complete total; messages escape markup', () => {
    const h = harness();
    h.run("updatePagination({logs:[{}], page:2, total_pages:2, total_count:50, truncated:true, has_previous:true, has_next:true})");
    assert.match(h.nodes.get('pagination-info').textContent, /partial scan/);
    assert.doesNotMatch(h.nodes.get('pagination-info').textContent, /of 2/);
    assert.match(h.run("renderLogEntry({level:'INFO',message:'<img src=x>'})"), /&lt;img/);
});

test('hidden page and page suspension stop requests and polling', async () => {
    const h = harness();
    h.document.hidden = true;
    await h.run('refresh(true)');
    assert.equal(h.pending.length, 0);
    h.document.hidden = false;
    const request = h.run("request('logs', '/logs', () => {})");
    h.run('suspend()');
    assert.equal(h.pending[0].options.signal.aborted, true);
    h.reply(0, {}); await request;
    assert.equal(h.timers.size, 0);
});

test('automatic refresh does not replace older log pages', async () => {
    const h = harness();
    h.run('currentPage = 2');
    const refresh = h.run('refresh(true)');
    assert.equal(h.pending.length, 2);
    assert.ok(h.pending.every(item => !item.url.startsWith('/api/logs?')));
    h.reply(0, {services: [], ml_worker: {status:'offline'}, errors: [], history_available:true});
    h.reply(1, {});
    await refresh;
});

test('optional source absence shows a useful empty state instead of refresh errors', async () => {
    const h = harness();
    h.run("currentSource = 'ml-job'");
    const logs = h.run('loadLogs()');
    h.reply(0, {logs: [], source_available: false,
        source_message: 'Created when a job process starts.', total_pages:0});
    await logs;
    assert.match(h.nodes.get('logs-container').innerHTML, /No logs available yet/);
    assert.match(h.nodes.get('logs-container').innerHTML, /Created when a job process starts/);
    assert.doesNotMatch(h.nodes.get('logs-freshness').textContent, /Refresh failed/);
    const stats = h.run('loadStats()');
    h.reply(1, {source_available:false, log_files:[], file_size_mb:0});
    await stats;
    assert.equal(h.nodes.get('stat-total-info').textContent, '-');
    assert.doesNotMatch(h.nodes.get('stats-freshness').textContent, /Refresh failed/);
});
