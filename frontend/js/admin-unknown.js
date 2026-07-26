/**
 * Unknown Faces Management
 * =======================
 * Admin interface for managing unknown faces and identities.
 */

let currentPage = 1;
let pageSize = 20;
let totalPages = 1;
let currentFilters = {};
// UNKNOWN_FACE_DISPLAY_HOURS display window: false = default recent-only view,
// true = Show-all (older unknowns stay STORED either way — display-only window)
let showAllUnknowns = false;
let currentIdentityId = null;
let multiSelectMode = false;
let selectedIdentities = new Set();
let pipelineGroupsPerPage = 12; // Number of pipeline groups per page
let allPipelineGroups = []; // Store all pipeline groups for pagination

// WebSocket connection for real-time unknown face updates
let ws = null;
let reconnectAttempts = 0;
const maxReconnectAttempts = 5;
let reconnectInterval = null;

// ---- Friendly pipeline display names (location_name) -----------------------
// pipeline_id stays the permanent identity everywhere (keys, data attributes,
// merge/prune logic); this map only affects what the USER reads. Populated
// from /api/pipelines in loadPipelines() and refreshed live from WS payloads.
// location_name is untrusted text: always render via escapeHtml/textContent.
let pipelineDisplayNames = {};

function getPipelineDisplayName(pipelineId) {
    return pipelineDisplayNames[pipelineId] || pipelineId;
}

function recordPipelineDisplayName(pipelineId, locationName) {
    if (!pipelineId) return;
    if (typeof locationName === 'string' && locationName.trim()) {
        const trimmed = locationName.trim();
        if (pipelineDisplayNames[pipelineId] !== trimmed) {
            pipelineDisplayNames[pipelineId] = trimmed;
            updatePipelineGroupTitle(pipelineId);
        }
    }
}

// Update an already-rendered pipeline group card title in place (safe DOM ops)
function updatePipelineGroupTitle(pipelineId) {
    document.querySelectorAll(
        `.pipeline-group-title[data-pipeline-id="${CSS.escape(pipelineId)}"]`
    ).forEach(titleEl => {
        titleEl.textContent = getPipelineDisplayName(pipelineId);
        titleEl.title = pipelineId;
    });
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', async () => {
    await loadUserInfo();
    await displayUserPrivileges();
    await checkUserRoleForLiveAlerts(); // Check and show "YOUR LIVE ALERTS" button for regular users
    await loadPipelines(); // Load pipelines for dropdown filter
    await loadUnknownFaces();
    setupEventListeners();
    connectWebSocket(); // Connect to WebSocket for real-time updates
    
    // Backend handles ?view= parameter logic and injects identity data
    // Frontend just reads the injected data if present (no URL parsing needed)
    // The injected script tag will automatically call viewIdentityDetails()
});

// Load user info for UI customization only - BACKEND HANDLES ALL AUTHENTICATION
// Backend route already authenticated user and validated access, exception handler handles redirects
async function loadUserInfo() {
    try {
        // Fetch user info for UI customization only (not for authentication)
        // Backend already authenticated user and validated access before serving this page
        const user = window.getAuthMe
            ? await window.getAuthMe()
            : await fetch('/api/auth/me', { credentials: 'include' }).then(r => r.ok ? r.json() : null);

        if (user) {
            // Get privileges from backend for display purposes only
            try {
                const privileges = window.getAuthPrivileges
                    ? await window.getAuthPrivileges()
                    : await fetch('/api/auth/me/privileges', { credentials: 'include' }).then(r => r.ok ? r.json() : null);

                if (privileges) {
                    // Store user info and pipelines for later use (for filtering, display, etc.)
                    window.currentUser = user;
                    window.userPipelines = privileges.user.role === 'admin' ? null : privileges.pipelines; // null means all pipelines for admin
                } else {
                    // If we can't get privileges, still allow page (backend already validated)
                    window.currentUser = user;
                    window.userPipelines = user.role === 'admin' ? null : [];
                }
            } catch (error) {
                console.warn('Error fetching privileges:', error);
                window.currentUser = user;
                window.userPipelines = user.role === 'admin' ? null : [];
            }
        } else {
            // If we can't get user info, backend will handle redirect via exception handler
            console.warn('[UNKNOWN] Could not fetch user info, but backend already authenticated');
            window.currentUser = null;
            window.userPipelines = null;
        }
    } catch (error) {
        // Non-fatal error - backend already authenticated user
        console.warn('[UNKNOWN] Error loading user info (non-fatal):', error);
        window.currentUser = null;
        window.userPipelines = null;
    }
}

// Load pipelines for dropdown filter
async function loadPipelines() {
    try {
        const pipelineFilter = document.getElementById('pipeline-filter');
        if (!pipelineFilter) {
            console.warn('[UNKNOWN] Pipeline filter dropdown not found');
            return;
        }

        const response = await fetch('/api/pipelines', {
            credentials: 'include'
        });

        if (!response.ok) {
            console.error('[UNKNOWN] Failed to load pipelines:', response.status, response.statusText);
            pipelineFilter.innerHTML = '<option value="">All Pipelines (Error loading)</option>';
            return;
        }

        const pipelines = await response.json();

        // Refresh the friendly display-name map (admin/DB name is the source of truth)
        const nameMap = {};
        (Array.isArray(pipelines) ? pipelines : []).forEach(p => {
            if (p && p.pipeline_id) {
                nameMap[p.pipeline_id] = (p.location_name && p.location_name.trim()) || p.pipeline_id;
            }
        });
        pipelineDisplayNames = nameMap;

        // Filter pipelines based on user access
        let accessiblePipelines = pipelines;
        if (window.userPipelines !== null && window.userPipelines !== undefined) {
            // Regular user: only show pipelines they have access to
            if (Array.isArray(window.userPipelines) && window.userPipelines.length > 0) {
                accessiblePipelines = pipelines.filter(p => window.userPipelines.includes(p.pipeline_id));
            } else {
                // User has no pipeline access
                accessiblePipelines = [];
            }
        }
        // Admin users (window.userPipelines === null) see all pipelines

        // Populate dropdown
        pipelineFilter.innerHTML = '<option value="">All Pipelines</option>';
        
        if (accessiblePipelines.length === 0) {
            pipelineFilter.innerHTML += '<option value="" disabled>No pipelines available</option>';
            console.warn('[UNKNOWN] No accessible pipelines found for user');
            return;
        }

        // Sort pipelines by pipeline_id for consistent ordering
        accessiblePipelines.sort((a, b) => {
            const nameA = (a.pipeline_name || a.pipeline_id || '').toLowerCase();
            const nameB = (b.pipeline_name || b.pipeline_id || '').toLowerCase();
            return nameA.localeCompare(nameB);
        });

        accessiblePipelines.forEach(pipeline => {
            const option = document.createElement('option');
            option.value = pipeline.pipeline_id;
            const displayName = pipeline.pipeline_name || pipeline.pipeline_id;
            option.textContent = displayName;
            option.title = `Pipeline: ${pipeline.pipeline_id}`;
            pipelineFilter.appendChild(option);
        });

        console.log(`[UNKNOWN] Loaded ${accessiblePipelines.length} pipeline(s) for dropdown filter`);
    } catch (error) {
        console.error('[UNKNOWN] Error loading pipelines:', error);
        const pipelineFilter = document.getElementById('pipeline-filter');
        if (pipelineFilter) {
            pipelineFilter.innerHTML = '<option value="">All Pipelines (Error loading)</option>';
        }
    }
}

// Display user privileges information
// Check user role and show/hide "YOUR LIVE ALERTS" button
async function checkUserRoleForLiveAlerts() {
    try {
        const privileges = window.getAuthPrivileges
            ? await window.getAuthPrivileges()
            : await fetch('/api/auth/me/privileges', { credentials: 'include' }).then(r => r.ok ? r.json() : null);
        if (privileges) {
            const userRole = privileges.user?.role || null;
            const canAccessUnknownFaces = privileges.can_access_unknown_faces || false;
            
            // Show "YOUR LIVE ALERTS" button for regular users with Unknown Faces access
            const liveAlertsBtn = document.getElementById('your-live-alerts-btn');
            if (liveAlertsBtn) {
                if (userRole !== 'admin' && canAccessUnknownFaces) {
                    liveAlertsBtn.style.display = 'block';
                } else {
                    liveAlertsBtn.style.display = 'none';
                }
            }
        }
    } catch (error) {
        console.error('Error checking user role for live alerts:', error);
    }
}

async function displayUserPrivileges() {
    const privilegesInfo = document.getElementById('user-privileges-info');
    const privilegesText = document.getElementById('privileges-text');
    
    if (!privilegesInfo || !privilegesText) return;
    
    try {
        // Get privileges from backend (single source of truth, shared promise)
        const privileges = window.getAuthPrivileges
            ? await window.getAuthPrivileges()
            : await fetch('/api/auth/me/privileges', { credentials: 'include' }).then(r => r.ok ? r.json() : null);

        if (!privileges) return;

        // Display privileges summary from backend
        privilegesInfo.style.display = 'flex';
        privilegesText.textContent = privileges.privileges_summary;
        
        // Set color based on access
        if (privileges.can_access_unknown_faces) {
            privilegesInfo.style.color = '#00ff88';
        } else {
            privilegesInfo.style.color = '#ff6b6b';
        }
    } catch (error) {
        console.error('Error displaying privileges:', error);
    }
}

// Setup event listeners
function setupEventListeners() {
    // Logout
    document.getElementById('logout-btn').addEventListener('click', () => {
        // Backend handles logout
    });

    // Filters
    document.getElementById('apply-filters-btn').addEventListener('click', () => {
        currentPage = 1;
        applyFilters();
    });

    document.getElementById('clear-filters-btn').addEventListener('click', () => {
        clearFilters();
    });

    // Show-all toggle for the UNKNOWN_FACE_DISPLAY_HOURS display window
    const showAllBtn = document.getElementById('show-all-toggle-btn');
    if (showAllBtn) {
        showAllBtn.addEventListener('click', () => {
            showAllUnknowns = !showAllUnknowns;
            showAllBtn.setAttribute('aria-pressed', showAllUnknowns ? 'true' : 'false');
            currentPage = 1;
            allPipelineGroups = [];
            loadUnknownFaces();
        });
    }

    // Pagination - if we have stored pipeline groups, just re-render, otherwise reload
    document.getElementById('prev-page-btn').addEventListener('click', () => {
        if (currentPage > 1) {
            currentPage--;
            if (allPipelineGroups.length > 0) {
                renderPipelineGroupsPage();
            } else {
                loadUnknownFaces();
            }
        }
    });

    document.getElementById('next-page-btn').addEventListener('click', () => {
        if (currentPage < totalPages) {
            currentPage++;
            if (allPipelineGroups.length > 0) {
                renderPipelineGroupsPage();
            } else {
                loadUnknownFaces();
            }
        }
    });

    // Quick search
    document.getElementById('search-by-image-btn').addEventListener('click', () => {
        document.getElementById('search-image-modal').style.display = 'flex';
    });

    document.getElementById('close-search-modal').addEventListener('click', () => {
        document.getElementById('search-image-modal').style.display = 'none';
    });

    document.getElementById('cancel-search-btn').addEventListener('click', () => {
        document.getElementById('search-image-modal').style.display = 'none';
    });

    // Image preview
    document.getElementById('search-image-file').addEventListener('change', (e) => {
        const file = e.target.files[0];
        if (file) {
            const reader = new FileReader();
            reader.onload = (event) => {
                document.getElementById('preview-img').src = event.target.result;
                document.getElementById('image-preview').style.display = 'block';
            };
            reader.readAsDataURL(file);
        }
    });

    // Search form
    document.getElementById('search-image-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        await searchByImage();
    });

    // Promote modal
    document.getElementById('close-promote-modal').addEventListener('click', () => {
        document.getElementById('promote-modal').style.display = 'none';
    });

    document.getElementById('cancel-promote-btn').addEventListener('click', () => {
        document.getElementById('promote-modal').style.display = 'none';
    });

    // Face detection alert modal event listeners
    const faceAlertModal = document.getElementById('face-detection-alert-modal');
    if (faceAlertModal) {
        // Close button
        const closeBtn = document.getElementById('close-face-alert-modal');
        if (closeBtn) {
            closeBtn.addEventListener('click', closeFaceDetectionAlert);
        }
        
        // OK button
        const okBtn = document.getElementById('face-alert-ok-btn');
        if (okBtn) {
            okBtn.addEventListener('click', closeFaceDetectionAlert);
        }
        
        // Close on background click
        faceAlertModal.addEventListener('click', (e) => {
            if (e.target === faceAlertModal) {
                closeFaceDetectionAlert();
            }
        });
        
        // Close on Escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && faceAlertModal.style.display === 'flex') {
                closeFaceDetectionAlert();
            }
        });
    }

    document.getElementById('promote-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        await promoteIdentity();
    });

    // Detail modal
    document.getElementById('close-detail-modal').addEventListener('click', () => {
        const modal = document.getElementById('identity-detail-modal');
        modal.style.display = 'none';
        
        // Backend handles redirect logic - check if we came from another page
        if (window.identityModalReferrer && window.identityModalReferrer !== window.location.pathname) {
            // Redirect back to the page we came from (without view parameter)
            window.location.href = window.identityModalReferrer;
        } else {
            // Remove view parameter from current URL if present
            const url = new URL(window.location.href);
            if (url.searchParams.has('view')) {
                url.searchParams.delete('view');
                url.searchParams.delete('from');
                window.history.replaceState({}, '', url.toString());
            }
        }
    });

    // Merge suggestions
    document.getElementById('merge-suggestions-btn').addEventListener('click', () => {
        document.getElementById('merge-suggestions-modal').style.display = 'flex';
        loadMergeSuggestions();
    });

    document.getElementById('close-suggestions-modal').addEventListener('click', () => {
        document.getElementById('merge-suggestions-modal').style.display = 'none';
    });
    
    // Pipeline merge suggestions modal
    document.getElementById('close-pipeline-suggestions-modal').addEventListener('click', () => {
        document.getElementById('pipeline-merge-suggestions-modal').style.display = 'none';
    });
    
    // Live Alert modal
    document.getElementById('close-live-alert-modal').addEventListener('click', () => {
        document.getElementById('create-live-alert-modal').style.display = 'none';
    });
    document.getElementById('cancel-live-alert-btn').addEventListener('click', () => {
        document.getElementById('create-live-alert-modal').style.display = 'none';
    });
    document.getElementById('create-live-alert-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        await createLiveAlert();
    });
    
    // Watchlist modal event listeners
    const closeWatchlistModal = document.getElementById('close-watchlist-modal');
    const cancelWatchlistBtn = document.getElementById('cancel-watchlist-btn');
    const addToWatchlistForm = document.getElementById('add-to-watchlist-form');
    
    if (closeWatchlistModal) {
        closeWatchlistModal.addEventListener('click', () => {
            document.getElementById('add-to-watchlist-modal').style.display = 'none';
        });
    }
    if (cancelWatchlistBtn) {
        cancelWatchlistBtn.addEventListener('click', () => {
            document.getElementById('add-to-watchlist-modal').style.display = 'none';
        });
    }
    if (addToWatchlistForm) {
        addToWatchlistForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            await addToWatchlist();
        });
    }
    
    // Live alert similarity slider
    const similaritySlider = document.getElementById('live-alert-min-similarity');
    const similarityValue = document.getElementById('live-alert-similarity-value');
    if (similaritySlider && similarityValue) {
        similaritySlider.addEventListener('input', (e) => {
            similarityValue.textContent = `${Math.round(e.target.value * 100)}%`;
        });
    }

    // Merge modal
    document.getElementById('close-merge-modal').addEventListener('click', () => {
        document.getElementById('merge-modal').style.display = 'none';
    });

    document.getElementById('cancel-merge-btn').addEventListener('click', () => {
        document.getElementById('merge-modal').style.display = 'none';
    });

    document.getElementById('merge-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        await mergeIdentities();
    });
    
    // Multi-select toggle
    document.getElementById('multi-select-toggle').addEventListener('click', toggleMultiSelectMode);
    
    // Merge multiple button
    document.getElementById('merge-multiple-btn').addEventListener('click', () => {
        if (selectedIdentities.size >= 2) {
            openMergeModal(null);
        }
    });
    
    // Pipeline merge suggestions buttons (delegated event listener for dynamically created buttons)
    document.addEventListener('click', async (e) => {
        if (e.target.closest('.pipeline-merge-suggestions-btn')) {
            const btn = e.target.closest('.pipeline-merge-suggestions-btn');
            const pipelineId = btn.getAttribute('data-pipeline-id');
            if (pipelineId) {
                console.log('[PIPELINE_MERGE] Button clicked for pipeline:', pipelineId);
                await openPipelineMergeSuggestions(pipelineId);
            }
        }
    });
    
    // Pipeline merge suggestions modal close button
    const closePipelineModal = document.getElementById('close-pipeline-suggestions-modal');
    if (closePipelineModal) {
        closePipelineModal.addEventListener('click', () => {
            document.getElementById('pipeline-merge-suggestions-modal').style.display = 'none';
        });
    }
}

