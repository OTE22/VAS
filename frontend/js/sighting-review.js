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
        const title = el('h2', '', 'Review sightings'); title.id = 'sighting-review-title';
        const close = el('button', 'sighting-review-close', 'Done reviewing'); close.type = 'button';
        close.addEventListener('click', () => window.ModalStack.close(dialog));
        heading.append(title, close);
        const content = el('div', 'sighting-review-content');
        panel.append(heading, content); dialog.append(panel); document.body.append(dialog);
        return dialog;
    }
    function publishReview(session) {
        if (!session.saved || !session.scope.isConnected) return;
        const saved = session.saved; session.saved = null;
        document.dispatchEvent(new CustomEvent('sighting-review-saved', {detail:{...saved,
            sourceRoot:session.scope, search_embedding_id:session.scope.dataset.reviewSearchEmbedding || null,
            search_unverified:session.scope.dataset.reviewSearchUnverified === 'true'}}));
    }
    async function openReview(button, session = null) {
        const scope = button.closest('.movement-review-scope');
        const reference = scope?.dataset.reviewReference;
        if (!session) {
            const pending = [...(scope?.querySelectorAll('[data-review-pending="true"]') || [])];
            const buttons = [button, ...pending].filter((item, index, all) =>
                all.findIndex(other => other.dataset.reviewSighting === item.dataset.reviewSighting) === index);
            session = {scope, buttons, index:0, saved:null, closed:false, decisions:new Map()};
        }
        const sighting = button.dataset.reviewSighting;
        const identity = scope?.dataset.reviewIdentity;
        if (!identity || !positiveId(reference) || !positiveId(sighting) || !window.ModalStack) return;
        const revision = ++generation;
        previewController?.abort(); previewController = new AbortController();
        const modal = ensureDialog(), content = modal.querySelector('.sighting-review-content');
        content.replaceChildren(el('p', '', 'Loading the exact reference and sighting…'));
        window.ModalStack.open(modal, {onClose: () => { ++generation; previewController?.abort(); session.closed = true; publishReview(session); }});
        const endpoint = `/api/admin/identity/${encodeURIComponent(identity)}/sightings/${sighting}/review`;
        try {
            const data = await responseJson(await fetch(`${endpoint}?reference_appearance_id=${reference}`, {
                credentials: 'include', signal: previewController.signal, cache: 'no-store'}));
            if (revision !== generation) return;
            const navigate = index => {
                if (index < 0 || index >= session.buttons.length) return;
                session.index = index;
                openReview(session.buttons[index], session);
            };
            const progress = el('p', 'sighting-review-progress', `Image ${session.index + 1} of ${session.buttons.length} · ${session.decisions.size} decisions saved`);
            progress.setAttribute('role', 'status');
            const picker = el('select', 'sighting-review-picker');
            picker.setAttribute('aria-label', 'Choose image to review');
            session.buttons.forEach((item, index) => {
                const camera = item.closest('.movement-card')?.querySelector('h5')?.textContent || '';
                const decision = session.decisions.get(item.dataset.reviewSighting);
                const option = el('option', '', `Image ${index + 1}${camera ? ` · ${camera}` : ''}${decision ? ` · ${decision === 'confirmed' ? 'Same person' : 'Different person'}` : ''}`);
                option.value = String(index); picker.append(option);
            });
            picker.value = String(session.index);
            picker.addEventListener('change', () => navigate(Number(picker.value)));
            const explanation = el('p', 'sighting-review-explainer', 'Decide whether these two recorded sightings show the same person. Your decision applies to this pair and is saved with your account and time.');
            const images = el('div', 'sighting-review-photos');
            const noteLabel = el('label', 'sighting-review-note', 'Note (optional)');
            const note = el('textarea'); note.maxLength = 500; note.rows = 2; noteLabel.append(note);
            const status = el('p', 'sighting-review-status', data.blocked_reason || 'Wait for both photos to load before deciding.');
            status.setAttribute('role', 'status');
            const actions = el('div', 'sighting-review-actions');
            const confirm = el('button', 'review-confirm', 'Same person · save & next'); confirm.type = 'button'; confirm.dataset.reviewDecision = 'confirmed';
            const reject = el('button', 'review-reject', 'Different person · save & next'); reject.type = 'button'; reject.dataset.reviewDecision = 'rejected';
            const unsure = el('button', 'review-defer', 'Skip / next'); unsure.type = 'button';
            unsure.addEventListener('click', () => navigate(session.index + 1));
            const previous = el('button', 'review-previous', 'Previous'); previous.type = 'button';
            previous.addEventListener('click', () => navigate(session.index - 1));
            const undo = el('button', 'review-undo', 'Undo decision'); undo.type = 'button'; undo.dataset.reviewDecision = 'clear'; undo.hidden = !data.can_undo;
            let loaded = 0, failed = false, busy = false;
            const sync = () => {
                confirm.disabled = reject.disabled = busy || !data.can_decide || loaded !== 2 || failed;
                undo.disabled = busy || !data.can_undo;
                unsure.disabled = busy || session.index === session.buttons.length - 1;
                previous.disabled = busy || session.index === 0; picker.disabled = busy;
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
                    session.saved = saved;
                    if (decision === 'clear') session.decisions.delete(sighting);
                    else session.decisions.set(sighting, decision);
                    if (session.closed || revision !== generation) { publishReview(session); return; }
                    if (decision === 'clear') { navigate(session.index); return; }
                    if (session.index < session.buttons.length - 1) { navigate(session.index + 1); return; }
                    // Keep the last comparison visible until the operator finishes.
                    data.can_decide = false; data.can_undo = false;
                    progress.textContent = `Image ${session.index + 1} of ${session.buttons.length} · ${session.decisions.size} decisions saved`;
                    status.textContent = 'Decision saved. End of this review list. Use Previous to revisit images, or Done reviewing to return to the updated list.';
                    const revisit = el('button', 'review-revisit', 'Review / undo last decision');
                    revisit.type = 'button'; revisit.addEventListener('click', () => navigate(session.index));
                    actions.append(revisit);
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
            actions.append(confirm, reject, unsure, previous, undo); sync();
            content.replaceChildren(progress, picker, explanation, images, noteLabel, status, actions,
                el('p', 'sighting-review-footnote', 'Confirmed sightings enter this journey with an Operator confirmed label. Different person excludes this pair from the reference journey and keeps both images and the review history. It does not split the identity or change future automatic matching. Decisions can be undone.'));
        } catch (error) {
            if (revision !== generation || error.name === 'AbortError') return;
            const message = el('p', 'sighting-review-status', error.message); message.setAttribute('role','alert');
            content.replaceChildren(message);
        }
    }
    document.addEventListener('click', event => {
        let button = event.target.closest('[data-review-sighting], [data-review-others]');
        if (!button) return;
        if (button.hasAttribute('data-review-others')) {
            const scope = button.closest('.movement-review-scope');
            button = scope?.querySelector('[data-review-pending="true"]') || scope?.querySelector('[data-review-sighting]');
            if (!button) return;
        }
        event.preventDefault(); event.stopPropagation(); openReview(button);
    });
})();
