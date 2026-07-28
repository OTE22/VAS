/**
 * Conversations feature module — the sidebar and message timeline on the
 * /api/v1 conversation domain (workspace -> conversation -> branch -> message).
 *
 * Boundaries:
 *  - This module OWNS conversation management (list, search, rename, pin,
 *    archive, delete, branch navigation, typed-block rendering).
 *  - tracking.js OWNS the live streaming send/stop lifecycle and exposes its
 *    rendering primitives on window.trackingUI; this module never reimplements
 *    markdown rendering or notice toasts.
 *  - All content from the server (titles, message blocks, SQL) is UNTRUSTED:
 *    everything reaches the DOM through textContent or through tracking.js's
 *    escape-first markdown pipeline. No innerHTML with interpolated data.
 *  - CSP: script-src 'self' — no inline handlers, no eval, no dynamic code.
 */
(function () {
    'use strict';

    const API_BASE = '/api/v1';
    const PAGE_SIZE = 30;

    let offset = 0;
    let hasMore = false;
    let searchTerm = '';
    let searchTimer = null;
    let listController = null;
    let activeConversationId = null;
    let activeBranchId = null;
    let branches = [];

    // ------------------------------------------------------------------
    // API helper (cookie auth + CSRF header on mutations)
    // ------------------------------------------------------------------
    async function api(path, options = {}) {
        const method = (options.method || 'GET').toUpperCase();
        const headers = { 'Accept': 'application/json' };
        if (method !== 'GET') {
            headers['X-Requested-With'] = 'XMLHttpRequest';
            headers['Content-Type'] = 'application/json';
        }
        try {
            const resp = await fetch(API_BASE + path, {
                method,
                headers,
                credentials: 'include',
                cache: 'no-store',
                body: options.body ? JSON.stringify(options.body) : undefined,
                signal: options.controller ? options.controller.signal : undefined,
            });
            let payload = null;
            try { payload = await resp.json(); } catch (e) { /* empty body */ }
            return { ok: resp.ok, status: resp.status, payload };
        } catch (err) {
            if (err && err.name === 'AbortError') return { ok: false, status: 0, aborted: true };
            return { ok: false, status: 0, payload: null };
        }
    }

    function ui() { return window.trackingUI || {}; }
    function notice(message, kind) {
        if (ui().showNotice) ui().showNotice(message, kind);
    }

    // ------------------------------------------------------------------
    // Sidebar: search + sectioned list (Pinned / Recent)
    // ------------------------------------------------------------------
    function ensureSearchInput() {
        const content = document.getElementById('sidebarContent');
        if (!content || document.getElementById('convSearchInput')) return;
        const wrap = document.createElement('div');
        wrap.className = 'conv-search-wrap';
        const input = document.createElement('input');
        input.type = 'search';
        input.id = 'convSearchInput';
        input.className = 'conv-search-input';
        input.placeholder = 'Search conversations…';
        input.setAttribute('aria-label', 'Search conversations');
        input.autocomplete = 'off';
        input.addEventListener('input', () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                searchTerm = input.value.trim();
                refresh();
            }, 300);
        });
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && input.value) {
                input.value = '';
                searchTerm = '';
                refresh();
                e.stopPropagation();
            }
        });
        wrap.appendChild(input);
        content.insertBefore(wrap, content.firstChild);
    }

    async function refresh(append = false) {
        const listEl = document.getElementById('historyList');
        const emptyEl = document.getElementById('historyEmpty');
        const loadingEl = document.getElementById('historyLoading');
        if (!listEl) return;
        ensureSearchInput();

        if (listController) { try { listController.abort(); } catch (e) { } }
        listController = new AbortController();
        if (!append) offset = 0;
        if (!append && loadingEl && !listEl.childElementCount) loadingEl.style.display = 'flex';

        const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
        if (searchTerm) params.set('q', searchTerm);
        const result = await api(`/conversations?${params}`, { controller: listController });
        if (loadingEl) loadingEl.style.display = 'none';
        if (result.aborted) return;
        if (!result.ok || !result.payload) {
            if (result.status === 401 || result.status === 403) return; // auth flow handles it
            if (emptyEl && !listEl.childElementCount) {
                emptyEl.replaceChildren(document.createTextNode('Failed to load conversations'));
                emptyEl.style.display = 'flex';
            }
            return;
        }

        const conversations = result.payload.conversations || [];
        hasMore = conversations.length === PAGE_SIZE;

        if (!append) listEl.replaceChildren();
        if (!conversations.length && !listEl.childElementCount) {
            if (emptyEl) {
                emptyEl.replaceChildren(document.createTextNode(
                    searchTerm ? `No conversations match "${searchTerm}"` : 'No conversations yet — ask your first question'));
                emptyEl.style.display = 'flex';
            }
            listEl.style.display = 'none';
            return;
        }
        if (emptyEl) emptyEl.style.display = 'none';
        listEl.style.display = 'block';

        const pinned = conversations.filter(c => c.pinned);
        const recent = conversations.filter(c => !c.pinned);
        if (!append && pinned.length) {
            listEl.appendChild(sectionHeader('Pinned'));
            pinned.forEach(c => listEl.appendChild(buildItem(c)));
        }
        if (recent.length) {
            if (!append && pinned.length) listEl.appendChild(sectionHeader('Recent'));
            recent.forEach(c => listEl.appendChild(buildItem(c)));
        }
        renderLoadMore(listEl);
    }

    function sectionHeader(label) {
        const el = document.createElement('div');
        el.className = 'conv-section-header';
        el.textContent = label;
        return el;
    }

    function renderLoadMore(listEl) {
        let btn = document.getElementById('convLoadMoreBtn');
        if (hasMore) {
            if (!btn) {
                btn = document.createElement('button');
                btn.id = 'convLoadMoreBtn';
                btn.type = 'button';
                btn.className = 'new-chat-btn';
                btn.style.cssText = 'margin:0.5rem auto;display:block;';
                btn.textContent = 'Load more';
                btn.addEventListener('click', () => { offset += PAGE_SIZE; refresh(true); });
            }
            listEl.appendChild(btn);
        } else if (btn) {
            btn.remove();
        }
    }

    function buildItem(conversation) {
        const node = document.createElement('div');
        node.className = 'history-item conv-item';
        node.dataset.conversationId = conversation.id;
        if (conversation.id === activeConversationId) node.classList.add('active');
        node.setAttribute('role', 'button');
        node.tabIndex = 0;

        const title = document.createElement('div');
        title.className = 'history-item-query';
        title.textContent = conversation.title || 'Untitled';
        node.appendChild(title);

        const meta = document.createElement('div');
        meta.className = 'history-item-meta';
        const time = document.createElement('span');
        time.className = 'history-item-time';
        time.textContent = formatTime(conversation.last_message_at);
        meta.appendChild(time);
        if (conversation.archived) {
            const badge = document.createElement('span');
            badge.className = 'conv-badge';
            badge.textContent = 'archived';
            meta.appendChild(badge);
        }
        node.appendChild(meta);

        const actions = document.createElement('div');
        actions.className = 'conv-item-actions';
        actions.appendChild(actionButton('fa-thumbtack', conversation.pinned ? 'Unpin' : 'Pin',
            (e) => { e.stopPropagation(); togglePin(conversation); }));
        actions.appendChild(actionButton('fa-pen', 'Rename',
            (e) => { e.stopPropagation(); renameInline(node, conversation); }));
        actions.appendChild(actionButton('fa-box-archive', conversation.archived ? 'Restore' : 'Archive',
            (e) => { e.stopPropagation(); toggleArchive(conversation); }));
        actions.appendChild(actionButton('fa-trash', 'Delete',
            (e) => { e.stopPropagation(); confirmDelete(conversation); }));
        node.appendChild(actions);

        const open = () => openConversation(conversation.id);
        node.addEventListener('click', open);
        node.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        });
        return node;
    }

    function actionButton(icon, label, handler) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'conv-action-btn';
        btn.title = label;
        btn.setAttribute('aria-label', label);
        const i = document.createElement('i');
        i.className = `fas ${icon}`;
        i.setAttribute('aria-hidden', 'true');
        btn.appendChild(i);
        btn.addEventListener('click', handler);
        return btn;
    }

    function formatTime(iso) {
        if (!iso) return '';
        const date = new Date(iso);
        if (isNaN(date.getTime())) return '';
        const now = new Date();
        const sameDay = date.toDateString() === now.toDateString();
        return sameDay
            ? date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
            : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    }

    // ------------------------------------------------------------------
    // Conversation actions
    // ------------------------------------------------------------------
    async function togglePin(conversation) {
        const result = await api(`/conversations/${encodeURIComponent(conversation.id)}/flags`,
            { method: 'PATCH', body: { pinned: !conversation.pinned } });
        if (!result.ok) { notice('Could not update pin', 'error'); return; }
        refresh();
    }

    async function toggleArchive(conversation) {
        const result = await api(`/conversations/${encodeURIComponent(conversation.id)}/flags`,
            { method: 'PATCH', body: { archived: !conversation.archived } });
        if (!result.ok) { notice('Could not update archive state', 'error'); return; }
        refresh();
    }

    function renameInline(node, conversation) {
        const titleEl = node.querySelector('.history-item-query');
        if (!titleEl || node.querySelector('input.conv-rename-input')) return;
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'conv-rename-input';
        input.value = conversation.title || '';
        input.maxLength = 500;
        input.setAttribute('aria-label', 'Conversation title');
        titleEl.replaceChildren(input);
        input.focus();
        input.select();

        let done = false;
        const commit = async () => {
            if (done) return; done = true;
            const newTitle = input.value.trim();
            if (!newTitle || newTitle === conversation.title) { refresh(); return; }
            const result = await api(`/conversations/${encodeURIComponent(conversation.id)}`,
                { method: 'PATCH', body: { title: newTitle } });
            if (!result.ok) notice('Rename failed', 'error');
            refresh();
        };
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); commit(); }
            if (e.key === 'Escape') { done = true; refresh(); e.stopPropagation(); }
        });
        input.addEventListener('blur', commit);
        input.addEventListener('click', (e) => e.stopPropagation());
    }

    function confirmDelete(conversation) {
        // Native confirm: dependency-free, keyboard/screen-reader accessible,
        // and impossible to dismiss accidentally by a stray click.
        const title = (conversation.title || 'this conversation').slice(0, 60);
        if (!window.confirm(`Delete "${title}"?\n\nIt will disappear from your history.`)) return;
        api(`/conversations/${encodeURIComponent(conversation.id)}`, { method: 'DELETE' })
            .then(result => {
                if (!result.ok) { notice('Delete failed', 'error'); return; }
                if (conversation.id === activeConversationId && ui().startNewChat) ui().startNewChat();
                refresh();
            });
    }

    // ------------------------------------------------------------------
    // Message timeline: typed block rendering
    // ------------------------------------------------------------------
    async function openConversation(conversationId, branchId = null) {
        activeConversationId = conversationId;
        activeBranchId = branchId;
        document.querySelectorAll('.conv-item').forEach(n =>
            n.classList.toggle('active', n.dataset.conversationId === conversationId));

        const params = branchId ? `?branch_id=${encodeURIComponent(branchId)}` : '';
        const [messagesResult, branchesResult] = await Promise.all([
            api(`/conversations/${encodeURIComponent(conversationId)}/messages${params}`),
            api(`/conversations/${encodeURIComponent(conversationId)}/branches`),
        ]);
        if (!messagesResult.ok || !messagesResult.payload) {
            notice('Failed to load conversation', 'error');
            return;
        }
        branches = (branchesResult.ok && branchesResult.payload && branchesResult.payload.branches) || [];
        activeBranchId = messagesResult.payload.branch_id;

        const container = document.getElementById('chatMessages');
        if (!container) return;
        const welcome = document.getElementById('welcomeMessage');
        container.replaceChildren();
        if (welcome) { container.appendChild(welcome); welcome.classList.add('hidden'); }

        renderBranchBar(container);
        (messagesResult.payload.messages || []).forEach(m => renderMessage(container, m));
        if (ui().scrollToBottom) ui().scrollToBottom(true);
        if (window.innerWidth <= 768 && ui().toggleSidebar) ui().toggleSidebar();
    }

    function renderBranchBar(container) {
        if (!branches || branches.length <= 1) return;
        const bar = document.createElement('div');
        bar.className = 'branch-bar';
        bar.setAttribute('role', 'navigation');
        bar.setAttribute('aria-label', 'Conversation branches');
        const index = Math.max(0, branches.findIndex(b => b.id === activeBranchId));

        const label = document.createElement('span');
        label.className = 'branch-label';
        label.textContent = `Branch ${index + 1} of ${branches.length}`;

        const prev = branchNavButton('fa-chevron-left', 'Previous branch', index > 0,
            () => openConversation(activeConversationId, branches[index - 1].id));
        const next = branchNavButton('fa-chevron-right', 'Next branch', index < branches.length - 1,
            () => openConversation(activeConversationId, branches[index + 1].id));

        bar.appendChild(prev);
        bar.appendChild(label);
        bar.appendChild(next);
        container.appendChild(bar);
    }

    function branchNavButton(icon, label, enabled, handler) {
        const btn = actionButton(icon, label, handler);
        btn.classList.add('branch-nav-btn');
        btn.disabled = !enabled;
        return btn;
    }

    function renderMessage(container, message) {
        const wrap = document.createElement('div');
        wrap.className = `message ${message.role === 'user' ? 'user-message' : 'assistant-message'}`;
        const inner = document.createElement('div');
        inner.className = 'message-inner';

        const avatar = document.createElement('div');
        avatar.className = `message-avatar ${message.role === 'user' ? 'user' : 'assistant'}`;
        const icon = document.createElement('i');
        icon.className = message.role === 'user' ? 'fas fa-user' : 'fas fa-robot';
        avatar.appendChild(icon);

        const content = document.createElement('div');
        content.className = `message-content ${message.role === 'user' ? 'user' : 'assistant'}`;
        const text = document.createElement('div');
        text.className = 'message-text';
        renderBlocks(text, message);
        content.appendChild(text);

        if (message.status === 'failed') {
            const failed = document.createElement('div');
            failed.className = 'message-failed-note';
            failed.textContent = 'This response did not complete successfully.';
            content.appendChild(failed);
        }

        if (message.role === 'user') {
            const editBtn = actionButton('fa-pen', 'Edit message (creates a new branch)',
                () => editMessage(message));
            editBtn.classList.add('message-edit-btn');
            content.appendChild(editBtn);
        }

        inner.appendChild(avatar);
        inner.appendChild(content);
        wrap.appendChild(inner);
        container.appendChild(wrap);
    }

    /**
     * The central block renderer. Every block type gets a dedicated,
     * escape-safe renderer; unknown types fall back to a labelled safe
     * placeholder instead of being dropped silently or rendered raw.
     */
    function renderBlocks(target, message) {
        const blocks = Array.isArray(message.content_blocks) ? message.content_blocks : [];
        blocks.forEach(block => {
            const type = block && block.type;
            if (type === 'text') {
                const el = document.createElement('div');
                el.className = 'block-text';
                if (message.role === 'user') {
                    String(block.text || '').split('\n').forEach((line, i) => {
                        if (i > 0) el.appendChild(document.createElement('br'));
                        el.appendChild(document.createTextNode(line));
                    });
                } else if (ui().renderInto) {
                    ui().renderInto(el, String(block.text || ''));
                } else {
                    el.textContent = String(block.text || '');
                }
                target.appendChild(el);
            } else if (type === 'sql') {
                target.appendChild(sqlBlock(String(block.sql || '')));
            } else if (type === 'error') {
                const el = document.createElement('div');
                el.className = 'block-error';
                el.setAttribute('role', 'alert');
                el.textContent = String(block.message || 'An error occurred.');
                target.appendChild(el);
            } else if (type === 'warning') {
                const el = document.createElement('div');
                el.className = 'block-warning';
                el.textContent = String(block.message || '');
                target.appendChild(el);
            } else {
                // Unknown block type: show a safe, honest placeholder.
                const el = document.createElement('div');
                el.className = 'block-unknown';
                el.textContent = `[Unsupported content: ${String(type || 'unknown').slice(0, 40)}]`;
                target.appendChild(el);
            }
        });
    }

    /** Dedicated SQL viewer block: monospace, copy button, collapse for long
     *  statements. SQL is textContent — never parsed as markdown or HTML. */
    function sqlBlock(sql) {
        const wrap = document.createElement('div');
        wrap.className = 'sql-block';

        const header = document.createElement('div');
        header.className = 'sql-block-header';
        const label = document.createElement('span');
        label.className = 'sql-block-label';
        label.textContent = 'SQL';
        header.appendChild(label);

        const copyBtn = actionButton('fa-copy', 'Copy SQL', async () => {
            try {
                await navigator.clipboard.writeText(sql);
                notice('SQL copied', 'success');
            } catch (e) { notice('Copy failed', 'error'); }
        });
        header.appendChild(copyBtn);
        wrap.appendChild(header);

        const pre = document.createElement('pre');
        pre.className = 'sql-block-body';
        const code = document.createElement('code');
        code.textContent = sql;
        pre.appendChild(code);
        wrap.appendChild(pre);

        if (sql.split('\n').length > 12) {
            pre.classList.add('collapsed');
            const toggle = document.createElement('button');
            toggle.type = 'button';
            toggle.className = 'sql-expand-btn';
            toggle.textContent = 'Show full query';
            toggle.addEventListener('click', () => {
                const collapsed = pre.classList.toggle('collapsed');
                toggle.textContent = collapsed ? 'Show full query' : 'Collapse';
            });
            wrap.appendChild(toggle);
        }
        return wrap;
    }

    // ------------------------------------------------------------------
    // Edit -> branch
    // ------------------------------------------------------------------
    function editMessage(message) {
        const current = (message.content_blocks || [])
            .filter(b => b && b.type === 'text')
            .map(b => b.text || '')
            .join('\n');
        const edited = window.prompt('Edit your message (this creates a new branch):', current);
        if (edited === null) return;                    // cancelled
        const text = edited.trim();
        if (!text || text === current.trim()) return;   // nothing changed

        api(`/conversations/${encodeURIComponent(activeConversationId)}/branches`,
            { method: 'POST', body: { message_id: message.id, new_text: text } })
            .then(result => {
                if (!result.ok || !result.payload || !result.payload.branch_id) {
                    notice('Could not create branch', 'error');
                    return;
                }
                notice('Branch created', 'success');
                openConversation(activeConversationId, result.payload.branch_id);
            });
    }

    // ------------------------------------------------------------------
    // Public surface + init
    // ------------------------------------------------------------------
    /**
     * Ensure there is an active conversation to send into, creating one when
     * the user is in a fresh chat. Returns its id, or null when the backend
     * refuses (the caller then sends without a target and the server files
     * the exchange by session — degraded but never lost).
     */
    async function ensureActiveConversation(firstMessageText) {
        if (activeConversationId) return activeConversationId;
        const title = String(firstMessageText || '').trim().slice(0, 120) || 'New conversation';
        const result = await api('/conversations', { method: 'POST', body: { title } });
        if (!result.ok || !result.payload || !result.payload.id) return null;
        activeConversationId = result.payload.id;
        activeBranchId = result.payload.primary_branch_id || null;
        branches = [];
        refresh();
        return activeConversationId;
    }

    function clearActive() {
        activeConversationId = null;
        activeBranchId = null;
        branches = [];
        document.querySelectorAll('.conv-item.active').forEach(n => n.classList.remove('active'));
    }

    window.conversationsPanel = {
        refresh: () => refresh(false),
        open: openConversation,
        getActiveId: () => activeConversationId,
        ensureActiveConversation,
        clearActive,
    };

    function init() {
        ensureSearchInput();
        refresh(false);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