// Load unknown faces
// Honest toggle label from the backend response (textContent only):
// window active -> offer "SHOW ALL (older than Nh hidden)";
// show-all view  -> offer "RECENT ONLY".
function updateShowAllToggle(data) {
    const label = document.getElementById('show-all-label');
    const btn = document.getElementById('show-all-toggle-btn');
    if (!label || !btn) return;
    if (data && data.show_all) {
        label.textContent = 'RECENT ONLY';
        btn.title = 'Showing ALL unknown faces. Click to show only the recent display window again.';
    } else if (data && data.display_window_hours) {
        label.textContent = `SHOW ALL (>${data.display_window_hours}h hidden)`;
        btn.title = `Showing unknown faces from the last ${data.display_window_hours}h (Settings → UNKNOWN_FACE_DISPLAY_HOURS). Older ones stay stored — click to reveal them.`;
    } else {
        // Window disabled (setting = 0) or filtered by explicit dates
        label.textContent = 'SHOW ALL';
        btn.title = 'Display window is disabled — all unknown faces are already listed.';
    }
}

async function loadUnknownFaces() {
    const grid = document.getElementById('unknown-grid');
    const loading = document.getElementById('loading-indicator');
    const noResults = document.getElementById('no-results');

    grid.innerHTML = '';
    loading.style.display = 'flex';
    noResults.style.display = 'none';

    try {
        const params = new URLSearchParams({
            page: currentPage,
            page_size: pageSize,
            ...currentFilters
        });
        if (showAllUnknowns) params.set('show_all', 'true');

        const response = await fetch(`/api/admin/unknown?${params}`, {
            credentials: 'include' // Include HttpOnly cookies
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Failed to load unknown faces' }));
            throw new Error(errorData.detail || `HTTP ${response.status}: Failed to load unknown faces`);
        }

        const data = await response.json();

        updateShowAllToggle(data);

        console.log('[UNKNOWN] Loaded data:', {
            total: data.total || data.identities?.length || 0,
            identities: data.identities?.length || 0,
            stats: data.stats,
            sample_identity: data.identities?.[0] ? {
                id: data.identities[0].id,
                has_snapshot_url: !!data.identities[0].snapshot_url,
                has_best_snapshot_path: !!data.identities[0].best_snapshot_path,
                snapshot_url: data.identities[0].snapshot_url,
                best_snapshot_path: data.identities[0].best_snapshot_path
            } : null
        });
        
        // Ensure snapshot_url is set for all identities (convert best_snapshot_path if needed)
        if (data.identities && Array.isArray(data.identities)) {
            data.identities.forEach(identity => {
                if (!identity.snapshot_url && identity.best_snapshot_path) {
                    // Convert path to URL
                    let path = identity.best_snapshot_path;
                    if (path.startsWith('storage/')) {
                        identity.snapshot_url = `/${path}`;
                    } else if (!path.startswith('/')) {
                        identity.snapshot_url = `/storage/${path}`;
                    } else {
                        identity.snapshot_url = path;
                    }
                }
            });
        }
        
        // Note: Pagination will be calculated after grouping by pipeline
        // This is just a placeholder - will be updated after pipeline grouping

        // Update stats from backend (accurate calculations)
        if (data.stats) {
            // Use backend-calculated stats (accurate)
            document.getElementById('total-unknown').textContent = data.stats.total_unknown || 0;
            document.getElementById('total-appearances').textContent = data.stats.total_appearances || 0;
            document.getElementById('total-cameras').textContent = data.stats.active_cameras || 0;
        } else {
            // Fallback to frontend calculation (less accurate)
            document.getElementById('total-unknown').textContent = data.total || 0;
            
            let totalAppearances = 0;
            let uniqueCameras = new Set();
            data.identities.forEach(identity => {
                totalAppearances += identity.appearances_count;
                if (identity.pipeline_ids && identity.pipeline_ids.length > 0) {
                    identity.pipeline_ids.forEach(pid => uniqueCameras.add(pid));
                }
            });
            document.getElementById('total-appearances').textContent = totalAppearances;
            document.getElementById('total-cameras').textContent = uniqueCameras.size;
        }

        // CRITICAL: Frontend safety filter - ensure only UNKNOWN identities are displayed
        // Backend should already filter, but this is a safety measure
        const unknownIdentities = data.identities.filter(identity => {
            const isUnknown = identity.type === 'unknown' || identity.type === 'UNKNOWN';
            if (!isUnknown) {
                console.warn(`[SECURITY] Frontend filtered out non-unknown identity: ${identity.id} (type: ${identity.type})`);
            }
            return isUnknown;
        });

        // Render identities grouped by pipeline
        if (unknownIdentities.length === 0) {
            loading.style.display = 'none';
            noResults.style.display = 'flex';
            return;
        }

        // Group identities by pipeline (skip identities with no pipeline IDs)
        const groupedByPipeline = {};
        
        unknownIdentities.forEach(identity => {
            const pipelineIds = identity.pipeline_ids || [];
            // Only process identities that have at least one pipeline ID
            if (pipelineIds.length > 0) {
                pipelineIds.forEach(pipelineId => {
                    if (!groupedByPipeline[pipelineId]) {
                        groupedByPipeline[pipelineId] = [];
                    }
                    groupedByPipeline[pipelineId].push(identity);
                });
            }
            // Identities with no pipeline IDs are completely skipped
        });

        // Store all pipeline groups
        const sortedPipelines = Object.keys(groupedByPipeline).sort();
        allPipelineGroups = sortedPipelines.map(pipelineId => ({
            id: pipelineId,
            identities: groupedByPipeline[pipelineId]
        }));
        
        // Render pipeline groups with pagination
        renderPipelineGroupsPage();
        
        // No Pipeline group is completely removed - identities with no pipeline IDs are filtered out

        loading.style.display = 'none';
    } catch (error) {
        console.error('Error loading unknown faces:', error);
        loading.style.display = 'none';
        const errorMessage = error.message || 'Error loading unknown faces';
        showNotification(errorMessage, 'error');
        
        // Show error in the grid as well
        grid.innerHTML = `
            <div class="no-results">
                <i class="fas fa-exclamation-triangle"></i>
                <p>Error: ${errorMessage}</p>
                <button class="intelligence-admin-btn" onclick="loadUnknownFaces()" style="margin-top: 1rem;">
                    <div class="btn-content">
                        <div class="btn-icon-wrapper">
                            <i class="fas fa-redo"></i>
                        </div>
                        <span class="btn-title">RETRY</span>
                    </div>
                </button>
            </div>
        `;
    }
}

// WebSocket Connection for Real-time Unknown Face Updates
// Variables are already declared at the top of the file (lines 18-21)
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // view=unknown tells the server this client wants unknown-face payloads
    // (they are filtered out for regular dashboard connections)
    const wsUrl = `${protocol}//${window.location.host}/ws?view=unknown`;

    console.log('[UNKNOWN] 🔌 Starting WebSocket connection...');
    console.log('[UNKNOWN] 🔗 WebSocket URL:', wsUrl);
    console.log('[UNKNOWN] 🔗 Current protocol:', window.location.protocol);
    console.log('[UNKNOWN] 🔗 Current host:', window.location.host);

    try {
        console.log('[UNKNOWN] 🚀 Creating WebSocket instance...');
        ws = new WebSocket(wsUrl);
        console.log('[UNKNOWN] ✅ WebSocket instance created, readyState:', ws.readyState, '(CONNECTING=0)');

        ws.onopen = () => {
            console.log('[UNKNOWN] ✅ WebSocket connected successfully');
            console.log('[UNKNOWN] 🔗 WebSocket URL:', wsUrl);
            console.log('[UNKNOWN] 🔗 WebSocket readyState:', ws.readyState, '(OPEN=1)');
            reconnectAttempts = 0;
            if (reconnectInterval) {
                clearInterval(reconnectInterval);
                reconnectInterval = null;
            }
        };

        ws.onmessage = (event) => {
            try {
                const message = JSON.parse(event.data);
                console.log('[UNKNOWN] 📨 Received WebSocket message:', message.type, message);
                handleWebSocketMessage(message);
            } catch (error) {
                console.error('[UNKNOWN] ❌ Error parsing WebSocket message:', error, 'Raw data:', event.data);
            }
        };

        ws.onerror = (error) => {
            console.error('[UNKNOWN] ❌ WebSocket error event:', error);
            console.error('[UNKNOWN] ❌ WebSocket readyState:', ws?.readyState);
            console.error('[UNKNOWN] ❌ WebSocket URL:', wsUrl);
        };

        ws.onclose = (event) => {
            console.log('[UNKNOWN] 🔌 WebSocket disconnected');
            console.log('[UNKNOWN] 📊 Close event details:', {
                code: event.code,
                reason: event.reason,
                wasClean: event.wasClean
            });
            attemptReconnect();
        };
    } catch (error) {
        console.error('[UNKNOWN] ❌ Failed to create WebSocket:', error);
        attemptReconnect();
    }
}

function attemptReconnect() {
    if (reconnectAttempts >= maxReconnectAttempts) {
        console.warn('[UNKNOWN] Max reconnection attempts reached');
        return;
    }

    if (!reconnectInterval) {
        // Exponential backoff + ±30% jitter (avoid reconnect thundering herd)
        const base = Math.min(1000 * Math.pow(2, reconnectAttempts), 30000);
        const delay = Math.round(base * (0.7 + Math.random() * 0.6));
        console.log(`[UNKNOWN] ⏳ Reconnecting in ${delay / 1000}s (attempt ${reconnectAttempts + 1}/${maxReconnectAttempts})`);

        reconnectInterval = setTimeout(() => {
            reconnectAttempts++;
            reconnectInterval = null;
            connectWebSocket();
        }, delay);
    }
}

function handleWebSocketMessage(message) {
    console.log('[UNKNOWN] 📨 Handling WebSocket message:', message.type);
    console.log('[UNKNOWN] 📦 Full message data:', JSON.stringify(message).substring(0, 500));

    switch (message.type) {
        case 'new_unknown_detection':
            console.log('[UNKNOWN] 🆕 Processing new_unknown_detection message');
            // Handle new unknown face detection in real-time
            const unknownData = message.data;
            const pipelineId = unknownData.pipeline_id;
            const face = unknownData.face;

            // Keep friendly card titles in sync with the live payload
            recordPipelineDisplayName(pipelineId, unknownData.location_name);
            
            console.log(`[UNKNOWN] 📊 Unknown detection data:`, {
                pipeline_id: pipelineId,
                timestamp: unknownData.timestamp,
                face_name: face?.name,
                identity_id: unknownData.identity_id,
                has_image: !!face?.image
            });
            
            // Filter: Only process if user has access to this pipeline
            if (window.userPipelines !== null && !window.userPipelines.includes(pipelineId)) {
                console.log(`[UNKNOWN] [FILTER] Skipping unknown face from pipeline ${pipelineId} - user does not have access (user pipelines: ${window.userPipelines})`);
                break;
            }
            console.log(`[UNKNOWN] ✅ Processing unknown face from pipeline ${pipelineId}`);
            
            // Check if identity already exists (if identity_id is provided)
            if (unknownData.identity_id) {
                // Try to find existing identity in current data
                const existingIdentity = findIdentityInPipelineGroups(unknownData.identity_id, pipelineId);
                if (existingIdentity) {
                    // Update existing identity (refresh its data) - OPTIMIZED: Update only the card, not entire page
                    console.log(`[UNKNOWN] 🔄 Updating existing identity ${unknownData.identity_id} in pipeline ${pipelineId}`);
                    
                    // Update the identity data in memory
                    const updatedIdentity = {
                        ...existingIdentity,
                        snapshot_url: face.image ? `data:image/jpeg;base64,${face.image}` : (face.face_image_path ? `/storage/${face.face_image_path.replace(/^.*storage[\/\\]/, '')}` : existingIdentity.snapshot_url),
                        last_seen_at: unknownData.timestamp,
                        appearances_count: (existingIdentity.appearances_count || 1) + 1,
                        best_snapshot_path: face.face_image_path || existingIdentity.best_snapshot_path
                    };
                    
                    // Update the identity in the pipeline group
                    const pipelineGroup = allPipelineGroups.find(g => g.id === pipelineId);
                    if (pipelineGroup) {
                        const identityIndex = pipelineGroup.identities.findIndex(id => id.id === unknownData.identity_id);
                        if (identityIndex >= 0) {
                            pipelineGroup.identities[identityIndex] = updatedIdentity;
                        }
                    }
                    
                    // Try to update the card in place (only if visible on current page)
                    const updated = updateIdentityCard(unknownData.identity_id, updatedIdentity);
                    
                    if (!updated) {
                        // Card not visible (on different page), just update data in memory
                        console.log(`[UNKNOWN] 📄 Identity ${unknownData.identity_id} updated in memory (not visible on current page)`);
                    } else {
                        console.log(`[UNKNOWN] ✅ Identity ${unknownData.identity_id} card updated in place`);
                    }
                    
                    // Update stats without re-rendering
                    updateStatsFromPipelineGroups();
                    break;
                }
            }
            
            // Create new identity object from detection data
            const newIdentity = {
                id: unknownData.identity_id || `temp_${Date.now()}`, // Use identity_id if available, otherwise temp ID
                type: 'unknown',
                display_name: face.name || 'Unknown',
                status: 'active',
                first_seen_at: unknownData.timestamp,
                last_seen_at: unknownData.timestamp,
                appearances_count: 1,
                best_snapshot_path: face.face_image_path || null,
                snapshot_url: face.image ? `data:image/jpeg;base64,${face.image}` : (face.face_image_path ? `/storage/${face.face_image_path.replace(/^.*storage[\/\\]/, '')}` : null),
                pipeline_ids: [pipelineId] // Single pipeline for this detection
            };
            
            // Add to the appropriate pipeline group
            addUnknownFaceToPipeline(pipelineId, newIdentity);
            break;
            
        case 'ping':
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'pong' }));
            }
            break;
            
        case 'initial_unknown_data':
            console.log('[UNKNOWN] 📥 Processing initial_unknown_data message');
            // Handle initial unknown faces data from WebSocket
            const unknownInitialData = message.data || [];
            console.log(`[UNKNOWN] 📊 Received ${unknownInitialData.length} pipeline entries with unknown faces`);
            
            // Process each pipeline entry
            for (const entry of unknownInitialData) {
                const pipelineId = entry.pipeline_id;
                recordPipelineDisplayName(pipelineId, entry.location_name);

                // Filter: Only process if user has access to this pipeline
                if (window.userPipelines !== null && !window.userPipelines.includes(pipelineId)) {
                    console.log(`[UNKNOWN] [FILTER] Skipping pipeline ${pipelineId} - user does not have access`);
                    continue;
                }
                
                // Process each face in this pipeline entry
                for (const faceData of entry.faces || []) {
                    const identityId = faceData.identity_id || `temp_${Date.now()}`;
                    
                    // Create identity object from face data
                    const identity = {
                        id: identityId,
                        type: 'unknown',
                        display_name: faceData.name || 'Unknown',
                        status: 'active',
                        first_seen_at: entry.timestamp,
                        last_seen_at: entry.timestamp,
                        appearances_count: 1,
                        best_snapshot_path: faceData.face_image_path || null,
                        snapshot_url: faceData.image ? `data:image/jpeg;base64,${faceData.image}` : (faceData.face_image_path ? `/storage/${faceData.face_image_path.replace(/^.*storage[\/\\]/, '')}` : null),
                        pipeline_ids: [pipelineId]
                    };
                    
                    // Add to the appropriate pipeline group
                    addUnknownFaceToPipeline(pipelineId, identity);
                }
            }
            
            console.log('[UNKNOWN] ✅ Initial unknown data processed');
            break;
            
        case 'initial_data':
        case 'new_detection':
            // Known faces and initial data - ignore on unknown page
            break;
    }
}

// Helper function to find identity in current pipeline groups
function findIdentityInPipelineGroups(identityId, pipelineId) {
    for (const group of allPipelineGroups) {
        if (group.id === pipelineId) {
            const found = group.identities.find(id => id.id === identityId);
            if (found) return found;
        }
    }
    return null;
}

// Helper to get currently visible pipeline groups
function getCurrentPageGroups() {
    const startIndex = (currentPage - 1) * pipelineGroupsPerPage;
    const endIndex = startIndex + pipelineGroupsPerPage;
    return allPipelineGroups.slice(startIndex, endIndex);
}

