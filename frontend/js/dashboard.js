/**
 * Face Recognition Dashboard
 * ==========================
 * Architectural rules implemented here (do not regress):
 *
 *  THREE SEPARATE DURATIONS — never conflated:
 *    - DASHBOARD_FACE_DISPLAY (cfg.faceDisplayMs): how long a known face stays
 *      VISIBLE on this page. Expiry uses last_seen_at.
 *    - ALERT_NOTIFICATION_WINDOW (cfg.alertWindowMs): re-alert cooldown.
 *    - DATABASE RETENTION (cfg.databaseRetentionDays): storage lifetime —
 *      comes ONLY from the backend; this file never invents deletion dates.
 *
 *  - typed config validation (parseDurationMs) — zero is a valid value,
 *    truthiness is never used to accept/reject config
 *  - initialization is ordered: config + pipeline names + user access load
 *    BEFORE the WebSocket connects; a visible error banner appears if
 *    critical config fails and safe defaults (fallback mode) are logged
 *  - strict timestamp parsing: invalid timestamps are REJECTED (skipped +
 *    counted), never replaced with "now"; naive UTC strings go through ONE
 *    legacy compatibility function; future timestamps display safely
 *  - per-face state separates best-confidence evidence from recency:
 *    last_seen_at/latest_detection_at always advance on newer valid
 *    sightings; best_similarity/best_image only improve
 *  - display-name precedence: admin-approved DB name → webhook-reported
 *    name → pipeline_id; reported names never overwrite approved names
 *  - card pruning only on the authoritative /api/dashboard/pipelines
 *    response (complete === true), never on partial data
 *  - single WebSocket (CONNECTING/OPEN guard), intentional-close flag,
 *    stored reconnect timer, bounded exponential backoff + jitter,
 *    socket-bound initial-data fallback timer, initialDataReceived flag
 *  - strict per-type WebSocket message validation; event_id dedup with a
 *    bounded cache; is_new_detection may bypass the person cooldown but
 *    NEVER exact-event deduplication; ONE sound decision per event
 *  - one shared AudioContext behind an explicit "Enable Alert Sound"
 *    button with honest enabled/disabled/blocked/unsupported states
 *  - zero innerHTML with dynamic data, zero inline handlers, element
 *    registries (Maps) instead of untrusted string selectors
 *  - image validation limits (base64 size/alphabet) with placeholders
 *  - every in-memory collection is bounded; timers are named, stored and
 *    cleared on shutdown; hidden tabs pause non-critical work
 *
 * Metric ownership (documented contract):
 *    #totalPipelines  backend stats (active pipelines)
 *    #totalFaces      frontend: currently_visible_known_faces
 *    #totalDetections backend stats: total_processed (persistent total)
 *    #queueSize/#processing/system metrics: backend stats
 */
'use strict';

