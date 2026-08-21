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

    /** What an element looked like BEFORE we suppressed it, so close() can put
     *  it back exactly. Blindly removing aria-hidden / clearing inert on close
     *  would EXPOSE an element that the page had deliberately hidden before any
     *  modal opened — a real risk now this module runs on every page rather
     *  than only Unknown Faces. */
    var priorSuppression = new WeakMap();

    /** Non-interactive but visible: the "there is a modal above you" state.
     *  Never applied while a descendant still owns focus — open() moves focus
     *  into the new modal FIRST, because aria-hidden on a focused subtree is
     *  both an a11y violation and a screen-reader trap. */
    function suppress(el) {
        if (!priorSuppression.has(el)) {
            priorSuppression.set(el, {
                ariaHidden: el.getAttribute('aria-hidden'),          // null when absent
                inert: ('inert' in el) ? el.inert : null,
                hadClass: el.classList.contains('is-stack-under')
            });
        }
        el.classList.add('is-stack-under');
        el.setAttribute('aria-hidden', 'true');
        if ('inert' in el) { el.inert = true; }
    }

    /** Restore the captured state verbatim. Idempotent: an element we never
     *  suppressed is left untouched rather than being "cleaned". */
    function unsuppress(el) {
        var prior = priorSuppression.get(el);
        if (!prior) { return; }
        priorSuppression.delete(el);

        if (!prior.hadClass) { el.classList.remove('is-stack-under'); }
        if (prior.ariaHidden === null) {
            el.removeAttribute('aria-hidden');
        } else {
            el.setAttribute('aria-hidden', prior.ariaHidden);
        }
        if ('inert' in el && prior.inert !== null) { el.inert = prior.inert; }
    }

    /** Everything that makes an entry's element "closed", EXCEPT focus
     *  handling and body unlock — those are per-operation, not per-modal,
     *  so cascades restore focus exactly once at the end. */
    function retire(entry, reason) {
        var el = entry.el;
        el.style.display = 'none';
        el.style.zIndex = '';
        el.classList.remove('is-stack-nested');
        // Same rule as unsuppress(): restore what was there before, never
        // blanket-clear. A modal that was itself suppressed (something opened
        // on top of it) must not come back with the page's original
        // aria-hidden/inert stripped off.
        unsuppress(el);
        // The callback runs AFTER the entry has left the stack, so a legacy
        // close function that calls ModalStack.close() again hits the
        // idempotent no-op path instead of recursing.
        if (typeof entry.onClose === 'function') {
            try { entry.onClose(el, reason); } catch (err) {
                console.error('[ModalStack] onClose failed for #' + (el.id || '?'), err);
            }
        }
    }

    // ---- page scroll lock -------------------------------------------------
    //
    // Two scrolling models exist in this app and the lock must handle both:
    //
    //   app shell   admin.css sets html/body { overflow: hidden; height: 100vh }
    //               and each page designates an inner overflow-y:auto scroller.
    //               window.scrollY is always 0; freezing the inner element is
    //               what actually stops the background moving.
    //   document    signin.html (no admin.css) scrolls normally.
    //
    // The previous lock was a CSS rule scoped to ONE page's scroller
    // (`body.modal-stack-locked .intelligence-content`), so it did nothing
    // anywhere else — which mattered the moment this module started loading on
    // every page.
    //
    // Captured state is restored verbatim on final close: whatever inline
    // styles the page had before are put back, not cleared.

    var pageLock = null;   // non-null only while at least one modal is open

    function verticalScrollers(exclude) {
        // Every element that is actually scrolling right now.
        //
        // This was a depth-limited walk (4 levels) on the assumption that a
        // page's scroller sits near the top of the tree. It does on
        // /admin/intelligence (depth 2) and it does NOT on /admin/settings,
        // where #settings-container and .table-container are at depth 5 — so
        // the lock silently missed them and the background still scrolled
        // behind an open modal. Measured, not assumed: the probe scrolls the
        // background while the modal is open and fails if it moves.
        //
        // Runs once per modal open, not per frame, so a single full query is
        // cheap. Capped so a pathological DOM cannot stall the open.
        //
        // `exclude` is the modal being opened, and skipping its subtree is
        // load-bearing rather than an optimisation. lockPage() runs AFTER the
        // dialog is displayed, so a tall dialog is already scrolling by then
        // and was collected like any other scroller — then frozen with
        // overflow:hidden. The dialog could no longer scroll itself, and since
        // the page behind it is locked too, everything past the fold became
        // unreachable: the bottom of a form, its buttons, everything.
        //
        // Freeze the BACKGROUND. Never freeze the dialog.
        var found = [];
        var all = document.body.querySelectorAll('*');
        var limit = Math.min(all.length, 4000);
        for (var i = 0; i < limit; i++) {
            var el = all[i];
            if (exclude && (el === exclude || exclude.contains(el))) { continue; }
            if (el.scrollHeight > el.clientHeight + 1) {
                var overflowY = getComputedStyle(el).overflowY;
                if (overflowY === 'auto' || overflowY === 'scroll') { found.push(el); }
            }
        }
        return found;
    }

    function lockPage(exclude) {
        if (pageLock) { return; }   // already locked by an outer modal
        var body = document.body;
        var docEl = document.documentElement;

        // Does the DOCUMENT scroll, or does an inner element?
        var documentScrolls = docEl.scrollHeight > docEl.clientHeight + 1;
        var scrollY = window.scrollY || window.pageYOffset || 0;

        // Only compensate when a scrollbar is really there to disappear;
        // padding the body unconditionally is itself a horizontal jump.
        var scrollbarWidth = window.innerWidth - docEl.clientWidth;

        pageLock = {
            scrollY: scrollY,
            documentScrolls: documentScrolls,
            body: {
                overflow: body.style.overflow,
                position: body.style.position,
                top: body.style.top,
                left: body.style.left,
                right: body.style.right,
                width: body.style.width,
                paddingRight: body.style.paddingRight
            },
            scrollers: verticalScrollers(exclude).map(function (el) {
                return { el: el, overflow: el.style.overflow, scrollTop: el.scrollTop };
            })
        };

        if (documentScrolls) {
            // position:fixed is the only technique that also stops iOS rubber
            // banding; the negative top keeps the page visually still.
            body.style.position = 'fixed';
            body.style.top = (-scrollY) + 'px';
            body.style.left = '0';
            body.style.right = '0';
            body.style.width = '100%';
        }
        body.style.overflow = 'hidden';
        if (scrollbarWidth > 0) {
            var current = parseInt(getComputedStyle(body).paddingRight, 10) || 0;
            body.style.paddingRight = (current + scrollbarWidth) + 'px';
        }

        // Freezing an element's overflow preserves its scrollTop, so the
        // background stays exactly where the user left it.
        pageLock.scrollers.forEach(function (saved) {
            saved.el.style.overflow = 'hidden';
        });

        body.classList.add('modal-stack-locked');
    }

    function unlockPage() {
        if (!pageLock) { return; }
        var body = document.body;
        var saved = pageLock;
        pageLock = null;

        // Restore the captured inline styles EXACTLY — including back to ''
        // where the page had none, so we never leave our own values behind.
        body.style.overflow = saved.body.overflow;
        body.style.position = saved.body.position;
        body.style.top = saved.body.top;
        body.style.left = saved.body.left;
        body.style.right = saved.body.right;
        body.style.width = saved.body.width;
        body.style.paddingRight = saved.body.paddingRight;

        saved.scrollers.forEach(function (entry) {
            entry.el.style.overflow = entry.overflow;
            entry.el.scrollTop = entry.scrollTop;
        });

        body.classList.remove('modal-stack-locked');

        if (saved.documentScrolls) {
            window.scrollTo(0, saved.scrollY);
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
            // Last modal gone: restore the page exactly as it was.
            unlockPage();
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
            // Optional veto. Some dialogs must refuse to close at certain
            // moments — the watchlist editor guards `state.saving` so a save
            // in flight cannot be abandoned by an accidental Escape or
            // backdrop click. Without this hook, adopting the shared stack
            // would have silently removed that protection.
            canClose: typeof opts.canClose === 'function' ? opts.canClose : null,
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
        // Pass the dialog so the lock freezes the background around it and
        // not the dialog's own scroller.
        if (stack.length === 1) { lockPage(el); }
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

        // A dialog may refuse. The veto applies to user-driven dismissal
        // (Escape, backdrop) — an explicit programmatic close with
        // reason 'force' always wins, so a page can still tear its own modal
        // down when its work finishes.
        if (reason !== 'force') {
            for (var v = stack.length - 1; v >= index; v--) {
                var candidate = stack[v];
                if (candidate.canClose && candidate.canClose(candidate.el, reason) === false) {
                    return;
                }
            }
        }

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