// Update a single identity card in place (without reloading page)
function updateIdentityCard(identityId, updatedIdentity) {
    // Find the card element by identity ID
    const card = document.querySelector(`[data-identity-id="${identityId}"]`);
    
    if (!card) {
        return false; // Card not visible (on different page)
    }
    
    // Update image if new one is provided
    // Try to find image by data attribute first, then fallback to any img in card
    let img = card.querySelector(`img[data-identity-image="${identityId}"]`);
    if (!img) {
        img = card.querySelector('img');
    }
    
    if (img && updatedIdentity.snapshot_url) {
        // Check if snapshot_url is a data URI (base64 image)
        const isDataUri = updatedIdentity.snapshot_url.startsWith('data:');
        
        // Normalize URLs for comparison (skip for data URIs)
        const currentSrc = isDataUri ? img.src : img.src.replace(window.location.origin, '');
        const newSrc = isDataUri 
            ? updatedIdentity.snapshot_url
            : (updatedIdentity.snapshot_url.startsWith('http') 
                ? updatedIdentity.snapshot_url.replace(window.location.origin, '')
                : updatedIdentity.snapshot_url);
        
        // Only update if URL is different (avoid unnecessary reloads)
        if (currentSrc !== newSrc && currentSrc !== '/' + newSrc) {
            // SMOOTH CROSSFADE: Preload new image, then swap with crossfade
            // For data URIs, use them directly; for regular URLs, construct full URL
            const fullUrl = isDataUri
                ? updatedIdentity.snapshot_url
                : (updatedIdentity.snapshot_url.startsWith('http') 
                    ? updatedIdentity.snapshot_url 
                    : (updatedIdentity.snapshot_url.startsWith('/') 
                        ? window.location.origin + updatedIdentity.snapshot_url 
                        : window.location.origin + '/' + updatedIdentity.snapshot_url));
            
            // Create new image element to preload
            const newImg = new Image();
            
            // Set up crossfade transition
            newImg.onload = () => {
                // New image loaded - now do smooth crossfade
                const imgContainer = img.parentElement;
                if (!imgContainer) return;
                
                // Ensure container has position relative for absolute positioning
                const containerStyle = window.getComputedStyle(imgContainer);
                if (containerStyle.position === 'static') {
                    imgContainer.style.position = 'relative';
                }
                
                // Get computed dimensions from old image to ensure proper sizing
                const imgRect = img.getBoundingClientRect();
                const imgComputedStyle = window.getComputedStyle(img);
                const containerRect = imgContainer.getBoundingClientRect();
                
                // Ensure container maintains its height during transition
                if (!imgContainer.style.minHeight) {
                    imgContainer.style.minHeight = containerRect.height + 'px';
                }
                
                // Style the new image (same as old one) - carry the CSS class too,
                // the card image is class-styled (.identity-img), not inline-styled
                newImg.className = img.className;
                newImg.style.cssText = img.style.cssText;
                newImg.style.position = 'absolute';
                newImg.style.top = '0';
                newImg.style.left = '0';
                newImg.style.width = '100%';
                newImg.style.height = '100%';
                newImg.style.opacity = '0';
                newImg.style.transition = 'opacity 0.5s ease-in-out';
                newImg.style.zIndex = '2';
                newImg.style.objectFit = imgComputedStyle.objectFit || 'contain';
                newImg.style.display = 'block';
                newImg.setAttribute('data-identity-image', identityId);
                newImg.setAttribute('alt', img.alt || 'Identity');
                newImg.setAttribute('loading', 'lazy');
                
                // Copy event handlers from old image
                if (img.onmouseenter) newImg.onmouseenter = img.onmouseenter;
                if (img.onmouseleave) newImg.onmouseleave = img.onmouseleave;
                if (img.onload) newImg.onload = img.onload;
                if (img.onerror) newImg.onerror = img.onerror;
                if (img.onclick) newImg.onclick = img.onclick;
                if (img.ondblclick) newImg.ondblclick = img.ondblclick;
                
                // CRITICAL: Insert new image FIRST (invisible) before fading out old one
                imgContainer.appendChild(newImg);
                
                // Force a reflow to ensure new image is in DOM and rendered
                void newImg.offsetHeight;
                
                // Double-check new image is actually in DOM and has dimensions
                const newImgRect = newImg.getBoundingClientRect();
                if (newImgRect.width === 0 || newImgRect.height === 0) {
                    // New image not properly sized, wait a bit more
                    setTimeout(() => {
                        startCrossfade();
                    }, 50);
                } else {
                    startCrossfade();
                }
                
                function startCrossfade() {
                    // Now start both transitions simultaneously
                    requestAnimationFrame(() => {
                        requestAnimationFrame(() => {
                            // Fade out old image
                            img.style.transition = 'opacity 0.5s ease-in-out';
                            img.style.opacity = '0';
                            img.style.zIndex = '1';
                            
                            // Fade in new image at the same time
                            newImg.style.opacity = '1';
                        });
                    });
                    
                    // Remove old image after transition completes
                    setTimeout(() => {
                        if (img && img.parentElement && newImg.parentElement) {
                            img.remove();
                        }
                    }, 600); // Slightly longer than transition
                }
            };
            
            // Handle preload errors
            newImg.onerror = () => {
                // If preload fails, fallback to simple update
                img.style.transition = 'opacity 0.3s ease';
                img.style.opacity = '0.7';
                img.src = fullUrl;
                img.onload = () => {
                    img.style.opacity = '1';
                };
                img.onerror = () => {
                    img.style.opacity = '1';
                };
            };
            
            // Start preloading
            newImg.src = fullUrl;
        }
    }
    
    // Update appearances count
    const countBadge = card.querySelector('[data-appearances-count]');
    if (countBadge) {
        const newCount = updatedIdentity.appearances_count || 1;
        if (countBadge.textContent !== String(newCount)) {
            countBadge.textContent = newCount;
            countBadge.setAttribute('data-appearances-count', newCount);
            
            // Add pulse animation to indicate update
            countBadge.style.animation = 'none';
            setTimeout(() => {
                countBadge.style.animation = 'pulse 0.5s ease';
            }, 10);
        }
    }
    
    // Update last seen date
    const dateEl = card.querySelector('[data-last-seen]');
    if (dateEl) {
        const newDate = new Date(updatedIdentity.last_seen_at || updatedIdentity.first_seen_at).toLocaleDateString();
        if (dateEl.textContent !== newDate) {
            dateEl.textContent = newDate;
        }
    }
    
    return true; // Successfully updated
}

// Update a single pipeline group in place (without re-rendering entire page)
function updatePipelineGroupInPlace(pipelineId, pipelineGroup) {
    // Find the existing group section
    const groupSection = document.querySelector(`[data-pipeline-id="${pipelineId}"]`);
    if (!groupSection) {
        return; // Group not visible
    }
    
    // Find the grid inside the group
    const groupGrid = groupSection.querySelector('.pipeline-identities-grid');
    if (!groupGrid) {
        return;
    }
    
    // Check all identities in the group to see which ones need updating
    // Find the most recently updated identity (highest appearances_count or most recent last_seen_at)
    const sortedIdentities = [...pipelineGroup.identities].sort((a, b) => {
        const aTime = new Date(a.last_seen_at || a.first_seen_at).getTime();
        const bTime = new Date(b.last_seen_at || b.first_seen_at).getTime();
        return bTime - aTime; // Most recent first
    });
    
    for (const identity of sortedIdentities) {
        // Check if identity already exists in the grid
        const existingCard = groupGrid.querySelector(`[data-identity-id="${identity.id}"]`);
        
        if (existingCard) {
            // Identity already exists - update the card
            updateIdentityCard(identity.id, identity);
        } else {
            // New identity - add only this card (don't re-render entire group)
            const newCard = createIdentityCard(identity);
            groupGrid.insertBefore(newCard, groupGrid.firstChild); // Add at top
            
            // Update group header count
            const countSpan = groupSection.querySelector('.pipeline-group-header span');
            if (countSpan) {
                // Count unique identities (remove duplicates)
                const uniqueCount = new Set(pipelineGroup.identities.map(id => id.id)).size;
                countSpan.textContent = `${uniqueCount} ${uniqueCount === 1 ? 'identity' : 'identities'}`;
            }
            
            // Smooth scroll to show new card if needed (only for first new card)
            if (identity === sortedIdentities[0]) {
                setTimeout(() => {
                    newCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                }, 100);
            }
            break; // Only add the most recent new identity to avoid multiple additions
        }
    }
}

// Add unknown face to pipeline group in real-time
function addUnknownFaceToPipeline(pipelineId, identity) {
    // Find or create pipeline group
    let pipelineGroup = allPipelineGroups.find(g => g.id === pipelineId);
    
    if (!pipelineGroup) {
        // Create new pipeline group
        pipelineGroup = {
            id: pipelineId,
            identities: []
        };
        allPipelineGroups.push(pipelineGroup);
        // Sort pipeline groups
        allPipelineGroups.sort((a, b) => a.id.localeCompare(b.id));
    }
    
    // Check if identity already exists in this pipeline group
    const existingIndex = pipelineGroup.identities.findIndex(id => id.id === identity.id);
    if (existingIndex >= 0) {
        // Update existing identity
        const existingIdentity = pipelineGroup.identities[existingIndex];
        pipelineGroup.identities[existingIndex] = {
            ...existingIdentity,
            ...identity,
            appearances_count: (existingIdentity.appearances_count || 1) + 1,
            last_seen_at: identity.last_seen_at || existingIdentity.last_seen_at
        };
        console.log(`[UNKNOWN] 🔄 Updated identity ${identity.id} in pipeline ${pipelineId}`);
        
        // Try to update the card in place (only if visible)
        const updated = updateIdentityCard(identity.id, pipelineGroup.identities[existingIndex]);
        if (updated) {
            // Card updated in place, no need to re-render
            updateStatsFromPipelineGroups();
            return;
        }
    } else {
        // Add new identity to pipeline group
        pipelineGroup.identities.push(identity);
        console.log(`[UNKNOWN] ➕ Added new identity ${identity.id} to pipeline ${pipelineId}`);
    }
    
    // Check if this pipeline group is currently visible on the page
    const currentPageGroups = getCurrentPageGroups();
    const isVisible = currentPageGroups.some(g => g.id === pipelineId);
    
    if (isVisible) {
        // Pipeline group is visible - update it in place (don't re-render entire page)
        updatePipelineGroupInPlace(pipelineId, pipelineGroup);
    } else {
        // Pipeline group not visible - just update data in memory
        // (will show when user navigates to that page)
        console.log(`[UNKNOWN] 📄 Pipeline ${pipelineId} not visible on current page, updated in memory only`);
    }
    
    // Update stats without re-rendering
    updateStatsFromPipelineGroups();
}

// Update stats from current pipeline groups
function updateStatsFromPipelineGroups() {
    let totalUnknown = 0;
    let totalAppearances = 0;
    const uniquePipelines = new Set();
    
    allPipelineGroups.forEach(group => {
        uniquePipelines.add(group.id);
        group.identities.forEach(identity => {
            totalUnknown++;
            totalAppearances += identity.appearances_count || 1;
        });
    });
    
    const totalUnknownEl = document.getElementById('total-unknown');
    const totalAppearancesEl = document.getElementById('total-appearances');
    const totalCamerasEl = document.getElementById('total-cameras');
    
    if (totalUnknownEl) totalUnknownEl.textContent = totalUnknown;
    if (totalAppearancesEl) totalAppearancesEl.textContent = totalAppearances;
    if (totalCamerasEl) totalCamerasEl.textContent = uniquePipelines.size;
}

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
    if (ws) {
        ws.close();
    }
});

// Render current page of pipeline groups
function renderPipelineGroupsPage() {
    const grid = document.getElementById('unknown-grid');
    grid.innerHTML = ''; // Clear existing content
    
    // Set grid style for pipeline groups
    grid.style.cssText = 'display: grid; grid-template-columns: repeat(auto-fill, minmax(400px, 1fr)); gap: 0.75rem; margin: 0.15rem 0 0 0;';
    
    if (allPipelineGroups.length === 0) {
        return;
    }
    
    // Calculate pagination for pipeline groups
    const totalPipelineGroups = allPipelineGroups.length;
    totalPages = Math.max(1, Math.ceil(totalPipelineGroups / pipelineGroupsPerPage));
    
    // Ensure current page is valid
    if (currentPage > totalPages) {
        currentPage = totalPages;
    }
    if (currentPage < 1) {
        currentPage = 1;
    }
    
    // Update pagination controls
    document.getElementById('current-page').textContent = currentPage;
    document.getElementById('total-pages').textContent = totalPages;
    document.getElementById('prev-page-btn').disabled = currentPage === 1;
    document.getElementById('next-page-btn').disabled = currentPage >= totalPages;
    
    // Get current page of pipeline groups
    const startIndex = (currentPage - 1) * pipelineGroupsPerPage;
    const endIndex = startIndex + pipelineGroupsPerPage;
    const currentPageGroups = allPipelineGroups.slice(startIndex, endIndex);
    
    // Render only current page pipeline groups
    currentPageGroups.forEach(group => {
        const groupSection = createPipelineGroup(group.id, group.identities);
        grid.appendChild(groupSection);
    });
}

// Create pipeline group section (matrix/square layout)
function createPipelineGroup(pipelineId, identities) {
    const groupSection = document.createElement('div');
    groupSection.className = 'pipeline-group';
    // Add data attribute for easy finding
    groupSection.setAttribute('data-pipeline-id', pipelineId);
    groupSection.style.cssText = `
        background: rgba(10, 25, 41, 0.8);
        border: 2px solid rgba(0, 255, 150, 0.3);
        border-radius: 10px;
        padding: 0.5rem;
        display: flex;
        flex-direction: column;
        min-height: 400px;
        max-height: 600px;
        height: auto;
        transition: all 0.3s ease;
        box-shadow: 0 4px 15px rgba(0, 0, 0, 0.3);
        overflow: hidden;
        width: 100%;
        min-width: 0;
    `;
    
    // Add hover effect
    groupSection.addEventListener('mouseenter', () => {
        groupSection.style.borderColor = 'rgba(0, 255, 150, 0.6)';
        groupSection.style.boxShadow = '0 6px 25px rgba(0, 255, 150, 0.3)';
        groupSection.style.transform = 'translateY(-2px)';
    });
    groupSection.addEventListener('mouseleave', () => {
        groupSection.style.borderColor = 'rgba(0, 255, 150, 0.3)';
        groupSection.style.boxShadow = '0 4px 15px rgba(0, 0, 0, 0.3)';
        groupSection.style.transform = 'translateY(0)';
    });
    
    const groupHeader = document.createElement('div');
    groupHeader.className = 'pipeline-group-header';
    groupHeader.style.cssText = `
        display: flex;
        align-items: center;
        gap: 0.5rem;
        margin-bottom: 0.6rem;
        padding: 0.5rem 0.75rem;
        background: linear-gradient(135deg, rgba(0, 255, 150, 0.15) 0%, rgba(0, 255, 150, 0.05) 100%);
        border-left: 3px solid #00ff96;
        border-radius: 6px;
        flex-shrink: 0;
    `;
    groupHeader.innerHTML = `
        <i class="fas fa-video" style="color: #00ff96; font-size: 1.1rem;"></i>
        <div style="flex: 1; min-width: 0;">
            <h2 class="pipeline-group-title" data-pipeline-id="${escapeHtml(pipelineId)}" title="${escapeHtml(pipelineId)}" style="margin: 0; color: #00ff96; font-size: 0.95rem; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(getPipelineDisplayName(pipelineId))}</h2>
            <span style="color: #999; font-size: 0.7rem; display: block; margin-top: 0.15rem;">${identities.length} ${identities.length === 1 ? 'identity' : 'identities'}</span>
        </div>
        <button class="pipeline-merge-suggestions-btn" data-pipeline-id="${pipelineId}" title="View merge suggestions for this pipeline" onclick="window.openPipelineMergeSuggestions && window.openPipelineMergeSuggestions('${pipelineId}')" style="
            background: rgba(0, 255, 150, 0.2);
            border: 1px solid rgba(0, 255, 150, 0.4);
            color: #00ff96;
            padding: 0.4rem 0.8rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.75rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 0.4rem;
            transition: all 0.2s;
            white-space: nowrap;
        " onmouseover="this.style.background='rgba(0, 255, 150, 0.3)'; this.style.borderColor='rgba(0, 255, 150, 0.6)';" onmouseout="this.style.background='rgba(0, 255, 150, 0.2)'; this.style.borderColor='rgba(0, 255, 150, 0.4)';">
            <i class="fas fa-object-group"></i>
            <span>MERGE</span>
        </button>
    `;
    
    // Also attach event listener programmatically as backup
    const mergeBtn = groupHeader.querySelector('.pipeline-merge-suggestions-btn');
    if (mergeBtn) {
        mergeBtn.addEventListener('click', async (e) => {
            e.preventDefault();
            e.stopPropagation();
            const pipelineId = mergeBtn.getAttribute('data-pipeline-id');
            console.log('[PIPELINE_MERGE] Button clicked (event listener), pipelineId:', pipelineId);
            if (pipelineId && typeof openPipelineMergeSuggestions === 'function') {
                await openPipelineMergeSuggestions(pipelineId);
            } else {
                console.error('[PIPELINE_MERGE] Function not available or pipelineId missing');
                showNotification('Error: Function not loaded. Please refresh the page.', 'error');
            }
        });
    }
    
    const groupGrid = document.createElement('div');
    groupGrid.className = 'pipeline-identities-grid';
    groupGrid.style.cssText = `
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        grid-auto-rows: 1fr;
        gap: 0.5rem;
        flex: 1;
        overflow-y: auto;
        overflow-x: hidden;
        padding: 0.5rem;
        align-items: stretch;
        min-height: 0;
        max-height: 550px;
        width: 100%;
        box-sizing: border-box;
    `;
    
    // Remove duplicates (same identity might appear in multiple pipelines)
    const uniqueIdentities = [];
    const seenIds = new Set();
    identities.forEach(identity => {
        if (!seenIds.has(identity.id)) {
            seenIds.add(identity.id);
            uniqueIdentities.push(identity);
        }
    });
    
    uniqueIdentities.forEach(identity => {
        const card = createIdentityCard(identity);
        groupGrid.appendChild(card);
    });
    
    groupSection.appendChild(groupHeader);
    groupSection.appendChild(groupGrid);
    
    return groupSection;
}

