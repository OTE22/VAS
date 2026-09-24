/* Read-only diagnostics for dataset jobs. Notebook kernels live separately. */
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
    let workspace;
    function workspaceLink(panel) {
        if (!workspace) workspace = fetch('/api/ml/debug-workspace', {credentials: 'same-origin', cache: 'no-store'})
            .then(response => response.ok ? response.json() : {}).catch(() => ({}));
        workspace.then(data => {
            if (!data.url) return;
            const url = new URL(data.url, window.location.origin);
            if (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(url.hostname))) return;
            const link = el('a', 'Open Notebook', 'mlops-btn mlops-btn-small');
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
        panel.append(el('summary', 'Pipeline diagnostics & notebook'));
        const data = task.details || {}, history = data.diagnostics_version === 1 && Array.isArray(data.stage_history) ? data.stage_history : [];
        if (!history.length) panel.append(el('p', 'Stage evidence is not available for this job. New dataset builds record each step.'));
        else {
            const list = el('ol', null, 'mlops-diagnostic-stages');
            history.forEach(event => {
                let status = event.status;
                if (status === 'running' && ['failed', 'cancelled'].includes(task.status)) status = 'interrupted';
                const row = el('li');
                row.dataset.status = status;
                row.append(el('strong', String(event.stage).replaceAll('_', ' ')), el('span', status, 'mlops-diagnostic-status'));
                const facts = [];
                if (typeof event.duration_seconds === 'number') facts.push(event.duration_seconds.toFixed(2) + ' s');
                Object.entries(event).forEach(([name, value]) => {
                    if (!['stage', 'status', 'started_at', 'duration_seconds'].includes(name)) facts.push(name.replaceAll('_', ' ') + ': ' + String(value));
                });
                if (facts.length) row.append(el('small', facts.join(' · ')));
                list.append(row);
            });
            panel.append(list);
        }
        const failure = data.failure;
        if (failure) {
            panel.append(el('p', 'Failure: ' + failure.code, 'mlops-note note-bad'));
            if (Array.isArray(failure.failed_checks) && failure.failed_checks.length) panel.append(el('p', 'Failed checks: ' + failure.failed_checks.join(', ')));
            if (Array.isArray(failure.frames) && failure.frames.length) {
                panel.append(el('pre', failure.frames.map(frame => frame.module + ':' + frame.line + ' · ' + frame.function).join('\n'), 'mlops-diagnostic-trace'));
            }
            if (failure.log_reference) panel.append(el('p', 'Worker log reference: ' + failure.log_reference));
        }
        panel.append(download('/api/ml/jobs/' + encodeURIComponent(key) + '/debug-notebook'));
        workspaceLink(panel);
        panel.append(el('p', 'Open the notebook in your Jupyter workspace. It inspects saved evidence and rechecks an available snapshot; it does not rerun production jobs.', 'mlops-mode-desc'));
        return panel;
    }
    const launch = document.getElementById('notebook-launch');
    if (launch) workspaceLink(launch);
    window.MLOpsDiagnostics = {render, datasetLink: id => download('/api/ml/datasets/' + encodeURIComponent(id) + '/debug-notebook')};
})();
