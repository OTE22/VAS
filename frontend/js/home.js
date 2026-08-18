// ---------------------------------------------------------------------------
// Home page — system dashboard.
//
// Contracts this file is pinned to:
//   * CSP script-src 'self': no inline handlers, no eval, no javascript: URLs.
//     Every interaction is data-action + Actions.register (actions.js).
//   * Backend values reach the DOM through textContent ONLY. There is no
//     innerHTML in this file, and the one place a class attribute is assigned
//     reads from a literal five-key map keyed by OUR OWN status values — never
//     by an API string, which would be class-attribute injection.
//   * Admin styling is toggled from the enforced capability list, never added
//     unconditionally, and both the show AND hide branches exist.
// ---------------------------------------------------------------------------
'use strict';

// --- logging ---------------------------------------------------------------
// Production console must be clean. The stats payload is never logged
// unconditionally; console.warn/error survive only for genuinely unexpected
// failures, not for an expected non-ok response.
const DEBUG_HOME = false;

function homeDebug() {
    if (DEBUG_HOME) {
        console.debug.apply(console, ['[home]'].concat([].slice.call(arguments)));
    }
}

// --- constants -------------------------------------------------------------
const STATS_TIMEOUT_MS = 10000;   // /api/stats aggregates a pool read + a storage walk
const STATS_POLL_MS = 30000;
const STALE_AFTER_MS = 90000;
const HEARTBEAT_MS = 15000;
const CLOCK_SKEW_TOLERANCE_MS = 300000;
const EM_DASH = '—';
const NOT_REPORTED = 'Not reported by the service';

const ERROR_COPY = {
    timeout: 'The service did not respond within 10 seconds.',
    network: 'The service could not be reached.',
    auth: 'Your session is no longer authorised for system statistics.',
    http: 'The service returned an error.'
};

// Literal lookup. `status` is always one of our own five values.
const STATUS_ICON = {
    ok: 'fas fa-circle-check',
    warn: 'fas fa-triangle-exclamation',
    crit: 'fas fa-circle-exclamation',
    idle: 'fas fa-circle-minus',
    unknown: 'fas fa-circle-question'
};

const KPI_CARDS = [
    ['kpi-pipelines', 'kpi-pipelines-value', 'kpi-pipelines-note'],
    ['kpi-detections', 'kpi-detections-value', 'kpi-detections-note'],
    ['kpi-faces', 'kpi-faces-value', 'kpi-faces-note'],
    ['kpi-queue', 'kpi-queue-value', 'kpi-queue-note'],
    ['kpi-processed', 'kpi-processed-value', 'kpi-processed-note'],
    ['kpi-storage', 'kpi-storage-value', 'kpi-storage-note'],
    ['kpi-cache', 'kpi-cache-value', 'kpi-cache-note'],
    ['kpi-tracker', 'kpi-tracker-value', 'kpi-tracker-note']
];

const DATA_PANELS = ['pipelines-section', 'queue-panel', 'storage-panel', 'tracker-panel'];

const DATA_FIELDS = [
    'pl-active', 'pl-detections',
    'q-size', 'q-max', 'q-processing', 'q-received', 'q-processed', 'q-skipped',
    'st-used', 'st-max', 'st-files', 'st-used-gb', 'st-retention',
    'tk-pipelines', 'tk-faces', 'tk-added', 'tk-skipped', 'tk-duplicates',
    'tk-window', 'tk-notify', 'tk-memory'
];

const HEALTH_SUBSYSTEMS = ['api', 'db', 'cache', 'tracker', 'queue', 'storage'];

// --- module state ----------------------------------------------------------
let currentUser = null;
let inFlight = null;
let lastGoodAt = null;
let lastGoodStamp = null;
let lastAttemptAt = 0;
let heartbeat = null;
let errorShown = false;

// ---------------------------------------------------------------------------
// Safe accessors — the zero-vs-absent distinction
//
// The explicit null/'' guard is load-bearing: Number(null) === 0 and
// Number('') === 0, so the naive version turns a field the service never
// reported into a confident "0".
// ---------------------------------------------------------------------------
function safeGet(obj, path) {
    return String(path).split('.').reduce(
        (acc, key) => (acc !== null && acc !== undefined) ? acc[key] : undefined, obj);
}

function safeNumber(obj, path) {
    const raw = safeGet(obj, path);
    if (raw === null || raw === undefined || raw === '') return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
}

// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------
function formatInt(n) {
    if (n === null || n === undefined || !Number.isFinite(Number(n))) return EM_DASH;
    return Number(n).toLocaleString();
}

function trimDecimals(text) {
    if (text.indexOf('.') === -1) return text;
    return text.replace(/0+$/, '').replace(/\.$/, '');
}

// usage_percent is ALREADY a percentage — never multiply by 100. The
// "< 0.01%" branch is tested before any rounding, and a real 0 must still
// print "0%".
function formatPercent(n) {
    if (n === null || n === undefined || !Number.isFinite(n)) return EM_DASH;
    if (n <= 0) return '0%';
    if (n < 0.01) return '< 0.01%';
    if (n < 1) return n.toFixed(2) + '%';
    if (n < 10) return n.toFixed(1) + '%';
    return Math.round(n) + '%';
}

