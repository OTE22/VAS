const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function harness() {
    const charts = {}, timers = [], errors = [], nodes = [];
    const element = (tag, opts = {}) => {
        const node = {tag, text: opts.text, append() {}, replaceChildren() {},
            querySelector() { return null; }, addEventListener() {}};
        nodes.push(node);
        return node;
    };
    const window = {addEventListener() {}, location: {origin: 'https://example.invalid'},
        setTimeout: cb => { timers.push(cb); return timers.length; }, clearTimeout() {},
        Chart: function(ctx, config) { charts[ctx.id] = config; }};
    const document = {addEventListener() {}, querySelectorAll() { return []; },
        getElementById: id => ({getContext: () => ({id})}), createElement: element};
    const context = vm.createContext({window, document, console, AbortController,
        URL, URLSearchParams, Date, Map, Set, Intl, navigator: {},
        fetch: (_url, options) => new Promise((_resolve, reject) => {
            const abort = () => reject(Object.assign(new Error('aborted'), {name: 'AbortError'}));
            if (options.signal.aborted) abort();
            else options.signal.addEventListener('abort', abort, {once: true});
        })});
    window.element = element;
    window.error = (...args) => errors.push(args);
    let source = fs.readFileSync('frontend/js/admin-intelligence.js', 'utf8');
    source = source.replace(/\}\)\(\);\s*$/, `
        window.test = {api, renderTemporalPatterns, renderCompleteAnalysis, loadTemporalPatterns, elements, state};
        el = window.element;
        renderLoading = prependEngineBadge = () => {};
        renderError = window.error;
    })();`);
    vm.runInContext(source, context);
    window.test.elements.temporalContainer = element('div');
    window.test.elements.completeContainer = element('div');
    return {t: window.test, charts, timers, errors, nodes};
}

test('weekday chart maps the actual JSON weekday indices, Monday first', () => {
    const h = harness();
    h.t.renderTemporalPatterns({hourly_distribution: {'12': 7}, daily_distribution: {'0': 4, '6': 3}, most_common_pipelines: []});
    assert.deepEqual(Array.from(h.charts['daily-chart'].data.datasets[0].data), [4, 0, 0, 0, 0, 0, 3]);
});

for (const external of [false, true]) {
    test(`timeout reports an error with external signal=${external}, without AbortSignal.any`, async () => {
        const h = harness();
        const ctl = new AbortController();
        const pending = h.t.api('/api/test', external ? {signal: ctl.signal} : {});
        h.timers[0]();
        await assert.rejects(pending, error => error.code === 'TIMEOUT' && !error.aborted);
        assert.equal(ctl.signal.aborted, false);
    });
}

test('intentional cancellation remains silent', async () => {
    const h = harness(), ctl = new AbortController();
    const pending = h.t.api('/api/test', {signal: ctl.signal});
    ctl.abort();
    await assert.rejects(pending, error => error.aborted && error.code !== 'TIMEOUT');
});

test('timed-out temporal request replaces loading with a retry message', async () => {
    const h = harness();
    h.t.state.selectedIdentityId = 'selected';
    const pending = h.t.loadTemporalPatterns();
    h.timers[0]();
    await pending;
    assert.equal(h.errors.length, 1);
    assert.match(h.errors[0][1], /timed out.*retry/i);
});

test('disabled complete-analysis sections are labeled and cannot open details', () => {
    const h = harness();
    h.t.renderCompleteAnalysis({sections: {
        related: {status: 'disabled'}, temporal: {status: 'disabled'}, tracking: {status: 'disabled'}
    }});
    assert.equal(h.nodes.filter(n => n.text === 'Disabled in settings').length, 3);
    const buttons = h.nodes.filter(n => n.tag === 'button');
    assert.equal(buttons.length, 3);
    assert.ok(buttons.every(n => n.disabled));
});
