/**
 * Ingest credentials admin page.
 *
 * SECURITY CONTRACT. The raw token exists in the modal input and nowhere else.
 * Never in the URL, never in sessionStorage/localStorage, never in a data-
 * attribute, never in console.log — each of those outlives the modal and would
 * turn a one-time secret into a recoverable one. Dismissing clears the field.
 *
 * Rows are built with createElement + textContent, never innerHTML: `name` is
 * operator-supplied text that round-trips through the database.
 */

const API_BASE = '/api/admin/webhook-credentials';

let cacheTtlSeconds = 30;
let pendingRevoke = null;     // { id, name }

/* -------------------------------------------------------------------------
   helpers
   ---------------------------------------------------------------------- */

/** Cookie clients must announce themselves for require_upload_csrf. */
function mutatingHeaders() {
    return {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest'
    };
}

function byId(id) {
    return document.getElementById(id);
}

function setText(id, text) {
    const el = byId(id);
    if (el) el.textContent = text || '';
}

/** Absolute timestamp plus a relative hint — "when" and "how long ago" answer
 *  different questions and an operator usually wants both. */
function formatStamp(iso) {
    if (!iso) return null;
    const parsed = new Date(iso);
    if (Number.isNaN(parsed.getTime())) return iso;

    const seconds = Math.floor((Date.now() - parsed.getTime()) / 1000);
    let relative;
    if (seconds < 60) relative = 'just now';
    else if (seconds < 3600) relative = `${Math.floor(seconds / 60)}m ago`;
    else if (seconds < 86400) relative = `${Math.floor(seconds / 3600)}h ago`;
    else relative = `${Math.floor(seconds / 86400)}d ago`;

    return { absolute: parsed.toLocaleString(), relative };
}

function cell(row, text, className) {
    const td = document.createElement('td');
    td.textContent = text;
    if (className) td.className = className;
    row.appendChild(td);
    return td;
}

/** A timestamp cell: relative text, absolute in the tooltip. */
function stampCell(row, iso) {
    const stamp = formatStamp(iso);
    if (!stamp) {
        cell(row, 'never', 'ic-table__never');
        return;
    }
    if (typeof stamp === 'string') {
        cell(row, stamp, 'ic-table__muted');
        return;
    }
    const td = cell(row, stamp.relative, 'ic-table__muted');
    td.title = stamp.absolute;
}

/* -------------------------------------------------------------------------
   load + render
   ---------------------------------------------------------------------- */

async function loadCredentials() {
    const body = byId('credentials-body');
    if (!body) return;

    let data;
    try {
        const response = await fetch(API_BASE, { credentials: 'include' });
        if (!response.ok) {
            body.replaceChildren();
            renderEmpty(body, response.status === 403
                ? 'Administrator access required'
                : `Could not load credentials (HTTP ${response.status})`,
                response.status === 403
                    ? 'Only administrators can view or manage ingest credentials.'
                    : 'Reload the page to try again.');
            return;
        }
        data = await response.json();
    } catch (err) {
        body.replaceChildren();
        renderEmpty(body, 'Could not reach the server',
                    'Check your connection and reload the page.');
        return;
    }

    if (Number.isFinite(data.cache_ttl_seconds)) {
        cacheTtlSeconds = data.cache_ttl_seconds;
    }

    renderSummary(data);
    renderRows(body, data.credentials || []);

    setText('last-used-note',
        `“Last used” is recorded at most once every ${cacheTtlSeconds} seconds per worker, `
        + 'so a credential used moments ago can still read “never”.');
}

function renderSummary(data) {
    const count = data.count || 0;
    setText('ic-count', count === 1 ? '1 issued' : `${count} issued`);

    const status = byId('ic-breakglass');
    if (!status) return;

    if (data.env_keys_configured) {
        status.setAttribute('data-status', 'ok');
        setText('ic-breakglass-label', 'Break-glass key configured');
        status.title = 'A key is also configured in the environment. It keeps ingest '
                     + 'working if the database is unreachable, and cannot be changed here.';
    } else {
        // Not an error: it is legal, and it is the state where revoking the last
        // credential locks every sender out. Warn, do not alarm.
        status.setAttribute('data-status', 'warn');
        setText('ic-breakglass-label', 'No break-glass key');
        status.title = 'No environment key is configured. If you revoke every credential '
                     + 'below, no sender will be able to submit frames.';
    }
}