function formatStorage(mb, gb) {
    if (mb !== null && Number.isFinite(mb) && mb < 1024) return mb.toFixed(2) + ' MB';
    if (gb !== null && Number.isFinite(gb)) return gb.toFixed(2) + ' GB';
    if (mb !== null && Number.isFinite(mb)) return (mb / 1024).toFixed(2) + ' GB';
    return EM_DASH;
}

function formatGB(n) {
    if (n === null || n === undefined || !Number.isFinite(n)) return EM_DASH;
    if (n > 0 && n < 0.01) return '< 0.01 GB';
    if (n < 1) return trimDecimals(n.toFixed(4)) + ' GB';
    return formatInt(Math.round(n * 100) / 100) + ' GB';
}

function formatMB(n) {
    if (n === null || n === undefined || !Number.isFinite(n)) return EM_DASH;
    return n.toFixed(1) + ' MB';
}

function plural(value, unit) {
    return value + ' ' + unit + (value === 1 ? '' : 's');
}

function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return EM_DASH;
    if (seconds <= 0) return 'disabled';
    if (seconds < 60) return Math.round(seconds) + ' sec';
    if (seconds < 3600) {
        const minutes = Math.floor(seconds / 60);
        const rest = Math.round(seconds % 60);
        return rest === 0 ? minutes + ' min' : minutes + ' min ' + rest + ' sec';
    }
    if (seconds < 86400) {
        const hours = Math.floor(seconds / 3600);
        const restMin = Math.floor((seconds % 3600) / 60);
        const head = plural(hours, 'hour');
        return restMin === 0 ? head : head + ' ' + restMin + ' min';
    }
    const days = Math.floor(seconds / 86400);
    const restHours = Math.floor((seconds % 86400) / 3600);
    const headDays = plural(days, 'day');
    return restHours === 0 ? headDays : headDays + ' ' + restHours + ' hr';
}

function formatRelative(value) {
    const ms = typeof value === 'number' ? value : Date.parse(value);
    if (!Number.isFinite(ms)) return EM_DASH;
    const diff = Date.now() - ms;
    if (diff < 10000) return 'just now';
    const sec = Math.floor(diff / 1000);
    if (sec < 60) return sec + ' sec ago';
    const min = Math.floor(sec / 60);
    if (min < 60) return min + ' min ago';
    const hours = Math.floor(min / 60);
    if (hours < 24) return plural(hours, 'hour') + ' ago';
    return new Date(ms).toLocaleDateString();
}

