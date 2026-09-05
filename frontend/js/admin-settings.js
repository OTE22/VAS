// Admin Settings Management
// ==========================
// Backend provides TYPED metadata per setting (stored/effective value, source,
// unit, min/max/enum, apply mode). This page renders it honestly:
//   "applied immediately" vs "next job run" vs "RESTART REQUIRED".
// Security: all untrusted text via textContent/escapeHtml; no inline onclick.
// Values 0 and false are VALID — only null/undefined are treated as empty.

let settingsData = null;
let auditLogData = null;
let saveInFlight = false;

document.addEventListener('DOMContentLoaded', async () => {
    await loadAllData();

    const settingForm = document.getElementById('setting-form');
    if (settingForm) {
        settingForm.addEventListener('submit', handleSettingUpdate);
    }

    // Delegated listeners (no inline onclick anywhere)
    const filters = document.getElementById('category-filters');
    if (filters) {
        filters.addEventListener('click', (e) => {
            const btn = e.target.closest('.category-filter-btn');
            if (btn && btn.dataset.category) filterByCategory(btn.dataset.category);
        });
    }
    const container = document.getElementById('settings-container');
    if (container) {
        container.addEventListener('click', (e) => {
            const editBtn = e.target.closest('.setting-btn.edit');
            if (editBtn) {
                const card = editBtn.closest('.setting-card');
                if (card && card.dataset.key) editSetting(card.dataset.key);
            }
        });
    }

    // Retention tools panel
    bindRetentionTools();
    loadMLCapabilities();
});

// ---------------------------------------------------------------------------
// Fetch helpers: cookies + no-store + content-type-aware error handling
// ---------------------------------------------------------------------------

async function apiFetch(url, options = {}) {
    const response = await fetch(url, {
        credentials: 'include',
        cache: 'no-store',
        ...options,
        headers: {
            // require_settings_csrf (backend/routes/settings.py) rejects any
            // cookie-authenticated write without this. It was added to the
            // settings writer without this side of it, so every Save Changes
            // returned "CSRF check failed: X-Requested-With header required".
            // Bearer-token callers are exempt, which is why the test suite
            // never saw it. Sent from the one place this page reaches the API.
            'X-Requested-With': 'XMLHttpRequest',
            ...(options.headers || {}),
        },
    });
    const contentType = response.headers.get('content-type') || '';
    let body = null;
    if (contentType.includes('application/json')) {
        try { body = await response.json(); } catch (e) { body = null; }
    } else {
        try { body = { detail: (await response.text()).slice(0, 300) }; } catch (e) { body = null; }
    }
    if (!response.ok) {
        const detail = (body && (body.detail || body.message)) || `HTTP ${response.status}`;
        const err = new Error(typeof detail === 'string' ? detail : describeStructuredError(detail));
        err.status = response.status;
        throw err;
    }
    return body;
}

// A structured refusal (e.g. MODE_GATED from the ML decision-authority gate)
// is rendered as its message plus each gate's state — never raw JSON.
function describeStructuredError(detail) {
    if (!detail || typeof detail !== 'object') return String(detail);
    const lines = [];
    if (detail.message) lines.push(String(detail.message));
    if (Array.isArray(detail.gates) && detail.gates.length) {
        detail.gates.forEach(function (g) {
            if (!g || typeof g !== 'object') return;
            lines.push((g.ok ? '✓ ' : '✗ ') + String(g.label || g.gate) + ': ' + String(g.status));
        });
    } else if (Array.isArray(detail.unmet_gates) && detail.unmet_gates.length) {
        detail.unmet_gates.forEach(function (g) { lines.push('• ' + String(g)); });
    }
    if (detail.note) lines.push(String(detail.note));
    if (!lines.length) return JSON.stringify(detail);
    return lines.join('\n');
}

