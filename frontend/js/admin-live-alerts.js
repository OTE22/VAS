/**
 * Live Alerts Management
 * ======================
 * Security & reliability rules implemented here:
 *  - IDs normalized with one helper (numeric/UUID backend ids vs string DOM
 *    attributes can never mismatch silently)
 *  - zero HTML interpolation of untrusted values: DOM built with
 *    createElement/textContent/setAttribute, no inline onclick
 *  - image URLs pass an allowlist (local /storage paths or the built-in
 *    data-URI fallback); unsafe schemes are rejected
 *  - one shared request helper: credentials, no-store, Accept, timeout,
 *    AbortController, safe body parsing, auth-expiry handling, and the
 *    X-Requested-With CSRF header on every mutating call
 *  - per-action request locks + disabled buttons (no double submits)
 *  - ONE debounced alerts refresh per WebSocket burst (never N reloads)
 *  - bulk acknowledgement through one backend call (never N+1)
 *  - server-side trigger pagination — the modal loads only its page
 *  - single WebSocket with full lifecycle: no duplicate connects, stored
 *    reconnect timer, bounded exponential backoff + jitter, heartbeat,
 *    online/offline + visibility handling, intentional-close flag,
 *    visible connection status (incl. "Authentication failed")
 *  - strict WS message validation + bounded event-id dedup cache
 *  - popup STACK (simultaneous alerts queue instead of overwriting)
 *  - one shared AudioContext behind an explicit "Enable Alert Sound" button
 *    with honest Enabled/Disabled/Blocked-by-browser status
 *  - periodic API reconciliation (missed WS events are recovered)
 *  - full cleanup on page unload
 */
'use strict';