function prefersReducedMotion() {
    return typeof window.matchMedia === 'function' &&
        window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

// ---------------------------------------------------------------------------
// DOM writers — the only ways data reaches the page
// ---------------------------------------------------------------------------
function setText(id, text) {
    const el = document.getElementById(id);
    if (!el) return;
    const next = (text === null || text === undefined) ? EM_DASH : String(text);
    if (el.textContent !== next) el.textContent = next;
}

function setState(id, state) {
    const el = document.getElementById(id);
    if (el) el.setAttribute('data-state', state);
}

function setStatus(rootId, labelId, status, label) {
    const root = document.getElementById(rootId);
    if (!root) return;
    root.setAttribute('data-status', status);
    const icon = root.querySelector('.hp-status__icon');
    if (icon) icon.className = 'hp-status__icon ' + STATUS_ICON[status];
    if (labelId) setText(labelId, label);
}

function setMeter(id, pct, valuetext) {
    const el = document.getElementById(id);
    if (!el) return;
    if (pct === null || pct === undefined || !Number.isFinite(pct)) {
        el.setAttribute('data-state', 'unavailable');
        el.style.setProperty('--hp-fill', '0%');
        el.removeAttribute('aria-valuenow');
        el.setAttribute('aria-valuetext', valuetext || 'Not reported');
        return;
    }
    const clamped = Math.max(0, Math.min(100, pct));
    el.setAttribute('data-state', clamped === 0 ? 'empty' : 'ready');
    el.style.setProperty('--hp-fill', clamped + '%');
    el.setAttribute('aria-valuenow', String(Math.round(clamped * 100) / 100));
    if (valuetext) el.setAttribute('aria-valuetext', valuetext);
}

// A callout is an authored sentence derived client-side; passing null hides it.
function setCallout(id, status, text, iconClass) {
    const el = document.getElementById(id);
    if (!el) return;
    if (text === null || text === undefined) {
        if (!el.hidden) el.hidden = true;
        return;
    }
    el.setAttribute('data-status', status);
    const icon = el.querySelector('.hp-callout__icon');
    if (icon) icon.className = 'hp-callout__icon ' + (iconClass || STATUS_ICON[status]);
    const body = el.querySelector('.hp-callout__text');
    if (body && body.textContent !== text) body.textContent = text;
    if (el.hidden) el.hidden = false;
}

// ---------------------------------------------------------------------------
// Freshness / timestamps
//
// The service timestamp carries no timezone offset, so it cannot be trusted to
// measure elapsed time across a clock-skewed or differently-zoned client. It is
// used when it agrees with our receipt time and ignored when it does not; the
// absolute title/datetime always come from the reported value when parseable.
// ---------------------------------------------------------------------------
function effectiveUpdatedMillis() {
    if (lastGoodAt === null) return null;
    const parsed = lastGoodStamp === null ? NaN : Date.parse(lastGoodStamp);
    if (Number.isFinite(parsed) && Math.abs(lastGoodAt - parsed) <= CLOCK_SKEW_TOLERANCE_MS) {
        return parsed;
    }
    return lastGoodAt;
}

function renderUpdatedTimes() {
    const ms = effectiveUpdatedMillis();
    const text = ms === null ? 'never' : formatRelative(ms);
    const stamp = ms === null ? null : new Date(ms);
    ['hp-updated', 'hp-error-last'].forEach(function (id) {
        const el = document.getElementById(id);
        if (!el) return;
        if (el.textContent !== text) el.textContent = text;
        if (stamp) {
            el.setAttribute('datetime', stamp.toISOString());
            el.title = stamp.toLocaleString();
        } else {
            el.setAttribute('datetime', '');
            el.removeAttribute('title');
        }
    });
}

function renderFreshness() {
    const badge = document.getElementById('hp-stale');
    if (lastGoodAt === null) {
        document.body.setAttribute('data-freshness', 'never');
        if (badge && !badge.hidden) badge.hidden = true;
        return;
    }
    const stale = (Date.now() - lastGoodAt) > STALE_AFTER_MS;
    document.body.setAttribute('data-freshness', stale ? 'stale' : 'fresh');
    if (badge && badge.hidden === stale) badge.hidden = !stale;
}

function renderConnection() {
    setText('hp-error-conn', 'Connection: ' + (navigator.onLine === false ? 'offline' : 'online'));
}

// ---------------------------------------------------------------------------
// Section renderers
// ---------------------------------------------------------------------------
function renderCountKpi(cardId, valueId, noteId, value, readyNote, emptyNote) {
    const state = value === null ? 'unavailable' : (value === 0 ? 'empty' : 'ready');
    setState(cardId, state);
    setText(valueId, formatInt(value));
    if (state === 'unavailable') setText(noteId, NOT_REPORTED);
    else if (state === 'empty') setText(noteId, emptyNote);
    else setText(noteId, readyNote);
}

function renderApiHealth() {
    setStatus('health-api', 'health-api-state', 'ok', 'Operational');
    setText('health-api-detail', 'Stats endpoint responded');
    return 'ok';
}

// Never key a status off a specific field name, and never render 0 for an
// empty object: a reporting gap must not look like a healthy idle pool.
function renderDatabaseHealth(stats) {
    const db = safeGet(stats, 'database');
    const keys = (db && typeof db === 'object' && !Array.isArray(db)) ? Object.keys(db) : [];
    if (keys.length === 0) {
        setStatus('health-db', 'health-db-state', 'unknown', 'No pool data');
        setText('health-db-detail', 'Service reported no pool statistics');
        return 'unknown';
    }
    const parts = [];
    for (let i = 0; i < keys.length && parts.length < 4; i += 1) {
        const value = Number(db[keys[i]]);
        if (Number.isFinite(value)) {
            parts.push(keys[i].replace(/_/g, ' ') + ' ' + formatInt(value));
        }
    }
    setStatus('health-db', 'health-db-state', 'ok', 'Reporting');
    setText('health-db-detail', parts.length ? parts.join(' · ') : 'Pool statistics available');
    return 'ok';
}

function renderCache(stats) {
    const cache = safeGet(stats, 'cache');
    const enabled = cache && typeof cache === 'object' ? cache.enabled : undefined;
    const healthy = cache && typeof cache === 'object' ? cache.healthy : undefined;

    let status = 'unknown';
    let label = 'Not reported';
    let detail = 'Service reported no cache statistics';
    let note = NOT_REPORTED;
    let cardState = 'unavailable';

    if (enabled === false) {
        status = 'idle';
        label = 'Disabled';
        detail = 'Caching is switched off';
        note = 'Caching is switched off for this deployment';
        cardState = 'empty';
    } else if (enabled === true) {
        cardState = 'ready';
        if (healthy === true) {
            status = 'ok'; label = 'Healthy';
            detail = 'Enabled and responding';
            note = 'Enabled and responding';
        } else if (healthy === false) {
            status = 'crit'; label = 'Unhealthy';
            detail = 'Enabled but the health check is failing';
            note = 'Enabled, health check failing';
        } else {
            label = 'Unknown';
            detail = 'Enabled — health not reported';
            note = 'Enabled — health not reported';
        }
    }

    setStatus('health-cache', 'health-cache-state', status, label);
    setText('health-cache-detail', detail);
    setState('kpi-cache', cardState);
    setStatus('kpi-cache', 'kpi-cache-value', status, label);
    setText('kpi-cache-note', note);
    return status;
}

function renderTracker(stats) {
    const enabled = safeGet(stats, 'tracker.enabled');
    const active = safeNumber(stats, 'tracker.active_pipelines');
    const faces = safeNumber(stats, 'tracker.total_tracked_faces');
    const windowSeconds = safeNumber(stats, 'tracker.window_seconds');
    const memUsed = safeNumber(stats, 'tracker.memory_usage_mb');
    const memLimit = safeNumber(stats, 'tracker.memory_limit_mb');

    let status = 'unknown';
    let label = 'Not reported';
    if (enabled === true) { status = 'ok'; label = 'Active'; }
    else if (enabled === false) { status = 'idle'; label = 'Disabled'; }

    setState('tracker-panel', status === 'unknown' ? 'unavailable' : 'ready');
    setStatus('tracker-panel', 'tk-state', status, label);

    setText('tk-pipelines', formatInt(active));
    setText('tk-faces', formatInt(faces));
    setText('tk-added', formatInt(safeNumber(stats, 'tracker.total_faces_added')));
    setText('tk-skipped', formatInt(safeNumber(stats, 'tracker.total_faces_skipped')));
    setText('tk-duplicates', formatInt(safeNumber(stats, 'tracker.total_duplicates_prevented')));
    setText('tk-window', formatDuration(windowSeconds));
    setText('tk-notify', formatDuration(safeNumber(stats, 'tracker.frontend_notification_window_seconds')));

    if (memUsed === null || memLimit === null || memLimit <= 0) {
        setText('tk-memory', memUsed === null ? EM_DASH : formatMB(memUsed));
        setMeter('tk-mem-meter', null, 'Tracker memory limit not reported');
    } else {
        const memPct = (memUsed / memLimit) * 100;
        const memSentence = formatMB(memUsed) + ' of ' + formatInt(memLimit) + ' MB';
        setText('tk-memory', memSentence);
        setMeter('tk-mem-meter', memPct, memSentence + ' used (' + formatPercent(memPct) + ')');
    }

    let detail = 'Subsystem state not reported';
    if (status !== 'unknown') {
        const bits = [formatInt(active) + ' active'];
        if (memUsed !== null) bits.push(formatMB(memUsed) + ' in use');
        detail = bits.join(' · ');
    }
    setStatus('health-tracker', 'health-tracker-state', status, label);
    setText('health-tracker-detail', detail);

    setState('kpi-tracker', status === 'unknown' ? 'unavailable' : (enabled === false ? 'empty' : 'ready'));
    setStatus('kpi-tracker', 'kpi-tracker-value', status, label);
    setText('kpi-tracker-note', status === 'unknown'
        ? NOT_REPORTED
        : formatInt(faces) + ' faces in the current window');

    // An all-zero tracker is the system's normal resting state, stated
    // positively: idle tone and a check icon, never a warning.
    if (enabled === true && active === 0 && faces === 0) {
        setCallout('tk-empty', 'idle',
            'Tracker is running — no faces in the current ' +
            formatDuration(windowSeconds) + ' window.',
            'fas fa-circle-check');
    } else {
        setCallout('tk-empty', 'idle', null);
    }
    return status;
}

function renderQueue(stats) {
    const size = safeNumber(stats, 'queue.queue_size');
    const max = safeNumber(stats, 'queue.max_size');
    const received = safeNumber(stats, 'queue.total_received');
    const processed = safeNumber(stats, 'queue.total_processed');
    const skipped = safeNumber(stats, 'queue.total_skipped');

    setText('q-size', formatInt(size));
    setText('q-max', max === null ? 'of ' + EM_DASH : 'of ' + formatInt(max));
    setText('q-processing', formatInt(safeNumber(stats, 'queue.processing')));
    setText('q-received', formatInt(received));
    setText('q-processed', formatInt(processed));
    setText('q-skipped', formatInt(skipped));

    // Success rate (moved from the old dashboard stats block). An em-dash —
    // not "0%" — when nothing has been received: no traffic is not failure.
    setText('q-success-rate',
        (received === null || processed === null || received <= 0)
            ? EM_DASH
            : formatPercent((processed / received) * 100));

    let status;
    let label;
    let usage = null;

    if (size === null || max === null || max <= 0) {
        status = 'unknown';
        label = 'Capacity unknown';
        setState('queue-panel', size === null ? 'unavailable' : 'ready');
        setMeter('q-meter', null, 'Queue capacity not reported');
        setText('q-usage', 'Capacity not reported');
        setText('health-queue-detail', 'Maximum queue size not reported');
    } else {
        usage = (size / max) * 100;
        if (usage >= 90) { status = 'crit'; label = 'Near capacity'; }
        else if (usage >= 70) { status = 'warn'; label = 'Filling'; }
        else if (size === 0) { status = 'idle'; label = 'Idle'; }
        else { status = 'ok'; label = 'Nominal'; }

        const sentence = formatInt(size) + ' of ' + formatInt(max) + ' (' + formatPercent(usage) + ')';
        setState('queue-panel', size === 0 ? 'empty' : 'ready');
        setText('q-usage', sentence);
        setMeter('q-meter', usage,
            formatInt(size) + ' of ' + formatInt(max) + ' queued (' + formatPercent(usage) + ')');
        setText('health-queue-detail', formatInt(size) + ' of ' + formatInt(max) + ' slots used');
    }

    setStatus('queue-panel', 'q-state', status, label);
    setStatus('health-queue', 'health-queue-state', status, label);

    setState('kpi-queue', usage === null ? 'unavailable' : (usage === 0 ? 'empty' : 'ready'));
    setText('kpi-queue-value', formatPercent(usage));
    setText('kpi-queue-note', usage === null
        ? NOT_REPORTED
        : formatInt(size) + ' of ' + formatInt(max) + ' slots in use');

    if (received !== null && received > 0 && processed === 0 && skipped === received) {
        setCallout('q-anomaly', 'warn',
            'Every item received so far was skipped (' + formatInt(skipped) + ' of ' +
            formatInt(received) + '). Nothing has been processed yet.');
    } else if (received !== null && received > 0 && skipped !== null && (skipped / received) > 0.5) {
        setCallout('q-anomaly', 'warn',
            'High skip rate: ' + formatInt(skipped) + ' of ' + formatInt(received) + ' items skipped.');
    } else if (size === 0 && processed !== null && processed > 0) {
        setCallout('q-anomaly', 'idle', 'Caught up — the queue is drained.');
    } else {
        setCallout('q-anomaly', 'idle', null);
    }
    return status;
}

function renderStorage(stats) {
    // Two different questions, two different numbers — the panel used to
    // conflate them and answer neither. `total_size_*` is what OUR files
    // occupy; `usage_percent` / `disk_*` is what the VOLUME is doing. The
    // meter tracks the volume, because that is the one that can run out.
    const mb = safeNumber(stats, 'storage.total_size_mb');
    const gb = safeNumber(stats, 'storage.total_size_gb');
    const files = safeNumber(stats, 'storage.file_count');
    const maxGb = safeNumber(stats, 'storage.max_storage_gb');
    const usage = safeNumber(stats, 'storage.usage_percent');
    const appUsage = safeNumber(stats, 'storage.app_usage_percent');
    const diskTotal = safeNumber(stats, 'storage.disk_total_gb');
    const diskFree = safeNumber(stats, 'storage.disk_free_gb');
    const retention = safeNumber(stats, 'retention_days');

    // The soft budget is a policy allowance, never presented as capacity.
    const budgetText = maxGb === null ? EM_DASH : formatInt(maxGb) + ' GB';
    // Real capacity. Falls back to the budget wording only when the disk probe
    // could not read the mount (capacity_source === 'configured').
    const capacityText = diskTotal === null
        ? budgetText
        : formatGB(diskTotal) + ' GB';

    setText('st-used', formatStorage(mb, gb));
    setText('st-max', diskFree === null
        ? 'of ' + capacityText
        : formatGB(diskFree) + ' GB free of ' + capacityText);
    setText('st-files', formatInt(files));
    setText('st-used-gb', formatGB(gb));
    setText('st-budget', maxGb === null ? EM_DASH : budgetText);
    setText('st-retention', retention === null ? EM_DASH : plural(retention, 'day'));

    let status;
    let label;

    if (usage === null) {
        status = 'unknown';
        label = 'Not reported';
        setState('storage-panel', mb === null && gb === null ? 'unavailable' : 'ready');
        setMeter('st-meter', null, 'Storage usage not reported');
        setText('st-usage', 'Usage not reported');
        setText('health-storage-detail', 'Usage percentage not reported');
    } else {
        if (usage >= 95) { status = 'crit'; label = 'Critical'; }
        else if (usage >= 80) { status = 'warn'; label = 'Filling'; }
        else { status = 'ok'; label = 'Healthy'; }

        // 'empty' describes OUR footprint, not the disk.
        setState('storage-panel', mb === 0 ? 'empty' : 'ready');
        const usageText = formatPercent(usage) + ' of ' + capacityText + ' used';
        setText('st-usage', usageText);
        setMeter('st-meter', usage, usageText);
        setText('health-storage-detail', usageText);
    }

    setStatus('storage-panel', 'st-state', status, label);
    setStatus('health-storage', 'health-storage-state', status, label);

    let storageState = 'ready';
    if (mb === null && gb === null) storageState = 'unavailable';
    else if (mb === 0 || (mb === null && gb === 0)) storageState = 'empty';

    setState('kpi-storage', storageState);
    setText('kpi-storage-value', formatStorage(mb, gb));
    setText('kpi-storage-note', diskFree === null
        ? (storageState === 'unavailable' ? NOT_REPORTED : 'Free space not reported')
        : formatGB(diskFree) + ' GB free on disk');

    // The callout is about OUR files against OUR budget, so it reads
    // app_usage_percent — keying it off the disk percentage would have it
    // announce "well within the limit" while the volume was full.
    if (files !== null && files > 0 && appUsage !== null && appUsage < 0.01) {
        setCallout('st-note', 'idle',
            formatInt(files) + ' files using ' + formatStorage(mb, gb) +
            ' — well within the ' + budgetText + ' budget.');
    } else if (files === 0) {
        setCallout('st-note', 'idle', 'No files stored yet.');
    } else {
        setCallout('st-note', 'idle', null);
    }
    return status;
}

function renderPipelineFigures(stats) {
    const active = safeNumber(stats, 'pipelines.active');
    setText('pl-active', formatInt(active));
    setText('pl-detections', formatInt(safeNumber(stats, 'pipelines.total_detections')));
    setState('pipelines-section', active === null ? 'unavailable' : (active === 0 ? 'empty' : 'ready'));
}

function renderHeadlineKpis(stats) {
    renderCountKpi('kpi-pipelines', 'kpi-pipelines-value', 'kpi-pipelines-note',
        safeNumber(stats, 'pipelines.active'),
        'Currently running', 'No pipelines running');

    renderCountKpi('kpi-detections', 'kpi-detections-value', 'kpi-detections-note',
        safeNumber(stats, 'pipelines.total_detections'),
        'Since service start', 'No detections recorded yet');

    renderCountKpi('kpi-faces', 'kpi-faces-value', 'kpi-faces-note',
        safeNumber(stats, 'faces.total'),
        'Enrolled identities', 'No faces enrolled yet — add one to begin');

    renderCountKpi('kpi-processed', 'kpi-processed-value', 'kpi-processed-note',
        safeNumber(stats, 'queue.total_processed'),
        'Items completed', 'Nothing processed yet');
}

function renderHealthSummary(statuses) {
    const counts = { ok: 0, warn: 0, crit: 0, idle: 0, unknown: 0 };
    statuses.forEach(function (status) {
        if (counts[status] !== undefined) counts[status] += 1;
    });
    const parts = [];
    if (counts.ok) parts.push(counts.ok + ' operational');
    if (counts.warn) parts.push(counts.warn + ' need attention');
    if (counts.crit) parts.push(counts.crit + ' failing');
    if (counts.idle) parts.push(counts.idle + ' idle');
    if (counts.unknown) parts.push(counts.unknown + ' not reported');
    setText('health-summary', statuses.length + ' subsystems · ' + parts.join(', '));
}

function renderOverall(stats, statuses) {
    let status = 'ok';
    let label = 'Operational';

    if (statuses.indexOf('crit') !== -1) {
        status = 'crit';
        label = 'Degraded';
    } else if (statuses.indexOf('warn') !== -1) {
        status = 'warn';
        label = 'Needs attention';
    } else if (safeNumber(stats, 'pipelines.active') === 0 &&
               safeNumber(stats, 'queue.total_received') === 0 &&
               safeNumber(stats, 'faces.total') === 0) {
        status = 'idle';
        label = 'Idle — no traffic yet';
    }
    setStatus('hp-overall', 'hp-overall-state', status, label);
}

function renderAll(stats) {
    clearError();

    const service = safeGet(stats, 'service');
    setText('hp-service', typeof service === 'string' && service ? service : 'Face Recognition Service');

    const version = safeGet(stats, 'version');
    setText('hp-version', version ? 'v' + version : 'v ' + EM_DASH);

    lastGoodAt = Date.now();
    lastGoodStamp = typeof safeGet(stats, 'timestamp') === 'string' ? stats.timestamp : null;
    renderUpdatedTimes();
    renderFreshness();

    const statuses = [
        renderApiHealth(),
        renderDatabaseHealth(stats),
        renderCache(stats),
        renderTracker(stats),
        renderQueue(stats),
        renderStorage(stats)
    ];

    renderPipelineFigures(stats);
    renderHeadlineKpis(stats);
    renderHealthSummary(statuses);
    renderOverall(stats, statuses);
}

// ---------------------------------------------------------------------------
// Error / unavailable rendering
// ---------------------------------------------------------------------------
function markAllUnavailable() {
    KPI_CARDS.forEach(function (ids) {
        setState(ids[0], 'unavailable');
        setText(ids[1], EM_DASH);
        setText(ids[2], NOT_REPORTED);
    });
    setStatus('kpi-cache', null, 'unknown', null);
    setStatus('kpi-tracker', null, 'unknown', null);

    DATA_PANELS.forEach(function (id) { setState(id, 'unavailable'); });
    DATA_FIELDS.forEach(function (id) { setText(id, EM_DASH); });

    setMeter('q-meter', null, 'Queue usage not reported');
    setMeter('st-meter', null, 'Storage usage not reported');
    setMeter('tk-mem-meter', null, 'Tracker memory not reported');
    setText('q-usage', 'Usage not reported');
    setText('st-usage', 'Usage not reported');

    setStatus('queue-panel', 'q-state', 'unknown', 'Not reported');
    setStatus('storage-panel', 'st-state', 'unknown', 'Not reported');
    setStatus('tracker-panel', 'tk-state', 'unknown', 'Not reported');

    setCallout('q-anomaly', 'idle', null);
    setCallout('st-note', 'idle', null);
    setCallout('tk-empty', 'idle', null);
}

function showError(kind, status) {
    document.body.setAttribute('data-state', 'error');

    let detail = ERROR_COPY[kind] || ERROR_COPY.network;
    if (typeof status === 'number' && Number.isFinite(status)) {
        detail = detail + ' (HTTP ' + status + ')';
    }
    setText('hp-error-detail', detail);
    renderUpdatedTimes();
    renderConnection();

    const panel = document.getElementById('hp-error');
    // role="alert" announces on reveal; a flag stops the 30 s poll from
    // re-announcing the same failure every cycle.
    if (panel && !errorShown) panel.hidden = false;
    errorShown = true;

    setStatus('health-api', 'health-api-state', 'crit', 'Unreachable');
    setText('health-api-detail', 'No response from /api/stats');

    // A subsystem we could not ask about is not a failing subsystem.
    HEALTH_SUBSYSTEMS.slice(1).forEach(function (name) {
        setStatus('health-' + name, 'health-' + name + '-state', 'unknown', 'Not reported');
        setText('health-' + name + '-detail', 'Subsystem could not be queried');
    });
    setText('health-summary', '6 subsystems · 1 unreachable, 5 not reported');
    setStatus('hp-overall', 'hp-overall-state', 'crit', 'Unreachable');

    // Last good numbers stay on screen (dimmed) — wiping them destroys the
    // operator's only information. Only a never-succeeded page blanks out.
    if (lastGoodAt === null) markAllUnavailable();
}

function clearError() {
    document.body.setAttribute('data-state', 'ready');
    const panel = document.getElementById('hp-error');
    if (panel && !panel.hidden) panel.hidden = true;
    errorShown = false;
}

// ---------------------------------------------------------------------------
// Fetch — single in-flight, newest wins
// ---------------------------------------------------------------------------
function setRefreshBusy(busy) {
    const button = document.getElementById('hp-refresh');
    if (button) {
        button.disabled = busy;
        if (busy) button.setAttribute('aria-busy', 'true');
        else button.removeAttribute('aria-busy');
    }
    const icon = document.getElementById('hp-refresh-icon');
    if (icon) icon.classList.toggle('fa-spin', busy);
}

async function loadStats() {
    if (inFlight) inFlight.abort();
    const controller = new AbortController();
    inFlight = controller;
    const timer = setTimeout(function () { controller.abort(); }, STATS_TIMEOUT_MS);
    lastAttemptAt = Date.now();
    setRefreshBusy(true);

    try {
        const response = await fetch('/api/stats', {
            credentials: 'include',
            cache: 'no-store',
            signal: controller.signal
        });
        if (inFlight !== controller) return;

        if (!response.ok) {
            homeDebug('stats endpoint responded', response.status);
            showError((response.status === 401 || response.status === 403) ? 'auth' : 'http', response.status);
            return;
        }

        const stats = await response.json();
        if (inFlight !== controller) return;
        renderAll(stats);
        homeDebug('stats rendered');
    } catch (error) {
        // The identity guard, not the abort reason, is what stops a superseded
        // request from painting the error panel over a newer render.
        if (inFlight !== controller) return;
        homeDebug('stats request failed', error && error.name);
        showError(error && error.name === 'AbortError' ? 'timeout' : 'network', null);
    } finally {
        clearTimeout(timer);
        if (inFlight === controller) {
            inFlight = null;
            setRefreshBusy(false);
        }
    }
}

// ---------------------------------------------------------------------------
// Lifecycle — exactly one timer
// ---------------------------------------------------------------------------
function startHeartbeat() {
    if (heartbeat !== null) return;
    heartbeat = window.setInterval(function () {
        renderUpdatedTimes();
        renderFreshness();
        if (!document.hidden && (Date.now() - lastAttemptAt) >= STATS_POLL_MS) {
            loadStats();
        }
    }, HEARTBEAT_MS);
}

function stopHeartbeat() {
    if (heartbeat !== null) {
        window.clearInterval(heartbeat);
        heartbeat = null;
    }
}

function initPage() {
    const year = document.getElementById('current-year');
    if (year) year.textContent = String(new Date().getFullYear());

    renderConnection();
    renderUpdatedTimes();

    document.addEventListener('visibilitychange', function () {
        if (!document.hidden && (Date.now() - lastAttemptAt) >= STATS_POLL_MS) loadStats();
    });

    // Without this a backgrounded tab stacks aborted requests.
    window.addEventListener('pagehide', function () {
        stopHeartbeat();
        if (inFlight) inFlight.abort();
    });

    // A bfcache restore does not re-run scripts, so without this a returning
    // tab shows arbitrarily old numbers.
    window.addEventListener('pageshow', function (event) {
        if (event && event.persisted) {
            startHeartbeat();
            loadStats();
        }
    });

    window.addEventListener('online', function () { renderConnection(); loadStats(); });
    window.addEventListener('offline', renderConnection);

    startHeartbeat();
}

// ---------------------------------------------------------------------------
// Auth — UI customization only. The backend authenticated this request before
// serving the page and enforces authorization on every admin route; a stale
// class here exposes controls that do not work, but it still tells a non-admin
// the controls exist, which is why a demoted user appeared to keep access.
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', async () => {
    initPage();
    document.body.classList.remove('admin-verified', 'admin-user');

    try {
        const response = await fetch('/api/auth/me', {
            credentials: 'include'
        });

        if (response.ok) {
            currentUser = await response.json();
            homeDebug('user resolved');

            displayUserInfo();

            const permissions = Array.isArray(currentUser.permissions) ? currentUser.permissions : [];
            const isAdmin = permissions.includes('admin.users.manage');
            document.body.classList.toggle('admin-verified', isAdmin);
            document.body.classList.toggle('admin-user', isAdmin);

            // Both branches present: without the else a revoked privilege never
            // hides anything, which is how demoted users kept seeing admin UI.
            const adminSection = document.getElementById('admin-section');
            if (adminSection) {
                adminSection.style.display = isAdmin ? 'block' : 'none';
            }

            loadStats();
            loadPipelines();

            // Show OR hide the assistant link. 'flex' (not 'block') because
            // .hp-action is a flex row; block collapses the tile internals.
            const trackingLink = document.getElementById('tracking-link');
            if (trackingLink) {
                const canUseChatbot = permissions.includes('chatbot.use');
                trackingLink.style.display = canUseChatbot ? 'flex' : 'none';
            }

            document.body.classList.add('hp-auth-resolved');
        } else {
            console.warn('[HOME] Could not fetch user info, but backend already authenticated');
            document.body.classList.add('hp-auth-resolved');
            loadStats();
            loadPipelines();
        }
    } catch (error) {
        console.warn('[HOME] Error loading user info (non-fatal):', error);
        document.body.classList.add('hp-auth-resolved');
        loadStats();
        loadPipelines();
    }
});

