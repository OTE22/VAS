/**
 * Security Intelligence Frontend (hardened rewrite)
 * =================================================
 * Social network, patterns, anomalies, threats, advanced ML features, maps.
 *
 * Contract:
 *  - Deterministic startup: pipelines + capabilities load first, selectors
 *    are built after, URL preselection uses the component API (no timers).
 *  - No backend value ever passes through innerHTML or inline handlers.
 *  - Identity selection is SERVER-SIDE searched and paginated.
 *  - The map is MapLibre GL JS rendering GeoJSON from /map-data over the
 *    offline Martin basemap (frontend/js/identity-map.js) — no iframe.
 *  - Every resource has an AbortController + generation: stale responses
 *    can never overwrite newer results.
 *  - Expensive/security features are opt-in; a missing control means OFF.
 *  - Correlation is described as association — never causation.
 */

(function () {
    'use strict';

    const DEBUG = false;
    const SEARCH_DEBOUNCE_MS = 300;
    // 50, not 25: the picker lists 159 identities here and the point of it
    // is to find a face. Half the 'Load more' clicks for one request, and
    // well inside API_MAX_PAGE_SIZE (100), which the endpoint enforces.
    const PAGE_SIZE = 50;
    const API_TIMEOUT_MS = 30000;
    const LONG_TIMEOUT_MS = 90000;
    const MAX_IMAGE_URL_LENGTH = 2048;
    const JOB_POLL_INTERVAL_MS = 3000;
    const JOB_POLL_MAX = 200;
    const MAP_STYLES = ['light', 'dark', 'satellite', 'terrain'];
    const VALID_SECURITY_TABS = new Set(['network', 'patterns', 'anomalies', 'threats', 'advanced', 'map']);
    const SEVERITY_CLASSES = new Set(['low', 'medium', 'high', 'critical']);
    // Unified risk-engine severities include 'moderate' (0-24 low, 25-49
    // moderate, 50-74 high, 75-100 critical); 'medium'/'minimal' remain for
    // older payloads. Styling maps moderate onto the medium classes.
    const THREAT_LEVEL_CLASSES = new Set(['critical', 'high', 'medium', 'moderate', 'low', 'minimal']);
    function severityClass(level) {
        return level === 'moderate' ? 'medium' : level;
    }
    const STRENGTH_CLASSES = new Set(['strong', 'moderate', 'weak', 'none']);

    const DEFAULT_AVATAR = 'data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%2780%27 height=%2780%27%3E%3Crect fill=%27%23333%27 width=%2780%27 height=%2780%27/%3E%3Ccircle cx=%2740%27 cy=%2730%27 r=%2712%27 fill=%27%23999%27/%3E%3Cpath d=%27M 20 55 Q 20 45 30 45 L 50 45 Q 60 45 60 55 L 60 65 L 20 65 Z%27 fill=%27%23999%27/%3E%3C/svg%3E';

    function log() { if (DEBUG) console.log.apply(console, arguments); }

    // ============================================
    // Normalization + safe helpers
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

    /** best_snapshot_path -> a safe same-origin URL, or '' for the placeholder.
     *  Mirrors the canonical fallback in admin-search.js:1030-1045. */
    function snapshotUrlFromPath(path) {
        if (typeof path !== 'string' || !path) return '';
        // Anchor on the 'storage/' segment: paths arrive relative
        // ('storage/faces/...'), bare, or ABSOLUTE ('/app/storage/...') —
        // the absolute form passed straight through became /app/storage/...
        // in the browser and 404'd every camera snapshot.
        const idx = path.indexOf('storage/');
        const url = (idx >= 0 ? '/' + path.slice(idx) : '/storage/' + path.replace(/^\/+/, '')).trim();
        if (url.startsWith('//') || url.includes('..')) return '';
        return url;
    }

    /** Compact copyable ID chip: shows the first 8 chars, copies the FULL
     *  uuid. Native <button> => Enter/Space fire click natively; ONE handler,
     *  stopPropagation so copying never selects/toggles the row. */
    function buildIdChip(id) {
        const full = normalizeId(id);
        const codeEl = el('code', { text: (full ? full.slice(0, 8) : '?') + '\u2026' });
        const btn = el('button', {
            className: 'identity-item-id',
            attrs: { type: 'button', title: full, 'aria-label': 'Copy identity ID ' + full }
        }, [codeEl]);
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            e.preventDefault();
            const restore = codeEl.textContent;
            const done = function (ok) {
                codeEl.textContent = ok ? 'copied \u2713' : 'copy failed';
                window.setTimeout(function () { codeEl.textContent = restore; }, 1200);
            };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(full).then(function () { done(true); },
                                                         function () { done(false); });
            } else { done(false); }
        });
        return btn;
    }

    function shortId(id) {
        const s = normalizeId(id);
        return s ? s.slice(0, 8) : '?';
    }

    function toFiniteNumber(value, fallback) {
        const n = Number(value);
        return Number.isFinite(n) ? n : (fallback !== undefined ? fallback : 0);
    }

    function toNonNegativeInteger(value, fallback) {
        const n = Math.floor(toFiniteNumber(value, fallback !== undefined ? fallback : 0));
        return Math.max(0, n);
    }

    function clamp(value, min, max) {
        return Math.max(min, Math.min(max, toFiniteNumber(value, min)));
    }

    // 0..1 fraction -> "63.5%"
    function formatPercent01(value, digits) {
        const normalized = clamp(value, 0, 1);
        return (normalized * 100).toFixed(digits === undefined ? 1 : digits) + '%';
    }

    function formatScore(value, digits) {
        const n = toFiniteNumber(value, null);
        return n === null ? 'N/A' : n.toFixed(digits === undefined ? 1 : digits);
    }

    // Strict timestamp parsing — invalid values become null, never "now".
    function parseServerTimestamp(value) {
        if (value instanceof Date) return Number.isFinite(value.getTime()) ? value : null;
        if (typeof value !== 'string' || !value.trim()) return null;
        let v = value;
        if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(v)) v += 'Z'; // naive legacy = UTC
        const d = new Date(v);
        return Number.isFinite(d.getTime()) ? d : null;
    }

    function fmtDateTime(value) {
        const d = parseServerTimestamp(value);
        return d ? d.toLocaleString() : 'Unknown time';
    }

    function fmtDate(value) {
        const d = parseServerTimestamp(value);
        return d ? d.toLocaleDateString() : 'Unknown time';
    }

    function normalizeCoordinate(value) {
        if (value === null || value === undefined || value === '') return null;
        const n = Number(value);
        return Number.isFinite(n) ? n : null;
    }

    // Only same-origin relative paths or the fixed local placeholder.
    function normalizeImageUrl(value) {
        if (typeof value !== 'string' || !value) return null;
        if (value === DEFAULT_AVATAR) return value;
        if (value.length > MAX_IMAGE_URL_LENGTH) return null;
        if (/^\/(?!\/)/.test(value)) return value;
        return null;
    }

    function normalizeIdentityType(value) {
        const t = String(value || '').toLowerCase();
        return t === 'known' ? 'known' : 'unknown';
    }

    // ONE shape for identities regardless of which endpoint produced them.
    function normalizeIdentity(raw) {
        if (!raw || typeof raw !== 'object') return null;
        const id = normalizeId(raw.id !== undefined && raw.id !== null ? raw.id : raw.identity_id);
        if (!id) return null;
        return {
            id: id,
            displayName: safeText(raw.display_name || raw.name || '', '') || ('Unknown #' + shortId(id)),
            type: normalizeIdentityType(raw.type !== undefined ? raw.type : raw.identity_type),
            lastSeenAt: raw.last_seen_at || null,
            snapshotUrl: normalizeImageUrl(raw.snapshot_url || null),
            pipelineIds: Array.isArray(raw.pipeline_ids) ? raw.pipeline_ids.map(String) : []
        };
    }

    function normalizePipeline(raw) {
        if (!raw || typeof raw !== 'object') return null;
        const id = normalizeId(raw.pipeline_id);
        if (!id) return null;
        return {
            id: id,
            displayName: safeText(raw.pipeline_name || raw.location_name || id),
            latitude: normalizeCoordinate(raw.latitude),
            longitude: normalizeCoordinate(raw.longitude),
            totalDetections: toNonNegativeInteger(raw.total_detections, 0),
            isActive: raw.is_active === true || raw.is_active === 1
        };
    }

    // DOM builders — the only rendering primitives in this file.
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

    function safeImg(url, alt, className) {
        const normalized = normalizeImageUrl(url) || DEFAULT_AVATAR;
        const img = el('img', { className: className || '', attrs: { alt: safeText(alt, ''), loading: 'lazy' } });
        img.src = normalized;
        img.addEventListener('error', function onErr() {
            img.removeEventListener('error', onErr);
            img.src = DEFAULT_AVATAR;
        });
        return img;
    }

    function renderStateInto(container, iconClass, message, referenceId) {
        if (!container) return;
        const children = [faIcon(iconClass), el('p', { text: message })];
        if (referenceId) children.push(el('p', { className: 'state-reference', text: 'Reference: ' + referenceId }));
        const cls = iconClass.indexOf('spinner') >= 0 ? 'loading-state' : 'empty-state';
        container.replaceChildren(el('div', { className: cls }, children));
    }

    function renderLoading(container, message) { renderStateInto(container, 'fas fa-spinner fa-spin', message); }
    function renderError(container, message, referenceId) { renderStateInto(container, 'fas fa-exclamation-triangle', message, referenceId); }

    // ============================================
    // Shared API client
    // ============================================

    function ApiError(message, opts) {
        const e = new Error(message);
        e.name = 'ApiError';
        e.status = (opts && opts.status) || 0;
        e.code = (opts && opts.code) || null;
        e.referenceId = (opts && opts.referenceId) || null;
        e.jobId = (opts && opts.jobId) || null;
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

        const headers = { 'Accept': options.expect === 'html' ? 'text/html' : 'application/json' };
        if (method !== 'GET' && method !== 'HEAD') {
            headers['X-Requested-With'] = 'XMLHttpRequest'; // CSRF header
            // FormData must NOT get a Content-Type: the browser writes the
            // multipart boundary itself, and a manual header breaks parsing.
            if (options.body !== undefined && !(options.body instanceof FormData)) {
                headers['Content-Type'] = 'application/json';
            }
        }

        let response;
        try {
            response = await fetch(url.toString(), {
                method: method,
                credentials: 'include',
                cache: 'no-store',
                headers: headers,
                body: options.body === undefined ? undefined
                    : (options.body instanceof FormData ? options.body : JSON.stringify(options.body)),
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
            let code = null, referenceId = null, jobId = null;
            let message = 'Request failed (' + response.status + ')';
            try {
                const body = await response.json();
                const detail = body && (body.detail !== undefined ? body.detail : body.error);
                if (detail && typeof detail === 'object') {
                    code = detail.error_code || detail.code || null;
                    referenceId = detail.reference_id || null;
                    jobId = detail.job_id || null;
                    if (typeof detail.message === 'string') message = detail.message;
                } else if (typeof detail === 'string') {
                    message = detail;
                    const refMatch = detail.match(/Reference:\s*([A-Za-z0-9-]+)/);
                    if (refMatch) referenceId = refMatch[1];
                }
            } catch (_) { /* keep generic message */ }
            throw ApiError(message, { status: response.status, code: code, referenceId: referenceId, jobId: jobId });
        }

        if (options.expect === 'html') return response.text();
        if (response.status === 204) return null;
        return response.json();
    }

    // ============================================
    // Request lifecycle: per-resource abort + generation
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
        mapController: null,
        mapDataKey: null,
        activeTab: 'network',
        networkInstance: null,
        pipelines: [],            // normalized
        pipelineNames: new Map(),
        capabilities: null,
        jobPollTimer: null
    };

    function pipelineDisplayName(pipelineId) {
        const id = normalizeId(pipelineId);
        if (!id) return 'Unknown location';
        return state.pipelineNames.get(id) || id;
    }

    // ============================================
    // Data loads
    // ============================================

    async function loadPipelines() {
        try {
            const data = await api('/api/pipelines');
            const list = Array.isArray(data) ? data : (data && Array.isArray(data.items) ? data.items : []);
            state.pipelines = list.map(normalizePipeline).filter(Boolean);
            populatePatternPipelineFilter();
            state.pipelineNames.clear();
            for (const p of state.pipelines) state.pipelineNames.set(p.id, p.displayName);
        } catch (err) {
            if (!err.aborted) log('[SEC] pipelines load failed:', err.status);
        }
    }

    async function loadCapabilities() {
        try {
            const data = await api('/api/security/capabilities');
            state.capabilities = (data && data.capabilities) || null;
        } catch (err) {
            state.capabilities = null;
            if (!err.aborted) log('[SEC] capabilities load failed:', err.status);
        }
    }

    // ============================================
    // Reusable accessible selector (identity server-search / pipeline)
    // ============================================

    const selectorRegistry = new Map(); // selectId -> component
    const openSelectorPanels = new Set();

    // ONE document-level outside-click listener for all selectors.
    document.addEventListener('click', function (e) {
        for (const component of openSelectorPanels) {
            if (!component.wrapper.contains(e.target)) component.close();
        }
    });

    function destroySelector(selectId) {
        const existing = selectorRegistry.get(selectId);
        if (existing) {
            existing.destroy();
            selectorRegistry.delete(selectId);
        }
    }

    function createSelector(originalSelect, config) {
        if (!originalSelect) return null;
        destroySelector(originalSelect.id); // idempotent re-init

        const component = {
            wrapper: null,
            cleanups: [],
            searchTimer: null,
            page: 1,
            totalPages: 1,
            items: [],            // normalized items currently rendered
            activeIndex: -1,
            selection: config.multi ? [] : null,      // ids
            selectionLabels: new Map()                // id -> label
        };

        originalSelect.style.display = 'none';
        originalSelect.setAttribute('aria-hidden', 'true');
        originalSelect.tabIndex = -1;

        const listboxId = originalSelect.id + '-listbox';
        const wrapper = el('div', { className: 'advanced-identity-selector' });
        wrapper.dataset.originalId = originalSelect.id;
        component.wrapper = wrapper;

        const trigger = el('button', {
            className: 'identity-selector-trigger',
            attrs: { type: 'button', 'aria-haspopup': 'listbox', 'aria-expanded': 'false', 'aria-controls': listboxId }
        }, [
            el('span', { className: 'trigger-text', text: config.label }),
            faIcon('fas fa-chevron-down trigger-icon')
        ]);

        const selectedTags = el('div', { className: 'selected-identity-tags' });
        selectedTags.style.display = config.multi ? 'flex' : 'none';

        const panel = el('div', { className: 'identity-selector-panel' });
        panel.style.display = 'none';

        const searchInput = el('input', {
            className: 'filter-search',
            attrs: {
                type: 'text', placeholder: 'Search by name or ID...', autocomplete: 'off',
                role: 'combobox', 'aria-autocomplete': 'list', 'aria-controls': listboxId
            }
        });

        const filterRows = [el('div', { className: 'filter-row' }, [searchInput])];

        // --- find by photo (identity pickers only) -------------------------
        let photoInput = null, photoBtn = null, photoClearBtn = null;
        if (config.mode === 'identity') {
            photoInput = el('input', { className: 'filter-photo-input', attrs: { type: 'file', accept: 'image/*' } });
            photoInput.style.display = 'none';
            photoBtn = el('button', {
                className: 'filter-photo-btn',
                attrs: { type: 'button', title: 'Find by photo', 'aria-label': 'Find identity by photo' }
            }, [faIcon('fas fa-camera')]);
            photoClearBtn = el('button', {
                className: 'filter-photo-clear',
                attrs: { type: 'button', title: 'Clear photo search', 'aria-label': 'Clear photo search' }
            }, [faIcon('fas fa-times'), el('span', { text: ' photo' })]);
            photoClearBtn.style.display = 'none';
            filterRows[0].append(photoBtn, photoClearBtn, photoInput);
        }
        let typeSelect = null, pipelineSelect = null, lastSeenSelect = null;
        if (config.mode === 'identity') {
            typeSelect = el('select', { className: 'filter-type', attrs: { 'aria-label': 'Filter by type' } }, [
                el('option', { text: 'All Types', attrs: { value: '' } }),
                el('option', { text: 'Known', attrs: { value: 'known' } }),
                el('option', { text: 'Unknown', attrs: { value: 'unknown' } })
            ]);
            filterRows[0].append(typeSelect);
            pipelineSelect = el('select', { className: 'filter-pipeline', attrs: { 'aria-label': 'Filter by pipeline' } },
                [el('option', { text: 'All Pipelines', attrs: { value: '' } })]);
            for (const p of state.pipelines) {
                pipelineSelect.append(el('option', { text: p.displayName, attrs: { value: p.id } }));
            }
            lastSeenSelect = el('select', { className: 'filter-last-seen', attrs: { 'aria-label': 'Filter by last seen' } }, [
                el('option', { text: 'All Time', attrs: { value: '' } }),
                el('option', { text: 'Last 24 Hours', attrs: { value: '1' } }),
                el('option', { text: 'Last 7 Days', attrs: { value: '7' } }),
                el('option', { text: 'Last 30 Days', attrs: { value: '30' } }),
                el('option', { text: 'Last 90 Days', attrs: { value: '90' } })
            ]);
            filterRows.push(el('div', { className: 'filter-row' }, [
                el('label', { text: 'Pipeline:' }), pipelineSelect,
                el('label', { text: 'Last Seen:' }), lastSeenSelect
            ]));
        }

        const statusLine = el('div', { className: 'identity-selector-status', attrs: { 'aria-live': 'polite', role: 'status' } });
        const resultsContainer = el('div', {
            className: 'identity-selector-results',
            attrs: { id: listboxId, role: 'listbox', 'aria-label': config.label + ' results' }
        });
        if (config.multi) resultsContainer.setAttribute('aria-multiselectable', 'true');
        const loadMoreBtn = el('button', { className: 'btn-secondary identity-load-more', text: 'Load more', attrs: { type: 'button' } });
        loadMoreBtn.style.display = 'none';

        panel.append(el('div', { className: 'identity-selector-filters' }, filterRows), statusLine, resultsContainer, loadMoreBtn);
        wrapper.append(trigger, selectedTags, panel);
        originalSelect.parentNode.insertBefore(wrapper, originalSelect.nextSibling);

        function isSelected(id) {
            return config.multi ? component.selection.indexOf(id) >= 0 : component.selection === id;
        }

        function labelFor(id) {
            return component.selectionLabels.get(id) || ('Unknown #' + shortId(id));
        }

        function syncHiddenSelect() {
            // Silent sync — NO synthetic change events (they caused double loads)
            originalSelect.replaceChildren();
            const ids = config.multi ? component.selection : (component.selection ? [component.selection] : []);
            for (const id of ids) {
                originalSelect.append(el('option', { text: labelFor(id), attrs: { value: id, selected: 'selected' } }));
            }
        }

        function updateTrigger() {
            const textSpan = trigger.querySelector('.trigger-text');
            if (config.multi) {
                const count = component.selection.length;
                textSpan.textContent = count > 0 ? count + ' selected' : config.label;
            } else {
                textSpan.textContent = component.selection ? labelFor(component.selection) : config.label;
            }
        }

        function updateTags() {
            if (!config.multi) return;
            selectedTags.replaceChildren();
            for (const id of component.selection) {
                const removeBtn = el('button', {
                    className: 'tag-remove',
                    attrs: { type: 'button', 'aria-label': 'Remove ' + labelFor(id) }
                }, faIcon('fas fa-times'));
                removeBtn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    component.selection = component.selection.filter(function (x) { return x !== id; });
                    updateTags(); updateTrigger(); syncHiddenSelect(); refreshOptionStates();
                });
                selectedTags.append(el('div', { className: 'selected-identity-tag' }, [
                    el('span', { text: labelFor(id) }), removeBtn
                ]));
            }
        }

        function refreshOptionStates() {
            resultsContainer.querySelectorAll('[role="option"]').forEach(function (opt) {
                const id = opt.dataset.itemId;
                opt.classList.toggle('selected', isSelected(id));
            });
        }

        function choose(item) {
            const id = item.id;
            component.selectionLabels.set(id, item.displayName);
            if (config.multi) {
                const idx = component.selection.indexOf(id);
                if (idx >= 0) component.selection.splice(idx, 1);
                else component.selection.push(id);
                updateTags();
            } else {
                component.selection = id;
                component.close(true);
            }
            updateTrigger();
            syncHiddenSelect();
            refreshOptionStates();
            if (typeof config.onSelect === 'function' && !config.multi) config.onSelect(id);
        }

        function setActive(index) {
            const options = resultsContainer.querySelectorAll('[role="option"]');
            if (!options.length) { component.activeIndex = -1; searchInput.removeAttribute('aria-activedescendant'); return; }
            component.activeIndex = Math.max(0, Math.min(index, options.length - 1));
            options.forEach(function (opt, i) {
                opt.classList.toggle('active', i === component.activeIndex);
                opt.setAttribute('aria-selected', i === component.activeIndex ? 'true' : 'false');
            });
            const active = options[component.activeIndex];
            searchInput.setAttribute('aria-activedescendant', active.id);
            active.scrollIntoView({ block: 'nearest' });
        }

        function renderItems(items, append) {
            if (!append) { resultsContainer.replaceChildren(); component.items = []; }
            const base = component.items.length;
            items.forEach(function (item, i) {
                component.items.push(item);
                const optionId = originalSelect.id + '-option-' + (base + i);
                const metaChildren = [];
                if (config.mode === 'identity') {
                    metaChildren.push(el('span', { className: 'identity-item-type ' + item.type, text: item.type }));
                    metaChildren.push(buildIdChip(item.id));
                    metaChildren.push(el('span', {
                        className: 'identity-item-date',
                        text: 'Last seen: ' + (item.lastSeenAt ? fmtDate(item.lastSeenAt) : 'Never')
                    }));
                    if (typeof item.similarity === 'number') {
                        metaChildren.push(el('span', {
                            className: 'identity-item-similarity',
                            text: Math.round(item.similarity * 100) + '% match'
                        }));
                    }
                } else {
                    metaChildren.push(el('span', { className: 'identity-item-type', text: item.id }));
                    metaChildren.push(el('span', { className: 'identity-item-date', text: 'Detections: ' + item.totalDetections.toLocaleString() }));
                }
                const node = el('div', {
                    className: 'identity-selector-item' + (isSelected(item.id) ? ' selected' : ''),
                    attrs: { role: 'option', id: optionId, 'aria-selected': 'false', tabindex: '-1' }
                }, [
                    el('div', { className: 'identity-item-thumbnail' },
                        config.mode === 'identity'
                            ? (item.snapshotUrl ? safeImg(item.snapshotUrl, item.displayName) : faIcon('fas fa-user'))
                            : faIcon('fas fa-video')),
                    el('div', { className: 'identity-item-info' }, [
                        el('div', { className: 'identity-item-name', text: item.displayName }),
                        el('div', { className: 'identity-item-meta' }, metaChildren)
                    ]),
                    el('div', { className: 'identity-item-check' }, faIcon('fas fa-check'))
                ]);
                node.dataset.itemId = item.id;
                node.addEventListener('click', function () { choose(item); });
                resultsContainer.append(node);
            });
            if (!component.items.length) {
                resultsContainer.replaceChildren(el('div', { className: 'no-results', text: 'No results found' }));
            }
        }

        async function runIdentitySearch(reset) {
            if (reset) component.page = 1;
            const req = beginRequest('selector:' + originalSelect.id);
            statusLine.textContent = 'Loading...';
            loadMoreBtn.disabled = true;
            try {
                const data = await api('/api/admin/identities', {
                    signal: req.signal,
                    params: {
                        page: component.page,
                        page_size: PAGE_SIZE,
                        q: searchInput.value.trim() || undefined,
                        type: typeSelect && typeSelect.value ? typeSelect.value : undefined,
                        pipeline_id: pipelineSelect && pipelineSelect.value ? pipelineSelect.value : undefined,
                        last_seen_within_days: lastSeenSelect && lastSeenSelect.value ? lastSeenSelect.value : undefined
                    }
                });
                if (!req.isCurrent()) return;
                const items = ((data && data.items) || []).map(normalizeIdentity).filter(Boolean);
                component.totalPages = toNonNegativeInteger(data && data.total_pages, 1) || 1;
                renderItems(items, !reset && component.page > 1);
                const total = toNonNegativeInteger(data && data.total, component.items.length);
                statusLine.textContent = total === 0 ? 'No identities found' :
                    'Showing ' + component.items.length + ' of ' + total + ' identities';
                loadMoreBtn.style.display = component.page < component.totalPages ? 'block' : 'none';
                loadMoreBtn.disabled = false;
                if (reset) setActive(-1);
            } catch (err) {
                if (err.aborted || !req.isCurrent()) return;
                statusLine.textContent = 'Failed to load identities';
                renderItems([], false);
                loadMoreBtn.disabled = false;
            }
        }

        // --- find by photo -------------------------------------------------
        // Same request key as the text search: beginRequest aborts the
        // previous controller and creates a FRESH one per request (an aborted
        // controller is never reused); isCurrent() independently drops any
        // stale response that slips past the abort.

        let uploadLimitsPromise = null;
        function loadUploadLimits() {
            if (!uploadLimitsPromise) {
                uploadLimitsPromise = api('/api/dashboard/config')
                    .then(function (payload) {
                        const cfg = (payload && payload.config) || {};
                        return {
                            maxBytes: Number.isFinite(cfg.max_file_size_bytes) ? cfg.max_file_size_bytes : null,
                            extensions: Array.isArray(cfg.allowed_extensions) && cfg.allowed_extensions.length
                                ? cfg.allowed_extensions.map(function (x) { return String(x).replace(/^\./, '').toLowerCase(); })
                                : null
                        };
                    })
                    .catch(function () { return { maxBytes: null, extensions: null }; });
            }
            return uploadLimitsPromise;
        }

        function exitPhotoMode(rerun) {
            component.photoMode = false;
            if (photoClearBtn) photoClearBtn.style.display = 'none';
            if (photoInput) photoInput.value = '';
            if (rerun) refresh(true);
        }

        async function runPhotoSearch(file) {
            if (!file) return;
            // Client pre-checks mirror the server's published limits; the
            // server stays authoritative (no invented frontend limit).
            const limits = await loadUploadLimits();
            const ext = (file.name.split('.').pop() || '').toLowerCase();
            if (limits.extensions && ext && limits.extensions.indexOf(ext) === -1) {
                statusLine.textContent = 'Unsupported image type. Allowed: ' + limits.extensions.join(', ');
                photoInput.value = '';
                return;
            }
            if (!limits.extensions && file.type && file.type.indexOf('image/') !== 0) {
                statusLine.textContent = 'Please choose an image file.';
                photoInput.value = '';
                return;
            }
            if (limits.maxBytes && file.size > limits.maxBytes) {
                statusLine.textContent = 'Photo is too large (limit ' +
                    Math.round(limits.maxBytes / 1048576) + 'MB).';
                photoInput.value = '';
                return;
            }

            const req = beginRequest('selector:' + originalSelect.id);
            component.photoMode = true;
            statusLine.textContent = 'Searching by photo\u2026';
            loadMoreBtn.style.display = 'none';
            loadMoreBtn.disabled = true;
            try {
                const formData = new FormData();
                formData.append('image', file);
                formData.append('scope', 'both');
                formData.append('top_k', '20');
                const matches = await api('/api/search/by-image', {
                    method: 'POST', body: formData, signal: req.signal, timeout: 60000
                });
                if (!req.isCurrent() || !component.photoMode) return;
                // Normalise through the SAME normalizeIdentity the text flow
                // uses (one shape, one renderer); backend type preserved.
                const items = (Array.isArray(matches) ? matches : []).map(function (m) {
                    const norm = normalizeIdentity({
                        identity_id: m.identity_id,
                        display_name: m.display_name,
                        type: m.type,
                        last_seen_at: m.last_seen_at,
                        snapshot_url: snapshotUrlFromPath(m.best_snapshot_path)
                    });
                    if (norm && typeof m.similarity === 'number') norm.similarity = m.similarity;
                    return norm;
                }).filter(Boolean);
                renderItems(items, false);
                statusLine.textContent = items.length === 0
                    ? 'No matching identities for that photo'
                    : items.length + ' match(es) by photo \u2014 best first';
                photoClearBtn.style.display = '';
                setActive(-1);
            } catch (err) {
                if (err.aborted || !req.isCurrent()) return;   // intentional abort: silence
                if (err.status === 400) {
                    statusLine.textContent = 'No face detected in that photo.';
                } else if (err.status === 401 || err.status === 403) {
                    statusLine.textContent = 'Not permitted \u2014 sign in again.';
                } else {
                    statusLine.textContent = 'Photo search failed. Please try again.';
                }
                photoClearBtn.style.display = '';
            } finally {
                // Always reset, so choosing the SAME image again re-fires change.
                photoInput.value = '';
            }
        }

        function runPipelineFilter() {
            const term = searchInput.value.trim().toLowerCase();
            const filtered = state.pipelines.filter(function (p) {
                return !term || p.displayName.toLowerCase().indexOf(term) >= 0 || p.id.toLowerCase().indexOf(term) >= 0;
            }).sort(function (a, b) { return a.displayName.localeCompare(b.displayName); });
            renderItems(filtered, false);
            statusLine.textContent = filtered.length + ' pipeline(s)';
        }

        function refresh(reset) {
            if (config.mode === 'identity') runIdentitySearch(reset);
            else runPipelineFilter();
        }

        component.open = function () {
            panel.style.display = 'block';
            fitPanelToViewport();
            trigger.classList.add('active');
            trigger.setAttribute('aria-expanded', 'true');
            openSelectorPanels.add(component);
            refresh(true);
            // preventScroll matters: fitPanelToViewport() has just placed the
            // panel against the CURRENT scroll position, but the panel is
            // absolutely positioned against this wrapper. A plain focus() makes
            // the browser scroll .security-content to reveal the search box,
            // which moves the wrapper — and the panel with it — leaving the
            // freshly computed fit stale and the panel hanging below the fold.
            window.setTimeout(function () { searchInput.focus({ preventScroll: true }); }, 50);
        };


        /** Size the panel to the space actually available, and flip it above
         *  the trigger when there is more room there.
         *
         *  A fixed max-height cannot fit: the trigger sits partway down the
         *  page, so the panel opened at y=365 and ran to y=865 in a 768px
         *  viewport — the last faces, the Load-more button and the pager were
         *  all below the fold and unreachable. Measured, not assumed: the probe
         *  asserts the panel's bottom edge is on screen.
         */
        function fitPanelToViewport() {
            const GAP = 12;                     // breathing room at the edge
            const MIN = 260;                    // below this the list is useless
            const rect = trigger.getBoundingClientRect();
            const below = window.innerHeight - rect.bottom - GAP;
            const above = rect.top - GAP;
            // Use whichever side has more room; only prefer flipping up when
            // below is genuinely too small to be useful.
            const openUp = below < MIN && above > below;

            panel.style.top = openUp ? 'auto' : '100%';
            panel.style.bottom = openUp ? '100%' : 'auto';

            // Measure the panel's OWN top rather than assuming it sits just
            // under the trigger: `top: 100%` is relative to the wrapper, which
            // also holds the selected-identity tags, so the real offset was
            // 24px where the trigger implied 8 — and the panel overhung the
            // viewport by exactly that difference.
            const panelTop = panel.getBoundingClientRect().top;
            // Clamp to what exists. Forcing a MIN taller than the available
            // space is how the panel overhung the fold at 1024x768: the floor
            // is a preference, not a licence to leave the viewport.
            const available = Math.floor(openUp ? above : window.innerHeight - panelTop - GAP);
            const space = Math.max(120, Math.min(available, Math.max(MIN, available)));

            panel.style.maxHeight = space + 'px';

            // Horizontal clamp. The panel is positioned against its wrapper,
            // so on a narrow viewport a wrapper sitting right of centre pushes
            // it off-screen even at 92vw. Nudge it back by however much it
            // overhangs, and never let it start left of the edge.
            panel.style.left = '0';
            panel.style.right = 'auto';
            const box = panel.getBoundingClientRect();
            const overhangRight = box.right - (window.innerWidth - GAP);
            if (overhangRight > 0) {
                panel.style.left = (-overhangRight) + 'px';
            }
            const shifted = panel.getBoundingClientRect();
            if (shifted.left < GAP) {
                panel.style.left = (parseFloat(panel.style.left || '0') + (GAP - shifted.left)) + 'px';
            }

            if (openUp) {
                panel.style.top = 'auto';
                panel.style.bottom = '100%';
                panel.style.marginTop = '0';
                panel.style.marginBottom = '0.5rem';
            } else {
                panel.style.top = '100%';
                panel.style.bottom = 'auto';
                panel.style.marginTop = '0.5rem';
                panel.style.marginBottom = '0';
            }
        }

        component.close = function (restoreFocus) {
            // Nothing may keep running behind a closed picker; reopening must
            // start clean rather than resuming a stale photo search.
            const previous = requestControllers.get('selector:' + originalSelect.id);
            if (previous) {
                try { previous.abort(); } catch (_) { /* noop */ }
            }
            if (component.photoMode) exitPhotoMode(false);
            panel.style.display = 'none';
            trigger.classList.remove('active');
            trigger.setAttribute('aria-expanded', 'false');
            openSelectorPanels.delete(component);
            if (restoreFocus) trigger.focus();
        };

        component.getValue = function () {
            return config.multi ? component.selection.slice() : component.selection;
        };

        // Programmatic preselection through the component API — updates
        // private state, hidden select AND the visible label.
        component.setValue = async function (rawId, opts) {
            const id = normalizeId(rawId);
            if (!id) return false;
            let label = null;
            if (config.mode === 'identity') {
                try {
                    const detail = await api('/api/admin/identity/' + encodeURIComponent(id));
                    const norm = normalizeIdentity(detail);
                    if (norm) label = norm.displayName;
                } catch (err) {
                    if (err.status === 404) return false; // unknown/unauthorized id from URL
                }
            } else {
                label = pipelineDisplayName(id);
            }
            component.selectionLabels.set(id, label || ('Unknown #' + shortId(id)));
            if (config.multi) {
                if (component.selection.indexOf(id) < 0) component.selection.push(id);
                updateTags();
            } else {
                component.selection = id;
            }
            updateTrigger();
            syncHiddenSelect();
            if (opts && opts.emit && typeof config.onSelect === 'function' && !config.multi) config.onSelect(id);
            return true;
        };

        component.clear = function () {
            component.selection = config.multi ? [] : null;
            updateTags(); updateTrigger(); syncHiddenSelect(); refreshOptionStates();
        };

        component.destroy = function () {
            for (const fn of component.cleanups.splice(0)) {
                try { fn(); } catch (_) { /* noop */ }
            }
            if (component.searchTimer) window.clearTimeout(component.searchTimer);
            openSelectorPanels.delete(component);
            if (wrapper.parentNode) wrapper.parentNode.removeChild(wrapper);
        };

        // Listeners
        function on(target, evt, fn) {
            target.addEventListener(evt, fn);
            component.cleanups.push(function () { target.removeEventListener(evt, fn); });
        }

        on(trigger, 'click', function (e) {
            e.stopPropagation();
            if (panel.style.display === 'none') component.open(); else component.close(false);
        });
        on(searchInput, 'input', function () {
            if (component.searchTimer) window.clearTimeout(component.searchTimer);
            // Typing leaves photo mode; refresh() begins a new request, which
            // aborts any in-flight photo search.
            if (component.photoMode) exitPhotoMode(false);
            component.searchTimer = window.setTimeout(function () { refresh(true); }, SEARCH_DEBOUNCE_MS);
        });
        if (photoBtn) {
            on(photoBtn, 'click', function (e) { e.stopPropagation(); photoInput.click(); });
            on(photoClearBtn, 'click', function (e) { e.stopPropagation(); exitPhotoMode(true); });
            on(photoInput, 'change', function () { runPhotoSearch(photoInput.files && photoInput.files[0]); });
        }
        if (typeSelect) on(typeSelect, 'change', function () { refresh(true); });
        if (pipelineSelect) on(pipelineSelect, 'change', function () { refresh(true); });
        if (lastSeenSelect) on(lastSeenSelect, 'change', function () { refresh(true); });
        on(loadMoreBtn, 'click', function () { component.page += 1; refresh(false); });
        on(searchInput, 'keydown', function (e) {
            const options = resultsContainer.querySelectorAll('[role="option"]');
            switch (e.key) {
                case 'ArrowDown': e.preventDefault(); setActive(component.activeIndex + 1); break;
                case 'ArrowUp': e.preventDefault(); setActive(component.activeIndex - 1); break;
                case 'Home': if (options.length) { e.preventDefault(); setActive(0); } break;
                case 'End': if (options.length) { e.preventDefault(); setActive(options.length - 1); } break;
                case 'Enter':
                    e.preventDefault();
                    if (component.activeIndex >= 0 && component.items[component.activeIndex]) {
                        choose(component.items[component.activeIndex]);
                    }
                    break;
                case 'Escape': e.preventDefault(); component.close(true); break;
            }
        });

        selectorRegistry.set(originalSelect.id, component);
        return component;
    }

    function getSelectorValue(selectId) {
        const component = selectorRegistry.get(selectId);
        if (component) return component.getValue();
        const raw = document.getElementById(selectId);
        return raw ? normalizeId(raw.value) : null;
    }

    // ============================================
    // Accessible modal (replaces alert/confirm dialogs)
    // ============================================

    let activeModal = null;

    function showModal(title, bodyNodes) {
        closeModal();
        const closeBtn = el('button', { className: 'btn-primary', text: 'Close', attrs: { type: 'button' } });
        const dialog = el('div', {
            className: 'sec-modal-dialog',
            attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-label': title }
        }, [
            el('h3', { text: title }),
            el('div', { className: 'sec-modal-body' }, bodyNodes),
            closeBtn
        ]);
        dialog.style.cssText = 'background:#101725;color:#fff;border:1px solid rgba(0,255,150,0.35);border-radius:10px;' +
            'padding:1.5rem;max-width:560px;width:92%;max-height:80vh;overflow:auto;';
        const backdrop = el('div', { className: 'sec-modal-backdrop' }, dialog);
        backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.7);display:flex;' +
            'align-items:center;justify-content:center;z-index:10001;';

        function close() { closeModal(); }
        closeBtn.addEventListener('click', close);
        backdrop.addEventListener('click', function (e) { if (e.target === backdrop) close(); });
        const keyHandler = function (e) {
            if (e.key === 'Escape') { e.preventDefault(); close(); }
            if (e.key === 'Tab') { e.preventDefault(); closeBtn.focus(); } // single-control focus trap
        };
        document.addEventListener('keydown', keyHandler);

        activeModal = { node: backdrop, keyHandler: keyHandler, previousFocus: document.activeElement };
        document.body.appendChild(backdrop);
        closeBtn.focus();
    }

    function closeModal() {
        if (!activeModal) return;
        document.removeEventListener('keydown', activeModal.keyHandler);
        activeModal.node.remove();
        if (activeModal.previousFocus && activeModal.previousFocus.focus) activeModal.previousFocus.focus();
        activeModal = null;
    }

    function showNotification(message, type) {
        type = ['info', 'success', 'error', 'warning'].indexOf(type) >= 0 ? type : 'info';
        const colors = { info: '#3498db', success: '#2ecc71', error: '#e74c3c', warning: '#f39c12' };
        const notification = el('div', { className: 'notification ' + type });
        notification.style.cssText = 'position:fixed;top:20px;right:20px;padding:15px 20px;background:' + colors[type] +
            ';color:#fff;border-radius:5px;z-index:10000;box-shadow:0 4px 6px rgba(0,0,0,0.3);font-weight:600;';
        notification.textContent = message;
        notification.setAttribute('role', type === 'error' ? 'alert' : 'status');
        document.body.appendChild(notification);
        window.setTimeout(function () {
            notification.style.opacity = '0';
            notification.style.transition = 'opacity 0.3s';
            window.setTimeout(function () { notification.remove(); }, 300);
        }, 4000);
    }

    // ============================================
    // Tabs
    // ============================================

    function switchTab(rawTab) {
        const tabName = VALID_SECURITY_TABS.has(rawTab) ? rawTab : 'network';
        state.activeTab = tabName;
        document.querySelectorAll('.sec-tab').forEach(function (tab) {
            const active = tab.dataset.tab === tabName;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        document.querySelectorAll('.sec-tab-content').forEach(function (content) {
            content.classList.toggle('active', content.id === 'tab-' + tabName);
        });
    }

    // ============================================
    // Social network analysis (bounded, validated, vis lifecycle)
    // ============================================

    function normalizeNetworkNode(raw) {
        if (!raw || typeof raw !== 'object') return null;
        const id = normalizeId(raw.identity_id);
        if (!id) return null;
        return {
            id: id,
            displayName: safeText(raw.display_name || '', '') || ('ID: ' + shortId(id)),
            type: normalizeIdentityType(raw.identity_type),
            connections: toNonNegativeInteger(raw.connections_count, 0),
            riskScore: clamp(raw.risk_score, 0, 100),
            snapshotUrl: normalizeImageUrl(raw.snapshot_url)
        };
    }

    function normalizeNetworkEdge(raw, nodeIds) {
        if (!raw || typeof raw !== 'object') return null;
        const source = normalizeId(raw.source_id);
        const target = normalizeId(raw.target_id);
        if (!source || !target || !nodeIds.has(source) || !nodeIds.has(target)) return null;
        return {
            source: source,
            target: target,
            strength: clamp(raw.strength, 0, 1),
            coAppearances: toNonNegativeInteger(raw.co_appearances, 0),
            relationshipType: STRENGTH_CLASSES.has(String(raw.relationship_type || '').toLowerCase())
                ? String(raw.relationship_type).toLowerCase() : 'weak'
        };
    }

    async function loadNetwork() {
        const graphContainer = document.getElementById('network-graph');
        if (!graphContainer) return;
        const identityIds = getSelectorValue('network-identity-ids') || [];
        const minConnections = toNonNegativeInteger(document.getElementById('network-min-connections') &&
            document.getElementById('network-min-connections').value, 1);
        const daysBack = toNonNegativeInteger(document.getElementById('network-days-back') &&
            document.getElementById('network-days-back').value, 90) || 90;

        const req = beginRequest('network');
        const btn = document.getElementById('network-analyze-btn');
        if (btn) btn.disabled = true;
        renderLoading(graphContainer, 'Building network graph...');
        try {
            const data = await api('/api/security/network', {
                signal: req.signal, timeout: LONG_TIMEOUT_MS,
                params: {
                    min_connections: minConnections,
                    days_back: daysBack,
                    identity_ids: Array.isArray(identityIds) && identityIds.length ? identityIds.join(',') : undefined
                }
            });
            if (!req.isCurrent()) return;
            renderNetwork(data || {});
            updateNetworkStats(data || {});
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderError(graphContainer, 'Failed to load network', err.referenceId);
        } finally {
            if (req.isCurrent() && btn) btn.disabled = false;
        }
    }

    function destroyNetworkInstance() {
        if (state.networkInstance) {
            try { state.networkInstance.destroy(); } catch (_) { /* noop */ }
            state.networkInstance = null;
        }
    }

    function renderNetwork(data) {
        const container = document.getElementById('network-graph');
        if (!container) return;
        destroyNetworkInstance();

        const nodesRaw = Array.isArray(data.nodes) ? data.nodes : [];
        const normalizedNodes = nodesRaw.map(normalizeNetworkNode).filter(Boolean);
        if (!normalizedNodes.length) {
            renderStateInto(container, 'fas fa-info-circle', 'No network data available');
            return;
        }
        if (typeof window.vis === 'undefined' || !window.vis.Network) {
            renderError(container, 'Network visualization library unavailable');
            return;
        }
        const nodeIds = new Set(normalizedNodes.map(function (n) { return n.id; }));
        const normalizedEdges = (Array.isArray(data.edges) ? data.edges : [])
            .map(function (e) { return normalizeNetworkEdge(e, nodeIds); }).filter(Boolean);

        const visNodes = normalizedNodes.map(function (node) {
            const nodeSize = clamp(40 + node.connections * 3, 40, 80);
            const borderColor = node.riskScore > 50 ? '#ff0000' :
                node.riskScore > 25 ? '#ffaa00' :
                    node.type === 'known' ? '#00ff96' : '#ffaa00';
            const base = {
                id: node.id,
                label: node.displayName,
                title: node.displayName + '\nType: ' + node.type + '\nConnections: ' + node.connections +
                    '\nRisk: ' + node.riskScore.toFixed(1) + '%',
                size: nodeSize,
                borderWidth: node.riskScore > 50 ? 4 : node.riskScore > 25 ? 3 : 2,
                font: { color: '#fff', size: 12, strokeWidth: 2, strokeColor: '#000' }
            };
            if (node.snapshotUrl) {
                base.shape = 'circularImage';
                base.image = node.snapshotUrl;
                base.brokenImage = DEFAULT_AVATAR;
                base.color = { border: borderColor };
            } else {
                base.shape = 'dot';
                base.color = { background: node.type === 'known' ? '#00ff96' : '#ffaa00', border: borderColor };
            }
            return base;
        });

        const visEdges = normalizedEdges.map(function (edge) {
            return {
                from: edge.source,
                to: edge.target,
                value: edge.strength * 10,
                title: 'Strength: ' + formatPercent01(edge.strength) + '\nCo-appearances: ' + edge.coAppearances,
                color: {
                    color: edge.relationshipType === 'strong' ? '#00ff96' :
                        edge.relationshipType === 'moderate' ? '#ffaa00' : '#888',
                    highlight: '#00ff96'
                },
                width: clamp(edge.strength * 5, 1, 5)
            };
        });

        container.replaceChildren();

        // Truncation honesty banner
        if (data.truncated === true) {
            const banner = el('div', {
                className: 'network-truncation-banner',
                text: 'Showing ' + toNonNegativeInteger(data.returned_nodes, visNodes.length) + ' of ' +
                    toNonNegativeInteger(data.total_nodes, visNodes.length) + ' nodes (scope: ' +
                    safeText(data.scope, 'bounded') + ') — refine filters to focus the graph'
            });
            banner.style.cssText = 'padding:0.5rem 0.75rem;margin-bottom:0.5rem;border:1px solid rgba(255,170,0,0.5);' +
                'border-radius:6px;color:#ffaa00;font-size:0.85rem;';
            container.append(banner);
        }
        const graphDiv = el('div');
        graphDiv.style.cssText = 'width:100%;height:100%;min-height:480px;';
        container.append(graphDiv);

        try {
            const network = new window.vis.Network(graphDiv, { nodes: visNodes, edges: visEdges }, {
                nodes: {
                    shapeProperties: { useBorderWithImage: true },
                    scaling: { min: 30, max: 100 }
                },
                edges: { smooth: { type: 'continuous', roundness: 0.5 } },
                physics: { enabled: true, stabilization: { iterations: 200 } },
                interaction: { hover: true, tooltipDelay: 200, zoomView: true, dragView: true }
            });
            // Stop physics after layout settles — large graphs stay responsive.
            network.once('stabilizationIterationsDone', function () {
                network.setOptions({ physics: { enabled: false } });
            });
            state.networkInstance = network;
        } catch (err) {
            log('[SEC] vis construction failed');
            renderError(container, 'Failed to render network graph');
        }
    }

    function updateNetworkStats(data) {
        const nodes = Array.isArray(data.nodes) ? data.nodes : [];
        const setStat = function (id, value) {
            const node = document.getElementById(id);
            if (node) node.textContent = String(value);
        };
        // Truncation honesty: a clipped graph must not read as the whole graph.
        const totalNodes = toNonNegativeInteger(data.total_nodes, nodes.length);
        setStat('stat-nodes', data.truncated === true && totalNodes > nodes.length
            ? nodes.length + ' / ' + totalNodes : nodes.length);
        setStat('stat-edges', Array.isArray(data.edges) ? data.edges.length : 0);
        setStat('stat-clusters', Array.isArray(data.clusters) ? data.clusters.length : 0);
        setStat('stat-central', Array.isArray(data.central_nodes) ? data.central_nodes.length : 0);

        const nodeById = new Map();
        for (const n of nodes) {
            const norm = normalizeNetworkNode(n);
            if (norm) nodeById.set(norm.id, norm);
        }

        const centralList = document.getElementById('central-nodes-list');
        if (centralList) {
            const central = (Array.isArray(data.central_nodes) ? data.central_nodes : []).slice(0, 10);
            if (central.length) {
                centralList.replaceChildren.apply(centralList, central.map(function (rawId) {
                    const id = normalizeId(rawId);
                    const node = id ? nodeById.get(id) : null;
                    const label = (node ? node.displayName : shortId(id)) +
                        ' (' + (node ? node.connections : 0) + ' connections)';
                    return el('div', { className: 'node-item', text: label });
                }));
            } else {
                centralList.replaceChildren(el('div', { className: 'empty-state', text: 'No central nodes' }));
            }
        }

        const clustersList = document.getElementById('clusters-list');
        if (clustersList) {
            const clusters = Array.isArray(data.clusters) ? data.clusters : [];
            if (clusters.length) {
                clustersList.replaceChildren.apply(clustersList, clusters.map(function (cluster, idx) {
                    const size = Array.isArray(cluster) ? cluster.length : 0;
                    return el('div', { className: 'cluster-item', text: 'Cluster ' + (idx + 1) + ': ' + size + ' identities' });
                }));
            } else {
                clustersList.replaceChildren(el('div', { className: 'empty-state', text: 'No clusters found' }));
            }
        }
    }

    // ============================================
    // Suspicious patterns
    // ============================================

    const PATTERN_ICONS = {
        group_activity: 'fas fa-users',
        unusual_timing: 'fas fa-clock',
        rapid_movement: 'fas fa-route'
    };

    async function loadPatterns() {
        const container = document.getElementById('patterns-container');
        if (!container) return;
        const daysBack = toNonNegativeInteger(document.getElementById('patterns-days-back') &&
            document.getElementById('patterns-days-back').value, 30) || 30;
        const minGroup = toNonNegativeInteger(document.getElementById('patterns-min-group') &&
            document.getElementById('patterns-min-group').value, 3) || 3;

        const req = beginRequest('patterns');
        renderLoading(container, 'Detecting patterns...');
        try {
            const data = await api('/api/security/patterns', {
                signal: req.signal, timeout: LONG_TIMEOUT_MS,
                params: { days_back: daysBack, min_group_size: minGroup,
                          pipeline_id: (document.getElementById('patterns-pipeline-id') || {}).value || undefined }
            });
            if (!req.isCurrent()) return;
            // patterns-v2 envelope: {items, truncated, total, analysis_window}
            const items = data && Array.isArray(data.items) ? data.items
                : (Array.isArray(data) ? data : []);
            renderPatterns(items, !!(data && data.truncated === true));
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderError(container, 'Failed to load patterns', err.referenceId);
        }
    }

    function buildTruncationNote() {
        const note = el('div', {
            className: 'patterns-truncation-note',
            text: 'Partial scan — the analysis window hit its cap; older activity was not examined.'
        });
        note.style.cssText = 'padding:0.5rem 0.75rem;margin-bottom:0.5rem;border:1px solid rgba(255,170,0,0.5);' +
            'border-radius:6px;color:#ffaa00;font-size:0.85rem;';
        return note;
    }

    function renderPatterns(patterns, truncated) {
        const container = document.getElementById('patterns-container');
        if (!container) return;
        if (!patterns.length) {
            // A clean result from a PARTIAL scan must not read as a clean
            // complete result — the note survives the empty state.
            if (truncated) {
                container.replaceChildren(buildTruncationNote(), el('div', {
                    className: 'empty-state',
                    text: 'No suspicious patterns detected in the scanned portion of the window'
                }));
                return;
            }
            renderStateInto(container, 'fas fa-info-circle', 'No suspicious patterns detected');
            return;
        }
        const cards = patterns.map(function (pattern) {
            if (!pattern || typeof pattern !== 'object') return null;
            const severity = SEVERITY_CLASSES.has(String(pattern.severity || '').toLowerCase())
                ? String(pattern.severity).toLowerCase() : 'low';
            const patternType = safeText(pattern.pattern_type, 'unknown');
            const locations = (Array.isArray(pattern.locations) ? pattern.locations : [])
                .map(function (l) { return pipelineDisplayName(l); }).join(', ') || 'Unknown';
            function detail(label, value) {
                return el('div', { className: 'pattern-detail-item' }, [
                    el('span', { className: 'pattern-detail-label', text: label }),
                    el('span', { className: 'pattern-detail-value', text: value })
                ]);
            }
            const card = el('div', {
                className: 'pattern-card pattern-card-clickable',
                attrs: { role: 'button', tabindex: '0',
                         title: 'Click for details: who, where and the evidence' }
            }, [
                el('div', { className: 'pattern-header' }, [
                    el('div', { className: 'pattern-type' }, [
                        faIcon(PATTERN_ICONS[patternType] || 'fas fa-exclamation-triangle'),
                        document.createTextNode(' ' + patternType.replace(/_/g, ' ').toUpperCase())
                    ]),
                    el('span', { className: 'pattern-severity ' + severity, text: severity })
                ]),
                el('div', { className: 'pattern-description', text: safeText(pattern.description) }),
                el('div', { className: 'pattern-details' }, [
                    detail('Confidence', formatPercent01(pattern.confidence)),
                    detail('Identities Involved', String((Array.isArray(pattern.identities_involved) ? pattern.identities_involved : []).length)),
                    detail('Locations', locations),
                    detail('First Detected', fmtDateTime(pattern.first_detected))
                ]),
                el('div', { className: 'pattern-open-hint' }, [
                    faIcon('fas fa-up-right-from-square'),
                    document.createTextNode(' View details')
                ])
            ]);
            // The pattern object rides on the node; the container's single
            // delegated listener (installed once, below) opens the popup.
            card.__pattern = pattern;
            return card;
        }).filter(Boolean);
        if (truncated) {
            cards.unshift(buildTruncationNote());
        }
        container.replaceChildren.apply(container, cards);
    }

    // ============================================
    // Pattern detail popup
    // ============================================
    //
    // Cards summarise; the popup answers "who exactly, where exactly, and
    // why did the detector fire". Every field comes from the pattern object
    // already on the client; the only extra request resolves the involved
    // identity ids to names and thumbnails (one light list query per id,
    // matched on the full id), so the popup opens immediately and fills in.

    const PATTERN_EVIDENCE_LABELS = {
        group_size: 'People together',
        window_start: 'Window start',
        window_end: 'Window end',
        window_minutes: 'Window (minutes)',
        group_recurrence: 'Times this exact group was seen together',
        window_time: 'Window',
        off_hour_appearances: 'Off-hours sightings',
        total_appearances: 'All sightings in range',
        off_hours_share: 'Share of activity that is off-hours',
        time_range: 'Off-hours window (local time)',
        timezones: 'Timezone used',
        from_location: 'From camera',
        to_location: 'To camera',
        time_seconds: 'Seconds between cameras',
        implied_speed_kmh: 'Implied speed (km/h)',
        distance_meters: 'Distance (m)',
        pipeline_id: 'Camera'
    };

    function formatEvidenceValue(key, value) {
        if (value === null || value === undefined || value === '') return '-';
        if (key === 'from_location' || key === 'to_location' || key === 'pipeline_id') {
            return pipelineDisplayName(String(value));
        }
        if (key === 'window_start' || key === 'window_end' || key === 'window_time') {
            return fmtDateTime(value);
        }
        if (key === 'off_hours_share') return Math.round(Number(value) * 100) + '%';
        if (key === 'implied_speed_kmh' || key === 'distance_meters') {
            return String(Math.round(Number(value) * 10) / 10);
        }
        if (Array.isArray(value)) return value.map(function (v) { return safeText(v); }).join(', ');
        if (typeof value === 'object') return JSON.stringify(value);
        return safeText(value);
    }

    async function resolveIdentityRow(identityId) {
        // /api/admin/identities matches the uuid as a prefix; asking with the
        // full id and filtering on exact id keeps this precise and cheap.
        try {
            const data = await api('/api/admin/identities', {
                params: { q: identityId, page: 1, page_size: 5 }
            });
            const rows = ((data && data.items) || []).map(normalizeIdentity).filter(Boolean);
            return rows.find(function (r) { return r.id === normalizeId(identityId); }) || null;
        } catch (_) {
            return null;
        }
    }

    function buildIdentityTile(identityId, row) {
        const id = normalizeId(identityId);
        const name = row ? row.displayName : ('Unknown #' + shortId(id));
        const link = el('a', {
            className: 'pattern-identity-link',
            attrs: { href: '/admin/identity/' + encodeURIComponent(id),
                     title: 'Open profile' }
        }, [faIcon('fas fa-user'), document.createTextNode(' Profile')]);
        return el('div', { className: 'pattern-identity-tile' }, [
            el('div', { className: 'identity-item-thumbnail' },
                row && row.snapshotUrl ? safeImg(row.snapshotUrl, name) : faIcon('fas fa-user')),
            el('div', { className: 'pattern-identity-info' }, [
                el('div', { className: 'identity-item-name', text: name }),
                el('div', { className: 'identity-item-meta' }, [
                    el('span', { className: 'identity-item-type ' + (row ? row.type : 'unknown'),
                                 text: row ? row.type : 'unknown' }),
                    buildIdChip(id),
                    link
                ])
            ])
        ]);
    }

    function openPatternDetail(pattern) {
        const modal = document.getElementById('pattern-detail-modal');
        const body = document.getElementById('pattern-detail-body');
        const title = document.getElementById('pattern-detail-title');
        if (!modal || !body || !pattern) return;

        const patternType = safeText(pattern.pattern_type, 'unknown');
        const severity = SEVERITY_CLASSES.has(String(pattern.severity || '').toLowerCase())
            ? String(pattern.severity).toLowerCase() : 'low';
        title.replaceChildren(
            faIcon(PATTERN_ICONS[patternType] || 'fas fa-exclamation-triangle'),
            document.createTextNode(' ' + patternType.replace(/_/g, ' ').toUpperCase()));

        const ids = (Array.isArray(pattern.identities_involved) ? pattern.identities_involved : [])
            .map(normalizeId).filter(Boolean);
        const range = Array.isArray(pattern.time_range) ? pattern.time_range : [];
        const locations = (Array.isArray(pattern.locations) ? pattern.locations : []);

        function fact(label, value) {
            return el('div', { className: 'pattern-detail-item' }, [
                el('span', { className: 'pattern-detail-label', text: label }),
                el('span', { className: 'pattern-detail-value', text: value })
            ]);
        }

        const summary = el('div', { className: 'pattern-detail-summary' }, [
            el('span', { className: 'pattern-severity ' + severity, text: severity }),
            el('p', { className: 'pattern-description', text: safeText(pattern.description) }),
            el('div', { className: 'pattern-details' }, [
                fact('Confidence', formatPercent01(pattern.confidence)),
                fact('Where', locations.map(function (l) { return pipelineDisplayName(l); }).join(', ') || 'Unknown'),
                fact('From', range[0] ? fmtDateTime(range[0]) : fmtDateTime(pattern.first_detected)),
                fact('To', range[1] ? fmtDateTime(range[1]) : '-')
            ])
        ]);

        const evidence = pattern.evidence && typeof pattern.evidence === 'object' ? pattern.evidence : {};
        const evidenceRows = Object.keys(evidence).map(function (key) {
            return el('div', { className: 'pattern-evidence-row' }, [
                el('span', { className: 'pattern-detail-label',
                             text: PATTERN_EVIDENCE_LABELS[key] || key.replace(/_/g, ' ') }),
                el('span', { className: 'pattern-detail-value',
                             text: formatEvidenceValue(key, evidence[key]) })
            ]);
        });
        const evidenceSection = el('section', { className: 'pattern-detail-section' }, [
            el('h3', { text: 'Why this was flagged' }),
            el('div', { className: 'pattern-evidence' },
                evidenceRows.length ? evidenceRows : [el('p', { text: 'No additional evidence recorded.' })])
        ]);

        const grid = el('div', { className: 'pattern-identity-grid' },
            ids.map(function (id) { return buildIdentityTile(id, null); }));
        const peopleSection = el('section', { className: 'pattern-detail-section' }, [
            el('h3', { text: ids.length === 1 ? 'Person involved' : ids.length + ' people involved' }),
            grid
        ]);

        body.replaceChildren(summary, evidenceSection, peopleSection);

        if (window.ModalStack) {
            window.ModalStack.open(modal, { backdropClose: true });
        } else {
            modal.style.display = 'flex';
        }

        // Fill names and thumbnails in place; a stale fill (user opened a
        // different pattern meanwhile) is dropped by the generation check.
        const gen = (openPatternDetail._gen = (openPatternDetail._gen || 0) + 1);
        ids.forEach(function (id, index) {
            resolveIdentityRow(id).then(function (row) {
                if (openPatternDetail._gen !== gen || !row) return;
                const tile = grid.children[index];
                if (tile) grid.replaceChild(buildIdentityTile(id, row), tile);
            });
        });
    }

    function closePatternDetail() {
        const modal = document.getElementById('pattern-detail-modal');
        if (!modal) return;
        if (window.ModalStack && window.ModalStack.isOpen(modal)) window.ModalStack.close(modal);
        else modal.style.display = 'none';
    }

    /** The Detect Patterns camera dropdown: every pipeline the page knows,
     *  by display name, with "All cameras" first. Keeps the current choice
     *  across reloads of the pipeline list. */
    function populatePatternPipelineFilter() {
        const select = document.getElementById('patterns-pipeline-id');
        if (!select) return;
        const current = select.value;
        const options = [el('option', { text: 'All cameras', attrs: { value: '' } })];
        state.pipelines
            .slice()
            .sort(function (a, b) { return a.displayName.localeCompare(b.displayName); })
            .forEach(function (p) {
                options.push(el('option', { text: p.displayName, attrs: { value: p.id } }));
            });
        select.replaceChildren.apply(select, options);
        if (current && state.pipelines.some(function (p) { return p.id === current; })) {
            select.value = current;
        }
    }

    function installPatternDetailHandlers() {
        const container = document.getElementById('patterns-container');
        const closeBtn = document.getElementById('close-pattern-detail-modal');
        if (container && !container.__patternDetailWired) {
            container.__patternDetailWired = true;
            container.addEventListener('click', function (e) {
                const card = e.target.closest('.pattern-card-clickable');
                if (card && card.__pattern) openPatternDetail(card.__pattern);
            });
            container.addEventListener('keydown', function (e) {
                if (e.key !== 'Enter' && e.key !== ' ') return;
                const card = e.target.closest('.pattern-card-clickable');
                if (card && card.__pattern) { e.preventDefault(); openPatternDetail(card.__pattern); }
            });
        }
        if (closeBtn && !closeBtn.__wired) {
            closeBtn.__wired = true;
            closeBtn.addEventListener('click', closePatternDetail);
        }
    }

    // ============================================
    // Anomalies
    // ============================================

    async function loadAnomalies() {
        const container = document.getElementById('anomalies-container');
        if (!container) return;
        const identityId = getSelectorValue('anomaly-identity-id');
        if (!identityId) { showNotification('Please select an identity', 'error'); return; }
        const daysBack = toNonNegativeInteger(document.getElementById('anomaly-days-back') &&
            document.getElementById('anomaly-days-back').value, 90) || 90;

        const req = beginRequest('anomalies');
        renderLoading(container, 'Detecting anomalies...');
        try {
            const data = await api('/api/security/anomalies/' + encodeURIComponent(identityId), {
                signal: req.signal, timeout: LONG_TIMEOUT_MS, params: { days_back: daysBack }
            });
            if (!req.isCurrent()) return;
            // anomaly-v2 envelope: {items, baseline: {sufficient, samples, ...}}
            const items = data && Array.isArray(data.items) ? data.items
                : (Array.isArray(data) ? data : []);
            renderAnomalies(items, data && typeof data.baseline === 'object' ? data.baseline : null);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderError(container, err.status === 404 ? 'Identity not found' : 'Failed to load anomalies', err.referenceId);
        }
    }

    function renderAnomalies(anomalies, baseline) {
        const container = document.getElementById('anomalies-container');
        if (!container) return;
        // 'Not enough history to judge' is NOT the same claim as 'behavior is
        // normal' — the green check must never cover an empty baseline.
        if (baseline && baseline.sufficient === false) {
            const samples = toNonNegativeInteger(baseline.samples, 0);
            renderStateInto(container, 'fas fa-hourglass-half',
                'Insufficient baseline — only ' + samples +
                ' earlier appearance' + (samples === 1 ? '' : 's') +
                ' to compare against. Anomaly detection needs more history before it can judge this identity.');
            return;
        }
        if (!anomalies.length) {
            renderStateInto(container, 'fas fa-check-circle', 'No anomalies detected');
            return;
        }
        const cards = anomalies.map(function (anomaly) {
            if (!anomaly || typeof anomaly !== 'object') return null;
            function detail(label, value) {
                return el('div', { className: 'pattern-detail-item' }, [
                    el('span', { className: 'pattern-detail-label', text: label }),
                    el('span', { className: 'pattern-detail-value', text: value })
                ]);
            }
            return el('div', { className: 'anomaly-card' }, [
                el('div', { className: 'anomaly-header' }, [
                    el('div', { className: 'anomaly-type', text: safeText(anomaly.anomaly_type, 'unknown').replace(/_/g, ' ').toUpperCase() }),
                    el('span', { className: 'anomaly-severity', text: safeText(anomaly.severity, 'unknown') })
                ]),
                el('div', { className: 'anomaly-description', text: safeText(anomaly.description) }),
                el('div', { className: 'anomaly-details' }, [
                    detail('Risk Score', formatScore(anomaly.risk_score)),
                    detail('Detected At', fmtDateTime(anomaly.detected_at))
                ])
            ]);
        }).filter(Boolean);
        container.replaceChildren.apply(container, cards);
    }

    // ============================================
    // Threat assessment
    // ============================================

    async function loadThreatAssessment() {
        const container = document.getElementById('threat-container');
        if (!container) return;
        const identityId = getSelectorValue('threat-identity-id');
        if (!identityId) { showNotification('Please select an identity', 'error'); return; }

        const req = beginRequest('threats');
        renderLoading(container, 'Assessing threat...');
        try {
            const assessment = await api('/api/security/threat/' + encodeURIComponent(identityId), {
                signal: req.signal, timeout: LONG_TIMEOUT_MS
            });
            if (!req.isCurrent()) return;
            renderThreatAssessment(assessment || {});
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderError(container, err.status === 404 ? 'Identity not found' : 'Failed to load threat assessment', err.referenceId);
        }
    }

    function renderThreatAssessment(assessment) {
        const container = document.getElementById('threat-container');
        if (!container) return;
        const threatLevel = THREAT_LEVEL_CLASSES.has(String(assessment.threat_level || '').toLowerCase())
            ? String(assessment.threat_level).toLowerCase() : 'minimal';
        const levelClass = severityClass(threatLevel);
        const riskFactors = Array.isArray(assessment.risk_factors) ? assessment.risk_factors : [];
        const recommendations = Array.isArray(assessment.recommendations) ? assessment.recommendations : [];

        const children = [
            el('div', { className: 'threat-header' }, [
                el('div', { className: 'threat-identity' }, [
                    el('h3', { className: 'threat-identity-name', text: safeText(assessment.display_name, 'Unknown Identity') }),
                    el('div', { className: 'threat-identity-id', text: safeText(assessment.identity_id) })
                ]),
                el('div', { className: 'threat-score-container' }, [
                    el('div', { className: 'threat-score ' + levelClass, text: formatScore(assessment.overall_risk_score) }),
                    el('div', { className: 'threat-level ' + levelClass, text: threatLevel.toUpperCase() })
                ])
            ]),
            el('div', { className: 'risk-factors' },
                [el('h3', { text: 'Risk Factors' })].concat(riskFactors.map(function (factor) {
                    if (!factor || typeof factor !== 'object') return null;
                    return el('div', { className: 'risk-factor-item' }, [
                        el('div', { className: 'risk-factor-header' }, [
                            el('span', { className: 'risk-factor-name', text: safeText(factor.factor) }),
                            el('span', { className: 'risk-factor-score', text: '+' + formatScore(factor.score) })
                        ]),
                        el('div', { className: 'risk-factor-description', text: safeText(factor.description) })
                    ]);
                }).filter(Boolean)))
        ];
        if (assessment.last_assessed) {
            children.push(el('p', {
                className: 'threat-calculated-at',
                text: 'Calculated: ' + fmtDateTime(assessment.last_assessed) +
                    (assessment.algorithm_version ? ' · ' + safeText(assessment.algorithm_version) : '')
            }));
        }
        if (recommendations.length) {
            children.push(el('div', { className: 'recommendations' },
                [el('h3', { text: 'Recommendations' })].concat(recommendations.map(function (rec) {
                    return el('div', { className: 'recommendation-item', text: safeText(rec) });
                }))));
        }
        container.replaceChildren(el('div', { className: 'threat-card' }, children));
    }

    // ============================================
    // Threshold learning (background job + polling)
    // ============================================

    function stopJobPolling() {
        if (state.jobPollTimer) { window.clearTimeout(state.jobPollTimer); state.jobPollTimer = null; }
    }

    function renderThresholdTable(thresholds) {
        const header = el('tr', {}, ['Camera 1', 'Camera 2', 'Time Window (min)', 'Distance (m)', 'Confidence', 'Samples']
            .map(function (h) { return el('th', { text: h }); }));
        const rows = thresholds.map(function (t) {
            if (!t || typeof t !== 'object') return null;
            return el('tr', {}, [
                el('td', { text: safeText(t.camera_1) }),
                el('td', { text: safeText(t.camera_2) }),
                el('td', { text: formatScore(t.optimal_time_window_minutes) }),
                el('td', { text: formatScore(t.optimal_distance_meters, 0) }),
                el('td', { text: formatPercent01(t.confidence, 0) }),
                el('td', { text: String(toNonNegativeInteger(t.sample_count, 0)) })
            ]);
        }).filter(Boolean);
        const table = el('table', { className: 'threshold-table' }, [el('thead', {}, header), el('tbody', {}, rows)]);
        const scroller = el('div');
        scroller.style.cssText = 'margin-top:10px;max-height:400px;overflow-y:auto;';
        scroller.append(table);
        return scroller;
    }

    function renderThresholdJobResult(resultsDiv, task) {
        const result = (task && task.result) || {};
        const thresholds = Array.isArray(result.thresholds) ? result.thresholds : [];
        const children = [
            faIcon('fas fa-check-circle'),
            el('h4', { text: 'Threshold Learning Complete' }),
            el('p', { text: 'Learned thresholds for ' + toNonNegativeInteger(result.learned_pairs, thresholds.length) + ' camera pairs' }),
            el('p', {
                className: 'threshold-meta',
                text: 'Algorithm: ' + safeText(result.algorithm_version, 'unknown') +
                    ' — calculated ' + fmtDateTime(result.calculated_at)
            })
        ];
        if (thresholds.length) {
            const details = el('details');
            details.append(el('summary', { text: 'View Learned Thresholds (' + thresholds.length + ')' }));
            details.append(renderThresholdTable(thresholds));
            children.push(details);
        }
        resultsDiv.replaceChildren(el('div', { className: 'success-message' }, children));
    }

    async function pollThresholdJob(jobId, resultsDiv, attempt) {
        if (attempt > JOB_POLL_MAX) {
            renderError(resultsDiv, 'Job is taking too long — check the Background Tasks page', jobId);
            return;
        }
        try {
            const task = await api('/api/intelligence/thresholds/jobs/' + encodeURIComponent(jobId));
            const status = safeText(task && task.status, 'unknown');
            if (status === 'completed') {
                renderThresholdJobResult(resultsDiv, task);
                showNotification('Threshold learning finished', 'success');
                return;
            }
            if (status === 'failed') {
                renderError(resultsDiv, 'Threshold learning failed — see Background Tasks for details', jobId);
                return;
            }
            const progress = toNonNegativeInteger(task && task.progress_percent, 0);
            renderStateInto(resultsDiv, 'fas fa-spinner fa-spin',
                'Learning thresholds — job ' + jobId + ' (' + status + (progress ? ', ' + progress + '%' : '') + ')');
        } catch (err) {
            if (err.aborted) return;
            if (err.status === 404) {
                // The job is gone from tracking — either a synchronous
                // (deprecated-endpoint) run that finished, or history was
                // pruned. Polling to the cap would end with a misleading
                // "check Background Tasks" for a job that is not there.
                renderStateInto(resultsDiv, 'fas fa-info-circle',
                    'Threshold job ' + jobId + ' is no longer tracked — it likely finished. Re-run to see fresh results.');
                return;
            }
            // transient poll failure — keep trying within the bound
        }
        state.jobPollTimer = window.setTimeout(function () {
            pollThresholdJob(jobId, resultsDiv, attempt + 1);
        }, JOB_POLL_INTERVAL_MS);
    }

    async function learnThresholds(allPipelines) {
        const resultsDiv = document.getElementById('threshold-results');
        if (!resultsDiv) return;
        stopJobPolling();
        const selected = allPipelines ? [] : (getSelectorValue('threshold-pipeline-ids') || []);
        const btn = document.getElementById('threshold-learn-btn');
        if (btn) btn.disabled = true;
        renderLoading(resultsDiv, 'Scheduling threshold learning job...');
        try {
            const result = await api('/api/intelligence/thresholds/jobs', {
                method: 'POST',
                params: Array.isArray(selected) && selected.length ? { pipeline_ids: selected.join(',') } : {}
            });
            const jobId = safeText(result && result.job_id);
            showNotification('Threshold learning scheduled (job ' + jobId + ')', 'success');
            pollThresholdJob(jobId, resultsDiv, 1);
        } catch (err) {
            if (err.status === 409) {
                const running = err.jobId ? safeText(err.jobId) : null;
                showNotification('A threshold learning job is already running' + (running ? ' (job ' + running + ')' : ''), 'info');
                if (running) pollThresholdJob(running, resultsDiv, 1);
            } else if (!err.aborted) {
                renderError(resultsDiv, 'Failed to schedule threshold learning', err.referenceId);
                showNotification('Failed to schedule threshold learning', 'error');
            }
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    // ============================================
    // Trajectory prediction
    // ============================================

    async function predictTrajectory() {
        const resultsDiv = document.getElementById('trajectory-results');
        if (!resultsDiv) return;
        const identityId = getSelectorValue('trajectory-identity-id');
        const cameraSelect = document.getElementById('trajectory-current-camera');
        const currentCamera = cameraSelect ? normalizeId(cameraSelect.value) : null;
        const topK = clamp(document.getElementById('trajectory-top-k') &&
            document.getElementById('trajectory-top-k').value, 1, 10) || 3;

        if (!identityId || !currentCamera) {
            showNotification('Please select an identity and current camera', 'error');
            return;
        }
        const req = beginRequest('trajectory');
        renderLoading(resultsDiv, 'Predicting trajectory...');
        try {
            const data = await api('/api/intelligence/trajectory/predict', {
                signal: req.signal, timeout: LONG_TIMEOUT_MS,
                params: { identity_id: identityId, current_camera: currentCamera, top_k: Math.round(topK) }
            });
            if (!req.isCurrent()) return;
            renderTrajectory(data || {}, identityId, currentCamera);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderError(resultsDiv, err.status === 404 ? 'Identity not found' : 'Failed to predict trajectory', err.referenceId);
        }
    }

    function renderTrajectory(data, identityId, currentCamera) {
        const resultsDiv = document.getElementById('trajectory-results');
        if (!resultsDiv) return;
        const predictions = Array.isArray(data.predictions) ? data.predictions : [];

        if (data.insufficient_evidence === true || !predictions.length) {
            resultsDiv.replaceChildren(el('div', { className: 'info-message' }, [
                faIcon('fas fa-info-circle'),
                el('h4', { text: 'Insufficient Evidence' }),
                el('p', { text: 'Not enough historical trajectories to make a prediction for this identity from this camera.' })
            ]));
            return;
        }

        const items = predictions.map(function (pred, idx) {
            if (!pred || typeof pred !== 'object') return null;
            const confidence = ['high', 'moderate', 'low'].indexOf(String(pred.confidence || '').toLowerCase()) >= 0
                ? String(pred.confidence).toLowerCase() : 'low';
            return el('div', { className: 'prediction-item confidence-' + confidence }, [
                el('div', { className: 'prediction-main' }, [
                    el('strong', { text: '#' + (idx + 1) + ' ' + pipelineDisplayName(pred.camera_id) }),
                    el('span', { className: 'prediction-prob', text: ' ' + formatPercent01(pred.probability) + ' probability (' + confidence + ' confidence)' })
                ]),
                el('div', { className: 'prediction-time', text: 'Estimated around ' + fmtDateTime(pred.estimated_time) + ' (projection, not certainty)' })
            ]);
        }).filter(Boolean);

        resultsDiv.replaceChildren(el('div', { className: 'success-message' }, [
            faIcon('fas fa-route'),
            el('h4', { text: 'Trajectory Predictions' }),
            el('p', { className: 'identity-selected-id' }, [
                el('span', { text: 'Identity: ' }), (function () {
                    const chip = buildIdChip(identityId);
                    chip.querySelector('code').textContent = normalizeId(identityId);
                    return chip;
                })(),
                el('span', { text: ' \u2014 current camera: ' + pipelineDisplayName(currentCamera) })
            ]),
            el('p', { className: 'prediction-note', text: safeText(data.note, 'Estimated times are statistical projections, not certainties.') }),
            el('div', { className: 'predictions-list' }, items),
            el('p', { className: 'prediction-model', text: 'Model: ' + safeText(data.model_version, 'unknown') })
        ]));
    }

    // ============================================
    // Activity correlation (association — NOT causation)
    // ============================================

    async function calculateCorrelation() {
        const resultsDiv = document.getElementById('correlation-results');
        if (!resultsDiv) return;
        const identityA = getSelectorValue('correlation-identity-a');
        const identityB = getSelectorValue('correlation-identity-b');
        const daysBack = toNonNegativeInteger(document.getElementById('correlation-days-back') &&
            document.getElementById('correlation-days-back').value, 90) || 90;

        if (!identityA || !identityB) {
            showNotification('Please select both identities', 'error');
            return;
        }
        const req = beginRequest('correlation');
        renderLoading(resultsDiv, 'Calculating association...');
        try {
            const data = await api('/api/intelligence/correlation/calculate', {
                signal: req.signal, timeout: LONG_TIMEOUT_MS,
                params: { identity_a: identityA, identity_b: identityB, days_back: daysBack }
            });
            if (!req.isCurrent()) return;
            renderCorrelation(data || {}, identityA, identityB);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderError(resultsDiv, err.status === 404 ? 'Identity not found' : 'Failed to calculate correlation', err.referenceId);
        }
    }

    function renderCorrelation(data, identityA, identityB) {
        const resultsDiv = document.getElementById('correlation-results');
        if (!resultsDiv) return;
        const strength = STRENGTH_CLASSES.has(String(data.correlation_strength || '').toLowerCase())
            ? String(data.correlation_strength).toLowerCase() : 'none';
        const sequences = Array.isArray(data.sequences) ? data.sequences : [];
        const sequenceCount = toNonNegativeInteger(data.sequence_count, sequences.length);

        const children = [
            faIcon('fas fa-link'),
            el('h4', { text: 'Activity Association Analysis' }),
            el('p', {
                className: 'correlation-note',
                text: safeText(data.note, 'Measures temporal and spatial association between two identities. Correlation does not prove causation.')
            }),
            el('div', { className: 'correlation-summary' }, [
                el('div', { text: shortId(identityA) + '... ↔ ' + shortId(identityB) + '...' }),
                el('div', { className: 'correlation-score strength-' + strength, text: formatPercent01(data.correlation_score) })
            ]),
            el('div', { className: 'correlation-stats' }, [
                el('div', { className: 'correlation-stat' }, [
                    el('div', { className: 'stat-label', text: 'Association Strength' }),
                    el('div', { className: 'stat-value strength-' + strength, text: strength.toUpperCase() })
                ]),
                el('div', { className: 'correlation-stat' }, [
                    el('div', { className: 'stat-label', text: 'Activity Sequences' }),
                    el('div', { className: 'stat-value', text: String(sequenceCount) })
                ])
            ])
        ];

        if (data.insufficient_evidence === true) {
            const warning = el('p', {
                className: 'correlation-warning',
                text: 'Low sample size (' + sequenceCount + ' sequences) — this result may be coincidental and should not be relied on alone.'
            });
            warning.style.color = '#ffaa00';
            children.push(warning);
        }

        if (sequences.length) {
            const details = el('details');
            details.append(el('summary', { text: 'View Activity Sequences (' + sequences.length + ')' }));
            const list = el('div', { className: 'sequence-list' }, sequences.map(function (seq) {
                if (!seq || typeof seq !== 'object') return null;
                return el('div', { className: 'sequence-item' }, [
                    el('strong', { text: pipelineDisplayName(seq.from_camera) }),
                    document.createTextNode(' → '),
                    el('strong', { text: pipelineDisplayName(seq.to_camera) }),
                    el('span', { className: 'sequence-meta', text: ' ' + formatScore(seq.time_diff_minutes) + ' min — ' + fmtDateTime(seq.from_time) })
                ]);
            }).filter(Boolean));
            details.append(list);
            children.push(details);
        }

        resultsDiv.replaceChildren(el('div', { className: 'success-message' }, children));
    }

    // ============================================
    // Feature status + help (real capabilities, accessible modal)
    // ============================================

    async function showFeatureStatus() {
        await loadCapabilities();
        const caps = state.capabilities;
        if (!caps) {
            showModal('Feature Status', [el('p', { text: 'Could not load capability status from the backend.' })]);
            return;
        }
        const rows = Object.keys(caps).map(function (key) {
            const cap = caps[key] || {};
            const label = key.replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
            const status = safeText(cap.status, 'unknown').replace(/_/g, ' ');
            const enabled = cap.enabled === true;
            const line = el('div', { className: 'capability-row' }, [
                el('strong', { text: label + ': ' }),
                el('span', { text: (enabled ? 'enabled' : 'disabled') + ' — ' + status + (cap.job_id ? ' (job ' + safeText(cap.job_id) + ')' : '') })
            ]);
            line.style.cssText = 'padding:0.3rem 0;border-bottom:1px solid rgba(255,255,255,0.08);' +
                (enabled ? '' : 'opacity:0.6;');
            return line;
        });
        showModal('Feature Status (backend-verified)', rows);
    }

    function showFeatureHelp() {
        showModal('Advanced SNA Features Help', [
            el('h4', { text: '1. Automatic Threshold Learning' }),
            el('p', { text: 'Learns optimal distance/time thresholds per camera pair from historical data. Runs as a background job — progress appears in Background Tasks.' }),
            el('h4', { text: '2. Trajectory Prediction' }),
            el('p', { text: 'Predicts where a person may appear next based on historical movement. Estimates are statistical projections with confidence levels, not certainties.' }),
            el('h4', { text: '3. Activity Correlation' }),
            el('p', { text: 'Measures temporal and spatial association between two identities. Correlation does not prove causation — treat low-sample results as inconclusive.' })
        ]);
    }

    // ============================================
    // Map view (MapLibre)
    // ============================================

    // Missing control === feature OFF. Never default a security feature on.
    function readCheckbox(id, fallback) {
        const element = document.getElementById(id);
        return element ? element.checked === true : (fallback === true);
    }

    async function loadMapView() {
        const mapContainer = document.getElementById('security-map');
        if (!mapContainer) return;
        const identityId = getSelectorValue('map-identity-id');
        if (!identityId) { showNotification('Please select an identity first', 'error'); return; }

        const req = beginRequest('map');
        const btn = document.getElementById('map-load-btn');
        if (btn) btn.disabled = true;
        renderLoading(mapContainer, 'Loading map...');
        try {
            const params = {};
            const date = document.getElementById('map-date') && document.getElementById('map-date').value;
            if (date) params.date = date;
            else params.days_back = toNonNegativeInteger(document.getElementById('map-days-back') &&
                document.getElementById('map-days-back').value, 7) || 7;
            const styleSelect = document.getElementById('map-style-select');
            params.map_style = styleSelect && MAP_STYLES.indexOf(styleSelect.value) >= 0 ? styleSelect.value : 'light';
            params.include_popups = readCheckbox('map-include-popups', false);
            params.show_routes = readCheckbox('map-show-routes', false);
            params.cluster_markers = readCheckbox('map-cluster-markers', false);
            params.enable_security_features = readCheckbox('map-enable-security', false);
            params.detect_patterns = readCheckbox('map-detect-patterns', false);
            params.show_risk_heatmap = readCheckbox('map-show-heatmap', false);

            const flags = {
                popups: params.include_popups, cluster: params.cluster_markers,
                routes: params.show_routes, security: params.enable_security_features,
                patterns: params.detect_patterns, risk: params.show_risk_heatmap
            };
            const style = params.map_style;
            // Only server-side flags travel; display flags stay client-side.
            const serverParams = {
                date: params.date, days_back: params.days_back,
                show_routes: params.show_routes,
                enable_security_features: params.enable_security_features,
                detect_patterns: params.detect_patterns,
                show_risk_heatmap: params.show_risk_heatmap
            };

            // MapLibre renders INTO the page (no iframe, no backend HTML).
            const IM = await window.IdentityMap.ready;
            const dataKey = identityId + '|' + JSON.stringify(serverParams);
            let ctl = state.mapController;
            if (!ctl || ctl.container !== mapContainer || !ctl.map) {
                if (ctl) ctl.destroy();
                ctl = new IM.Controller(mapContainer, {
                    style: 'light',
                    onError: function (kind, detail) {
                        if (kind === 'dataset') showNotification('Map style not installed (' + detail.code + ')', 'error');
                    }
                });
                await ctl.init();
                await ctl.loadAvailability(styleSelect);
                state.mapController = ctl;
                state.mapDataKey = null;
            }
            if (!req.isCurrent()) return;
            if (ctl.isStyleAvailable(style)) {
                if (style !== ctl.style) await ctl.setBasemap(style);
            } else {
                if (styleSelect) styleSelect.value = ctl.style;
                showNotification('Map style "' + style + '" is unavailable: ' + IM.UNAVAILABLE, 'error');
            }
            if (!req.isCurrent()) return;
            if (state.mapDataKey !== dataKey) {
                await ctl.load(identityId, serverParams, flags);
                state.mapDataKey = dataKey;
            } else {
                Object.assign(ctl.flags, flags);
                ctl._restoreOverlays();
            }
            if (req.isCurrent() && btn) btn.disabled = false;
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderError(mapContainer,
                err.status === 503 ? 'Map service is temporarily unavailable' :
                    err.status === 404 ? 'Identity not found' : 'Map generation failed',
                err.referenceId);
            if (btn) btn.disabled = false;
        }
    }

    // ============================================
    // Selectors + trajectory camera options
    // ============================================

    function initializeSelectors() {
        installPatternDetailHandlers();
        createSelector(document.getElementById('network-identity-ids'), { mode: 'identity', multi: true, label: 'Select Identities' });
        createSelector(document.getElementById('anomaly-identity-id'), { mode: 'identity', multi: false, label: 'Select Identity' });
        createSelector(document.getElementById('threat-identity-id'), { mode: 'identity', multi: false, label: 'Select Identity' });
        createSelector(document.getElementById('trajectory-identity-id'), { mode: 'identity', multi: false, label: 'Select Identity' });
        createSelector(document.getElementById('correlation-identity-a'), { mode: 'identity', multi: false, label: 'Select Identity A' });
        createSelector(document.getElementById('correlation-identity-b'), { mode: 'identity', multi: false, label: 'Select Identity B' });
        createSelector(document.getElementById('map-identity-id'), { mode: 'identity', multi: false, label: 'Select Identity' });
        createSelector(document.getElementById('threshold-pipeline-ids'), { mode: 'pipeline', multi: true, label: 'Select Pipelines' });
    }

    function populateTrajectoryCameraSelect() {
        const select = document.getElementById('trajectory-current-camera');
        if (!select) return;
        select.replaceChildren(el('option', { text: '-- Select Camera --', attrs: { value: '' } }));
        for (const p of state.pipelines) {
            select.append(el('option', { text: p.displayName, attrs: { value: p.id } }));
        }
    }

    // URL preselection through the component API — no fixed delays.
    async function applyPreselectedUrlState() {
        const urlParams = new URLSearchParams(window.location.search);
        const identityId = normalizeId(urlParams.get('identity_id'));
        const tab = urlParams.get('tab');
        if (tab || identityId) switchTab(tab || 'network');
        if (!identityId) return;

        const targetByTab = {
            network: 'network-identity-ids',
            anomalies: 'anomaly-identity-id',
            threats: 'threat-identity-id',
            advanced: 'trajectory-identity-id',
            map: 'map-identity-id'
        };
        const selectId = targetByTab[state.activeTab] || 'threat-identity-id';
        const component = selectorRegistry.get(selectId);
        if (!component) return;
        const ok = await component.setValue(identityId);
        if (!ok) {
            showNotification('The identity from the link was not found', 'warning');
            return;
        }
        if (state.activeTab === 'anomalies') loadAnomalies();
        else if (state.activeTab === 'threats') loadThreatAssessment();
    }

    // ============================================
    // Wiring (no inline handlers; idempotent)
    // ============================================

    function attachOnce(id, evt, fn) {
        const node = document.getElementById(id);
        if (node && !node.dataset.listenerAttached) {
            node.addEventListener(evt, fn);
            node.dataset.listenerAttached = 'true';
        }
    }

    function setupStaticEventListeners() {
        document.querySelectorAll('.sec-tab').forEach(function (tab) {
            if (tab.dataset.listenerAttached) return;
            tab.addEventListener('click', function () { switchTab(tab.dataset.tab); });
            tab.dataset.listenerAttached = 'true';
        });
        attachOnce('network-analyze-btn', 'click', loadNetwork);
        attachOnce('patterns-detect-btn', 'click', loadPatterns);
        attachOnce('anomalies-detect-btn', 'click', loadAnomalies);
        attachOnce('threat-assess-btn', 'click', loadThreatAssessment);
        attachOnce('threshold-learn-btn', 'click', function () { learnThresholds(false); });
        attachOnce('learn-all-thresholds-btn', 'click', function () { learnThresholds(true); });
        attachOnce('trajectory-predict-btn', 'click', predictTrajectory);
        attachOnce('correlation-calc-btn', 'click', calculateCorrelation);
        attachOnce('feature-status-btn', 'click', showFeatureStatus);
        attachOnce('feature-help-btn', 'click', showFeatureHelp);
        attachOnce('map-load-btn', 'click', loadMapView);
    }

    function destroy() {
        abortAllRequests();
        if (state.mapController) { state.mapController.destroy(); state.mapController = null; state.mapDataKey = null; }
        stopJobPolling();
        destroyNetworkInstance();
        for (const id of Array.from(selectorRegistry.keys())) destroySelector(id);
        closeModal();
    }

    // Controlled startup: required data first, then components, then URL state.
    async function initializeSecurityIntelligence() {
        setupStaticEventListeners();
        await Promise.all([loadPipelines(), loadCapabilities()]);
        initializeSelectors();
        populateTrajectoryCameraSelect();
        await applyPreselectedUrlState();
    }

    document.addEventListener('DOMContentLoaded', initializeSecurityIntelligence);
    window.addEventListener('pagehide', destroy);
})();
