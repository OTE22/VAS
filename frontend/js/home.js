// Home Page JavaScript
let currentUser = null;

// Load user info for UI customization only - BACKEND HANDLES ALL AUTHENTICATION
// Backend route already authenticated user and checks admin role, exception handler handles redirects
document.addEventListener('DOMContentLoaded', async () => {
    // Show the page immediately — the backend authenticated this request before
    // serving it. But do NOT assume administrator: `admin-user` and
    // `admin-verified` are what the stylesheet keys admin-only UI off, and
    // adding them unconditionally showed every user admin controls until /me
    // came back (and left them in place entirely if it failed).
    //
    // This is presentation only. Every admin route enforces authorization
    // server-side, so a stale class exposes controls that do not work — but it
    // still tells a non-admin the controls exist, and it is why a demoted user
    // appeared to keep their old access.
    document.body.classList.remove('admin-verified', 'admin-user');

    try {
        // Fetch user info for UI customization only (not for authentication)
        // Backend already authenticated user and verified admin role before serving this page
        const response = await fetch('/api/auth/me', {
            credentials: 'include' // Include HttpOnly cookies
        });

        if (response.ok) {
            currentUser = await response.json();

            console.log('[HOME] Loading home page content');

            displayUserInfo();

            // Apply admin styling only once the server has said so, and only
            // from the capability list the backend actually enforces.
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

            // Load stats after currentUser is set
            loadStats();
            loadPipelines();
            
            // Show OR hide the assistant link. This previously only ever showed
            // it — with no else, revoking chatbot access never took the link
            // away, so a user who had lost access still saw the entry point.
            const trackingLink = document.getElementById('tracking-link');
            if (trackingLink) {
                const canUseChatbot = permissions.includes('chatbot.use');
                trackingLink.style.display = canUseChatbot ? 'block' : 'none';
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

            // home.html has never contained these elements (verified against
            // git history) — the section was aspirational. Without this guard
            // every home load logged "Cannot read properties of null" once the
            // pipelines response arrived.
            if (!pipelineList || !pipelinesSection) return;

            pipelinesSection.style.display = 'block';

            if (pipelines.length > 0) {
                // DOM-built, not an innerHTML template: pipeline ids are
                // server data and must not be interpolated into markup.
                pipelineList.replaceChildren();
                pipelines.forEach(p => {
                    const card = document.createElement('div');
                    card.className = 'pipeline-card';
                    card.dataset.action = 'goToPipeline';
                    card.dataset.arg = String(p);
                    const h3 = document.createElement('h3');
                    h3.textContent = String(p);
                    const hint = document.createElement('p');
                    hint.textContent = 'View detections';
                    card.appendChild(h3);
                    card.appendChild(hint);
                    pipelineList.appendChild(card);
                });
            } else if (currentUser && currentUser.role !== 'admin') {
                const note = document.createElement('p');
                note.style.cssText = 'color: #666; text-align: center; padding: 2rem;';
                note.textContent = 'No pipelines assigned. Please contact your administrator.';
                pipelineList.replaceChildren(note);
            }
        }
    } catch (error) {
        console.error('Error loading pipelines:', error);
    }
}



// ---------------------------------------------------------------------------
// CSP-safe event registration
//
// These handlers were previously reached through inline onclick/onchange/
// onsubmit attributes, which `script-src 'self'` blocks. Registered rather
// than looked up on window, so only these names are invocable; delegated in
// actions.js, so dynamically rendered elements work without rebinding.
// ---------------------------------------------------------------------------
Actions.register({
    goToPipeline: (el) => {
        const pipeline = el.dataset.arg;
        if (pipeline) {
            window.location.href = '/dashboard?pipeline=' + encodeURIComponent(pipeline);
        }
    },
});
