const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('frontend/js/admin-unknown.js','utf8');
const code=source.slice(source.indexOf('let quickSearchSequence ='),source.indexOf('function createSearchResultCard('));
const elements=new Map(),pending=[];
function node(id){if(!elements.has(id)) elements.set(id,{files:[{}],value:'both',disabled:false,style:{},children:[],textContent:'',replaceChildren(){this.children=[];this.textContent='';},appendChild(child){this.children.push(child);}});return elements.get(id);}
const context={AbortController,FormData:class{append(){}},document:{getElementById:node,querySelector:node,createElement:()=>({textContent:''})},showNotification(){},createSearchResultCard:r=>r,fetch:(url,options)=>new Promise(resolve=>pending.push({resolve,options}))};
vm.createContext(context);vm.runInContext(code,context);
const run=c=>vm.runInContext(c,context);
const response=id=>({ok:true,headers:{get:()=> '2'},json:async()=>[{identity_id:id}]});
(async()=>{
 const old=run('searchByImage()');await run('searchByImage()');assert.equal(pending.length,1);
 run('cancelQuickSearch()');assert.equal(pending[0].options.signal.aborted,true);
 const latest=run('searchByImage()');pending[1].resolve(response('new'));await latest;
 pending[0].resolve(response('old'));await old;
 const children=node('search-results-grid').children;
 assert.equal(children.at(-1).identity_id,'new');assert.match(children[0].textContent,/2 faces detected/);
 assert.equal(node('#search-image-form button[type="submit"]').disabled,false);
 console.log('PASS Quick Search: duplicate submit blocked, cancellation aborts request, late response ignored, group-photo notice, button recovery.');
})().catch(e=>{console.error(e);process.exitCode=1;});