function renderEmpty(body, title, text) {
    const row = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 6;

    const wrap = document.createElement('div');
    wrap.className = 'hp-empty';

    const heading = document.createElement('p');
    heading.className = 'hp-empty__title';
    heading.textContent = title;

    const detail = document.createElement('p');
    detail.className = 'hp-empty__text';
    detail.textContent = text;

    wrap.append(heading, detail);
    td.appendChild(wrap);
    row.appendChild(td);
    body.appendChild(row);
}

function renderRows(body, credentials) {
    body.replaceChildren();

    if (credentials.length === 0) {
        renderEmpty(body, 'No credentials issued yet',
                    'Issue one above for each external system that will send frames.');
        return;
    }

    credentials.forEach((credential) => {
        const row = document.createElement('tr');

        cell(row, credential.name, 'ic-table__name');

        const fp = cell(row, credential.fingerprint, 'ic-mono');
        fp.title = 'The first 8 characters of the token’s SHA-256. Whoever holds the '
                 + 'token can match it against this; it cannot be reversed.';

        stampCell(row, credential.created_at);
        cell(row, credential.created_by_username || '—', 'ic-table__muted');
        stampCell(row, credential.last_used_at);

        const actions = document.createElement('td');
        actions.className = 'ic-table__actions';

        const revokeBtn = document.createElement('button');
        revokeBtn.type = 'button';
        revokeBtn.className = 'ic-btn-icon';
        revokeBtn.setAttribute('aria-label', `Revoke ${credential.name}`);

        const icon = document.createElement('i');
        icon.className = 'fas fa-ban';
        icon.setAttribute('aria-hidden', 'true');

        const label = document.createElement('span');
        label.textContent = 'Revoke';

        revokeBtn.append(icon, label);
        revokeBtn.addEventListener('click',
            () => openRevokeModal(credential.id, credential.name));

        actions.appendChild(revokeBtn);
        row.appendChild(actions);
        body.appendChild(row);
    });
}

/* -------------------------------------------------------------------------
   issue
   ---------------------------------------------------------------------- */

async function issueCredential() {
    const input = byId('credential-name');
    const button = byId('issue-btn');
    if (!input || !button) return;

    const name = input.value.trim();
    setText('issue-error', '');

    if (name.length < 2) {
        setText('issue-error', 'Enter the name of the system this token is for (at least 2 characters).');
        input.focus();
        return;
    }

    button.disabled = true;
    try {
        const response = await fetch(API_BASE, {
            method: 'POST',
            credentials: 'include',
            headers: mutatingHeaders(),
            body: JSON.stringify({ name })
        });

        if (response.status === 409) {
            setText('issue-error',
                `A credential named “${name}” already exists. Names are compared ignoring `
                + 'case and extra spaces, so pick something distinct.');
            return;
        }
        if (response.status === 403) {
            setText('issue-error', 'Administrator access is required to issue credentials.');
            return;
        }
        if (!response.ok) {
            setText('issue-error', `Could not issue the credential (HTTP ${response.status}).`);
            return;
        }

        const created = await response.json();
        input.value = '';
        showToken(created.name, created.token);
        await loadCredentials();
    } catch (err) {
        setText('issue-error', 'Could not reach the server. The credential was not issued.');
    } finally {
        button.disabled = false;
    }
}

/* -------------------------------------------------------------------------
   the one-time token
   ---------------------------------------------------------------------- */

function showToken(name, token) {
    const modal = byId('token-modal');
    const field = byId('token-value');
    if (!modal || !field) return;

    setText('token-modal-name', name);
    setText('copy-status', '');
    field.value = token;
    // This page hides dialogs with the `hidden` attribute rather than
    // `display`. Both are kept in sync: `hidden` carries the semantics,
    // ModalStack owns layering, Escape, backdrop, focus and the scroll lock.
    // Focus restore is the stack's too, so lastFocused is not tracked here.
    modal.hidden = false;
    window.ModalStack.open(modal, { backdropClose: true, onClose: () => dismissToken() });
    field.focus();
    field.select();
}

function dismissToken() {
    const modal = byId('token-modal');
    if (modal && window.ModalStack.isOpen(modal)) {
        window.ModalStack.close(modal);   // re-enters here via onClose
        return;
    }
    const field = byId('token-value');
    // Security cleanup, preserved and now reached from Escape and backdrop
    // dismissal as well as the button: this is the only copy of the one-time
    // token in the DOM.
    if (field) field.value = '';
    if (modal) modal.hidden = true;
    setText('copy-status', '');
}

