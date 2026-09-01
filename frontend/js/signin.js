/**
 * Sign-In (hardened rewrite)
 * ==========================
 * Backend performs all authentication and authorization. This script only
 * collects credentials, submits them once, and navigates.
 *
 * Contract:
 *  - ONE login request per submission (state machine, not a boolean flag).
 *  - No /api/auth/me verification loop: the browser has already stored the
 *    Set-Cookie header by the time the login fetch resolves.
 *  - The auth credential is never visible to JavaScript — it lives only in
 *    the HttpOnly cookie. We announce ourselves with X-Requested-With so the
 *    backend omits any token from the response body.
 *  - The backend chooses the destination; we validate it against a local
 *    allowlist. Role checks here would be cosmetic — every destination route
 *    enforces authorization server-side.
 *  - Never logs usernames, passwords, user objects or responses.
 *  - Errors render with textContent only, announced to screen readers.
 */

(function () {
    'use strict';

    const LOGIN_TIMEOUT_MS = 15000;
    const MAX_USERNAME_LENGTH = 150;   // mirrors the backend limit
    const MAX_PASSWORD_LENGTH = 1024;  // mirrors the backend limit

    // The backend returns the destination; we still refuse anything not on
    // this list so a compromised/spoofed response cannot redirect off-site.
    // '/change-password' is where the backend sends an account still carrying a
    // seeded or admin-assigned password; it is a real login destination.
    const ALLOWED_REDIRECTS = new Set(['/home', '/dashboard', '/change-password']);
    const DEFAULT_REDIRECT = '/dashboard';

    // Messages are chosen HERE by code, never interpolated from the server,
    // so a hostile response body cannot control what the user is told.
    const ERROR_MESSAGES = {
        INVALID_CREDENTIALS: 'Invalid username or password.',
        RATE_LIMITED: 'Too many sign-in attempts. Please wait and try again.',
        ACCOUNT_DISABLED: 'This account is not able to sign in. Contact an administrator.',
        ACCOUNT_LOCKED: 'This account is temporarily locked. Please try again later.',
        MFA_REQUIRED: 'Additional verification is required to continue.',
        SESSION_CREATION_FAILED: 'Could not establish a session. Please try again.',
        AUTH_SERVICE_UNAVAILABLE: 'Sign-in is temporarily unavailable. Please try again.',
        CSRF_FAILED: 'This sign-in request was blocked for security reasons. Reload the page and try again.',
        INVALID_REQUEST: 'Please check your username and password and try again.',
        TIMEOUT: 'The server did not respond in time. Please try again.',
        NETWORK: 'Cannot reach the server. Check your connection and try again.',
        UNEXPECTED: 'Sign-in failed. Please try again.'
    };

    // ============================================
    // State machine
    // ============================================
    const STATES = Object.freeze({
        IDLE: 'idle',
        SUBMITTING: 'submitting',
        SUCCEEDED: 'succeeded',
        FAILED: 'failed',
        RATE_LIMITED: 'rate_limited',
        BLOCKED: 'blocked'
    });

    const state = {
        current: STATES.IDLE,
        controller: null,
        originalButtonHtml: null
    };

    /** Only IDLE and the recoverable failure states may start a request.
     *  SUCCEEDED is terminal — a completed login can never be repeated. */
    function canSubmit() {
        return state.current === STATES.IDLE ||
            state.current === STATES.FAILED ||
            state.current === STATES.RATE_LIMITED;
    }

    // ============================================
    // Safe element access
    // ============================================
    function requireElement(id) {
        const element = document.getElementById(id);
        if (!element) {
            console.error('[SIGNIN] Missing required element', { id });
            return null;
        }
        return element;
    }

    function optionalElement(id) {
        return document.getElementById(id);
    }

    // ============================================
    // UI helpers (textContent only — never innerHTML)
    // ============================================
    function showError(elements, message) {
        const { errorContainer, errorText } = elements;
        if (!errorContainer) return;
        if (errorText) {
            errorText.textContent = message;
        } else {
            errorContainer.textContent = message;
        }
        errorContainer.style.display = 'flex';
        // role="alert" + aria-live="assertive" on the container announce this
        // automatically; we do NOT steal focus, which would trap keyboard users.
    }

    function clearError(elements) {
        const { errorContainer, errorText } = elements;
        if (!errorContainer) return;
        errorContainer.style.display = 'none';
        if (errorText) errorText.textContent = '';
    }

    function setSubmitting(elements, submitting) {
        const { submitButton, statusRegion, usernameInput, passwordInput } = elements;
        if (submitButton) {
            submitButton.disabled = submitting;
            submitButton.setAttribute('aria-busy', submitting ? 'true' : 'false');
            if (submitting) {
                if (state.originalButtonHtml === null) {
                    state.originalButtonHtml = submitButton.innerHTML; // static markup we authored
                }
                submitButton.replaceChildren();
                const icon = document.createElement('i');
                icon.className = 'fas fa-spinner fa-spin';
                icon.setAttribute('aria-hidden', 'true');
                const label = document.createElement('span');
                label.textContent = 'SIGNING IN...';
                submitButton.append(icon, label);
            } else if (state.originalButtonHtml !== null) {
                submitButton.innerHTML = state.originalButtonHtml; // restore our own trusted markup
            }
        }
        if (usernameInput) usernameInput.readOnly = submitting;
        if (passwordInput) passwordInput.readOnly = submitting;
        if (statusRegion) {
            statusRegion.textContent = submitting ? 'Signing in, please wait...' : '';
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
    // Network
    // ============================================
    async function loginWithTimeout(payload, timeoutMs) {
        const controller = new AbortController();
        state.controller = controller;
        const timeout = window.setTimeout(function () { controller.abort(); }, timeoutMs);
        try {
            return await fetch('/api/auth/login', {
                method: 'POST',
                credentials: 'include',
                cache: 'no-store',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    // Tells the backend this is a browser session: the response
                    // body will contain NO token (cookie only).
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
            else if (response.status === 401) code = 'INVALID_CREDENTIALS';
            else if (response.status === 403) code = 'CSRF_FAILED';
            else if (response.status >= 500) code = 'AUTH_SERVICE_UNAVAILABLE';
            else code = 'INVALID_REQUEST';
        }

        const nextState =
            code === 'RATE_LIMITED' ? STATES.RATE_LIMITED :
            (code === 'ACCOUNT_DISABLED' || code === 'ACCOUNT_LOCKED') ? STATES.BLOCKED :
            STATES.FAILED;

        return { code: code, message: ERROR_MESSAGES[code] || ERROR_MESSAGES.UNEXPECTED, nextState: nextState };
    }

    // ============================================
    // Submission
    // ============================================
    async function handleSubmit(event, elements) {
        event.preventDefault();

        // One authoritative guard: a second click or a repeated Enter press
        // while a request is in flight is ignored entirely.
        if (!canSubmit()) return;

        const { usernameInput, passwordInput } = elements;
        if (!usernameInput || !passwordInput) return;

        // Username: trim surrounding whitespace, cap length. Password: never
        // modified, only length-capped to protect the verification service.
        const username = usernameInput.value.trim().slice(0, MAX_USERNAME_LENGTH);
        const password = passwordInput.value.slice(0, MAX_PASSWORD_LENGTH);

        clearError(elements);

        if (!username || !password) {
            state.current = STATES.FAILED;
            showError(elements, ERROR_MESSAGES.INVALID_REQUEST);
            (username ? passwordInput : usernameInput).focus();
            return;
        }

        state.current = STATES.SUBMITTING;
        setSubmitting(elements, true);

        try {
            const response = await loginWithTimeout({ username: username, password: password }, LOGIN_TIMEOUT_MS);

            if (!response.ok) {
                const failure = await describeFailure(response);
                state.current = failure.nextState;
                setSubmitting(elements, false);
                showError(elements, failure.message);
                // Clear the password, keep the username so the user does not retype it
                passwordInput.value = '';
                passwordInput.focus();
                return;
            }

            let data = null;
            try {
                data = await response.json();
            } catch (_) {
                data = null;
            }

            // The session cookie is already stored by the browser at this point —
            // no verification round-trip is needed or performed.
            state.current = STATES.SUCCEEDED;
            const destination = safeRedirect(data && data.redirect_url);
            window.location.assign(destination);

        } catch (error) {
            const aborted = error && error.name === 'AbortError';
            state.current = STATES.FAILED;
            setSubmitting(elements, false);
            showError(elements, aborted ? ERROR_MESSAGES.TIMEOUT : ERROR_MESSAGES.NETWORK);
            passwordInput.value = '';
            passwordInput.focus();
        }
    }

    // ============================================
    // Password visibility + Caps Lock
    // ============================================
    function setupPasswordControls(elements) {
        const { passwordInput, toggleButton, capsWarning } = elements;

        if (toggleButton && passwordInput) {
            toggleButton.setAttribute('aria-pressed', 'false');
            toggleButton.addEventListener('click', function () {
                const revealed = passwordInput.type === 'text';
                passwordInput.type = revealed ? 'password' : 'text';
                toggleButton.setAttribute('aria-pressed', revealed ? 'false' : 'true');
                toggleButton.setAttribute('aria-label', revealed ? 'Show password' : 'Hide password');
                const icon = toggleButton.querySelector('i');
                if (icon) {
                    icon.classList.toggle('fa-eye', revealed);
                    icon.classList.toggle('fa-eye-slash', !revealed);
                }
                passwordInput.focus();
            });
        }

        if (passwordInput && capsWarning) {
            const updateCaps = function (e) {
                const on = typeof e.getModifierState === 'function' && e.getModifierState('CapsLock');
                capsWarning.style.display = on ? 'flex' : 'none';
            };
            passwordInput.addEventListener('keydown', updateCaps);
            passwordInput.addEventListener('keyup', updateCaps);
            passwordInput.addEventListener('blur', function () {
                capsWarning.style.display = 'none';
            });
        }
    }

    // ============================================
    // Initialization (after the DOM exists)
    // ============================================
    function initialize() {
        const form = requireElement('signin-form');
        if (!form) return; // page structure missing — fail quietly, never throw

        const elements = {
            form: form,
            usernameInput: requireElement('username'),
            passwordInput: requireElement('password'),
            errorContainer: optionalElement('error-message'),
            errorText: optionalElement('error-text'),
            statusRegion: optionalElement('signin-status'),
            capsWarning: optionalElement('capslock-warning'),
            toggleButton: document.querySelector('.toggle-password-btn'),
            submitButton: form.querySelector('button[type="submit"], input[type="submit"]')
        };

        if (!elements.usernameInput || !elements.passwordInput) return;

        if (!form.dataset.listenerAttached) {
            form.addEventListener('submit', function (event) {
                handleSubmit(event, elements);
            });
            form.dataset.listenerAttached = 'true';
        }

        setupPasswordControls(elements);

        // Abandon an in-flight login if the user navigates away
        window.addEventListener('pagehide', function () {
            if (state.controller) {
                try { state.controller.abort(); } catch (_) { /* noop */ }
            }
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize);
    } else {
        initialize(); // script is deferred; DOM is already parsed
    }
})();
