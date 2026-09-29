/* Saved ML evidence and isolated notebook diagnostics. Production jobs stay in ML Ops. */
(() => {
    'use strict';
    const expanded = new Set();
    function el(tag, text, cls) {
        const node = document.createElement(tag);
        if (text != null) node.textContent = String(text);
        if (cls) node.className = cls;
        return node;
    }
    function download(url) {
        const link = el('a', 'Download debug notebook', 'mlops-btn mlops-btn-small');
        link.href = url;
        link.setAttribute('download', '');
        return link;
    }
    function contextFor(value = {}) {
        const context = {};
        if (typeof value.model_type === 'string' && value.model_type) context.model_type = value.model_type;
        // A primary record owns its lineage. Independent picker values can be stale.
        for (const key of ['model_id', 'job_id', 'dataset_id']) {
            if (typeof value[key] === 'string' && value[key]) { context[key] = value[key]; break; }
        }
        return context;
    }
    function workflowUrl(value) {
        const query = new URLSearchParams(contextFor(value));
        return '/api/ml/workflow-notebook' + (query.size ? '?' + query.toString() : '');
    }
    function safeNotebookUrl(value) {
        if (typeof value !== 'string' || !value.startsWith('/notebooks/lab/tree/')) return null;
        const url = new URL(value, window.location.origin);
        if (url.origin !== window.location.origin || url.username || url.password || url.search || url.hash) return null;
        if (!url.pathname.startsWith('/notebooks/lab/tree/') || !url.pathname.endsWith('.ipynb')) return null;
        return url.href;
    }
    const imports = new Map();
    async function importNotebook(context) {
        const key = JSON.stringify(context);
        if (imports.has(key)) return imports.get(key);
        const pending = (async () => {
            const controller = new AbortController();
            const timer = window.setTimeout(() => controller.abort(), 30000);
            try {
                const response = await fetch('/notebooks/vas/import', {
                    method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
                    headers: {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
                    body: JSON.stringify(context)
                });
                if (!response.ok) {
                    let message = response.status === 401 ? 'Your session expired. Sign in again, then retry.'
                        : response.status === 403 ? 'An ML administrator session is required to open this notebook.'
                        : 'Could not prepare the notebook (' + response.status + '). Retry or download the file.';
                    try {
                        const data = await response.json(), detail = data.detail || data.error;
                        if (detail && typeof detail.message === 'string') message = detail.message;
                    } catch (_) { /* A gateway may return a non-JSON error. */ }
                    throw new Error(message);
                }
                const data = await response.json(), url = safeNotebookUrl(data.url);
                if (!url) throw new Error('The notebook service returned an invalid link. Download the file or retry.');
                return url;
            } catch (error) {
                if (error.name === 'AbortError') throw new Error('Preparing the notebook timed out. Retry or download the file.');
                throw error;
            } finally { window.clearTimeout(timer); }
        })();
        imports.set(key, pending);
        try { return await pending; } finally { imports.delete(key); }
    }
    function actions(value, options = {}) {
        const context = contextFor(value), panel = el('div', null, 'mlops-notebook-actions');
        const buttons = el('div', null, 'mlops-notebook-buttons');
        const open = el('button', options.label || 'Open step-by-step notebook', 'mlops-btn mlops-btn-small');
        open.type = 'button';
        const file = download(workflowUrl(context)); file.textContent = 'Download .ipynb';
        const status = el('p', '', 'mlops-notebook-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
        open.addEventListener('click', async () => {
            if (open.disabled) return;
            open.disabled = true; status.textContent = 'Preparing a notebook from the current saved evidence…';
            // Open during the user gesture so a successful request can use a new tab.
            // The opener is removed immediately; only a validated local notebook URL is used.
            let tab = null;
            try { tab = window.open('about:blank', '_blank'); if (tab) tab.opener = null; } catch (_) { /* Use the explicit link below when popups are blocked. */ }
            try {
                const url = await importNotebook(context);
                const link = el('a', 'Open the prepared notebook', 'mlops-notebook-ready');
                link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
                status.replaceChildren(el('span', 'Notebook ready. '), link);
                if (tab && !tab.closed) tab.location.replace(url);
            } catch (error) {
                if (tab && !tab.closed) tab.close();
                status.textContent = error.message || 'Notebook service unavailable. Retry or download the file.';
            } finally { open.disabled = false; }
        });
        buttons.append(open, file); panel.append(buttons, status);
        return panel;
    }
    function updatePanel(panel, context, options) {
        if (!panel) return;
        const signature = JSON.stringify(contextFor(context));
        if (panel.dataset.notebookContext === signature) return;
        panel.dataset.notebookContext = signature;
        panel.replaceChildren(actions(context, options));
    }
    let workspace;
    function workspaceLink(panel) {
        if (!workspace) workspace = fetch('/api/ml/debug-workspace', {credentials: 'same-origin', cache: 'no-store'})
            .then(response => response.ok ? response.json() : {}).catch(() => ({}));
        workspace.then(data => {
            if (!data.url) return;
            const url = new URL(data.url, window.location.origin);
            if (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(url.hostname))) return;
            const link = el('a', 'Jupyter workspace', 'mlops-btn mlops-btn-small');
            link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
            panel.append(link);
        }).catch(() => {});
    }
    function render(task) {
        const panel = el('details', null, 'mlops-diagnostics');
        const key = String(task.job_id);
        panel.open = expanded.has(key);
        panel.addEventListener('toggle', () => {
            if (panel.open) expanded.add(key); else expanded.delete(key);
        });
        panel.append(el('summary', 'Step evidence & notebook'));
        const data = task.details || {}, history = [];
        if (data.preparation && typeof data.preparation === 'object') history.push({stage: 'feature_collection', ...data.preparation});
        const groups = [['Dataset', data.dataset_diagnostics], ['Training', data.training_diagnostics]];
        if (groups.some(([, value]) => Array.isArray(value?.stage_history))) {
            for (const [phase, value] of groups) {
                for (const event of Array.isArray(value?.stage_history) ? value.stage_history : []) if (event && typeof event === 'object') history.push({...event, phase});
            }
        } else if (Array.isArray(data.stage_history)) history.push(...data.stage_history.filter(event => event && typeof event === 'object'));
        if (!history.length) panel.append(el('p', 'No recorded stage history is available for this job. The notebook identifies missing evidence and explains each pipeline step.'));
        else {
            const list = el('ol', null, 'mlops-diagnostic-stages');
            history.forEach((event, index) => {
                let status = event.status || (typeof event.duration_seconds === 'number' ? 'completed'
                    : index === history.length - 1 ? task.status || 'not recorded' : 'not recorded');
                if (status === 'running' && ['failed', 'cancelled'].includes(task.status)) status = 'interrupted';
                const row = el('li');
                row.dataset.status = status;
                row.append(el('strong', (event.phase ? event.phase + ' · ' : '') + String(event.stage || 'step').replaceAll('_', ' ')), el('span', status, 'mlops-diagnostic-status'));
                const facts = [];
                if (typeof event.duration_seconds === 'number') facts.push(event.duration_seconds.toFixed(2) + ' s');
                Object.entries(event).forEach(([name, value]) => {
                    if (!['stage', 'status', 'started_at', 'duration_seconds', 'phase'].includes(name)) facts.push(name.replaceAll('_', ' ') + ': ' + (value && typeof value === 'object' ? JSON.stringify(value) : String(value)));
                });
                if (facts.length) row.append(el('small', facts.join(' · ')));
                list.append(row);
            });
            panel.append(list);
        }
        const failure = data.training_diagnostics?.failure || data.dataset_diagnostics?.failure || data.failure;
        if (failure) {
            panel.append(el('p', 'Failure: ' + failure.code, 'mlops-note note-bad'));
            if (Array.isArray(failure.failed_checks) && failure.failed_checks.length) panel.append(el('p', 'Failed checks: ' + failure.failed_checks.join(', ')));
            if (Array.isArray(failure.frames) && failure.frames.length) {
                panel.append(el('pre', failure.frames.map(frame => frame.module + ':' + frame.line + ' · ' + frame.function).join('\n'), 'mlops-diagnostic-trace'));
            }
            if (failure.log_reference) panel.append(el('p', 'Worker log reference: ' + failure.log_reference));
        }
        panel.append(actions({job_id: key}));
        panel.append(el('p', 'Inspect the recorded job, data extraction, validation, model evidence and service use. Snapshot checks run in Jupyter; production collection, training and activation stay in ML Ops.', 'mlops-mode-desc'));
        return panel;
    }

    // Presentation only. The server remains authoritative for every training gate.
    const checkGuides = {
        both_classes_in_train: ['Both reviewed outcome classes', 'Add reviewed examples of both classes and inspect their training split coverage.'],
        class_balance: ['Reviewed class balance', 'Review more examples of the underrepresented class, then prepare a new dataset.'],
        no_target_adjacent_features: ['Outcome leakage', 'Remove features that reveal the target outcome from the supervised dataset.'],
        point_in_time_label_anchor: ['Features precede the outcome', 'Correct feature cutoffs so they do not include information collected after the labeled event.'],
        minimum_rows: ['Usable dataset rows', 'Collect eligible observations, check extraction exclusions, then prepare a new dataset.'],
        minimum_train_rows: ['Training split rows', 'Inspect time boundaries and identity exclusions. Collect more distinct identities across the required periods.'],
        minimum_usable_features: ['Usable features', 'Inspect feature coverage and extraction. Prepare a new version with enough supported measurements.'],
        max_null_rate: ['Missing feature values', 'Inspect missingness by feature and correct collection. Do not replace unavailable evidence with arbitrary zeros.'],
        no_empty_feature_rows: ['Empty feature rows', 'Check active feature definitions and extraction eligibility, then rebuild the dataset.'],
        no_duplicates: ['Duplicate entity/time rows', 'Inspect duplicate records and the extraction logic before preparing a new version.'],
        schema_known_features: ['Recognized feature names', 'Match the dataset feature schema to the selected service.'],
        no_future_as_of: ['Future-dated records', 'Check source timestamps and the extraction cutoff.'],
        ratio_ranges: ['Feature ranges', 'Inspect out-of-range values and their feature calculation.'],
        feature_dtype_numeric: ['Numeric feature values', 'Correct nonnumeric feature values in the extraction process.'],
        no_nan_inf: ['Finite feature values', 'Inspect invalid calculations that produced NaN or infinity.'],
        timestamp_parseable: ['Valid timestamps', 'Check timestamp formatting and extraction.']
    };
    const dataCodes = new Set(['DATASET_VALIDATION_FAILED', 'DATASET_QUALITY_FAILED', 'QUALITY_GATES_FAILED',
        'SOURCE_HISTORY_REQUIRED', 'INSUFFICIENT_REVIEWED_LABELS', 'SUPERVISED_LABEL_GATE_CLOSED',
        'DATASET_NOT_BUILT', 'DATASET_NOT_READY', 'DATASET_SERVICE_MISMATCH', 'DATASET_KIND_MISMATCH', 'DATASET_FEATURE_SET_MISMATCH']);
    const stageGroups = [
        ['Prepare dataset', ['preparing_features', 'loading_dataset', 'building_dataset', 'configuration', 'counting_source', 'extracting_rows', 'matching_labels', 'selecting_features']],
        ['Validate dataset', ['validation', 'splitting', 'population_checks', 'writing_artifact', 'writing_manifest', 'registering_dataset', 'preprocessing', 'feature_engineering']],
        ['Train model', ['training']], ['Evaluate', ['evaluating']],
        ['Save candidate', ['saving_candidate', 'registering']],
    ];
    const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    const readable = value => value == null ? 'Not recorded' : typeof value === 'number'
        ? value.toLocaleString(undefined, {maximumFractionDigits: 3}) : typeof value === 'object' ? JSON.stringify(value) : String(value);
    function jobEvidence(task) {
        const d = object(task.details), result = object(task.result);
        const history = [...(Array.isArray(d.stage_history) ? d.stage_history : []),
            ...(Array.isArray(d.training_diagnostics?.stage_history) ? d.training_diagnostics.stage_history : [])];
        let report = object(d.validation_report || result.quality_report);
        if (!Object.keys(report).length && typeof task.error_message === 'string') {
            try { report = object(JSON.parse(task.error_message)); } catch (_) { /* Older reports may be truncated. */ }
        }
        if (!report.checks && Object.values(report).some(value => typeof value?.passed === 'boolean')) report = {checks: report};
        const code = task.error_code || d.dataset_diagnostics?.failure?.code || '';
        const current = d.stage || d.training_diagnostics?.stage || d.dataset_diagnostics?.stage;
        const index = stageGroups.findIndex(([, names]) => names.includes(current));
        const fittingStarted = history.some(event => stageGroups.slice(2).some(([, names]) => names.includes(event.stage))) || index >= 2 || task.status === 'completed';
        const blocked = task.status === 'failed' && dataCodes.has(code);
        return {report, code, current, index, history, fittingStarted, blocked,
            datasetId: d.dataset_id || d.dataset_diagnostics?.dataset_id || result.dataset_id,
            title: blocked ? 'Blocked · data requirements not met' : task.status === 'failed' ? 'Failed · investigation needed'
                : task.status === 'completed' ? 'Candidate saved · review next' : task.status === 'cancelled' ? 'Run cancelled'
                : task.status === 'scheduled' ? 'Queued · waiting for worker' : 'Run in progress'};
    }
    function validationReport(report) {
        report = object(report);
        const area = el('section', null, 'mlops-validation-report');
        area.setAttribute('aria-label', 'Dataset validation results');
        const checks = Object.entries(object(report.checks)).filter(([, value]) => typeof value?.passed === 'boolean');
        const failed = checks.filter(([, check]) => check.passed === false);
        const informational = checks.filter(([, check]) => check.current?.enforced === false).length;
        area.append(el('h4', checks.length ? `${failed.length} blocked · ${checks.length - failed.length - informational} passed` + (informational ? ` · ${informational} informational` : '') : 'Validation evidence'));
        if (!checks.length) area.append(el('p', 'Detailed checks were not recorded here. Open the saved dataset report or notebook; missing evidence is not a pass.'));
        if (report.row_count != null) area.append(el('p', readable(report.row_count) + ' dataset rows inspected'));
        if (checks.length) {
            const scroll = el('div', null, 'mlops-validation-table-wrap');
            scroll.tabIndex = 0; scroll.setAttribute('aria-label', 'Validation checks; scroll for all columns');
            const table = el('table', null, 'mlops-validation-table');
            const head = el('thead'), header = el('tr');
            ['Check', 'Result', 'Actual', 'Required', 'Next action'].forEach(title => { const th = el('th', title); th.scope = 'col'; header.append(th); });
            head.append(header); table.append(head);
            const body = el('tbody');
            checks.sort((a, b) => Number(a[1].passed) - Number(b[1].passed)).forEach(([key, check]) => {
                const row = el('tr'); row.dataset.result = check.passed ? 'passed' : 'blocked';
                const guide = checkGuides[key] || [key.replaceAll('_', ' '), 'Inspect this check in the dataset report, correct the cause, then prepare a new version.'];
                const title = el('th', guide[0]); title.scope = 'row';
                if (check.detail) title.append(el('small', check.detail, 'mlops-check-detail'));
                const info = check.current?.enforced === false;
                const required = info ? 'Reported only' : check.required == null ? 'Not recorded' : (key.startsWith('minimum_') ? 'At least ' : key === 'max_null_rate' ? 'At most ' : '') + (key === 'max_null_rate' && typeof check.required === 'number' ? readable(check.required * 100) + '%' : readable(check.required));
                const actual = key === 'max_null_rate' && typeof check.current?.rate === 'number' ? readable(check.current.rate * 100) + '% · ' + readable(check.current.feature) : readable(check.current ?? check.actual);
                row.append(title, el('td', info ? 'Informational' : check.passed ? 'Passed' : 'Blocked'), el('td', actual), el('td', required),
                    el('td', info ? 'Review feature coverage before interpreting results.' : check.passed ? 'Requirement met' : key === 'max_null_rate' && report.row_count === 0 ? 'No rows to measure. Resolve the empty dataset first.' : guide[1]));
                row.querySelectorAll('td').forEach((cell, index) => { cell.dataset.label = ['Result', 'Actual', 'Required', 'Next action'][index]; });
                body.append(row);
            });
            table.append(body); scroll.append(table); area.append(scroll);
        }
        for (const warning of Array.isArray(report.warnings) ? report.warnings : []) area.append(el('p', readable(warning), 'mlops-note note-warn'));
        const raw = el('details', null, 'mlops-validation-technical');
        raw.append(el('summary', 'Technical validation report'), el('pre', JSON.stringify(report, null, 2)));
        area.append(raw);
        return area;
    }
    const savedValidationReports = new Map();
    function trainingPanel(task, {openDataset, compact = false} = {}) {
        const evidence = jobEvidence(task), panel = el('section', null, 'mlops-training-checks');
        panel.dataset.state = evidence.blocked ? 'blocked' : task.status;
        panel.append(el('h4', evidence.title));
        if (!compact) {
            const track = el('ol', null, 'mlops-training-track'); track.setAttribute('aria-label', 'Recorded training stages');
            stageGroups.forEach(([label], index) => {
                let status = 'Not recorded';
                if (task.status === 'scheduled') status = 'Waiting';
                else if (task.status === 'completed') status = 'Completed';
                else if (evidence.index >= 0) status = index < evidence.index ? 'Completed' : index > evidence.index ? 'Not started'
                    : evidence.blocked ? 'Blocked' : task.status === 'failed' ? 'Failed' : task.status === 'cancelled' ? 'Stopped' : 'In progress';
                const item = el('li'); item.dataset.state = status.toLowerCase().replaceAll(' ', '-');
                item.append(el('span', String(index + 1), 'mlops-training-number'), el('strong', label), el('small', status));
                track.append(item);
            });
            panel.append(track);
            panel.append(el('p', 'After evaluation: review the candidate → connect the service. These are separate actions.', 'mlops-mode-desc'));
        }
        if (task.status === 'failed') {
            panel.append(el('p', evidence.blocked
                ? (!evidence.fittingStarted && evidence.index >= 0 ? 'Model fitting did not start. ' : '') + 'Resolve the requirements below, inspect the dataset, then submit a new run.'
                : 'Open the technical details and inspect the worker error before retrying. This is not classified as an insufficient-data condition.'));
            if (task.error_message && !String(task.error_message).trim().startsWith('{')) panel.append(el('p', String(task.error_message).slice(0, 500), 'mlops-note note-warn'));
            const reportArea = el('div');
            const recorded = Object.keys(evidence.report).length ? evidence.report : savedValidationReports.get(evidence.datasetId) || {};
            if (evidence.blocked || Object.keys(recorded).length) reportArea.append(validationReport(recorded));
            panel.append(reportArea);
            // Full saved reports remain inspectable even when an old job message was truncated.
            if (evidence.datasetId) {
                const inspect = el('button', 'Load complete validation report', 'mlops-btn mlops-btn-small'); inspect.type = 'button';
                inspect.addEventListener('click', async () => {
                    inspect.disabled = true; inspect.textContent = 'Loading validation report…';
                    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
                    try {
                        const response = await fetch('/api/ml/datasets/' + encodeURIComponent(evidence.datasetId) + '/validation-report',
                            {credentials: 'same-origin', signal: controller.signal, headers: {Accept: 'application/json'}});
                        if (!response.ok) throw new Error('HTTP ' + response.status);
                        const data = await response.json();
                        savedValidationReports.set(evidence.datasetId, data.validation_report);
                        if (savedValidationReports.size > 32) savedValidationReports.delete(savedValidationReports.keys().next().value);
                        reportArea.replaceChildren();
                        if (evidence.code === 'QUALITY_GATES_FAILED' && Object.keys(evidence.report).length) {
                            reportArea.append(el('h4', 'Checks immediately before fitting'), validationReport(evidence.report), el('h4', 'Saved dataset checks'));
                        }
                        reportArea.append(validationReport(data.validation_report));
                        inspect.textContent = 'Reload validation report';
                    } catch (_) { inspect.textContent = 'Report unavailable · retry'; }
                    finally { clearTimeout(timer); inspect.disabled = false; }
                });
                panel.append(inspect);
            }
        }
        if (evidence.datasetId && openDataset) {
            const inspect = el('button', 'Inspect dataset & records', 'mlops-btn mlops-btn-small'); inspect.type = 'button';
            inspect.addEventListener('click', () => openDataset(evidence.datasetId)); panel.append(inspect);
        }
        if (!compact && task.job_id) panel.append(actions({job_id: task.job_id}));
        const technical = el('details', null, 'mlops-validation-technical');
        technical.append(el('summary', 'Job details · ' + String(task.job_id || 'Not recorded')),
            el('pre', JSON.stringify({job_id: task.job_id, status: task.status, stage: evidence.current,
                error_code: evidence.code, error_message: task.error_message}, null, 2)));
        panel.append(technical);
        return panel;
    }

    const launch = document.getElementById('notebook-launch');
    if (launch) workspaceLink(launch);
    window.MLOpsDiagnostics = {render, jobEvidence, trainingPanel, validationReport, actions, updatePanel, contextFor, workflowUrl, safeNotebookUrl, datasetLink: id => actions({dataset_id: String(id)})};
})();
