const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const timers=[],nodes=[];
function node(tag,opts){const n={tag,opts,hidden:false,append(){},appendChild(){},replaceChildren(){},setAttribute(){},classList:{add(){},remove(){}},querySelector(){return null},addEventListener(event,fn){this[event]=fn}};nodes.push(n);return n}
const window={addEventListener(){},location:{origin:'https://example.invalid'},setTimeout:cb=>{timers.push(cb);return timers.length},clearTimeout(){}};
const document={addEventListener(){},querySelectorAll(){return[]},getElementById:()=>node(),createElement:node,createTextNode:t=>t};
const context=vm.createContext({window,document,console,AbortController,AbortSignal,URL,URLSearchParams,Date,Map,Set,Intl,navigator:{},fetch:(_url,opt)=>new Promise((_resolve,reject)=>opt.signal.addEventListener('abort',()=>reject(Object.assign(new Error('timeout'),{name:'AbortError'}))))});
let source=fs.readFileSync('frontend/js/admin-security-intelligence.js','utf8').replace(/\}\)\(\);\s*$/,`window.test={renderThreatAssessment,state,api}; el=window.fakeNode; })();`);
window.fakeNode=node;vm.runInContext(source,context);
const {test}=require('node:test');
test('revealing an assessment survives rerender and reload, without leaking to another assessment',()=>{
 const a={identity_id:'audit',assessment_id:'same-id',overall_risk_score:10,ml_observation:{score:1}};
 const stored=new Map();window.sessionStorage={getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v)};
 window.test.renderThreatAssessment(a);
 nodes.find(n=>n.opts?.className?.includes('ml-observation-reveal')).click();
 assert.equal(window.test.state.mlObservationRevealed,true);
 window.test.renderThreatAssessment(a);
 assert.equal(window.test.state.mlObservationRevealed,true);
 window.test.renderThreatAssessment({...a,assessment_id:'other-id'});
 assert.equal(window.test.state.mlObservationRevealed,false);
 vm.runInContext(source,context);
 window.test.renderThreatAssessment(a);
 assert.equal(window.test.state.mlObservationRevealed,true);
});
for(const external of [false,true]) test('timeout visible with external signal='+external,async()=>{
 const ctl=new AbortController();
 const pending=window.test.api('/api/test',external?{signal:ctl.signal}:{});timers.at(-1)();
 await assert.rejects(pending,e=>e.code==='TIMEOUT'&&!e.aborted);
 assert.equal(ctl.signal.aborted,false);
});
test('intentional cancellation stays silent',async()=>{
 const ctl=new AbortController();const pending=window.test.api('/api/test',{signal:ctl.signal});ctl.abort();
 await assert.rejects(pending,e=>e.aborted&&!e.code);
});
