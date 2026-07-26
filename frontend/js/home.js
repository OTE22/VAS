// Home Page JavaScript
let currentUser = null;

// Load user info for UI customization only - BACKEND HANDLES ALL AUTHENTICATION
// Backend route already authenticated user and checks admin role, exception handler handles redirects
document.addEventListener('DOMContentLoaded', async () => {
    // IMMEDIATELY show page - backend already authenticated user
    // Don't wait for API call to show content
    document.body.classList.add('admin-verified');
    document.body.classList.add('admin-user');
    
    try {
        // Fetch user info for UI customization only (not for authentication)
        // Backend already authenticated user and verified admin role before serving this page
        const response = await fetch('/api/auth/me', {
            credentials: 'include' // Include HttpOnly cookies
        });

        if (response.ok) {
            currentUser = await response.json();
        
            // Backend already verified admin role - just load UI
            console.log('[HOME] Loading home page content');
            
            displayUserInfo();
            
            // Show/hide admin-only elements (stats) based on role
            const adminSection = document.getElementById('admin-section');
            if (adminSection) {
                adminSection.style.display = 'block';
            }
            
            // Load stats after currentUser is set
            loadStats();
            loadPipelines();
            
            // Show tracking link if user has chatbot access
            if (currentUser.can_use_chatbot) {
                const trackingLink = document.getElementById('tracking-link');
                if (trackingLink) {
                    trackingLink.style.display = 'block';
                }
            }
        } else {
            // If we can't get user info, still show page - backend already authenticated
            console.warn('[HOME] Could not fetch user info, but backend already authenticated');
            // Still try to load stats and pipelines
            loadStats();
            loadPipelines();
        }
    } catch (error) {
        // Non-fatal error - backend already authenticated user, still show page
        console.warn('[HOME] Error loading user info (non-fatal):', error);
        // Still try to load stats and pipelines
        loadStats();
        loadPipelines();
    }
});

function displayUserInfo() {
    // Note: user-info and logout-btn are in the navbar, which loads dynamically
    // This function will be called after navbar loads, or we can retry
    const userInfo = document.getElementById('user-info');
    const logoutBtn = document.getElementById('logout-btn');
    
    if (currentUser) {
        if (userInfo) {
            userInfo.textContent = `${currentUser.full_name || currentUser.username} (${currentUser.role})`;
        }
        if (logoutBtn) {
            logoutBtn.style.display = 'block';
        } else {
            // Retry after navbar loads
            setTimeout(() => {
                const retryLogoutBtn = document.getElementById('logout-btn');
                if (retryLogoutBtn) {
                    retryLogoutBtn.style.display = 'block';
                }
            }, 600);
        }
    }
}

async function loadStats() {
    try {
        const response = await fetch('/api/stats', {
            credentials: 'include' // Include HttpOnly cookies
        });

        if (!response.ok) {
            console.error('[HOME] Stats API error:', response.status, response.statusText);
            const errorText = await response.text();
            console.error('[HOME] Error response:', errorText);
            
            // Set error states
            if (currentUser && currentUser.role === 'admin') {
                document.getElementById('system-status').textContent = 'Error';
            }
            return;
        }

        const stats = await response.json();
        console.log('[HOME] Stats received:', stats);
        
        // Update all stats - admin sees all, regular users see filtered
        if (currentUser && currentUser.role === 'admin') {
            // Admin sees all stats - ensure all elements are visible
            const totalFacesEl = document.getElementById('total-faces');
            const activePipelinesEl = document.getElementById('active-pipelines');
            const queueSizeEl = document.getElementById('queue-size');
            const systemStatusEl = document.getElementById('system-status');
            
            if (totalFacesEl) {
                totalFacesEl.textContent = stats.faces?.total ?? 0;
            }
            if (activePipelinesEl) {
                activePipelinesEl.textContent = stats.pipelines?.active ?? 0;
            }
            if (queueSizeEl) {
                queueSizeEl.textContent = stats.queue?.queue_size ?? 0;
            }
            if (systemStatusEl) {
                // Determine system status based on stats
                const isHealthy = stats.queue && stats.queue.queue_size !== undefined;
                systemStatusEl.textContent = isHealthy ? 'Healthy' : 'Unknown';
            }
            
            console.log('[HOME] Admin stats updated:', {
                faces: stats.faces?.total,
                pipelines: stats.pipelines?.active,
                queue: stats.queue?.queue_size
            });
        } else {
            // Regular users see filtered stats
            const totalFacesEl = document.getElementById('total-faces');
            if (totalFacesEl) {
                totalFacesEl.textContent = stats.faces?.total ?? 0;
            }
            
            // For regular users, show their pipeline IDs instead of count
            const activePipelinesEl = document.getElementById('active-pipelines');
            if (activePipelinesEl) {
                try {
                    const pipelinesResponse = await fetch('/api/users/me/pipelines', {
                        credentials: 'include' // Include HttpOnly cookies
                    });
                    if (pipelinesResponse.ok) {
                        const pipelines = await pipelinesResponse.json();
                        if (pipelines.length > 0) {
                            // Show pipeline IDs separated by comma
                            activePipelinesEl.textContent = pipelines.join(', ');
                        } else {
                            activePipelinesEl.textContent = '0';
                        }
                    } else {
                        activePipelinesEl.textContent = stats.pipelines?.active ?? 0;
                    }
                } catch (e) {
                    console.error('Error fetching user pipelines:', e);
                    activePipelinesEl.textContent = stats.pipelines?.active ?? 0;
                }
            }
            
            // Regular users don't see queue-size and system-status (hidden by CSS)
            const queueSizeEl = document.getElementById('queue-size');
            const systemStatusEl = document.getElementById('system-status');
            if (queueSizeEl) queueSizeEl.textContent = stats.queue?.queue_size ?? 0;
            if (systemStatusEl) systemStatusEl.textContent = 'Healthy';
        }
    } catch (error) {
        console.error('[HOME] Error loading stats:', error);
        if (currentUser && currentUser.role === 'admin') {
            const systemStatusEl = document.getElementById('system-status');
            if (systemStatusEl) {
                systemStatusEl.textContent = 'Error';
            }
        }
    }
}

async function loadPipelines() {
    try {
        // Get user's accessible pipelines
        const response = await fetch('/api/users/me/pipelines', {
            credentials: 'include' // Include HttpOnly cookies
        });

        if (response.ok) {
            const pipelines = await response.json();
            const pipelineList = document.getElementById('pipeline-list');
            const pipelinesSection = document.getElementById('pipelines-section');
            
            // Always show the section for regular users (not just admins)
            if (currentUser && currentUser.role !== 'admin') {
                pipelinesSection.style.display = 'block';
            }
            
            if (pipelines.length > 0) {
                if (currentUser && currentUser.role === 'admin') {
                    pipelinesSection.style.display = 'block';
                }
                pipelineList.innerHTML = pipelines.map(p => `
                    <div class="pipeline-card" onclick="window.location.href='/dashboard?pipeline=${p}'">
                        <h3>${p}</h3>
                        <p>View detections</p>
                    </div>
                `).join('');
            } else {
                // Show message if no pipelines assigned
                if (currentUser && currentUser.role !== 'admin') {
                    pipelineList.innerHTML = '<p style="color: #666; text-align: center; padding: 2rem;">No pipelines assigned. Please contact your administrator.</p>';
                }
            }
        }
    } catch (error) {
        console.error('Error loading pipelines:', error);
    }
}