// Create identity card - uniform square-image card, styled by .identity-* classes in admin.css
function createIdentityCard(identity) {
    const card = document.createElement('div');
    card.className = 'identity-card-compact';

    // Click handler - depends on mode
    card.addEventListener('click', (e) => {
        if (multiSelectMode) {
            toggleIdentitySelection(identity.id, e);
        } else {
            viewIdentityDetails(identity.id);
        }
    });

    card.setAttribute('title', 'Click to view details and create alerts for this unknown person');
    card.setAttribute('data-identity-id', identity.id);

    const sightings = identity.appearances_count || 1;
    const lastSeen = new Date(identity.last_seen_at || identity.first_seen_at);

    card.innerHTML = `
        <div class="identity-media">
            ${identity.snapshot_url ?
                `<img
                    class="identity-img"
                    src="${identity.snapshot_url}"
                    alt="Unknown identity"
                    loading="lazy"
                    data-identity-image="${identity.id}"
                    onerror="this.src='data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'200\' height=\'200\'%3E%3Crect fill=\'%23333\' width=\'200\' height=\'200\'/%3E%3Ctext fill=\'%23999\' font-family=\'sans-serif\' font-size=\'14\' x=\'50%25\' y=\'50%25\' text-anchor=\'middle\' dy=\'.3em\'%3ENo Image%3C/text%3E%3C/svg%3E';"
                >` :
                `<div class="identity-no-image"><i class="fas fa-user-secret"></i><span>No snapshot</span></div>`
            }
            <div class="identity-badge" data-appearances-count="${sightings}" title="${sightings} sighting${sightings === 1 ? '' : 's'}">
                <i class="fas fa-eye"></i>${sightings}
            </div>
        </div>
        <div class="identity-meta" data-last-seen title="Last seen">
            <i class="fas fa-clock"></i>${lastSeen.toLocaleDateString()}
        </div>
        <div class="identity-actions">
            <button class="identity-action-primary" onclick="event.stopPropagation(); viewIdentityDetails('${identity.id}')" title="View details and create alerts">
                <i class="fas fa-eye"></i><span>VIEW &amp; ALERT</span>
            </button>
            <div class="identity-icon-row">
                <button class="identity-icon-btn danger" onclick="event.stopPropagation(); analyzeInSecurityIntelligence('${identity.id}')" title="Security Intelligence analysis">
                    <i class="fas fa-shield-alt"></i>
                </button>
                <button class="identity-icon-btn" onclick="event.stopPropagation(); promoteIdentityModal('${identity.id}')" title="Promote to known person">
                    <i class="fas fa-arrow-up"></i>
                </button>
                <button class="identity-icon-btn" onclick="event.stopPropagation(); openMergeModal('${identity.id}')" title="Merge with another identity">
                    <i class="fas fa-code-branch"></i>
                </button>
            </div>
        </div>
    `;
    return card;
}

// Apply filters
function applyFilters() {
    currentFilters = {};
    
    const dateFrom = document.getElementById('date-from').value;
    const dateTo = document.getElementById('date-to').value;
    const pipeline = document.getElementById('pipeline-filter').value;
    const minAppearances = document.getElementById('min-appearances').value;

    if (dateFrom) currentFilters.date_from = dateFrom;
    if (dateTo) currentFilters.date_to = dateTo;
    if (pipeline) currentFilters.pipeline_id = pipeline;
    if (minAppearances) currentFilters.min_appearances = parseInt(minAppearances);

    currentPage = 1; // Reset to first page when filters change
    allPipelineGroups = []; // Clear stored groups
    loadUnknownFaces();
}

// Clear filters
function clearFilters() {
    document.getElementById('date-from').value = '';
    document.getElementById('date-to').value = '';
    document.getElementById('pipeline-filter').value = '';
    document.getElementById('min-appearances').value = '1';
    currentFilters = {};
    currentPage = 1; // Reset to first page
    allPipelineGroups = []; // Clear stored groups
    loadUnknownFaces();
}

// Render advanced timeline visualization
function renderAdvancedTimeline(appearances) {
    if (!appearances || appearances.length === 0) {
        return '<div class="timeline-empty"><i class="fas fa-info-circle"></i> No appearance data available</div>';
    }
    
    // Sort appearances by time
    const sortedAppearances = [...appearances].sort((a, b) => 
        new Date(a.start_time) - new Date(b.start_time)
    );
    
    // Get unique pipelines in order of first appearance
    const uniquePipelines = [...new Set(sortedAppearances.map(a => a.pipeline_id || 'Unknown'))];
    
    // Calculate time range for 24-hour scale
    // We'll use a 24-hour day scale (0-24 hours) for positioning
    const hoursInDay = 24;
    const minutesInDay = hoursInDay * 60;
    
    // Find the actual time range across all appearances
    const firstTime = new Date(sortedAppearances[0].start_time);
    const lastTime = new Date(sortedAppearances[sortedAppearances.length - 1].start_time);
    const totalDuration = lastTime - firstTime;
    
    // Calculate timeline width based on 24-hour scale
    // Each hour gets a fixed width, so 24 hours = 24 * hourWidth pixels
    const hourWidth = 120; // Width per hour in pixels (adjustable)
    const timelineWidth = hoursInDay * hourWidth; // Total width for 24 hours = 2880px
    
    // Calculate optimal scale to fit timeline in view
    const containerWidth = 1000; // Approximate container width
    const optimalScale = Math.min(1, (containerWidth - 100) / timelineWidth);
    const defaultScale = Math.max(0.3, optimalScale); // Default scale, minimum 30%
    
    // Generate timeline HTML
    let timelineHTML = `
        <div class="timeline-controls">
            <div class="timeline-legend">
                <div class="legend-item">
                    <div class="legend-color" style="background: #00ff96;"></div>
                    <span>Location</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: rgba(0, 255, 150, 0.3);"></div>
                    <span>Movement Path</span>
                </div>
                <div class="legend-item">
                    <div class="legend-color" style="background: #ffc107;"></div>
                    <span>Time Gap</span>
                </div>
            </div>
            <div class="timeline-zoom-controls">
                <button class="zoom-btn" onclick="timelineZoomOut()" title="Zoom Out">
                    <i class="fas fa-search-minus"></i>
                </button>
                <input type="range" id="timeline-scale-slider" min="0.1" max="2" step="0.1" value="${defaultScale}" 
                       oninput="updateTimelineScale(this.value)" 
                       style="width: 150px; margin: 0 0.5rem;">
                <span class="zoom-value" id="zoom-value">${Math.round(defaultScale * 100)}%</span>
                <button class="zoom-btn" onclick="timelineZoomIn()" title="Zoom In">
                    <i class="fas fa-search-plus"></i>
                </button>
                <button class="zoom-btn" onclick="timelineFitToView()" title="Fit to View">
                    <i class="fas fa-compress-arrows-alt"></i>
                </button>
            </div>
        </div>
        <div class="timeline-track" id="timeline-track" style="transform: scale(${defaultScale}); transform-origin: left center; width: ${timelineWidth}px;">
    `;
    
    sortedAppearances.forEach((app, index) => {
        const startTime = new Date(app.start_time);
        const endTime = app.end_time ? new Date(app.end_time) : null;
        const duration = endTime ? Math.round((endTime - startTime) / 1000) : null;
        
        // Calculate position based on hour of day (0-24 hours)
        // Get hours, minutes, and seconds from the start time
        const hours = startTime.getHours();
        const minutes = startTime.getMinutes();
        const seconds = startTime.getSeconds();
        
        // Convert to decimal hours (e.g., 14:30:00 = 14.5 hours)
        const decimalHours = hours + (minutes / 60) + (seconds / 3600);
        
        // Calculate position on 24-hour scale (0-24 hours mapped to 0-timelineWidth pixels)
        const hourWidth = timelineWidth / hoursInDay; // Width per hour
        let leftPosition = decimalHours * hourWidth;
        
        // Handle nodes that might overlap at the same hour - stack them vertically or offset slightly
        const minSpacing = 50; // Minimum spacing between nodes at same time
        if (index > 0) {
            const prevApp = sortedAppearances[index - 1];
            const prevStartTime = new Date(prevApp.start_time);
            const prevHours = prevStartTime.getHours();
            const prevMinutes = prevStartTime.getMinutes();
            const prevSeconds = prevStartTime.getSeconds();
            const prevDecimalHours = prevHours + (prevMinutes / 60) + (prevSeconds / 3600);
            const prevLeftPos = prevDecimalHours * hourWidth;
            
            // If nodes are very close (within same hour or less than minSpacing), offset them
            if (Math.abs(leftPosition - prevLeftPos) < minSpacing) {
                leftPosition = prevLeftPos + minSpacing;
            }
        }
        
        const isFirst = index === 0;
        const isLast = index === sortedAppearances.length - 1;
        const prevApp = index > 0 ? sortedAppearances[index - 1] : null;
        const nextApp = index < sortedAppearances.length - 1 ? sortedAppearances[index + 1] : null;
        const isPipelineChange = prevApp && prevApp.pipeline_id !== app.pipeline_id;
        const timeSincePrev = prevApp ? Math.round((startTime - new Date(prevApp.start_time)) / 1000 / 60) : null;
        
        // Calculate gap position if needed (based on hour difference)
        let gapLeft = 0;
        let gapWidth = 0;
        if (isPipelineChange && timeSincePrev && timeSincePrev > 5 && prevApp) {
            const prevStartTime = new Date(prevApp.start_time);
            const prevHours = prevStartTime.getHours();
            const prevMinutes = prevStartTime.getMinutes();
            const prevSeconds = prevStartTime.getSeconds();
            const prevDecimalHours = prevHours + (prevMinutes / 60) + (prevSeconds / 3600);
            gapLeft = prevDecimalHours * hourWidth;
            gapWidth = leftPosition - gapLeft;
        }
        
        timelineHTML += `
            ${isPipelineChange && timeSincePrev && timeSincePrev > 5 && gapWidth > 0 ? `
                <div class="timeline-gap" style="left: ${gapLeft}px; width: ${gapWidth}px;">
                    <div class="gap-indicator">
                        <i class="fas fa-arrow-right"></i>
                        <span>${timeSincePrev} min</span>
                    </div>
                </div>
            ` : ''}
            <div class="timeline-node ${isFirst ? 'first' : ''} ${isLast ? 'last' : ''} ${isPipelineChange ? 'pipeline-change' : ''}" 
                 style="left: ${leftPosition}px;" 
                 data-index="${index}">
                <div class="timeline-node-connector"></div>
                <div class="timeline-node-content">
                    <div class="node-image">
                        ${app.snapshot_url ? 
                            `<img src="${app.snapshot_url}" alt="Appearance ${index + 1}" onerror="this.parentElement.innerHTML='<div class=\\'node-image-placeholder\\'><i class=\\'fas fa-user\\'></i></div>'">` :
                            `<div class="node-image-placeholder"><i class="fas fa-user"></i></div>`
                        }
                    </div>
                    <div class="node-info">
                        <div class="node-pipeline" title="${escapeHtml(app.pipeline_id || '')}">
                            <i class="fas fa-video"></i>
                            <span>${escapeHtml(app.pipeline_id ? getPipelineDisplayName(app.pipeline_id) : 'Unknown')}</span>
                        </div>
                        <div class="node-time">
                            <i class="fas fa-clock"></i>
                            <span>${startTime.toLocaleTimeString()}</span>
                        </div>
                        ${duration ? `
                            <div class="node-duration">
                                <i class="fas fa-hourglass-half"></i>
                                <span>${duration}s</span>
                            </div>
                        ` : ''}
                        ${app.track_id ? `
                            <div class="node-track">
                                <i class="fas fa-fingerprint"></i>
                                <span>${app.track_id.substring(0, 8)}...</span>
                            </div>
                        ` : ''}
                    </div>
                    <div class="node-date">${startTime.toLocaleDateString()}</div>
                </div>
                ${nextApp && nextApp.pipeline_id !== app.pipeline_id ? `
                    <div class="timeline-connection">
                        <div class="connection-line"></div>
                        <div class="connection-label" title="${escapeHtml(nextApp.pipeline_id || '')}">
                            <i class="fas fa-arrow-right"></i>
                            <span>${escapeHtml(nextApp.pipeline_id ? getPipelineDisplayName(nextApp.pipeline_id) : 'Unknown')}</span>
                        </div>
                    </div>
                ` : ''}
            </div>
        `;
    });
    
    // Add hour markers for 24-hour scale
    let hourMarkersHTML = '';
    for (let hour = 0; hour <= hoursInDay; hour++) {
        const markerPosition = (hour / hoursInDay) * timelineWidth;
        hourMarkersHTML += `
            <div class="hour-marker" style="left: ${markerPosition}px;">
                <div class="hour-marker-line"></div>
                <div class="hour-marker-label">${hour}:00</div>
            </div>
        `;
    }
    
    timelineHTML += `
            ${hourMarkersHTML}
        </div>
        <div class="timeline-summary">
            <div class="summary-item">
                <i class="fas fa-route"></i>
                <div>
                    <div class="summary-value">${uniquePipelines.length}</div>
                    <div class="summary-label">Cameras Visited</div>
                </div>
            </div>
            <div class="summary-item">
                <i class="fas fa-clock"></i>
                <div>
                    <div class="summary-value">${formatDuration(totalDuration)}</div>
                    <div class="summary-label">Total Time Span</div>
                </div>
            </div>
            <div class="summary-item">
                <i class="fas fa-map-marker-alt"></i>
                <div>
                    <div class="summary-value">${sortedAppearances.length}</div>
                    <div class="summary-label">Total Stops</div>
                </div>
            </div>
        </div>
    `;
    
    return timelineHTML;
}

// Format duration in human-readable format
function formatDuration(ms) {
    const seconds = Math.floor(ms / 1000);
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);
    
    if (days > 0) return `${days}d ${hours % 24}h`;
    if (hours > 0) return `${hours}h ${minutes % 60}m`;
    if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
    return `${seconds}s`;
}

// Timeline zoom controls
let currentTimelineScale = 0.3;

function updateTimelineScale(scale) {
    currentTimelineScale = parseFloat(scale);
    const track = document.getElementById('timeline-track');
    const zoomValue = document.getElementById('zoom-value');
    const slider = document.getElementById('timeline-scale-slider');
    
    if (track) {
        track.style.transform = `scale(${currentTimelineScale})`;
        track.style.transformOrigin = 'left center';
    }
    
    if (zoomValue) {
        zoomValue.textContent = `${Math.round(currentTimelineScale * 100)}%`;
    }
    
    if (slider && parseFloat(slider.value) !== currentTimelineScale) {
        slider.value = currentTimelineScale;
    }
}

function timelineZoomIn() {
    const slider = document.getElementById('timeline-scale-slider');
    if (slider) {
        const currentValue = parseFloat(slider.value) || currentTimelineScale;
        const newScale = Math.min(2, currentValue + 0.1);
        updateTimelineScale(newScale);
    }
}

function timelineZoomOut() {
    const slider = document.getElementById('timeline-scale-slider');
    if (slider) {
        const currentValue = parseFloat(slider.value) || currentTimelineScale;
        const newScale = Math.max(0.1, currentValue - 0.1);
        updateTimelineScale(newScale);
    }
}

function timelineFitToView() {
    const track = document.getElementById('timeline-track');
    if (!track) return;
    
    // Calculate optimal scale for 24-hour timeline (2880px width)
    const container = track.parentElement;
    const containerWidth = container ? container.offsetWidth : 1000;
    const trackWidth = 2880; // 24 hours * 120px per hour
    const optimalScale = Math.min(1, Math.max(0.3, (containerWidth - 100) / trackWidth));
    
    updateTimelineScale(optimalScale);
}

