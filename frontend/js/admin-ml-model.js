/**
 * ML Model Management (hardened rewrite)
 * ======================================
 * Similarity-model lifecycle: status, readiness, background training jobs,
 * candidate review, activation and rollback.
 *
 * Contract:
 *  - All backend values pass through typed normalizers: string "false" is
 *    false, numeric strings parse, zero stays zero, missing stays missing.
 *  - No backend value ever passes through innerHTML; DOM building only.
 *  - Status loads from the authenticated no-store API — no page-injected
 *    window globals.
 *  - Training is a background job: schedule -> poll real job state (no
 *    arbitrary setTimeout guesses) -> render staged progress.
 *  - Requests use AbortController + latest-wins generations; Refresh is
 *    disabled while a request runs.
 *  - Filesystem paths are never displayed — only logical artifact names.
 */

(function () {
    'use strict';

    const DEBUG = false;
    const API_TIMEOUT_MS = 30000;
    const JOB_POLL_INTERVAL_MS = 1500;
    const JOB_POLL_MAX = 400;

    function log() { if (DEBUG) console.log.apply(console, arguments); }

    // ============================================
    // Typed normalization helpers
    // ============================================

    function toBoolean(value, fallback) {
        if (typeof value === 'boolean') return value;
        if (typeof value === 'string') {
            const normalized = value.trim().toLowerCase();
            if (normalized === 'true') return true;
            if (normalized === 'false') return false;
        }
        if (value === 1 || value === '1') return true;
        if (value === 0 || value === '0') return false;
        return fallback === undefined ? false : fallback;
    }

    function toFiniteNumber(value, fallback) {
        if (value === null || value === undefined || value === '') {
            return fallback === undefined ? null : fallback;
        }
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : (fallback === undefined ? null : fallback);
    }

    function toNonNegativeInteger(value, fallback) {
        const parsed = Math.floor(Number(value));
        return Number.isFinite(parsed) ? Math.max(0, parsed) : (fallback === undefined ? 0 : fallback);
    }

    function formatMetric(value, digits) {
        const number = toFiniteNumber(value);
        return number === null ? 'N/A' : number.toFixed(digits === undefined ? 4 : digits);
    }

    function safeText(value, fallback) {
        if (value === null || value === undefined || value === '') {
            return fallback !== undefined ? fallback : '';
        }
        return String(value);
    }

    function parseTimestamp(value) {
        if (typeof value !== 'string' || !value.trim()) return null;
        let v = value;
        if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(v)) v += 'Z';
        const d = new Date(v);
        return Number.isFinite(d.getTime()) ? d : null;
    }

    function fmtDateTime(value) {
        const d = parseTimestamp(value);
        return d ? d.toLocaleString() : 'Unknown time';
    }

    function getElement(id) {
        const element = document.getElementById(id);
        if (!element) log('[MODEL MANAGEMENT] element missing:', id);
        return element;
    }

    function el(tag, opts, children) {
        const node = document.createElement(tag);
        opts = opts || {};
        if (opts.className) node.className = opts.className;
        if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
        if (opts.attrs) {
            for (const key of Object.keys(opts.attrs)) {
                const v = opts.attrs[key];
                if (v !== undefined && v !== null) node.setAttribute(key, String(v));
            }
        }
        if (children) {
            for (const child of [].concat(children)) {
                if (child) node.append(child);
            }
        }
        return node;
    }

    function faIcon(name) { return el('i', { className: name, attrs: { 'aria-hidden': 'true' } }); }

    // ============================================
    // Shared API client
    // ============================================

    function ApiError(message, opts) {
        const e = new Error(message);
        e.name = 'ApiError';
        e.status = (opts && opts.status) ?? 0;
        e.code = (opts && opts.code) || null;
        e.referenceId = (opts && opts.referenceId) || null;
        e.jobId = (opts && opts.jobId) || null;
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
            let code = null, referenceId = null, jobId = null, detailExtra = null;
            let message = 'Request failed (' + response.status + ')';
            try {
                const body = await response.json();
                const detail = body && body.detail;
                if (detail && typeof detail === 'object') {
                    code = detail.error_code || null;
                    jobId = detail.job_id || null;
                    detailExtra = detail;
                    if (typeof detail.message === 'string') message = detail.message;
                } else if (typeof detail === 'string') {
                    message = detail;
                    const refMatch = detail.match(/Reference:\s*([A-Za-z0-9-]+)/);
                    if (refMatch) referenceId = refMatch[1];
                }
            } catch (_) { /* keep generic */ }
            throw ApiError(message, { status: response.status, code: code, referenceId: referenceId, jobId: jobId, detailExtra: detailExtra });
        }
        if (response.status === 204) return null;
        return response.json();
    }

    // ============================================
    // Request lifecycle
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
        status: null,          // normalized status payload
        activeJobId: null,
        jobPollTimer: null,
        training: false
    };

    function normalizeStatus(raw) {
        if (!raw || typeof raw !== 'object') return null;
        return {
            isTrained: toBoolean(raw.is_trained, false),
            runtimeLoaded: toBoolean(raw.runtime_loaded, false),
            sklearnAvailable: toBoolean(raw.sklearn_available, false),
            trainingSamples: toNonNegativeInteger(raw.training_samples),
            approvedSamples: toNonNegativeInteger(raw.approved_samples),
            rejectedSamples: toNonNegativeInteger(raw.rejected_samples),
            uniquePairs: toNonNegativeInteger(raw.unique_identity_pairs),
            minSamples: toNonNegativeInteger(raw.min_samples, 50) || 50,
            readyToTrain: toBoolean(raw.ready_to_train, false),
            readinessReason: safeText(raw.readiness_reason, ''),
            readinessChecks: (raw.readiness_checks && typeof raw.readiness_checks === 'object') ? raw.readiness_checks : {},
            activeModel: raw.active_model || null,
            candidateModel: raw.candidate_model || null,
            trainingJobRunning: safeText(raw.training_job_running, '') || null,
            configuration: raw.configuration || {}
        };
    }

    // ============================================
    // Notifications + confirm dialog (no browser popups)
    // ============================================

    function showNotification(message, type) {
        type = ['info', 'success', 'error', 'warning'].indexOf(type) >= 0 ? type : 'info';
        const colors = { info: '#3498db', success: '#2ecc71', error: '#e74c3c', warning: '#f39c12' };
        const n = el('div', { text: message, attrs: { role: type === 'error' ? 'alert' : 'status' } });
        n.style.cssText = 'position:fixed;top:20px;right:20px;padding:14px 20px;background:' + colors[type] +
            ';color:#fff;border-radius:6px;z-index:10010;box-shadow:0 4px 6px rgba(0,0,0,0.3);font-weight:600;';
        document.body.appendChild(n);
        window.setTimeout(function () {
            n.style.opacity = '0'; n.style.transition = 'opacity 0.3s';
            window.setTimeout(function () { n.remove(); }, 300);
        }, 4000);
    }

    let activeDialog = null;

    function closeDialog() {
        if (!activeDialog) return;
        document.removeEventListener('keydown', activeDialog.keyHandler);
        activeDialog.node.remove();
        if (activeDialog.previousFocus && activeDialog.previousFocus.focus) activeDialog.previousFocus.focus();
        activeDialog = null;
    }

    function showDialog(title, bodyNodes, confirmLabel) {
        return new Promise(function (resolve) {
            closeDialog();
            const confirmBtn = el('button', { className: 'train-btn', text: confirmLabel, attrs: { type: 'button' } });
            const cancelBtn = el('button', { className: 'refresh-btn', text: 'Cancel', attrs: { type: 'button' } });
            const buttons = el('div', {}, [cancelBtn, confirmBtn]);
            buttons.style.cssText = 'display:flex;gap:0.75rem;justify-content:flex-end;margin-top:1rem;';
            const dialog = el('div', { attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-label': title } }, [
                el('h3', { text: title }),
                el('div', {}, bodyNodes),
                buttons
            ]);
            dialog.style.cssText = 'background:#131a29;color:#fff;border:1px solid rgba(99,102,241,0.5);border-radius:10px;' +
                'padding:1.5rem;max-width:520px;width:92%;max-height:80vh;overflow:auto;';
            const backdrop = el('div', {}, dialog);
            backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center;z-index:10005;';
            function finish(result) { closeDialog(); resolve(result); }
            cancelBtn.addEventListener('click', function () { finish(false); });
            confirmBtn.addEventListener('click', function () { finish(true); });
            backdrop.addEventListener('click', function (e) { if (e.target === backdrop) finish(false); });
            const focusables = [cancelBtn, confirmBtn];
            const keyHandler = function (e) {
                if (e.key === 'Escape') { e.preventDefault(); finish(false); }
                if (e.key === 'Tab') {
                    e.preventDefault();
                    const idx = focusables.indexOf(document.activeElement);
                    focusables[(idx + (e.shiftKey ? -1 : 1) + 2) % 2].focus();
                }
            };
            document.addEventListener('keydown', keyHandler);
            activeDialog = { node: backdrop, keyHandler: keyHandler, previousFocus: document.activeElement };
            document.body.appendChild(backdrop);
            confirmBtn.focus();
        });
    }

    // ============================================
    // Status loading + rendering
    // ============================================

    async function loadModelStatus() {
        const statusBody = getElement('model-status-body');
        const refreshBtn = getElement('refresh-status-btn');
        if (!statusBody) return;
        const req = beginRequest('status');
        if (refreshBtn) refreshBtn.disabled = true;
        statusBody.replaceChildren(el('div', { className: 'loading-spinner' },
            [faIcon('fas fa-spinner fa-spin'), el('span', { text: ' Loading model status...' })]));
        try {
            const raw = await api('/api/admin/merge-suggestions/model-status', { signal: req.signal });
            if (!req.isCurrent()) return; // stale response never overwrites newer state
            state.status = normalizeStatus(raw);
            renderAll();
            // If a job is running server-side (e.g. page reopened), resume polling
            if (state.status.trainingJobRunning && !state.jobPollTimer) {
                pollTrainingJob(state.status.trainingJobRunning, 1);
            }
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            statusBody.replaceChildren(el('div', { className: 'training-result error' }, [
                faIcon('fas fa-exclamation-circle'),
                el('span', { text: ' Failed to load model status' + (err.referenceId ? ' (Reference: ' + err.referenceId + ')' : '') })
            ]));
        } finally {
            if (req.isCurrent() && refreshBtn) refreshBtn.disabled = false;
        }
    }

    function statusItem(label, value, cls) {
        return el('div', { className: 'status-item' }, [
            el('div', { className: 'status-label', text: label }),
            el('div', { className: 'status-value' + (cls ? ' ' + cls : ''), text: value })
        ]);
    }

    function renderAll() {
        renderModelStatus();
        renderReadiness();
        renderLifecycle();
        updateTrainingButton();
        updateStatistics();
        renderConfiguration();
    }

    function renderModelStatus() {
        const statusBody = getElement('model-status-body');
        const s = state.status;
        if (!statusBody || !s) return;

        const grid = el('div', { className: 'status-grid' }, [
            statusItem('Model Status', s.isTrained ? 'Trained' : 'Not Trained', s.isTrained ? 'true' : 'false'),
            statusItem('Runtime Loaded', s.runtimeLoaded ? 'Yes' : 'No', s.runtimeLoaded ? 'true' : 'false'),
            statusItem('Training Samples', String(s.trainingSamples)),
            statusItem('Minimum Required', String(s.minSamples)),
            statusItem('Ready to Train', s.readyToTrain ? 'Yes' : 'No', s.readyToTrain ? 'true' : 'false')
        ]);

        const children = [grid];
        const active = s.activeModel;
        const metrics = active && active.metrics && active.metrics.validation;
        if (metrics) {
            children.push(el('div', { className: 'metrics-grid' }, [
                el('div', { className: 'metric-item' }, [
                    el('div', { className: 'metric-label', text: 'Precision (validation)' }),
                    el('div', { className: 'metric-value', text: formatMetric(metrics.precision) })
                ]),
                el('div', { className: 'metric-item' }, [
                    el('div', { className: 'metric-label', text: 'False-merge rate' }),
                    el('div', { className: 'metric-value', text: formatMetric(metrics.false_merge_rate) })
                ]),
                el('div', { className: 'metric-item' }, [
                    el('div', { className: 'metric-label', text: 'Recall' }),
                    el('div', { className: 'metric-value', text: formatMetric(metrics.recall) })
                ]),
                el('div', { className: 'metric-item' }, [
                    el('div', { className: 'metric-label', text: 'R² / MSE' }),
                    el('div', { className: 'metric-value', text: formatMetric(metrics.r2) + ' / ' + formatMetric(metrics.mse, 6) })
                ])
            ]));
        }
        statusBody.replaceChildren.apply(statusBody, children);
    }

    function renderReadiness() {
        const host = getElement('readiness-body');
        const s = state.status;
        if (!host || !s) return;
        const checks = s.readinessChecks;
        const keys = Object.keys(checks);
        if (!keys.length) {
            host.replaceChildren(el('p', { text: 'No readiness information available' }));
            return;
        }
        const rows = keys.map(function (key) {
            const check = checks[key] || {};
            const passed = toBoolean(check.passed, false);
            const label = key.replace(/_/g, ' ');
            let detail;
            if (check.ratio !== undefined) {
                detail = 'ratio ' + formatMetric(check.ratio, 2) + ' (min ' + formatMetric(check.minimum_ratio, 2) + ')';
            } else {
                detail = toNonNegativeInteger(check.current) + ' / ' + toNonNegativeInteger(check.required) + ' required';
            }
            const row = el('div', { className: 'readiness-row' }, [
                faIcon(passed ? 'fas fa-check-circle' : 'fas fa-times-circle'),
                el('span', { text: ' ' + label + ': ' + detail })
            ]);
            row.style.cssText = 'padding:0.25rem 0;color:' + (passed ? '#2ecc71' : '#ff6b6b') + ';font-size:0.9rem;';
            return row;
        });
        host.replaceChildren.apply(host, rows);
    }

    function shortHash(value) {
        const s = safeText(value, '');
        return s ? s.slice(0, 12) + '…' : 'N/A';
    }

    function modelCard(title, model, actions) {
        const rows = [
            el('h3', { text: title }),
            el('div', { text: 'Artifact: ' + safeText(model.artifact_name, 'unknown') }),
            el('div', { text: 'Version: v' + toNonNegativeInteger(model.version) + ' — status: ' + safeText(model.status) }),
            el('div', { text: 'Hash: ' + shortHash(model.artifact_hash) }),
            el('div', { text: 'Created: ' + fmtDateTime(model.created_at) })
        ];
        if (model.activated_at) rows.push(el('div', { text: 'Activated: ' + fmtDateTime(model.activated_at) }));
        const gates = model.quality_gates;
        if (gates) {
            const passed = toBoolean(gates.passed, false);
            const gateLine = el('div', { text: 'Quality gates: ' + (passed ? 'PASSED' : 'FAILED') });
            gateLine.style.color = passed ? '#2ecc71' : '#ff6b6b';
            rows.push(gateLine);
        }
        const validation = model.metrics && model.metrics.validation;
        if (validation) {
            rows.push(el('div', {
                text: 'Validation: precision ' + formatMetric(validation.precision) +
                    ', false-merge ' + formatMetric(validation.false_merge_rate) +
                    ', samples ' + toNonNegativeInteger(validation.sample_count)
            }));
        }
        if (model.comparison && model.comparison.recommendation) {
            rows.push(el('div', { text: 'Recommendation: ' + safeText(model.comparison.recommendation) }));
        }
        if (actions && actions.length) {
            const bar = el('div', {}, actions);
            bar.style.cssText = 'display:flex;gap:0.5rem;margin-top:0.6rem;flex-wrap:wrap;';
            rows.push(bar);
        }
        const card = el('div', { className: 'model-lifecycle-card' }, rows);
        card.style.cssText = 'border:1px solid rgba(99,102,241,0.35);border-radius:8px;padding:0.8rem;margin:0.5rem 0;font-size:0.9rem;';
        return card;
    }

    function lifecycleButton(label, iconClass, handler) {
        const btn = el('button', { className: 'refresh-btn', attrs: { type: 'button' } },
            [faIcon(iconClass), el('span', { text: ' ' + label })]);
        btn.addEventListener('click', handler);
        return btn;
    }

    function renderLifecycle() {
        const host = getElement('lifecycle-body');
        const s = state.status;
        if (!host || !s) return;
        const cards = [];

        if (s.activeModel) {
            cards.push(modelCard('Active model', s.activeModel, []));
        } else {
            cards.push(el('p', { text: 'No versioned model is active yet — the runtime uses the heuristic (or a legacy artifact) until a candidate is activated.' }));
        }

        if (s.candidateModel) {
            const candidate = s.candidateModel;
            const gatesPassed = toBoolean(candidate.quality_gates && candidate.quality_gates.passed, false);
            const actions = [];
            if (gatesPassed) {
                actions.push(lifecycleButton('Activate', 'fas fa-rocket', function () {
                    activateModel(candidate.id, false);
                }));
            }
            actions.push(lifecycleButton('Reject', 'fas fa-ban', function () {
                rejectModel(candidate.id);
            }));
            cards.push(modelCard('Candidate awaiting review', candidate, actions));
        }

        loadRollbackTargets(cards, host);
    }

    async function loadRollbackTargets(cards, host) {
        host.replaceChildren.apply(host, cards);
        try {
            const data = await api('/api/admin/merge-suggestions/models', { params: { limit: 10 } });
            const archived = ((data && data.items) || []).filter(function (m) { return m.status === 'archived'; });
            if (archived.length) {
                const target = archived[0];
                const bar = el('div', {}, [lifecycleButton('Rollback to v' + toNonNegativeInteger(target.version), 'fas fa-history', function () {
                    activateModel(target.id, true);
                })]);
                bar.style.cssText = 'margin-top:0.5rem;';
                host.append(bar);
            }
        } catch (err) {
            if (!err.aborted) log('[MODEL MANAGEMENT] rollback targets unavailable');
        }
    }

    function updateTrainingButton() {
        const trainBtn = getElement('train-btn');
        const s = state.status;
        if (!trainBtn) return;
        if (!s) { trainBtn.disabled = true; return; }

        trainBtn.replaceChildren();
        if (state.training || s.trainingJobRunning) {
            trainBtn.disabled = true;
            trainBtn.append(faIcon('fas fa-spinner fa-spin'), el('span', { text: ' Training in progress...' }));
            return;
        }
        if (s.readyToTrain) {
            trainBtn.disabled = false;
            trainBtn.append(faIcon('fas fa-brain'), el('span', { text: ' Train Model' }));
            return;
        }
        trainBtn.disabled = true;
        // Never a negative count — and sample count is not the only rule
        const remaining = Math.max(0, s.minSamples - s.trainingSamples);
        const label = remaining > 0
            ? 'Need ' + remaining + ' more samples'
            : 'Not ready: ' + (s.readinessReason || 'see readiness checks').replace(/_/g, ' ');
        trainBtn.append(faIcon('fas fa-clock'), el('span', { text: ' ' + label }));
    }

    function updateStatistics() {
        const s = state.status;
        if (!s) return;
        const set = function (id, value) {
            const node = getElement(id);
            if (node) node.textContent = String(value);
        };
        set('total-samples', s.trainingSamples);
        set('approved-samples', s.approvedSamples);
        set('rejected-samples', s.rejectedSamples);
        set('ready-status', s.readyToTrain ? 'Yes' : 'No');
    }

    function renderConfiguration() {
        const s = state.status;
        if (!s) return;
        const config = s.configuration || {};
        const artifactEl = getElement('model-artifact');
        if (artifactEl) {
            artifactEl.textContent = s.activeModel ? safeText(s.activeModel.artifact_name) : 'No versioned artifact yet';
        }
        const minSamplesEl = getElement('min-samples');
        if (minSamplesEl) minSamplesEl.textContent = String(toNonNegativeInteger(config.minimum_samples, s.minSamples));
        // Always reflect the server's configured minimum unless the operator
        // has typed over it in this session. The old `if (!input.value)` guard
        // never fired, because the HTML shipped a hard-coded value="50".
        const minSamplesInput = getElement('min-samples-input');
        if (minSamplesInput && !minSamplesInput.dataset.userEdited) {
            minSamplesInput.value = String(s.minSamples);
        }
        if (minSamplesInput && !minSamplesInput.dataset.editListenerBound) {
            minSamplesInput.dataset.editListenerBound = '1';
            minSamplesInput.addEventListener('input', () => {
                minSamplesInput.dataset.userEdited = '1';
            });
        }
        const autoTrainEl = getElement('auto-train');
        if (autoTrainEl) {
            // Explicit boolean render — a real false must show "Disabled"
            autoTrainEl.textContent = toBoolean(config.auto_train, false) ? 'Enabled' : 'Disabled';
        }
        const schemaEl = getElement('feature-schema');
        if (schemaEl) schemaEl.textContent = safeText(config.feature_schema_version, 'N/A');
    }

    // ============================================
    // Training job flow (background + real polling)
    // ============================================

    function stopJobPolling() {
        if (state.jobPollTimer) { window.clearTimeout(state.jobPollTimer); state.jobPollTimer = null; }
    }

    function renderTrainingProgress(task) {
        const resultDiv = getElement('training-result');
        if (!resultDiv) return;
        const details = (task && task.details) || {};
        const stage = safeText(details.stage, task && task.status === 'scheduled' ? 'scheduled' : 'running');
        const progress = toNonNegativeInteger(task && task.progress_percent);
        resultDiv.className = 'training-result';
        resultDiv.style.display = 'block';
        resultDiv.setAttribute('aria-live', 'polite');
        resultDiv.replaceChildren(
            faIcon('fas fa-spinner fa-spin'),
            el('span', { text: ' Training job ' + safeText(state.activeJobId) + ' — ' + stage.replace(/_/g, ' ') + ' (' + progress + '%)' })
        );
    }

    function renderTrainingCompleted(task) {
        const resultDiv = getElement('training-result');
        if (!resultDiv) return;
        const result = (task && task.result) || {};
        const validation = result.validation_metrics || {};
        const gates = result.quality_gates || {};
        const gatesPassed = toBoolean(gates.passed, false);
        resultDiv.className = 'training-result success';
        resultDiv.style.display = 'block';
        resultDiv.replaceChildren(
            faIcon('fas fa-check-circle'),
            el('strong', { text: ' Training completed — candidate v' + toNonNegativeInteger(result.version) + ' awaits review' }),
            el('div', { text: 'Validation precision: ' + formatMetric(validation.precision) + ' — false-merge rate: ' + formatMetric(validation.false_merge_rate) }),
            el('div', { text: 'R²: ' + formatMetric(validation.r2) + ' — MSE: ' + formatMetric(validation.mse, 6) + ' — validation samples: ' + toNonNegativeInteger(validation.sample_count) }),
            el('div', {
                text: 'Quality gates: ' + (gatesPassed ? 'PASSED — the candidate can be activated' : 'FAILED — activation is blocked')
            })
        );
    }

    function renderTrainingFailed(task, fallbackMessage) {
        const resultDiv = getElement('training-result');
        if (!resultDiv) return;
        const code = safeText(task && task.error_code, '');
        resultDiv.className = 'training-result error';
        resultDiv.style.display = 'block';
        resultDiv.replaceChildren(
            faIcon('fas fa-exclamation-circle'),
            el('strong', { text: ' Training failed' }),
            el('div', { text: code ? 'Reason: ' + code.replace(/_/g, ' ').toLowerCase() : safeText(fallbackMessage, 'See Background Tasks for details') })
        );
    }

    async function pollTrainingJob(jobId, attempt) {
        state.activeJobId = jobId;
        if (attempt > JOB_POLL_MAX) {
            renderTrainingFailed(null, 'Job is taking too long — check the Background Tasks page');
            finishTrainingUi();
            return;
        }
        try {
            const task = await api('/api/admin/merge-suggestions/training-jobs/' + encodeURIComponent(jobId));
            const status = safeText(task && task.status, 'unknown');
            if (status === 'completed') {
                renderTrainingCompleted(task);
                showNotification('Training finished — candidate ready for review', 'success');
                finishTrainingUi();
                loadModelStatus(); // driven by real job state, not a timer guess
                return;
            }
            if (status === 'failed') {
                renderTrainingFailed(task);
                finishTrainingUi();
                loadModelStatus();
                return;
            }
            renderTrainingProgress(task);
        } catch (err) {
            if (err.aborted) return;
        }
        state.jobPollTimer = window.setTimeout(function () { pollTrainingJob(jobId, attempt + 1); }, JOB_POLL_INTERVAL_MS);
    }

    function finishTrainingUi() {
        state.training = false;
        state.activeJobId = null;
        stopJobPolling();
        const cancelBtn = getElement('cancel-train-btn');
        if (cancelBtn) cancelBtn.style.display = 'none';
        updateTrainingButton();
    }

    async function trainModel() {
        if (state.training) return;
        const minSamplesInput = getElement('min-samples-input');
        const minSamples = minSamplesInput ? toNonNegativeInteger(minSamplesInput.value, 0) : 0;

        state.training = true;
        updateTrainingButton();
        try {
            const result = await api('/api/admin/merge-suggestions/training-jobs', {
                method: 'POST',
                params: minSamples >= 10 ? { min_samples: minSamples } : {}
            });
            const jobId = safeText(result && result.job_id);
            showNotification('Training scheduled (job ' + jobId + ')', 'success');
            const cancelBtn = getElement('cancel-train-btn');
            if (cancelBtn) cancelBtn.style.display = 'inline-flex';
            pollTrainingJob(jobId, 1);
        } catch (err) {
            state.training = false;
            updateTrainingButton();
            if (err.code === 'TRAINING_ALREADY_RUNNING' && err.jobId) {
                showNotification('A training job is already running (job ' + err.jobId + ')', 'info');
                pollTrainingJob(err.jobId, 1);
            } else if (err.code === 'DATASET_NOT_READY') {
                const resultDiv = getElement('training-result');
                if (resultDiv) {
                    resultDiv.className = 'training-result error';
                    resultDiv.style.display = 'block';
                    resultDiv.replaceChildren(
                        faIcon('fas fa-exclamation-circle'),
                        el('strong', { text: ' Dataset not ready' }),
                        el('div', { text: safeText(err.detailExtra && err.detailExtra.readiness_reason, 'see readiness checks').replace(/_/g, ' ') })
                    );
                }
                loadModelStatus();
            } else if (!err.aborted) {
                showNotification('Failed to schedule training' + (err.referenceId ? ' (Reference: ' + err.referenceId + ')' : ''), 'error');
            }
        }
    }

    async function cancelTraining() {
        if (!state.activeJobId) return;
        try {
            await api('/api/admin/merge-suggestions/training-jobs/' + encodeURIComponent(state.activeJobId) + '/cancel', { method: 'POST' });
            showNotification('Cancellation requested', 'info');
        } catch (err) {
            if (!err.aborted) showNotification('Could not cancel the job', 'error');
        }
    }

    // ============================================
    // Lifecycle actions
    // ============================================

    async function activateModel(modelId, isRollback) {
        const reasonInput = el('input', {
            className: 'form-control',
            attrs: { type: 'text', placeholder: 'Reason (recommended)', maxlength: '500', 'aria-label': 'Reason' }
        });
        const confirmed = await showDialog(
            isRollback ? 'Rollback to this model version?' : 'Activate this candidate?',
            [
                el('p', {
                    text: isRollback
                        ? 'The archived version will be revalidated and atomically restored as the active model.'
                        : 'The candidate will be revalidated (hash + load test), the current active model archived, and the runtime refreshed atomically.'
                }),
                reasonInput
            ],
            isRollback ? 'Rollback' : 'Activate');
        if (!confirmed) return;
        try {
            const path = '/api/admin/merge-suggestions/models/' + encodeURIComponent(modelId) +
                (isRollback ? '/rollback' : '/activate');
            const outcome = await api(path, {
                method: 'POST',
                params: reasonInput.value.trim() ? { reason: reasonInput.value.trim() } : {}
            });
            if (outcome.runtime_degraded) {
                showNotification('Model v' + outcome.version + ' activated, but the runtime reload is degraded — check logs', 'warning');
            } else {
                showNotification('Model v' + outcome.version + ' is now active', 'success');
            }
            loadModelStatus();
        } catch (err) {
            if (err.code === 'QUALITY_GATES_FAILED') {
                showNotification('Activation blocked: the candidate failed its quality gates', 'error');
            } else if (!err.aborted) {
                showNotification(safeText(err.message, 'Activation failed'), 'error');
            }
        }
    }

    async function rejectModel(modelId) {
        const reasonInput = el('input', {
            className: 'form-control',
            attrs: { type: 'text', placeholder: 'Reason (recommended)', maxlength: '500', 'aria-label': 'Reason' }
        });
        const confirmed = await showDialog('Reject this candidate?',
            [el('p', { text: 'The candidate is marked rejected and can never be activated. The artifact is kept for audit.' }), reasonInput],
            'Reject candidate');
        if (!confirmed) return;
        try {
            await api('/api/admin/merge-suggestions/models/' + encodeURIComponent(modelId) + '/reject', {
                method: 'POST',
                params: reasonInput.value.trim() ? { reason: reasonInput.value.trim() } : {}
            });
            showNotification('Candidate rejected', 'success');
            loadModelStatus();
        } catch (err) {
            if (!err.aborted) showNotification('Rejection failed', 'error');
        }
    }

    // ============================================
    // Wiring
    // ============================================

    function attachOnce(id, evt, fn) {
        const node = document.getElementById(id);
        if (node && !node.dataset.listenerAttached) {
            node.addEventListener(evt, fn);
            node.dataset.listenerAttached = 'true';
        }
    }

    function destroy() {
        abortAllRequests();
        stopJobPolling();
        closeDialog();
    }

    document.addEventListener('DOMContentLoaded', function () {
        attachOnce('refresh-status-btn', 'click', loadModelStatus);
        attachOnce('train-btn', 'click', trainModel);
        attachOnce('cancel-train-btn', 'click', cancelTraining);
        loadModelStatus();
    });

    window.addEventListener('pagehide', destroy);
})();
