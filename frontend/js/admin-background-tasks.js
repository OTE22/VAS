/**
 * Background Tasks Monitor
 * ========================
 * Production monitoring page for background jobs + retention testing.
 *
 * Design rules implemented here:
 *  - ONE polling mechanism per resource, recursive setTimeout (never
 *    overlapping setInterval), per-resource in-flight locks + AbortController
 *  - server-side pagination/filtering — only the visible page is downloaded
 *  - cache: 'no-store' + Accept header on every monitoring request
 *  - content-type-aware response parsing (JSON / text / empty / nginx HTML)
 *  - status CSS classes validated against a fixed allowlist
 *  - task details in an accessible modal (never window.alert)
 *  - one shared AudioContext, created only after explicit user opt-in
 *  - alert dedup via alert_instance_id persisted in sessionStorage
 *  - polling paused while document.hidden, immediate refresh on return
 *  - full cleanup on pagehide: timers cleared, requests aborted, audio closed
 */
'use strict';

(() => {
    // ------------------------------------------------------------------
    // Constants & state
    // ------------------------------------------------------------------
    const VALID_STATUSES = new Set([
        'scheduled', 'running', 'completed', 'failed', 'cancelled', 'overdue'
    ]);
    const STATUS_ICONS = {
        scheduled: 'fa-clock',
        running: 'fa-spinner fa-spin',
        completed: 'fa-check-circle',
        failed: 'fa-times-circle',
        cancelled: 'fa-ban',
        overdue: 'fa-exclamation-triangle'
    };
    const TAB_TO_STATUS = {
        all: '', upcoming: 'upcoming', running: 'running', completed: 'completed',
        failed: 'failed', cancelled: 'cancelled', overdue: 'overdue'
    };
    const RETENTION_CONFIRMATION = 'DELETE_EXPIRED_DATA';
    const ALERT_ACK_KEY = 'taskAlertAckIds';       // sessionStorage
    const ALERTS_ENABLED_KEY = 'taskAlertsEnabled'; // localStorage

    const state = {
        userIsAdmin: false,
        currentTab: 'all',
        page: 1,
        pageSize: 20,
        totalPages: 1,
        taskTypeFilter: '',
        searchFilter: '',
        dateFrom: '',
        dateTo: '',
        tasksById: new Map(),  // current page only — for the details modal
        audioContext: null,
        alertsEnabled: false,
        monitoredJob: null,    // {taskId, timer, controller} while watching a retention job
        destroyed: false
    };

    // Every timer id and AbortController is tracked for page cleanup
    const timers = new Set();
    const controllers = new Set();

    // ------------------------------------------------------------------
    // Safe fetch + response parsing
    // ------------------------------------------------------------------
    function trackedController() {
        const c = new AbortController();
        controllers.add(c);
        return c;
    }

    async function apiFetch(url, options = {}) {
        const controller = options.controller || trackedController();
        try {
            const resp = await fetch(url, {
                method: options.method || 'GET',
                headers: {
                    Accept: 'application/json',
                    ...(options.body ? { 'Content-Type': 'application/json' } : {})
                },
                body: options.body ? JSON.stringify(options.body) : undefined,
                credentials: 'include',
                cache: 'no-store',
                signal: controller.signal
            });
            const payload = await parseResponse(resp);
            return { ok: resp.ok, status: resp.status, payload };
        } finally {
            controllers.delete(controller);
        }
    }

    /** Handles JSON, plain text, empty bodies, nginx HTML pages and 204. */
    async function parseResponse(resp) {
        if (resp.status === 204) return null;
        const ctype = (resp.headers.get('content-type') || '').toLowerCase();
        try {
            if (ctype.includes('application/json')) return await resp.json();
            const text = await resp.text();
            if (!text) return null;
            if (ctype.includes('text/html')) {
                return { detail: `Server returned an HTML error page (HTTP ${resp.status})` };
            }
            return { detail: text.slice(0, 300) };
        } catch (e) {
            return { detail: `Unreadable response (HTTP ${resp.status})` };
        }
    }

    function errorDetail(result, fallback) {
        const p = result && result.payload;
        if (p && typeof p.detail === 'string') return p.detail;
        if (p && p.detail) return JSON.stringify(p.detail).slice(0, 300);
        return fallback || `Request failed (HTTP ${result ? result.status : '?'})`;
    }

    // ------------------------------------------------------------------
    // Poller: recursive setTimeout + in-flight lock (never overlaps)
    // ------------------------------------------------------------------
    class Poller {
        constructor(name, fn, intervalMs) {
            this.name = name;
            this.fn = fn;
            this.intervalMs = intervalMs;
            this.timerId = null;
            this.inFlight = false;
            this.stopped = false;
        }
        start() {
            this.stopped = false;
            this._run();
        }
        async _run() {
            if (this.stopped || state.destroyed) return;
            if (!document.hidden && !this.inFlight) {
                this.inFlight = true;
                try { await this.fn(); }
                catch (e) { if (e && e.name !== 'AbortError') console.warn(`[${this.name}] poll error:`, e); }
                finally { this.inFlight = false; }
            }
            this._schedule();
        }
        _schedule() {
            if (this.stopped || state.destroyed) return;
            timers.delete(this.timerId);
            // Slow down 4x while the tab is hidden
            const delay = document.hidden ? this.intervalMs * 4 : this.intervalMs;
            this.timerId = setTimeout(() => this._run(), delay);
            timers.add(this.timerId);
        }
        /** Run now (if not already running), then continue the schedule. */
        kick() {
            if (this.inFlight) return;
            timers.delete(this.timerId);
            clearTimeout(this.timerId);
            this._run();
        }
        stop() {
            this.stopped = true;
            timers.delete(this.timerId);
            clearTimeout(this.timerId);
        }
    }

    let tasksPoller = null;
    let alertsPoller = null;
    let retentionPoller = null;

    // ------------------------------------------------------------------
    // Formatting helpers
    // ------------------------------------------------------------------
    function fmtDateTime(iso) {
        if (!iso) return '-';
        const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z');
        if (isNaN(d)) return '-';
        return d.toLocaleString('en-US', {
            year: 'numeric', month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
    }

    function fmtDuration(seconds) {
        if (seconds === null || seconds === undefined) return '-';
        if (seconds < 60) return `${Number(seconds).toFixed(1)}s`;
        if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m`;
        return `${(seconds / 3600).toFixed(1)}h`;
    }

    function fmtBytes(bytes) {
        if (bytes === null || bytes === undefined) return '-';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let v = Number(bytes), i = 0;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
    }

    function safeStatus(status) {
        return VALID_STATUSES.has(status) ? status : 'scheduled';
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    // ------------------------------------------------------------------
    // Notices (toast, aria-live)
    // ------------------------------------------------------------------
    function noticeArea() {
        let area = document.getElementById('bt-notice-area');
        if (!area) {
            area = el('div');
            area.id = 'bt-notice-area';
            area.setAttribute('role', 'status');
            area.setAttribute('aria-live', 'polite');
            document.body.appendChild(area);
        }
        return area;
    }

    function showNotice(message, kind = 'info', ms = 6000) {
        const area = noticeArea();
        const note = el('div', `bt-notice bt-notice-${kind}`);
        const icon = el('i', 'fas ' + (kind === 'error' ? 'fa-times-circle'
            : kind === 'success' ? 'fa-check-circle' : 'fa-info-circle'));
        icon.setAttribute('aria-hidden', 'true');
        note.appendChild(icon);
        note.appendChild(el('span', null, ' ' + message));
        area.appendChild(note);
        const t = setTimeout(() => { note.remove(); timers.delete(t); }, ms);
        timers.add(t);
    }

    // ------------------------------------------------------------------
    // Stats
    // ------------------------------------------------------------------
    async function loadStats() {
        const result = await apiFetch('/api/tasks/stats');
        const grid = document.getElementById('stats-grid');
        if (!grid) return;
        if (!result.ok) {
            if (result.status === 403) grid.replaceChildren();
            return;
        }
        renderStats(result.payload || {});
    }

    function statCard(label, value, extraClass, iconClass) {
        const card = el('div', 'stat-card' + (extraClass ? ' ' + extraClass : ''));
        const icon = el('div', 'stat-icon');
        const i = el('i', 'fas ' + iconClass);
        i.setAttribute('aria-hidden', 'true');
        icon.appendChild(i);
        card.appendChild(icon);
        card.appendChild(el('div', 'stat-label', label));
        card.appendChild(el('div', 'stat-value', value));
        return card;
    }

    function renderStats(stats) {
        const grid = document.getElementById('stats-grid');
        grid.replaceChildren(
            statCard('Completed', stats.completed || 0, 'completed', 'fa-check-circle'),
            statCard('Failed', stats.failed || 0, 'failed', 'fa-times-circle'),
            statCard('Running', stats.running || 0, 'running', 'fa-spinner fa-spin'),
            statCard('Upcoming', stats.upcoming || 0, 'scheduled', 'fa-clock'),
            statCard('Overdue', stats.overdue || 0, 'overdue', 'fa-exclamation-triangle'),
            statCard('Success Rate', `${(stats.success_rate || 0).toFixed(1)}%`, '', 'fa-chart-line')
        );
    }

    // ------------------------------------------------------------------
    // Task history (server-side pagination)
    // ------------------------------------------------------------------
    async function loadTasks() {
        const loading = document.getElementById('loading-indicator');
        if (loading && state.tasksById.size === 0) loading.style.display = 'block';

        const params = new URLSearchParams({
            page: String(state.page),
            page_size: String(state.pageSize),
            sort_by: 'created_at',
            sort_order: 'desc'
        });
        const status = TAB_TO_STATUS[state.currentTab];
        if (status) params.set('status', status);
        if (state.taskTypeFilter) params.set('task_type', state.taskTypeFilter);
        if (state.searchFilter) params.set('search', state.searchFilter);
        if (state.dateFrom) params.set('date_from', state.dateFrom);
        if (state.dateTo) params.set('date_to', state.dateTo + 'T23:59:59');

        const result = await apiFetch(`/api/tasks/history?${params}`);
        if (loading) loading.style.display = 'none';
        if (!result.ok) {
            showNotice(`Failed to load tasks: ${errorDetail(result)}`, 'error');
            return;
        }
        renderTasks(result.payload || { items: [], total: 0, page: 1, total_pages: 1 });
    }

    function renderTasks(pageData) {
        const tbody = document.getElementById('tasks-tbody');
        const tableWrap = document.getElementById('tasks-table-container');
        const emptyState = document.getElementById('empty-state');
        const pagination = document.getElementById('pagination-container');
        if (!tbody) return;

        state.totalPages = pageData.total_pages || 1;
        state.page = pageData.page || 1;
        state.tasksById.clear();

        const items = Array.isArray(pageData.items) ? pageData.items : [];
        if (items.length === 0) {
            tableWrap.style.display = 'none';
            emptyState.style.display = 'block';
            pagination.style.display = 'none';
            return;
        }
        tableWrap.style.display = 'block';
        emptyState.style.display = 'none';

        const rows = items.map(task => {
            state.tasksById.set(task.id, task);
            const status = safeStatus(task.effective_status || task.status);

            const tr = el('tr');

            const nameTd = el('td');
            nameTd.appendChild(el('strong', null, task.task_name || '(unnamed)'));
            if (task.description) {
                nameTd.appendChild(el('br'));
                nameTd.appendChild(el('small', 'task-desc', task.description));
            }
            tr.appendChild(nameTd);

            const typeTd = el('td');
            typeTd.appendChild(el('span', 'task-type-badge', task.task_type || '-'));
            tr.appendChild(typeTd);

            const statusTd = el('td');
            const badge = el('span', `status-badge ${status}`);
            const icon = el('i', 'fas ' + (STATUS_ICONS[status] || 'fa-question'));
            icon.setAttribute('aria-hidden', 'true');
            badge.appendChild(icon);
            badge.appendChild(el('span', null, ' ' + status));
            statusTd.appendChild(badge);
            if (status === 'running' && task.progress_percent !== null && task.progress_percent !== undefined) {
                const bar = el('div', 'task-progress');
                bar.setAttribute('role', 'progressbar');
                bar.setAttribute('aria-valuenow', String(task.progress_percent));
                bar.setAttribute('aria-valuemin', '0');
                bar.setAttribute('aria-valuemax', '100');
                const fill = el('div', 'task-progress-fill');
                fill.style.width = `${Math.max(0, Math.min(100, task.progress_percent))}%`;
                bar.appendChild(fill);
                statusTd.appendChild(bar);
                statusTd.appendChild(el('small', 'task-progress-label', `${task.progress_percent}%`));
            }
            tr.appendChild(statusTd);

            tr.appendChild(el('td', null, fmtDateTime(task.scheduled_time)));
            tr.appendChild(el('td', null, fmtDateTime(task.started_at)));
            tr.appendChild(el('td', null, fmtDateTime(task.completed_at)));
            tr.appendChild(el('td', null, fmtDuration(task.duration_seconds)));

            const actionsTd = el('td');
            const detailsBtn = el('button', 'details-btn');
            detailsBtn.type = 'button';
            detailsBtn.dataset.taskId = String(task.id);
            detailsBtn.dataset.action = 'details';
            detailsBtn.setAttribute('aria-label', `View details of ${task.task_name || 'task'}`);
            const dIcon = el('i', 'fas fa-info-circle');
            dIcon.setAttribute('aria-hidden', 'true');
            detailsBtn.appendChild(dIcon);
            detailsBtn.appendChild(el('span', null, ' View'));
            actionsTd.appendChild(detailsBtn);
            tr.appendChild(actionsTd);

            return tr;
        });
        tbody.replaceChildren(...rows);

        document.getElementById('current-page').textContent = String(state.page);
        document.getElementById('total-pages').textContent = String(state.totalPages);
        document.getElementById('total-count').textContent = String(pageData.total || 0);
        document.getElementById('prev-page-btn').disabled = state.page <= 1;
        document.getElementById('next-page-btn').disabled = state.page >= state.totalPages;
        pagination.style.display = 'flex';
    }

    // ------------------------------------------------------------------
    // Task details modal (accessible — no window.alert)
    // ------------------------------------------------------------------
    let modalOpener = null;

    async function openTaskDetails(taskId) {
        // Always re-fetch: the row cache may be stale for running jobs
        const result = await apiFetch(`/api/tasks/${taskId}`);
        const task = result.ok ? result.payload : state.tasksById.get(taskId);
        if (!task) {
            showNotice(`Could not load task ${taskId}: ${errorDetail(result)}`, 'error');
            return;
        }
        renderTaskModal(task);
    }

    function detailRow(dl, label, value) {
        if (value === undefined || value === null || value === '') return;
        dl.appendChild(el('dt', null, label));
        dl.appendChild(el('dd', null, String(value)));
    }

    function renderTaskModal(task) {
        const modal = document.getElementById('task-modal');
        const body = document.getElementById('task-modal-body');
        const title = document.getElementById('task-modal-title-text');
        if (!modal || !body) return;

        title.textContent = task.task_name || `Task #${task.id}`;
        body.replaceChildren();

        const status = safeStatus(task.effective_status || task.status);
        const badge = el('span', `status-badge ${status}`, status.toUpperCase());
        body.appendChild(badge);

        const dl = el('dl', 'task-detail-list');
        detailRow(dl, 'Task ID', task.id);
        detailRow(dl, 'Job ID', task.job_id);
        detailRow(dl, 'Type', task.task_type);
        detailRow(dl, 'Status', status + (task.is_overdue ? ' (scheduled time has passed)' : ''));
        detailRow(dl, 'Description', task.description);
        detailRow(dl, 'Scheduled', fmtDateTime(task.scheduled_time));
        detailRow(dl, 'Started', fmtDateTime(task.started_at));
        detailRow(dl, 'Completed', fmtDateTime(task.completed_at));
        detailRow(dl, 'Duration', fmtDuration(task.duration_seconds));
        if (task.progress_percent !== null && task.progress_percent !== undefined) {
            detailRow(dl, 'Progress', `${task.progress_percent}%`);
        }
        if (task.retry_count) detailRow(dl, 'Retries', `${task.retry_count} of ${task.max_retries || '-'}`);
        detailRow(dl, 'Created by user', task.created_by_user_id);
        detailRow(dl, 'Worker', task.worker_name);
        detailRow(dl, 'Host', task.hostname);
        detailRow(dl, 'Request ID', task.request_id);
        detailRow(dl, 'Correlation ID', task.correlation_id);
        detailRow(dl, 'Error code', task.error_code);
        body.appendChild(dl);

        if (task.error_message) {
            const err = el('div', 'task-error-box');
            err.appendChild(el('strong', null, 'Error: '));
            err.appendChild(el('span', null, task.error_message));
            body.appendChild(err);
        }

        const structured = task.result || task.details;
        if (structured && typeof structured === 'object') {
            body.appendChild(el('h4', 'task-json-heading', task.result ? 'Result' : 'Details'));
            // Friendly summary for retention results
            if (structured.rows_deleted !== undefined || structured.candidate_rows !== undefined) {
                const summary = el('ul', 'task-result-summary');
                const add = (label, v) => {
                    if (v !== undefined && v !== null) {
                        const li = el('li');
                        li.appendChild(el('strong', null, label + ': '));
                        li.appendChild(el('span', null, String(v)));
                        summary.appendChild(li);
                    }
                };
                add('Dry run', structured.dry_run);
                add('Rows scanned', structured.rows_scanned);
                add('Candidate rows', structured.candidate_rows);
                add('Rows deleted', structured.rows_deleted);
                add('Files deleted', structured.files_deleted);
                add('Missing files', structured.missing_files);
                add('Batches', structured.batch_count);
                if (structured.bytes_freed !== undefined) add('Space freed', fmtBytes(structured.bytes_freed));
                if (structured.estimated_freed_bytes !== undefined) add('Estimated space', fmtBytes(structured.estimated_freed_bytes));
                body.appendChild(summary);
            }
            const pre = el('pre', 'task-json');
            pre.textContent = JSON.stringify(structured, null, 2);
            body.appendChild(pre);
        }

        // Admin actions
        const actions = el('div', 'task-modal-actions');
        if (state.userIsAdmin && (task.status === 'scheduled' || task.status === 'running')) {
            const cancelBtn = el('button', 'military-btn secondary-btn');
            cancelBtn.type = 'button';
            cancelBtn.textContent = task.status === 'running' ? 'Request Cancel' : 'Cancel Task';
            cancelBtn.addEventListener('click', () => taskAction(task.id, 'cancel', cancelBtn));
            actions.appendChild(cancelBtn);
        }
        if (state.userIsAdmin && ['failed', 'cancelled'].includes(task.status) && task.task_type === 'data_retention') {
            const retryBtn = el('button', 'military-btn primary-btn');
            retryBtn.type = 'button';
            retryBtn.textContent = 'Retry';
            retryBtn.addEventListener('click', () => taskAction(task.id, 'retry', retryBtn));
            actions.appendChild(retryBtn);
        }
        if (actions.childElementCount) body.appendChild(actions);

        modalOpener = document.activeElement;
        modal.style.display = 'flex';
        modal.setAttribute('aria-hidden', 'false');
        document.getElementById('task-modal-close').focus();
    }

    function closeTaskModal() {
        const modal = document.getElementById('task-modal');
        if (!modal) return;
        modal.style.display = 'none';
        modal.setAttribute('aria-hidden', 'true');
        if (modalOpener && typeof modalOpener.focus === 'function') modalOpener.focus();
        modalOpener = null;
    }

    async function taskAction(taskId, action, btn) {
        if (btn) btn.disabled = true;
        try {
            const result = await apiFetch(`/api/tasks/${taskId}/${action}`, { method: 'POST' });
            if (result.ok) {
                showNotice(action === 'cancel'
                    ? `Cancel ${result.payload && result.payload.outcome === 'cancel_requested' ? 'requested — the job stops at the next batch' : 'done'}.`
                    : `Retry scheduled (job ${result.payload && result.payload.job_id ? result.payload.job_id : ''}).`, 'success');
                closeTaskModal();
                tasksPoller.kick();
            } else {
                showNotice(`${action} failed: ${errorDetail(result)}`, 'error');
            }
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    // ------------------------------------------------------------------
    // Alerts: opt-in audio, shared AudioContext, stable dedup
    // ------------------------------------------------------------------
    function getAckedAlerts() {
        try { return new Set(JSON.parse(sessionStorage.getItem(ALERT_ACK_KEY) || '[]')); }
        catch (e) { return new Set(); }
    }

    function ackAlert(instanceId) {
        const acked = getAckedAlerts();
        acked.add(instanceId);
        // cap storage
        const arr = Array.from(acked).slice(-200);
        try { sessionStorage.setItem(ALERT_ACK_KEY, JSON.stringify(arr)); } catch (e) { /* full */ }
    }

    async function loadAlerts() {
        const result = await apiFetch('/api/tasks/alerts');
        if (!result.ok || !Array.isArray(result.payload)) return;
        const acked = getAckedAlerts();
        for (const alert of result.payload) {
            const id = alert.alert_instance_id || `${alert.task_id}:${alert.scheduled_time}:starting_soon`;
            if (acked.has(id)) continue;
            ackAlert(id);
            showAlertNotification(alert);
            playAlertSound();
        }
    }

    function ensureAudioContext() {
        if (!state.audioContext) {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return null;
            state.audioContext = new Ctx();
        }
        if (state.audioContext.state === 'suspended') {
            state.audioContext.resume().catch(() => {});
        }
        return state.audioContext;
    }

    function beep(ctx, when) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.frequency.value = 800;
        osc.type = 'sine';
        gain.gain.setValueAtTime(0.3, when);
        gain.gain.exponentialRampToValueAtTime(0.01, when + 0.2);
        osc.start(when);
        osc.stop(when + 0.2);
    }

    function playAlertSound() {
        if (!state.alertsEnabled) return;
        try {
            const ctx = ensureAudioContext();
            if (!ctx) return;
            beep(ctx, ctx.currentTime);
            beep(ctx, ctx.currentTime + 0.3);
        } catch (e) {
            console.warn('[TASK_ALERTS] Could not play sound:', e);
        }
    }

    function showAlertNotification(alert) {
        const note = el('div', 'bt-notice bt-notice-warn bt-alert');
        const icon = el('i', 'fas fa-bell');
        icon.setAttribute('aria-hidden', 'true');
        note.appendChild(icon);
        const textWrap = el('div');
        textWrap.appendChild(el('strong', null, ' Task starting soon: '));
        textWrap.appendChild(el('span', null, alert.task_name || alert.task_type || 'background task'));
        const when = alert.scheduled_time ? new Date(alert.scheduled_time).toLocaleTimeString() : 'soon';
        textWrap.appendChild(el('small', 'bt-alert-time', ` at ${when}`));
        note.appendChild(textWrap);
        noticeArea().appendChild(note);
        const t = setTimeout(() => { note.remove(); timers.delete(t); }, 8000);
        timers.add(t);
    }

    function updateAlertsButton() {
        const btn = document.getElementById('enable-alerts-btn');
        if (!btn) return;
        btn.classList.toggle('alerts-on', state.alertsEnabled);
        btn.querySelector('span').textContent = state.alertsEnabled ? 'Task Alerts: ON' : 'Enable Task Alerts';
        btn.setAttribute('aria-pressed', state.alertsEnabled ? 'true' : 'false');
    }

    function toggleAlerts() {
        state.alertsEnabled = !state.alertsEnabled;
        try { localStorage.setItem(ALERTS_ENABLED_KEY, state.alertsEnabled ? '1' : '0'); } catch (e) { /* ignore */ }
        if (state.alertsEnabled) {
            // Must be created during the user gesture to satisfy autoplay policy
            const ctx = ensureAudioContext();
            if (ctx) beep(ctx, ctx.currentTime); // confirmation blip
            showNotice('Task alerts enabled — you will hear a sound before tasks start.', 'success');
        } else if (state.audioContext) {
            state.audioContext.suspend().catch(() => {});
        }
        updateAlertsButton();
    }

    // ------------------------------------------------------------------
    // Retention panel
    // ------------------------------------------------------------------
    async function loadRetentionStatus() {
        const result = await apiFetch('/api/admin/retention/status');
        const panel = document.getElementById('retention-panel');
        if (!panel) return;
        if (!result.ok) {
            if (result.status === 403) panel.style.display = 'none';
            return;
        }
        panel.style.display = 'block';
        renderRetentionStatus(result.payload || {});
    }

    function retentionStat(label, value) {
        const cell = el('div', 'retention-stat');
        cell.appendChild(el('div', 'retention-stat-label', label));
        cell.appendChild(el('div', 'retention-stat-value', value === null || value === undefined ? '-' : value));
        return cell;
    }

    function renderRetentionStatus(s) {
        const grid = document.getElementById('retention-status-grid');
        if (!grid) return;
        const stored = s.stored_retention_days;
        const effective = s.effective_retention_days;
        grid.replaceChildren(
            retentionStat('Stored retention', stored !== null && stored !== undefined ? `${stored} days` : '-'),
            retentionStat('Effective retention', effective !== null && effective !== undefined ? `${effective} days` : '-'),
            retentionStat('Source', s.source),
            retentionStat('Apply mode', s.apply_mode),
            retentionStat('Cleanup interval', s.cleanup_interval_seconds ? fmtDuration(s.cleanup_interval_seconds) : '-'),
            retentionStat('Currently running', s.currently_running ? 'YES' : 'no'),
            retentionStat('Last run', s.last_run_at ? fmtDateTime(s.last_run_at) : 'never (since restart)'),
            retentionStat('Last status', s.last_run_status),
            retentionStat('Last rows deleted', s.last_deleted_rows),
            retentionStat('Last files deleted', s.last_deleted_files),
            retentionStat('Last space freed', s.last_freed_bytes !== null && s.last_freed_bytes !== undefined ? fmtBytes(s.last_freed_bytes) : '-'),
            retentionStat('Next run', s.next_run_at ? fmtDateTime(s.next_run_at) : '-')
        );
        const errBox = document.getElementById('retention-errors');
        if (errBox) {
            if (s.last_errors && s.last_errors.length) {
                errBox.style.display = 'block';
                errBox.replaceChildren(el('strong', null, 'Last run errors: '));
                s.last_errors.forEach(e2 => errBox.appendChild(el('div', null, String(e2))));
            } else {
                errBox.style.display = 'none';
            }
        }
    }

    let retentionActionInFlight = false;

    async function runRetention(dryRun) {
        if (retentionActionInFlight) return;
        retentionActionInFlight = true;
        const dryBtn = document.getElementById('retention-dryrun-btn');
        const runBtn = document.getElementById('retention-run-btn');
        if (dryBtn) dryBtn.disabled = true;
        if (runBtn) runBtn.disabled = true;
        try {
            const body = dryRun ? { dry_run: true } : { dry_run: false, confirmation: RETENTION_CONFIRMATION };
            const result = await apiFetch('/api/admin/retention/run', { method: 'POST', body });
            if (result.status === 409) {
                showNotice('A retention run is already in progress.', 'error');
                return;
            }
            if (!result.ok || !result.payload || !result.payload.accepted) {
                showNotice(`Retention ${dryRun ? 'dry run' : 'run'} failed: ${errorDetail(result)}`, 'error');
                return;
            }
            showNotice(`${dryRun ? 'Dry run' : 'Cleanup'} scheduled (job ${result.payload.job_id}). Monitoring…`, 'info');
            monitorRetentionJob(result.payload.task_id);
        } finally {
            retentionActionInFlight = false;
            if (dryBtn) dryBtn.disabled = false;
            if (runBtn) runBtn.disabled = false;
        }
    }

    /** Poll ONLY the single job (2s, max 5 min) — not the whole history. */
    function monitorRetentionJob(taskId) {
        stopJobMonitor();
        const box = document.getElementById('retention-job-monitor');
        if (box) {
            box.style.display = 'block';
            box.replaceChildren(el('span', null, `Job #${taskId}: starting…`));
        }
        let polls = 0;
        const poll = async () => {
            if (state.destroyed) return;
            polls += 1;
            const result = await apiFetch(`/api/tasks/${taskId}`).catch(() => null);
            const task = result && result.ok ? result.payload : null;
            if (task && box) {
                const bits = [`Job #${taskId}: ${safeStatus(task.effective_status || task.status)}`];
                if (task.status === 'running' && task.progress_percent !== null && task.progress_percent !== undefined) {
                    bits.push(`${task.progress_percent}%`);
                    if (task.details && task.details.processed_rows !== undefined) {
                        bits.push(`${task.details.processed_rows}/${task.details.total_rows} rows`);
                    }
                }
                box.replaceChildren(el('span', null, bits.join(' — ')));
            }
            if (task && ['completed', 'failed', 'cancelled'].includes(task.status)) {
                const r = task.result || {};
                const summary = task.status === 'completed'
                    ? (r.dry_run
                        ? `Dry run: ${r.candidate_rows ?? '?'} candidate rows, ${r.existing_files ?? 0} files (${fmtBytes(r.estimated_freed_bytes || 0)}). Nothing deleted.`
                        : `Cleanup: deleted ${r.rows_deleted ?? 0} rows, ${r.files_deleted ?? 0} files (${fmtBytes(r.bytes_freed || 0)}), ${r.missing_files ?? 0} missing.`)
                    : `Job ${task.status}${task.error_message ? ': ' + task.error_message : ''}`;
                showNotice(summary, task.status === 'completed' ? 'success' : 'error', 10000);
                if (box) box.replaceChildren(el('span', null, summary));
                stopJobMonitor();
                retentionPoller.kick();
                tasksPoller.kick();
                return;
            }
            if (polls >= 150) { stopJobMonitor(); return; } // 5 min cap
            const t = setTimeout(poll, 2000);
            timers.add(t);
            state.monitoredJob = { taskId, timer: t };
        };
        poll();
    }

    function stopJobMonitor() {
        if (state.monitoredJob && state.monitoredJob.timer) {
            clearTimeout(state.monitoredJob.timer);
            timers.delete(state.monitoredJob.timer);
        }
        state.monitoredJob = null;
    }

    // Real-run confirmation dialog (typed confirmation — no accidental click)
    function openRetentionConfirm() {
        const modal = document.getElementById('retention-confirm-modal');
        const input = document.getElementById('retention-confirm-input');
        const btn = document.getElementById('retention-confirm-go');
        if (!modal || !input || !btn) return;
        input.value = '';
        btn.disabled = true;
        modalOpener = document.activeElement;
        modal.style.display = 'flex';
        modal.setAttribute('aria-hidden', 'false');
        input.focus();
    }

    function closeRetentionConfirm() {
        const modal = document.getElementById('retention-confirm-modal');
        if (!modal) return;
        modal.style.display = 'none';
        modal.setAttribute('aria-hidden', 'true');
        if (modalOpener && typeof modalOpener.focus === 'function') modalOpener.focus();
        modalOpener = null;
    }

    // ------------------------------------------------------------------
    // Event wiring (delegated; no inline handlers)
    // ------------------------------------------------------------------
    function setupEventListeners() {
        document.getElementById('refresh-btn').addEventListener('click', () => {
            tasksPoller.kick();
            alertsPoller.kick();
            if (state.userIsAdmin) retentionPoller.kick();
            loadStats().catch(() => {});
        });

        document.querySelector('.tabs-container').addEventListener('click', (e) => {
            const btn = e.target.closest('.tab-btn');
            if (!btn) return;
            const tab = btn.getAttribute('data-tab');
            if (!tab || !(tab in TAB_TO_STATUS)) return;
            state.currentTab = tab;
            state.page = 1;
            document.querySelectorAll('.tab-btn').forEach(b => {
                b.classList.toggle('active', b === btn);
                b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
            });
            tasksPoller.kick();
        });

        document.getElementById('task-type-filter').addEventListener('change', (e) => {
            state.taskTypeFilter = e.target.value;
            state.page = 1;
            tasksPoller.kick();
        });

        let searchDebounce = null;
        document.getElementById('task-search').addEventListener('input', (e) => {
            clearTimeout(searchDebounce);
            timers.delete(searchDebounce);
            searchDebounce = setTimeout(() => {
                state.searchFilter = e.target.value.trim();
                state.page = 1;
                tasksPoller.kick();
            }, 400);
            timers.add(searchDebounce);
        });

        document.getElementById('date-from').addEventListener('change', (e) => {
            state.dateFrom = e.target.value;
            state.page = 1;
            tasksPoller.kick();
        });
        document.getElementById('date-to').addEventListener('change', (e) => {
            state.dateTo = e.target.value;
            state.page = 1;
            tasksPoller.kick();
        });

        document.getElementById('clear-filters-btn').addEventListener('click', () => {
            state.taskTypeFilter = '';
            state.searchFilter = '';
            state.dateFrom = '';
            state.dateTo = '';
            state.page = 1;
            document.getElementById('task-type-filter').value = '';
            document.getElementById('task-search').value = '';
            document.getElementById('date-from').value = '';
            document.getElementById('date-to').value = '';
            tasksPoller.kick();
        });

        document.getElementById('prev-page-btn').addEventListener('click', () => {
            if (state.page > 1) { state.page -= 1; tasksPoller.kick(); }
        });
        document.getElementById('next-page-btn').addEventListener('click', () => {
            if (state.page < state.totalPages) { state.page += 1; tasksPoller.kick(); }
        });

        // Details buttons (delegated)
        document.getElementById('tasks-tbody').addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-action="details"]');
            if (!btn) return;
            const id = parseInt(btn.dataset.taskId, 10);
            if (Number.isFinite(id)) openTaskDetails(id).catch(() => {});
        });

        // Task modal close
        document.getElementById('task-modal-close').addEventListener('click', closeTaskModal);
        document.getElementById('task-modal').addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closeTaskModal();
        });

        // Alerts opt-in
        document.getElementById('enable-alerts-btn').addEventListener('click', toggleAlerts);

        // Retention controls
        const statusBtn = document.getElementById('retention-status-btn');
        if (statusBtn) statusBtn.addEventListener('click', () => retentionPoller.kick());
        const dryBtn = document.getElementById('retention-dryrun-btn');
        if (dryBtn) dryBtn.addEventListener('click', () => runRetention(true));
        const runBtn = document.getElementById('retention-run-btn');
        if (runBtn) runBtn.addEventListener('click', openRetentionConfirm);

        const confirmInput = document.getElementById('retention-confirm-input');
        const confirmGo = document.getElementById('retention-confirm-go');
        if (confirmInput && confirmGo) {
            confirmInput.addEventListener('input', () => {
                confirmGo.disabled = confirmInput.value.trim() !== RETENTION_CONFIRMATION;
            });
            confirmGo.addEventListener('click', () => {
                closeRetentionConfirm();
                runRetention(false);
            });
        }
        const confirmCancel = document.getElementById('retention-confirm-cancel');
        if (confirmCancel) confirmCancel.addEventListener('click', closeRetentionConfirm);
        const confirmModal = document.getElementById('retention-confirm-modal');
        if (confirmModal) confirmModal.addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closeRetentionConfirm();
        });

        // Escape closes whichever dialog is open
        document.addEventListener('keydown', onKeydown);

        // Visibility: pause hidden, refresh immediately on return
        document.addEventListener('visibilitychange', onVisibilityChange);

        // Full page cleanup
        window.addEventListener('pagehide', destroy);
        window.addEventListener('beforeunload', destroy);
    }

    function onKeydown(e) {
        if (e.key !== 'Escape') return;
        const confirmModal = document.getElementById('retention-confirm-modal');
        if (confirmModal && confirmModal.style.display !== 'none' && confirmModal.style.display !== '') {
            closeRetentionConfirm();
            return;
        }
        const taskModal = document.getElementById('task-modal');
        if (taskModal && taskModal.style.display !== 'none' && taskModal.style.display !== '') {
            closeTaskModal();
        }
    }

    function onVisibilityChange() {
        if (!document.hidden) {
            tasksPoller.kick();
            alertsPoller.kick();
            if (state.userIsAdmin) retentionPoller.kick();
        }
    }

    function destroy() {
        if (state.destroyed) return;
        state.destroyed = true;
        if (tasksPoller) tasksPoller.stop();
        if (alertsPoller) alertsPoller.stop();
        if (retentionPoller) retentionPoller.stop();
        stopJobMonitor();
        for (const t of timers) clearTimeout(t);
        timers.clear();
        for (const c of controllers) { try { c.abort(); } catch (e) { /* ignore */ } }
        controllers.clear();
        if (state.audioContext) {
            try { state.audioContext.close(); } catch (e) { /* ignore */ }
            state.audioContext = null;
        }
        document.removeEventListener('keydown', onKeydown);
        document.removeEventListener('visibilitychange', onVisibilityChange);
    }

    // ------------------------------------------------------------------
    // Init
    // ------------------------------------------------------------------
    async function checkUserRole() {
        try {
            const result = await apiFetch('/api/auth/me/privileges');
            if (result.ok && result.payload) state.userIsAdmin = result.payload.role === 'admin';
        } catch (e) {
            console.error('Error checking user role:', e);
        }
    }

    document.addEventListener('DOMContentLoaded', async () => {
        try { state.alertsEnabled = localStorage.getItem(ALERTS_ENABLED_KEY) === '1'; } catch (e) { /* ignore */ }
        updateAlertsButton();

        await checkUserRole();

        // Stats+history poll together (30s); alerts have their OWN single
        // poller (15s); retention status refreshes every 60s (admin only).
        tasksPoller = new Poller('tasks', async () => {
            await Promise.all([loadTasks(), loadStats()]);
        }, 30000);
        alertsPoller = new Poller('alerts', loadAlerts, 15000);
        retentionPoller = new Poller('retention', loadRetentionStatus, 60000);

        setupEventListeners();

        tasksPoller.start();
        alertsPoller.start();
        if (state.userIsAdmin) {
            retentionPoller.start();
        } else {
            const panel = document.getElementById('retention-panel');
            if (panel) panel.style.display = 'none';
        }
    });
})();
