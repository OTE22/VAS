/* Guided view over the existing ML console. All mutations stay in its forms. */
(function () {
    'use strict';
    window.MLOpsWorkflow = function (host) {
        const { api, beginRequest, el, kvList, simpleTable, jsonBlock, formatMetric: metric,
            formatActionError, state, activateWorkspace, openDatasetDetail, loadModelDetail } = host;
        const node = id => document.getElementById(id);
        const stages = ['Dataset', 'Validation', 'Preprocessing', 'Feature Engineering', 'Training', 'Evaluation', 'Model Registration / Deployment'];
        let dataset = null, model = null, explorer = null, selectedStage = 0, page = 1;
        let selectedDataset = '', selectedModel = '', selectedJobId = '', signature = '', selectionGeneration = 0;
        let datasetLoaded = false, modelsLoaded = false;
        const loadErrors = new Map();
        const filters = { split: '', label: '', q: '' };
        const text = value => value === null || value === undefined ? 'Not recorded' : typeof value === 'object' ? JSON.stringify(value) : String(value);
        const metricLabel = key => ({ rows: 'Rows', positive: 'Positive labels', negative: 'Negative labels',
            roc_auc: 'ROC AUC', average_precision: 'Average precision', score_p50: 'Median score',
            score_p90: '90th percentile score', score_p99: '99th percentile score' }[key] || key.replaceAll('_', ' '));
        const metricValue = (key, value) => metric(value, ['rows', 'positive', 'negative'].includes(key) ? 0 : 4);
        function button(label, action) {
            const b = el('button', 'mlops-btn', label); b.type = 'button'; b.addEventListener('click', action); return b;
        }
        function detail(title, value) {
            const d = el('details', 'workflow-evidence'); d.append(el('summary', null, title), jsonBlock(value)); return d;
        }
        function message(value) { node('workflow-notice').textContent = value; }
        function go(view, target) {
            activateWorkspace(view, false);
            const control = node(target);
            if (control) { control.scrollIntoView({ block: 'center', behavior: 'smooth' }); control.focus(); }
        }
        function trainingJob() {
            if (selectedJobId) return state.recentJobs.find(j => j.job_id === selectedJobId) || null;
            return state.recentJobs.find(j => j.kind === 'training' && (model
                ? j.job_id === model.training_job_id
                : selectedDataset && ((j.details || {}).dataset_id === selectedDataset || (j.result || {}).dataset_id === selectedDataset))) || null;
        }
        function stageStatus(index) {
            const job = trainingJob(), report = dataset && dataset.quality_report;
            const stageNames = [['loading_dataset', 'building_dataset'], ['validation'], ['preprocessing'], ['feature_engineering'], ['training'], ['evaluating'], ['saving_candidate', 'registering']];
            const current = job && (job.details || {}).stage;
            if (job && stageNames[index].includes(current)) return text(job.status);
            const history = job && (job.details || {}).stage_history || [];
            if (history.some(event => stageNames[index].includes(event.stage) && typeof event.duration_seconds === 'number')) return 'Completed';
            if (index === 0) return dataset ? text(dataset.status) : 'Choose data';
            if (index === 1) return report ? report.passed === true ? 'Passed' : report.passed === false ? 'Failed' : 'Not recorded' : 'Not recorded';
            if (index === 2 || index === 3) return model && model.training_config ? 'Recorded in model' : 'Runs during training';
            if (index === 4) return job ? text(job.status) : model ? 'Completed' : 'Not started';
            if (index === 5) return model && model.evaluation_report ? 'Results available' : 'Not recorded';
            return model ? text(model.stage) : 'Not registered';
        }
        function renderPipeline() {
            const list = node('workflow-pipeline');
            if (!list.children.length) stages.forEach((title, index) => {
                const li = el('li'), b = button('', () => {
                    selectedStage = index; node('workflow-summary').hidden = true;
                    node('workflow-stage').hidden = false; renderPipeline(); renderStage(); node('workflow-stage').focus();
                });
                b.append(el('span', 'workflow-number', String(index + 1)), el('strong', null, title), el('small'));
                li.append(b); list.append(li);
            });
            [...list.querySelectorAll('button')].forEach((b, index) => {
                b.setAttribute('aria-pressed', String(index === selectedStage));
                b.querySelector('small').textContent = stageStatus(index);
            });
        }
        function duration(seconds) {
            return Number.isFinite(seconds) ? Math.floor(Math.max(0, seconds) / 60) + 'm ' + Math.floor(Math.max(0, seconds) % 60) + 's' : 'Not recorded';
        }
        function renderProgress() {
            const area = node('workflow-progress'), job = trainingJob();
            area.replaceChildren();
            if (!job) { area.append(el('p', null, model ? 'Saved experiment selected. Open Work in progress below for other runs.' : 'Select a dataset to follow its training job. All jobs remain available in Overview.')); return; }
            const active = ['running', 'scheduled'].includes(job.status), d = job.details || {};
            const start = job.started_at ? Date.parse(/Z$|[+-]\d\d:\d\d$/.test(job.started_at) ? job.started_at : job.started_at + 'Z') : NaN;
            const elapsed = active ? (Date.now() - start) / 1000 : job.duration_seconds;
            const percent = typeof job.progress_percent === 'number' ? Math.max(0, Math.min(100, job.progress_percent)) : null;
            area.append(el('h3', null, 'Training progress · ' + text(active ? d.stage || job.status : job.status)));
            const bar = el('progress'); bar.max = 100; bar.setAttribute('aria-label', 'Training completion');
            if (percent !== null) bar.value = percent;
            area.append(bar, kvList([
                ['Completion', percent === null ? 'Waiting for worker' : percent + '%'], ['Elapsed', duration(elapsed)],
                ['Estimated remaining', active && percent > 0 && percent < 100 && Number.isFinite(elapsed)
                    ? duration(elapsed * (100 - percent) / percent) + ' (rough estimate from reported progress)' : active ? 'Estimating after progress is reported' : job.status === 'completed' ? 'Finished' : 'Stopped'],
                ['Resource snapshot', d.resource_usage ? 'Process memory: ' + metric(d.resource_usage.memory_mb, 1) + ' MB · CPU time: ' + metric(d.resource_usage.cpu_seconds, 2) + ' seconds · sampled at ' + text(d.resource_usage.sampled_at) + '. GPU usage is not reported.' : 'Worker does not report CPU, memory or GPU usage for this run.'],
                ['Last worker message', text(job.progress_message)], ['Job', job.job_id]
            ]));
            if (job.error_message) area.append(el('p', 'mlops-note note-bad', text(job.error_message) + ' Fix the reported cause, then review configuration and submit a new run.'));
        }
        function readyText() {
            if (!model) return 'No model selected. Train and evaluate a candidate first.';
            const report = model.evaluation_report || {};
            const eng = (report.engineering_gate || {}).status, sci = (report.scientific_gate || {}).status;
            if (model.model_type === 'tabular_regression_model' || model.model_type === 'threat_ranking_model') return 'Registry: ' + text(model.stage) + '. Offline use only. Review held-out metrics and lineage before approving this artifact. Live security deployment remains gated.';
            return 'Registry: ' + text(model.stage) + '. Engineering gate: ' + text(eng) + '. Scientific gate: ' + text(sci)
                + '. Live ML deployment remains gated. Shadow observation requires explicit administrator approval.';
        }
        function summary() {
            const area = node('workflow-summary'), config = model && model.training_config;
            area.replaceChildren(el('h3', null, 'Run summary'), kvList([
                ['What data was used?', dataset ? dataset.name + ' v' + dataset.version + ' · ' + metric(dataset.row_count) + ' rows · checksum ' + text(dataset.checksum) : 'No dataset selected'],
                ['What transformations were applied?', config ? 'Preprocessor ' + text(config.preprocessor_version) + '; feature set ' + text(config.feature_set_version) + '; coverage floor ' + text(config.feature_coverage_floor) : 'No saved preprocessing configuration yet'],
                ['Which model was trained?', model ? text(model.algorithm) + ' v' + model.version + ' · seed ' + text(model.seed) : 'No model selected'],
                ['How well did it perform?', model && model.evaluation_report ? performanceText() : 'Evaluation not recorded'],
                ['Is it ready for deployment?', readyText()]
            ]));
            if (model) area.append(button('Inspect evaluation results', () => node('workflow-pipeline').querySelectorAll('button')[5].click()));
            area.append(button('Return to selected stage', () => { area.hidden = true; node('workflow-stage').hidden = false; node('workflow-stage').focus(); }));
        }
        function performanceText() {
            const report = model.evaluation_report || {}, test = (report.splits || {}).test || {};
            const measured = Object.entries(test).filter(([, value]) => typeof value === 'number').map(([key, value]) => metricLabel(key) + ': ' + metricValue(key, value)).join(' · ');
            return (measured ? 'Held-out test · ' + measured : 'No held-out test metrics recorded') + '. Scores are not threat probabilities.';
        }
        function trainingCurve(points) {
            const rows = Array.isArray(points) ? points.filter(p => Number.isFinite(p.iteration) && Number.isFinite(p.training_loss)) : [];
            if (!rows.length) return jsonBlock(points);
            const box = el('div'), ns = 'http://www.w3.org/2000/svg';
            const svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', '0 0 500 180');
            svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'Training loss by iteration; exact values in the table below.');
            const losses = rows.map(p => p.training_loss), lo = Math.min(...losses), hi = Math.max(...losses);
            const first = rows[0].iteration, last = rows[rows.length - 1].iteration;
            const line = document.createElementNS(ns, 'polyline');
            line.setAttribute('points', rows.map(p => (20 + 460 * (p.iteration - first) / Math.max(1, last - first)) + ',' + (150 - 130 * (p.training_loss - lo) / Math.max(1e-12, hi - lo))).join(' '));
            line.setAttribute('fill', 'none'); line.setAttribute('stroke', '#7dd3fc'); line.setAttribute('stroke-width', '3');
            svg.append(line); box.append(svg, el('p', null, 'Iteration ' + first + ' → ' + last + ' · loss ' + metric(losses[0], 4) + ' → ' + metric(losses[losses.length - 1], 4)));
            const table = el('details'); table.append(el('summary', null, 'Exact curve values'), simpleTable(['Iteration', 'Training loss', 'Validation loss'], rows.map(p => [String(p.iteration), metric(p.training_loss, 6), metric(p.validation_loss, 6)]))); box.append(table);
            return box;
        }
        function results() {
            const area = el('div', 'workflow-results'), report = model.evaluation_report || {};
            area.append(el('h3', null, 'Results dashboard'));
            const guide = el('details', 'workflow-evidence');
            guide.append(el('summary', null, 'How to read these metrics and charts'), kvList([
                ['Train / validation / test', 'Train fits the model. Validation selects settings. Test checks the chosen model on held-out records. Compare the same split and dataset version.'],
                ['MAE and RMSE', 'Regression errors in target units; lower is better. RMSE penalizes larger mistakes more. For example, MAE 3 for a count target means an average absolute error of 3 counts.'],
                ['R squared (R2)', 'Compares squared error with a constant test-mean reference. 1 indicates perfect predictions; 0 matches that reference; a negative value is worse.'],
                ['ROC AUC / average precision', 'Higher means better ranking of reviewed positive versus negative labels. ROC AUC 0.5 is chance-level ranking. Average precision depends on class balance, so compare the same data.'],
                ['Score percentiles', 'The 90th percentile is the score at or below which 90% of records fall. It describes the score distribution; it is not 90% accuracy.'],
                ['Confusion matrix', 'Rows are actual classes and columns are predicted classes at the recorded threshold. Diagonal cells are correct predictions; other cells are mistakes. The costs of false positives and false negatives can differ.'],
                ['Feature importance / SHAP', 'Importance summarizes which inputs influenced the model. SHAP adds signed contributions for one prediction. Neither proves that a feature causes an outcome.'],
                ['Training curves', 'Read loss against iteration. Lower loss is usually better. Falling training loss with rising validation loss can indicate overfitting; consult the held-out metrics too.'],
                ['Baseline', 'A reference to beat: the recorded comparison may use a simple constant prediction or an incumbent model. Check which baseline is shown and compare identical data.']
            ])); area.append(guide);
            const splits = report.splits || (model.metrics && model.metrics.evaluation || {}).splits || {};
            const names = [...new Set(Object.values(splits).flatMap(v => Object.keys(v).filter(k => typeof v[k] === 'number')))];
            if (names.length) area.append(simpleTable(['Split', ...names.map(metricLabel)], Object.entries(splits).map(([name, values]) => [name, ...names.map(k => metricValue(k, values[k]))])));
            else area.append(el('p', null, 'No measured split metrics were saved for this model.'));
            if (model.model_type === 'tabular_regression_model') area.append(el('p', null, 'Lower MAE and RMSE mean smaller errors in target units. R? compares predictions to a constant reference and can be negative. Compare the recorded training-mean baseline on the same held-out split.'));
            else area.append(el('p', null, 'Validation helps choose the model; test results describe held-out data. Score percentiles summarize unusualness, not classification accuracy. Higher ranking metrics such as ROC AUC and average precision indicate better ranking on the labelled split.'));
            for (const [title, value, why] of [
                ['Confusion matrix', report.confusion_matrix, 'Not recorded. A confusion matrix requires labelled outcomes and a defined decision threshold; anomaly bands are not ground-truth classes.'],
                ['Feature importance', report.feature_importance, 'Not recorded for this run. Feature coverage is not predictive importance.'],
                ['Training curves', report.training_curves, 'No learning curve was recorded for this run. Isolation Forest and MAD do not emit per-epoch training loss.'],
                ['Baseline comparison', report.incumbent_comparison || report.baseline, 'No incumbent comparison was recorded. Train a baseline on the same dataset version for a meaningful comparison.']
            ]) {
                const card = el('section', 'workflow-result'); card.append(el('h4', null, title));
                if (title === 'Confusion matrix' && Array.isArray(value)) {
                    const labels = (report.confusion_matrix_context || {}).labels || value.map((_, i) => String(i));
                    card.append(simpleTable(['Actual / predicted', ...labels], value.map((r, i) => [labels[i], ...r.map(text)])));
                    card.append(el('p', null, text((report.confusion_matrix_context || {}).meaning)), el('p', null, 'Threshold: ' + text((report.confusion_matrix_context || {}).threshold)));
                }
                else if (title === 'Feature importance' && value && !Array.isArray(value)) {
                    const entries = Object.entries(value).filter(([, n]) => typeof n === 'number' && Number.isFinite(n));
                    const max = Math.max(1e-9, ...entries.map(([, n]) => Math.abs(n)));
                    for (const [name, n] of entries) { const line = el('div'); const bar = el('meter'); bar.min = 0; bar.max = max; bar.value = Math.abs(n); bar.setAttribute('aria-label', name); line.append(el('span', null, name + ' · ' + metric(n, 4)), bar); card.append(line); }
                    if (report.feature_importance_method) card.append(el('p', null, report.feature_importance_method));
                } else if (title === 'Training curves' && value) {
                    card.append(trainingCurve(value), el('p', null, text(report.training_curves_context)));
                } else if (title === 'Baseline comparison' && value && value.strategy) {
                    card.append(el('p', null, value.strategy + ': the reference predicts ' + metric(value.reference_value, 4) + ' for every row. Compare its measured results with the candidate on the same split.'));
                    if (model.model_type === 'tabular_regression_model') card.append(simpleTable(['Split', 'Model MAE', 'Baseline MAE', 'Model RMSE', 'Baseline RMSE'], Object.entries(splits).map(([split, v]) => [split, metric(v.mae, 4), metric(v.baseline_mae, 4), metric(v.rmse, 4), metric(v.baseline_rmse, 4)])));
                    else card.append(simpleTable(['Split', 'Model ROC AUC', 'Baseline ROC AUC', 'Model average precision', 'Baseline average precision'], Object.entries(value.splits || {}).map(([split, v]) => [split, metric((splits[split] || {}).roc_auc, 4), metric(v.roc_auc, 4), metric((splits[split] || {}).average_precision, 4), metric(v.average_precision, 4)])));
                } else if (title === 'Baseline comparison' && value) {
                    card.append(el('p', null, 'Comparison: ' + text(value.status) + '. ' + text(value.note || value.reason)));
                    card.append(el('p', null, 'The baseline here is the incumbent shadow model: ' + text((value.incumbent || {}).algorithm) + '. Comparison does not approve deployment.'));
                    const comparisons = [];
                    for (const [split, data] of Object.entries(value.splits || {})) {
                        for (const key of ['latency_ms_per_row', 'failure_rate']) {
                            if (data[key]) comparisons.push([split + ' · ' + key, metric(data[key].candidate, 4), metric(data[key].incumbent, 4)]);
                        }
                        const quantiles = data.score_quantiles || {};
                        for (const key of Object.keys(quantiles.candidate || {})) comparisons.push([split + ' · score ' + key, metric(quantiles.candidate[key], 4), metric((quantiles.incumbent || {})[key], 4)]);
                    }
                    if (comparisons.length) card.append(simpleTable(['Measure', 'Candidate', 'Incumbent'], comparisons));
                    card.append(detail('Complete comparison', value));
                } else if (value) card.append(jsonBlock(value));
                else card.append(el('p', null, why));
                area.append(card);
            }
            area.append(detail('All measured metrics and evaluation evidence', report));
            if (host.platformResults) { const extra = host.platformResults(model); if (extra) area.append(extra); }
            return area;
        }
        // Instructional copy only: status and actions remain driven by existing evidence.
        const stageGuides = [
            [
                'A dataset version is a frozen collection of feature records. A row describes an entity at a particular time; columns hold measurements such as appearance counts.',
                'Choose an existing version above. Check its source, dates, row count and sample records. If none exists, use Prepare data to collect features and build a dataset.',
                'Continue to Validation when the selected data represents the population and time period you want to study. Keep the same version when comparing algorithms.',
                'If the list is empty, check data readiness and the dataset build job. If a file is missing, inspect the error and build a new version; editing sample filters will not repair it.'
            ], [
                'Validation checks whether the data can be used: known features, numeric values, allowed ranges, missingness, duplicates and applicable label/leakage rules.',
                'Read the saved validation report and inspect the failed checks. Class distribution shows how many reviewed examples belong to each class; a rare class may be poorly represented.',
                'A passed data check allows you to continue configuring training. It does not show that the future model will be accurate. Training verifies the snapshot and applies its own quality gates again.',
                'Correct the source or collection configuration, then build a new dataset version. Download the report when asking an administrator to investigate. Partial explorer counts describe only the scanned rows.'
            ], [
                'Preprocessing turns the selected records into a numeric matrix the model can learn from. Missing predictor values use medians learned from the training split.',
                'Inspect missing values and the saved transformation configuration. For regression, the target is separated from predictors before fitting.',
                'Continue when you understand which features are usable and how missing values are handled. A saved model retains these rules so later predictions use the same preprocessing.',
                'If too few features are usable, improve feature collection or select appropriate predictors and start a new run. Filling missing source values with arbitrary zeros can change their meaning.'
            ], [
                'Features are the measurements supplied to the model. This platform collects feature snapshots before building the dataset; this stage shows the feature selection and matrix used for training.',
                'Choose predictors in a saved pipeline, or leave the list empty to use eligible features. Exclude the regression target and any measurements that reveal an outcome unavailable at prediction time.',
                'Continue when the predictor list and feature version match your question. The saved feature schema lets you identify the exact inputs used by a model.',
                'An unknown feature means the name is absent from this dataset version. Check the explorer schema and correct the configuration, or collect the required features in a new version.'
            ], [
                'Training fits an algorithm to the training split. Validation data helps select settings; the held-out test split is reserved for evaluation.',
                'Open Configure training. For a first run, choose an available model contract, use the default parameters and seed, and leave Optuna and SHAP off. Regression also requires a saved pipeline with an explicit numeric target.',
                'Start one run and follow its active stage and worker messages. A completed run creates a model record for review; inspect its quality gates and evaluation before any approval.',
                'A scheduled job is waiting for a worker. A failed job needs its error resolved before a new run. Progress estimates are approximate; CPU fallback alone does not mean the run failed.'
            ], [
                'Evaluation measures how the fitted model behaves on each data split. Test results are the main check on data that was not used to fit or select parameters.',
                'Compare held-out results with the baseline and models trained on the same dataset and target. Read the metric definitions below and inspect the mistakes that matter to your use case.',
                'Proceed to registration review only when the evidence supports your intended use. Good training scores alone are insufficient; missing metrics mean evidence is absent, not zero.',
                'If training looks good but validation or test results are much worse, investigate overfitting or a change in the data. Review features or parameters and compare a new run; keep the test set out of tuning.'
            ], [
                'Registration gives the saved model artifact a version and links it to its data, configuration and evaluation. Deployment determines whether and how that model is used.',
                'Review the lineage, checksum, quality gates and intended use. An administrator records the reason for approval. Check MLflow synchronization separately from the local registry stage.',
                'A validated candidate still needs review. Approved offline models are for offline use; approved shadow models observe alongside rules. Neither status automatically grants live decision authority.',
                'If MLflow synchronization fails, restore integration access and retry synchronization; retraining is unnecessary when the local artifact is intact. If an artifact checksum fails, investigate that artifact before requesting approval.'
            ]
        ];
        function stageGuidance() {
            const box = el('aside', 'workflow-evidence workflow-instructions');
            box.setAttribute('aria-label', stages[selectedStage] + ' guidance');
            const labels = ['What happens here', 'What to do', 'When to continue', 'If you are stuck'];
            stageGuides[selectedStage].forEach((copy, index) => {
                const paragraph = el('p'); paragraph.append(el('strong', null, labels[index] + ': '), document.createTextNode(copy)); box.append(paragraph);
            });
            return box;
        }
        function renderStage() {
            const area = node('workflow-stage'), config = model && model.training_config, job = trainingJob();
            area.replaceChildren();
            const heading = el('h3', null, stages[selectedStage]); heading.id = 'workflow-stage-title'; area.append(heading, stageGuidance());
            const inputs = [dataset && dataset.definition_name, dataset && dataset.checksum, dataset && dataset.feature_set_version,
                config && config.preprocessor_version, dataset && dataset.id, model && model.artifact_name, model && model.id];
            const outputs = [dataset && dataset.id, dataset && dataset.quality_report, config && config.preprocessor_version,
                config && config.feature_schema_hash, model && model.artifact_name, model && model.evaluation_report, model && model.stage];
            const stageNames = [['loading_dataset', 'building_dataset'], ['validation'], ['preprocessing'], ['feature_engineering'], ['training'], ['evaluating'], ['saving_candidate', 'registering']][selectedStage];
            const events = ((job || {}).details || {}).stage_history || [];
            const measured = events.filter(e => stageNames.includes(e.stage) && typeof e.duration_seconds === 'number');
            area.append(kvList([['Status', stageStatus(selectedStage)], ['Input', text(inputs[selectedStage])],
                ['Output', typeof outputs[selectedStage] === 'object' && outputs[selectedStage] ? 'See evidence below' : text(outputs[selectedStage])],
                ['Stage duration', measured.length ? duration(measured.reduce((sum, e) => sum + e.duration_seconds, 0)) : 'Not recorded separately; total run duration is shown above.']]));
            area.append(detail('Stage configuration', selectedStage <= 1 ? { extraction: dataset && dataset.extraction, split: dataset && dataset.split_config } : config || 'No resolved configuration recorded'));
            if (selectedStage <= 1) renderExplorer(area);
            if (selectedStage === 2 || selectedStage === 3) {
                area.append(el('p', null, selectedStage === 2
                    ? 'Training selects covered numeric features and fills missing values using medians learned only from the training split. Inference uses the same saved preprocessor.'
                    : 'Feature snapshots are collected before dataset construction. This stage shows the feature contract used during training; it does not recompute historical snapshots.'));
                area.append(detail('Configuration and transformations as run', config || { status: 'Train a model to record the resolved configuration.' }));
                if (dataset) area.append(detail('Feature limitations and split configuration', { limitations: dataset.feature_set_limitations, split: dataset.split_config }));
            }
            if (selectedStage === 4) {
                area.append(detail('Training configuration', config || (job || {}).payload || { status: 'Choose defaults in the training form.' }));
                area.append(button(job && ['failed', 'cancelled'].includes(job.status) ? 'Review configuration and retry training' : 'Configure training', () => {
                    go('prepare', 'training-dataset-select');
                    if (selectedDataset) node('training-dataset-select').value = selectedDataset;
                    if (config) {
                        node('training-model-type').value = model.model_type; node('training-model-type').dispatchEvent(new Event('change'));
                        node('training-algorithm').value = config.algorithm;
                        node('training-seed-input').value = config.seed;
                        node('training-hyperparameters-input').value = JSON.stringify(config.hyperparameters || {});
                    }
                }));
            }
            if (selectedStage === 5) {
                if (model) area.append(results());
                else area.append(el('p', null, 'Select a saved model to inspect its evaluation.'));
                area.append(button('Open evaluation tools', () => go('review', 'model-evaluation')));
            }
            if (selectedStage === 6) {
                area.append(el('p', null, readyText()));
                const chain = el('ol', 'workflow-lineage');
                for (const [title, value] of [['Dataset version', dataset ? dataset.name + ' v' + dataset.version + ' · ' + dataset.id : null],
                    ['Preprocessing configuration', config], ['Experiment', model && model.training_job_id],
                    ['Model artifact', model && { name: model.artifact_name, sha256: model.artifact_hash }],
                    ['Evaluation result', model && model.evaluation_report], ['Deployment status', model && { stage: model.stage, shadow_approval: model.shadow_approval }]]) {
                    const li = el('li'); li.append(detail(title, value || 'Not recorded')); chain.append(li);
                }
                area.append(chain, button('Review registration and deployment controls', () => go('review', 'models-table-body')));
                if (model) { area.append(button('Full model record', () => loadModelDetail(model.id))); if (host.platformResults) { const extra = host.platformResults(model); if (extra) area.append(extra); } }
            }
            area.append(detail('Logs and errors for the selected experiment', job ? {
                job_id: job.job_id, stage: (job.details || {}).stage, message: job.progress_message,
                error_code: job.error_code, error_message: job.error_message, request_id: job.request_id, result: job.result, stage_history: (job.details || {}).stage_history
            } : { status: 'No matching job in the recent queue. Open Audit for historical call records.' }));
            area.append(button('Open logs and recovery details', () => go('audit', 'calls-body')));
            const nav = el('div', 'mlops-workflow-controls');
            if (selectedStage > 0) nav.append(button('Previous stage', () => node('workflow-pipeline').querySelectorAll('button')[selectedStage - 1].click()));
            if (selectedStage < stages.length - 1) nav.append(button('Next: ' + stages[selectedStage + 1], () => node('workflow-pipeline').querySelectorAll('button')[selectedStage + 1].click()));
            area.append(nav);
        }
        function renderExplorer(area) {
            if (!dataset) { area.append(el('p', null, 'Choose a dataset version above, or build your first dataset.'), button('Prepare data', () => go('prepare', 'build-dataset-btn'))); return; }
            area.append(kvList([['Source', text(dataset.definition_name || 'Feature snapshots')], ['Version', text(dataset.version)], ['Rows', metric(dataset.row_count)], ['Feature set', text(dataset.feature_set_version)]]));
            area.append(button('Full dataset record', () => openDatasetDetail(dataset.id)), button('Download validation report', async event => {
                const b = event.currentTarget; b.disabled = true;
                try {
                    const report = await api('/api/ml/datasets/' + encodeURIComponent(dataset.id) + '/validation-report');
                    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }));
                    const a = el('a'); a.href = url; a.download = 'validation-' + report.dataset_id + '.json'; document.body.append(a); a.click(); a.remove();
                    window.setTimeout(() => URL.revokeObjectURL(url), 1000); message('Validation report downloaded.');
                } catch (err) { message(formatActionError('Download failed', err)); } finally { b.disabled = false; }
            }));
            if (dataset.quality_report) area.append(detail('Saved validation report (build authority)', dataset.quality_report));
            area.append(button('Build a new dataset version', () => go('prepare', 'build-dataset-btn')));
            area.append(el('p', null, 'Explorer filters change the records displayed here. They do not change the saved dataset, its validation report or the rows used for training. To change training data, build a new version.'));
            const form = el('form', 'mlops-workflow-controls');
            for (const [name, options] of [['split', ['', 'train', 'val', 'test']], ['label', ['', 'positive', 'negative', 'unknown', 'unlabelled']]]) {
                const label = el('label', null, name === 'split' ? 'Split' : 'Class'), select = el('select'); select.name = name;
                options.forEach(value => { const o = el('option', null, value || 'All'); o.value = value; select.append(o); }); select.value = filters[name]; label.append(select); form.append(label);
            }
            const searchLabel = el('label', null, 'Search sample records'), search = el('input'); search.name = 'q'; search.type = 'search'; search.maxLength = 200; search.value = filters.q; searchLabel.append(search); form.append(searchLabel);
            const submit = el('button', 'mlops-btn', 'Apply filters'); submit.type = 'submit'; form.append(submit);
            form.addEventListener('submit', e => { e.preventDefault(); const data = new FormData(form); Object.keys(filters).forEach(k => { filters[k] = data.get(k); }); page = 1; loadExplorer(); }); area.append(form);
            const body = el('div'); body.id = 'workflow-explorer-body'; body.setAttribute('aria-live', 'polite'); area.append(body);
            paintExplorer();
        }
        function paintExplorer() {
            const area = node('workflow-explorer-body'); if (!area) return;
            area.replaceChildren();
            if (!explorer) { area.append(el('p', null, 'Loading dataset records…')); return; }
            if (explorer.error) { area.append(el('p', 'mlops-note note-bad', explorer.error), button('Retry loading records', loadExplorer)); return; }
            area.append(el('p', null, 'Statistics cover ' + explorer.scanned_rows + ' of ' + explorer.total_rows + ' rows before display filters.' + (explorer.truncated ? ' Scan limit reached; these are partial counts.' : ' Complete artifact scanned.')));
            area.append(kvList([['Columns / features', explorer.column_count + ' / ' + explorer.feature_count], ['Duplicates (entity + time)', metric(explorer.duplicates)], ['Invalid rows', metric(explorer.invalid_rows)]]));
            area.append(el('p', null, explorer.invalid_rows_definition));
            area.append(detail('Schema', explorer.schema), detail('Missing values by feature', explorer.missing_values));
            area.append(el('h4', null, 'Class distribution'), simpleTable(['Class', 'Rows'], Object.entries(explorer.class_distribution).map(([k, v]) => [k, String(v)])));
            area.append(el('h4', null, 'Sample records'));
            const columns = ['entity_id', 'as_of', 'split', 'label', 'features'];
            if (explorer.items.length) area.append(simpleTable(columns, explorer.items.map(row => columns.map(k => text(row[k])))));
            else area.append(el('p', null, 'No records match these filters. Clear the search or choose All.'));
            const prev = button('Previous records', () => { page -= 1; loadExplorer(); }), next = button('Next records', () => { page += 1; loadExplorer(); });
            prev.disabled = page <= 1; next.disabled = page * explorer.page_size >= explorer.filtered_rows;
            area.append(prev, el('span', null, ' Page ' + page + ' · ' + explorer.filtered_rows + ' matching rows '), next);
        }
        async function loadExplorer() {
            if (!selectedDataset) return;
            const req = beginRequest('workflow-explorer'); explorer = null; paintExplorer();
            try {
                const result = await api('/api/ml/datasets/' + encodeURIComponent(selectedDataset) + '/explorer', { params: { ...filters, page, page_size: 25 }, signal: req.signal });
                if (!req.isCurrent()) return; explorer = result;
            } catch (err) { if (err.aborted || !req.isCurrent()) return; explorer = { error: formatActionError('Dataset explorer unavailable', err) }; }
            paintExplorer();
        }
        async function selectEvidence() {
            const generation = ++selectionGeneration, req = beginRequest('workflow-evidence');
            beginRequest('workflow-explorer'); explorer = null; dataset = null; model = null;
            message('Loading selected evidence…'); renderPipeline(); renderStage(); renderProgress();
            try {
                if (selectedModel) {
                    const result = await api('/api/ml/models/' + encodeURIComponent(selectedModel), { signal: req.signal });
                    if (generation !== selectionGeneration) return;
                    model = result; selectedDataset = model.dataset_id || ''; node('workflow-dataset').value = selectedDataset;
                }
                if (selectedDataset) {
                    const result = await api('/api/ml/datasets/' + encodeURIComponent(selectedDataset), { signal: req.signal });
                    if (generation !== selectionGeneration) return; dataset = result;
                    fillPicker('workflow-dataset', [dataset, ...state.datasets.filter(item => item.id !== dataset.id)], 'Choose a dataset (recent 25)', selectedDataset);
                }
                if (generation !== selectionGeneration) return;
                message(model ? 'Showing the dataset and configuration linked to this model.' : dataset ? 'Dataset selected. Continue through validation and training.' : 'No evidence selected. Prepare a dataset to begin.');
                renderPipeline(); renderStage(); renderProgress(); summary(); if (dataset) loadExplorer();
            } catch (err) {
                if (err.aborted || generation !== selectionGeneration) return;
                dataset = null; model = null;
                message(formatActionError('Could not load workflow evidence', err));
                renderPipeline(); renderStage(); renderProgress(); summary();
            }
        }
        function fillPicker(id, items, placeholder, selected) {
            const picker = node(id), values = JSON.stringify(items.map(item => [item.id, item.name || item.algorithm, item.version, item.status || item.stage]));
            if (picker.dataset.signature === values && picker.dataset.ready && (!selected || [...picker.options].some(option => option.value === selected))) { picker.value = selected; return; }
            picker.dataset.signature = values; picker.dataset.ready = 'true';
            const empty = el('option', null, placeholder); empty.value = ''; picker.replaceChildren(empty);
            items.forEach(item => { const o = el('option', null, (item.name || item.algorithm) + ' v' + item.version + ' · ' + (item.status || item.stage)); o.value = item.id; picker.append(o); });
            if (selected && !items.some(i => i.id === selected)) { const o = el('option', null, 'Linked dataset / model ' + selected); o.value = selected; picker.append(o); }
            picker.value = selected;
        }
        function sync(source) {
            loadErrors.delete(source);
            const listed = state.models.find(item => item.id === selectedModel);
            if (source === 'models' && model && listed && listed.stage !== model.stage) selectEvidence();
            fillPicker('workflow-dataset', state.datasets, 'Choose a dataset (recent 25)', selectedDataset);
            fillPicker('workflow-model', state.models, 'Choose a model (recent 25)', selectedModel);
            fillPicker('workflow-job', state.recentJobs.filter(j => j.kind === 'training').map(j => ({ id: j.job_id, name: j.job_id, version: '', status: j.status })), 'Choose a training run', selectedJobId);
            datasetLoaded = datasetLoaded || source === 'datasets'; modelsLoaded = modelsLoaded || source === 'models';
            renderPipeline(); renderProgress();
            const next = JSON.stringify(state.recentJobs.map(j => [j.job_id, j.status]));
            if (signature && signature !== next && (selectedModel || selectedDataset || selectedJobId)) {
                const job = trainingJob();
                if (selectedJobId && job) { selectedModel = (job.result || {}).model_id || ''; selectedDataset = (job.details || {}).dataset_id || (job.result || {}).dataset_id || selectedDataset; }
                selectEvidence();
            }
            signature = next;
            if (loadErrors.size) message([...loadErrors.values()].join(' '));
            else if (!selectedModel && !selectedDataset) message(datasetLoaded && modelsLoaded
                ? state.datasets.length || state.models.length ? 'Choose a dataset to begin, or a saved model to review its full lineage.' : 'No datasets or models yet. Prepare your first dataset below.'
                : 'Loading datasets and models…');
        }
        function loadError(source, err) {
            loadErrors.set(source, formatActionError('Could not load ' + source, err));
            message([...loadErrors.values()].join(' '));
        }
        node('workflow-dataset').addEventListener('change', () => { selectedJobId = ''; node('workflow-job').value = ''; selectedDataset = node('workflow-dataset').value; selectedModel = ''; node('workflow-model').value = ''; page = 1; selectEvidence(); });
        node('workflow-model').addEventListener('change', () => { selectedJobId = ''; node('workflow-job').value = ''; selectedModel = node('workflow-model').value; page = 1; selectEvidence(); });
        node('workflow-job').addEventListener('change', () => { selectedJobId = node('workflow-job').value; const job = trainingJob(); selectedModel = (job && job.result || {}).model_id || ''; selectedDataset = (job && job.details || {}).dataset_id || (job && job.result || {}).dataset_id || ''; node('workflow-model').value = selectedModel; node('workflow-dataset').value = selectedDataset; page = 1; selectEvidence(); });
        node('workflow-refresh').addEventListener('click', async () => { await host.refreshConsole(); if (!loadErrors.size) await selectEvidence(); });
        node('workflow-summary-button').addEventListener('click', () => { summary(); node('workflow-stage').hidden = true; node('workflow-summary').hidden = false; node('workflow-summary').focus(); });
        renderPipeline(); renderStage(); renderProgress();
        return { sync, loadError };
    };
}());
