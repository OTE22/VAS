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
                method: method,
                credentials: 'include',
                cache: 'no-store',
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
            ';color:#fff;border-radius:6px;z-index:10010;box-shadow:0 4px 6px rgba(0,0,0,0.3);font-weight:600;';
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
        document.removeEventListener('keydown', activeDialog.keyHandler);
        activeDialog.node.remove();
        if (activeDialog.previousFocus && activeDialog.previousFocus.focus) activeDialog.previousFocus.focus();
        activeDialog = null;
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
                'align-items:center;justify-content:center;z-index:10005;';

            function finish(result) { closeDialog(); resolve(result); }
            cancelBtn.addEventListener('click', function () { finish(false); });
            if (confirmBtn) confirmBtn.addEventListener('click', function () { finish(true); });
            backdrop.addEventListener('click', function (e) { if (e.target === backdrop) finish(false); });

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
            document.addEventListener('keydown', keyHandler);
            activeDialog = { node: backdrop, keyHandler: keyHandler, previousFocus: document.activeElement };
            document.body.appendChild(backdrop);
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

    function renderWatchlists(items) {
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
            stat(String(wl.entriesCount), 'Entries'),
            stat(String(wl.alertsToday), 'Alerts Today'),
            stat(statusText, 'Status')
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
            [header, stats, actions]);
        if (wl.deletedAt) card.style.opacity = '0.6';
        card.dataset.watchlistId = wl.id;

        const meta = el('div', {
            className: 'watchlist-meta',
            text: 'Updated ' + fmtDateTime(wl.updatedAt) +
                (wl.lastAlertAt ? ' — last alert ' + fmtDateTime(wl.lastAlertAt) : '')
        });
        meta.style.cssText = 'font-size:0.72rem;color:rgba(255,255,255,0.45);margin-top:0.4rem;';
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

    function closeDrawer() {
        if (drawerKeyHandler) { document.removeEventListener('keydown', drawerKeyHandler); drawerKeyHandler = null; }
        if (drawerNode) { drawerNode.remove(); drawerNode = null; }
    }

    async function openDetailDrawer(id) {
        const watchlistId = normalizeId(id);
        if (!watchlistId) return;
        closeDrawer();

        const panel = el('div', { attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Watchlist details' } });
        panel.style.cssText = 'position:fixed;top:0;right:0;height:100vh;width:min(560px,95vw);background:#0f1524;' +
            'color:#fff;border-left:1px solid rgba(99,102,241,0.5);z-index:10004;overflow-y:auto;padding:1.25rem;box-shadow:-8px 0 30px rgba(0,0,0,0.5);';
        const closeBtn = el('button', { className: 'watchlist-btn', text: 'Close', attrs: { type: 'button', 'aria-label': 'Close details' } });
        closeBtn.style.cssText = 'position:sticky;top:0;float:right;';
        closeBtn.addEventListener('click', closeDrawer);
        panel.append(closeBtn);

        const body = el('div', { attrs: { 'aria-live': 'polite' } });
        body.append(el('p', { text: 'Loading watchlist details...' }));
        panel.append(body);

        drawerNode = panel;
        drawerKeyHandler = function (e) { if (e.key === 'Escape') { e.preventDefault(); closeDrawer(); } };
        document.addEventListener('keydown', drawerKeyHandler);
        document.body.appendChild(panel);
        closeBtn.focus();

        const req = beginRequest('detail');
        try {
            const raw = await api('/api/watchlists/' + encodeURIComponent(watchlistId), { signal: req.signal });
            if (!req.isCurrent() || !drawerNode) return;
            const wl = normalizeWatchlist(raw);
            if (!wl) { body.replaceChildren(el('p', { text: 'Invalid watchlist data' })); return; }
            renderDrawer(body, wl);
            loadDrawerEntries(wl.id, body, 1);
        } catch (err) {
            if (err.aborted || !req.isCurrent() || !drawerNode) return;
            body.replaceChildren(el('p', {
                text: err.status === 404 ? 'Watchlist not found'
                    : 'Failed to load details' + (err.referenceId ? ' (Reference: ' + err.referenceId + ')' : '')
            }));
        }
    }

    function infoRow(label, value) {
        const row = el('div', {}, [
            el('strong', { text: label + ': ' }),
            el('span', { text: value })
        ]);
        row.style.cssText = 'padding:0.2rem 0;border-bottom:1px solid rgba(255,255,255,0.06);font-size:0.9rem;';
        return row;
    }

    function renderDrawer(body, wl) {
        const header = el('div', {}, [
            el('h2', { text: wl.name }),
            el('p', { text: wl.description || 'No description' })
        ]);
        const badge = el('span', {
            className: 'alert-level-badge ' + wl.alertLevel,
            text: ALERT_LEVEL_LABELS[wl.alertLevel]
        });

        body.replaceChildren(
            header, badge,
            el('div', { className: 'drawer-info' }, [
                infoRow('Status', wl.deletedAt ? 'Deleted (' + fmtDateTime(wl.deletedAt) + ')' : (wl.isActive ? 'Active' : 'Inactive')),
                infoRow('Entries', String(wl.entriesCount)),
                infoRow('Alerts today', String(wl.alertsToday)),
                infoRow('Total alerts', String(wl.totalAlerts)),
                infoRow('Last alert', wl.lastAlertAt ? fmtDateTime(wl.lastAlertAt) : 'Never'),
                infoRow('Created', fmtDateTime(wl.createdAt)),
                infoRow('Updated', fmtDateTime(wl.updatedAt)),
                infoRow('Version', String(wl.version))
            ]),
            el('h3', { text: 'Entries' }),
            el('div', { attrs: { id: 'drawer-entries' } }, el('p', { text: 'Loading entries...' })),
            wl.deletedAt ? null : buildAddEntrySection(wl.id)
        );
    }

    async function loadDrawerEntries(watchlistId, body, page) {
        const container = body.querySelector('#drawer-entries');
        if (!container) return;
        const req = beginRequest('drawerEntries');
        try {
            const data = await api('/api/watchlists/' + encodeURIComponent(watchlistId) + '/entries', {
                signal: req.signal,
                params: { page: page, page_size: ENTRY_PAGE_SIZE }
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
                        loadDrawerEntries(watchlistId, body, 1);
                        loadWatchlists();
                    } catch (err) {
                        if (!err.aborted) showNotification('Failed to remove entry', 'error');
                    }
                });
                const row = el('div', {}, [
                    el('div', {}, [
                        el('strong', { text: name }),
                        el('span', { text: '  ' + safeText(entry.identity_type, 'unknown') + ' — priority ' + safeText(entry.priority, 'normal') })
                    ]),
                    el('div', { text: 'Added ' + fmtDateTime(entry.added_at) }),
                    entry.notes ? el('div', { text: 'Notes: ' + safeText(entry.notes) }) : null,
                    removeBtn
                ]);
                row.style.cssText = 'padding:0.5rem;border:1px solid rgba(255,255,255,0.08);border-radius:6px;margin:0.4rem 0;font-size:0.85rem;';
                return row;
            });
            container.replaceChildren.apply(container, rows);
            if (toNonNegativeInteger(data.total_pages, 1) > page) {
                const more = el('button', { className: 'watchlist-btn', text: 'Load more entries', attrs: { type: 'button' } });
                more.addEventListener('click', function () { loadDrawerEntries(watchlistId, body, page + 1); });
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
                const q = searchInput.value.trim();
                if (q.length < 2) { results.replaceChildren(); return; }
                const req = beginRequest('entrySearch');
                results.replaceChildren(el('p', { text: 'Searching...' }));
                try {
                    const data = await api('/api/admin/identities', {
                        signal: req.signal,
                        params: { page: 1, page_size: 10, q: q }
                    });
                    if (!req.isCurrent()) return;
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
                                const body = drawerNode && drawerNode.querySelector('[aria-live]');
                                if (body) loadDrawerEntries(watchlistId, body, 1);
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

        const section = el('div', {}, [
            el('h3', { text: 'Add identity' }),
            searchInput, prioritySelect, results
        ]);
        section.style.cssText = 'margin-top:1rem;border-top:1px solid rgba(255,255,255,0.12);padding-top:0.75rem;';
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