(() => {
    // ------------------------------------------------------------------
    // Constants & state
    // ------------------------------------------------------------------
    const VALID_ALERT_STATUSES = new Set(['active', 'paused', 'disabled', 'expired', 'triggered']);
    const FALLBACK_AVATAR = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="100" height="100"%3E%3Crect fill="%23333" width="100" height="100"/%3E%3Ctext fill="%23999" x="50" y="50" text-anchor="middle" dominant-baseline="middle" font-size="40"%3E%3F%3C/text%3E%3C/svg%3E';
    const SOUND_PREF_KEY = 'liveAlertSoundEnabled';
    const MAX_VISIBLE_POPUPS = 3;
    const EVENT_CACHE_LIMIT = 300;

    const state = {
        alerts: [],
        role: null,
        isAdmin: false,
        soundEnabled: false,
        audioCtx: null,
        destroyed: false,
        // WS event dedup: bounded FIFO of event ids
        seenEvents: new Set(),
        seenEventOrder: [],
        popupQueue: [],
        visiblePopups: 0,
        refreshTimer: null,
        modal: null,   // triggers-modal state {alertId, page, filter, opener}
    };

    const locks = new Set();
    const timers = new Set();
    const controllers = new Set();

    // ------------------------------------------------------------------
    // Small safe helpers
    // ------------------------------------------------------------------

    /** The one true ID comparison: numeric backend ids survive string DOM round-trips. */
    function idsEqual(a, b) {
        return a !== null && a !== undefined && b !== null && b !== undefined
            && String(a) === String(b);
    }

    function findAlert(alertId) {
        return state.alerts.find(a => idsEqual(a.id, alertId)) || null;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function icon(cls) {
        const i = el('i', 'fas ' + cls);
        i.setAttribute('aria-hidden', 'true');
        return i;
    }

    function safeStatus(status) {
        return VALID_ALERT_STATUSES.has(status) ? status : 'unknown';
    }

    /** Allow only local /storage paths (or our own fallback). Reject javascript:,
     *  external hosts, protocol-relative and every other unexpected scheme. */
    function safeImageUrl(url) {
        if (typeof url !== 'string' || !url) return FALLBACK_AVATAR;
        if (url === FALLBACK_AVATAR) return url;
        if (/^data:image\/svg\+xml,/.test(url)) return FALLBACK_AVATAR; // only OUR fallback data-URI
        let path = url.trim();
        if (/^https?:\/\//i.test(path)) {
            try {
                const u = new URL(path);
                if (u.host !== window.location.host) return FALLBACK_AVATAR;
                path = u.pathname;
            } catch (e) { return FALLBACK_AVATAR; }
        }
        if (path.startsWith('//')) return FALLBACK_AVATAR;
        if (!path.startsWith('/')) path = '/' + path;
        if (/^\/(storage|frontend\/images)\//.test(path) && !path.includes('..')) return path;
        return FALLBACK_AVATAR;
    }

    /** Clamp to [0,1]; null for anything non-numeric (never NaN in the UI). */
    function clampSimilarity(v) {
        const n = Number(v);
        if (!Number.isFinite(n)) return null;
        return Math.min(1, Math.max(0, n));
    }

    function similarityLabel(v) {
        const s = clampSimilarity(v);
        return s === null ? '—' : `${Math.round(s * 100)}%`;
    }

    // Confidence band boundaries, fetched from GET /api/search/config.
    //
    // These were hard-coded 0.8 / 0.6 — numbers that matched NO backend
    // boundary (CONFIDENCE_HIGH_MIN is 0.75, CONFIDENCE_MEDIUM_MIN is 0.60),
    // so this page painted a 0.78 match "medium" while the search page called
    // the same score "High". Null until loaded; the render falls back to the
    // neutral class rather than inventing a threshold.
    const bandThresholds = { high: null, medium: null };

    async function loadBandThresholds() {
        try {
            const response = await fetch('/api/search/config', { credentials: 'same-origin' });
            if (!response.ok) return;
            const config = await response.json();
            const high = Number(config?.confidence_bands?.HIGH?.min);
            const medium = Number(config?.confidence_bands?.MEDIUM?.min);
            if (Number.isFinite(high)) bandThresholds.high = high;
            if (Number.isFinite(medium)) bandThresholds.medium = medium;
        } catch (err) {
            console.warn('[LIVE_ALERTS] Confidence bands unavailable:', err);
        }
    }

    function similarityClass(v) {
        const s = clampSimilarity(v);
        if (s === null) return 'low';
        if (bandThresholds.high === null) return 'low';
        if (s >= bandThresholds.high) return 'high';
        if (bandThresholds.medium !== null && s >= bandThresholds.medium) return 'medium';
        return 'low';
    }

    /** Invalid dates -> 'Unknown'; future times never claim 'Just now'. */
    function fmtTime(isoString) {
        if (!isoString) return 'Never';
        const date = new Date(isoString);
        if (isNaN(date.getTime())) return 'Unknown';
        const diff = Date.now() - date.getTime();
        if (diff < -60000) return date.toLocaleString(); // future: absolute, not "Just now"
        if (diff < 60000) return 'Just now';
        if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
        if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
        return date.toLocaleString();
    }

    // ------------------------------------------------------------------
    // Shared request helper
    // ------------------------------------------------------------------
    const MUTATING = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

    async function api(url, options = {}) {
        const method = (options.method || 'GET').toUpperCase();
        const controller = new AbortController();
        controllers.add(controller);
        const timeoutMs = options.timeoutMs || 15000;
        const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
        timers.add(timeoutId);
        try {
            const headers = { Accept: 'application/json' };
            if (MUTATING.has(method)) {
                // CSRF defense-in-depth: cross-site pages cannot set custom headers
                headers['X-Requested-With'] = 'XMLHttpRequest';
            }
            if (options.body !== undefined) headers['Content-Type'] = 'application/json';
            const resp = await fetch(url, {
                method,
                headers,
                body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
                credentials: 'include',
                cache: 'no-store',
                signal: controller.signal
            });
            const payload = await parseResponse(resp);
            if (resp.status === 401) handleAuthExpired();
            return { ok: resp.ok, status: resp.status, payload };
        } catch (err) {
            // destroy() aborts every in-flight request on page teardown; that
            // abort is intentional and must not surface as an unhandled
            // rejection ("signal is aborted without reason"). A timeout abort
            // (page still alive) keeps propagating so callers report it.
            if (err && err.name === 'AbortError' && state.destroyed) {
                return { ok: false, status: 0, payload: null, aborted: true };
            }
            throw err;
        } finally {
            clearTimeout(timeoutId);
            timers.delete(timeoutId);
            controllers.delete(controller);
        }
    }

    /** Safe parsing for JSON, text, nginx HTML pages, empty bodies and 204. */
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
        return fallback || `Request failed (HTTP ${result ? result.status : 'network'})`;
    }

    let authExpiredNotified = false;
    function handleAuthExpired() {
        if (authExpiredNotified) return;
        authExpiredNotified = true;
        setWsStatus('auth_failed');
        showNotification('Session expired — please log in again.', 'error');
    }

    /** Per-action request lock + optional button disabling. */
    async function withLock(key, btn, fn) {
        if (locks.has(key)) return null;
        locks.add(key);
        if (btn) btn.disabled = true;
        try {
            return await fn();
        } finally {
            locks.delete(key);
            if (btn) btn.disabled = false;
        }
    }

    // ------------------------------------------------------------------
    // Notifications (toast)
    // ------------------------------------------------------------------
    function showNotification(message, type = 'info') {
        const notification = el('div', `live-alert-notification ${type === 'success' ? 'success' : type === 'error' ? 'error' : 'info'}`);
        notification.setAttribute('role', 'status');
        notification.appendChild(icon(type === 'success' ? 'fa-check-circle' : type === 'error' ? 'fa-exclamation-circle' : 'fa-info-circle'));
        notification.appendChild(el('span', null, ' ' + message));
        document.body.appendChild(notification);
        const t1 = setTimeout(() => notification.classList.add('show'), 10);
        const t2 = setTimeout(() => {
            notification.classList.remove('show');
            setTimeout(() => notification.remove(), 300);
        }, 4000);
        timers.add(t1); timers.add(t2);
    }

    // ------------------------------------------------------------------
    // Alerts list (load + render)
    // ------------------------------------------------------------------
    async function loadAlerts() {
        return withLock('load-alerts', null, async () => {
            const result = await api('/api/live-alerts?include_inactive=true');
            if (!result.ok) {
                showNotification(`Failed to load alerts: ${errorDetail(result)}`, 'error');
                return;
            }
            state.alerts = Array.isArray(result.payload) ? result.payload : [];
            renderAlerts();
        });
    }

    function renderAlerts() {
        const grid = document.getElementById('alerts-grid');
        if (!grid) return;
        const createCard = grid.querySelector('.create-alert-card');

        Array.from(grid.children).forEach(child => {
            if (!child.classList.contains('create-alert-card')) child.remove();
        });

        if (state.alerts.length === 0) {
            const emptyState = el('div', 'live-alerts-empty-state');
            emptyState.appendChild(icon('fa-bell-slash'));
            emptyState.appendChild(el('h3', null, 'No Live Alerts'));
            emptyState.appendChild(el('p', null, state.isAdmin
                ? 'Create an alert by searching for a person and clicking "Create Live Alert"'
                : 'Create an alert by viewing an identity from Unknown Faces and clicking "Create Live Alert"'));
            grid.insertBefore(emptyState, createCard);
            return;
        }

        state.alerts.forEach(alert => {
            grid.insertBefore(buildAlertCard(alert), createCard);
        });
    }

    function buildAlertCard(alert) {
        const status = safeStatus(alert.status);
        const card = el('div', `live-alert-card ${status}`);
        card.dataset.alertId = String(alert.id);

        // Header
        const header = el('div', 'live-alert-header');
        const avatar = el('img', 'live-alert-avatar');
        avatar.alt = 'Identity snapshot';
        avatar.src = safeImageUrl(alert.identity_snapshot_path);
        avatar.addEventListener('error', () => { avatar.src = FALLBACK_AVATAR; }, { once: true });
        header.appendChild(avatar);
        const info = el('div', 'live-alert-info');
        info.appendChild(el('h3', null, alert.name || '(unnamed alert)'));
        info.appendChild(el('p', null, alert.identity_name || 'Unknown identity'));
        header.appendChild(info);
        header.appendChild(el('span', `status-badge ${status}`, status));
        card.appendChild(header);

        // Config
        const config = el('div', 'live-alert-config');
        const addRow = (label, value) => {
            const row = el('div', 'config-row');
            row.appendChild(el('span', 'label', label));
            row.appendChild(el('span', 'value', value));
            config.appendChild(row);
        };
        addRow('Min Similarity', similarityLabel(alert.min_similarity));
        addRow('Cooldown', `${Number.isFinite(Number(alert.cooldown_minutes)) ? alert.cooldown_minutes : '—'} min`);
        // Empty/null pipeline list means ALL cameras — never display "0"
        const pipelines = Array.isArray(alert.pipeline_ids) && alert.pipeline_ids.length > 0
            ? String(alert.pipeline_ids.length) : 'All';
        addRow('Cameras', pipelines);
        card.appendChild(config);

        // Stats
        const stats = el('div', 'live-alert-stats');
        const statBox = (value, label) => {
            const box = el('div', 'stat-box');
            box.appendChild(el('div', 'value', value));
            box.appendChild(el('div', 'label', label));
            return box;
        };
        stats.appendChild(statBox(String(alert.triggers_count ?? 0), 'Total Triggers'));
        stats.appendChild(statBox(fmtTime(alert.last_triggered_at), 'Last Trigger'));
        card.appendChild(stats);

        // Channel icons (configured, not necessarily working — health shows readiness)
        const channels = el('div', 'notification-icons');
        const channel = (enabled, title, iconCls) => {
            const div = el('div', `notification-icon ${enabled ? 'enabled' : 'disabled'}`);
            div.title = `${title}: ${enabled ? 'configured' : 'off'}`;
            div.appendChild(icon(iconCls));
            return div;
        };
        channels.appendChild(channel(!!alert.notify_dashboard, 'Dashboard', 'fa-desktop'));
        channels.appendChild(channel(!!alert.notify_email, 'Email', 'fa-envelope'));
        channels.appendChild(channel(!!alert.notify_sms, 'SMS', 'fa-sms'));
        channels.appendChild(channel(!!alert.sound_alert, 'Sound', 'fa-volume-up'));
        card.appendChild(channels);

        // Actions (delegated via data-action)
        const actions = el('div', 'live-alert-actions');
        const actionBtn = (action, label, iconCls, extraClass) => {
            const btn = el('button', 'live-alert-btn' + (extraClass ? ' ' + extraClass : ''));
            btn.type = 'button';
            btn.dataset.action = action;
            btn.dataset.alertId = String(alert.id);
            btn.appendChild(icon(iconCls));
            if (label) btn.appendChild(el('span', null, ' ' + label));
            btn.setAttribute('aria-label', `${label || action} ${alert.name || ''}`);
            return btn;
        };
        if (status === 'active') actions.appendChild(actionBtn('pause', 'Pause', 'fa-pause'));
        else if (status === 'paused') actions.appendChild(actionBtn('resume', 'Resume', 'fa-play'));
        actions.appendChild(actionBtn('triggers', 'Triggers', 'fa-history'));
        actions.appendChild(actionBtn('health', 'Health', 'fa-heartbeat'));
        if (state.isAdmin) actions.appendChild(actionBtn('test', 'Test', 'fa-vial'));
        actions.appendChild(actionBtn('delete', '', 'fa-trash', 'danger'));
        card.appendChild(actions);

        return card;
    }

    // ------------------------------------------------------------------
    // Alert actions
    // ------------------------------------------------------------------
    async function pauseAlert(alertId, btn) {
        await withLock(`pause-${alertId}`, btn, async () => {
            const result = await api(`/api/live-alerts/${encodeURIComponent(alertId)}/pause`, { method: 'POST' });
            if (result.ok) { showNotification('Alert paused', 'success'); await loadAlerts(); }
            else showNotification(`Pause failed: ${errorDetail(result)}`, 'error');
        });
    }

    async function resumeAlert(alertId, btn) {
        await withLock(`resume-${alertId}`, btn, async () => {
            const result = await api(`/api/live-alerts/${encodeURIComponent(alertId)}/resume`, { method: 'POST' });
            if (result.ok) { showNotification('Alert resumed', 'success'); await loadAlerts(); }
            else showNotification(`Resume failed: ${errorDetail(result)}`, 'error');
        });
    }

    async function deleteAlert(alertId, btn) {
        const alert = findAlert(alertId);
        if (!confirm(`Delete alert "${alert ? alert.name : alertId}"? This cannot be undone.`)) return;
        await withLock(`delete-${alertId}`, btn, async () => {
            const result = await api(`/api/live-alerts/${encodeURIComponent(alertId)}`, { method: 'DELETE' });
            if (result.ok) { showNotification('Alert deleted', 'success'); await loadAlerts(); }
            else showNotification(`Delete failed: ${errorDetail(result)}`, 'error');
        });
    }

    async function showHealth(alertId, btn) {
        await withLock(`health-${alertId}`, btn, async () => {
            const result = await api(`/api/live-alerts/${encodeURIComponent(alertId)}/health`);
            if (!result.ok) { showNotification(`Health check failed: ${errorDetail(result)}`, 'error'); return; }
            const h = result.payload || {};
            const alert = findAlert(alertId);
            const box = openModalShell(`Health: ${alert ? alert.name : alertId}`, btn);
            const dl = el('dl', 'health-list');
            const row = (label, ok, extra) => {
                dl.appendChild(el('dt', null, label));
                const dd = el('dd', ok === true ? 'health-ok' : ok === false ? 'health-bad' : '');
                dd.textContent = extra !== undefined ? extra : (ok ? 'OK' : 'PROBLEM');
                dl.appendChild(dd);
            };
            row('Stored status', null, String(h.stored_status || 'unknown'));
            row('Effective status', h.effective_status === 'active', String(h.effective_status || 'unknown'));
            row('Identity exists', !!h.identity_exists);
            row('Snapshot exists', !!h.snapshot_exists);
            row('Pipelines valid', !!h.pipelines_valid,
                h.pipelines_valid ? 'OK' : `Invalid: ${(h.invalid_pipelines || []).join(', ') || '?'}`);
            row('Dashboard channel', !!h.dashboard_channel_ready,
                h.dashboard_channel_ready ? `ready (${h.websocket_clients} client(s))` : 'not ready');
            row('Email channel', !!h.email_channel_ready, h.email_channel_ready ? 'ready' : 'not configured/off');
            row('SMS channel', !!h.sms_channel_ready, h.sms_channel_ready ? 'ready' : 'not configured/off');
            row('WebSocket', !!h.websocket_ready, h.websocket_ready ? 'connected clients present' : 'no clients');
            row('Evaluated', null, h.last_evaluated_at ? new Date(h.last_evaluated_at).toLocaleString() : 'now');
            if (h.last_delivery_error) row('Last delivery error', false, String(h.last_delivery_error));
            box.body.appendChild(dl);
        });
    }

    async function runChannelTest(alertId, btn) {
        const alert = findAlert(alertId);
        if (!confirm(`Send a clearly-labeled TEST notification for "${alert ? alert.name : alertId}"?\nNo real email/SMS will be sent without a configured provider.`)) return;
        await withLock(`test-${alertId}`, btn, async () => {
            const start = await api(`/api/live-alerts/${encodeURIComponent(alertId)}/test`, {
                method: 'POST', body: { channels: ['dashboard', 'email', 'sms', 'sound'] }
            });
            if (!start.ok || !start.payload || !start.payload.job_id) {
                showNotification(`Channel test failed to start: ${errorDetail(start)}`, 'error');
                return;
            }
            const jobId = start.payload.job_id;
            for (let i = 0; i < 20; i++) {
                await new Promise(r => { const t = setTimeout(r, 1000); timers.add(t); });
                if (state.destroyed) return;
                const poll = await api(`/api/live-alerts/test-jobs/${encodeURIComponent(jobId)}`);
                if (poll.ok && poll.payload && ['completed', 'failed'].includes(poll.payload.status)) {
                    renderTestResults(alert, poll.payload, btn);
                    return;
                }
            }
            showNotification('Channel test timed out — check Background Tasks for the job result.', 'error');
        });
    }

    function renderTestResults(alert, task, opener) {
        const channels = (task.result && task.result.channels) || {};
        const box = openModalShell(`Channel test: ${alert ? alert.name : ''}`, opener);
        const dl = el('dl', 'health-list');
        for (const name of ['dashboard', 'email', 'sms', 'sound']) {
            const res = channels[name];
            if (!res) continue;
            dl.appendChild(el('dt', null, name.toUpperCase()));
            const ok = res.status === 'sent';
            const dd = el('dd', ok ? 'health-ok' : (res.status === 'disabled' || res.status === 'frontend_required') ? '' : 'health-bad');
            let label = res.status;
            if (res.error_code) label += ` (${res.error_code})`;
            if (name === 'sound' && res.status === 'frontend_required') {
                label = state.soundEnabled ? 'will play when the test event arrives (sound enabled)' : 'sound is disabled in this browser tab';
            }
            dd.textContent = label;
            dl.appendChild(dd);
        }
        if (!Object.keys(channels).length) dl.appendChild(el('dt', null, 'No channel results returned'));
        box.body.appendChild(dl);
        box.body.appendChild(el('p', 'test-note', 'Statuses are real delivery outcomes — a channel is only "sent" when it actually went out.'));
    }

    // ------------------------------------------------------------------
    // Generic accessible modal shell
    // ------------------------------------------------------------------
    function openModalShell(title, opener) {
        closeModalShell();
        const overlay = el('div', 'modal-overlay');
        overlay.id = 'la-modal';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');

        const content = el('div', 'modal-content triggers-modal-content');
        const header = el('div', 'modal-header');
        const h2 = el('h2');
        h2.appendChild(icon('fa-info-circle'));
        h2.appendChild(el('span', null, ' ' + title));
        h2.id = 'la-modal-title';
        overlay.setAttribute('aria-labelledby', 'la-modal-title');
        header.appendChild(h2);
        const closeBtn = el('button', 'modal-close');
        closeBtn.type = 'button';
        closeBtn.setAttribute('aria-label', 'Close dialog');
        closeBtn.appendChild(icon('fa-times'));
        closeBtn.addEventListener('click', closeModalShell);
        header.appendChild(closeBtn);
        content.appendChild(header);

        const body = el('div', 'modal-body');
        content.appendChild(body);
        const footer = el('div', 'modal-footer');
        content.appendChild(footer);
        overlay.appendChild(content);

        overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModalShell(); });
        document.addEventListener('keydown', modalEscapeHandler);
        document.body.appendChild(overlay);

        state.modalShell = { overlay, body, footer, opener: opener || document.activeElement };
        closeBtn.focus();
        return state.modalShell;
    }

    function modalEscapeHandler(e) {
        if (e.key === 'Escape') closeModalShell();
    }

    function closeModalShell() {
        const existing = document.getElementById('la-modal');
        if (existing) existing.remove();
        document.removeEventListener('keydown', modalEscapeHandler);
        if (state.modalShell && state.modalShell.opener && typeof state.modalShell.opener.focus === 'function') {
            state.modalShell.opener.focus();
        }
        state.modalShell = null;
    }

    // ------------------------------------------------------------------
    // Triggers modal (server-side pagination + bulk acknowledge)
    // ------------------------------------------------------------------
    async function viewTriggers(alertId, opener) {
        const alert = findAlert(alertId);
        if (!alert) { showNotification('Alert not found in current list — refreshing.', 'error'); loadAlerts(); return; }
        state.triggerModal = { alertId: String(alertId), page: 1, filter: 'all', selected: new Set() };
        const shell = openModalShell(`Trigger History: ${alert.name}`, opener);
        buildTriggersToolbar(shell, alert);
        await loadTriggersPage();
    }

    function buildTriggersToolbar(shell, alert) {
        const bar = el('div', 'triggers-toolbar');
        const filterSel = el('select', 'triggers-filter');
        filterSel.id = 'triggers-filter';
        [['all', 'All triggers'], ['unack', 'Unacknowledged'], ['ack', 'Acknowledged']].forEach(([v, label]) => {
            const opt = el('option', null, label);
            opt.value = v;
            filterSel.appendChild(opt);
        });
        filterSel.addEventListener('change', () => {
            state.triggerModal.filter = filterSel.value;
            state.triggerModal.page = 1;
            state.triggerModal.selected.clear();
            loadTriggersPage();
        });
        const filterLabel = el('label', null, 'Show: ');
        filterLabel.setAttribute('for', 'triggers-filter');
        filterLabel.appendChild(filterSel);
        bar.appendChild(filterLabel);
        shell.body.appendChild(bar);
        shell.body.appendChild(el('div', 'triggers-content'));

        // Footer: bulk actions + pagination
        const ackSelectedBtn = el('button', 'btn-primary');
        ackSelectedBtn.type = 'button';
        ackSelectedBtn.id = 'ack-selected-btn';
        ackSelectedBtn.appendChild(icon('fa-check'));
        ackSelectedBtn.appendChild(el('span', null, ' Acknowledge Selected'));
        ackSelectedBtn.disabled = true;
        ackSelectedBtn.addEventListener('click', () => bulkAcknowledge([...state.triggerModal.selected], ackSelectedBtn));

        const ackAllBtn = el('button', 'btn-primary');
        ackAllBtn.type = 'button';
        ackAllBtn.id = 'ack-all-btn';
        ackAllBtn.appendChild(icon('fa-check-double'));
        ackAllBtn.appendChild(el('span', null, ' Acknowledge All'));
        ackAllBtn.addEventListener('click', () => bulkAcknowledge(null, ackAllBtn));

        const closeBtn = el('button', 'btn-secondary', 'Close');
        closeBtn.type = 'button';
        closeBtn.addEventListener('click', closeModalShell);

        shell.footer.appendChild(closeBtn);
        shell.footer.appendChild(ackSelectedBtn);
        shell.footer.appendChild(ackAllBtn);
    }

    async function loadTriggersPage() {
        const modal = state.triggerModal;
        const shell = state.modalShell;
        if (!modal || !shell) return;
        const content = shell.body.querySelector('.triggers-content');
        if (!content) return;

        content.replaceChildren(el('div', 'triggers-loading', 'Loading triggers…'));

        await withLock('load-triggers', null, async () => {
            const params = new URLSearchParams({ page: String(modal.page), page_size: '20' });
            if (modal.filter === 'unack') params.set('acknowledged', 'false');
            if (modal.filter === 'ack') params.set('acknowledged', 'true');
            const result = await api(`/api/live-alerts/${encodeURIComponent(modal.alertId)}/triggers?${params}`);

            if (!result.ok) {
                content.replaceChildren();
                const err = el('div', 'triggers-error');
                err.appendChild(el('p', null, `Failed to load triggers: ${errorDetail(result)}`));
                const retry = el('button', 'btn-secondary', 'Retry');
                retry.type = 'button';
                retry.addEventListener('click', () => loadTriggersPage());
                err.appendChild(retry);
                content.replaceChildren(err);
                return;
            }
            renderTriggersPage(content, result.payload || { items: [], total: 0, page: 1, total_pages: 1, unacknowledged_total: 0 });
        });
    }

    function renderTriggersPage(content, pageData) {
        const modal = state.triggerModal;
        content.replaceChildren();

        // Stats strip
        const statsBar = el('div', 'trigger-stats');
        const stat = (value, label) => {
            const box = el('div', 'trigger-stat');
            box.appendChild(el('span', 'stat-value', String(value)));
            box.appendChild(el('span', 'stat-label', label));
            return box;
        };
        statsBar.appendChild(stat(pageData.total ?? 0, 'Total (filtered)'));
        statsBar.appendChild(stat(pageData.unacknowledged_total ?? 0, 'Unacknowledged'));
        content.appendChild(statsBar);

        const items = Array.isArray(pageData.items) ? pageData.items : [];
        if (items.length === 0) {
            const empty = el('div', 'no-triggers');
            empty.appendChild(icon('fa-bell-slash'));
            empty.appendChild(el('p', null, 'No triggers match this filter.'));
            content.appendChild(empty);
        } else {
            const wrap = el('div', 'triggers-list');
            const table = el('table', 'triggers-table');
            const thead = el('thead');
            const headRow = el('tr');
            ['', 'Snapshot', 'Time', 'Camera', 'Similarity', 'Status', 'Action'].forEach(h => {
                const th = el('th', null, h);
                th.scope = 'col';
                headRow.appendChild(th);
            });
            thead.appendChild(headRow);
            table.appendChild(thead);
            const tbody = el('tbody');

            items.forEach(t => {
                const tr = el('tr', t.acknowledged ? 'acknowledged' : 'unacknowledged');

                const selTd = el('td');
                if (!t.acknowledged) {
                    const cb = el('input');
                    cb.type = 'checkbox';
                    cb.setAttribute('aria-label', 'Select trigger for bulk acknowledgement');
                    cb.checked = modal.selected.has(String(t.id));
                    cb.addEventListener('change', () => {
                        if (cb.checked) modal.selected.add(String(t.id));
                        else modal.selected.delete(String(t.id));
                        const btn = document.getElementById('ack-selected-btn');
                        if (btn) btn.disabled = modal.selected.size === 0;
                    });
                    selTd.appendChild(cb);
                }
                tr.appendChild(selTd);

                const snapTd = el('td');
                if (t.snapshot_path) {
                    const img = el('img', 'trigger-snapshot');
                    img.alt = 'Trigger snapshot';
                    img.src = safeImageUrl(t.snapshot_path);
                    img.addEventListener('error', () => { img.src = FALLBACK_AVATAR; }, { once: true });
                    snapTd.appendChild(img);
                } else {
                    snapTd.appendChild(el('span', 'no-snapshot', '—'));
                }
                tr.appendChild(snapTd);

                const timeTd = el('td');
                timeTd.appendChild(el('span', 'trigger-time', fmtTime(t.created_at)));
                tr.appendChild(timeTd);

                const camTd = el('td');
                camTd.appendChild(el('span', 'trigger-camera', t.pipeline_id || 'Unknown'));
                tr.appendChild(camTd);

                const simTd = el('td');
                simTd.appendChild(el('span', `trigger-similarity ${similarityClass(t.similarity_score)}`, similarityLabel(t.similarity_score)));
                tr.appendChild(simTd);

                const statusTd = el('td');
                if (t.acknowledged) {
                    const span = el('span', 'status-ack');
                    span.title = `Acknowledged by ${t.acknowledged_by || 'unknown'}${t.acknowledged_at ? ' at ' + new Date(t.acknowledged_at).toLocaleString() : ''}`;
                    span.appendChild(icon('fa-check-circle'));
                    span.appendChild(el('span', null, " Ack'd"));
                    statusTd.appendChild(span);
                } else {
                    const span = el('span', 'status-unack');
                    span.appendChild(icon('fa-exclamation-circle'));
                    span.appendChild(el('span', null, ' New'));
                    statusTd.appendChild(span);
                }
                tr.appendChild(statusTd);

                const actionTd = el('td');
                if (!t.acknowledged) {
                    const btn = el('button', 'btn-acknowledge');
                    btn.type = 'button';
                    btn.appendChild(icon('fa-check'));
                    btn.appendChild(el('span', null, ' Acknowledge'));
                    btn.addEventListener('click', () => acknowledgeTrigger(t.id, btn));
                    actionTd.appendChild(btn);
                }
                tr.appendChild(actionTd);
                tbody.appendChild(tr);
            });
            table.appendChild(tbody);
            wrap.appendChild(table);
            content.appendChild(wrap);
        }

        // Pagination (server-side — only the selected page is ever loaded)
        const pager = el('div', 'triggers-pager');
        const prev = el('button', 'btn-secondary', '← Prev');
        prev.type = 'button';
        prev.disabled = (pageData.page || 1) <= 1;
        prev.addEventListener('click', () => { modal.page = Math.max(1, modal.page - 1); loadTriggersPage(); });
        const next = el('button', 'btn-secondary', 'Next →');
        next.type = 'button';
        next.disabled = (pageData.page || 1) >= (pageData.total_pages || 1);
        next.addEventListener('click', () => { modal.page += 1; loadTriggersPage(); });
        pager.appendChild(prev);
        pager.appendChild(el('span', 'pager-info', `Page ${pageData.page || 1} of ${pageData.total_pages || 1}`));
        pager.appendChild(next);
        content.appendChild(pager);

        const ackAllBtn = document.getElementById('ack-all-btn');
        if (ackAllBtn) ackAllBtn.disabled = (pageData.unacknowledged_total || 0) === 0;
        const ackSelBtn = document.getElementById('ack-selected-btn');
        if (ackSelBtn) ackSelBtn.disabled = modal.selected.size === 0;
    }

    async function acknowledgeTrigger(triggerId, btn) {
        await withLock(`ack-${triggerId}`, btn, async () => {
            const result = await api(`/api/live-alerts/triggers/${encodeURIComponent(triggerId)}/acknowledge`, { method: 'POST' });
            if (result.ok) {
                showNotification('Trigger acknowledged', 'success');
                await loadTriggersPage();
                scheduleAlertsRefresh();
            } else {
                showNotification(`Failed to acknowledge: ${errorDetail(result)}`, 'error');
            }
        });
    }

    /** ONE bulk request — never a loop of per-trigger POSTs. */
    async function bulkAcknowledge(triggerIds, btn) {
        const modal = state.triggerModal;
        if (!modal) return;
        await withLock('bulk-ack', btn, async () => {
            const body = triggerIds && triggerIds.length ? { trigger_ids: triggerIds } : {};
            const result = await api(
                `/api/live-alerts/${encodeURIComponent(modal.alertId)}/triggers/acknowledge-all`,
                { method: 'POST', body });
            if (result.ok && result.payload) {
                showNotification(`${result.payload.acknowledged ?? 0} trigger(s) acknowledged`, 'success');
                modal.selected.clear();
                await loadTriggersPage();
                scheduleAlertsRefresh();
            } else {
                showNotification(`Bulk acknowledge failed: ${errorDetail(result)}`, 'error');
            }
        });
    }

    // ------------------------------------------------------------------
    // WebSocket lifecycle
    // ------------------------------------------------------------------
    const wsState = {
        socket: null,
        status: 'disconnected',
        attempts: 0,
        maxAttempts: 10,
        reconnectTimer: null,
        stableTimer: null,
        heartbeatTimer: null,
        lastMessageAt: 0,
        intentionalClose: false,
        authFailed: false,
    };

    function setWsStatus(status) {
        wsState.status = status;
        const pill = document.getElementById('ws-status');
        if (!pill) return;
        const labels = {
            connected: 'Connected',
            connecting: 'Connecting…',
            reconnecting: 'Reconnecting…',
            disconnected: 'Disconnected',
            auth_failed: 'Authentication failed',
        };
        pill.textContent = labels[status] || status;
        pill.className = `ws-status-pill ws-${status}`;
        const btn = document.getElementById('ws-reconnect-btn');
        if (btn) btn.style.display = (status === 'disconnected' || status === 'auth_failed') ? '' : 'none';
    }

    function connectWebSocket() {
        if (state.destroyed || wsState.intentionalClose || wsState.authFailed) return;
        // Never create a duplicate while one is CONNECTING or OPEN
        if (wsState.socket && (wsState.socket.readyState === WebSocket.CONNECTING
            || wsState.socket.readyState === WebSocket.OPEN)) return;
        if (wsState.reconnectTimer) {
            clearTimeout(wsState.reconnectTimer);
            timers.delete(wsState.reconnectTimer);
            wsState.reconnectTimer = null;
        }

        setWsStatus(wsState.attempts > 0 ? 'reconnecting' : 'connecting');
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        let socket;
        try {
            socket = new WebSocket(`${protocol}//${window.location.host}/ws?view=alerts`);
        } catch (e) {
            scheduleReconnect();
            return;
        }
        wsState.socket = socket;

        socket.onopen = () => {
            setWsStatus('connected');
            // Reset backoff only after the connection proves STABLE (10s)
            wsState.stableTimer = setTimeout(() => { wsState.attempts = 0; }, 10000);
            timers.add(wsState.stableTimer);
            wsState.lastMessageAt = Date.now();
            startHeartbeat();
            scheduleAlertsRefresh(); // reconcile anything missed while offline
        };

        socket.onmessage = (event) => {
            wsState.lastMessageAt = Date.now();
            let message;
            try { message = JSON.parse(event.data); }
            catch (e) { return; } // malformed frame — rejected safely
            handleWsMessage(message);
        };

        socket.onclose = (event) => {
            stopHeartbeat();
            if (wsState.stableTimer) { clearTimeout(wsState.stableTimer); timers.delete(wsState.stableTimer); }
            wsState.socket = null;
            if (wsState.intentionalClose || state.destroyed) return;
            if (event.code === 1008) {
                wsState.authFailed = true;
                setWsStatus('auth_failed');
                return; // do not retry a rejected authentication forever
            }
            scheduleReconnect();
        };

        socket.onerror = () => { /* onclose follows and handles retry */ };
    }

    function scheduleReconnect() {
        if (state.destroyed || wsState.intentionalClose || wsState.authFailed) return;
        if (wsState.reconnectTimer) return; // one pending reconnect max
        if (wsState.attempts >= wsState.maxAttempts) {
            setWsStatus('disconnected');
            return;
        }
        wsState.attempts += 1;
        setWsStatus('reconnecting');
        // Bounded exponential backoff with ±30% jitter
        const base = Math.min(1000 * Math.pow(2, wsState.attempts), 30000);
        const delay = Math.round(base * (0.7 + Math.random() * 0.6));
        wsState.reconnectTimer = setTimeout(() => {
            timers.delete(wsState.reconnectTimer);
            wsState.reconnectTimer = null;
            connectWebSocket();
        }, delay);
        timers.add(wsState.reconnectTimer);
    }

    function startHeartbeat() {
        stopHeartbeat();
        wsState.heartbeatTimer = setInterval(() => {
            if (!wsState.socket || wsState.socket.readyState !== WebSocket.OPEN) return;
            // Stale detection: server pings/pongs every ~30s — 90s of silence means dead
            if (Date.now() - wsState.lastMessageAt > 90000) {
                try { wsState.socket.close(); } catch (e) { /* onclose reconnects */ }
                return;
            }
            try { wsState.socket.send('ping'); } catch (e) { /* ignore */ }
        }, 30000);
        timers.add(wsState.heartbeatTimer);
    }

    function stopHeartbeat() {
        if (wsState.heartbeatTimer) {
            clearInterval(wsState.heartbeatTimer);
            timers.delete(wsState.heartbeatTimer);
            wsState.heartbeatTimer = null;
        }
    }

    function manualReconnect() {
        wsState.attempts = 0;
        wsState.authFailed = false;
        wsState.intentionalClose = false;
        connectWebSocket();
    }

    // ------------------------------------------------------------------
    // WS message validation + trigger handling
    // ------------------------------------------------------------------

    /** Strict schema: reject arbitrary object shapes from the wire.
     *  Live-alert triggers arrive ONLY in `detection_alerts` — the event the
     *  server broadcasts after the trigger rows are committed. `new_detection`
     *  is the pre-commit detection feed and carries no alerts. */
    function validateAlertEvent(message) {
        if (!message || typeof message !== 'object') return null;
        if (message.type !== 'detection_alerts' && message.type !== 'live_alert_test') return null;
        const data = message.data;
        if (!data || typeof data !== 'object') return null;

        if (message.type === 'live_alert_test') {
            if (typeof data.alert_name !== 'string') return null;
            return {
                test: true,
                eventId: typeof data.event_id === 'string' ? data.event_id : `test:${data.alert_id}:${data.created_at}`,
                alerts: [{ alert_id: data.alert_id, alert_name: data.alert_name, sound_alert: true, trigger_id: null }],
                faceName: 'TEST NOTIFICATION',
                pipelineId: 'test',
                similarity: null,
                createdAt: data.created_at,
            };
        }

        if (!Array.isArray(data.live_alerts) || data.live_alerts.length === 0) return null;
        const alerts = data.live_alerts.filter(a =>
            a && typeof a === 'object'
            && (typeof a.alert_id === 'string' || typeof a.alert_id === 'number')
            && typeof a.alert_name === 'string'
        );
        if (!alerts.length) return null;

        return {
            test: false,
            eventId: typeof data.event_id === 'string' ? data.event_id
                : `${alerts[0].alert_id}:${data.detection_id || data.timestamp || ''}`,
            alerts,
            faceName: typeof data.identity_name === 'string' && data.identity_name ? data.identity_name : 'Unknown',
            pipelineId: typeof data.pipeline_id === 'string' ? data.pipeline_id : 'unknown',
            similarity: clampSimilarity(data.similarity),
            createdAt: typeof data.created_at === 'string' ? data.created_at
                : (typeof data.timestamp === 'string' ? data.timestamp : null),
        };
    }

    function handleWsMessage(message) {
        const event = validateAlertEvent(message);
        if (!event) return;

        // Bounded dedup: reconnects/replays of the same event_id do nothing
        if (state.seenEvents.has(event.eventId)) return;
        state.seenEvents.add(event.eventId);
        state.seenEventOrder.push(event.eventId);
        while (state.seenEventOrder.length > EVENT_CACHE_LIMIT) {
            state.seenEvents.delete(state.seenEventOrder.shift());
        }

        // One popup per triggered alert (stacked/queued), ONE sound per event
        let playSound = false;
        event.alerts.forEach(alertInfo => {
            enqueuePopup(alertInfo, event);
            if (alertInfo.sound_alert) playSound = true;
        });
        if (playSound) playAlertSound();

        // ONE debounced refresh for the whole burst — never one per alert
        if (!event.test) scheduleAlertsRefresh();
    }

    function scheduleAlertsRefresh() {
        if (state.refreshTimer) return;
        state.refreshTimer = setTimeout(() => {
            timers.delete(state.refreshTimer);
            state.refreshTimer = null;
            loadAlerts();
        }, 500);
        timers.add(state.refreshTimer);
    }

    // ------------------------------------------------------------------
    // Popup stack (simultaneous alerts queue instead of overwriting)
    // ------------------------------------------------------------------
    function popupContainer() {
        let c = document.getElementById('alert-popup-container');
        if (!c) {
            c = el('div');
            c.id = 'alert-popup-container';
            c.setAttribute('role', 'region');
            c.setAttribute('aria-live', 'assertive');
            c.setAttribute('aria-label', 'Live alert notifications');
            document.body.appendChild(c);
        }
        return c;
    }

    function enqueuePopup(alertInfo, event) {
        state.popupQueue.push({ alertInfo, event });
        drainPopupQueue();
    }

    function drainPopupQueue() {
        while (state.visiblePopups < MAX_VISIBLE_POPUPS && state.popupQueue.length > 0) {
            const { alertInfo, event } = state.popupQueue.shift();
            showPopup(alertInfo, event);
        }
    }

    function showPopup(alertInfo, event) {
        state.visiblePopups += 1;
        const popup = el('div', 'alert-trigger-popup');

        const content = el('div', 'alert-trigger-content');
        const iconWrap = el('div', 'alert-trigger-icon');
        iconWrap.appendChild(icon('fa-bell'));
        content.appendChild(iconWrap);

        const info = el('div', 'alert-trigger-info');
        info.appendChild(el('div', 'alert-trigger-title', event.test ? 'TEST ALERT' : 'LIVE ALERT TRIGGERED'));
        info.appendChild(el('div', 'alert-trigger-name', alertInfo.alert_name));
        const details = el('div', 'alert-trigger-details');
        const detail = (iconCls, text) => {
            const span = el('span');
            span.appendChild(icon(iconCls));
            span.appendChild(el('span', null, ' ' + text));
            details.appendChild(span);
        };
        detail('fa-user', alertInfo.identity_name || event.faceName || 'Unknown');
        detail('fa-video', event.pipelineId || 'unknown');
        const sim = clampSimilarity(alertInfo.similarity !== undefined ? alertInfo.similarity : event.similarity);
        if (sim !== null) detail('fa-percentage', `${Math.round(sim * 100)}%`);
        detail('fa-clock', event.createdAt ? fmtTime(event.createdAt) : 'now');
        info.appendChild(details);

        const actions = el('div', 'alert-popup-actions');
        const viewBtn = el('button', 'alert-popup-btn');
        viewBtn.type = 'button';
        viewBtn.textContent = 'View triggers';
        viewBtn.addEventListener('click', () => { removePopup(); viewTriggers(String(alertInfo.alert_id), null); });
        actions.appendChild(viewBtn);
        if (alertInfo.trigger_id) {
            const ackBtn = el('button', 'alert-popup-btn');
            ackBtn.type = 'button';
            ackBtn.textContent = 'Acknowledge';
            ackBtn.addEventListener('click', async () => {
                ackBtn.disabled = true;
                const result = await api(`/api/live-alerts/triggers/${encodeURIComponent(alertInfo.trigger_id)}/acknowledge`, { method: 'POST' });
                if (result.ok) { showNotification('Trigger acknowledged', 'success'); removePopup(); scheduleAlertsRefresh(); }
                else { ackBtn.disabled = false; showNotification(`Failed: ${errorDetail(result)}`, 'error'); }
            });
            actions.appendChild(ackBtn);
        }
        info.appendChild(actions);
        content.appendChild(info);

        const closeBtn = el('button', 'alert-trigger-close');
        closeBtn.type = 'button';
        closeBtn.setAttribute('aria-label', 'Dismiss notification');
        closeBtn.appendChild(icon('fa-times'));
        closeBtn.addEventListener('click', removePopup);
        content.appendChild(closeBtn);

        popup.appendChild(content);
        popupContainer().appendChild(popup);

        const t1 = setTimeout(() => popup.classList.add('show'), 10);
        timers.add(t1);
        const autoT = setTimeout(removePopup, 12000);
        timers.add(autoT);

        let removed = false;
        function removePopup() {
            if (removed) return;
            removed = true;
            clearTimeout(autoT);
            timers.delete(autoT);
            popup.classList.remove('show');
            setTimeout(() => popup.remove(), 300);
            state.visiblePopups -= 1;
            drainPopupQueue();
        }
    }

    // ------------------------------------------------------------------
    // Sound (one shared AudioContext, explicit opt-in, honest status)
    // ------------------------------------------------------------------
    function ensureAudioCtx() {
        if (!state.audioCtx) {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return null;
            state.audioCtx = new Ctx();
        }
        return state.audioCtx;
    }

    function updateSoundButton(blocked) {
        const btn = document.getElementById('sound-toggle-btn');
        if (!btn) return;
        const label = btn.querySelector('span');
        let text;
        if (blocked) text = 'Sound: Blocked by browser';
        else text = state.soundEnabled ? 'Sound: Enabled' : 'Enable Alert Sound';
        if (label) label.textContent = text;
        btn.classList.toggle('sound-on', state.soundEnabled && !blocked);
        btn.setAttribute('aria-pressed', state.soundEnabled ? 'true' : 'false');
    }

    async function toggleSound() {
        state.soundEnabled = !state.soundEnabled;
        try { localStorage.setItem(SOUND_PREF_KEY, state.soundEnabled ? '1' : '0'); } catch (e) { /* ignore */ }
        if (state.soundEnabled) {
            const ctx = ensureAudioCtx();
            if (!ctx) { updateSoundButton(true); return; }
            try {
                if (ctx.state === 'suspended') await ctx.resume();
            } catch (e) { /* fallthrough to state check */ }
            if (ctx.state !== 'running') {
                updateSoundButton(true);
                showNotification('The browser blocked audio — interact with the page and try again.', 'error');
                return;
            }
            playTone(ctx); // confirmation blip during the user gesture
            showNotification('Alert sound enabled', 'success');
        } else if (state.audioCtx) {
            try { state.audioCtx.suspend(); } catch (e) { /* ignore */ }
        }
        updateSoundButton(false);
    }

    function playTone(ctx) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.frequency.setValueAtTime(880, ctx.currentTime);
        osc.frequency.setValueAtTime(1046.5, ctx.currentTime + 0.15);
        osc.frequency.setValueAtTime(880, ctx.currentTime + 0.3);
        osc.type = 'sine';
        gain.gain.setValueAtTime(0.4, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.5);
        osc.start(ctx.currentTime);
        osc.stop(ctx.currentTime + 0.5);
    }

    function playAlertSound() {
        if (!state.soundEnabled) return;
        const ctx = ensureAudioCtx();
        if (!ctx) return;
        if (ctx.state !== 'running') {
            // Autoplay policy blocked us — report honestly, never claim delivery
            updateSoundButton(true);
            return;
        }
        try { playTone(ctx); } catch (e) { console.warn('[LIVE_ALERTS] sound failed:', e); }
    }

    // ------------------------------------------------------------------
    // Reconciliation poller (WS for immediacy, API refresh for truth)
    // ------------------------------------------------------------------
    let pollTimer = null;
    let pollInFlight = false;

    function schedulePoll() {
        if (state.destroyed) return;
        const delay = document.hidden ? 240000 : 60000; // reduced while hidden
        pollTimer = setTimeout(async () => {
            timers.delete(pollTimer);
            if (!document.hidden && !pollInFlight) {
                pollInFlight = true;
                try { await loadAlerts(); } catch (e) { /* logged in loadAlerts */ }
                finally { pollInFlight = false; }
            }
            schedulePoll();
        }, delay);
        timers.add(pollTimer);
    }

    // ------------------------------------------------------------------
    // Role handling (documented contract: top-level `role`)
    // ------------------------------------------------------------------
    async function checkUserRole() {
        const result = await api('/api/auth/me/privileges');
        if (!result.ok || !result.payload) return;
        const privileges = result.payload;
        // Stable contract: top-level role (nested user.role kept as fallback
        // for older cached responses only). Role shapes UI — the backend
        // authorizes every request independently.
        state.role = privileges.role || (privileges.user && privileges.user.role) || null;
        state.isAdmin = state.role === 'admin';
        window.currentUserRole = state.role;

        const searchBtn = document.getElementById('search-btn');
        if (searchBtn) searchBtn.style.display = state.isAdmin ? 'block' : 'none';
        const instructionText = document.getElementById('create-alert-instruction');
        if (instructionText) {
            instructionText.textContent = state.isAdmin
                ? 'Search for a person first, then create alert'
                : 'View an identity from Unknown Faces, then create alert';
        }
    }

    function showCreateInstructions() {
        const box = openModalShell('How to create a Live Alert', document.activeElement);
        const ol = el('ol', 'create-steps');
        const steps = state.isAdmin
            ? ['Go to Advanced Search', 'Search for the person you want to track',
               'Click on a match result', 'Click "Create Live Alert"']
            : ['Go to Unknown Faces', 'Find the person you want to track',
               'Click "VIEW" to see identity details', 'Click "Create Live Alert"'];
        steps.forEach(s => ol.appendChild(el('li', null, s)));
        box.body.appendChild(ol);
        box.body.appendChild(el('p', null, 'You will be notified whenever this person is detected.'));
    }

    // ------------------------------------------------------------------
    // Wiring + lifecycle
    // ------------------------------------------------------------------
    function setupEventListeners() {
        // Card actions — one delegated listener, no inline onclick anywhere
        const grid = document.getElementById('alerts-grid');
        if (grid) {
            grid.addEventListener('click', (e) => {
                const createCard = e.target.closest('.create-alert-card');
                if (createCard) { showCreateInstructions(); return; }
                const btn = e.target.closest('button[data-action]');
                if (!btn) return;
                const alertId = btn.dataset.alertId;
                switch (btn.dataset.action) {
                    case 'pause': pauseAlert(alertId, btn); break;
                    case 'resume': resumeAlert(alertId, btn); break;
                    case 'delete': deleteAlert(alertId, btn); break;
                    case 'triggers': viewTriggers(alertId, btn); break;
                    case 'health': showHealth(alertId, btn); break;
                    case 'test': runChannelTest(alertId, btn); break;
                }
            });
        }

        const soundBtn = document.getElementById('sound-toggle-btn');
        if (soundBtn) soundBtn.addEventListener('click', toggleSound);
        const reconnectBtn = document.getElementById('ws-reconnect-btn');
        if (reconnectBtn) reconnectBtn.addEventListener('click', manualReconnect);

        document.addEventListener('visibilitychange', onVisibilityChange);
        window.addEventListener('online', onOnline);
        window.addEventListener('offline', onOffline);
        window.addEventListener('pagehide', destroy);
        window.addEventListener('beforeunload', destroy);
    }

    function onVisibilityChange() {
        if (!document.hidden) {
            scheduleAlertsRefresh();
            if (!wsState.socket && !wsState.authFailed) { wsState.attempts = 0; connectWebSocket(); }
        }
    }

    function onOnline() {
        if (!wsState.authFailed) { wsState.attempts = 0; connectWebSocket(); }
        scheduleAlertsRefresh();
    }

    function onOffline() {
        setWsStatus('disconnected');
    }

    function destroy() {
        if (state.destroyed) return;
        state.destroyed = true;
        wsState.intentionalClose = true; // never reconnect after intentional shutdown
        stopHeartbeat();
        if (wsState.socket) { try { wsState.socket.close(1000); } catch (e) { /* ignore */ } }
        for (const t of timers) { clearTimeout(t); clearInterval(t); }
        timers.clear();
        for (const c of controllers) { try { c.abort(); } catch (e) { /* ignore */ } }
        controllers.clear();
        if (state.audioCtx) { try { state.audioCtx.close(); } catch (e) { /* ignore */ } state.audioCtx = null; }
        document.removeEventListener('visibilitychange', onVisibilityChange);
        window.removeEventListener('online', onOnline);
        window.removeEventListener('offline', onOffline);
    }

    document.addEventListener('DOMContentLoaded', async () => {
        try { state.soundEnabled = localStorage.getItem(SOUND_PREF_KEY) === '1'; } catch (e) { /* ignore */ }
        updateSoundButton(false);
        setupEventListeners();
        await checkUserRole();
        await loadBandThresholds();
        await loadAlerts();
        connectWebSocket();
        schedulePoll();
    });
})();
