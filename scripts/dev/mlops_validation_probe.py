"""Offline ML-Ops browser regression. Uses Firefox WebDriver at WEBDRIVER_URL.
All API calls are fixtures; no application database or service is contacted.
Run: python3 scripts/dev/mlops_validation_probe.py
"""
import base64
import json
import os
import re
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / 'frontend'
OUT = Path(os.environ.get('ML_UI_TEST_OUTPUT', '/tmp/mlops-validation-ui'))
OUT.mkdir(parents=True, exist_ok=True)
DRIVER = os.environ.get('WEBDRIVER_URL', 'http://127.0.0.1:4445')

FIXTURE = r"""
window.fixtureErrors=[];window.addEventListener('error',e=>fixtureErrors.push(e.message));window.addEventListener('unhandledrejection',e=>fixtureErrors.push(String(e.reason)));
window.fixtureWrites=[];window.fixtureFailServices=false;window.fixtureJobs=[];window.fixtureModels=[];window.fixtureSelected={};
window.fixtureSpecs=[['behavior_anomaly_model','secintel-features-v2','behavior_anomaly_person','unsupervised','shadow'],['coappearance_anomaly_model','coappearance-features-v1','coappearance_pair','unsupervised','on_demand_shadow'],['social_graph_anomaly_model','social-graph-features-v1','social_graph_person','unsupervised','on_demand_shadow'],['threat_ranking_model','secintel-features-v2','threat_ranking_person_labeled','supervised','offline_ranking'],['tabular_regression_model','','behavior_anomaly_person','unsupervised','offline_regression']].map(([model_type,feature_set_version,dataset_definition,dataset_kind,serving_mode])=>({model_type,feature_set_version,dataset_definition,dataset_kind,serving_mode,algorithms:['isolation_forest'],default_algorithm:'isolation_forest',trainable:true}));
window.fixtureDatasets=fixtureSpecs.slice(0,4).map((s,i)=>({id:'dataset-'+i,name:'Dataset '+i,version:1,status:'built',row_count:100,parquet_sha256:'hash',file_present:true,definition_name:s.dataset_definition,feature_set_version:s.feature_set_version,kind:s.dataset_kind}));
window.fixtureMode='rules';window.fetch=async(input,options={})=>{
 const url=new URL(typeof input==='string'?input:input.url,location.href),p=url.pathname,method=options.method||'GET';
 const reply=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
 if(!p.startsWith('/api/'))return reply({});
 if(method!=='GET'){
  let b=JSON.parse(options.body||'{}');fixtureWrites.push({path:p,body:b,method});
  if(p==='/api/ml/training-jobs'){fixtureJobs=[{job_id:'guided-job',kind:'training',status:'running',progress_percent:30,details:{stage:'collect_features',model_type:b.model_type}}];return reply({job_id:'guided-job',status:'scheduled'});}
  if(p.includes('/services/')&&p.endsWith('/deploy')){fixtureSelected[p.split('/')[4]]=fixtureModels.find(m=>m.id===b.model_id);return reply({model:fixtureSelected[p.split('/')[4]]});}
  if(p.includes('/services/')&&p.endsWith('/rollback')){delete fixtureSelected[p.split('/')[4]];return reply({});}
  if(p==='/api/ml/config/mode'){fixtureMode=b.mode;return reply({mode:b.mode});}
  return reply({});
 }
 if(p==='/api/ml/services/status')return fixtureFailServices?reply({detail:{message:'Fixture service failure'}},503):reply({items:fixtureSpecs.map(s=>({model_type:s.model_type,service_id:s.model_type,destination:{name:'Security Intelligence / '+s.model_type,url:'/admin/security-intelligence'},serving_mode:s.serving_mode,state:fixtureSelected[s.model_type]?'observational':'not_configured',selected_model:fixtureSelected[s.model_type]||null,candidate:fixtureModels.find(m=>m.model_type===s.model_type)||null,training_readiness:{ready:true,blockers:[]},blockers:[],decision_mode:fixtureMode,allowed_modes:{shadow:{available:true}},consumption:{available:true,model_version:fixtureSelected[s.model_type]?.version,last_success_at:null,note:'No real requests recorded in this fixture.'},rollback:{available:!!fixtureSelected[s.model_type]}}))});
 if(p==='/api/ml/overview')return reply({mode:{current_mode:fixtureMode,modes:{}},model_types:fixtureSpecs,data_readiness:{feature_snapshots:764},label_readiness:{counted:{}},optional_capabilities:{}});
 if(p==='/api/ml/models')return reply({items:fixtureModels.filter(m=>!url.searchParams.get('model_type')||m.model_type===url.searchParams.get('model_type')),total:fixtureModels.length});
 if(p.startsWith('/api/ml/models/'))return reply(fixtureModels.find(m=>m.id===p.split('/')[4])||{});
 if(p==='/api/ml/datasets')return reply({items:fixtureDatasets,total:fixtureDatasets.length});
 if(p==='/api/ml/jobs')return reply({items:fixtureJobs,worker:{status:'healthy'}});
 if(p==='/api/ml/capabilities')return reply({items:{},limits:{}});
 if(p.startsWith('/api/settings/'))return reply({can_edit:false,key:p.split('/').pop(),value:false});
 return reply({items:[],total:0,definitions:[],enabled:false});
};
"""

