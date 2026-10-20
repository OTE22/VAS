const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
const {JSDOM}=require('jsdom');
const id='e1bfe233-3918-407c-8822-2344387babc9';
function harness(){
 const dom=new JSDOM(fs.readFileSync('frontend/admin/search.html','utf8'),{url:'https://ui.test',runScripts:'outside-only'});
 const w=dom.window;w.Actions={registered:{},register(handlers){Object.assign(this.registered,handlers)}};w.ModalStack={open(){},close(){},isOpen(){return false}};
 const urls=new Map(),revoked=[];
 w.URL.createObjectURL=file=>{const url='blob:https://ui.test/'+urls.size;urls.set(url,file);return url;};
 w.URL.revokeObjectURL=url=>revoked.push(url);
 w.eval(fs.readFileSync('frontend/js/search-image.js','utf8'));
 let source=fs.readFileSync('frontend/js/admin-search.js','utf8');
 const start=source.indexOf('    // Start when DOM is ready'),end=source.indexOf('    // Add CSS animation styles',start);
 source=source.slice(0,start)+'window.probe={state,elements,displayResults,clearResults,performSearch,viewIdentity,renderIdentityPanel,pipelineLabel,captureSearchContext,viewSearchAppearances,localSearchDate,closeIdentityModal,viewPossibleMatch,loadSearchConfig};\n'+source.slice(end);
 w.eval(source);return {w,t:w.probe,urls,revoked,close:()=>w.close()};
}
const match={identity_id:id,display_name:'Joey',type:'known',similarity:.89,confidence_band:'high',appearances_count:14,last_seen_at:'2026-09-17T07:56:29Z'};
test('Date To covers the full local day, including microseconds and DST offsets',()=>{
 const h=harness();
 for(const day of ['2026-10-15','2026-03-29','2026-10-25']){
  assert.equal(h.t.localSearchDate(day),new Date(`${day}T00:00:00`).toISOString());
  assert.equal(h.t.localSearchDate(day,true),new Date(`${day}T23:59:59`).toISOString().replace('.000Z','.999999Z'));
 }
 h.close();
});

test('matching appearances use original context, paginate and retain the submitted filters',async()=>{
 const h=harness(),token='e1bfe233-3918-407c-8822-2344387babc9';
 h.t.state.currentResults={...results(),appearance_context:token};
 h.t.state.currentResults.faces[0].face_index=0;
 h.t.state.resultContext={scope:'All Identities',camera:'All Cameras',dates:'2026-10-01 → 2026-10-15'};
 const urls=[];
 h.w.MovementTimeline={render:rows=>`<div data-tested-journey>${rows.map(r=>r.id).join(',')}</div>`};
 h.w.fetch=async url=>{
  urls.push(url);const later=String(url).includes('after_id=50');
  return {ok:true,json:async()=>({total:51,cameras:3,threshold:.8,coverage:'Direct comparison',has_more:!later,next_after_id:later?null:50,
   appearances:[{id:later?51:50,pipeline_id:'north',location_name:'North gate',start_time:'2026-10-15T18:00:00Z',similarity:.9}]})};
 };
 await h.t.viewIdentity(id,0);
 assert.match(urls[0],new RegExp(`/api/search/appearances/${token}\\?face_index=0&limit=50`));
 assert.doesNotMatch(urls[0],/search_embedding_id/);
 assert.match(h.w.document.querySelector('#identity-modal-body').textContent,/of 51 matching recorded appearances/);
 h.t.elements.pipelineFilter.value='different';
 h.w.document.querySelector('.appearance-pagination button:last-child').click();
 await new Promise(resolve=>setTimeout(resolve,0));
 assert.match(urls.at(-1),/after_id=50/);
 assert.equal(h.w.document.querySelector('[data-tested-journey]').textContent,'51');
 assert.match(h.w.document.querySelector('#identity-modal-body').textContent,/All Cameras/);
 assert.equal(h.w.document.querySelector('.appearance-pagination button:last-child').disabled,true);
 h.w.document.querySelector('.appearance-pagination button').click();
 await new Promise(resolve=>setTimeout(resolve,0));
 assert.doesNotMatch(urls.at(-1),/after_id/);
 h.close();
});