function escapeHtml(text) {
    // Explicit null/undefined check — 0 and false must render as "0"/"false"
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

function displayValue(v) {
    if (v === null || v === undefined) return '(empty)';
    if (v === '') return '(empty)';
    return String(v);
}

// Inline notice area (no alert() for routine feedback)
function showNotice(message, kind = 'info') {
    let area = document.getElementById('settings-notice-area');
    if (!area) {
        area = document.createElement('div');
        area.id = 'settings-notice-area';
        area.style.cssText = 'position:fixed;top:80px;right:20px;z-index:10050;display:flex;flex-direction:column;gap:8px;max-width:420px;';
        document.body.appendChild(area);
    }
    const colors = { info: '#00b0ff', success: '#00ff96', warn: '#ffc107', error: '#ff5252' };
    const c = colors[kind] || colors.info;
    const note = document.createElement('div');
    note.style.cssText = `background:rgba(10,20,25,0.95);border:1px solid ${c};border-left:4px solid ${c};color:#eee;padding:0.7rem 1rem;border-radius:6px;font-size:0.85rem;box-shadow:0 4px 16px rgba(0,0,0,0.5);`;
    note.textContent = message;
    area.appendChild(note);
    setTimeout(() => note.remove(), kind === 'error' ? 12000 : 7000);
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function loadAllData() {
    await Promise.all([loadSettings(), loadAuditLog()]);
}

async function loadSettings() {
    const container = document.getElementById('settings-container');
    if (!container) return;

    container.innerHTML = '<div class="loading"><i class="fas fa-spinner fa-spin"></i> Loading settings...</div>';
    attachRefreshButtonListener();

    try {
        settingsData = await apiFetch('/api/settings');
        renderCategoryFilters();
        renderSettings();
    } catch (error) {
        container.innerHTML = `<div class="error"><i class="fas fa-exclamation-triangle"></i> Error loading settings: ${escapeHtml(error.message)}</div>`;
        console.error('Error loading settings:', error);
        attachRefreshButtonListener();
    }
}

function renderCategoryFilters() {
    const container = document.getElementById('category-filters');
    if (!container || !settingsData) return;

    container.innerHTML = `
        <button class="category-filter-btn active" data-category="all">
            <i class="fas fa-th"></i> All Settings
        </button>
        ${settingsData.categories.map(cat => `
            <button class="category-filter-btn" data-category="${escapeHtml(cat)}">
                <i class="fas fa-tag"></i> ${escapeHtml(cat.charAt(0).toUpperCase() + cat.slice(1))}
            </button>
        `).join('')}
    `;
}

function filterByCategory(category) {
    document.querySelectorAll('.category-filter-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.category === category);
    });
    renderSettings(category);
}

// ---------------------------------------------------------------------------
// Rendering: stored vs effective, source, apply mode — the honesty layer
// ---------------------------------------------------------------------------

const APPLY_MODE_LABELS = {
    immediate: { text: 'live', icon: 'fa-bolt', color: '#00ff96', title: 'Applies immediately when saved' },
    next_request: { text: 'live', icon: 'fa-bolt', color: '#00ff96', title: 'Applies from the next request' },
    next_job_run: { text: 'next job', icon: 'fa-clock', color: '#4ecdc4', title: 'Applies at the next scheduled job run' },
    api_restart: { text: 'restart', icon: 'fa-power-off', color: '#ffc107', title: 'Requires API restart to take effect' },
    worker_restart: { text: 'restart', icon: 'fa-power-off', color: '#ffc107', title: 'Requires worker restart' },
    container_recreate: { text: 'recreate', icon: 'fa-box', color: '#ff9800', title: 'Requires docker compose up -d (container recreate)' },
    index_rebuild: { text: 'rebuild', icon: 'fa-sitemap', color: '#e17055', title: 'Requires vector index rebuild' },
};

function applyModeBadge(setting) {
    const m = APPLY_MODE_LABELS[setting.apply_mode] || APPLY_MODE_LABELS.api_restart;
    return `<span class="setting-badge" title="${escapeHtml(m.title)}" style="background:${m.color}20;color:${m.color};border:1px solid ${m.color}40;">
        <i class="fas ${m.icon}"></i> ${m.text}</span>`;
}

function sourceBadge(setting) {
    if (setting.source === 'database') {
        const warn = setting.overridden
            ? ` title="Admin value differs from environment (env=${escapeHtml(displayValue(setting.env_value))}); the admin value wins"`
            : ' title="Value set by an admin (stored in database)"';
        return `<span class="setting-badge"${warn} style="background:#00b0ff20;color:#00b0ff;border:1px solid #00b0ff40;"><i class="fas fa-database"></i> db${setting.overridden ? ' ⚠' : ''}</span>`;
    }
    if (setting.source === 'environment') {
        return `<span class="setting-badge" title="Value comes from the environment (.env / docker-compose)" style="background:#a29bfe20;color:#a29bfe;border:1px solid #a29bfe40;"><i class="fas fa-server"></i> env</span>`;
    }
    return `<span class="setting-badge" title="Built-in default value" style="background:#63636320;color:#999;border:1px solid #63636340;">default</span>`;
}

function renderSettings(category = 'all') {
    const container = document.getElementById('settings-container');
    if (!container || !settingsData) return;

    let settingsToShow = category === 'all'
        ? settingsData.all_settings
        : (settingsData.settings_by_category[category] || []);

    if (settingsToShow.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <i class="fas fa-inbox"></i>
                <h3>No Settings Found</h3>
                <p>No settings match the selected category.</p>
            </div>
        `;
        return;
    }

    container.innerHTML = settingsToShow.map(setting => {
        const unitBadge = setting.unit ? `
            <span class="setting-unit-badge" style="background:#4ecdc420;color:#4ecdc4;border:1px solid #4ecdc440;">${escapeHtml(setting.unit)}</span>` : '';

        const stored = setting.is_sensitive ? '***HIDDEN***' : displayValue(setting.stored_value);
        const effective = setting.is_sensitive ? '***HIDDEN***' : displayValue(setting.effective_value);
        const diverged = !setting.is_sensitive && String(stored) !== String(effective);

        return `
        <div class="setting-card ${setting.is_readonly ? 'readonly' : ''}" data-key="${escapeHtml(setting.key)}">
            <div class="setting-card-header">
                <div class="setting-key" title="${escapeHtml(setting.description)}">${escapeHtml(setting.key)}</div>
                <div class="setting-badges">
                    ${setting.is_sensitive ? '<span class="setting-badge sensitive" title="Sensitive value (hidden)"><i class="fas fa-lock"></i></span>' : ''}
                    ${setting.is_readonly ? '<span class="setting-badge readonly" title="Read-only"><i class="fas fa-ban"></i></span>' : ''}
                    <span class="setting-badge category">${escapeHtml(setting.category)}</span>
                    ${applyModeBadge(setting)}
                    ${sourceBadge(setting)}
                    ${unitBadge}
                </div>
            </div>
            <div class="setting-value-container">
                <div class="setting-value ${setting.is_sensitive ? 'hidden' : ''}">
                    ${escapeHtml(effective)}${setting.unit && !setting.is_sensitive ? ` <small style="color:#4ecdc4;font-weight:500;">${escapeHtml(setting.unit)}</small>` : ''}
                    ${diverged ? `<div style="font-size:0.7rem;color:#ffc107;margin-top:0.2rem;" title="Stored value differs from what the running process uses — apply action or restart pending">stored: ${escapeHtml(stored)} <i class="fas fa-hourglass-half"></i></div>` : ''}
                </div>
                <span class="setting-value-type">${escapeHtml(setting.value_type)}</span>
            </div>
            <div class="setting-actions">
                ${setting.can_edit ? `
                    <button class="setting-btn edit" type="button" title="Edit ${escapeHtml(setting.key)}">
                        <i class="fas fa-edit"></i>
                    </button>
                ` : `
                    <button class="setting-btn" disabled title="Readonly: ${escapeHtml(setting.key)}">
                        <i class="fas fa-lock"></i>
                    </button>
                `}
            </div>
        </div>
    `}).join('');
}

function attachRefreshButtonListener() {
    const refreshBtn = document.getElementById('refresh-settings-btn');
    if (refreshBtn) {
        const newBtn = refreshBtn.cloneNode(true);
        refreshBtn.parentNode.replaceChild(newBtn, refreshBtn);
        newBtn.addEventListener('click', async () => { await loadAllData(); });
    }
}

// ---------------------------------------------------------------------------
// Audit log
// ---------------------------------------------------------------------------

async function loadAuditLog() {
    const tbody = document.getElementById('audit-log-body');
    if (!tbody) return;

    tbody.innerHTML = '<tr><td colspan="6" class="loading"><i class="fas fa-spinner fa-spin"></i> Loading audit log...</td></tr>';

    try {
        auditLogData = await apiFetch('/api/settings/audit/log?limit=50');
        renderAuditLog();
    } catch (error) {
        tbody.innerHTML = `<tr><td colspan="6" class="error"><i class="fas fa-exclamation-triangle"></i> Error loading audit log: ${escapeHtml(error.message)}</td></tr>`;
        console.error('Error loading audit log:', error);
    }
}

const AUDIT_ACTION_LABELS = {
    value_saved: { text: 'saved', color: '#ffc107' },
    value_applied: { text: 'applied', color: '#00ff96' },
    application_failed: { text: 'apply failed', color: '#ff5252' },
    retention_dry_run: { text: 'dry run', color: '#4ecdc4' },
    retention_executed: { text: 'retention run', color: '#00b0ff' },
    setting_reverted: { text: 'reverted', color: '#ff9800' },
};

function renderAuditLog() {
    const tbody = document.getElementById('audit-log-body');
    if (!tbody || !auditLogData) return;

    if (auditLogData.logs.length === 0) {
        tbody.innerHTML = `
            <tr><td colspan="6" class="empty-state">
                <i class="fas fa-inbox"></i>
                <h3>No Changes Yet</h3>
                <p>Settings change history will appear here.</p>
            </td></tr>
        `;
        return;
    }

    tbody.innerHTML = auditLogData.logs.map(log => {
        const action = AUDIT_ACTION_LABELS[log.action] || AUDIT_ACTION_LABELS.value_saved;
        return `
        <tr>
            <td><strong>${escapeHtml(log.setting_key)}</strong>
                <br><small style="color:${action.color};">${escapeHtml(action.text)}</small></td>
            <td><code>${escapeHtml(displayValue(log.old_value))}</code></td>
            <td><code>${escapeHtml(displayValue(log.new_value))}</code></td>
            <td>${escapeHtml(log.changed_by_username)}</td>
            <td>${escapeHtml(log.change_reason)}</td>
            <td>
                ${escapeHtml(log.created_at_formatted)}
                ${log.time_ago ? `<br><small style="color: rgba(0, 255, 150, 0.6);">${escapeHtml(log.time_ago)}</small>` : ''}
            </td>
        </tr>
    `}).join('');
}

// ---------------------------------------------------------------------------
// Edit modal: typed controls per value_type / allowed_values
// ---------------------------------------------------------------------------

function buildValueControl(setting) {
    const container = document.getElementById('setting-input-container');
    if (!container) return;
    container.innerHTML = '';

    let control;
    // Pre-fill from the EFFECTIVE value — what the process is actually running
    // — not the stored row. When an environment variable overrides the stored
    // value, or a stored row predates a code change, those two differ, and
    // pre-filling `stored_value` showed the admin a number the system was not
    // using and offered to "keep" it.
    const currentRaw = setting.effective_value !== undefined && setting.effective_value !== null
        ? setting.effective_value
        : setting.stored_value;
    const current = setting.is_sensitive ? '' : currentRaw;

    if (Array.isArray(setting.allowed_values) && setting.allowed_values.length > 0) {
        control = document.createElement('select');
        setting.allowed_values.forEach(v => {
            const opt = document.createElement('option');
            opt.value = v;
            opt.textContent = v;
            if (String(current) === String(v)) opt.selected = true;
            control.appendChild(opt);
        });
    } else if (setting.value_type === 'boolean') {
        control = document.createElement('select');
        [['true', 'true (enabled)'], ['false', 'false (disabled)']].forEach(([v, label]) => {
            const opt = document.createElement('option');
            opt.value = v;
            opt.textContent = label;
            if (String(current) === v) opt.selected = true;
            control.appendChild(opt);
        });
    } else if (setting.value_type === 'integer' || setting.value_type === 'float') {
        control = document.createElement('input');
        control.type = 'number';
        if (setting.minimum !== null && setting.minimum !== undefined) control.min = setting.minimum;
        if (setting.maximum !== null && setting.maximum !== undefined) control.max = setting.maximum;
        control.step = setting.value_type === 'integer' ? '1' : 'any';
        // Explicit null check: 0 must populate as "0", not empty
        control.value = (current === null || current === undefined) ? '' : String(current);
    } else if (setting.is_sensitive) {
        control = document.createElement('input');
        control.type = 'password';
        control.placeholder = 'Enter new value (current hidden)';
        control.autocomplete = 'new-password';
    } else {
        control = document.createElement('input');
        control.type = 'text';
        control.value = (current === null || current === undefined) ? '' : String(current);
    }

    control.id = 'setting-new-value';
    control.className = 'security-input';
    control.required = true;
    container.appendChild(control);
}

function editSetting(key) {
    if (!settingsData) return;

    const setting = settingsData.all_settings.find(s => s.key === key);
    if (!setting || !setting.can_edit) return;

    document.getElementById('setting-key').value = setting.key;
    document.getElementById('setting-key-display').value = setting.key;
    document.getElementById('setting-type').value = setting.value_type;

    const oldField = document.getElementById('setting-old-value');
    if (oldField) {
        const stored = setting.is_sensitive ? '***HIDDEN***' : displayValue(setting.stored_value);
        const effective = setting.is_sensitive ? '***HIDDEN***' : displayValue(setting.effective_value);
        oldField.value = stored === effective ? stored : `stored: ${stored} | effective: ${effective}`;
    }

    buildValueControl(setting);
    document.getElementById('change-reason').value = '';

    // Typed hint from backend metadata (textContent — never HTML)
    const hint = document.getElementById('value-hint');
    if (hint) {
        const parts = [];
        if (setting.unit) parts.push(`Unit: ${setting.unit}`);
        if (setting.minimum !== null && setting.minimum !== undefined) parts.push(`min ${setting.minimum}`);
        if (setting.maximum !== null && setting.maximum !== undefined) parts.push(`max ${setting.maximum}`);
        const m = APPLY_MODE_LABELS[setting.apply_mode];
        if (m) parts.push(m.title);
        if (setting.value_hint) parts.push(setting.value_hint);
        hint.textContent = parts.join(' • ');
    }

    const modal = document.getElementById('setting-modal');
    // Shared lifecycle. This page had no Escape handling; the stack supplies
    // it, plus focus containment and the background scroll lock.
    if (modal) {
        window.ModalStack.open(modal, {
            backdropClose: true,
            onClose: () => closeSettingModal()
        });
    }
}

function closeSettingModal() {
    const modal = document.getElementById('setting-modal');
    if (!modal) return;
    if (window.ModalStack.isOpen(modal)) {
        window.ModalStack.close(modal);   // re-enters here via onClose
        return;
    }
    modal.style.display = 'none';
    // Business cleanup, preserved: a reopened dialog must not show the
    // previous setting's edits.
    const form = document.getElementById('setting-form');
    if (form) form.reset();
}

async function handleSettingUpdate(e) {
    e.preventDefault();
    if (saveInFlight) return;  // double-submit guard

    const key = document.getElementById('setting-key').value;
    const control = document.getElementById('setting-new-value');
    const changeReason = document.getElementById('change-reason').value;
    const newValue = control ? control.value : '';

    // Explicit empty check: '0' and 'false' are valid — only truly empty blocks
    if (!key || newValue === '' || newValue === null || newValue === undefined) {
        showNotice('Please enter a value', 'warn');
        return;
    }

    const submitBtn = e.target.querySelector('button[type="submit"], input[type="submit"]');
    saveInFlight = true;
    if (submitBtn) submitBtn.disabled = true;

    try {
        const data = await apiFetch(`/api/settings/${encodeURIComponent(key)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ value: String(newValue), change_reason: changeReason || null }),
        });

        closeSettingModal();
        await loadAllData();

        // HONEST feedback from the backend contract
        if (data.applied) {
            showNotice(data.message || `${key} saved and applied`, 'success');
        } else if (data.restart_required) {
            showNotice(data.message || `${key} saved — RESTART REQUIRED to take effect`, 'warn');
        } else if (data.apply_error) {
            showNotice(data.message || `${key} saved but apply FAILED`, 'error');
        } else {
            showNotice(data.message || `${key} saved`, 'info');
        }
    } catch (error) {
        showNotice(`Error updating setting: ${error.message}`, 'error');
        console.error('Error updating setting:', error);
    } finally {
        saveInFlight = false;
        if (submitBtn) submitBtn.disabled = false;
    }
}

