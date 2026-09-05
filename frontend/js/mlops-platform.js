/* Shared API/auth/renderers are provided by the existing console. */
(function () {
    'use strict';
    window.MLOpsPlatform = function (host) {
        const { api, el, kvList, simpleTable, jsonBlock, state, formatActionError, openActionPanel } = host;
        const node = id => document.getElementById(id);
        let pipelines = [], caps = {}, generation = 0;
        const list = id => node(id).value.split(',').map(v => v.trim()).filter(Boolean);
        function notice(message) { node('platform-note').textContent = message; }
        function button(label, action) { const b = el('button', 'mlops-btn', label); b.type = 'button'; b.addEventListener('click', action); return b; }
        function evidence(title, value) { const d = el('details', 'workflow-evidence'); d.append(el('summary', null, title), jsonBlock(value)); return d; }
        function jsonDownload(label, name, data) {
            return button(label, () => {
                const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
                const a = el('a'); a.href = url; a.download = name; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
            });
        }
        function applyPipeline() {
            const saved = pipelines.find(p => p.id === node('platform-pipeline').value), c = saved && saved.configuration;
            if (c) {
                node('training-model-type').value = c.model_type; host.updateTrainingAvailability();
                node('training-algorithm').value = c.algorithm;
                node('platform-name').value = saved.name; node('platform-target').value = c.target || '';
                node('platform-features').value = c.features.join(', '); node('platform-metrics').value = c.metrics.join(', ');
            }
            host.fillTrainingDatasetPicker(state.datasets || []);
            notice(c ? 'Selected ' + saved.name + ' v' + saved.version + '. This immutable configuration will be recorded with the run.' : 'Using existing model defaults. Save a pipeline to record an explicit target, predictors and metrics.');
            updateOptions();
        }
        function updateOptions() {
            const algo = node('training-algorithm').value;
            for (const name of ['optuna', 'shap']) {
                const item = caps[name] || {}, supported = name === 'optuna' ? algo.startsWith('xgboost') : ['logreg', 'random_forest', 'gradient_boosting', 'xgboost_classifier', 'xgboost_regressor'].includes(algo);
                const input = node('platform-' + name); input.disabled = item.status !== 'Available' || !supported;
                if (input.disabled) input.checked = false;
                node('platform-' + name + '-help').textContent = (item.status || 'Unavailable') + ' ? ' + (item.action || 'Refresh capabilities.') + (supported ? '' : ' Choose a supported algorithm; tuning requires XGBoost and SHAP requires a supervised tree or linear model.');
            }
            node('platform-tuning').hidden = !node('platform-optuna').checked;
        }
        async function refresh() {
            const request = ++generation;
            notice('Loading capabilities and saved pipeline versions?');
            const results = await Promise.allSettled([api('/api/ml/capabilities'), api('/api/ml/pipelines')]);
            if (request !== generation) return;
            const messages = [];
            if (results[0].status === 'fulfilled') {
                const data = results[0].value; caps = data.items || {};
                const area = node('optional-capabilities-body');
                area.replaceChildren(simpleTable(['Capability', 'Status', 'Version', 'Next action'], Object.entries(caps).map(([name, c]) => [name, c.status, c.version || 'Built in', c.action])));
                const link = el('a', 'mlops-btn', 'Manage integrations, defaults and limits in Admin Settings'); link.href = '/admin/settings'; area.append(link);
                const limits = data.limits || {};
                for (const [id, max] of [['platform-trials', limits.optuna_trials], ['platform-timeout', limits.optuna_timeout_seconds]]) if (max) { node(id).max = max; node(id).value = Math.min(Number(node(id).value), max); }
            } else { caps = {}; const error = formatActionError('Capabilities could not load', results[0].reason); node('optional-capabilities-body').replaceChildren(el('p', null, error)); messages.push(error); }
            if (results[1].status === 'fulfilled') {
                pipelines = results[1].value.items || [];
                const picker = node('platform-pipeline'), keep = picker.value, first = el('option', null, 'Use existing model defaults'); first.value = '';
                picker.replaceChildren(first, ...pipelines.map(p => { const o = el('option', null, p.name + ' v' + p.version); o.value = p.id; return o; }));
                if (pipelines.some(p => p.id === keep)) picker.value = keep;
            } else messages.push(formatActionError('Pipeline configurations could not load', results[1].reason));
            notice(messages.length ? messages.join(' ') + ' Restore access and refresh. Existing model defaults remain selectable.' : pipelines.length ? 'Select a saved pipeline or use existing defaults.' : 'No saved pipelines yet. Existing model defaults are ready; save a configuration to reuse it.');
            updateOptions();
        }
        async function save() {
            const b = node('platform-save'); b.disabled = true;
            try {
                const created = await api('/api/ml/pipelines', { method: 'POST', body: { name: node('platform-name').value.trim(), configuration: {
                    model_type: node('training-model-type').value, algorithm: node('training-algorithm').value,
                    target: node('platform-target').value.trim() || null, features: list('platform-features'), metrics: list('platform-metrics'), validation_strategy: 'dataset_split'
                } } });
                await refresh(); node('platform-pipeline').value = created.id; applyPipeline();
                notice('Saved ' + created.name + ' v' + created.version + '. Future runs can reuse this configuration.');
            } catch (err) { notice(formatActionError('Pipeline was not saved', err)); } finally { b.disabled = false; }
        }
        function configureRun(body) {
            const saved = pipelines.find(p => p.id === node('platform-pipeline').value);
            if (saved) {
                if (saved.configuration.model_type !== body.model_type || saved.configuration.algorithm !== body.algorithm) throw new Error('Model or algorithm changed. Save a new pipeline version or select existing model defaults.');
                body.pipeline_id = saved.id;
            }
            if (body.model_type === 'tabular_regression_model' && (!saved || !body.dataset_id)) throw new Error('Regression requires a saved pipeline with a numeric target and an existing dataset version.');
            body.run_options = { shap: node('platform-shap').checked, require_clean_git: node('platform-clean').checked, optuna: { enabled: node('platform-optuna').checked } };
            if (body.run_options.optuna.enabled) {
                let search; try { search = JSON.parse(node('platform-space').value); } catch (_) { throw new Error('Correct the Optuna search space JSON before starting.'); }
                body.run_options.optuna = { enabled: true, trials: Number(node('platform-trials').value), timeout_seconds: Number(node('platform-timeout').value), pruning: node('platform-pruning').checked, search_space: search };
            }
        }
        function results(model) {
            const area = el('section', 'workflow-evidence workflow-platform-results'), cfg = model.training_config || {}, report = model.evaluation_report || {}, tracking = model.tracking || {};
            area.append(el('h3', null, 'Experiment, artifacts and registration'), kvList([
                ['MLflow synchronization', tracking.status || 'Not recorded for this historical run'], ['Experiment run', tracking.run_id || 'Not recorded'],
                ['Registered model / version', tracking.registered_name ? tracking.registered_name + ' / ' + tracking.registered_version : 'Not synchronized'],
                ['Training device', report.execution ? report.execution.device + (report.execution.fallback_reason ? ' ? ' + report.execution.fallback_reason : '') : 'Not recorded'], ['Local registry stage', model.stage], ['Dataset snapshot', String(model.dataset_id || '')], ['Random seed', String(model.seed)],
                ['Pipeline version', cfg.pipeline ? (cfg.pipeline.name || 'Run configuration') + ' v' + (cfg.pipeline.version || '?') : 'Existing model defaults']
            ]));
            const statusGuide = el('details', 'workflow-evidence');
            statusGuide.append(el('summary', null, 'What these statuses mean and what to do next'), kvList([
                ['Training versus tracking', 'Training creates the local artifact. MLflow synchronization copies experiment evidence to the tracking service. These are separate statuses.'],
                ['Pending / running synchronization', 'The worker is waiting to copy or is copying evidence. Wait for it to finish, then refresh the selected model.'],
                ['Failed synchronization', 'Check capability status, storage and service access in Admin Settings, then retry synchronization. The retained local model can still be reviewed; no new training is needed solely to retry a copy.'],
                ['Disabled / not recorded', 'Disabled means tracking is switched off. Not recorded means this record has no saved tracking evidence, which can happen for historical runs.'],
                ['Validated / approved / shadow', 'Validated means candidate quality gates passed. Approved here permits offline use for eligible models. Shadow means an approved model observes alongside rules. Read the intended use before approving.'],
                ['CPU / CUDA', 'CUDA is GPU execution. XGBoost probes availability and falls back to CPU if needed. A recorded fallback explains the device choice; it does not by itself invalidate the model.'],
                ['Reproducibility manifest', 'Download this record to identify the dataset, code, parameters, seed and runtime used. Keep the source or deployment image and dataset artifact as well when reproducing work.']
            ])); area.append(statusGuide);
            if (tracking.last_error) area.append(el('p', 'mlops-note note-bad', tracking.last_error));
            const note = el('p', 'mlops-note'); note.setAttribute('role', 'status'); area.append(note);
            if (['failed', 'disabled', 'pending'].includes(tracking.status)) area.append(button('Retry MLflow synchronization', () => openActionPanel('Retry experiment synchronization', async reason => {
                await api('/api/ml/experiments/' + encodeURIComponent(model.training_job_id) + '/retry', { method: 'POST', body: { reason } }); note.textContent = 'Synchronization queued. Refresh the model after the worker finishes.';
            })));
            if (cfg.reproducibility) area.append(evidence('Reproducibility manifest', cfg.reproducibility), jsonDownload('Download reproducibility manifest', 'reproducibility-' + model.id + '.json', cfg.reproducibility));
            area.append(jsonDownload('Download evaluation evidence', 'evaluation-' + model.id + '.json', report));
            if (report.baseline) area.append(evidence('Baseline reference and measured results', report.baseline));
            if (report.optuna) area.append(evidence('Optuna search, trials and best parameters', report.optuna));
            const shap = report.shap;
            if (shap && shap.status === 'completed') {
                area.append(el('h4', null, 'SHAP explanations'), el('p', null, 'Contributions describe this model on the recorded holdout sample. Positive and negative contributions move the output above or below the reference value; they do not establish causation.'), evidence('Global importance and sampling', shap));
                const importance = Object.entries(shap.global_importance || {}).sort((a, b) => b[1] - a[1]);
                const peak = Math.max(...importance.map(v => v[1]), 0.000001);
                for (const [name, value] of importance.slice(0, 20)) { const row = el('div', 'workflow-shap-bar'); const bar = el('meter'); bar.min = 0; bar.max = peak; bar.value = value; bar.setAttribute('aria-label', name + ' mean absolute SHAP contribution'); row.append(el('span', null, name + ': ' + Number(value).toPrecision(4)), bar); area.append(row); }
                const input = el('input'); input.type = 'number'; input.min = 0; input.max = Math.max(0, (shap.rows || shap.sample_count || 1) - 1); input.value = '0'; input.setAttribute('aria-label', 'Explanation sample index');
                const output = el('div'); output.setAttribute('aria-live', 'polite');
                area.append(input, button('Explain selected prediction', async () => {
                    output.replaceChildren(el('p', null, 'Loading explanation...'));
                    try {
                        const data = await api('/api/ml/models/' + model.id + '/explanations', { params: { sample: input.value } });
                        const sample = data.sample, prediction = sample.base_value + sample.contributions.reduce((sum, v) => sum + v, 0);
                        output.replaceChildren(el('p', null, data.output_space + '. Reference: ' + sample.base_value + '; explained prediction: ' + prediction), simpleTable(['Feature', 'Value', 'Signed contribution'], data.feature_names.map((name, i) => [name, sample.features[i], sample.contributions[i]])));
                    }
                    catch (err) { output.replaceChildren(el('p', null, formatActionError('Explanation unavailable', err))); }
                }), output);
                for (const filename of Object.keys(shap.exports || {})) { const a = el('a', 'mlops-btn', 'Download ' + filename); a.href = '/api/ml/models/' + model.id + '/explanations/download/' + encodeURIComponent(filename); a.download = filename; area.append(a); }
            } else area.append(el('p', null, 'SHAP was not requested or recorded. Enable it for a supported future run to generate global and individual explanations.'));
            const others = (state.models || []).filter(m => m.id !== model.id);
            if (others.length) {
                const picker = el('select'); picker.setAttribute('aria-label', 'Model to compare');
                for (const m of others) { const o = el('option', null, m.model_type + ' v' + m.version); o.value = m.id; picker.append(o); }
                const output = el('div'); output.setAttribute('aria-live', 'polite');
                area.append(picker, button('Compare models', async () => {
                    output.replaceChildren(el('p', null, 'Loading comparison...'));
                    try { const data = await api('/api/ml/comparisons?model_ids=' + encodeURIComponent(model.id) + '&model_ids=' + encodeURIComponent(picker.value)); output.replaceChildren(el('p', null, (data.comparable ? 'Same dataset and task. ' : 'Different data or task: metrics are not directly comparable. ') + data.note), jsonBlock(data.items)); }
                    catch (err) { output.replaceChildren(el('p', null, formatActionError('Comparison unavailable', err))); }
                }), output);
            } else area.append(el('p', null, 'Train another model on the same dataset to compare experiments.'));
            if (model.stage === 'validated' && ['tabular_regression_model', 'threat_ranking_model'].includes(model.model_type)) area.append(button('Approve artifact for offline use', () => openActionPanel('Approve reviewed artifact for offline use (live deployment remains gated)', async reason => {
                await api('/api/ml/models/' + model.id + '/promote', { method: 'POST', body: { reason, artifact_checksum: model.artifact_hash } });
                note.textContent = 'Artifact approved for offline use. MLflow alias synchronization is pending; refresh after the worker finishes.'; await host.refreshConsole();
            })));
            return area;
        }
        node('platform-refresh').addEventListener('click', refresh); node('platform-save').addEventListener('click', save);
        node('platform-pipeline').addEventListener('change', applyPipeline); node('platform-optuna').addEventListener('change', updateOptions);
        for (const id of ['training-model-type', 'training-algorithm']) node(id).addEventListener('change', () => { node('platform-pipeline').value = ''; setTimeout(updateOptions, 0); });
        refresh(); return { configureRun, results, refresh };
    };
}());
