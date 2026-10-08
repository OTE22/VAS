const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
const {JSDOM}=require('jsdom');
const id='e1bfe233-3918-407c-8822-2344387babc9';
function harness(){
 const dom=new JSDOM(fs.readFileSync('frontend/admin/search.html','utf8'),{url:'https://ui.test',runScripts:'outside-only'});
 const w=dom.window;w.Actions={register(){}};w.ModalStack={open(){},close(){}};
 const urls=new Map(),revoked=[];
 w.URL.createObjectURL=file=>{const url='blob:https://ui.test/'+urls.size;urls.set(url,file);return url;};
 w.URL.revokeObjectURL=url=>revoked.push(url);
 w.eval(fs.readFileSync('frontend/js/search-image.js','utf8'));
 let source=fs.readFileSync('frontend/js/admin-search.js','utf8');
 const start=source.indexOf('    // Start when DOM is ready'),end=source.indexOf('    // Add CSS animation styles',start);
 source=source.slice(0,start)+'window.probe={state,elements,displayResults,clearResults,performSearch,viewIdentity,renderIdentityPanel,pipelineLabel,captureSearchContext};\n'+source.slice(end);
 w.eval(source);return {w,t:w.probe,urls,revoked,close:()=>w.close()};
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

test('missing result image uses submitted photo, preserves API records and releases on clear',async()=>{
 const h=harness(),file=new h.w.File(['reference'],'photo.png',{type:'image/png'});
 h.t.state.selectedFile=file;const data=results(),original=JSON.stringify(data);
 h.w.fetch=async()=>({ok:true,json:async()=>data});await h.t.performSearch();
 const imgs=[...h.w.document.querySelectorAll('.match-snapshot')];
 assert.ok(imgs.every(img=>img.dataset.searchImageSource==='uploaded'));
 assert.equal(h.urls.get(imgs[0].src),file);assert.equal(imgs[1].src,imgs[0].src);
 assert.equal(h.w.document.querySelector('.search-image-caption').textContent,'Uploaded search image');
 assert.equal(JSON.stringify(data),original);
 h.t.clearResults();assert.deepEqual(h.revoked,[imgs[0].src]);assert.equal(h.t.state.resultReferences.length,0);h.close();
});

test('existing portrait stays preferred and failed loads switch to labelled upload',async()=>{
 const h=harness();h.t.state.selectedFile=new h.w.File(['ref'],'photo.png');
 const data=results();data.faces=[{matches:[{...match,snapshot_url:'/storage/portrait.jpg'}]}];
 h.w.fetch=async()=>({ok:true,json:async()=>data});await h.t.performSearch();
 const img=h.w.document.querySelector('.match-snapshot'),caption=h.w.document.querySelector('.search-image-caption');
 assert.equal(img.src,'https://ui.test/storage/portrait.jpg');assert.equal(caption.hidden,true);assert.equal(h.urls.size,0);
 img.dispatchEvent(new h.w.Event('error'));assert.equal(img.dataset.searchImageSource,'uploaded');assert.equal(caption.hidden,false);
 img.dispatchEvent(new h.w.Event('error'));assert.equal(img.dataset.searchImageSource,'unavailable');
 assert.match(caption.textContent,/history retained/);img.dispatchEvent(new h.w.Event('error'));assert.equal(img.hasAttribute('src'),false);h.close();
});

test('reference stays tied to request during file changes and a failed rerun',async()=>{
 const h=harness(),first=new h.w.File(['first'],'first.png'),second=new h.w.File(['second'],'second.png');
 h.t.state.selectedFile=first;let resolve;
 h.w.fetch=()=>new Promise(r=>{resolve=r;});const request=h.t.performSearch();
 h.t.state.selectedFile=second;resolve({ok:true,json:async()=>results()});await request;
 const url=h.w.document.querySelector('.match-snapshot').src;assert.equal(h.urls.get(url),first);
 h.w.fetch=async()=>{throw new Error('Offline')};await h.t.performSearch();
 assert.equal(h.w.document.querySelector('.match-snapshot').src,url);assert.deepEqual(h.revoked,[]);
 h.w.fetch=async()=>({ok:true,json:async()=>results()});await h.t.performSearch();
 assert.equal(h.urls.get(h.w.document.querySelector('.match-snapshot').src),second);assert.deepEqual(h.revoked,[url]);h.close();
});

test('batch uses response image_index, including duplicate filenames, partial failures and detail modal',async()=>{
 const h=harness();h.t.state.isBatchMode=true;
 const files=[0,1,2].map(n=>new h.w.File([String(n)],'same.png'));h.t.state.batchFiles=files;
 const data={batch_id:'batch',results:[
  {image_index:2,image_name:'same.png',status:'success',matches:[{...match,similarity:.75}]},
  {image_index:0,image_name:'same.png',status:'error',error_message:'No face'},
  {image_index:1,image_name:'same.png',status:'success',matches:[match]}],watchlist_alerts:0};
 h.w.fetch=async()=>({ok:true,json:async()=>data});await h.t.performSearch();
 const cards=[...h.w.document.querySelectorAll('.match-card')];
 assert.equal(h.urls.get(cards[0].querySelector('img').src),files[2]);assert.equal(h.urls.get(cards[1].querySelector('img').src),files[1]);
 h.w.fetch=async url=>({ok:true,json:async()=>String(url).endsWith('/watchlists')?{watchlists:[]}:{id,display_name:'Example',appearances:[]}});
 await h.t.viewIdentity(id,2);
 assert.equal(h.urls.get(h.w.document.querySelector('.identity-snapshot').src),files[2]);
 assert.match(h.w.document.querySelector('.identity-match-context').textContent,/75%/);
 assert.match(h.w.document.querySelector('.identity-header').textContent,/Uploaded search image/);h.close();
});

test('no returned matches do not display the reference as a result',async()=>{
 const h=harness();h.t.state.selectedFile=new h.w.File(['ref'],'photo.png');
 h.w.fetch=async()=>({ok:true,json:async()=>({faces:[{matches:[]}],watchlist_alerts:[]})});await h.t.performSearch();
 assert.equal(h.w.document.querySelectorAll('.match-snapshot').length,0);assert.equal(h.urls.size,0);h.close();
});

test('batch decoding omissions remap unique filenames and never guess between duplicate names',async()=>{
 const h=harness();h.t.state.isBatchMode=true;
 const bad=new h.w.File(['bad'],'broken.png'),good=new h.w.File(['good'],'valid.png');
 h.t.state.batchFiles=[bad,good];
 let data={batch_id:'batch',results:[{image_index:0,image_name:'valid.png',status:'success',matches:[match]}],watchlist_alerts:0};
 h.w.fetch=async()=>({ok:true,json:async()=>data});await h.t.performSearch();
 assert.equal(h.urls.get(h.w.document.querySelector('.match-snapshot').src),good);
 const fileA=new h.w.File(['bad'],'same.png'),fileB=new h.w.File(['good'],'same.png');
 h.t.state.batchFiles=[fileA,fileB];data={...data,results:[{...data.results[0],image_name:'same.png'}]};
 await h.t.performSearch();
 assert.equal(h.w.document.querySelector('.match-snapshot').dataset.searchImageSource,'unavailable');
 assert.match(h.w.document.querySelector('.search-image-caption').textContent,/Image unavailable/);h.close();
});
