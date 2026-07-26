/**
 * Search History Management
 * =========================
 * Handles viewing, filtering, and exporting search history.
 */

(function() {
    'use strict';

    const state = {
        history: [],
        currentOffset: 0,
        limit: 50,
        hasMore: true,
        filters: {
            searchType: '',
            daysBack: 30
        }
    };

    const elements = {
        filterType: document.getElementById('filter-type'),
        daysBack: document.getElementById('days-back'),
        applyFiltersBtn: document.getElementById('apply-filters-btn'),
        historyList: document.getElementById('history-list'),
        loadingState: document.getElementById('loading-state'),
        loadMoreBtn: document.getElementById('load-more-btn'),
        pagination: document.getElementById('pagination'),
        exportHistoryBtn: document.getElementById('export-history-btn'),
        clearHistoryBtn: document.getElementById('clear-history-btn'),
        exportModal: document.getElementById('export-modal'),
        closeExportModal: document.getElementById('close-export-modal'),
        cancelExportBtn: document.getElementById('cancel-export-btn'),
        confirmExportBtn: document.getElementById('confirm-export-btn'),
        exportFormat: document.getElementById('export-format'),
        exportDaysBack: document.getElementById('export-days-back'),
        totalSearches: document.getElementById('total-searches'),
        uniqueIdentities: document.getElementById('unique-identities'),
        watchlistAlerts: document.getElementById('watchlist-alerts')
    };

    // Initialize
    document.addEventListener('DOMContentLoaded', () => {
        setupEventListeners();
        loadHistory();
    });

    function setupEventListeners() {
        elements.applyFiltersBtn.addEventListener('click', () => {
            state.filters.searchType = elements.filterType.value;
            state.filters.daysBack = parseInt(elements.daysBack.value);
            state.currentOffset = 0;
            state.history = [];
            loadHistory();
        });

        elements.loadMoreBtn.addEventListener('click', loadMore);

        elements.exportHistoryBtn.addEventListener('click', () => {
            elements.exportModal.classList.add('active');
        });

        elements.closeExportModal.addEventListener('click', closeExportModal);
        elements.cancelExportBtn.addEventListener('click', closeExportModal);
        elements.confirmExportBtn.addEventListener('click', exportHistory);

        elements.clearHistoryBtn.addEventListener('click', () => {
            if (confirm('Are you sure you want to clear all search history? This action cannot be undone.')) {
                clearHistory();
            }
        });

        // Close modal on outside click
        elements.exportModal.addEventListener('click', (e) => {
            if (e.target === elements.exportModal) {
                closeExportModal();
            }
        });
    }

    async function loadHistory() {
        elements.loadingState.style.display = 'block';
        elements.historyList.innerHTML = '';
        elements.historyList.appendChild(elements.loadingState);

        try {
            const params = new URLSearchParams({
                days_back: state.filters.daysBack,
                limit: state.limit,
                offset: state.currentOffset
            });

            if (state.filters.searchType) {
                params.append('search_type', state.filters.searchType);
            }

            const response = await fetch(`/api/search/history?${params}`);
            
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            const history = await response.json();
            
            if (history.length === 0 && state.currentOffset === 0) {
                showEmptyState();
                return;
            }

            state.history = state.currentOffset === 0 ? history : [...state.history, ...history];
            state.hasMore = history.length === state.limit;
            
            renderHistory();
            updateStats();
            
            if (state.hasMore) {
                elements.pagination.style.display = 'block';
            } else {
                elements.pagination.style.display = 'none';
            }

        } catch (error) {
            console.error('Error loading history:', error);
            showError('Failed to load search history. Please try again.');
        } finally {
            elements.loadingState.style.display = 'none';
        }
    }

    function loadMore() {
        state.currentOffset += state.limit;
        loadHistory();
    }

    function renderHistory() {
        elements.historyList.innerHTML = '';

        if (state.history.length === 0) {
            showEmptyState();
            return;
        }

        state.history.forEach(item => {
            const historyItem = createHistoryItem(item);
            elements.historyList.appendChild(historyItem);
        });
    }

    function createHistoryItem(item) {
        const div = document.createElement('div');
        div.className = 'history-item';

        const typeIcon = getTypeIcon(item.search_type);
        const typeLabel = getTypeLabel(item.search_type);
        const date = new Date(item.created_at);
        const formattedDate = date.toLocaleString();

        div.innerHTML = `
            <div class="history-item-header">
                <div class="history-item-type">
                    <i class="${typeIcon}"></i>
                    <span>${typeLabel}</span>
                </div>
                <div class="history-item-date">${formattedDate}</div>
            </div>
            <div class="history-item-details">
                <div class="detail-item">
                    <span class="detail-label">Scope</span>
                    <span class="detail-value">${item.scope || 'N/A'}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Faces Detected</span>
                    <span class="detail-value">${item.input_faces_count || 0}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Results</span>
                    <span class="detail-value">${item.results_count || 0}</span>
                </div>
                <div class="detail-item">
                    <span class="detail-label">Unique Identities</span>
                    <span class="detail-value">${item.unique_identities_count || 0}</span>
                </div>
                ${item.watchlist_alerts_count > 0 ? `
                <div class="detail-item">
                    <span class="detail-label">Watchlist Alerts</span>
                    <span class="detail-value" style="color: #ff6b6b;">${item.watchlist_alerts_count}</span>
                </div>
                ` : ''}
                <div class="detail-item">
                    <span class="detail-label">Processing Time</span>
                    <span class="detail-value">${item.processing_time_ms || 0}ms</span>
                </div>
            </div>
            <div class="history-item-actions">
                <button class="btn-small btn-small-primary" onclick="rerunSearch('${item.id}')">
                    <i class="fas fa-redo"></i> Rerun
                </button>
                <button class="btn-small btn-small-primary" onclick="viewSearchDetails('${item.id}')">
                    <i class="fas fa-eye"></i> View Details
                </button>
            </div>
        `;

        return div;
    }

    function getTypeIcon(type) {
        const icons = {
            'single': 'fas fa-user',
            'multi': 'fas fa-users',
            'batch': 'fas fa-layer-group'
        };
        return icons[type] || 'fas fa-search';
    }

    function getTypeLabel(type) {
        const labels = {
            'single': 'Single Search',
            'multi': 'Multi-Face Search',
            'batch': 'Batch Search'
        };
        return labels[type] || 'Search';
    }

    function updateStats() {
        const total = state.history.length;
        const uniqueIds = new Set();
        let watchlistAlerts = 0;

        state.history.forEach(item => {
            if (item.unique_identities_count) {
                // Approximate unique identities
                uniqueIds.add(item.id);
            }
            if (item.watchlist_alerts_count) {
                watchlistAlerts += item.watchlist_alerts_count;
            }
        });

        elements.totalSearches.textContent = total;
        elements.uniqueIdentities.textContent = uniqueIds.size;
        elements.watchlistAlerts.textContent = watchlistAlerts;
    }

    function showEmptyState() {
        elements.historyList.innerHTML = `
            <div class="empty-state">
                <i class="fas fa-inbox"></i>
                <h3>No Search History</h3>
                <p>Your search history will appear here once you perform searches.</p>
            </div>
        `;
    }

    function showError(message) {
        elements.historyList.innerHTML = `
            <div class="empty-state">
                <i class="fas fa-exclamation-triangle"></i>
                <h3>Error</h3>
                <p>${message}</p>
            </div>
        `;
    }

    async function exportHistory() {
        const format = elements.exportFormat.value;
        const daysBack = parseInt(elements.exportDaysBack.value);

        try {
            const url = `/api/search/history/export?format=${format}&days_back=${daysBack}`;
            const response = await fetch(url);

            if (!response.ok) {
                throw new Error('Export failed');
            }

            const blob = await response.blob();
            const downloadUrl = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = downloadUrl;
            a.download = `search-history-${new Date().toISOString().split('T')[0]}.${format}`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(downloadUrl);

            closeExportModal();
            showNotification('History exported successfully!', 'success');
        } catch (error) {
            console.error('Export error:', error);
            showNotification('Failed to export history', 'error');
        }
    }

    function closeExportModal() {
        elements.exportModal.classList.remove('active');
    }

    async function clearHistory() {
        // Note: This would require a DELETE endpoint
        // For now, just show a message
        showNotification('Clear history feature requires backend endpoint', 'info');
    }

    function rerunSearch(searchId) {
        // Navigate to search page with search ID
        window.location.href = `/admin/search?rerun=${searchId}`;
    }

    function viewSearchDetails(searchId) {
        // Find the search item in history
        const searchItem = state.history.find(item => item.id === searchId);
        
        if (!searchItem) {
            showNotification('Search details not found', 'error');
            return;
        }
        
        // Show details modal
        showSearchDetailsModal(searchItem);
    }

    function showSearchDetailsModal(item) {
        // Create modal if it doesn't exist
        let modal = document.getElementById('search-details-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'search-details-modal';
            modal.className = 'modal';
            modal.innerHTML = `
                <div class="modal-content" style="max-width: 800px; max-height: 90vh;">
                    <div class="modal-header">
                        <h3>Search Details</h3>
                        <button class="modal-close" id="close-details-modal">
                            <i class="fas fa-times"></i>
                        </button>
                    </div>
                    <div class="modal-body" id="search-details-content" style="overflow-y: auto; max-height: calc(90vh - 150px);">
                        <!-- Content will be inserted here -->
                    </div>
                    <div class="modal-footer">
                        <button class="btn-secondary" id="close-details-btn">Close</button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);
            
            // Add event listeners
            document.getElementById('close-details-modal').addEventListener('click', closeDetailsModal);
            document.getElementById('close-details-btn').addEventListener('click', closeDetailsModal);
            modal.addEventListener('click', (e) => {
                if (e.target === modal) {
                    closeDetailsModal();
                }
            });
        }
        
        // Populate content
        const content = document.getElementById('search-details-content');
        const date = new Date(item.created_at);
        const formattedDate = date.toLocaleString();
        const typeIcon = getTypeIcon(item.search_type);
        const typeLabel = getTypeLabel(item.search_type);
        
        content.innerHTML = `
            <div class="details-section">
                <h4><i class="fas fa-info-circle"></i> Search Information</h4>
                <div class="details-grid">
                    <div class="detail-row">
                        <span class="detail-label">Search Type:</span>
                        <span class="detail-value">
                            <i class="${typeIcon}"></i> ${typeLabel}
                        </span>
                    </div>
                    <div class="detail-row">
                        <span class="detail-label">Date & Time:</span>
                        <span class="detail-value">${formattedDate}</span>
                    </div>
                    <div class="detail-row">
                        <span class="detail-label">Search ID:</span>
                        <span class="detail-value" style="font-family: monospace; font-size: 0.9rem;">${item.id}</span>
                    </div>
                    <div class="detail-row">
                        <span class="detail-label">Scope:</span>
                        <span class="detail-value">${item.scope || 'N/A'}</span>
                    </div>
                </div>
            </div>
            
            <div class="details-section">
                <h4><i class="fas fa-chart-bar"></i> Statistics</h4>
                <div class="details-grid">
                    <div class="detail-row">
                        <span class="detail-label">Faces Detected:</span>
                        <span class="detail-value">${item.input_faces_count || 0}</span>
                    </div>
                    <div class="detail-row">
                        <span class="detail-label">Total Results:</span>
                        <span class="detail-value">${item.results_count || 0}</span>
                    </div>
                    <div class="detail-row">
                        <span class="detail-label">Unique Identities:</span>
                        <span class="detail-value">${item.unique_identities_count || 0}</span>
                    </div>
                    <div class="detail-row">
                        <span class="detail-label">Watchlist Alerts:</span>
                        <span class="detail-value" style="color: ${item.watchlist_alerts_count > 0 ? '#ff6b6b' : '#666'};">
                            ${item.watchlist_alerts_count || 0}
                        </span>
                    </div>
                    <div class="detail-row">
                        <span class="detail-label">Processing Time:</span>
                        <span class="detail-value">${item.processing_time_ms || 0}ms</span>
                    </div>
                </div>
            </div>
            
            ${item.watchlist_alerts_count > 0 ? `
            <div class="details-section" style="background: rgba(255, 107, 107, 0.1); border-left: 4px solid #ff6b6b;">
                <h4 style="color: #ff6b6b;"><i class="fas fa-exclamation-triangle"></i> Watchlist Alerts</h4>
                <p>This search triggered ${item.watchlist_alerts_count} watchlist alert(s). Review the results for more details.</p>
            </div>
            ` : ''}
        `;
        
        // Show modal
        modal.classList.add('active');
    }

    function closeDetailsModal() {
        const modal = document.getElementById('search-details-modal');
        if (modal) {
            modal.classList.remove('active');
        }
    }

    function showNotification(message, type = 'info') {
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
        `;
        notification.textContent = message;
        document.body.appendChild(notification);

        setTimeout(() => {
            notification.style.opacity = '0';
            notification.style.transition = 'opacity 0.3s';
            setTimeout(() => notification.remove(), 300);
        }, 3000);
    }

    // Make functions available globally for onclick handlers
    window.rerunSearch = rerunSearch;
    window.viewSearchDetails = viewSearchDetails;
})();

