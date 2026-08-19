/**
 * Upload Modal Loader
 * ===================
 * Dynamically loads the upload modal component into admin pages.
 */

(function() {
    'use strict';
    
    // Don't run on signin page
    if (window.location.pathname === '/signin' || window.location.pathname.startsWith('/signin')) {
        return;
    }

    // Load upload modal component
    async function loadUploadModal() {
        // Idempotent: navbar-loader.js also asks for this component on pages
        // that carry no script tag for it, so both routes can fire on a page
        // that has one. A second #uploadModal in the DOM would give every
        // getElementById a stale first match.
        if (document.getElementById('uploadModal')) {
            return;
        }
        try {
            const response = await fetch('/frontend/components/upload-modal.html');
            if (!response.ok) {
                console.warn('[Upload Modal] Failed to load upload modal component');
                return;
            }
            
            const modalHtml = await response.text();
            
            // Inject into body
            const tempDiv = document.createElement('div');
            tempDiv.innerHTML = modalHtml;
            const modal = tempDiv.querySelector('#uploadModal');
            
            if (modal) {
                document.body.appendChild(modal);
                
                // Load CSS if not already loaded
                if (!document.querySelector('link[href="/frontend/css/upload-modal.css"]')) {
                    const link = document.createElement('link');
                    link.rel = 'stylesheet';
                    link.href = '/frontend/css/upload-modal.css';
                    document.head.appendChild(link);
                }
                
                // Load JavaScript if not already loaded
                if (!document.querySelector('script[src="/frontend/js/upload-modal.js"]')) {
                    const script = document.createElement('script');
                    script.src = '/frontend/js/upload-modal.js';
                    document.body.appendChild(script);
                }
            }
        } catch (error) {
            console.error('[Upload Modal] Error loading upload modal:', error);
        }
    }

    // Load when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', loadUploadModal);
    } else {
        loadUploadModal();
    }
})();

