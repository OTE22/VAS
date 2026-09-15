const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function harness() {
    const nodes = {};
    for (const id of ['min-co-app', 'time-window', 'refresh-related-btn']) {
        nodes[id] = {value: '', dataset: {}, valid: true, handlers: {},
            reportValidity() { return this.valid; },
            addEventListener(event, fn) { this.handlers[event] = fn; }};
    }
    const requests = [];
    const window = {addEventListener() {}};
    const document = {addEventListener() {}, getElementById: id => nodes[id] || null,
        querySelectorAll: () => []};
    const context = vm.createContext({window, document, console, AbortController, URLSearchParams,
        URL, Date, Map, Set, Intl, setTimeout, clearTimeout, navigator: {}});
    let source = fs.readFileSync('frontend/js/admin-intelligence.js', 'utf8');
    source = source.replace(/\}\)\(\);\s*$/, `
        window.test = {state, elements, setupEventListeners, loadRelatedIdentities};
        api = async (url, options) => window.request(url, options);
        renderLoading = renderRelatedIdentities = prependEngineBadge = () => {};
        renderError = () => { throw new Error('unexpected rendering error'); };
    })();`);
    window.request = async (url, options) => { requests.push({url, options}); return {items: []}; };
    vm.runInContext(source, context);
    const t = window.test;
    t.state.selectedIdentityId = 'selected-id';
    t.elements.minCoApp = nodes['min-co-app'];
    t.elements.timeWindow = nodes['time-window'];
    t.setupEventListeners();
    return {t, nodes, requests};
}

test('changing either filter reloads with the selected values', async () => {
    const h = harness();
    h.nodes['min-co-app'].value = '12'; h.nodes['time-window'].value = '4';
    await h.nodes['min-co-app'].handlers.change();
    assert.equal(h.requests[0].options.params.min_co_appearances, 12);
    assert.equal(h.requests[0].options.params.time_window_minutes, 4);
    h.nodes['time-window'].value = '9';
    await h.nodes['time-window'].handlers.change();
    assert.equal(h.requests[1].options.params.time_window_minutes, 9);
});

test('Enter listener coexists with change; invalid values do not submit', async () => {
    const h = harness();
    assert.equal(typeof h.nodes['time-window'].handlers.keydown, 'function');
    h.nodes['time-window'].handlers.keydown({key: 'Enter', preventDefault() {}});
    assert.equal(h.requests.length, 1);
    h.nodes['time-window'].valid = false;
    await h.t.loadRelatedIdentities();
    assert.equal(h.requests.length, 1);
});