// Make functions global
window.updateTimelineScale = updateTimelineScale;
window.timelineZoomIn = timelineZoomIn;
window.timelineZoomOut = timelineZoomOut;
window.timelineFitToView = timelineFitToView;

// View identity details
async function viewIdentityDetails(identityId) {
    const modal = document.getElementById('identity-detail-modal');
    const content = document.getElementById('identity-detail-content');

    try {
        const response = await fetch(`/api/admin/identity/${identityId}`, {
            credentials: 'include' // Include HttpOnly cookies
        });

        if (!response.ok) {
            throw new Error('Failed to load identity details');
        }

        const identity = await response.json();
        
        content.innerHTML = `
            <div class="detail-header">
                <div class="detail-image">
                    ${identity.snapshot_url ? 
                        `<img src="${identity.snapshot_url}" alt="Identity" onerror="this.src='data:image/svg+xml,%3Csvg xmlns=\\'http://www.w3.org/2000/svg\\' width=\\'100\\' height=\\'100\\'%3E%3Crect fill=\\'%23333\\' width=\\'100\\' height=\\'100\\'/%3E%3Ctext fill=\\'%23999\\' x=\\'50\\' y=\\'50\\' text-anchor=\\'middle\\' dominant-baseline=\\'middle\\' font-size=\\'40\\'%3E%3F%3C/text%3E%3C/svg%3E'">` :
                        `<div class="no-image"><i class="fas fa-user"></i></div>`
                    }
                </div>
                <div class="detail-info">
                    <h3>${identity.display_name || 'Unknown Identity'}</h3>
                    <div style="margin: 1rem 0; padding: 0.75rem; background: rgba(0, 255, 150, 0.1); border: 1px solid rgba(0, 255, 150, 0.3); border-radius: 6px;">
                        <p style="margin: 0 0 0.5rem 0; font-size: 0.85rem; color: #999;">Identity ID:</p>
                        <div style="display: flex; align-items: center; gap: 0.5rem;">
                            <code id="identity-id-display" style="flex: 1; font-size: 0.9rem; padding: 0.5rem; background: rgba(0, 0, 0, 0.5); border: 1px solid rgba(0, 255, 150, 0.3); border-radius: 4px; color: #00ff96; word-break: break-all; cursor: pointer;" onclick="copyIdentityId('${identity.id}')" title="Click to copy">${identity.id}</code>
                            <button class="intelligence-admin-btn small" onclick="copyIdentityId('${identity.id}')" style="padding: 0.5rem;" title="Copy Identity ID">
                                <div class="btn-content" style="padding: 0;">
                                    <div class="btn-icon-wrapper" style="width: 24px; height: 24px;">
                                        <i class="fas fa-copy" style="font-size: 0.8rem;"></i>
                                    </div>
                                </div>
                            </button>
                        </div>
                    </div>
                    <p>Type: <span class="badge">${identity.type}</span></p>
                    <p>Status: <span class="badge">${identity.status}</span></p>
                    <p>Appearances: ${identity.appearances_count}</p>
                    ${identity.best_snapshot_path ? `<p>Image Path: <code style="font-size: 0.85em; word-break: break-all;">${identity.best_snapshot_path}</code></p>` : ''}
                    <p>First Seen: ${new Date(identity.first_seen_at).toLocaleString()}</p>
                    <p>Last Seen: ${new Date(identity.last_seen_at).toLocaleString()}</p>
                </div>
            </div>
            <div class="detail-timeline-advanced">
                <div class="timeline-header">
                    <h4><i class="fas fa-route"></i> Movement Timeline</h4>
                    <div class="timeline-stats">
                        <span><i class="fas fa-video"></i> ${identity.pipeline_ids?.length || 0} Cameras</span>
                        <span><i class="fas fa-clock"></i> ${identity.appearances_count} Appearances</span>
                        <span><i class="fas fa-calendar"></i> ${identity.first_seen_at ? new Date(identity.first_seen_at).toLocaleDateString() : 'N/A'} - ${identity.last_seen_at ? new Date(identity.last_seen_at).toLocaleDateString() : 'N/A'}</span>
                    </div>
                </div>
                <div class="advanced-timeline-container">
                    ${renderAdvancedTimeline(identity.appearances || [])}
                </div>
            </div>
            <div class="detail-actions">
                <button class="intelligence-admin-btn" onclick="analyzeInSecurityIntelligence('${identity.id}')" style="background: linear-gradient(135deg, rgba(255, 68, 68, 0.2) 0%, rgba(255, 100, 100, 0.1) 100%); border-color: rgba(255, 68, 68, 0.4);">
                    <div class="btn-content">
                        <div class="btn-icon-wrapper">
                            <i class="fas fa-shield-alt"></i>
                        </div>
                        <span class="btn-title">ANALYZE IN SECURITY INTELLIGENCE</span>
                    </div>
                </button>
                <button class="intelligence-admin-btn" onclick="openCreateLiveAlertModal('${identity.id}', '${identity.display_name || 'Unknown'}')">
                    <div class="btn-content">
                        <div class="btn-icon-wrapper">
                            <i class="fas fa-bell"></i>
                        </div>
                        <span class="btn-title">CREATE LIVE ALERT</span>
                    </div>
                </button>
                <button class="intelligence-admin-btn" onclick="openAddToWatchlistModal('${identity.id}', '${identity.display_name || 'Unknown'}')">
                    <div class="btn-content">
                        <div class="btn-icon-wrapper">
                            <i class="fas fa-list-alt"></i>
                        </div>
                        <span class="btn-title">ADD TO WATCHLIST</span>
                    </div>
                </button>
                ${identity.type === 'unknown' ? `
                    <button class="intelligence-admin-btn" onclick="promoteIdentityModal('${identity.id}')">
                        <div class="btn-content">
                            <div class="btn-icon-wrapper">
                                <i class="fas fa-arrow-up"></i>
                            </div>
                            <span class="btn-title">PROMOTE TO KNOWN</span>
                        </div>
                    </button>
                    <button class="intelligence-admin-btn" onclick="openMergeModal('${identity.id}')">
                        <div class="btn-content">
                            <div class="btn-icon-wrapper">
                                <i class="fas fa-code-branch"></i>
                            </div>
                            <span class="btn-title">MERGE</span>
                        </div>
                    </button>
                ` : ''}
            </div>
        `;

        modal.style.display = 'flex';
    } catch (error) {
        console.error('Error loading identity details:', error);
        showNotification('Error loading identity details', 'error');
    }
}

// Promote identity modal
// Navigate to Security Intelligence page with identity pre-selected
function analyzeInSecurityIntelligence(identityId) {
    // Determine which tab to open based on context (default to threats)
    const tab = 'threats'; // Can be 'network', 'anomalies', 'threats', etc.
    window.location.href = `/admin/security-intelligence?identity_id=${identityId}&tab=${tab}`;
}

function promoteIdentityModal(identityId) {
    currentIdentityId = identityId;
    document.getElementById('promote-name').value = '';
    document.getElementById('promote-code').value = '';
    document.getElementById('promote-modal').style.display = 'flex';
}

// Promote identity - ALL VALIDATION IN BACKEND
async function promoteIdentity() {
    const name = document.getElementById('promote-name').value.trim();
    const code = document.getElementById('promote-code').value.trim();

    // Frontend just sends data - backend handles all validation
    try {
        const response = await fetch(`/api/admin/unknown/${currentIdentityId}/promote`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include', // Include HttpOnly cookies
            body: JSON.stringify({
                display_name: name,
                person_code: code || null
            })
        });

        if (!response.ok) {
            const error = await response.json();
            const errorMessage = error.detail || 'Failed to promote identity';
            
            // Check if it's a face detection error - show popup modal
            if (errorMessage.includes('No face detected') || errorMessage.includes('face detected')) {
                showFaceDetectionAlert(
                    'The image does not contain a detectable face. This may happen if the image was processed before face detection was enabled. Please choose another image for this identity.'
                );
            } else if (errorMessage.includes('snapshot image file is missing') || errorMessage.includes('Could not read')) {
                showFaceDetectionAlert(
                    'The identity\'s snapshot image is missing or corrupted. Please choose another image for this identity.'
                );
            } else {
                // For other errors, show notification but also show alert modal
                showFaceDetectionAlert(errorMessage);
            }
            return; // Don't close modal on error
        }

        const result = await response.json(); // Backend sends success message
        showNotification(result.message || 'Identity promoted successfully', 'success');
        document.getElementById('promote-modal').style.display = 'none';
        loadUnknownFaces();
    } catch (error) {
        console.error('Error promoting identity:', error);
        const errorMessage = error.message || 'Error promoting identity';
        
        // Show alert modal for any error
        if (errorMessage.includes('No face detected') || errorMessage.includes('face detected')) {
            showFaceDetectionAlert(
                'No face detected in the image. Please choose another image for this identity.'
            );
        } else {
            showFaceDetectionAlert(errorMessage);
        }
    }
}

// Show face detection alert modal (popup in front of user)
function showFaceDetectionAlert(message) {
    const modal = document.getElementById('face-detection-alert-modal');
    const messageElement = document.getElementById('face-alert-message');
    
    if (modal && messageElement) {
        messageElement.textContent = message;
        modal.style.display = 'flex';
        modal.style.zIndex = '99999'; // Ensure it's on top of everything
        
        // Add pulse animation
        const icon = modal.querySelector('.fa-user-slash');
        if (icon && icon.parentElement) {
            icon.parentElement.style.animation = 'pulse 2s infinite';
        }
    } else {
        // Fallback to notification if modal not found
        showNotification(message, 'error', 8000);
    }
}

// Close face detection alert modal
function closeFaceDetectionAlert() {
    const modal = document.getElementById('face-detection-alert-modal');
    if (modal) {
        modal.style.display = 'none';
    }
}

