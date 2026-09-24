/* ML tool settings use the same authorization, CSRF and audit path as Admin Settings. */
(() => {
    'use strict';
    const root = document.getElementById('mlops-tool-controls');
    if (!root) return;
    const definitions = [
        ['MLFLOW_ENABLED', 'mlflow', 'Experiment tracking · MLflow', 'Track experiments and model versions. Local dataset lineage is always recorded.'],
        ['XGBOOST_ENABLED', 'xgboost', 'XGBoost algorithms', 'Make installed XGBoost algorithms available for compatible future runs.'],
        ['OPTUNA_ENABLED', 'optuna', 'Parameter tuning · Optuna', 'Allow tuning for supported XGBoost runs. Also select tuning in each training run.'],
        ['SHAP_ENABLED', 'shap', 'Model explanations · SHAP', 'Allow explanations for supported models. Also select explanations in each training run.'],
        ['ML_DRIFT_MONITORING_ENABLED', 'drift', 'Scheduled drift monitoring', 'Requires an ML worker restart after a change and sufficient qualifying production inference samples. This setting alone does not activate monitoring.']
    ];
    const rows = [];
    let caps = {}, refreshing = false;
    const boolean = value => value === true || value === 'true' ? true : value === false || value === 'false' ? false : null;
    function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text) n.textContent = text; return n; }
    async function request(path, options = {}) {
        const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20000);
        try {
            const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', ...options,
                signal: controller.signal, headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}});
            const data = await response.json();
            if (!response.ok) throw new Error(response.status === 403 ? 'Administrator settings permission required.' : response.status === 401 ? 'Session expired. Sign in again.' : typeof data.detail === 'string' ? data.detail : 'Settings request failed (' + response.status + ').');
            return data;
        } finally { clearTimeout(timer); }
    }
    const toolbar = el('div', 'mlops-header-actions');
    const refresh = el('button', 'mlops-btn mlops-btn-small', 'Refresh tool status'); refresh.type = 'button';
    const settingsLink = el('a', 'mlops-btn mlops-btn-small', 'Advanced settings'); settingsLink.href = '/admin/settings';
    toolbar.append(refresh, settingsLink); root.append(toolbar);
    root.append(el('p', 'mlops-mode-desc', 'Switches change the saved configuration. Availability and running processes are shown separately. Changes are recorded in the settings audit log.'));
    const list = el('div', 'mlops-tools-grid'); root.append(list);
    root.append(el('p', 'mlops-mode-desc', 'Always on: dataset lineage, validation and reproducibility. Data collection remains an explicit job. Decision modes and model approval stay in their existing controls.'));
    function paint(row, setting) {
        row.setting = setting;
        const stored = boolean(setting.stored_value), effective = boolean(setting.effective_value);
        row.value = stored === null ? effective : stored;
        row.input.checked = row.value === true;
        row.input.disabled = row.busy || row.value === null || setting.is_readonly || setting.can_edit === false;
        row.state.textContent = row.value === null ? 'Unknown' : row.value ? 'On' : 'Off';
        const cap = caps[row.capability];
        row.detail.textContent = 'Saved: ' + (stored === null ? 'default / environment' : stored ? 'On' : 'Off')
            + ' · API effective: ' + (effective === null ? 'unknown' : effective ? 'On' : 'Off')
            + (row.capability === 'drift' ? ' · Worker state is not confirmed here. Production monitoring remains gated.' : ' · Availability: ' + (cap ? cap.status : 'not confirmed'));
        if (setting.requires_worker_restart) row.detail.textContent += ' Restart the ML worker to apply changes.';
        else row.detail.textContent += ' Future jobs reload settings; running jobs are not reconfigured.';
        if (setting.is_readonly || setting.can_edit === false) row.detail.textContent += ' Read-only setting.';
    }
    async function loadRow(row) {
        try { paint(row, await request('/api/settings/' + row.key)); }
        catch (error) { row.input.disabled = true; row.value = null; row.state.textContent = 'Unavailable'; row.detail.textContent = error.message + ' Refresh to retry.'; }
    }
    async function refreshAll() {
        if (refreshing || rows.some(row => row.busy)) return;
        refreshing = true; refresh.disabled = true;
        rows.forEach(row => { row.input.disabled = true; });
        try {
            try { caps = (await request('/api/ml/capabilities')).items || {}; } catch (_) { caps = {}; }
            await Promise.all(rows.map(loadRow));
        } finally { refreshing = false; refresh.disabled = false; }
    }
    for (const [key, capability, name, description] of definitions) {
        const box = el('article', 'mlops-tool');
        const heading = el('label', 'mlops-tool-heading'); heading.htmlFor = 'tool-' + key;
        const input = el('input'); input.type = 'checkbox'; input.id = 'tool-' + key; input.setAttribute('role', 'switch'); input.disabled = true;
        const state = el('span', 'mlops-tool-state', 'Checking');
        heading.append(el('strong', null, name), input, state);
        const detail = el('p', 'mlops-tool-detail', 'Loading saved configuration…'); detail.id = input.id + '-detail'; input.setAttribute('aria-describedby', detail.id);
        const message = el('p', 'mlops-note'); message.setAttribute('role', 'status');
        box.append(heading, el('p', 'mlops-mode-desc', description), detail, message); list.append(box);
        const row = {key, capability, input, state, detail, message, value: null, busy: false}; rows.push(row);
        input.addEventListener('change', async () => {
            if (row.busy || refreshing || row.value === null) { input.checked = row.value === true; return; }
            const wanted = input.checked;
            input.checked = row.value; input.disabled = true; row.busy = true; refresh.disabled = true;
            message.textContent = 'Saving…'; message.className = 'mlops-note';
            try {
                const result = await request('/api/settings/' + key, {method: 'PUT', body: JSON.stringify({value: String(wanted), change_reason: (wanted ? 'Enabled ' : 'Disabled ') + name + ' from ML Operations tool controls.'})});
                if (!result.saved || !result.setting) throw new Error('Save was not confirmed. Refresh before retrying.');
                paint(row, result.setting);
                message.textContent = result.message || 'Configuration saved.';
                message.className = 'mlops-note ' + (result.apply_error ? 'note-bad' : result.restart_required ? 'note-warn' : 'note-ok');
                if (capability === 'drift') message.textContent += ' Worker restart and production sample gates still apply.';
                document.getElementById('platform-refresh')?.click();
                document.getElementById('refresh-overview-btn')?.click();
            } catch (error) {
                message.textContent = (error.name === 'AbortError' ? 'Save timed out; the server may have saved it.' : error.message) + ' Reloading the saved value; refresh status before retrying.';
                message.className = 'mlops-note note-bad';
            } finally {
                try { caps = (await request('/api/ml/capabilities')).items || {}; } catch (_) { caps = {}; }
                row.busy = false; await loadRow(row);
                refresh.disabled = rows.some(item => item.busy);
            }
        });
    }
    refresh.addEventListener('click', refreshAll);
    refreshAll();
})();
