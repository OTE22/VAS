const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
const id='e1bfe233-3918-407c-8822-2344387babc9';
const match={identity_id:id,display_name:'Demo match',type:'unknown',similarity:.88};
function harness(html='<body><img><p></p></body>') {
 const dom=new JSDOM(html,{url:'https://ui.test',runScripts:'outside-only'}),w=dom.window;
 const urls=new Map(),revoked=[];
 w.URL.createObjectURL=file=>{const url='blob:https://ui.test/'+urls.size;urls.set(url,file);return url;};
 w.URL.revokeObjectURL=url=>revoked.push(url);
 w.eval(fs.readFileSync('frontend/js/actions.js','utf8'));
 w.eval(fs.readFileSync('frontend/js/search-image.js','utf8'));
 w.HTMLElement.prototype.scrollIntoView=function(){};
 const file=new w.File(['synthetic'],'photo.png',{type:'image/png'});
 return {w,urls,revoked,file,close:()=>w.close()};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('reference fallback rejects external/API-supplied blob URLs and retains CSP-safe error handling',()=>{
 const h=harness(),img=h.w.document.querySelector('img'),caption=h.w.document.querySelector('p');
 const ref=h.w.SearchImage.createReference(h.file);
 for(const source of ['https://outside.test/face.jpg','//outside.test/face.jpg','javascript:alert(1)','blob:https://ui.test/untrusted','data:image/png;base64,eA==']){
  img.dataset.fallbackSrc='/wrong.jpg';h.w.SearchImage.render(img,caption,source,ref);
  assert.equal(img.dataset.searchImageSource,'uploaded');assert.equal(h.urls.get(img.src),h.file);
  assert.equal(img.hasAttribute('data-fallback-src'),false);assert.equal(img.alt,'Uploaded search image');
 }
 assert.equal(h.urls.size,1);assert.match(caption.title,/not a stored camera image/);
 h.w.SearchImage.render(img,caption,'/gone.jpg',ref);img.dispatchEvent(new h.w.Event('error'));
 assert.equal(img.dataset.searchImageSource,'uploaded');
 ref.release();ref.release();assert.equal(h.revoked.length,1);
 h.w.SearchImage.render(img,caption,null,ref);assert.equal(img.dataset.searchImageSource,'unavailable');h.close();
});

test('leaving page releases local reference URLs except while in back-forward cache',()=>{
 const h=harness(),img=h.w.document.querySelector('img'),caption=h.w.document.querySelector('p');
 h.w.SearchImage.render(img,caption,null,h.w.SearchImage.createReference(h.file));
 h.w.dispatchEvent(new h.w.PageTransitionEvent('pagehide',{persisted:true}));assert.equal(h.revoked.length,0);
 h.w.dispatchEvent(new h.w.PageTransitionEvent('pagehide',{persisted:false}));assert.equal(h.revoked.length,1);h.close();
});

function quickHarness(){
 const h=harness('<form id="search-image-form"><button type="submit">Search</button></form><input type="file" id="search-image-file"><select id="search-scope"><option>both</option></select><div id="search-results"><div id="search-results-grid"></div></div>');
 const source=fs.readFileSync('frontend/js/admin-unknown.js','utf8');
 h.w.showNotification=()=>{};
 h.w.eval(source.slice(source.indexOf('let quickSearchSequence ='),source.indexOf('// Show notification',source.indexOf('let quickSearchSequence ='))));
 Object.defineProperty(h.w.document.querySelector('#search-image-file'),'files',{value:[h.file],configurable:true});
 return h;
}
test('Quick Search missing and 404 images use uploaded reference; cancel releases it',async()=>{
 const h=quickHarness();
 h.w.fetch=async()=>({ok:true,headers:{get:()=>1},json:async()=>[match,{...match,snapshot_url:'/gone.jpg'}]});
 await h.w.searchByImage();
 const imgs=h.w.document.querySelectorAll('.card-image img');
 assert.equal(h.urls.get(imgs[0].src),h.file);assert.equal(imgs[1].dataset.searchImageSource,'stored');
 imgs[1].dispatchEvent(new h.w.Event('error'));assert.equal(imgs[1].src,imgs[0].src);
 assert.equal(h.w.document.querySelectorAll('.search-image-caption:not([hidden])').length,2);
 h.w.cancelQuickSearch();assert.equal(h.revoked.length,1);assert.equal(h.w.document.querySelector('#search-results-grid').children.length,0);h.close();
});

test('Quick Search ignores a cancelled response and uses new request photo',async()=>{
 const h=quickHarness(),pending=[];
 h.w.fetch=()=>new Promise(resolve=>pending.push(resolve));
 const old=h.w.searchByImage();h.w.cancelQuickSearch();
 const newer=new h.w.File(['new'],'new.png');Object.defineProperty(h.w.document.querySelector('#search-image-file'),'files',{value:[newer],configurable:true});
 const latest=h.w.searchByImage(),response={ok:true,headers:{get:()=>1},json:async()=>[match]};
 pending[1](response);await latest;pending[0](response);await old;
 assert.equal(h.urls.get(h.w.document.querySelector('.card-image img').src),newer);assert.equal(h.urls.size,1);h.close();
});

for (const security of [false,true]) test(`${security?'Security':'Identity'} Intelligence photo picker shows labelled fallback and releases on exit`,async()=>{
 const h=harness('<body><select id="test-select"></select></body>'),w=h.w;
 if(w.HTMLDialogElement){w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};}
 const name=security?'admin-security-intelligence':'admin-intelligence';
 let source=fs.readFileSync(`frontend/js/${name}.js`,'utf8');
 const start=source.indexOf(security?"    document.addEventListener('DOMContentLoaded', initializeSecurityIntelligence);":"    document.addEventListener('DOMContentLoaded', async function () {");
 assert.ok(start>0);
 source=source.slice(0,start)+(security?'window.probe={createSelector,selectorRegistry};':'window.probe={createIdentityPicker,picker,destroyIdentityPicker};')+'\n})();';
 w.eval(source);
 w.fetch=async url=>({ok:true,status:200,json:async()=>String(url).includes('/config')?{allowed_extensions:['.png'],max_file_size_mb:10}:String(url).includes('/by-image')?[match,{...match,identity_id:'f1bfe233-3918-407c-8822-2344387babc9',snapshot_url:'/gone.jpg'}]:{identities:[],total:0,pages:1}});
 const select=w.document.querySelector('select');
 if(security)w.probe.createSelector(select,{mode:'identity',multi:false,label:'Select Identity'});else w.probe.createIdentityPicker(select);
 const input=w.document.querySelector('.filter-photo-input');Object.defineProperty(input,'files',{value:[h.file]});
 input.dispatchEvent(new w.Event('change'));
 for(let i=0;i<15&&!w.document.querySelector('[data-search-image-source]');i++)await tick();
 const imgs=w.document.querySelectorAll('[data-search-image-source]');assert.equal(imgs.length,2);
 assert.equal(imgs[0].dataset.searchImageSource,'uploaded');assert.equal(h.urls.get(imgs[0].src),h.file);
 assert.equal(imgs[1].dataset.searchImageSource,'stored');imgs[1].dispatchEvent(new w.Event('error'));
 assert.equal(imgs[1].src,imgs[0].src);assert.equal(w.document.querySelectorAll('.search-image-caption:not([hidden])').length,2);
 w.document.querySelector('.filter-photo-clear').click();await tick();assert.equal(h.revoked.length,1);
 assert.equal(w.document.querySelectorAll('[data-search-image-source="uploaded"]').length,0);
 if(security)w.probe.selectorRegistry.get('test-select').destroy();else w.probe.destroyIdentityPicker();h.close();
});