class H(SimpleHTTPRequestHandler):
 def do_GET(self):
  path=self.path.split('?')[0]
  if path in ('/','/admin/ml-ops'):
   body=(ROOT/'admin/ml-ops.html').read_text();body=re.sub(r'<script src="/frontend/js/(actions|navbar-loader)[^"]*"[^>]*></script>','',body);body=body.replace('</head>','<script src="/fixture.js"></script></head>');body=body.replace('<div id="navbar-placeholder"></div>','<div id="navbar-placeholder" style="height:65px;flex-shrink:0;padding:20px;color:#00dc9a;background:#122236">ADMIN PANEL · ISOLATED INTERFACE TEST</div>');self.reply(body.encode(),'text/html');return
  if path=='/fixture.js':self.reply(FIXTURE.encode(),'text/javascript');return
  if path.startswith('/frontend/'):
   f=(ROOT/path[len('/frontend/'):]).resolve()
   if f.is_relative_to(ROOT) and f.is_file():
    body=f.read_bytes()
    if f.name=='admin-ml-ops.js':body=body.decode().replace("    window.addEventListener('pagehide', destroy);","    window.mlUiTest={state,serviceModels,compatibleDataset,selectedServiceContract,renderServiceJourney,loadModels,loadServices,refreshConsole,selectService,updateNextStep};\n    window.addEventListener('pagehide', destroy);").encode()
    self.reply(body,self.guess_type(str(f)));return
  self.send_error(404)
 def reply(self,b,t):self.send_response(200);self.send_header('Content-Type',t);self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def log_message(self,*a):pass

server = ThreadingHTTPServer(('127.0.0.1', 0), H)
threading.Thread(target=server.serve_forever, daemon=True).start()
def call(path, data=None, method=None):
    request = urllib.request.Request(DRIVER + path, data=json.dumps(data).encode() if data is not None else None,
        headers={'Content-Type': 'application/json'}, method=method)
    with urllib.request.urlopen(request, timeout=45) as response:
        value = json.load(response)['value']
    if isinstance(value, dict) and value.get('error'):
        raise RuntimeError(value)
    return value
sid = call('/session', {'capabilities': {'alwaysMatch': {'browserName': 'firefox',
    'moz:firefoxOptions': {'args': ['-headless']}}}})['sessionId']
path = '/session/' + sid
def js(code):
    return call(path + '/execute/sync', {'script': code, 'args': []})
def wait(code):
    for _ in range(100):
        if js(code): return
        time.sleep(.1)
    raise AssertionError('Timed out: ' + code + ' errors=' + str(js('return window.fixtureErrors')))
def screenshot(name):
    (OUT / name).write_bytes(base64.b64decode(call(path + '/screenshot')))
