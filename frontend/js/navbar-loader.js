/**
 * Navbar Loader
 * Dynamically loads and injects the navbar component into pages
 * 
 * NOTE: This loader will NOT run on the signin page to avoid interfering with login redirects
 */

(function () {
    'use strict';

    // Don't run on signin page
    if (window.location.pathname === '/signin' || window.location.pathname.startsWith('/signin')) {
        return;
    }

    // ---- Shared auth fetch dedup -------------------------------------------
    // Every page script (navbar, dashboard, unknown page...) needs /api/auth/me
    // and /api/auth/me/privileges. These helpers make the WHOLE page share ONE
    // in-flight request per endpoint instead of firing duplicates on every load.
    window.getAuthMe = window.getAuthMe || function () {
        if (!window.__authMePromise) {
            window.__authMePromise = fetch('/api/auth/me', { credentials: 'include' })
                .then(r => (r.ok ? r.json() : null))
                .catch(() => null);
        }
        return window.__authMePromise;
    };
    window.getAuthPrivileges = window.getAuthPrivileges || function () {
        if (!window.__authPrivilegesPromise) {
            window.__authPrivilegesPromise = fetch('/api/auth/me/privileges', { credentials: 'include' })
                .then(r => (r.ok ? r.json() : null))
                .catch(() => null);
        }
        return window.__authPrivilegesPromise;
    };

    // Get the current page path to determine active link
    function getCurrentPage() {
        const path = window.location.pathname;
        if (path === '/home' || path === '/') return 'home';
        if (path === '/dashboard') return 'dashboard';
        if (path.startsWith('/admin/users')) return 'users';
        if (path.startsWith('/admin/pipelines')) return 'pipelines';
        if (path.startsWith('/admin/unknown')) return 'unknown';
        if (path.startsWith('/admin/search-history')) return 'search-history';
        if (path.startsWith('/admin/search')) return 'search';
        if (path.startsWith('/admin/intelligence')) return 'intelligence';
        if (path.startsWith('/admin/security-intelligence')) return 'security-intelligence';
        if (path.startsWith('/admin/ml-model')) return 'ml-model';
        if (path.startsWith('/admin/background-tasks')) return 'background-tasks';
        if (path.startsWith('/admin/live-alerts')) return 'live-alerts';
        if (path.startsWith('/admin/ml-model')) return 'ml-model';
        if (path.startsWith('/admin/tutorial')) return 'tutorial';
        if (path.startsWith('/admin/settings')) return 'settings';
        if (path.startsWith('/admin/audit')) return 'audit';
        if (path.startsWith('/admin/logs')) return 'logs';
        if (path.startsWith('/tracking-people')) return 'tracking';
        if (path.startsWith('/docs')) return 'docs';
        return null;
    }

    // Load navbar from component file
    async function loadNavbar() {
        const placeholder = document.getElementById('navbar-placeholder');
        if (!placeholder) {
            // Placeholder removed (likely due to redirect), exit gracefully
            return;
        }

        try {
            // Create abort controller for timeout
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 5000);

            // Determine which navbar to load based on path
            // Load admin-navbar.html for admin pages, home, dashboard, and tracking-people
            const navbarPath = (window.location.pathname.startsWith('/admin') ||
                window.location.pathname === '/home' ||
                window.location.pathname === '/' ||
                window.location.pathname === '/dashboard' ||
                window.location.pathname === '/tracking-people')
                ? '/frontend/components/admin-navbar.html'
                : '/frontend/components/navbar.html';

            // Session-cache the navbar HTML so navigation between pages renders the
            // bar instantly instead of waiting on a network fetch each time.
            const htmlCacheKey = 'navbar_html:' + navbarPath;
            let navbarHtml = sessionStorage.getItem(htmlCacheKey);

            if (navbarHtml) {
                clearTimeout(timeoutId);
                // Refresh the cache in the background for the next navigation
                fetch(navbarPath).then(r => r.ok ? r.text() : null).then(html => {
                    if (html) sessionStorage.setItem(htmlCacheKey, html);
                }).catch(() => { });
            } else {
                const response = await fetch(navbarPath, {
                    signal: controller.signal
                });

                clearTimeout(timeoutId);

                if (!response.ok) {
                    throw new Error(`Failed to load navbar: ${response.status}`);
                }

                navbarHtml = await response.text();
                sessionStorage.setItem(htmlCacheKey, navbarHtml);
            }

            // Double-check placeholder still exists before replacing (in case of redirect)
            const currentPlaceholder = document.getElementById('navbar-placeholder');
            if (!currentPlaceholder) {
                // Page was redirected, don't inject navbar
                return;
            }

            currentPlaceholder.outerHTML = navbarHtml;

            // Set active link based on current page
            const currentPage = getCurrentPage();
            if (currentPage) {
                // Check regular nav links
                const activeLink = document.querySelector(`.military-nav-link[data-page="${currentPage}"]:not(.dropdown-toggle)`);
                if (activeLink) {
                    activeLink.classList.add('active');
                    activeLink.setAttribute('aria-current', 'page');
                }

                // Check dropdown menu items
                const activeDropdownItem = document.querySelector(`.dropdown-item[data-page="${currentPage}"]`);
                if (activeDropdownItem) {
                    activeDropdownItem.classList.add('active');
                    activeDropdownItem.setAttribute('aria-current', 'page');
                    // Also highlight the parent dropdown toggle
                    const parentDropdown = activeDropdownItem.closest('.nav-dropdown');
                    if (parentDropdown) {
                        const toggle = parentDropdown.querySelector('.dropdown-toggle');
                        if (toggle) {
                            toggle.classList.add('active');
                        }
                    }
                }

                // Special case: highlight "ADD PERSON" when on dashboard
                if (currentPage === 'dashboard') {
                    const addPersonItem = document.querySelector(`.dropdown-item[data-page="add-person"]`);
                    if (addPersonItem) {
                        addPersonItem.classList.add('active');
                    }
                }
            }

            // Attach logout handler
            const logoutBtn = document.getElementById('logout-btn');
            if (logoutBtn) {
                logoutBtn.addEventListener('click', handleLogout);
            }

            // Attach refresh handler
            const refreshBtn = document.getElementById('navbar-refresh-btn');
            if (refreshBtn) {
                refreshBtn.addEventListener('click', handleRefresh);
            }

            // Run all data loads in PARALLEL - none depends on another, and awaiting
            // them sequentially is what made the navbar/status appear slowly.
            initializeConnectionStatus(); // starts the /health check immediately
            loadUserInfo();
            initializeNavbar();
            await customizeNavbarForUser();

        } catch (error) {
            // Only log error if placeholder still exists (page wasn't redirected)
            const placeholder = document.getElementById('navbar-placeholder');
            if (placeholder && error.name !== 'AbortError') {
                console.error('[Navbar Loader] Error loading navbar:', error);
                placeholder.innerHTML = `
                    <nav class="military-navbar" style="background: rgba(0,0,0,0.8); padding: 1rem;">
                        <div style="color: #ff4444;">
                            <i class="fas fa-exclamation-triangle"></i>
                            <span>Navbar failed to load</span>
                        </div>
                    </nav>
                `;
            }
        }
    }

    // Handle logout
    async function handleLogout() {
        // Clear session caches so the next user doesn't see stale navbar data
        sessionStorage.removeItem('navbar_privileges');
        sessionStorage.removeItem('navbar_username');
        try {
            const response = await fetch('/api/auth/logout', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json'
                }
            });

            if (response.ok || response.status === 401) {
                // Clear any local storage tokens
                localStorage.removeItem('access_token');
                // Redirect to login
                window.location.href = '/signin';
            } else {
                console.error('Logout failed:', response.status);
                // Still redirect to login
                window.location.href = '/signin';
            }
        } catch (error) {
            console.error('Logout error:', error);
            // Still redirect to login
            window.location.href = '/signin';
        }
    }

    // Handle refresh - reloads the current page
    function handleRefresh(e) {
        if (e) {
            e.preventDefault();
            e.stopPropagation();
        }

        const refreshBtn = document.getElementById('navbar-refresh-btn');
        const refreshIcon = document.getElementById('navbar-refresh-icon');

        if (refreshBtn && refreshIcon) {
            // Disable button and show spinning animation
            refreshBtn.disabled = true;
            refreshIcon.classList.add('fa-spin');

            // Close dropdown if it's open
            const systemDropdown = document.querySelector('.nav-dropdown[data-dropdown="system"]');
            if (systemDropdown) {
                systemDropdown.setAttribute('data-open', 'false');
            }

            // Reload the page after a short delay
            setTimeout(() => {
                window.location.reload();
            }, 300);
        } else {
            // Fallback: just reload
            window.location.reload();
        }
    }

    // Load user info and display "Logged as (username)"
    async function loadUserInfo() {
        try {
            const userInfoDiv = document.getElementById('navbar-user-info');
            const usernameSpan = document.getElementById('navbar-username');

            if (!userInfoDiv || !usernameSpan) {
                return;
            }

            // Show cached username instantly; verify with the backend below
            const cachedName = sessionStorage.getItem('navbar_username');
            if (cachedName) {
                usernameSpan.textContent = `Logged as ${cachedName}`;
                userInfoDiv.style.display = 'flex';
            }

            const user = await window.getAuthMe();

            if (user) {
                sessionStorage.setItem('navbar_username', user.username);
                usernameSpan.textContent = `Logged as ${user.username}`;
                userInfoDiv.style.display = 'flex';
            } else {
                // If we can't get user info, hide the element
                userInfoDiv.style.display = 'none';
            }
        } catch (error) {
            console.warn('[Navbar] Could not load user info:', error);
            const userInfoDiv = document.getElementById('navbar-user-info');
            if (userInfoDiv) {
                userInfoDiv.style.display = 'none';
            }
        }
    }

    // Initialize connection status indicator
    function initializeConnectionStatus() {
        const statusDiv = document.getElementById('navbar-connection-status');
        const statusText = document.getElementById('navbar-status-text');

        if (!statusDiv || !statusText) {
            return;
        }

        // Check connection status against the zero-I/O liveness endpoint.
        // Skips when the tab is hidden or a check is already in flight, and
        // aborts requests that hang (so a slow backend can't stack requests).
        let checkInFlight = false;
        let abortCtrl = null;

        function checkConnection(force = false) {
            if (!force && (document.hidden || checkInFlight)) return;
            checkInFlight = true;
            abortCtrl = new AbortController();
            const abortTimer = setTimeout(() => abortCtrl.abort(), 8000);

            fetch('/health/live', {
                method: 'GET',
                credentials: 'include',
                signal: abortCtrl.signal
            })
                .then(response => {
                    if (response.ok) {
                        statusDiv.classList.remove('disconnected');
                        statusDiv.classList.add('connected');
                        statusText.textContent = 'Connected';
                    } else {
                        statusDiv.classList.remove('connected');
                        statusDiv.classList.add('disconnected');
                        statusText.textContent = 'Disconnected';
                    }
                })
                .catch(() => {
                    statusDiv.classList.remove('connected');
                    statusDiv.classList.add('disconnected');
                    statusText.textContent = 'Disconnected';
                })
                .finally(() => {
                    clearTimeout(abortTimer);
                    checkInFlight = false;
                });
        }

        // Initial check
        checkConnection(true);

        // Check every 30 seconds (paused while the tab is hidden)
        setInterval(() => checkConnection(), 30000);

        // Re-check immediately when the tab becomes visible again
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) checkConnection(true);
        });

        // Make it clickable to manually check
        statusDiv.style.cursor = 'pointer';
        statusDiv.addEventListener('click', () => {
            statusText.textContent = 'Checking...';
            checkConnection(true);
        });
    }

    // Fetch privileges from the backend (source of truth for which links to show)
    async function fetchPrivileges() {
        const privileges = await window.getAuthPrivileges();
        if (!privileges) {
            console.error('[Navbar] Failed to get privileges');
        }
        return privileges;
    }

    // Customize navbar based on backend-provided navbar links.
    // Uses a session cache so links appear instantly on navigation; a background
    // fetch keeps the cache in sync with the backend (still the source of truth).
    async function customizeNavbarForUser() {
        const cacheKey = 'navbar_privileges';
        try {
            const cached = sessionStorage.getItem(cacheKey);
            if (cached) {
                try {
                    applyNavbarPrivileges(JSON.parse(cached));
                } catch (e) {
                    sessionStorage.removeItem(cacheKey);
                }
                // Refresh from backend in the background and re-apply if changed
                fetchPrivileges().then(fresh => {
                    if (fresh) {
                        const serialized = JSON.stringify(fresh);
                        if (serialized !== cached) {
                            sessionStorage.setItem(cacheKey, serialized);
                            applyNavbarPrivileges(fresh);
                        }
                    }
                }).catch(() => { });
                return;
            }

            const privileges = await fetchPrivileges();
            if (!privileges) return;
            sessionStorage.setItem(cacheKey, JSON.stringify(privileges));
            applyNavbarPrivileges(privileges);
        } catch (error) {
            console.error('[Navbar] Error customizing navbar:', error);
            // Don't block page load if navbar customization fails
        }
    }

    // Apply backend-provided navbar links to the DOM
    function applyNavbarPrivileges(privileges) {
        try {
            // BACKEND PROVIDES navbar_links - Frontend only displays what backend allows
            if (!privileges.navbar_links || !Array.isArray(privileges.navbar_links)) {
                console.warn('[Navbar] No navbar_links provided by backend, using default behavior');
                return;
            }

            console.log(`[Navbar] Backend provided ${privileges.navbar_links.length} navbar links`);

            // Get all navbar links and dropdown items in the DOM
            const allNavLinks = document.querySelectorAll('.military-nav-link:not(.dropdown-toggle)');
            const allDropdownItems = document.querySelectorAll('.dropdown-item');

            // Hide all links first, then show only what backend allows
            allNavLinks.forEach(link => {
                link.style.display = 'none';
            });
            allDropdownItems.forEach(item => {
                item.style.display = 'none';
            });

            // Show only the links that backend says are visible
            privileges.navbar_links.forEach(backendLink => {
                if (backendLink.visible) {
                    // Try to find in regular nav links first
                    let domLink = document.querySelector(`.military-nav-link[data-page="${backendLink.page}"]:not(.dropdown-toggle)`);

                    // If not found, try dropdown items
                    if (!domLink) {
                        domLink = document.querySelector(`.dropdown-item[data-page="${backendLink.page}"]`);
                    }

                    if (domLink) {
                        // Remove admin-only class and show the link
                        domLink.classList.remove('admin-only');
                        domLink.style.display = 'flex';
                        // Update label if provided by backend
                        if (backendLink.label) {
                            const span = domLink.querySelector('span');
                            if (span) span.textContent = backendLink.label;
                        }
                        // Update icon if provided by backend
                        if (backendLink.icon) {
                            const icon = domLink.querySelector('i:not(.dropdown-arrow)');
                            if (icon) icon.className = backendLink.icon;
                        }
                        // Update title/tooltip if provided by backend
                        if (backendLink.title) {
                            domLink.setAttribute('title', backendLink.title);
                        }
                        console.log(`[Navbar] Showing link: ${backendLink.label} (${backendLink.page})`);
                    } else {
                        console.warn(`[Navbar] Link not found in DOM: ${backendLink.page}`);
                    }
                }
            });

            // Show admin-only dropdowns if any of their items are visible
            document.querySelectorAll('.nav-dropdown.admin-only').forEach(dropdown => {
                const hasVisibleItems = Array.from(dropdown.querySelectorAll('.dropdown-item')).some(item =>
                    item.style.display !== 'none' && !item.classList.contains('admin-only')
                );
                if (hasVisibleItems) {
                    dropdown.classList.remove('admin-only');
                    dropdown.style.display = 'inline-block';
                }
            });

            // Show SYSTEM dropdown if any of its items are visible (and not admin-only)
            const systemDropdown = document.querySelector('.nav-dropdown[data-dropdown="system"]');
            if (systemDropdown) {
                const hasVisibleItems = Array.from(systemDropdown.querySelectorAll('.dropdown-item')).some(item =>
                    item.style.display !== 'none' && !item.classList.contains('admin-only')
                );
                if (hasVisibleItems) {
                    systemDropdown.classList.remove('admin-only');
                    systemDropdown.style.display = 'inline-block';
                } else {
                    systemDropdown.style.display = 'none';
                }
            }

            // Show MANAGEMENT dropdown if any of its items are visible (and not admin-only)
            const managementDropdown = document.querySelector('.nav-dropdown[data-dropdown="management"]');
            if (managementDropdown) {
                const hasVisibleItems = Array.from(managementDropdown.querySelectorAll('.dropdown-item')).some(item =>
                    item.style.display !== 'none' && !item.classList.contains('admin-only')
                );
                if (hasVisibleItems) {
                    managementDropdown.classList.remove('admin-only');
                    managementDropdown.style.display = 'inline-block';
                } else {
                    managementDropdown.style.display = 'none';
                }
            }

            // Show SEARCH dropdown if any of its items are visible (and not admin-only)
            const searchDropdown = document.querySelector('.nav-dropdown[data-dropdown="search"]');
            if (searchDropdown) {
                const hasVisibleItems = Array.from(searchDropdown.querySelectorAll('.dropdown-item')).some(item =>
                    item.style.display !== 'none' && !item.classList.contains('admin-only')
                );
                if (hasVisibleItems) {
                    searchDropdown.classList.remove('admin-only');
                    searchDropdown.style.display = 'inline-block';
                } else {
                    searchDropdown.style.display = 'none';
                }
            }

            // Add tooltip to UNKNOWN FACES link if user has pipeline access
            if (privileges.can_access_unknown_faces && privileges.pipelines.length > 0) {
                const unknownFacesLink = document.querySelector('.military-nav-link[data-page="unknown"]');
                if (unknownFacesLink) {
                    const pipelineList = privileges.pipelines.slice(0, 3).join(', ') +
                        (privileges.pipelines.length > 3 ? ` +${privileges.pipelines.length - 3} more` : '');
                    unknownFacesLink.setAttribute('title', `Access to pipelines: ${pipelineList}`);
                }
            }

            console.log(`[Navbar] Navbar customization complete - showing ${privileges.navbar_links.filter(l => l.visible).length} links`);
        } catch (error) {
            console.error('[Navbar] Error customizing navbar:', error);
            // Don't block page load if navbar customization fails
        }
    }

    // Initialize navbar functionality
    function initializeNavbar() {
        // Hover styling is handled purely in CSS (flat professional style - no
        // translateY jumps). JS only needs to wire up the dropdown menus.
        initializeDropdowns();
    }

    // Initialize dropdown menus
    function initializeDropdowns() {
        const dropdowns = document.querySelectorAll('.nav-dropdown');

        dropdowns.forEach(dropdown => {
            const toggle = dropdown.querySelector('.dropdown-toggle');
            const menu = dropdown.querySelector('.dropdown-menu');

            if (!toggle || !menu) return;

            // Toggle dropdown on click
            toggle.addEventListener('click', function (e) {
                e.preventDefault();
                e.stopPropagation();

                const isOpen = dropdown.getAttribute('data-open') === 'true';

                // Close all other dropdowns
                document.querySelectorAll('.nav-dropdown').forEach(d => {
                    if (d !== dropdown) {
                        d.setAttribute('data-open', 'false');
                    }
                });

                // Toggle current dropdown
                dropdown.setAttribute('data-open', isOpen ? 'false' : 'true');
            });

            // Close dropdown when clicking on a menu item
            const menuItems = menu.querySelectorAll('.dropdown-item');
            menuItems.forEach(item => {
                item.addEventListener('click', function () {
                    dropdown.setAttribute('data-open', 'false');
                });
            });
        });

        // Close dropdowns when clicking outside
        document.addEventListener('click', function (e) {
            if (!e.target.closest('.nav-dropdown')) {
                document.querySelectorAll('.nav-dropdown').forEach(d => {
                    d.setAttribute('data-open', 'false');
                });
            }
        });

        // Close dropdowns on escape key
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                document.querySelectorAll('.nav-dropdown').forEach(d => {
                    d.setAttribute('data-open', 'false');
                });
            }
        });
    }

    // Load navbar when DOM is ready
    // Use a small delay to ensure redirects complete first
    function initNavbarLoader() {
        // Check if we're on a page that should have a navbar
        const placeholder = document.getElementById('navbar-placeholder');
        if (!placeholder) {
            // No placeholder found, this page doesn't use navbar component
            return;
        }

        // Small delay to ensure any redirects complete first
        setTimeout(() => {
            // Double-check placeholder still exists (in case of redirect)
            if (document.getElementById('navbar-placeholder')) {
                loadNavbar();
            }
        }, 50);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initNavbarLoader);
    } else {
        initNavbarLoader();
    }

})();

