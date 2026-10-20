const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
function harness(){
 const dom=new JSDOM('<main id="root"></main>',{url:'https://vas.test/admin/unknown',runScripts:'outside-only'});
 const w=dom.window, requests=[], saved=[], closes=[], observers=[];
 const Observer=w.MutationObserver;
 w.MutationObserver=class extends Observer {constructor(callback){super(callback);observers.push(this)}};
 w.ModalStack={open(modal,options){modal.style.display='flex';this.current={modal,options}},close(modal){modal.style.display='none';this.current?.options.onClose?.();closes.push(modal)}};
 w.fetch=(url,options={})=>new Promise(resolve=>requests.push({url:String(url),options,resolve}));
 w.eval(fs.readFileSync('frontend/js/movement-timeline.js','utf8'));
 w.eval(fs.readFileSync('frontend/js/sighting-review.js','utf8'));
 w.document.addEventListener('sighting-review-saved',e=>saved.push(e.detail));
 const rows=[{id:1,identity_id:'synthetic',pipeline_id:'camera',start_time:'2026-01-01T10:00:00Z',snapshot_url:'/storage/reference.jpg',association_status:'selected'},
 {id:2,identity_id:'synthetic',pipeline_id:'camera',start_time:'2026-01-01T10:01:00Z',snapshot_url:'/storage/review.jpg',association_status:'review_required'}];
 const context={identity_id:'synthetic',reference_appearance_id:1,can_review:true};
 w.document.getElementById('root').innerHTML=w.MovementTimeline.render(rows,id=>id,context);
 return {dom,w,requests,saved,closes,rows,context,close:()=>{observers.forEach(o=>o.disconnect());dom.window.close()}};
}
const preview=(extra={})=>({preview_token:'synthetic-preview-token',can_decide:true,can_undo:false,
 reference:{id:1,snapshot_url:'/storage/reference.jpg',camera_name:'Synthetic Lobby',seen_at:'2026-01-01T10:00:00Z'},
 sighting:{id:2,snapshot_url:'/storage/review.jpg',camera_name:'Synthetic Exit',seen_at:'2026-01-01T10:01:00Z'},...extra});
const flush=()=>new Promise(r=>setImmediate(r));
async function respond(request,data,ok=true){request.resolve({ok,json:async()=>data});await flush()}
function loaded(h){h.w.document.querySelectorAll('.sighting-review-photo img').forEach(img=>{Object.defineProperty(img,'naturalWidth',{value:128});img.dispatchEvent(new h.w.Event('load'))})}
async function open(h,data=preview()){h.w.document.querySelector('[data-review-sighting]').click();await respond(h.requests.at(-1),data)}

test('server capability gates review controls; operator confirmations enter journey with clear labels',()=>{
 const h=harness();try{
  assert.equal(h.w.document.querySelectorAll('[data-review-sighting]').length,1);
  let html=h.w.MovementTimeline.render(h.rows,id=>id,{...h.context,can_review:false});
  assert.doesNotMatch(html,/data-review-sighting/);
  h.rows[1].association_status='operator_confirmed';h.rows[1].manual_review={reviewer:'<script>unsafe</script>',reviewed_at:'2026-01-01T11:00:00Z'};
  h.w.document.getElementById('root').innerHTML=h.w.MovementTimeline.render(h.rows,id=>id,h.context);
  assert.equal(h.w.document.querySelectorAll('[data-graph-select]').length,2);
  assert.equal(h.w.document.querySelectorAll('.movement-association-review').length,0);
  assert.match(h.w.document.body.textContent,/Operator confirmed/);assert.equal(h.w.document.querySelector('script'),null);
  assert.equal(h.w.document.querySelector('[data-review-sighting]').textContent,'Review / undo decision');
 }finally{h.close()}
});

