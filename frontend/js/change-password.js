/**
 * Change Password
 * ===============
 * Self-service password change, and the only page an account with a seeded or
 * admin-assigned password can use until it rotates.
 *
 * Contract (identical in spirit to signin.js):
 *  - ONE request per submission (state machine, not a boolean flag).
 *  - The auth credential is never visible to JavaScript; X-Requested-With
 *    announces a browser session so the response carries no token.
 *  - The backend is authoritative on password policy. The checks here only
 *    save a round-trip; they never decide anything.
 *  - The destination comes from the backend but is validated against a local
 *    allowlist, so a spoofed response cannot redirect off-site.
 *  - Never logs passwords, user objects or response bodies.
 *  - Errors render with textContent only, announced to screen readers.
 *
 * Failures open a popup rather than writing a line under the fields. A failure
 * here is not a typo to glance at — "your current password is wrong" or "that
 * password is too weak" has to be read before retrying — and dismissing it
 * returns focus to the field that needs attention.
 */

(function () {
    'use strict';

    const REQUEST_TIMEOUT_MS = 20000;   // bcrypt runs twice here (verify + hash)
    const MAX_PASSWORD_LENGTH = 1024;   // mirrors the backend limit

    // Mirrors backend/security/config_guard.py::assess_admin_password.
    // The backend re-checks; disagreement costs a round-trip, never access.
    const MIN_PASSWORD_LENGTH = 12;
    const MIN_DISTINCT_CHARACTERS = 6;

    const ALLOWED_REDIRECTS = new Set(['/home', '/dashboard']);
    const DEFAULT_REDIRECT = '/dashboard';

    // Chosen HERE by code, never interpolated from the server, so a hostile
    // response body cannot control what the user is told.
    const ERROR_MESSAGES = {
        INVALID_CURRENT_PASSWORD: 'Your current password is incorrect.',
        PASSWORD_REUSED: 'The new password must be different from your current one.',
        WEAK_PASSWORD: 'That password is not strong enough. Use at least ' +
            MIN_PASSWORD_LENGTH + ' characters and at least ' +
            MIN_DISTINCT_CHARACTERS + ' different ones.',
        RATE_LIMITED: 'Too many attempts. Please wait and try again.',
        PASSWORD_UPDATE_FAILED: 'Could not change the password. Please try again.',
        CSRF_FAILED: 'This request was blocked for security reasons. Reload the page and try again.',
        SESSION_EXPIRED: 'Your session has ended. Please sign in again.',
        MISMATCH: 'The two new passwords do not match.',
        INVALID_REQUEST: 'Please fill in every field and try again.',
        TIMEOUT: 'The server did not respond in time. Please try again.',
        NETWORK: 'Cannot reach the server. Check your connection and try again.',
        UNEXPECTED: 'Could not change the password. Please try again.'
    };

    // ============================================
    // State machine
    // ============================================
    const STATES = Object.freeze({
        IDLE: 'idle',
        SUBMITTING: 'submitting',
        SUCCEEDED: 'succeeded',
        FAILED: 'failed',
        RATE_LIMITED: 'rate_limited'
    });

    const state = {
        current: STATES.IDLE,
        controller: null,
        originalButtonHtml: null,
        // Which field to focus once the user dismisses the popup.
        focusAfterDismiss: null
    };

    /** SUCCEEDED is terminal — a completed change is never repeated. */
    function canSubmit() {
        return state.current === STATES.IDLE ||
            state.current === STATES.FAILED ||
            state.current === STATES.RATE_LIMITED;
    }

    // ============================================
    // Error popup
    // ============================================

    // Titles are chosen here, like the messages — nothing from the server
    // reaches the DOM. `severity` only picks a colour palette.
    const ALERT_TITLES = {
        error: 'Something went wrong',
        warning: 'Check your entry'
    };

    // Which field the user should land on after dismissing, per failure.
    const FOCUS_TARGET = {
        INVALID_CURRENT_PASSWORD: 'currentInput',
        PASSWORD_REUSED: 'newInput',
        WEAK_PASSWORD: 'newInput',
        MISMATCH: 'confirmInput'
    };

    // Problems the user fixes by typing again are amber; everything else red.
    const WARNING_CODES = new Set(['MISMATCH', 'PASSWORD_REUSED', 'WEAK_PASSWORD', 'INVALID_REQUEST']);

    function showError(elements, message, code) {
        const { popupBackdrop, popupCard, popupTitle, popupMessage, popupIcon,
                popupDismiss, statusRegion } = elements;

        // Screen readers get it from the live region regardless of the popup.
        if (statusRegion) statusRegion.textContent = message;

        if (!popupBackdrop || !popupMessage) return;   // markup missing: still announced

        const severity = WARNING_CODES.has(code) ? 'warning' : 'error';
        if (popupCard) popupCard.setAttribute('data-severity', severity);
        if (popupIcon) {
            const icon = popupIcon.querySelector('i');
            if (icon) {
                icon.className = severity === 'warning'
                    ? 'fas fa-circle-exclamation'
                    : 'fas fa-triangle-exclamation';
            }
        }
        if (popupTitle) popupTitle.textContent = ALERT_TITLES[severity];
        popupMessage.textContent = message;            // textContent, never innerHTML

        state.focusAfterDismiss = FOCUS_TARGET[code] || null;

        popupBackdrop.hidden = false;
        // Next frame, so the transition runs from the hidden state rather than
        // being collapsed into the same style recalculation.
        window.requestAnimationFrame(function () {
            popupBackdrop.classList.add('is-open');
        });
        if (popupDismiss) popupDismiss.focus();
    }

    function dismissError(elements) {
        const { popupBackdrop, statusRegion } = elements;
        if (!popupBackdrop || popupBackdrop.hidden) return;

        popupBackdrop.classList.remove('is-open');
        if (statusRegion) statusRegion.textContent = '';

        const finish = function () {
            popupBackdrop.hidden = true;
            const target = state.focusAfterDismiss && elements[state.focusAfterDismiss];
            state.focusAfterDismiss = null;
            if (target) {
                target.focus();
                target.select();
            } else if (elements.currentInput) {
                elements.currentInput.focus();
            }
        };

        // Wait for the fade, but never strand the popup if the transition does
        // not fire (reduced motion, a backgrounded tab).
        let done = false;
        const once = function () {
            if (done) return;
            done = true;
            popupBackdrop.removeEventListener('transitionend', once);
            finish();
        };
        popupBackdrop.addEventListener('transitionend', once);
        window.setTimeout(once, 300);
    }

    /** Close without moving focus. Used when a new submission starts — the
     *  user is already where they want to be, and yanking focus mid-submit
     *  would be worse than the stale popup. */
    function clearError(elements) {
        const { popupBackdrop, statusRegion } = elements;
        if (statusRegion) statusRegion.textContent = '';
        if (!popupBackdrop || popupBackdrop.hidden) return;
        state.focusAfterDismiss = null;
        popupBackdrop.classList.remove('is-open');
        popupBackdrop.hidden = true;
    }

    function setSubmitting(elements, submitting) {
        const { submitButton, inputs, statusRegion } = elements;
        if (submitButton) {
            submitButton.disabled = submitting;
            submitButton.setAttribute('aria-busy', submitting ? 'true' : 'false');
            if (submitting) {
                if (state.originalButtonHtml === null) {
                    state.originalButtonHtml = submitButton.innerHTML;
                }
                submitButton.textContent = '';
                const icon = document.createElement('i');
                icon.className = 'fas fa-spinner fa-spin';
                icon.setAttribute('aria-hidden', 'true');
                const label = document.createElement('span');
                label.textContent = 'CHANGING...';
                submitButton.append(icon, label);
            } else if (state.originalButtonHtml !== null) {
                submitButton.innerHTML = state.originalButtonHtml; // our own trusted markup
            }
        }
        inputs.forEach(function (input) {
            if (input) input.readOnly = submitting;
        });
        if (statusRegion) {
            statusRegion.textContent = submitting ? 'Changing your password, please wait...' : '';
        }
    }

    // ============================================
    // Redirect safety
    // ============================================
    function safeRedirect(candidate) {
        if (typeof candidate === 'string' && ALLOWED_REDIRECTS.has(candidate)) {
            return candidate;
        }
        return DEFAULT_REDIRECT;
    }

    // ============================================
    // Local policy mirror (advisory only)
    // ============================================
    function localPolicyFailure(newPassword) {
        if (newPassword.length < MIN_PASSWORD_LENGTH) return ERROR_MESSAGES.WEAK_PASSWORD;
        const distinct = new Set(newPassword.split(''));
        if (distinct.size < MIN_DISTINCT_CHARACTERS) return ERROR_MESSAGES.WEAK_PASSWORD;
        return null;
    }

    // ============================================
    // Network
    // ============================================
    async function submitWithTimeout(payload, timeoutMs) {
        const controller = new AbortController();
        state.controller = controller;
        const timeout = window.setTimeout(function () { controller.abort(); }, timeoutMs);
        try {
            return await fetch('/api/auth/change-password', {
                method: 'POST',
                credentials: 'include',
                cache: 'no-store',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    // Browser session: no token in the response body, and the
                    // backend's CSRF dependency requires this header.
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body: JSON.stringify(payload),
                signal: controller.signal
            });
        } finally {
            window.clearTimeout(timeout);
            state.controller = null;
        }
    }

    /** Map a failed response to a locally-defined message + terminal state. */
    async function describeFailure(response) {
        let code = null;
        try {
            const body = await response.json();
            const error = body && body.error;
            if (error && typeof error.code === 'string') code = error.code;
        } catch (_) { /* non-JSON body — fall through to status mapping */ }

        if (!code) {
            if (response.status === 429) code = 'RATE_LIMITED';
            else if (response.status === 401) code = 'SESSION_EXPIRED';
            else if (response.status === 403) code = 'INVALID_CURRENT_PASSWORD';
            else if (response.status >= 500) code = 'PASSWORD_UPDATE_FAILED';
            else code = 'INVALID_REQUEST';
        }

        const nextState = code === 'RATE_LIMITED' ? STATES.RATE_LIMITED : STATES.FAILED;
        return {
            code: code,
            message: ERROR_MESSAGES[code] || ERROR_MESSAGES.UNEXPECTED,
            nextState: nextState
        };
    }

    // ============================================
    // Submission
    // ============================================
    async function handleSubmit(event, elements) {
        event.preventDefault();
        if (!canSubmit()) return;

        const { currentInput, newInput, confirmInput } = elements;
        if (!currentInput || !newInput || !confirmInput) return;

        // Passwords are never modified, only length-capped to protect the
        // verification service.
        const currentPassword = currentInput.value.slice(0, MAX_PASSWORD_LENGTH);
        const newPassword = newInput.value.slice(0, MAX_PASSWORD_LENGTH);
        const confirmPassword = confirmInput.value.slice(0, MAX_PASSWORD_LENGTH);

        clearError(elements);

        // Dismissing the popup returns focus to the offending field, so these
        // paths do not move focus themselves.
        if (!currentPassword || !newPassword || !confirmPassword) {
            state.current = STATES.FAILED;
            const missing = !currentPassword ? 'INVALID_CURRENT_PASSWORD'
                : !newPassword ? 'WEAK_PASSWORD' : 'MISMATCH';
            showError(elements, ERROR_MESSAGES.INVALID_REQUEST, missing);
            return;
        }

        if (newPassword !== confirmPassword) {
            state.current = STATES.FAILED;
            confirmInput.value = '';
            showError(elements, ERROR_MESSAGES.MISMATCH, 'MISMATCH');
            return;
        }

        if (newPassword === currentPassword) {
            state.current = STATES.FAILED;
            showError(elements, ERROR_MESSAGES.PASSWORD_REUSED, 'PASSWORD_REUSED');
            return;
        }

        const policyFailure = localPolicyFailure(newPassword);
        if (policyFailure) {
            state.current = STATES.FAILED;
            showError(elements, policyFailure, 'WEAK_PASSWORD');
            return;
        }

        state.current = STATES.SUBMITTING;
        setSubmitting(elements, true);

        try {
            const response = await submitWithTimeout({
                current_password: currentPassword,
                new_password: newPassword
            }, REQUEST_TIMEOUT_MS);

            if (!response.ok) {
                const failure = await describeFailure(response);
                state.current = failure.nextState;
                setSubmitting(elements, false);

                if (failure.code === 'SESSION_EXPIRED') {
                    window.location.assign('/signin');
                    return;
                }

                // A wrong CURRENT password is the one failure where the new
                // password is worth keeping — the user typed it correctly and
                // only mistyped the old one. Clearing all three would make
                // them redo the whole form for someone else's mistake.
                if (failure.code === 'INVALID_CURRENT_PASSWORD') {
                    currentInput.value = '';
                } else {
                    currentInput.value = '';
                    newInput.value = '';
                    confirmInput.value = '';
                }
                showError(elements, failure.message, failure.code);
                return;
            }

            let data = null;
            try {
                data = await response.json();
            } catch (_) {
                data = null;
            }

            // The replacement session cookie is already stored by the browser.
            state.current = STATES.SUCCEEDED;
            window.location.assign(safeRedirect(data && data.redirect_url));

        } catch (error) {
            const timedOut = error && error.name === 'AbortError';
            state.current = STATES.FAILED;
            setSubmitting(elements, false);
            showError(elements,
                timedOut ? ERROR_MESSAGES.TIMEOUT : ERROR_MESSAGES.NETWORK,
                timedOut ? 'TIMEOUT' : 'NETWORK');
        }
    }

    // ============================================
    // Caps Lock warning (same courtesy as sign-in)
    // ============================================
    function wireCapsLockWarning(elements) {
        const { warning, currentInput, newInput, confirmInput } = elements;
        if (!warning) return;
        const update = function (event) {
            if (typeof event.getModifierState !== 'function') return;
            warning.style.display = event.getModifierState('CapsLock') ? '' : 'none';
        };
        [currentInput, newInput, confirmInput].forEach(function (input) {
            if (!input) return;
            input.addEventListener('keyup', update);
            input.addEventListener('keydown', update);
            input.addEventListener('blur', function () { warning.style.display = 'none'; });
        });
    }

    // ============================================
    // Popup dismissal
    // ============================================
    function wirePopupDismissal(elements) {
        const { popupBackdrop, popupDismiss, popupCard } = elements;
        if (!popupBackdrop) return;

        if (popupDismiss) {
            popupDismiss.addEventListener('click', function () {
                dismissError(elements);
            });
        }

        // Clicking the backdrop, but not the dialog itself.
        popupBackdrop.addEventListener('click', function (event) {
            if (event.target === popupBackdrop) dismissError(elements);
        });

        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && !popupBackdrop.hidden) {
                dismissError(elements);
            }
        });

        // While the dialog is open it is modal, so Tab must not walk into the
        // form behind it. There is exactly one focusable control inside, which
        // makes the trap a single line rather than a focus-order calculation.
        if (popupCard) {
            popupCard.addEventListener('keydown', function (event) {
                if (event.key === 'Tab') {
                    event.preventDefault();
                    if (elements.popupDismiss) elements.popupDismiss.focus();
                }
            });
        }
    }

    // ============================================
    // Bootstrap
    // ============================================
    function init() {
        const form = document.getElementById('change-password-form');
        if (!form) return;

        const currentInput = document.getElementById('current-password');
        const newInput = document.getElementById('new-password');
        const confirmInput = document.getElementById('confirm-password');

        const elements = {
            form: form,
            currentInput: currentInput,
            newInput: newInput,
            confirmInput: confirmInput,
            inputs: [currentInput, newInput, confirmInput],
            submitButton: form.querySelector('button[type="submit"]'),
            statusRegion: document.getElementById('change-password-status'),
            warning: document.getElementById('capslock-warning'),
            popupBackdrop: document.getElementById('alert-popup-backdrop'),
            popupCard: document.getElementById('alert-popup'),
            popupIcon: document.getElementById('alert-popup-icon'),
            popupTitle: document.getElementById('alert-popup-title'),
            popupMessage: document.getElementById('alert-popup-message'),
            popupDismiss: document.getElementById('alert-popup-dismiss')
        };

        form.addEventListener('submit', function (event) {
            handleSubmit(event, elements);
        });

        wirePopupDismissal(elements);
        wireCapsLockWarning(elements);
        if (currentInput) currentInput.focus();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
