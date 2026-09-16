const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.classList = {add(){}, remove(){}}; }
    setAttribute() {}
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    addEventListener(name, fn) { this.events[name] = fn; }
    remove() { this.removed = true; }
}
const flush = () => new Promise(resolve => setImmediate(resolve));
function harness() {
    const requests = [], sounds = [];
    const window = {};
    const context = vm.createContext({window, document: {createElement: tag => new Element(tag), addEventListener(){}, removeEventListener(){}},
        fetch: (url, options) => new Promise(resolve => requests.push({url, options, resolve})),
        setTimeout(){return 1;}, clearTimeout(){}, AbortController, console});
    vm.runInContext(fs.readFileSync('frontend/js/detection-alert-inbox.js', 'utf8'), context);
    const inbox = new window.DetectionAlertInbox(new Element('section'), level => sounds.push(level));
    return {inbox, requests, sounds};
}
function item(id, level='warning') {
    return {source:'live', first_id:id, latest_id:id, alert_level:level, identity_name:'<img onerror=bad>',
        rule_name:'Entrance', sightings:1, first_seen_at:'2026-09-16T10:00:00Z', last_seen_at:'2026-09-16T10:00:00Z'};
}
function reply(request, items=[], sounds=[], total=items.length, ok=true) {
    request.resolve({ok, json:async () => ({items,total,sound_candidates:sounds,observed_at:'2026-09-16T10:00:00Z',detail:'Please retry'})});
}
test('initial history is silent; subsequent batches play highest severity once and repeats stay silent', async () => {
    const h = harness(); reply(h.requests[0], [item('old')]); await flush();
    assert.deepEqual(h.sounds, []);
    h.inbox.refresh(); reply(h.requests[1], [], [item('new','info'),item('critical','critical')]); await flush();
    assert.deepEqual(h.sounds, ['critical']);
    h.inbox.refresh(); reply(h.requests[2], [], [item('new','info'),item('critical','critical')]); await flush();
    assert.deepEqual(h.sounds, ['critical']);
    assert.match(h.requests[1].url, /sounds_since=/);
});
test('new groups sound on later pages and history appearing on page one stays silent', async () => {
    const h = harness(); reply(h.requests[0], [item('old')], [], 100); await flush();
    h.inbox.offset = 50; h.inbox.refresh(); reply(h.requests[1], [item('old2')], [item('new','info')],100); await flush();
    assert.deepEqual(h.sounds, ['info']);
    h.inbox.offset = 0; h.inbox.refresh(); reply(h.requests[2], [item('old3')], [],100); await flush();
    assert.deepEqual(h.sounds, ['info']);
});
test('failed refresh keeps confirmed cards and exposes recovery message', async () => {
    const h = harness(); reply(h.requests[0], [item('old')]); await flush();
    const card = h.inbox.list.children[0];
    h.inbox.refresh(); reply(h.requests[1], [], [],0,false); await flush();
    assert.equal(h.inbox.list.children[0], card);
    assert.match(h.inbox.status.textContent, /out of date/);
    assert.equal(h.inbox.refreshButton.disabled, undefined);
});
test('acknowledgement sends source, displayed boundary and CSRF header then refreshes', async () => {
    const h = harness(); reply(h.requests[0], [item('first')]); await flush();
    const card = h.inbox.list.children[0];
    const actions = card.children.find(n => n.className === 'watchlist-inbox-controls');
    const button = actions.children.find(n => n.tag === 'button');
    const ack = button.events.click();
    assert.equal(h.requests[1].options.method, 'POST');
    assert.equal(h.requests[1].options.headers['X-Requested-With'], 'XMLHttpRequest');
    assert.deepEqual(JSON.parse(h.requests[1].options.body), {source:'live', latest_id:'first'});
    reply(h.requests[1]); await flush(); reply(h.requests[2]); await ack;
    assert.equal(card.removed, true);
});
test('a response started before acknowledgement cannot restore acknowledged cards', async () => {
    const h = harness(); reply(h.requests[0], [item('first')]); await flush();
    h.inbox.refresh();
    const card = h.inbox.list.children[0];
    const actions = card.children.find(n => n.className === 'watchlist-inbox-controls');
    const ack = actions.children[0].events.click();
    reply(h.requests[2]); await ack;
    reply(h.requests[1], [item('first')]); await flush();
    assert.equal(card.removed, true);
    assert.equal(h.requests.length, 4);
    reply(h.requests[3]); await flush();
    assert.equal(h.inbox.list.children.length, 0);
});
test('invalid snapshot URLs are omitted and names are rendered as text', async () => {
    const h = harness(); reply(h.requests[0], [{...item('x'),snapshot_url:'javascript:alert(1)'}]); await flush();
    const card = h.inbox.list.children[0];
    assert.equal(card.children.some(n => n.tag === 'img'), false);
    assert.match(card.children[0].textContent, /<img onerror=bad>/);
});
test('stopping prevents in-flight responses from updating the page', async () => {
    const h = harness(); h.inbox.stop(); reply(h.requests[0], [item('x')]); await flush();
    assert.equal(h.inbox.list.children.length, 0);
    assert.deepEqual(h.sounds, []);
});
