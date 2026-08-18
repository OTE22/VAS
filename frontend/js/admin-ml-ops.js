/**
 * ML Operations page — /admin/ml-ops
 * ==================================
 * Rules remain the production decision system. This page shows all four
 * decision modes with their exact gate reasons, label/data readiness,
 * the model registry (approve-to-shadow / reject / rollback), the shadow
 * comparison summary (descriptive only — rule threat severity and anomaly
 * bands are different concepts), recent predictions with fallback status,
 * drift reports (observations only), manual training/dataset builds,
 * the labels queue, retraining policy, and the ML audit log.
 *
 * House contracts: DOM building only (no markup injection), typed
 * normalizers (no silent zero/false coercion), AbortController with
 * latest-wins generations, bounded job polling, pagehide cleanup,
 * no browser dialogs, no filesystem paths rendered (the API is path-free).
 */

(function () {
    'use strict';

    const DEBUG = false;

    const API_TIMEOUT_MS = 20000;
    const JOB_POLL_INTERVAL_MS = 1500;
    const JOB_POLL_MAX = 400;
    const PAGE_SIZE = 10;
    const MODE_ORDER = ['rules', 'shadow', 'hybrid', 'ml'];

    function debugLog() {
        if (DEBUG && window.console) console.log.apply(console, arguments);
    }

    // ============================================
    // Typed normalizers — never coerce absent data to 0/false
    // ============================================

    function toBoolean(value) {
        return value === true || value === 'true' || value === 1;
    }

    function toFiniteNumber(value) {
        if (value === null || value === undefined || value === '') return null;
        const num = Number(value);
        return Number.isFinite(num) ? num : null;
    }

    function formatMetric(value, digits) {
        const num = toFiniteNumber(value);
        if (num === null) return 'N/A';
        return (digits === undefined) ? String(num) : num.toFixed(digits);
    }

    function toText(value, fallback) {
        if (value === null || value === undefined || value === '') {
            return (fallback === undefined) ? '—' : fallback;
        }
        return String(value);
    }

    function formatDateTime(value) {
        if (!value) return '—';
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return String(value);
        return parsed.toLocaleString();
    }

    // ============================================
    // DOM helpers — createElement/textContent only
    // ============================================

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function faIcon(classes) {
        const icon = el('i', classes);
        icon.setAttribute('aria-hidden', 'true');
        return icon;
    }

    function chip(text, tone) {
        return el('span', 'mlops-chip' + (tone ? ' chip-' + tone : ''), text);
    }

    function getElement(id) {
        return document.getElementById(id);
    }

    function jsonBlock(value) {
        let text;
        try {
            text = JSON.stringify(value, null, 2);
        } catch (_) {
            text = String(value);
        }
        return el('div', 'mlops-json-block', text);
    }

    function kvList(pairs) {
        const dl = el('dl', 'mlops-kv');
        for (const pair of pairs) {
            dl.appendChild(el('dt', null, pair[0]));
            dl.appendChild(el('dd', null, pair[1]));
        }
        return dl;
    }

    function statGrid(stats) {
        const grid = el('div', 'mlops-stat-grid');
        for (const stat of stats) {
            const box = el('div', 'mlops-stat');
            box.appendChild(el('div', 'stat-value', stat.value));
            box.appendChild(el('div', 'stat-label', stat.label));
            grid.appendChild(box);
        }
        return grid;
    }

    function setNote(id, message, tone) {
        const node = getElement(id);
        if (!node) return;
        node.textContent = toText(message, '');
        node.className = 'mlops-note' + (tone ? ' note-' + tone : '');
    }

    // ============================================
    // API client (cookie auth; CSRF header on mutations; timeouts)
    // ============================================

    function ApiError(message, opts) {
        const e = new Error(message);
        e.status = (opts && opts.status) || null;
        e.code = (opts && opts.code) || null;
        e.detailExtra = (opts && opts.detailExtra) || null;
        e.aborted = !!(opts && opts.aborted);
        return e;
    }

    async function api(path, options) {
        options = options || {};
        const method = (options.method || 'GET').toUpperCase();
        const url = new URL(path, window.location.origin);
        if (options.params) {
            for (const key of Object.keys(options.params)) {
                const v = options.params[key];
                if (v !== undefined && v !== null && v !== '') url.searchParams.set(key, String(v));
            }
        }
        const timeoutCtl = new AbortController();
        const timer = window.setTimeout(function () { timeoutCtl.abort(); }, options.timeout || API_TIMEOUT_MS);
        const signals = [timeoutCtl.signal];
        if (options.signal) signals.push(options.signal);
        const signal = (typeof AbortSignal.any === 'function') ? AbortSignal.any(signals) : (options.signal || timeoutCtl.signal);

        const headers = { 'Accept': 'application/json' };
        if (method !== 'GET' && method !== 'HEAD') {
            headers['X-Requested-With'] = 'XMLHttpRequest'; // CSRF header
            if (options.body !== undefined) headers['Content-Type'] = 'application/json';
        }

        let response;
        try {
            response = await fetch(url.toString(), {
                method: method, credentials: 'include', cache: 'no-store',
                headers: headers,
                body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
                signal: signal
            });
        } catch (err) {
            window.clearTimeout(timer);
            if (err && err.name === 'AbortError') throw ApiError('Request cancelled', { aborted: true });
            throw ApiError('Network error — backend unreachable', { status: 0 });
        }
        window.clearTimeout(timer);

        if (response.status === 401) {
            window.location.href = '/login';
            throw ApiError('Session expired', { status: 401, code: 'AUTH_EXPIRED' });
        }
        if (!response.ok) {
            let code = null, detailExtra = null;
            let message = 'Request failed (' + response.status + ')';
            try {
                const body = await response.json();
                const detail = body && body.detail;
                if (detail && typeof detail === 'object') {
                    code = detail.error_code || null;
                    detailExtra = detail;
                    if (typeof detail.message === 'string') message = detail.message;
                } else if (typeof detail === 'string') {
                    message = detail;
                }
            } catch (_) { /* keep generic */ }
            throw ApiError(message, { status: response.status, code: code, detailExtra: detailExtra });
        }
        if (response.status === 204) return null;
        return response.json();
    }

    // ============================================
    // Request lifecycle — latest response wins per key
    // ============================================

    const requestControllers = new Map();
    const requestGenerations = new Map();

    function beginRequest(key) {
        const previous = requestControllers.get(key);
        if (previous) previous.abort();
        const controller = new AbortController();
        requestControllers.set(key, controller);
        const gen = (requestGenerations.get(key) ?? 0) + 1;
        requestGenerations.set(key, gen);
        return {
            signal: controller.signal,
            isCurrent: function () { return requestGenerations.get(key) === gen; }
        };
    }

    function abortAllRequests() {
        for (const controller of requestControllers.values()) {
            try { controller.abort(); } catch (_) { /* noop */ }
        }
        requestControllers.clear();
    }

    // ============================================
    // State
    // ============================================

    const state = {
        currentMode: null,
        predictionsPage: 1,
        labelsPage: 1,
        auditPage: 1,
        activeJobId: null,
        activeJobKind: null,   // 'training' | 'collection'
        jobPollTimer: null,
        pendingAction: null    // { title, execute(reason) }
    };

    function stopJobPolling() {
        if (state.jobPollTimer) {
            window.clearTimeout(state.jobPollTimer);
            state.jobPollTimer = null;
        }
    }

    // ============================================
    // Inline reason panel (replaces browser dialogs)
    // ============================================

    function openActionPanel(title, execute) {
        state.pendingAction = { title: title, execute: execute };
        const panel = getElement('registry-action-panel');
        const titleNode = getElement('registry-action-title');
        const reasonInput = getElement('registry-action-reason');
        if (!panel || !titleNode || !reasonInput) return;
        titleNode.textContent = title;
        reasonInput.value = '';
        panel.hidden = false;
        panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        reasonInput.focus();
    }

    function closeActionPanel() {
        state.pendingAction = null;
        const panel = getElement('registry-action-panel');
        if (panel) panel.hidden = true;
    }

    async function confirmPendingAction() {
        const pending = state.pendingAction;
        const reasonInput = getElement('registry-action-reason');
        if (!pending || !reasonInput) return;
        const reason = reasonInput.value.trim();
        if (reason.length < 3) {
            setNote('mode-action-note', 'A reason of at least 3 characters is required.', 'bad');
            return;
        }
        closeActionPanel();
        await pending.execute(reason);
    }

    // ============================================
    // Overview: mode panel + readiness + capabilities
    // ============================================

    async function loadOverview() {
        const req = beginRequest('overview');
        try {
            const data = await api('/api/ml/overview', { signal: req.signal });
            if (!req.isCurrent()) return;
            renderModePanel(data && data.mode);
            renderLabelReadiness(data && data.label_readiness);
            renderDataReadiness(data && data.data_readiness);
            renderOptionalCapabilities(data && data.optional_capabilities);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderCardError('mode-cards', err);
            renderCardError('label-readiness-body', err);
            renderCardError('data-readiness-body', err);
            renderCardError('optional-capabilities-body', err);
        }
    }

    function renderCardError(id, err) {
        const body = getElement(id);
        if (!body) return;
        const box = el('div', 'mlops-note note-bad',
            'Failed to load: ' + toText(err && err.message, 'unknown error'));
        body.replaceChildren(box);
    }

    function renderModePanel(availability) {
        const badge = getElement('current-mode-badge');
        const cards = getElement('mode-cards');
        if (!cards) return;
        const modes = (availability && availability.modes) || {};
        state.currentMode = toText(availability && availability.current_mode, 'rules');
        if (badge) badge.textContent = state.currentMode.toUpperCase();

        const frag = document.createDocumentFragment();
        for (const modeName of MODE_ORDER) {
            const info = modes[modeName] || { available: false, reasons: ['no data'], description: '' };
            const isCurrent = modeName === state.currentMode;
            const available = toBoolean(info.available);
            const card = el('div', 'mlops-mode-card'
                + (isCurrent ? ' mode-current' : '')
                + (available ? '' : ' mode-unavailable'));

            const nameRow = el('div', 'mlops-mode-name', modeName);
            if (isCurrent) nameRow.appendChild(chip('current', 'ok'));
            else if (available) nameRow.appendChild(chip('available', 'info'));
            else nameRow.appendChild(chip('gated', 'warn'));
            card.appendChild(nameRow);

            card.appendChild(el('div', 'mlops-mode-desc', toText(info.description, '')));

            const reasons = Array.isArray(info.reasons) ? info.reasons : [];
            if (reasons.length) {
                const list = el('ul', 'mlops-mode-reasons');
                for (const reason of reasons) list.appendChild(el('li', null, reason));
                card.appendChild(list);
            }

            const activateBtn = el('button', 'mlops-btn', 'Activate');
            activateBtn.type = 'button';
            activateBtn.disabled = isCurrent;
            activateBtn.addEventListener('click', function () {
                openActionPanel('Switch decision mode to "' + modeName + '"', function (reason) {
                    return changeMode(modeName, reason);
                });
            });
            card.appendChild(activateBtn);
            frag.appendChild(card);
        }
        cards.replaceChildren(frag);
    }

    async function changeMode(mode, reason) {
        try {
            const result = await api('/api/ml/config/mode', {
                method: 'PUT', body: { mode: mode, reason: reason }
            });
            setNote('mode-action-note',
                'Mode changed: ' + toText(result && result.previous_mode) + ' → '
                + toText(result && result.mode), 'ok');
            loadOverview();
        } catch (err) {
            if (err.aborted) return;
            let message = toText(err.message, 'mode change failed');
            const gates = err.detailExtra && err.detailExtra.unmet_gates;
            if (Array.isArray(gates) && gates.length) {
                message += '\nUnmet gates:\n' + gates.map(function (g) { return '• ' + g; }).join('\n');
            }
            setNote('mode-action-note', message, 'bad');
        }
    }

    async function pauseMl() {
        openActionPanel('Pause ML — restore rules as the sole decision path', async function (reason) {
            try {
                const result = await api('/api/ml/pause', { method: 'POST', body: { reason: reason } });
                setNote('mode-action-note',
                    toText(result && result.note, 'rules restored'), 'ok');
                loadOverview();
            } catch (err) {
                if (err.aborted) return;
                setNote('mode-action-note', toText(err.message, 'pause failed'), 'bad');
            }
        });
    }

    function renderLabelReadiness(stats) {
        const body = getElement('label-readiness-body');
        if (!body) return;
        if (!stats) { renderCardError('label-readiness-body', { message: 'no data' }); return; }
        const counted = stats.counted_reviewed_manual || {};
        const frag = document.createDocumentFragment();

        const gateOpen = toBoolean(stats.supervised_gate_open);
        const gateLine = el('div', null);
        gateLine.appendChild(el('strong', null, 'Supervised gate: '));
        gateLine.appendChild(chip(gateOpen ? 'OPEN' : 'CLOSED', gateOpen ? 'ok' : 'warn'));
        frag.appendChild(gateLine);

        frag.appendChild(statGrid([
            { label: 'Reviewed manual', value: formatMetric(counted.total) },
            { label: 'Positive', value: formatMetric(counted.positive) },
            { label: 'Negative', value: formatMetric(counted.negative) },
            { label: 'Unknown', value: formatMetric(counted.unknown) }
        ]));

        frag.appendChild(kvList([
            ['Required total', formatMetric(stats.required_total)],
            ['Required per class', formatMetric(stats.required_per_class)]
        ]));

        const notCounted = Array.isArray(stats.not_counted) ? stats.not_counted : [];
        if (notCounted.length) {
            frag.appendChild(el('div', 'mlops-subheading', 'Not counted toward the gate:'));
            const list = el('ul', 'mlops-mode-reasons');
            for (const item of notCounted) {
                list.appendChild(el('li', null,
                    toText(item.label_kind) + ' / ' + toText(item.review_status)
                    + ': ' + formatMetric(item.count)));
            }
            frag.appendChild(list);
        }
        frag.appendChild(el('div', 'mlops-mode-desc',
            'Only manual labels that passed review count toward supervised minimums.'));
        body.replaceChildren(frag);
    }

    function renderDataReadiness(data) {
        const body = getElement('data-readiness-body');
        if (!body) return;
        if (!data) { renderCardError('data-readiness-body', { message: 'no data' }); return; }
        const predictions = toFiniteNumber(data.predictions);
        const fallbacks = toFiniteNumber(data.fallback_predictions);
        let fallbackRate = 'N/A';
        if (predictions !== null && fallbacks !== null && predictions > 0) {
            fallbackRate = (100 * fallbacks / predictions).toFixed(1) + '%';
        }
        const frag = document.createDocumentFragment();
        frag.appendChild(statGrid([
            { label: 'Feature snapshots', value: formatMetric(data.feature_snapshots) },
            { label: 'Predictions', value: formatMetric(data.predictions) },
            { label: 'Fallback predictions', value: formatMetric(data.fallback_predictions) },
            { label: 'Fallback rate', value: fallbackRate }
        ]));
        const note = el('div', 'mlops-note');
        note.id = 'data-readiness-note';
        frag.appendChild(note);
        body.replaceChildren(frag);
    }

    function renderOptionalCapabilities(caps) {
        const body = getElement('optional-capabilities-body');
        if (!body) return;
        if (!caps) { renderCardError('optional-capabilities-body', { message: 'no data' }); return; }
        const wrap = el('div', 'mlops-table-wrap');
        const table = el('table', 'mlops-table');
        const thead = el('thead');
        const headRow = el('tr');
        for (const label of ['Integration', 'Configured', 'Implemented', 'Dependency', 'Operational']) {
            headRow.appendChild(el('th', null, label));
        }
        thead.appendChild(headRow);
        table.appendChild(thead);
        const tbody = el('tbody');
        for (const name of Object.keys(caps)) {
            const status = caps[name] || {};
            const row = el('tr');
            row.appendChild(el('td', null, name));
            for (const field of ['configured', 'implemented', 'dependency_available', 'operational']) {
                const cell = el('td');
                const on = toBoolean(status[field]);
                cell.appendChild(chip(on ? 'yes' : 'no', on ? 'ok' : 'bad'));
                row.appendChild(cell);
            }
            tbody.appendChild(row);
        }
        table.appendChild(tbody);
        wrap.appendChild(table);
        const frag = document.createDocumentFragment();
        frag.appendChild(wrap);
        frag.appendChild(el('div', 'mlops-mode-desc',
            'A flag being on does not make a capability available: all four statuses must hold before it is operational.'));
        body.replaceChildren(frag);
    }

    // ============================================
    // Model registry
    // ============================================

    async function loadModels() {
        const req = beginRequest('models');
        const tbody = getElement('models-table-body');
        if (!tbody) return;
        try {
            const data = await api('/api/ml/models', {
                params: { page: 1, page_size: 25 }, signal: req.signal
            });
            if (!req.isCurrent()) return;
            const items = (data && Array.isArray(data.items)) ? data.items : [];
            const frag = document.createDocumentFragment();
            if (!items.length) {
                const row = el('tr');
                const cell = el('td', null, 'No models registered yet — train a candidate first.');
                cell.colSpan = 9;
                row.appendChild(cell);
                frag.appendChild(row);
            }
            for (const model of items) frag.appendChild(modelRow(model));
            tbody.replaceChildren(frag);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            const row = el('tr');
            const cell = el('td', 'mlops-note note-bad', 'Failed to load models: ' + toText(err.message));
            cell.colSpan = 9;
            row.appendChild(cell);
            tbody.replaceChildren(row);
        }
    }

    function stageTone(stage) {
        if (stage === 'shadow') return 'info';
        if (stage === 'validated') return 'ok';
        if (stage === 'rejected' || stage === 'failed') return 'bad';
        return null;
    }

    function modelRow(model) {
        const row = el('tr');
        row.appendChild(el('td', null, toText(model.model_type)));
        row.appendChild(el('td', null, formatMetric(model.version)));
        const stageCell = el('td');
        stageCell.appendChild(chip(toText(model.stage), stageTone(model.stage)));
        row.appendChild(stageCell);
        row.appendChild(el('td', null, toText(model.algorithm)));
        row.appendChild(el('td', null, toText(model.score_type)));
        row.appendChild(el('td', null, toBoolean(model.is_probability) ? 'yes' : 'no'));
        row.appendChild(el('td', null, toText(model.calibration_status)));
        row.appendChild(el('td', null, formatDateTime(model.created_at)));

        const actions = el('td');
        const detailBtn = el('button', 'mlops-btn', 'Detail');
        detailBtn.type = 'button';
        detailBtn.addEventListener('click', function () { loadModelDetail(model.id); });
        actions.appendChild(detailBtn);

        if (model.stage === 'validated') {
            const approveBtn = el('button', 'mlops-btn mlops-btn-primary', 'Approve → shadow');
            approveBtn.type = 'button';
            approveBtn.addEventListener('click', function () {
                openActionPanel(
                    'Approve ' + toText(model.model_type) + ' v' + formatMetric(model.version)
                    + ' into SHADOW (rules stay live; the approval record persists on the model row)',
                    function (reason) { return approveShadow(model.id, reason); });
            });
            actions.appendChild(approveBtn);
        }
        if (model.stage === 'validated' || model.stage === 'shadow') {
            const rejectBtn = el('button', 'mlops-btn mlops-btn-danger', 'Reject');
            rejectBtn.type = 'button';
            rejectBtn.addEventListener('click', function () {
                openActionPanel(
                    'Reject ' + toText(model.model_type) + ' v' + formatMetric(model.version),
                    function (reason) { return rejectModel(model.id, reason); });
            });
            actions.appendChild(rejectBtn);
        }
        row.appendChild(actions);
        return row;
    }

    async function approveShadow(modelId, reason) {
        try {
            await api('/api/ml/models/' + encodeURIComponent(modelId) + '/shadow-approve', {
                method: 'POST', body: { reason: reason, intended_scope: 'all_pipelines' }
            });
            setNote('mode-action-note', 'Model approved into shadow. Rules remain the decision system.', 'ok');
            loadModels();
            loadOverview();
        } catch (err) {
            if (err.aborted) return;
            setNote('mode-action-note',
                toText(err.code, 'ERROR') + ': ' + toText(err.message, 'shadow approval failed'), 'bad');
        }
    }

    async function rejectModel(modelId, reason) {
        try {
            await api('/api/ml/models/' + encodeURIComponent(modelId) + '/reject', {
                method: 'POST', body: { reason: reason }
            });
            setNote('mode-action-note', 'Model rejected.', 'ok');
            loadModels();
            loadOverview();
        } catch (err) {
            if (err.aborted) return;
            setNote('mode-action-note', toText(err.message, 'rejection failed'), 'bad');
        }
    }

    function stopShadow() {
        openActionPanel('Stop shadow (rollback drill) — archives the shadow model; live decisions were never affected',
            async function (reason) {
                try {
                    const result = await api('/api/ml/shadow/stop', {
                        method: 'POST', body: { reason: reason }
                    });
                    setNote('mode-action-note', toText(result && result.note, 'shadow stopped'), 'ok');
                    loadModels();
                    loadOverview();
                    loadShadowSummary();
                } catch (err) {
                    if (err.aborted) return;
                    setNote('mode-action-note', toText(err.message, 'shadow stop failed'), 'bad');
                }
            });
    }

    async function loadModelDetail(modelId) {
        const req = beginRequest('model-detail');
        const drawer = getElement('model-detail-drawer');
        const body = getElement('model-detail-body');
        const title = getElement('model-detail-title');
        if (!drawer || !body) return;
        drawer.hidden = false;
        body.replaceChildren(el('div', 'mlops-loading', 'Loading model detail…'));
        try {
            const model = await api('/api/ml/models/' + encodeURIComponent(modelId), { signal: req.signal });
            if (!req.isCurrent()) return;
            if (title) {
                title.textContent = toText(model.model_type) + ' v' + formatMetric(model.version)
                    + ' — ' + toText(model.stage);
            }
            const frag = document.createDocumentFragment();
            frag.appendChild(kvList([
                ['Model purpose', toText(model.model_purpose)],
                ['Score type', toText(model.score_type)],
                ['Is probability', toBoolean(model.is_probability) ? 'yes' : 'no'],
                ['Calibration status', toText(model.calibration_status)],
                ['Algorithm', toText(model.algorithm)],
                ['Seed', formatMetric(model.seed)],
                ['Feature set', toText(model.feature_set_version)],
                ['Dataset', toText(model.dataset_id)],
                ['Training job', toText(model.training_job_id)],
                ['Artifact', toText(model.artifact_name)],
                ['Artifact sha256', toText(model.artifact_hash)],
                ['Validated at', formatDateTime(model.validated_at)],
                ['Shadow started', formatDateTime(model.shadow_started_at)],
                ['Created', formatDateTime(model.created_at)]
            ]));

            if (model.metrics) {
                frag.appendChild(el('div', 'mlops-subheading', 'Metrics (only what was truly measured)'));
                frag.appendChild(jsonBlock(model.metrics));
            }
            if (model.quality_gates) {
                frag.appendChild(el('div', 'mlops-subheading', 'Quality gates'));
                frag.appendChild(jsonBlock(model.quality_gates));
            }
            if (model.evaluation_report) {
                frag.appendChild(el('div', 'mlops-subheading', 'Evaluation report'));
                frag.appendChild(jsonBlock(model.evaluation_report));
            }
            if (model.shadow_approval) {
                frag.appendChild(el('div', 'mlops-subheading', 'Shadow approval record'));
                frag.appendChild(jsonBlock(model.shadow_approval));
            }
            // Threshold SETS: one row per (scope, version) with the three band
            // cutpoints; the ACTIVE set is what inference bands with and what
            // each prediction records as threshold_id / threshold_version.
            const thresholds = Array.isArray(model.thresholds) ? model.thresholds : [];
            if (thresholds.length) {
                frag.appendChild(el('div', 'mlops-subheading', 'Threshold sets'));
                for (const t of thresholds) {
                    const cp = (t && typeof t.cutpoints === 'object' && t.cutpoints) ? t.cutpoints : {};
                    const line = el('div', 'mlops-mode-desc',
                        'v' + toText(t.version) + ' [' + toText(t.status) + '] scope '
                        + toText(t.scope_type) + (t.scope_id ? ':' + toText(t.scope_id) : '')
                        + ' — elevated ≥ ' + formatMetric(cp.elevated, 4)
                        + ' · unusual ≥ ' + formatMetric(cp.unusual, 4)
                        + ' · highly_unusual ≥ ' + formatMetric(cp.highly_unusual, 4)
                        + ' (n=' + toText(t.sample_count) + ', source ' + toText(t.source) + ')'
                        + (t.activated_at ? ' — activated ' + formatDateTime(t.activated_at)
                            + (t.activated_by ? ' by ' + toText(t.activated_by) : '') : '')
                        + (t.retired_at ? ' — retired ' + formatDateTime(t.retired_at) : ''));
                    frag.appendChild(line);
                }
            }
            body.replaceChildren(frag);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            body.replaceChildren(el('div', 'mlops-note note-bad',
                'Failed to load detail: ' + toText(err.message)));
        }
    }

    // ============================================
    // Shadow summary — descriptive, different concepts
    // ============================================

    async function loadShadowSummary() {
        const req = beginRequest('shadow-summary');
        const body = getElement('shadow-summary-body');
        const daysSelect = getElement('shadow-days-select');
        if (!body) return;
        const days = daysSelect ? toFiniteNumber(daysSelect.value) : 7;
        try {
            const data = await api('/api/ml/shadow/summary', {
                params: { days: days === null ? 7 : days }, signal: req.signal
            });
            if (!req.isCurrent()) return;
            const frag = document.createDocumentFragment();
            frag.appendChild(el('div', 'mlops-mode-desc', toText(data.note, '')));

            if (toBoolean(data.insufficient_data)) {
                frag.appendChild(el('div', 'mlops-note',
                    'Insufficient data: no shadow comparisons in the selected window.'));
                body.replaceChildren(frag);
                return;
            }

            frag.appendChild(statGrid([
                { label: 'Comparisons', value: formatMetric(data.comparisons) },
                { label: 'Shadow failures', value: formatMetric(data.shadow_failure_count) },
                { label: 'Failure rate', value: formatMetric(data.shadow_failure_rate, 4) },
                { label: 'Latency p50 (ms)', value: formatMetric(data.latency_ms_p50) },
                { label: 'Latency p95 (ms)', value: formatMetric(data.latency_ms_p95) }
            ]));

            const disagreement = data.operational_disagreement || {};
            frag.appendChild(el('div', 'mlops-subheading', 'Operational disagreement (review-signal crossings)'));
            const pairs = [];
            for (const key of ['both_flagged', 'rules_only', 'anomaly_only', 'neither']) {
                pairs.push([key, formatMetric(disagreement[key], 0) === 'N/A' ? '0' : formatMetric(disagreement[key])]);
            }
            frag.appendChild(kvList(pairs));

            const crosstab = data.band_distribution_by_rule_severity || {};
            const severities = Object.keys(crosstab);
            if (severities.length) {
                frag.appendChild(el('div', 'mlops-subheading',
                    'Anomaly-band distribution by rule severity (side-by-side view of different concepts)'));
                const bands = ['normal', 'elevated', 'unusual', 'highly_unusual'];
                const wrap = el('div', 'mlops-table-wrap');
                const table = el('table', 'mlops-table mlops-crosstab');
                const thead = el('thead');
                const headRow = el('tr');
                headRow.appendChild(el('th', null, 'Rule severity \\ band'));
                for (const band of bands) headRow.appendChild(el('th', null, band));
                thead.appendChild(headRow);
                table.appendChild(thead);
                const tbody = el('tbody');
                for (const severity of severities) {
                    const row = el('tr');
                    row.appendChild(el('td', null, severity));
                    const byBand = crosstab[severity] || {};
                    for (const band of bands) {
                        const count = toFiniteNumber(byBand[band]);
                        row.appendChild(el('td', null, count === null ? '0' : String(count)));
                    }
                    tbody.appendChild(row);
                }
                table.appendChild(tbody);
                wrap.appendChild(table);
                frag.appendChild(wrap);
            }
            body.replaceChildren(frag);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderCardError('shadow-summary-body', err);
        }
    }

    // ============================================
    // Predictions
    // ============================================

    async function loadPredictions() {
        const req = beginRequest('predictions');
        const tbody = getElement('predictions-body');
        const info = getElement('predictions-page-info');
        const fallbackOnly = getElement('predictions-fallback-only');
        if (!tbody) return;
        try {
            const data = await api('/api/ml/predictions', {
                params: {
                    page: state.predictionsPage, page_size: PAGE_SIZE,
                    fallback_only: (fallbackOnly && fallbackOnly.checked) ? 'true' : ''
                },
                signal: req.signal
            });
            if (!req.isCurrent()) return;
            const items = (data && Array.isArray(data.items)) ? data.items : [];
            const frag = document.createDocumentFragment();
            if (!items.length) {
                const row = el('tr');
                const cell = el('td', null, 'No predictions recorded.');
                cell.colSpan = 10;
                row.appendChild(cell);
                frag.appendChild(row);
            }
            for (const p of items) {
                const row = el('tr');
                row.appendChild(el('td', null, toText(p.subject_id)));
                const bandCell = el('td');
                bandCell.appendChild(chip(toText(p.ml_anomaly_band, 'n/a'),
                    p.ml_anomaly_band === 'highly_unusual' ? 'warn' : null));
                row.appendChild(bandCell);
                row.appendChild(el('td', null, formatMetric(p.behavioral_anomaly_score, 4)));
                row.appendChild(el('td', null, toText(p.requested_mode)));
                row.appendChild(el('td', null, toText(p.actual_mode_used)));
                row.appendChild(el('td', null, toText(p.fallback_reason, '—')));
                const missing = Array.isArray(p.missing_features) ? p.missing_features.length : 0;
                row.appendChild(el('td', null, String(missing)));
                row.appendChild(el('td', null, formatMetric(p.latency_ms)));
                // lineage: the exact threshold set + outcome persisted with the row
                row.appendChild(el('td', null, toText(p.threshold_version, '—')
                    + (p.outcome_label ? ' · outcome ' + toText(p.outcome_label) : '')));
                row.appendChild(el('td', null, formatDateTime(p.created_at)));
                frag.appendChild(row);
            }
            tbody.replaceChildren(frag);
            if (info) {
                const total = toFiniteNumber(data.total);
                info.textContent = 'Page ' + state.predictionsPage
                    + (total === null ? '' : ' — ' + total + ' total');
            }
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            const row = el('tr');
            const cell = el('td', 'mlops-note note-bad', 'Failed to load predictions: ' + toText(err.message));
            cell.colSpan = 9;
            row.appendChild(cell);
            tbody.replaceChildren(row);
        }
    }

    // ============================================
    // Drift
    // ============================================

    async function loadDriftReports() {
        const req = beginRequest('drift');
        const body = getElement('drift-reports-body');
        if (!body) return;
        try {
            const data = await api('/api/ml/drift/reports', {
                params: { page: 1, page_size: 10 }, signal: req.signal
            });
            if (!req.isCurrent()) return;
            const items = (data && Array.isArray(data.items)) ? data.items : [];
            const frag = document.createDocumentFragment();
            frag.appendChild(el('div', 'mlops-mode-desc', toText(data.note, '')));
            if (!items.length) {
                frag.appendChild(el('div', 'mlops-note', 'No drift reports yet.'));
            }
            for (const report of items) {
                const box = el('div', 'mlops-mode-card');
                const head = el('div', 'mlops-mode-name', toText(report.report_kind) + ' drift');
                const severity = toText(report.severity, 'unknown');
                head.appendChild(chip(severity,
                    severity === 'critical' ? 'bad' : (severity === 'warning' ? 'warn' : 'ok')));
                box.appendChild(head);
                box.appendChild(el('div', 'mlops-mode-desc',
                    'Window ' + formatDateTime(report.window_start) + ' → '
                    + formatDateTime(report.window_end)
                    + ' — samples: ' + formatMetric(report.sample_count)
                    + ' (baseline: ' + formatMetric(report.baseline_sample_count) + ')'));
                if (toBoolean(report.insufficient_data)) {
                    box.appendChild(el('div', 'mlops-note',
                        'Insufficient data — metrics in this report are not evidence of drift.'));
                }
                if (report.metrics) {
                    const details = el('details');
                    details.appendChild(el('summary', 'mlops-mode-desc', 'metrics'));
                    details.appendChild(jsonBlock(report.metrics));
                    box.appendChild(details);
                }
                frag.appendChild(box);
            }
            body.replaceChildren(frag);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderCardError('drift-reports-body', err);
        }
    }

    async function runDrift() {
        const btn = getElement('run-drift-btn');
        if (btn) btn.disabled = true;
        try {
            await api('/api/ml/drift/run', { method: 'POST', timeout: 60000 });
            loadDriftReports();
        } catch (err) {
            if (!err.aborted) {
                setNote('mode-action-note', 'Drift run failed: ' + toText(err.message), 'bad');
            }
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    // ============================================
    // Training + datasets (manual only) with bounded job polling
    // ============================================

    function renderJobStatus(task, headline) {
        const body = getElement('training-status-body');
        if (!body) return;
        const frag = document.createDocumentFragment();
        frag.appendChild(el('div', 'mlops-subheading', headline));
        if (task) {
            const pairs = [
                ['Job', toText(task.job_id)],
                ['Status', toText(task.status)],
                ['Progress', formatMetric(task.progress) === 'N/A' ? '—' : formatMetric(task.progress) + '%']
            ];
            const message = task.progress_message || task.error_message || task.description;
            if (message) pairs.push(['Message', toText(message)]);
            frag.appendChild(kvList(pairs));
            if (task.result && typeof task.result === 'object') {
                frag.appendChild(jsonBlock(task.result));
            }
        }
        body.replaceChildren(frag);
    }

    async function pollJob(jobId, attempt, onFinished) {
        state.activeJobId = jobId;
        if (attempt > JOB_POLL_MAX) {
            renderJobStatus(null, 'Job is taking too long — check the Background Tasks page.');
            finishJobUi();
            return;
        }
        try {
            const task = await api('/api/ml/training-jobs/' + encodeURIComponent(jobId));
            const status = toText(task && task.status, 'unknown');
            if (status === 'completed' || status === 'failed' || status === 'cancelled') {
                renderJobStatus(task, status === 'completed' ? 'Job finished' : 'Job ' + status);
                finishJobUi();
                if (onFinished) onFinished(task);
                return;
            }
            renderJobStatus(task, 'Job running…');
        } catch (err) {
            if (err.aborted) return;
        }
        state.jobPollTimer = window.setTimeout(function () {
            pollJob(jobId, attempt + 1, onFinished);
        }, JOB_POLL_INTERVAL_MS);
    }

    function finishJobUi() {
        state.activeJobId = null;
        state.activeJobKind = null;
        stopJobPolling();
        const startBtn = getElement('start-training-btn');
        const cancelBtn = getElement('cancel-training-btn');
        if (startBtn) startBtn.disabled = false;
        if (cancelBtn) cancelBtn.hidden = true;
    }

    async function startTraining() {
        if (state.activeJobId) return;
        const typeSelect = getElement('training-model-type');
        const algoSelect = getElement('training-algorithm');
        const startBtn = getElement('start-training-btn');
        const cancelBtn = getElement('cancel-training-btn');
        if (startBtn) startBtn.disabled = true;
        try {
            const result = await api('/api/ml/training-jobs', {
                method: 'POST',
                body: {
                    model_type: typeSelect ? typeSelect.value : 'behavior_anomaly_model',
                    algorithm: algoSelect ? algoSelect.value : 'isolation_forest'
                }
            });
            state.activeJobKind = 'training';
            if (cancelBtn) cancelBtn.hidden = false;
            renderJobStatus(result, 'Training scheduled');
            pollJob(toText(result && result.job_id, ''), 1, function () {
                loadModels();
                loadOverview();
            });
        } catch (err) {
            if (startBtn) startBtn.disabled = false;
            if (err.aborted) return;
            renderJobStatus(null,
                toText(err.code, 'ERROR') + ': ' + toText(err.message, 'training failed to schedule'));
        }
    }

    async function cancelTraining() {
        if (!state.activeJobId || state.activeJobKind !== 'training') return;
        try {
            await api('/api/ml/training-jobs/' + encodeURIComponent(state.activeJobId) + '/cancel', {
                method: 'POST', body: {}
            });
        } catch (err) {
            if (!err.aborted) {
                renderJobStatus(null, 'Cancel failed: ' + toText(err.message));
            }
        }
    }

    async function computeFeatures() {
        const btn = getElement('compute-features-btn');
        if (btn) btn.disabled = true;
        try {
            const result = await api('/api/ml/features/compute', { method: 'POST', body: {} });
            setNote('data-readiness-note',
                'Feature collection scheduled (job ' + toText(result && result.job_id) + ').', 'ok');
            const jobId = toText(result && result.job_id, '');
            if (jobId && jobId !== '—') {
                state.activeJobKind = 'collection';
                pollJob(jobId, 1, function () { loadOverview(); });
            }
        } catch (err) {
            if (!err.aborted) {
                setNote('data-readiness-note',
                    toText(err.code, 'ERROR') + ': ' + toText(err.message, 'collection failed'), 'bad');
            }
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    async function buildDataset() {
        const nameInput = getElement('dataset-name-input');
        const kindSelect = getElement('dataset-kind-select');
        const btn = getElement('build-dataset-btn');
        const name = nameInput ? nameInput.value.trim() : '';
        if (!name) {
            renderDatasetsMessage('A dataset name is required.', 'bad');
            return;
        }
        if (btn) btn.disabled = true;
        try {
            const outcome = await api('/api/ml/datasets', {
                method: 'POST', timeout: 120000,
                body: { name: name, kind: kindSelect ? kindSelect.value : 'unsupervised' }
            });
            renderDatasetsMessage('Dataset built: ' + toText(outcome && outcome.name)
                + ' v' + formatMetric(outcome && outcome.version)
                + ' (' + formatMetric(outcome && outcome.row_count) + ' rows)', 'ok');
            loadDatasets();
        } catch (err) {
            if (!err.aborted) {
                let message = toText(err.message, 'dataset build failed');
                if (err.status === 422 && err.detailExtra) message += ' — validation did not pass';
                renderDatasetsMessage(message, 'bad');
            }
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    function renderDatasetsMessage(message, tone) {
        const body = getElement('datasets-body');
        if (!body) return;
        const note = el('div', 'mlops-note' + (tone ? ' note-' + tone : ''), message);
        body.replaceChildren(note);
    }

    async function loadDatasets() {
        const req = beginRequest('datasets');
        const body = getElement('datasets-body');
        if (!body) return;
        try {
            const data = await api('/api/ml/datasets', {
                params: { page: 1, page_size: 5 }, signal: req.signal
            });
            if (!req.isCurrent()) return;
            const items = (data && Array.isArray(data.items)) ? data.items : [];
            const frag = document.createDocumentFragment();
            frag.appendChild(el('div', 'mlops-subheading',
                'Recent datasets (' + formatMetric(data.total) + ' total)'));
            if (!items.length) frag.appendChild(el('div', 'mlops-mode-desc', 'None built yet.'));
            for (const ds of items) {
                frag.appendChild(el('div', 'mlops-mode-desc',
                    toText(ds.name) + ' v' + formatMetric(ds.version) + ' — ' + toText(ds.kind)
                    + ', rows: ' + formatMetric(ds.row_count)
                    + ', status: ' + toText(ds.status)
                    + ', built ' + formatDateTime(ds.created_at)));
            }
            body.replaceChildren(frag);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderDatasetsMessage('Failed to load datasets: ' + toText(err.message), 'bad');
        }
    }

    // ============================================
    // Labels queue
    // ============================================

    async function loadLabels() {
        const req = beginRequest('labels');
        const body = getElement('labels-body');
        const info = getElement('labels-page-info');
        const filter = getElement('labels-filter-review');
        if (!body) return;
        try {
            const data = await api('/api/ml/labels', {
                params: {
                    page: state.labelsPage, page_size: PAGE_SIZE,
                    review_status: filter ? filter.value : ''
                },
                signal: req.signal
            });
            if (!req.isCurrent()) return;
            const items = (data && Array.isArray(data.items)) ? data.items : [];
            const frag = document.createDocumentFragment();
            if (!items.length) frag.appendChild(el('div', 'mlops-mode-desc', 'No labels found.'));
            for (const label of items) {
                const box = el('div', 'mlops-mode-card');
                const head = el('div', 'mlops-mode-name', toText(label.label));
                head.appendChild(chip(toText(label.label_kind),
                    label.label_kind === 'manual' ? 'info' : null));
                head.appendChild(chip(toText(label.review_status),
                    label.review_status === 'reviewed' ? 'ok'
                        : (label.review_status === 'disputed' ? 'bad' : 'warn')));
                box.appendChild(head);
                box.appendChild(el('div', 'mlops-mode-desc',
                    'Subject ' + toText(label.subject_id)
                    + ' — source: ' + toText(label.source)
                    + ', confidence: ' + formatMetric(label.confidence, 2)
                    + ', event: ' + formatDateTime(label.event_time)));
                if (label.status === 'active') {
                    const actionRow = el('div');
                    for (const action of ['confirm', 'dispute', 'retract']) {
                        const btn = el('button', 'mlops-btn', action);
                        btn.type = 'button';
                        btn.addEventListener('click', function () { reviewLabel(label.id, action); });
                        actionRow.appendChild(btn);
                    }
                    // Correction = supersession (history kept): pick the corrected value.
                    const correct = el('select', 'mlops-select');
                    for (const v of ['', 'positive', 'negative', 'unknown']) {
                        const opt = el('option', null, v ? 'correct → ' + v : 'correct…');
                        opt.value = v;
                        correct.appendChild(opt);
                    }
                    correct.addEventListener('change', function () {
                        if (correct.value) supersedeLabel(label.id, correct.value);
                    });
                    actionRow.appendChild(correct);
                    box.appendChild(actionRow);
                } else {
                    box.appendChild(el('div', 'mlops-mode-desc', 'status: ' + toText(label.status)
                        + (label.supersedes_id ? ' (supersedes ' + toText(label.supersedes_id) + ')' : '')));
                }
                frag.appendChild(box);
            }
            body.replaceChildren(frag);
            if (info) {
                const total = toFiniteNumber(data.total);
                info.textContent = 'Page ' + state.labelsPage
                    + (total === null ? '' : ' — ' + total + ' total');
            }
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            body.replaceChildren(el('div', 'mlops-note note-bad',
                'Failed to load labels: ' + toText(err.message)));
        }
    }

    async function reviewLabel(labelId, action) {
        try {
            await api('/api/ml/labels/' + encodeURIComponent(labelId) + '/review', {
                method: 'POST', body: { action: action }
            });
            setNote('label-form-note', 'Label ' + action + ' recorded.', 'ok');
            loadLabels();
            loadOverview();
        } catch (err) {
            if (err.aborted) return;
            setNote('label-form-note', toText(err.message, 'review failed'), 'bad');
        }
    }

    async function supersedeLabel(labelId, newLabel) {
        try {
            await api('/api/ml/labels/' + encodeURIComponent(labelId) + '/supersede', {
                method: 'POST', body: { label: newLabel }
            });
            setNote('label-form-note', 'Label corrected (superseded) → ' + toText(newLabel) + '.', 'ok');
            loadLabels();
            loadOverview();
        } catch (err) {
            if (err.aborted) return;
            setNote('label-form-note', toText(err.message, 'supersede failed'), 'bad');
        }
    }

    async function createLabel() {
        const subjectInput = getElement('label-subject-id');
        const valueSelect = getElement('label-value');
        const kindSelect = getElement('label-kind');
        const sourceInput = getElement('label-source');
        const eventInput = getElement('label-event-time');
        const subjectId = subjectInput ? subjectInput.value.trim() : '';
        const eventRaw = eventInput ? eventInput.value : '';
        if (subjectId.length < 8) {
            setNote('label-form-note', 'An identity id (at least 8 characters) is required.', 'bad');
            return;
        }
        if (!eventRaw) {
            setNote('label-form-note', 'An event time is required — labels are anchored in time.', 'bad');
            return;
        }
        // `datetime-local` yields the analyst's LOCAL wall clock with no zone.
        // Appending 'Z' relabelled that reading as UTC, shifting the label's
        // anchor by the browser's offset — the one place in this codebase that
        // really was blindly stamping Z onto a local time. Parse it as local
        // (which is what it is) and convert to the true UTC instant.
        const eventTime = new Date(eventRaw).toISOString();
        const source = sourceInput && sourceInput.value.trim() ? sourceInput.value.trim() : 'analyst_review';
        try {
            const result = await api('/api/ml/labels', {
                method: 'POST',
                body: {
                    subject_id: subjectId,
                    label: valueSelect ? valueSelect.value : 'negative',
                    label_kind: kindSelect ? kindSelect.value : 'manual',
                    source: source,
                    event_time: eventTime
                }
            });
            setNote('label-form-note',
                toBoolean(result && result.deduplicated)
                    ? 'Identical label already existed — no duplicate created.'
                    : 'Label created. Manual labels must pass review before they count.', 'ok');
            loadLabels();
            loadOverview();
        } catch (err) {
            if (err.aborted) return;
            setNote('label-form-note',
                toText(err.code, 'ERROR') + ': ' + toText(err.message, 'label creation failed'), 'bad');
        }
    }

    // ============================================
    // Retraining policy (display-only; scheduled retraining is gated)
    // ============================================

    async function loadPolicy() {
        const req = beginRequest('policy');
        const body = getElement('policy-body');
        const typeSelect = getElement('policy-model-type');
        if (!body) return;
        const modelType = typeSelect ? typeSelect.value : 'behavior_anomaly_model';
        try {
            const data = await api('/api/ml/retraining-policy/' + encodeURIComponent(modelType),
                { signal: req.signal });
            if (!req.isCurrent()) return;
            const frag = document.createDocumentFragment();
            const enabledLine = el('div', null);
            enabledLine.appendChild(el('strong', null, 'Scheduled retraining: '));
            enabledLine.appendChild(chip(toBoolean(data.enabled) ? 'ENABLED' : 'DISABLED',
                toBoolean(data.enabled) ? 'warn' : 'ok'));
            frag.appendChild(enabledLine);
            frag.appendChild(kvList([
                ['Interval (hours)', formatMetric(data.schedule_interval_hours)],
                ['Min new labels', formatMetric(data.min_new_labels)],
                ['Min total labels', formatMetric(data.min_total_labels)],
                ['Cooldown (hours)', formatMetric(data.cooldown_hours)],
                ['Min drift reports', formatMetric(data.min_drift_reports)],
                ['Last triggered', formatDateTime(data.last_triggered_at)]
            ]));
            frag.appendChild(el('div', 'mlops-mode-desc', toText(data.note, '')));
            body.replaceChildren(frag);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderCardError('policy-body', err);
        }
    }

    // ============================================
    // Audit log
    // ============================================

    async function loadAudit() {
        const req = beginRequest('audit');
        const body = getElement('audit-body');
        const info = getElement('audit-page-info');
        if (!body) return;
        try {
            const data = await api('/api/ml/audit', {
                params: { page: state.auditPage, page_size: PAGE_SIZE }, signal: req.signal
            });
            if (!req.isCurrent()) return;
            const items = (data && Array.isArray(data.items)) ? data.items : [];
            const frag = document.createDocumentFragment();
            if (!items.length) frag.appendChild(el('div', 'mlops-mode-desc', 'No audit entries.'));
            for (const entry of items) {
                frag.appendChild(el('div', 'mlops-mode-desc',
                    formatDateTime(entry.created_at) + ' — ' + toText(entry.actor_username)
                    + ': ' + toText(entry.action) + ' on ' + toText(entry.object_type)
                    + ' ' + toText(entry.object_id, '')
                    + (entry.reason ? ' (reason: ' + toText(entry.reason) + ')' : '')));
            }
            body.replaceChildren(frag);
            if (info) {
                const total = toFiniteNumber(data.total);
                info.textContent = 'Page ' + state.auditPage
                    + (total === null ? '' : ' — ' + total + ' total');
            }
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            body.replaceChildren(el('div', 'mlops-note note-bad',
                'Failed to load audit log: ' + toText(err.message)));
        }
    }

    // ============================================
    // Wiring
    // ============================================

    function on(id, event, handler) {
        const node = getElement(id);
        if (node) node.addEventListener(event, handler);
    }

    function init() {
        debugLog('ml-ops init');
        on('refresh-overview-btn', 'click', function () { loadOverview(); loadModels(); });
        on('pause-ml-btn', 'click', pauseMl);
        on('registry-action-confirm', 'click', confirmPendingAction);
        on('registry-action-cancel', 'click', closeActionPanel);
        on('models-refresh-btn', 'click', loadModels);
        on('stop-shadow-btn', 'click', stopShadow);
        on('model-detail-close', 'click', function () {
            const drawer = getElement('model-detail-drawer');
            if (drawer) drawer.hidden = true;
        });
        on('shadow-days-select', 'change', loadShadowSummary);
        on('predictions-fallback-only', 'change', function () {
            state.predictionsPage = 1;
            loadPredictions();
        });
        on('predictions-prev', 'click', function () {
            if (state.predictionsPage > 1) { state.predictionsPage -= 1; loadPredictions(); }
        });
        on('predictions-next', 'click', function () {
            state.predictionsPage += 1; loadPredictions();
        });
        on('run-drift-btn', 'click', runDrift);
        on('start-training-btn', 'click', startTraining);
        on('cancel-training-btn', 'click', cancelTraining);
        on('compute-features-btn', 'click', computeFeatures);
        on('build-dataset-btn', 'click', buildDataset);
        on('labels-filter-review', 'change', function () {
            state.labelsPage = 1;
            loadLabels();
        });
        on('labels-prev', 'click', function () {
            if (state.labelsPage > 1) { state.labelsPage -= 1; loadLabels(); }
        });
        on('labels-next', 'click', function () {
            state.labelsPage += 1; loadLabels();
        });
        on('create-label-btn', 'click', createLabel);
        on('policy-model-type', 'change', loadPolicy);
        on('audit-prev', 'click', function () {
            if (state.auditPage > 1) { state.auditPage -= 1; loadAudit(); }
        });
        on('audit-next', 'click', function () {
            state.auditPage += 1; loadAudit();
        });

        loadOverview();
        loadModels();
        loadShadowSummary();
        loadPredictions();
        loadDriftReports();
        loadDatasets();
        loadLabels();
        loadPolicy();
        loadAudit();
    }

    function destroy() {
        stopJobPolling();
        abortAllRequests();
    }

    window.addEventListener('pagehide', destroy);

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