try:
    call(path + '/window/rect', {'width': 1440, 'height': 1000})
    call(path + '/url', {'url': f'http://127.0.0.1:{server.server_port}/admin/ml-ops'})
    wait("return window.mlUiTest && mlUiTest.state.servicesState === 'ready'")
    js("document.querySelector('[data-service-type=social_graph_anomaly_model]').click()")
    wait("return mlUiTest.state.selectedService === 'social_graph_anomaly_model' && mlUiTest.state.modelsFamily === 'social_graph_anomaly_model'")
    js("""
        window.failedReport = {passed:false,row_count:0,checks:{
            minimum_rows:{passed:false,current:0,required:10},
            max_null_rate:{passed:false,current:null,required:0.5,detail:'no rows'},
            no_duplicates:{passed:true,current:0,required:0}}};
        window.fixtureJobs=[{job_id:'blocked-graph',kind:'training',status:'failed',progress_percent:55,
            error_code:'DATASET_VALIDATION_FAILED',error_message:JSON.stringify(failedReport),
            details:{stage:'validation',model_type:'social_graph_anomaly_model',dataset_id:'dataset-2'}}];
        const originalFetch=window.fetch;
        window.validationDenied=false;
        window.fetch=async(input,options={})=>{
            const p=new URL(input,location.href).pathname;
            if(p==='/api/ml/datasets/dataset-2/validation-report') return new Response(JSON.stringify({validation_report:failedReport}),{status:validationDenied?403:200});
            if(p==='/api/ml/datasets/dataset-2') return new Response(JSON.stringify({...fixtureDatasets[2],status:'failed',row_count:0,quality_report:failedReport}));
            if(p==='/api/ml/datasets/dataset-2/explorer') return new Response(JSON.stringify({detail:{message:'No artifact was written because validation failed.'}}),{status:409});
            return originalFetch(input,options);
        };
        mlUiTest.refreshConsole();
    """)
    wait("return document.querySelector('#service-job-progress').textContent.includes('Blocked · data requirements')")
    assert js("return document.querySelector('#service-job-progress').textContent.includes('Model fitting did not start')")
    assert js("return document.querySelector('#service-job-progress .mlops-validation-table tbody tr').textContent.includes('At least 10')")
    assert js("return document.querySelectorAll('#service-job-progress .mlops-validation-table tbody tr[data-result=blocked]').length") == 2
    assert js("return [...document.querySelectorAll('#service-job-progress .mlops-training-track li')].map(n=>n.dataset.state)") == ['completed','blocked','not-started','not-started','not-started']
    js("document.querySelector('#service-job-progress').scrollIntoView({block:'center'})")
    screenshot('validation-desktop.png')
    print('PASS: failed data checks, actual/required values, fitting stopped, and correct stage tracking')
    js("fixtureJobs[0].error_message=JSON.stringify(failedReport).slice(0,50);mlUiTest.refreshConsole()")
    wait("return document.querySelector('#service-job-progress').textContent.includes('Detailed checks were not recorded')")
    js("[...document.querySelectorAll('#service-job-progress button')].find(n=>n.textContent==='Load complete validation report').click()")
    wait("return document.querySelectorAll('#service-job-progress .mlops-validation-table tbody tr').length === 3")
    js("window.validationDenied=true;[...document.querySelectorAll('#service-job-progress button')].find(n=>n.textContent==='Reload validation report').click()")
    wait("return document.querySelector('#service-job-progress').textContent.includes('Report unavailable · retry')")
    js('mlUiTest.refreshConsole()')
    wait("return document.querySelectorAll('#service-job-progress .mlops-validation-table tbody tr').length === 3")
    print('PASS: truncated historic report recovery, retained evidence after refresh, and permission failure with retry')
    js("[...document.querySelectorAll('#service-job-progress button')].find(n=>n.textContent==='Inspect dataset & records').click()")
    wait("return document.querySelector('#workflow-notice').textContent.includes('Dataset selected')")
    assert js("return document.querySelector('#mlops-evidence-browser').open && document.querySelector('#workflow-dataset').value==='dataset-2'")
    print('PASS: correct dataset selection opens the evidence browser')
    js("""
        const diag=MLOpsDiagnostics;
        const example={kind:'training',status:'failed',error_code:'TRAINING_FAILED',error_message:'Worker unavailable',details:{stage:'training'}};
        window.checkResults={
            runtime:diag.jobEvidence(example).blocked===false,
            cancelled:diag.jobEvidence({...example,status:'cancelled',error_code:'DATASET_VALIDATION_FAILED'}).blocked===false,
            unknown:diag.trainingPanel({...example,details:{}}).textContent.includes('Not recorded'),
            noFalseFit:!diag.trainingPanel(example).textContent.includes('Model fitting did not start'),
            noFalsePass:diag.validationReport({}).textContent.includes('missing evidence is not a pass'),
            info:diag.validationReport({checks:{max_null_rate:{passed:true,current:{rate:.8,enforced:false,feature:'x'},required:.5}}}).textContent.includes('Informational'),
            gate:diag.trainingPanel({...example,error_code:'QUALITY_GATES_FAILED',details:{stage:'preprocessing'},error_message:JSON.stringify({minimum_train_rows:{actual:0,required:20,passed:false}})}).textContent.includes('At least 20'),
            safe:diag.validationReport({checks:{'<img src=x onerror=alert(1)>':{passed:false,current:'<script>unsafe</script>',required:0}}}).querySelectorAll('img,script').length===0
        };
    """)
    results = js('return checkResults')
    assert all(results.values()), results
    print('PASS: runtime/cancelled/unknown states, informational missingness, split gates, safe text rendering')
    js("document.querySelector('[data-service-type=behavior_anomaly_model]').click()")
    wait("return mlUiTest.state.selectedService==='behavior_anomaly_model' && mlUiTest.state.modelsFamily==='behavior_anomaly_model'")
    assert js("return document.querySelector('#service-job-progress').hidden")
    js("document.querySelector('[data-service-type=social_graph_anomaly_model]').click()")
    wait("return !document.querySelector('#service-job-progress').hidden")
    call(path+'/window/rect',{'width':390,'height':844})
    js("document.querySelector('#mlops-evidence-browser').open=false;document.querySelector('#service-job-progress').scrollIntoView({block:'start'})")
    screenshot('validation-mobile.png')
    assert js('return document.documentElement.scrollWidth <= window.innerWidth + 1')
    assert js('return fixtureWrites.length') == 0
    assert js('return fixtureErrors') == []
    print('PASS: service isolation, mobile width, zero operational writes and zero JavaScript errors')
finally:
    call(path, method='DELETE')
    server.shutdown()
