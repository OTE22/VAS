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
    const launch = document.getElementById('notebook-launch');
    if (launch) workspaceLink(launch);
    window.MLOpsDiagnostics = {render, actions, updatePanel, contextFor, workflowUrl, safeNotebookUrl, datasetLink: id => actions({dataset_id: String(id)})};
})();
