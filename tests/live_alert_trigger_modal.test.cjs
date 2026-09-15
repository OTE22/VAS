const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function harness() {
    const nodes = {'ack-selected-btn': {}, 'ack-all-btn': {}};
    const pending = [];
    const content = {replaceChildren() {}};
    const window = {addEventListener() {}};
    const document = {addEventListener() {}, getElementById: id => nodes[id],
        createElement: () => ({appendChild() {}, addEventListener() {}})};
    const context = vm.createContext({window, document, console, Set, Map, URLSearchParams, AbortController});
    let source = fs.readFileSync('frontend/js/admin-live-alerts.js', 'utf8');
    source = source.replace(/\}\)\(\);\s*$/, `
        window.test = {state, loadTriggersPage, updateTriggerActions, withLock};
        api = url => window.request(url);
        renderTriggersPage = (content, data) => {
            state.triggerModal.unacknowledgedTotal = data.unacknowledged_total;
            window.rendered = data.marker;
            updateTriggerActions();
        };
    })();`);
    window.request = url => new Promise(resolve => pending.push({url, resolve}));
    vm.runInContext(source, context);
    window.test.state.triggerModal = {alertId: 'a', page: 1, filter: 'all', selected: new Set(), requestId: 0, unacknowledgedTotal: 0};
    window.test.state.modalShell = {body: {querySelector: () => content}};
    return {t: window.test, pending, nodes, window};
}

test('rapid filter changes load the latest filter and ignore the earlier response', async () => {
    const h = harness();
    const first = h.t.loadTriggersPage();
    h.t.state.triggerModal.filter = 'ack';
    const second = h.t.loadTriggersPage();
    assert.match(h.pending[1].url, /acknowledged=true/);
    h.pending[1].resolve({ok: true, payload: {marker: 'new', unacknowledged_total: 0}});
    await second;
    h.pending[0].resolve({ok: true, payload: {marker: 'old', unacknowledged_total: 10}});
    await first;
    assert.equal(h.window.rendered, 'new');
    assert.equal(h.nodes['ack-all-btn'].disabled, true);
});

test('finishing bulk acknowledgement does not re-enable an empty action', async () => {
    const h = harness();
    await h.t.withLock('bulk-ack', h.nodes['ack-all-btn'], async () => {});
    assert.equal(h.nodes['ack-all-btn'].disabled, true);
    assert.equal(h.nodes['ack-selected-btn'].disabled, true);
});
