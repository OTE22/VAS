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
        pipelines: []
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
        exportIncludeQuality: document.getElementById('export-include-quality'),
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
            
            const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/'));
            if (files.length === 0) {
                showNotification('Please drop image files only', 'error');
                return;
            }

            if (state.isBatchMode) {
                addBatchFiles(files);
            } else {
                handleFileSelect(files[0]);
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
            elements.imagePreview.style.display = 'none';
            
            if (state.isBatchMode) {
                elements.uploadArea.querySelector('p').textContent = 'Drag & drop multiple images';
            } else {
                elements.uploadArea.querySelector('p').textContent = 'Drag & drop image here';
                state.batchFiles = [];
                renderBatchFiles();
            }
            updateSearchButton();
        });
    }

    function handleFileSelect(file) {
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
        // Limit to 100 files
        const remaining = 100 - state.batchFiles.length;
        const toAdd = files.slice(0, remaining);
        
        state.batchFiles.push(...toAdd);
        renderBatchFiles();
        updateSearchButton();
        
        if (files.length > remaining) {
            showNotification(`Only ${remaining} more files can be added (max 100)`, 'info');
        }
    }

    function renderBatchFiles() {
        elements.batchFilesList.innerHTML = state.batchFiles.map((file, index) => `
            <div class="batch-file-item">
                <i class="fas fa-image"></i>
                <span class="file-name">${file.name}</span>
                <span class="remove-file" onclick="removeBatchFile(${index})">
                    <i class="fas fa-times"></i>
                </span>
            </div>
        `).join('');
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
        
        state.isSearching = true;
        elements.searchingOverlay.style.display = 'flex';
        updateSearchButton();

        try {
            let results;
            
            if (state.isBatchMode) {
                results = await performBatchSearch();
            } else {
                results = await performSingleSearch();
            }

            state.currentResults = results;
            displayResults(results);
            
        } catch (error) {
            console.error('Search error:', error);
            showNotification(`Search failed: ${error.message}`, 'error');
        } finally {
            state.isSearching = false;
            elements.searchingOverlay.style.display = 'none';
            updateSearchButton();
        }
    }

    async function performSingleSearch() {
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
            body: formData
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Search failed');
        }

        return await response.json();
    }

    async function performBatchSearch() {
        const formData = new FormData();
        
        state.batchFiles.forEach(file => {
            formData.append('images', file);
        });
        
        formData.append('scope', elements.searchScope.value);
        formData.append('top_k', elements.topK.value);
        formData.append('check_watchlist', elements.checkWatchlist.checked);

        const response = await fetch('/api/search/batch', {
            method: 'POST',
            credentials: 'include',
            body: formData
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Batch search failed');
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

        try {
            const formData = new FormData();
            formData.append('image', state.selectedFile);

            const response = await fetch('/api/search/quality-check', {
                method: 'POST',
                credentials: 'include',
                body: formData
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'Quality check failed');
            }

            const quality = await response.json();
            displayQualityResult(quality);

        } catch (error) {
            console.error('Quality check error:', error);
            showNotification(`Quality check failed: ${error.message}`, 'error');
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
                        <div style="text-transform: uppercase; font-weight: 700; color: ${bandColors[quality.band]}">${quality.band}</div>
                        <div style="font-size: 0.85rem; color: rgba(255,255,255,0.6)">${quality.proceed_recommendation ? 'Ready for search' : 'Quality too low'}</div>
                    </div>
                </div>
                
                <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.5rem; margin-bottom: 1rem;">
                    ${Object.entries(quality.details).map(([key, value]) => `
                        <div style="background: rgba(0,0,0,0.2); padding: 0.5rem; border-radius: 4px;">
                            <div style="font-size: 0.75rem; color: rgba(255,255,255,0.5); text-transform: uppercase;">${key}</div>
                            <div style="color: ${value.score >= 0.7 ? '#00ff96' : value.score >= 0.5 ? '#ffcc00' : '#ff4444'}">${Math.round(value.score * 100)}%</div>
                        </div>
                    `).join('')}
                </div>
                
                ${quality.warnings.length > 0 ? `
                    <div style="margin-top: 1rem;">
                        ${quality.warnings.map(w => `
                            <div style="color: #ffcc00; font-size: 0.85rem; margin-bottom: 0.25rem;">${w}</div>
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
            totalAlerts = results.watchlist_alerts ? (Array.isArray(results.watchlist_alerts) ? results.watchlist_alerts.length : results.watchlist_alerts) : 0;
        } else {
            // Single search results (standard format)
            const faces = results.faces || [];
            faces.forEach(face => {
                totalMatches += face.matches ? face.matches.length : 0;
            });
            totalAlerts = results.watchlist_alerts ? (Array.isArray(results.watchlist_alerts) ? results.watchlist_alerts.length : 0) : 0;
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
                                    <h4>${imgResult.image_name}</h4>
                                    <span style="color: #ff4444;">Error: ${imgResult.error_message || 'Processing failed'}</span>
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
                                <h4>${imgResult.image_name}</h4>
                                <div class="face-quality">
                                    <span style="color: rgba(255,255,255,0.5); font-size: 0.8rem;">
                                        ${imgResult.faces_detected} face(s) detected • ${imgResult.matches?.length || 0} match(es)
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
                                    <span style="color: #ffcc00; font-size: 0.85rem;">Skipped: ${face.skip_reason}</span>
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
                                        <div class="quality-fill ${qualityClass}" style="width: ${face.quality_score * 100}%"></div>
                                    </div>
                                    <span class="quality-text">${Math.round(face.quality_score * 100)}% quality</span>
                                </div>
                                ${face.quality_warning ? `<div style="color: #ffcc00; font-size: 0.75rem; margin-top: 0.25rem;">${face.quality_warning}</div>` : ''}
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
            const similarityClass = getSimilarityClass(match.similarity);
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
            // Final fallback: user icon SVG
            if (!snapshotUrl) {
                snapshotUrl = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="100" height="100"%3E%3Crect fill="%23333" width="100" height="100"/%3E%3Ccircle cx="50" cy="35" r="15" fill="%23999"/%3E%3Cpath d="M 25 70 Q 25 60 35 60 L 65 60 Q 75 60 75 70 L 75 85 L 25 85 Z" fill="%23999"/%3E%3C/svg%3E';
            }
            const fallbackSvg = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="100" height="100"%3E%3Crect fill="%23333" width="100" height="100"/%3E%3Ccircle cx="50" cy="35" r="15" fill="%23999"/%3E%3Cpath d="M 25 70 Q 25 60 35 60 L 65 60 Q 75 60 75 70 L 75 85 L 25 85 Z" fill="%23999"/%3E%3C/svg%3E';

            return `
                <div class="match-card" onclick="viewIdentity('${match.identity_id}')">
                    <img class="match-snapshot" src="${snapshotUrl}" alt="${match.display_name || 'Match'}" 
                         onerror="this.onerror=null; this.src='${fallbackSvg}';" 
                         style="display: block; width: 50px; height: 50px; object-fit: cover; border-radius: 6px;">
                    <div class="match-details">
                        <div class="match-name">${match.display_name || 'Unknown'}</div>
                        <div class="match-meta">
                            <span><i class="fas fa-${match.type === 'known' ? 'user-check' : 'user-secret'}"></i> ${match.type}</span>
                            <span><i class="fas fa-eye"></i> ${match.appearances_count || 0}</span>
                        </div>
                        ${match.watchlist_match ? `
                            <div class="watchlist-badge ${match.watchlist_match.alert_level}" style="margin-top: 0.3rem;">
                                <i class="fas fa-exclamation-triangle"></i>
                                ${match.watchlist_match.list_name}
                            </div>
                        ` : ''}
                    </div>
                    <div class="match-similarity ${similarityClass}">${Math.round(match.similarity * 100)}%</div>
                </div>
            `;
        }).join('');
    }

    function renderAlertsTab(results) {
        const alerts = results.watchlist_alerts || [];

        if (alerts.length === 0) {
            elements.alertsTab.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-check-circle" style="color: #00ff96;"></i>
                    <h3>No Watchlist Alerts</h3>
                    <p>No matches found on any watchlists</p>
                </div>
            `;
            return;
        }

        elements.alertsTab.innerHTML = alerts.map(alert => `
            <div class="alert-card ${alert.alert_level}">
                <div class="alert-header">
                    <div class="alert-icon ${alert.alert_level}">
                        <i class="fas fa-${alert.alert_level === 'critical' ? 'skull-crossbones' : 'exclamation-triangle'}"></i>
                    </div>
                    <div class="alert-title">
                        <h4>${alert.identity_name || 'Unknown Identity'}</h4>
                        <p>${alert.list_name} • ${Math.round(alert.similarity * 100)}% match</p>
                    </div>
                </div>
                ${alert.notes ? `<div style="color: rgba(255,255,255,0.7); font-size: 0.85rem; margin-bottom: 0.5rem;">${alert.notes}</div>` : ''}
                ${alert.action_instructions ? `
                    <div style="background: rgba(0,0,0,0.3); padding: 0.75rem; border-radius: 6px; margin-top: 0.5rem;">
                        <div style="color: #ffcc00; font-size: 0.75rem; text-transform: uppercase; margin-bottom: 0.25rem;">Action Required</div>
                        <div style="color: #fff; font-size: 0.85rem;">${alert.action_instructions}</div>
                    </div>
                ` : ''}
            </div>
        `).join('');
    }

    function renderSummaryTab(results) {
        const summary = results.summary || {};
        const isBatch = !!results.batch_id;

        const html = `
            <div class="summary-stats">
                <div class="stat-card">
                    <div class="stat-value">${isBatch ? results.total_images : summary.total_faces_detected || 0}</div>
                    <div class="stat-label">${isBatch ? 'Images' : 'Faces'}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${summary.total_matches || results.total_matches || 0}</div>
                    <div class="stat-label">Matches</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${summary.unique_identities_found || results.unique_identities || 0}</div>
                    <div class="stat-label">Unique IDs</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${summary.watchlist_alerts || results.watchlist_alerts?.length || 0}</div>
                    <div class="stat-label">Alerts</div>
                </div>
            </div>

            <div class="search-section" style="margin-top: 1rem;">
                <h3><i class="fas fa-info-circle"></i> Search Details</h3>
                <div style="display: grid; gap: 0.5rem; font-size: 0.85rem;">
                    <div style="display: flex; justify-content: space-between;">
                        <span style="color: rgba(255,255,255,0.6);">Search ID</span>
                        <span style="font-family: monospace;">${results.search_id || results.batch_id}</span>
                    </div>
                    <div style="display: flex; justify-content: space-between;">
                        <span style="color: rgba(255,255,255,0.6);">Processing Time</span>
                        <span>${results.processing_time_ms}ms</span>
                    </div>
                    ${isBatch ? `
                        <div style="display: flex; justify-content: space-between;">
                            <span style="color: rgba(255,255,255,0.6);">Successful Images</span>
                            <span style="color: #00ff96;">${results.successful_images}/${results.total_images}</span>
                        </div>
                    ` : `
                        <div style="display: flex; justify-content: space-between;">
                            <span style="color: rgba(255,255,255,0.6);">Faces Searched</span>
                            <span style="color: #00ff96;">${summary.faces_searchable}/${summary.total_faces_detected}</span>
                        </div>
                    `}
                </div>
            </div>

            ${!isBatch && results.faces && results.faces.some(f => f.quality_score < 0.5) ? `
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

    // ============================================
    // Export Functions
    // ============================================
    async function exportResults(format) {
        if (!state.currentResults) {
            showNotification('No results to export', 'info');
            return;
        }

        try {
            const endpoint = state.currentResults.batch_id ? 
                '/api/search/batch/export' : '/api/search/export';

            const response = await fetch(`${endpoint}?format=${format}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                credentials: 'include',
                body: JSON.stringify(state.currentResults)
            });

            if (!response.ok) {
                throw new Error('Export failed');
            }

            // Download file
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `search_results.${format}`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(url);

            showNotification(`Exported as ${format.toUpperCase()}`, 'success');

        } catch (error) {
            console.error('Export error:', error);
            showNotification(`Export failed: ${error.message}`, 'error');
        }
    }

    // ============================================
    // Helper Functions
    // ============================================
    function getQualityClass(score) {
        if (score >= 0.85) return 'excellent';
        if (score >= 0.70) return 'good';
        if (score >= 0.50) return 'moderate';
        if (score >= 0.30) return 'poor';
        return 'unusable';
    }

    function getSimilarityClass(similarity) {
        if (similarity >= 0.90) return 'very-high';
        if (similarity >= 0.75) return 'high';
        if (similarity >= 0.60) return 'medium';
        return 'low';
    }

    window.viewIdentity = function(identityId) {
        // Pass current page as referrer so we can return here after closing modal
        const currentPage = window.location.pathname;
        window.location.href = `/admin/unknown?view=${identityId}&from=${encodeURIComponent(currentPage)}`;
    };

    // ============================================
    // Load Pipelines
    // ============================================
    async function loadPipelines() {
        try {
            const response = await fetch('/api/pipelines', {
                credentials: 'include'
            });

            if (response.ok) {
                const pipelines = await response.json();
                state.pipelines = pipelines;

                elements.pipelineFilter.innerHTML = '<option value="">All Cameras</option>' +
                    pipelines.map(p => `<option value="${p.pipeline_id}">${p.pipeline_id}</option>`).join('');
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

    async function handleExport() {
        if (!state.currentResults) {
            showNotification('No results to export', 'info');
            return;
        }

        const format = elements.exportFormat?.value || 'csv';
        const includeImages = elements.exportIncludeImages?.checked || false;
        const includeQuality = elements.exportIncludeQuality?.checked || true;

        try {
            const endpoint = state.currentResults.batch_id ? 
                '/api/search/batch/export' : '/api/search/export';
            
            let url = `${endpoint}?format=${format}`;
            if (includeImages) url += '&include_images=true';
            if (!includeQuality) url += '&include_quality=false';

            const response = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                credentials: 'include',
                body: JSON.stringify(state.currentResults)
            });

            if (!response.ok) {
                throw new Error('Export failed');
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
            console.error('Export error:', error);
            showNotification(`Export failed: ${error.message}`, 'error');
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
    
    function renderIdentitiesDropdown(searchTerm = '') {
        if (!elements.excludeIdentitiesList) return;
        
        const filtered = identitiesData.filter(identity => {
            if (!searchTerm) return true;
            const name = (identity.display_name || '').toLowerCase();
            return name.includes(searchTerm.toLowerCase());
        });
        
        if (filtered.length === 0) {
            elements.excludeIdentitiesList.innerHTML = '<div class="exclude-empty-state">No identities found</div>';
            return;
        }
        
        elements.excludeIdentitiesList.innerHTML = filtered.map(identity => {
            const isSelected = selectedIdentities.has(identity.id);
            return `
                <div class="exclude-item ${isSelected ? 'selected' : ''}" 
                     data-id="${identity.id}" 
                     data-name="${(identity.display_name || identity.id.substring(0, 8)).replace(/"/g, '&quot;')}"
                     onclick="window.adminSearch?.toggleIdentity('${identity.id}', '${(identity.display_name || identity.id.substring(0, 8)).replace(/'/g, "\\'")}')">
                    <i class="fas ${isSelected ? 'fa-check-circle' : 'fa-circle'}"></i>
                    <span class="exclude-item-name">${identity.display_name || `Identity ${identity.id.substring(0, 8)}`}</span>
                    <span class="exclude-item-type">${identity.type || 'known'}</span>
                </div>
            `;
        }).join('');
    }
    
    function renderIdentitiesChips() {
        if (!elements.excludeIdentitiesChips) return;
        
        elements.excludeIdentitiesChips.innerHTML = Array.from(selectedIdentities).map(id => {
            const identity = identitiesData.find(i => i.id === id);
            if (!identity) return '';
            const name = identity.display_name || `Identity ${id.substring(0, 8)}`;
            return `
                <div class="exclude-chip" data-id="${id}">
                    <i class="fas fa-user"></i>
                    <span>${name}</span>
                    <span class="exclude-chip-remove" onclick="window.adminSearch?.removeIdentity('${id}')">
                        <i class="fas fa-times"></i>
                    </span>
                </div>
            `;
        }).join('');
        
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
    window.adminSearch.toggleIdentity = function(id, name) {
        if (selectedIdentities.has(id)) {
            selectedIdentities.delete(id);
        } else {
            selectedIdentities.add(id);
        }
        renderIdentitiesDropdown(elements.excludeIdentitiesSearch?.value || '');
        renderIdentitiesChips();
    };
    
    window.adminSearch.removeIdentity = function(id) {
        selectedIdentities.delete(id);
        renderIdentitiesDropdown(elements.excludeIdentitiesSearch?.value || '');
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
    
    function renderWatchlistsDropdown(searchTerm = '') {
        if (!elements.excludeWatchlistsList) return;
        
        const filtered = watchlistsData.filter(watchlist => {
            if (!searchTerm) return true;
            const name = (watchlist.name || '').toLowerCase();
            return name.includes(searchTerm.toLowerCase());
        });
        
        if (filtered.length === 0) {
            elements.excludeWatchlistsList.innerHTML = '<div class="exclude-empty-state">No watchlists found</div>';
            return;
        }
        
        elements.excludeWatchlistsList.innerHTML = filtered.map(watchlist => {
            const isSelected = selectedWatchlists.has(watchlist.id);
            const typeIcon = watchlist.type === 'vip' ? 'fa-crown' : 
                           watchlist.type === 'threat' ? 'fa-exclamation-triangle' : 
                           watchlist.type === 'poi' ? 'fa-user-secret' : 'fa-list';
            return `
                <div class="exclude-item ${isSelected ? 'selected' : ''}" 
                     data-id="${watchlist.id}" 
                     data-name="${watchlist.name}"
                     onclick="window.adminSearch?.toggleWatchlist('${watchlist.id}', '${watchlist.name.replace(/'/g, "\\'")}')">
                    <i class="fas ${isSelected ? 'fa-check-circle' : 'fa-circle'}"></i>
                    <span class="exclude-item-name">${watchlist.name}</span>
                    <span class="exclude-item-type">${watchlist.type || 'custom'}</span>
                </div>
            `;
        }).join('');
    }
    
    function renderWatchlistsChips() {
        if (!elements.excludeWatchlistsChips) return;
        
        elements.excludeWatchlistsChips.innerHTML = Array.from(selectedWatchlists).map(id => {
            const watchlist = watchlistsData.find(w => w.id === id);
            if (!watchlist) return '';
            return `
                <div class="exclude-chip" data-id="${id}">
                    <i class="fas fa-list-alt"></i>
                    <span>${watchlist.name}</span>
                    <span class="exclude-chip-remove" onclick="window.adminSearch?.removeWatchlist('${id}')">
                        <i class="fas fa-times"></i>
                    </span>
                </div>
            `;
        }).join('');
        
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
    
    window.adminSearch.toggleWatchlist = function(id, name) {
        if (selectedWatchlists.has(id)) {
            selectedWatchlists.delete(id);
        } else {
            selectedWatchlists.add(id);
        }
        renderWatchlistsDropdown(elements.excludeWatchlistsSearch?.value || '');
        renderWatchlistsChips();
    };
    
    window.adminSearch.removeWatchlist = function(id) {
        selectedWatchlists.delete(id);
        renderWatchlistsDropdown(elements.excludeWatchlistsSearch?.value || '');
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
        
        // Close dropdowns when clicking outside
        document.addEventListener('click', (e) => {
            if (elements.excludeIdentitiesDropdown && 
                !elements.excludeIdentitiesDropdown.contains(e.target) &&
                !elements.excludeIdentitiesSearch?.contains(e.target)) {
                elements.excludeIdentitiesDropdown.classList.remove('show');
            }
            
            if (elements.excludeWatchlistsDropdown && 
                !elements.excludeWatchlistsDropdown.contains(e.target) &&
                !elements.excludeWatchlistsSearch?.contains(e.target)) {
                elements.excludeWatchlistsDropdown.classList.remove('show');
            }
        });
    }

    // ============================================
    // Initialize
    // ============================================
    function init() {
        console.log('Initializing Advanced Search...');
        
        setupUploadHandlers();
        setupEventListeners();
        loadPipelines();
        loadIdentitiesForExclusion();
        loadWatchlistsForExclusion();
        setupExcludeUI();

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

