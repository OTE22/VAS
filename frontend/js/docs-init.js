/**
 * Swagger UI / ReDoc bootstrap — CSP-safe.
 *
 * FastAPI's built-in /docs and /redoc pages emit their initialization as an
 * INLINE <script>, and load Swagger/ReDoc themselves from cdn.jsdelivr.net.
 * This deployment sends `script-src 'self'` with no 'unsafe-inline', so both
 * were blocked: the CDN bundle for not being same-origin, and the inline
 * bootstrap for being inline. The result was a blank documentation page even
 * WITH internet — and this system must run fully offline anyway.
 *
 * So the bootstrap lives here, as a same-origin file, and reads its settings
 * from data-* attributes on its own <script> tag instead of interpolated
 * inline JavaScript.
 */
(function () {
    'use strict';

    var script = document.currentScript;
    if (!script) { return; }

    var specUrl = script.getAttribute('data-openapi-url') || '/openapi.json';
    var mode = script.getAttribute('data-mode') || 'swagger';

    if (mode === 'redoc') {
        // ReDoc reads the spec URL from the element's own attribute; the
        // element is already in the document, so nothing else is needed.
        var target = document.getElementById('redoc-container');
        if (target && !target.getAttribute('spec-url')) {
            target.setAttribute('spec-url', specUrl);
        }
        return;
    }

    if (typeof SwaggerUIBundle === 'undefined') {
        var box = document.getElementById('swagger-ui');
        if (box) {
            box.textContent = 'Swagger UI assets failed to load from '
                + '/frontend/vendor/swagger/. Check that the frontend volume is '
                + 'mounted and nginx is serving /frontend/.';
        }
        return;
    }

    SwaggerUIBundle({
        url: specUrl,
        dom_id: '#swagger-ui',
        layout: 'BaseLayout',
        deepLinking: true,
        showExtensions: true,
        showCommonExtensions: true,
        // Same-origin redirect page, served by the app.
        oauth2RedirectUrl: window.location.origin + '/docs/oauth2-redirect',
        presets: [
            SwaggerUIBundle.presets.apis,
            SwaggerUIBundle.SwaggerUIStandalonePreset
        ]
    });
})();