function displayUserInfo() {
    const userInfo = document.getElementById('user-info');
    const logoutBtn = document.getElementById('logout-btn');

    if (currentUser) {
        if (userInfo) {
            userInfo.textContent = `${currentUser.full_name || currentUser.username} (${currentUser.role})`;
        }
        if (logoutBtn) {
            logoutBtn.style.display = 'flex';
        } else {
            // The navbar is injected asynchronously; retry once it has landed.
            setTimeout(() => {
                const retryLogoutBtn = document.getElementById('logout-btn');
                if (retryLogoutBtn) {
                    retryLogoutBtn.style.display = 'flex';
                }
            }, 600);
        }
    }
}

// Pipelines the signed-in user may see, keyed by pipeline_id, so the change
// handler can show a camera's detail without a second request.
let pipelinesById = new Map();

async function loadPipelines() {
    const select = document.getElementById('pipeline-select');
    const section = document.getElementById('pipelines-section');
    // The guard stays regardless of markup: an empty page must not
    // dereference null once the pipelines response arrives.
    if (!select || !section) return;

    const empty = document.getElementById('pl-empty');
    const detail = document.getElementById('pipeline-selected-detail');

    try {
        // /api/dashboard/pipelines (not /api/users/me/pipelines): that endpoint
        // returns bare id strings, so the dropdown could only ever show opaque
        // UUIDs. This one is authorization-scoped the same way and carries
        // display_name — location_name with a pipeline_id fallback — plus the
        // active flag and last activity.
        const response = await fetch('/api/dashboard/pipelines', {
            credentials: 'include',
            cache: 'no-store'
        });
        if (!response.ok) {
            setSelectPlaceholder(select, 'Pipelines unavailable');
            return;
        }

        const payload = await response.json();
        const pipelines = Array.isArray(payload && payload.pipelines) ? payload.pipelines : [];

        section.style.display = 'block';
        pipelinesById = new Map();
        select.replaceChildren();

        if (!pipelines.length) {
            setSelectPlaceholder(select, 'No pipelines assigned');
            select.disabled = true;
            if (detail) detail.hidden = true;
            if (empty) empty.hidden = false;
            return;
        }

        if (empty) empty.hidden = true;
        select.disabled = false;

        const lead = document.createElement('option');
        lead.value = '';
        lead.textContent = 'Select a camera… (' + pipelines.length + ')';
        select.appendChild(lead);

        pipelines.forEach(p => {
            const id = String(safeGet(p, 'pipeline_id') || '');
            if (!id) return;
            pipelinesById.set(id, p);

            // textContent, never innerHTML: display_name is operator-supplied
            // data and must never be parsed as markup.
            const option = document.createElement('option');
            option.value = id;
            const label = String(safeGet(p, 'display_name') || id);
            option.textContent = safeGet(p, 'is_active') === false
                ? label + ' (inactive)'
                : label;
            select.appendChild(option);
        });

        if (detail) detail.hidden = true;
    } catch (error) {
        homeDebug('pipeline list request failed', error && error.name);
        setSelectPlaceholder(select, 'Pipelines unavailable');
    }
}

