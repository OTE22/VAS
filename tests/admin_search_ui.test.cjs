const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
const {JSDOM}=require('jsdom');
const id='e1bfe233-3918-407c-8822-2344387babc9';
function harness(){
 const dom=new JSDOM(fs.readFileSync('frontend/admin/search.html','utf8'),{url:'https://ui.test',runScripts:'outside-only'});
 const w=dom.window;w.Actions={register(){}};w.ModalStack={open(){},close(){}};
 let source=fs.readFileSync('frontend/js/admin-search.js','utf8');
 const start=source.indexOf('    // Start when DOM is ready'),end=source.indexOf('    // Add CSS animation styles',start);
 source=source.slice(0,start)+'window.probe={state,elements,displayResults,clearResults,performSearch,viewIdentity,renderIdentityPanel,pipelineLabel,captureSearchContext};\n'+source.slice(end);
 w.eval(source);return {w,t:w.probe,close:()=>w.close()};
}
const match={identity_id:id,display_name:'Joey',type:'known',similarity:.89,confidence_band:'high',appearances_count:14,last_seen_at:'2026-09-17T07:56:29Z'};
function results(){return {search_id:'search-1',faces:[{quality_score:.8,matches:[match]},{quality_score:.9,matches:[match]}],summary:{total_faces_detected:2,faces_searchable:2,total_matches:2,unique_identities_found:1},watchlist_alerts:[],processing_time_ms:20};}
test('result overview distinguishes matches from unique identities and explains scores',()=>{
 const h=harness();h.t.displayResults(results());
 const metrics=[...h.w.document.querySelectorAll('.search-result-metric strong')].map(x=>x.textContent);
 assert.deepEqual(metrics,['2','2','1','0']);assert.match(h.w.document.querySelector('#search-overview').textContent,/not a probability/);
 assert.match(h.w.document.querySelector('.match-card').textContent,/Similarity/);assert.match(h.w.document.querySelector('.match-card').textContent,/14 detections/);
 h.t.clearResults();assert.equal(h.w.document.querySelector('#search-overview').hidden,true);h.close();
});
test('batch overview handles integer watchlist counts and partial failures',()=>{
 const h=harness();h.t.displayResults({batch_id:'batch',total_images:2,results:[{status:'success',image_name:'one.png',faces_detected:1,matches:[match]},{status:'error',image_name:'two.png',error_message:'No face'}],watchlist_alerts:3});
 assert.deepEqual([...h.w.document.querySelectorAll('.search-result-metric strong')].map(x=>x.textContent),['2','1','1','3']);
 assert.match(h.w.document.querySelector('.search-result-note').textContent,/1 image\(s\) failed/);h.close();
});
test('watchlist request failure is shown as unavailable, never absent',async()=>{
 const h=harness();h.w.fetch=async url=>String(url).endsWith('/watchlists')?{ok:false,status:503}:{ok:true,json:async()=>({id,display_name:'Joey',appearances:[]})};
 await h.t.viewIdentity(id);const text=h.w.document.querySelector('#identity-modal-body').textContent;
 assert.match(text,/membership could not be loaded/);assert.doesNotMatch(text,/Not on any watchlist/);h.close();
});
test('camera details show names and suppress unnamed UUIDs',()=>{
 const h=harness();h.t.state.pipelines=[{pipeline_id:id,display_name:'Logistics entrance'}];
 assert.equal(h.t.pipelineLabel(id),'Logistics entrance');h.t.state.pipelines=[];
 assert.equal(h.t.pipelineLabel(id),'Camera name unavailable');h.close();
});
test('failed rerun preserves previous result context and clearly labels it',async()=>{
 const h=harness();h.t.state.selectedFile=new h.w.File(['test'],'reference.png',{type:'image/png'});
 h.w.fetch=async()=>({ok:true,json:async()=>results()});await h.t.performSearch();
 h.t.elements.searchScope.value='unknown';h.w.fetch=async()=>{throw new Error('Offline')};await h.t.performSearch();
 assert.equal(h.t.state.resultContext.scope,'All Identities (Known + Unknown)');
 assert.match(h.w.document.querySelector('#search-feedback').textContent,/previous successful search/);h.close();
});
