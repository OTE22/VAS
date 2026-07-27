/**
 * Admin Navbar Component
 * =====================
 * Handles navbar functionality and active state management
 */

document.addEventListener('DOMContentLoaded', () => {
    // Set active state based on current page
    const currentPath = window.location.pathname;
    const navLinks = document.querySelectorAll('.military-nav-link[data-page]');
    
    navLinks.forEach(link => {
        const page = link.getAttribute('data-page');
        link.classList.remove('active');
        
        // Determine active page
        if (currentPath === '/home' && page === 'home') {
            link.classList.add('active');
        } else if (currentPath === '/dashboard' && page === 'dashboard') {
            link.classList.add('active');
        } else if (currentPath === '/admin/users' && page === 'users') {
            link.classList.add('active');
        } else if (currentPath === '/admin/pipelines' && page === 'pipelines') {
            link.classList.add('active');
        } else if (currentPath === '/admin/unknown' && page === 'unknown') {
            link.classList.add('active');
        } else if (currentPath === '/admin/tutorial' && page === 'tutorial') {
            link.classList.add('active');
        } else if (currentPath === '/admin/audit' && page === 'audit') {
            link.classList.add('active');
        } else if (currentPath === '/tracking-people' && page === 'tracking') {
            link.classList.add('active');
        } else if (currentPath.startsWith('/docs') && page === 'docs') {
            link.classList.add('active');
        }
    });
    
    // Logout functionality
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

