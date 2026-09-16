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

function jobStatus(completedAt, status = 'completed') {
    return {services: [], ml_worker: {status: 'healthy', worker_state: 'idle'},
        errors: [], history_available: true,
        last_log_cleanup: completedAt ? {status, completed_at: completedAt,
            result: {deleted_records: 12, deleted_files: 0}} : null};
}
function logPage(message, page = 1, totalPages = 1) {
    return {logs: message ? [{level: 'INFO', message}] : [], page,
        total_pages: totalPages, total_count: message ? 1 : 0,
        has_previous: page > 1, has_next: false, truncated: false};
}

test('cleanup refreshes older pages and rejects a response containing deleted records', async () => {
    const h = harness();
    const baseline = h.run('loadJobs()');
    h.reply(0, jobStatus('2026-09-16T06:00:00Z')); await baseline;
    h.run("currentPage = 8; currentLevel = 'ERROR'; currentSource = 'application'; currentDateFrom = '2026-09-14'");
    const stale = h.run('loadLogs()');
    const cleanup = h.run('loadJobs()');
    h.reply(2, jobStatus('2026-09-16T12:00:00Z'));
    await new Promise(setImmediate);
    assert.equal(h.pending[1].options.signal.aborted, true);
    assert.equal(h.run('currentPage'), 1);
    assert.match(h.nodes.get('logs-container').textContent, /Refreshing retained logs/);
    assert.equal(h.nodes.get('pagination-section').style.display, 'none');
    const url = new URL(h.pending[3].url, 'https://example.test');
    assert.equal(url.searchParams.get('page'), '1');
    assert.equal(url.searchParams.get('level'), 'ERROR');
    assert.equal(url.searchParams.get('date_from'), '2026-09-14');
    h.reply(3, logPage('retained record'));
    h.reply(4, {total_info: 1});
    await cleanup;
    h.reply(1, logPage('deleted record', 8)); await stale;
    assert.match(h.nodes.get('logs-container').innerHTML, /retained record/);
    assert.doesNotMatch(h.nodes.get('logs-container').innerHTML, /deleted record/);
    assert.match(h.nodes.get('pagination-info').textContent, /Page 1 of 1/);
    assert.equal(h.nodes.get('stat-total-info').textContent, 1);
    const unchanged = h.run('loadJobs()');
    h.reply(5, jobStatus('2026-09-16T12:00:00Z')); await unchanged;
    assert.equal(h.pending.length, 6); // no repeated invalidation
});

test('first completed cleanup refreshes even when the job partially failed', async () => {
    const h = harness();
    const baseline = h.run('loadJobs()');
    h.reply(0, jobStatus(null)); await baseline;
    h.run('currentPage = 4');
    const cleanup = h.run('loadJobs()');
    h.reply(1, jobStatus('2026-09-16T12:00:00Z', 'failed'));
    await new Promise(setImmediate);
    h.reply(2, logPage('remaining'));
    h.reply(3, {});
    await cleanup;
    assert.equal(h.run('currentPage'), 1);
    assert.match(h.nodes.get('logs-container').innerHTML, /remaining/);
});

test('pagination recovers when cleanup removes the requested last page', async () => {
    const h = harness();
    h.run('currentPage = 9');
    const logs = h.run('loadLogs()');
    h.reply(0, logPage(null, 9, 2));
    await new Promise(setImmediate);
    assert.equal(new URL(h.pending[1].url, 'https://example.test').searchParams.get('page'), '2');
    h.reply(1, logPage('last remaining page', 2, 2));
    await logs;
    assert.equal(h.run('currentPage'), 2);
    assert.match(h.nodes.get('logs-container').innerHTML, /last remaining page/);
});
