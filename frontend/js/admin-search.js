/**
 * Advanced Search Intelligence Frontend
 * =====================================
 * Handles multi-face search, watchlist checking, batch operations, and exports.
 */

(function() {
    'use strict';

    // ============================================
    // State Management
    // ============================================
    const state = {
        selectedFile: null,
        batchFiles: [],
        isBatchMode: false,
        isSearching: false,
        currentResults: null,
        activeTab: 'matches',
        pipelines: [],
        // Upload limits. These are only the fallbacks used before
        // GET /api/search/config answers (and if it never does) — the live
        // values always win. Hardcoding them was the bug: the page staged 100
        // images against an enforced limit of 20 and the whole batch 400'd
        // after the user had already uploaded everything.
        limits: {
            batchMaxImages: 20,
            maxFileSizeBytes: 10 * 1024 * 1024,
            allowedExtensions: ['.jpg', '.jpeg', '.png', '.webp']
        },
        // Confidence bands as the SERVER defines them. Empty until
        // /api/search/config answers; getSimilarityClass prefers the band the
        // API attaches to each match anyway, so this is only a fallback.
        confidenceBands: {},
        // Quality thresholds and result depth, also server-owned. Null means
        // "not loaded yet" — never a second copy of the configured number.
        // The quality warning here used to fire at a hard-coded 0.5 while the
        // server warned at SEARCH_QUALITY_WARNING_THRESHOLD (0.6), so the page
        // and the API disagreed about which faces were poor quality.
        quality: { minThreshold: null, warningThreshold: null },
        results: { defaultTopK: null, maxTopK: null }
    };

    // ============================================
    // DOM Elements
    // ============================================
    const elements = {
        uploadArea: document.getElementById('upload-area'),
        imageInput: document.getElementById('image-input'),
        imagePreview: document.getElementById('image-preview'),
        previewImg: document.getElementById('preview-img'),
        removeImage: document.getElementById('remove-image'),
        batchModeToggle: document.getElementById('batch-mode-toggle'),
        batchInput: document.getElementById('batch-input'),
        batchFilesContainer: document.getElementById('batch-files-container'),
        batchFilesList: document.getElementById('batch-files-list'),
        searchScope: document.getElementById('search-scope'),
        topK: document.getElementById('top-k'),
        checkWatchlist: document.getElementById('check-watchlist'),
        dateFrom: document.getElementById('date-from'),
        dateTo: document.getElementById('date-to'),
        pipelineFilter: document.getElementById('pipeline-filter'),
        searchBtn: document.getElementById('search-btn'),
        qualityCheckBtn: document.getElementById('quality-check-btn'),
        clearResultsBtn: document.getElementById('clear-results-btn'),
        resultsPanel: document.getElementById('results-panel'),
        resultsTabs: document.getElementById('results-tabs'),
        resultsContent: document.getElementById('results-content'),
        emptyState: document.getElementById('empty-state'),
        matchesTab: document.getElementById('matches-tab'),
        alertsTab: document.getElementById('alerts-tab'),
        summaryTab: document.getElementById('summary-tab'),
        matchesCount: document.getElementById('matches-count'),
        alertsCount: document.getElementById('alerts-count'),
        searchingOverlay: document.getElementById('searching-overlay'),
        exportBtn: document.getElementById('export-btn'),
        exportModal: document.getElementById('export-modal'),
        closeExportModal: document.getElementById('close-export-modal'),
        cancelExportBtn: document.getElementById('cancel-export-btn'),
        confirmExportBtn: document.getElementById('confirm-export-btn'),
        exportFormat: document.getElementById('export-format'),
        exportIncludeImages: document.getElementById('export-include-images'),
        excludeIdentities: document.getElementById('exclude-identities'),
        excludeWatchlists: document.getElementById('exclude-watchlists'),
        excludeIdentitiesSearch: document.getElementById('exclude-identities-search'),
        excludeWatchlistsSearch: document.getElementById('exclude-watchlists-search'),
        excludeIdentitiesDropdown: document.getElementById('exclude-identities-dropdown'),
        excludeWatchlistsDropdown: document.getElementById('exclude-watchlists-dropdown'),
        excludeIdentitiesList: document.getElementById('exclude-identities-list'),
        excludeWatchlistsList: document.getElementById('exclude-watchlists-list'),
        excludeIdentitiesChips: document.getElementById('exclude-identities-chips'),
        excludeWatchlistsChips: document.getElementById('exclude-watchlists-chips'),
        excludeIdentitiesCount: document.getElementById('exclude-identities-count'),
        excludeWatchlistsCount: document.getElementById('exclude-watchlists-count'),
        batchProgressModal: document.getElementById('batch-progress-modal'),
        batchOverallProgress: document.getElementById('batch-overall-progress'),
        batchProgressText: document.getElementById('batch-progress-text'),
        batchImagesList: document.getElementById('batch-images-list'),
        closeBatchProgressBtn: document.getElementById('close-batch-progress-btn')
    };

    // ============================================
    // Utility Functions
    // ============================================
    // Removed getAuthHeaders - backend authenticates via cookies

    function showNotification(message, type = 'info') {
        // Create notification element
        const notification = document.createElement('div');
        notification.className = `notification notification-${type}`;
        notification.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 1rem 1.5rem;
            background: ${type === 'error' ? 'rgba(255, 0, 0, 0.9)' : 
                         type === 'success' ? 'rgba(0, 255, 150, 0.9)' : 
                         'rgba(0, 150, 255, 0.9)'};
            color: ${type === 'success' ? '#000' : '#fff'};
            border-radius: 8px;
            z-index: 10000;
            font-weight: 600;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
            animation: slideIn 0.3s ease;
        `;
        notification.textContent = message;
        document.body.appendChild(notification);

        setTimeout(() => {
            notification.style.animation = 'slideOut 0.3s ease';
            setTimeout(() => notification.remove(), 300);
        }, 3000);
    }

    // ============================================
    // Safe rendering helpers
    //
    // This file had NO escaping helper at all: API strings (identity
    // display_name, watchlist name/notes/action_instructions, error
    // messages) and the user's own filenames were interpolated straight into
    // innerHTML. The only prior defence was `.replace(/'/g, "\\'")` — a
    // JavaScript string escape emitted into an HTML attribute, which does not
    // escape `"` at all and corrupts legitimate names like O'Brien.
    //
    // Prefer buildEl/setText (real DOM, nothing parsed as markup).
    // escapeHtml exists for the few remaining template-literal renderers.
    // ============================================

    /** Escape all five HTML-significant characters. Text AND attribute safe. */
    function escapeHtml(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Create an element. `text` is always applied via textContent, so a value
     * from the API can never be parsed as markup.
     */
    function buildEl(tag, className, text) {
        const el = document.createElement(tag);
        if (className) el.className = className;
        if (text !== undefined && text !== null) el.textContent = String(text);
        return el;
    }

    /** Standard empty/informational panel, DOM-built. */
    function buildEmptyState(iconClass, title, message) {
        const wrap = buildEl('div', 'empty-state');
        const icon = buildEl('i', iconClass);
        icon.setAttribute('aria-hidden', 'true');
        wrap.appendChild(icon);
        wrap.appendChild(buildEl('h3', null, title));
        if (message) wrap.appendChild(buildEl('p', null, message));
        return wrap;
    }

    /**
     * Only same-origin/relative image paths may reach an `img src`.
     * `snapshot_url` arrives from the API unvalidated; a value containing a
     * quote could otherwise break out of the attribute, and a
     * protocol-relative `//host/x` would silently load cross-origin.
     */
    function safeImageUrl(url) {
        if (typeof url !== 'string' || !url) return '';
        const trimmed = url.trim();
        if (trimmed.startsWith('//')) return '';        // protocol-relative
        if (!trimmed.startsWith('/')) return '';        // absolute or scheme-bearing
        if (trimmed.includes('..')) return '';          // path traversal
        return trimmed;
    }

    // Formats `export_service.export_batch_results` implements. Single-result
    // export additionally supports 'pdf'.
    const BATCH_EXPORT_FORMATS = ['csv', 'json'];

    // Inline placeholder shown when an identity has no usable snapshot, and as
    // the data-fallback-src actions.js swaps in when a snapshot 404s.
    //
    // It contains raw double quotes (xmlns="..."), so it MUST be escaped at
    // every attribute interpolation — unescaped, the attribute value ended at
    // the first quote and the fallback silently rendered a broken image.
    const PLACEHOLDER_AVATAR =
        'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="100" height="100"%3E' +
        '%3Crect fill="%23333" width="100" height="100"/%3E' +
        '%3Ccircle cx="50" cy="35" r="15" fill="%23999"/%3E' +
        '%3Cpath d="M 25 70 Q 25 60 35 60 L 65 60 Q 75 60 75 70 L 75 85 L 25 85 Z" fill="%23999"/%3E' +
        '%3C/svg%3E';

    // The watchlist serializer emits alert_level (info | warning | critical)
    // and has no `type` field at all, so every branch on `watchlist.type` was
    // dead: rows rendered the generic icon and the label "custom".
    // Also the allow-list that keeps a server string out of a class name.
    const ALERT_LEVEL_ICONS = {
        critical: 'fa-skull-crossbones',
        warning: 'fa-exclamation-triangle',
        info: 'fa-circle-info'
    };

    // ============================================
    // Request lifecycle
    //
    // Every mutating call carries X-Requested-With. The auth cookie is
    // SameSite=lax, but AUTH_COOKIE_SAMESITE is env-configurable and the three
    // multipart endpoints here are otherwise reachable from a cross-site form.
    // A cross-site page cannot attach a custom header without a CORS preflight,
    // so the header is the check the backend dependency looks for.
    //
    // Each long-running operation also owns an AbortController. Without one,
    // a quality check issued during a search landed whenever it landed and
    // displayQualityResult hid the results tabs — blanking search results the
    // user was reading.
    // ============================================
    const CSRF_HEADERS = { 'X-Requested-With': 'XMLHttpRequest' };

    const inflight = { search: null, quality: null, export: null, identity: null };

    /**
     * Abort whatever `key` was doing and hand back a signal for the new
     * attempt. Returns null if the caller should not proceed.
     */
    function beginRequest(key) {
        if (inflight[key]) {
            inflight[key].abort();
        }
        const controller = new AbortController();
        inflight[key] = controller;
        return controller;
    }

    /** Clear only if this controller is still the current one. */
    function endRequest(key, controller) {
        if (inflight[key] === controller) {
            inflight[key] = null;
        }
    }

    function isAbort(error) {
        return error && (error.name === 'AbortError' || error.code === 20);
    }

    // ============================================
    // Error surfacing
    // ============================================

    /**
     * Turn a failed Response into the most useful sentence available.
     *
     * `error.detail || 'Search failed'` discarded exactly the fields an admin
     * needs and threw its own error on top of the server's:
     *   - 422 sends `detail` as a LIST of objects -> "[object Object]"
     *   - the 500 handler (main.py) sends {error, request_id} with NO detail,
     *     so the request_id — the only way to find the log line — was dropped
     *   - nginx 413/502 bodies are HTML, so `response.json()` rejected with a
     *     SyntaxError that replaced the real failure
     * This never throws; the caller always gets a string.
     */
    async function describeHttpError(response, fallback) {
        let body = null;
        try {
            body = await response.json();
        } catch (_) {
            // Non-JSON (proxy error page, empty body, truncated stream).
            return `${fallback} (HTTP ${response.status} ${response.statusText || ''})`.trim();
        }

        const parts = [];
        const detail = body?.detail;

        if (typeof detail === 'string' && detail) {
            parts.push(detail);
        } else if (Array.isArray(detail)) {
            // FastAPI validation errors: {loc: [...], msg, type}
            const messages = detail
                .map(d => {
                    if (typeof d === 'string') return d;
                    const field = Array.isArray(d?.loc) ? d.loc.filter(p => p !== 'body').join('.') : '';
                    const msg = d?.msg || d?.message || '';
                    return field && msg ? `${field}: ${msg}` : (msg || field);
                })
                .filter(Boolean);
            if (messages.length) parts.push(messages.join('; '));
        } else if (detail && typeof detail === 'object') {
            const msg = detail.message || detail.error;
            if (msg) parts.push(String(msg));
        }

        if (parts.length === 0) {
            const msg = body?.message || body?.error;
            parts.push(msg ? String(msg) : `${fallback} (HTTP ${response.status})`);
        }
        if (body?.request_id) {
            parts.push(`[request ${body.request_id}]`);
        }
        return parts.join(' ');
    }

    /** Throw an Error carrying the server's real reason. */
    async function throwHttpError(response, fallback) {
        throw new Error(await describeHttpError(response, fallback));
    }

    // ============================================
    // Upload limits (authoritative values come from the server)
    // ============================================

    /**
     * Pull the enforced limits from GET /api/search/config. Failure is not
     * fatal: `state.limits` keeps its conservative defaults, which are at or
     * below every shipped server value, so we under-promise rather than let
     * the user stage work the server will reject.
     */
    async function loadSearchConfig() {
        try {
            const response = await fetch('/api/search/config', { credentials: 'same-origin' });
            if (!response.ok) return;
            const config = await response.json();

            const maxImages = Number(config?.batch?.max_images);
            if (Number.isFinite(maxImages) && maxImages > 0) {
                state.limits.batchMaxImages = Math.floor(maxImages);
            }
            const maxBytes = Number(config?.upload?.max_file_size_bytes);
            if (Number.isFinite(maxBytes) && maxBytes > 0) {
                state.limits.maxFileSizeBytes = Math.floor(maxBytes);
            }
            const bands = config?.confidence_bands;
            if (bands && typeof bands === 'object') {
                state.confidenceBands = bands;
            }
            const extensions = config?.upload?.allowed_extensions;
            if (Array.isArray(extensions) && extensions.length > 0) {
                state.limits.allowedExtensions = extensions.map(e => String(e).toLowerCase());
            }
            const warnAt = Number(config?.quality?.warning_threshold);
            if (Number.isFinite(warnAt)) state.quality.warningThreshold = warnAt;
            const minQuality = Number(config?.quality?.min_threshold);
            if (Number.isFinite(minQuality)) state.quality.minThreshold = minQuality;

            // Result depth: one control on this page drives both the single
            // and batch searches, which used to default to 10 and 5
            // respectively, so a batch silently ran at half the requested
            // depth. Both now come from here.
            const defaultTopK = Number(config?.results?.default_top_k);
            const maxTopK = Number(config?.results?.max_top_k);
            if (Number.isFinite(defaultTopK) && defaultTopK > 0) {
                state.results.defaultTopK = Math.floor(defaultTopK);
            }
            if (Number.isFinite(maxTopK) && maxTopK > 0) {
                state.results.maxTopK = Math.floor(maxTopK);
            }
            applyResultDepthConfig();
        } catch (error) {
            // Offline / blocked: keep the defaults, do not disable the page.
            console.warn('Search config unavailable, using default upload limits:', error);
        }
    }

    /**
     * Point the result-depth control at the server's configured default and
     * ceiling. The <select> ships with no `selected` option, so before this
     * runs the browser picks the first — never a second copy of the default
     * baked into the markup. Options above SEARCH_MAX_TOP_K are removed rather
     * than left to be rejected with a 422 after the user has uploaded.
     */
    function applyResultDepthConfig() {
        const control = elements.topK;
        if (!control) return;
        const max = state.results.maxTopK;
        if (max) {
            Array.from(control.options).forEach(opt => {
                if (Number(opt.value) > max) opt.remove();
            });
        }
        const wanted = state.results.defaultTopK;
        if (!wanted || control.dataset.userEdited) return;
        const exact = Array.from(control.options).find(o => Number(o.value) === wanted);
        if (exact) {
            control.value = exact.value;
        } else {
            const opt = document.createElement('option');
            opt.value = String(wanted);
            opt.textContent = `${wanted} matches`;
            control.appendChild(opt);
            control.value = String(wanted);
        }
        if (!control.dataset.editListenerBound) {
            control.dataset.editListenerBound = '1';
            control.addEventListener('change', () => { control.dataset.userEdited = '1'; });
        }
    }

    function formatBytes(bytes) {
        if (!Number.isFinite(bytes) || bytes <= 0) return '0 MB';
        const mb = bytes / (1024 * 1024);
        return mb >= 1 ? `${mb.toFixed(mb >= 10 ? 0 : 1)} MB` : `${Math.round(bytes / 1024)} KB`;
    }

    /**
     * Single validator for all four entry paths (drag-drop, single picker,
     * batch picker, and the drop handler's batch branch). Previously only
     * drag-drop checked anything, and nothing anywhere checked size — so a
     * multi-GB file went straight into FileReader.readAsDataURL and killed the
     * tab.
     *
     * Returns null when acceptable, otherwise a human-readable reason.
     * Extension is checked as well as MIME type: `File.type` comes from the
     * OS and is trivially wrong or empty, and the server rejects on extension.
     */
    function validateImageFile(file) {
        if (!file) return 'No file selected';

        const name = String(file.name || '');
        const dot = name.lastIndexOf('.');
        const extension = dot >= 0 ? name.slice(dot).toLowerCase() : '';
        const allowed = state.limits.allowedExtensions;

        if (!extension || !allowed.includes(extension)) {
            return `"${name}" is not a supported image (allowed: ${allowed.join(', ')})`;
        }
        if (file.type && !file.type.startsWith('image/')) {
            return `"${name}" is not an image file`;
        }
        if (file.size > state.limits.maxFileSizeBytes) {
            return `"${name}" is ${formatBytes(file.size)} — the limit is ${formatBytes(state.limits.maxFileSizeBytes)}`;
        }
        if (file.size === 0) {
            return `"${name}" is empty`;
        }
        return null;
    }

    /** Partition a list, reporting each distinct rejection reason once. */
    function filterValidImages(files) {
        const accepted = [];
        const reasons = [];
        for (const file of files) {
            const reason = validateImageFile(file);
            if (reason) {
                if (!reasons.includes(reason)) reasons.push(reason);
            } else {
                accepted.push(file);
            }
        }
        if (reasons.length > 0) {
            const shown = reasons.slice(0, 3).join('; ');
            const more = reasons.length > 3 ? ` (+${reasons.length - 3} more)` : '';
            showNotification(`${shown}${more}`, 'error');
        }
        return accepted;
    }

    // ============================================
    // Image Upload Handling
    // ============================================
    function setupUploadHandlers() {
        // Click to upload
        elements.uploadArea.addEventListener('click', () => {
            if (state.isBatchMode) {
                elements.batchInput.click();
            } else {
                elements.imageInput.click();
            }
        });

        // Drag and drop
        elements.uploadArea.addEventListener('dragover', (e) => {
            e.preventDefault();
            elements.uploadArea.classList.add('dragover');
        });

        elements.uploadArea.addEventListener('dragleave', () => {
            elements.uploadArea.classList.remove('dragover');
        });

        elements.uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            elements.uploadArea.classList.remove('dragover');

            const dropped = Array.from(e.dataTransfer.files);
            if (dropped.length === 0) return;

            if (state.isBatchMode) {
                addBatchFiles(dropped);
            } else {
                handleFileSelect(dropped[0]);
            }
        });

        // Single file input
        elements.imageInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                handleFileSelect(e.target.files[0]);
            }
        });

        // Batch file input
        elements.batchInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                addBatchFiles(Array.from(e.target.files));
            }
        });

        // Remove image
        elements.removeImage.addEventListener('click', () => {
            clearSelectedFile();
        });

        // Batch mode toggle
        elements.batchModeToggle.addEventListener('change', (e) => {
            state.isBatchMode = e.target.checked;
            elements.batchFilesContainer.style.display = state.isBatchMode ? 'block' : 'none';

            if (state.isBatchMode) {
                elements.uploadArea.querySelector('p').textContent = 'Drag & drop multiple images';
                // The single-image preview is hidden by the mode switch, so its
                // Remove button becomes unreachable. Clearing the file too is
                // what keeps the visible state honest: leaving it set meant
                // Search stayed enabled for an image the user could no longer
                // see, and toggling back showed no preview but still searched it.
                clearSelectedFile();
            } else {
                elements.uploadArea.querySelector('p').textContent = 'Drag & drop image here';
                clearBatchFiles();
            }
            applyBatchModeCapabilities();
            updateSearchButton();
        });
    }

    function handleFileSelect(file) {
        const reason = validateImageFile(file);
        if (reason) {
            showNotification(reason, 'error');
            // Reset the picker so re-choosing a corrected file still fires
            // `change` (the input keeps its value on a rejected selection).
            elements.imageInput.value = '';
            return;
        }

        state.selectedFile = file;

        // Show preview
        const reader = new FileReader();
        reader.onload = (e) => {
            elements.previewImg.src = e.target.result;
            elements.imagePreview.style.display = 'block';
        };
        reader.readAsDataURL(file);

        updateSearchButton();
    }

    function addBatchFiles(files) {
        const accepted = filterValidImages(files);

        // Cap comes from the server (GET /api/search/config), never a literal:
        // the page used to stage 100 against an enforced 20 and lose the whole
        // batch to a 400.
        const max = state.limits.batchMaxImages;
        const remaining = Math.max(0, max - state.batchFiles.length);
        const toAdd = accepted.slice(0, remaining);

        if (toAdd.length > 0) {
            state.batchFiles.push(...toAdd);
            renderBatchFiles();
            updateSearchButton();
        }

        if (accepted.length > remaining) {
            showNotification(
                remaining === 0
                    ? `Batch is full — the server accepts at most ${max} images per search`
                    : `Only ${remaining} more file${remaining === 1 ? '' : 's'} could be added (max ${max})`,
                'info');
        }

        // Always clear the picker: without this, re-selecting the same files
        // fires no `change` event and the click looks like it did nothing.
        elements.batchInput.value = '';
    }

    function clearBatchFiles() {
        state.batchFiles = [];
        elements.batchInput.value = '';
        renderBatchFiles();
    }

    function renderBatchFiles() {
        // DOM-built: `file.name` is attacker-controllable (a downloaded or
        // shared file), it was interpolated into innerHTML unescaped, and it
        // round-trips back from the server as image_name.
        const items = state.batchFiles.map((file, index) => {
            const row = buildEl('div', 'batch-file-item');
            const icon = buildEl('i', 'fas fa-image');
            icon.setAttribute('aria-hidden', 'true');
            row.appendChild(icon);
            row.appendChild(buildEl('span', 'file-name', file.name));

            // A real <button>, not a span with role="button": buttons are
            // keyboard-activatable and honour `disabled` without extra code.
            const remove = buildEl('button', 'remove-file');
            remove.type = 'button';
            remove.dataset.action = 'removeBatchFile';
            remove.dataset.arg = String(index);
            remove.setAttribute('aria-label', `Remove ${file.name}`);
            const removeIcon = buildEl('i', 'fas fa-times');
            removeIcon.setAttribute('aria-hidden', 'true');
            remove.appendChild(removeIcon);
            row.appendChild(remove);
            return row;
        });
        elements.batchFilesList.replaceChildren(...items);
    }

    window.removeBatchFile = function(index) {
        state.batchFiles.splice(index, 1);
        renderBatchFiles();
        updateSearchButton();
    };

    function clearSelectedFile() {
        state.selectedFile = null;
        elements.imageInput.value = '';
        elements.imagePreview.style.display = 'none';
        elements.previewImg.src = '';
        updateSearchButton();
    }

    function updateSearchButton() {
        const hasFile = state.isBatchMode ? state.batchFiles.length > 0 : state.selectedFile !== null;
        elements.searchBtn.disabled = !hasFile || state.isSearching;
    }

    // ============================================
    // Search Functionality
    // ============================================
    async function performSearch() {
        if (state.isSearching) return;

        const controller = beginRequest('search');
        state.isSearching = true;
        elements.searchingOverlay.style.display = 'flex';
        updateSearchButton();

        try {
            const results = state.isBatchMode
                ? await performBatchSearch(controller.signal)
                : await performSingleSearch(controller.signal);

            state.currentResults = results;
            displayResults(results);

        } catch (error) {
            // A search the user themselves replaced is not a failure.
            if (isAbort(error)) return;
            console.error('Search error:', error);
            showNotification(`Search failed: ${error.message}`, 'error');
        } finally {
            endRequest('search', controller);
            state.isSearching = false;
            elements.searchingOverlay.style.display = 'none';
            updateSearchButton();
        }
    }

    async function performSingleSearch(signal) {
        const formData = new FormData();
        formData.append('image', state.selectedFile);
        formData.append('scope', elements.searchScope.value);
        formData.append('top_k', elements.topK.value);
        formData.append('check_watchlist', elements.checkWatchlist.checked);
        
        if (elements.dateFrom.value) {
            formData.append('date_from', elements.dateFrom.value);
        }
        if (elements.dateTo.value) {
            formData.append('date_to', elements.dateTo.value);
        }
        if (elements.pipelineFilter.value) {
            formData.append('pipeline_id', elements.pipelineFilter.value);
        }

        // Add exclude parameters
        const excludeParams = getExcludeParams();
        if (excludeParams.excludeIds) {
            formData.append('exclude_identity_ids', excludeParams.excludeIds);
        }
        if (excludeParams.excludeWatchlistIds) {
            formData.append('exclude_watchlist_ids', excludeParams.excludeWatchlistIds);
        }

        const response = await fetch('/api/search/advanced', {
            method: 'POST',
            credentials: 'include',
            headers: CSRF_HEADERS,
            body: formData,
            signal
        });

        if (!response.ok) {
            await throwHttpError(response, 'Search failed');
        }

        return await response.json();
    }

    async function performBatchSearch(signal) {
        const formData = new FormData();
        
        state.batchFiles.forEach(file => {
            formData.append('images', file);
        });
        
        formData.append('scope', elements.searchScope.value);
        formData.append('top_k', elements.topK.value);
        formData.append('check_watchlist', elements.checkWatchlist.checked);

        // Filters and exclusions are deliberately absent: POST
        // /api/search/batch accepts only these four fields and
        // batch_search_service never forwards exclusions. The panels are
        // disabled in batch mode so the UI does not imply otherwise.
        const response = await fetch('/api/search/batch', {
            method: 'POST',
            credentials: 'include',
            headers: CSRF_HEADERS,
            body: formData,
            signal
        });

        if (!response.ok) {
            await throwHttpError(response, 'Batch search failed');
        }

        return await response.json();
    }

    // ============================================
    // Quality Check
    // ============================================
    async function performQualityCheck() {
        if (!state.selectedFile) {
            showNotification('Please select an image first', 'info');
            return;
        }

        // No in-flight guard existed here: rapid clicks fired N concurrent
        // uploads and whichever resolved last won. Aborting the previous one
        // makes the newest click authoritative.
        const controller = beginRequest('quality');
        if (elements.qualityCheckBtn) elements.qualityCheckBtn.disabled = true;

        try {
            const formData = new FormData();
            formData.append('image', state.selectedFile);

            const response = await fetch('/api/search/quality-check', {
                method: 'POST',
                credentials: 'include',
                headers: CSRF_HEADERS,
                body: formData,
                signal: controller.signal
            });

            if (!response.ok) {
                await throwHttpError(response, 'Quality check failed');
            }

            const quality = await response.json();

            // A quality check that started before a search must not blank the
            // results the user is now reading: displayQualityResult hides the
            // results tabs.
            if (inflight.quality !== controller) return;
            displayQualityResult(quality);

        } catch (error) {
            if (isAbort(error)) return;
            console.error('Quality check error:', error);
            showNotification(`Quality check failed: ${error.message}`, 'error');
        } finally {
            endRequest('quality', controller);
            if (elements.qualityCheckBtn) elements.qualityCheckBtn.disabled = false;
        }
    }

    function displayQualityResult(quality) {
        // Clear all previous results first (backend logic: clear before display)
        clearQualityResults();
        
        const bandColors = {
            excellent: '#00ff96',
            good: '#88ff00',
            moderate: '#ffcc00',
            poor: '#ff8800',
            unusable: '#ff4444'
        };

        const html = `
            <div class="quality-result" style="padding: 1.5rem; background: rgba(0,0,0,0.3); border-radius: 8px; border: 1px solid ${bandColors[quality.band] || '#888'}; margin-bottom: 1rem;">
                <div style="display: flex; align-items: center; gap: 1rem; margin-bottom: 1rem;">
                    <div style="font-size: 2.5rem; color: ${bandColors[quality.band]}">${Math.round(quality.overall_score * 100)}%</div>
                    <div>
                        <div style="text-transform: uppercase; font-weight: 700; color: ${bandColors[quality.band] || '#888'}">${escapeHtml(quality.band)}</div>
                        <div style="font-size: 0.85rem; color: rgba(255,255,255,0.6)">${quality.proceed_recommendation ? 'Ready for search' : 'Quality too low'}</div>
                    </div>
                </div>
                
                <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.5rem; margin-bottom: 1rem;">
                    ${Object.entries(quality.details).map(([key, value]) => `
                        <div style="background: rgba(0,0,0,0.2); padding: 0.5rem; border-radius: 4px;">
                            <div style="font-size: 0.75rem; color: rgba(255,255,255,0.5); text-transform: uppercase;">${escapeHtml(key)}</div>
                            <div style="color: ${value.score >= 0.7 ? '#00ff96' : value.score >= 0.5 ? '#ffcc00' : '#ff4444'}">${Math.round(value.score * 100)}%</div>
                        </div>
                    `).join('')}
                </div>
                
                ${quality.warnings.length > 0 ? `
                    <div style="margin-top: 1rem;">
                        ${quality.warnings.map(w => `
                            <div style="color: #ffcc00; font-size: 0.85rem; margin-bottom: 0.25rem;">${escapeHtml(w)}</div>
                        `).join('')}
                    </div>
                ` : ''}
            </div>
        `;

        elements.emptyState.style.display = 'none';
        elements.resultsTabs.style.display = 'none';
        elements.matchesTab.style.display = 'none';
        elements.alertsTab.style.display = 'none';
        elements.summaryTab.style.display = 'none';
        
        // Insert quality result at the top of results content (backend logic: single source of truth)
        const qualityContainer = document.createElement('div');
        qualityContainer.id = 'quality-result-container';
        qualityContainer.innerHTML = html;
        elements.resultsContent.insertBefore(qualityContainer, elements.resultsContent.firstChild);
    }
    
    function clearQualityResults() {
        // Backend logic: clear quality results completely
        const qualityContainer = document.getElementById('quality-result-container');
        if (qualityContainer) {
            qualityContainer.remove();
        }
        // Also remove any quality-result divs that might exist
        const existingQualityResults = elements.resultsContent.querySelectorAll('.quality-result');
        existingQualityResults.forEach(el => el.remove());
    }

    // ============================================
    // Display Results
    // ============================================
    function displayResults(results) {
        // Backend logic: clear quality results when displaying search results
        clearQualityResults();
        
        elements.emptyState.style.display = 'none';
        elements.resultsTabs.style.display = 'flex';
        
        // Show export button (single button that opens modal)
        if (elements.exportBtn) {
            elements.exportBtn.style.display = 'inline-flex';
        }

        // Count totals
        let totalMatches = 0;
        let totalAlerts = 0;

        // Handle both single and batch results
        if (results.batch_id || results.results) {
            // Batch results
            const batchResults = results.results || [];
            batchResults.forEach(r => {
                totalMatches += r.matches ? r.matches.length : 0;
            });
            totalAlerts = alertCount(results.watchlist_alerts);
        } else {
            // Single search results (standard format)
            const faces = results.faces || [];
            faces.forEach(face => {
                totalMatches += face.matches ? face.matches.length : 0;
            });
            totalAlerts = alertCount(results.watchlist_alerts);
        }

        elements.matchesCount.textContent = totalMatches;
        elements.alertsCount.textContent = totalAlerts;

        // Render tabs
        renderMatchesTab(results);
        renderAlertsTab(results);
        renderSummaryTab(results);

        // Show first tab
        switchTab('matches');
    }

    function renderMatchesTab(results) {
        let html = '';

        // Check if results structure is valid
        if (!results) {
            console.error('[ADVANCED_SEARCH] No results provided to renderMatchesTab');
            elements.matchesTab.innerHTML = '<div class="empty-state"><p>No results available</p></div>';
            return;
        }

        if (results.batch_id || results.results) {
            // Batch results
            const batchResults = results.results || [];
            html = batchResults.map((imgResult, imgIndex) => {
                if (imgResult.status === 'error') {
                    return `
                        <div class="face-card" style="border-color: rgba(255, 0, 0, 0.3);">
                            <div class="face-header">
                                <div class="face-index" style="background: rgba(255,0,0,0.2); border-color: rgba(255,0,0,0.4); color: #ff4444;">${imgIndex + 1}</div>
                                <div class="face-info">
                                    <h4>${escapeHtml(imgResult.image_name)}</h4>
                                    <span style="color: #ff4444;">Error: ${escapeHtml(imgResult.error_message || 'Processing failed')}</span>
                                </div>
                            </div>
                        </div>
                    `;
                }

                return `
                    <div class="face-card">
                        <div class="face-header">
                            <div class="face-index">${imgIndex + 1}</div>
                            <div class="face-info">
                                <h4>${escapeHtml(imgResult.image_name)}</h4>
                                <div class="face-quality">
                                    <span style="color: rgba(255,255,255,0.5); font-size: 0.8rem;">
                                        ${escapeHtml(imgResult.faces_detected)} face(s) detected • ${imgResult.matches?.length || 0} match(es)
                                    </span>
                                </div>
                            </div>
                        </div>
                        <div class="matches-list">
                            ${renderMatches(imgResult.matches || [])}
                        </div>
                    </div>
                `;
            }).join('');
        } else {
            // Single search results
            const faces = results.faces || [];
            if (faces.length === 0) {
                html = '<div class="empty-state"><p>No faces detected in image</p></div>';
            } else {
                html = faces.map((face, faceIndex) => {
                if (face.skipped) {
                    return `
                        <div class="face-card" style="opacity: 0.6;">
                            <div class="face-header">
                                <div class="face-index">${faceIndex + 1}</div>
                                <div class="face-info">
                                    <h4>Face ${faceIndex + 1}</h4>
                                    <span style="color: #ffcc00; font-size: 0.85rem;">Skipped: ${escapeHtml(face.skip_reason)}</span>
                                </div>
                            </div>
                        </div>
                    `;
                }

                const qualityClass = getQualityClass(face.quality_score);

                return `
                    <div class="face-card">
                        <div class="face-header">
                            <div class="face-index">${faceIndex + 1}</div>
                            <div class="face-info">
                                <h4>Face ${faceIndex + 1}</h4>
                                <div class="face-quality">
                                    <div class="quality-bar">
                                        <div class="quality-fill ${qualityClass}" style="width: ${Math.max(0, Math.min(100, Number(face.quality_score) * 100)) || 0}%"></div>
                                    </div>
                                    <span class="quality-text">${Math.round(face.quality_score * 100)}% quality</span>
                                </div>
                                ${face.quality_warning ? `<div style="color: #ffcc00; font-size: 0.75rem; margin-top: 0.25rem;">${escapeHtml(face.quality_warning)}</div>` : ''}
                            </div>
                        </div>
                        <div class="matches-list">
                            ${renderMatches(face.matches || [])}
                        </div>
                    </div>
                `;
                }).join('');
            }
        }

        elements.matchesTab.innerHTML = html || '<div class="empty-state"><p>No matches found</p></div>';
    }

    function renderMatches(matches) {
        if (!matches || matches.length === 0) {
            return '<div style="color: rgba(255,255,255,0.5); font-size: 0.85rem; padding: 0.5rem;">No matches found</div>';
        }

        return matches.map(match => {
            const similarityClass = getSimilarityClass(match);
            // Backend provides snapshot_url - frontend just uses it (all logic in backend)
            // Use snapshot_url if available, otherwise try best_snapshot_path, then fallback
            let snapshotUrl = match.snapshot_url;
            if (!snapshotUrl && match.best_snapshot_path) {
                // Fallback: construct URL from path if snapshot_url not provided
                const path = match.best_snapshot_path;
                if (path.startsWith('storage/')) {
                    snapshotUrl = `/${path}`;
                } else if (!path.startsWith('/')) {
                    snapshotUrl = `/storage/${path}`;
                } else {
                    snapshotUrl = path;
                }
            }
            // snapshot_url arrives from the API unvalidated and went straight
            // into an img src. Anything not same-origin-relative is dropped so
            // the built-in placeholder is used instead.
            snapshotUrl = safeImageUrl(snapshotUrl);

            // Final fallback: the built-in placeholder.
            if (!snapshotUrl) {
                snapshotUrl = PLACEHOLDER_AVATAR;
            }

            const cardLabel = `View details for ${match.display_name || 'unidentified person'}, `
                + `${Math.round(match.similarity * 100)}% match`;

            // role="button" + tabindex: the card is a div, so without these it
            // cannot be reached or activated by keyboard at all. The keydown
            // action reuses the delegation actions.js already has for
            // data-action-keydown.
            return `
                <div class="match-card" role="button" tabindex="0"
                     aria-label="${escapeHtml(cardLabel)}"
                     data-action="viewIdentity" data-action-keydown="viewIdentityKey"
                     data-arg="${escapeHtml(match.identity_id)}"
                     data-identity-type="${escapeHtml(match.type)}">
                    <img class="match-snapshot" src="${escapeHtml(snapshotUrl)}" alt="${escapeHtml(match.display_name || 'Match')}"
                         data-fallback-src="${escapeHtml(PLACEHOLDER_AVATAR)}"
                         style="display: block; width: 50px; height: 50px; object-fit: cover; border-radius: 6px;">
                    <div class="match-details">
                        <div class="match-name">${escapeHtml(match.display_name || 'Unknown')}</div>
                        <div class="match-meta">
                            <span><i class="fas fa-${match.type === 'known' ? 'user-check' : 'user-secret'}"></i> ${escapeHtml(match.type)}</span>
                            <span><i class="fas fa-eye"></i> ${escapeHtml(match.appearances_count || 0)}</span>
                        </div>
                        ${match.watchlist_match ? `
                            <div class="watchlist-badge ${escapeHtml(match.watchlist_match.alert_level)}" style="margin-top: 0.3rem;">
                                <i class="fas fa-exclamation-triangle"></i>
                                ${escapeHtml(match.watchlist_match.list_name)}
                            </div>
                        ` : ''}
                    </div>
                    <div class="match-similarity ${similarityClass}">${Math.round(match.similarity * 100)}%</div>
                </div>
            `;
        }).join('');
    }

    /**
     * The two search endpoints disagree about this field's type:
     *   - single (/api/search/advanced) returns an ARRAY of alert objects
     *   - batch  (/api/search/batch)    returns an INT count
     *     (batch_export.py: `watchlist_alerts: int`)
     *
     * Reading `.length`/`.map()` off the int threw "alerts.map is not a
     * function", which escaped to the search catch — so a batch search that
     * hit any watchlist rendered an EMPTY results panel plus a red
     * "Search failed" toast, after the search had actually succeeded.
     * Returns a renderable array; a bare count has no per-alert detail to
     * render, so it yields [] and the caller shows the count-only state.
     */
    function normalizeAlerts(value) {
        if (Array.isArray(value)) return value;
        return [];
    }

    /** Alert count that works for both response shapes. */
    function alertCount(value) {
        if (Array.isArray(value)) return value.length;
        const n = Number(value);
        return Number.isFinite(n) ? n : 0;
    }

    function renderAlertsTab(results) {
        const alerts = normalizeAlerts(results.watchlist_alerts);
        const count = alertCount(results.watchlist_alerts);

        // Batch returns a count with no alert bodies. Say so plainly rather
        // than claiming "No Watchlist Alerts" when there were in fact some.
        if (alerts.length === 0 && count > 0) {
            elements.alertsTab.replaceChildren(
                buildEmptyState(
                    'fas fa-triangle-exclamation',
                    `${count} watchlist alert${count === 1 ? '' : 's'}`,
                    'Per-alert detail is not returned for batch searches. Run a single-image search to see the matches.'
                )
            );
            return;
        }

        if (alerts.length === 0) {
            elements.alertsTab.replaceChildren(
                buildEmptyState('fas fa-check-circle', 'No Watchlist Alerts',
                    'No matches found on any watchlists'));
            return;
        }

        // DOM-built. `notes` and `action_instructions` are operator free text
        // from the watchlist editor: interpolating them into innerHTML made
        // this a stored-XSS path straight into an admin session.
        elements.alertsTab.replaceChildren(...alerts.map(alert => {
            const level = ALERT_LEVEL_ICONS[String(alert.alert_level || '').toLowerCase()]
                ? String(alert.alert_level).toLowerCase()
                : 'info';

            const card = buildEl('div', `alert-card ${level}`);

            // Emitting the class names admin-search.css actually defines.
            // The old markup used alert-header/alert-icon/alert-title, which
            // no stylesheet this page loads declares, so alert cards rendered
            // unstyled; the sheet has always called them search-alert-*.
            const header = buildEl('div', 'search-alert-header');
            const iconWrap = buildEl('div', `search-alert-icon ${level}`);
            const icon = buildEl('i', `fas ${ALERT_LEVEL_ICONS[level]}`);
            icon.setAttribute('aria-hidden', 'true');
            iconWrap.appendChild(icon);
            header.appendChild(iconWrap);

            const title = buildEl('div', 'search-alert-title');
            title.appendChild(buildEl('h4', null, alert.identity_name || 'Unknown Identity'));
            const similarity = Number(alert.similarity);
            const subtitle = [
                alert.list_name || 'Unknown list',
                Number.isFinite(similarity) ? `${Math.round(similarity * 100)}% match` : null
            ].filter(Boolean).join(' • ');
            title.appendChild(buildEl('p', null, subtitle));
            header.appendChild(title);
            card.appendChild(header);

            if (alert.notes) {
                card.appendChild(buildEl('div', 'alert-notes', alert.notes));
            }
            if (alert.action_instructions) {
                const box = buildEl('div', 'alert-action');
                box.appendChild(buildEl('div', 'alert-action-label', 'Action Required'));
                box.appendChild(buildEl('div', 'alert-action-body', alert.action_instructions));
                card.appendChild(box);
            }
            return card;
        }));
    }

    function renderSummaryTab(results) {
        const summary = results.summary || {};
        const isBatch = !!results.batch_id;

        const html = `
            <div class="summary-stats">
                <div class="stat-card">
                    <div class="stat-value">${escapeHtml(isBatch ? results.total_images : summary.total_faces_detected || 0)}</div>
                    <div class="stat-label">${isBatch ? 'Images' : 'Faces'}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${escapeHtml(summary.total_matches || results.total_matches || 0)}</div>
                    <div class="stat-label">Matches</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${escapeHtml(summary.unique_identities_found || results.unique_identities || 0)}</div>
                    <div class="stat-label">Unique IDs</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${escapeHtml(summary.watchlist_alerts || alertCount(results.watchlist_alerts))}</div>
                    <div class="stat-label">Alerts</div>
                </div>
            </div>

            <div class="search-section" style="margin-top: 1rem;">
                <h3><i class="fas fa-info-circle"></i> Search Details</h3>
                <div style="display: grid; gap: 0.5rem; font-size: 0.85rem;">
                    <div style="display: flex; justify-content: space-between;">
                        <span style="color: rgba(255,255,255,0.6);">Search ID</span>
                        <span style="font-family: monospace;">${escapeHtml(results.search_id || results.batch_id)}</span>
                    </div>
                    <div style="display: flex; justify-content: space-between;">
                        <span style="color: rgba(255,255,255,0.6);">Processing Time</span>
                        <span>${escapeHtml(results.processing_time_ms)}ms</span>
                    </div>
                    ${isBatch ? `
                        <div style="display: flex; justify-content: space-between;">
                            <span style="color: rgba(255,255,255,0.6);">Successful Images</span>
                            <span style="color: #00ff96;">${escapeHtml(results.successful_images)}/${escapeHtml(results.total_images)}</span>
                        </div>
                    ` : `
                        <div style="display: flex; justify-content: space-between;">
                            <span style="color: rgba(255,255,255,0.6);">Faces Searched</span>
                            <span style="color: #00ff96;">${escapeHtml(summary.faces_searchable)}/${escapeHtml(summary.total_faces_detected)}</span>
                        </div>
                    `}
                </div>
            </div>

            ${!isBatch && results.faces && qualityWarningThreshold() !== null && results.faces.some(f => f.quality_score < qualityWarningThreshold()) ? `
                <div style="background: rgba(255, 204, 0, 0.1); border: 1px solid rgba(255, 204, 0, 0.3); border-radius: 8px; padding: 1rem; margin-top: 1rem;">
                    <div style="color: #ffcc00; font-weight: 600; margin-bottom: 0.5rem;">
                        <i class="fas fa-exclamation-triangle"></i> Quality Warning
                    </div>
                    <div style="color: rgba(255,255,255,0.8); font-size: 0.85rem;">
                        Some faces have low quality scores. For better results, try using clearer images.
                    </div>
                </div>
            ` : ''}
        `;

        elements.summaryTab.innerHTML = html;
    }

    function switchTab(tabName) {
        state.activeTab = tabName;

        // Update tab buttons
        document.querySelectorAll('.results-tab').forEach(tab => {
            tab.classList.toggle('active', tab.dataset.tab === tabName);
        });

        // Show/hide tab content
        elements.matchesTab.style.display = tabName === 'matches' ? 'block' : 'none';
        elements.alertsTab.style.display = tabName === 'alerts' ? 'block' : 'none';
        elements.summaryTab.style.display = tabName === 'summary' ? 'block' : 'none';
    }

    // `exportResults(format)` lived here: ~40 lines superseded by
    // handleExport (which reads the modal, gates unsupported formats, sends
    // the CSRF header and surfaces the server reason). Nothing referenced it,
    // and it was the last mutating POST on this page with no CSRF header.

    // ============================================
    // Helper Functions
    // ============================================
    /** Server-configured quality warning bar, or null before config loads. */
    function qualityWarningThreshold() {
        return state.quality.warningThreshold;
    }

    // Quality BANDS (excellent/good/moderate/poor/unusable) are the fixed
    // presentation scale defined by backend/core/face_quality.py's BANDS
    // table, not operator settings — unlike the warning bar above, which is
    // SEARCH_QUALITY_WARNING_THRESHOLD and is read from the server.
    const QUALITY_BANDS = [
        [0.85, 'excellent'], [0.70, 'good'], [0.50, 'moderate'], [0.30, 'poor']
    ];

    function getQualityClass(score) {
        for (const [floor, name] of QUALITY_BANDS) {
            if (score >= floor) return name;
        }
        return 'unusable';
    }

    // Band -> colour class. The BACKEND decides the band; this only paints it.
    //
    // This used to re-threshold client-side against hard-coded 0.90/0.75/0.60,
    // a copy of CONFIDENCE_*_MIN. Those settings are runtime-editable, so the
    // moment an admin changed one, the colour here and the confidence_band the
    // API returned disagreed with nothing to detect it. It also only knew four
    // bands where the backend emits five, so VERY_LOW was painted as LOW.
    const BAND_CLASS = {
        VERY_HIGH: 'very-high',
        HIGH: 'high',
        MEDIUM: 'medium',
        LOW: 'low',
        VERY_LOW: 'very-low'
    };

    function getSimilarityClass(match) {
        const band = match && match.confidence_band;
        if (band && BAND_CLASS[band]) return BAND_CLASS[band];
        // No band from the server: fall back to the thresholds it published
        // via /api/search/config, never to a literal copied into this file.
        const similarity = Number(match && match.similarity);
        if (!Number.isFinite(similarity)) return 'very-low';
        // Server shape: { VERY_HIGH: {min, max, label}, ... }. Walk it highest
        // first so the published minimums decide, never a literal in this file.
        const bands = state.confidenceBands || {};
        for (const key of ['VERY_HIGH', 'HIGH', 'MEDIUM', 'LOW']) {
            const min = bands[key] && Number(bands[key].min);
            if (Number.isFinite(min) && similarity >= min) return BAND_CLASS[key];
        }
        return 'very-low';
    }

    // ============================================
    // Identity detail — in place, not a page load
    //
    // This used to be `window.location.href = '/admin/unknown?view=' + id`.
    // Three things were wrong with that:
    //   * /admin/unknown is "Unknown Faces Center" and its grid is filtered to
    //     type == UNKNOWN, so a KNOWN match opened a modal over a list that
    //     cannot contain them;
    //   * the search lives only in memory here (no sessionStorage, no
    //     pushState), so navigating away and coming back meant re-uploading the
    //     image and re-running the search;
    //   * it is the odd one out — every other person-click in this app opens a
    //     detail modal in place.
    // ============================================

    /** What to return focus to when the panel closes. */
    let identityModalOpener = null;

    /** Most recent sightings to draw. The API returns ALL appearance rows. */
    const MAX_SIGHTINGS_SHOWN = 20;

    /** Resolve a pipeline id to its human name using the list already loaded
     *  for the Camera filter. Falls back to the id so a camera missing from
     *  that list still renders something identifiable. */
    function pipelineLabel(pipelineId) {
        const id = String(pipelineId || '');
        if (!id) return 'Unknown camera';
        const match = (state.pipelines || []).find(p => String(p.pipeline_id) === id);
        const name = match && String(match.display_name || '').trim();
        return name || id;
    }

    function formatDateTime(value) {
        if (!value) return '—';
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return String(value);
        return parsed.toLocaleString();
    }

    // The only types the API emits (FaceMatchResponse.type). Anything else is
    // rendered neutrally and logged — silently coercing an unexpected value to
    // "unknown" would mislabel a person on a security tool.
    const IDENTITY_TYPES = ['known', 'unknown'];

    function normalizeIdentityType(value) {
        const type = String(value || '').toLowerCase();
        if (IDENTITY_TYPES.includes(type)) return type;
        if (type) console.warn('Unexpected identity type from API:', value);
        return null;
    }

    /** UUID shape (the id column is uuid). Anything else never reaches fetch. */
    const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

    /**
     * Find the clicked match in the current results, for context the identity
     * endpoint does not return: THIS search's similarity, and the match's own
     * watchlist_match body (notes / action_instructions).
     * Handles both response shapes: single (faces[].matches) and batch
     * (results[].matches).
     */
    function findMatchContext(identityId) {
        const results = state.currentResults;
        if (!results) return null;
        const groups = results.faces || results.results || [];
        for (const group of groups) {
            for (const match of (group.matches || [])) {
                if (match && match.identity_id === identityId) return match;
            }
        }
        return null;
    }

    /** "This match" section: similarity/confidence, plus the watchlist hit's
     *  operator instructions when present. All text via buildEl — notes and
     *  action_instructions are free text from the watchlist editor. */
    function buildMatchContext(match) {
        const section = buildEl('section', 'identity-section identity-match-context');
        section.appendChild(buildEl('h5', 'identity-section-title', 'This match'));

        const grid = buildEl('div', 'identity-facts');
        const similarity = Number(match.similarity);
        grid.appendChild(buildDetailRow('Similarity',
            Number.isFinite(similarity) ? `${Math.round(similarity * 100)}%` : '—'));
        if (match.confidence_band) {
            grid.appendChild(buildDetailRow('Confidence',
                String(match.confidence_band).replace(/_/g, ' ')));
        }
        section.appendChild(grid);

        const hit = match.watchlist_match;
        if (hit) {
            const level = String(hit.alert_level || 'info').toLowerCase();
            const safeLevel = ALERT_LEVEL_ICONS[level] ? level : 'info';

            const alert = buildEl('div', `identity-match-alert alert-level-${safeLevel}`);
            const head = buildEl('div', 'match-alert-head');
            const icon = buildEl('i', `fas ${ALERT_LEVEL_ICONS[safeLevel]}`);
            icon.setAttribute('aria-hidden', 'true');
            head.appendChild(icon);
            head.appendChild(buildEl('strong', null, hit.list_name || 'Watchlist hit'));
            head.appendChild(buildEl('span', 'match-alert-level', safeLevel.toUpperCase()));
            alert.appendChild(head);

            if (hit.notes) {
                alert.appendChild(buildEl('p', 'match-alert-notes', hit.notes));
            }
            if (hit.action_instructions) {
                const action = buildEl('div', 'match-alert-action');
                action.appendChild(buildEl('span', 'match-alert-action-label', 'Action required'));
                action.appendChild(buildEl('p', 'match-alert-action-body', hit.action_instructions));
                alert.appendChild(action);
            }
            section.appendChild(alert);
        }
        return section;
    }

    /** One label/value row in the profile grid. */
    function buildDetailRow(label, value) {
        const row = buildEl('div', 'identity-row');
        row.appendChild(buildEl('span', 'identity-row-label', label));
        row.appendChild(buildEl('span', 'identity-row-value', value));
        return row;
    }

    function buildIdentityHeader(identity) {
        const header = buildEl('div', 'identity-header');

        const img = document.createElement('img');
        img.className = 'identity-snapshot';
        img.alt = identity.display_name || 'Identity snapshot';
        img.src = safeImageUrl(identity.snapshot_url) || PLACEHOLDER_AVATAR;
        img.dataset.fallbackSrc = PLACEHOLDER_AVATAR;
        header.appendChild(img);

        const summary = buildEl('div', 'identity-summary');
        summary.appendChild(buildEl('h4', 'identity-name',
            identity.display_name || 'Unidentified person'));

        const badges = buildEl('div', 'identity-badges');
        // Allowlisted, not coerced: an unexpected type renders a neutral badge
        // showing the raw value rather than being mislabelled "unknown".
        const type = normalizeIdentityType(identity.type);
        badges.appendChild(buildEl('span',
            type ? `identity-badge type-${type}` : 'identity-badge',
            type ? type.toUpperCase() : String(identity.type || 'UNTYPED').toUpperCase()));
        if (identity.status) {
            badges.appendChild(buildEl('span', 'identity-badge status',
                String(identity.status).toUpperCase()));
        }
        summary.appendChild(badges);
        header.appendChild(summary);
        return header;
    }

    function buildIdentityFacts(identity, sightings) {
        const grid = buildEl('div', 'identity-facts');

        // `appearances_count` is a denormalised counter and has drifted from the
        // number of appearance rows in live data, so the two are labelled
        // separately rather than reconciled here.
        grid.appendChild(buildDetailRow('Detections', String(identity.appearances_count ?? 0)));
        grid.appendChild(buildDetailRow('Recorded sightings', String(sightings.length)));
        grid.appendChild(buildDetailRow('First seen', formatDateTime(identity.first_seen_at)));
        grid.appendChild(buildDetailRow('Last seen', formatDateTime(identity.last_seen_at)));

        const cameras = Array.isArray(identity.pipeline_ids) ? identity.pipeline_ids : [];
        grid.appendChild(buildDetailRow(
            cameras.length === 1 ? 'Camera' : 'Cameras',
            cameras.length ? cameras.map(pipelineLabel).join(', ') : 'None recorded'));

        if (identity.embeddings_count !== undefined) {
            grid.appendChild(buildDetailRow('Face embeddings', String(identity.embeddings_count)));
        }
        return grid;
    }

    function buildSightings(sightings) {
        const section = buildEl('section', 'identity-section');
        const heading = buildEl('h5', 'identity-section-title', 'Recent sightings');
        section.appendChild(heading);

        if (sightings.length === 0) {
            section.appendChild(buildEl('p', 'identity-empty',
                'No sightings recorded for this person.'));
            return section;
        }

        const shown = sightings.slice(0, MAX_SIGHTINGS_SHOWN);
        if (sightings.length > shown.length) {
            // Say what is being withheld — a truncated list that looks complete
            // is worse than a shorter one that admits it.
            heading.textContent =
                `Recent sightings — showing ${shown.length} of ${sightings.length}`;
        }

        const list = buildEl('ul', 'identity-sightings');
        shown.forEach(sighting => {
            const item = buildEl('li', 'identity-sighting');
            item.appendChild(buildEl('span', 'sighting-camera',
                pipelineLabel(sighting.pipeline_id)));
            item.appendChild(buildEl('span', 'sighting-time',
                formatDateTime(sighting.start_time)));
            list.appendChild(item);
        });
        section.appendChild(list);
        return section;
    }

    function buildWatchlists(watchlists) {
        const section = buildEl('section', 'identity-section');
        section.appendChild(buildEl('h5', 'identity-section-title', 'Watchlists'));

        if (!Array.isArray(watchlists) || watchlists.length === 0) {
            section.appendChild(buildEl('p', 'identity-empty', 'Not on any watchlist.'));
            return section;
        }

        const list = buildEl('ul', 'identity-watchlists');
        watchlists.forEach(entry => {
            const level = String(entry.alert_level || 'info').toLowerCase();
            const safeLevel = ALERT_LEVEL_ICONS[level] ? level : 'info';
            const item = buildEl('li', `identity-watchlist alert-level-${safeLevel}`);
            const icon = buildEl('i', `fas ${ALERT_LEVEL_ICONS[safeLevel]}`);
            icon.setAttribute('aria-hidden', 'true');
            item.appendChild(icon);
            item.appendChild(buildEl('span', 'watchlist-name',
                entry.name || 'Untitled watchlist'));
            item.appendChild(buildEl('span', 'watchlist-level', safeLevel));
            list.appendChild(item);
        });
        section.appendChild(list);
        return section;
    }

    function renderIdentityPanel(identity, watchlists, matchContext) {
        const body = document.getElementById('identity-modal-body');
        const title = document.getElementById('identity-modal-title');
        if (!body) return;

        if (title) {
            title.textContent = identity.display_name || 'Identity';
        }

        const sightings = Array.isArray(identity.appearances) ? identity.appearances : [];
        const children = [
            buildIdentityHeader(identity),
            buildIdentityFacts(identity, sightings)
        ];
        if (matchContext) {
            children.push(buildMatchContext(matchContext));
        }
        children.push(buildWatchlists(watchlists), buildSightings(sightings));
        body.replaceChildren(...children);

        // Full profile opens the dedicated identity page in a NEW tab (the
        // anchor carries target="_blank"), so no `from` param: a back link in
        // a fresh tab would land on an EMPTY search page — the results only
        // exist in this tab's memory. The same-tab callers (intelligence,
        // dashboard) do pass `from`, where the back link is truthful.
        const fullProfile = document.getElementById('identity-full-profile');
        if (fullProfile) {
            fullProfile.href = `/admin/identity/${encodeURIComponent(identity.id)}`;
        }
        // The existing cross-page deep link (?identity_id= is read by
        // admin-security-intelligence.js) — the useful next step for a known
        // or watchlisted person.
        const analyze = document.getElementById('identity-analyze');
        if (analyze) {
            analyze.href = `/admin/security-intelligence`
                + `?identity_id=${encodeURIComponent(identity.id)}&tab=threats`;
        }
    }

    function openIdentityModal() {
        const modal = document.getElementById('identity-modal');
        if (!modal) return;
        // Remember what to focus when the panel closes, so a keyboard user is
        // not dropped at the top of the document.
        identityModalOpener = document.activeElement;
        modal.classList.add('active');
        const close = document.getElementById('close-identity-modal');
        if (close) close.focus();
    }

    function closeIdentityModal() {
        const modal = document.getElementById('identity-modal');
        if (!modal) return;
        modal.classList.remove('active');
        if (identityModalOpener && document.contains(identityModalOpener)) {
            identityModalOpener.focus();
        }
        identityModalOpener = null;
    }

    // Scoped, not window.viewIdentity: dispatch goes through the Actions
    // registry alone, so there is no reason to offer this as a global — and a
    // global is one more thing an injected script could replace.
    async function viewIdentity(identityId) {
        if (!identityId) return;

        const body = document.getElementById('identity-modal-body');
        const title = document.getElementById('identity-modal-title');
        if (title) title.textContent = 'Identity';

        // Ids come from data attributes on rendered markup. The column is a
        // uuid; anything else is malformed and never reaches the network.
        if (!UUID_RE.test(String(identityId))) {
            console.error('Rejected malformed identity id:', identityId);
            if (body) {
                body.replaceChildren(buildEmptyState(
                    'fas fa-triangle-exclamation',
                    'Could not load this identity',
                    'The identity reference on this card is malformed.'));
            }
            openIdentityModal();
            return;
        }

        if (body) {
            body.replaceChildren(buildEl('p', 'identity-loading', 'Loading identity…'));
        }
        openIdentityModal();

        // Clicking quickly down a result list must not let an earlier response
        // overwrite a later one.
        const controller = beginRequest('identity');

        try {
            const response = await fetch(`/api/admin/identity/${encodeURIComponent(identityId)}`, {
                credentials: 'include',
                signal: controller.signal
            });
            if (!response.ok) {
                await throwHttpError(response, 'Could not load this identity');
            }
            const identity = await response.json();

            // Watchlist membership is a separate endpoint, and a failure there
            // must not blank the profile that already loaded.
            let watchlists = [];
            try {
                const wlResponse = await fetch(
                    `/api/identities/${encodeURIComponent(identityId)}/watchlists`,
                    { credentials: 'include', signal: controller.signal });
                if (wlResponse.ok) {
                    const payload = await wlResponse.json();
                    watchlists = Array.isArray(payload?.watchlists) ? payload.watchlists : [];
                }
            } catch (error) {
                if (isAbort(error)) return;
                console.warn('Watchlist membership unavailable:', error);
            }

            if (inflight.identity !== controller) return;
            renderIdentityPanel(identity, watchlists, findMatchContext(identityId));

        } catch (error) {
            if (isAbort(error)) return;
            console.error('Identity detail error:', error);
            if (body) {
                body.replaceChildren(buildEmptyState(
                    'fas fa-triangle-exclamation',
                    'Could not load this identity',
                    error.message));
            }
        } finally {
            endRequest('identity', controller);
        }
    }

    // Registered here, inside the IIFE, because viewIdentity is deliberately
    // not a window global. actions.js is script #1 on this page, so the
    // registry exists by the time this runs.
    Actions.register({
        viewIdentity: (el) => viewIdentity(el.dataset.arg),
        // Keyboard activation for the result cards (role="button" divs).
        // Delegated through the data-action-keydown binding actions.js already
        // has; Space is preventDefault'ed so the page does not scroll.
        viewIdentityKey: (el, event) => {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault();
            viewIdentity(el.dataset.arg);
        },
    });

    // ============================================
    // Load Pipelines
    // ============================================
    async function loadPipelines() {
        try {
            // /api/dashboard/pipelines, not /api/pipelines: this filter listed
            // raw pipeline_ids, and most cameras have a UUID for an id, so the
            // dropdown was a column of opaque GUIDs. This endpoint carries
            // display_name — the admin-set location_name with a pipeline_id
            // fallback — so an unnamed camera still gets a label instead of a
            // blank row. Same source and precedence as the home dashboard.
            const response = await fetch('/api/dashboard/pipelines', {
                credentials: 'include',
                cache: 'no-store'
            });

            if (response.ok) {
                const payload = await response.json();
                const pipelines = Array.isArray(payload && payload.pipelines)
                    ? payload.pipelines
                    : [];
                state.pipelines = pipelines;

                // Options are built, not interpolated: location_name is
                // operator-supplied and used to reach an innerHTML attribute raw.
                const all = buildEl('option', null, 'All Cameras');
                all.value = '';

                const options = [all].concat(
                    pipelines
                        .filter(pipeline => pipeline && pipeline.pipeline_id)
                        .map(pipeline => {
                            const id = String(pipeline.pipeline_id);
                            const name = String(pipeline.display_name || '').trim() || id;
                            // The value stays the id: that is what
                            // /api/search/advanced filters on. Only the label
                            // changes.
                            const option = buildEl(
                                'option', null,
                                pipeline.is_active === false ? `${name} (inactive)` : name);
                            option.value = id;
                            // Keep the id discoverable for a camera whose
                            // label is now a location the admin may not
                            // recognise as that pipeline.
                            option.title = name === id ? id : `${name} — ${id}`;
                            return option;
                        })
                        // Human-readable list, so sort the way a human reads it.
                        .sort((a, b) => a.textContent.localeCompare(
                            b.textContent, undefined, { sensitivity: 'base' }))
                );
                elements.pipelineFilter.replaceChildren(...options);
            }
        } catch (error) {
            console.error('Failed to load pipelines:', error);
        }
    }

    // ============================================
    // Clear Results
    // ============================================
    function clearResults() {
        // Backend logic: centralized clearing of all results
        state.currentResults = null;
        
        // Clear quality results
        clearQualityResults();
        
        // Clear search results
        elements.resultsTabs.style.display = 'none';
        elements.emptyState.style.display = 'flex';
        elements.matchesTab.innerHTML = '';
        elements.alertsTab.innerHTML = '';
        elements.summaryTab.innerHTML = '';
        if (elements.exportBtn) {
            elements.exportBtn.style.display = 'none';
        }
    }

    // ============================================
    // Event Listeners
    // ============================================
    function setupEventListeners() {
        // Search button
        elements.searchBtn.addEventListener('click', performSearch);

        // Quality check
        elements.qualityCheckBtn.addEventListener('click', performQualityCheck);

        // Clear results
        elements.clearResultsBtn.addEventListener('click', () => {
            clearResults();
            clearSelectedFile();
            state.batchFiles = [];
            renderBatchFiles();
        });

        // Tab switching
        document.querySelectorAll('.results-tab').forEach(tab => {
            tab.addEventListener('click', () => switchTab(tab.dataset.tab));
        });

        // Export button (opens modal)
        if (elements.exportBtn) {
            elements.exportBtn.addEventListener('click', () => {
                if (!state.currentResults) {
                    showNotification('No results to export', 'info');
                    return;
                }
                syncExportOptions();
                elements.exportModal.classList.add('active');
            });
        }

        // Export modal handlers
        if (elements.closeExportModal) {
            elements.closeExportModal.addEventListener('click', closeExportModal);
        }
        if (elements.cancelExportBtn) {
            elements.cancelExportBtn.addEventListener('click', closeExportModal);
        }
        if (elements.confirmExportBtn) {
            elements.confirmExportBtn.addEventListener('click', handleExport);
        }

        // Close modal on outside click
        if (elements.exportModal) {
            elements.exportModal.addEventListener('click', (e) => {
                if (e.target === elements.exportModal) {
                    closeExportModal();
                }
            });
        }

        // Identity panel: close button, Done, backdrop click.
        const identityModal = document.getElementById('identity-modal');
        ['close-identity-modal', 'identity-modal-done'].forEach(id => {
            const button = document.getElementById(id);
            if (button) button.addEventListener('click', closeIdentityModal);
        });
        if (identityModal) {
            identityModal.addEventListener('click', (e) => {
                if (e.target === identityModal) closeIdentityModal();
            });
        }

        // Escape closes whichever panel is open. The identity panel is checked
        // first because it can be opened on top of nothing else.
        document.addEventListener('keydown', (e) => {
            if (e.key !== 'Escape') return;
            if (identityModal && identityModal.classList.contains('active')) {
                closeIdentityModal();
            } else if (elements.exportModal && elements.exportModal.classList.contains('active')) {
                closeExportModal();
            }
        });

        // Batch progress modal
        if (elements.closeBatchProgressBtn) {
            elements.closeBatchProgressBtn.addEventListener('click', () => {
                elements.batchProgressModal.classList.remove('active');
            });
        }
    }

    function closeExportModal() {
        if (elements.exportModal) {
            elements.exportModal.classList.remove('active');
        }
    }

    /**
     * Make the modal reflect what the current result shape actually supports,
     * so an unsupported combination cannot be confirmed in the first place.
     */
    function syncExportOptions() {
        const isBatch = Boolean(state.currentResults?.batch_id);
        const select = elements.exportFormat;
        const hint = document.getElementById('export-format-hint');

        if (select) {
            let reselect = false;
            Array.from(select.options).forEach(option => {
                const unsupported = isBatch && !BATCH_EXPORT_FORMATS.includes(option.value);
                option.disabled = unsupported;
                option.hidden = unsupported;
                if (unsupported && option.selected) reselect = true;
            });
            if (reselect) select.value = 'csv';
        }
        if (hint) hint.hidden = !isBatch;

        // include_images only exists on the single-result export route.
        const imagesToggle = elements.exportIncludeImages;
        if (imagesToggle) {
            imagesToggle.disabled = isBatch;
            if (isBatch) imagesToggle.checked = false;
            const label = imagesToggle.closest('label');
            if (label) label.hidden = isBatch;
        }
    }

    async function handleExport() {
        if (!state.currentResults) {
            showNotification('No results to export', 'info');
            return;
        }

        const isBatch = Boolean(state.currentResults.batch_id);
        const format = elements.exportFormat?.value || 'csv';
        const includeImages = elements.exportIncludeImages?.checked || false;

        // export_service.export_batch_results supports csv/json only; picking
        // PDF here used to 400 after the user confirmed.
        if (isBatch && !BATCH_EXPORT_FORMATS.includes(format)) {
            showNotification(
                `${format.toUpperCase()} export is not available for batch results — choose CSV or JSON`,
                'error');
            return;
        }

        // Repeated Confirm clicks fired duplicate POSTs and triggered
        // multiple downloads; the modal only closed after the request
        // completed, so the button stayed live the whole time.
        const controller = beginRequest('export');
        if (elements.confirmExportBtn) elements.confirmExportBtn.disabled = true;

        try {
            const endpoint = isBatch ? '/api/search/batch/export' : '/api/search/export';

            // Only parameters the route actually declares are sent.
            // `include_images` exists on /api/search/export alone, and
            // `include_quality` exists on neither (quality is always written),
            // so the old `x || true` checkbox could not have had any effect.
            const params = new URLSearchParams({ format });
            if (!isBatch && includeImages) params.set('include_images', 'true');

            const response = await fetch(`${endpoint}?${params.toString()}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    ...CSRF_HEADERS
                },
                credentials: 'include',
                body: JSON.stringify(state.currentResults),
                signal: controller.signal
            });

            if (!response.ok) {
                // The server says WHY ("Unsupported export format: pdf").
                // Replacing that with a bare "Export failed" threw the only
                // actionable information away.
                await throwHttpError(response, 'Export failed');
            }

            const blob = await response.blob();
            const downloadUrl = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = downloadUrl;
            const filename = state.currentResults.batch_id 
                ? `batch-search-${new Date().toISOString().split('T')[0]}.${format}`
                : `search-results-${new Date().toISOString().split('T')[0]}.${format}`;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(downloadUrl);

            closeExportModal();
            showNotification(`Exported as ${format.toUpperCase()}`, 'success');

        } catch (error) {
            if (isAbort(error)) return;
            console.error('Export error:', error);
            showNotification(`Export failed: ${error.message}`, 'error');
        } finally {
            endRequest('export', controller);
            if (elements.confirmExportBtn) elements.confirmExportBtn.disabled = false;
        }
    }

    // Store identities and watchlists data
    let identitiesData = [];
    let watchlistsData = [];
    let selectedIdentities = new Set();
    let selectedWatchlists = new Set();

    async function loadIdentitiesForExclusion() {
        try {
            // Only load KNOWN identities for exclusion (excluding unknown identities doesn't make sense)
            const response = await fetch('/api/admin/identities?limit=1000&type=known', {
                credentials: 'include'
            });
            if (response.ok) {
                const data = await response.json();
                if (data.identities) {
                    identitiesData = data.identities.filter(identity => 
                        identity.type === 'known' && identity.display_name
                    );
                    
                    // Populate hidden select for form submission
                    if (elements.excludeIdentities) {
                        elements.excludeIdentities.innerHTML = '';
                        identitiesData.forEach(identity => {
                            const option = document.createElement('option');
                            option.value = identity.id;
                            option.textContent = identity.display_name || `Identity ${identity.id.substring(0, 8)}`;
                            elements.excludeIdentities.appendChild(option);
                        });
                    }
                    
                    // Render dropdown items
                    renderIdentitiesDropdown();
                }
            }
        } catch (error) {
            console.error('Error loading identities:', error);
        }
    }
    
    /**
     * Build one selectable row.
     *
     * DOM-built rather than interpolated: display_name and watchlist.name are
     * operator-supplied strings that used to land in innerHTML with only a
     * JavaScript-style quote escape in front of them, which leaves the double
     * quote and the angle brackets untouched inside an HTML attribute.
     *
     * data-arg2 is gone: toggleIdentity/toggleWatchlist never read the name.
     */
    function buildExcludeItem(id, name, typeLabel, action, isSelected) {
        const item = buildEl('div', `exclude-item${isSelected ? ' selected' : ''}`);
        item.dataset.id = id;
        item.dataset.name = name;
        item.dataset.action = action;
        item.dataset.arg = id;
        item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', isSelected ? 'true' : 'false');

        const icon = buildEl('i', `fas ${isSelected ? 'fa-check-circle' : 'fa-circle'}`);
        icon.setAttribute('aria-hidden', 'true');
        item.appendChild(icon);
        item.appendChild(buildEl('span', 'exclude-item-name', name));
        item.appendChild(buildEl('span', 'exclude-item-type', typeLabel));
        return item;
    }

    /**
     * Flip one row in place.
     *
     * Re-rendering the whole list on every selection is what made the dropdown
     * collapse after each click: the handler destroyed the clicked node, so by
     * the time the outside-click listener ran, dropdown.contains(e.target) was
     * false for a node that was no longer in the document.
     */
    function updateExcludeItemState(listEl, id, isSelected) {
        if (!listEl) return;
        const item = Array.from(listEl.children).find(el => el.dataset && el.dataset.id === id);
        if (!item) return;
        item.classList.toggle('selected', isSelected);
        item.setAttribute('aria-selected', isSelected ? 'true' : 'false');
        const icon = item.querySelector('i');
        if (icon) icon.className = `fas ${isSelected ? 'fa-check-circle' : 'fa-circle'}`;
    }

    function identityLabel(identity) {
        return identity.display_name || `Identity ${String(identity.id).substring(0, 8)}`;
    }

    function renderIdentitiesDropdown(searchTerm = '') {
        if (!elements.excludeIdentitiesList) return;

        const needle = searchTerm.toLowerCase();
        const filtered = identitiesData.filter(identity => {
            if (!needle) return true;
            return (identity.display_name || '').toLowerCase().includes(needle);
        });

        if (filtered.length === 0) {
            elements.excludeIdentitiesList.replaceChildren(
                buildEl('div', 'exclude-empty-state', 'No identities found'));
            return;
        }

        elements.excludeIdentitiesList.replaceChildren(...filtered.map(identity =>
            buildExcludeItem(
                identity.id,
                identityLabel(identity),
                identity.type || 'known',
                'adminSearchToggleIdentity',
                selectedIdentities.has(identity.id))));
    }

    function buildExcludeChip(id, iconClass, name, removeAction) {
        const chip = buildEl('div', 'exclude-chip');
        chip.dataset.id = id;
        const icon = buildEl('i', `fas ${iconClass}`);
        icon.setAttribute('aria-hidden', 'true');
        chip.appendChild(icon);
        chip.appendChild(buildEl('span', null, name));

        const remove = buildEl('button', 'exclude-chip-remove');
        remove.type = 'button';
        remove.dataset.action = removeAction;
        remove.dataset.arg = id;
        remove.setAttribute('aria-label', `Remove ${name}`);
        const removeIcon = buildEl('i', 'fas fa-times');
        removeIcon.setAttribute('aria-hidden', 'true');
        remove.appendChild(removeIcon);
        chip.appendChild(remove);
        return chip;
    }

    function renderIdentitiesChips() {
        if (!elements.excludeIdentitiesChips) return;

        const chips = Array.from(selectedIdentities)
            .map(id => {
                const identity = identitiesData.find(i => i.id === id);
                if (!identity) return null;
                return buildExcludeChip(id, 'fa-user', identityLabel(identity),
                    'adminSearchRemoveIdentity');
            })
            .filter(Boolean);
        elements.excludeIdentitiesChips.replaceChildren(...chips);

        if (elements.excludeIdentitiesCount) {
            elements.excludeIdentitiesCount.textContent = selectedIdentities.size;
        }

        // Update hidden select
        if (elements.excludeIdentities) {
            Array.from(elements.excludeIdentities.options).forEach(opt => {
                opt.selected = selectedIdentities.has(opt.value);
            });
        }
    }

    window.adminSearch = window.adminSearch || {};
    window.adminSearch.toggleIdentity = function(id) {
        const isSelected = !selectedIdentities.has(id);
        if (isSelected) {
            selectedIdentities.add(id);
        } else {
            selectedIdentities.delete(id);
        }
        updateExcludeItemState(elements.excludeIdentitiesList, id, isSelected);
        renderIdentitiesChips();
    };

    window.adminSearch.removeIdentity = function(id) {
        selectedIdentities.delete(id);
        updateExcludeItemState(elements.excludeIdentitiesList, id, false);
        renderIdentitiesChips();
    };

    async function loadWatchlistsForExclusion() {
        try {
            const response = await fetch('/api/watchlists', {
                credentials: 'include'
            });
            if (response.ok) {
                watchlistsData = await response.json();
                
                // Populate hidden select for form submission
                if (elements.excludeWatchlists) {
                    elements.excludeWatchlists.innerHTML = '';
                    watchlistsData.forEach(watchlist => {
                        const option = document.createElement('option');
                        option.value = watchlist.id;
                        option.textContent = watchlist.name;
                        elements.excludeWatchlists.appendChild(option);
                    });
                }
                
                // Render dropdown items
                renderWatchlistsDropdown();
            }
        } catch (error) {
            console.error('Error loading watchlists:', error);
        }
    }
    
    function watchlistLevel(watchlist) {
        const level = String(watchlist.alert_level || 'info').toLowerCase();
        return ALERT_LEVEL_ICONS[level] ? level : 'info';
    }

    function renderWatchlistsDropdown(searchTerm = '') {
        if (!elements.excludeWatchlistsList) return;

        const needle = searchTerm.toLowerCase();
        const filtered = watchlistsData.filter(watchlist => {
            if (!needle) return true;
            return (watchlist.name || '').toLowerCase().includes(needle);
        });

        if (filtered.length === 0) {
            elements.excludeWatchlistsList.replaceChildren(
                buildEl('div', 'exclude-empty-state', 'No watchlists found'));
            return;
        }

        elements.excludeWatchlistsList.replaceChildren(...filtered.map(watchlist => {
            const level = watchlistLevel(watchlist);
            const item = buildExcludeItem(
                watchlist.id,
                watchlist.name || 'Untitled watchlist',
                level,
                'adminSearchToggleWatchlist',
                selectedWatchlists.has(watchlist.id));
            item.classList.add(`alert-level-${level}`);
            return item;
        }));
    }

    function renderWatchlistsChips() {
        if (!elements.excludeWatchlistsChips) return;

        const chips = Array.from(selectedWatchlists)
            .map(id => {
                const watchlist = watchlistsData.find(w => w.id === id);
                if (!watchlist) return null;
                return buildExcludeChip(
                    id,
                    ALERT_LEVEL_ICONS[watchlistLevel(watchlist)],
                    watchlist.name || 'Untitled watchlist',
                    'adminSearchRemoveWatchlist');
            })
            .filter(Boolean);
        elements.excludeWatchlistsChips.replaceChildren(...chips);

        if (elements.excludeWatchlistsCount) {
            elements.excludeWatchlistsCount.textContent = selectedWatchlists.size;
        }

        // Update hidden select
        if (elements.excludeWatchlists) {
            Array.from(elements.excludeWatchlists.options).forEach(opt => {
                opt.selected = selectedWatchlists.has(opt.value);
            });
        }
    }

    window.adminSearch.toggleWatchlist = function(id) {
        const isSelected = !selectedWatchlists.has(id);
        if (isSelected) {
            selectedWatchlists.add(id);
        } else {
            selectedWatchlists.delete(id);
        }
        updateExcludeItemState(elements.excludeWatchlistsList, id, isSelected);
        renderWatchlistsChips();
    };

    window.adminSearch.removeWatchlist = function(id) {
        selectedWatchlists.delete(id);
        updateExcludeItemState(elements.excludeWatchlistsList, id, false);
        renderWatchlistsChips();
    };

    function getExcludeParams() {
        // Get from selected sets (new UI) or fallback to hidden selects
        const excludeIds = selectedIdentities.size > 0 
            ? Array.from(selectedIdentities)
            : Array.from(elements.excludeIdentities?.selectedOptions || [])
                .map(opt => opt.value)
                .filter(v => v);
        
        const excludeWatchlistIds = selectedWatchlists.size > 0
            ? Array.from(selectedWatchlists)
            : Array.from(elements.excludeWatchlists?.selectedOptions || [])
                .map(opt => opt.value)
                .filter(v => v);
        
        return {
            excludeIds: excludeIds.length > 0 ? excludeIds.join(',') : null,
            excludeWatchlistIds: excludeWatchlistIds.length > 0 ? excludeWatchlistIds.join(',') : null
        };
    }
    
    // Setup exclude UI event handlers
    function setupExcludeUI() {
        // Identities search
        if (elements.excludeIdentitiesSearch) {
            elements.excludeIdentitiesSearch.addEventListener('input', (e) => {
                const searchTerm = e.target.value;
                renderIdentitiesDropdown(searchTerm);
                if (searchTerm && elements.excludeIdentitiesDropdown) {
                    elements.excludeIdentitiesDropdown.classList.add('show');
                }
            });
            
            elements.excludeIdentitiesSearch.addEventListener('focus', () => {
                if (elements.excludeIdentitiesDropdown) {
                    elements.excludeIdentitiesDropdown.classList.add('show');
                }
            });
        }
        
        // Watchlists search
        if (elements.excludeWatchlistsSearch) {
            elements.excludeWatchlistsSearch.addEventListener('input', (e) => {
                const searchTerm = e.target.value;
                renderWatchlistsDropdown(searchTerm);
                if (searchTerm && elements.excludeWatchlistsDropdown) {
                    elements.excludeWatchlistsDropdown.classList.add('show');
                }
            });
            
            elements.excludeWatchlistsSearch.addEventListener('focus', () => {
                if (elements.excludeWatchlistsDropdown) {
                    elements.excludeWatchlistsDropdown.classList.add('show');
                }
            });
        }
        
        // Close dropdowns when clicking outside.
        //
        // Registered in the CAPTURE phase deliberately. actions.js dispatches
        // on bubble, so in the bubble phase this listener can run *after* a
        // handler has already re-rendered the list — and `contains(e.target)`
        // is false for a detached node, which closed the dropdown on every
        // selection. In capture we always see the target still in the tree.
        document.addEventListener('click', (e) => {
            const inside = (dropdown, input) =>
                (dropdown && dropdown.contains(e.target)) || (input && input.contains(e.target));

            if (elements.excludeIdentitiesDropdown &&
                !inside(elements.excludeIdentitiesDropdown, elements.excludeIdentitiesSearch)) {
                elements.excludeIdentitiesDropdown.classList.remove('show');
            }

            if (elements.excludeWatchlistsDropdown &&
                !inside(elements.excludeWatchlistsDropdown, elements.excludeWatchlistsSearch)) {
                elements.excludeWatchlistsDropdown.classList.remove('show');
            }
        }, true);
    }

    // ============================================
    // Batch-mode capability gating (§7)
    //
    // POST /api/search/batch accepts only images/scope/top_k/check_watchlist,
    // and batch_search_service never forwards exclusions — so in batch mode the
    // Filters and Exclude panels were fully interactive with zero effect.
    // Extending the batch endpoint is a backend change; until then the UI must
    // not offer what it cannot deliver.
    // ============================================
    function applyBatchModeCapabilities() {
        const batch = state.isBatchMode;

        // Disabled, deliberately NOT cleared. Toggling batch mode on and off
        // must not silently discard a date range or an exclusion list the user
        // spent time assembling — the visible disabled state plus the note
        // next to it already says the values are not being applied.
        [elements.dateFrom, elements.dateTo, elements.pipelineFilter,
         elements.excludeIdentitiesSearch, elements.excludeWatchlistsSearch]
            .forEach(el => { if (el) el.disabled = batch; });

        if (batch) {
            [elements.excludeIdentitiesDropdown, elements.excludeWatchlistsDropdown]
                .forEach(dropdown => dropdown && dropdown.classList.remove('show'));
        }

        document.querySelectorAll('[data-batch-unsupported]').forEach(section => {
            section.classList.toggle('capability-disabled', batch);
            const notice = section.querySelector('.batch-unsupported-note');
            if (notice) notice.hidden = !batch;
        });
    }

    // ============================================
    // Initialize
    // ============================================
    function init() {
        console.log('Initializing Advanced Search...');
        
        setupUploadHandlers();
        setupEventListeners();
        loadSearchConfig();
        loadPipelines();
        loadIdentitiesForExclusion();
        loadWatchlistsForExclusion();
        setupExcludeUI();

        // Browsers restore checkbox state across a soft reload, so the toggle
        // can come back checked while state.isBatchMode is still its initial
        // false. Adopt whatever the control actually says before anything
        // reads the mode.
        if (elements.batchModeToggle) {
            state.isBatchMode = elements.batchModeToggle.checked;
            elements.batchFilesContainer.style.display = state.isBatchMode ? 'block' : 'none';
            const prompt = elements.uploadArea.querySelector('p');
            if (prompt) {
                prompt.textContent = state.isBatchMode
                    ? 'Drag & drop multiple images'
                    : 'Drag & drop image here';
            }
        }
        applyBatchModeCapabilities();
        updateSearchButton();

        console.log('Advanced Search initialized');
    }

    // Start when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Add CSS animation styles
    const style = document.createElement('style');
    style.textContent = `
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        @keyframes slideOut {
            from { transform: translateX(0); opacity: 1; }
            to { transform: translateX(100%); opacity: 0; }
        }
    `;
    document.head.appendChild(style);

})();



// ---------------------------------------------------------------------------
// CSP-safe event registration
//
// These handlers were previously reached through inline onclick/onchange/
// onsubmit attributes, which `script-src 'self'` blocks. Registered rather
// than looked up on window, so only these names are invocable; delegated in
// actions.js, so dynamically rendered elements work without rebinding.
// ---------------------------------------------------------------------------
Actions.register({
    removeBatchFile: (el) => removeBatchFile(el.dataset.arg),
    // viewIdentity / viewIdentityKey are registered INSIDE the page IIFE —
    // the function is scoped there, not exposed on window.
    // The second argument was dropped: neither toggle ever read the name, so
    // it was an injection sink carrying a value nothing consumed.
    adminSearchToggleIdentity: (el) => window.adminSearch?.toggleIdentity(el.dataset.arg),
    adminSearchRemoveIdentity: (el) => window.adminSearch?.removeIdentity(el.dataset.arg),
    adminSearchToggleWatchlist: (el) => window.adminSearch?.toggleWatchlist(el.dataset.arg),
    adminSearchRemoveWatchlist: (el) => window.adminSearch?.removeWatchlist(el.dataset.arg),
});
