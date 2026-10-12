const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
const {JSDOM}=require('jsdom');
function harness(){
 const html=fs.readFileSync('frontend/admin/unknown.html','utf8');
 const dom=new JSDOM(html,{url:'https://vas.test/admin/unknown',runScripts:'outside-only'}),w=dom.window;
 const source=fs.readFileSync('frontend/js/admin-unknown.js','utf8');
 let opening, pending=[], notifications=[];
 w.ModalStack={open:(el,opts)=>{opening=opts;},close:()=>opening?.onClose?.()};
 w.fetch=(url,opts)=>new Promise(resolve=>pending.push({url,opts,resolve}));
 w.showNotification=(...args)=>notifications.push(args);w.showFaceDetectionAlert=(...args)=>notifications.push(args);
 w.loadUnknownFaces=()=>{};w.AppConfirm={confirm:async()=>true};
 w.eval('var mergeSubmitInFlight=false,unknownLoadSequence=0,mergeRefreshTimer=null;var mergedAwayIdentities=new Set();');
 w.eval(source.slice(source.indexOf('let currentIdentityId'),source.indexOf('let multiSelectMode')).replace(/\b(let|const) /g,'var '));
 w.eval(source.slice(source.indexOf('function promoteIdentityModal('),source.indexOf('// Show the face detection alert.')));
 return {dom,w,pending,notifications,run:s=>w.eval(s),close:()=>opening?.onClose?.()};
}
const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
function respond(req,id,status=200){req.resolve({ok:status===200,status,json:async()=>status===200?{
 selection:{appearance_id:id,photo_sha256:'a'.repeat(64),preview_image:'/storage/selected-'+id+'.jpg',camera_name:'Synthetic Lobby',seen_at:'2026-01-01T00:00:00Z'},candidates:[]}: {detail:'Selected photo changed'}});}
test('same identity/different photo and closed preview cannot accept stale responses',async()=>{
 const h=harness();try{
 h.w.promoteIdentityModal('source',1);h.w.promoteIdentityModal('source',2);
 assert.equal(h.pending[0].opts.signal.aborted,true);
 respond(h.pending[1],2);await flush();respond(h.pending[0],1);await flush();
 assert.match(h.w.document.querySelector('#promote-preview img').src,/selected-2/);
 const img=h.w.document.querySelector('#promote-preview img');
 assert.equal(h.w.document.querySelector('#promote-form button[type=submit]').disabled,true);
 img.dispatchEvent(new h.w.Event('load'));
 assert.equal(h.run('promotionSelection.appearanceId'),2);
 h.close();img.dispatchEvent(new h.w.Event('load'));
 assert.equal(h.run('promotionSelection'),null);
 }finally{h.dom.window.close();}
});
test('submit pins previewed event and checksum; remaining Unknown group is never tombstoned',async()=>{
 const h=harness();try{
 h.w.promoteIdentityModal('source',7);respond(h.pending[0],7);await flush();
 h.w.document.querySelector('#promote-preview img').dispatchEvent(new h.w.Event('load'));
 h.w.document.getElementById('promote-name').value='Synthetic person';
 const saving=h.w.promoteIdentity();assert.equal(h.pending.length,2);
 const payload=JSON.parse(h.pending[1].opts.body);
 assert.equal(payload.appearance_id,7);assert.equal(payload.photo_sha256,'a'.repeat(64));
 await h.w.promoteIdentity();assert.equal(h.pending.length,2,'double click does not send again');
 h.pending[1].resolve({ok:true,status:200,json:async()=>({success:true,appearance_id:7,source_identity_id:'source',source_status:'active',source_remaining_appearances:2,identity:{id:'new-known'}})});
 await saving;assert.equal(h.run("mergedAwayIdentities.has('source')"),false);
 assert.equal(h.run("promotedSightings.has('7')"),true);
 }finally{h.dom.window.close();}
});
test('missing, stale or undisplayable preview cannot be submitted',async()=>{
 const h=harness();try{
 h.w.promoteIdentityModal('source',3);respond(h.pending[0],3,409);await flush();
 await h.w.promoteIdentity();assert.equal(h.pending.length,1);
 assert.match(h.w.document.getElementById('promote-candidates-warning').textContent,/changed/);
 h.w.promoteIdentityModal('source',4);respond(h.pending[1],4);await flush();
 h.w.document.querySelector('#promote-preview img').dispatchEvent(new h.w.Event('error'));
 await h.w.promoteIdentity();assert.equal(h.pending.length,2);
 }finally{h.dom.window.close();}
});
test('promotion without a sighting opens details instead of silently selecting a group portrait',()=>{
 const h=harness();try{
 let viewed;h.w.viewIdentityDetails=id=>viewed=id;
 h.w.promoteIdentityModal('source');assert.equal(viewed,'source');assert.equal(h.pending.length,0);
 }finally{h.dom.window.close();}
});
