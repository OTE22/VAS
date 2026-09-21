/**
 * Watchlist Management (hardened rewrite)
 * =======================================
 * CRUD + entries + real statistics + soft delete/restore.
 *
 * Contract:
 *  - No backend value ever passes through innerHTML or inline handlers.
 *  - Colors/icons/alert levels are allowlisted before touching the DOM.
 *  - The list is server-side paginated with search/filter/sort.
 *  - "Alerts Today" is the backend's number for an explicit reporting
 *    period — never a hard-coded zero.
 *  - Every mutation carries the CSRF header and the record version;
 *    409 conflicts are surfaced, not silently lost.
 *  - Deletion is soft, preceded by a backend impact summary; deleted
 *    watchlists can be restored.
 *  - Browser popup dialogs are replaced by accessible in-app modals.
 */

(function () {
    'use strict';

    const DEBUG = false;
    const PAGE_SIZE = 20;
    const ENTRY_PAGE_SIZE = 50;
    const SEARCH_DEBOUNCE_MS = 300;
    const API_TIMEOUT_MS = 30000;

    const ALLOWED_WATCHLIST_ICONS = new Set([
        'list', 'shield-alt', 'user-shield', 'exclamation-triangle',
        'eye', 'users', 'star', 'ban', 'user-secret', 'crosshairs'
    ]);
    const ALERT_LEVELS = new Set(['info', 'warning', 'critical']);
    const ALERT_LEVEL_LABELS = { info: 'Info', warning: 'Warning', critical: 'Critical' };
    const ENTRY_PRIORITIES = ['low', 'normal', 'high', 'critical'];
    const DEFAULT_COLOR = '#6366f1';

    function log() { if (DEBUG) console.log.apply(console, arguments); }

    // ============================================
    // Safe helpers
    // ============================================

    function normalizeId(value) {
        if (value === null || value === undefined) return null;
        const s = String(value).trim();
        return s || null;
    }

    function safeText(value, fallback) {
        if (value === null || value === undefined || value === '') {
            return fallback !== undefined ? fallback : '';
        }
        return String(value);
    }

    function toNonNegativeInteger(value, fallback) {
        const n = Math.floor(Number(value));
        return Number.isFinite(n) && n >= 0 ? n : (fallback !== undefined ? fallback : 0);
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

    function normalizeColor(value) {
        return /^#[0-9a-fA-F]{6}$/.test(String(value)) ? String(value).toLowerCase() : DEFAULT_COLOR;
    }

    function hexToRgba(hex, alpha) {
        const safe = normalizeColor(hex);
        const r = parseInt(safe.slice(1, 3), 16);
        const g = parseInt(safe.slice(3, 5), 16);
        const b = parseInt(safe.slice(5, 7), 16);
        const a = Math.max(0, Math.min(1, Number(alpha) || 0));
        return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')';
    }

    function normalizeIcon(value) {
        return ALLOWED_WATCHLIST_ICONS.has(String(value)) ? String(value) : 'list';
    }

    function normalizeAlertLevel(value) {
        const level = String(value || '').toLowerCase();
        return ALERT_LEVELS.has(level) ? level : 'info';
    }

    function normalizeWatchlist(raw) {
        if (!raw || typeof raw !== 'object') return null;
        const id = normalizeId(raw.id);
        if (!id) return null;
        return {
            id: id,
            name: safeText(raw.name, 'Unnamed'),
            description: safeText(raw.description),
            alertLevel: normalizeAlertLevel(raw.alert_level),
            color: normalizeColor(raw.color),
            icon: normalizeIcon(raw.icon),
            isActive: raw.is_active === true,
            entriesCount: toNonNegativeInteger(raw.entries_count),
            alertsToday: toNonNegativeInteger(raw.alerts_today),
            totalAlerts: toNonNegativeInteger(raw.total_alerts),
            lastAlertAt: raw.last_alert_at || null,
            createdAt: raw.created_at || null,
            updatedAt: raw.updated_at || null,
            deletedAt: raw.deleted_at || null,
            deletionReason: safeText(raw.deletion_reason),
            version: toNonNegativeInteger(raw.version, 1) || 1
        };
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

    function faIcon(name) {
        return el('i', { className: name, attrs: { 'aria-hidden': 'true' } });
    }

    // ============================================
    // Shared API client
    // ============================================

    function ApiError(message, opts) {
        const e = new Error(message);
        e.name = 'ApiError';
        e.status = (opts && opts.status) || 0;
        e.code = (opts && opts.code) || null;
        e.referenceId = (opts && opts.referenceId) || null;
        e.currentVersion = (opts && opts.currentVersion) || null;
        e.impact = (opts && opts.impact) || null;
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
        let timedOut = false;
        const timer = window.setTimeout(function () { timedOut = true; timeoutCtl.abort(); }, options.timeout || API_TIMEOUT_MS);
        const cancel = function () { timeoutCtl.abort(); };
        if (options.signal) {
            if (options.signal.aborted) cancel();
            else options.signal.addEventListener('abort', cancel, { once: true });
        }
        const signal = timeoutCtl.signal;

        const headers = { 'Accept': 'application/json' };
        if (method !== 'GET' && method !== 'HEAD') {
            headers['X-Requested-With'] = 'XMLHttpRequest'; // CSRF header
            if (options.body !== undefined) headers['Content-Type'] = 'application/json';
        }

        let response;
        try {
            response = await fetch(url.toString(), {
                method: method,
                credentials: 'include',
                cache: 'no-store',
                headers: headers,
                body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
                signal: signal
            });
        } catch (err) {
            window.clearTimeout(timer);
            if (options.signal) options.signal.removeEventListener('abort', cancel);
            if (timedOut && !(options.signal && options.signal.aborted)) throw ApiError('Request timed out. Please retry.', { code: 'TIMEOUT' });
            if (err && err.name === 'AbortError') throw ApiError('Request cancelled', { aborted: true });
            throw ApiError('Network error — backend unreachable', { status: 0 });
        }
        window.clearTimeout(timer);
        if (options.signal) options.signal.removeEventListener('abort', cancel);

        if (response.status === 401) {
            window.location.href = '/login';
            throw ApiError('Session expired', { status: 401, code: 'AUTH_EXPIRED' });
        }

        if (!response.ok) {
            let code = null, referenceId = null, currentVersion = null, impact = null;
            let message = 'Request failed (' + response.status + ')';
            try {
                const body = await response.json();
                const detail = body && body.detail;
                if (detail && typeof detail === 'object') {
                    code = detail.error_code || null;
                    currentVersion = detail.current_version || null;
                    impact = detail.impact || null;
                    if (typeof detail.message === 'string') message = detail.message;
                } else if (typeof detail === 'string') {
                    message = detail;
                    const refMatch = detail.match(/Reference:\s*([A-Za-z0-9-]+)/);
                    if (refMatch) referenceId = refMatch[1];
                } else if (Array.isArray(detail) && detail.length && detail[0].msg) {
                    message = String(detail[0].msg);
                    code = 'VALIDATION_ERROR';
                }
            } catch (_) { /* keep generic message */ }
            throw ApiError(message, {
                status: response.status, code: code,
                referenceId: referenceId, currentVersion: currentVersion, impact: impact
            });
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
        const gen = (requestGenerations.get(key) || 0) + 1;
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
        watchlists: new Map(),   // id -> normalized
        page: 1,
        totalPages: 1,
        total: 0,
        search: '',
        alertLevelFilter: '',
        statusFilter: 'all',     // all | active | inactive | deleted
        sortBy: 'name',
        sortOrder: 'asc',
        selectedColor: DEFAULT_COLOR,
        editingVersion: null,
        saving: false,
        searchTimer: null
    };

    // ============================================
    // Notifications + accessible modals
    // ============================================

    function showNotification(message, type) {
        type = ['info', 'success', 'error', 'warning'].indexOf(type) >= 0 ? type : 'info';
        const colors = { info: '#3498db', success: '#2ecc71', error: '#e74c3c', warning: '#f39c12' };
        const notification = el('div', { className: 'notification ' + type });
        notification.style.cssText = 'position:fixed;top:20px;right:20px;padding:14px 20px;background:' + colors[type] +
            ';color:#fff;border-radius:6px;z-index:var(--z-toast,10050);box-shadow:0 4px 6px rgba(0,0,0,0.3);font-weight:600;';
        notification.textContent = message;
        notification.setAttribute('role', type === 'error' ? 'alert' : 'status');
        document.body.appendChild(notification);
        window.setTimeout(function () {
            notification.style.opacity = '0';
            notification.style.transition = 'opacity 0.3s';
            window.setTimeout(function () { notification.remove(); }, 300);
        }, 4000);
    }

    let activeDialog = null;

    function closeDialog() {
        if (!activeDialog) return;
        const dialog = activeDialog;
        activeDialog = null;
        document.removeEventListener('keydown', dialog.keyHandler);
        if (window.ModalStack) window.ModalStack.close(dialog.node);
        dialog.node.remove();
        if (dialog.previousFocus && dialog.previousFocus.isConnected) dialog.previousFocus.focus();
    }

    // Accessible confirm/info dialog. Returns a Promise<boolean>.
    function showDialog(title, bodyNodes, opts) {
        opts = opts || {};
        return new Promise(function (resolve) {
            closeDialog();
            const confirmBtn = opts.confirmLabel
                ? el('button', { className: 'submit-btn', text: opts.confirmLabel, attrs: { type: 'button' } })
                : null;
            const cancelBtn = el('button', {
                className: 'watchlist-btn',
                text: opts.confirmLabel ? 'Cancel' : 'Close',
                attrs: { type: 'button' }
            });
            const buttons = el('div', {}, [cancelBtn, confirmBtn]);
            buttons.style.cssText = 'display:flex;gap:0.75rem;justify-content:flex-end;margin-top:1rem;';

            const dialog = el('div', {
                attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-label': title }
            }, [
                el('h3', { text: title }),
                el('div', { className: 'app-dialog-body' }, bodyNodes),
                buttons
            ]);
            dialog.style.cssText = 'background:#131a29;color:#fff;border:1px solid rgba(99,102,241,0.5);' +
                'border-radius:10px;padding:1.5rem;max-width:520px;width:92%;max-height:80vh;overflow:auto;';
            const backdrop = el('div', {}, dialog);
            backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.7);display:flex;' +
                'align-items:center;justify-content:center;z-index:var(--z-modal-base,10000);';

            let finished = false;
            function finish(result) { if (finished) return; finished = true; closeDialog(); resolve(result); }
            cancelBtn.addEventListener('click', function () { finish(false); });
            if (confirmBtn) confirmBtn.addEventListener('click', function () { finish(true); });
            if (!window.ModalStack) backdrop.addEventListener('click', function (e) { if (e.target === backdrop) finish(false); });

            const focusables = [cancelBtn].concat(confirmBtn ? [confirmBtn] : []);
            const keyHandler = function (e) {
                if (e.key === 'Escape') { e.preventDefault(); finish(false); }
                if (e.key === 'Tab') {
                    // simple 2-control focus trap
                    e.preventDefault();
                    const idx = focusables.indexOf(document.activeElement);
                    const next = focusables[(idx + (e.shiftKey ? -1 : 1) + focusables.length) % focusables.length];
                    next.focus();
                }
            };
            if (!window.ModalStack) document.addEventListener('keydown', keyHandler);
            activeDialog = { node: backdrop, keyHandler: keyHandler, previousFocus: document.activeElement };
            document.body.appendChild(backdrop);
            if (window.ModalStack) window.ModalStack.open(backdrop, { backdropClose: true, onClose: () => finish(false) });
            (confirmBtn || cancelBtn).focus();
        });
    }

    // ============================================
    // Toolbar (search / filters / sort / pagination)
    // ============================================

    function buildToolbar() {
        const host = document.getElementById('watchlist-toolbar');
        if (!host || host.dataset.built) return;
        host.dataset.built = 'true';

        const searchInput = el('input', {
            className: 'form-control',
            attrs: { type: 'text', id: 'watchlist-search', placeholder: 'Search watchlists...', 'aria-label': 'Search watchlists', autocomplete: 'off' }
        });
        searchInput.addEventListener('input', function () {
            if (state.searchTimer) window.clearTimeout(state.searchTimer);
            state.searchTimer = window.setTimeout(function () {
                state.search = searchInput.value.trim();
                state.page = 1;
                loadWatchlists();
            }, SEARCH_DEBOUNCE_MS);
        });

        const levelSelect = el('select', { className: 'form-control', attrs: { 'aria-label': 'Filter by alert level' } }, [
            el('option', { text: 'All Levels', attrs: { value: '' } }),
            el('option', { text: 'Info', attrs: { value: 'info' } }),
            el('option', { text: 'Warning', attrs: { value: 'warning' } }),
            el('option', { text: 'Critical', attrs: { value: 'critical' } })
        ]);
        levelSelect.addEventListener('change', function () {
            state.alertLevelFilter = levelSelect.value;
            state.page = 1;
            loadWatchlists();
        });

        const statusSelect = el('select', { className: 'form-control', attrs: { 'aria-label': 'Filter by status' } }, [
            el('option', { text: 'All (live)', attrs: { value: 'all' } }),
            el('option', { text: 'Active', attrs: { value: 'active' } }),
            el('option', { text: 'Inactive', attrs: { value: 'inactive' } }),
            el('option', { text: 'Deleted', attrs: { value: 'deleted' } })
        ]);
        statusSelect.addEventListener('change', function () {
            state.statusFilter = statusSelect.value;
            state.page = 1;
            loadWatchlists();
        });

        const sortSelect = el('select', { className: 'form-control', attrs: { 'aria-label': 'Sort watchlists' } }, [
            el('option', { text: 'Name A-Z', attrs: { value: 'name:asc' } }),
            el('option', { text: 'Name Z-A', attrs: { value: 'name:desc' } }),
            el('option', { text: 'Recently updated', attrs: { value: 'updated_at:desc' } }),
            el('option', { text: 'Newest first', attrs: { value: 'created_at:desc' } })
        ]);
        sortSelect.addEventListener('change', function () {
            const parts = sortSelect.value.split(':');
            state.sortBy = parts[0];
            state.sortOrder = parts[1] || 'asc';
            state.page = 1;
            loadWatchlists();
        });

        const prevBtn = el('button', { className: 'watchlist-btn', text: '← Prev', attrs: { type: 'button', id: 'watchlist-prev-btn' } });
        const nextBtn = el('button', { className: 'watchlist-btn', text: 'Next →', attrs: { type: 'button', id: 'watchlist-next-btn' } });
        const pageInfo = el('span', { className: 'watchlist-page-info', attrs: { id: 'watchlist-page-info', 'aria-live': 'polite' } });
        prevBtn.addEventListener('click', function () {
            if (state.page > 1) { state.page -= 1; loadWatchlists(); }
        });
        nextBtn.addEventListener('click', function () {
            if (state.page < state.totalPages) { state.page += 1; loadWatchlists(); }
        });

        const bar = el('div', {}, [searchInput, levelSelect, statusSelect, sortSelect, prevBtn, pageInfo, nextBtn]);
        bar.style.cssText = 'display:flex;flex-wrap:wrap;gap:0.6rem;align-items:center;margin-bottom:1rem;';
        host.append(bar);
    }

    function updatePagination() {
        const info = document.getElementById('watchlist-page-info');
        if (info) {
            info.textContent = state.total === 0 ? 'No watchlists'
                : 'Page ' + state.page + ' of ' + state.totalPages + ' (' + state.total + ' total)';
        }
        const prev = document.getElementById('watchlist-prev-btn');
        const next = document.getElementById('watchlist-next-btn');
        if (prev) prev.disabled = state.page <= 1;
        if (next) next.disabled = state.page >= state.totalPages;
    }

    // ============================================
    // Listing
    // ============================================

    function listStateContainer() {
        let node = document.getElementById('watchlist-list-state');
        if (!node) {
            node = el('div', { attrs: { id: 'watchlist-list-state', 'aria-live': 'polite' } });
            node.style.cssText = 'padding:1rem 0;color:rgba(255,255,255,0.75);';
            const grid = document.getElementById('watchlist-grid');
            if (grid && grid.parentNode) grid.parentNode.insertBefore(node, grid);
        }
        return node;
    }

    async function loadWatchlists() {
        const req = beginRequest('list');
        const stateNode = listStateContainer();
        stateNode.textContent = 'Loading watchlists...';
        try {
            const params = {
                page: state.page,
                page_size: PAGE_SIZE,
                search: state.search || undefined,
                alert_level: state.alertLevelFilter || undefined,
                sort_by: state.sortBy,
                sort_order: state.sortOrder
            };
            if (state.statusFilter === 'active') params.is_active = 'true';
            else if (state.statusFilter === 'inactive') params.is_active = 'false';
            else if (state.statusFilter === 'deleted') { params.include_deleted = 'true'; }

            const data = await api('/api/watchlists', { signal: req.signal, params: params });
            if (!req.isCurrent()) return; // stale response never overwrites newer state

            // Envelope (paginated) or legacy array — both accepted
            const rawItems = Array.isArray(data) ? data : ((data && Array.isArray(data.items)) ? data.items : []);
            let items = rawItems.map(normalizeWatchlist).filter(Boolean);
            if (state.statusFilter === 'deleted') items = items.filter(function (w) { return w.deletedAt; });

            state.total = Array.isArray(data) ? items.length : toNonNegativeInteger(data.total, items.length);
            state.totalPages = Array.isArray(data) ? 1 : (toNonNegativeInteger(data.total_pages, 1) || 1);
            state.watchlists = new Map(items.map(function (w) { return [w.id, w]; }));

            renderWatchlists(items);
            updatePagination();
            stateNode.textContent = items.length === 0
                ? (state.search ? 'Search returned no results' : 'No watchlists found') : '';
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            stateNode.textContent = 'Failed to load watchlists' + (err.referenceId ? ' (Reference: ' + err.referenceId + ')' : '');
            showNotification('Failed to load watchlists', 'error');
        }
    }

    function monitoringText(wl) {
        if (wl.deletedAt) return 'Deleted · matching stopped. Historical alerts are retained.';
        if (!wl.isActive) return 'Paused · this list does not generate new matches.';
        if (!wl.entriesCount) return 'No eligible identities · add an identity to begin monitoring.';
        return 'Monitoring enabled · eligible identities can trigger watchlist alerts.';
    }

    function renderWatchlists(items) {
        const summary = document.getElementById('watchlist-summary');
        if (summary) summary.replaceChildren(
            el('div', { className: 'wl-summary-heading' }, [
                el('h2', { text: 'At a glance' }),
                el('p', { text: 'On this page · reflects your current filters' })
            ]),
            el('div', { className: 'wl-summary-metrics' }, [
                stat(String(items.length), 'Watchlists shown'),
                stat(String(items.filter(w => w.isActive && !w.deletedAt).length), 'Active lists'),
                stat(String(items.reduce((n, w) => n + w.entriesCount, 0)), 'Eligible memberships'),
                stat(String(items.reduce((n, w) => n + w.alertsToday, 0)), 'Alerts today · UTC')
            ])
        );
        const grid = document.getElementById('watchlist-grid');
        if (!grid) return;
        const createCard = grid.querySelector('.create-card');
        Array.from(grid.children).forEach(function (child) {
            if (!child.classList.contains('create-card')) child.remove();
        });

        for (const wl of items) {
            grid.insertBefore(buildWatchlistCard(wl), createCard);
        }
    }

    function stat(value, label) {
        return el('div', { className: 'stat-item' }, [
            el('div', { className: 'value', text: value }),
            el('div', { className: 'label', text: label })
        ]);
    }

    function actionButton(iconClass, label, handler, title) {
        const btn = el('button', { className: 'watchlist-btn', attrs: { type: 'button', title: title || label } },
            [faIcon(iconClass), document.createTextNode(' ' + label)]);
        btn.addEventListener('click', handler);
        return btn;
    }

    function buildWatchlistCard(wl) {
        const iconWrap = el('div', { className: 'watchlist-icon' }, faIcon('fas fa-' + normalizeIcon(wl.icon)));
        iconWrap.style.background = hexToRgba(wl.color, 0.13);
        iconWrap.style.color = normalizeColor(wl.color);

        const badge = el('span', {
            className: 'alert-level-badge ' + normalizeAlertLevel(wl.alertLevel),
            text: ALERT_LEVEL_LABELS[wl.alertLevel] || 'Info'
        });

        const header = el('div', { className: 'watchlist-header' }, [
            iconWrap,
            el('div', { className: 'watchlist-title' }, [
                el('h3', { text: wl.name }),
                el('p', { text: wl.description || 'No description' })
            ]),
            badge
        ]);

        const statusText = wl.deletedAt ? 'Deleted' : (wl.isActive ? 'Active' : 'Inactive');
        const stats = el('div', { className: 'watchlist-stats' }, [
            stat(String(wl.entriesCount), 'Eligible identities'),
            stat(String(wl.alertsToday), 'Today · UTC'),
            stat(String(wl.totalAlerts), 'All-time alerts')
        ]);

        const actions = el('div', { className: 'watchlist-actions' });
        if (wl.deletedAt) {
            actions.append(
                actionButton('fas fa-eye', 'View', function () { openDetailDrawer(wl.id); }),
                actionButton('fas fa-trash-restore', 'Restore', function () { restoreWatchlist(wl.id); })
            );
        } else {
            actions.append(
                actionButton('fas fa-eye', 'View', function () { openDetailDrawer(wl.id); }),
                actionButton('fas fa-edit', 'Edit', function () { openEditModal(wl.id); }),
                actionButton(wl.isActive ? 'fas fa-pause' : 'fas fa-play',
                    wl.isActive ? 'Deactivate' : 'Activate',
                    function () { toggleWatchlistStatus(wl.id); }),
                actionButton('fas fa-trash', 'Delete', function () { deleteWatchlistFlow(wl.id); })
            );
        }

        const card = el('div', { className: 'watchlist-card' + (wl.deletedAt ? ' watchlist-deleted' : '') },
            [header,
                el('div', { className: 'wl-status ' + (wl.isActive && !wl.deletedAt ? 'is-active' : '') }, [
                    el('span', { className: 'wl-status-dot', attrs: { 'aria-hidden': 'true' } }),
                    el('strong', { text: statusText }),
                    el('span', { text: wl.isActive && !wl.deletedAt ? 'Matching enabled' : 'Matching stopped' })
                ]), stats, el('p', { className: 'wl-help', text: monitoringText(wl) }), actions]);
        if (wl.deletedAt) card.style.opacity = '0.6';
        card.dataset.watchlistId = wl.id;

        const meta = el('div', {
            className: 'watchlist-meta',
            text: 'Updated ' + fmtDateTime(wl.updatedAt) +
                (wl.lastAlertAt ? ' — last alert ' + fmtDateTime(wl.lastAlertAt) : '')
        });

        card.append(meta);
        return card;
    }

    // ============================================
    // Create / Edit modal (accessible, versioned)
    // ============================================

    let modalKeyHandler = null;

    function fieldError(fieldId, message) {
        const field = document.getElementById(fieldId);
        if (!field) return;
        clearFieldError(fieldId);
        const err = el('div', { className: 'field-error', text: message, attrs: { role: 'alert' } });
        err.style.cssText = 'color:#ff6b6b;font-size:0.8rem;margin-top:0.25rem;';
        err.dataset.errorFor = fieldId;
        field.parentNode.appendChild(err);
    }

    function clearFieldError(fieldId) {
        document.querySelectorAll('[data-error-for="' + fieldId + '"]').forEach(function (n) { n.remove(); });
    }

    function clearAllFieldErrors() {
        document.querySelectorAll('.field-error').forEach(function (n) { n.remove(); });
    }

    function openModal() {
        const overlay = document.getElementById('modal-overlay');
        const content = overlay && overlay.querySelector('.modal-content');
        if (!overlay) return;
        // Shared lifecycle, with the page's own rule preserved as a veto:
        // a save in flight must not be abandoned by Escape or a backdrop
        // click. That guard used to live inside this page's private keydown
        // handler; handing the key to ModalStack without canClose would have
        // silently dropped it.
        window.ModalStack.open(overlay, {
            backdropClose: true,
            canClose: () => !state.saving,
            onClose: () => closeModal()
        });
        overlay.classList.add('active');
        if (content) {
            content.setAttribute('role', 'dialog');
            content.setAttribute('aria-modal', 'true');
            content.setAttribute('aria-labelledby', 'modal-title');
        }
        const nameInput = document.getElementById('watchlist-name');
        if (nameInput) nameInput.focus();
    }

    function closeModal() {
        if (state.saving) return; // never close silently while saving
        const overlay = document.getElementById('modal-overlay');
        if (!overlay) return;
        if (window.ModalStack.isOpen(overlay)) {
            window.ModalStack.close(overlay);   // re-enters here via onClose
            return;
        }
        overlay.classList.remove('active');
        if (modalKeyHandler) { document.removeEventListener('keydown', modalKeyHandler); modalKeyHandler = null; }
        // Business cleanup, preserved: stale field errors must not greet the
        // next open.
        clearAllFieldErrors();
    }

    function openCreateModal() {
        document.getElementById('modal-title').textContent = 'Create Watchlist';
        document.getElementById('watchlist-id').value = '';
        document.getElementById('watchlist-form').reset();
        state.selectedColor = DEFAULT_COLOR;
        state.editingVersion = null;
        updateColorPicker();
        openModal();
    }

    function openEditModal(id) {
        const wl = state.watchlists.get(normalizeId(id));
        if (!wl) return;
        document.getElementById('modal-title').textContent = 'Edit Watchlist';
        document.getElementById('watchlist-id').value = wl.id;
        document.getElementById('watchlist-name').value = wl.name;
        document.getElementById('watchlist-description').value = wl.description;
        document.getElementById('watchlist-alert-level').value = wl.alertLevel;
        state.selectedColor = wl.color;
        state.editingVersion = wl.version;
        updateColorPicker();
        openModal();
    }

    function updateColorPicker() {
        document.querySelectorAll('.color-option').forEach(function (opt) {
            opt.classList.toggle('active', opt.dataset.color === state.selectedColor);
        });
    }

    function validateForm(name, description) {
        clearAllFieldErrors();
        let ok = true;
        if (name.length < 2 || name.length > 100) {
            fieldError('watchlist-name', 'Name must be 2-100 characters');
            ok = false;
        }
        if (description.length > 1000) {
            fieldError('watchlist-description', 'Description must be at most 1,000 characters');
            ok = false;
        }
        const level = document.getElementById('watchlist-alert-level').value;
        if (!ALERT_LEVELS.has(level)) {
            fieldError('watchlist-alert-level', 'Unsupported alert level');
            ok = false;
        }
        return ok;
    }

    async function submitWatchlistForm() {
        if (state.saving) return; // double-submission guard
        const id = normalizeId(document.getElementById('watchlist-id').value);
        const name = document.getElementById('watchlist-name').value.trim().replace(/\s+/g, ' ');
        const description = document.getElementById('watchlist-description').value.trim();
        if (!validateForm(name, description)) return;

        const payload = {
            name: name,
            description: description || null,
            alert_level: normalizeAlertLevel(document.getElementById('watchlist-alert-level').value),
            color: normalizeColor(state.selectedColor)
        };
        if (id && state.editingVersion) payload.version = state.editingVersion;

        state.saving = true;
        const submitBtn = document.querySelector('#watchlist-form .submit-btn');
        if (submitBtn) submitBtn.disabled = true;
        try {
            if (id) {
                await api('/api/watchlists/' + encodeURIComponent(id), { method: 'PUT', body: payload });
                showNotification('Watchlist updated', 'success');
            } else {
                await api('/api/watchlists', { method: 'POST', body: payload });
                showNotification('Watchlist created', 'success');
            }
            state.saving = false;
            closeModal();
            loadWatchlists();
        } catch (err) {
            state.saving = false;
            if (err.code === 'VERSION_CONFLICT') {
                fieldError('watchlist-name',
                    'This watchlist was modified by another administrator. Reload the latest version before saving.');
            } else if (err.code === 'NAME_CONFLICT' || err.status === 409) {
                fieldError('watchlist-name', 'A watchlist with this name already exists');
            } else if (err.status === 422 || err.code === 'VALIDATION_ERROR') {
                fieldError('watchlist-name', safeText(err.message, 'Validation failed'));
            } else if (!err.aborted) {
                showNotification('Failed to save watchlist' + (err.referenceId ? ' (Reference: ' + err.referenceId + ')' : ''), 'error');
            }
        } finally {
            if (submitBtn) submitBtn.disabled = false;
        }
    }

    // ============================================
    // Status toggle / delete / restore flows
    // ============================================

    async function toggleWatchlistStatus(id) {
        const wl = state.watchlists.get(normalizeId(id));
        if (!wl) return;
        const target = !wl.isActive;
        const confirmed = await showDialog(
            (target ? 'Activate' : 'Deactivate') + ' watchlist?',
            [el('p', {
                text: target
                    ? '"' + wl.name + '" will resume matching detections and generating alerts.'
                    : '"' + wl.name + '" will stop matching detections. No watchlist alerts will be generated while inactive.'
            })],
            { confirmLabel: target ? 'Activate' : 'Deactivate' });
        if (!confirmed) return;
        try {
            await api('/api/watchlists/' + encodeURIComponent(wl.id) + '/status', {
                method: 'PATCH',
                body: { is_active: target, version: wl.version }
            });
            showNotification('Watchlist ' + (target ? 'activated' : 'deactivated'), 'success');
            loadWatchlists();
        } catch (err) {
            if (err.code === 'VERSION_CONFLICT') {
                showNotification('This watchlist was modified by another administrator — list reloaded', 'warning');
                loadWatchlists();
            } else if (!err.aborted) {
                showNotification('Failed to change status', 'error');
            }
        }
    }

    async function deleteWatchlistFlow(id) {
        const wl = state.watchlists.get(normalizeId(id));
        if (!wl) return;
        let impact = null;
        try {
            impact = await api('/api/watchlists/' + encodeURIComponent(wl.id) + '/deletion-impact');
        } catch (err) {
            if (!err.aborted) showNotification('Could not load deletion impact', 'error');
            return;
        }
        const reasonInput = el('input', {
            className: 'form-control',
            attrs: { type: 'text', placeholder: 'Reason (optional)', maxlength: '500', 'aria-label': 'Deletion reason' }
        });
        const confirmed = await showDialog('Delete "' + wl.name + '"?', [
            el('p', { text: 'This is a SOFT delete: matching stops immediately, but history is preserved and the watchlist can be restored.' }),
            el('ul', {}, [
                el('li', { text: 'Entries: ' + toNonNegativeInteger(impact.entries) }),
                el('li', { text: 'Active entries: ' + toNonNegativeInteger(impact.active_entries) }),
                el('li', { text: 'Historical alerts kept: ' + toNonNegativeInteger(impact.alerts) })
            ]),
            reasonInput
        ], { confirmLabel: 'Delete watchlist' });
        if (!confirmed) return;
        try {
            await api('/api/watchlists/' + encodeURIComponent(wl.id), {
                method: 'DELETE',
                params: { reason: reasonInput.value.trim() || undefined }
            });
            showNotification('Watchlist deleted (restorable)', 'success');
            loadWatchlists();
        } catch (err) {
            if (!err.aborted) showNotification('Failed to delete watchlist', 'error');
        }
    }

    async function restoreWatchlist(id) {
        const wl = state.watchlists.get(normalizeId(id));
        if (!wl) return;
        const confirmed = await showDialog('Restore "' + wl.name + '"?',
            [el('p', { text: 'The watchlist becomes active again and resumes matching detections.' })],
            { confirmLabel: 'Restore' });
        if (!confirmed) return;
        try {
            await api('/api/watchlists/' + encodeURIComponent(wl.id) + '/restore', { method: 'POST' });
            showNotification('Watchlist restored', 'success');
            loadWatchlists();
        } catch (err) {
            if (err.code === 'NAME_CONFLICT') {
                showNotification('A live watchlist with this name now exists — rename it first', 'warning');
            } else if (!err.aborted) {
                showNotification('Failed to restore watchlist', 'error');
            }
        }
    }

    // ============================================
    // Detail drawer (real view + entry management)
    // ============================================

    let drawerNode = null;
    let drawerKeyHandler = null;
    let drawerTrigger = null;
    let drawerWatchlistId = null;
    let drawerOverflow = "";

    function closeDrawer() {
        ['detail', 'drawerEntries', 'drawerActivity', 'drawerStats', 'entrySearch'].forEach(function (key) { beginRequest(key); });
        drawerWatchlistId = null;
        const node = drawerNode;
        drawerNode = null;
        if (node) {
            if (window.ModalStack) window.ModalStack.close(node);
            else document.body.style.overflow = drawerOverflow;
            node.remove();
        }
        if (drawerTrigger && drawerTrigger.isConnected) drawerTrigger.focus();
        if (drawerKeyHandler) { document.removeEventListener('keydown', drawerKeyHandler); drawerKeyHandler = null; }
    }

    async function openDetailDrawer(id) {
        const watchlistId = normalizeId(id);
        if (!watchlistId) return;
        closeDrawer();

        drawerTrigger = document.activeElement;
        drawerWatchlistId = watchlistId;
        drawerOverflow = document.body.style.overflow;
        if (!window.ModalStack) document.body.style.overflow = 'hidden';
        const panel = el('section', { className: 'wl-drawer', attrs: { tabindex: '-1' } });
        const closeBtn = el('button', { className: 'wl-close', text: '×', attrs: { type: 'button', 'aria-label': 'Close watchlist details' } });
        closeBtn.addEventListener('click', closeDrawer);
        panel.append(el('div', { className: 'wl-drawer-bar' }, [
            el('span', { text: 'WATCHLIST DETAILS' }), closeBtn
        ]));
        const body = el('div', { className: 'wl-drawer-body', attrs: { 'aria-live': 'polite' } });
        body.append(el('p', { text: 'Loading watchlist details…', attrs: { id: 'wl-detail-title' } }));
        panel.append(body);
        const backdrop = el('div', { className: 'wl-drawer-backdrop', attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'wl-detail-title' } }, panel);
        if (!window.ModalStack) backdrop.addEventListener('click', function (e) { if (e.target === backdrop && !activeDialog) closeDrawer(); });
        drawerNode = backdrop;
        drawerKeyHandler = function (e) {
            if (activeDialog) return;
            if (e.key === 'Escape') { e.preventDefault(); closeDrawer(); }
            if (e.key === 'Tab') {
                const targets = Array.from(panel.querySelectorAll('a[href],button:not([disabled]),input,select,[tabindex="0"]'));
                const first = targets[0], last = targets[targets.length - 1];
                if (e.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
                else if (!e.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
            }
        };
        if (!window.ModalStack) document.addEventListener('keydown', drawerKeyHandler);
        document.body.appendChild(backdrop);
        if (window.ModalStack) window.ModalStack.open(backdrop, { backdropClose: true, onClose: closeDrawer });
        closeBtn.focus();

        const req = beginRequest('detail');
        try {
            const raw = await api('/api/watchlists/' + encodeURIComponent(watchlistId), { signal: req.signal });
            if (!req.isCurrent() || !drawerNode) return;
            const wl = normalizeWatchlist(raw);
            if (!wl) { body.replaceChildren(el('p', { text: 'Invalid watchlist data' })); return; }
            renderDrawer(body, wl);
            loadDrawerEntries(wl.id, body, 1);
            loadDrawerActivity(wl.id, body);
            loadDrawerStats(wl.id, body);
        } catch (err) {
            if (err.aborted || !req.isCurrent() || !drawerNode) return;
            body.replaceChildren(el('p', {
                text: err.status === 404 ? 'Watchlist not found'
                    : 'Failed to load details' + (err.referenceId ? ' (Reference: ' + err.referenceId + ')' : '')
            }));
        }
    }

    function infoRow(label, value) {
        return el('div', { className: 'wl-info-row' }, [el('dt', { text: label }), el('dd', { text: value })]);
    }

    function renderDrawer(body, wl) {
        body.replaceChildren(
            el('header', { className: 'wl-detail-heading' }, [
                el('div', { className: 'wl-badges' }, [
                    el('span', { className: 'alert-level-badge ' + wl.alertLevel, text: ALERT_LEVEL_LABELS[wl.alertLevel] + ' alerts' }),
                    el('span', { className: 'wl-status-pill', text: wl.deletedAt ? 'Deleted' : wl.isActive ? 'Active' : 'Paused' })
                ]),
                el('h2', { text: wl.name, attrs: { id: 'wl-detail-title' } }),
                el('p', { className: 'wl-description', text: wl.description || 'No description added.' }),
                el('p', { className: 'wl-monitoring-note', text: monitoringText(wl) })
            ]),
            el('div', { className: 'watchlist-stats wl-detail-metrics' }, [
                stat(String(wl.entriesCount), 'Eligible identities'), stat(String(wl.alertsToday), 'Today · UTC'), stat(String(wl.totalAlerts), 'All-time alerts')
            ]),
            el('div', { className: 'wl-entry-health', attrs: { id: 'wl-entry-health' }, text: 'Loading entry and review status…' }),
            el('section', { className: 'wl-section' }, [
                el('div', { className: 'wl-section-heading' }, [el('h3', { text: 'Recent alert activity' }), el('a', { text: 'Live alerts ↗', attrs: { href: '/admin/live-alerts' } })]),
                el('p', { className: 'wl-help', text: 'Latest 8 recorded matches. Times use your browser timezone; “today” is counted in UTC.' }),
                el('div', { attrs: { id: 'wl-recent-alerts' }, text: 'Loading recent alerts…' })
            ]),
            el('section', { className: 'wl-section' }, [
                el('h3', { text: 'Identities on this list' }),
                el('p', { className: 'wl-help', text: 'All entries, including inactive and expired identities. Only eligible entries can match while the list is active.' }),
                el('div', { attrs: { id: 'drawer-entries', 'data-readonly': wl.deletedAt ? 'true' : 'false' } }, el('p', { text: 'Loading entries…' }))
            ]),
            wl.deletedAt ? null : buildAddEntrySection(wl.id),
            el('section', { className: 'wl-section wl-record-details' }, [
                el('h3', { text: 'List details' }),
                el('dl', {}, [infoRow('Last alert', wl.lastAlertAt ? fmtDateTime(wl.lastAlertAt) : 'No alerts recorded'), infoRow('Created', fmtDateTime(wl.createdAt)), infoRow('Updated', fmtDateTime(wl.updatedAt)), wl.deletedAt ? infoRow('Deleted', fmtDateTime(wl.deletedAt)) : null, wl.deletionReason ? infoRow('Deletion reason', wl.deletionReason) : null])
            ])
        );
    }

    async function loadDrawerStats(id, body) {
        const req = beginRequest('drawerStats');
        const host = body.querySelector('#wl-entry-health');
        try {
            const data = await api('/api/watchlists/' + encodeURIComponent(id) + '/stats', { signal: req.signal });
            if (!req.isCurrent() || drawerWatchlistId !== id) return;
            host.replaceChildren(
                el('strong', { text: toNonNegativeInteger(data.unacknowledged_alerts) + ' alerts awaiting acknowledgement' }),
                el('span', { text: toNonNegativeInteger(data.active_entries) + ' eligible / ' + toNonNegativeInteger(data.total_entries) + ' total entries · ' + toNonNegativeInteger(data.expired_entries) + ' expired' })
            );
        } catch (err) { if (!err.aborted && req.isCurrent()) host.textContent = 'Entry and review status unavailable. Reopen this panel to retry.'; }
    }

    async function loadDrawerActivity(id, body) {
        const req = beginRequest('drawerActivity');
        const host = body.querySelector('#wl-recent-alerts');
        try {
            const [data, pipelines] = await Promise.all([
                api('/api/watchlist-alerts', { signal: req.signal, params: { watchlist_id: id, limit: 8 } }),
                api('/api/pipelines', { signal: req.signal }).catch(function () { return []; })
            ]);
            const cameraNames = new Map((Array.isArray(pipelines) ? pipelines : []).map(function (camera) {
                const name = safeText(camera.pipeline_name || camera.location_name).trim();
                return [String(camera.pipeline_id), /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(name) ? '' : name];
            }));
            if (!req.isCurrent() || drawerWatchlistId !== id) return;
            if (!Array.isArray(data) || !data.length) { host.replaceChildren(el('p', { className: 'wl-empty', text: 'No alerts recorded yet. Matches will appear here when an eligible identity triggers an alert.' })); return; }
            host.replaceChildren(...data.map(function (alert) {
                return el('article', { className: 'wl-activity-row' }, [
                    el('span', { className: 'wl-activity-dot ' + (alert.acknowledged ? 'is-read' : ''), attrs: { 'aria-hidden': 'true' } }),
                    el('div', {}, [el('strong', { text: safeText(alert.identity_name) || 'Unknown identity' }),
                        el('p', { text: (alert.pipeline_id ? (cameraNames.get(String(alert.pipeline_id)) || 'Camera name unavailable') : 'No camera recorded') + ' · ' + safeText(alert.triggered_by, 'Match') }),
                        el('time', { text: fmtDateTime(alert.created_at) })]),
                    el('span', { className: 'wl-review-label', text: alert.acknowledged ? 'Acknowledged' : 'Needs review' })
                ]);
            }));
        } catch (err) { if (!err.aborted && req.isCurrent()) host.replaceChildren(el('p', { className: 'wl-empty', text: 'Recent alerts could not be loaded. Reopen this panel to retry.' })); }
    }

    async function loadDrawerEntries(watchlistId, body, page) {
        const container = body.querySelector('#drawer-entries');
        if (!container) return;
        const req = beginRequest('drawerEntries');
        try {
            const data = await api('/api/watchlists/' + encodeURIComponent(watchlistId) + '/entries', {
                signal: req.signal,
                params: { page: page, page_size: ENTRY_PAGE_SIZE, include_inactive: true, include_expired: true }
            });
            if (!req.isCurrent() || !drawerNode) return;
            const items = (data && Array.isArray(data.items)) ? data.items : [];
            if (!items.length) {
                container.replaceChildren(el('p', { text: 'No identities on this watchlist yet' }));
                return;
            }
            const rows = items.map(function (entry) {
                const identityId = normalizeId(entry.identity_id);
                const name = safeText(entry.identity_name, '') || ('Unknown #' + String(identityId || '').slice(0, 8));
                const removeBtn = el('button', {
                    className: 'watchlist-btn', text: 'Remove',
                    attrs: { type: 'button', 'aria-label': 'Remove ' + name }
                });
                removeBtn.addEventListener('click', async function () {
                    const ok = await showDialog('Remove entry?',
                        [el('p', { text: 'Remove "' + name + '" from this watchlist?' })],
                        { confirmLabel: 'Remove' });
                    if (!ok) return;
                    try {
                        await api('/api/watchlists/' + encodeURIComponent(watchlistId) +
                            '/entries/' + encodeURIComponent(identityId), { method: 'DELETE' });
                        showNotification('Entry removed', 'success');
                        if (drawerWatchlistId === watchlistId) openDetailDrawer(watchlistId);
                        loadWatchlists();
                    } catch (err) {
                        if (!err.aborted) showNotification('Failed to remove entry', 'error');
                    }
                });
                const expired = parseTimestamp(entry.expires_at);
                const status = expired && expired <= new Date() ? 'Expired' : entry.is_active ? 'Eligible' : 'Inactive';
                const row = el('article', { className: 'wl-entry-card' }, [
                    el('div', { className: 'wl-entry-top' }, [
                        el('span', { className: 'wl-avatar', text: name.slice(0, 2).toUpperCase(), attrs: { 'aria-hidden': 'true' } }),
                        el('div', { className: 'wl-entry-name' }, [el('strong', { text: name }), el('span', { text: safeText(entry.identity_type, 'unknown') + ' identity' })]),
                        el('span', { className: 'wl-status-pill', text: status })
                    ]),
                    el('dl', { className: 'wl-entry-meta' }, [infoRow('Priority', safeText(entry.priority, 'normal')), infoRow('Added', fmtDateTime(entry.added_at)), infoRow('Expires', entry.expires_at ? fmtDateTime(entry.expires_at) : 'No expiry')]),
                    entry.notes ? el('p', { className: 'wl-entry-note', text: 'Notes: ' + safeText(entry.notes) }) : null,
                    entry.action_instructions ? el('p', { className: 'wl-entry-note', text: 'Instructions: ' + safeText(entry.action_instructions) }) : null,
                    container.dataset.readonly === 'true' ? null : removeBtn
                ]);
                return row;
            });
            const previousMore = container.querySelector('.wl-load-more');
            if (previousMore) previousMore.remove();
            if (page === 1) container.replaceChildren(...rows);
            else container.append(...rows);
            if (toNonNegativeInteger(data.total_pages, 1) > page) {
                const more = el('button', { className: 'watchlist-btn wl-load-more', text: 'Load more entries', attrs: { type: 'button' } });
                more.addEventListener('click', function () { more.disabled = true; loadDrawerEntries(watchlistId, body, page + 1); });
                container.append(more);
            }
        } catch (err) {
            if (err.aborted || !req.isCurrent() || !drawerNode) return;
            container.replaceChildren(el('p', { text: 'Failed to load entries' }));
        }
    }

    function buildAddEntrySection(watchlistId) {
        const searchInput = el('input', {
            className: 'form-control',
            attrs: { type: 'text', placeholder: 'Search identities to add...', 'aria-label': 'Search identities', autocomplete: 'off' }
        });
        const prioritySelect = el('select', { className: 'form-control', attrs: { 'aria-label': 'Entry priority' } },
            ENTRY_PRIORITIES.map(function (p) {
                return el('option', { text: p, attrs: p === 'normal' ? { value: p, selected: 'selected' } : { value: p } });
            }));
        const results = el('div', { attrs: { role: 'listbox', 'aria-label': 'Identity search results' } });
        results.style.cssText = 'max-height:220px;overflow-y:auto;margin-top:0.4rem;';
        let timer = null;

        searchInput.addEventListener('input', function () {
            if (timer) window.clearTimeout(timer);
            timer = window.setTimeout(async function () {
                if (!searchInput.isConnected || drawerWatchlistId !== watchlistId) return;
                const q = searchInput.value.trim();
                if (q.length < 2) { results.replaceChildren(); return; }
                const req = beginRequest('entrySearch');
                results.replaceChildren(el('p', { text: 'Searching...' }));
                try {
                    const data = await api('/api/admin/identities', {
                        signal: req.signal,
                        params: { page: 1, page_size: 10, q: q }
                    });
                    if (!req.isCurrent() || !searchInput.isConnected || drawerWatchlistId !== watchlistId) return;
                    const items = (data && Array.isArray(data.items)) ? data.items : [];
                    if (!items.length) { results.replaceChildren(el('p', { text: 'No identities found' })); return; }
                    results.replaceChildren.apply(results, items.map(function (identity) {
                        const identityId = normalizeId(identity.id);
                        const label = safeText(identity.display_name, '') || ('Unknown #' + String(identityId || '').slice(0, 8));
                        const addBtn = el('button', {
                            className: 'watchlist-btn',
                            text: 'Add ' + label,
                            attrs: { type: 'button', role: 'option' }
                        });
                        addBtn.style.cssText = 'display:block;width:100%;text-align:left;margin:0.2rem 0;';
                        addBtn.addEventListener('click', async function () {
                            addBtn.disabled = true; // duplicate-click guard
                            try {
                                await api('/api/watchlists/' + encodeURIComponent(watchlistId) + '/entries', {
                                    method: 'POST',
                                    body: { identity_id: identityId, priority: prioritySelect.value }
                                });
                                showNotification('Added "' + label + '" to watchlist', 'success');
                                if (drawerWatchlistId === watchlistId) openDetailDrawer(watchlistId);
                                loadWatchlists();
                            } catch (err) {
                                if (!err.aborted) showNotification('Failed to add entry', 'error');
                            } finally {
                                addBtn.disabled = false;
                            }
                        });
                        return addBtn;
                    }));
                } catch (err) {
                    if (err.aborted || !req.isCurrent()) return;
                    results.replaceChildren(el('p', { text: 'Identity search failed' }));
                }
            }, SEARCH_DEBOUNCE_MS);
        });

        const section = el('section', { className: 'wl-section wl-add-entry' }, [
            el('h3', { text: 'Add an identity' }),
            el('p', { className: 'wl-help', text: 'Search by name, choose a priority, then select an identity to add it to this list.' }),
            el('label', {}, [el('span', { text: 'Identity search' }), searchInput]),
            el('label', {}, [el('span', { text: 'Entry priority' }), prioritySelect]), results
        ]);
        return section;
    }

    // ============================================
    // Wiring
    // ============================================

    function setupEventListeners() {
        const createCard = document.querySelector('.create-card');
        if (createCard && !createCard.dataset.listenerAttached) {
            createCard.addEventListener('click', openCreateModal);
            createCard.setAttribute('role', 'button');
            createCard.setAttribute('tabindex', '0');
            createCard.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openCreateModal(); }
            });
            createCard.dataset.listenerAttached = 'true';
        }

        const modalClose = document.getElementById('modal-close-btn');
        if (modalClose && !modalClose.dataset.listenerAttached) {
            modalClose.addEventListener('click', closeModal);
            modalClose.dataset.listenerAttached = 'true';
        }

        const colorPicker = document.getElementById('color-picker');
        if (colorPicker && !colorPicker.dataset.listenerAttached) {
            colorPicker.addEventListener('click', function (e) {
                const opt = e.target.closest('.color-option');
                if (opt) {
                    state.selectedColor = normalizeColor(opt.dataset.color);
                    updateColorPicker();
                }
            });
            colorPicker.dataset.listenerAttached = 'true';
        }

        const form = document.getElementById('watchlist-form');
        if (form && !form.dataset.listenerAttached) {
            form.addEventListener('submit', function (e) {
                e.preventDefault();
                submitWatchlistForm();
            });
            form.dataset.listenerAttached = 'true';
        }

        // Backdrop clicks are ModalStack's (opted in at open time, and subject
        // to the same canClose veto), so no listener is registered here.
    }

    function destroy() {
        abortAllRequests();
        closeDrawer();
        closeDialog();
        if (state.searchTimer) window.clearTimeout(state.searchTimer);
    }

    document.addEventListener('DOMContentLoaded', function () {
        setupEventListeners();
        buildToolbar();
        loadWatchlists();
    });

    window.addEventListener('pagehide', destroy);
})();