function setSelectPlaceholder(select, text) {
    select.replaceChildren();
    const option = document.createElement('option');
    option.value = '';
    option.textContent = text;
    select.appendChild(option);
    select.disabled = true;
}

// Render the chosen camera's detail. Every value goes in via textContent.
function showSelectedPipeline(pipelineId) {
    const detail = document.getElementById('pipeline-selected-detail');
    if (!detail) return;

    const pipeline = pipelinesById.get(pipelineId);
    if (!pipelineId || !pipeline) {
        detail.hidden = true;
        return;
    }

    const location = safeGet(pipeline, 'location_name');
    setText('pl-sel-location', location ? String(location) : 'Not named');
    setText('pl-sel-id', String(safeGet(pipeline, 'pipeline_id') || EM_DASH));
    setText('pl-sel-state', safeGet(pipeline, 'is_active') === false ? 'Inactive' : 'Active');

    const seen = safeGet(pipeline, 'last_webhook_at');
    setText('pl-sel-seen', seen ? formatRelative(seen) : 'No activity recorded');

    detail.hidden = false;
}

// ---------------------------------------------------------------------------
// CSP-safe event registration
//
// Markup declares a data-action attribute naming a handler; actions.js
// delegates one document-level listener per event type and invokes only the
// names registered here. An unregistered name does nothing, and no global is
// reachable by name. (The attribute is described rather than written out
// literally: a contract test scans these files for data-action names and
// would read the example as a real, unhandled action.)
// ---------------------------------------------------------------------------
Actions.register({
    refreshStats: () => {
        loadStats();
    },
    goToPipeline: (el) => {
        const pipeline = el.dataset.arg;
        if (pipeline) {
            window.location.href = '/dashboard?pipeline=' + encodeURIComponent(pipeline);
        }
    },
    // Registered under data-action-change (actions.js delegates 'change'
    // separately from 'click'), so choosing a camera shows its detail
    // in place rather than navigating away.
    selectPipeline: (el) => {
        showSelectedPipeline(el.value);
    },
    focusHealth: () => {
        const section = document.getElementById('health-section');
        if (!section) return;
        section.scrollIntoView({
            behavior: prefersReducedMotion() ? 'auto' : 'smooth',
            block: 'start'
        });
        section.focus({ preventScroll: true });
    },
});
