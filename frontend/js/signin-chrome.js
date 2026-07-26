/**
 * Sign-in page decoration: side images and the offline clock.
 *
 * Extracted from an inline <script> in signin.html so the production
 * Content-Security-Policy can use script-src 'self' without 'unsafe-inline'.
 * Authentication itself lives in signin.js; nothing here touches credentials.
 */
(function () {
    'use strict';

    // Decorative side images: hide on load failure via a listener, never an
    // inline onerror attribute.
    function wireDecorativeImages() {
        document.querySelectorAll('img[data-decorative="true"]').forEach(function (img) {
            img.addEventListener('error', function () { img.style.display = 'none'; });
            if (img.complete && img.naturalWidth === 0) {
                img.style.display = 'none';
            }
        });
    }

    var MONTHS = [
        'January', 'February', 'March', 'April', 'May', 'June',
        'July', 'August', 'September', 'October', 'November', 'December'
    ];

    function updateDateTime() {
        var dateElement = document.getElementById('current-date');
        var timeElement = document.getElementById('current-time');
        if (!dateElement || !timeElement) return;

        var now = new Date();
        var day = String(now.getDate()).padStart(2, '0');
        var month = MONTHS[now.getMonth()];
        var year = now.getFullYear();
        var hours = String(now.getHours()).padStart(2, '0');
        var minutes = String(now.getMinutes()).padStart(2, '0');

        dateElement.textContent = day + ' ' + month + ' ' + year;
        timeElement.textContent = hours + ':' + minutes;
    }

    document.addEventListener('DOMContentLoaded', function () {
        wireDecorativeImages();
        updateDateTime();
        setInterval(updateDateTime, 1000);
    });
})();
