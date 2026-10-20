const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const source = fs.readFileSync('frontend/js/admin-unknown.js', 'utf8');
function harness() {
    const dom = new JSDOM('<div id="identity-detail-modal"><div id="identity-detail-content"><details open><summary>Photos</summary></details><details class="movement-association-review" open><summary>Review</summary></details></div></div>', {url:'https://vas.test', runScripts:'outside-only'});
    const w = dom.window, requests = [], opens = [], notices = [];
    w.ModalStack = {open: (...args) => opens.push(args)};
    w.fetch = () => new Promise(resolve => requests.push(resolve));
    w.showNotification = (...args) => notices.push(args);
    w.renderAdvancedTimeline = () => '<details><summary>Photos</summary></details><details class="movement-association-review"><summary>Review</summary></details>';
    w.eval(source.slice(source.indexOf('let identityDetailRevision = 0;'), source.indexOf('// Runs whenever the detail modal leaves the stack')) + '\nfunction handleDetailModalClose(){++identityDetailRevision;identityDetailController?.abort();}');
    const content = w.document.getElementById('identity-detail-content');
    content.scrollTop = 720;
    return {w, requests, opens, notices, content, close: () => w.close()};
}
const identity = {id:'example', appearances:[], type:'unknown', appearances_count:2};
test('saved-review refresh keeps details open and scroll position without reopening modal', async () => {
    const h = harness();
    try {
        const old = h.content.innerHTML;
        const pending = h.w.viewIdentityDetails('example', 1, null, false, true);
        assert.equal(h.content.innerHTML, old, 'retain gallery while refresh loads');
        h.requests[0]({ok:true, json:async()=>identity});
        await pending;
        assert.equal(h.opens.length, 0);
        assert.deepEqual([...h.content.querySelectorAll('details')].map(n=>n.open), [true,true]);
        assert.equal(h.content.scrollTop, 720);
        assert.match(h.content.textContent, /example/);
    } finally {h.close();}
});
test('failed refresh retains the review list; close during refresh cannot repopulate it', async () => {
    const h = harness();
    try {
        const old = h.content.innerHTML;
        let pending = h.w.viewIdentityDetails('example', 1, null, false, true);
        h.requests[0]({ok:false,status:500}); await pending;
        assert.equal(h.content.innerHTML, old);
        assert.match(h.notices[0][0], /Decision saved/);
        pending = h.w.viewIdentityDetails('example', 1, null, false, true);
        h.w.handleDetailModalClose();
        h.requests[1]({ok:true,json:async()=>identity}); await pending;
        assert.equal(h.content.innerHTML, old);
        assert.equal(h.opens.length, 0);
    } finally {h.close();}
});
test('saved event requests an in-place refresh of its original details', async () => {
    const h = harness();
    try {
        const calls=[];
        h.w.viewIdentityDetails=(...args)=>calls.push(args);
        h.w.document.dispatchEvent(new h.w.CustomEvent('sighting-review-saved',{detail:{sourceRoot:h.content.firstElementChild,identity_id:'example',reference_appearance_id:1}}));
        assert.equal(calls.length,1);
        assert.equal(calls[0][4],true);
    } finally {h.close();}
});
