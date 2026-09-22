/** Live admin monitoring. Every resource owns its request and freshness state. */
'use strict';
let currentPage = 1;
let currentPageSize = null;
let currentDateFrom = null;
let currentDateTo = null;
let currentLevel = 'all';
let currentSource = 'application';
let logConfig = null;
let timer = null;
let suspended = false;
let lastCleanupCompletedAt;
let displayedLogs = [];
const requests = new Map();
const updated = new Map();
const $ = id => document.getElementById(id);

function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
}
function stamp(value) {
    if (!value) return 'Not recorded';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Unknown' : date.toLocaleString();
}
function freshness(key, error, note = '') {
    const target = $(key + '-freshness');
    if (!target) return;
    if (!error) updated.set(key, new Date().toISOString());
    target.classList.toggle('monitor-error', Boolean(error));
    target.textContent = `${error ? 'Refresh failed: ' + error + '. ' : ''}Last updated: ${stamp(updated.get(key))}. ${note}`;
}
async function request(key, url, render) {
    if (suspended) return;
    requests.get(key)?.abort();
    const controller = new AbortController();
    requests.set(key, controller);
    const deadline = setTimeout(() => controller.abort(), 12000);
    try {
        const response = await fetch(url, {credentials: 'include', cache: 'no-store',
            headers: {Accept: 'application/json'}, signal: controller.signal});
        if (!response.ok) {
            let detail = `HTTP ${response.status}`;
            if ((response.headers.get('content-type') || '').includes('application/json')) {
                const body = await response.json();
                if (typeof body.detail === 'string') detail = body.detail;
            }
            throw new Error(detail);
        }
        if (!(response.headers.get('content-type') || '').includes('application/json')) {
            throw new Error('Expected JSON; sign in again if your session expired');
        }
        const data = await response.json();
        if (requests.get(key) !== controller || suspended) return;
        return await render(data);
    } catch (error) {
        if (requests.get(key) !== controller || suspended) return;
        freshness(key, error.name === 'AbortError' ? 'Request timed out' : error.message);
    } finally {
        clearTimeout(deadline);
        if (requests.get(key) === controller) requests.delete(key);
    }
}
async function loadConfig() {
    await request('config', '/api/logs/config', config => {
        logConfig = config;
        currentPageSize = config.default_page_size;
        currentLevel = config.default_level || 'all';
        $('page-size').replaceChildren(...config.page_size_options.map(size => new Option(`${size} per page`, String(size))));
        $('page-size').value = String(currentPageSize);
        $('level-filter').replaceChildren(new Option('All Levels', 'all'), ...config.levels.map(level => new Option(level, level)));
        $('level-filter').value = currentLevel;
        $('log-source').replaceChildren(...Object.entries(config.sources || {application: 'Application'}).map(([key, label]) => new Option(label, key)));
    });
}
function loadStats() {
    return request('stats', `/api/logs/stats?source=${encodeURIComponent(currentSource)}`, stats => {
        for (const name of ['debug', 'info', 'warning', 'errors', 'critical']) {
            $('stat-total-' + name).textContent = stats.source_available === false && !stats.log_files?.length ? '-' : (stats['total_' + name] ?? 0);
        }
        $('stat-file-size').textContent = stats.source_available === false && !stats.log_files?.length ? '-' : `${stats.file_size_mb ?? 0} MB`;
        freshness('stats', null, stats.source_available === false ? 'Active source file not available.' : stats.truncated ? 'Counts cover only the scanned portion of this source.' : 'Counts cover this log source.');
    });
}
function loadLogs() {
    const params = new URLSearchParams({page: String(currentPage), source: currentSource});
    if (currentPageSize) params.set('page_size', String(currentPageSize));
    if (currentDateFrom) params.set('date_from', currentDateFrom);
    if (currentDateTo) params.set('date_to', currentDateTo);
    if (currentLevel !== 'all') params.set('level', currentLevel);
    return request('logs', `/api/logs?${params}`, data => {
        if (!data.truncated && !data.logs.length && currentPage > 1) {
            currentPage = Math.max(1, data.total_pages || 0);
            return loadLogs();
        }
        displayedLogs = data.logs;
        $('logs-container').innerHTML = data.logs.length ? data.logs.map(renderLogEntry).join('') : data.source_available === false
            ? `<div class="empty-state"><h3>No logs available yet</h3><p>${escapeHtml(data.source_message)}</p><p>Automatic refresh will show records when they become available.</p></div>`
            : '<div class="empty-state"><h3>No logs found</h3><p>No records match these filters in the scanned portion.</p></div>';
        $('scan-notice').textContent = data.source_available === false && data.logs.length ? `${data.source_message} Showing retained rotated logs.` : data.truncated ? `Partial history: scanned ${data.scanned_files} files and ${data.scanned_bytes} bytes. Counts are not the full log total. Narrow the date or level filter if needed.` : '';
        const summary = $('log-view-summary');
        if (summary) summary.textContent = `${data.logs.length} records on this page · ${logConfig?.sources?.[currentSource] || currentSource} · ${currentLevel === 'all' ? 'All levels' : currentLevel} · ${currentDateFrom || 'Any start date'} to ${currentDateTo || 'Any end date'}${data.truncated ? ' · Partial history' : ''}`;
        filterLogPage();
        updatePagination(data);
        freshness('logs', null, currentPage > 1 ? 'Older page: automatic log refresh paused.' : '');
    });
}
function renderLogEntry(log) {
    const level = ['DEBUG','INFO','WARNING','ERROR','CRITICAL'].includes(log.level) ? log.level.toLowerCase() : 'info';
    const message = String(log.message || 'No message recorded');
    const firstLine = message.split('\n')[0];
    return `<details class="log-entry ${level}"><summary class="log-event-summary"><span class="log-header">
        <span class="log-level ${level}">${escapeHtml(log.level)}</span>
        <time class="log-timestamp">${escapeHtml(log.timestamp)}</time>
        <span class="log-logger">${escapeHtml(log.logger_name || 'Unknown logger')}</span></span>
        <span class="log-preview">${escapeHtml(firstLine.slice(0, 240))}${firstLine.length > 240 ? '…' : ''}</span>
        <span class="log-expand-label">Expand event details</span></summary>
        <div class="log-event-body"><dl class="log-event-meta"><div><dt>Process</dt><dd>${escapeHtml(log.process_id ?? 'Not recorded')}</dd></div><div><dt>Source file</dt><dd>${escapeHtml(log.source_file || 'Not recorded')}</dd></div><div><dt>Logger</dt><dd>${escapeHtml(log.logger_name || 'Not recorded')}</dd></div></dl>
        <pre class="log-message">${escapeHtml(message)}</pre></div></details>`;
}
function filterLogPage() {
    const input = $('log-page-search');
    if (!input || !document.querySelectorAll) return;
    const needle = input.value.trim().toLowerCase();
    let visible = 0;
    document.querySelectorAll('#logs-container .log-entry').forEach((node, index) => {
        const log = displayedLogs[index] || {};
        const text = [log.message, log.logger_name, log.source_file, log.level, log.process_id].join(' ').toLowerCase();
        node.hidden = !!needle && !text.includes(needle);
        if (!node.hidden) visible++;
    });
    const status = $('log-find-status');
    if (status) status.textContent = needle ? `${visible} of ${displayedLogs.length} loaded records match. This search applies to the current page only.` : 'Select an event to read its full message. Timestamps are shown as recorded in the log.';
}
function updatePagination(data) {
    $('pagination-section').style.display = data.logs.length || data.has_previous ? 'flex' : 'none';
    $('pagination-info').textContent = data.truncated ? `Page ${data.page} · ${data.total_count} matching records in partial scan` : `Page ${data.page} of ${data.total_pages} (${data.total_count} matching records)`;
    $('prev-page-btn').disabled = !data.has_previous;
    $('next-page-btn').disabled = !data.has_next;
}
function stateLabel(state) {
    const allowed = ['healthy','waiting','starting','running','idle','stale','degraded','stopped','offline','unavailable'];
    const safe = allowed.includes(state) ? state : 'unavailable';
    return `<span class="job-state" data-state="${safe}">${safe}</span>`;
}
function invalidateLogView() {
    // An in-flight response may still contain records deleted by cleanup.
    for (const key of ['logs', 'stats']) {
        requests.get(key)?.abort();
        requests.delete(key);
        updated.delete(key);
    }
    currentPage = 1;
    $('logs-container').textContent = 'Cleanup finished. Refreshing retained logs…';
    $('pagination-section').style.display = 'none';
    $('pagination-info').textContent = '';
    $('scan-notice').textContent = '';
    for (const name of ['debug', 'info', 'warning', 'errors', 'critical']) {
        $('stat-total-' + name).textContent = '—';
    }
    $('stat-file-size').textContent = '—';
}
function loadJobs() {
    return request('jobs', '/api/logs/background-status', data => {
        $('jobs-body').innerHTML = data.services.map(service => `<tr>
            <td><a href="/admin/background-tasks?task_type=${encodeURIComponent(service.name)}">${escapeHtml(service.name.replaceAll('_', ' '))}</a></td>
            <td>${stateLabel(service.state)}</td><td>${escapeHtml(stamp(service.last_success_at))}</td>
            <td>${data.history_available ? escapeHtml(stamp(service.last_completed_at)) : 'Unavailable'}</td>
            <td>${escapeHtml(stamp(service.next_run_at))}</td>
            <td>${escapeHtml(service.failures)} consecutive failures. ${escapeHtml(service.last_error || service.activity)}</td></tr>`).join('');
        const cleanup = data.last_log_cleanup;
        if (cleanup) {
            const result = cleanup.result;
            const removed = Number.isFinite(result.deleted_records) ? `${result.deleted_records} expired records removed` : 'Record removals were not counted by this older run';
            $('cleanup-result').textContent = `Last log cleanup: ${cleanup.status} at ${stamp(cleanup.completed_at)}. ${removed}; ${result.deleted_files ?? 0} files deleted; ${Number(result.freed_space_mb || 0).toFixed(2)} MB freed. Retained records remain visible.`;
        } else {
            $('cleanup-result').textContent = data.history_available ? 'No completed log cleanup recorded.' : 'Cleanup results unavailable.';
        }
        if (data.log_inventory) {
            $('log-directory').textContent = `${data.log_inventory.directory}: ${data.log_inventory.files.length} log files, ${(data.log_inventory.total_bytes / 1048576).toFixed(2)} MB. Application rotations use the log retention policy; diagnostic logs use diagnostic retention.`;
            $('log-inventory').innerHTML = data.log_inventory.files.map(file => `<li>${escapeHtml(file.name)} (${(file.bytes / 1048576).toFixed(2)} MB)</li>`).join('');
        }
        const worker = data.ml_worker;
        const state = worker.status === 'healthy' ? worker.worker_state : worker.status;
        $('ml-worker-status').innerHTML = `ML worker: ${stateLabel(state)} · Last heartbeat: ${escapeHtml(stamp(worker.heartbeat_at))}${worker.current_job_id ? ' · Job: ' + escapeHtml(worker.current_job_id) : ''}`;
        const problems = data.services.filter(s => ['stale','degraded','stopped'].includes(s.state)).length;
        const mlProblem = ['stale','offline','unavailable','stopped'].includes(state);
        freshness('jobs', data.errors.length ? data.errors.join(' ') : null,
            `${data.services.length} service registrations; ${problems} need attention.${mlProblem ? ' ML worker needs attention.' : ''} ${!data.services.length ? 'No service registry reported.' : ''}`);
        if (data.history_available) {
            const completedAt = cleanup?.completed_at || null;
            const cleanupChanged = lastCleanupCompletedAt !== undefined &&
                completedAt !== null && completedAt !== lastCleanupCompletedAt;
            lastCleanupCompletedAt = completedAt;
            if (cleanupChanged) {
                // Includes partially failed cleanups: they may have deleted
                // records before reporting a failure. Keep all user filters.
                invalidateLogView();
                return Promise.all([loadLogs(), loadStats()]);
            }
        }
    });
}
async function refresh(automatic = false) {
    clearTimeout(timer);
    if (suspended || document.hidden) return;
    await Promise.all([loadJobs(), loadStats(), automatic && currentPage > 1 ? Promise.resolve() : loadLogs()]);
    schedule();
}
function schedule() {
    clearTimeout(timer);
    if (!suspended && !document.hidden && $('auto-refresh').checked) timer = setTimeout(() => refresh(true), 15000);
}
function suspend() {
    suspended = true;
    clearTimeout(timer);
    for (const controller of requests.values()) controller.abort();
    requests.clear();
}
document.addEventListener('DOMContentLoaded', async () => {
    await loadConfig();
    $('apply-filters-btn').addEventListener('click', () => {
        currentDateFrom = $('date-from').value || null;
        currentDateTo = $('date-to').value || null;
        currentLevel = $('level-filter').value || 'all';
        currentPageSize = Number($('page-size').value) || null;
        currentSource = $('log-source').value;
        currentPage = 1;
        refresh();
    });
    $('log-source').addEventListener('change', () => {
        currentSource = $('log-source').value;
        currentPage = 1;
        $('logs-container').textContent = 'Loading selected log source…';
        $('pagination-section').style.display = 'none';
        $('scan-notice').textContent = '';
        updated.delete('logs');
        updated.delete('stats');
        for (const name of ['debug','info','warning','errors','critical']) $('stat-total-' + name).textContent = '?';
        $('stat-file-size').textContent = '?';
        refresh();
    });
    $('clear-filters-btn').addEventListener('click', () => {
        $('date-from').value = $('date-to').value = '';
        if ($('log-page-search')) $('log-page-search').value = '';
        currentDateFrom = currentDateTo = null;
        currentLevel = logConfig?.default_level || 'all';
        currentPageSize = logConfig?.default_page_size || null;
        $('level-filter').value = currentLevel;
        if (currentPageSize) $('page-size').value = String(currentPageSize);
        currentPage = 1;
        refresh();
    });
    $('refresh-logs-btn').addEventListener('click', () => refresh());
    $('prev-page-btn').addEventListener('click', () => { if (currentPage > 1) { currentPage--; loadLogs(); } });
    $('next-page-btn').addEventListener('click', () => { currentPage++; loadLogs(); });
    $('auto-refresh').addEventListener('change', schedule);
    $('log-page-search')?.addEventListener('input', filterLogPage);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) suspend();
        else { suspended = false; refresh(true); }
    });
    window.addEventListener('pagehide', suspend);
    window.addEventListener('pageshow', event => { if (event.persisted) { suspended = false; refresh(true); } });
    refresh();
});