// ---------------------------------------------------------------------------
// Retention tools (status / dry run / run now)
// ---------------------------------------------------------------------------

function bindRetentionTools() {
    const statusBtn = document.getElementById('retention-status-btn');
    const dryBtn = document.getElementById('retention-dryrun-btn');
    const runBtn = document.getElementById('retention-run-btn');
    const out = document.getElementById('retention-output');
    if (!out) return;

    const show = (obj) => { out.style.display = 'block'; out.textContent = JSON.stringify(obj, null, 2); };
    const busy = (btn, on) => { if (btn) btn.disabled = on; };

    if (statusBtn) statusBtn.addEventListener('click', async () => {
        busy(statusBtn, true);
        try { show(await apiFetch('/api/admin/retention/status')); }
        catch (e) { showNotice(`Retention status failed: ${e.message}`, 'error'); }
        finally { busy(statusBtn, false); }
    });

    if (dryBtn) dryBtn.addEventListener('click', async () => {
        busy(dryBtn, true);
        try {
            const r = await apiFetch('/api/admin/retention/run?dry_run=true', { method: 'POST' });
            show(r);
            showNotice(`Dry run: ${r.candidate_rows ?? 0} candidate rows, nothing deleted`, 'info');
        } catch (e) { showNotice(`Dry run failed: ${e.message}`, 'error'); }
        finally { busy(dryBtn, false); }
    });

    if (runBtn) runBtn.addEventListener('click', async () => {
        if (!window.confirm('Run retention NOW? Expired detections and their images will be permanently deleted.')) return;
        busy(runBtn, true);
        try {
            const r = await apiFetch('/api/admin/retention/run?dry_run=false', { method: 'POST' });
            show(r);
            showNotice(`Retention executed: ${r.rows_deleted ?? 0} rows, ${r.files_deleted ?? 0} files deleted`, 'success');
            await loadAuditLog();
        } catch (e) { showNotice(`Retention run failed: ${e.message}`, 'error'); }
        finally { busy(runBtn, false); }
    });
}