// Quick search
async function searchByImage() {
    const fileInput = document.getElementById('search-image-file');
    const scope = document.getElementById('search-scope').value;
    const resultsDiv = document.getElementById('search-results');
    const resultsGrid = document.getElementById('search-results-grid');

    if (!fileInput.files || !fileInput.files[0]) {
        showNotification('Please select an image', 'error');
        return;
    }

    const formData = new FormData();
    formData.append('image', fileInput.files[0]);
    formData.append('scope', scope);
    formData.append('top_k', '10');

    try {
        resultsGrid.innerHTML = '<div class="loading"><i class="fas fa-spinner fa-spin"></i> Searching...</div>';
        resultsDiv.style.display = 'block';

        const response = await fetch('/api/search/by-image', {
            method: 'POST',
            credentials: 'include', // Include HttpOnly cookies
            body: formData
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Search failed');
        }

        const results = await response.json();
        resultsGrid.innerHTML = '';

        if (results.length === 0) {
            resultsGrid.innerHTML = '<div class="no-results"><p>No matches found</p></div>';
            return;
        }

        results.forEach(result => {
            const card = createSearchResultCard(result);
            resultsGrid.appendChild(card);
        });
    } catch (error) {
        console.error('Error in quick search:', error);
        resultsGrid.innerHTML = `<div class="error">${error.message}</div>`;
        showNotification(error.message || 'Error in quick search', 'error');
    }
}

// Create search result card
function createSearchResultCard(result) {
    const card = document.createElement('div');
    card.className = 'identity-card';
    card.innerHTML = `
        <div class="card-image">
            ${result.snapshot_url || result.best_snapshot_path ? 
                `<img src="${result.snapshot_url || `/${result.best_snapshot_path}`}" alt="Match" onerror="this.parentElement.innerHTML='<div class=\\'no-image\\'><i class=\\'fas fa-user\\'></i></div>'">` :
                `<div class="no-image"><i class="fas fa-user"></i></div>`
            }
            <div class="similarity-badge">${(result.similarity * 100).toFixed(1)}%</div>
        </div>
        <div class="card-content">
            <div class="card-header">
                <h3>${result.display_name || 'Unknown'}</h3>
                <span class="card-badge ${result.type}">${result.type}</span>
            </div>
            <div class="card-info">
                <div class="info-item">
                    <i class="fas fa-eye"></i>
                    <span>${result.appearances_count} appearances</span>
                </div>
                <div class="info-item">
                    <i class="fas fa-clock"></i>
                    <span>Last seen: ${new Date(result.last_seen_at).toLocaleDateString()}</span>
                </div>
            </div>
            <div class="card-actions">
                <button class="intelligence-admin-btn small" onclick="viewIdentityDetails('${result.identity_id}')">
                    <div class="btn-content">
                        <div class="btn-icon-wrapper">
                            <i class="fas fa-eye"></i>
                        </div>
                        <span class="btn-title">VIEW</span>
                    </div>
                </button>
                ${result.type === 'unknown' ? `
                    <button class="intelligence-admin-btn small" onclick="promoteIdentityModal('${result.identity_id}')">
                        <div class="btn-content">
                            <div class="btn-icon-wrapper">
                                <i class="fas fa-arrow-up"></i>
                            </div>
                            <span class="btn-title">PROMOTE</span>
                        </div>
                    </button>
                ` : ''}
            </div>
        </div>
    `;
    return card;
}

// Show notification
function showNotification(message, type = 'info', duration = 3000) {
    // Create notification element
    const notification = document.createElement('div');
    notification.className = `notification ${type}`;
    notification.innerHTML = `
        <i class="fas fa-${type === 'success' ? 'check-circle' : type === 'error' ? 'exclamation-circle' : 'info-circle'}"></i>
        <span>${message}</span>
    `;
    
    document.body.appendChild(notification);
    
    setTimeout(() => {
        notification.classList.add('show');
    }, 10);
    
    setTimeout(() => {
        notification.classList.remove('show');
        setTimeout(() => notification.remove(), 300);
    }, duration);
}

// =====================================================
// Merge Functionality
// =====================================================

// Toggle multi-select mode
function toggleMultiSelectMode() {
    multiSelectMode = !multiSelectMode;
    selectedIdentities.clear();
    
    const btn = document.getElementById('multi-select-toggle');
    const label = document.getElementById('multi-select-label');
    const mergeBtnContainer = document.getElementById('merge-multiple-btn-container');
    const mergeBtnLabel = document.getElementById('merge-multiple-label');
    
    if (multiSelectMode) {
        btn.style.background = 'rgba(0, 255, 150, 0.2)';
        btn.style.borderColor = 'rgba(0, 255, 150, 0.5)';
        label.textContent = 'MULTI-SELECT ON';
        mergeBtnContainer.style.display = 'inline-block';
        updateMergeMultipleButton();
        showNotification('Multi-select mode enabled. Click identity cards to select them for merging.', 'success');
    } else {
        btn.style.background = '';
        btn.style.borderColor = '';
        label.textContent = 'MULTI-SELECT';
        mergeBtnContainer.style.display = 'none';
        showNotification('Multi-select mode disabled.', 'info');
    }
    
    // Refresh the grid to show selection state
    loadUnknownFaces();
}

// Update merge multiple button
function updateMergeMultipleButton() {
    const count = selectedIdentities.size;
    document.getElementById('selected-count-header').textContent = count;
    const mergeBtn = document.getElementById('merge-multiple-btn');
    
    if (count < 2) {
        mergeBtn.disabled = true;
        mergeBtn.style.opacity = '0.5';
        mergeBtn.style.cursor = 'not-allowed';
    } else {
        mergeBtn.disabled = false;
        mergeBtn.style.opacity = '1';
        mergeBtn.style.cursor = 'pointer';
    }
}

// Toggle identity selection in multi-select mode
function toggleIdentitySelection(identityId, event) {
    if (!multiSelectMode) {
        // If not in multi-select mode, open merge modal as before
        openMergeModal(identityId);
        return;
    }
    
    event.stopPropagation();
    
    if (selectedIdentities.has(identityId)) {
        selectedIdentities.delete(identityId);
    } else {
        selectedIdentities.add(identityId);
    }
    
    // Update UI to show selection state (styled by .identity-card-compact.selected)
    const card = event.currentTarget.closest('.identity-card-compact');
    if (card) {
        card.classList.toggle('selected', selectedIdentities.has(identityId));
    }
    
    // Update merge button and form
    updateMergeMultipleButton();
    updateMultiMergeForm();
}

// Update multi-merge form with selected identities
function updateMultiMergeForm() {
    const count = selectedIdentities.size;
    document.getElementById('selected-count').textContent = count;
    
    const listDiv = document.getElementById('selected-identities-list');
    if (count === 0) {
        listDiv.innerHTML = '<p style="color: #999; font-style: italic; margin: 0;">No identities selected. Use MULTI-SELECT mode and click on identity cards to select them.</p>';
        return;
    }
    
    // Fetch identity details for display
    Promise.all(Array.from(selectedIdentities).map(id => fetchIdentityDetails(id)))
        .then(identities => {
            listDiv.innerHTML = identities.map(identity => `
                <div style="display: flex; align-items: center; gap: 1rem; padding: 0.75rem; background: rgba(0, 0, 0, 0.3); border: 1px solid rgba(0, 255, 150, 0.2); border-radius: 6px; margin-bottom: 0.5rem;">
                    <div style="width: 50px; height: 50px; border-radius: 4px; overflow: hidden; background: rgba(0, 0, 0, 0.5); flex-shrink: 0;">
                        ${identity.snapshot_url || identity.best_snapshot_path ? 
                            `<img src="${escapeHtml(identity.snapshot_url || `/${identity.best_snapshot_path}`)}" alt="Identity" style="width: 100%; height: 100%; object-fit: cover;" onerror="this.parentElement.innerHTML='<div style=\\'width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: #999;\\'><i class=\\'fas fa-user\\'></i></div>'">` :
                            `<div style="width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: #999;"><i class="fas fa-user"></i></div>`
                        }
                    </div>
                    <div style="flex: 1; min-width: 0;">
                        <p style="margin: 0 0 0.25rem 0; font-size: 0.85rem; color: #00ff96; font-weight: 600;">${escapeHtml(identity.display_name || 'Unknown Identity')}</p>
                        <p style="margin: 0; font-size: 0.75rem; color: #999; word-break: break-all;">${escapeHtml(identity.id)}</p>
                        <p style="margin: 0.25rem 0 0 0; font-size: 0.75rem; color: #ccc;">Appearances: ${identity.appearances_count}</p>
                    </div>
                    <button class="intelligence-admin-btn small" onclick="removeFromSelection('${identity.id}')" style="padding: 0.4rem;">
                        <div class="btn-content" style="padding: 0;">
                            <div class="btn-icon-wrapper" style="width: 20px; height: 20px;">
                                <i class="fas fa-times" style="font-size: 0.7rem;"></i>
                            </div>
                        </div>
                    </button>
                </div>
            `).join('');
        })
        .catch(err => {
            console.error('Error loading identity details:', err);
            listDiv.innerHTML = '<p style="color: #ff9800;">Error loading identity details. Please try again.</p>';
        });
}

// Fetch identity details
async function fetchIdentityDetails(identityId) {
    try {
        const response = await fetch(`/api/admin/identity/${identityId}`, {
            credentials: 'include' // Include HttpOnly cookies
        });
        if (response.ok) {
            return await response.json();
        }
    } catch (err) {
        console.error('Error fetching identity:', err);
    }
    return { id: identityId, display_name: 'Unknown', appearances_count: 0 };
}

// Remove identity from selection
function removeFromSelection(identityId) {
    selectedIdentities.delete(identityId);
    updateMultiMergeForm();
    // Refresh grid to update visual state
    loadUnknownFaces();
}

// Open merge modal
function openMergeModal(identityId) {
    currentIdentityId = identityId;
    
    // Show appropriate form based on multi-select mode
    if (multiSelectMode && selectedIdentities.size > 0) {
        // Multi-merge mode
        document.getElementById('single-merge-form').style.display = 'none';
        document.getElementById('multi-merge-form').style.display = 'block';
        updateMultiMergeForm();
    } else {
        // Single merge mode
        document.getElementById('single-merge-form').style.display = 'block';
        document.getElementById('multi-merge-form').style.display = 'none';
        document.getElementById('merge-from-id').value = identityId;
        document.getElementById('merge-to-id').value = '';
    }
    
    document.getElementById('merge-notes').value = '';
    document.getElementById('merge-search-results').style.display = 'none';
    document.getElementById('merge-modal').style.display = 'flex';
}

// Helper function to escape HTML (XSS protection)
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Search identity for merge - ALL LOGIC IN BACKEND
async function searchIdentityForMerge() {
    const searchQuery = document.getElementById('merge-to-id').value.trim();
    const resultsDiv = document.getElementById('merge-search-results');

    if (!searchQuery) {
        showNotification('Please enter an identity ID or name to search', 'error');
        resultsDiv.style.display = 'none';
        return;
    }

    try {
        resultsDiv.innerHTML = '<div class="loading"><i class="fas fa-spinner fa-spin"></i> Searching...</div>';
        resultsDiv.style.display = 'block';

        // Backend handles all search logic - frontend just displays results
        const response = await fetch(`/api/admin/identities/search?query=${encodeURIComponent(searchQuery)}&limit=10`, {
            credentials: 'include' // Include HttpOnly cookies
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Search failed');
        }

        const data = await response.json(); // Backend sends: { results: [...], total: N, query: "..." }
        
        // Frontend only displays what backend sends
        if (!data.results || data.results.length === 0) {
            resultsDiv.innerHTML = `<div class="no-results"><p>No identities found matching "${escapeHtml(data.query)}"</p></div>`;
            return;
        }

        // Render results (backend sends all formatted data)
        resultsDiv.innerHTML = data.results.map(identity => `
            <div class="merge-search-result">
                <div class="result-image">
                    ${identity.snapshot_url || identity.best_snapshot_path ? 
                        `<img src="${escapeHtml(identity.snapshot_url || `/${identity.best_snapshot_path}`)}" alt="Identity" onerror="this.parentElement.innerHTML='<div class=\\'no-image\\'><i class=\\'fas fa-user\\'></i></div>'">` :
                        `<div class="no-image"><i class="fas fa-user"></i></div>`
                    }
                </div>
                <div class="result-info">
                    <h4>${escapeHtml(identity.display_name || 'Unknown Identity')}</h4>
                    <div style="margin: 0.5rem 0; padding: 0.5rem; background: rgba(0, 255, 150, 0.1); border: 1px solid rgba(0, 255, 150, 0.3); border-radius: 4px;">
                        <p style="margin: 0 0 0.25rem 0; font-size: 0.75rem; color: #999;">Identity ID:</p>
                        <code style="font-size: 0.8rem; color: #00ff96; word-break: break-all; cursor: pointer;" onclick="copyIdentityId('${identity.id}')" title="Click to copy">${escapeHtml(identity.id)}</code>
                    </div>
                    <p>Type: ${escapeHtml(identity.type)} | Status: ${escapeHtml(identity.status)}</p>
                    <p>Appearances: ${identity.appearances_count}</p>
                    <p>First Seen: ${new Date(identity.first_seen_at).toLocaleString()}</p>
                    <div style="display: flex; gap: 0.5rem; margin-top: 0.75rem;">
                        <button class="intelligence-admin-btn small" onclick="selectIdentityForMerge('${identity.id}')" style="flex: 1;">
                            <div class="btn-content">
                                <div class="btn-icon-wrapper">
                                    <i class="fas fa-check"></i>
                                </div>
                                <span class="btn-title">SELECT</span>
                            </div>
                        </button>
                        <button class="intelligence-admin-btn small" onclick="copyIdentityId('${identity.id}')" title="Copy Identity ID">
                            <div class="btn-content" style="padding: 0;">
                                <div class="btn-icon-wrapper" style="width: 24px; height: 24px;">
                                    <i class="fas fa-copy" style="font-size: 0.8rem;"></i>
                                </div>
                            </div>
                        </button>
                    </div>
                </div>
            </div>
        `).join('');
        
    } catch (error) {
        console.error('Error searching identities:', error);
        resultsDiv.innerHTML = `<div class="no-results"><p>Search failed: ${escapeHtml(error.message)}</p></div>`;
    }
}

// Copy identity ID to clipboard
function copyIdentityId(identityId) {
    navigator.clipboard.writeText(identityId).then(() => {
        showNotification('Identity ID copied to clipboard!', 'success');
    }).catch(err => {
        // Fallback for older browsers
        const textArea = document.createElement('textarea');
        textArea.value = identityId;
        textArea.style.position = 'fixed';
        textArea.style.opacity = '0';
        document.body.appendChild(textArea);
        textArea.select();
        try {
            document.execCommand('copy');
            showNotification('Identity ID copied to clipboard!', 'success');
        } catch (err) {
            showNotification('Failed to copy ID. Please copy manually.', 'error');
        }
        document.body.removeChild(textArea);
    });
}

// Select identity for merge
function selectIdentityForMerge(identityId) {
    document.getElementById('merge-to-id').value = identityId;
    document.getElementById('merge-search-results').style.display = 'none';
    showNotification('Identity selected for merge', 'success');
}

// Merge identities - ALL VALIDATION IN BACKEND
async function mergeIdentities() {
    const notes = document.getElementById('merge-notes').value.trim();
    
    // Check if multi-merge or single merge
    const multiMergeForm = document.getElementById('multi-merge-form');
    const isMultiMerge = multiMergeForm.style.display !== 'none' && selectedIdentities.size >= 2;
    
    try {
        let response;
        
        if (isMultiMerge) {
            // Multi-merge: use new endpoint
            const identityIds = Array.from(selectedIdentities);
            const targetId = document.getElementById('multi-merge-target-id').value.trim() || null;
            
            response = await fetch('/api/admin/identities/merge-multiple', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                credentials: 'include', // Include HttpOnly cookies
                body: JSON.stringify({
                    identity_ids: identityIds,
                    target_identity_id: targetId,
                    notes: notes || null
                })
            });
        } else {
            // Single merge: use existing endpoint
            const fromId = document.getElementById('merge-from-id').value.trim();
            const toId = document.getElementById('merge-to-id').value.trim();
            
            response = await fetch('/api/admin/identities/merge', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                credentials: 'include', // Include HttpOnly cookies
                body: JSON.stringify({
                    from_identity_id: fromId,
                    to_identity_id: toId,
                    notes: notes || null
                })
            });
        }

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to merge identities');
        }

        const result = await response.json(); // Backend sends success message
        showNotification(result.message || 'Identities merged successfully', 'success');
        document.getElementById('merge-modal').style.display = 'none';
        
        // Clear selections if multi-merge
        if (isMultiMerge) {
            selectedIdentities.clear();
            document.getElementById('multi-merge-target-id').value = '';
        }
        
        loadUnknownFaces(); // Refresh the list
    } catch (error) {
        console.error('Error merging identities:', error);
        showNotification(error.message || 'Error merging identities', 'error');
    }
}

// =====================================================
// Advanced Merge Preview (Production Feature)
// =====================================================

// Open advanced merge preview modal
async function openAdvancedMergePreview() {
    if (selectedIdentities.size < 2) {
        showNotification('Select at least 2 identities to preview merge', 'error');
        return;
    }
    
    const previewModal = document.getElementById('merge-preview-modal');
    const previewContent = document.getElementById('merge-preview-content');
    const previewLoading = document.getElementById('merge-preview-loading');
    
    // Show modal with loading state
    previewModal.style.display = 'flex';
    previewLoading.style.display = 'block';
    previewContent.style.display = 'none';
    
    try {
        const identityIds = Array.from(selectedIdentities);
        const targetId = document.getElementById('multi-merge-target-id')?.value.trim() || null;
        
        // Fetch merge preview from backend
        const response = await fetch('/api/admin/identities/merge-preview', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include', // Include HttpOnly cookies
            body: JSON.stringify({
                identity_ids: identityIds,
                target_identity_id: targetId
            })
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to generate merge preview');
        }
        
        const preview = await response.json();
        renderMergePreview(preview);
        
    } catch (error) {
        console.error('Error fetching merge preview:', error);
        previewContent.innerHTML = `
            <div style="text-align: center; padding: 2rem; color: #ff6b6b;">
                <i class="fas fa-exclamation-triangle" style="font-size: 2rem; margin-bottom: 1rem;"></i>
                <h3>Preview Failed</h3>
                <p>${escapeHtml(error.message)}</p>
            </div>
        `;
        previewContent.style.display = 'block';
    } finally {
        previewLoading.style.display = 'none';
    }
}

