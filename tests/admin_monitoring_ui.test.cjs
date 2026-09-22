const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
const {JSDOM}=require('jsdom');
function tasks(){
 const dom=new JSDOM(fs.readFileSync('frontend/admin/background-tasks.html','utf8'),{url:'https://ui.test',runScripts:'outside-only'}),w=dom.window;
 w.ModalStack={open:n=>n.style.display='flex',close:n=>n.style.display='none',isOpen:()=>false};
 let src=fs.readFileSync('frontend/js/admin-background-tasks.js','utf8');
 src=src.slice(0,src.lastIndexOf("    document.addEventListener('DOMContentLoaded'"))+'window.probe={renderStats,renderTaskModal,openTaskDetails,closeTaskModal,state};})();';w.eval(src);
 return {dom,w,t:w.probe};
}
function response(data,ok=true){return {ok,status:ok?200:503,headers:{get:()=> 'application/json'},json:async()=>data};}
test('task summary states its scope and has no invented success rate without finished work',()=>{
 const h=tasks();h.t.renderStats({window_days:7,completed:0,failed:0});
 assert.match(h.w.document.getElementById('task-stats-scope').textContent,/last 7 days/);
 assert.equal([...h.w.document.querySelectorAll('.stat-value')].at(-1).textContent,'—');h.w.close();
});
test('task details explain status and put raw results behind an expandable section',()=>{
 const h=tasks();h.t.renderTaskModal({id:1,task_name:'Retention',status:'completed',result:{rows_deleted:8,files_deleted:3}});
 assert.match(h.w.document.querySelector('.task-state-explanation').textContent,/reported completion/);
 assert.match(h.w.document.querySelector('.task-result-summary').textContent,/Rows deleted: 8/);
 assert.equal(h.w.document.querySelector('.task-json').closest('details').open,false);h.w.close();
});
test('a stale task request cannot replace the newer detail view',async()=>{
 const h=tasks(),pending=[];h.w.fetch=()=>new Promise(resolve=>pending.push(resolve));
 const old=h.t.openTaskDetails(1),newer=h.t.openTaskDetails(2);
 pending[1](response({id:2,task_name:'Newer task',status:'running'}));await newer;
 pending[0](response({id:1,task_name:'Old task',status:'completed'}));await old;
 assert.equal(h.w.document.getElementById('task-modal-title-text').textContent,'Newer task');h.w.close();
});
test('task fetch failure never silently substitutes a cached task',async()=>{
 const h=tasks();h.t.state.tasksById.set(1,{id:1,task_name:'Stale task',status:'running'});
 h.w.fetch=async()=>response({detail:'Unavailable'},false);await h.t.openTaskDetails(1);
 assert.match(h.w.document.getElementById('task-modal-body').textContent,/Could not load current task details/);h.w.close();
});
test('log page search finds full messages while keeping markup inert',async()=>{
 const dom=new JSDOM(fs.readFileSync('frontend/admin/logs.html','utf8'),{url:'https://ui.test',runScripts:'outside-only'}),w=dom.window;
 let src=fs.readFileSync('frontend/js/admin-logs.js','utf8');src=src.slice(0,src.indexOf("document.addEventListener('DOMContentLoaded'"))+'window.probe={loadLogs,filterLogPage};';w.eval(src);
 w.fetch=async()=>response({logs:[{level:'ERROR',message:'Failure\nSecret detail <img src=x>',source_file:'app.log'},{level:'INFO',message:'Started'}],total_count:2,total_pages:1,page:1});
 await w.probe.loadLogs();assert.equal(w.document.querySelector('#logs-container img'),null);
 const input=w.document.getElementById('log-page-search');input.value='secret detail';w.probe.filterLogPage();
 const rows=[...w.document.querySelectorAll('.log-entry')];assert.equal(rows[0].hidden,false);assert.equal(rows[1].hidden,true);
 assert.match(w.document.getElementById('log-find-status').textContent,/1 of 2 loaded records/);w.close();
});
