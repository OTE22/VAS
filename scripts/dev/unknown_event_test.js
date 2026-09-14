// Execute the actual event reducer with a stub display, without touching app data.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('frontend/js/admin-unknown.js', 'utf8');
const body = source.slice(source.indexOf('function applyUnknownEvent('),
    source.indexOf('// Helper function to find identity in current pipeline groups'));
const cards = new Map(), updates = [];
const context = {
    window: { userPipelines: null }, currentFilters: {},
    findIdentityInPipelineGroups: (id,pid) => cards.get(`${id}:${pid}`),
    recordPipelineDisplayName() {},
    addUnknownFaceToPipeline(pid, identity) { cards.set(`${identity.id}:${pid}`, identity); updates.push(`${identity.id}:${pid}`); }
};
vm.createContext(context); vm.runInContext(body, context);
function emit(id, camera, time, n) {
    context.applyUnknownEvent({ identity_id:id, pipeline_id:camera, event_id:`event-${n}`,
        appearance_id:n, timestamp:`2026-09-13T${time}Z`, appearances_count:n,
        snapshot_url:`/storage/event-${n}.jpg` });
}
emit('a','01','10:15:32',1);
emit('a','04','10:21:07',2);
emit('b','01','10:21:08',3);
const b = cards.get('b:01'), camera4 = cards.get('a:04');
emit('a','01','10:45:18',4);
assert.equal(cards.get('b:01'), b);
assert.equal(cards.get('a:04'), camera4);
assert.equal(cards.get('a:01').last_seen_at, '2026-09-13T10:45:18Z');
const count = updates.length;
emit('a','01','10:45:18',4); // replay
emit('a','01','10:17:00',5); // delayed delivery
assert.equal(updates.length, count);
context.window.userPipelines = ['01'];
emit('c','04','11:00:00',6);
assert(!cards.has('c:04'));
assert(source.includes('data-pipeline-id="${CSS.escape(updatedIdentity.pipeline_id'));
console.log('PASS: identity/camera targeting, independent people, duplicate and late-event guards');
