/* Exact-sighting human review. Controls appear only with server authorization. */
(() => {
    'use strict';
    let dialog, generation = 0, previewController = null;
    const el = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    };
    const positiveId = value => /^[1-9][0-9]*$/.test(String(value || ''));
    function safePhoto(value) {
        try {
            const url = new URL(value, location.origin);
            return value && url.origin === location.origin && /^https?:$/.test(url.protocol) ? url.href : null;
        } catch (_) { return null; }
    }
    async function responseJson(response) {
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'The review could not be completed. Reopen it and try again.');
        return body;
    }
    function ensureDialog() {
        if (dialog) return dialog;
        dialog = el('div', 'security-modal sighting-review-modal');
        dialog.id = 'sighting-review-modal';
        dialog.setAttribute('role', 'dialog');
        dialog.setAttribute('aria-modal', 'true');
        dialog.setAttribute('aria-labelledby', 'sighting-review-title');
        const panel = el('div', 'sighting-review-panel');
        const heading = el('header', 'sighting-review-heading');
        const title = el('h2', '', 'Review this sighting'); title.id = 'sighting-review-title';
        const close = el('button', 'sighting-review-close', 'Close'); close.type = 'button';
        close.addEventListener('click', () => window.ModalStack.close(dialog));
        heading.append(title, close);
        const content = el('div', 'sighting-review-content');
        panel.append(heading, content); dialog.append(panel); document.body.append(dialog);
        return dialog;
    }
    async function openReview(button) {
        const scope = button.closest('.movement-review-scope');
        const reference = scope?.dataset.reviewReference, sighting = button.dataset.reviewSighting;
        const identity = scope?.dataset.reviewIdentity;
        if (!identity || !positiveId(reference) || !positiveId(sighting) || !window.ModalStack) return;
        const revision = ++generation;
        previewController?.abort(); previewController = new AbortController();
        const modal = ensureDialog(), content = modal.querySelector('.sighting-review-content');
        content.replaceChildren(el('p', '', 'Loading the exact reference and sighting…'));
        window.ModalStack.open(modal, {onClose: () => { ++generation; previewController?.abort(); }});
        const endpoint = `/api/admin/identity/${encodeURIComponent(identity)}/sightings/${sighting}/review`;
        try {
            const data = await responseJson(await fetch(`${endpoint}?reference_appearance_id=${reference}`, {
                credentials: 'include', signal: previewController.signal, cache: 'no-store'}));
            if (revision !== generation) return;
            const explanation = el('p', 'sighting-review-explainer', 'Decide whether these two recorded sightings show the same person. Your decision applies to this pair and is saved with your account and time.');
            const images = el('div', 'sighting-review-photos');
            const noteLabel = el('label', 'sighting-review-note', 'Note (optional)');
            const note = el('textarea'); note.maxLength = 500; note.rows = 2; noteLabel.append(note);
            const status = el('p', 'sighting-review-status', data.blocked_reason || 'Wait for both photos to load before deciding.');
            status.setAttribute('role', 'status');
            const actions = el('div', 'sighting-review-actions');
            const confirm = el('button', 'review-confirm', 'Confirm same person'); confirm.type = 'button'; confirm.dataset.reviewDecision = 'confirmed';
            const reject = el('button', 'review-reject', 'Different person'); reject.type = 'button'; reject.dataset.reviewDecision = 'rejected';
            const unsure = el('button', 'review-defer', 'Not sure — close'); unsure.type = 'button';
            unsure.addEventListener('click', () => window.ModalStack.close(modal));
            const undo = el('button', 'review-undo', 'Undo decision'); undo.type = 'button'; undo.dataset.reviewDecision = 'clear'; undo.hidden = !data.can_undo;
            let loaded = 0, failed = false, busy = false;
            const sync = () => {
                confirm.disabled = reject.disabled = busy || !data.can_decide || loaded !== 2 || failed;
                undo.disabled = busy || !data.can_undo; unsure.disabled = busy;
            };
            [data.reference, data.sighting].forEach((photo, index) => {
                const figure = el('figure');
                figure.append(el('h3', '', index ? 'Sighting to review' : 'Reference sighting'));
                const frame = el('div', 'sighting-review-photo');
                const url = safePhoto(photo?.snapshot_url);
                if (url) {
                    const image = el('img'); image.alt = index ? 'Exact sighting being reviewed' : 'Selected reference sighting';
                    image.addEventListener('load', () => {
                        if (revision !== generation || !image.naturalWidth) return;
                        ++loaded;
                        if (loaded === 2 && !failed && data.can_decide) status.textContent = 'Compare both faces, then choose a decision.';
                        sync();
                    }, {once:true});
                    image.addEventListener('error', () => {
                        failed = true; image.hidden = true; frame.append(el('span', '', 'Image unavailable'));
                        status.textContent = 'A photo could not load. Reopen the review to retry; confirmation is disabled.'; sync();
                    }, {once:true});
                    image.src = url; frame.append(image);
                } else { failed = true; frame.append(el('span', '', 'No saved photo')); }
                const caption = el('figcaption');
                caption.append(el('strong', '', photo?.camera_name || 'Unknown camera'),
                    el('time', '', photo?.seen_at ? new Date(photo.seen_at).toLocaleString() : 'Time unavailable'));
                figure.append(frame, caption); images.append(figure);
            });
            if (data.reviewer) explanation.append(el('small', '', `Last decision: ${data.decision === 'confirmed' ? 'same person' : data.decision === 'rejected' ? 'different person' : 'undone'} · ${data.reviewer} · ${new Date(data.reviewed_at).toLocaleString()}`));
            const save = async decision => {
                if (busy || revision !== generation || (decision !== 'clear' && confirm.disabled)) return;
                busy = true; sync(); status.textContent = 'Saving decision…';
                try {
                    // Do not abort a submitted mutation on modal close: its
                    // committed response still refreshes the original view.
                    const saved = await responseJson(await fetch(endpoint, {method:'POST', credentials:'include',
                        headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},
                        body:JSON.stringify({preview_token:data.preview_token, decision, note:note.value})}));
                    if (revision === generation) window.ModalStack.close(modal);
                    if (scope.isConnected) document.dispatchEvent(new CustomEvent('sighting-review-saved', {detail:{...saved,
                        sourceRoot:scope, search_embedding_id:scope.dataset.reviewSearchEmbedding || null,
                        search_unverified:scope.dataset.reviewSearchUnverified === 'true'}}));
                } catch (error) {
                    if (revision !== generation) return;
                    // A failed response may follow a committed request. Force a
                    // fresh versioned preview before allowing another decision.
                    data.can_decide = false; data.can_undo = false;
                    status.textContent = error.message + ' Close and reopen to check the saved state.';
                } finally { busy = false; if (revision === generation) sync(); }
            };
            confirm.addEventListener('click', () => save('confirmed'));
            reject.addEventListener('click', () => save('rejected'));
            undo.addEventListener('click', () => save('clear'));
            actions.append(confirm, reject, unsure, undo); sync();
            content.replaceChildren(explanation, images, noteLabel, status, actions,
                el('p', 'sighting-review-footnote', 'Confirmed sightings enter this journey with an Operator confirmed label. Rejection keeps the record outside this journey. Recognition-reference enrollment remains a separate action.'));
        } catch (error) {
            if (revision !== generation || error.name === 'AbortError') return;
            const message = el('p', 'sighting-review-status', error.message); message.setAttribute('role','alert');
            content.replaceChildren(message);
        }
    }
    document.addEventListener('click', event => {
        const button = event.target.closest('[data-review-sighting]');
        if (!button) return;
        event.preventDefault(); event.stopPropagation(); openReview(button);
    });
})();
