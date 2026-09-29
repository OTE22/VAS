"""Execute exported service notebooks using real ipykernel, retaining every output.

Runs in the isolated Jupyter image. Input artifacts must be mounted read-only at
/validation-output/artifacts; /validation-output contains only disposable exports.
"""
import hashlib,json,time,traceback,os
from pathlib import Path
import nbformat
from nbclient import NotebookClient
from nbconvert import HTMLExporter

ROOT=Path('/validation-output')
FAMILIES=tuple(os.environ.get('VAS_VALIDATION_FAMILIES', 'behavior_anomaly_model,coappearance_anomaly_model,social_graph_anomaly_model,threat_ranking_model,tabular_regression_model').split(','))
report=[]
for family in FAMILIES:
    source=ROOT/'notebooks'/f'{family}.ipynb'
    record={'family':family,'kernel':'python3','synthetic_data':True,'cells':[]}
    if not source.exists():
        record.update(status='failed',error='Worker did not produce an export');report.append(record);continue
    book=nbformat.read(source,as_version=4)
    # Keep the archived bundle rerunnable without using production /artifacts.
    for cell in book.cells:
        if cell.cell_type=='code':
            cell.source=cell.source.replace('Path("/artifacts")', 'Path("../artifacts").resolve()').replace("Path('/artifacts')", 'Path("../artifacts").resolve()')
    book.cells.insert(0,nbformat.v4.new_markdown_cell('# Executed service validation\n'
        '**Synthetic isolated validation, not production accuracy evidence.**\n'
        'The worker genuinely collected features, built a dataset, fitted/evaluated the model and exercised its connection. '
        'This notebook executes the exported evidence and offline rechecks cell by cell in the same Jupyter runtime used by the application.'))
    book.cells.append(nbformat.v4.new_markdown_cell('## Final independent checks\nFail this notebook if required worker evidence or any offline preprocessing check is missing.'))
    book.cells.append(nbformat.v4.new_code_cell('''assert evidence['job']['status'] == 'completed', 'Worker training did not complete'
assert model['model_type'] == pipeline['model_type'], 'Model family mismatch'
assert dataset['status'] == 'built' and dataset['row_count'] > 0, 'No built dataset'
assert rechecked['passed'], 'Dataset validation failed'
assert matrix_recheck['status'] == 'matched', 'Preprocessing does not reproduce the recorded contract'
assert model['training_config']['engineering_gate'] == 'PASS', 'Engineering gate failed'
assert model['code_version'], 'Training code revision was not recorded'
assert training.get('stage_history'), 'Training stage history missing'
assert all(matrices[name].shape[0] > 0 for name in ('train','val','test')), 'A declared split is empty'
assert all(matrices[name].shape[1] == len(model['feature_names']) for name in matrices), 'Feature dimension mismatch'
if pipeline['model_type'] == 'tabular_regression_model':
    assert pipeline['service']['destination']['mode'] == 'offline_only' and pipeline['service']['selected_model'] is None, 'Regression unexpectedly has live deployment'
else:
    assert pipeline['service']['selected_model']['id'] == model['id'], 'Consumer is bound to a different model'
print('PASS: worker outcome, family, immutable dataset, validation, split matrices, engineering, code revision, stage history and deployment scope')
print('Scientific status:', model['training_config'].get('scientific_gate', 'not recorded'))
print('Synthetic validation does not establish real-world accuracy, scientific approval or 30-camera throughput.')
'''))
    book.cells.append(nbformat.v4.new_markdown_cell('## Isolated worker and service lifecycle result\nThe real worker ran before this notebook. Read its recorded outcome, including scoring and stopping the selected model.'))
    book.cells.append(nbformat.v4.new_code_cell("""worker_result = json.loads((Path('..') / (pipeline['model_type'] + '-result.json')).read_text())
assert worker_result['status'] == 'passed', worker_result
assert worker_result['engineering'] == 'PASS', worker_result
assert worker_result['model_id'] == model['id'], 'Worker evidence belongs to another model'
assert worker_result['code_version'] == model['code_version'], 'Worker release differs from notebook evidence'
if pipeline['model_type'] == 'tabular_regression_model':
    assert worker_result['expected_offline_refusal'] == 'OFFLINE_ONLY'
else:
    assert worker_result['deployment']['success'] and worker_result['stop']['success']
    print('Real consumer result:')
    pprint(worker_result['consumer'])
    print('Stop result:')
    pprint(worker_result['stop'])
print('PASS: isolated training, artifact reload, deployment scope, actual consumer scoring and stop checks')
"""))
    if family == 'social_graph_anomaly_model' and (ROOT/'SYNTHETIC-drift-results.json').exists():
        book.cells.append(nbformat.v4.new_markdown_cell('## Synthetic drift reporting check\nMonitoring samples are generated only in the disposable database. Current node weights were doubled deliberately.'))
        book.cells.append(nbformat.v4.new_code_cell("""drift = json.loads((Path('..') / 'SYNTHETIC-drift-results.json').read_text())
assert drift['synthetic'] is True and drift['model_id'] == model['id']
assert not drift['data_drift']['insufficient_data']
assert drift['prediction_drift']['insufficient_data'] is True
assert drift['prediction_drift']['metrics']['unavailable_reason'] == 'PERSISTED_SCORE_TELEMETRY_UNAVAILABLE_FOR_MODEL_FAMILY'
assert drift['data_drift']['metrics']['worst_psi'] > 0
pprint(drift)
print('PASS: feature drift is assessed; unsupported persistent graph-score monitoring is reported explicitly.')
print('Offline synthetic score PSI:', drift['offline_score_psi'])
"""))
    headings={};heading='Setup'
    for i,c in enumerate(book.cells):
        if c.cell_type=='markdown': heading=c.source.splitlines()[0] if c.source else heading
        headings[i]=heading
    started={}
    def before(cell,cell_index,**kwargs):
        started[cell_index]=time.monotonic()
        print(f'{family} cell {cell_index+1}/{len(book.cells)} {headings[cell_index]}',flush=True)
    def after(cell,cell_index,**kwargs):
        outputs=list(cell.get('outputs',[]))
        errors=[o for o in outputs if o.output_type=='error']
        record['cells'].append({'cell_index':cell_index,'heading':headings[cell_index],
            'execution_count':cell.get('execution_count'),'seconds':round(time.monotonic()-started[cell_index],3),
            'status':'failed' if errors else 'passed','output_types':[o.output_type for o in outputs],
            'outputs_sha256':hashlib.sha256(json.dumps(outputs,sort_keys=True).encode()).hexdigest(),
            'errors':[{'name':e.ename,'value':e.evalue} for e in errors]})
    begun=time.monotonic()
    try:
        NotebookClient(book,timeout=120,kernel_name='python3',resources={'metadata':{'path':str(ROOT/'notebooks')}},
            on_cell_execute=before,on_cell_executed=after).execute()
        record['status']='passed'
    except Exception as exc:
        record.update(status='failed',error=str(exc),exception=type(exc).__name__)
    record['seconds']=round(time.monotonic()-begun,3)
    record['executed_code_cells']=sum(c.cell_type=='code' and c.execution_count is not None for c in book.cells)
    record['total_code_cells']=sum(c.cell_type=='code' for c in book.cells)
    nbformat.write(book,ROOT/'notebooks'/f'{family}-executed.ipynb')
    body,_=HTMLExporter().from_notebook_node(book)
    (ROOT/'notebooks'/f'{family}-executed.html').write_text(body)
    report.append(record)
    print('RESULT',family,record['status'],record['executed_code_cells'],record['total_code_cells'],flush=True)
(ROOT/'cell-execution-report.json').write_text(json.dumps(report,indent=2))
print('FINISHED:',sum(r['status']=='passed' for r in report),'/',len(report),'notebooks',flush=True)
raise SystemExit(0 if all(r['status']=='passed' for r in report) else 1)
