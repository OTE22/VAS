/**
 * System Logs Viewer - Frontend JavaScript
 * All logic is in the backend - this file only displays data
 * Supports all log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
 */

// Global state
let currentPage = 1;
let currentPageSize = 50;
let currentDateFrom = null;
let currentDateTo = null;
let currentLevel = 'all';

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    checkAuth();
    loadStats();
    loadLogs();
    setupEventListeners();
});

/**
 * Load user info for UI - BACKEND HANDLES ALL AUTHENTICATION
 */
async function checkAuth() {
    // Backend already authenticated user before serving this page
    // Just fetch user info for UI customization
    try {
        const response = await fetch('/api/auth/me', {
            credentials: 'include' // Include HttpOnly cookies
        });
        if (!response.ok) {
            // Backend will handle redirect via exception handler
            return;
        }
    } catch (error) {
        // Non-fatal - backend already authenticated
        console.warn('Error loading user info:', error);
    }
}

/**
 * Load statistics from backend
 */
async function loadStats() {
    try {
        const response = await fetch('/api/logs/stats', {
            credentials: 'include' // Include HttpOnly cookies
        });

        if (!response.ok) {
            console.error('Failed to load stats');
            return;
        }

        const stats = await response.json();
        
        // Display stats for all log levels (backend sends formatted data)
        document.getElementById('stat-total-debug').textContent = stats.total_debug || 0;
        document.getElementById('stat-total-info').textContent = stats.total_info || 0;
        document.getElementById('stat-total-warning').textContent = stats.total_warning || 0;
        document.getElementById('stat-total-errors').textContent = stats.total_errors || 0;
        document.getElementById('stat-total-critical').textContent = stats.total_critical || 0;
        document.getElementById('stat-file-size').textContent = stats.file_size_mb ? `${stats.file_size_mb} MB` : '-';
    } catch (error) {
        console.error('Error loading stats:', error);
    }
}

/**
 * Load error logs from backend
 */
async function loadLogs() {
    const container = document.getElementById('logs-container');
    container.innerHTML = '<div class="loading"><i class="fas fa-spinner fa-spin"></i> Loading system logs...</div>';

    try {
        // Build query string (backend handles all filtering)
        const params = new URLSearchParams({
            page: currentPage.toString(),
            page_size: currentPageSize.toString()
        });

        if (currentDateFrom) {
            params.append('date_from', currentDateFrom);
        }
        if (currentDateTo) {
            params.append('date_to', currentDateTo);
        }
        if (currentLevel && currentLevel !== 'all') {
            params.append('level', currentLevel);
        }

        const response = await fetch(`/api/logs?${params.toString()}`, {
            credentials: 'include' // Include HttpOnly cookies
        });

        if (!response.ok) {
            container.innerHTML = `<div class="error"><i class="fas fa-exclamation-circle"></i> Failed to load system logs</div>`;
            return;
        }

        const data = await response.json();

        // Backend sends all processed data - just display it
            if (data.logs.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-check-circle"></i>
                    <h3>No Logs Found</h3>
                    <p>No logs match your current filters.</p>
                </div>
            `;
            document.getElementById('pagination-section').style.display = 'none';
            return;
        }

        // Render logs (backend sends formatted data)
        container.innerHTML = data.logs.map(log => renderLogEntry(log)).join('');

        // Update pagination (backend sends pagination info)
        updatePagination(data);

    } catch (error) {
        console.error('Error loading logs:', error);
        container.innerHTML = `<div class="error"><i class="fas fa-exclamation-circle"></i> Error loading system logs: ${error.message}</div>`;
    }
}

/**
 * Render a single log entry (backend sends structured data)
 */
function renderLogEntry(log) {
    const levelClass = log.level.toLowerCase();
    const isCritical = log.level === 'CRITICAL';
    const isError = log.level === 'ERROR';
    const isWarning = log.level === 'WARNING';
    const isInfo = log.level === 'INFO';
    const isDebug = log.level === 'DEBUG';
    
    // Determine entry class based on level
    let entryClass = '';
    if (isCritical) entryClass = 'critical';
    else if (isError) entryClass = 'error';
    else if (isWarning) entryClass = 'warning';
    else if (isInfo) entryClass = 'info';
    else if (isDebug) entryClass = 'debug';
    
    return `
        <div class="log-entry ${entryClass}">
            <div class="log-header">
                <span class="log-timestamp">${escapeHtml(log.timestamp)}</span>
                <span class="log-level ${levelClass}">${escapeHtml(log.level)}</span>
                ${log.process_id ? `<span class="log-process">PID: ${escapeHtml(log.process_id)}</span>` : ''}
                ${log.logger_name ? `<span class="log-logger">${escapeHtml(log.logger_name)}</span>` : ''}
                ${log.line_number ? `<span class="log-line-number">Line ${log.line_number}</span>` : ''}
            </div>
            <div class="log-message">${escapeHtml(log.message)}</div>
        </div>
    `;
}

/**
 * Update pagination controls (backend sends pagination data)
 */
function updatePagination(data) {
    const paginationSection = document.getElementById('pagination-section');
    const paginationInfo = document.getElementById('pagination-info');
    const prevBtn = document.getElementById('prev-page-btn');
    const nextBtn = document.getElementById('next-page-btn');

    if (data.total_pages === 0) {
        paginationSection.style.display = 'none';
        return;
    }

    paginationSection.style.display = 'flex';
    paginationInfo.textContent = `Page ${data.page} of ${data.total_pages} (${data.total_count} total logs${data.level_filter && data.level_filter !== 'all' ? ` - ${data.level_filter}` : ''})`;
    
    prevBtn.disabled = !data.has_previous;
    nextBtn.disabled = !data.has_next;
}

/**
 * Setup event listeners
 */
function setupEventListeners() {
    // Apply filters button
    document.getElementById('apply-filters-btn').addEventListener('click', () => {
        currentDateFrom = document.getElementById('date-from').value || null;
        currentDateTo = document.getElementById('date-to').value || null;
        currentLevel = document.getElementById('level-filter').value || 'all';
        currentPageSize = parseInt(document.getElementById('page-size').value);
        currentPage = 1;
        loadLogs();
    });

    // Clear filters button
    document.getElementById('clear-filters-btn').addEventListener('click', () => {
        document.getElementById('date-from').value = '';
        document.getElementById('date-to').value = '';
        document.getElementById('level-filter').value = 'all';
        document.getElementById('page-size').value = '50';
        currentDateFrom = null;
        currentDateTo = null;
        currentLevel = 'all';
        currentPageSize = 50;
        currentPage = 1;
        loadLogs();
    });

    // Refresh button
    document.getElementById('refresh-logs-btn').addEventListener('click', () => {
        loadStats();
        loadLogs();
    });

    // Pagination buttons
    document.getElementById('prev-page-btn').addEventListener('click', () => {
        if (currentPage > 1) {
            currentPage--;
            loadLogs();
        }
    });

    document.getElementById('next-page-btn').addEventListener('click', () => {
        currentPage++;
        loadLogs();
    });
}

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

