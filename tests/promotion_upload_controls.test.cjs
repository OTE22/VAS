const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function harness(order) {
    const handlers = {}, nodes = new Map();
    function element(tag = 'div') {
        return {tag, children: [], dataset: {}, style: {}, value: '', textContent: '',
            append(...items) { this.children.push(...items); },
            appendChild(item) { this.children.push(item); return item; },
            setAttribute(name, value) {
                if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
                else this[name] = value;
            },
            focus() { this.focused = true; },
            replaceChildren(...items) { this.children = items; },
            addEventListener() {}, querySelectorAll() { return []; }
        };
    }
    const context = vm.createContext({console, URLSearchParams, AbortController,
        setTimeout, clearTimeout, setInterval, clearInterval,
        document: {readyState: 'loading', addEventListener() {},
            createElement: element, getElementById: id => nodes.get(id) || null,
            querySelector: () => null, querySelectorAll: () => []},
        Actions: {register(map) { Object.assign(handlers, map); }},
        addEventListener() {},
    });
    context.window = context;
    for (const file of order) vm.runInContext(fs.readFileSync('frontend/js/' + file, 'utf8'), context, {filename:file});
    return {context, handlers, nodes, element, run: code => vm.runInContext(code, context)};
}
function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }
const candidate = {identity_id: 'known-123', display_name: 'Existing person', similarity: .9};

for (const order of [['admin-unknown.js', 'upload-modal.js'], ['upload-modal.js', 'admin-unknown.js']]) {
    test('candidate buttons keep the correct action when loaded: ' + order.join(', '), async () => {
        const h = harness(order);
        h.context.candidate = candidate;
        const promote = h.run('buildPromoteCandidateRow(candidate)');
        const link = descendants(promote).find(node => node.tag === 'a');
        assert.equal(link.href, '/admin/identity/' + candidate.identity_id + '?from=/admin/unknown');
        assert.equal(descendants(promote).some(node => node.tag === 'button'), false,
            'promotion candidates must not offer a whole-group merge');

        const upload = h.run('buildCandidateRow(candidate, true, false)');
        const add = descendants(upload).find(node => node.tag === 'button');
        assert.equal(add.textContent, 'Add to this person');
        assert.equal(add.dataset.action, 'enrollmentAddToExisting');
        let payload;
        h.context.submitDecision = data => { payload = data; };
        h.run("pendingDecision = {token:'test-upload'}");
        h.handlers[add.dataset.action](add);
        assert.equal(payload.action, 'add_to_existing');
        assert.equal(payload.identity_id, candidate.identity_id);
        assert.equal(payload.upload_token, 'test-upload');

        const duplicate = h.run('buildCandidateRow(candidate, false, true)');
        const disabled = descendants(duplicate).find(node => node.tag === 'button');
        assert.equal(disabled.disabled, true);
        assert.equal(disabled.dataset.action, undefined);
        assert.equal(disabled.textContent, 'Already stored');
    });
}

test('none-of-these switches to the new-person form without a mutation', () => {
    const h = harness(['admin-unknown.js', 'upload-modal.js']);
    for (const id of ['promote-candidates-section','promote-none-of-these','promote-name']) h.nodes.set(id, h.element());
    h.context.fetch = () => { throw new Error('unexpected request'); };
    h.run('chooseNoneOfThese()');
    assert.equal(h.nodes.get('promote-candidates-section').style.display, 'none');
    assert.equal(h.nodes.get('promote-name').focused, true);
});

test('upload create and cancel controls act on the pending upload only', () => {
    const h = harness(['admin-unknown.js', 'upload-modal.js']);
    const name = h.element(); name.value = ' New person '; h.nodes.set('enrollmentNewName', name);
    let payload, cancelled, closed = false;
    h.context.submitDecision = data => { payload = data; };
    h.context.cancelPendingUpload = token => { cancelled = token; };
    h.context.closeUploadModal = () => { closed = true; };
    h.run("pendingDecision = {token:'upload-token'}");
    h.handlers.enrollmentCreateNew();
    assert.equal(payload.display_name, 'New person');
    assert.equal(payload.action, 'create_new');
    h.handlers.enrollmentCancel();
    assert.equal(cancelled, 'upload-token');
    assert.equal(closed, true);
    assert.equal(h.run('pendingDecision'), null);
});

