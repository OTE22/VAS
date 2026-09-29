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
    const JOB_POLL_MAX_BACKOFF_MS = 30000;
    const PAGE_SIZE = 10;
    const MODE_ORDER = ['rules', 'shadow', 'hybrid', 'ml'];
    const WORKSPACE_ORDER = ['overview', 'prepare', 'review', 'monitor', 'audit'];
    const WORKSPACES = {
        'overview': {
            kicker: 'Current state',
            title: 'Overview',
            description: 'Check service health, active work, and which system is making live decisions.',
            purpose: 'Begin here before changing anything. This stage confirms that the control plane and worker can accept work and shows whether rules or shadow ML are active.',
            run: ['Select Refresh console.', 'Review Work in progress, System health, and Live decision mode.'],
            verify: ['The console says Connected, the worker says Healthy, and the decision authority matches your expectation.', 'Any queued or running job has a visible stage and progress value.'],
            recover: ['If your session expired, sign in again and return to this page.', 'If the worker is unavailable, restore it and refresh; durable queued commands remain recorded. Production rules continue to protect live decisions.'],
            primaryTarget: 'refresh-console-btn',
            primaryLabel: 'Refresh all status'
        },
        'prepare': {
            kicker: 'Lifecycle step 2',
            title: 'Prepare data and train',
            description: 'Review readiness, build datasets, manage labels, and start a manual training run.',
            purpose: 'Create trustworthy, point-in-time data and a candidate model. Nothing in this stage changes live decisions.',
            run: ['Compute missing features and review outcome labels.', 'Build or select a compatible immutable dataset, then start a training run.'],
            verify: ['The queue reports Completed, readiness shows sufficient coverage, and the dataset records its hashes.', 'A completed run creates a model record. Inspect its actual stage and evaluation gates in Review models.'],
            recover: ['Read the error beside the action and correct the stated field, time range, or compatibility problem.', 'Use the request ID in Audit when the cause is unclear, then rerun or cancel the active job.'],
            primaryTarget: 'workflows',
            primaryLabel: 'Open build and train'
        },
        'review': {
            kicker: 'Lifecycle step 3',
            title: 'Review models',
            description: 'Inspect registered models and test observational scoring before approving shadow use.',
            purpose: 'Decide whether a validated candidate has enough evidence for safe shadow observation. Approval still gives it no live decision authority.',
            run: ['Open Model detail and check dataset hashes, metrics, gates, purpose, and serving scope.', 'Run an observational evaluation, then approve a suitable candidate for Shadow with an audit reason.'],
            verify: ['The model stage changes to Shadow and the confirmation appears beside the registry.', 'Rules remain the decision authority and shadow predictions begin appearing in Monitor.'],
            recover: ['Reject an unsuitable candidate with a reason.', 'Use Stop shadow (rollback) if confidence is lost; this archives the shadow model while rules remain live.'],
            primaryTarget: 'model-registry',
            primaryLabel: 'Review candidate models'
        },
        'monitor': {
            kicker: 'Lifecycle step 4',
            title: 'Monitor shadow ML',
            description: 'Compare rules with shadow results, inspect fallbacks, and review drift without affecting live decisions.',
            purpose: 'Observe how shadow ML behaves next to the rule system. These results are evidence for review, never an automatic deployment decision.',
            run: ['Choose the time window and model, then review the shadow comparison and recent predictions.', 'Run a drift check and inspect fallbacks, missing features, sample size, and response time.'],
            verify: ['Predictions arrive for the expected model with acceptable fallback and failure rates.', 'Drift reports have enough samples and no unresolved compatibility warning.'],
            recover: ['Compute missing features or approve a compatible shadow model when data is absent.', 'Stop shadow from Review models if safety or confidence is in doubt.'],
            primaryTarget: 'observability',
            primaryLabel: 'Inspect shadow evidence'
        },
        'audit': {
            kicker: 'Lifecycle step 5',
            title: 'Audit and troubleshoot',
            description: 'Trace administrator actions and API calls with reasons, request IDs, and error codes.',
            purpose: 'Reconstruct what happened and troubleshoot failures without guessing. Every lifecycle change should have an actor, reason, and outcome.',
            run: ['Review Administrator activity for lifecycle changes.', 'Use Recent API calls and Errors only to correlate the time, status, error code, and request ID.'],
            verify: ['The actor, action, target, reason, and API result describe the same event.', 'A successful retry is visible after the original failed request.'],
            recover: ['For 401, sign in again. For 409, refresh and review lifecycle gates. For 422, correct the input. For 404, choose a current record.', 'Give the request ID to an administrator when the service error cannot be resolved here.'],
            primaryTarget: 'audit-trail',
            primaryLabel: 'Review activity and errors'
        }
    };

    const MODEL_TYPE_LABELS = {
        behavior_anomaly_model: 'Behavior anomaly (person)',
        coappearance_anomaly_model: 'Coappearance anomaly (identity pair)',
        social_graph_anomaly_model: 'Social graph anomaly (person)',
        threat_ranking_model: 'Analyst review queue',
        tabular_regression_model: 'Numeric experiments (offline)'
    };
    const ALGORITHM_LABELS = {
        isolation_forest: 'Isolation Forest',
        mad_baseline: 'Median/MAD baseline',
        empirical_baseline: 'Empirical baseline',
        logistic_regression: 'Logistic regression'
    };

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

    function humanizeToken(value) {
        const text = toText(value, 'Unknown').replace(/[_-]+/g, ' ').toLowerCase();
        return text.charAt(0).toUpperCase() + text.slice(1);
    }

    function friendlyModelType(value) {
        return MODEL_TYPE_LABELS[value] || humanizeToken(value);
    }

    function friendlyAlgorithm(value) {
        return ALGORITHM_LABELS[value] || humanizeToken(value);
    }

    function recoveryForError(err) {
        const byCode = {
            MODE_GATED: 'Review the unmet gate in Live decision mode before retrying.',
            VALIDATION_ERROR: 'Check the required fields and use compatible values.',
            INVALID_TIME_RANGE: 'Choose a valid start and end time, with the start before the end.',
            DATASET_DEFINITION_KIND_MISMATCH: 'Select a dataset definition made for this model type.',
            DATASET_REFERENCED_BY_MODEL: 'This dataset is in use by a model and cannot be archived.',
            DATASET_ALREADY_ARCHIVED: 'Refresh datasets and choose an active dataset.',
            MODEL_NOT_FOUND: 'Refresh models and choose a current model.',
            INVALID_MODEL_ID: 'Select a registered model and retry.',
            AUTH_EXPIRED: 'Sign in again, then return to this page.'
        };
        if (err && err.code && byCode[err.code]) return byCode[err.code];
        const status = err && err.status;
        if (status === 401) return 'Sign in again, then return to this page.';
        if (status === 404) return 'The selected record no longer exists. Refresh and choose a current record.';
        if (status === 409) return 'Refresh this workspace and review the current lifecycle state or gate before retrying.';
        if (status === 422) return 'Check the required fields and compatible options, then retry.';
        if (status >= 500) return 'Check System health, note the request ID, and retry after the service recovers.';
        return 'Check the backend connection, refresh this workspace, and retry.';
    }

    function formatActionError(context, err) {
        let text = context + ': ' + toText(err && err.message, 'Unknown error') + '. ' + recoveryForError(err);
        if (err && err.code) text += ' Error code: ' + err.code + '.';
        if (err && err.requestId) text += ' Request ID: ' + err.requestId + '.';
        return text;
    }

    function formatDateTime(value) {
        if (!value) return '—';
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return String(value);
        return parsed.toLocaleString();
    }

    function formatRelativeTime(value) {
        if (!value) return 'time not reported';
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return 'time not reported';
        const seconds = Math.round((Date.now() - parsed.getTime()) / 1000);
        if (Math.abs(seconds) < 45) return 'just now';
        const minutes = Math.round(seconds / 60);
        if (Math.abs(minutes) < 60) return Math.abs(minutes) + 'm ' + (minutes >= 0 ? 'ago' : 'from now');
        const hours = Math.round(minutes / 60);
        if (Math.abs(hours) < 48) return Math.abs(hours) + 'h ' + (hours >= 0 ? 'ago' : 'from now');
        const days = Math.round(hours / 24);
        return Math.abs(days) + 'd ' + (days >= 0 ? 'ago' : 'from now');
    }

    function setSummaryValue(id, value, tone, title) {
        const node = getElement(id);
        if (!node) return;
        node.textContent = toText(value);
        node.className = tone ? 'is-' + tone : '';
        if (title) node.title = title;
    }

    function setConsoleConnection(status, label) {
        const node = getElement('console-connection-state');
        if (!node) return;
        if (status === 'online' && (state.evidence.overview === 'error' || state.jobPollFailures)) {
            status = 'offline'; label = 'Some status checks failed — refresh';
        }
        state.consoleStatus = status;
        node.className = 'mlops-live-state is-' + status;
        const textNode = node.querySelector('span:last-child');
        if (textNode) textNode.textContent = label;
        updateNextStep();
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

    function simpleTable(headers, rows) {
        const table = el('table', 'mlops-table');
        const thead = el('thead');
        const hr = el('tr');
        headers.forEach(function (h) { hr.appendChild(el('th', null, h)); });
        thead.appendChild(hr);
        table.appendChild(thead);
        const tbody = el('tbody');
        rows.forEach(function (cells) {
            const tr = el('tr');
            cells.forEach(function (c, i) {
                const td = el('td', null, c === null || c === undefined ? 'N/A' : String(c));
                if (i === cells.length - 1 && /^(PASS|FAIL|NOT_CONFIGURED|INSUFFICIENT_SAMPLE)$/.test(String(c))) {
                    td.className = String(c) === 'PASS' ? 'mlops-status-ok'
                        : String(c) === 'FAIL' ? 'mlops-status-bad' : 'mlops-status-warn';
                }
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        const wrap = el('div', 'mlops-table-wrap');
        wrap.appendChild(table);
        return wrap;
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
        e.requestId = (opts && opts.requestId) || null;
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
            if (err && err.name === 'AbortError') {
                if (timeoutCtl.signal.aborted) throw ApiError('Request timed out. Retry after checking the service connection.', { status: 408, code: 'REQUEST_TIMEOUT' });
                throw ApiError('Request cancelled', { aborted: true });
            }
            throw ApiError('Network error — backend unreachable', { status: 0 });
        }
        window.clearTimeout(timer);

        if (response.status === 401) {
            window.location.href = '/signin';
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
            throw ApiError(message, {
                status: response.status,
                code: code,
                detailExtra: detailExtra,
                requestId: response.headers.get('X-Request-ID') || response.headers.get('X-Request-Id')
            });
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
        selectedService: 'behavior_anomaly_model',
        journey: {selections: {}},
        services: [],
        servicesState: 'loading',
        servicesError: '',
        serviceEvidence: null,
        serviceActionBusy: false,
        currentMode: null,
        releaseNotes: [],
        predictionsPage: 1,
        labelsPage: 1,
        auditPage: 1,
        activeJobs: new Map(),
        recentJobs: [],
        mlWorker: null,
        jobPollTimer: null,
        jobPollFailures: 0,
        lastTerminalJobSignature: '',
        lastJobsSyncAt: null,
        modelTypes: [],
        models: [],
        modelsFamily: null,
        datasets: [],
        evidence: { overview: 'loading', datasets: 'loading', models: 'loading' },
        featureSnapshots: null,
        activeWorkspace: 'overview',
        consoleStatus: 'connecting',
        pendingAction: null    // { title, execute(reason) }
    };

    const SERVICE_CHOICES = [
        {model_type: 'behavior_anomaly_model', title: 'Behavior assessment', icon: 'fa-person-walking', description: 'Add unusual behavior evidence to threat assessment.', destination: 'Security Intelligence · Threat Assessment'},
        {model_type: 'coappearance_anomaly_model', title: 'Pair relationships', icon: 'fa-user-group', description: 'Observe unusual patterns between identity pairs.', destination: 'Security Intelligence · Social Network'},
        {model_type: 'social_graph_anomaly_model', title: 'Social network', icon: 'fa-diagram-project', description: 'Observe unusual positions in a relationship network.', destination: 'Security Intelligence · Social Network'},
        {model_type: 'threat_ranking_model', title: 'Analyst review queue', icon: 'fa-list-check', description: 'Prioritize reviews using independently reviewed outcomes.', destination: 'ML Ops · Analyst review'},
        {model_type: 'tabular_regression_model', title: 'Numeric experiments', icon: 'fa-flask', description: 'Predict an explicitly chosen numeric target offline.', destination: 'ML Ops · Offline evaluation only'}
    ];
    const JOURNEY_STORAGE_KEY = 'mlops.serviceJourney.v1';
    function savedJourney() {
        try {
            const value = JSON.parse(window.localStorage.getItem(JOURNEY_STORAGE_KEY) || '{}');
            return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
        } catch (_) { return {}; }
    }
    function persistJourney() {
        try { window.localStorage.setItem(JOURNEY_STORAGE_KEY, JSON.stringify(state.journey)); } catch (_) { /* Storage can be unavailable in private sessions. */ }
    }
    function journeySelection() {
        const family = state.selectedService || 'behavior_anomaly_model';
        if (!state.journey.selections[family] || typeof state.journey.selections[family] !== 'object' || Array.isArray(state.journey.selections[family])) state.journey.selections[family] = {};
        return state.journey.selections[family];
    }
    function selectedServiceContract() {
        return state.modelTypes.find(item => item.model_type === state.selectedService) || null;
    }
    function serviceModels() {
        return state.models.filter(model => model.model_type === state.selectedService);
    }
    function compatibleDataset(dataset, contract) {
        if (!contract || !dataset || dataset.status !== 'built' || dataset.file_present === false || !dataset.parquet_sha256) return false;
        if (dataset.kind !== contract.dataset_kind) return false;
        if (contract.feature_set_version && dataset.feature_set_version !== contract.feature_set_version) return false;
        // Definition identifies the training population; two unsupervised datasets are not interchangeable.
        return !contract.dataset_definition || dataset.definition_name === contract.dataset_definition;
    }
    function serviceStatus() {
        return state.services.find(item => item.model_type === state.selectedService) || null;
    }
    function serviceModel() {
        const selected = journeySelection().model;
        return serviceModels().find(model => model.id === selected) || null;
    }
    function setJourneyStage(stage, focus) {
        if (!['prepare', 'test', 'activate', 'monitor'].includes(stage)) stage = 'prepare';
        journeySelection().stage = stage;
        persistJourney();
        document.querySelectorAll('[data-journey-stage]').forEach(button => {
            const selected = button.dataset.journeyStage === stage;
            button.setAttribute('aria-pressed', String(selected));
            button.classList.toggle('is-active', selected);
            if (selected) button.setAttribute('aria-current', 'step'); else button.removeAttribute('aria-current');
        });
        document.querySelectorAll('[data-journey-panel]').forEach(panel => { panel.hidden = panel.dataset.journeyPanel !== stage; });
        if (focus) {
            const panel = document.querySelector('[data-journey-panel="' + stage + '"]');
            if (panel) { panel.setAttribute('tabindex', '-1'); panel.focus({preventScroll: true}); panel.scrollIntoView({block: 'nearest'}); }
        }
        renderServiceNotebook();
        if (stage === 'test' || stage === 'activate') loadServiceModelEvidence();
    }
    function selectService(modelType, refresh) {
        if (!SERVICE_CHOICES.some(item => item.model_type === modelType)) return;
        state.selectedService = modelType;
        if (refresh && state.modelsFamily !== modelType) state.evidence.models = 'loading';
        state.journey.service = modelType;
        state.serviceEvidence = null;
        // Invalidate any pending evidence request when the service changes.
        beginRequest('service-model');
        persistJourney();
        for (const id of ['training-model-type', 'policy-model-type', 'evaluation-model-type']) {
            const select = getElement(id);
            if (select && [...select.options].some(option => option.value === modelType)) select.value = modelType;
        }
        const contract = selectedServiceContract();
        const kind = getElement('dataset-kind-select'), definition = getElement('dataset-definition-select');
        if (kind && contract) kind.value = contract.dataset_kind;
        if (definition && contract && [...definition.options].some(option => option.value === contract.dataset_definition)) definition.value = contract.dataset_definition;
        updateTrainingAvailability();
        fillTrainingDatasetPicker(state.datasets);
        updateEvaluationForm();
        setNote('service-action-note', '', null);
        renderServiceJourney();
        setJourneyStage(journeySelection().stage || 'prepare', false);
        updateNextStep();
        if (refresh) { loadModels(); loadPolicy(); }
    }
    function openAdvanced(workspace, targetId) {
        const details = getElement('mlops-advanced-tools');
        if (details) details.open = true;
        activateWorkspace(workspace, true);
        const target = getElement(targetId);
        if (target) { target.scrollIntoView({block: 'center'}); if (target.matches('button, input, select, a')) target.focus(); }
    }
    async function loadServices() {
        const req = beginRequest('services');
        try {
            const data = await api('/api/ml/services/status', {signal: req.signal});
            if (!req.isCurrent()) return;
            if (!data || !Array.isArray(data.items)) throw {message: 'The service readiness response was incomplete.'};
            state.services = data.items;
            state.servicesState = 'ready';
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            state.servicesState = 'error';
            state.servicesError = formatActionError('Service status unavailable', err);
        }
        renderServiceJourney();
        updateNextStep();
    }
    function serviceStateLabel(service) {
        if (state.servicesState !== 'ready') return state.servicesState === 'error' ? 'Status unavailable' : 'Checking status';
        if (!service) return 'Status not reported';
        return humanizeToken(service.state || 'unknown');
    }
    function serviceConnectionBlockers(model) {
        if (!model) return [{message: 'Prepare and train a candidate first.'}];
        return Array.isArray(model.connection_blockers) ? model.connection_blockers
            : [{message: 'Connection evidence is unavailable. Refresh the model list before continuing.'}];
    }
    function serviceTrainingBlocked(service, datasetId) {
        const readiness = service?.training_readiness;
        if (!readiness) return true;
        return (readiness.blockers || []).some(item => !datasetId || item.code !== 'SOURCE_HISTORY_REQUIRED');
    }
    function renderServiceChecklist(service, model, datasetId) {
        const root = getElement('service-workflow-checklist');
        if (!root) return;
        const selected = service?.selected_model, blockers = serviceConnectionBlockers(model);
        const trained = !!model && ['validated', 'shadow', 'approved'].includes(model.stage);
        const dataBlocked = serviceTrainingBlocked(service, datasetId);
        const connected = !!selected;
        const used = connected && service?.consumption?.selected_model_used === true;
        const steps = [
            ['Check data', trained || !dataBlocked ? 'Ready' : 'Blocked', 'prepare'],
            ['Prepare dataset', trained ? 'Completed' : datasetId ? 'Saved dataset selected' : 'Pending', 'prepare'],
            ['Train model', trained ? 'Completed' : state.activeJobs.size ? 'Job in progress' : 'Pending', 'prepare'],
            ['Review evaluation', !model ? 'Pending' : blockers.length ? 'Blocked' : 'Ready for review', 'test'],
            ['Connect service', connected ? 'Selected version ' + selected.version : 'Pending', 'activate'],
            ['Verify service use', used ? 'Verified' : connected ? 'Waiting for a successful request' : 'Pending', 'monitor']
        ];
        const focusLabel = root.contains(document.activeElement) ? document.activeElement.textContent : null;
        root.replaceChildren();
        for (const [label, status, stage] of steps) {
            const item = el('li', 'mlops-checklist-item');
            const button = el('button', 'mlops-btn', label); button.type = 'button';
            button.addEventListener('click', () => setJourneyStage(stage, true));
            item.append(button, el('span', null, status)); root.appendChild(item);
        }
        if (focusLabel) Array.from(root.querySelectorAll('button')).find(button => button.textContent === focusLabel)?.focus({preventScroll: true});
        const next = getElement('service-workflow-next');
        let stage = 'prepare', label = 'Prepare & train';
        if (service?.serving_mode === 'offline_regression') { label = 'Configure offline experiment'; }
        else if (selected) { stage = state.selectedService === 'behavior_anomaly_model' && service.decision_mode === 'rules' ? 'activate' : 'monitor'; label = stage === 'activate' ? 'Start observation' : used ? 'Monitor service use' : 'Verify service use'; }
        else if (trained || model) { stage = 'test'; label = blockers.length ? 'Resolve evaluation requirements' : 'Review trained model'; }
        else if (dataBlocked) { label = 'Inspect data requirements'; }
        else if (state.activeJobs.size) { label = 'View training progress'; }
        next.textContent = label; next.dataset.journeyStage = stage;
        next.disabled = state.servicesState !== 'ready';
        getElement('service-workflow-milestone').textContent = connected
            ? (used ? 'Model connected; successful service use recorded.' : 'Model selected; successful service use still needs verification.')
            : trained ? 'Model trained. Complete evaluation and connection before application use.'
            : 'Complete the prerequisites below. Your existing jobs, datasets and model versions are preserved.';
    }

    function renderServiceJourney() {
        const cards = getElement('mlops-service-cards');
        if (!cards) return;
        const frag = document.createDocumentFragment();
        for (const choice of SERVICE_CHOICES) {
            const service = state.services.find(item => item.model_type === choice.model_type);
            const button = el('button', 'mlops-service-card' + (state.selectedService === choice.model_type ? ' is-active' : ''));
            button.type = 'button'; button.dataset.serviceType = choice.model_type;
            button.setAttribute('aria-pressed', String(state.selectedService === choice.model_type));
            button.appendChild(faIcon('fas ' + choice.icon));
            button.appendChild(el('strong', null, choice.title));
            button.appendChild(el('span', null, choice.description));
            button.appendChild(chip(serviceStateLabel(service), state.servicesState === 'error' ? 'bad' : service && service.selected_model ? 'info' : 'warn'));
            button.addEventListener('click', () => selectService(choice.model_type, true));
            frag.appendChild(button);
        }
        // Keep focus stable while periodic status refreshes update the cards.
        const focusedType = document.activeElement && document.activeElement.dataset.serviceType;
        cards.replaceChildren(frag);
        if (focusedType) cards.querySelector('[data-service-type="' + focusedType + '"]')?.focus({preventScroll: true});
        const choice = SERVICE_CHOICES.find(item => item.model_type === state.selectedService);
        const service = serviceStatus(), contract = selectedServiceContract();
        getElement('mlops-journey-title').textContent = choice.title;
        getElement('mlops-service-destination').textContent = 'Used by: ' + toText(service && service.destination && service.destination.name, choice.destination);
        getElement('mlops-service-state').textContent = serviceStateLabel(service);
        const blockers = getElement('mlops-service-blockers');
        blockers.replaceChildren();
        if (state.servicesState === 'error') blockers.appendChild(el('p', 'mlops-note note-bad', state.servicesError));
        else if (service && Array.isArray(service.blockers) && service.blockers.length) {
            const list = el('ul', 'mlops-service-blocker-list');
            for (const blocker of service.blockers) list.appendChild(el('li', null, toText(blocker.message)));
            blockers.appendChild(list);
        }
        const selection = journeySelection();
        const datasets = state.datasets.filter(dataset => compatibleDataset(dataset, contract));
        const datasetPicker = getElement('service-dataset-select');
        datasetPicker.replaceChildren(el('option', null, 'Prepare a new dataset from available history'));
        datasetPicker.firstChild.value = '';
        for (const dataset of datasets) {
            const option = el('option', null, toText(dataset.name) + ' · v' + formatMetric(dataset.version) + ' · ' + formatMetric(dataset.row_count) + ' rows');
            option.value = dataset.id; datasetPicker.appendChild(option);
        }
        datasetPicker.value = datasets.some(dataset => dataset.id === selection.dataset) ? selection.dataset : '';
        const models = serviceModels().filter(model => model.stage !== 'archived' && model.stage !== 'rejected');
        const modelPicker = getElement('service-model-select');
        modelPicker.replaceChildren(el('option', null, models.length ? 'Choose a candidate to review' : 'No candidate yet — prepare and train first'));
        modelPicker.firstChild.value = '';
        for (const model of models) {
            const option = el('option', null, 'Version ' + formatMetric(model.version) + ' · ' + friendlyAlgorithm(model.algorithm) + ' · ' + humanizeToken(model.stage));
            option.value = model.id; modelPicker.appendChild(option);
        }
        if (state.evidence.models === 'ready' && state.modelsFamily === state.selectedService && !models.some(model => model.id === selection.model)) {
            const recommended = service && service.candidate;
            selection.model = recommended && models.some(model => model.id === recommended.id) ? recommended.id : (models.find(model => ['validated', 'shadow', 'approved'].includes(model.stage)) || {}).id || '';
        }
        modelPicker.value = selection.model || '';
        persistJourney();
        const offline = state.selectedService === 'tabular_regression_model';
        getElement('mlops-prepare-copy').textContent = offline
            ? 'Numeric experiments need an explicit target and a saved pipeline. Configure those in Advanced training options; the result remains offline.'
            : 'One durable job collects features, builds and validates a dataset, then trains and evaluates a compatible candidate. This does not activate a model.';
        getElement('service-train-btn').disabled = offline || !contract || !contract.trainable || state.servicesState !== 'ready' || state.mlWorker?.status !== 'healthy' || state.activeJobs.size > 0 || state.serviceActionBusy || serviceTrainingBlocked(service, datasetPicker.value);
        getElement('service-data-requirements').replaceChildren();
        for (const requirement of service?.training_readiness?.blockers || []) {
            if (datasetPicker.value && requirement.code === 'SOURCE_HISTORY_REQUIRED') continue;
            getElement('service-data-requirements').appendChild(el('p', 'mlops-note note-warn', requirement.message));
        }
        getElement('service-dataset-note').textContent = datasets.length + ' compatible, verified dataset' + (datasets.length === 1 ? '' : 's') + ' available. The default prepares fresh data. All configured cameras are included.';
        const model = serviceModel();
        getElement('service-review-btn').disabled = !model;
        getElement('service-readiness-btn').disabled = !model || state.serviceActionBusy;
        const connectionBlocked = serviceConnectionBlockers(model).length > 0;
        getElement('service-continue-btn').disabled = !model || connectionBlocked || state.servicesState !== 'ready';
        getElement('service-deploy-btn').disabled = !model || offline || connectionBlocked || model.stage !== 'validated' || state.servicesState !== 'ready' || state.serviceActionBusy;
        getElement('service-connect-title').textContent = offline ? 'Offline experiment — no live service connection' : 'Connect the reviewed model';
        getElement('service-activation-summary').replaceChildren(kvList([
            ['Destination', toText(service?.destination?.name, choice.destination)],
            ['Candidate', model ? 'Version ' + formatMetric(model.version) + ' · ' + humanizeToken(model.stage) : 'Choose a candidate in Test & review'],
            ['Serving mode', toText(service?.serving_mode || contract?.serving_mode, 'Not reported').replaceAll('_', ' ')],
            ['Camera scope', 'All configured cameras']
        ]));
        const behavior = state.selectedService === 'behavior_anomaly_model';
        getElement('service-activation-note').textContent = offline
            ? 'This family produces offline numeric predictions. It cannot be connected to security decision services.'
            : behavior ? 'Connection selects a reviewed version for observation. Start observation explicitly below; changing live decision authority requires a separate evidence review.'
            : 'Connection selects the exact reviewed version for this service. Outputs support investigation or review; they do not automatically change live threat decisions.';
        const shadowButton = getElement('service-shadow-btn');
        shadowButton.hidden = !behavior || !service?.selected_model || service.decision_mode !== 'rules';
        shadowButton.disabled = state.serviceActionBusy || (service?.allowed_modes?.shadow?.available === false);
        getElement('service-mode-advanced').hidden = !behavior;
        renderServiceChecklist(service, model, datasetPicker.value);
        renderServiceConsumption(service, choice);
        renderServiceJob();
        renderServiceNotebook();
        if (!model) getElement('service-model-evidence').replaceChildren(el('p', null, 'Prepare and train a candidate for this service to see its evaluation here.'));
        else if (state.serviceEvidence?.id === model.id) renderServiceModelEvidence(state.serviceEvidence);
        else getElement('service-model-evidence').replaceChildren(el('p', null, 'Select Test & review to load this candidate’s current evidence.'));
    }
    function renderServiceNotebook() {
        if (!window.MLOpsDiagnostics) return;
        const selection = journeySelection(), model = serviceModel();
        const context = {model_type: state.selectedService};
        let label = 'Service walkthrough · no run selected';
        if (selection.stage !== 'prepare' && model) {
            context.model_id = model.id;
            label = 'Selected candidate · version ' + formatMetric(model.version) + ' · linked data and training evidence';
        } else {
            const task = state.recentJobs.find(job => job.job_id === selection.job && (job.details?.model_type || job.model_type) === state.selectedService);
            const dataset = state.datasets.find(item => item.id === selection.dataset && compatibleDataset(item, selectedServiceContract()));
            if (task) { context.job_id = task.job_id; label = 'Selected training run · ' + humanizeToken(task.status); }
            else if (dataset) { context.dataset_id = dataset.id; label = 'Selected dataset · ' + toText(dataset.name) + ' v' + formatMetric(dataset.version); }
        }
        const note = getElement('service-notebook-context');
        if (note) note.textContent = label;
        window.MLOpsDiagnostics.updatePanel(getElement('service-notebook-actions'), context);
    }
    function renderServiceConsumption(service, choice) {
        const usage = service?.consumption || {}, selected = service?.selected_model;
        const body = getElement('service-consumption');
        body.replaceChildren(kvList([
            ['Connected model', selected ? 'Version ' + formatMetric(selected.version) + ' · ' + humanizeToken(selected.stage) : 'No service model connected'],
            ['Last successful use', usage.last_success_at ? formatDateTime(usage.last_success_at) : 'No successful use recorded'],
            ['Model actually used', usage.model_version !== null && usage.model_version !== undefined ? String(usage.model_version) : 'Not recorded'],
            ['Last attempt', usage.last_attempt_at ? formatDateTime(usage.last_attempt_at) : 'Not recorded'],
            ['Executed mode', toText(usage.actual_mode_used, 'Not recorded')],
            ['Fallback', toText(usage.fallback_reason, 'No fallback reported')],
            ['Selected version used', usage.selected_model_used === true ? 'Yes — confirmed by a consuming request' : 'Not confirmed'],
            ['Last isolated test', usage.last_test_at ? formatDateTime(usage.last_test_at) : 'Not recorded']
        ]));
        body.appendChild(el('p', 'mlops-mode-desc', toText(usage.note, 'A connected model is verified in use only after the consuming application records a successful request.')));
        if (usage.available === false) body.appendChild(el('p', 'mlops-note note-warn', 'Usage telemetry is unavailable. Model connection alone does not confirm use.'));
        const link = getElement('service-open-consumer');
        const url = service?.destination?.url;
        link.href = typeof url === 'string' && url.startsWith('/admin/') ? url : '/admin/security-intelligence';
        link.hidden = choice.model_type === 'tabular_regression_model' || !url;
        const stop = getElement('service-stop-btn');
        stop.textContent = choice.model_type === 'behavior_anomaly_model' ? 'Stop model and return to rules' : 'Stop service model';
        stop.disabled = !service?.rollback?.available || !selected || state.servicesState !== 'ready' || state.serviceActionBusy;
    }
    function renderServiceJob() {
        const selection = journeySelection();
        const task = state.recentJobs.find(job => job.job_id === selection.job)
            || state.recentJobs.find(job => (job.details?.model_type || job.model_type) === state.selectedService && job.kind === 'training');
        const body = getElement('service-job-progress');
        if (!body) return;
        body.hidden = !task;
        if (!task) return;
        const details = task.details || {};
        body.replaceChildren(el('strong', null, 'Latest training · ' + humanizeToken(task.status)));
        body.appendChild(el('p', null, 'Step: ' + humanizeToken(details.stage || task.status) + (toFiniteNumber(task.progress_percent) !== null ? ' · ' + formatMetric(task.progress_percent) + '%' : '')));
        if (task.status === 'failed') body.appendChild(el('p', 'mlops-note note-bad', toText(task.error_message, 'Training failed. Open Advanced tools → Work in progress for details.') + ' Correct the reported cause, then use Prepare & train to retry.'));
        if (task.status === 'completed') body.appendChild(el('p', null, 'Training finished. Continue to Test & review; the service model has not been changed.'));
        if (['scheduled', 'running'].includes(task.status)) {
            const button = el('button', 'mlops-btn mlops-btn-small', task.cancel_requested ? 'Cancellation requested' : 'Cancel this run');
            button.type = 'button'; button.disabled = task.cancel_requested === true;
            button.addEventListener('click', () => cancelJob(task.job_id)); body.appendChild(button);
        }
        const modelId = task.result?.model_id || details.model_id;
        if (task.status === 'completed' && modelId && serviceModels().some(model => model.id === modelId) && selection.job === task.job_id && selection.completedJob !== task.job_id) {
            selection.model = modelId; selection.completedJob = task.job_id; persistJourney();
        }
    }
    async function loadServiceModelEvidence() {
        const model = serviceModel();
        if (!model) return;
        const req = beginRequest('service-model');
        const family = state.selectedService;
        getElement('service-model-evidence').replaceChildren(el('p', null, 'Loading this model’s evaluation…'));
        try {
            const result = await api('/api/ml/models/' + encodeURIComponent(model.id), {signal: req.signal});
            if (!req.isCurrent() || state.selectedService !== family || serviceModel()?.id !== model.id) return;
            state.serviceEvidence = result;
            renderServiceModelEvidence(result);
        } catch (err) {
            if (!err.aborted && req.isCurrent()) getElement('service-model-evidence').replaceChildren(el('p', 'mlops-note note-bad', formatActionError('Could not load candidate evidence', err)));
        }
    }
    function renderServiceModelEvidence(model) {
        const report = model.evaluation_report || {}, config = model.training_config || {};
        const engineering = report.engineering_gate?.status || config.engineering_gate?.status || config.engineering_gate;
        const scientific = report.scientific_gate?.status || config.scientific_gate?.status || config.scientific_gate;
        const body = getElement('service-model-evidence');
        body.replaceChildren(kvList([
            ['Model', 'Version ' + formatMetric(model.version) + ' · ' + friendlyAlgorithm(model.algorithm)],
            ['Registry stage', humanizeToken(model.stage)],
            ['Engineering checks', toText(engineering, 'Not recorded')],
            ['Scientific evidence', toText(scientific, 'Not recorded')],
            ['Held-out rows', formatMetric(report.splits?.test?.rows)],
            ['Feature contract', toText(model.feature_set_version, 'Not recorded')]
        ]));
        const candidateBlockers = serviceConnectionBlockers(model);
        if (Array.isArray(candidateBlockers) && candidateBlockers.length) {
            const list = el('ul', 'mlops-service-blocker-list');
            candidateBlockers.forEach(blocker => list.appendChild(el('li', null, toText(blocker.message))));
            body.appendChild(list);
        }
        body.appendChild(el('p', 'mlops-mode-desc', 'Readiness checks and sample counts are evidence, not a guarantee of analytical accuracy. The server checks current deployment gates when you connect.'));
    }
    async function startServiceTraining() {
        const contract = selectedServiceContract();
        if (!contract || !contract.trainable || state.activeJobs.size || state.serviceActionBusy || state.selectedService === 'tabular_regression_model') return;
        const family = state.selectedService, selection = journeySelection();
        if (serviceTrainingBlocked(serviceStatus(), getElement('service-dataset-select').value)) return;
        const body = {model_type: family, algorithm: contract.default_algorithm, prepare_features: true};
        const dataset = state.datasets.find(item => item.id === getElement('service-dataset-select').value);
        if (dataset && compatibleDataset(dataset, contract)) body.dataset_id = dataset.id;
        state.serviceActionBusy = true; renderServiceJourney();
        try {
            const result = await api('/api/ml/training-jobs', {method: 'POST', body});
            selection.job = result.job_id; selection.dataset = body.dataset_id || ''; persistJourney();
            if (state.selectedService === family) setNote('service-action-note', 'Preparation and training scheduled. Follow the current step below; you can leave this page and return to the saved run.', 'ok');
            await refreshJobs();
        } catch (err) { if (!err.aborted && state.selectedService === family) setNote('service-action-note', formatActionError('Could not prepare and train', err), 'bad'); }
        finally { state.serviceActionBusy = false; renderServiceJourney(); }
    }
    async function recheckServiceModel() {
        const model = serviceModel();
        if (!model || state.serviceActionBusy) return;
        state.serviceActionBusy = true; renderServiceJourney();
        try {
            await api('/api/ml/models/' + encodeURIComponent(model.id) + '/readiness', {method: 'POST', body: {}});
            await Promise.allSettled([loadServiceModelEvidence(), loadServices()]);
            setNote('service-action-note', 'Readiness recomputed. Review the current evidence and connection blockers.', 'ok');
        } catch (err) { if (!err.aborted) setNote('service-action-note', formatActionError('Could not recheck readiness', err), 'bad'); }
        finally { state.serviceActionBusy = false; renderServiceJourney(); }
    }
    function connectServiceModel() {
        const model = serviceModel(), service = serviceStatus(), family = state.selectedService;
        if (!model || !service || state.servicesState !== 'ready' || serviceConnectionBlockers(model).length) return;
        openActionPanel('Connect version ' + formatMetric(model.version) + ' to ' + toText(service.destination?.name, friendlyModelType(family)) + ' for ' + toText(service.serving_mode).replaceAll('_', ' '), async reason => {
            state.serviceActionBusy = true; renderServiceJourney();
            try {
                if (!model.artifact_hash) throw {message: 'This model has no recorded artifact checksum. Rebuild and validate it before connection.'};
                await api('/api/ml/services/' + encodeURIComponent(family) + '/deploy', {method: 'POST', body: {model_id: model.id, artifact_hash: model.artifact_hash, reason}});
                await Promise.allSettled([loadServices(), loadModels(), loadOverview()]);
                if (state.selectedService === family) {
                    setNote('service-action-note', family === 'behavior_anomaly_model' ? 'Model selected. Start observation alongside rules, then verify actual use in Monitor.' : 'Model connected. Open the consuming service, then return to Monitor to verify actual use.', 'ok');
                    setJourneyStage(family === 'behavior_anomaly_model' ? 'activate' : 'monitor', true);
                }
            } catch (err) { if (!err.aborted) setNote('service-action-note', formatActionError('Model connection refused', err), 'bad'); }
            finally { state.serviceActionBusy = false; renderServiceJourney(); }
        });
    }
    function observeServiceModel() {
        openActionPanel('Start observing the selected behavior model alongside rules', async reason => {
            try {
                await api('/api/ml/config/mode', {method: 'PUT', body: {mode: 'shadow', reason}});
                await Promise.allSettled([loadServices(), loadOverview()]);
                setNote('service-action-note', 'Observation enabled. Rules continue to decide; Monitor shows when a real assessment uses the model.', 'ok');
                setJourneyStage('monitor', true);
            } catch (err) { if (!err.aborted) setNote('service-action-note', describeModeGate(err), 'bad'); }
        });
    }
    function stopServiceModel() {
        const service = serviceStatus(), family = state.selectedService;
        if (!service?.selected_model || !service.rollback?.available) return;
        openActionPanel(family === 'behavior_anomaly_model' ? 'Stop the behavior model and return to rules' : 'Stop the selected model for ' + toText(service.destination?.name, friendlyModelType(family)), async reason => {
            try {
                await api('/api/ml/services/' + encodeURIComponent(family) + '/rollback', {method: 'POST', body: {model_id: service.selected_model.id, reason}});
                await Promise.allSettled([loadServices(), loadModels(), loadOverview()]);
                setNote('service-action-note', 'Service model stopped. The archived model remains available in the audit and model registry.', 'ok');
            } catch (err) { if (!err.aborted) setNote('service-action-note', formatActionError('Could not stop the service model', err), 'bad'); }
        });
    }
    function installServiceJourney() {
        const stored = savedJourney();
        state.journey = {service: stored.service, selections: stored.selections && typeof stored.selections === 'object' && !Array.isArray(stored.selections) ? stored.selections : {}};
        // Read only a known family. Old/stale browser state never chooses an arbitrary service.
        const family = SERVICE_CHOICES.some(item => item.model_type === stored.service) ? stored.service : 'behavior_anomaly_model';
        document.querySelectorAll('[data-journey-stage]').forEach(button => button.addEventListener('click', () => setJourneyStage(button.dataset.journeyStage, true)));
        on('mlops-service-guide', 'click', () => { setJourneyStage('prepare', true); getElement('mlops-journey-title').scrollIntoView({block: 'start'}); });
        on('service-dataset-select', 'change', () => { journeySelection().dataset = getElement('service-dataset-select').value; journeySelection().job = ''; persistJourney(); renderServiceJourney(); });
        on('service-model-select', 'change', () => { journeySelection().model = getElement('service-model-select').value; state.serviceEvidence = null; persistJourney(); renderServiceJourney(); loadServiceModelEvidence(); });
        on('service-workflow-next', 'click', () => {
            if (state.selectedService === 'tabular_regression_model') openAdvanced('prepare', 'workflows');
            else setJourneyStage(getElement('service-workflow-next').dataset.journeyStage || 'prepare', true);
        });
        on('service-train-btn', 'click', startServiceTraining);
        on('service-training-advanced', 'click', () => openAdvanced('prepare', 'workflows'));
        on('service-review-btn', 'click', () => { const model = serviceModel(); if (model) { openAdvanced('review', 'model-registry'); loadModelDetail(model.id); } });
        on('service-readiness-btn', 'click', recheckServiceModel);
        on('service-continue-btn', 'click', () => setJourneyStage('activate', true));
        on('service-deploy-btn', 'click', connectServiceModel);
        on('service-shadow-btn', 'click', observeServiceModel);
        on('service-mode-advanced', 'click', () => openAdvanced('overview', 'mode-cards'));
        on('service-monitor-refresh', 'click', loadServices);
        on('service-stop-btn', 'click', stopServiceModel);
        selectService(family, false);
    }

    let workflow = null, platform = null;

    function replaceList(id, items) {
        const node = getElement(id);
        if (!node) return;
        const frag = document.createDocumentFragment();
        items.forEach(function (item) { frag.appendChild(el('li', null, item)); });
        node.replaceChildren(frag);
    }

    function workspaceStatus(workspace) {
        const workerStatus = toText(state.mlWorker && state.mlWorker.status, 'unknown').toLowerCase();
        if (['overview', 'prepare'].includes(workspace)) {
            if (state.evidence.overview === 'error' || state.jobPollFailures) return { text: 'Status unavailable — refresh', tone: 'bad' };
            if (state.evidence.overview !== 'ready' || !state.mlWorker) return { text: 'Checking readiness', tone: 'warn' };
        }
        if (workspace === 'overview') {
            if (state.consoleStatus === 'offline' || (state.mlWorker && workerStatus !== 'healthy')) {
                return { text: 'Needs attention', tone: 'bad' };
            }
            if (state.consoleStatus !== 'online') return { text: 'Checking readiness', tone: 'warn' };
            return { text: 'Ready', tone: 'ok' };
        }
        if (workspace === 'prepare') {
            if (state.mlWorker && workerStatus !== 'healthy') return { text: 'Blocked — worker unavailable', tone: 'bad' };
            if (state.activeJobs.size) return { text: 'Running — ' + state.activeJobs.size + ' active', tone: 'info' };
            return { text: 'Ready to prepare', tone: 'ok' };
        }
        if (workspace === 'review') {
            const candidates = serviceModels().filter(function (model) { return model.stage === 'validated'; }).length;
            if (candidates) return { text: candidates + (candidates === 1 ? ' candidate to review' : ' candidates to review'), tone: 'warn' };
            if (serviceModels().some(function (model) { return model.stage === 'shadow'; })) return { text: 'Shadow model active', tone: 'info' };
            return { text: 'Waiting for a candidate', tone: 'warn' };
        }
        if (workspace === 'monitor') {
            if ((state.selectedService === 'behavior_anomaly_model' && state.currentMode === 'shadow') || serviceModels().some(function (model) { return model.stage === 'shadow'; })) {
                return { text: 'Shadow observation active', tone: 'info' };
            }
            return { text: 'Waiting for shadow approval', tone: 'warn' };
        }
        return { text: 'Available for review', tone: 'ok' };
    }

    function updateRunbook(workspace) {
        const copy = WORKSPACES[workspace];
        const index = WORKSPACE_ORDER.indexOf(workspace);
        const position = getElement('mlops-runbook-position');
        const purpose = getElement('mlops-runbook-purpose');
        const status = getElement('mlops-runbook-status');
        if (position) position.textContent = 'Step ' + (index + 1) + ' of ' + WORKSPACE_ORDER.length;
        if (purpose) purpose.textContent = copy.purpose;
        replaceList('mlops-runbook-run', copy.run);
        replaceList('mlops-runbook-verify', copy.verify);
        replaceList('mlops-runbook-recover', copy.recover);
        if (status) {
            const current = workspaceStatus(workspace);
            status.textContent = current.text;
            status.className = 'mlops-chip chip-' + current.tone;
        }
        const previous = getElement('mlops-runbook-previous');
        const next = getElement('mlops-runbook-next');
        const primary = getElement('mlops-runbook-primary');
        if (previous) {
            previous.disabled = index === 0;
            previous.dataset.targetWorkspace = WORKSPACE_ORDER[index - 1] || '';
        }
        if (next) {
            next.disabled = index === WORKSPACE_ORDER.length - 1;
            next.dataset.targetWorkspace = WORKSPACE_ORDER[index + 1] || '';
        }
        if (primary) {
            primary.dataset.targetElement = copy.primaryTarget;
            primary.lastChild.textContent = ' ' + copy.primaryLabel;
        }
    }

    function activateWorkspace(name, shouldScroll) {
        const workspace = WORKSPACES[name] ? name : 'overview';
        const copy = WORKSPACES[workspace];
        state.activeWorkspace = workspace;

        document.querySelectorAll('[data-mlops-panel]').forEach(function (panel) {
            panel.hidden = panel.dataset.mlopsPanel !== workspace;
        });
        document.querySelectorAll('[data-mlops-view]').forEach(function (button) {
            const active = button.dataset.mlopsView === workspace;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
        document.querySelectorAll('.mlops-lifecycle [data-open-mlops-view]').forEach(function (button) {
            const active = button.dataset.openMlopsView === workspace;
            button.classList.toggle('is-active', active);
            if (active) button.setAttribute('aria-current', 'step');
            else button.removeAttribute('aria-current');
        });

        const kicker = getElement('mlops-workspace-kicker');
        const title = getElement('mlops-workspace-title');
        const description = getElement('mlops-workspace-description');
        const count = getElement('mlops-workspace-count');
        if (kicker) kicker.textContent = copy.kicker;
        if (title) title.textContent = copy.title;
        if (description) description.textContent = copy.description;
        if (count) {
            const total = document.querySelectorAll('[data-mlops-panel="' + workspace + '"]').length;
            count.textContent = total + (total === 1 ? ' section' : ' sections');
        }
        updateRunbook(workspace);

        if (shouldScroll) {
            const advanced = getElement('mlops-advanced-tools');
            if (advanced) advanced.open = true;
            const heading = document.querySelector('.mlops-workspace-heading');
            if (heading) heading.scrollIntoView({ block: 'start' });
        }
    }

    function installWorkspaceNavigation() {
        document.querySelectorAll('[data-mlops-view], [data-open-mlops-view]').forEach(function (control) {
            control.addEventListener('click', function () {
                if (control.id === 'mlops-next-step-action' && control.dataset.journeyStage) {
                    setJourneyStage(control.dataset.journeyStage, true);
                    if (control.dataset.refreshServices === 'true') refreshConsole();
                    return;
                }
                const workspace = control.dataset.mlopsView || control.dataset.openMlopsView;
                activateWorkspace(workspace, true);
                const target = getElement(control.dataset.nextTarget);
                if (target) { target.scrollIntoView({ block: 'center' }); target.focus(); }
            });
        });

        ['mlops-runbook-previous', 'mlops-runbook-next'].forEach(function (id) {
            const button = getElement(id);
            if (!button) return;
            button.addEventListener('click', function () {
                if (button.dataset.targetWorkspace) activateWorkspace(button.dataset.targetWorkspace, true);
            });
        });
        const primary = getElement('mlops-runbook-primary');
        if (primary) {
            primary.addEventListener('click', function () {
                const target = getElement(primary.dataset.targetElement);
                if (!target) return;
                target.scrollIntoView({ block: 'start' });
                const focusTarget = target.matches('button, input, select, a')
                    ? target : target.querySelector('button, input, select, a');
                if (focusTarget) focusTarget.focus();
            });
        }

        const hashWorkspace = {
            '#operations-queue': 'overview',
            '#system-governance': 'overview',
            '#workflows': 'prepare',
            '#model-registry': 'review',
            '#observability': 'monitor',
            '#audit-trail': 'audit'
        }[window.location.hash];
        activateWorkspace(hashWorkspace || 'overview', false);
        if (window.location.hash && window.history && window.history.replaceState) {
            window.history.replaceState(null, '', window.location.pathname + window.location.search);
        }
    }

    function updateNextStep() {
        const title = getElement('mlops-next-step-title'), description = getElement('mlops-next-step-description'), action = getElement('mlops-next-step-action');
        if (!title || !description || !action) return;
        const models = serviceModels(), contract = selectedServiceContract(), service = serviceStatus();
        const datasets = state.datasets.filter(dataset => compatibleDataset(dataset, contract));
        let next = {title: 'Prepare this service and train a candidate', description: 'Use the recommended settings. One job prepares the matching data, trains a candidate, and records its evaluation.', stage: 'prepare', action: 'Prepare & train'};
        if (state.servicesState === 'error' || state.evidence.overview === 'error' || state.jobPollFailures || (state.mlWorker && state.mlWorker.status !== 'healthy')) {
            next = {title: 'Check service health before starting work', description: 'Readiness could not be confirmed. Refresh status and resolve the reported service or worker problem.', stage: 'prepare', action: 'Refresh status', refresh: true};
        } else if (state.evidence.overview !== 'ready' || !state.mlWorker || state.servicesState !== 'ready') {
            next = {title: 'Checking this service…', description: 'Waiting for its contract, worker and current deployment state.', stage: 'prepare', action: 'Checking status', disabled: true};
        } else if (state.activeJobs.size) {
            next = {title: state.activeJobs.size + ' job(s) in progress', description: 'The guided run below shows this service’s progress. Existing worker jobs finish before another training run starts.', stage: 'prepare', action: 'Follow progress'};
        } else if (state.evidence.models === 'error' || state.evidence.datasets === 'error') {
            next = {title: 'Refresh missing evidence', description: 'A missing response does not mean there are no saved records for this service.', stage: 'test', action: 'Refresh evidence', refresh: true};
        } else if (state.evidence.models !== 'ready' || state.evidence.datasets !== 'ready') {
            next = {title: 'Loading this service’s saved work…', description: 'Checking compatible datasets and model versions.', stage: 'prepare', action: 'Loading evidence', disabled: true};
        } else if (service?.selected_model) {
            next = state.selectedService === 'behavior_anomaly_model' && service.decision_mode === 'rules'
                ? {title: 'Start observing the connected model', description: 'A version is selected. Explicitly start observation alongside rules, then verify actual usage.', stage: 'activate', action: 'Start observation'}
                : {title: 'Verify this service uses its connected model', description: 'Check the version actually used, last successful request and fallback reason.', stage: 'monitor', action: 'Monitor service'};
        } else if (models.some(model => ['validated', 'shadow', 'approved'].includes(model.stage))) {
            next = {title: 'Review this service’s candidate', description: 'Inspect held-out results and current evidence gates before connecting the selected version.', stage: 'test', action: 'Test & review'};
        } else if (service?.training_readiness?.blockers?.some(item => !datasets.length || item.code !== 'SOURCE_HISTORY_REQUIRED')) {
            next = {title: 'Complete this service’s data requirements', description: service.training_readiness.blockers.map(item => item.message).join(' '), stage: 'prepare', action: 'Inspect requirements'};
        } else if (datasets.length) {
            next = {title: 'Train on a compatible dataset', description: 'Reuse a verified dataset for this service, or prepare a fresh version from available history.', stage: 'prepare', action: 'Prepare & train'};
        }
        if (state.selectedService === 'tabular_regression_model' && state.servicesState === 'ready') next = {title: 'Configure an offline numeric experiment', description: 'Choose an explicit numeric target and saved pipeline in Advanced training options. This family has no live service connection.', stage: 'prepare', action: 'Set up experiment'};
        title.textContent = next.title; description.textContent = next.description; action.textContent = next.action;
        action.dataset.journeyStage = next.stage; action.dataset.refreshServices = String(Boolean(next.refresh));
        action.disabled = Boolean(next.disabled);
        updateRunbook(state.activeWorkspace);
    }

    function stopJobPolling() {
        if (state.jobPollTimer) {
            window.clearTimeout(state.jobPollTimer);
            state.jobPollTimer = null;
        }
    }

    function hasActiveJob(kind) {
        for (const job of state.activeJobs.values()) {
            if (!kind || job.kind === kind) return true;
        }
        return false;
    }

    // ============================================
    // Shared accessible confirmation dialog (replaces browser dialogs)
    // ============================================

    function openActionPanel(title, execute) {
        state.pendingAction = { title: title, execute: execute };
        const panel = getElement('registry-action-panel');
        const titleNode = getElement('registry-action-title');
        const reasonInput = getElement('registry-action-reason');
        if (!panel || !titleNode || !reasonInput) return;
        titleNode.textContent = title;
        reasonInput.value = '';
        setNote('registry-action-note', '', null);
        panel.hidden = false;
        if (window.ModalStack) {
            window.ModalStack.open(panel, {
                backdropClose: true,
                onClose: function () {
                    panel.hidden = true;
                    state.pendingAction = null;
                }
            });
        } else {
            panel.style.display = 'flex';
            reasonInput.focus();
        }
    }

    function closeActionPanel() {
        const panel = getElement('registry-action-panel');
        if (!panel) return;
        if (window.ModalStack && window.ModalStack.isOpen(panel)) {
            window.ModalStack.close(panel);
        } else {
            panel.style.display = 'none';
            panel.hidden = true;
            state.pendingAction = null;
        }
    }

    async function confirmPendingAction() {
        const pending = state.pendingAction;
        const reasonInput = getElement('registry-action-reason');
        if (!pending || !reasonInput) return;
        const reason = reasonInput.value.trim();
        if (reason.length < 3) {
            setNote('registry-action-note', 'Enter a reason of at least 3 characters so this action can be audited.', 'bad');
            reasonInput.focus();
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
            state.evidence.overview = 'ready';
            state.featureSnapshots = toFiniteNumber(data && data.data_readiness && data.data_readiness.feature_snapshots);
            renderSystemState(data && data.system);
            renderModePanel(data && data.mode);
            renderLabelReadiness(data && data.label_readiness);
            renderDataReadiness(data && data.data_readiness);
            renderOptionalCapabilities(data && data.optional_capabilities);
            renderModelTypeContract(data && data.model_types);
            setConsoleConnection('online', 'Control plane connected');
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            state.evidence.overview = 'error';
            renderCardError('mode-cards', err);
            renderCardError('label-readiness-body', err);
            renderCardError('data-readiness-body', err);
            renderCardError('optional-capabilities-body', err);
            setConsoleConnection('offline', 'Control plane unavailable');
        }
    }

    function renderCardError(id, err) {
        const body = getElement(id);
        if (!body) return;
        const box = el('div', 'mlops-error-state');
        box.setAttribute('role', 'alert');
        const heading = el('strong', null, 'Could not load this section');
        heading.prepend(faIcon('fas fa-circle-exclamation'));
        box.appendChild(heading);
        box.appendChild(el('p', null, toText(err && err.message, 'Unknown error.')));
        box.appendChild(el('p', 'mlops-error-recovery', 'Next step: ' + recoveryForError(err)));
        if (err && (err.code || err.requestId)) {
            const technical = [];
            if (err.code) technical.push('Error code: ' + err.code);
            if (err.requestId) technical.push('Request ID: ' + err.requestId);
            box.appendChild(el('small', null, technical.join(' · ')));
        }
        body.replaceChildren(box);
    }

    function renderModePanel(availability) {
        const badge = getElement('current-mode-badge');
        const cards = getElement('mode-cards');
        if (!cards) return;
        const modes = (availability && availability.modes) || {};
        state.currentMode = toText(availability && availability.current_mode, 'rules');
        if (badge) badge.textContent = state.currentMode.toUpperCase();
        setSummaryValue('summary-mode', state.currentMode.toUpperCase(),
            state.currentMode === 'rules' ? 'healthy' : 'warning',
            'Configured decision mode');
        updateNextStep();

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
            setNote('mode-action-note', describeModeGate(err), 'bad');
        }
    }

    // The gate refusal names EVERY gate with its state (✓ satisfied /
    // ✗ unmet), then the unmet reasons - the reader sees what is ready
    // as well as what blocks. Rules stay authoritative either way.
    function describeModeGate(err) {
        const detail = err && err.detailExtra && typeof err.detailExtra === 'object' ? err.detailExtra : {};
        const lines = [];
        if (err && err.code === 'MODE_GATED') {
            lines.push('ML decision authority is not ready.');
        } else {
            lines.push(toText(err && err.message, 'mode change failed'));
        }
        const gates = Array.isArray(detail.gates) ? detail.gates : [];
        gates.forEach(function (g) {
            if (!g || typeof g !== 'object') return;
            const mark = g.ok ? '✓' : (g.required === false ? '–' : '✗');
            lines.push(mark + ' ' + toText(g.label || g.gate) + ': ' + toText(g.status)
                + (g.required === false ? ' (not required for this mode)' : ''));
        });
        const unmet = Array.isArray(detail.unmet_gates) ? detail.unmet_gates : [];
        if (unmet.length) {
            lines.push('Unmet:');
            unmet.forEach(function (u) { lines.push('• ' + toText(u)); });
        }
        if (detail.note) lines.push(toText(detail.note));
        else if (err && err.code === 'MODE_GATED') lines.push('Rules remain authoritative.');
        return lines.join('\n');
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
                setNote('mode-action-note', formatActionError('Could not pause ML', err), 'bad');
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

    // The backend contract owns availability, algorithms, dataset kind and
    // score semantics. Both selectors are rebuilt from it.
    function renderModelTypeContract(types) {
        if (!Array.isArray(types) || !types.length) return;
        state.modelTypes = types;
        ['training-model-type', 'policy-model-type'].forEach(function (id) {
            const select = getElement(id);
            if (!select) return;
            const previous = state.selectedService || select.value;
            const frag = document.createDocumentFragment();
            types.forEach(function (t) {
                if (!t || typeof t !== 'object') return;
                const option = document.createElement('option');
                option.value = toText(t.model_type);
                const trainable = t.trainable === true;
                option.textContent = friendlyModelType(t.model_type) + (trainable
                    ? ' — Available'
                    : ' — Reserved / Future · not trainable');
                option.title = 'Internal type: ' + toText(t.model_type);
                option.dataset.trainable = trainable ? 'true' : 'false';
                if (id === 'training-model-type' && !trainable) option.disabled = true;
                frag.appendChild(option);
            });
            select.replaceChildren(frag);
            const keep = [...select.options].find(function (o) { return o.value === previous && !o.disabled; });
            select.value = keep ? previous : (types.find(function (t) { return t.trainable; }) || {}).model_type || '';
        });
        selectService(state.selectedService, false);
    }

    function selectedModelTypeContract() {
        const select = getElement('training-model-type');
        const value = select ? select.value : 'behavior_anomaly_model';
        return (state.modelTypes || []).find(function (t) { return t.model_type === value; }) || null;
    }

    function updateTrainingAvailability() {
        const contract = selectedModelTypeContract();
        const startBtn = getElement('start-training-btn');
        const note = getElement('model-type-note');
        const trainable = !contract || contract.trainable === true;
        const algorithm = getElement('training-algorithm');
        if (algorithm && contract && algorithm.dataset.modelType !== contract.model_type) {
            const options = (contract.algorithms || []).map(function (name) {
                const option = el('option', null, friendlyAlgorithm(name));
                option.value = toText(name);
                if (name === contract.default_algorithm) option.selected = true;
                return option;
            });
            algorithm.replaceChildren.apply(algorithm, options);
            algorithm.dataset.modelType = contract.model_type;
        }
        if (startBtn) startBtn.disabled = !trainable || hasActiveJob('training');
        if (note) {
            note.textContent = contract
                ? (trainable ? 'Status: Available · ' + toText(contract.entity_type)
                    + ' · ' + toText(contract.score_type) + ' — ' + toText(contract.note)
                             : 'Status: Reserved / Future — Not trainable. ' + toText(contract.note))
                : '';
            note.classList.toggle('note-bad', !trainable);
        }
    }

    function renderOptionalCapabilities(caps) {
        const body = getElement('optional-capabilities-body');
        if (!body) return;
        if (!caps) { renderCardError('optional-capabilities-body', { message: 'No capabilities reported. Refresh the console.' }); return; }
        body.replaceChildren(simpleTable(['Capability', 'Status', 'Next action'], Object.entries(caps).map(function ([name, item]) {
            return [name, item.status || (item.operational ? 'Available' : !item.configured ? 'Disabled' : !item.dependency_available ? 'Unavailable' : 'Misconfigured'), item.action || 'Check Admin Settings.'];
        })));
        const link = el('a', 'mlops-btn', 'Manage integrations and limits in Admin Settings'); link.href = '/admin/settings'; body.append(link);
    }

    // ============================================
    // Model registry
    // ============================================

    function renderEvidenceModelFilter(items) {
        const select = getElement('shadow-model-select');
        if (!select) return;
        const previous = select.value;
        const frag = document.createDocumentFragment();
        const all = el('option', null, 'All models');
        all.value = '';
        frag.appendChild(all);
        items.forEach(function (m) {
            if (!m || !m.id) return;
            const option = el('option', null, friendlyModelType(m.model_type) + ' v' + formatMetric(m.version) + ' · ' + humanizeToken(m.stage));
            option.value = String(m.id);
            option.title = 'Internal type: ' + toText(m.model_type);
            frag.appendChild(option);
        });
        select.replaceChildren(frag);
        if ([...select.options].some(function (o) { return o.value === previous; })) select.value = previous;
    }

    async function loadModels() {
        const family = state.selectedService;
        const req = beginRequest('models');
        const tbody = getElement('models-table-body');
        if (!tbody) return;
        try {
            const data = await api('/api/ml/models', {
                params: { page: 1, page_size: 100, model_type: family }, signal: req.signal
            });
            if (!req.isCurrent()) return;
            const items = (data && Array.isArray(data.items)) ? data.items : [];
            state.models = items;
            state.modelsFamily = family;
            state.evidence.models = 'ready';
            renderServiceJourney();
            if (['test', 'activate'].includes(journeySelection().stage)) loadServiceModelEvidence();
            updateNextStep();
            if (workflow) workflow.sync('models');
            renderEvidenceModelFilter(items);
            const frag = document.createDocumentFragment();
            if (!items.length) {
                const row = el('tr');
                const cell = el('td');
                cell.colSpan = 9;
                const empty = el('div', 'mlops-empty-state');
                empty.appendChild(el('div', null, 'No models registered yet. Train a candidate to begin review.'));
                const openPrepare = el('button', 'mlops-btn mlops-btn-primary', 'Open Prepare & train');
                openPrepare.type = 'button';
                openPrepare.addEventListener('click', function () { activateWorkspace('prepare', true); });
                empty.appendChild(openPrepare);
                cell.appendChild(empty);
                row.appendChild(cell);
                frag.appendChild(row);
            }
            for (const model of items) frag.appendChild(modelRow(model));
            tbody.replaceChildren(frag);
            updateRunbook(state.activeWorkspace);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            const row = el('tr');
            const cell = el('td', 'mlops-note note-bad', formatActionError('Could not load models', err));
            state.evidence.models = 'error';
            renderServiceJourney();
            updateNextStep();
            if (workflow) workflow.loadError('models', err);
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
        const typeCell = el('td', null, friendlyModelType(model.model_type));
        typeCell.title = 'Internal type: ' + toText(model.model_type);
        row.appendChild(typeCell);
        row.appendChild(el('td', null, formatMetric(model.version)));
        const stageCell = el('td');
        stageCell.appendChild(chip(humanizeToken(model.stage), stageTone(model.stage)));
        row.appendChild(stageCell);
        row.appendChild(el('td', null, friendlyAlgorithm(model.algorithm)));
        row.appendChild(el('td', null, humanizeToken(model.score_type)));
        row.appendChild(el('td', null, toBoolean(model.is_probability) ? 'Yes' : 'No'));
        row.appendChild(el('td', null, humanizeToken(model.calibration_status)));
        row.appendChild(el('td', null, formatDateTime(model.created_at)));

        const actions = el('td');
        const detailBtn = el('button', 'mlops-btn', 'Detail');
        detailBtn.type = 'button';
        detailBtn.addEventListener('click', function () { loadModelDetail(model.id); });
        actions.appendChild(detailBtn);

        const contract = (state.modelTypes || []).find(function (item) {
            return item.model_type === model.model_type;
        });
        if (model.stage === 'validated' && (!contract || contract.serving_mode === 'shadow'
                || contract.serving_mode === 'on_demand_shadow')) {
            const approveBtn = el('button', 'mlops-btn mlops-btn-primary', 'Approve for SHADOW (observation only)');
            approveBtn.title = 'Shadow = the model runs in parallel and is recorded; it gets NO decision authority. Rules stay authoritative.';
            approveBtn.type = 'button';
            approveBtn.addEventListener('click', function () {
                openActionPanel(
                    'Approve ' + friendlyModelType(model.model_type) + ' v' + formatMetric(model.version)
                    + ' into SHADOW (rules stay live; the approval record persists on the model row)',
                    function (reason) { return approveShadow(model.id, reason); });
            });
            actions.appendChild(approveBtn);
        } else if (model.stage === 'validated' && contract && contract.serving_mode === 'offline_ranking') {
            actions.appendChild(chip('Offline ranking only', 'info'));
        }
        if (model.stage === 'validated' || model.stage === 'shadow') {
            const rejectBtn = el('button', 'mlops-btn mlops-btn-danger', 'Reject');
            rejectBtn.type = 'button';
            rejectBtn.addEventListener('click', function () {
                openActionPanel(
                    'Reject ' + friendlyModelType(model.model_type) + ' v' + formatMetric(model.version),
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
            setNote('registry-note', 'Model approved into Shadow observation. Rules remain the live decision system. Continue to Monitor.', 'ok');
            loadModels();
            loadOverview();
        } catch (err) {
            if (err.aborted) return;
            setNote('registry-note', formatActionError('Shadow approval failed', err), 'bad');
        }
    }

    async function rejectModel(modelId, reason) {
        try {
            await api('/api/ml/models/' + encodeURIComponent(modelId) + '/reject', {
                method: 'POST', body: { reason: reason }
            });
            setNote('registry-note', 'Model rejected and the reason was recorded in Audit.', 'ok');
            loadModels();
            loadOverview();
        } catch (err) {
            if (err.aborted) return;
            setNote('registry-note', formatActionError('Model rejection failed', err), 'bad');
        }
    }

    function stopShadow() {
        openActionPanel('Stop shadow (rollback drill) — archives the shadow model; live decisions were never affected',
            async function (reason) {
                try {
                    const result = await api('/api/ml/shadow/stop', {
                        method: 'POST', body: { reason: reason }
                    });
                    setNote('registry-note', toText(result && result.note, 'Shadow stopped. Rules remain live.'), 'ok');
                    loadModels();
                    loadOverview();
                    loadShadowSummary();
                } catch (err) {
                    if (err.aborted) return;
                    setNote('registry-note', formatActionError('Could not stop shadow', err), 'bad');
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
            if (window.MLOpsDiagnostics) frag.appendChild(window.MLOpsDiagnostics.actions({model_id: String(model.id || modelId)}));
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
                    ['Scientific gate', toText(sg.status, 'NOT_RECORDED') + ' — ' + (Array.isArray(sg.reasons) ? sg.reasons.map(function (r) { return toText(r.code); }).join(', ') : '') + ' — ' + toText(sg.meaning)
                        + (sg.computation ? ' (' + toText(sg.computation) + ' computation' + (sg.computed_at ? ' at ' + formatDateTime(sg.computed_at) : '') + ')' : '')]
                ]));
                if (Array.isArray(sg.criteria) && sg.criteria.length) {
                    frag.appendChild(el('div', 'mlops-subheading', 'Scientific criteria — measured / required / status'));
                    frag.appendChild(simpleTable(['Criterion', 'Measured', 'Required', 'Status'], sg.criteria.map(function (c) {
                        const measured = (c.measured && typeof c.measured === 'object')
                            ? Object.keys(c.measured).map(function (k) { return k + '=' + formatMetric(c.measured[k]); }).join(', ')
                            : (typeof c.measured === 'number' ? formatMetric(c.measured, 2) : toText(c.measured, 'N/A'));
                        return [toText(c.criterion) + (c.setting ? ' (' + c.setting + ')' : ''), measured,
                                c.required === null || c.required === undefined ? 'not configured' : String(c.required),
                                toText(c.status)];
                    })));
                    frag.appendChild(el('div', 'mlops-mode-desc', 'NOT_CONFIGURED = no minimum is set for this criterion (ML_SCIENTIFIC_* / ML_EVIDENCE_* settings). No default is assumed; the gate stays INSUFFICIENT_EVIDENCE until minimums are configured from reviewed policy and met.'));
                }
                if (Array.isArray(eg.failed) || (eg.checks && typeof eg.checks === 'object')) {
                    const checks = eg.checks && typeof eg.checks === 'object' ? eg.checks : {};
                    const names = Object.keys(checks);
                    if (names.length) {
                        frag.appendChild(el('div', 'mlops-subheading', 'Engineering checks'));
                        frag.appendChild(simpleTable(['Check', 'Actual', 'Required', 'Status'], names.map(function (n) {
                            const c = checks[n] || {};
                            const fmt = function (v) { return (v && typeof v === 'object') ? JSON.stringify(v) : toText(v, 'N/A'); };
                            return [n, fmt(c.actual), fmt(c.required), c.passed ? 'PASS' : 'FAIL'];
                        })));
                    }
                }
                const recompute = el('button', 'mlops-btn', 'Recompute readiness (no retraining)');
                recompute.type = 'button';
                recompute.title = 'Re-evaluates both gates from the registered artifact, dataset, current reviewed evidence and mapping status; recorded on the model as computed_post_hoc. Nothing here marks a model validated.';
                recompute.addEventListener('click', function () { recomputeReadiness(model.id); });
                frag.appendChild(recompute);
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
            setNote('drift-action-note', 'Scheduling drift analysis…', null);
            const result = await api('/api/ml/drift/run', { method: 'POST' });
            renderJobStatus(result, 'Drift check scheduled');
            setNote('drift-action-note', 'Drift check scheduled. Open Overview to follow progress; this report cannot deploy or retrain a model.', 'ok');
            await refreshJobs();
        } catch (err) {
            if (!err.aborted) {
                setNote('drift-action-note', formatActionError('Drift check failed', err), 'bad');
            }
        } finally {
            syncJobControls();
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

    const JOB_PRESENTATION = {
        'training': { label: 'Model training', icon: 'fas fa-brain' },
        'collection': { label: 'Feature collection', icon: 'fas fa-database' },
        'dataset': { label: 'Dataset build', icon: 'fas fa-table' },
        'backfill': { label: 'Dataset verification', icon: 'fas fa-fingerprint' },
        'drift': { label: 'Drift analysis', icon: 'fas fa-chart-line' }
    };

    function renderJobs(items, worker) {
        const body = getElement('training-status-body');
        if (!body) return;
        const frag = document.createDocumentFragment();
        const workerStatus = toText(worker && worker.status, 'offline');
        const workerState = toText(worker && worker.worker_state, 'unknown');
        const heartbeatAt = worker && worker.heartbeat_at;
        const activeCount = items.filter(function (task) {
            return task.status === 'scheduled' || task.status === 'running';
        }).length;
        const failedCount = items.filter(function (task) { return task.status === 'failed'; }).length;

        state.lastJobsSyncAt = new Date();
        setSummaryValue('summary-worker',
            workerStatus === 'healthy' ? ('Healthy · ' + workerState) : workerStatus,
            workerStatus === 'healthy' ? 'healthy' : 'danger',
            heartbeatAt ? 'Last heartbeat ' + formatDateTime(heartbeatAt) : 'No worker heartbeat');
        setSummaryValue('summary-active-jobs', String(activeCount), activeCount ? 'warning' : 'healthy',
            failedCount + ' failed job(s) in the latest ' + items.length + ' records');
        setSummaryValue('summary-last-update', 'Just now', 'healthy', state.lastJobsSyncAt.toLocaleString());
        setConsoleConnection('online', workerStatus === 'healthy'
            ? 'Control plane connected' : 'Connected · worker ' + workerStatus);

        const summary = el('div', 'mlops-job-summary');
        const summaryCopy = el('div', 'mlops-job-summary-copy',
            activeCount + ' active · ' + failedCount + ' failed · ' + items.length + ' recent');
        summary.appendChild(summaryCopy);
        const executor = el('div');
        executor.appendChild(chip('worker ' + workerStatus,
            workerStatus === 'healthy' ? 'ok' : 'bad'));
        if (heartbeatAt) {
            const heartbeat = el('span', 'mlops-job-summary-copy',
                ' · heartbeat ' + formatRelativeTime(heartbeatAt));
            heartbeat.title = formatDateTime(heartbeatAt);
            executor.appendChild(heartbeat);
        }
        fillTrainingDatasetPicker(state.datasets || []);
        summary.appendChild(executor);
        frag.appendChild(summary);

        if (workerStatus !== 'healthy') {
            frag.appendChild(el('div', 'mlops-note note-bad',
                'Executor is ' + workerStatus + '. Commands remain durable and will resume when the worker recovers.'));
        }
        if (!items.length) {
            const empty = el('div', 'mlops-empty-state');
            const inner = el('div');
            inner.appendChild(faIcon('fas fa-inbox'));
            inner.appendChild(el('div', null, 'No ML jobs yet. Prepare data or train a candidate to create the first durable run.'));
            const start = el('button', 'mlops-btn mlops-btn-primary', 'Open Prepare & train');
            start.type = 'button';
            start.addEventListener('click', function () { activateWorkspace('prepare', true); });
            inner.appendChild(start);
            empty.appendChild(inner);
            frag.appendChild(empty);
            body.replaceChildren(frag);
            return;
        }

        const priority = { running: 0, scheduled: 1, failed: 2, cancelled: 3, completed: 4 };
        const ordered = items.slice().sort(function (left, right) {
            const stateOrder = (priority[left.status] ?? 9) - (priority[right.status] ?? 9);
            if (stateOrder !== 0) return stateOrder;
            return new Date(right.updated_at ?? right.created_at ?? 0)
                - new Date(left.updated_at ?? left.created_at ?? 0);
        });
        const list = el('div', 'mlops-job-list');
        ordered.slice(0, 10).forEach(function (task) {
            const status = toText(task.status, 'unknown');
            const kind = toText(task.kind, 'job');
            const presentation = JOB_PRESENTATION[kind] || { label: 'ML operation', icon: 'fas fa-cog' };
            const details = task && typeof task.details === 'object' && task.details ? task.details : {};
            const reportedPercent = toFiniteNumber(task.progress_percent);
            const percent = reportedPercent === null ? (status === 'completed' ? 100 : 0)
                : Math.max(0, Math.min(100, reportedPercent));
            const rowTone = (status === 'failed' || status === 'cancelled') ? 'failed' : status;
            const row = el('div', 'mlops-job-row is-' + rowTone);

            const identity = el('div', 'mlops-job-identity');
            const title = el('div', 'mlops-job-title');
            title.appendChild(faIcon(presentation.icon));
            title.appendChild(el('span', null, presentation.label));
            title.appendChild(chip(humanizeToken(status),
                status === 'completed' ? 'ok'
                    : ((status === 'failed' || status === 'cancelled') ? 'bad' : 'warn')));
            identity.appendChild(title);
            const id = el('div', 'mlops-job-id', toText(task.job_id));
            id.title = toText(task.job_id);
            identity.appendChild(id);
            row.appendChild(identity);

            const stage = el('div', 'mlops-job-stage');
            stage.appendChild(el('strong', null, toText(details.stage, status).replaceAll('_', ' ')));
            stage.appendChild(el('div', 'mlops-job-meta', task.cancel_requested
                ? 'Cancellation requested' : toText(task.description, kind)));
            row.appendChild(stage);

            const progress = el('div');
            const progressLabel = el('div', 'mlops-job-progress-label');
            progressLabel.appendChild(el('span', null, 'Progress'));
            progressLabel.appendChild(el('span', null, percent + '%'));
            progress.appendChild(progressLabel);
            const bar = el('div', 'mlops-progress');
            bar.setAttribute('role', 'progressbar');
            bar.setAttribute('aria-label', presentation.label + ' progress');
            bar.setAttribute('aria-valuemin', '0');
            bar.setAttribute('aria-valuemax', '100');
            bar.setAttribute('aria-valuenow', String(percent));
            const fill = el('span');
            fill.style.width = percent + '%';
            bar.appendChild(fill);
            progress.appendChild(bar);
            row.appendChild(progress);

            const time = el('div', 'mlops-job-time');
            const updatedAt = task.updated_at || task.created_at;
            time.appendChild(el('strong', null, formatRelativeTime(updatedAt)));
            const exact = el('span', null, formatDateTime(updatedAt));
            time.appendChild(exact);
            if (status === 'scheduled' || status === 'running') {
                const cancel = el('button', 'mlops-btn mlops-btn-small',
                    task.cancel_requested ? 'Cancellation requested' : 'Cancel job');
                cancel.type = 'button';
                cancel.dataset.cancelJobId = toText(task.job_id, '');
                cancel.disabled = task.cancel_requested === true;
                time.appendChild(cancel);
            }
            row.appendChild(time);

            if (task.error_code || task.error_message) {
                row.appendChild(el('div', 'mlops-job-error',
                    toText(task.error_code, 'ERROR') + ' · ' + toText(task.error_message, 'Job failed')
                    + ' Next step: review this job’s details and request ID, correct the cause, then rerun it.'));
            }
            if (window.MLOpsDiagnostics && (['training', 'dataset', 'collection'].includes(task.kind) || ['ml_training', 'ml_dataset_build', 'ml_feature_computation'].includes(task.task_type))) {
                row.appendChild(window.MLOpsDiagnostics.render(task));
            }
            list.appendChild(row);
        });
        frag.appendChild(list);
        body.replaceChildren(frag);
    }

    function syncJobControls() {
        updateTrainingAvailability();
        const featureBtn = getElement('compute-features-btn');
        const datasetBtn = getElement('build-dataset-btn');
        const driftBtn = getElement('run-drift-btn');
        if (featureBtn) featureBtn.disabled = hasActiveJob('collection');
        if (datasetBtn) datasetBtn.disabled = hasActiveJob('dataset');
        if (driftBtn) driftBtn.disabled = hasActiveJob('drift');
        const backfillBtn = getElement('backfill-dataset-hashes-btn');
        if (backfillBtn) backfillBtn.disabled = hasActiveJob('backfill');
        const legacyCancel = getElement('cancel-training-btn');
        if (legacyCancel) legacyCancel.hidden = true;
    }

    async function refreshJobs() {
        stopJobPolling();
        const req = beginRequest('jobs');
        try {
            const data = await api('/api/ml/jobs', { params: { limit: 20 }, signal: req.signal });
            if (!req.isCurrent()) return;
            const items = data && Array.isArray(data.items) ? data.items : [];
            state.recentJobs = items;
            if (workflow) workflow.sync('jobs');
            state.mlWorker = data && data.worker ? data.worker : null;
            state.activeJobs.clear();
            items.forEach(function (job) {
                if (job.status === 'scheduled' || job.status === 'running') {
                    state.activeJobs.set(toText(job.job_id, ''), job);
                }
            });
            state.jobPollFailures = 0;
            renderJobs(items, state.mlWorker);
            syncJobControls();
            renderServiceJourney();
            updateNextStep();
            const terminalSignature = items
                .filter(function (job) { return job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled'; })
                .slice(0, 4)
                .map(function (job) { return toText(job.job_id) + ':' + toText(job.status); })
                .join('|');
            if (state.lastTerminalJobSignature && terminalSignature !== state.lastTerminalJobSignature) {
                loadOverview();
                loadModels();
                loadDatasets();
                loadDriftReports();
                loadServices();
            }
            state.lastTerminalJobSignature = terminalSignature;
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            if (workflow) workflow.loadError('jobs', err);
            state.jobPollFailures += 1;
            setConsoleConnection('offline', 'Control plane unavailable');
            setSummaryValue('summary-worker', 'Unavailable', 'danger', toText(err.message));
            setSummaryValue('summary-last-update', 'Sync failed', 'danger', toText(err.message));
            if (!state.recentJobs.length) renderJobStatus(null, 'Unable to load ML jobs: ' + toText(err.message));
            updateNextStep();
        }
        const delay = state.activeJobs.size
            ? Math.min(JOB_POLL_MAX_BACKOFF_MS,
                JOB_POLL_INTERVAL_MS * Math.pow(2, Math.min(5, state.jobPollFailures)))
            : JOB_POLL_MAX_BACKOFF_MS;
        state.jobPollTimer = window.setTimeout(refreshJobs, delay);
    }

    async function cancelJob(jobId) {
        if (!jobId) return;
        try {
            await api('/api/ml/jobs/' + encodeURIComponent(jobId) + '/cancel', {
                method: 'POST', body: {}
            });
            await refreshJobs();
        } catch (err) {
            if (!err.aborted) renderJobStatus(null, 'Cancel failed: ' + toText(err.message));
        }
    }

    async function startTraining() {
        if (hasActiveJob('training')) return;
        const contract = selectedModelTypeContract();
        if (contract && contract.trainable !== true) {
            setNote('training-action-note', friendlyModelType(contract.model_type)
                + ' is currently unavailable. Check Admin Settings or choose an available model type.', 'bad');
            return;
        }
        const typeSelect = getElement('training-model-type');
        const algoSelect = getElement('training-algorithm');
        const datasetSelect = getElement('training-dataset-select');
        const seedInput = getElement('training-seed-input');
        const hpInput = getElement('training-hyperparameters-input');
        const startBtn = getElement('start-training-btn');
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
                setNote('training-action-note', 'Hyperparameters must be a JSON object, for example {"n_estimators": 200}. Correct the value and retry.', 'bad');
                return;
            }
            body.hyperparameters = parsed;
        }
        if (startBtn) startBtn.disabled = true;
        try {
            if (platform) platform.configureRun(body);
            const result = await api('/api/ml/training-jobs', { method: 'POST', body: body });
            renderJobStatus(result, 'Training scheduled');
            setNote('training-action-note', 'Training scheduled. Open Overview to follow every stage. Completion creates a candidate for Review models; it does not deploy it.', 'ok');
            await refreshJobs();
        } catch (err) {
            if (startBtn) startBtn.disabled = false;
            updateTrainingAvailability();
            if (err.aborted) return;
            setNote('training-action-note', formatActionError('Training could not be scheduled', err), 'bad');
        }
    }

    async function cancelTraining() {
        const training = [...state.activeJobs.values()].find(function (job) { return job.kind === 'training'; });
        if (training) await cancelJob(training.job_id);
    }

    async function computeFeatures() {
        const btn = getElement('compute-features-btn');
        if (btn) btn.disabled = true;
        try {
            const result = await api('/api/ml/features/compute', { method: 'POST', body: {} });
            setNote('data-readiness-note',
                'Feature collection scheduled (job ' + toText(result && result.job_id) + ').', 'ok');
            await refreshJobs();
        } catch (err) {
            if (!err.aborted) {
                setNote('data-readiness-note',
                    formatActionError('Feature collection failed', err), 'bad');
            }
        } finally {
            syncJobControls();
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
                method: 'POST', body: body
            });
            renderDatasetsMessage('Dataset build scheduled (job '
                + toText(outcome && outcome.job_id) + ').', 'ok');
            await refreshJobs();
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
                if (message === toText(err.message, 'dataset build failed')) {
                    message = formatActionError('Dataset build failed', err);
                }
                renderDatasetsMessage(message, 'bad');
            }
        } finally {
            syncJobControls();
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
        const contract = selectedModelTypeContract();
        for (const ds of items) {
            if (!compatibleDataset(ds, contract)) continue;
            const opt = el('option', null, toText(ds.name) + ' v' + formatMetric(ds.version)
                + ' — ' + formatMetric(ds.row_count) + ' rows, ' + toText(ds.checksum).slice(0, 10));
            opt.value = toText(ds.id);
            options.push(opt);
        }
        select.replaceChildren.apply(select, options);
        if ([...select.options].some(option => option.value === keep)) select.value = keep;
    }

    async function backfillDatasetHashes() {
        if (hasActiveJob('backfill')) return;
        const btn = getElement('backfill-dataset-hashes-btn');
        if (btn) btn.disabled = true;
        try {
            const job = await api('/api/ml/datasets/backfill-hashes', { method: 'POST', body: {} });
            renderDatasetsMessage('Dataset verification scheduled as ' + toText(job.job_id) + '.', 'ok');
            await refreshJobs();
        } catch (err) {
            if (!err.aborted) renderDatasetsMessage('Verification failed: ' + toText(err.message), 'bad');
        } finally {
            syncJobControls();
        }
    }

    function updateEvaluationForm() {
        const type = getElement('evaluation-model-type');
        const related = getElement('evaluation-related-row');
        const ids = getElement('evaluation-identity-ids');
        const value = type ? type.value : '';
        if (related) related.hidden = value !== 'coappearance_anomaly_model';
        if (ids) ids.placeholder = value === 'threat_ranking_model'
            ? 'Comma-separated identity UUIDs (maximum 200)'
            : 'Identity UUID';
    }

    async function runModelEvaluation() {
        const typeNode = getElement('evaluation-model-type');
        const idsNode = getElement('evaluation-identity-ids');
        const relatedNode = getElement('evaluation-related-id');
        const button = getElement('run-model-evaluation-btn');
        const resultNode = getElement('model-evaluation-result');
        if (!typeNode || !idsNode || !resultNode) return;
        const modelType = typeNode.value;
        const ids = idsNode.value.split(/[\s,]+/).map(function (value) { return value.trim(); })
            .filter(Boolean).slice(0, 200);
        if (!ids.length) {
            resultNode.replaceChildren(el('div', 'mlops-note note-bad', 'Enter at least one identity UUID.'));
            return;
        }
        if (button) button.disabled = true;
        resultNode.replaceChildren(el('div', 'mlops-loading', 'Running observational evaluation…'));
        try {
            let response;
            if (modelType === 'threat_ranking_model') {
                response = await api('/api/ml/rank/threat-review', {
                    method: 'POST', body: { identity_ids: ids }
                });
            } else {
                const body = { model_type: modelType, identity_id: ids[0] };
                if (modelType === 'coappearance_anomaly_model') {
                    body.related_identity_id = relatedNode ? relatedNode.value.trim() : '';
                    if (!body.related_identity_id) {
                        throw { code: 'PAIR_ID_REQUIRED', message: 'Enter the related identity UUID.' };
                    }
                }
                response = await api('/api/ml/score/relational', { method: 'POST', body: body });
            }
            const frag = document.createDocumentFragment();
            frag.appendChild(el('div', 'mlops-note note-ok',
                'Evaluation completed. The result was not applied to any live decision.'));
            frag.appendChild(jsonBlock(response));
            resultNode.replaceChildren(frag);
        } catch (err) {
            if (!err.aborted) resultNode.replaceChildren(el('div', 'mlops-note note-bad',
                formatActionError('Evaluation failed', err)));
        } finally {
            if (button) button.disabled = false;
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
    async function recomputeReadiness(modelId) {
        try {
            const out = await api('/api/ml/models/' + encodeURIComponent(modelId) + '/readiness',
                { method: 'POST', timeout: 120000, body: {} });
            setNote('registry-note', 'Readiness recomputed: engineering '
                + toText(out && out.engineering_gate && out.engineering_gate.status)
                + ' · scientific ' + toText(out && out.scientific_gate && out.scientific_gate.status), 'ok');
            loadModelDetail(modelId);
            loadOverview();
        } catch (err) {
            if (err.aborted) return;
            setNote('registry-note', toText(err.code, 'ERROR') + ': ' + toText(err.message, 'readiness recomputation failed'), 'bad');
        }
    }

    function renderEvidenceBlock(ev) {
        const frag = document.createDocumentFragment();
        if (!ev || typeof ev !== 'object') return frag;
        const adequacy = ev.adequacy && typeof ev.adequacy === 'object' ? ev.adequacy : {};
        const cov = ev.coverage && typeof ev.coverage === 'object' ? ev.coverage : {};
        const pops = ev.populations && typeof ev.populations === 'object' ? ev.populations : {};
        frag.appendChild(el('div', 'mlops-subheading', 'Evidence (reviewed outcomes only — ' + toText(ev.note) + ')'));
        frag.appendChild(kvList([
            ['Adequacy', toText(adequacy.status) + (adequacy.detail ? ' — ' + toText(adequacy.detail) : '')
                + (adequacy.shortfalls && typeof adequacy.shortfalls === 'object' && Object.keys(adequacy.shortfalls).length
                    ? ' — shortfalls: ' + JSON.stringify(adequacy.shortfalls) : '')],
            ['Reviewed / predictions', formatMetric(cov.reviewed_total) + ' / ' + formatMetric(cov.predictions_total)
                + ' (coverage ' + formatMetric(cov.review_coverage_overall, 4) + ')'],
            ['Populations', ['blind_reviewed', 'revealed_reviewed', 'self_reviewed', 'unreviewed', 'weak', 'synthetic_or_seed', 'disputed', 'retracted', 'unknown_outcome']
                .map(function (k) { return k + '=' + formatMetric(pops[k]); }).join(' · ')],
            ['Selection methods', Object.keys(ev.review_selection_methods || {}).map(function (k) { return k + '=' + formatMetric(ev.review_selection_methods[k]); }).join(', ') || 'none'],
            ['Sampling caveat', toText(ev.sampling_caveat)]
        ]));
        const bands = ev.bands && typeof ev.bands === 'object' ? ev.bands : {};
        const bandNames = Object.keys(bands);
        if (bandNames.length) {
            frag.appendChild(simpleTable(['Band', 'Reviewed', 'Positive', 'Negative', 'Positive rate', 'Wilson 95% CI'], bandNames.map(function (b) {
                const t = bands[b] || {};
                const ci = t.wilson_95 && typeof t.wilson_95 === 'object' ? formatMetric(t.wilson_95.low, 3) + ' – ' + formatMetric(t.wilson_95.high, 3) : 'N/A';
                return [b, formatMetric(t.reviewed_count), formatMetric(t.positive_count), formatMetric(t.negative_count), formatMetric(t.positive_rate, 3), ci];
            })));
        }
        const stat = function (label, obj, keys) {
            if (!obj || typeof obj !== 'object') return [label, 'N/A'];
            if (obj.status) return [label, toText(obj.status) + (obj.n !== undefined ? ' (n=' + formatMetric(obj.n) + ')' : '') + (obj.reason ? ' — ' + toText(obj.reason) : '')];
            return [label, keys.map(function (k) { return k + '=' + formatMetric(obj[k], 4); }).join(', ')];
        };
        frag.appendChild(kvList([
            stat('Monotonic trend (Cochran–Armitage)', ev.monotonicity_trend, ['statistic', 'p_value', 'reviewed_total']),
            stat('Spearman score↔outcome', ev.spearman_score_vs_outcome, ['rho', 'p_value', 'n']),
            stat('Ranking (PR-AUC / ROC-AUC / precision@5% / lift@5%)', ev.ranking, ['pr_auc', 'roc_auc', 'precision_at_top_5_pct', 'lift_at_top_5_pct']),
            ['Band separation', toText(ev.band_separation)]
        ]));
        return frag;
    }

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
            const modelSelect = getElement('shadow-model-select');
            const params = { days: daysSelect ? daysSelect.value : 90 };
            if (modelSelect && modelSelect.value) params.model_id = modelSelect.value;
            const report = await api('/api/ml/shadow/evidence', { params: params, signal: req.signal });
            if (!req.isCurrent()) return;
            const frag = document.createDocumentFragment();
            frag.appendChild(el('div', 'mlops-mode-desc', toText(report.note)));
            const dq = report.data_quality && typeof report.data_quality === 'object' ? report.data_quality : {};
            const pops = report.populations && typeof report.populations === 'object' ? report.populations : {};
            const excluded = report.excluded_non_evidence_labels && typeof report.excluded_non_evidence_labels === 'object' ? report.excluded_non_evidence_labels : {};
            frag.appendChild(kvList([
                ['Window', formatMetric(report.window_days) + ' days'],
                ['Predictions', formatMetric(report.predictions) + (toBoolean(report.truncated) ? ' (truncated)' : '')],
                ['Mapping decision', toText(report.mapping_decision)],
                ['Evidence-grade definition', toText(report.evidence_grade_definition)],
                ['Populations (all predictions)', Object.keys(pops).map(function (k) { return k + '=' + formatMetric(pops[k]); }).join(' · ')],
                ['Excluded non-evidence labels', Object.keys(excluded).length ? Object.keys(excluded).map(function (k) { return k + '=' + formatMetric(excluded[k]); }).join(', ') : 'none'],
                ['Duplicate comparison rows (historical, not counted)', formatMetric(dq.duplicate_comparisons)]
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
                frag.appendChild(renderEvidenceBlock(m.evidence));
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
            if (window.MLOpsDiagnostics) frag.appendChild(window.MLOpsDiagnostics.datasetLink(datasetId));
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
                params: { page: 1, page_size: 100 }, signal: req.signal
            });
            if (!req.isCurrent()) return;
            const items = (data && Array.isArray(data.items)) ? data.items : [];
            state.datasets = items;
            state.evidence.datasets = 'ready';
            renderServiceJourney();
            updateNextStep();
            if (workflow) workflow.sync('datasets');
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
            state.evidence.datasets = 'error';
            updateNextStep();
            if (workflow) workflow.loadError('datasets', err);
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
                    const actions = label.label_kind === 'manual' ? ['confirm', 'dispute', 'retract'] : ['dispute', 'retract'];
                    if (label.label_kind !== 'manual') {
                        actionRow.appendChild(el('span', 'mlops-mode-desc', 'weak label — never confirmable into reviewed evidence'));
                    }
                    for (const action of actions) {
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
        const methodSelect = getElement('label-selection-method');
        const assessmentInput = getElement('label-assessment-id');
        const notesInput = getElement('label-notes');
        const body = {
            subject_id: subjectId,
            label: valueSelect ? valueSelect.value : 'negative',
            label_kind: kindSelect ? kindSelect.value : 'manual',
            source: source,
            event_time: eventTime,
            // Recorded from THIS page, where bands are visible: never blind.
            selection: { method: methodSelect && methodSelect.value ? methodSelect.value : 'natural',
                         entry_point: 'ml_ops', ml_observation_revealed: true }
        };
        if (assessmentInput && assessmentInput.value.trim()) body.assessment_id = assessmentInput.value.trim();
        if (notesInput && notesInput.value.trim()) body.notes = notesInput.value.trim();
        try {
            const result = await api('/api/ml/labels', { method: 'POST', body: body });
            setNote('label-form-note',
                toBoolean(result && result.deduplicated)
                    ? 'Identical label already existed — no duplicate created.'
                    : 'Label created (unreviewed, recorded with the ML band visible). Manual labels must pass review before they count.', 'ok');
            loadLabels();
            loadOverview();
        } catch (err) {
            if (err.aborted) return;
            if (err.code === 'LABEL_CONFLICT') {
                const extra = err.detailExtra || {};
                setNote('label-form-note', 'A different label (' + toText(extra.existing_label)
                    + ') already exists for this subject/source/day — your value was NOT recorded. '
                    + 'Correct it through "correct →" on label ' + toText(extra.existing_label_id) + ' in the queue (history is kept).', 'bad');
                return;
            }
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
            title: 'Live decision mode',
            what: 'Which engine makes the live threat decision. RULES: the deterministic risk engine (risk-engine-v1) alone. SHADOW: rules still decide; the approved anomaly model runs in parallel and its output is only recorded for comparison. ML activation follows the current backend evidence gates. HYBRID remains unavailable; a refused mode change reports the exact blockers.',
            read: ['The badge is the mode configured NOW (settings.ML_DECISION_MODE).', 'A card marked "gated" lists the exact unmet conditions; nothing here invents readiness.', '"Pause ML" restores RULES immediately and writes an audit row.'],
            actions: ['Activate: changes the configured mode (reason required, audited).', 'Pause ML: emergency stop back to rules.'],
            progress: 'Nothing long-running here: a change applies to the next assessment.'
        },
        labels_readiness: {
            title: 'Are labels ready?',
            what: 'Counts of REVIEWED manual labels against the minimums supervised training would need (ML_SUPERVISED_MIN_LABELS / PER_CLASS). Weak, unreviewed and disputed labels are listed but never counted.',
            read: ['"supervised_gate_open" true means the reviewed-label count permits threat-ranking training; dataset, class-balance, leakage and artifact gates still apply.'],
            actions: [], progress: 'Grows as analysts confirm labels in the Labels Queue.'
        },
        data_readiness: {
            title: 'Is data ready?',
            what: 'Feature snapshots the collector has written (the rows datasets are built from) and the graph readiness floors for pair features.',
            read: ['Snapshots are computed point-in-time: only data strictly before each as_of is used.', 'The feature set version stamps every snapshot; models trained under another version never score current snapshots.'],
            actions: ['Run feature collection: computes snapshots for new events as a durable worker job.'],
            progress: 'The feature job reports its backend stage and percent in the Durable Job Queue.'
        },
        capabilities: {
            title: 'Available capabilities',
            what: 'MLflow records experiments and model versions; XGBoost is an available algorithm family; Optuna tunes parameters; SHAP explains predictions. Availability is managed in Admin Settings, while tuning and SHAP also require a per-run selection.',
            read: ['Available: the capability can be selected. Disabled: enable it in Admin Settings if needed. Unavailable: its dependency is missing or cannot import. Misconfigured: repair the reported storage or integration setting.', 'An available integration does not mean a model is ready for deployment. Check each run and its saved evidence.'],
            actions: [], progress: ''
        },
        registry: {
            title: 'Models awaiting review',
            what: 'Every trained model with its stage. Lifecycle: training → validated (quality gates passed) → shadow (explicit administrator approval) → archived. Anomaly models can never reach approved/production this release.',
            read: ['Exactly one model per type can be in SHADOW; approving a second one archives the first and retires its threshold set.', 'Detail shows full lineage: dataset id + both hashes, training configuration as run, code version, artifact sha256, whether the artifact file is present, dependency versions, evaluation and the descriptive comparison with the incumbent.'],
            actions: ['Approve for SHADOW: eligible anomaly models observe alongside rules after checksum-bound administrator review. Approve artifact for offline use: eligible ranking/regression candidates can be reviewed for offline use. Neither action grants live decision authority.', 'Reject: records a reason and retires thresholds.', 'Stop shadow: rollback — archives the shadow model; rules keep deciding.'],
            progress: 'Training produces a VALIDATED candidate, never a live model.'
        },
        shadow: {
            title: 'Rules compared with shadow ML',
            what: 'What the shadow model said alongside the rules result for the same assessments. Rule severity and anomaly band are different concepts shown side by side; no score difference is ever computed.',
            read: ['operational_disagreement: both_flagged / rules_only / anomaly_only / neither — which mechanism would have raised attention.', 'Evidence report: per band, how many predictions carry a reviewed outcome and how those outcomes split — the material a human needs to judge a future ML→risk mapping. The decision stays REQUIRES_VALIDATION.'],
            actions: ['Window: 7/30/90 days.', 'Evidence report: read-only.'],
            progress: 'Accumulates with every live assessment while the mode is SHADOW.'
        },
        predictions: {
            title: 'Recent shadow predictions',
            what: 'Individual shadow predictions with their lineage: model, threshold set version, snapshot, event time and, when an analyst later reviewed the assessment, the linked outcome label.',
            read: ['fallback_reason set = the model did not score (no approved model, timeout, artifact/feature-set mismatch…); the live decision was unaffected.', 'Scores are anomaly scores in [0,1], not probabilities.'],
            actions: ['Fallback only: filter to predictions that fell back.'],
            progress: ''
        },
        drift: {
            title: 'Data and prediction drift',
            what: 'PSI / KS / JS divergence of recent feature snapshots against the preceding window, plus prediction drift (score distribution, fallback and failure rates, latency). Observations only.',
            read: ['insufficient_data below ML_DRIFT_MIN_SAMPLES is honest, not a failure.', 'Severity follows the configured PSI thresholds; drift never triggers retraining or mode changes.'],
            actions: ['Run drift check now: enqueues a durable report-only job; automatic checks remain gated; production drift is deferred until deployed inference evidence exists.'],
            progress: 'Track the run in the Durable Job Queue.'
        },
        training: {
            title: 'Build and train',
            what: 'Builds an immutable Parquet dataset (or reuses one you pick) and trains a CANDIDATE. Stages: loading/building dataset → training → evaluating → saving candidate → registering. Inspect the resulting candidate and quality gates. Eligible anomaly models may be reviewed for shadow; ranking/regression artifacts may be reviewed for offline use.',
            read: ['The stage strip and percent come from the job record; a failed job shows its stable error code (e.g. DATASET_FILE_HASH_MISMATCH, QUALITY_GATES_FAILED).', 'Datasets: definition/version, extraction audit (candidate / selected / excluded rows and the cap policy), logical checksum and Parquet file hash. "legacy build" rows predate extraction auditing and are reported, never rewritten.', 'Seed and hyperparameters you enter are persisted verbatim as the model\'s training configuration.'],
            actions: ['Start training: background job; only one at a time.', 'Build dataset: explicit definition, optional time range, and what to do above the cap (refuse by default).', 'Verify legacy datasets: records a file hash only when the reloaded rows reproduce the registered checksum.', 'Archive: releases the Parquet bytes of a dataset no model was trained from (lineage row and manifest stay).'],
            progress: 'Watch the Durable Job Queue; the job id links the run to the audit log and call log.'
        },
        evaluation: {
            title: 'Test a model safely',
            what: 'Runs approved pair/graph anomaly models as on-demand shadow observations, or a validated threat ranker to order an analyst review batch.',
            read: ['Every response is explicitly applied_to_live_result=false.', 'Threat ranking is relative review priority, not a threat probability.'],
            actions: ['Run evaluation: computes current features, verifies the artifact and returns an audited observational score.'],
            progress: 'Synchronous and bounded to 200 identities for ranking.'
        },
        labels: {
            title: 'Review outcome labels',
            what: 'Analyst labels (manual) and weak labels about assessments. A manual label can only be created for a RESOLVED assessment; review actions confirm, dispute or retract it; supersede corrects it while keeping the chain.',
            read: ['Only active, manual, reviewed labels count toward readiness and supervised datasets.', 'Labels are linked to predictions as outcomes for later evaluation.'],
            actions: ['Create label, confirm/dispute/retract, supersede.'],
            progress: ''
        },
        policy: {
            title: 'Retraining policy',
            what: 'Scheduled retraining parameters. Scheduled retraining is GATED this release: enabling it is refused (SCHEDULED_RETRAINING_GATED); training stays a manual, reviewable action.',
            read: ['Values are advisory until the gate is lifted.'], actions: [], progress: ''
        },
        audit: {
            title: 'Administrator activity',
            what: 'Every administrator action on the ML system: mode changes, pause, training requests, model stage transitions, threshold activation/retirement, label lifecycle, dataset archive/verification.',
            read: ['Each row names the actor, the object and the reason given.'], actions: [], progress: ''
        },
        system: {
            title: 'System health',
            what: 'The facts that define the ML system right now, read from the database and settings at load time: the current feature set and its known limitations, which engine decides, what is in shadow and whether it can score current snapshots, dataset and model inventory, the extraction policy, the call log and the migration head.',
            read: ['Alerts are conditions that need an administrator (for example a shadow model trained under an older feature set — it falls back on every assessment until a current one is approved).', '"What changed" lists the core changes that produced this state, newest first.'],
            actions: ['What changed: release notes.'], progress: ''
        },
        calls: {
            title: 'API call diagnostics',
            what: 'One record per /api/ml/* request: time, request id, actor, method, route, status, error code, duration and the ids the call produced. The request id is the X-Request-ID header the browser received and the req=<id> on every server log line of that call.',
            read: ['Errors only: status ≥ 400 (refusals carry their stable error_code).', 'Bodies are sanitised summaries (keys and short values; no feature vectors, no secrets).', 'The same records are in the server application log, tagged [MLOPS_CALL], for offline debugging.'],
            actions: ['Refresh; Errors only.'], progress: ''
        }
    };

    // Short tooltips on the controls themselves (the help modal has the long form).
    const TOOLTIPS = {
        'refresh-console-btn': 'Refresh operational state, jobs, models, evidence and audit data.',
        'jobs-refresh-btn': 'Reload durable queue state and worker heartbeat now.',
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
        'training-model-type': 'Capability contract from the backend: each model selects its entity type, feature set, dataset kind, algorithms and score semantics.',
        'label-selection-method': 'How this review was selected. Anything other than natural marks a stratified subset: positive rates then describe the selection, not population prevalence.',
        'label-assessment-id': 'Anchor the outcome to a RESOLVED assessment so it links to that assessment\u2019s shadow prediction exactly (otherwise the link is by subject and event day).',
        'label-notes': 'Free text for reviewers. Stored on the label only; never written to the call log.',
        'shadow-model-select': 'Restrict the evidence report to one registered model version.',
        'training-algorithm': 'Choose an algorithm supported by the selected task. Start with its default; supervised ranking and numeric regression also offer XGBoost when available.',
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
            if (!node) return;
            if (!node.title) node.title = TOOLTIPS[id];
            const descriptionId = 'mlops-tip-' + id;
            if (!getElement(descriptionId)) {
                const description = el('span', 'mlops-sr-only', TOOLTIPS[id]);
                description.id = descriptionId;
                document.body.appendChild(description);
            }
            const describedBy = (node.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
            if (!describedBy.includes(descriptionId)) describedBy.push(descriptionId);
            node.setAttribute('aria-describedby', describedBy.join(' '));
        });
    }

    async function refreshConsole() {
        const button = getElement('refresh-console-btn');
        if (button) {
            button.disabled = true;
            button.setAttribute('aria-busy', 'true');
        }
        setConsoleConnection('connecting', 'Synchronizing control plane');
        try {
            await Promise.allSettled([
                loadOverview(), loadModels(), loadShadowSummary(), loadPredictions(),
                loadDriftReports(), loadDatasetDefinitions(), loadDatasets(), loadLabels(),
                loadPolicy(), loadAudit(), loadCalls(), refreshJobs(), loadServices()
            ]);
        } finally {
            if (button) {
                button.disabled = false;
                button.removeAttribute('aria-busy');
            }
        }
    }

    function installHelpButtons() {
        document.querySelectorAll('.mlops-card[data-help]').forEach(function (card) {
            const header = card.querySelector('.mlops-card-header');
            const key = card.getAttribute('data-help');
            if (!header || !HELP[key] || header.querySelector('.mlops-help-btn')) return;
            const btn = el('button', 'mlops-help-btn');
            btn.appendChild(faIcon('fas fa-circle-question'));
            btn.appendChild(el('span', null, 'Guide'));
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
        if (key === 'training' && state.activeJobs.size) {
            lines.push(String(state.activeJobs.size) + ' ML job(s) queued or running.');
        }
        if (key === 'training' && !state.activeJobs.size) lines.push('No job running.');
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
            if (items.length) {
                const header = el('div', 'mlops-call-row mlops-call-header');
                ['Time', 'Request ID', 'Method', 'Route', 'Status', 'Duration', 'Outcome'].forEach(function (label) {
                    header.appendChild(el('strong', null, label));
                });
                frag.appendChild(header);
            }
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
            renderCardError('calls-body', err);
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
        if (window.MLOpsPlatform) platform = window.MLOpsPlatform({ api, el, kvList, simpleTable, jsonBlock, state, formatActionError, openActionPanel, refreshConsole, updateTrainingAvailability, fillTrainingDatasetPicker });
        if (window.MLOpsWorkflow) workflow = window.MLOpsWorkflow({
            api, beginRequest, el, kvList, simpleTable, jsonBlock, formatMetric,
            formatActionError, state, activateWorkspace, openDatasetDetail, loadModelDetail, refreshConsole,
            platformResults: model => platform && platform.results(model)
        });
        installWorkspaceNavigation();
        installServiceJourney();
        on('system-notes-btn', 'click', openReleaseNotes);
        installHelpButtons();
        applyTooltips();
        on('mlops-help-close', 'click', closeHelp);
        document.querySelectorAll('.mlops-help-btn').forEach(function (btn) {
            btn.addEventListener('click', function () { openHelp(btn.dataset.helpKey); });
        });
        on('calls-refresh-btn', 'click', loadCalls);
        on('calls-errors-only', 'change', loadCalls);
        on('refresh-console-btn', 'click', refreshConsole);
        on('jobs-refresh-btn', 'click', refreshJobs);
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
        on('evaluation-model-type', 'change', updateEvaluationForm);
        on('run-model-evaluation-btn', 'click', runModelEvaluation);
        on('cancel-training-btn', 'click', cancelTraining);
        on('compute-features-btn', 'click', computeFeatures);
        on('build-dataset-btn', 'click', buildDataset);
        on('training-status-body', 'click', function (event) {
            const origin = event.target && event.target.closest ? event.target : null;
            const button = origin ? origin.closest('button[data-cancel-job-id]') : null;
            if (button) cancelJob(button.dataset.cancelJobId);
        });
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
        on('training-model-type', 'change', function () { selectService(getElement('training-model-type').value, true); });
        on('audit-prev', 'click', function () {
            if (state.auditPage > 1) { state.auditPage -= 1; loadAudit(); }
        });
        on('audit-next', 'click', function () {
            state.auditPage += 1; loadAudit();
        });

        updateEvaluationForm();

        loadServices();
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
        refreshJobs();
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