test('side-by-side photos must load before confirmation; exact preview is saved once',async()=>{
 const h=harness();try{
  await open(h);assert.match(h.requests[0].url,/sightings\/2\/review\?reference_appearance_id=1$/);
  const confirm=h.w.document.querySelector('.review-confirm');assert.equal(confirm.disabled,true);
  assert.equal(h.w.document.querySelectorAll('.sighting-review-photo img').length,2);
  loaded(h);assert.equal(confirm.disabled,false);
  h.w.document.querySelector('textarea').value='Compared both photos';confirm.click();confirm.click();
  assert.equal(h.requests.length,2);const post=h.requests[1];
  assert.equal(post.options.headers['X-Requested-With'],'XMLHttpRequest');
  assert.deepEqual(JSON.parse(post.options.body),{preview_token:'synthetic-preview-token',decision:'confirmed',note:'Compared both photos'});
  await respond(post,{success:true,identity_id:'synthetic',reference_appearance_id:1,appearance_id:2,decision:'confirmed'});
  assert.equal(h.saved.length,0);assert.equal(h.closes.length,0);
  assert.match(h.w.document.querySelector('.sighting-review-status').textContent,/Decision saved/);
  h.w.document.querySelector('.sighting-review-close').click();
  assert.equal(h.saved.length,1);assert.equal(h.saved[0].sourceRoot,h.w.document.querySelector('.movement-review-scope'));assert.equal(h.closes.length,1);
 }finally{h.close()}
});

test('different-person decision stays open; Done closes without another mutation',async()=>{
 const h=harness();try{
  await open(h);loaded(h);h.w.document.querySelector('.review-reject').click();
  assert.equal(JSON.parse(h.requests[1].options.body).decision,'rejected');
  await respond(h.requests[1],{success:true,identity_id:'synthetic',reference_appearance_id:1,appearance_id:2,decision:'rejected'});
  h.w.document.querySelector('.sighting-review-close').click();assert.equal(h.requests.length,2);assert.equal(h.saved.length,1);
 }finally{h.close()}
});

test('missing photos block confirmation but existing decision can be undone',async()=>{
 const h=harness();try{
  await open(h,preview({can_decide:false,can_undo:true,decision:'confirmed',blocked_reason:'Photo expired',sighting:{id:2,snapshot_url:null}}));
  assert.equal(h.w.document.querySelector('.review-confirm').disabled,true);assert.equal(h.w.document.querySelector('.review-undo').disabled,false);
  h.w.document.querySelector('.review-undo').click();assert.equal(JSON.parse(h.requests[1].options.body).decision,'clear');
 }finally{h.close()}
});

test('photo load failure and concurrent-review rejection do not claim success',async()=>{
 const h=harness();try{
  await open(h);h.w.document.querySelector('.sighting-review-photo img').dispatchEvent(new h.w.Event('error'));
  assert.equal(h.w.document.querySelector('.review-confirm').disabled,true);
  await open(h);loaded(h);h.w.document.querySelector('.review-confirm').click();
  await respond(h.requests.at(-1),{detail:'Another review was saved.'},false);
  assert.match(h.w.document.querySelector('.sighting-review-status').textContent,/Another review was saved/);
  assert.equal(h.w.document.querySelector('.review-confirm').disabled,true);assert.equal(h.saved.length,0);
 }finally{h.close()}
});

test('late or closed preview cannot replace the current review',async()=>{
 const h=harness();try{
  const button=h.w.document.querySelector('[data-review-sighting]');button.click();const first=h.requests[0];
  button.click();const second=h.requests[1];assert.equal(first.options.signal.aborted,true);
  await respond(second,preview());await respond(first,{detail:'old result'},false);
  assert.equal(h.w.document.querySelectorAll('.sighting-review-photo img').length,2);
  button.click();const pending=h.requests.at(-1);h.w.ModalStack.close(h.w.document.getElementById('sighting-review-modal'));
  await respond(pending,preview());assert.equal(h.w.document.getElementById('sighting-review-modal').style.display,'none');
 }finally{h.close()}
});

test('closing during save still delivers a successful persisted decision to the original view',async()=>{
 const h=harness();try{
  await open(h);loaded(h);h.w.document.querySelector('.review-confirm').click();
  h.w.ModalStack.close(h.w.document.getElementById('sighting-review-modal'));
  await respond(h.requests.at(-1),{success:true,identity_id:'synthetic',reference_appearance_id:1,appearance_id:2});
  assert.equal(h.saved.length,1);assert.equal(h.w.document.getElementById('sighting-review-modal').style.display,'none');
 }finally{h.close()}
});

