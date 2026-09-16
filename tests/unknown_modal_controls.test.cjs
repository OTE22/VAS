const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/js/admin-unknown.js', 'utf8');
const html = fs.readFileSync('frontend/admin/unknown.html', 'utf8');
function harness() {
    const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], {
        id: m[1], listeners: {}, addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    }]));
    const closed = [];
    const context = vm.createContext({
        document: {getElementById: id => nodes.get(id) || null},
        ModalStack: {close: el => closed.push(el.id)},
        cancelQuickSearch() {}, chooseNoneOfThese() {}, closeFaceDetectionAlert() {}, toggleMultiSelectMode() {}
    });
    const start = source.indexOf('function setupEventListeners()');
    const end = source.indexOf('// Load unknown faces', start);
    vm.runInContext(source.slice(start, end), context);
    return {nodes, closed, context};
}
test('all Unknown Faces close/cancel controls bind without a legacy logout button', () => {
    const h = harness();
    assert.equal(h.nodes.has('logout-btn'), false);
    vm.runInContext('setupEventListeners()', h.context);
    const pairs = {
        'close-search-modal':'search-image-modal', 'cancel-search-btn':'search-image-modal',
        'close-promote-modal':'promote-modal', 'cancel-promote-btn':'promote-modal',
        'close-detail-modal':'identity-detail-modal', 'close-suggestions-modal':'merge-suggestions-modal',
        'close-pipeline-suggestions-modal':'pipeline-merge-suggestions-modal',
        'close-live-alert-modal':'create-live-alert-modal', 'cancel-live-alert-btn':'create-live-alert-modal',
        'close-watchlist-modal':'add-to-watchlist-modal', 'cancel-watchlist-btn':'add-to-watchlist-modal',
        'close-merge-modal':'merge-modal', 'cancel-merge-btn':'merge-modal'
    };
    for (const [button, modal] of Object.entries(pairs)) {
        const listeners = h.nodes.get(button).listeners.click;
        assert.equal(listeners?.length, 1, button);
        listeners[0]();
        assert.equal(h.closed.at(-1), modal, button);
    }
});
test('modal controls are bound while the initial user request is still pending', () => {
    const h = harness();
    let init;
    h.context.document.addEventListener = (_, fn) => { init = fn; };
    h.context.loadUserInfo = () => new Promise(() => {});
    const start = source.indexOf("document.addEventListener('DOMContentLoaded', async () => {");
    const end = source.indexOf('\n});', start) + 4;
    vm.runInContext(source.slice(start, end), h.context);
    init();
    assert.equal(h.nodes.get('cancel-promote-btn').listeners.click?.length, 1);
});