for (const flow of ['upload', 'review']) {
    test(`late ${flow} save refreshes data without changing a reopened dialog`, async () => {
        const h = harness(['upload-modal.js']);
        for (const id of ['globalPersonName', 'globalFileInput', 'globalUploadSubmitBtn', 'uploadSuccessMessage', 'uploadSuccessText']) h.nodes.set(id, h.element());
        h.nodes.get('globalPersonName').value = 'Person A';
        h.nodes.get('globalFileInput').files = [{name: 'photo.jpg'}];
        h.context.FormData = class { append() {} };
        h.context.CustomEvent = class { constructor(type) { this.type = type; } };
        const events = [], timers = [];
        h.context.dispatchEvent = event => events.push(event.type);
        h.context.setTimeout = cb => { timers.push(cb); return timers.length; };
        let respond;
        h.context.fetch = () => new Promise(resolve => { respond = resolve; });
        if (flow === 'upload') h.context.handleGlobalUpload({preventDefault() {}});
        else h.context.submitDecision({upload_token: 'old-ticket'});
        h.context.closeUploadModal();
        h.nodes.get('globalPersonName').value = 'Person B';
        h.nodes.get('globalUploadSubmitBtn').disabled = true;
        respond({ok: true, status: 200, json: async () => ({success: true, image_created: true, message: 'Saved A'})});
        await new Promise(resolve => setImmediate(resolve));
        assert.deepEqual(events, ['enrollment:saved']);
        assert.equal(h.nodes.get('globalPersonName').value, 'Person B');
        assert.equal(h.nodes.get('uploadSuccessText').textContent, '');
        assert.equal(h.nodes.get('globalUploadSubmitBtn').disabled, true);
        assert.equal(timers.length, 0);
    });
}

test('late review prompt releases its pending file after the dialog closes', async () => {
    const h = harness(['upload-modal.js']);
    for (const id of ['globalPersonName', 'globalFileInput']) h.nodes.set(id, h.element());
    h.nodes.get('globalPersonName').value = 'Person A';
    h.nodes.get('globalFileInput').files = [{name: 'photo.jpg'}];
    h.context.FormData = class { append() {} };
    let respond, cancelled;
    h.context.fetch = () => new Promise(resolve => { respond = resolve; });
    h.context.cancelPendingUpload = token => { cancelled = token; };
    h.context.handleGlobalUpload({preventDefault() {}});
    h.context.closeUploadModal();
    respond({ok: true, status: 202, json: async () => ({decision_required: true, upload_token: 'old-ticket'})});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(cancelled, 'old-ticket');
});

for (const flow of ['upload', 'review']) {
    test(`${flow} displays pending-index warning and keeps the saved dialog open`, async () => {
        const h = harness(['upload-modal.js']);
        for (const id of ['globalPersonName', 'globalFileInput', 'uploadSuccessMessage', 'uploadSuccessText']) h.nodes.set(id, h.element());
        h.nodes.get('globalPersonName').value = 'Person A';
        h.nodes.get('globalFileInput').files = [{name: 'photo.jpg'}];
        h.context.FormData = class { append() {} };
        h.context.CustomEvent = class {};
        h.context.dispatchEvent = () => {};
        h.context.hideEnrollmentDecision = () => {};
        const alerts = [], timers = [];
        h.context.showUploadAlert = (...args) => alerts.push(args);
        h.context.setTimeout = cb => { timers.push(cb); return timers.length; };
        h.context.fetch = async () => ({ok: true, status: 200, json: async () => ({success: true, image_created: true, message: 'Saved', warnings: ['vector_index_sync_pending']})});
        if (flow === 'upload') h.context.handleGlobalUpload({preventDefault() {}});
        else await h.context.submitDecision({upload_token: 'ticket'});
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(alerts.length, 1);
        assert.match(alerts[0][1], /photo is saved.*indexing is pending/i);
        assert.equal(timers.length, 0);
    });
}
