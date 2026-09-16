// Exercise the actual submission function with mocked DOM/network; no API writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/js/admin-unknown.js', 'utf8');
const action = source.slice(source.indexOf('async function promoteIdentity() {'), source.indexOf('// Show the face detection alert.'));

async function scenario(proceed) {
    let releaseReview;
    const requests = [], notices = [], errors = [];
    const nodes = new Map();
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, {value: id === 'promote-name' ? 'Test person' : '', style: {}, replaceChildren() {}});
        return nodes.get(id);
    };
    const context = {
        document: {getElementById: node, querySelector: node},
        buildPromoteCandidateRow: candidate => candidate,
        AppConfirm: {confirm: () => new Promise(resolve => { releaseReview = resolve; })},
        ModalStack: {close() {}},
        showNotification: text => notices.push(text),
        showFaceDetectionAlert: text => errors.push(text), loadUnknownFaces() {},
        fetch: async (url, options) => {
            requests.push({url, payload: JSON.parse(options.body)});
            return requests.length === 1
                ? {ok: false, status: 409, json: async () => ({code:'PROMOTION_REVIEW_REQUIRED', review:{candidates:[{identity_id:'known', display_name:'Existing person'}]}})}
                : {ok: true, status: 200, json: async () => ({success:true, message:'Promoted'})};
        }
    };
    vm.createContext(context);
    vm.runInContext(`let currentIdentityId='unknown'; let mergeSubmitInFlight=false; let unknownLoadSequence=0;
        const mergedAwayIdentities=new Set(); ${action}
        globalThis.submit=promoteIdentity; globalThis.busy=()=>mergeSubmitInFlight;`, context);
    const first = context.submit();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(context.busy(), true);
    assert.equal(node('#promote-form button[type="submit"]').disabled, true);
    await context.submit();
    assert.equal(requests.length, 1, 'double submit is ignored while review is open');
    releaseReview(proceed);
    await first;
    assert.equal(requests.length, proceed ? 2 : 1);
    assert.equal(requests[0].payload.confirm_create_new, undefined);
    if (proceed) assert.equal(requests[1].payload.confirm_create_new, true);
    assert.equal(context.busy(), false);
    assert.equal(node('#promote-form button[type="submit"]').disabled, false);
    assert.deepEqual(errors, []);
    assert.equal(notices.length, proceed ? 1 : 0);
}
(async () => {
    await scenario(false); await scenario(true);
    console.log('PASS promotion: cancel/confirm review, bounded resend, duplicate-submit guard, button recovery. No API writes.');
})().catch(error => { console.error(error); process.exitCode=1; });
