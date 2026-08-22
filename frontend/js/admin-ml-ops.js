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

    function describeSplitStrategy(split) {

        const method = toText(split && split.method, 'not recorded');

        if (method !== 'temporal') {

            return method + ' (entity isolation: an entity belongs wholly to its earliest period; later rows dropped)';

        }

        const overlap = (split && split.entity_overlap) || {};

        const test = overlap.test || {};

        const frac = test.row_fraction_of_train_entities;

        const pct = (frac === null || frac === undefined) ? 'n/a' : Math.round(Number(frac) * 100) + '%';

        return 'temporal (entities recur across splits; ' + pct + ' of test rows belong to train entities — '

            + 'scores describe later behaviour of known entities, not unseen-entity generalisation)';

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
        releaseNotes: [],
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
            renderSystemState(data && data.system);
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
            const approveBtn = el('button', 'mlops-btn mlops-btn-primary', 'Approve for SHADOW (observation only)');
            approveBtn.title = 'Shadow = the model runs in parallel and is recorded; it gets NO decision authority. Rules stay authoritative.';
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
            const trainingConfig = (model.training_config && typeof model.training_config === 'object')
                ? model.training_config : null;
            frag.appendChild(kvList([
                ['Model purpose', toText(model.model_purpose)],
                ['Score type', toText(model.score_type)],
                ['Is probability', toBoolean(model.is_probability) ? 'yes' : 'no'],
                ['Calibration status', toText(model.calibration_status)],
                ['Algorithm', toText(model.algorithm)],
                ['Seed', formatMetric(model.seed)],
                ['Feature set', toText(model.feature_set_version)],
                ['Dataset', toText(model.dataset_id)],
                ['Dataset checksum / Parquet sha256', trainingConfig
                    ? toText(trainingConfig.dataset_checksum).slice(0, 16) + ' / '
                        + toText(trainingConfig.dataset_parquet_sha256, 'N/A').slice(0, 16)
                        + (toBoolean(trainingConfig.dataset_reused) ? ' (reused dataset)' : ' (built for this run)')
                    : 'not recorded (trained before lineage v2)'],
                ['Training job', toText(model.training_job_id)],
                ['Code version', toText(model.code_version, 'not recorded')],
                ['Artifact', toText(model.artifact_name)],
                ['Artifact sha256', toText(model.artifact_hash)],
                ['Artifact file present', model.artifact_present === null || model.artifact_present === undefined
                    ? 'N/A' : (toBoolean(model.artifact_present) ? 'yes' : 'MISSING — cannot be served')],
                ['Validated at', formatDateTime(model.validated_at)],
                ['Shadow started', formatDateTime(model.shadow_started_at)],
                ['Created', formatDateTime(model.created_at)]
            ]));

            if (trainingConfig) {
                frag.appendChild(el('div', 'mlops-subheading', 'Training configuration (as run)'));
                frag.appendChild(jsonBlock(trainingConfig));
            }
            if (model.dependency_versions) {
                frag.appendChild(el('div', 'mlops-subheading', 'Dependency versions (verified at load)'));
                frag.appendChild(jsonBlock(model.dependency_versions));
            }
            const report = (model.evaluation_report && typeof model.evaluation_report === 'object') ? model.evaluation_report : null;
            if (report && (report.engineering_gate || report.scientific_gate)) {
                const eg = report.engineering_gate && typeof report.engineering_gate === 'object' ? report.engineering_gate : {};
                const sg = report.scientific_gate && typeof report.scientific_gate === 'object' ? report.scientific_gate : {};
                frag.appendChild(el('div', 'mlops-subheading', 'Readiness gates (two different questions)'));
                frag.appendChild(kvList([
                    ['Engineering gate', toText(eg.status, 'NOT_RECORDED') + (Array.isArray(eg.failed) && eg.failed.length ? ' — failed: ' + eg.failed.join(', ') : '') + ' — ' + toText(eg.meaning)],
                    ['Scientific gate', toText(sg.status, 'NOT_RECORDED') + ' — ' + (Array.isArray(sg.reasons) ? sg.reasons.map(function (r) { return toText(r.code); }).join(', ') : '') + ' — ' + toText(sg.meaning)]
                ]));
                const m = sg.metrics && typeof sg.metrics === 'object' ? sg.metrics : {};
                const app = m.appearances_per_entity && typeof m.appearances_per_entity === 'object' ? m.appearances_per_entity : {};
                frag.appendChild(kvList([
                    ['History span (days)', formatMetric(m.history_span_days, 1)],
                    ['Entities', formatMetric(m.unique_entities)],
                    ['Appearances / entity (p10 · median · p90)', formatMetric(app.p10, 1) + ' · ' + formatMetric(app.median, 1) + ' · ' + formatMetric(app.p90, 1)],
                    ['Entities with ≥5 / ≥10 appearances', formatMetric(m.pct_entities_ge_5_appearances, 3) + ' / ' + formatMetric(m.pct_entities_ge_10_appearances, 3)],
                    ['Train→test score shift (p90)', (m.train_test_score_shift ? formatMetric(m.train_test_score_shift.train_p90, 3) + ' → ' + formatMetric(m.train_test_score_shift.test_p90, 3) : 'N/A')]
                ]));
            }
            if (report && report.temporal_shift && typeof report.temporal_shift === 'object') {
                frag.appendChild(el('div', 'mlops-subheading', 'Temporal shift (score drift vs feature-availability shift)'));
                frag.appendChild(jsonBlock(report.temporal_shift));
            }
            if (report && report.incumbent_comparison && typeof report.incumbent_comparison === 'object') {
                const cmp = report.incumbent_comparison;
                frag.appendChild(el('div', 'mlops-subheading', 'Candidate vs incumbent (descriptive)'));
                frag.appendChild(el('div', 'mlops-mode-desc', 'Status: ' + toText(cmp.status)
                    + ' · promotion decision: ' + toText(cmp.promotion_decision)
                    + (cmp.reason ? ' · ' + toText(cmp.reason) : '')));
            }
            if (report && Array.isArray(report.feature_set_limitations) && report.feature_set_limitations.length) {
                frag.appendChild(el('div', 'mlops-mode-desc', 'Feature-set limitations apply to this model: '
                    + report.feature_set_limitations.map(function (x) { return toText(x.feature); }).join(', ')));
            }
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

    // The trainer reports these stages (trainer.stage(...)); the feature job
    // reports its own. The strip marks done / current / pending from the job
    // record's details.stage and progress_percent — nothing is guessed.
    const TRAINING_STAGES = [
        ['loading_dataset', 'dataset'], ['building_dataset', 'dataset'], ['training', 'training'],
        ['evaluating', 'evaluating'], ['saving_candidate', 'saving'], ['registering', 'registering']
    ];
    const STAGE_LABELS = { dataset: 'Dataset', training: 'Training', evaluating: 'Evaluating',
                           saving: 'Saving candidate', registering: 'Registering', done: 'Done' };

    function stageStrip(task) {
        const details = task && typeof task.details === 'object' && task.details ? task.details : {};
        const stageName = toText(details.stage, '');
        const groups = ['dataset', 'training', 'evaluating', 'saving', 'registering', 'done'];
        let currentGroup = null;
        for (const pair of TRAINING_STAGES) {
            if (pair[0] === stageName) currentGroup = pair[1];
        }
        const status = toText(task && task.status, '');
        if (status === 'completed') currentGroup = 'done';
        const strip = el('div', 'mlops-stage-strip');
        strip.setAttribute('aria-label', 'Job stages');
        let reached = currentGroup === null ? -1 : groups.indexOf(currentGroup);
        groups.forEach(function (g, index) {
            let cls = 'mlops-stage';
            if (status === 'failed' || status === 'cancelled') {
                if (index < reached) cls += ' stage-done';
                else if (index === reached) cls += ' stage-failed';
            } else if (index < reached || (status === 'completed')) cls += ' stage-done';
            else if (index === reached) cls += ' stage-current';
            const chip = el('span', cls, STAGE_LABELS[g]);
            chip.title = g === 'dataset' ? 'Build or verify the immutable dataset'
                : g === 'training' ? 'Fit the algorithm on the train split'
                : g === 'evaluating' ? 'Distributional metrics, seed stability, incumbent comparison'
                : g === 'saving' ? 'Write the artifact atomically, reload-verify, checksum'
                : g === 'registering' ? 'Register the candidate and its threshold set'
                : 'Finished';
            strip.appendChild(chip);
        });
        return strip;
    }

    function renderJobStatus(task, headline) {
        const body = getElement('training-status-body');
        if (!body) return;
        const frag = document.createDocumentFragment();
        frag.appendChild(el('div', 'mlops-subheading', headline));
        if (task) {
            const percent = toFiniteNumber(task.progress_percent);
            const details = task && typeof task.details === 'object' && task.details ? task.details : {};
            frag.appendChild(stageStrip(task));
            const bar = el('div', 'mlops-progress');
            const fill = el('span');
            fill.style.width = (percent === null ? 0 : Math.max(0, Math.min(100, percent))) + '%';
            bar.appendChild(fill);
            bar.title = percent === null ? 'No progress reported yet' : percent + '% (' + toText(details.stage, 'queued') + ')';
            frag.appendChild(bar);
            const pairs = [
                ['Job', toText(task.job_id)],
                ['Status', toText(task.status)],
                ['Stage', toText(details.stage, task.status === 'completed' ? 'done' : 'queued')],
                ['Progress', percent === null ? '—' : percent + '%']
            ];
            if (task.error_code) pairs.push(['Error code', toText(task.error_code)]);
            const message = task.progress_message || task.error_message || task.description;
            if (message) pairs.push(['Message', toText(message)]);
            if (task.request_id) pairs.push(['Request id', toText(task.request_id)]);
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
        const datasetSelect = getElement('training-dataset-select');
        const seedInput = getElement('training-seed-input');
        const hpInput = getElement('training-hyperparameters-input');
        const startBtn = getElement('start-training-btn');
        const cancelBtn = getElement('cancel-training-btn');
        // Experiment knobs: an existing dataset (one immutable dataset, many
        // experiments), an explicit seed and explicit hyperparameters. Every
        // value sent here is persisted verbatim as the model's training_config.
        const body = {
            model_type: typeSelect ? typeSelect.value : 'behavior_anomaly_model',
            algorithm: algoSelect ? algoSelect.value : 'isolation_forest'
        };
        if (datasetSelect && datasetSelect.value) body.dataset_id = datasetSelect.value;
        const seed = seedInput ? toFiniteNumber(seedInput.value) : null;
        if (seed !== null && seed >= 0) body.seed = Math.floor(seed);
        const hpRaw = hpInput ? hpInput.value.trim() : '';
        if (hpRaw) {
            let parsed = null;
            try { parsed = JSON.parse(hpRaw); } catch (_) { parsed = null; }
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                renderJobStatus(null, 'Hyperparameters must be a JSON object, e.g. {"n_estimators": 200}');
                return;
            }
            body.hyperparameters = parsed;
        }
        if (startBtn) startBtn.disabled = true;
        try {
            const result = await api('/api/ml/training-jobs', { method: 'POST', body: body });
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
        const definitionSelect = getElement('dataset-definition-select');
        const policySelect = getElement('dataset-sampling-policy');
        const startRaw = getElement('dataset-range-start') ? getElement('dataset-range-start').value : '';
        const endRaw = getElement('dataset-range-end') ? getElement('dataset-range-end').value : '';
        const body = { name: name, kind: kindSelect ? kindSelect.value : 'unsupervised' };
        if (definitionSelect && definitionSelect.value) body.definition = definitionSelect.value;
        if (policySelect && policySelect.value) body.sampling_policy = policySelect.value;
        const splitSelect = getElement('dataset-split-strategy');
        if (splitSelect && splitSelect.value) body.split_strategy = splitSelect.value;
        // datetime-local is the analyst's LOCAL wall clock; convert to the true UTC instant.
        if (startRaw) body.time_range_start = new Date(startRaw).toISOString();
        if (endRaw) body.time_range_end = new Date(endRaw).toISOString();
        if (btn) btn.disabled = true;
        try {
            const outcome = await api('/api/ml/datasets', {
                method: 'POST', timeout: 120000, body: body
            });
            const ex = (outcome && typeof outcome.extraction === 'object' && outcome.extraction) ? outcome.extraction : {};
            renderDatasetsMessage('Dataset built: ' + toText(outcome && outcome.name)
                + ' v' + formatMetric(outcome && outcome.version)
                + ' (' + formatMetric(outcome && outcome.row_count) + ' rows; '
                + formatMetric(ex.candidate_rows) + ' candidates, '
                + formatMetric(ex.excluded_rows) + ' excluded by ' + toText(ex.sampling_policy) + ')', 'ok');
            loadDatasets();
        } catch (err) {
            if (!err.aborted) {
                let message = toText(err.message, 'dataset build failed');
                const extra = err.detailExtra && typeof err.detailExtra === 'object' ? err.detailExtra : null;
                if (extra && extra.refusal === 'EXTRACTION_EXCEEDS_CAP') {
                    const ex = extra.extraction && typeof extra.extraction === 'object' ? extra.extraction : {};
                    message = 'Refused: ' + formatMetric(ex.candidate_rows) + ' candidate rows exceed the cap of '
                        + formatMetric(ex.cap) + ' and the policy is refuse — narrow the range or choose a sampling policy';
                } else if (err.status === 422 && extra) {
                    message += ' — validation did not pass';
                }
                renderDatasetsMessage(message, 'bad');
            }
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    /** Typed extraction definitions + the known limitations of the feature
     *  set they use, straight from the backend (never hard-coded here). */
    async function loadDatasetDefinitions() {
        const req = beginRequest('dataset-definitions');
        const select = getElement('dataset-definition-select');
        const note = getElement('dataset-definition-note');
        if (!select) return;
        try {
            const data = await api('/api/ml/datasets/definitions', { signal: req.signal });
            if (!req.isCurrent()) return;
            const items = (data && Array.isArray(data.items)) ? data.items : [];
            const keep = select.value;
            const options = [el('option', null, 'default for kind')];
            options[0].value = '';
            for (const d of items) {
                const opt = el('option', null, toText(d.name) + ' ' + toText(d.version) + ' — ' + toText(d.kind)
                    + ' (cap ' + formatMetric(d.row_cap) + ', default ' + toText(d.sampling_policy) + ')');
                opt.value = toText(d.name);
                options.push(opt);
            }
            select.replaceChildren.apply(select, options);
            if (keep) select.value = keep;
            if (note) {
                const limits = items.length && Array.isArray(items[0].feature_set_limitations)
                    ? items[0].feature_set_limitations : [];
                const frag = document.createDocumentFragment();
                frag.appendChild(el('span', null, 'Feature set ' + toText(items.length ? items[0].feature_set_version : '')
                    + ' — reproducible is not the same as scientifically clean. Known limitations: '));
                if (!limits.length) frag.appendChild(el('span', null, 'none reported.'));
                for (const item of limits) {
                    frag.appendChild(chip(toText(item.feature) + ': ' + toText(item.class), 'warn'));
                    frag.appendChild(document.createTextNode(' '));
                }
                frag.appendChild(el('span', null, ' Scientific minimum sample size: REQUIRES_VALIDATION.'));
                note.replaceChildren(frag);
            }
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            if (note) note.replaceChildren(el('span', null, 'Definitions unavailable: ' + toText(err.message)));
        }
    }

    /** Built, unsupervised datasets an experiment can reuse. */
    function fillTrainingDatasetPicker(items) {
        const select = getElement('training-dataset-select');
        if (!select) return;
        const keep = select.value;
        const first = el('option', null, 'Build a new dataset for this run');
        first.value = '';
        const options = [first];
        for (const ds of items) {
            if (ds.status !== 'built' || ds.kind !== 'unsupervised' || !ds.parquet_sha256) continue;
            const opt = el('option', null, toText(ds.name) + ' v' + formatMetric(ds.version)
                + ' — ' + formatMetric(ds.row_count) + ' rows, ' + toText(ds.checksum).slice(0, 10));
            opt.value = toText(ds.id);
            options.push(opt);
        }
        select.replaceChildren.apply(select, options);
        if (keep) select.value = keep;
    }

    async function backfillDatasetHashes() {
        const btn = getElement('backfill-dataset-hashes-btn');
        if (btn) btn.disabled = true;
        try {
            const report = await api('/api/ml/datasets/backfill-hashes', { method: 'POST', timeout: 120000, body: {} });
            const verified = Array.isArray(report.verified) ? report.verified.length : 0;
            const bad = Array.isArray(report.unverifiable) ? report.unverifiable : [];
            renderDatasetsMessage('Verified ' + verified + ' legacy dataset(s); unverifiable: ' + bad.length
                + (bad.length ? ' (' + bad.map(function (x) { return toText(x.name) + ' v' + formatMetric(x.version) + ': ' + toText(x.reason); }).join('; ') + ')' : ''),
                bad.length ? 'warn' : 'ok');
            setTimeout(loadDatasets, 1500);
        } catch (err) {
            if (!err.aborted) renderDatasetsMessage('Verification failed: ' + toText(err.message), 'bad');
        }
    }

    function archiveDataset(datasetId, label) {
        // Releases the Parquet bytes of a dataset no registered model was
        // trained from; the lineage row and manifest stay. The server refuses
        // a dataset any model was trained from (DATASET_REFERENCED_BY_MODEL).
        openActionPanel('Archive dataset ' + label + ' (file released for good; lineage row kept)',
            async function (reason) {
                try {
                    const outcome = await api('/api/ml/datasets/' + encodeURIComponent(datasetId) + '/archive',
                        { method: 'POST', body: { reason: reason } });
                    renderDatasetsMessage('Archived ' + toText(outcome.name) + ' v' + formatMetric(outcome.version)
                        + ' — released ' + formatMetric(outcome.bytes_released) + ' bytes', 'ok');
                    setTimeout(loadDatasets, 1500);
                } catch (err) {
                    if (!err.aborted) renderDatasetsMessage('Archive refused: ' + toText(err.code, 'ERROR') + ' — ' + toText(err.message), 'bad');
                }
            });
    }

    /** Shadow evidence — what a reviewer needs to judge a signal mapping.
     *  Descriptive only; the mapping decision stays REQUIRES_VALIDATION. */
    async function openShadowEvidence() {
        const req = beginRequest('shadow-evidence');
        const drawer = getElement('model-detail-drawer');
        const title = getElement('model-detail-title');
        const body = getElement('model-detail-body');
        const daysSelect = getElement('shadow-days-select');
        if (!drawer || !body) return;
        drawer.hidden = false;
        if (title) title.textContent = 'Shadow evidence (offline mapping review)';
        body.replaceChildren(el('div', 'mlops-mode-desc', 'Loading…'));
        try {
            const report = await api('/api/ml/shadow/evidence', {
                params: { days: daysSelect ? daysSelect.value : 90 }, signal: req.signal
            });
            if (!req.isCurrent()) return;
            const frag = document.createDocumentFragment();
            frag.appendChild(el('div', 'mlops-mode-desc', toText(report.note)));
            frag.appendChild(kvList([
                ['Window', formatMetric(report.window_days) + ' days'],
                ['Predictions', formatMetric(report.predictions) + (toBoolean(report.truncated) ? ' (truncated)' : '')],
                ['Mapping decision', toText(report.mapping_decision)]
            ]));
            const models = (report.models && typeof report.models === 'object') ? Object.values(report.models) : [];
            if (!models.length) frag.appendChild(el('div', 'mlops-mode-desc', 'No shadow predictions in the window.'));
            for (const m of models) {
                const reg = (m.registry && typeof m.registry === 'object') ? m.registry : {};
                frag.appendChild(el('div', 'mlops-subheading', toText(m.model_version_label)
                    + ' · v' + formatMetric(reg.version) + ' · ' + toText(reg.stage) + ' · ' + toText(reg.algorithm)));
                frag.appendChild(kvList([
                    ['Predictions', formatMetric(m.predictions)],
                    ['With reviewed manual outcome', formatMetric(m.with_reviewed_outcome)
                        + ' (coverage ' + formatMetric(m.reviewed_outcome_coverage, 4) + ')'],
                    ['Threshold versions', (Array.isArray(m.threshold_versions) ? m.threshold_versions : []).join(', ') || 'N/A']
                ]));
                const bands = (m.bands && typeof m.bands === 'object') ? m.bands : {};
                for (const bandName of Object.keys(bands)) {
                    const b = bands[bandName];
                    frag.appendChild(el('div', 'mlops-mode-desc', bandName + ': n=' + formatMetric(b.n)
                        + ', reviewed=' + formatMetric(b.with_reviewed_outcome)
                        + ', positive=' + formatMetric(b.positive) + ', negative=' + formatMetric(b.negative)
                        + ', positive share of reviewed=' + formatMetric(b.positive_share_of_reviewed, 4)));
                }
                frag.appendChild(el('div', 'mlops-subheading', 'Rule severity × band · disagreement · score quantiles'));
                frag.appendChild(jsonBlock({
                    rule_severity_x_band: m.rule_severity_x_band,
                    operational_disagreement: m.operational_disagreement,
                    score_quantiles: m.score_quantiles
                }));
            }
            body.replaceChildren(frag);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            body.replaceChildren(el('div', 'mlops-note note-bad', 'Failed to load evidence: ' + toText(err.message)));
        }
    }

    /** Dataset detail — extraction audit, manifest, limitations, models. */
    async function openDatasetDetail(datasetId) {
        const req = beginRequest('dataset-detail');
        const drawer = getElement('model-detail-drawer');
        const title = getElement('model-detail-title');
        const body = getElement('model-detail-body');
        if (!drawer || !body) return;
        drawer.hidden = false;
        if (title) title.textContent = 'Dataset detail';
        body.replaceChildren(el('div', 'mlops-mode-desc', 'Loading…'));
        try {
            const ds = await api('/api/ml/datasets/' + encodeURIComponent(datasetId), { signal: req.signal });
            if (!req.isCurrent()) return;
            const ex = (ds && typeof ds.extraction === 'object' && ds.extraction) ? ds.extraction : {};
            const split = (ds && typeof ds.split_config === 'object' && ds.split_config) ? ds.split_config : {};
            const counts = (split.counts && typeof split.counts === 'object') ? split.counts : {};
            const frag = document.createDocumentFragment();
            frag.appendChild(el('div', 'mlops-subheading', toText(ds.name) + ' v' + formatMetric(ds.version)));
            frag.appendChild(kvList([
                ['Definition', ds.definition_name ? toText(ds.definition_name) + ' ' + toText(ds.definition_version) : 'not recorded (legacy build)'],
                ['Kind', toText(ds.kind)],
                ['Status', toText(ds.status)],
                ['Extraction policy', toText(ex.policy_version)],
                ['Candidate / selected / excluded rows', formatMetric(ex.candidate_rows) + ' / '
                    + formatMetric(ex.selected_rows) + ' / ' + formatMetric(ex.excluded_rows)],
                ['Cap · sampling', formatMetric(ex.cap) + ' · ' + toText(ex.sampling_policy)],
                ['Ordering', toText(ex.ordering, ex.note)],
                ['Time range', formatDateTime(ds.time_range_start) + ' → ' + formatDateTime(ds.time_range_end)],
                ['Split (train / val / test)', formatMetric(counts.train) + ' / ' + formatMetric(counts.val) + ' / ' + formatMetric(counts.test)
                    + ' (dropped for group integrity: ' + formatMetric(split.dropped_for_group_integrity) + ')'],
                ['Split strategy', describeSplitStrategy(split)],
                ['Feature set', toText(ds.feature_set_version)],
                ['Logical checksum (rows)', toText(ds.checksum)],
                ['Parquet sha256 (bytes)', toText(ds.parquet_sha256, 'not recorded (legacy build)')],
                ['File present', ds.file_present === null || ds.file_present === undefined ? 'N/A' : (toBoolean(ds.file_present) ? 'yes' : 'MISSING')],
                ['Manifest', toBoolean(ds.has_manifest) ? 'yes' : 'none'],
                ['Immutable (used by a model)', toBoolean(ds.immutable) ? 'yes' : 'no'],
                ['Code version', toText(ds.code_version)],
                ['Built', formatDateTime(ds.created_at)]
            ]));
            const limits = Array.isArray(ds.feature_set_limitations) ? ds.feature_set_limitations : [];
            frag.appendChild(el('div', 'mlops-subheading', 'Feature-set limitations (frozen under this version)'));
            if (!limits.length) frag.appendChild(el('div', 'mlops-mode-desc', 'None reported.'));
            for (const item of limits) {
                frag.appendChild(el('div', 'mlops-mode-desc', toText(item.feature) + ' — ' + toText(item.class) + ': ' + toText(item.detail)));
            }
            const models = Array.isArray(ds.models) ? ds.models : [];
            frag.appendChild(el('div', 'mlops-subheading', 'Models trained from this dataset (' + models.length + ')'));
            for (const m of models) {
                frag.appendChild(el('div', 'mlops-mode-desc', 'v' + formatMetric(m.version) + ' · ' + toText(m.algorithm) + ' · ' + toText(m.stage) + ' · ' + toText(m.id)));
            }
            if (ds.manifest && typeof ds.manifest === 'object') {
                frag.appendChild(el('div', 'mlops-subheading', 'Manifest'));
                frag.appendChild(jsonBlock(ds.manifest));
            }
            if (ds.quality_report) {
                frag.appendChild(el('div', 'mlops-subheading', 'Quality report'));
                frag.appendChild(jsonBlock(ds.quality_report));
            }
            body.replaceChildren(frag);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            body.replaceChildren(el('div', 'mlops-note note-bad', 'Failed to load dataset: ' + toText(err.message)));
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
                params: { page: 1, page_size: 25 }, signal: req.signal
            });
            if (!req.isCurrent()) return;
            const items = (data && Array.isArray(data.items)) ? data.items : [];
            const frag = document.createDocumentFragment();
            frag.appendChild(el('div', 'mlops-subheading',
                'Recent datasets (' + formatMetric(data.total) + ' total)'));
            if (!items.length) frag.appendChild(el('div', 'mlops-mode-desc', 'None built yet.'));
            for (const ds of items) {
                const ex = (ds && typeof ds.extraction === 'object' && ds.extraction) ? ds.extraction : {};
                const row = el('div', 'mlops-mode-desc mlops-dataset-row');
                row.appendChild(el('span', null,
                    toText(ds.name) + ' v' + formatMetric(ds.version) + ' — ' + toText(ds.kind)
                    + ' · ' + (ds.definition_name ? toText(ds.definition_name) + ' ' + toText(ds.definition_version) : 'legacy build')
                    + ' · rows ' + formatMetric(ds.row_count)
                    + ' (excluded ' + formatMetric(ex.excluded_rows) + ', ' + toText(ex.policy_version) + ')'
                    + ' · rows#' + toText(ds.checksum).slice(0, 10)
                    + ' · bytes#' + (ds.parquet_sha256 ? toText(ds.parquet_sha256).slice(0, 10) : 'N/A')
                    + ' · ' + toText(ds.status)
                    + (ds.file_present === false ? ' · FILE MISSING' : '')
                    + ' · ' + formatDateTime(ds.created_at) + ' '));
                const detailBtn = el('button', 'mlops-btn mlops-btn-small', 'Details');
                detailBtn.type = 'button';
                detailBtn.dataset.datasetId = toText(ds.id);
                row.appendChild(detailBtn);
                if (ds.status === 'built') {
                    // Explicit archive, never automatic: the server refuses a
                    // dataset any registered model was trained from.
                    const archiveBtn = el('button', 'mlops-btn mlops-btn-small', 'Archive');
                    archiveBtn.type = 'button';
                    archiveBtn.dataset.archiveDatasetId = toText(ds.id);
                    archiveBtn.dataset.datasetLabel = toText(ds.name) + ' v' + formatMetric(ds.version);
                    row.appendChild(archiveBtn);
                }
                frag.appendChild(row);
            }
            const legacy = items.filter(function (ds) { return ds.status === 'built' && !ds.parquet_sha256; }).length;
            if (legacy > 0) {
                const fix = el('div', 'mlops-mode-desc');
                fix.appendChild(el('span', null, legacy + ' built dataset(s) predate file hashing and cannot be reused for training until verified. '));
                const backfillBtn = el('button', 'mlops-btn mlops-btn-small', 'Verify legacy datasets');
                backfillBtn.type = 'button';
                backfillBtn.id = 'backfill-dataset-hashes-btn';
                fix.appendChild(backfillBtn);
                frag.appendChild(fix);
            }
            body.replaceChildren(frag);
            fillTrainingDatasetPicker(items);
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

    // ============================================
    // Section help — what each card shows, how to read it, what actions do.
    // Explanations are fixed copy; every NUMBER or STATE shown inside the
    // help comes from the latest API responses kept in `state`, never typed here.
    // ============================================

    const HELP = {
        mode: {
            title: 'Decision Mode',
            what: 'Which engine makes the live threat decision. RULES: the deterministic risk engine (risk-engine-v1) alone. SHADOW: rules still decide; the approved anomaly model runs in parallel and its output is only recorded for comparison. HYBRID and ML are gated this release — requesting them serves rules and records the gate reasons.',
            read: ['The badge is the mode configured NOW (settings.ML_DECISION_MODE).', 'A card marked "gated" lists the exact unmet conditions; nothing here invents readiness.', '"Pause ML" restores RULES immediately and writes an audit row.'],
            actions: ['Activate: changes the configured mode (reason required, audited).', 'Pause ML: emergency stop back to rules.'],
            progress: 'Nothing long-running here: a change applies to the next assessment.'
        },
        labels_readiness: {
            title: 'Label Readiness',
            what: 'Counts of REVIEWED manual labels against the minimums supervised training would need (ML_SUPERVISED_MIN_LABELS / PER_CLASS). Weak, unreviewed and disputed labels are listed but never counted.',
            read: ['"supervised_gate_open" true only means the label COUNT is sufficient; supervised training stays disabled this release (SUPERVISED_NOT_ENABLED).'],
            actions: [], progress: 'Grows as analysts confirm labels in the Labels Queue.'
        },
        data_readiness: {
            title: 'Data Readiness',
            what: 'Feature snapshots the collector has written (the rows datasets are built from) and the graph readiness floors for pair features.',
            read: ['Snapshots are computed point-in-time: only data strictly before each as_of is used.', 'The feature set version stamps every snapshot; models trained under another version never score current snapshots.'],
            actions: ['Run feature collection: computes snapshots for new events (a background job; progress is shown in Training).'],
            progress: 'The feature job reports its stage and percent in the Training card while it runs.'
        },
        capabilities: {
            title: 'Optional Capabilities',
            what: 'Optional ML libraries (MLflow, Optuna, XGBoost, SHAP). Each is reported with one of four honest statuses; none is used this release.',
            read: ['"flag_off" means not enabled; "not_installed" means the package is absent; nothing is downloaded automatically (offline deployment).'],
            actions: [], progress: ''
        },
        registry: {
            title: 'Model Registry',
            what: 'Every trained model with its stage. Lifecycle: training → validated (quality gates passed) → shadow (explicit administrator approval) → archived. Anomaly models can never reach approved/production this release.',
            read: ['Exactly one model per type can be in SHADOW; approving a second one archives the first and retires its threshold set.', 'Detail shows full lineage: dataset id + both hashes, training configuration as run, code version, artifact sha256, whether the artifact file is present, dependency versions, evaluation and the descriptive comparison with the incumbent.'],
            actions: ['Approve for SHADOW (observation only): the only promotion; requires a reason; binds to the artifact checksum; grants NO decision authority.', 'Reject: records a reason and retires thresholds.', 'Stop shadow: rollback — archives the shadow model; rules keep deciding.'],
            progress: 'Training produces a VALIDATED candidate, never a live model.'
        },
        shadow: {
            title: 'Shadow Comparison',
            what: 'What the shadow model said alongside the rules result for the same assessments. Rule severity and anomaly band are different concepts shown side by side; no score difference is ever computed.',
            read: ['operational_disagreement: both_flagged / rules_only / anomaly_only / neither — which mechanism would have raised attention.', 'Evidence report: per band, how many predictions carry a reviewed outcome and how those outcomes split — the material a human needs to judge a future ML→risk mapping. The decision stays REQUIRES_VALIDATION.'],
            actions: ['Window: 7/30/90 days.', 'Evidence report: read-only.'],
            progress: 'Accumulates with every live assessment while the mode is SHADOW.'
        },
        predictions: {
            title: 'Recent Predictions',
            what: 'Individual shadow predictions with their lineage: model, threshold set version, snapshot, event time and, when an analyst later reviewed the assessment, the linked outcome label.',
            read: ['fallback_reason set = the model did not score (no approved model, timeout, artifact/feature-set mismatch…); the live decision was unaffected.', 'Scores are anomaly scores in [0,1], not probabilities.'],
            actions: ['Fallback only: filter to predictions that fell back.'],
            progress: ''
        },
        drift: {
            title: 'Drift Reports',
            what: 'PSI / KS / JS divergence of recent feature snapshots against the preceding window, plus prediction drift (score distribution, fallback and failure rates, latency). Observations only.',
            read: ['insufficient_data below ML_DRIFT_MIN_SAMPLES is honest, not a failure.', 'Severity follows the configured PSI thresholds; drift never triggers retraining or mode changes.'],
            actions: ['Run drift check now: synchronous; also runs on the scheduled monitor.'],
            progress: ''
        },
        training: {
            title: 'Training (manual)',
            what: 'Builds an immutable Parquet dataset (or reuses one you pick) and trains a CANDIDATE. Stages: loading/building dataset → training → evaluating → saving candidate → registering. Success = a VALIDATED model awaiting your shadow approval.',
            read: ['The stage strip and percent come from the job record; a failed job shows its stable error code (e.g. DATASET_FILE_HASH_MISMATCH, QUALITY_GATES_FAILED).', 'Datasets: definition/version, extraction audit (candidate / selected / excluded rows and the cap policy), logical checksum and Parquet file hash. "legacy build" rows predate extraction auditing and are reported, never rewritten.', 'Seed and hyperparameters you enter are persisted verbatim as the model\'s training configuration.'],
            actions: ['Start training: background job; only one at a time.', 'Build dataset: explicit definition, optional time range, and what to do above the cap (refuse by default).', 'Verify legacy datasets: records a file hash only when the reloaded rows reproduce the registered checksum.', 'Archive: releases the Parquet bytes of a dataset no model was trained from (lineage row and manifest stay).'],
            progress: 'Watch the stage strip; the job id links the run to the audit log and the call log.'
        },
        labels: {
            title: 'Labels Queue',
            what: 'Analyst labels (manual) and weak labels about assessments. A manual label can only be created for a RESOLVED assessment; review actions confirm, dispute or retract it; supersede corrects it while keeping the chain.',
            read: ['Only active, manual, reviewed labels count toward readiness and supervised datasets.', 'Labels are linked to predictions as outcomes for later evaluation.'],
            actions: ['Create label, confirm/dispute/retract, supersede.'],
            progress: ''
        },
        policy: {
            title: 'Retraining Policy',
            what: 'Scheduled retraining parameters. Scheduled retraining is GATED this release: enabling it is refused (SCHEDULED_RETRAINING_GATED); training stays a manual, reviewable action.',
            read: ['Values are advisory until the gate is lifted.'], actions: [], progress: ''
        },
        audit: {
            title: 'ML Audit Log',
            what: 'Every administrator action on the ML system: mode changes, pause, training requests, model stage transitions, threshold activation/retirement, label lifecycle, dataset archive/verification.',
            read: ['Each row names the actor, the object and the reason given.'], actions: [], progress: ''
        },
        system: {
            title: 'System State',
            what: 'The facts that define the ML system right now, read from the database and settings at load time: the current feature set and its known limitations, which engine decides, what is in shadow and whether it can score current snapshots, dataset and model inventory, the extraction policy, the call log and the migration head.',
            read: ['Alerts are conditions that need an administrator (for example a shadow model trained under an older feature set — it falls back on every assessment until a current one is approved).', '"What changed" lists the core changes that produced this state, newest first.'],
            actions: ['What changed: release notes.'], progress: ''
        },
        calls: {
            title: 'Recent Calls',
            what: 'One record per /api/ml/* request: time, request id, actor, method, route, status, error code, duration and the ids the call produced. The request id is the X-Request-ID header the browser received and the req=<id> on every server log line of that call.',
            read: ['Errors only: status ≥ 400 (refusals carry their stable error_code).', 'Bodies are sanitised summaries (keys and short values; no feature vectors, no secrets).', 'The same records are in the server application log, tagged [MLOPS_CALL], for offline debugging.'],
            actions: ['Refresh; Errors only.'], progress: ''
        }
    };

    // Short tooltips on the controls themselves (the help modal has the long form).
    const TOOLTIPS = {
        'current-mode-badge': 'Configured decision mode (settings.ML_DECISION_MODE). Rules always make the live decision this release.',
        'pause-ml-btn': 'Emergency stop: restore RULES as the only decision path. Audited.',
        'refresh-overview-btn': 'Reload the overview and the registry.',
        'compute-features-btn': 'Run the feature collector as a background job (point-in-time snapshots for new events).',
        'models-refresh-btn': 'Reload the model registry.',
        'stop-shadow-btn': 'Rollback: archive the model currently in shadow; rules keep deciding.',
        'shadow-days-select': 'Window of shadow comparisons to summarise.',
        'shadow-evidence-btn': 'Per-band evidence with reviewed outcomes, for reviewing a future ML→risk mapping. Read-only; decision stays REQUIRES_VALIDATION.',
        'predictions-fallback-only': 'Show only predictions where the model did not score and rules served alone.',
        'run-drift-btn': 'Compute PSI/KS/JS drift now. Observation only; never retrains.',
        'training-model-type': 'Only behavior_anomaly_model trains this release; the others are reserved interfaces.',
        'training-algorithm': 'isolation_forest (sklearn, seeded) or mad_baseline (robust median/MAD). Both unsupervised.',
        'training-dataset-select': 'Reuse a built dataset (verified by checksum AND Parquet file hash) instead of building a new one.',
        'training-seed-input': 'Random seed recorded with the model (default 42).',
        'training-hyperparameters-input': 'JSON overrides, e.g. {"n_estimators": 200}. Unknown keys are refused, not ignored.',
        'start-training-btn': 'Background job → VALIDATED candidate at best. Never a live model.',
        'cancel-training-btn': 'Cooperative cancel of the running job.',
        'dataset-name-input': 'Dataset name; each build is a new immutable version of that name.',
        'dataset-kind-select': 'unsupervised: snapshots only. supervised: reviewed manual labels joined point-in-time.',
        'dataset-definition-select': 'Typed extraction definition (population, feature set, cap policy). Default = the definition of the kind.',
        'dataset-range-start': 'Earliest snapshot as_of to include (your local wall clock, sent as UTC).',
        'dataset-range-end': 'Exclusive end of the range (your local wall clock, sent as UTC).',
        'dataset-sampling-policy': 'What to do when the population exceeds the cap: refuse (default), or keep the newest/oldest rows — counts are recorded either way.',
        'dataset-split-strategy': 'temporal_group keeps every entity in its earliest period and drops its later rows (no entity recurs; with a long history of regulars this leaves almost no val/test rows). temporal keeps rows in their own period and lets entities recur; the measured overlap is recorded and the scientific gate states it as a fact.',
        'build-dataset-btn': 'Build one immutable dataset version: extraction audit, validation, split, Parquet + manifest + hashes.',
        'labels-filter-review': 'Filter labels by review status.',
        'create-label-btn': 'Manual labels need a RESOLVED assessment; weak labels are capped at confidence 0.5.',
        'policy-model-type': 'Retraining policy per model type (scheduled retraining is gated).',
        'calls-errors-only': 'Only calls that answered with status 400 or higher.',
        'calls-refresh-btn': 'Reload the call log.'
    };

    function applyTooltips() {
        Object.keys(TOOLTIPS).forEach(function (id) {
            const node = getElement(id);
            if (node && !node.title) node.title = TOOLTIPS[id];
        });
    }

    function installHelpButtons() {
        document.querySelectorAll('.mlops-card[data-help]').forEach(function (card) {
            const header = card.querySelector('.mlops-card-header');
            const key = card.getAttribute('data-help');
            if (!header || !HELP[key] || header.querySelector('.mlops-help-btn')) return;
            const btn = el('button', 'mlops-help-btn', '?');
            btn.type = 'button';
            btn.title = 'What is this section and what do its actions do?';
            btn.setAttribute('aria-label', 'Help: ' + HELP[key].title);
            btn.dataset.helpKey = key;
            header.appendChild(btn);
        });
    }

    /** Live facts for the help modal: read from the latest responses only. */
    function helpStatusLines(key) {
        const lines = [];
        if (key === 'mode' && state.currentMode) lines.push('Configured mode now: ' + toText(state.currentMode).toUpperCase());
        if (key === 'training' && state.activeJobId) lines.push('A job is running: ' + toText(state.activeJobId));
        if (key === 'training' && !state.activeJobId) lines.push('No job running.');
        return lines;
    }

    function openHelp(key) {
        const info = HELP[key];
        const modal = getElement('mlops-help-modal');
        const title = getElement('mlops-help-title');
        const body = getElement('mlops-help-body');
        if (!info || !modal || !body) return;
        if (title) title.textContent = info.title;
        const frag = document.createDocumentFragment();
        frag.appendChild(el('h4', null, 'What it is'));
        frag.appendChild(el('p', null, info.what));
        if (info.read && info.read.length) {
            frag.appendChild(el('h4', null, 'How to read it'));
            const ul = el('ul');
            info.read.forEach(function (line) { ul.appendChild(el('li', null, line)); });
            frag.appendChild(ul);
        }
        if (info.actions && info.actions.length) {
            frag.appendChild(el('h4', null, 'Actions'));
            const ul = el('ul');
            info.actions.forEach(function (line) { ul.appendChild(el('li', null, line)); });
            frag.appendChild(ul);
        }
        if (info.progress) {
            frag.appendChild(el('h4', null, 'Progress'));
            frag.appendChild(el('p', null, info.progress));
        }
        const status = helpStatusLines(key);
        if (status.length) {
            frag.appendChild(el('h4', null, 'Right now'));
            const box = el('div', 'mlops-help-status');
            status.forEach(function (line) { box.appendChild(el('div', 'mlops-mode-desc', line)); });
            frag.appendChild(box);
        }
        body.replaceChildren(frag);
        modal.hidden = false;
        if (window.ModalStack) {
            window.ModalStack.open(modal, { backdropClose: true, onClose: function () { modal.hidden = true; } });
        } else {
            modal.style.display = 'flex';
        }
    }

    function closeHelp() {
        const modal = getElement('mlops-help-modal');
        if (!modal) return;
        if (window.ModalStack && window.ModalStack.isOpen(modal)) window.ModalStack.close(modal);
        else { modal.style.display = 'none'; modal.hidden = true; }
    }

    // ============================================
    // Call log
    // ============================================

    async function loadCalls() {
        const req = beginRequest('calls');
        const body = getElement('calls-body');
        const errorsOnly = getElement('calls-errors-only');
        if (!body) return;
        try {
            const data = await api('/api/ml/calls', {
                params: { limit: 50, errors_only: errorsOnly && errorsOnly.checked ? 'true' : 'false' },
                signal: req.signal
            });
            if (!req.isCurrent()) return;
            const items = (data && Array.isArray(data.items)) ? data.items : [];
            const frag = document.createDocumentFragment();
            frag.appendChild(el('div', 'mlops-mode-desc', toText(data.note)));
            if (!items.length) frag.appendChild(el('div', 'mlops-mode-desc', 'No calls recorded yet.'));
            for (const c of items) {
                const status = toFiniteNumber(c.status);
                const row = el('div', 'mlops-call-row' + (status !== null && status >= 400 ? ' call-error' : ''));
                row.appendChild(el('span', null, formatDateTime(c.ts)));
                const rid = el('code', null, toText(c.request_id));
                rid.title = 'Request id — grep the server log for req=' + toText(c.request_id);
                row.appendChild(rid);
                row.appendChild(el('span', null, toText(c.method)));
                const route = el('span', null, toText(c.route, c.path));
                route.title = toText(c.path);
                row.appendChild(route);
                row.appendChild(el('span', null, formatMetric(c.status)));
                row.appendChild(el('span', null, formatMetric(c.ms) + ' ms'));
                const tail = c.error_code ? toText(c.error_code)
                    : (c.produced && typeof c.produced === 'object'
                        ? Object.keys(c.produced).map(function (k) { return k + '=' + toText(c.produced[k]).slice(0, 8); }).join(' ')
                        : toText(c.actor));
                row.appendChild(el('span', null, tail));
                frag.appendChild(row);
            }
            body.replaceChildren(frag);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            body.replaceChildren(el('div', 'mlops-note note-bad', 'Failed to load calls: ' + toText(err.message)));
        }
    }

    // ============================================
    // System state — facts + the core changes behind them
    // ============================================

    function renderSystemState(system) {
        const body = getElement('system-state-body');
        if (!body) return;
        if (!system || typeof system !== 'object') {
            body.replaceChildren(el('div', 'mlops-mode-desc', 'System state not reported.'));
            return;
        }
        state.releaseNotes = Array.isArray(system.release_notes) ? system.release_notes : [];
        const frag = document.createDocumentFragment();
        // The contract, in the exact vocabulary operators read — every value
        // from the backend; engineering readiness, scientific validity and
        // decision authority are three different things shown side by side.
        const contract = system.ml_contract && typeof system.ml_contract === 'object' ? system.ml_contract : null;
        if (contract) {
            const grid = el('div', 'mlops-contract');
            const items = [
                ['Dataset', contract.dataset], ['Feature set', contract.feature_set],
                ['Model', contract.model], ['Engineering readiness', contract.engineering_readiness],
                ['Scientific validity', contract.scientific_validity], ['Signal mapping', contract.signal_mapping],
                ['ML decision authority', contract.ml_decision_authority], ['Rules', contract.rules],
                ['Fallback', contract.fallback], ['Evidence collection', contract.evidence_collection]
            ];
            for (const [label, value] of items) {
                const cell = el('div', 'mlops-contract-cell');
                cell.appendChild(el('span', 'mlops-contract-label', label));
                const v = toText(value, 'NOT_RECORDED');
                const tone = /PASS|VALIDATED|ACTIVE|AUTHORITATIVE|SHADOW_APPROVED|VALID_FOR/.test(v) && !/INSUFFICIENT|REQUIRES|DISABLED|INCOMPATIBLE|NOT_/.test(v) ? 'ok'
                    : /INSUFFICIENT|REQUIRES|NOT_RECORDED|INCOMPATIBLE|INACTIVE|NONE|FAIL/.test(v) ? 'warn' : 'info';
                cell.appendChild(chip(v, tone));
                grid.appendChild(cell);
            }
            frag.appendChild(grid);
            frag.appendChild(el('div', 'mlops-mode-desc', toText(contract.note)));
        }
        const alerts = Array.isArray(system.alerts) ? system.alerts : [];
        for (const a of alerts) {
            const note = el('div', 'mlops-note ' + (a.level === 'warn' ? 'note-warn' : 'note-ok'), toText(a.message));
            note.title = toText(a.code);
            frag.appendChild(note);
        }
        const fs = system.feature_set && typeof system.feature_set === 'object' ? system.feature_set : {};
        const dec = system.decision && typeof system.decision === 'object' ? system.decision : {};
        const sm = system.shadow_model && typeof system.shadow_model === 'object' ? system.shadow_model : null;
        const ds = system.datasets && typeof system.datasets === 'object' ? system.datasets : {};
        const models = system.models && typeof system.models === 'object' ? system.models : {};
        const byStage = models.by_stage && typeof models.by_stage === 'object' ? models.by_stage : {};
        const cl = system.call_log && typeof system.call_log === 'object' ? system.call_log : {};
        const limits = Array.isArray(fs.limitations) ? fs.limitations : [];
        const activeFeatures = Array.isArray(fs.active_person_features) ? fs.active_person_features : [];
        const gated = dec.gated && typeof dec.gated === 'object' ? Object.keys(dec.gated) : [];
        frag.appendChild(kvList([
            ['Feature set', toText(fs.current) + ' (previous: ' + toText(fs.previous) + ') · '
                + activeFeatures.length + ' active person features · limitations: '
                + (limits.length ? limits.map(function (l) { return toText(l.feature); }).join(', ') : 'none reported')],
            ['Decision', 'requested ' + toText(dec.requested_mode).toUpperCase() + ' · live engine: '
                + toText(dec.live_engine) + ' · ML role: ' + toText(dec.ml_role)
                + (gated.length ? ' · gated: ' + gated.join(', ').toUpperCase() : '')],
            ['Shadow model', sm ? ('v' + formatMetric(sm.version) + ' ' + toText(sm.algorithm) + ' · trained under '
                + toText(sm.feature_set_version) + ' · '
                + (toBoolean(sm.compatible_with_current_features) ? 'compatible with current snapshots' : 'NOT compatible with current snapshots')
                + (toBoolean(sm.artifact_present) ? '' : ' · ARTIFACT FILE MISSING')) : 'none'],
            ['Datasets', formatMetric(ds.total) + ' total · ' + formatMetric(ds.built) + ' built · '
                + formatMetric(ds.archived) + ' archived · ' + formatMetric(ds.legacy_unverified) + ' legacy unverified · '
                + formatMetric(ds.legacy_extraction_policy) + ' under ' + toText(ds.legacy_extraction_policy_version)
                + ' · current policy ' + toText(ds.extraction_policy_version)],
            ['Dataset definitions', (Array.isArray(ds.definitions) ? ds.definitions : []).join(', ') || 'N/A'],
            ['Models by stage', Object.keys(byStage).map(function (k) { return k + ': ' + formatMetric(byStage[k]); }).join(' · ') || 'none'
                + (toFiniteNumber(models.missing_artifact_files) ? ' · MISSING FILES: ' + formatMetric(models.missing_artifact_files) : '')],
            ['Call log', (toBoolean(cl.enabled) ? 'enabled' : 'disabled') + ' · ' + toText(cl.sink)
                + ' · ' + toText(cl.readable_via)],
            ['Migration head', toText(system.migration_head)]
        ]));
        body.replaceChildren(frag);
    }

    function openReleaseNotes() {
        const modal = getElement('mlops-help-modal');
        const title = getElement('mlops-help-title');
        const body = getElement('mlops-help-body');
        if (!modal || !body) return;
        if (title) title.textContent = 'What changed — core changes behind the current state';
        const frag = document.createDocumentFragment();
        const notes = Array.isArray(state.releaseNotes) ? state.releaseNotes : [];
        if (!notes.length) frag.appendChild(el('p', null, 'No release notes reported.'));
        for (const n of notes) {
            frag.appendChild(el('h4', null, toText(n.title)));
            frag.appendChild(el('p', null, toText(n.detail)));
        }
        body.replaceChildren(frag);
        modal.hidden = false;
        if (window.ModalStack) {
            window.ModalStack.open(modal, { backdropClose: true, onClose: function () { modal.hidden = true; } });
        } else {
            modal.style.display = 'flex';
        }
    }

    function init() {
        debugLog('ml-ops init');
        on('system-notes-btn', 'click', openReleaseNotes);
        installHelpButtons();
        applyTooltips();
        on('mlops-help-close', 'click', closeHelp);
        document.querySelectorAll('.mlops-help-btn').forEach(function (btn) {
            btn.addEventListener('click', function () { openHelp(btn.dataset.helpKey); });
        });
        on('calls-refresh-btn', 'click', loadCalls);
        on('calls-errors-only', 'change', loadCalls);
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
        on('shadow-evidence-btn', 'click', openShadowEvidence);
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
        on('datasets-body', 'click', function (event) {
            const origin = event.target && event.target.closest ? event.target : null;
            if (!origin) return;
            const detail = origin.closest('button[data-dataset-id]');
            if (detail) { openDatasetDetail(detail.dataset.datasetId); return; }
            const archive = origin.closest('button[data-archive-dataset-id]');
            if (archive) { archiveDataset(archive.dataset.archiveDatasetId, archive.dataset.datasetLabel); return; }
            if (origin.closest('#backfill-dataset-hashes-btn')) backfillDatasetHashes();
        });
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
        loadDatasetDefinitions();
        loadDatasets();
        loadLabels();
        loadPolicy();
        loadAudit();
        loadCalls();
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
