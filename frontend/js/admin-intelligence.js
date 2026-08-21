/**
 * Intelligence Analysis Frontend (hardened rewrite)
 * =================================================
 * Related identities, temporal patterns, cross-camera tracking, backend maps.
 *
 * Security/correctness contract:
 *  - No backend value is ever rendered through innerHTML or inline handlers;
 *    all dynamic rendering uses createElement/textContent/setAttribute.
 *  - The map is MapLibre GL JS rendering GeoJSON from /map-data over the
 *    offline Martin basemap (frontend/js/identity-map.js) — no iframe, no
 *    backend-rendered HTML, no external host.
 *  - Every resource has its own AbortController + generation counter: a
 *    stale response can never overwrite a newer selection.
 *  - Identity search is server-side and paginated — the browser never
 *    downloads the full identity population.
 *  - Expensive/security map features are opt-in: a missing checkbox is
 *    treated as false, never true.
 */

(function () {
    'use strict';

    const DEBUG = false;
    const SEARCH_DEBOUNCE_MS = 300;
    const MAP_DEBOUNCE_MS = 400;
    // 50, not 25: the picker lists 159 identities here and the point of it
    // is to find a face. Half the 'Load more' clicks for one request, and
    // well inside API_MAX_PAGE_SIZE (100), which the endpoint enforces.
    const PAGE_SIZE = 50;
    const API_TIMEOUT_MS = 30000;
    const MAP_TIMEOUT_MS = 60000;
    const MAX_IMAGE_URL_LENGTH = 2048;
    const MAX_TRANSIT_MS = 24 * 3600 * 1000;
    const MAP_STYLES = ['light', 'dark', 'satellite', 'terrain'];

    const DEFAULT_AVATAR = 'data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%2780%27 height=%2780%27%3E%3Crect fill=%27%23333%27 width=%2780%27 height=%2780%27/%3E%3Ccircle cx=%2740%27 cy=%2730%27 r=%2712%27 fill=%27%23999%27/%3E%3Cpath d=%27M 20 55 Q 20 45 30 45 L 50 45 Q 60 45 60 55 L 60 65 L 20 65 Z%27 fill=%27%23999%27/%3E%3C/svg%3E';

    const CHART_COLORS = [
        'rgba(102, 126, 234, 0.7)', 'rgba(118, 75, 162, 0.7)', 'rgba(255, 99, 132, 0.7)',
        'rgba(54, 162, 235, 0.7)', 'rgba(255, 206, 86, 0.7)', 'rgba(75, 192, 192, 0.7)',
        'rgba(255, 159, 64, 0.7)', 'rgba(46, 204, 113, 0.7)', 'rgba(231, 76, 60, 0.7)',
        'rgba(149, 165, 166, 0.7)'
    ];

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

    function toFiniteNumber(value, fallback) {
        const n = Number(value);
        return Number.isFinite(n) ? n : fallback;
    }

    function toNonNegativeInteger(value, fallback) {
        const n = Number(value);
        return Number.isInteger(n) && n >= 0 ? n : fallback;
    }

    function fmtPercent(value) {
        const n = toFiniteNumber(value, null);
        return n === null ? 'N/A' : n.toFixed(1) + '%';
    }

    function shortId(id) {
        const s = normalizeId(id);
        return s ? s.slice(0, 8) : '?';
    }

    /** best_snapshot_path -> a safe same-origin URL, or '' for the placeholder.
     *  Mirrors the canonical fallback in admin-search.js:1030-1045 — paths may
     *  arrive 'storage/'-prefixed, bare, already '/'-prefixed, or null, and a
     *  cross-origin or traversal value must never reach an img src. */
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
     *  uuid. A native <button>, so Enter/Space fire click natively — one
     *  handler, no duplicate keyboard logic. stopPropagation so copying never
     *  selects the row it sits in. */
    function buildIdChip(id) {
        const full = normalizeId(id);
        const codeEl = el('code', { text: shortId(full) + '…' });
        const btn = el('button', {
            className: 'identity-item-id',
            attrs: {
                type: 'button',
                title: full,
                'aria-label': 'Copy identity ID ' + full
            }
        }, [codeEl]);
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            e.preventDefault();
            const restore = codeEl.textContent;
            const done = function (ok) {
                codeEl.textContent = ok ? 'copied ✓' : 'copy failed';
                window.setTimeout(function () { codeEl.textContent = restore; }, 1200);
            };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(full).then(function () { done(true); },
                                                         function () { done(false); });
            } else { done(false); }
        });
        return btn;
    }

    // Strict timestamp parsing. Naive legacy values are UTC. Invalid
    // values become null (rendered "Unknown time"), never "now".
    function parseTimestamp(value) {
        if (typeof value !== 'string' || !value) return null;
        let v = value;
        if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(v)) v += 'Z';
        const d = new Date(v);
        return Number.isNaN(d.getTime()) ? null : d;
    }

    function fmtDateTime(value) {
        const d = parseTimestamp(value);
        if (!d) { log('[INTEL] unparseable timestamp:', typeof value); return 'Unknown time'; }
        return d.toLocaleString();
    }

    function fmtDate(value) {
        const d = parseTimestamp(value);
        return d ? d.toLocaleDateString() : 'Unknown time';
    }

    // Only same-origin relative URLs or the fixed local placeholder.
    function safeImageUrl(value) {
        if (typeof value !== 'string' || !value) return DEFAULT_AVATAR;
        if (value === DEFAULT_AVATAR) return value;
        if (value.length > MAX_IMAGE_URL_LENGTH) return DEFAULT_AVATAR;
        // "/path" but not protocol-relative "//host"
        if (/^\/(?!\/)/.test(value)) return value;
        return DEFAULT_AVATAR;
    }

    const STATUS_CLASS_ALLOWLIST = { known: 'known', unknown: 'unknown' };
    function typeClass(value) {
        return STATUS_CLASS_ALLOWLIST[String(value || '').toLowerCase()] || 'unknown';
    }

    // DOM builders — the only rendering primitives used in this file.
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
        const img = el('img', { className: className || '', attrs: { alt: safeText(alt, ''), loading: 'lazy' } });
        img.src = safeImageUrl(url);
        img.addEventListener('error', function onErr() {
            img.removeEventListener('error', onErr);
            img.src = DEFAULT_AVATAR;
        });
        return img;
    }

    function renderState(container, iconClass, message, referenceId) {
        if (!container) return;
        const children = [faIcon(iconClass), el('p', { text: message })];
        if (referenceId) children.push(el('p', { className: 'state-reference', text: 'Reference: ' + referenceId }));
        container.replaceChildren(el('div', { className: iconClass.indexOf('spinner') >= 0 ? 'loading-state' : 'empty-state' }, children));
    }

    function renderLoading(container, message) {
        renderState(container, 'fas fa-spinner fa-spin', message);
    }

    function renderError(container, message, referenceId) {
        renderState(container, 'fas fa-exclamation-triangle', message, referenceId);
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
            headers['X-Requested-With'] = 'XMLHttpRequest'; // CSRF token header
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
            let code = null, referenceId = null, message = 'Request failed (' + response.status + ')';
            try {
                const body = await response.json();
                const detail = body && (body.detail !== undefined ? body.detail : body.error);
                if (detail && typeof detail === 'object') {
                    code = detail.error_code || detail.code || null;
                    referenceId = detail.reference_id || null;
                    if (typeof detail.message === 'string') message = detail.message;
                    if (detail.job_id) referenceId = detail.job_id;
                } else if (typeof detail === 'string') {
                    message = detail;
                    const refMatch = detail.match(/Reference:\s*([A-Za-z0-9-]+)/);
                    if (refMatch) referenceId = refMatch[1];
                }
            } catch (_) { /* non-JSON error body — keep generic message */ }
            throw ApiError(message, { status: response.status, code: code, referenceId: referenceId });
        }

        if (options.expect === 'html') return response.text();
        if (response.status === 204) return null;
        return response.json();
    }

    // ============================================
    // Request lifecycle: per-resource abort + generation
    // ============================================

    const requests = { controllers: {}, generations: {} };

    function beginRequest(key) {
        if (requests.controllers[key]) requests.controllers[key].abort();
        const controller = new AbortController();
        requests.controllers[key] = controller;
        const gen = (requests.generations[key] || 0) + 1;
        requests.generations[key] = gen;
        return {
            signal: controller.signal,
            isCurrent: function () { return requests.generations[key] === gen; }
        };
    }

    function abortAllRequests() {
        for (const key of Object.keys(requests.controllers)) {
            try { requests.controllers[key].abort(); } catch (_) { /* noop */ }
        }
        requests.controllers = {};
    }

    // ============================================
    // State + elements
    // ============================================

    const state = {
        selectedIdentityId: null,
        selectedIdentity: null,
        activeTab: 'related',
        temporalCharts: {},
        pipelineNames: new Map(), // pipeline_id -> approved display name
        mapRetryUsed: false,
        mapDebounceTimer: null,
        // MapLibre controller (frontend/js/identity-map.js). One per page;
        // re-used across identities, destroyed on page teardown.
        mapController: null,
        mapLoadedFor: null,        // identityId + params the data was fetched for
        mapDataKey: null
    };

    const elements = {};
    function cacheElements() {
        elements.identitySelect = document.getElementById('identity-select');
        elements.selectedIdentityInfo = document.getElementById('selected-identity-info');
        elements.identityName = document.getElementById('identity-name');
        elements.identityType = document.getElementById('identity-type');
        elements.identityId = document.getElementById('identity-id');
        elements.identitySnapshot = document.getElementById('identity-snapshot');
        elements.intelligenceTabs = document.getElementById('intelligence-tabs');
        elements.minCoApp = document.getElementById('min-co-app');
        elements.timeWindow = document.getElementById('time-window');
        elements.temporalDaysBack = document.getElementById('temporal-days-back');
        elements.trackingDate = document.getElementById('tracking-date');
        elements.trackingDaysBack = document.getElementById('tracking-days-back');
        elements.relatedContainer = document.getElementById('related-identities-container');
        elements.temporalContainer = document.getElementById('temporal-container');
        elements.trackingContainer = document.getElementById('tracking-container');
        elements.completeContainer = document.getElementById('complete-analysis-container');
    }

    function pipelineDisplayName(pipelineId) {
        const id = normalizeId(pipelineId);
        if (!id) return 'Unknown location';
        return state.pipelineNames.get(id) || id;
    }

    // ============================================
    // Pipelines (accepts array or {items:[...]})
    // ============================================

    async function loadPipelines() {
        try {
            const data = await api('/api/pipelines');
            const list = Array.isArray(data) ? data : (data && Array.isArray(data.items) ? data.items : []);
            state.pipelineNames.clear();
            for (const p of list) {
                if (!p) continue;
                const id = normalizeId(p.pipeline_id);
                if (!id) continue;
                // Approved precedence: display/location name, then id
                const name = safeText(p.pipeline_name || p.location_name || id);
                state.pipelineNames.set(id, name);
            }
        } catch (err) {
            if (!err.aborted) log('[INTEL] pipelines load failed:', err.status);
        }
    }

    // ============================================
    // Identity picker (server-side search, accessible combobox)
    // ============================================

    const picker = { wrapper: null, cleanups: [], searchTimer: null, page: 1, totalPages: 1, activeIndex: -1, items: [] };

    function destroyIdentityPicker() {
        for (const fn of picker.cleanups.splice(0)) {
            try { fn(); } catch (_) { /* noop */ }
        }
        if (picker.searchTimer) { window.clearTimeout(picker.searchTimer); picker.searchTimer = null; }
        if (picker.wrapper && picker.wrapper.parentNode) picker.wrapper.parentNode.removeChild(picker.wrapper);
        picker.wrapper = null;
        picker.items = [];
        picker.activeIndex = -1;
    }

    function createIdentityPicker(originalSelect) {
        if (!originalSelect) return;
        destroyIdentityPicker(); // idempotent re-init
        originalSelect.style.display = 'none';
        originalSelect.setAttribute('aria-hidden', 'true');
        originalSelect.tabIndex = -1;

        const wrapper = el('div', { className: 'advanced-identity-selector' });
        picker.wrapper = wrapper;

        const listboxId = 'identity-picker-listbox';
        const trigger = el('button', {
            className: 'identity-selector-trigger',
            attrs: { type: 'button', 'aria-haspopup': 'listbox', 'aria-expanded': 'false', 'aria-controls': listboxId }
        }, [
            el('span', { className: 'trigger-text', text: 'Select Identity' }),
            faIcon('fas fa-chevron-down trigger-icon')
        ]);

        const panel = el('div', { className: 'identity-selector-panel' });
        panel.style.display = 'none';

        // --- filters ---
        const searchInput = el('input', {
            className: 'filter-search',
            attrs: {
                type: 'text', placeholder: 'Search by name or ID...', autocomplete: 'off',
                role: 'combobox', 'aria-autocomplete': 'list', 'aria-expanded': 'true',
                'aria-controls': listboxId
            }
        });
        const typeSelect = el('select', { className: 'filter-type', attrs: { 'aria-label': 'Filter by type' } }, [
            el('option', { text: 'All Types', attrs: { value: '' } }),
            el('option', { text: 'Known', attrs: { value: 'known' } }),
            el('option', { text: 'Unknown', attrs: { value: 'unknown' } })
        ]);
        const pipelineSelect = el('select', { className: 'filter-pipeline', attrs: { 'aria-label': 'Filter by pipeline' } },
            [el('option', { text: 'All Pipelines', attrs: { value: '' } })]);
        for (const entry of state.pipelineNames.entries()) {
            pipelineSelect.append(el('option', { text: entry[1], attrs: { value: entry[0] } }));
        }
        const lastSeenSelect = el('select', { className: 'filter-last-seen', attrs: { 'aria-label': 'Filter by last seen (rolling window)' } }, [
            el('option', { text: 'All Time', attrs: { value: '' } }),
            el('option', { text: 'Last 24 Hours', attrs: { value: '1' } }),
            el('option', { text: 'Last 7 Days', attrs: { value: '7' } }),
            el('option', { text: 'Last 30 Days', attrs: { value: '30' } }),
            el('option', { text: 'Last 90 Days', attrs: { value: '90' } })
        ]);

        // --- find by photo -------------------------------------------------
        const photoInput = el('input', { className: 'filter-photo-input', attrs: { type: 'file', accept: 'image/*' } });
        photoInput.style.display = 'none';
        const photoBtn = el('button', {
            className: 'filter-photo-btn',
            attrs: { type: 'button', title: 'Find by photo', 'aria-label': 'Find identity by photo' }
        }, [faIcon('fas fa-camera')]);
        const photoClearBtn = el('button', {
            className: 'filter-photo-clear',
            attrs: { type: 'button', title: 'Clear photo search', 'aria-label': 'Clear photo search' }
        }, [faIcon('fas fa-times'), el('span', { text: ' photo' })]);
        photoClearBtn.style.display = 'none';

        const filtersSection = el('div', { className: 'identity-selector-filters' }, [
            el('div', { className: 'filter-row' }, [searchInput, typeSelect, photoBtn, photoClearBtn, photoInput]),
            el('div', { className: 'filter-row' }, [
                el('label', { text: 'Pipeline:' }), pipelineSelect,
                el('label', { text: 'Last Seen:' }), lastSeenSelect
            ])
        ]);

        const statusLine = el('div', { className: 'identity-selector-status', attrs: { 'aria-live': 'polite', role: 'status' } });
        const resultsContainer = el('div', {
            className: 'identity-selector-results',
            attrs: { id: listboxId, role: 'listbox', 'aria-label': 'Identity results' }
        });
        const loadMoreBtn = el('button', { className: 'btn-secondary identity-load-more', text: 'Load more', attrs: { type: 'button' } });
        loadMoreBtn.style.display = 'none';

        panel.append(filtersSection, statusLine, resultsContainer, loadMoreBtn);
        wrapper.append(trigger, panel);
        originalSelect.parentNode.insertBefore(wrapper, originalSelect.nextSibling);


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

        function setExpanded(open) {
            panel.style.display = open ? 'block' : 'none';
            if (open) fitPanelToViewport();
            trigger.classList.toggle('active', open);
            trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
        }

        const refit = function () {
            if (panel.style.display !== 'none') fitPanelToViewport();
        };
        window.addEventListener('resize', refit);
        picker.cleanups.push(function () { window.removeEventListener('resize', refit); });

        function openPanel() {
            setExpanded(true);
            runSearch(true);
            // preventScroll: see the matching comment in
            // admin-security-intelligence.js. Focusing normally scrolls the
            // page scroller to reveal the search box, which moves the wrapper
            // the panel is positioned against and undoes the fit just computed.
            window.setTimeout(function () { searchInput.focus({ preventScroll: true }); }, 50);
        }
        function closePanel(restoreFocus) {
            setExpanded(false);
            // Nothing may keep running behind a closed picker, and reopening
            // must start clean rather than resuming a stale photo search.
            if (requests.controllers.identitySearch) {
                try { requests.controllers.identitySearch.abort(); } catch (_) { /* noop */ }
            }
            if (picker.photoMode) exitPhotoMode(false);
            if (restoreFocus) trigger.focus();
        }

        function setActive(index) {
            const options = resultsContainer.querySelectorAll('[role="option"]');
            if (!options.length) { picker.activeIndex = -1; searchInput.removeAttribute('aria-activedescendant'); return; }
            picker.activeIndex = Math.max(0, Math.min(index, options.length - 1));
            options.forEach(function (opt, i) {
                opt.classList.toggle('active', i === picker.activeIndex);
                opt.setAttribute('aria-selected', i === picker.activeIndex ? 'true' : 'false');
            });
            const active = options[picker.activeIndex];
            searchInput.setAttribute('aria-activedescendant', active.id);
            active.scrollIntoView({ block: 'nearest' });
        }

        // THE single authoritative selection path — exactly one identity
        // request per user selection.
        function chooseIdentity(identity) {
            const id = normalizeId(identity && identity.id);
            if (!id) return;
            const label = identity.display_name ? safeText(identity.display_name) : 'Unknown #' + shortId(id);
            trigger.querySelector('.trigger-text').textContent = label;
            // Update hidden select silently — no synthetic change event, so
            // no second load path can fire.
            originalSelect.replaceChildren(el('option', { text: label, attrs: { value: id, selected: 'selected' } }));
            originalSelect.value = id;
            closePanel(true);
            selectIdentity(id);
        }

        function renderResults(items, append) {
            if (!append) { resultsContainer.replaceChildren(); picker.items = []; }
            const baseIndex = picker.items.length;
            items.forEach(function (identity, i) {
                const id = normalizeId(identity && identity.id);
                if (!id) return;
                picker.items.push(identity);
                const displayName = identity.display_name ? safeText(identity.display_name) : 'Unknown #' + shortId(id);
                const item = el('div', {
                    className: 'identity-selector-item',
                    attrs: { role: 'option', id: 'identity-option-' + (baseIndex + i), 'aria-selected': 'false', tabindex: '-1' }
                }, [
                    el('div', { className: 'identity-item-thumbnail' },
                        identity.snapshot_url ? safeImg(identity.snapshot_url, displayName) : faIcon('fas fa-user')),
                    el('div', { className: 'identity-item-info' }, [
                        el('div', { className: 'identity-item-name', text: displayName }),
                        el('div', { className: 'identity-item-meta' }, [
                            el('span', { className: 'identity-item-type ' + typeClass(identity.type), text: safeText(identity.type, 'unknown') }),
                            buildIdChip(id),
                            el('span', {
                                className: 'identity-item-date',
                                text: 'Last seen: ' + (identity.last_seen_at ? fmtDate(identity.last_seen_at) : 'Never')
                            })
                        ].concat(typeof identity.similarity === 'number' ? [
                            el('span', {
                                className: 'identity-item-similarity',
                                text: Math.round(identity.similarity * 100) + '% match'
                            })
                        ] : []))
                    ]),
                    el('div', { className: 'identity-item-check' }, faIcon('fas fa-check'))
                ]);
                item.addEventListener('click', function () { chooseIdentity(identity); });
                resultsContainer.append(item);
            });
            if (!picker.items.length) {
                resultsContainer.replaceChildren(el('div', { className: 'no-results', text: 'No identities found' }));
            }
        }

        async function runSearch(reset) {
            if (reset) { picker.page = 1; }
            const req = beginRequest('identitySearch');
            statusLine.textContent = 'Loading identities...';
            loadMoreBtn.disabled = true;
            try {
                const data = await api('/api/admin/identities', {
                    signal: req.signal,
                    params: {
                        page: picker.page,
                        page_size: PAGE_SIZE,
                        q: searchInput.value.trim() || undefined,
                        type: typeSelect.value || undefined,
                        pipeline_id: pipelineSelect.value || undefined,
                        last_seen_within_days: lastSeenSelect.value || undefined
                    }
                });
                if (!req.isCurrent()) return;
                const items = (data && Array.isArray(data.items)) ? data.items : [];
                picker.totalPages = toNonNegativeInteger(data && data.total_pages, 1) || 1;
                renderResults(items, !reset && picker.page > 1);
                const total = toNonNegativeInteger(data && data.total, picker.items.length);
                statusLine.textContent = total === 0 ? 'No identities found' :
                    'Showing ' + picker.items.length + ' of ' + total + ' identities';
                loadMoreBtn.style.display = picker.page < picker.totalPages ? 'block' : 'none';
                loadMoreBtn.disabled = false;
                if (reset) setActive(-1);
            } catch (err) {
                if (err.aborted || !req.isCurrent()) return;
                statusLine.textContent = 'Failed to load identities';
                renderResults([], false);
                loadMoreBtn.disabled = false;
            }
        }

        function scheduleSearch() {
            if (picker.searchTimer) window.clearTimeout(picker.searchTimer);
            // Typing leaves photo mode: the text flow re-runs the paged list,
            // and beginRequest() aborts any in-flight photo request.
            if (picker.photoMode) exitPhotoMode(false);
            picker.searchTimer = window.setTimeout(function () { runSearch(true); }, SEARCH_DEBOUNCE_MS);
        }

        // --- find by photo -------------------------------------------------
        //
        // Same request key as the text search, so the two flows can never
        // race: beginRequest aborts the previous controller and creates a
        // fresh one per request (an aborted controller is never reused), and
        // isCurrent() drops any stale response that slips past the abort.

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
            picker.photoMode = false;
            photoClearBtn.style.display = 'none';
            photoInput.value = '';
            if (rerun) runSearch(true);
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

            const req = beginRequest('identitySearch');
            picker.photoMode = true;
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
                if (!req.isCurrent() || !picker.photoMode) return;
                // Normalise into the EXACT shape the text flow renders, then
                // reuse the same renderer -- one card path, never two. The
                // backend type is preserved verbatim (unknown stays unknown).
                const items = (Array.isArray(matches) ? matches : []).map(function (m) {
                    return {
                        id: m.identity_id,
                        display_name: m.display_name,
                        type: m.type,
                        last_seen_at: m.last_seen_at,
                        snapshot_url: snapshotUrlFromPath(m.best_snapshot_path),
                        similarity: (typeof m.similarity === 'number') ? m.similarity : null
                    };
                });
                renderResults(items, false);
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

        // --- listeners (all registered with cleanups) ---
        function on(target, evt, fn) {
            target.addEventListener(evt, fn);
            picker.cleanups.push(function () { target.removeEventListener(evt, fn); });
        }

        on(trigger, 'click', function (e) {
            e.stopPropagation();
            if (panel.style.display === 'none') openPanel(); else closePanel(false);
        });
        on(searchInput, 'input', scheduleSearch);
        on(photoBtn, 'click', function (e) { e.stopPropagation(); photoInput.click(); });
        on(photoClearBtn, 'click', function (e) { e.stopPropagation(); exitPhotoMode(true); });
        on(photoInput, 'change', function () { runPhotoSearch(photoInput.files && photoInput.files[0]); });
        on(typeSelect, 'change', function () { runSearch(true); });
        on(pipelineSelect, 'change', function () { runSearch(true); });
        on(lastSeenSelect, 'change', function () { runSearch(true); });
        on(loadMoreBtn, 'click', function () { picker.page += 1; runSearch(false); });

        on(searchInput, 'keydown', function (e) {
            const options = resultsContainer.querySelectorAll('[role="option"]');
            switch (e.key) {
                case 'ArrowDown': e.preventDefault(); setActive(picker.activeIndex + 1); break;
                case 'ArrowUp': e.preventDefault(); setActive(picker.activeIndex - 1); break;
                case 'Home': if (options.length) { e.preventDefault(); setActive(0); } break;
                case 'End': if (options.length) { e.preventDefault(); setActive(options.length - 1); } break;
                case 'Enter':
                    e.preventDefault();
                    if (picker.activeIndex >= 0 && picker.items[picker.activeIndex]) {
                        chooseIdentity(picker.items[picker.activeIndex]);
                    }
                    break;
                case 'Escape': e.preventDefault(); closePanel(true); break;
            }
        });

        const outsideClick = function (e) {
            if (picker.wrapper && !picker.wrapper.contains(e.target)) closePanel(false);
        };
        document.addEventListener('click', outsideClick);
        picker.cleanups.push(function () { document.removeEventListener('click', outsideClick); });
    }

    // ============================================
    // Identity selection (race-safe)
    // ============================================

    async function selectIdentity(rawId) {
        const identityId = normalizeId(rawId);
        if (!identityId) return;

        state.selectedIdentityId = identityId;
        state.mapRetryUsed = false;
        const req = beginRequest('identity');

        elements.selectedIdentityInfo.style.display = 'block';
        elements.identityName.textContent = 'Loading...';
        elements.identityType.textContent = 'Type: -';
        elements.identityId.textContent = 'ID: -';

        try {
            const identity = await api('/api/admin/identity/' + encodeURIComponent(identityId), { signal: req.signal });
            if (!req.isCurrent() || state.selectedIdentityId !== identityId) return; // stale
            state.selectedIdentity = identity;
            displayIdentityInfo(identity);
            elements.intelligenceTabs.style.display = 'flex';
            const content = document.querySelector('.intelligence-content');
            if (content) content.style.display = 'block';
            switchTab(state.activeTab || 'related');
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            elements.identityName.textContent = err.status === 404 ? 'Identity not found' : 'Failed to load identity';
            elements.identityType.textContent = 'Type: -';
            elements.identityId.textContent = 'ID: ' + shortId(identityId) + '...';
            showNotification(err.status === 404 ? 'Identity not found or not accessible' : 'Failed to load identity', 'error');
        }
    }

    function displayIdentityInfo(identity) {
        const id = normalizeId(identity && identity.id) || state.selectedIdentityId;
        elements.identityName.textContent = identity.display_name ? safeText(identity.display_name) : 'Unknown #' + shortId(id);
        elements.identityType.textContent = 'Type: ' + safeText(identity.type, 'unknown');
        // Full UUID with copy -- a truncated id cannot be related back to
        // anything. buildIdChip copies the FULL id; here the visible text is
        // the full uuid too (the info card has room; CSS wraps it safely).
        elements.identityId.replaceChildren(el('span', { text: 'ID: ' }), (function () {
            const chip = buildIdChip(id);
            chip.querySelector('code').textContent = id;
            return chip;
        })());

        const securityBtn = document.getElementById('analyze-security-btn');
        if (securityBtn) securityBtn.style.display = 'block';

        const snap = elements.identitySnapshot;
        if (snap) {
            snap.src = safeImageUrl(identity.snapshot_url);
            if (!snap.dataset.errorHandlerAttached) {
                snap.addEventListener('error', function () {
                    if (snap.src !== DEFAULT_AVATAR) snap.src = DEFAULT_AVATAR;
                });
                snap.dataset.errorHandlerAttached = 'true';
            }
        }
        elements.selectedIdentityInfo.style.display = 'block';
    }

    function analyzeInSecurityIntelligence() {
        const id = normalizeId(state.selectedIdentityId);
        if (!id) { showNotification('Please select an identity first', 'error'); return; }
        const url = new URL('/admin/security-intelligence', window.location.origin);
        url.searchParams.set('identity_id', id);
        url.searchParams.set('tab', 'threats');
        window.location.href = url.pathname + url.search;
    }

    function viewIdentity(identityId) {
        const id = normalizeId(identityId);
        if (!id) return;
        // The dedicated identity page (used to be /admin/unknown?view=, which
        // framed known people as unknown faces).
        const url = new URL(`/admin/identity/${encodeURIComponent(id)}`, window.location.origin);
        url.searchParams.set('from', window.location.pathname);
        window.location.href = url.pathname + url.search;
    }

    // ============================================
    // Tabs
    // ============================================

    function switchTab(tabName) {
        state.activeTab = tabName;
        document.querySelectorAll('.intel-tab').forEach(function (tab) {
            const active = tab.dataset.tab === tabName;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        document.querySelectorAll('.intel-tab-content').forEach(function (content) {
            content.classList.toggle('active', content.id === 'tab-' + tabName);
        });
        switch (tabName) {
            case 'related': loadRelatedIdentities(); break;
            case 'temporal': loadTemporalPatterns(); break;
            case 'tracking': loadCrossCameraTrack(); break;
            case 'complete': loadCompleteAnalysis(); break;
        }
    }

    // ============================================
    // Related identities
    // ============================================

    async function loadRelatedIdentities() {
        if (!state.selectedIdentityId) return;
        const identityId = state.selectedIdentityId;
        const req = beginRequest('related');
        renderLoading(elements.relatedContainer, 'Loading related identities...');
        try {
            // Only send what the operator actually chose. These used to fall
            // back to literals (3 and 5) and were ALWAYS transmitted, so
            // RELATED_IDENTITY_MIN_CO_APPEARANCES and
            // RELATED_IDENTITY_TIME_WINDOW_MINUTES could never take effect —
            // the server-side defaults were structurally unreachable.
            const params = { limit: 50 };
            const minCoAppRaw = elements.minCoApp && elements.minCoApp.value;
            const timeWindowRaw = elements.timeWindow && elements.timeWindow.value;
            if (minCoAppRaw !== '' && minCoAppRaw != null) {
                const parsed = toNonNegativeInteger(minCoAppRaw, 0);
                if (parsed > 0) params.min_co_appearances = parsed;
            }
            if (timeWindowRaw !== '' && timeWindowRaw != null) {
                const parsed = toNonNegativeInteger(timeWindowRaw, 0);
                if (parsed > 0) params.time_window_minutes = parsed;
            }
            const data = await api('/api/identities/' + encodeURIComponent(identityId) + '/related', {
                signal: req.signal,
                params
            });
            if (!req.isCurrent() || state.selectedIdentityId !== identityId) return;
            // Envelope {items,thresholds} (current) or bare array (legacy)
            const items = Array.isArray(data) ? data : ((data && Array.isArray(data.items)) ? data.items : []);
            renderRelatedIdentities(items);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderError(elements.relatedContainer, 'Failed to load related identities', err.referenceId);
        }
    }

    function renderRelatedIdentities(related) {
        if (!related.length) {
            renderState(elements.relatedContainer, 'fas fa-users', 'No related identities found');
            return;
        }
        const groups = { strong: [], moderate: [], weak: [], other: [] };
        for (const r of related) {
            if (!r || typeof r !== 'object') continue;
            const strength = String(r.relationship_strength || '').toLowerCase();
            (groups[strength] || groups.other).push(r);
        }
        const sections = [];
        const labels = [
            ['strong', '🟢 Strong Relationships'],
            ['moderate', '🟡 Moderate Relationships'],
            ['weak', '🔵 Weak Relationships'],
            ['other', '⚪ Other Relationships']
        ];
        for (const pair of labels) {
            const list = groups[pair[0]];
            if (!list.length) continue;
            sections.push(el('div', { className: 'strength-group' }, [
                el('h3', { text: pair[1] }),
                el('div', { className: 'related-list' }, list.map(renderRelatedItem))
            ]));
        }
        elements.relatedContainer.replaceChildren.apply(elements.relatedContainer, sections);
    }

    function renderRelatedItem(related) {
        const id = normalizeId(related.identity_id);
        const displayName = related.display_name ? safeText(related.display_name) : 'Unknown #' + shortId(id);
        const commonPipelines = Array.isArray(related.common_pipelines) ? related.common_pipelines : [];

        function stat(label, value) {
            return el('div', { className: 'stat-item' }, [
                el('span', { className: 'stat-label', text: label }),
                el('span', { className: 'stat-value', text: value })
            ]);
        }

        const item = el('div', { className: 'related-item', attrs: { role: 'button', tabindex: '0' } }, [
            el('div', { className: 'related-item-header' }, [
                safeImg(related.snapshot_url, displayName, 'related-item-snapshot'),
                el('div', { className: 'related-item-name', text: displayName })
            ]),
            el('div', { className: 'related-item-stats' }, [
                stat('Co-appearances', String(toNonNegativeInteger(related.co_appearance_count, 0))),
                stat('Percentage', fmtPercent(related.co_appearance_percentage)),
                stat('Common Locations', String(commonPipelines.length)),
                stat('Type', safeText(related.identity_type, 'unknown'))
            ])
        ]);
        item.addEventListener('click', function () { viewIdentity(id); });
        item.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); viewIdentity(id); }
        });
        return item;
    }

    // ============================================
    // Temporal patterns
    // ============================================

    function destroyTemporalCharts() {
        for (const key of Object.keys(state.temporalCharts)) {
            try { state.temporalCharts[key].destroy(); } catch (_) { /* noop */ }
        }
        state.temporalCharts = {};
    }

    async function loadTemporalPatterns() {
        if (!state.selectedIdentityId) return;
        const identityId = state.selectedIdentityId;
        const req = beginRequest('temporal');
        renderLoading(elements.temporalContainer, 'Analyzing temporal patterns...');
        try {
            const patterns = await api('/api/identities/' + encodeURIComponent(identityId) + '/temporal-patterns', {
                signal: req.signal,
                params: { days_back: toNonNegativeInteger(elements.temporalDaysBack && elements.temporalDaysBack.value, 90) || 90 }
            });
            if (!req.isCurrent() || state.selectedIdentityId !== identityId) return;
            renderTemporalPatterns(patterns || {});
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderError(elements.temporalContainer, 'Failed to load temporal patterns', err.referenceId);
        }
    }

    function safeCount(value) { return toNonNegativeInteger(value, 0); }

    function distributionValue(dist, key) {
        if (!dist || typeof dist !== 'object') return 0;
        if (dist[key] !== undefined) return safeCount(dist[key]);
        if (dist[String(key)] !== undefined) return safeCount(dist[String(key)]);
        return 0;
    }

    function renderTemporalPatterns(patterns) {
        destroyTemporalCharts();

        if (typeof window.Chart !== 'function') {
            renderError(elements.temporalContainer, 'Chart library unavailable — cannot render temporal charts');
            return;
        }

        // Explicit hour mapping 0-23 — never trust object key order.
        const hourlyValues = Array.from({ length: 24 }, function (_, hour) {
            return distributionValue(patterns.hourly_distribution, hour);
        });
        const dayNames = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
        const dailyValues = dayNames.map(function (day) {
            return distributionValue(patterns.daily_distribution, day.toLowerCase());
        });
        const locationsRaw = Array.isArray(patterns.most_common_pipelines) ? patterns.most_common_pipelines : [];
        const locations = locationsRaw
            .filter(function (l) { return l && normalizeId(l.pipeline_id); })
            .map(function (l) {
                return { name: pipelineDisplayName(l.pipeline_id), count: safeCount(l.count) };
            });

        const total = hourlyValues.reduce(function (a, b) { return a + b; }, 0) +
            dailyValues.reduce(function (a, b) { return a + b; }, 0) + locations.length;
        if (total === 0) {
            renderState(elements.temporalContainer, 'fas fa-clock', 'No appearance data in the selected period');
            return;
        }

        function chartSection(title, canvasId) {
            return el('div', { className: 'temporal-chart-container' }, [
                el('h3', { text: title }),
                el('div', { className: 'chart-wrapper' }, el('canvas', { attrs: { id: canvasId, role: 'img', 'aria-label': title } }))
            ]);
        }

        const locationSection = chartSection('Location Distribution', 'location-chart');
        // Accessible data-table alternative for the location chart
        if (locations.length) {
            const tbody = el('tbody', {}, locations.map(function (l) {
                return el('tr', {}, [el('td', { text: l.name }), el('td', { text: String(l.count) })]);
            }));
            locationSection.append(el('table', { className: 'chart-data-table' }, [
                el('caption', { text: 'Appearances per location' }),
                el('thead', {}, el('tr', {}, [el('th', { text: 'Location' }), el('th', { text: 'Appearances' })])),
                tbody
            ]));
        }

        elements.temporalContainer.replaceChildren(
            chartSection('Hourly Distribution', 'hourly-chart'),
            chartSection('Daily Distribution', 'daily-chart'),
            locationSection
        );

        function makeChart(key, canvasId, config) {
            const canvas = document.getElementById(canvasId);
            const ctx = canvas && canvas.getContext ? canvas.getContext('2d') : null;
            if (!ctx) return;
            try {
                state.temporalCharts[key] = new window.Chart(ctx, config);
            } catch (err) {
                log('[INTEL] chart construction failed:', key);
            }
        }

        makeChart('hourly', 'hourly-chart', {
            type: 'bar',
            data: {
                labels: Array.from({ length: 24 }, function (_, i) { return i + ':00'; }),
                datasets: [{ label: 'Appearances', data: hourlyValues, backgroundColor: CHART_COLORS[0], borderWidth: 1 }]
            },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } }
        });
        makeChart('daily', 'daily-chart', {
            type: 'bar',
            data: {
                labels: dayNames,
                datasets: [{ label: 'Appearances', data: dailyValues, backgroundColor: CHART_COLORS[1], borderWidth: 1 }]
            },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } }
        });
        if (locations.length) {
            makeChart('location', 'location-chart', {
                type: 'doughnut',
                data: {
                    labels: locations.map(function (l) { return l.name; }),
                    datasets: [{
                        data: locations.map(function (l) { return l.count; }),
                        backgroundColor: locations.map(function (_, i) { return CHART_COLORS[i % CHART_COLORS.length]; })
                    }]
                },
                options: { responsive: true, maintainAspectRatio: false }
            });
        } else {
            const wrap = locationSection.querySelector('.chart-wrapper');
            if (wrap) wrap.replaceChildren(el('div', { className: 'empty-state', text: 'No location data' }));
        }
    }

    // ============================================
    // Cross-camera tracking
    // ============================================

    function calculateTransitTime(fromValue, toValue) {
        const from = parseTimestamp(fromValue);
        const to = parseTimestamp(toValue);
        if (!from || !to) return 'Unknown';
        const diff = to.getTime() - from.getTime();
        // Negative (clock skew) or implausibly large gaps are not shown as
        // fake durations.
        if (diff < 0 || diff > MAX_TRANSIT_MS) return 'Unknown';
        const minutes = Math.floor(diff / 60000);
        const seconds = Math.floor((diff % 60000) / 1000);
        return minutes + 'm ' + seconds + 's';
    }

    function validMovement(movement) {
        return movement && typeof movement === 'object' &&
            normalizeId(movement.pipeline_id) !== null &&
            parseTimestamp(movement.timestamp) !== null;
    }

    async function loadCrossCameraTrack() {
        if (!state.selectedIdentityId) {
            renderState(elements.trackingContainer, 'fas fa-exclamation-triangle', 'Please select an identity first');
            return;
        }
        const identityId = state.selectedIdentityId;
        const req = beginRequest('tracking');
        renderLoading(elements.trackingContainer, 'Loading movement tracking...');
        try {
            const date = elements.trackingDate && elements.trackingDate.value;
            const tracks = await api('/api/identities/' + encodeURIComponent(identityId) + '/cross-camera', {
                signal: req.signal,
                params: date ? { date: date } :
                    { days_back: toNonNegativeInteger(elements.trackingDaysBack && elements.trackingDaysBack.value, 7) || 7 }
            });
            if (!req.isCurrent() || state.selectedIdentityId !== identityId) return;
            renderCrossCameraTrack(Array.isArray(tracks) ? tracks : []);
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderError(elements.trackingContainer, 'Failed to load movement tracking', err.referenceId);
        }
    }

    function renderCrossCameraTrack(tracks) {
        if (!tracks.length) {
            renderState(elements.trackingContainer, 'fas fa-map-marked-alt', 'No movement data found');
            return;
        }

        let skipped = 0;
        const dayNodes = tracks.map(function (track) {
            const movements = (Array.isArray(track.movements) ? track.movements : []).filter(function (m) {
                if (validMovement(m)) return true;
                skipped += 1;
                return false;
            });
            const movementNodes = movements.map(function (movement, idx) {
                const contentChildren = [
                    el('div', { className: 'timeline-pipeline', text: pipelineDisplayName(movement.pipeline_id) }),
                    el('div', { className: 'timeline-time', text: fmtDateTime(movement.timestamp) })
                ];
                const duration = toNonNegativeInteger(movement.duration_at_location, null);
                if (duration !== null) {
                    contentChildren.push(el('div', { className: 'timeline-transit', text: 'Duration: ' + duration + ' seconds' }));
                }
                if (idx > 0) {
                    contentChildren.push(el('div', {
                        className: 'timeline-transit',
                        text: '↓ Transit from previous: ' + calculateTransitTime(movements[idx - 1].timestamp, movement.timestamp)
                    }));
                }
                return el('div', { className: 'timeline-item' }, [
                    el('div', { className: 'timeline-sequence', text: String(idx + 1) }),
                    el('div', { className: 'timeline-content' }, contentChildren)
                ]);
            });
            return el('div', { className: 'tracking-day' }, [
                el('h4', { text: safeText(track.date, 'Unknown date') }),
                el('div', { className: 'timeline-view' }, movementNodes)
            ]);
        });

        const timelineView = el('div', { className: 'timeline-view', attrs: { id: 'tracking-timeline-view' } }, dayNodes);
        if (skipped > 0) {
            timelineView.prepend(el('p', { className: 'timeline-warning', text: skipped + ' invalid movement record(s) were skipped' }));
        }
        const mapInner = el('div', { attrs: { id: 'tracking-map' } });
        mapInner.style.cssText = 'width:100%;height:600px;position:relative;border-radius:8px;';
        const mapView = el('div', { className: 'map-view', attrs: { id: 'tracking-map-view' } }, mapInner);
        mapView.style.display = 'none';

        elements.trackingContainer.replaceChildren(timelineView, mapView);
        showTimelineView();
    }

    function showTimelineView() {
        const timelineView = document.getElementById('tracking-timeline-view');
        const mapView = document.getElementById('tracking-map-view');
        const mapSettingsPanel = document.getElementById('map-settings-panel');
        if (timelineView) timelineView.style.display = 'block';
        if (mapView) mapView.style.display = 'none';
        if (mapSettingsPanel) mapSettingsPanel.style.display = 'none';
        document.querySelectorAll('.view-btn').forEach(function (btn) {
            btn.classList.toggle('active', btn.id === 'timeline-btn');
        });
    }

    function showMapView() {
        const mapSettingsPanel = document.getElementById('map-settings-panel');
        if (mapSettingsPanel) mapSettingsPanel.style.display = 'block';
        document.querySelectorAll('.view-btn').forEach(function (btn) {
            btn.classList.toggle('active', btn.id === 'map-btn');
        });
        loadBackendMap();
    }

    // ============================================
    // Backend map (sandboxed)
    // ============================================

    // Expensive/security overlays: missing checkbox === disabled. Only a
    // deliberately checked box turns a feature on.
    function optInChecked(id) {
        const box = document.getElementById(id);
        return box ? box.checked === true : false;
    }
    // Benign display toggles keep their checked state, defaulting off when absent.
    function displayChecked(id) {
        const box = document.getElementById(id);
        return box ? box.checked === true : false;
    }

    function scheduleMapRefresh() {
        if (state.mapDebounceTimer) window.clearTimeout(state.mapDebounceTimer);
        state.mapDebounceTimer = window.setTimeout(function () {
            state.mapDebounceTimer = null;
            loadBackendMap();
        }, MAP_DEBOUNCE_MS);
    }

    async function loadBackendMap() {
        const identityId = state.selectedIdentityId;
        if (!identityId) return;

        const mapView = document.getElementById('tracking-map-view');
        const timelineView = document.getElementById('tracking-timeline-view');
        const mapSettingsPanel = document.getElementById('map-settings-panel');
        if (mapView) mapView.style.display = 'block';
        if (timelineView) timelineView.style.display = 'none';
        if (mapSettingsPanel) mapSettingsPanel.style.display = 'block';
        document.querySelectorAll('.view-btn').forEach(function (btn) {
            btn.classList.toggle('active', btn.id === 'map-btn');
        });

        let mapContainer = document.getElementById('tracking-map');
        if (!mapContainer) {
            // Bounded recovery: load tracking data ONCE, re-check once. Never
            // an unbounded retry loop.
            if (state.mapRetryUsed) {
                showNotification('Map view unavailable — load tracking data first', 'error');
                return;
            }
            state.mapRetryUsed = true;
            await loadCrossCameraTrack();
            mapContainer = document.getElementById('tracking-map');
            if (!mapContainer) return;
            const mv = document.getElementById('tracking-map-view');
            const tv = document.getElementById('tracking-timeline-view');
            if (mv) mv.style.display = 'block';
            if (tv) tv.style.display = 'none';
        }

        const req = beginRequest('map');
        const refreshBtn = document.getElementById('refresh-map-btn');
        if (refreshBtn) refreshBtn.disabled = true;

        try {
            // Server-side flags (security overlays default OFF; a missing
            // checkbox never enables them) and client-side display flags.
            const params = {};
            const date = elements.trackingDate && elements.trackingDate.value;
            if (date) params.date = date;
            else params.days_back = toNonNegativeInteger(elements.trackingDaysBack && elements.trackingDaysBack.value, 7) || 7;
            params.show_routes = displayChecked('map-show-routes');
            params.enable_security_features = optInChecked('map-enable-security');
            params.detect_patterns = optInChecked('map-detect-patterns');
            params.show_risk_heatmap = optInChecked('map-show-heatmap');

            const flags = {
                popups: displayChecked('map-include-popups'),
                cluster: displayChecked('map-cluster-markers'),
                routes: params.show_routes,
                security: params.enable_security_features,
                patterns: params.detect_patterns,
                risk: params.show_risk_heatmap,
                timeline: optInChecked('map-show-timeline'),
                avatar: optInChecked('map-show-animated-avatar')
            };

            const styleSelect = document.getElementById('map-style-select');
            const style = styleSelect && MAP_STYLES.indexOf(styleSelect.value) >= 0 ? styleSelect.value : 'light';

            // MapLibre renders INTO the page (no iframe, no backend HTML). The
            // controller is an ES module bridged onto window.IdentityMap.
            const IM = await window.IdentityMap.ready;

            // A style change alone must not refetch analysis: only the data
            // key (identity + server params) triggers a new request.
            const dataKey = identityId + '|' + JSON.stringify(params);
            let ctl = state.mapController;
            if (!ctl || ctl.container !== mapContainer || !ctl.map) {
                if (ctl) ctl.destroy();
                renderLoading(mapContainer, 'Loading map...');
                ctl = new IM.Controller(mapContainer, {
                    style: 'light',
                    onError: function (kind, detail) {
                        if (kind === 'dataset') {
                            showNotification('That map style is not installed on this system (' + detail.code + ')', 'error');
                        }
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
                // Deterministic: refuse and tell the user; never substitute.
                if (styleSelect) styleSelect.value = ctl.style;
                showNotification('Map style "' + style + '" is unavailable: ' + IM.UNAVAILABLE, 'error');
            }
            if (!req.isCurrent()) return;

            if (state.mapDataKey !== dataKey) {
                await ctl.load(identityId, params, flags);
                state.mapDataKey = dataKey;
            } else {
                Object.assign(ctl.flags, flags);
                ctl._restoreOverlays();
            }
            if (req.isCurrent() && refreshBtn) refreshBtn.disabled = false;
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            if (err && err.message === (window.IdentityMap && window.IdentityMap.UNAVAILABLE)) {
                if (refreshBtn) refreshBtn.disabled = false;
                return;
            }
            renderError(mapContainer,
                err.status === 503 ? 'Map service is temporarily unavailable' : 'Failed to load map',
                err.referenceId);
            if (refreshBtn) refreshBtn.disabled = false;
        }
    }

    // ============================================
    // Complete analysis
    // ============================================

    async function loadCompleteAnalysis() {
        if (!state.selectedIdentityId) return;
        const identityId = state.selectedIdentityId;
        const req = beginRequest('complete');
        renderLoading(elements.completeContainer, 'Loading complete analysis...');
        try {
            const analysis = await api('/api/identities/' + encodeURIComponent(identityId) + '/analyze', {
                signal: req.signal, timeout: MAP_TIMEOUT_MS
            });
            if (!req.isCurrent() || state.selectedIdentityId !== identityId) return;
            renderCompleteAnalysis(analysis || {});
        } catch (err) {
            if (err.aborted || !req.isCurrent()) return;
            renderError(elements.completeContainer, 'Failed to load complete analysis', err.referenceId);
        }
    }

    function renderCompleteAnalysis(analysis) {
        const sections = (analysis && typeof analysis.sections === 'object' && analysis.sections) || {};

        function sectionCard(title, lines, tabName) {
            const btn = el('button', { className: 'btn-primary', text: 'View Details', attrs: { type: 'button' } });
            btn.addEventListener('click', function () { switchTab(tabName); });
            return el('div', { className: 'analysis-section' },
                [el('h3', { text: title })].concat(lines.map(function (line) { return el('p', { text: line }); }), [btn]));
        }

        const relatedSection = sections.related || {};
        const relatedLines = relatedSection.status === 'error'
            ? ['Analysis failed for this section']
            : ['Found ' + toNonNegativeInteger(relatedSection.count,
                Array.isArray(analysis.related_identities) ? analysis.related_identities.length : 0) + ' related identities'];

        const temporalSection = sections.temporal || {};
        let temporalLines;
        if (temporalSection.status === 'error') {
            temporalLines = ['Analysis failed for this section'];
        } else {
            const tp = analysis.temporal_patterns || {};
            const totalAppearances = toNonNegativeInteger(
                temporalSection.total_appearances !== undefined ? temporalSection.total_appearances : tp.total_appearances, 0);
            const peakHoursRaw = Array.isArray(temporalSection.peak_hours) ? temporalSection.peak_hours
                : (Array.isArray(tp.peak_hours) ? tp.peak_hours : []);
            const peakHours = peakHoursRaw.filter(function (h) { return Number.isInteger(Number(h)); })
                .map(function (h) { return h + ':00'; });
            temporalLines = [
                'Total appearances: ' + totalAppearances,
                'Peak hours: ' + (peakHours.length ? peakHours.join(', ') : 'N/A')
            ];
        }

        const trackingSection = sections.tracking || {};
        let trackingLines;
        switch (trackingSection.status) {
            case 'ready':
                trackingLines = [toNonNegativeInteger(trackingSection.movement_count, 0) + ' movements across ' +
                    toNonNegativeInteger(trackingSection.days_with_activity, 0) + ' day(s)'];
                break;
            case 'partial':
                trackingLines = [toNonNegativeInteger(trackingSection.movement_count, 0) + ' movements found',
                    'Camera coordinates missing — map view unavailable'];
                break;
            case 'unavailable':
                trackingLines = ['No movement data in the analysis window'];
                break;
            case 'error':
                trackingLines = ['Analysis failed for this section'];
                break;
            default:
                trackingLines = [Array.isArray(analysis.cross_camera_tracks) && analysis.cross_camera_tracks.length
                    ? 'Tracking data available' : 'No tracking data available'];
        }

        elements.completeContainer.replaceChildren(
            el('div', { className: 'complete-analysis-grid' }, [
                sectionCard('Related Identities', relatedLines, 'related'),
                sectionCard('Temporal Patterns', temporalLines, 'temporal'),
                sectionCard('Cross-Camera Tracking', trackingLines, 'tracking')
            ])
        );
    }

    // ============================================
    // Calculate-all relationships (background job)
    // ============================================

    let calcAllInFlight = false;

    async function calculateAllRelationships() {
        if (calcAllInFlight) return;
        if (!window.confirm('Calculate relationships for ALL identities?\n\nThis runs as a background job (typically 5-30 minutes). Progress appears in the Background Tasks page. Continue?')) {
            return;
        }
        calcAllInFlight = true;
        const btn = document.getElementById('calc-all-relationships-btn');
        if (btn) btn.disabled = true;
        try {
            const result = await api('/api/intelligence/relationships/calculate-all', { method: 'POST' });
            const jobId = result && result.job_id ? safeText(result.job_id) : null;
            showNotification('Relationship calculation scheduled' + (jobId ? ' (job ' + jobId + ')' : '') +
                ' — monitor it in Background Tasks', 'success');
        } catch (err) {
            if (err.status === 409) {
                showNotification('A relationship calculation is already running' +
                    (err.referenceId ? ' (job ' + err.referenceId + ')' : ''), 'info');
            } else if (err.status === 403) {
                showNotification('Not authorized to start relationship calculation', 'error');
            } else if (!err.aborted) {
                showNotification('Failed to schedule relationship calculation', 'error');
            }
        } finally {
            calcAllInFlight = false;
            if (btn) btn.disabled = false;
        }
    }

    // ============================================
    // Notifications (textContent only)
    // ============================================

    function showNotification(message, type) {
        type = type || 'info';
        const notification = el('div', { className: 'notification notification-' + typeClassSafe(type) });
        notification.style.cssText =
            'position:fixed;top:20px;right:20px;padding:1rem 1.5rem;border-radius:8px;z-index:10000;font-weight:600;' +
            'box-shadow:0 4px 20px rgba(0,0,0,0.3);color:' + (type === 'success' ? '#000' : '#fff') + ';background:' +
            (type === 'error' ? 'rgba(255,0,0,0.9)' : type === 'success' ? 'rgba(0,255,150,0.9)' : 'rgba(0,150,255,0.9)') + ';';
        notification.textContent = message;
        notification.setAttribute('role', type === 'error' ? 'alert' : 'status');
        document.body.appendChild(notification);
        window.setTimeout(function () {
            notification.style.opacity = '0';
            notification.style.transition = 'opacity 0.3s';
            window.setTimeout(function () { notification.remove(); }, 300);
        }, 4000);
    }

    function typeClassSafe(type) {
        return ['info', 'success', 'error'].indexOf(type) >= 0 ? type : 'info';
    }

    // ============================================
    // Wiring (idempotent — no inline handlers anywhere)
    // ============================================

    function attachOnce(id, evt, fn) {
        const node = document.getElementById(id);
        if (node && !node.dataset.listenerAttached) {
            node.addEventListener(evt, fn);
            node.dataset.listenerAttached = 'true';
        }
    }

    function setupEventListeners() {
        // Fallback: if the picker failed to init, the raw select still works.
        if (elements.identitySelect && !elements.identitySelect.dataset.listenerAttached) {
            elements.identitySelect.addEventListener('change', function (e) {
                if (picker.wrapper) return; // picker owns selection when present
                if (e.target.value) selectIdentity(e.target.value);
            });
            elements.identitySelect.dataset.listenerAttached = 'true';
        }

        document.querySelectorAll('.intel-tab').forEach(function (tab) {
            if (tab.dataset.listenerAttached) return;
            tab.addEventListener('click', function () { switchTab(tab.dataset.tab); });
            tab.dataset.listenerAttached = 'true';
        });

        attachOnce('analyze-security-btn', 'click', analyzeInSecurityIntelligence);
        attachOnce('refresh-related-btn', 'click', loadRelatedIdentities);
        attachOnce('calc-all-relationships-btn', 'click', calculateAllRelationships);
        attachOnce('refresh-temporal-btn', 'click', loadTemporalPatterns);
        attachOnce('refresh-tracking-btn', 'click', loadCrossCameraTrack);
        attachOnce('refresh-complete-btn', 'click', loadCompleteAnalysis);
        attachOnce('timeline-btn', 'click', showTimelineView);
        attachOnce('map-btn', 'click', showMapView);
        attachOnce('refresh-map-btn', 'click', function (e) {
            e.preventDefault();
            loadBackendMap();
        });

        const mapStyleSelect = document.getElementById('map-style-select');
        if (mapStyleSelect && !mapStyleSelect.dataset.listenerAttached) {
            mapStyleSelect.addEventListener('change', scheduleMapRefresh);
            mapStyleSelect.dataset.listenerAttached = 'true';
        }
        ['map-show-routes', 'map-cluster-markers', 'map-include-popups', 'map-enable-security',
            'map-detect-patterns', 'map-show-heatmap', 'map-show-timeline', 'map-show-animated-avatar'
        ].forEach(function (id) {
            const box = document.getElementById(id);
            if (box && !box.dataset.listenerAttached) {
                box.addEventListener('change', scheduleMapRefresh);
                box.dataset.listenerAttached = 'true';
            }
        });
    }

    function destroy() {
        abortAllRequests();
        if (state.mapController) { state.mapController.destroy(); state.mapController = null; state.mapDataKey = null; }
        destroyTemporalCharts();
        destroyIdentityPicker();
        if (state.mapDebounceTimer) { window.clearTimeout(state.mapDebounceTimer); state.mapDebounceTimer = null; }
    }

    document.addEventListener('DOMContentLoaded', async function () {
        cacheElements();
        setupEventListeners();
        await loadPipelines();
        createIdentityPicker(elements.identitySelect);
    });

    window.addEventListener('pagehide', destroy);
})();
