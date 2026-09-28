const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const context = {window:{}, location:{origin:'https://vas.local'}, URL, document:{addEventListener(){}}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../frontend/js/movement-timeline.js'),'utf8'),context);
const render = rows => context.window.MovementTimeline.render(rows, id => id);
test('orders dates across midnight and keeps simultaneous sightings separate',()=>{
 const html=render([{start_time:'2026-09-28T01:00:00Z',pipeline_id:'later'}, {start_time:'2026-09-27T23:59:00Z',pipeline_id:'earlier'}, {start_time:'2026-09-28T01:00:00Z',pipeline_id:'same-time'}]);
 assert.ok(html.indexOf('<h5>earlier</h5>')<html.indexOf('<h5>later</h5>'));
 assert.equal((html.match(/class="movement-card"/g)||[]).length,3);
 assert.ok(html.includes('1h 1m since previous sighting'));
 assert.ok(!html.includes('transform: scale'));
});
test('escapes metadata, blocks external and script images, accepts numeric track zero',()=>{
 const html=render([{start_time:'bad',pipeline_id:'<script>bad</script>',track_id:0,snapshot_url:'javascript:alert(1)'},{start_time:'bad',snapshot_url:'https://foreign.test/image.jpg'}]);
 assert.ok(!html.includes('<script>'));
 assert.ok(!html.includes('<img'));
 assert.ok(html.includes('<dd>0</dd>'));
 assert.ok(html.includes('Time unavailable'));
});
test('single-camera sightings do not claim camera movement',()=>{
 const html=render([0,1].map(i=>({start_time:`2026-09-28T01:0${i}:00Z`,pipeline_id:'one',snapshot_url:'/storage/a.jpg'})));
 assert.ok(html.includes('Same camera'));
 assert.ok(!html.includes('is-camera-change'));
 assert.ok(html.includes('src="/storage/a.jpg"'));
 assert.ok(render([]).includes('No recorded appearances'));
});
test('graph puts return visits on the same camera lane and retains every sighting',()=>{
 const html=render(['gate','lobby','gate','gate'].map((pipeline_id,i)=>({pipeline_id,start_time:`2026-09-28T01:0${i}:00Z`})))
 assert.equal((html.match(/class="graph-camera"/g)||[]).length,2);
 assert.equal((html.match(/data-graph-select="/g)||[]).length,4);
 assert.equal((html.match(/data-graph-edge="/g)||[]).length,3);
 assert.ok(html.includes('2 camera changes'));
 assert.ok(html.includes('graph-lane-labels'));
 assert.ok(html.includes('not a physical route'));
});
test('overview groups nearby sightings but splits long gaps and camera returns',()=>{
 const html=render([
 {pipeline_id:'gate',start_time:'2026-09-28T10:00:00Z'},
 {pipeline_id:'gate',start_time:'2026-09-28T10:02:00Z'},
 {pipeline_id:'gate',start_time:'2026-09-28T10:09:00Z'},
 {pipeline_id:'lobby',start_time:'2026-09-28T10:10:00Z'},
 {pipeline_id:'gate',start_time:'2026-09-28T10:11:00Z'}]);
 assert.equal((html.match(/class="journey-visit-block"/g)||[]).length,4);
 assert.ok(html.includes('data-visit-start="0" data-visit-end="1"'));
 assert.ok(html.includes('7m 0s since previous sighting'));
 assert.ok(html.includes('RETURN TO CAMERA'));
 assert.ok(html.includes('does not confirm continuous presence'));
});
test('unknown camera or missing time never merges separate records into a visit',()=>{
 const html=render([{start_time:'2026-09-28T10:00:00Z'},{start_time:'2026-09-28T10:01:00Z'},
 {pipeline_id:'gate',start_time:'bad'},{pipeline_id:'gate',start_time:'bad'}]);
 assert.equal((html.match(/class="journey-visit-block"/g)||[]).length,4);
});
