/**
 * People Tracking Intelligence — SQL Agent frontend
 * =================================================
 * Rules implemented here (do not regress):
 *  - ONE authoritative Send/Stop handler bound exactly once (idempotent init,
 *    dataset guard); keydown (not deprecated keypress); Shift+Enter = newline
 *  - explicit request state machine: idle|connecting|streaming|stopping|
 *    completed|failed|cancelled — never inferred from an AbortController
 *  - every request has a crypto.randomUUID request_id; every server event is
 *    correlated by request_id + sequence (stale/duplicate events are dropped)
 *  - Stop performs REAL server-side cancellation
 *    (POST /api/sql-agent/requests/{id}/cancel) plus a local abort; a stopped
 *    request renders as "stopped", never as failed
 *  - single safe markdown pipeline (escape-first — model output is never
 *    parsed as HTML) shared by SSE, WebSocket, REST, history and exports
 *  - standards-compliant SSE parser: \r\n, multiline data:, comments,
 *    event:/id: fields, split UTF-8 chunks, decoder flush, max event size;
 *    completion REQUIRES an explicit terminal event (complete|error|cancelled)
 *  - structured backend error codes drive behavior (ACCOUNT_BLOCKED opens the
 *    modal) — never substring matching on error prose
 *  - accessible blocking modal: DOM-built, textContent only, single instance,
 *    focus trap, one consistent close policy (button AND Escape both close)
 *  - shared API client: credentials, no-store, Accept, CSRF header on
 *    mutations, timeout, AbortController, safe body parsing, 401 handling
 *  - transport manager: SSE preferred; REST fallback ONLY when the SSE
 *    request was provably not accepted; the same request_id acts as the
 *    idempotency key so a fallback can never execute the query twice;
 *    WebSocket transport is opt-in with a managed lifecycle + dispatcher
 *  - per-request DOM ids (response-{id} / typing-{id}), state kept in a
 *    bounded Map; finalizeRequest(id, outcome) runs exactly once per request
 *  - raw markdown is kept separately from rendered HTML (copy/exports/retry
 *    never scrape the DOM); bounded sizes everywhere; no content logging
 */
'use strict';

