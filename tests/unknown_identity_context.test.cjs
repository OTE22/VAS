const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const source = fs.readFileSync('frontend/js/admin-unknown.js','utf8');
function harness() {
    const dom=new JSDOM('<div id="identity-detail-modal"><div id="identity-detail-content"></div></div>', {url:'https://vas.test/admin/unknown',runScripts:'outside-only'});
    const w=dom.window, pending=[], opens=[];
    w.ModalStack={open:(el,opts)=>opens.push({el,opts})};
    w.getPipelineDisplayName=id=>id;
    w.showNotification=()=>{};
    w.renderAdvancedTimeline=rows=>w.MovementTimeline.render(rows,id=>id);
    w.fetch=(url,opts)=>new Promise(resolve=>pending.push({url,opts,resolve}));
    w.eval(fs.readFileSync('frontend/js/movement-timeline.js','utf8'));
    w.eval(source.slice(source.indexOf('// Only the most recent click'),source.indexOf('// Promote identity modal')));
    return {w,pending,opens,dom};
}
function identity(id, selected=1) {
 return {id,type:'unknown',appearances_count:2,pipeline_ids:['camera'],first_seen_at:'2026-01-01T10:00:00Z',last_seen_at:'2026-01-01T10:01:00Z',snapshot_url:'/storage/old-portrait.jpg',
 association_review:{reference_appearance_id:selected,reference_kind:'selected_sighting',included_count:1,review_count:1},
 appearances:[1,2].map(n=>({id:n,identity_id:id,pipeline_id:'camera',start_time:`2026-01-01T10:0${n}:00Z`,snapshot_url:`/storage/sighting-${n}.jpg`,association_status:n===selected?'selected':'review_required'}))};
}
const respond=(request,data)=>request.resolve({ok:true,json:async()=>data});
test('newer click wins even when aborted fetch returns late; portrait is the clicked sighting',async()=>{
 const h=harness();
 try {
  const a=h.w.viewIdentityDetails('first',1),b=h.w.viewIdentityDetails('second',2);
  assert.equal(h.pending[0].opts.signal.aborted,true);
  assert.equal(h.pending[1].url,'/api/admin/identity/second?appearance_id=2');
  respond(h.pending[1],identity('second',2)); await b;
  respond(h.pending[0],identity('first',1)); await a;
  assert.equal(h.w.document.getElementById('identity-id-display').textContent,'second');
  assert.equal(h.w.document.querySelector('.detail-image img').getAttribute('src'),'/storage/sighting-2.jpg');
  assert.equal(h.w.document.querySelectorAll('[data-graph-select]').length,1);
  assert.equal(h.w.document.querySelectorAll('.movement-association-review .movement-photo').length,1);
 } finally {h.dom.window.close();}
});
test('closing a pending detail request prevents it from reopening or publishing',async()=>{
 const h=harness();
 try {
  const request=h.w.viewIdentityDetails('first',1);
  h.opens[0].opts.onClose();
  respond(h.pending[0],identity('first')); await request;
  assert.equal(h.pending[0].opts.signal.aborted,true);
  assert.equal(h.w.document.querySelector('#identity-id-display'),null);
  assert.equal(h.opens.length,1);
 } finally {h.dom.window.close();}
});
test('review photos enlarge without entering the journey; selected sighting opens first',async()=>{
 const h=harness();
 try {
  const rows=[1,2,3].map(n=>({id:n,identity_id:'synthetic',pipeline_id:'camera',start_time:`2026-01-01T10:0${n}:00Z`,snapshot_url:`/storage/${n}.jpg`,association_status:n===3?'review_required':n===2?'selected':'consistent'}));
  h.w.document.getElementById('identity-detail-content').innerHTML=h.w.MovementTimeline.render(rows,id=>id);
  await new Promise(resolve=>h.w.setTimeout(resolve,0));
  assert.equal(h.w.document.querySelector('.graph-photo img').getAttribute('src'),'/storage/2.jpg');
  h.w.document.querySelector('.movement-association-review .movement-photo').click();
  assert.equal(h.w.document.querySelector('.movement-viewer img').getAttribute('src'),'/storage/3.jpg');
  assert.equal(h.w.document.querySelectorAll('[data-graph-select]').length,2);
  assert.match(h.w.document.querySelector('.movement-viewer-status').textContent,/Review required/);
 } finally {h.dom.window.close();}
});
test('live card updates pass the currently displayed appearance, not the initial closure',()=>{
 const h=harness();
 try {
  const w=h.w;
  w.eval('var multiSelectMode=false; var selectedIdentities=new Set();');
  w.FaceImage={update(){}};w.refreshUnknownImageVisibility=()=>{};
  w.CSS={escape:s=>s};
  const calls=[];w.viewIdentityDetails=(...args)=>calls.push(args);
  w.eval(source.slice(source.indexOf('function createIdentityCard('),source.indexOf('// A `<input type="date">')));
  w.eval(source.slice(source.indexOf('function updateIdentityCard('),source.indexOf('// Update a single pipeline group')));
  const card=w.createIdentityCard({id:'synthetic',pipeline_id:'camera',appearance_id:10});w.document.body.append(card);
  card.click();assert.deepEqual(calls[0],['synthetic','10']);
  w.updateIdentityCard('synthetic',{pipeline_id:'camera',appearance_id:20,event_id:'new',last_seen_at:'2026-01-01'});
  card.click();assert.deepEqual(calls[1],['synthetic','20']);
  assert.match(source,/el\.closest\('\[data-identity-id\]'\)\?\.dataset.appearanceId/);
 } finally {h.dom.window.close();}
});
test('review context action prevents link navigation and passes the selected appearance',()=>{
 const h=harness();
 try {
  h.w.eval(fs.readFileSync('frontend/js/actions.js','utf8'));
  const start=source.indexOf('    viewIdentityDetails: (el, event) => {');
  const end=source.indexOf('    promoteIdentityModal:',start);
  h.w.eval('Actions.register({'+source.slice(start,end)+'});');
  const link=h.w.document.createElement('a');
  link.href='/admin/unknown?view=synthetic&appearance_id=7';
  link.dataset.action='viewIdentityDetails';link.dataset.arg='synthetic';link.dataset.appearanceId='7';
  h.w.document.body.append(link);
  const click=new h.w.MouseEvent('click',{bubbles:true,cancelable:true});link.dispatchEvent(click);
  assert.equal(click.defaultPrevented,true);
  assert.equal(h.pending[0].url,'/api/admin/identity/synthetic?appearance_id=7');
 } finally {h.dom.window.close();}
});