(() => {
    // ==================================================================
    // Constants (named — no magic interval numbers)
    // ==================================================================
    const ONE_MINUTE_MS = 60_000;
    const FACE_EXPIRY_SWEEP_MS = ONE_MINUTE_MS;       // expired faces leave within ~1min
    const ALERT_CACHE_CLEANUP_MS = ONE_MINUTE_MS;
    const NAME_RECONCILE_MS = ONE_MINUTE_MS;
    const CLOCK_SKEW_MS = 60_000;                      // tolerated future skew -> "Just now"
    const ALERT_COOLDOWN_MS = 2_000;
    const NOTIFICATION_COOLDOWN_MS = 5_000;

    // Bounded-collection limits (eviction: oldest-first)
    const MAX_PIPELINES = 60;
    const MAX_FACES_PER_PIPELINE = 80;
    const MAX_ALERT_HISTORY = 100;
    const MAX_PROCESSED_EVENT_IDS = 500;
    const MAX_FIRST_TIME_KEYS = 2000;
    const MAX_RECENT_ALERT_KEYS = 500;
    const MAX_TASK_NOTIFICATION_IDS = 100;

    // Image safety limits
    const MAX_BASE64_IMAGE_CHARS = 1_500_000;          // ~1.1 MB decoded

    // ==================================================================
    // Config (typed, validated — zero is valid; truthiness is banned here)
    // ==================================================================
    function parseDurationMs(value, fallback, options = {}) {
        const parsed = Number(value);
        const minimum = options.minimum ?? 0;
        const maximum = options.maximum ?? Number.MAX_SAFE_INTEGER;
        if (!Number.isFinite(parsed) || parsed < minimum || parsed > maximum) {
            return fallback;
        }
        return parsed;
    }

    const cfg = {
        faceDisplayMs: 3 * 60 * 60 * 1000,   // DASHBOARD_FACE_DISPLAY_HOURS (display only)
        alertWindowMs: 1 * 60 * 60 * 1000,   // ALERT_NOTIFICATION_WINDOW_HOURS (alert cooldown only)
        databaseRetentionDays: null,          // backend-owned; null => never displayed
        loaded: false,
        fallbackMode: false,
    };

    function applyConfigPayload(config, source) {
        if (!config || typeof config !== 'object') return false;
        cfg.faceDisplayMs = parseDurationMs(config.face_display_ms, cfg.faceDisplayMs,
            { minimum: 0, maximum: 365 * 24 * 3600 * 1000 });
        cfg.alertWindowMs = parseDurationMs(config.alert_notification_window_ms, cfg.alertWindowMs,
            { minimum: 0, maximum: 365 * 24 * 3600 * 1000 });
        const retention = Number(config.database_retention_days);
        cfg.databaseRetentionDays = (Number.isFinite(retention) && retention > 0) ? retention : cfg.databaseRetentionDays;
        console.log(`[CONFIG] Applied (${source}): display=${cfg.faceDisplayMs}ms alertWindow=${cfg.alertWindowMs}ms retentionDays=${cfg.databaseRetentionDays ?? 'unknown'}`);
        return true;
    }

    async function loadDashboardConfig() {
        const result = await api('/api/dashboard/config');
        if (result.ok && result.payload && result.payload.success && result.payload.config) {
            applyConfigPayload(result.payload.config, 'api');
            cfg.loaded = true;
            return true;
        }
        cfg.fallbackMode = true;
        console.warn('[CONFIG] ⚠️ FALLBACK MODE: /api/dashboard/config unavailable — using built-in defaults '
            + `(display=${cfg.faceDisplayMs}ms, alertWindow=${cfg.alertWindowMs}ms, retention=unknown)`);
        return false;
    }

    // ==================================================================
    // Timestamp handling (strict; legacy naive-UTC through ONE gate)
    // ==================================================================
    let invalidTimestampCount = 0;
    let legacyTimestampWarned = false;

    /** Strict: returns Date or null. NEVER substitutes "now". */
    function parseServerTimestamp(value) {
        if (value instanceof Date) {
            return Number.isFinite(value.getTime()) ? value : null;
        }
        if (typeof value !== 'string' || !value.trim()) return null;
        const s = value.trim();
        if (/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)) {
            const parsed = new Date(s);
            return Number.isFinite(parsed.getTime()) ? parsed : null;
        }
        // Legacy naive timestamp — the backend's contract is UTC. Route through
        // the single compatibility gate so no caller guesses differently.
        return parseLegacyNaiveUtcTimestamp(s);
    }

    /** TEMPORARY compatibility for timezone-less backend strings (treated as UTC). */
    function parseLegacyNaiveUtcTimestamp(s) {
        if (!legacyTimestampWarned) {
            legacyTimestampWarned = true;
            console.warn('[TIME] Legacy naive timestamp received — interpreting as UTC. '
                + 'Backend should send timezone-aware ISO-8601 (…Z).');
        }
        const parsed = new Date(s + 'Z');
        return Number.isFinite(parsed.getTime()) ? parsed : null;
    }

    /** Parse or count-and-null. Callers must SKIP null (never treat as now). */
    function requireTimestamp(value, context) {
        const parsed = parseServerTimestamp(value);
        if (!parsed) {
            invalidTimestampCount += 1;
            console.warn(`[TIME] Invalid timestamp rejected (${context}); total invalid: ${invalidTimestampCount}`);
        }
        return parsed;
    }

    /** Safe age label: handles future timestamps and clock skew — no "-30s ago". */
    function formatAge(date) {
        if (!(date instanceof Date) || !Number.isFinite(date.getTime())) return 'Unknown';
        const diff = Date.now() - date.getTime();
        if (diff < -CLOCK_SKEW_MS) return 'Future timestamp';
        if (diff < ONE_MINUTE_MS) return 'Just now';   // includes small negative skew
        const seconds = Math.floor(diff / 1000);
        const minutes = Math.floor(seconds / 60);
        const hours = Math.floor(minutes / 60);
        const days = Math.floor(hours / 24);
        if (days > 0) return `${days}d ${hours % 24}h ago`;
        if (hours > 0) return `${hours}h ${minutes % 60}m ago`;
        return `${minutes}m ago`;
    }

    function formatBeirut(date) {
        if (!(date instanceof Date) || !Number.isFinite(date.getTime())) return 'Unknown';
        return date.toLocaleString('en-US', {
            timeZone: 'Asia/Beirut',
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
        });
    }

    // ==================================================================
    // Shared API helper (timeout, no-store, safe parsing, in-flight dedup)
    // ==================================================================
    const inFlight = new Map();
    const controllers = new Set();
    const timers = new Set();
    let destroyed = false;

    function trackTimeout(fn, ms) {
        const id = setTimeout(() => { timers.delete(id); fn(); }, ms);
        timers.add(id);
        return id;
    }

    function trackInterval(fn, ms) {
        const id = setInterval(fn, ms);
        timers.add(id);
        return id;
    }

    async function api(url, options = {}) {
        // One in-flight request per resource — overlapping calls share a promise
        if (inFlight.has(url)) return inFlight.get(url);
        const promise = (async () => {
            const controller = new AbortController();
            controllers.add(controller);
            const timeoutId = setTimeout(() => controller.abort(), options.timeoutMs || 15000);
            timers.add(timeoutId);
            try {
                const resp = await fetch(url, {
                    method: options.method || 'GET',
                    headers: { Accept: 'application/json' },
                    credentials: 'include',
                    cache: 'no-store',
                    body: options.body,
                    signal: controller.signal
                });
                let payload = null;
                if (resp.status !== 204) {
                    const ctype = (resp.headers.get('content-type') || '').toLowerCase();
                    try {
                        if (ctype.includes('application/json')) payload = await resp.json();
                        else {
                            const text = await resp.text();
                            payload = text ? { detail: text.slice(0, 200) } : null;
                        }
                    } catch (e) { payload = null; }
                }
                return { ok: resp.ok, status: resp.status, payload };
            } catch (e) {
                return { ok: false, status: 0, payload: null, error: e && e.name };
            } finally {
                clearTimeout(timeoutId);
                timers.delete(timeoutId);
                controllers.delete(controller);
            }
        })();
        inFlight.set(url, promise);
        try { return await promise; }
        finally { inFlight.delete(url); }
    }

    // ==================================================================
    // User access (explicit — null is never ambiguous)
    // ==================================================================
    const access = { loaded: false, mode: 'restricted', pipelineIds: [] };

    function hasPipelineAccess(pipelineId) {
        if (!access.loaded) return false;      // not loaded yet => render nothing
        if (access.mode === 'all') return true;
        return access.pipelineIds.includes(pipelineId);
    }

    async function loadUserPipelineAccess() {
        try {
            const user = window.getAuthMe ? await window.getAuthMe() : null;
            // toggle, not add: this only ever added the class, so a demoted
            // administrator kept admin-only styling for the whole session.
            const permissions = (user && Array.isArray(user.permissions)) ? user.permissions : [];
            document.body.classList.toggle('admin-user', permissions.includes('admin.users.manage'));
            const trackingBtn = document.getElementById('tracking-btn');
            if (trackingBtn) trackingBtn.style.display = permissions.includes('chatbot.use') ? 'flex' : 'none';

            const privileges = window.getAuthPrivileges ? await window.getAuthPrivileges() : null;
            const role = privileges ? (privileges.role || (privileges.user && privileges.user.role)) : (user && user.role);
            if (role === 'admin') {
                access.mode = 'all';
                access.pipelineIds = [];
            } else {
                access.mode = 'restricted';
                access.pipelineIds = (privileges && privileges.pipelines) || [];
            }
            access.loaded = true;

            const unknownFacesBtn = document.getElementById('unknown-faces-btn');
            if (unknownFacesBtn && privileges) {
                unknownFacesBtn.style.display = privileges.can_access_unknown_faces ? 'flex' : 'none';
            }
        } catch (e) {
            // Backend still enforces access on every message/API — worst case we
            // render nothing until a retry succeeds.
            console.warn('[ACCESS] Could not load privileges (backend still enforces access):', e && e.message);
            access.loaded = true;
            access.mode = 'restricted';
            access.pipelineIds = [];
        }
    }

    // ==================================================================
    // Pipeline display names (approved DB name > reported > pipeline_id)
    // ==================================================================
    const approvedNames = new Map();   // pipeline_id -> admin-approved display name (DB)
    const reportedNames = new Map();   // pipeline_id -> webhook-reported name (untrusted)

    function getPipelineDisplayName(pipelineId) {
        return approvedNames.get(pipelineId)
            || reportedNames.get(pipelineId)
            || pipelineId;
    }

    /** Webhook-reported names are stored separately — NEVER into approvedNames. */
    function recordReportedName(pipelineId, locationName) {
        if (!pipelineId || typeof locationName !== 'string') return;
        const trimmed = locationName.trim();
        if (!trimmed || trimmed.length > 200) return;
        if (reportedNames.get(pipelineId) !== trimmed) {
            reportedNames.set(pipelineId, trimmed);
            if (!approvedNames.has(pipelineId)) updatePipelineCardTitle(pipelineId);
        }
    }

    /** Authoritative reconciliation: complete responses only may prune. */
    async function refreshPipelineDisplayNames() {
        const result = await api('/api/dashboard/pipelines');
        if (!result.ok || !result.payload || !Array.isArray(result.payload.pipelines)) return;
        const payload = result.payload;

        approvedNames.clear();
        payload.pipelines.forEach(p => {
            if (p && typeof p.pipeline_id === 'string' && typeof p.display_name === 'string') {
                approvedNames.set(p.pipeline_id, p.display_name);
            }
        });
        pipelineCardElements.forEach((_, pid) => updatePipelineCardTitle(pid));

        // Prune ONLY when the backend explicitly declares completeness
        if (payload.complete === true) {
            const existing = new Set(approvedNames.keys());
            let pruned = false;
            for (const pid of [...faceStore.keys()]) {
                if (!existing.has(pid)) { faceStore.delete(pid); pruned = true; }
            }
            for (const pid of [...unknownCounts.keys()]) {
                if (!existing.has(pid)) { unknownCounts.delete(pid); pruned = true; }
            }
            if (pruned) {
                console.log('[DASHBOARD] 🧹 Pruned pipelines absent from the authoritative complete listing');
                scheduleRender();
            }
        }
    }

    // ==================================================================
    // Face store — best-evidence vs recency are SEPARATE
    // ==================================================================
    // faceStore: Map<pipelineId, Map<faceName, entry>>
    // entry: { name, best_similarity, best_image, best_image_seen_at,
    //          first_seen_at, last_seen_at, latest_detection_at, processing_time_ms }
    const faceStore = new Map();
    const unknownCounts = new Map();   // pipeline_id -> count

    function getPipelineFaces(pipelineId) {
        let faces = faceStore.get(pipelineId);
        if (!faces) {
            if (faceStore.size >= MAX_PIPELINES) {
                const oldest = faceStore.keys().next().value; // eviction: oldest-inserted
                faceStore.delete(oldest);
                removePipelineCard(oldest);
            }
            faces = new Map();
            faceStore.set(pipelineId, faces);
        }
        return faces;
    }

    function validImage(base64) {
        if (typeof base64 !== 'string' || !base64) return null;
        if (base64.length > MAX_BASE64_IMAGE_CHARS) {
            console.warn('[IMAGE] Oversized face image rejected (length capped)');
            return null;
        }
        // light sanity check: base64 alphabet only (never log the payload itself)
        if (!/^[A-Za-z0-9+/=]+$/.test(base64.slice(0, 64))) return null;
        return base64;
    }

    /**
     * Ingest one sighting.
     * RULES: recency fields ALWAYS advance for a newer valid sighting;
     * best_* fields only improve. A newer LOWER-confidence sighting therefore
     * still extends the display period (regression-tested).
     */
    function ingestFaceSighting(pipelineId, face, detectionTime, processingMs) {
        const faces = getPipelineFaces(pipelineId);
        const name = face.name;
        const similarity = Number(face.similarity);
        const sim = Number.isFinite(similarity) ? Math.min(1, Math.max(0, similarity)) : 0;
        const image = validImage(face.image);
        const lastSeen = face.last_seen_at ? (parseServerTimestamp(face.last_seen_at) || detectionTime) : detectionTime;

        let entry = faces.get(name);
        if (!entry) {
            if (faces.size >= MAX_FACES_PER_PIPELINE) {
                // Evict the least-recently-seen entry
                let oldestKey = null, oldestTime = Infinity;
                faces.forEach((e, k) => {
                    const t = e.last_seen_at.getTime();
                    if (t < oldestTime) { oldestTime = t; oldestKey = k; }
                });
                if (oldestKey !== null) {
                    faces.delete(oldestKey);
                    removeDetectionItem(pipelineId, oldestKey);
                }
            }
            entry = {
                name,
                best_similarity: sim,
                best_image: image,
                best_image_seen_at: detectionTime,
                first_seen_at: detectionTime,
                last_seen_at: lastSeen,
                latest_detection_at: detectionTime,
                processing_time_ms: Number.isFinite(processingMs) ? processingMs : null,
            };
            faces.set(name, entry);
            return { entry, isNew: true };
        }

        // Recency ALWAYS advances (independent of confidence)
        if (detectionTime > entry.latest_detection_at) entry.latest_detection_at = detectionTime;
        if (lastSeen > entry.last_seen_at) entry.last_seen_at = lastSeen;
        if (Number.isFinite(processingMs)) entry.processing_time_ms = processingMs;

        // Best-evidence improves only (documented image policy: the image of the
        // highest-confidence sighting wins; equal confidence keeps the existing)
        if (sim > entry.best_similarity) {
            entry.best_similarity = sim;
            if (image) {
                entry.best_image = image;
                entry.best_image_seen_at = detectionTime;
            }
        } else if (!entry.best_image && image) {
            entry.best_image = image;
            entry.best_image_seen_at = detectionTime;
        }
        return { entry, isNew: false };
    }

    /** Expiry sweep — DISPLAY duration only, keyed on last_seen_at. */
    function sweepExpiredFaces() {
        const now = Date.now();
        let changed = false;
        faceStore.forEach((faces, pipelineId) => {
            faces.forEach((entry, name) => {
                if (now - entry.last_seen_at.getTime() > cfg.faceDisplayMs) {
                    faces.delete(name);
                    removeDetectionItem(pipelineId, name);
                    changed = true;
                }
            });
            if (faces.size === 0) faceStore.delete(pipelineId);
        });
        if (changed) scheduleRender();
    }

    // ==================================================================
    // Element registries (no untrusted string selectors)
    // ==================================================================
    const pipelineCardElements = new Map();   // pipeline_id -> card element
    const detectionItemElements = new Map();  // `${pipelineId} ${name}` -> item element

    const itemKey = (pipelineId, name) => `${pipelineId} ${name}`;

    function removePipelineCard(pipelineId) {
        const card = pipelineCardElements.get(pipelineId);
        if (card) card.remove();
        pipelineCardElements.delete(pipelineId);
        for (const key of [...detectionItemElements.keys()]) {
            if (key.startsWith(pipelineId + ' ')) detectionItemElements.delete(key);
        }
    }

    function removeDetectionItem(pipelineId, name) {
        const key = itemKey(pipelineId, name);
        const node = detectionItemElements.get(key);
        if (node) node.remove();
        detectionItemElements.delete(key);
    }

    function updatePipelineCardTitle(pipelineId) {
        const card = pipelineCardElements.get(pipelineId);
        if (!card) return;
        const titleEl = card.querySelector('.pipeline-title');
        if (!titleEl) return;
        const iconEl = titleEl.querySelector('i');
        titleEl.textContent = ' ' + getPipelineDisplayName(pipelineId);
        if (iconEl) titleEl.prepend(iconEl);
        // Pending-name indicator: reported differs from the approved DB name
        const reported = reportedNames.get(pipelineId);
        const approved = approvedNames.get(pipelineId);
        titleEl.title = (approved && reported && reported !== approved)
            ? `Camera reports name "${reported}" (pending admin approval)` : '';
    }

    // ==================================================================
    // Rendering (DOM builders only — zero innerHTML with data)
    // ==================================================================
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

    let renderTimer = null;
    function scheduleRender() {
        if (renderTimer) return;
        renderTimer = trackTimeout(() => { renderTimer = null; renderDashboard(); }, 100);
    }

    function buildEmptyState() {
        const empty = el('div', 'no-data');
        empty.appendChild(icon('fa-inbox'));
        empty.appendChild(el('h3', null, 'No detections yet'));
        empty.appendChild(el('p', null, 'Waiting for webhook requests...'));
        return empty;
    }

    function renderDashboard() {
        const grid = document.getElementById('pipelineGrid');
        if (!grid || !access.loaded) return;
        if (document.hidden) return; // skip expensive rerenders while hidden; reconciled on return

        const visiblePipelines = new Set();
        faceStore.forEach((faces, pid) => { if (faces.size && hasPipelineAccess(pid)) visiblePipelines.add(pid); });
        unknownCounts.forEach((count, pid) => { if (count > 0 && hasPipelineAccess(pid)) visiblePipelines.add(pid); });

        for (const pid of [...pipelineCardElements.keys()]) {
            if (!visiblePipelines.has(pid)) removePipelineCard(pid);
        }

        if (visiblePipelines.size === 0) {
            if (!grid.querySelector('.no-data')) grid.replaceChildren(buildEmptyState());
            return;
        }
        const noData = grid.querySelector('.no-data');
        if (noData) noData.remove();

        visiblePipelines.forEach(pid => renderPipelineCard(grid, pid));
    }

    function renderPipelineCard(grid, pipelineId) {
        const faces = faceStore.get(pipelineId) || new Map();
        let card = pipelineCardElements.get(pipelineId);

        if (!card) {
            card = el('div', 'pipeline-card');
            card.dataset.pipelineId = pipelineId;

            const header = el('div', 'pipeline-header');
            const title = el('div', 'pipeline-title');
            title.appendChild(icon('fa-video'));
            header.appendChild(title);
            header.appendChild(el('div', 'pipeline-badge'));
            card.appendChild(header);
            card.appendChild(el('div', 'pipeline-content'));

            grid.appendChild(card);
            pipelineCardElements.set(pipelineId, card);
            updatePipelineCardTitle(pipelineId);
        }

        const badge = card.querySelector('.pipeline-badge');
        if (badge) badge.textContent = `${faces.size} unique person${faces.size !== 1 ? 's' : ''}`;

        updateUnknownBadge(card, pipelineId);

        const content = card.querySelector('.pipeline-content');
        if (!content) return;

        // Unknown-only explanatory note
        let note = content.querySelector('.unknown-only-note');
        if (faces.size === 0 && (unknownCounts.get(pipelineId) || 0) > 0) {
            if (!note) {
                note = el('div', 'unknown-only-note');
                note.appendChild(icon('fa-user-secret'));
                note.appendChild(el('p', null, 'No known faces detected'));
                const link = el('a', null, 'Review unknown faces →');
                link.href = '/admin/unknown?pipeline=' + encodeURIComponent(pipelineId);
                note.appendChild(link);
                content.appendChild(note);
            }
        } else if (note) {
            note.remove();
        }

        faces.forEach((entry, name) => renderDetectionItem(content, pipelineId, name, entry));
    }

    function renderDetectionItem(content, pipelineId, name, entry) {
        const key = itemKey(pipelineId, name);
        let item = detectionItemElements.get(key);

        if (!item) {
            item = el('div', 'detection-item');
            item.dataset.pipeline = pipelineId;
            item.dataset.face = name;

            const imgContainer = el('div', 'detection-image-container');
            if (entry.best_image) {
                const img = el('img', 'detection-image');
                img.alt = name;
                img.loading = 'lazy';
                img.src = 'data:image/jpeg;base64,' + entry.best_image;
                img.title = 'Click to view alert, double-click to view identity details';
                // click behavior via delegation on #pipelineGrid (no inline handlers)
                imgContainer.appendChild(img);
            } else {
                const placeholder = el('div', 'no-data');
                placeholder.appendChild(icon('fa-user-slash'));
                placeholder.appendChild(el('p', null, 'No image available'));
                imgContainer.appendChild(placeholder);
            }
            imgContainer.appendChild(el('div', 'detection-image-overlay'));
            item.appendChild(imgContainer);

            const body = el('div', 'detection-item-content');
            const facesList = el('div', 'faces-list');
            const faceItem = el('div', 'face-item');
            const faceName = el('div', 'face-name');
            faceName.style.cursor = 'pointer';
            faceName.title = 'Click to view identity details';
            faceName.appendChild(icon('fa-user-check'));
            faceName.appendChild(el('span', null, ' ' + name));
            faceItem.appendChild(faceName);
            faceItem.appendChild(el('div', 'face-similarity'));
            facesList.appendChild(faceItem);
            body.appendChild(facesList);

            const ts = el('div', 'timestamp');
            const primary = el('div', 'timestamp-primary');
            primary.appendChild(icon('fa-hourglass-half'));
            primary.appendChild(el('span', 'visible-label', ' Visible for: '));
            primary.appendChild(el('span', 'time-remaining'));
            primary.appendChild(el('span', 'processing-time'));
            ts.appendChild(primary);
            const seen = el('div', 'timestamp-secondary');
            seen.appendChild(icon('fa-calendar-alt'));
            seen.appendChild(el('span', 'last-seen-line'));
            ts.appendChild(seen);
            // "Stored until" line only exists when the backend told us retention
            if (cfg.databaseRetentionDays !== null) {
                const stored = el('div', 'timestamp-secondary');
                stored.appendChild(icon('fa-database'));
                stored.appendChild(el('span', 'stored-until-line'));
                ts.appendChild(stored);
            }
            body.appendChild(ts);
            item.appendChild(body);

            content.appendChild(item);
            detectionItemElements.set(key, item);
        }

        // Update dynamic fields (textContent only)
        const simEl = item.querySelector('.face-similarity');
        if (simEl) simEl.textContent = `${(entry.best_similarity * 100).toFixed(1)}%`;

        const img = item.querySelector('.detection-image');
        if (img && entry.best_image) {
            const newSrc = 'data:image/jpeg;base64,' + entry.best_image;
            if (img.src !== newSrc) crossfadeImage(img, newSrc);
        }

        const procEl = item.querySelector('.processing-time');
        if (procEl) {
            procEl.textContent = Number.isFinite(entry.processing_time_ms)
                ? ` ⚡ ${entry.processing_time_ms.toFixed(1)}ms` : '';
        }
        updateItemTimes(item, entry);
    }

    function updateItemTimes(item, entry) {
        const remainingEl = item.querySelector('.time-remaining');
        if (remainingEl) {
            const remaining = cfg.faceDisplayMs - (Date.now() - entry.last_seen_at.getTime());
            remainingEl.textContent = remaining <= 0 ? 'expired' : formatRemaining(remaining);
        }
        const seenEl = item.querySelector('.last-seen-line');
        if (seenEl) seenEl.textContent = ` Last seen: ${formatAge(entry.last_seen_at)}`;
        const storedEl = item.querySelector('.stored-until-line');
        if (storedEl && cfg.databaseRetentionDays !== null) {
            // Backend-provided retention only — this file never invents a policy
            const storedUntil = new Date(entry.latest_detection_at.getTime()
                + cfg.databaseRetentionDays * 24 * 3600 * 1000);
            storedEl.textContent = ` Stored until: ${storedUntil.toLocaleDateString('en-US',
                { year: 'numeric', month: 'short', day: 'numeric' })}`;
        }
    }

    function formatRemaining(ms) {
        const hours = Math.floor(ms / 3_600_000);
        const minutes = Math.floor((ms % 3_600_000) / 60_000);
        const seconds = Math.floor((ms % 60_000) / 1000);
        let out = '';
        if (hours > 0) out += `${hours}h `;
        if (minutes > 0 || hours > 0) out += `${minutes}m `;
        return (out + `${seconds}s`).trim();
    }

    /** Safe crossfade: preload, then swap src on the SAME element.
     *  No cloned nodes, no copied executable attributes, no stacked images. */
    function crossfadeImage(img, newSrc) {
        if (img.dataset.pendingSrc === newSrc) return; // collapse rapid updates
        img.dataset.pendingSrc = newSrc;
        const pre = new Image();
        pre.onload = () => {
            if (destroyed || img.dataset.pendingSrc !== newSrc) return;
            img.style.transition = 'opacity 0.35s ease-in-out';
            img.style.opacity = '0.25';
            trackTimeout(() => {
                img.src = newSrc;
                img.style.opacity = '1';
                delete img.dataset.pendingSrc;
            }, 200);
        };
        pre.onerror = () => { delete img.dataset.pendingSrc; }; // keep original image
        pre.src = newSrc;
    }

    function updateUnknownBadge(card, pipelineId) {
        const header = card.querySelector('.pipeline-header');
        if (!header) return;
        const count = unknownCounts.get(pipelineId) || 0;
        let badge = header.querySelector('.unknown-badge');
        if (count > 0) {
            if (!badge) {
                badge = el('a', 'unknown-badge');
                badge.title = 'Unknown faces detected on this camera — click to review';
                header.appendChild(badge);
            }
            badge.href = '/admin/unknown?pipeline=' + encodeURIComponent(pipelineId);
            badge.replaceChildren(icon('fa-user-secret'), el('span', null, `${count} unknown`));
        } else if (badge) {
            badge.remove();
        }
    }

    // Per-second countdown refresh for visible items (display only)
    function tickCountdowns() {
        if (document.hidden) return;
        detectionItemElements.forEach((item, key) => {
            const sep = key.indexOf(' ');
            const pid = key.slice(0, sep);
            const name = key.slice(sep + 1);
            const entry = faceStore.get(pid) && faceStore.get(pid).get(name);
            if (entry) updateItemTimes(item, entry);
        });
    }

    // ==================================================================
    // WebSocket lifecycle
    // ==================================================================
    let ws = null;
    let reconnectAttempts = 0;
    const MAX_RECONNECT_ATTEMPTS = 10;
    let reconnectTimer = null;
    let initialDataFallbackTimer = null;
    let intentionalWebSocketClose = false;
    let initialDataReceived = false;

    function updateConnectionStatus(status) {
        const statusEl = document.getElementById('connectionStatus');
        const statusText = document.getElementById('statusText');
        if (!statusEl || !statusText) return;
        statusEl.className = `connection-status ${status}`;
        statusText.textContent = status === 'connected' ? 'Connected'
            : status === 'connecting' ? 'Connecting...' : 'Disconnected';
    }

    function connectWebSocket() {
        if (destroyed || intentionalWebSocketClose) return;
        // Never open a duplicate while one is CONNECTING or OPEN
        if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
        if (reconnectTimer) { clearTimeout(reconnectTimer); timers.delete(reconnectTimer); reconnectTimer = null; }

        updateConnectionStatus('connecting');
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        let socket;
        try {
            socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
        } catch (e) {
            scheduleReconnect();
            return;
        }
        ws = socket;

        socket.onopen = () => {
            updateConnectionStatus('connected');
            reconnectAttempts = 0;
            hideError();

            // Initial-data fallback: bound to THIS socket instance; cancelled on
            // reconnect; skipped once valid initial data has been received
            // (a valid EMPTY response also counts — zero faces is not a failure).
            if (initialDataFallbackTimer) { clearTimeout(initialDataFallbackTimer); timers.delete(initialDataFallbackTimer); }
            const socketInstance = socket;
            initialDataFallbackTimer = trackTimeout(async () => {
                initialDataFallbackTimer = null;
                if (ws !== socketInstance || socketInstance.readyState !== WebSocket.OPEN) return;
                if (initialDataReceived) return;
                console.warn('[DASHBOARD] No initial_data after 3s — using API fallback');
                await loadInitialFaces();
            }, 3000);
        };

        socket.onmessage = (event) => {
            let message;
            try { message = JSON.parse(event.data); }
            catch (e) { console.warn('[WS] Malformed frame rejected'); return; }
            try { handleWebSocketMessage(message); }
            catch (e) { console.error('[WS] Handler error:', e && e.message); }
        };

        socket.onclose = () => {
            updateConnectionStatus('disconnected');
            if (ws === socket) ws = null;
            if (intentionalWebSocketClose || destroyed) return;
            scheduleReconnect();
        };

        socket.onerror = () => { /* onclose follows */ };
    }

    function scheduleReconnect() {
        if (destroyed || intentionalWebSocketClose || reconnectTimer) return;
        if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
            showError('Live connection lost. Refresh the page to reconnect.');
            return;
        }
        reconnectAttempts += 1;
        // Bounded exponential backoff with ±30% jitter
        const base = Math.min(1000 * Math.pow(2, reconnectAttempts), 30000);
        const delay = Math.round(base * (0.7 + Math.random() * 0.6));
        reconnectTimer = trackTimeout(() => { reconnectTimer = null; connectWebSocket(); }, delay);
    }

    // ==================================================================
    // WebSocket message validation + handling
    // ==================================================================
    const processedEventIds = new Map();  // event_id -> ts (bounded FIFO)

    function alreadyProcessed(eventId) {
        if (!eventId) return false;
        if (processedEventIds.has(eventId)) return true;
        processedEventIds.set(eventId, Date.now());
        if (processedEventIds.size > MAX_PROCESSED_EVENT_IDS) {
            processedEventIds.delete(processedEventIds.keys().next().value);
        }
        return false;
    }

    const VALID_MESSAGE_TYPES = new Set([
        'config_changed', 'initial_data', 'new_detection', 'unknown_activity',
        'detection_alerts',
        'ping', 'pong', 'background_task_notification', 'background_task_completed',
        'live_alert_test',
    ]);

    function handleWebSocketMessage(message) {
        if (!message || typeof message !== 'object' || !VALID_MESSAGE_TYPES.has(message.type)) {
            return; // rejected safely (never log raw events — they may carry images)
        }
        switch (message.type) {
            case 'config_changed':
                // Same typed validation as the API path — zero stays valid
                if (message.config && typeof message.config === 'object') {
                    applyConfigPayload(message.config, 'websocket');
                    scheduleRender();
                }
                break;
            case 'initial_data':
                handleInitialData(message);
                break;
            case 'new_detection':
                handleNewDetection(message);
                break;
            case 'unknown_activity':
                handleUnknownActivity(message);
                break;
            case 'detection_alerts':
                handleDetectionAlerts(message);
                break;
            case 'ping':
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({ type: 'pong' }));
                }
                break;
            case 'background_task_notification':
                if (message.data && typeof message.data === 'object') showTaskNotification(message.data, false);
                break;
            case 'background_task_completed':
                if (message.data && typeof message.data === 'object') showTaskNotification(message.data, true);
                break;
            default:
                break;
        }
    }

    function handleInitialData(message) {
        initialDataReceived = true;  // a valid EMPTY payload still counts
        if (initialDataFallbackTimer) {
            clearTimeout(initialDataFallbackTimer);
            timers.delete(initialDataFallbackTimer);
            initialDataFallbackTimer = null;
        }

        faceStore.clear();
        detectionItemElements.forEach(node => node.remove());
        detectionItemElements.clear();

        unknownCounts.clear();
        if (message.unknown_counts && typeof message.unknown_counts === 'object') {
            Object.entries(message.unknown_counts).forEach(([pid, count]) => {
                const n = Number(count);
                if (typeof pid === 'string' && Number.isFinite(n) && n > 0) unknownCounts.set(pid, n);
            });
        }
        if (message.pipeline_display_names && typeof message.pipeline_display_names === 'object') {
            Object.entries(message.pipeline_display_names).forEach(([pid, name]) => {
                // Server initial_data names are DB-resolved — treat as approved
                if (typeof name === 'string' && name.trim()) approvedNames.set(pid, name.trim());
            });
        }

        let detections = [];
        if (Array.isArray(message.data)) detections = message.data;
        else if (message.data && Array.isArray(message.data.detections)) detections = message.data.detections;

        detections.forEach(detection => {
            if (!detection || typeof detection.pipeline_id !== 'string') return;
            if (!hasPipelineAccess(detection.pipeline_id)) return;
            const detectionTime = requireTimestamp(detection.timestamp, 'initial_data.timestamp');
            if (!detectionTime) return;  // invalid timestamps are skipped, not "now"
            ingestDetectionFaces(detection, detectionTime, { markSeen: true });
        });

        renderDashboard();
        // message.stats is ignored: stats rendering lives on /home now.
    }

    function ingestDetectionFaces(detection, detectionTime, { markSeen = false } = {}) {
        if (!Array.isArray(detection.faces)) return;
        detection.faces.forEach(face => {
            if (!face || typeof face.name !== 'string' || !face.name) return;
            if (face.name.toLowerCase() === 'unknown') return;
            ingestFaceSighting(detection.pipeline_id, face, detectionTime, Number(detection.processing_time_ms));
            if (markSeen) rememberFirstTime(detection.pipeline_id, face.name);
        });
    }

    function handleNewDetection(message) {
        const detection = message.data;
        if (!detection || typeof detection !== 'object') return;
        if (typeof detection.pipeline_id !== 'string' || !Array.isArray(detection.faces)) return;
        if (!hasPipelineAccess(detection.pipeline_id)) return;

        // Exact-event idempotency: NOTHING bypasses this — not even
        // is_new_detection (it only bypasses the person-level cooldown).
        const eventId = typeof detection.event_id === 'string' ? detection.event_id : null;
        if (alreadyProcessed(eventId)) return;

        const detectionTime = requireTimestamp(detection.timestamp, 'new_detection.timestamp');
        if (!detectionTime) return;

        recordReportedName(detection.pipeline_id, detection.location_name);
        ingestDetectionFaces(detection, detectionTime);
        scheduleRender();

        const face = detection.faces[0];
        if (!face || typeof face.name !== 'string') return;

        const isNewDetection = detection.is_new_detection === true;
        const wantAlert = isNewDetection || detection.should_show_alert !== false;
        if (!wantAlert) return;

        // Sound for a plain detection: only a genuinely new sighting. Live-alert
        // and watchlist sounds arrive with the PERSISTED `detection_alerts` event
        // (after the database commit) — never from this pre-commit payload.
        const shouldPlaySound = isNewDetection;

        showAdvancedAlert(detection, detectionTime, { bypassPersonCooldown: isNewDetection, playSound: shouldPlaySound });
        showRealtimeNotification(face.name, detection.pipeline_id, Number(face.similarity) || 0);
    }

    // `detection_alerts`: the persisted live-alert triggers and watchlist alerts
    // of one (detection, identity), broadcast only AFTER the rows committed.
    // Rows carry their ids; the event id is exact per detection+identity, so a
    // redelivery bumps nothing twice. Permission filtering applies exactly as
    // for detections (server-side pipeline filter + this client-side check).
    function handleDetectionAlerts(message) {
        const data = message.data;
        if (!data || typeof data !== 'object') return;
        if (typeof data.pipeline_id !== 'string' || !data.pipeline_id) return;
        if (!hasPipelineAccess(data.pipeline_id)) return;
        const eventId = typeof data.event_id === 'string' ? data.event_id : null;
        if (alreadyProcessed(eventId)) return;

        const liveAlerts = Array.isArray(data.live_alerts) ? data.live_alerts.filter(a => a && typeof a === 'object') : [];
        const watchlistAlerts = Array.isArray(data.watchlist_alerts) ? data.watchlist_alerts.filter(a => a && typeof a === 'object') : [];
        if (!liveAlerts.length && !watchlistAlerts.length) return;

        const wantSound = liveAlerts.some(a => a.sound_alert === true)
            || watchlistAlerts.some(a => a.notify_dashboard !== false);
        if (wantSound) playAlertSound();

        const name = typeof data.identity_name === 'string' && data.identity_name ? data.identity_name : 'Unknown person';
        const parts = [];
        if (watchlistAlerts.length) {
            const lists = watchlistAlerts.map(a => String(a.list_name || 'watchlist')).slice(0, 3).join(', ');
            parts.push(`Watchlist: ${lists}`);
        }
        if (liveAlerts.length) {
            const alerts = liveAlerts.map(a => String(a.alert_name || 'alert')).slice(0, 3).join(', ');
            parts.push(`Live alert: ${alerts}`);
        }
        showAlertNotification(name, data.pipeline_id, parts.join(' · '));
    }

    function showAlertNotification(name, pipelineId, detail) {
        const notification = document.getElementById('realtimeNotification');
        const nameEl = document.getElementById('notificationName');
        const pipeEl = document.getElementById('notificationPipeline');
        if (!notification || !nameEl || !pipeEl) return;
        nameEl.textContent = `${name} — ${detail}`;
        pipeEl.textContent = getPipelineDisplayName(pipelineId);
        notification.classList.add('show');
        trackTimeout(() => notification.classList.remove('show'), 5000);
    }

    function handleUnknownActivity(message) {
        const pid = message.pipeline_id;
        if (typeof pid !== 'string' || !pid) return;
        if (!hasPipelineAccess(pid)) return;
        if (alreadyProcessed(message.event_id)) return;  // duplicate deliveries bump once
        recordReportedName(pid, message.location_name);
        unknownCounts.set(pid, (unknownCounts.get(pid) || 0) + 1);
        const card = pipelineCardElements.get(pid);
        if (card) updateUnknownBadge(card, pid);
        else scheduleRender();
    }

    // ==================================================================
    // Alerts (full-screen overlay, history, cooldowns)
    // ==================================================================
    const alertHistory = [];
    const recentlyShownAlerts = new Map();  // person cooldown keys
    const firstTimeDetections = new Set();
    let alertTimestampInterval = null;

    function rememberFirstTime(pipelineId, name) {
        if (firstTimeDetections.size >= MAX_FIRST_TIME_KEYS) {
            firstTimeDetections.delete(firstTimeDetections.values().next().value);
        }
        firstTimeDetections.add(`${pipelineId} ${name}`);
    }

    function personCooldownAllows(pipelineId, name) {
        const firstKey = `${pipelineId} ${name}`;
        if (!firstTimeDetections.has(firstKey)) { rememberFirstTime(pipelineId, name); return true; }
        const key = `alert ${firstKey}`;
        const last = recentlyShownAlerts.get(key);
        if (last && Date.now() - last < ALERT_COOLDOWN_MS) return false;
        return true;
    }

    function markAlertShown(pipelineId, name) {
        if (recentlyShownAlerts.size >= MAX_RECENT_ALERT_KEYS) {
            recentlyShownAlerts.delete(recentlyShownAlerts.keys().next().value);
        }
        recentlyShownAlerts.set(`alert ${pipelineId} ${name}`, Date.now());
    }

    function sweepAlertCaches() {
        const now = Date.now();
        recentlyShownAlerts.forEach((ts, key) => {
            if (now - ts > Math.max(ALERT_COOLDOWN_MS, NOTIFICATION_COOLDOWN_MS)) recentlyShownAlerts.delete(key);
        });
        // Trim expired alert history (display-window scoped)
        let changed = false;
        for (let i = alertHistory.length - 1; i >= 0; i--) {
            if (now - alertHistory[i].detectionTime.getTime() > cfg.faceDisplayMs) {
                alertHistory.splice(i, 1);
                changed = true;
            }
        }
        if (changed) { renderAlertHistory(); updateAlertBadge(); }
    }

    function showAdvancedAlert(detection, detectionTime, { bypassPersonCooldown = false, playSound = false } = {}) {
        const face = detection.faces && detection.faces[0];
        if (!face || typeof face.name !== 'string') return;

        if (!bypassPersonCooldown && !personCooldownAllows(detection.pipeline_id, face.name)) return;
        markAlertShown(detection.pipeline_id, face.name);

        populateAlertOverlay({
            name: face.name,
            image: validImage(face.image),
            pipelineId: detection.pipeline_id,
            similarity: Number(face.similarity) || 0,
            detectionTime,
        });

        if (playSound) playAlertSound();

        const backdrop = document.getElementById('alertBackdrop');
        if (backdrop) backdrop.classList.add('show');
        const overlay = document.getElementById('alertOverlay');
        if (overlay) overlay.classList.add('show');

        alertHistory.unshift({
            name: face.name,
            pipelineId: detection.pipeline_id,
            similarity: Number(face.similarity) || 0,
            image: validImage(face.image),
            detectionTime,
        });
        if (alertHistory.length > MAX_ALERT_HISTORY) alertHistory.length = MAX_ALERT_HISTORY;
        renderAlertHistory();
        updateAlertBadge();
    }

    function populateAlertOverlay({ name, image, pipelineId, similarity, detectionTime }) {
        const nameEl = document.getElementById('alertPersonName');
        if (nameEl) nameEl.replaceChildren(icon('fa-user-check'), el('span', null, ' ' + name));

        const img = document.getElementById('alertImagePreview');
        if (img) {
            if (image) { img.src = 'data:image/jpeg;base64,' + image; img.style.display = 'block'; }
            else img.style.display = 'none';
        }
        const pipeEl = document.getElementById('alertPipelineValue');
        if (pipeEl) pipeEl.textContent = getPipelineDisplayName(pipelineId);
        const simEl = document.getElementById('alertSimilarityValue');
        if (simEl) simEl.textContent = `${(similarity * 100).toFixed(1)}%`;

        const tsEl = document.getElementById('alertTimestampValue');
        if (tsEl) {
            const update = () => {
                tsEl.replaceChildren(
                    el('span', 'alert-ts-label', 'DETECTED: '),
                    el('span', 'alert-ts-time', `${formatBeirut(detectionTime)} (Beirut)`),
                    el('br'),
                    el('span', 'alert-ts-ago', formatAge(detectionTime)),
                );
            };
            update();
            if (alertTimestampInterval) { clearInterval(alertTimestampInterval); timers.delete(alertTimestampInterval); }
            alertTimestampInterval = trackInterval(update, 1000);
        }
    }

    function closeAdvancedAlert() {
        const overlay = document.getElementById('alertOverlay');
        if (overlay) overlay.classList.remove('show');
        const backdrop = document.getElementById('alertBackdrop');
        if (backdrop) backdrop.classList.remove('show');
        if (alertTimestampInterval) {
            clearInterval(alertTimestampInterval);
            timers.delete(alertTimestampInterval);
            alertTimestampInterval = null;
        }
    }

    function renderAlertHistory() {
        const container = document.getElementById('alertHistoryContent');
        if (!container) return;
        if (alertHistory.length === 0) {
            const empty = el('div', 'no-data');
            empty.appendChild(el('p', null, 'No detection history'));
            container.replaceChildren(empty);
            return;
        }
        const items = alertHistory.map((entry, index) => {
            const item = el('div', 'alert-history-item');
            item.dataset.index = String(index);
            item.tabIndex = 0;
            item.setAttribute('role', 'button');
            const header = el('div', 'alert-history-item-header');
            header.appendChild(el('div', 'alert-history-name', entry.name));
            header.appendChild(el('div', 'alert-history-time', formatBeirut(entry.detectionTime)));
            item.appendChild(header);
            item.appendChild(el('div', 'alert-history-details',
                `${getPipelineDisplayName(entry.pipelineId)} • ${(entry.similarity * 100).toFixed(1)}%`));
            return item;
        });
        container.replaceChildren(...items);
    }

    function updateAlertBadge() {
        const badge = document.getElementById('alertBadge');
        if (!badge) return;
        const active = alertHistory.filter(a => Date.now() - a.detectionTime.getTime() < cfg.faceDisplayMs).length;
        badge.textContent = String(active);
        badge.style.display = active > 0 ? 'flex' : 'none';
    }

    function toggleAlertHistory() {
        const panel = document.getElementById('alertHistoryPanel');
        if (panel) panel.classList.toggle('show');
    }

    function replayHistoryEntry(index) {
        const entry = alertHistory[index];
        if (!entry) return;
        populateAlertOverlay({
            name: entry.name, image: entry.image, pipelineId: entry.pipelineId,
            similarity: entry.similarity, detectionTime: entry.detectionTime,
        });
        const backdrop = document.getElementById('alertBackdrop');
        if (backdrop) backdrop.classList.add('show');
        const overlay = document.getElementById('alertOverlay');
        if (overlay) overlay.classList.add('show');
        const panel = document.getElementById('alertHistoryPanel');
        if (panel) panel.classList.remove('show');
    }

    function showRealtimeNotification(name, pipelineId, similarity) {
        const key = `notif ${pipelineId} ${name}`;
        const last = recentlyShownAlerts.get(key);
        if (last && Date.now() - last < NOTIFICATION_COOLDOWN_MS) return;
        recentlyShownAlerts.set(key, Date.now());

        const notification = document.getElementById('realtimeNotification');
        const nameEl = document.getElementById('notificationName');
        const pipeEl = document.getElementById('notificationPipeline');
        if (!notification || !nameEl || !pipeEl) return;
        nameEl.textContent = `${name} detected!`;
        pipeEl.textContent = `${getPipelineDisplayName(pipelineId)} • ${(similarity * 100).toFixed(1)}% match`;
        notification.classList.add('show');
        trackTimeout(() => notification.classList.remove('show'), 3000);
    }

    // ==================================================================
    // Sound (shared AudioContext behind explicit opt-in)
    // ==================================================================
    const SOUND_PREF_KEY = 'dashboardAlertSoundEnabled';
    let audioContext = null;
    let soundEnabled = false;

    function soundState() {
        if (!(window.AudioContext || window.webkitAudioContext)) return 'unsupported';
        if (!soundEnabled) return 'disabled';
        if (audioContext && audioContext.state === 'running') return 'enabled';
        if (audioContext && audioContext.state !== 'running') return 'blocked';
        return 'disabled';
    }

    function updateSoundButton() {
        const btn = document.getElementById('sound-toggle-btn');
        if (!btn) return;
        const state = soundState();
        const label = btn.querySelector('span');
        const text = state === 'enabled' ? 'Sound: Enabled'
            : state === 'blocked' ? 'Sound: Blocked by browser'
            : state === 'unsupported' ? 'Sound: Unsupported'
            : 'Enable Alert Sound';
        if (label) label.textContent = text;
        btn.classList.toggle('sound-on', state === 'enabled');
        btn.setAttribute('aria-pressed', soundEnabled ? 'true' : 'false');
    }

    function ensureAudioContext() {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return null;
        if (!audioContext) audioContext = new Ctx();
        return audioContext;
    }

    async function toggleSound() {
        soundEnabled = !soundEnabled;
        try { localStorage.setItem(SOUND_PREF_KEY, soundEnabled ? '1' : '0'); } catch (e) { /* ignore */ }
        if (soundEnabled) {
            const ctx = ensureAudioContext();
            if (ctx) {
                try { if (ctx.state === 'suspended') await ctx.resume(); } catch (e) { /* state check below */ }
                if (ctx.state === 'running') playTone(ctx); // test tone during the gesture
            }
        } else if (audioContext) {
            try { audioContext.suspend(); } catch (e) { /* ignore */ }
        }
        updateSoundButton();
    }

    function playTone(ctx) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.frequency.value = 880;
        osc.type = 'sine';
        gain.gain.setValueAtTime(0.3, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.5);
        osc.start(ctx.currentTime);
        osc.stop(ctx.currentTime + 0.5);
    }

    function playAlertSound() {
        if (!soundEnabled) return;
        const ctx = ensureAudioContext();
        if (!ctx || ctx.state !== 'running') { updateSoundButton(); return; } // honestly blocked
        try { playTone(ctx); } catch (e) { /* never claim delivery */ }
    }

    // ==================================================================
    // Background-task notifications (safe DOM, deduplicated)
    // ==================================================================
    const shownTaskNotifications = new Set();

    function showTaskNotification(data, isCompletion) {
        const dedupeKey = `${data.task_type || '?'} ${isCompletion ? 'done' : 'up'} ${data.scheduled_timestamp || data.completed_at || data.scheduled_time || ''}`;
        if (shownTaskNotifications.has(dedupeKey)) return;  // same event after reconnect
        if (shownTaskNotifications.size >= MAX_TASK_NOTIFICATION_IDS) {
            shownTaskNotifications.delete(shownTaskNotifications.values().next().value);
        }
        shownTaskNotifications.add(dedupeKey);

        const isError = isCompletion && data.success === false;
        const alertBox = el('div', `background-task-alert ${isCompletion ? 'completion' : 'upcoming'} ${isError ? 'error' : ''}`);

        const header = el('div', 'alert-header');
        header.appendChild(icon(isCompletion ? (isError ? 'fa-exclamation-circle' : 'fa-check-circle') : 'fa-clock'));
        const title = isCompletion
            ? `${data.task_name || 'Background Task'} ${isError ? 'failed' : 'completed'}`
            : `${data.task_name || 'Background Task'} starting soon`;
        header.appendChild(el('h3', null, title));
        const closeBtn = el('button', 'alert-close');
        closeBtn.type = 'button';
        closeBtn.setAttribute('aria-label', 'Dismiss notification');
        closeBtn.appendChild(icon('fa-times'));
        closeBtn.addEventListener('click', () => alertBox.remove());
        header.appendChild(closeBtn);
        alertBox.appendChild(header);

        const body = el('div', 'alert-body');
        body.appendChild(el('p', 'alert-message', isCompletion
            ? (isError ? 'Task encountered an error' : `Completed in ${data.duration || 'unknown time'}`)
            : (data.description || 'A background task is about to start')));
        if (!isCompletion && data.scheduled_time) {
            body.appendChild(el('div', 'alert-time', `⏰ ${data.scheduled_time}`));
        }
        if (data.details && typeof data.details === 'object') {
            const pre = el('pre', 'alert-details');
            try { pre.textContent = JSON.stringify(data.details, null, 2).slice(0, 2000); }
            catch (e) { pre.textContent = '(details unavailable)'; }
            body.appendChild(pre);
        }
        const link = el('a', 'alert-task-link', 'View background tasks →');
        link.href = '/admin/background-tasks';
        body.appendChild(link);
        alertBox.appendChild(body);

        let container = document.getElementById('background-task-alerts');
        if (!container) {
            container = el('div', 'background-task-alerts-container');
            container.id = 'background-task-alerts';
            document.body.appendChild(container);
        }
        container.appendChild(alertBox);
        trackTimeout(() => { if (alertBox.parentElement) alertBox.remove(); }, isCompletion ? 10000 : 70000);
    }

    // ==================================================================
    // Stats: NOT here. /home owns every stats-API figure (home.js);
    // this page renders only what arrives over the live socket.
    // ==================================================================

    // ==================================================================
    // Initial-face API fallback (only when WS initial_data truly failed)
    // ==================================================================
    async function loadInitialFaces() {
        if (!access.loaded || access.mode !== 'restricted') return; // detections API is per-pipeline
        for (const pipelineId of access.pipelineIds.slice(0, 10)) {
            const result = await api(`/api/detections/${encodeURIComponent(pipelineId)}?limit=50`);
            if (!result.ok || !result.payload || !Array.isArray(result.payload.detections)) continue;
            result.payload.detections.forEach(det => {
                const t = requireTimestamp(det.timestamp, 'fallback.timestamp');
                if (!t) return;
                ingestDetectionFaces({
                    pipeline_id: det.pipeline_id,
                    faces: (det.faces || []).map(f => ({ name: f.name, similarity: f.similarity, image: null })),
                    processing_time_ms: det.processing_time_ms,
                }, t, { markSeen: true });
            });
        }
        scheduleRender();
    }

    // ==================================================================
    // Identity view helper (kept as global — other pages may call it)
    // ==================================================================
    async function viewKnownPersonIdentity(faceName) {
        try {
            const result = await api(`/api/admin/identities/search?query=${encodeURIComponent(faceName)}&limit=1`);
            if (!result.ok) throw new Error('Search failed');
            const data = result.payload;
            if (!data || !data.results || data.results.length === 0) {
                showError(`Identity "${faceName}" not found.`);
                return;
            }
            const identity = data.results[0];
            if (window.viewIdentityDetails) window.viewIdentityDetails(identity.id);
            else window.location.href = `/admin/identity/${encodeURIComponent(identity.id)}?from=${encodeURIComponent(window.location.pathname)}`;
        } catch (e) {
            showError(`Error loading identity details: ${e && e.message}`);
        }
    }
    window.viewKnownPersonIdentity = viewKnownPersonIdentity;

    // ==================================================================
    // Errors / init banner
    // ==================================================================
    function showError(message) {
        const errorEl = document.getElementById('errorMessage');
        const errorText = document.getElementById('errorText');
        if (!errorEl || !errorText) { console.error('[DASHBOARD]', message); return; }
        errorText.textContent = message;
        errorEl.classList.add('show');
    }

    function hideError() {
        const errorEl = document.getElementById('errorMessage');
        if (errorEl) errorEl.classList.remove('show');
    }

    // ==================================================================
    // Add Person: handled entirely by the shared component
    // (upload-modal-loader.js injects components/upload-modal.html and
    // upload-modal.js, which registers openUploadModal and does the POST).
    // The ~120-line copy that lived here fed an inline #uploadModal that
    // DUPLICATED the injected one's id. The dead #imageModal handlers
    // went with it — nothing ever opened that modal.
    // ==================================================================

    window.logout = function () { window.location.href = '/signin'; };

    // ==================================================================
    // Event wiring (delegation — no inline handlers anywhere)
    // ==================================================================
    function setupEventListeners() {
        // Pipeline grid: image click = alert overlay; dblclick/name click = identity
        const grid = document.getElementById('pipelineGrid');
        if (grid) {
            grid.addEventListener('click', (e) => {
                const img = e.target.closest('.detection-image');
                if (img) {
                    const item = img.closest('.detection-item');
                    if (!item) return;
                    const pid = item.dataset.pipeline;
                    const name = item.dataset.face;
                    const entry = faceStore.get(pid) && faceStore.get(pid).get(name);
                    if (entry) {
                        populateAlertOverlay({
                            name, image: entry.best_image, pipelineId: pid,
                            similarity: entry.best_similarity, detectionTime: entry.latest_detection_at,
                        });
                        const backdrop = document.getElementById('alertBackdrop');
                        if (backdrop) backdrop.classList.add('show');
                        const overlay = document.getElementById('alertOverlay');
                        if (overlay) overlay.classList.add('show');
                    }
                    return;
                }
                const nameEl = e.target.closest('.face-name');
                if (nameEl) {
                    const item = nameEl.closest('.detection-item');
                    if (item && item.dataset.face) viewKnownPersonIdentity(item.dataset.face);
                }
            });
            grid.addEventListener('dblclick', (e) => {
                const img = e.target.closest('.detection-image');
                if (!img) return;
                const item = img.closest('.detection-item');
                if (item && item.dataset.face) viewKnownPersonIdentity(item.dataset.face);
            });
        }

        // Alert overlay / history / toggle buttons
        bindClick('alert-ack-btn', closeAdvancedAlert);
        bindClick('alert-history-btn', () => { toggleAlertHistory(); closeAdvancedAlert(); });
        bindClick('alert-history-close-btn', toggleAlertHistory);
        bindClick('alert-toggle-btn', toggleAlertHistory);
        const backdrop = document.getElementById('alertBackdrop');
        if (backdrop) backdrop.addEventListener('click', closeAdvancedAlert);

        const historyContent = document.getElementById('alertHistoryContent');
        if (historyContent) {
            historyContent.addEventListener('click', (e) => {
                const item = e.target.closest('.alert-history-item');
                if (item) replayHistoryEntry(parseInt(item.dataset.index, 10));
            });
        }

        // Sound opt-in
        bindClick('sound-toggle-btn', toggleSound);

        // Keyboard shortcuts
        document.addEventListener('keydown', onKeydown);

        // Visibility: reconcile immediately on return
        document.addEventListener('visibilitychange', onVisibilityChange);

        window.addEventListener('pagehide', destroy);
        window.addEventListener('beforeunload', destroy);
    }

    function bindClick(id, handler) {
        const node = document.getElementById(id);
        if (node) node.addEventListener('click', handler);
    }

    function onKeydown(e) {
        if (e.key === 'Escape') {
            closeAdvancedAlert();
            const panel = document.getElementById('alertHistoryPanel');
            if (panel) panel.classList.remove('show');
        } else if (e.key === 'r' && e.ctrlKey) {
            e.preventDefault();
            // Stats are gone from this page; refresh means "reconcile the feed".
            sweepExpiredFaces();
            refreshPipelineDisplayNames();
            scheduleRender();
        }
    }

    function onVisibilityChange() {
        if (!document.hidden) {
            // Reconcile everything missed while hidden
            sweepExpiredFaces();
            scheduleRender();
            refreshPipelineDisplayNames();
            if (!ws) connectWebSocket();
        }
    }

    function destroy() {
        if (destroyed) return;
        destroyed = true;
        intentionalWebSocketClose = true;      // never reconnect after page shutdown
        if (ws) { try { ws.close(1000); } catch (e) { /* ignore */ } ws = null; }
        for (const id of timers) { clearTimeout(id); clearInterval(id); }
        timers.clear();
        for (const c of controllers) { try { c.abort(); } catch (e) { /* ignore */ } }
        controllers.clear();
        if (audioContext) { try { audioContext.close(); } catch (e) { /* ignore */ } audioContext = null; }
        document.removeEventListener('keydown', onKeydown);
        document.removeEventListener('visibilitychange', onVisibilityChange);
    }

    // ==================================================================
    // Ordered initialization (config + names + access BEFORE the socket)
    // ==================================================================
    let schedulersStarted = false;
    function startCleanupSchedulers() {
        if (schedulersStarted) return;  // never duplicated on re-init
        schedulersStarted = true;
        trackInterval(sweepExpiredFaces, FACE_EXPIRY_SWEEP_MS);
        trackInterval(sweepAlertCaches, ALERT_CACHE_CLEANUP_MS);
        trackInterval(tickCountdowns, 1000);
        trackInterval(() => { if (!document.hidden) refreshPipelineDisplayNames(); }, NAME_RECONCILE_MS);
    }

    let initialized = false;
    async function initializeDashboard() {
        if (initialized) return;   // duplicate startup handlers must not double-connect
        initialized = true;

        try { soundEnabled = localStorage.getItem(SOUND_PREF_KEY) === '1'; } catch (e) { /* ignore */ }
        updateSoundButton();
        setupEventListeners();

        const [configOk] = await Promise.all([
            loadDashboardConfig(),
            refreshPipelineDisplayNames(),
            loadUserPipelineAccess(),
        ]);

        if (!configOk) {
            showError('Dashboard configuration could not be loaded — running with safe defaults.');
        }

        startCleanupSchedulers();
        connectWebSocket();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initializeDashboard);
    } else {
        initializeDashboard();
    }
})();
