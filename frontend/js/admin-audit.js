// Admin Audit Log JavaScript
// BACKEND HANDLES ALL AUTHENTICATION
let currentPage = 1;
let pageLimit = 100;
let currentFilters = {};

document.addEventListener('DOMContentLoaded', async () => {
    // Backend already authenticated user before serving this page

    // Load usernames for dropdown
    await loadUsernames();

    // Load stats and data
    await loadStats();
    await loadAuditLogs();

    // Event listeners
    const logoutBtn = document.getElementById('logout-btn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', (e) => {
            // Defer to the ONE real logout (navbar-loader.js): it calls
            // POST /api/auth/logout, which revokes the token server-side, and
            // clears the cached navigation. This handler used to remove a
            // localStorage 'access_token' that is never written — the credential
            // is an HttpOnly cookie — so it redirected while leaving the session
            // valid.
            if (typeof window.handleLogout === 'function') {
                e.preventDefault();
                window.handleLogout();
            }
            // If navbar-loader has not loaded, fall through to the button's
            // default behaviour rather than faking a logout that did not happen.
        });
    }
});

// Handlers previously reached through inline onclick/onsubmit/onchange
// attributes, which `script-src 'self'` blocks. Registered rather than looked
// up on window, so only these names are invocable. Delegation means the
// per-row "View Details" buttons work without rebinding after every render.
Actions.register({
    applyFilters,
    clearFilters,
    refreshData,
    previousPage,
    nextPage,
    changeLimit,
    closeDetailsModal,
    viewDetails: (element) => {
        const logId = Actions.intFrom(element, 'logId');
        if (logId !== null) viewDetails(logId);
    },
});

async function loadUsernames() {
    try {
        const response = await fetch('/api/users', {
            credentials: 'include' // Include HttpOnly cookies
        });
        
        if (response.ok) {
            const users = await response.json();
            const usernameSelect = document.getElementById('filter-username');
            
            if (usernameSelect) {
                // Clear existing options except "All Usernames"
                usernameSelect.innerHTML = '<option value="">All Usernames</option>';
                
                // Add unique usernames to dropdown
                const uniqueUsernames = [...new Set(users.map(u => u.username).filter(Boolean))].sort();
                uniqueUsernames.forEach(username => {
                    const option = document.createElement('option');
                    option.value = username;
                    option.textContent = username;
                    usernameSelect.appendChild(option);
                });
                
                console.log(`✅ Loaded ${uniqueUsernames.length} usernames for dropdown`);
            }
        }
    } catch (error) {
        console.error('Error loading usernames:', error);
        // Fallback: keep the select but show error
        const usernameSelect = document.getElementById('filter-username');
        if (usernameSelect) {
            usernameSelect.innerHTML = '<option value="">All Usernames (Error loading list)</option>';
        }
    }
}

async function loadStats() {
    try {
        const response = await fetch('/api/audit/chatbot/stats', {
            credentials: 'include' // Include HttpOnly cookies
        });
        
        if (response.ok) {
            const stats = await response.json();
            document.getElementById('total-queries').textContent = stats.total_queries.toLocaleString();
            document.getElementById('successful-queries').textContent = stats.successful_queries.toLocaleString();
            document.getElementById('failed-queries').textContent = stats.failed_queries.toLocaleString();
            document.getElementById('unique-users').textContent = stats.unique_users.toLocaleString();
            
            if (stats.avg_processing_time_ms) {
                document.getElementById('avg-time').textContent = `${stats.avg_processing_time_ms.toFixed(0)} ms`;
            } else {
                document.getElementById('avg-time').textContent = 'N/A';
            }
        }
    } catch (error) {
        console.error('Error loading stats:', error);
    }
}

async function loadAuditLogs() {
    try {
        const params = new URLSearchParams({
            limit: pageLimit.toString(),
            offset: ((currentPage - 1) * pageLimit).toString(),
            ...currentFilters
        });

        const response = await fetch(`/api/audit/chatbot?${params}`, {
            credentials: 'include' // Include HttpOnly cookies
        });
        
        if (!response.ok) throw new Error('Failed to load audit logs');
        
        const logs = await response.json();
        renderAuditTable(logs);
        
        // Update pagination buttons
        document.getElementById('prev-btn').disabled = currentPage === 1;
        document.getElementById('next-btn').disabled = logs.length < pageLimit;
        document.getElementById('current-page').textContent = currentPage;
    } catch (error) {
        document.getElementById('audit-table-body').innerHTML = 
            `<tr><td colspan="8" class="loading">Error loading audit logs: ${error.message}</td></tr>`;
    }
}

function renderAuditTable(logs) {
    const tbody = document.getElementById('audit-table-body');
    
    if (logs.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="loading">No audit logs found</td></tr>';
        return;
    }

    tbody.innerHTML = logs.map(log => {
        const queryPreview = log.query.length > 50 ? log.query.substring(0, 50) + '...' : log.query;
        const responsePreview = log.response 
            ? (log.response.length > 100 ? log.response.substring(0, 100) + '...' : log.response)
            : (log.error_message || 'N/A');
        const date = new Date(log.created_at).toLocaleString();
        const timeMs = log.processing_time_ms ? `${log.processing_time_ms.toFixed(0)} ms` : 'N/A';
        
        // Every value below comes from the API and is escaped. Previously
        // log.username, log.query and the response preview were interpolated
        // raw, so a stored query containing markup executed when an
        // administrator opened this page — the audit log is exactly where
        // attacker-controlled text lands.
        const logId = Number(log.id);

        return `
            <tr>
                <td>${escapeHtml(String(log.id ?? ''))}</td>
                <td>${escapeHtml(String(log.username ?? ''))} (ID: ${escapeHtml(String(log.user_id ?? ''))})</td>
                <td class="query-cell" title="${escapeHtml(String(log.query ?? ''))}">${escapeHtml(queryPreview)}</td>
                <td class="response-cell">${escapeHtml(responsePreview)}</td>
                <td><span class="badge badge-${log.success ? 'active' : 'inactive'}">${log.success ? 'Success' : 'Failed'}</span></td>
                <td>${escapeHtml(timeMs)}</td>
                <td>${escapeHtml(date)}</td>
                <td>
                    <button class="btn-action view-details-btn" type="button"
                            data-action="viewDetails" data-log-id="${Number.isInteger(logId) ? logId : ''}">View Details</button>
                </td>
            </tr>
        `;
    }).join('');
}

