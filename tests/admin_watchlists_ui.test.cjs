// Run with node --test; jsdom is supplied by the temporary UI verification environment.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
const watchlist={id:'vip',name:'VIP',description:'ENTER THE LOGISTICS',alert_level:'critical',is_active:true,entries_count:1,alerts_today:0,total_alerts:14,last_alert_at:'2026-09-17T07:56:29Z',created_at:'2026-09-16T09:30:18Z',updated_at:'2026-09-16T09:30:18Z',version:1};
const entry={identity_id:'joey',identity_name:'Joey',identity_type:'known',priority:'normal',is_active:true,added_at:'2026-09-16T09:43:03Z'};
const tick=()=>new Promise(r=>setTimeout(r,20));
function harness(options={}){
 const dom=new JSDOM(fs.readFileSync('frontend/admin/watchlists.html','utf8'),{url:'https://ui.test',runScripts:'outside-only',pretendToBeVisual:true});
 const w=dom.window,calls=[];
 w.fetch=async(url,opts)=>{
  const u=new URL(url);calls.push(u);let value;
  if(u.pathname==='/api/pipelines') value=[{pipeline_id:'e1bfe233-3918-407c-8822-2344387babc9',pipeline_name:'Logistics entrance'}];
  else if(u.pathname==='/api/watchlists') value={items:[watchlist],total:1,total_pages:1};
  else if(u.pathname.endsWith('/entries')) value={items:[{...entry,identity_id:u.searchParams.get('page')==='2'?'second':'joey',identity_name:u.searchParams.get('page')==='2'?'Second person':'Joey'}],total_pages:2};
  else if(u.pathname.endsWith('/stats')) value={active_entries:1,total_entries:3,expired_entries:1,unacknowledged_alerts:4};
  else if(u.pathname==='/api/watchlist-alerts') {if(options.failAlerts) return {ok:false,status:503,json:async()=>({detail:'Unavailable'})};value=options.withAlerts?[{identity_name:"Joey",pipeline_id:"e1bfe233-3918-407c-8822-2344387babc9",created_at:"2026-09-17T07:56:29Z"},{identity_name:"Joey",pipeline_id:"unknown-camera-uuid"}]:[];}
  else value=watchlist;
  return {ok:true,status:200,json:async()=>value};
 };
 w.eval(fs.readFileSync('frontend/js/admin-watchlists.js','utf8').replace(/\}\)\(\);\s*$/, 'window.probe={openDetailDrawer,closeDrawer,showDialog}; })();'));
 return {w,dom,calls};
}
test('overview and drawer explain real counts, entry eligibility and empty alert history',async()=>{
 const h=harness();await tick();await h.w.probe.openDetailDrawer('vip');await tick();
 const d=h.w.document;
 assert.match(d.querySelector('#watchlist-summary').textContent,/On this page/);
 assert.match(d.querySelector('#wl-entry-health').textContent,/4 alerts awaiting acknowledgement/);
 assert.match(d.querySelector('#drawer-entries').textContent,/Eligible/);
 assert.match(d.querySelector('#wl-recent-alerts').textContent,/No alerts recorded/);
 assert.equal(h.calls.find(u=>u.pathname.endsWith('/entries')).searchParams.get('include_expired'),'true');
 d.querySelector('.wl-load-more').click();await tick();
 assert.equal(d.querySelectorAll('.wl-entry-card').length,2);
 assert.match(d.querySelector('#drawer-entries').textContent,/Joey.*Second person/);
 h.dom.window.close();
});
test('an alert API failure is not shown as zero alerts',async()=>{
 const h=harness({failAlerts:true});await tick();await h.w.probe.openDetailDrawer('vip');await tick();
 assert.match(h.w.document.querySelector('#wl-recent-alerts').textContent,/could not be loaded/);
 h.dom.window.close();
});
test('closing the drawer clears it and invalidates its requests',async()=>{
 const h=harness();await tick();await h.w.probe.openDetailDrawer('vip');h.w.probe.closeDrawer();await tick();
 assert.equal(h.w.document.querySelector('.wl-drawer'),null);
 assert.notEqual(h.w.document.body.style.overflow,'hidden');h.dom.window.close();
});

test('recent alerts show camera names and never fall back to raw camera identifiers',async()=>{
 const h=harness({withAlerts:true});await tick();await h.w.probe.openDetailDrawer('vip');await tick();
 const text=h.w.document.querySelector('#wl-recent-alerts').textContent;
 assert.match(text,/Logistics entrance/);assert.match(text,/Camera name unavailable/);
 assert.doesNotMatch(text,/e1bfe233|unknown-camera-uuid/);h.dom.window.close();
});