function addMore(h) {
 h.rows.push({...h.rows[1],id:3}, {...h.rows[1],id:4,association_status:'operator_rejected',manual_review:{reviewer:'Operator',reviewed_at:'2026-01-01T11:00:00Z'}});
 h.w.document.getElementById('root').innerHTML=h.w.MovementTimeline.render(h.rows,id=>id,h.context);
}
test('save advances within the modal, skips already reviewed rows, and fetches a fresh preview',async()=>{
 const h=harness();try{
  addMore(h);await open(h);loaded(h);
  assert.match(h.w.document.querySelector('.sighting-review-progress').textContent,/Image 1 of 2/);
  h.w.document.querySelector('.review-confirm').click();
  assert.equal(h.w.document.querySelector('.sighting-review-picker').disabled,true);
  await respond(h.requests[1],{identity_id:'synthetic',reference_appearance_id:1,appearance_id:2,decision:'confirmed'});
  assert.equal(h.closes.length,0);assert.equal(h.saved.length,0);
  assert.match(h.requests[2].url,/sightings\/3\/review/);
  await respond(h.requests[2],preview({preview_token:'next-token',sighting:{id:3,snapshot_url:'/storage/3.jpg'}}));
  assert.equal(h.w.document.querySelector('.review-confirm').disabled,true);
  loaded(h);h.w.document.querySelector('.review-reject').click();
  assert.equal(JSON.parse(h.requests[3].options.body).preview_token,'next-token');
  await respond(h.requests[3],{identity_id:'synthetic',reference_appearance_id:1,appearance_id:3,decision:'rejected'});
  assert.match(h.w.document.querySelector('.sighting-review-progress').textContent,/2 decisions saved/);
  h.w.document.querySelector('.sighting-review-close').click();assert.equal(h.saved.length,1);
 }finally{h.close()}
});
test('skip, previous, and image selection navigate without saving',async()=>{
 const h=harness();try{
  addMore(h);await open(h);h.w.document.querySelector('.review-defer').click();
  assert.match(h.requests[1].url,/sightings\/3\/review/);
  await respond(h.requests[1],preview());
  h.w.document.querySelector('.review-previous').click();
  assert.match(h.requests[2].url,/sightings\/2\/review/);
  await respond(h.requests[2],preview());
  const picker=h.w.document.querySelector('.sighting-review-picker');picker.value='1';picker.dispatchEvent(new h.w.Event('change'));
  assert.match(h.requests[3].url,/sightings\/3\/review/);
  assert.ok(h.requests.every(r=>r.options.method!=='POST'));
 }finally{h.close()}
});
test('failed save stays on the same image and never auto-advances',async()=>{
 const h=harness();try{
  addMore(h);await open(h);loaded(h);h.w.document.querySelector('.review-confirm').click();
  await respond(h.requests[1],{detail:'Review changed; reload.'},false);
  assert.equal(h.requests.length,2);assert.equal(h.closes.length,0);assert.equal(h.saved.length,0);
  assert.equal(h.w.document.querySelector('.review-confirm').disabled,true);
 }finally{h.close()}
});

test('last decision remains visible and undo reloads its current version',async()=>{
 const h=harness();try{
  await open(h);loaded(h);h.w.document.querySelector('.review-confirm').click();
  await respond(h.requests[1],{identity_id:'synthetic',reference_appearance_id:1,appearance_id:2,decision:'confirmed'});
  h.w.document.querySelector('.review-revisit').click();
  await respond(h.requests[2],preview({preview_token:'current-version',can_undo:true,decision:'confirmed'}));
  h.w.document.querySelector('.review-undo').click();
  assert.deepEqual(JSON.parse(h.requests[3].options.body),{preview_token:'current-version',decision:'clear',note:''});
  await respond(h.requests[3],{identity_id:'synthetic',reference_appearance_id:1,appearance_id:2,decision:'clear'});
  assert.match(h.requests[4].url,/sightings\/2\/review/);
  await respond(h.requests[4],preview());
  assert.match(h.w.document.querySelector('.sighting-review-progress').textContent,/0 decisions saved/);
 }finally{h.close()}
});

test('reference sighting can open a comparison against another sighting without saving',async()=>{
 const h=harness();try{
  const trigger=h.w.document.querySelector('[data-review-others]');
  assert.ok(trigger);assert.match(trigger.textContent,/Compare other sightings/);
  assert.match(h.w.document.querySelector('[data-review-sighting]').textContent,/Not the same person/);
  trigger.click();assert.equal(h.requests.length,1);
  assert.match(h.requests[0].url,/sightings\/2\/review\?reference_appearance_id=1$/);
  await respond(h.requests[0],preview());
  assert.equal(h.w.document.querySelector('.review-reject').disabled,true);
  loaded(h);assert.equal(h.w.document.querySelector('.review-reject').disabled,false);
  assert.equal(h.requests.length,1,'opening comparison must not reject anything');
 }finally{h.close()}
});