(() => {
    const DEBUG = false; // never log queries/responses in production
    const dlog = (...args) => { if (DEBUG) console.log('[TRACKING]', ...args); };

    // ------------------------------------------------------------------
    // Bounds
    // ------------------------------------------------------------------
    const MAX_ACTIVE_REQUESTS = 20;       // terminal entries pruned FIFO
    const MAX_RAW_RESPONSE_CHARS = 400_000;
    const MAX_DOM_MESSAGES = 100;
    const MAX_SSE_EVENT_CHARS = 1_000_000;
    const HISTORY_PAGE_SIZE = 25;
    const REQUEST_TIMEOUT_MS = 20_000;    // non-streaming API calls
    const STREAM_IDLE_TIMEOUT_MS = 90_000; // no event (incl. heartbeat) => dead

    // ------------------------------------------------------------------
    // Global state
    // ------------------------------------------------------------------
    let currentUser = null;
    let currentSessionId = null;
    let activeRequestId = null;               // at most one in-flight query
    const activeRequests = new Map();         // request_id -> request record
    const timers = new Set();
    const controllers = new Set();
    let destroyed = false;

    function trackTimeout(fn, ms) {
        const id = setTimeout(() => { timers.delete(id); fn(); }, ms);
        timers.add(id);
        return id;
    }

    function newRequestId() {
        return (window.crypto && crypto.randomUUID)
            ? crypto.randomUUID().replace(/-/g, '')
            : `req${Date.now()}${Math.random().toString(36).slice(2, 10)}`;
    }

    // ------------------------------------------------------------------
    // Shared API client
    // ------------------------------------------------------------------
    const MUTATING = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

    async function api(url, options = {}) {
        const method = (options.method || 'GET').toUpperCase();
        const controller = options.controller || new AbortController();
        controllers.add(controller);
        const timeoutId = setTimeout(() => controller.abort(), options.timeoutMs || REQUEST_TIMEOUT_MS);
        timers.add(timeoutId);
        try {
            const headers = { Accept: 'application/json' };
            if (MUTATING.has(method)) headers['X-Requested-With'] = 'XMLHttpRequest'; // CSRF
            if (options.body !== undefined) headers['Content-Type'] = 'application/json';
            const resp = await fetch(url, {
                method,
                headers,
                body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
                credentials: 'include',
                cache: 'no-store',
                signal: controller.signal,
            });
            const payload = await parseBody(resp);
            if (resp.status === 401) handleAuthExpired();
            return { ok: resp.ok, status: resp.status, payload, errorCode: extractErrorCode(payload) };
        } catch (e) {
            return { ok: false, status: 0, payload: null, errorCode: e && e.name === 'AbortError' ? 'ABORTED' : 'NETWORK_ERROR' };
        } finally {
            clearTimeout(timeoutId);
            timers.delete(timeoutId);
            controllers.delete(controller);
        }
    }

    async function parseBody(resp) {
        if (resp.status === 204) return null;
        const ctype = (resp.headers.get('content-type') || '').toLowerCase();
        try {
            if (ctype.includes('application/json')) return await resp.json();
            const text = await resp.text();
            if (!text) return null;
            if (ctype.includes('text/html')) return { detail: `Server returned an HTML page (HTTP ${resp.status})` };
            return { detail: text.slice(0, 300) };
        } catch (e) {
            return null;
        }
    }

    /** Structured error contract: {error:{code,message,reference_id,retryable}} */
    function extractErrorCode(payload) {
        return payload && payload.error && typeof payload.error === 'object'
            ? payload.error.code || null : null;
    }

    function errorMessage(result, fallback) {
        const p = result && result.payload;
        if (p && p.error && typeof p.error.message === 'string') return p.error.message;
        if (p && typeof p.detail === 'string') return p.detail;
        if (p && typeof p.error === 'string') return p.error;
        return fallback || 'Request failed';
    }

    let authExpiredNotified = false;
    function handleAuthExpired() {
        if (authExpiredNotified) return;
        authExpiredNotified = true;
        updateConnectionStatus('disconnected', 'Session expired — please sign in again');
    }

    // ------------------------------------------------------------------
    // Safe rendering (escape-first — ONE pipeline for all transports)
    // ------------------------------------------------------------------
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text === null || text === undefined ? '' : String(text);
        return div.innerHTML;
    }

    function inlineMd(text) {
        // Input is already HTML-escaped; these only add formatting tags
        return text
            .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*(.*?)\*/g, '<em>$1</em>')
            .replace(/`([^`]+)`/g, '<code>$1</code>');
    }

    function splitTableRow(row) {
        let r = row.trim();
        if (r.startsWith('|')) r = r.slice(1);
        if (r.endsWith('|')) r = r.slice(0, -1);
        return r.split('|').map(c => c.trim());
    }

    /** Model output is UNTRUSTED TEXT: escaped first, then markdown formatting
     *  is layered on top. Raw HTML in the model output can never execute. */
    function renderMarkdown(content) {
        if (!content) return '';
        const lines = escapeHtml(content).split('\n');
        const html = [];
        let i = 0;
        while (i < lines.length) {
            const line = lines[i];
            const next = i + 1 < lines.length ? lines[i + 1].trim() : '';
            const isTableStart = line.trim().startsWith('|') &&
                next.startsWith('|') && next.includes('-') &&
                /^[\s|:-]+$/.test(next);
            if (isTableStart) {
                const header = splitTableRow(line);
                i += 2;
                const rows = [];
                while (i < lines.length && lines[i].trim().startsWith('|')) {
                    rows.push(splitTableRow(lines[i]));
                    i++;
                }
                let t = '<div class="table-scroll"><table class="md-table"><thead><tr>';
                t += header.map(c => `<th>${inlineMd(c)}</th>`).join('');
                t += '</tr></thead><tbody>';
                rows.forEach(r => {
                    t += '<tr>' + r.map(c => `<td>${inlineMd(c)}</td>`).join('') + '</tr>';
                });
                t += '</tbody></table></div>';
                html.push(t);
            } else if (/^#{1,4}\s+/.test(line.trim())) {
                const level = Math.min(4, line.trim().match(/^#+/)[0].length);
                html.push(`<h${level + 2}>${inlineMd(line.trim().replace(/^#+\s+/, ''))}</h${level + 2}>`);
                i++;
            } else {
                html.push(inlineMd(line) + '<br>');
                i++;
            }
        }
        return html.join('');
    }

    /** The ONLY place rendered markdown reaches the DOM. */
    function renderInto(el, rawMarkdown) {
        el.innerHTML = renderMarkdown(rawMarkdown);
    }

    // ------------------------------------------------------------------
    // Timestamps (strict — no now-substitution, no crash on junk)
    // ------------------------------------------------------------------
    function parseServerDate(ts) {
        if (ts instanceof Date) return Number.isFinite(ts.getTime()) ? ts : null;
        if (typeof ts !== 'string' || !ts.trim()) return null;
        const s = ts.trim();
        const parsed = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s) ? new Date(s) : new Date(s + 'Z');
        return Number.isFinite(parsed.getTime()) ? parsed : null;
    }

    function formatTime(timestamp) {
        const date = parseServerDate(timestamp);
        if (!date) return 'Unknown';
        const diffMs = Date.now() - date.getTime();
        if (diffMs < -60_000) return date.toLocaleString();
        const diffMins = Math.floor(diffMs / 60000);
        if (diffMins < 1) return 'Just now';
        if (diffMins < 60) return `${diffMins}m ago`;
        const diffHours = Math.floor(diffMs / 3600000);
        if (diffHours < 24) return `${diffHours}h ago`;
        const diffDays = Math.floor(diffMs / 86400000);
        if (diffDays < 7) return `${diffDays}d ago`;
        return date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
    }

    // ------------------------------------------------------------------
    // Connection status + notices
    // ------------------------------------------------------------------
    function updateConnectionStatus(status, text) {
        const statusEl = document.getElementById('connectionStatus');
        const statusText = document.getElementById('statusText');
        if (!statusEl || !statusText) return;
        statusEl.className = `connection-status ${status}`;
        statusText.textContent = text || status.charAt(0).toUpperCase() + status.slice(1);
    }

    function showNotice(message, kind = 'info') {
        let area = document.getElementById('tracking-notices');
        if (!area) {
            area = document.createElement('div');
            area.id = 'tracking-notices';
            area.setAttribute('role', 'status');
            area.setAttribute('aria-live', 'polite');
            area.style.cssText = 'position:fixed;top:80px;right:20px;z-index:10000;display:flex;flex-direction:column;gap:.5rem;max-width:420px;';
            document.body.appendChild(area);
        }
        const note = document.createElement('div');
        note.style.cssText = 'padding:.7rem 1rem;border-radius:8px;color:#fff;font-size:.85rem;box-shadow:0 4px 20px rgba(0,0,0,.4);'
            + (kind === 'error' ? 'background:#b91c1c;' : kind === 'success' ? 'background:#047857;' : 'background:#1e40af;');
        note.textContent = message;
        area.appendChild(note);
        trackTimeout(() => note.remove(), 5000);
    }

    // ------------------------------------------------------------------
    // Blocking modal (structured code-driven, safe DOM, accessible)
    // ------------------------------------------------------------------
    let blockingModalOpen = false;

    /**
     * ACCOUNT_BLOCKED means the account really is deactivated server-side — the
     * backend only sends that code after the database transaction commits. So
     * every cached credential and privilege here is now worthless: drop them
     * rather than leave the page acting signed-in against a server that will
     * refuse it. The modal explains; this makes the client's state true.
     *
     * Deliberately NOT called for QUERY_DENIED (a refused query, session intact)
     * or for ENFORCEMENT_FAILED, which the backend downgrades to QUERY_DENIED
     * precisely so it can never log anyone out over a block that did not happen.
     */
    function clearBlockedSession() {
        try {
            localStorage.removeItem('access_token');
            sessionStorage.removeItem('navbar_privileges');
        } catch (e) { /* storage unavailable — the server still refuses the token */ }
        // Best-effort: clears the httpOnly cookie the browser holds.
        try {
            fetch('/api/auth/logout', {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
            }).catch(function () { /* the account is already inactive */ });
        } catch (e) { /* ignore */ }
    }

    function showBlockingModal(safeMessage, referenceId) {
        if (blockingModalOpen) return;   // never stack duplicates
        blockingModalOpen = true;
        clearBlockedSession();

        const opener = document.activeElement;
        const modal = document.createElement('div');
        modal.id = 'blockingModal';
        modal.setAttribute('role', 'alertdialog');
        modal.setAttribute('aria-modal', 'true');
        modal.setAttribute('aria-labelledby', 'blockingModalTitle');
        modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;justify-content:center;align-items:center;z-index:10000;';

        const box = document.createElement('div');
        box.style.cssText = 'background:#fff;border-radius:10px;padding:30px;max-width:500px;width:90%;box-shadow:0 10px 40px rgba(0,0,0,.3);';

        const title = document.createElement('h2');
        title.id = 'blockingModalTitle';
        title.style.cssText = 'color:#dc3545;margin:0 0 10px;font-size:24px;font-weight:bold;text-align:center;';
        title.textContent = 'Access Restricted';
        box.appendChild(title);

        const greeting = document.createElement('p');
        greeting.style.cssText = 'color:#856404;background:#fff3cd;border:1px solid #ffc107;border-radius:5px;padding:15px;font-size:15px;';
        const name = (currentUser && (currentUser.full_name || currentUser.username)) || 'User';
        greeting.textContent = `Hi ${name}, your access to the SQL assistant is currently restricted.`;
        box.appendChild(greeting);

        const reason = document.createElement('p');
        reason.style.cssText = 'color:#495057;background:#f8f9fa;border:1px solid #dee2e6;border-radius:5px;padding:15px;font-size:14px;';
        reason.textContent = safeMessage || 'Please contact an administrator.';
        box.appendChild(reason);

        if (referenceId) {
            const ref = document.createElement('p');
            ref.style.cssText = 'color:#6c757d;font-size:12px;text-align:center;';
            ref.textContent = `Reference: ${referenceId}`;
            box.appendChild(ref);
        }

        const btnRow = document.createElement('div');
        btnRow.style.cssText = 'text-align:center;margin-top:10px;';
        const returnBtn = document.createElement('button');
        returnBtn.type = 'button';
        returnBtn.textContent = 'Return';
        returnBtn.style.cssText = 'background:#dc3545;color:#fff;border:none;padding:12px 30px;border-radius:5px;font-size:16px;font-weight:bold;cursor:pointer;';
        btnRow.appendChild(returnBtn);
        box.appendChild(btnRow);
        modal.appendChild(box);
        document.body.appendChild(modal);

        function close() {
            document.removeEventListener('keydown', onKey, true);
            modal.remove();
            blockingModalOpen = false;
            if (opener && typeof opener.focus === 'function') opener.focus();
            const target = currentUser && currentUser.role === 'admin' ? '/home' : '/dashboard';
            window.location.replace(target);
        }

        // ONE consistent policy: the button, the backdrop AND Escape all close
        // (and redirect). Focus is trapped on the single actionable button.
        function onKey(e) {
            if (e.key === 'Escape') { e.preventDefault(); close(); }
            else if (e.key === 'Tab') { e.preventDefault(); returnBtn.focus(); }
        }
        document.addEventListener('keydown', onKey, true);
        returnBtn.addEventListener('click', close);
        modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
        returnBtn.focus();
    }

    // ------------------------------------------------------------------
    // Chat messages (safe DOM; bounded)
    // ------------------------------------------------------------------
    function pruneMessages(container) {
        const messages = container.querySelectorAll('.chat-message');
        for (let i = 0; i < messages.length - MAX_DOM_MESSAGES; i++) messages[i].remove();
    }

    function addMessage(type, content, isReport = false) {
        const messagesContainer = document.getElementById('chatMessages');
        if (!messagesContainer) return null;

        const messageDiv = document.createElement('div');
        messageDiv.className = `chat-message ${type === 'user' ? 'user' : 'assistant'}`;
        const messageInner = document.createElement('div');
        messageInner.className = 'message-inner';

        const avatar = document.createElement('div');
        avatar.className = `message-avatar ${type === 'user' ? 'user' : 'assistant'}`;
        const avatarIcon = document.createElement('i');
        avatarIcon.className = type === 'user' ? 'fas fa-user' : 'fas fa-robot';
        avatar.appendChild(avatarIcon);

        const messageContent = document.createElement('div');
        messageContent.className = isReport ? 'message-content report-message'
            : `message-content ${type === 'user' ? 'user' : 'assistant'}`;
        // Bidi: an Arabic report rendered in an LTR container displays its
        // words in scrambled order. dir="auto" + the unicode-bidi:plaintext
        // rules in tracking.css make every paragraph resolve its own base
        // direction, so Arabic prose flows RTL while camera names, numbers
        // and English lines stay readable inside it.
        messageContent.setAttribute('dir', 'auto');

        const messageText = document.createElement('div');
        messageText.className = 'message-text';
        if (type === 'user') {
            // plain text with line breaks — no markdown for user input
            content.split('\n').forEach((lineText, idx) => {
                if (idx > 0) messageText.appendChild(document.createElement('br'));
                messageText.appendChild(document.createTextNode(lineText));
            });
        } else if (content) {
            renderInto(messageText, content);
        }

        messageContent.appendChild(messageText);
        messageInner.appendChild(avatar);
        messageInner.appendChild(messageContent);
        messageDiv.appendChild(messageInner);
        messagesContainer.appendChild(messageDiv);
        pruneMessages(messagesContainer);
        trackTimeout(() => messagesContainer.scrollTo({ top: messagesContainer.scrollHeight, behavior: 'smooth' }), 60);
        return messageText;
    }

    function scrollToBottom(instant = false) {
        const c = document.getElementById('chatMessages');
        if (c) c.scrollTo({ top: c.scrollHeight, behavior: instant ? 'auto' : 'smooth' });
    }

    // ------------------------------------------------------------------
    // Request lifecycle (explicit state machine)
    // ------------------------------------------------------------------
    // states: idle -> connecting -> streaming -> (stopping) ->
    //         completed | failed | cancelled | interrupted
    function createRequest(query, conversationId = null) {
        const id = newRequestId();
        // bound the map (prune oldest terminal entries)
        while (activeRequests.size >= MAX_ACTIVE_REQUESTS) {
            const oldest = activeRequests.keys().next().value;
            activeRequests.delete(oldest);
        }
        const req = {
            id,
            query,
            conversationId,
            state: 'connecting',
            transport: null,
            lastSequence: 0,
            raw: '',
            controller: new AbortController(),
            finalized: false,
            responseEl: null,
            typingEl: null,
            lastEventAt: Date.now(),
        };
        activeRequests.set(id, req);
        return req;
    }

    /**
     * Append a download link for a document generated during a streamed turn.
     * Same rule as the conversation renderer: the id is the only thing taken
     * from the server, and the URL is rebuilt from it locally.
     */
    function appendArtifactLink(req, artifact) {
        try {
            const id = String((artifact && artifact.artifact_id) || '');
            if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) return;
            const host = req && req.responseEl ? req.responseEl : null;
            if (!host) return;
            const wrap = document.createElement('div');
            wrap.className = 'block-artifact';
            const link = document.createElement('a');
            link.className = 'artifact-download';
            link.href = '/api/sql-agent/artifacts/' + encodeURIComponent(id);
            link.rel = 'noopener noreferrer';
            link.setAttribute('download', '');
            link.textContent = String((artifact && artifact.title) || 'Report')
                + '  (' + String((artifact && artifact.artifact_type) || 'file').toUpperCase() + ')';
            wrap.appendChild(link);
            host.appendChild(wrap);
        } catch (e) {
            /* a missing link must never break the stream */
        }
    }

    function appendRawContent(req, chunk) {
        if (!chunk) return;
        if (req.raw.length + chunk.length > MAX_RAW_RESPONSE_CHARS) {
            chunk = chunk.slice(0, Math.max(0, MAX_RAW_RESPONSE_CHARS - req.raw.length));
        }
        req.raw += chunk;
        if (req.responseEl) {
            req.responseEl.textContent = req.raw; // plain text while streaming
            scrollToBottom(true);
        }
    }

    /** Runs EXACTLY once per request; the only place UI state is restored. */
    function finalizeRequest(requestId, outcome, detail = {}) {
        const req = activeRequests.get(requestId);
        if (!req || req.finalized) return;
        req.finalized = true;
        req.state = outcome;

        if (req.patienceTimer) { clearTimeout(req.patienceTimer); req.patienceTimer = null; }
        if (req.typingEl) { req.typingEl.remove(); req.typingEl = null; req.typingLabelEl = null; }
        try { req.controller.abort(); } catch (e) { /* ignore */ }

        if (req.responseEl) {
            if (outcome === 'completed' || (outcome === 'cancelled' && req.raw)) {
                renderInto(req.responseEl, req.raw);
                if (outcome === 'cancelled') {
                    const note = document.createElement('div');
                    note.className = 'stopped-note';
                    note.textContent = '■ Generation stopped';
                    req.responseEl.appendChild(note);
                }
                if (req.raw.trim()) addResponseTools(req);
                if (req.artifact) appendArtifactLink(req, req.artifact);
            } else if (outcome === 'cancelled') {
                req.responseEl.textContent = 'Generation stopped.';
            } else if (outcome === 'interrupted') {
                renderInto(req.responseEl, req.raw);
                const note = document.createElement('div');
                note.className = 'stopped-note';
                note.textContent = '⚠ Response incomplete — the connection ended before completion.';
                req.responseEl.appendChild(note);
            } else if (outcome === 'failed') {
                req.responseEl.textContent = detail.message || 'The query failed. Please try again.';
            }
        }

        if (activeRequestId === requestId) activeRequestId = null;
        setInputEnabled(true);
        setSendButtonMode('send');
        updateConnectionStatus('connected', 'Ready');
        scrollToBottom();

        // History refresh: once now, once shortly after (covers commit lag) —
        // no blind fixed-delay guessing loop.
        refreshHistory();
        trackTimeout(refreshHistory, 2000);
    }

    // ------------------------------------------------------------------
    // Send / Stop — ONE authoritative handler
    // ------------------------------------------------------------------
    function setInputEnabled(enabled) {
        const input = document.getElementById('chatInput');
        if (input) {
            input.disabled = !enabled;
            if (enabled) input.focus();
        }
    }

    function setSendButtonMode(mode) {
        const btn = document.getElementById('sendBtn');
        if (!btn) return;
        btn.disabled = false;
        btn.replaceChildren();
        const icon = document.createElement('i');
        if (mode === 'stop') {
            btn.classList.add('stop-mode');
            icon.className = 'fas fa-stop';
            btn.title = 'Stop generating';
        } else {
            btn.classList.remove('stop-mode');
            icon.className = 'fas fa-arrow-up';
            btn.title = 'Send message';
        }
        btn.appendChild(icon);
    }

    async function handleSendOrStop() {
        const req = activeRequestId ? activeRequests.get(activeRequestId) : null;
        if (req && (req.state === 'connecting' || req.state === 'streaming')) {
            await stopRequest(req);
            return;
        }
        if (req && req.state === 'stopping') return; // stop already in flight
        await startQuery();
    }

    async function stopRequest(req) {
        req.state = 'stopping';
        setSendButtonMode('send');
        updateConnectionStatus('connecting', 'Stopping…');
        // REAL server-side cancellation: stops LLM + SQL work, not just the pipe
        if (req.transport === 'websocket') {
            wsManager.sendCancel(req.id);
        }
        api(`/api/sql-agent/requests/${encodeURIComponent(req.id)}/cancel`, { method: 'POST' })
            .catch(() => { /* local abort below still stops the stream */ });
        // Local abort ends the SSE read; finalize as cancelled (not failed)
        try { req.controller.abort(); } catch (e) { /* ignore */ }
        trackTimeout(() => finalizeRequest(req.id, 'cancelled'), 150);
    }

    async function startQuery() {
        const input = document.getElementById('chatInput');
        const messagesContainer = document.getElementById('chatMessages');
        if (!input || !messagesContainer) return;
        if (activeRequestId) return; // double-submit guard (state, not luck)

        const query = input.value.trim();
        if (!query) return;

        window.lastQuery = query;
        const welcomeMsg = document.getElementById('welcomeMessage');
        if (welcomeMsg) welcomeMsg.classList.add('hidden');

        // Send INTO the selected conversation. In a fresh chat, create one
        // first (title = the question), so "new chat" genuinely means a new
        // conversation instead of everything piling into one session thread.
        // A null id is tolerated: the server then files the exchange by
        // session — degraded placement, never a lost message.
        let conversationId = null;
        if (window.conversationsPanel) {
            try {
                conversationId = await window.conversationsPanel.ensureActiveConversation(query);
            } catch (e) { conversationId = null; }
        }

        addMessage('user', query);
        input.value = '';
        if (input.tagName === 'TEXTAREA') input.style.height = 'auto';
        setInputEnabled(false);
        setSendButtonMode('stop');
        updateConnectionStatus('connecting', 'Thinking…');

        const req = createRequest(query, conversationId);
        activeRequestId = req.id;

        // Typing indicator + response container with PER-REQUEST ids.
        // The status text is VISIBLE (ChatGPT-style "Thinking…"), not only an
        // aria-label: the pipeline runs several LLM steps before the first
        // content token, and a silent row of dots reads as "hung".
        const typing = document.createElement('div');
        typing.className = 'typing-indicator';
        typing.id = `typing-${req.id}`;
        for (let i = 0; i < 3; i++) typing.appendChild(document.createElement('span'));
        const typingLabel = document.createElement('em');
        typingLabel.className = 'typing-label';
        typingLabel.textContent = 'Thinking…';
        typing.appendChild(typingLabel);
        messagesContainer.appendChild(typing);
        req.typingEl = typing;
        req.typingLabelEl = typingLabel;
        // Long pre-content waits get an honest patience message instead of the
        // same three dots. Cleared the moment content arrives or the request
        // finalizes (finalizeRequest removes typingEl, so the guard suffices).
        req.patienceTimer = window.setTimeout(() => {
            if (req.typingLabelEl && req.typingEl && !req.finalized) {
                req.typingLabelEl.textContent =
                    'Please be patient — we are processing your query…';
            }
        }, 15000);

        const responseEl = addMessage('assistant', '', true);
        if (responseEl) responseEl.id = `response-${req.id}`;
        req.responseEl = responseEl;
        scrollToBottom(true);

        // Transport manager: WS only when explicitly enabled; SSE preferred;
        // REST only if SSE was provably NOT accepted (idempotent request_id
        // means even a race cannot execute the query twice server-side).
        const preferWs = localStorage.getItem('sqlAgentTransport') === 'websocket';
        try {
            if (preferWs && await wsManager.ensureConnected()) {
                req.transport = 'websocket';
                wsManager.sendQuery(req);
            } else {
                await runSSE(req);
            }
        } catch (e) {
            if (!req.finalized) finalizeRequest(req.id, 'failed', { message: 'Could not reach the assistant.' });
        }
    }

    // ------------------------------------------------------------------
    // Server event handling (shared by SSE + WS; correlated + de-duplicated)
    // ------------------------------------------------------------------
    function handleServerEvent(req, data) {
        if (!data || typeof data !== 'object') return;
        // Correlation: ignore events for other/stale requests
        if (data.request_id && data.request_id !== req.id) return;
        if (req.finalized) return;
        // Sequence: drop duplicated/out-of-order events
        if (typeof data.sequence === 'number') {
            if (data.sequence <= req.lastSequence) return;
            req.lastSequence = data.sequence;
        }
        req.lastEventAt = Date.now();

        switch (data.type) {
            case 'status': {
                // Surface the pipeline stage visibly, ChatGPT-style, instead
                // of hiding it in an aria-label nobody sees.
                const msg = String(data.message || '');
                if (req.typingEl) req.typingEl.setAttribute('aria-label', msg);
                if (req.typingLabelEl && msg) req.typingLabelEl.textContent = msg;
                req.state = 'streaming';
                break;
            }
            case 'heartbeat':
                break; // liveness only
            case 'content':
                req.state = 'streaming';
                if (req.patienceTimer) { clearTimeout(req.patienceTimer); req.patienceTimer = null; }
                if (req.typingEl) { req.typingEl.remove(); req.typingEl = null; req.typingLabelEl = null; }
                appendRawContent(req, typeof data.content === 'string' ? data.content : '');
                break;
            case 'complete': {
                // Stash it; the link is appended in finalizeRequest, which
                // re-renders responseEl from req.raw and would otherwise
                // wipe anything added here.
                if (data.artifact) req.artifact = data.artifact;
                // The complete event may carry the authoritative full response
                if (typeof data.response === 'string' && data.response.length > req.raw.length) {
                    req.raw = data.response.slice(0, MAX_RAW_RESPONSE_CHARS);
                }
                // Success requires BOTH the server's verdict and actual content.
                //
                // This was `req.raw.trim() || data.success !== false ? ... : ...`,
                // where `||` binds tighter than `?:` — so it read
                // `(raw || success !== false) ? 'completed' : 'failed'`, and any
                // partial content marked the request completed even when the
                // server had explicitly reported success:false. Requiring content
                // as well also stops an empty assistant message being rendered
                // for a success:true response that streamed nothing.
                const serverSaysOk = data.success !== false;
                const hasContent = req.raw.trim().length > 0;
                finalizeRequest(req.id, serverSaysOk && hasContent ? 'completed' : 'failed');
                break;
            }
            case 'cancelled':
                finalizeRequest(req.id, 'cancelled');
                break;
            case 'error': {
                const code = data.error_code || null;
                if (code === 'ACCOUNT_BLOCKED') {
                    finalizeRequest(req.id, 'failed', { message: 'Access restricted.' });
                    showBlockingModal(String(data.message || ''), data.reference_id);
                } else if (code === 'AUTHORIZATION_CHANGED') {
                    // An administrator changed this account's access mid-stream.
                    // The server has already closed the stream; the cached
                    // navigation privileges are now stale, so drop them rather
                    // than leaving a menu the backend will refuse.
                    finalizeRequest(req.id, 'failed', {
                        message: 'Your access to the assistant has changed. Please sign in again.',
                    });
                    try {
                        sessionStorage.removeItem('navbar_privileges');
                    } catch (e) { /* storage unavailable — the redirect still corrects it */ }
                    showNotice('Your access has changed. Please sign in again.', 'error');
                } else {
                    finalizeRequest(req.id, 'failed', {
                        message: typeof data.message === 'string' ? data.message : 'The query failed.',
                    });
                }
                break;
            }
            default:
                break;
        }
    }

    // ------------------------------------------------------------------
    // SSE transport (standards-compliant parser)
    // ------------------------------------------------------------------
    async function runSSE(req) {
        req.transport = 'sse';
        let accepted = false;
        let response;
        try {
            response = await fetch('/api/sql-agent/query/stream', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'text/event-stream',
                    'X-Requested-With': 'XMLHttpRequest',
                },
                credentials: 'include',
                cache: 'no-store',
                body: JSON.stringify({ query: req.query, request_id: req.id, session_id: currentSessionId, conversation_id: req.conversationId || undefined }),
                signal: req.controller.signal,
            });
        } catch (e) {
            if (e && e.name === 'AbortError') { finalizeRequest(req.id, 'cancelled'); return; }
            // Connection never established => provably not accepted => REST
            // fallback with the SAME request_id (idempotency key).
            await runRESTFallback(req);
            return;
        }

        if (!response.ok) {
            const payload = await parseBody(response);
            const code = extractErrorCode(payload);
            if (response.status === 403 && code === 'ACCOUNT_BLOCKED') {
                finalizeRequest(req.id, 'failed', { message: 'Access restricted.' });
                showBlockingModal(payload.error.message, payload.error.reference_id);
                return;
            }
            finalizeRequest(req.id, 'failed', { message: errorMessage({ payload }, `Server error (${response.status})`) });
            return;
        }
        accepted = true;
        req.state = 'streaming';
        updateConnectionStatus('connected', 'Generating…');

        // ---- Standards-compliant SSE parsing -------------------------------
        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        let terminalSeen = false;

        const dispatchBlock = (block) => {
            // Per the SSE spec: join all data: lines; ignore comments (:)
            const dataLines = [];
            for (const rawLine of block.split(/\r\n|\n|\r/)) {
                if (!rawLine || rawLine.startsWith(':')) continue;      // comment/heartbeat line
                const colon = rawLine.indexOf(':');
                const field = colon === -1 ? rawLine : rawLine.slice(0, colon);
                let value = colon === -1 ? '' : rawLine.slice(colon + 1);
                if (value.startsWith(' ')) value = value.slice(1);
                if (field === 'data') dataLines.push(value);
                // event:/id:/retry: accepted and currently unused
            }
            if (!dataLines.length) return;
            const dataStr = dataLines.join('\n');
            if (dataStr.length > MAX_SSE_EVENT_CHARS) return;           // oversized event: drop safely
            let data;
            try { data = JSON.parse(dataStr); } catch (e) { return; }    // malformed: drop safely
            if (data && ['complete', 'error', 'cancelled'].includes(data.type)) terminalSeen = true;
            handleServerEvent(req, data);
        };

        const processBuffer = (final = false) => {
            // Events are delimited by a blank line (\n\n or \r\n\r\n)
            let idx;
            while ((idx = buffer.search(/\r\n\r\n|\n\n|\r\r/)) !== -1) {
                const sep = buffer.match(/\r\n\r\n|\n\n|\r\r/)[0];
                const block = buffer.slice(0, idx);
                buffer = buffer.slice(idx + sep.length);
                dispatchBlock(block);
                if (req.finalized) return;
            }
            if (final && buffer.trim()) { dispatchBlock(buffer); buffer = ''; }
            if (buffer.length > MAX_SSE_EVENT_CHARS * 2) buffer = '';    // runaway buffer guard
        };

        try {
            while (true) {
                // Idle timeout: heartbeats arrive every ~20s; a long silent gap
                // means the stream is dead even if TCP hasn't noticed.
                const readPromise = reader.read();
                const idleTimer = new Promise(resolve =>
                    trackTimeout(() => resolve({ idle: true }), STREAM_IDLE_TIMEOUT_MS));
                const result = await Promise.race([readPromise, idleTimer]);
                if (result.idle) {
                    try { req.controller.abort(); } catch (e) { /* ignore */ }
                    finalizeRequest(req.id, 'interrupted');
                    return;
                }
                const { done, value } = result;
                if (done) {
                    buffer += decoder.decode();     // final decoder flush
                    processBuffer(true);
                    break;
                }
                buffer += decoder.decode(value, { stream: true });
                processBuffer();
                if (req.finalized) return;
            }
        } catch (error) {
            if (error && error.name === 'AbortError') {
                finalizeRequest(req.id, 'cancelled');
                return;
            }
            finalizeRequest(req.id, 'interrupted');
            return;
        } finally {
            try { reader.releaseLock(); } catch (e) { /* ignore */ }
        }

        // Explicit terminal event REQUIRED — never inferred from length
        if (!req.finalized) {
            finalizeRequest(req.id, terminalSeen ? 'completed' : 'interrupted');
        }
    }

    // ------------------------------------------------------------------
    // REST fallback (only when SSE was provably not accepted)
    // ------------------------------------------------------------------
    async function runRESTFallback(req) {
        req.transport = 'rest';
        const result = await api('/api/sql-agent/query', {
            method: 'POST',
            body: { query: req.query, request_id: req.id, session_id: currentSessionId },
            controller: req.controller,
            timeoutMs: 600_000, // CPU LLM inference is slow; bounded, not infinite
        });
        if (req.finalized) return;
        if (result.errorCode === 'ABORTED') { finalizeRequest(req.id, 'cancelled'); return; }
        if (result.ok && result.payload && result.payload.success && result.payload.response) {
            req.raw = String(result.payload.response).slice(0, MAX_RAW_RESPONSE_CHARS);
            finalizeRequest(req.id, 'completed');
            return;
        }
        if (result.status === 403 && result.errorCode === 'ACCOUNT_BLOCKED') {
            finalizeRequest(req.id, 'failed', { message: 'Access restricted.' });
            showBlockingModal(result.payload.error.message, result.payload.error.reference_id);
            return;
        }
        finalizeRequest(req.id, 'failed', { message: errorMessage(result, 'The query failed. Please try again.') });
    }

    // ------------------------------------------------------------------
    // WebSocket transport (opt-in) — managed lifecycle, single dispatcher
    // ------------------------------------------------------------------
    const wsManager = {
        socket: null,
        connectPromise: null,
        intentionalClose: false,
        attempts: 0,

        async ensureConnected() {
            if (this.socket && this.socket.readyState === WebSocket.OPEN) return true;
            if (this.socket && this.socket.readyState === WebSocket.CONNECTING && this.connectPromise) {
                return this.connectPromise; // never open a duplicate while CONNECTING
            }
            this.connectPromise = new Promise((resolve) => {
                let socket;
                try {
                    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                    socket = new WebSocket(`${protocol}//${window.location.host}/ws/sql-agent`);
                } catch (e) { resolve(false); return; }
                this.socket = socket;
                const failTimer = trackTimeout(() => { try { socket.close(); } catch (e) { } resolve(false); }, 8000);
                socket.onopen = () => { clearTimeout(failTimer); this.attempts = 0; resolve(true); };
                socket.onerror = () => { /* onclose follows */ };
                socket.onclose = (ev) => {
                    clearTimeout(failTimer);
                    this.socket = null;
                    resolve(false);
                    if (ev.code === 1008) return;              // auth failure: do not retry
                    if (this.intentionalClose || destroyed) return;
                };
                // ONE permanent dispatcher keyed by request_id — never one
                // listener per query
                socket.onmessage = (event) => {
                    let data;
                    try { data = JSON.parse(event.data); } catch (e) { return; }
                    const rid = data && data.request_id;
                    const req = rid ? activeRequests.get(rid) : null;
                    if (req) handleServerEvent(req, data);
                };
            }).finally(() => { this.connectPromise = null; });
            return this.connectPromise;
        },

        sendQuery(req) {
            if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
                runSSE(req);
                return;
            }
            req.state = 'streaming';
            this.socket.send(JSON.stringify({
                type: 'query', request_id: req.id, session_id: currentSessionId, query: req.query, conversation_id: req.conversationId || undefined,
            }));
            // WS idle watchdog
            const watchdog = () => {
                if (destroyed || req.finalized) return;
                if (Date.now() - req.lastEventAt > STREAM_IDLE_TIMEOUT_MS) {
                    finalizeRequest(req.id, 'interrupted');
                    return;
                }
                trackTimeout(watchdog, 10_000);
            };
            trackTimeout(watchdog, 10_000);
        },

        sendCancel(requestId) {
            // Cancel ONE request — never close the shared socket to stop it
            if (this.socket && this.socket.readyState === WebSocket.OPEN) {
                try { this.socket.send(JSON.stringify({ type: 'cancel', request_id: requestId })); }
                catch (e) { /* REST cancel endpoint is the backup */ }
            }
        },

        shutdown() {
            this.intentionalClose = true;
            if (this.socket) { try { this.socket.close(1000); } catch (e) { } this.socket = null; }
        },
    };

    // ------------------------------------------------------------------
    // Response tools (copy / retry / export) — raw markdown, never DOM scrape
    // ------------------------------------------------------------------
    function addResponseTools(req) {
        const messageDiv = req.responseEl && req.responseEl.closest('.chat-message');
        if (!messageDiv) return;
        const messageContent = messageDiv.querySelector('.message-content');
        if (!messageContent || messageContent.querySelector('.export-buttons')) return;

        const raw = req.raw;
        const bar = document.createElement('div');
        bar.className = 'export-buttons';
        bar.style.cssText = 'margin-top:0.75rem;display:flex;gap:0.5rem;';

        const makeBtn = (cls, iconCls, label, title) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = `export-btn ${cls}`;
            b.title = title;
            const i = document.createElement('i');
            i.className = `fas ${iconCls}`;
            b.appendChild(i);
            b.appendChild(document.createTextNode(' ' + label));
            return b;
        };

        const copyBtn = makeBtn('copy-btn', 'fa-copy', 'COPY', 'Copy response');
        copyBtn.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(raw);
                copyBtn.replaceChildren(document.createTextNode('✓ COPIED'));
                trackTimeout(() => {
                    copyBtn.replaceChildren();
                    const i = document.createElement('i'); i.className = 'fas fa-copy';
                    copyBtn.appendChild(i); copyBtn.appendChild(document.createTextNode(' COPY'));
                }, 1500);
            } catch (e) { showNotice('Copy failed', 'error'); }
        });

        const retryBtn = makeBtn('regen-btn', 'fa-rotate-right', 'RETRY', 'Regenerate response');
        retryBtn.addEventListener('click', () => {
            const input = document.getElementById('chatInput');
            if (input && !input.disabled && req.query) {
                input.value = req.query;
                handleSendOrStop();
            }
        });

        const pdfBtn = makeBtn('export-pdf', 'fa-file-pdf', 'PDF', 'Export to PDF');
        pdfBtn.addEventListener('click', () => exportReport('pdf', raw, req.query, pdfBtn));
        const wordBtn = makeBtn('export-word', 'fa-file-word', 'WORD', 'Export to Word');
        wordBtn.addEventListener('click', () => exportReport('word', raw, req.query, wordBtn));

        bar.appendChild(copyBtn);
        bar.appendChild(retryBtn);
        bar.appendChild(pdfBtn);
        bar.appendChild(wordBtn);
        messageContent.appendChild(bar);
    }

    const exportsInFlight = new Set();
    async function exportReport(format, rawContent, title, btn) {
        const key = `${format}`;
        if (exportsInFlight.has(key)) return;
        exportsInFlight.add(key);
        if (btn) btn.disabled = true;
        let url = null;
        try {
            const resp = await fetch(`/api/sql-agent/export/${format}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                },
                credentials: 'include',
                cache: 'no-store',
                body: JSON.stringify({
                    content: rawContent,
                    title: (title || 'Intelligence Report').slice(0, 200),
                    timestamp: new Date().toISOString(),
                }),
            });
            if (!resp.ok) {
                const payload = await parseBody(resp);
                showNotice(`Export failed: ${errorMessage({ payload }, `HTTP ${resp.status}`)}`, 'error');
                return;
            }
            const ctype = resp.headers.get('content-type') || '';
            if (!/(pdf|officedocument)/.test(ctype)) {
                showNotice('Export failed: unexpected file type returned', 'error');
                return;
            }
            const blob = await resp.blob();
            url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `Intelligence_Report_${new Date().toISOString().split('T')[0]}.${format === 'pdf' ? 'pdf' : 'docx'}`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            showNotice(`${format.toUpperCase()} exported`, 'success');
        } catch (e) {
            showNotice('Export failed: network error', 'error');
        } finally {
            if (url) window.URL.revokeObjectURL(url);
            exportsInFlight.delete(key);
            if (btn) btn.disabled = false;
        }
    }

    // ------------------------------------------------------------------
    // Health
    // ------------------------------------------------------------------
    async function checkSQLAgentHealth() {
        const result = await api('/api/sql-agent/health');
        if (result.ok && result.payload && result.payload.status === 'operational') {
            updateConnectionStatus('connected', 'Ready');
        } else if (result.ok && result.payload) {
            updateConnectionStatus('disconnected', 'Assistant degraded');
        } else {
            updateConnectionStatus('disconnected', 'Health check failed');
        }
    }

    // ------------------------------------------------------------------
    // History sidebar (server pagination; owner-scoped server-side)
    // ------------------------------------------------------------------
    let historyPage = 1;
    let historyHasMore = false;
    let historyController = null;
    let loadedHistoryQueryId = null;
    const historyItemNodes = new Map(); // id -> node (no string selectors on ids)

    async function refreshHistory() {
        // The conversations module (conversations.js) owns the sidebar when
        // loaded: it reads the /api/v1 conversation domain (pinned/recent,
        // search, branches). The flat legacy list below remains ONLY as the
        // fallback if that module failed to load — never both at once.
        if (window.conversationsPanel && typeof window.conversationsPanel.refresh === 'function') {
            window.conversationsPanel.refresh();
            return;
        }
        await loadQueryHistory(1, false);
    }

    async function loadQueryHistory(page = 1, append = false) {
        const loadingEl = document.getElementById('historyLoading');
        const emptyEl = document.getElementById('historyEmpty');
        const listEl = document.getElementById('historyList');
        if (!listEl) return;

        // Abort the older refresh when a newer one begins
        if (historyController) { try { historyController.abort(); } catch (e) { } }
        historyController = new AbortController();

        if (!append && loadingEl && !listEl.childElementCount) loadingEl.style.display = 'flex';

        const result = await api(`/api/sql-agent/history?page=${page}&page_size=${HISTORY_PAGE_SIZE}`,
            { controller: historyController });
        if (loadingEl) loadingEl.style.display = 'none';
        if (!result.ok || !result.payload || !result.payload.success) {
            if (result.errorCode === 'ABORTED') return;
            if (emptyEl && !listEl.childElementCount) {
                emptyEl.replaceChildren(document.createTextNode('Failed to load history'));
                emptyEl.style.display = 'flex';
            }
            return;
        }

        historyPage = page;
        historyHasMore = !!result.payload.has_more;
        const items = result.payload.history || [];

        if (!append) {
            listEl.replaceChildren();
            historyItemNodes.clear();
        }
        if (!items.length && !listEl.childElementCount) {
            if (emptyEl) emptyEl.style.display = 'flex';
            listEl.style.display = 'none';
            return;
        }
        if (emptyEl) emptyEl.style.display = 'none';
        listEl.style.display = 'block';

        items.forEach(item => listEl.appendChild(buildHistoryItem(item)));
        renderLoadMore(listEl);
    }

    function renderLoadMore(listEl) {
        let btn = document.getElementById('historyLoadMoreBtn');
        if (historyHasMore) {
            if (!btn) {
                btn = document.createElement('button');
                btn.id = 'historyLoadMoreBtn';
                btn.type = 'button';
                btn.className = 'new-chat-btn';
                btn.style.cssText = 'margin:0.5rem auto;display:block;';
                btn.textContent = 'Load more';
                btn.addEventListener('click', () => loadQueryHistory(historyPage + 1, true));
            }
            listEl.appendChild(btn); // keep at bottom
        } else if (btn) {
            btn.remove();
        }
    }

    function buildHistoryItem(item) {
        const node = document.createElement('div');
        node.className = 'history-item';
        node.dataset.queryId = String(item.id);
        historyItemNodes.set(String(item.id), node);
        if (String(item.id) === String(loadedHistoryQueryId)) node.classList.add('active');

        const queryDiv = document.createElement('div');
        queryDiv.className = 'history-item-query';
        queryDiv.textContent = item.query || '';
        node.appendChild(queryDiv);

        const delBtn = document.createElement('button');
        delBtn.className = 'history-item-delete';
        delBtn.type = 'button';
        delBtn.title = 'Delete from history';
        delBtn.setAttribute('aria-label', 'Delete from history');
        const delIcon = document.createElement('i');
        delIcon.className = 'fas fa-trash-can';
        delBtn.appendChild(delIcon);
        node.appendChild(delBtn);

        const meta = document.createElement('div');
        meta.className = 'history-item-meta';
        const time = document.createElement('div');
        time.className = 'history-item-time';
        time.textContent = formatTime(item.timestamp);
        meta.appendChild(time);
        const status = document.createElement('div');
        status.className = `history-item-status ${item.success ? 'success' : 'error'}`;
        const statusIcon = document.createElement('i');
        statusIcon.className = `fas fa-${item.success ? 'check-circle' : 'exclamation-circle'}`;
        status.appendChild(statusIcon);
        meta.appendChild(status);
        node.appendChild(meta);

        node.addEventListener('click', () => loadHistoryItem(item));
        delBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            if (!confirm('Delete this conversation from your history?')) return;
            delBtn.disabled = true;
            const result = await api(`/api/sql-agent/history/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
            if (result.ok) {
                node.remove();
                historyItemNodes.delete(String(item.id));
            } else {
                delBtn.disabled = false;
                showNotice(`Delete failed: ${errorMessage(result)}`, 'error');
            }
        });
        return node;
    }

    async function loadHistoryItem(item) {
        historyItemNodes.forEach(n => n.classList.remove('active'));
        const node = historyItemNodes.get(String(item.id));
        if (node) node.classList.add('active');
        loadedHistoryQueryId = item.id;
        currentSessionId = item.session_id;

        const result = await api(`/api/sql-agent/history/${encodeURIComponent(item.id)}`);
        if (!result.ok || !result.payload || !result.payload.success || !result.payload.query) {
            showNotice('Failed to load conversation', 'error');
            return;
        }
        const q = result.payload.query;
        const messagesContainer = document.getElementById('chatMessages');
        if (!messagesContainer) return;
        const welcomeMsg = document.getElementById('welcomeMessage');
        messagesContainer.replaceChildren();
        if (welcomeMsg) { messagesContainer.appendChild(welcomeMsg); welcomeMsg.classList.add('hidden'); }

        addMessage('user', q.query || '');
        if (q.response) addMessage('assistant', q.response, true);
        else if (q.error_message) addMessage('assistant', 'This query did not complete successfully.', false);
        scrollToBottom(true);
        if (window.innerWidth <= 768) toggleSidebar();
    }

    // ------------------------------------------------------------------
    // Sidebar (idempotent init, ARIA)
    // ------------------------------------------------------------------
    function toggleSidebar() {
        const sidebar = document.getElementById('historySidebar');
        const toggleBtn = document.getElementById('sidebarToggleBtn');
        if (!sidebar) return;
        const isOpening = !sidebar.classList.contains('open');
        sidebar.classList.toggle('open');
        sidebar.setAttribute('aria-hidden', isOpening ? 'false' : 'true');
        try { localStorage.setItem('tracking_sidebar_open', String(isOpening)); } catch (e) { }
        if (toggleBtn) {
            toggleBtn.style.display = isOpening ? 'none' : 'flex';
            toggleBtn.setAttribute('aria-expanded', String(isOpening));
        }
    }

    function startNewChat() {
        if (window.conversationsPanel && window.conversationsPanel.clearActive) {
            window.conversationsPanel.clearActive();
        }
        const messagesContainer = document.getElementById('chatMessages');
        if (messagesContainer) {
            const welcomeMsg = document.getElementById('welcomeMessage');
            messagesContainer.replaceChildren();
            if (welcomeMsg) {
                messagesContainer.appendChild(welcomeMsg);
                welcomeMsg.classList.remove('hidden');
            }
        }
        currentSessionId = null;
        loadedHistoryQueryId = null;
        historyItemNodes.forEach(n => n.classList.remove('active'));
        if (window.innerWidth <= 768) toggleSidebar();
    }

    // ------------------------------------------------------------------
    // Instructions helpers (kept; safe)
    // ------------------------------------------------------------------
    function toggleInstructions() {
        const welcomeMsg = document.getElementById('welcomeMessage');
        const instructionsBtn = document.getElementById('showInstructionsBtn');
        if (!welcomeMsg || !instructionsBtn) return;
        const hidden = welcomeMsg.classList.toggle('hidden');
        instructionsBtn.replaceChildren();
        const i = document.createElement('i');
        i.className = hidden ? 'fas fa-circle-question' : 'fas fa-times-circle';
        instructionsBtn.appendChild(i);
        const span = document.createElement('span');
        span.textContent = hidden ? 'Help' : 'Hide';
        instructionsBtn.appendChild(span);
        if (!hidden) welcomeMsg.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function useExampleQuery(text) {
        const input = document.getElementById('chatInput');
        if (input) {
            input.value = (text || '').replace(/^["']|["']$/g, '').trim();
            input.focus();
        }
    }

    // ------------------------------------------------------------------
    // Initialization (exactly once — duplicate paths guarded)
    // ------------------------------------------------------------------
    let initialized = false;

    function setupChatInterface() {
        const chatInput = document.getElementById('chatInput');
        const sendBtn = document.getElementById('sendBtn');
        if (!chatInput || !sendBtn) return;
        if (sendBtn.dataset.listenerAttached === 'true') return; // idempotent
        sendBtn.dataset.listenerAttached = 'true';

        // ONE authoritative handler: Send when idle, Stop while generating
        sendBtn.addEventListener('click', (e) => {
            e.preventDefault();
            handleSendOrStop();
        });

        // keydown (keypress is deprecated); Enter sends via the SAME handler,
        // Shift+Enter inserts a newline
        chatInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSendOrStop();
            }
        });

        chatInput.addEventListener('input', () => {
            if (chatInput.tagName === 'TEXTAREA') {
                chatInput.style.height = 'auto';
                chatInput.style.height = Math.min(chatInput.scrollHeight, 200) + 'px';
            }
        });
    }

    function setupSidebar() {
        const toggleBtn = document.getElementById('sidebarToggleBtn');
        const closeBtn = document.getElementById('sidebarCloseBtn');
        const newChatBtn = document.getElementById('newChatBtn');
        const sidebar = document.getElementById('historySidebar');
        if (toggleBtn && toggleBtn.dataset.listenerAttached !== 'true') {
            toggleBtn.dataset.listenerAttached = 'true';
            toggleBtn.setAttribute('aria-controls', 'historySidebar');
            toggleBtn.setAttribute('aria-expanded', 'false');
            toggleBtn.addEventListener('click', toggleSidebar);
        }
        if (closeBtn && closeBtn.dataset.listenerAttached !== 'true') {
            closeBtn.dataset.listenerAttached = 'true';
            closeBtn.addEventListener('click', toggleSidebar);
        }
        if (newChatBtn && newChatBtn.dataset.listenerAttached !== 'true') {
            newChatBtn.dataset.listenerAttached = 'true';
            newChatBtn.addEventListener('click', startNewChat);
        }
        let savedState = null;
        try { savedState = localStorage.getItem('tracking_sidebar_open'); } catch (e) { }
        const shouldOpen = savedState !== null ? savedState === 'true' : window.innerWidth > 968;
        if (shouldOpen && sidebar && !sidebar.classList.contains('open')) toggleSidebar();
    }

    function setupHelpers() {
        const instructionsBtn = document.getElementById('showInstructionsBtn');
        if (instructionsBtn && instructionsBtn.dataset.listenerAttached !== 'true') {
            instructionsBtn.dataset.listenerAttached = 'true';
            instructionsBtn.addEventListener('click', toggleInstructions);
        }
        // Example queries: delegation, no inline onclick
        const welcome = document.getElementById('welcomeMessage');
        if (welcome && welcome.dataset.listenerAttached !== 'true') {
            welcome.dataset.listenerAttached = 'true';
            welcome.addEventListener('click', (e) => {
                const li = e.target.closest('.example-queries li');
                if (li) useExampleQuery(li.textContent);
            });
        }
    }

    // The camera example prompt names a REAL camera this user can see, the
    // busiest one, or stays hidden. /api/pipelines is already scoped per
    // user, so a restricted account is never invited to ask about a camera
    // it cannot query.
    async function refreshCameraExample() {
        const li = document.querySelector('.example-queries li[data-example="camera"]');
        if (!li) return;
        try {
            const result = await api('/api/pipelines');
            const cameras = Array.isArray(result.payload) ? result.payload : [];
            const best = cameras
                .filter((c) => c && c.pipeline_name)
                .sort((a, b) => (b.total_detections || 0) - (a.total_detections || 0))[0];
            if (!best) return;
            const slot = li.querySelector('.example-camera-name');
            if (slot) slot.textContent = best.pipeline_name;
            li.hidden = false;
        } catch (e) { /* the suggestion simply stays hidden */ }
    }

    async function initialize() {
        if (initialized) return;
        initialized = true;

        setupChatInterface();
        setupSidebar();
        setupHelpers();

        window.addEventListener('pagehide', destroy);
        window.addEventListener('beforeunload', destroy);

        try {
            const result = await api('/api/auth/me');
            if (result.ok && result.payload) {
                currentUser = result.payload;
                const userInfo = document.getElementById('user-info');
                if (userInfo) userInfo.textContent = `${currentUser.full_name || currentUser.username} (${currentUser.role})`;
                const logoutBtn = document.getElementById('logout-btn');
                if (logoutBtn) logoutBtn.style.display = 'block';
                document.body.classList.toggle('admin-user', currentUser.role === 'admin');
                const homeLink = document.querySelector('.nav-link.admin-only');
                if (homeLink) homeLink.style.display = currentUser.role === 'admin' ? 'inline-block' : 'none';
            }
        } catch (e) { /* backend enforces auth regardless */ }

        refreshCameraExample();
        checkSQLAgentHealth();
        // Through refreshHistory(), NOT loadQueryHistory() directly: the
        // conversations module owns the sidebar when it is loaded, and both
        // modules render into the same #historyList / #historyEmpty nodes.
        // Calling the legacy loader here raced conversations.js's own init —
        // two concurrent fetches, last writer wins, and the sidebar showed
        // flat query-history rows or conversation titles nondeterministically.
        refreshHistory();
        updateConnectionStatus('connected', 'Ready');
    }

    function destroy() {
        if (destroyed) return;
        destroyed = true;
        wsManager.shutdown();
        for (const req of activeRequests.values()) {
            try { req.controller.abort(); } catch (e) { }
        }
        for (const id of timers) { clearTimeout(id); clearInterval(id); }
        timers.clear();
        for (const c of controllers) { try { c.abort(); } catch (e) { } }
        controllers.clear();
    }

    // Rendering + UX primitives for feature modules (conversations.js).
    // Modules reuse these instead of reimplementing them, so there is exactly
    // ONE escape-first markdown pipeline and ONE notice system on the page.
    window.trackingUI = {
        renderInto,
        showNotice,
        scrollToBottom,
        toggleSidebar,
        startNewChat,
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize);
    } else {
        initialize();
    }
})();