async function viewDetails(logId) {
    try {
        const response = await fetch(`/api/audit/chatbot/${logId}`, {
            credentials: 'include' // Include HttpOnly cookies
        });
        
        if (!response.ok) throw new Error('Failed to load log details');
        
        const log = await response.json();
        const date = new Date(log.created_at).toLocaleString();
        
        document.getElementById('details-content').innerHTML = `
            <div class="form-group">
                <label>ID</label>
                <div>${log.id}</div>
            </div>
            <div class="form-group">
                <label>User</label>
                <div>${escapeHtml(String(log.username ?? ''))} (ID: ${escapeHtml(String(log.user_id ?? (log.historical_user_id != null ? log.historical_user_id + ' — account deleted' : '—')))})</div>
            </div>
            <div class="form-group">
                <label>Date & Time</label>
                <div>${date}</div>
            </div>
            <div class="form-group">
                <label>Status</label>
                <div><span class="badge badge-${log.success ? 'active' : 'inactive'}">${log.success ? 'Success' : 'Failed'}</span></div>
            </div>
            <div class="form-group">
                <label>Processing Time</label>
                <div>${log.processing_time_ms ? `${log.processing_time_ms.toFixed(2)} ms` : 'N/A'}</div>
            </div>
            <div class="form-group">
                <label>Session ID</label>
                <div>${log.session_id || 'N/A'}</div>
            </div>
            <div class="form-group">
                <label>Query</label>
                <div class="query-display">${escapeHtml(log.query)}</div>
            </div>
            <div class="form-group">
                <label>${log.success ? 'Response' : 'Error Message'}</label>
                <div class="response-display">${escapeHtml(log.response || log.error_message || 'N/A')}</div>
            </div>
        `;
        
        // Shared lifecycle: layering, Escape, backdrop, focus containment,
        // background suppression and the page scroll lock all come from
        // ModalStack. This page previously had NO Escape handling at all.
        window.ModalStack.open(document.getElementById('details-modal'), {
            backdropClose: true,
            onClose: () => closeDetailsModal()
        });
    } catch (error) {
        alert('Error loading details: ' + error.message);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function closeDetailsModal() {
    const modal = document.getElementById('details-modal');
    if (!modal) return;
    if (window.ModalStack.isOpen(modal)) {
        // The stack's onClose re-enters here once the entry has left it, so
        // the hide below runs exactly once.
        window.ModalStack.close(modal);
        return;
    }
    modal.style.display = 'none';
}

function applyFilters() {
    currentFilters = {};
    
    const userId = document.getElementById('filter-user-id').value;
    if (userId) currentFilters.user_id = userId;
    
    const username = document.getElementById('filter-username').value;
    if (username) currentFilters.username = username;
    
    const success = document.getElementById('filter-success').value;
    if (success) currentFilters.success = success === 'true';
    
    const startDate = document.getElementById('filter-start-date').value;
    if (startDate) currentFilters.start_date = new Date(startDate).toISOString();
    
    const endDate = document.getElementById('filter-end-date').value;
    if (endDate) {
        const end = new Date(endDate);
        end.setHours(23, 59, 59, 999); // End of day
        currentFilters.end_date = end.toISOString();
    }
    
    currentPage = 1;
    loadAuditLogs();
    loadStats();
}

function clearFilters() {
    document.getElementById('filter-user-id').value = '';
    document.getElementById('filter-username').value = '';
    document.getElementById('filter-success').value = '';
    document.getElementById('filter-start-date').value = '';
    document.getElementById('filter-end-date').value = '';
    currentFilters = {};
    currentPage = 1;
    loadAuditLogs();
    loadStats();
}

async function refreshData() {
    // Add loading state to refresh button
    const refreshBtn = document.querySelector('.audit-btn-refresh');
    const refreshIcon = refreshBtn?.querySelector('.fa-sync-alt');
    if (refreshIcon) {
        refreshIcon.classList.add('fa-spin');
    }
    if (refreshBtn) {
        refreshBtn.disabled = true;
        refreshBtn.style.opacity = '0.7';
    }
    
    try {
        await loadAuditLogs();
        await loadStats();
    } catch (error) {
        console.error('Error refreshing data:', error);
    } finally {
        // Remove loading state
        if (refreshIcon) {
            refreshIcon.classList.remove('fa-spin');
        }
        if (refreshBtn) {
            refreshBtn.disabled = false;
            refreshBtn.style.opacity = '1';
        }
    }
}

function previousPage() {
    if (currentPage > 1) {
        currentPage--;
        loadAuditLogs();
    }
}

function nextPage() {
    currentPage++;
    loadAuditLogs();
}

function changeLimit() {
    pageLimit = parseInt(document.getElementById('page-limit').value);
    currentPage = 1;
    loadAuditLogs();
}

// Backdrop clicks are ModalStack's (opted in via backdropClose at open time);
// a second listener here would double-close.

