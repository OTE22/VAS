/**
 * ModalStack — the single authority for modal layering on pages that load it.
 *
 * WHY THIS EXISTS. Every modal on the Unknown Faces page is a .security-modal
 * at the same z-index, so which one painted on top was decided by DOM source
 * order — it happened to work for detail -> merge -> preview because those
 * elements appear in that order in the HTML, and silently broke for any other
 * pairing. The face-detection alert "fixed" itself with z-index 99999 in three
 * different places. This module replaces all of that with one rule:
 *
 *     the modal opened last is on top, and only it is interactive.
 *
 * Levels are assigned at open time from the CSS tokens --z-modal-base and
 * --z-modal-step (admin.css :root), so no element ever carries a hand-picked
 * z-index again.
 *
 * OWNERSHIP. This module owns, exactly once per document:
 *   - the Escape key (closes only the TOP modal),
 *   - Tab containment (focus cannot leave the top modal),
 *   - backdrop clicks (only for modals that opted in via backdropClose).
 * Pages must not register their own modal Escape/backdrop handlers when this
 * script is present.
 *
 * CSP: classic script, no inline handlers, no dynamic code. Loaded after
 * actions.js and before page scripts (see test_frontend_script_order.py).
 */
(function () {
    'use strict';

    if (window.ModalStack && window.ModalStack.__initialized) {
        return; // second load keeps the first, live stack
    }

    /** @type {{el: Element, backdropClose: boolean, onClose: Function|null, trigger: Element|null}[]} */
    var stack = [];

    var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
        'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    function cssToken(name, fallback) {
        var raw = getComputedStyle(document.documentElement).getPropertyValue(name);
        var parsed = parseInt(raw, 10);
        return isNaN(parsed) ? fallback : parsed;
    }

    function zIndexForLevel(level) {
        return cssToken('--z-modal-base', 10000) + level * cssToken('--z-modal-step', 10);
    }

    function entryFor(el) {
        for (var i = 0; i < stack.length; i++) {
            if (stack[i].el === el) { return i; }
        }
        return -1;
    }

    function topEntry() {
        return stack.length ? stack[stack.length - 1] : null;
    }

    function focusables(el) {
        var nodes = el.querySelectorAll(FOCUSABLE);
        var out = [];
        for (var i = 0; i < nodes.length; i++) {
            // getClientRects() is empty for display:none subtrees — skips
            // hidden form branches (e.g. the merge modal's inactive form).
            if (nodes[i].getClientRects().length) { out.push(nodes[i]); }
        }
        return out;
    }

    /** Move focus into el: [autofocus] first, else first focusable, else the
     *  container itself (given tabindex="-1" so it can hold focus). */
    function focusInto(el) {
        var target = el.querySelector('[autofocus]');
        if (!target || !target.getClientRects().length) {
            var candidates = focusables(el);
            target = candidates.length ? candidates[0] : null;
        }
        if (!target) {
            if (!el.hasAttribute('tabindex')) { el.setAttribute('tabindex', '-1'); }
            target = el;
        }
        try { target.focus(); } catch (ignored) { /* detached mid-flight */ }
    }

    /** Non-interactive but visible: the "there is a modal above you" state.
     *  Never applied while a descendant still owns focus — open() moves focus
     *  into the new modal FIRST, because aria-hidden on a focused subtree is
     *  both an a11y violation and a screen-reader trap. */
    function suppress(el) {
        el.classList.add('is-stack-under');
        el.setAttribute('aria-hidden', 'true');
        if ('inert' in el) { el.inert = true; }
    }

    function unsuppress(el) {
        el.classList.remove('is-stack-under');
        el.removeAttribute('aria-hidden');
        if ('inert' in el) { el.inert = false; }
    }

    /** Everything that makes an entry's element "closed", EXCEPT focus
     *  handling and body unlock — those are per-operation, not per-modal,
     *  so cascades restore focus exactly once at the end. */
    function retire(entry, reason) {
        var el = entry.el;
        el.style.display = 'none';
        el.style.zIndex = '';
        el.classList.remove('is-stack-nested');
        el.classList.remove('is-stack-under');
        el.removeAttribute('aria-hidden');
        if ('inert' in el) { el.inert = false; }
        // The callback runs AFTER the entry has left the stack, so a legacy
        // close function that calls ModalStack.close() again hits the
        // idempotent no-op path instead of recursing.
        if (typeof entry.onClose === 'function') {
            try { entry.onClose(el, reason); } catch (err) {
                console.error('[ModalStack] onClose failed for #' + (el.id || '?'), err);
            }
        }
    }

    /** After any close operation: re-expose the new top (or unlock the page)
     *  and restore focus exactly once. */
    function settle(finalTrigger) {
        var top = topEntry();
        if (top) {
            unsuppress(top.el);
            if (stack.length === 1) { top.el.classList.remove('is-stack-nested'); }
        } else {
            document.body.classList.remove('modal-stack-locked');
        }

        // A trigger is only worth restoring if it still exists, is visible,
        // and lives in what is now the interactive layer (the new top modal,
        // or the page when the stack is empty).
        var trigger = finalTrigger;
        var valid = trigger && trigger.isConnected && trigger.getClientRects().length &&
            !trigger.disabled &&
            (top ? top.el.contains(trigger) : !trigger.closest('.is-stack-under'));
        if (valid) {
            try { trigger.focus(); } catch (ignored) { valid = false; }
        }
        if (!valid && top) { focusInto(top.el); }
    }

    function open(el, opts) {
        if (!el) { return; }
        opts = opts || {};
        var existing = entryFor(el);
        if (existing === stack.length - 1 && existing !== -1) {
            return; // already on top — double-open is a no-op
        }
        if (existing !== -1) {
            console.warn('[ModalStack] #' + (el.id || '?') + ' is already open mid-stack; ignoring open()');
            return;
        }

        var entry = {
            el: el,
            backdropClose: !!opts.backdropClose,
            onClose: opts.onClose || null,
            trigger: document.activeElement && document.activeElement !== document.body
                ? document.activeElement : null
        };
        var previous = topEntry();

        // ORDER MATTERS (accessibility): show + focus the new modal BEFORE
        // hiding the previous one from assistive tech — aria-hidden must
        // never land on an element whose descendant still owns focus.
        stack.push(entry);
        el.style.zIndex = String(zIndexForLevel(stack.length - 1));
        el.style.display = 'flex';
        if (!el.hasAttribute('role')) { el.setAttribute('role', 'dialog'); }
        if (!el.hasAttribute('aria-modal')) { el.setAttribute('aria-modal', 'true'); }
        if (stack.length > 1) { el.classList.add('is-stack-nested'); }
        focusInto(el);

        if (previous) { suppress(previous.el); }
        if (stack.length === 1) { document.body.classList.add('modal-stack-locked'); }
    }

    /** Close el. If other modals sit above it, they cascade-close first,
     *  strictly top-down (LIFO), each cleanup and callback running exactly
     *  once, with no intermediate focus churn or re-activation. Idempotent:
     *  closing a modal that is not open is a no-op. */
    function close(el, reason) {
        if (!el) { return; }
        var index = entryFor(el);
        if (index === -1) { return; }
        reason = reason || 'api';

        // The focus target for the whole operation is the trigger recorded
        // by the LOWEST modal being closed — that element lives in whatever
        // becomes the top after the cascade.
        var finalTrigger = stack[index].trigger;

        while (stack.length > index) {
            var entry = stack.pop();
            retire(entry, stack.length > index ? 'cascade' : reason);
        }
        settle(finalTrigger);
    }

    function closeTop(reason) {
        var top = topEntry();
        if (top) { close(top.el, reason || 'api'); }
    }

    // ---- the ONE keyboard handler -----------------------------------------
    document.addEventListener('keydown', function (event) {
        if (!stack.length) { return; }

        if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            closeTop('escape');
            return;
        }

        if (event.key === 'Tab') {
            var top = topEntry();
            var items = focusables(top.el);
            if (!items.length) {
                // Nothing focusable: keep focus parked on the container.
                event.preventDefault();
                focusInto(top.el);
                return;
            }
            var first = items[0];
            var last = items[items.length - 1];
            var current = document.activeElement;
            if (!top.el.contains(current)) {
                // Focus escaped (or never entered) — pull it back in.
                event.preventDefault();
                first.focus();
            } else if (event.shiftKey && current === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && current === last) {
                event.preventDefault();
                first.focus();
            }
        }
    }, true);

    // ---- the ONE backdrop handler -----------------------------------------
    // The .security-modal container itself is the backdrop; a click whose
    // target IS the container means the user clicked outside .modal-content.
    // Only the top modal reacts, and only if it opted in.
    document.addEventListener('click', function (event) {
        var top = topEntry();
        if (top && top.backdropClose && event.target === top.el) {
            close(top.el, 'backdrop');
        }
    });

    window.ModalStack = {
        open: open,
        close: close,
        closeTop: closeTop,
        top: function () { var t = topEntry(); return t ? t.el : null; },
        getTopModal: function () { var t = topEntry(); return t ? t.el : null; },
        depth: function () { return stack.length; },
        isOpen: function (el) { return entryFor(el) !== -1; },
        __initialized: true
    };

    // ------------------------------------------------------------------
    // AppConfirm — the application's replacement for native confirm().
    //
    // A native confirm() blocks the whole tab, ignores the modal stack, and
    // cannot be styled or layered; this one is an ordinary stacked modal, so
    // "Detail -> Promote -> candidate -> confirmation" nests correctly and
    // Escape/backdrop follow the same rules as everything else.
    //
    // Built entirely with createElement/textContent — message text is never
    // interpolated into markup, so caller-supplied strings cannot inject.
    // ------------------------------------------------------------------

    function buildConfirmDialog(options) {
        var overlay = document.createElement('div');
        overlay.className = 'security-modal';
        overlay.style.display = 'none';

        var content = document.createElement('div');
        content.className = 'modal-content';
        content.style.maxWidth = '520px';

        var header = document.createElement('div');
        header.className = 'modal-header';
        var heading = document.createElement('h2');
        if (options.danger) { heading.style.color = '#ff6b6b'; }
        var icon = document.createElement('i');
        icon.className = options.danger
            ? 'fas fa-exclamation-triangle' : 'fas fa-circle-question';
        icon.setAttribute('aria-hidden', 'true');
        icon.style.marginRight = '10px';
        heading.appendChild(icon);
        heading.appendChild(document.createTextNode(options.title || 'Confirm'));
        header.appendChild(heading);

        var body = document.createElement('div');
        body.className = 'modal-body';
        (options.lines || []).forEach(function (line) {
            var paragraph = document.createElement('p');
            paragraph.style.margin = '0 0 0.75rem 0';
            paragraph.textContent = line;        // text node, never markup
            body.appendChild(paragraph);
        });

        var actions = document.createElement('div');
        actions.className = 'modal-actions';
        actions.style.cssText =
            'display:flex; gap:0.75rem; justify-content:flex-end; padding:1rem 2rem 1.5rem;';

        function makeButton(label, kind) {
            var button = document.createElement('button');
            button.type = 'button';
            button.textContent = label;
            button.style.cssText =
                'padding:0.6rem 1.4rem; border-radius:6px; cursor:pointer; ' +
                'font-weight:600; border:1px solid ' +
                (kind === 'danger' ? 'rgba(255,68,68,0.6); background:rgba(255,68,68,0.15); color:#ff6b6b;'
                    : kind === 'primary' ? 'rgba(0,255,150,0.5); background:rgba(0,255,150,0.15); color:#00ff96;'
                        : 'rgba(255,255,255,0.25); background:rgba(255,255,255,0.06); color:#ddd;');
            return button;
        }

        var cancelButton = makeButton(options.cancelLabel || 'Cancel', 'neutral');
        var confirmButton = makeButton(options.confirmLabel || 'Confirm',
            options.danger ? 'danger' : 'primary');
        // Cancel is the safe default focus target for a destructive question.
        cancelButton.setAttribute('autofocus', '');
        actions.appendChild(cancelButton);
        actions.appendChild(confirmButton);

        content.appendChild(header);
        content.appendChild(body);
        content.appendChild(actions);
        overlay.appendChild(content);
        return { overlay: overlay, confirmButton: confirmButton, cancelButton: cancelButton };
    }

    function appConfirm(options) {
        options = options || {};
        return new Promise(function (resolve) {
            var parts = buildConfirmDialog(options);
            var settled = false;

            function settle(answer) {
                if (settled) { return; }   // single-flight: first answer wins
                settled = true;
                close(parts.overlay, 'api');
                if (parts.overlay.parentNode) {
                    parts.overlay.parentNode.removeChild(parts.overlay);
                }
                resolve(answer);
            }

            parts.confirmButton.addEventListener('click', function () { settle(true); });
            parts.cancelButton.addEventListener('click', function () { settle(false); });

            document.body.appendChild(parts.overlay);
            open(parts.overlay, {
                backdropClose: true,
                // Escape / backdrop / any external close counts as Cancel.
                onClose: function () {
                    if (!settled) {
                        settled = true;
                        if (parts.overlay.parentNode) {
                            parts.overlay.parentNode.removeChild(parts.overlay);
                        }
                        resolve(false);
                    }
                }
            });
        });
    }

    window.AppConfirm = { confirm: appConfirm, __initialized: true };
})();