// Render merge preview content
function renderMergePreview(preview) {
    const previewContent = document.getElementById('merge-preview-content');
    
    // Build warnings HTML
    const warningsHtml = preview.warnings && preview.warnings.length > 0 ? `
        <div class="preview-section warnings-section">
            <h4><i class="fas fa-exclamation-triangle"></i> Warnings</h4>
            <div class="warnings-list">
                ${preview.warnings.map(w => `<div class="warning-item">${escapeHtml(w)}</div>`).join('')}
            </div>
        </div>
    ` : '';
    
    // Build target identity HTML
    const targetHtml = `
        <div class="preview-section target-section">
            <h4><i class="fas fa-crown"></i> Target Identity ${preview.target_identity.auto_selected ? '<span class="auto-selected-badge">Auto-Selected</span>' : ''}</h4>
            <div class="identity-preview-card target-card">
                <div class="preview-image">
                    ${preview.target_identity.snapshot_url || preview.target_identity.best_snapshot_path ? 
                        `<img src="${escapeHtml(preview.target_identity.snapshot_url || `/${preview.target_identity.best_snapshot_path}`)}" alt="Target" onerror="this.parentElement.innerHTML='<div class=\\'no-image\\'><i class=\\'fas fa-user\\'></i></div>'">` :
                        `<div class="no-image"><i class="fas fa-user"></i></div>`
                    }
                </div>
                <div class="preview-info">
                    <p class="identity-name">${escapeHtml(preview.target_identity.display_name || 'Unknown Identity')}</p>
                    <p class="identity-id">${escapeHtml(preview.target_identity.id)}</p>
                    <div class="preview-badges">
                        <span class="badge type-badge ${preview.target_identity.type}">${preview.target_identity.type.toUpperCase()}</span>
                        <span class="badge status-badge">${preview.target_identity.status}</span>
                    </div>
                    <p class="preview-stat"><i class="fas fa-eye"></i> ${preview.target_identity.appearances_count} appearances</p>
                    <p class="preview-stat"><i class="fas fa-video"></i> ${(preview.target_identity.pipelines || []).length} pipelines</p>
                    ${preview.target_identity.pipelines && preview.target_identity.pipelines.length > 0 ? 
                        `<div class="pipeline-tags">${preview.target_identity.pipelines.map(p => `<span class="pipeline-tag">${escapeHtml(p)}</span>`).join('')}</div>` : ''
                    }
                </div>
            </div>
        </div>
    `;
    
    // Build source identities HTML
    const sourcesHtml = `
        <div class="preview-section sources-section">
            <h4><i class="fas fa-compress-arrows-alt"></i> Source Identities (${preview.source_identities.length})</h4>
            <div class="sources-list">
                ${preview.source_identities.map(source => `
                    <div class="identity-preview-card source-card">
                        <div class="preview-image small">
                            ${source.snapshot_url || source.best_snapshot_path ? 
                                `<img src="${escapeHtml(source.snapshot_url || `/${source.best_snapshot_path}`)}" alt="Source" onerror="this.parentElement.innerHTML='<div class=\\'no-image\\'><i class=\\'fas fa-user\\'></i></div>'">` :
                                `<div class="no-image"><i class="fas fa-user"></i></div>`
                            }
                        </div>
                        <div class="preview-info">
                            <p class="identity-name">${escapeHtml(source.display_name || 'Unknown')}</p>
                            <p class="identity-id-short">${escapeHtml(source.id.substring(0, 8))}...</p>
                            <span class="badge type-badge ${source.type}">${source.type.toUpperCase()}</span>
                            <p class="preview-stat-small"><i class="fas fa-eye"></i> ${source.appearances_count}</p>
                            ${source.pipelines && source.pipelines.length > 0 ? 
                                `<div class="pipeline-tags-small">${source.pipelines.map(p => `<span class="pipeline-tag-small">${escapeHtml(p)}</span>`).join('')}</div>` : ''
                            }
                        </div>
                    </div>
                `).join('')}
            </div>
        </div>
    `;
    
    // Build type promotion HTML
    const typePromotionHtml = preview.type_promotion && preview.type_promotion.will_change ? `
        <div class="preview-section promotion-section">
            <h4><i class="fas fa-level-up-alt"></i> Type Promotion</h4>
            <div class="promotion-info">
                <div class="promotion-flow">
                    <span class="type-badge unknown">UNKNOWN</span>
                    <i class="fas fa-arrow-right"></i>
                    <span class="type-badge known">KNOWN</span>
                </div>
                <p class="promotion-reason">${escapeHtml(preview.type_promotion.reason || 'Source contains KNOWN identity')}</p>
                ${preview.type_promotion.inherited_name ? `<p class="promotion-name">Inherited name: <strong>${escapeHtml(preview.type_promotion.inherited_name)}</strong></p>` : ''}
            </div>
        </div>
    ` : '';
    
    // Build snapshot selection HTML
    const snapshotHtml = preview.snapshot_selection && preview.snapshot_selection.will_change ? `
        <div class="preview-section snapshot-section">
            <h4><i class="fas fa-camera"></i> Snapshot Update</h4>
            <div class="snapshot-info">
                <p><i class="fas fa-arrow-up" style="color: #00ff96;"></i> Better quality snapshot found</p>
                <p class="snapshot-quality">${escapeHtml(preview.snapshot_selection.quality_improvement || 'Quality improvement')}</p>
            </div>
        </div>
    ` : '';
    
    // Build statistics HTML
    const statsHtml = `
        <div class="preview-section stats-section">
            <h4><i class="fas fa-chart-bar"></i> Merge Statistics</h4>
            <div class="stats-grid">
                <div class="stat-item">
                    <span class="stat-value">${preview.statistics.total_identities}</span>
                    <span class="stat-label">Identities</span>
                </div>
                <div class="stat-item">
                    <span class="stat-value">${preview.statistics.total_appearances}</span>
                    <span class="stat-label">Total Appearances</span>
                </div>
                <div class="stat-item">
                    <span class="stat-value">${preview.statistics.total_embeddings}</span>
                    <span class="stat-label">Embeddings</span>
                </div>
                <div class="stat-item">
                    <span class="stat-value">${preview.statistics.total_pipelines}</span>
                    <span class="stat-label">Pipelines</span>
                </div>
            </div>
            ${preview.statistics.pipeline_list && preview.statistics.pipeline_list.length > 0 ? `
                <div class="all-pipelines">
                    <p style="margin: 0.5rem 0 0.25rem 0; font-size: 0.75rem; color: #999;">All Pipelines:</p>
                    <div class="pipeline-tags">${preview.statistics.pipeline_list.map(p => `<span class="pipeline-tag">${escapeHtml(p)}</span>`).join('')}</div>
                </div>
            ` : ''}
        </div>
    `;
    
    // Build selection details HTML (if auto-selected)
    const selectionHtml = preview.selection_details && preview.selection_details.candidates ? `
        <div class="preview-section selection-section">
            <h4><i class="fas fa-brain"></i> Target Selection (AI Scoring)</h4>
            <div class="selection-details">
                <table class="selection-table">
                    <thead>
                        <tr>
                            <th>Identity</th>
                            <th>Type</th>
                            <th>Appearances</th>
                            <th>Pipelines</th>
                            <th>Score</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${preview.selection_details.candidates.slice(0, 5).map((c, i) => `
                            <tr class="${i === 0 ? 'selected-row' : ''}">
                                <td title="${c.id}">${escapeHtml(c.display_name || c.id.substring(0, 8) + '...')}</td>
                                <td><span class="badge type-badge ${c.type}">${c.type.toUpperCase()}</span></td>
                                <td>${c.appearances}</td>
                                <td>${c.pipeline_count}</td>
                                <td class="score-cell">${c.score.toLocaleString()}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
                <p class="selection-note">* Target selected based on highest composite score (type, appearances, pipeline diversity, age)</p>
            </div>
        </div>
    ` : '';
    
    // Combine all sections
    previewContent.innerHTML = `
        <div class="merge-preview-container">
            ${warningsHtml}
            ${targetHtml}
            ${sourcesHtml}
            ${typePromotionHtml}
            ${snapshotHtml}
            ${statsHtml}
            ${selectionHtml}
        </div>
    `;
    
    previewContent.style.display = 'block';
}

// Execute merge from preview modal
async function executeMergeFromPreview() {
    const identityIds = Array.from(selectedIdentities);
    const targetId = document.getElementById('multi-merge-target-id')?.value.trim() || null;
    const notes = document.getElementById('merge-preview-notes')?.value.trim() || '';
    
    // Show loading state
    const executeBtn = document.querySelector('#merge-preview-modal .execute-merge-btn');
    const originalText = executeBtn.innerHTML;
    executeBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Merging...';
    executeBtn.disabled = true;
    
    try {
        const response = await fetch('/api/admin/identities/merge-multiple', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include', // Include HttpOnly cookies
            body: JSON.stringify({
                identity_ids: identityIds,
                target_identity_id: targetId,
                notes: notes || null
            })
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to merge identities');
        }
        
        const result = await response.json();
        
        // Close preview modal
        document.getElementById('merge-preview-modal').style.display = 'none';
        document.getElementById('merge-modal').style.display = 'none';
        
        // Show success notification with enhanced details
        let message = result.message || 'Identities merged successfully';
        if (result.type_promotion && result.type_promotion.changed) {
            message += ` | Type promoted to KNOWN`;
        }
        if (result.statistics) {
            message += ` | ${result.statistics.pipeline_count} pipelines`;
        }
        showNotification(message, 'success');
        
        // Clear selections
        selectedIdentities.clear();
        updateMergeMultipleButton();
        
        // Refresh the list
        loadUnknownFaces();
        
    } catch (error) {
        console.error('Error executing merge:', error);
        showNotification(error.message || 'Error merging identities', 'error');
    } finally {
        executeBtn.innerHTML = originalText;
        executeBtn.disabled = false;
    }
}

// Close preview modal
function closeMergePreviewModal() {
    document.getElementById('merge-preview-modal').style.display = 'none';
}

// =====================================================
// Merge Suggestions
// =====================================================

// Load merge suggestions
async function loadMergeSuggestions() {
    const listDiv = document.getElementById('merge-suggestions-list');

    try {
        listDiv.innerHTML = '<div class="loading"><i class="fas fa-spinner fa-spin"></i> Loading merge suggestions...</div>';

        const response = await fetch('/api/admin/merge-suggestions', {
            credentials: 'include' // Include HttpOnly cookies
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Failed to load merge suggestions' }));
            throw new Error(errorData.detail || `HTTP ${response.status}: Failed to load merge suggestions`);
        }

        const suggestions = await response.json();
        
        console.log('[MERGE_SUGGESTIONS] Received response:', {
            status: response.status,
            count: suggestions ? suggestions.length : 0,
            data: suggestions
        });

        if (!suggestions || suggestions.length === 0) {
            console.log('[MERGE_SUGGESTIONS] No suggestions found - this is normal if clustering hasn\'t run yet or no duplicates exist');
            listDiv.innerHTML = `
                <div class="no-results">
                    <i class="fas fa-inbox"></i>
                    <p><strong>No merge suggestions available</strong></p>
                    <p style="margin-top: 0.5rem; font-size: 0.85rem; color: #999; text-align: left; max-width: 600px; margin-left: auto; margin-right: auto;">
                        This is normal if:
                        <ul style="text-align: left; margin: 0.5rem 0; padding-left: 1.5rem;">
                            <li>Clustering job hasn't run yet (configurable delay after startup)</li>
                            <li>No duplicate identities have been detected</li>
                            <li>All suggestions have been approved/rejected</li>
                        </ul>
                    </p>
                    <p style="margin-top: 0.5rem; font-size: 0.85rem; color: #00ff96;">
                        <i class="fas fa-lightbulb"></i> <strong>Tip:</strong> The system automatically finds duplicates across ALL cameras, 
                        including cross-camera matches (same person on different cameras).
                    </p>
                    <p style="margin-top: 0.5rem; font-size: 0.85rem; color: #999;">
                        <i class="fas fa-info-circle"></i> Check Admin → Settings for clustering configuration.
                    </p>
                </div>
            `;
            return;
        }

        // Frontend just displays what backend sends - ALL LOGIC IN BACKEND
        listDiv.innerHTML = suggestions.map(suggestion => {
            // Backend sends all formatted data - frontend just renders
            const confidenceClass = suggestion.confidence_percent >= 70 ? 'high-confidence' : 
                                   suggestion.confidence_percent >= 50 ? 'medium-confidence' : 'low-confidence';
            
            // Different icons and colors for different suggestion types
            let clusterIcon = 'fa-link';
            let badgeHtml = '';
            let cardExtraClass = '';
            
            if (suggestion.is_cross_camera) {
                // Cross-camera: orange/warning theme
                clusterIcon = 'fa-video';
                cardExtraClass = 'cross-camera-match';
                badgeHtml = '<span style="background: rgba(255, 165, 0, 0.3); padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; color: #ffa500; border: 1px solid rgba(255, 165, 0, 0.5);"><i class="fas fa-exclamation-triangle"></i> CROSS-CAMERA</span>';
            } else if (suggestion.is_large_cluster) {
                clusterIcon = 'fa-object-group';
                badgeHtml = '<span style="background: rgba(0, 255, 150, 0.2); padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; color: #00ff96;">GRAPH CLUSTER</span>';
            }
            
            // Show pipelines involved
            const pipelinesHtml = suggestion.pipelines && suggestion.pipelines.length > 0 
                ? `<div style="margin-top: 0.5rem; display: flex; flex-wrap: wrap; gap: 0.3rem;">
                     ${suggestion.pipelines.map(p => `<span style="background: rgba(100, 100, 255, 0.2); padding: 0.15rem 0.4rem; border-radius: 3px; font-size: 0.7rem; color: #88f;"><i class="fas fa-camera"></i> ${escapeHtml(p)}</span>`).join('')}
                   </div>`
                : '';
            
            return `
            <div class="merge-suggestion-card ${confidenceClass} ${cardExtraClass}" ${suggestion.is_cross_camera ? 'style="border-left: 3px solid #ffa500;"' : ''}>
                <div class="suggestion-header">
                    <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                        <i class="fas ${clusterIcon}" style="color: ${suggestion.is_cross_camera ? '#ffa500' : '#00ff96'};"></i>
                        <h3>${escapeHtml(suggestion.display_name || `Cluster ${suggestion.cluster_id}`)}</h3>
                        ${badgeHtml}
                    </div>
                    <span class="confidence-badge ${confidenceClass}">${suggestion.confidence_percent}% confidence</span>
                </div>
                ${suggestion.is_cross_camera ? `
                <div style="padding: 0.5rem 0.75rem; background: rgba(255, 165, 0, 0.1); border-radius: 4px; margin: 0.5rem 0; border: 1px solid rgba(255, 165, 0, 0.3);">
                    <p style="margin: 0; font-size: 0.8rem; color: #ffa500;">
                        <i class="fas fa-exclamation-triangle"></i> <strong>Cross-Camera Match:</strong> These identities were seen on different cameras. 
                        The faces look similar, but since they weren't seen together, please verify carefully before approving.
                    </p>
                </div>
                ` : ''}
                <div style="padding: 0.75rem; background: rgba(0, 0, 0, 0.3); border-radius: 4px; margin: 0.5rem 0;">
                    <p style="margin: 0 0 0.5rem 0; font-size: 0.85rem; color: #999;">
                        <i class="fas fa-info-circle"></i> <strong>Recommendation:</strong> ${escapeHtml(suggestion.recommendation || 'Review carefully')}
                    </p>
                    <p style="margin: 0; font-size: 0.85rem; color: #ccc;">
                        <strong>Identities to merge:</strong> ${suggestion.identity_count || 0} | 
                        <strong>Snapshots:</strong> ${suggestion.snapshot_count || 0}
                    </p>
                    ${pipelinesHtml}
                </div>
                ${suggestion.identity_ids && suggestion.identity_ids.length > 0 ? `
                    <div class="suggestion-identities" style="margin: 0.5rem 0;">
                        <p style="margin: 0 0 0.5rem 0; font-size: 0.85rem; color: #999;"><strong>Identity IDs:</strong></p>
                        <div class="identity-ids" style="display: flex; flex-wrap: wrap; gap: 0.5rem;">
                            ${suggestion.identity_ids.map(id => 
                                `<span class="identity-tag" title="${escapeHtml(id)}" style="cursor: pointer;" onclick="copyIdentityId('${escapeHtml(id)}')">${escapeHtml(id.substring(0, 8))}...</span>`
                            ).join('')}
                        </div>
                    </div>
                ` : ''}
                ${suggestion.representative_snapshots && suggestion.representative_snapshots.length > 0 ? `
                    <div class="suggestion-snapshots" style="display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.75rem 0; padding: 0.75rem; background: rgba(0, 0, 0, 0.2); border-radius: 4px;">
                        ${suggestion.representative_snapshots.map(snap => 
                            `<img src="/${escapeHtml(snap)}" alt="Identity snapshot" 
                                  style="max-width: 120px; max-height: 120px; border: 2px solid #00ff96; border-radius: 4px; object-fit: cover; cursor: pointer;" 
                                  onclick="window.open('/${escapeHtml(snap)}', '_blank')"
                                  onerror="this.style.display='none'">`
                        ).join('')}
                    </div>
                ` : '<div style="padding: 1rem; text-align: center; color: #999; font-style: italic;">No snapshots available</div>'}
                <div class="suggestion-actions" style="display: flex; gap: 0.5rem; margin-top: 1rem;">
                    ${suggestion.id ? `
                        <button class="intelligence-admin-btn small" onclick="approveMergeSuggestion(${suggestion.id})" style="flex: 1;">
                            <div class="btn-content">
                                <div class="btn-icon-wrapper">
                                    <i class="fas fa-check"></i>
                                </div>
                                <span class="btn-title">APPROVE</span>
                            </div>
                        </button>
                        <button class="intelligence-admin-btn small" onclick="rejectMergeSuggestion(${suggestion.id})" style="flex: 1;">
                            <div class="btn-content">
                                <div class="btn-icon-wrapper">
                                    <i class="fas fa-times"></i>
                                </div>
                                <span class="btn-title">REJECT</span>
                            </div>
                        </button>
                    ` : `
                        <div style="flex: 1; padding: 0.75rem; text-align: center; color: #999; font-size: 0.85rem; font-style: italic;">
                            <i class="fas fa-info-circle"></i> DBSCAN-generated cluster (refresh to regenerate)
                        </div>
                    `}
                </div>
            </div>
        `;
        }).join('');
    } catch (error) {
        console.error('Error loading merge suggestions:', error);
        listDiv.innerHTML = `<div class="no-results"><i class="fas fa-exclamation-triangle"></i><p>Error: ${error.message}</p><button class="intelligence-admin-btn small" onclick="loadMergeSuggestions()" style="margin-top: 1rem;"><div class="btn-content"><div class="btn-icon-wrapper"><i class="fas fa-redo"></i></div><span class="btn-title">RETRY</span></div></button></div>`;
    }
}

// Approve merge suggestion
async function approveMergeSuggestion(suggestionId) {
    // Safety check: prevent approving suggestions without IDs (on-the-fly DBSCAN suggestions)
    if (!suggestionId || suggestionId === null || suggestionId === 'null' || suggestionId === undefined) {
        showNotification('Cannot approve on-the-fly suggestions. Please use the regular merge suggestions page.', 'error');
        return;
    }

    if (!confirm('Are you sure you want to approve and execute this merge?')) {
        return;
    }

    try {
        const response = await fetch(`/api/admin/merge-suggestions/${suggestionId}/approve`, {
            method: 'POST',
            credentials: 'include' // Include HttpOnly cookies
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to approve merge suggestion');
        }

        const result = await response.json(); // Backend sends success message
        showNotification(result.message || `Merge approved: ${result.merged_identities || 'identities'} merged`, 'success');
        loadMergeSuggestions(); // Refresh the list
        loadUnknownFaces(); // Refresh unknown faces list
    } catch (error) {
        console.error('Error approving merge suggestion:', error);
        showNotification(error.message || 'Error approving merge suggestion', 'error');
    }
}

// Reject merge suggestion - ALL LOGIC IN BACKEND
async function rejectMergeSuggestion(suggestionId) {
    if (!confirm('Are you sure you want to reject this merge suggestion?')) {
        return;
    }

    try {
        const response = await fetch(`/api/admin/merge-suggestions/${suggestionId}/reject`, {
            method: 'POST',
            credentials: 'include' // Include HttpOnly cookies
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to reject merge suggestion');
        }

        const result = await response.json(); // Backend sends success message
        showNotification(result.message || 'Merge suggestion rejected', 'success');
        loadMergeSuggestions(); // Refresh the list
    } catch (error) {
        console.error('Error rejecting merge suggestion:', error);
        showNotification(error.message || 'Error rejecting merge suggestion', 'error');
    }
}

// =====================================================
// Pipeline-Specific Merge Suggestions
// =====================================================

// Open pipeline merge suggestions modal
async function openPipelineMergeSuggestions(pipelineId) {
    console.log('[PIPELINE_MERGE] openPipelineMergeSuggestions called with pipelineId:', pipelineId);
    // Check user permissions for this pipeline
    if (window.userPipelines !== null && !window.userPipelines.includes(pipelineId)) {
        showNotification(`You do not have permission to access pipeline: ${getPipelineDisplayName(pipelineId)}`, 'error');
        return;
    }

    // Show the friendly name in the modal header (raw id stays on hover)
    const mergeTitleEl = document.getElementById('pipeline-merge-pipeline-id');
    mergeTitleEl.textContent = getPipelineDisplayName(pipelineId);
    mergeTitleEl.title = pipelineId;
    
    // Show modal
    document.getElementById('pipeline-merge-suggestions-modal').style.display = 'flex';
    
    // Load suggestions for this pipeline
    await loadPipelineMergeSuggestions(pipelineId);
}

// Load merge suggestions for a specific pipeline
async function loadPipelineMergeSuggestions(pipelineId) {
    const listDiv = document.getElementById('pipeline-merge-suggestions-list');

    try {
        listDiv.innerHTML = '<div class="loading"><i class="fas fa-spinner fa-spin"></i> Loading merge suggestions for this pipeline...</div>';

        const response = await fetch(`/api/admin/merge-suggestions/pipeline/${encodeURIComponent(pipelineId)}`, {
            credentials: 'include'
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || `HTTP ${response.status}: ${response.statusText}`);
        }

        const suggestions = await response.json();

        if (suggestions.length === 0) {
            listDiv.innerHTML = `
                <div class="no-results">
                    <i class="fas fa-info-circle"></i>
                    <p>No merge suggestions found for pipeline <strong>${escapeHtml(getPipelineDisplayName(pipelineId))}</strong>.</p>
                    <p style="font-size: 0.85rem; color: #999; margin-top: 0.5rem;">
                        The system will generate suggestions based on face similarity when identities are detected.
                    </p>
                </div>
            `;
            return;
        }

        // Render suggestions (reuse the same rendering logic as general merge suggestions)
        listDiv.innerHTML = suggestions.map(suggestion => {
            const confidenceColor = suggestion.confidence >= 0.7 ? '#00ff96' : 
                                   suggestion.confidence >= 0.5 ? '#ffc107' : '#ff9800';
            const confidenceLabel = suggestion.confidence >= 0.7 ? 'High' : 
                                   suggestion.confidence >= 0.5 ? 'Medium' : 'Low';
            
            const snapshotsHtml = suggestion.representative_snapshots && suggestion.representative_snapshots.length > 0
                ? suggestion.representative_snapshots.map(snap => {
                    const snapUrl = snap.startsWith('storage/') ? `/${snap}` : `/storage/${snap}`;
                    return `<img src="${snapUrl}" alt="Snapshot" style="width: 80px; height: 80px; object-fit: cover; border-radius: 4px; border: 1px solid rgba(0, 255, 150, 0.3);" onerror="this.src='data:image/svg+xml,%3Csvg xmlns=\\'http://www.w3.org/2000/svg\\' viewBox=\\'0 0 24 24\\' fill=\\'%23999\\'%3E%3Cpath d=\\'M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z\\'/%3E%3C/svg%3E';">`;
                }).join('')
                : '<p style="color: #999; font-size: 0.85rem;">No snapshots available</p>';

            return `
            <div class="merge-suggestion-item" style="
                background: rgba(0, 0, 0, 0.3);
                border: 1px solid rgba(0, 255, 150, 0.2);
                border-radius: 6px;
                padding: 1rem;
                margin-bottom: 1rem;
            ">
                <div style="display: flex; justify-content: space-between; align-items: start; margin-bottom: 0.75rem;">
                    <div style="flex: 1;">
                        <h3 style="margin: 0 0 0.5rem 0; color: #00ff96; font-size: 1rem;">
                            ${suggestion.identity_count} Identities to Merge
                        </h3>
                        <p style="margin: 0; color: #ccc; font-size: 0.85rem;">
                            ${suggestion.display_name || 'Merge suggestion'}
                        </p>
                    </div>
                    <div style="text-align: right;">
                        <div style="color: ${confidenceColor}; font-weight: 600; font-size: 1.1rem;">
                            ${suggestion.confidence_percent}%
                        </div>
                        <div style="color: ${confidenceColor}; font-size: 0.75rem; margin-top: 0.25rem;">
                            ${confidenceLabel} Confidence
                        </div>
                    </div>
                </div>
                
                <div style="margin-bottom: 0.75rem;">
                    <p style="margin: 0 0 0.5rem 0; color: #999; font-size: 0.8rem;">
                        <strong>Recommendation:</strong> ${suggestion.recommendation || 'Review carefully'}
                    </p>
                    <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                        ${snapshotsHtml}
                    </div>
                </div>
                
                <div style="display: flex; gap: 0.5rem; margin-top: 1rem;">
                    ${suggestion.id ? `
                        <button class="intelligence-admin-btn small" onclick="approveMergeSuggestion(${suggestion.id})" style="flex: 1;">
                            <div class="btn-content">
                                <div class="btn-icon-wrapper">
                                    <i class="fas fa-check"></i>
                                </div>
                                <span class="btn-title">APPROVE</span>
                            </div>
                        </button>
                        <button class="intelligence-admin-btn small" onclick="rejectMergeSuggestion(${suggestion.id})" style="flex: 1;">
                            <div class="btn-content">
                                <div class="btn-icon-wrapper">
                                    <i class="fas fa-times"></i>
                                </div>
                                <span class="btn-title">REJECT</span>
                            </div>
                        </button>
                    ` : `
                        <div style="flex: 1; padding: 0.75rem; text-align: center; color: #999; font-size: 0.85rem; font-style: italic;">
                            <i class="fas fa-info-circle"></i> DBSCAN-generated cluster (on-the-fly suggestion - cannot be approved directly)
                        </div>
                    `}
                </div>
            </div>
        `;
        }).join('');
    } catch (error) {
        console.error('Error loading pipeline merge suggestions:', error);
        listDiv.innerHTML = `
            <div class="no-results">
                <i class="fas fa-exclamation-triangle"></i>
                <p>Error: ${error.message}</p>
                <button class="intelligence-admin-btn small" onclick="loadPipelineMergeSuggestions('${pipelineId}')" style="margin-top: 1rem;">
                    <div class="btn-content">
                        <div class="btn-icon-wrapper">
                            <i class="fas fa-redo"></i>
                        </div>
                        <span class="btn-title">RETRY</span>
                    </div>
                </button>
            </div>
        `;
    }
}

// Live Alert Functions
let currentLiveAlertIdentityId = null;

// Open create live alert modal - Backend provides all defaults
async function openCreateLiveAlertModal(identityId, identityName) {
    currentLiveAlertIdentityId = identityId;
    
    // Show loading state
    const modal = document.getElementById('create-live-alert-modal');
    modal.style.display = 'flex';
    document.getElementById('live-alert-identity-name').textContent = 'Loading...';
    
    // Set identity ID immediately (we already have it, no need to wait for backend)
    const identityIdElement = document.getElementById('live-alert-identity-id');
    if (identityIdElement) {
        identityIdElement.textContent = identityId;
        identityIdElement.setAttribute('data-identity-id', identityId);
    } else {
        console.error('[LIVE_ALERT] Identity ID element not found in DOM');
    }
    
    try {
        // Fetch default settings from backend (all logic in backend)
        const response = await fetch(`/api/live-alerts/defaults/${identityId}`, {
            credentials: 'include' // Include HttpOnly cookies
        });
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to load default settings');
        }
        
        const defaults = await response.json();
        
        // Backend provides all defaults - frontend just displays them
        document.getElementById('live-alert-identity-name').textContent = defaults.identity_name || 'Unknown';
        // Ensure identity ID is still set (in case element was recreated)
        if (identityIdElement) {
            identityIdElement.textContent = identityId;
            identityIdElement.setAttribute('data-identity-id', identityId);
        }
        document.getElementById('live-alert-name').value = defaults.default_name;
        document.getElementById('live-alert-min-similarity').value = defaults.default_min_similarity;
        document.getElementById('live-alert-similarity-value').textContent = `${Math.round(defaults.default_min_similarity * 100)}%`;
        document.getElementById('live-alert-notify-dashboard').checked = defaults.default_notify_dashboard;
        document.getElementById('live-alert-sound-alert').checked = defaults.default_sound_alert;
        document.getElementById('live-alert-auto-capture').checked = defaults.default_auto_capture;
        
        // Show warnings from backend if any
        if (defaults.warnings && defaults.warnings.length > 0) {
            const warningsHtml = defaults.warnings.map(w => 
                `<div style="color: #ffcc00; font-size: 0.85rem; margin-top: 0.5rem;">
                    <i class="fas fa-exclamation-triangle"></i> ${w}
                </div>`
            ).join('');
            
            // Add warnings to the form
            let warningsDiv = document.getElementById('live-alert-warnings');
            if (!warningsDiv) {
                warningsDiv = document.createElement('div');
                warningsDiv.id = 'live-alert-warnings';
                warningsDiv.style.marginBottom = '1rem';
                const form = document.getElementById('create-live-alert-form');
                form.insertBefore(warningsDiv, form.firstChild);
            }
            warningsDiv.innerHTML = warningsHtml;
        } else {
            const warningsDiv = document.getElementById('live-alert-warnings');
            if (warningsDiv) {
                warningsDiv.innerHTML = '';
            }
        }
        
        // Disable form if cannot create
        if (!defaults.can_create) {
            document.getElementById('create-live-alert-form').querySelector('button[type="submit"]').disabled = true;
            showNotification(`Cannot create alert: Maximum alerts per user (${defaults.max_alerts}) reached`, 'error');
        } else {
            document.getElementById('create-live-alert-form').querySelector('button[type="submit"]').disabled = false;
        }
        
    } catch (error) {
        console.error('Error loading default settings:', error);
        // Even if backend fails, show the identity ID and name we have
        document.getElementById('live-alert-identity-name').textContent = identityName || 'Unknown';
        // Ensure identity ID is still visible
        const identityIdElement = document.getElementById('live-alert-identity-id');
        if (identityIdElement) {
            identityIdElement.textContent = identityId;
            identityIdElement.setAttribute('data-identity-id', identityId);
        }
        showNotification(error.message || 'Error loading default settings', 'error');
        // Don't close modal - let user see the identity ID even if defaults failed
    }
}

// Create live alert - ALL LOGIC IN BACKEND
async function createLiveAlert() {
    // Frontend only collects form data - NO VALIDATION, backend handles everything
    const name = document.getElementById('live-alert-name').value.trim();
    const minSimilarity = parseFloat(document.getElementById('live-alert-min-similarity').value);
    const notifyDashboard = document.getElementById('live-alert-notify-dashboard').checked;
    const soundAlert = document.getElementById('live-alert-sound-alert').checked;
    const autoCapture = document.getElementById('live-alert-auto-capture').checked;
    
    // Disable submit button during request
    const submitBtn = document.getElementById('create-live-alert-form').querySelector('button[type="submit"]');
    const originalText = submitBtn.innerHTML;
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Creating...';
    
    try {
        // Backend handles all validation, limits, and business logic
        const response = await fetch('/api/live-alerts', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest', // CSRF header required on mutating live-alert routes
            },
            credentials: 'include', // Include HttpOnly cookies
            body: JSON.stringify({
                name: name,
                identity_id: currentLiveAlertIdentityId,
                min_similarity: minSimilarity,
                notify_dashboard: notifyDashboard,
                sound_alert: soundAlert,
                auto_capture_snapshot: autoCapture,
                expiration_type: 'never'
            })
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to create live alert');
        }
        
        const result = await response.json();
        console.log('[LIVE_ALERT] Alert created successfully:', result);
        showNotification(`Live alert "${name}" created successfully!`, 'success');
        document.getElementById('create-live-alert-modal').style.display = 'none';
        
        // Backend logic: redirect to live alerts page after successful creation
        // Use setTimeout to ensure notification is visible before redirect
        setTimeout(() => {
            console.log('[LIVE_ALERT] Redirecting to /admin/live-alerts');
            window.location.href = '/admin/live-alerts';
        }, 800);  // Increased delay to ensure notification is visible
        
    } catch (error) {
        console.error('Error creating live alert:', error);
        showNotification(error.message || 'Error creating live alert', 'error');
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalText;
    }
}

// Watchlist Functions
let currentWatchlistIdentityId = null;

// Open add to watchlist modal - Backend provides all defaults
async function openAddToWatchlistModal(identityId, identityName) {
    currentWatchlistIdentityId = identityId;
    
    // Show loading state
    const modal = document.getElementById('add-to-watchlist-modal');
    modal.style.display = 'flex';
    document.getElementById('watchlist-identity-name').textContent = 'Loading...';
    document.getElementById('watchlist-select').innerHTML = '<option value="">Loading watchlists...</option>';
    
    try {
        // Fetch defaults from backend (all logic in backend)
        const response = await fetch(`/api/watchlists/add-identity/${identityId}/defaults`, {
            credentials: 'include' // Include HttpOnly cookies
        });
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to load defaults');
        }
        
        const defaults = await response.json();
        
        // Backend provides all defaults - frontend just displays them
        document.getElementById('watchlist-identity-name').textContent = defaults.identity_name || 'Unknown';
        
        // Populate watchlist dropdown
        const select = document.getElementById('watchlist-select');
        if (!defaults.available_watchlists || defaults.available_watchlists.length === 0) {
            select.innerHTML = '<option value="">No watchlists available. Create a watchlist first.</option>';
            select.disabled = true;
        } else {
            select.innerHTML = defaults.available_watchlists.map(wl => {
                const disabled = wl.is_already_added ? 'disabled' : '';
                const selected = wl.is_already_added ? '' : '';
                const label = wl.is_already_added ? `${wl.name} (Already Added)` : wl.name;
                return `<option value="${wl.id}" ${disabled} ${selected}>${label}</option>`;
            }).join('');
            select.disabled = false;
        }
        
        // Set default priority
        document.getElementById('watchlist-priority').value = defaults.default_priority || 'normal';
        
        // Clear notes and action instructions
        document.getElementById('watchlist-notes').value = '';
        document.getElementById('watchlist-action-instructions').value = '';
        
    } catch (error) {
        console.error('Error loading defaults:', error);
        showNotification(error.message || 'Error loading defaults', 'error');
        modal.style.display = 'none';
    }
}

// Add identity to watchlist - ALL LOGIC IN BACKEND
async function addToWatchlist() {
    // Frontend only collects form data - NO VALIDATION, backend handles everything
    const watchlistId = document.getElementById('watchlist-select').value.trim();
    const priority = document.getElementById('watchlist-priority').value;
    const notes = document.getElementById('watchlist-notes').value.trim();
    const actionInstructions = document.getElementById('watchlist-action-instructions').value.trim();
    
    // Basic frontend check - ensure watchlist is selected (backend will validate too)
    if (!watchlistId) {
        showNotification('Please select a watchlist', 'error');
        return;
    }
    
    if (!currentWatchlistIdentityId) {
        showNotification('Identity ID is missing. Please try again.', 'error');
        return;
    }
    
    // Disable submit button during request
    const submitBtn = document.getElementById('add-to-watchlist-form').querySelector('button[type="submit"]');
    const originalText = submitBtn.innerHTML;
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Adding...';
    
    try {
        console.log('[WATCHLIST] Adding identity to watchlist:', {
            watchlistId: watchlistId,
            identityId: currentWatchlistIdentityId,
            priority: priority
        });
        
        // Backend handles ALL validation, checks, and business logic - frontend just sends data
        const response = await fetch(`/api/watchlists/${encodeURIComponent(watchlistId)}/entries`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest', // CSRF header (required on mutations)
            },
            credentials: 'include', // Include HttpOnly cookies
            body: JSON.stringify({
                identity_id: currentWatchlistIdentityId,
                priority: priority,
                notes: notes || null,
                action_instructions: actionInstructions || null,
                expires_at: null
            })
        });
        
        if (!response.ok) {
            let errorMessage = 'Failed to add to watchlist';
            try {
                const error = await response.json();
                errorMessage = error.detail || error.message || errorMessage;
                console.error('[WATCHLIST] Backend error:', error);
            } catch (e) {
                // If response is not JSON, use status text
                errorMessage = response.statusText || `HTTP ${response.status}: ${errorMessage}`;
                console.error('[WATCHLIST] Non-JSON error response:', response.status, response.statusText);
            }
            throw new Error(errorMessage);
        }
        
        const result = await response.json();
        console.log('[WATCHLIST] Identity added successfully:', result);
        showNotification(`Identity added to watchlist successfully!`, 'success');
        document.getElementById('add-to-watchlist-modal').style.display = 'none';
        
    } catch (error) {
        console.error('Error adding to watchlist:', error);
        showNotification(error.message || 'Error adding to watchlist', 'error');
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalText;
    }
}

// Copy identity ID from alert form
function copyIdentityIdFromAlert() {
    const identityIdElement = document.getElementById('live-alert-identity-id');
    if (!identityIdElement) {
        showNotification('Identity ID not found', 'error');
        return;
    }
    const identityId = identityIdElement.getAttribute('data-identity-id') || identityIdElement.textContent.trim();
    copyIdentityId(identityId);
}

// Make functions global for onclick handlers
window.viewIdentityDetails = viewIdentityDetails;
window.promoteIdentityModal = promoteIdentityModal;
window.loadUnknownFaces = loadUnknownFaces;
window.openMergeModal = openMergeModal;
window.toggleIdentitySelection = toggleIdentitySelection;
window.removeFromSelection = removeFromSelection;
window.searchIdentityForMerge = searchIdentityForMerge;
window.selectIdentityForMerge = selectIdentityForMerge;
window.approveMergeSuggestion = approveMergeSuggestion;
window.rejectMergeSuggestion = rejectMergeSuggestion;
window.openCreateLiveAlertModal = openCreateLiveAlertModal;
window.openAddToWatchlistModal = openAddToWatchlistModal;
window.copyIdentityId = copyIdentityId; // Make copyIdentityId global so copyIdentityIdFromAlert can use it
window.copyIdentityIdFromAlert = copyIdentityIdFromAlert;
window.openPipelineMergeSuggestions = openPipelineMergeSuggestions;
window.loadPipelineMergeSuggestions = loadPipelineMergeSuggestions;

