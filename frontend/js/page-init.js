/**
 * Shared page initialisation.
 *
 * Replaces the small inline <script> blocks that used to sit at the bottom of
 * admin/audit.html, admin/logs.html, admin/pipelines.html, admin/settings.html
 * and admin/users.html. Those violated `script-src 'self'` — an inline block
 * is inline whether it is two lines or two hundred.
 *
 * Behaviour is driven declaratively from attributes on <body>, so a page opts
 * in by adding markup rather than by shipping a script:
 *
 *   <body data-page-class="settings-page" data-page-class-scope="containers">
 *
 *   data-page-class        class applied to <html> and <body>
 *   data-page-class-scope  "containers" additionally applies it to
 *                          .intelligence-main-container and
 *                          .intelligence-content
 *
 * The scope opt-in exists because logs.html and settings.html behaved
 * differently: settings tagged those containers, logs did not, and logs.html
 * does contain them. Applying the class unconditionally would have silently
 * changed which CSS rules matched on the logs page.
 *
 * #current-year is filled in wherever it exists; setting textContent is
 * idempotent, so a page that already sets it elsewhere is unaffected.
 */
(function () {
    'use strict';

    function applyPageClasses() {
        var pageClass = document.body && document.body.dataset
            ? document.body.dataset.pageClass
            : '';
        if (!pageClass) return;

        document.documentElement.classList.add(pageClass);
        document.body.classList.add(pageClass);

        if (document.body.dataset.pageClassScope !== 'containers') return;

        ['.intelligence-main-container', '.intelligence-content'].forEach(function (selector) {
            var element = document.querySelector(selector);
            if (element) element.classList.add(pageClass);
        });
    }

    function setCurrentYear() {
        var element = document.getElementById('current-year');
        if (element) {
            element.textContent = String(new Date().getFullYear());
        }
    }

    function init() {
        applyPageClasses();
        setCurrentYear();
    }

    // The page class must land before first paint where possible; these
    // scripts are loaded with defer, so the DOM is already parsed.
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