// Backdrop clicks belong to ModalStack (opted in at open time).

// Keep global exports for the modal close button in HTML
window.filterByCategory = filterByCategory;
window.editSetting = editSetting;
window.closeSettingModal = closeSettingModal;


// ---------------------------------------------------------------------------
// CSP-safe event registration. Replaces the inline onclick attributes in
// admin/settings.html, which `script-src 'self'` blocks.
// ---------------------------------------------------------------------------
Actions.register({
    closeSettingModal,
});

// Capability health uses the existing authenticated settings request helper.
async function loadMLCapabilities() {
    const area = document.getElementById('ml-capability-health');
    if (!area) return;
    area.textContent = 'Checking ML capability availability?';
    try {
        const data = await apiFetch('/api/ml/capabilities');
        const table = document.createElement('table'); table.className = 'settings-audit-table';
        table.setAttribute('aria-label', 'ML capability registry');
        const head = document.createElement('tr');
        for (const label of ['Capability', 'Status', 'Action']) { const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = label; head.append(cell); }
        const thead = document.createElement('thead'); thead.append(head); table.append(thead);
        const body = document.createElement('tbody');
        for (const [name, item] of Object.entries(data.items || {})) {
            const row = document.createElement('tr');
            for (const value of [name, item.status, item.action]) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
            body.append(row);
        }
        table.append(body); area.replaceChildren(table);
        if (!body.children.length) area.textContent = 'No capabilities reported. Refresh after checking the API version.';
    } catch (error) { area.textContent = 'ML capability status could not load: ' + error.message + '. Restore ML management access and refresh.'; }
}
