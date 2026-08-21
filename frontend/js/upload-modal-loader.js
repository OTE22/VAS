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
            // Bounded retry. This fetch fails intermittently while the page is
            // still settling, and it used to give up on the first error — the
            // component then never existed and ADD PERSON did nothing, with a
            // console message as the only clue. It matters more now: the
            // `display = 'flex'` fallback in upload-modal.js is gone, so a
            // missing component is a missing dialog rather than a degraded
            // one. Three tries, short backoff, then give up loudly as before.
            let response = null;
            for (let attempt = 0; attempt < 3; attempt++) {
                try {
                    response = await fetch('/frontend/components/upload-modal.html');
                    if (response.ok) { break; }
                } catch (networkError) {
                    if (attempt === 2) { throw networkError; }
                }
                await new Promise((resolve) => setTimeout(resolve, 150 * (attempt + 1)));
            }
            if (!response || !response.ok) {
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

                // The component file ships TWO dialogs: #uploadModal and
                // #face-detection-alert-modal. Only the first was injected, so
                // on every page served by this loader the face-detection alert
                // silently degraded to a toast (upload-modal.js falls back to
                // showUploadAlert when the element is missing). Move the rest
                // of the component's top-level dialogs across too, skipping
                // any id the page already provides itself.
                Array.prototype.slice.call(tempDiv.children).forEach(function (extra) {
                    if (extra.id && document.getElementById(extra.id)) { return; }
                    document.body.appendChild(extra);
                });
                
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