test('stale appearance requests cannot overwrite a newer face selection',async()=>{
 const h=harness();h.t.state.currentResults={appearance_context:id,faces:[{face_index:0},{face_index:1}]};
 const pending=[];h.w.fetch=url=>new Promise(resolve=>pending.push({url,resolve}));
 const first=h.t.viewSearchAppearances(0),second=h.t.viewSearchAppearances(1);
 const response=total=>({ok:true,json:async()=>({appearances:[],total,cameras:0,threshold:.8,coverage:'Direct',has_more:false})});
 pending[1].resolve(response(0));await second;pending[0].resolve(response(999));await first;
 assert.match(h.w.document.querySelector('#identity-modal-title').textContent,/Face 2/);
 assert.doesNotMatch(h.w.document.querySelector('#identity-modal-body').textContent,/999/);h.close();
});

test('expired appearance context is explained and does not fall back to group history',async()=>{
 const h=harness();h.t.state.currentResults={appearance_context:id,faces:[{face_index:0}]};
 let requests=0;h.w.fetch=async()=>{requests++;return {ok:false,status:410,json:async()=>({detail:'This search has expired. Run the image search again.'})};};
 await h.t.viewSearchAppearances(0);
 assert.match(h.w.document.querySelector('#identity-modal-body').textContent,/expired.*again/);
 assert.equal(requests,1);assert.equal(h.w.document.querySelector('.appearance-load-error button'),null);h.close();
});
test('operator-confirmed appearances show separate provenance without a fabricated similarity',async()=>{
 const h=harness();try{
  h.t.state.currentResults={appearance_context:id,faces:[{face_index:0}]};
  let rendered;
  h.w.MovementTimeline={render:(rows,label,options)=>{rendered={rows,options};return '<div>Test journey</div>'}};
  h.w.fetch=async()=>({ok:true,json:async()=>({total:2,cameras:2,threshold:.8,direct_count:1,operator_confirmed_count:1,has_more:false,
   appearances:[{id:1,pipeline_id:'entry',start_time:'2026-01-01T10:00:00Z',similarity:.9,match_basis:'direct'},
    {id:2,identity_id:id,pipeline_id:'exit',start_time:'2026-01-02T10:00:00Z',similarity:null,match_basis:'operator_confirmed',
     manual_review:{reviewer:'<script>not markup</script>',reviewed_at:'2026-01-03T10:00:00Z',reference_appearance_id:1,reference_similarity:.9}}]})});
  await h.t.viewSearchAppearances(0);
  const body=h.w.document.querySelector('#identity-modal-body');
  assert.match(body.textContent,/1 direct image matches.*1 operator-confirmed sightings/);
  assert.match(body.textContent,/no direct similarity score claimed/);
  assert.doesNotMatch(body.textContent,/(?:^|[^0-9])0% similarity|NaN%/);
  assert.equal(body.querySelector('script'),null);
  assert.equal(body.querySelector('.appearance-match-scores a').getAttribute('href'),`/admin/identity/${id}?appearance_id=1`);
  assert.equal(rendered.options.mode,'search');
 }finally{h.close()}
});
test('a changed review stops paging and requests a new search',async()=>{
 const h=harness();try{
  h.t.state.currentResults={appearance_context:id,faces:[{face_index:0}]};
  h.w.fetch=async()=>({ok:false,status:409,json:async()=>({detail:'Sighting review decisions changed. Run the image search again to refresh results.'})});
  await h.t.viewSearchAppearances(0);
  assert.match(h.w.document.querySelector('.appearance-load-error').textContent,/review decisions changed/);
  assert.equal(h.w.document.querySelector('.appearance-load-error button'),null);
 }finally{h.close()}
});
test('confirmed sightings can be explored when there are no direct summary cards in the filters',()=>{
 const h=harness();try{
  const data=results();data.faces[0].matches=[];data.faces[1].matches=[];data.appearance_context=id;
  h.t.displayResults(data);
  assert.match(h.w.document.querySelector('#search-overview').textContent,/Open matching appearances.*operator-confirmed/);
  assert.equal(h.w.document.querySelectorAll('[data-action="viewSearchAppearances"]').length,2);
 }finally{h.close()}
});
function results(){return {search_id:'search-1',faces:[{quality_score:.8,matches:[match]},{quality_score:.9,matches:[match]}],summary:{total_faces_detected:2,faces_searchable:2,total_matches:2,unique_identities_found:1},watchlist_alerts:[],processing_time_ms:20};}
test('result overview distinguishes matches from unique identities and explains scores',()=>{
 const h=harness();h.t.displayResults(results());
 const metrics=[...h.w.document.querySelectorAll('.search-result-metric strong')].map(x=>x.textContent);
 assert.deepEqual(metrics,['2','2','1','0']);assert.match(h.w.document.querySelector('#search-overview').textContent,/not a probability/);
 assert.match(h.w.document.querySelector('.match-card').textContent,/Similarity/);assert.match(h.w.document.querySelector('.match-card').textContent,/14 group records/);
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


test('same identity matched by different faces opens the exact clicked observation',async()=>{
 const h=harness(),data=results();
 data.faces[0].matches=[{...match,matched_embedding_id:11,matched_appearance_id:21,snapshot_url:'/storage/first.jpg',match_source:'sighting'}];
 data.faces[1].matches=[{...match,matched_embedding_id:12,matched_appearance_id:22,snapshot_url:'/storage/second.jpg',match_source:'sighting',similarity:.94}];
 h.t.state.currentResults=data;h.t.displayResults(data);
 const requested=[];h.w.fetch=async url=>{requested.push(String(url));return {ok:true,json:async()=>String(url).endsWith('/watchlists')?{watchlists:[]}:{id,display_name:'Different group portrait',snapshot_url:'/storage/wrong.jpg',appearances:[{id:21,association_status:'review_required'},{id:22,association_status:'selected'}],association_review:{included_count:1,review_count:1}}};};
 await h.t.viewIdentity(id,0,12);
 assert.ok(requested.some(url=>url.endsWith('?search_embedding_id=12')));
 assert.equal(new URL(h.w.document.querySelector('.identity-snapshot').src).pathname,'/storage/second.jpg');
 assert.equal(h.w.document.querySelectorAll('.identity-sighting').length,1);
 assert.match(h.w.document.querySelector('#identity-modal-body').textContent,/1 historical association/);
 assert.match(h.w.document.querySelector('.identity-match-context').textContent,/94%/);
 assert.ok(h.w.document.querySelector('#identity-full-profile').href.endsWith('?search_embedding_id=12'));
 h.close();
});
test('unverified historical match does not inherit group name or watchlist claim',async()=>{
 const h=harness(),data=results();data.faces=[{matches:[{...match,type:'unverified',display_name:null,identity_verification:'unverified',matched_embedding_id:44}]}];
 h.t.state.currentResults=data;h.t.displayResults(data);assert.match(h.w.document.querySelector('.match-card').textContent,/Unverified historical match/);
 h.w.fetch=async url=>({ok:true,json:async()=>String(url).endsWith('/watchlists')?{watchlists:[{name:'Sensitive group'}]}:{id,display_name:'Wrong group name',snapshot_url:'/storage/wrong.jpg',appearances:[]}});
 await h.t.viewIdentity(id,0,44);
 const text=h.w.document.querySelector('#identity-modal-body').textContent;
 assert.doesNotMatch(text,/Wrong group name|Sensitive group/);
 assert.match(text,/not verified/);assert.equal(h.w.document.querySelector('#identity-analyze').hidden,true);
 h.close();
});

const possible={...match,similarity:.542,match_tier:'possible',match_source:'sighting',matched_embedding_id:52,
 matched_at:'2026-10-20T10:00:00Z',matched_pipeline_id:'North gate',snapshot_url:'/storage/candidate.jpg',
 // Deliberately hostile legacy data: a lower-tier card must ignore names/alerts.
 display_name:'Misleading name',watchlist_match:{list_name:'Sensitive watchlist',alert_level:'critical'}};
function tieredResults(){return {...results(),thresholds:{strong:.6,possible:.5},appearance_context:id,
 faces:[{face_index:0,quality_score:.8,matches:[match],possible_matches:[possible,{...possible,snapshot_url:'/storage/second-candidate.jpg',similarity:.51}]}]};}

test('strong and possible results have separate counts, ranges and no inherited possible identity claim',()=>{
 const h=harness();try{
  const data=tieredResults();h.t.displayResults(data);
  assert.deepEqual([...h.w.document.querySelectorAll('.search-result-metric strong')].map(el=>el.textContent),['1','1','2','1','0']);
  assert.equal(h.t.elements.matchesCount.textContent,'3');
  const strong=h.w.document.querySelector('.strong-tier'),lower=h.w.document.querySelector('.possible-tier');
  assert.match(strong.textContent,/Strong matches.*1/s);
  assert.match(strong.textContent,/60% and above/);
  assert.match(lower.textContent,/50% to below 60%/);
  assert.match(lower.textContent,/Review required/);
  assert.match(lower.textContent,/54.2%/);
  assert.doesNotMatch(lower.textContent,/Misleading name|Sensitive watchlist|14 group records/);
  assert.equal(lower.querySelector('.possible-match-card').tagName,'BUTTON');
  assert.equal(lower.querySelector('[data-action="viewIdentity"]'),null);
  assert.match(h.w.document.querySelector('#summary-tab').textContent,/Possible · review required/);
 }finally{h.close()}
});

test('possible-only search remains useful and review compares exact evidence without group history requests',()=>{
 const h=harness();try{
  const data=tieredResults();data.faces[0].matches=[];
  h.t.state.currentResults=data;
  const file=new h.w.File(['upload'],'reference.jpg',{type:'image/jpeg'});
  h.t.state.resultReferences=[h.w.SearchImage.createReference(file)];
  h.t.displayResults(data);
  assert.match(h.w.document.querySelector('.search-result-note').textContent,/No strong matches.*lower-similarity/);
  let requests=0;h.w.fetch=()=>{requests++;throw new Error('Candidate preview must not fetch group history');};
  const card=h.w.document.querySelector('.possible-match-card');
  h.w.Actions.registered.viewPossibleMatch(card);
  const body=h.w.document.querySelector('#identity-modal-body');
  assert.match(body.textContent,/identity unconfirmed/);
  assert.doesNotMatch(body.textContent,/Misleading name|Sensitive watchlist|Joey/);
  assert.equal(h.w.document.querySelector('#identity-full-profile').hidden,true);
  assert.equal(h.w.document.querySelector('#identity-analyze').hidden,true);
  assert.equal(h.urls.get(body.querySelector('.reference-photo img').src),file);
  assert.equal(new URL(body.querySelector('.candidate-photo img').src).pathname,'/storage/candidate.jpg');
  assert.equal(body.querySelector('[data-possible-nav="previous"]').disabled,true);
  body.querySelector('[data-possible-nav="next"]').click();
  assert.match(body.textContent,/Candidate 2 of 2/);
  assert.equal(new URL(body.querySelector('.candidate-photo img').src).pathname,'/storage/second-candidate.jpg');
  assert.equal(body.querySelector('[data-possible-nav="next"]').disabled,true);
  assert.equal(h.w.document.activeElement.dataset.possibleNav,'previous');
  assert.equal(h.w.document.querySelector('#identity-modal').classList.contains('active'),true);
  assert.equal(requests,0);
 }finally{h.close()}
});

test('missing or failed possible photo never substitutes the uploaded reference as evidence',()=>{
 const h=harness();try{
  const data=tieredResults();data.faces[0].possible_matches=[{...possible,snapshot_url:null,best_snapshot_path:null,match_source:'unavailable'}];
  h.t.state.currentResults=data;
  h.t.state.resultReferences=[h.w.SearchImage.createReference(new h.w.File(['upload'],'reference.jpg'))];
  h.t.displayResults(data);
  assert.equal(h.w.document.querySelector('.possible-match-card img').dataset.searchImageSource,'unavailable');
  assert.match(h.w.document.querySelector('.possible-match-card').textContent,/Photo source unavailable/);
  h.t.viewPossibleMatch(0,0);
  assert.match(h.w.document.querySelector('.possible-review-facts').textContent,/Photo source unavailable/);
  assert.equal(h.w.document.querySelector('.candidate-photo img').dataset.searchImageSource,'unavailable');
  assert.equal(h.w.document.querySelector('.reference-photo img').dataset.searchImageSource,'uploaded');
  data.faces[0].possible_matches=[possible];h.t.viewPossibleMatch(0,0);
  h.w.document.querySelector('.candidate-photo img').dispatchEvent(new h.w.Event('error'));
  assert.equal(h.w.document.querySelector('.candidate-photo img').dataset.searchImageSource,'unavailable');
 }finally{h.close()}
});

test('server config drives the guide while completed search keeps its original thresholds',async()=>{
 const h=harness();try{
  h.w.fetch=async()=>({ok:true,json:async()=>({results:{strong_threshold:.72,possible_threshold:.48}})});
  await h.t.loadSearchConfig();
  assert.match(h.w.document.querySelector('#search-threshold-guide').textContent,/72% and above/);
  assert.match(h.w.document.querySelector('#search-threshold-guide').textContent,/48% to below 72%/);
  h.t.displayResults(tieredResults());
  assert.match(h.w.document.querySelector('.strong-tier .tier-range').textContent,/60% and above/);
  assert.match(h.w.document.querySelector('.possible-tier .tier-range').textContent,/50% to below 60%/);
  const data=tieredResults();data.thresholds={strong:.6,possible:.6};data.faces[0].possible_matches=[];
  h.t.displayResults(data);
  assert.match(h.w.document.querySelector('.possible-tier').textContent,/lower tier is disabled/);
 }finally{h.close()}
});

test('batch possible review uses the correct uploaded image and preserves strong-only alert totals',()=>{
 const h=harness();try{
  const files=[new h.w.File(['one'],'first.jpg'),new h.w.File(['two'],'second.jpg')];
  h.t.state.resultReferences=files.map(file=>h.w.SearchImage.createReference(file));
  h.t.state.resultContext={referenceNames:files.map(file=>file.name)};
  const data={batch_id:'batch',thresholds:{strong:.6,possible:.5},watchlist_alerts:0,results:[
   {status:'success',image_index:0,image_name:'first.jpg',faces_detected:1,matches:[match],possible_matches:[]},
   {status:'success',image_index:1,image_name:'second.jpg',faces_detected:2,matches:[],possible_matches:[{...possible,face_index:1}]}]};
  h.t.state.currentResults=data;h.t.displayResults(data);
  h.w.Actions.registered.viewPossibleMatch(h.w.document.querySelector('.possible-match-card'));
  assert.equal(h.urls.get(h.w.document.querySelector('.reference-photo img').src),files[1]);
  assert.match(h.w.document.querySelector('.reference-photo figcaption').textContent,/Uploaded image · compare Face 2/);
  assert.equal(h.t.elements.alertsCount.textContent,'0');
  assert.match(h.w.document.querySelector('#alerts-tab').textContent,/No watchlist alerts/i);
 }finally{h.close()}
});

test('a stale identity request cannot replace a newer possible-photo comparison',async()=>{
 const h=harness();try{
  const data=tieredResults();delete data.appearance_context;h.t.state.currentResults=data;
  let resolveIdentity;
  h.w.fetch=url=>String(url).endsWith('/watchlists')
   ? Promise.resolve({ok:true,json:async()=>({watchlists:[]})})
   : new Promise(resolve=>{resolveIdentity=resolve});
  const pending=h.t.viewIdentity(id,0);
  h.t.viewPossibleMatch(0,0);
  resolveIdentity({ok:true,json:async()=>({id,display_name:'Old request',appearances:[]})});
  await pending;
  assert.notEqual(h.w.document.querySelector('.possible-review'),null);
  assert.doesNotMatch(h.w.document.querySelector('#identity-modal-body').textContent,/Old request/);
 }finally{h.close()}
});

test('multi-face reference preview outlines only the server-selected query face',()=>{
 const h=harness();try{
  const data=tieredResults();data.faces[0].face_index=1;
  data.faces[0].bounding_box={x1:200,y1:50,x2:300,y2:175};
  h.t.state.currentResults=data;
  h.t.state.resultReferences=[h.w.SearchImage.createReference(new h.w.File(['image'],'two-faces.jpg'))];
  h.t.viewPossibleMatch(0,0);
  const image=h.w.document.querySelector('.reference-photo img');
  Object.defineProperty(image,'naturalWidth',{value:640});
  Object.defineProperty(image,'naturalHeight',{value:480});
  image.dispatchEvent(new h.w.Event('load'));
  assert.match(h.w.document.querySelector('.reference-photo figcaption').textContent,/highlighted Face 2/);
  assert.equal(h.w.document.querySelector('.reference-face-box').getAttribute('viewBox'),'0 0 640 480');
  const rect=h.w.document.querySelector('.reference-face-box rect');
  assert.equal(rect.getAttribute('x'),'200');assert.equal(rect.getAttribute('width'),'100');
  assert.equal(h.w.document.querySelector('.candidate-photo .reference-face-box'),null);
 }finally{h.close()}
});

test('possible scores below the strong boundary are never rounded into the strong tier',()=>{
 const h=harness();try{
  const data=tieredResults();data.faces[0].possible_matches=[{...possible,similarity:.59999}];
  h.t.state.currentResults=data;h.t.displayResults(data);
  assert.match(h.w.document.querySelector('.possible-score').textContent,/59.9%/);
  h.t.viewPossibleMatch(0,0);
  assert.equal(h.w.document.querySelector('.possible-review-score').textContent,'59.9% similarity');
 }finally{h.close()}
});
