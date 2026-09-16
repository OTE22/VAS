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
        const button = descendants(promote).find(node => node.tag === 'button');
        assert.equal(button.dataset.action, 'mergeIntoKnownCandidate');
        assert.equal(button.dataset.arg, candidate.identity_id);
        assert.equal(button.type, 'button');
        let mergeArgs;
        h.context.mergeIntoKnownCandidate = (...args) => { mergeArgs = args; };
        h.handlers[button.dataset.action](button);
        assert.deepEqual(mergeArgs, [candidate.identity_id, candidate.display_name]);

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

test('promotion candidate confirms and sends the unknown into the selected known identity', async () => {
    const h = harness(['admin-unknown.js', 'upload-modal.js']);
    h.nodes.set('promote-modal', h.element());
    h.run("currentIdentityId = 'unknown-456'");
    let confirmed = false, requests = [], closed = 0, refreshed = 0;
    h.context.AppConfirm = {confirm: async () => confirmed};
    h.context.postMergeWithRiskGate = async (url, payload) => { requests.push({url, payload}); return {message:'Merged'}; };
    h.context.ModalStack = {close() { closed++; }};
    h.context.showNotification = () => {};
    h.context.loadUnknownFaces = () => { refreshed++; };
    await h.run("mergeIntoKnownCandidate('known-123', 'Existing person')");
    assert.equal(requests.length, 0, 'cancel must not merge');
    confirmed = true;
    await h.run("mergeIntoKnownCandidate('known-123', 'Existing person')");
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/api/admin/identities/merge');
    assert.equal(requests[0].payload.from_identity_id, 'unknown-456');
    assert.equal(requests[0].payload.to_identity_id, 'known-123');
    assert.equal(requests[0].payload.decision, 'merge_existing');
    assert.equal(closed, 1);
    assert.equal(refreshed, 1);
    assert.equal(h.run('mergeSubmitInFlight'), false);
});