async function copyToken() {
    const field = byId('token-value');
    const status = byId('copy-status');
    if (!field || !field.value) return;

    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(field.value);
        } else {
            // Plain-HTTP deployments have no navigator.clipboard.
            field.select();
            document.execCommand('copy');
        }
        if (status) status.classList.remove('ic-status--error');
        setText('copy-status', 'Copied to your clipboard.');
    } catch (err) {
        if (status) status.classList.add('ic-status--error');
        setText('copy-status', 'Copy failed — select the token above and copy it manually.');
    }
}

/* -------------------------------------------------------------------------
   revoke
   ---------------------------------------------------------------------- */

function openRevokeModal(id, name) {
    pendingRevoke = { id, name };
    const modal = byId('revoke-modal');
    const confirmInput = byId('revoke-confirm');
    const confirmBtn = byId('confirm-revoke-btn');
    if (!modal || !confirmInput || !confirmBtn) return;

    setText('revoke-modal-name', name);
    setText('revoke-warning',
        `${name} will stop being accepted, and the token cannot be restored — issuing a `
        + `replacement means handing over a new one. Already-cached verifiers refresh within `
        + `${cacheTtlSeconds} seconds, so a frame sent inside that window may still go through.`);
    setText('revoke-error', '');
    confirmInput.value = '';
    confirmBtn.disabled = true;
    modal.hidden = false;
    window.ModalStack.open(modal, { backdropClose: true, onClose: () => closeRevokeModal() });
    confirmInput.focus();
}

function closeRevokeModal() {
    const modal = byId('revoke-modal');
    if (modal && window.ModalStack.isOpen(modal)) {
        window.ModalStack.close(modal);
        return;
    }
    if (modal) modal.hidden = true;
    // Business cleanup, preserved: a stale pendingRevoke would revoke the
    // wrong credential on the next confirm.
    pendingRevoke = null;
}

async function confirmRevoke() {
    if (!pendingRevoke) return;
    const { id } = pendingRevoke;

    try {
        const response = await fetch(`${API_BASE}/${encodeURIComponent(id)}`, {
            method: 'DELETE',
            credentials: 'include',
            headers: mutatingHeaders()
        });
        if (!response.ok) {
            setText('revoke-error', `Could not revoke (HTTP ${response.status}).`);
            return;
        }
        closeRevokeModal();
        await loadCredentials();
    } catch (err) {
        setText('revoke-error', 'Could not reach the server. Nothing was revoked.');
    }
}


/* -------------------------------------------------------------------------
   wiring
   ---------------------------------------------------------------------- */

document.addEventListener('DOMContentLoaded', () => {
    const issueBtn = byId('issue-btn');
    if (issueBtn) issueBtn.addEventListener('click', issueCredential);

    const nameInput = byId('credential-name');
    if (nameInput) {
        nameInput.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') issueCredential();
        });
    }

    const copyBtn = byId('copy-token-btn');
    if (copyBtn) copyBtn.addEventListener('click', copyToken);

    const dismissBtn = byId('dismiss-token-btn');
    if (dismissBtn) dismissBtn.addEventListener('click', dismissToken);

    const confirmInput = byId('revoke-confirm');
    const confirmBtn = byId('confirm-revoke-btn');
    if (confirmInput && confirmBtn) {
        confirmInput.addEventListener('input', () => {
            confirmBtn.disabled = !pendingRevoke
                || confirmInput.value.trim() !== pendingRevoke.name;
        });
        confirmInput.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' && !confirmBtn.disabled) confirmRevoke();
        });
    }
    if (confirmBtn) confirmBtn.addEventListener('click', confirmRevoke);

    const cancelBtn = byId('cancel-revoke-btn');
    if (cancelBtn) cancelBtn.addEventListener('click', closeRevokeModal);

    // Escape closes the revoke dialog, which is cancellable. It deliberately
    // does NOT close the token dialog: that one is dismissed only by the
    // explicit "I have saved it", because closing it loses the token forever.
    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        const revokeModal = byId('revoke-modal');
        if (revokeModal && !revokeModal.hidden) closeRevokeModal();
    });

    loadCredentials();
});
