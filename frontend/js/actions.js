/**
 * CSP-safe declarative event binding.
 *
 * The frontend had 134 inline event attributes (onclick, onsubmit, onchange,
 * ...) across 20 files, both hand-written in HTML and generated from template
 * literals in JavaScript. All of them violate `script-src 'self'` — an inline
 * handler is an inline script, whether a browser parses it from a .html file
 * or from a string built at runtime.
 *
 * Markup declares intent:
 *
 *   <button data-action="clearFilters">Clear</button>
 *   <select data-action-change="changeLimit">
 *   <form   data-action-submit="applyFilters">
 *   <button data-action="viewDetails" data-log-id="42">Details</button>
 *
 * and each page registers the handlers it permits:
 *
 *   Actions.register({
 *       clearFilters,
 *       changeLimit,
 *       applyFilters,
 *       viewDetails: (el) => viewDetails(Number(el.dataset.logId)),
 *   });
 *
 * Why registration rather than looking the name up on `window`: a name-to-
 * global lookup would let any element with a data-action attribute invoke any
 * global function. Registration is an allowlist — an unregistered name does
 * nothing.
 *
 * Why delegation rather than one addEventListener per element: most of these
 * buttons are rendered from JavaScript after load, so per-element binding
 * would have to be re-run on every render and would silently miss rows added
 * later. One delegated listener covers static and dynamic markup identically.
 *
 * Handlers receive (element, event). Functions declared with no parameters
 * simply ignore them, so existing no-argument functions register as-is.
 */
(function () {
    'use strict';

    var handlers = new Map();
    var warned = new Set();

    /** Attribute -> DOM event it delegates. */
    var BINDINGS = {
        'data-action': 'click',
        'data-action-change': 'change',
        'data-action-submit': 'submit',
        'data-action-input': 'input',
        'data-action-keydown': 'keydown',
        'data-action-dblclick': 'dblclick'
    };

    function register(map) {
        if (!map) return;
        Object.keys(map).forEach(function (name) {
            var fn = map[name];
            if (typeof fn === 'function') {
                handlers.set(name, fn);
            } else {
                console.warn('[actions] Ignoring non-function handler:', name);
            }
        });
    }

    function dispatch(event, attribute) {
        var target = event.target;
        if (!target || typeof target.closest !== 'function') return;

        var element = target.closest('[' + attribute + ']');
        if (!element || !document.contains(element)) return;

        var name = element.getAttribute(attribute);
        if (!name) return;

        var handler = handlers.get(name);
        if (!handler) {
            // Warn once per name. A typo or a missing register() call should
            // be visible, but some pages (admin-background-tasks.js) own a
            // data-action element with their own delegated listener, and a
            // warning on every click there would be pure noise.
            if (!warned.has(name)) {
                warned.add(name);
                console.debug('[actions] No handler registered here for "' + name +
                    '" (another listener may own it)');
            }
            return;
        }

        // A form's default submit would reload the page; every inline
        // onsubmit this replaced began with event.preventDefault().
        if (attribute === 'data-action-submit') {
            event.preventDefault();
        }

        try {
            handler(element, event);
        } catch (error) {
            console.error('[actions] Handler "' + name + '" failed:', error);
        }
    }

    Object.keys(BINDINGS).forEach(function (attribute) {
        document.addEventListener(BINDINGS[attribute], function (event) {
            dispatch(event, attribute);
        });
    });

    // --- image fallbacks -------------------------------------------------
    //
    // `error` does not bubble, so the delegation above cannot see it. It does
    // propagate through the CAPTURE phase, hence the `true` below — this is
    // the one listener that genuinely needs it.
    //
    // Replaces onerror="this.src='...'" and
    // onerror="this.parentElement.innerHTML='...'":
    //
    //   <img data-fallback-src="/frontend/img/placeholder.png">
    //   <img data-fallback-icon="fas fa-user">
    //
    // A failed fallback must not re-trigger this handler, so the marker
    // attribute is removed before the new src is assigned.
    document.addEventListener('error', function (event) {
        var element = event.target;
        if (!element || element.tagName !== 'IMG') return;

        var fallbackSrc = element.getAttribute('data-fallback-src');
        if (fallbackSrc) {
            element.removeAttribute('data-fallback-src');
            element.src = fallbackSrc;
            return;
        }

        var icon = element.getAttribute('data-fallback-icon');
        if (icon && element.parentElement) {
            // Replaces onerror="this.parentElement.innerHTML='<div class=...>'".
            // Built with DOM APIs rather than innerHTML, and the class names
            // come from the attribute authored in our own template, never
            // from API data.
            var wrapper = document.createElement('div');
            wrapper.className = element.getAttribute('data-fallback-class') || 'no-image';
            var glyph = document.createElement('i');
            glyph.className = icon;
            wrapper.appendChild(glyph);
            element.parentElement.replaceChild(wrapper, element);
            return;
        }

        if (element.hasAttribute('data-fallback-hide')) {
            element.style.display = 'none';
        }
    }, true);

    /**
     * Read a positive integer from a data attribute.
     *
     * Ids arriving from rendered HTML are strings and may be absent or
     * malformed; callers that pass them to an API should not send NaN.
     * Returns null when the value is not a positive integer.
     */
    function intFrom(element, datasetKey) {
        var raw = element && element.dataset ? element.dataset[datasetKey] : null;
        var value = Number(raw);
        if (!Number.isInteger(value) || value <= 0) {
            console.error('[actions] Invalid ' + datasetKey + ':', raw);
            return null;
        }
        return value;
    }

    window.Actions = { register: register, intFrom: intFrom };
})();
