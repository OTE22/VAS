/* Known Faces: server-paginated directory and enrollment photo management. */
(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const state = { page: 1, pages: 1, items: [], selected: null, request: 0, galleryRequest: 0 };
    let operation = null;
    const el = (tag, cls, text) => {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined) node.textContent = text;
        return node;
    };
    function notice(message, error = false, detail = false) {
        const node = $(detail ? 'known-detail-notice' : 'known-notice');
        node.textContent = message;
        node.classList.toggle('error', error);
        node.hidden = !message;
    }
    async function api(url, options = {}) {
        const response = await fetch(url, { credentials: 'same-origin', ...options,
            headers: { 'X-Requested-With': 'XMLHttpRequest', ...(options.headers || {}) } });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const detail = typeof data.detail === 'string' ? data.detail : null;
            throw new Error(response.status === 401 ? 'Your session expired. Sign in again.'
                : response.status === 403 ? 'Only administrators can manage known faces.'
                : data.message || detail || data.detail?.message || 'The request failed. Please try again.');
        }
        return data;
    }
    function photo(url, name, cls) {
        const fallback = () => {
            const node = el('div', 'known-placeholder');
            const icon = el('i', 'fas fa-user'); icon.setAttribute('aria-hidden', 'true');
            node.append(icon); node.setAttribute('aria-label', 'No photo available'); return node;
        };
        if (typeof url !== 'string' || !/^\/(?!\/)/.test(url) || url.includes('..') || url.includes('\\')) return fallback();
        const img = el('img', cls); img.alt = name; img.loading = 'lazy'; img.src = url;
        img.addEventListener('error', () => img.replaceWith(fallback()), { once: true });
        return img;
    }
    function date(value) {
        if (!value) return 'Not seen on camera';
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime()) ? 'Date unavailable' : parsed.toLocaleDateString(undefined,
            { year: 'numeric', month: 'short', day: 'numeric' });
    }
    function card(person) {
        const article = el('article', 'known-card');
        article.append(photo(person.photo_url, person.display_name, 'known-card-photo'));
        const body = el('div', 'known-card-body'), top = el('div', 'known-card-top');
        top.append(el('h3', '', person.display_name), el('span', 'known-badge ' + person.status, person.status));
        const id = el('p', 'known-card-id', 'ID ' + person.id.slice(0, 8)); id.title = person.id;
        const meta = el('div', 'known-card-meta');
        meta.append(el('span', '', `${person.photo_count} photo${person.photo_count === 1 ? '' : 's'}`),
            el('span', '', person.last_seen_at ? 'Seen ' + date(person.last_seen_at) : date(null)));
        const button = el('button', 'known-button', 'Manage person'); button.type = 'button';
        button.setAttribute('aria-label', `Manage ${person.display_name}`);
        button.addEventListener('click', () => openPerson(person));
        body.append(top, id, meta, button); article.append(body); return article;
    }
    async function load() {
        const request = ++state.request;
        const query = new URLSearchParams({ q: $('known-query').value.trim(), status: $('known-status').value,
            sort: $('known-sort').value, page: state.page, page_size: 24 });
        $('known-grid').setAttribute('aria-busy', 'true');
        $('known-prev').disabled = $('known-next').disabled = true;
        $('known-state').hidden = false; $('known-state').textContent = 'Loading your directory…';
        $('known-grid').replaceChildren();
        try {
            const data = await api('/api/admin/known-faces?' + query);
            if (request !== state.request) return;
            state.items = data.items; state.pages = data.total_pages;
            if (state.page > state.pages) { state.page = state.pages; return load(); }
            $('known-count').textContent = data.total.toLocaleString();
            $('known-grid').replaceChildren(...state.items.map(card));
            $('known-state').hidden = !!state.items.length;
            if (!state.items.length) {
                const filtered = $('known-query').value.trim() || $('known-status').value !== 'current';
                $('known-state').textContent = filtered ? 'No people found. Try another name or change the status filter.'
                    : 'No current known people yet. Add a person to start your directory.';
                const reset = el('button', 'known-button', filtered ? 'Clear filters' : 'Add your first person'); reset.type = 'button';
                reset.addEventListener('click', () => {
                    if (!filtered) { openUpload(); return; }
                    $('known-query').value = ''; $('known-status').value = 'current'; state.page = 1; load();
                });
                $('known-state').append(reset);
            }
            $('known-page-label').textContent = `Page ${state.page} of ${state.pages} · ${data.total} people`;
            $('known-prev').disabled = state.page <= 1; $('known-next').disabled = state.page >= state.pages;
        } catch (error) {
            if (request !== state.request) return;
            $('known-count').textContent = '—'; $('known-page-label').textContent = '';
            $('known-state').textContent = error.message;
            const retry = el('button', 'known-button', 'Try again'); retry.type = 'button';
            retry.addEventListener('click', load); $('known-state').append(retry);
        } finally {
            if (request === state.request) $('known-grid').setAttribute('aria-busy', 'false');
        }
    }
    function openPerson(person) {
        state.selected = person;
        $('known-detail-name').textContent = person.display_name;
        $('known-detail-id').textContent = person.id;
        $('known-profile').href = '/admin/identity/' + encodeURIComponent(person.id) + '?from=/admin/known';
        $('known-name').value = person.display_name;
        $('known-rename').querySelectorAll('input,button').forEach(n => { n.disabled = !person.can_edit; });
        $('known-add-photo').disabled = !person.can_add_photo;
        $('known-readonly').hidden = person.can_add_photo;
        $('known-activation').textContent = person.status === 'inactive' ? 'Reactivate person' : 'Deactivate person';
        $('known-activation').disabled = $('known-delete').disabled = !person.can_edit;
        notice('', false, true);
        $('known-detail').hidden = false;
        window.ModalStack.open($('known-detail'), { backdropClose: true, onClose: () => {
            $('known-detail').hidden = true; state.selected = null; state.galleryRequest++;
        }});
        loadGallery();
    }
    async function loadGallery() {
        if (!state.selected) return;
        const person = state.selected, request = ++state.galleryRequest;
        $('known-photo-count').textContent = '';
        $('known-gallery').textContent = 'Loading enrollment photos…';
        try {
            const data = await api(`/api/identities/${encodeURIComponent(person.id)}/images`);
            if (request !== state.galleryRequest || state.selected?.id !== person.id) return;
            $('known-photo-count').textContent = `(${data.total})`;
            $('known-gallery').replaceChildren();
            if (!data.images.length) $('known-gallery').textContent = 'No enrolled photos yet.';
            data.images.forEach(image => {
                const item = el('article', 'known-photo' + (image.is_primary ? ' primary-photo' : ''));
                item.append(photo(image.url, `Enrollment photo of ${person.display_name}`, ''));
                const body = el('div');
                body.append(el('p', '', `${image.source_type === 'promotion' ? 'Promoted photo' : 'Uploaded photo'} · ${date(image.created_at)}`));
                if (image.is_primary) body.append(el('span', 'known-photo-primary', '✓ Primary photo'));
                else {
                    const button = el('button', 'known-button', 'Set as primary'); button.type = 'button';
                    button.disabled = !person.can_add_photo;
                    button.addEventListener('click', async () => {
                        button.disabled = true;
                        try {
                            await api(`/api/identities/${encodeURIComponent(person.id)}/images/${image.image_id}/primary`, { method: 'PUT' });
                            if (state.selected?.id === person.id) { notice('Primary photo updated.', false, true); loadGallery(); }
                            load();
                        } catch (error) { if (state.selected?.id === person.id) notice(error.message, true, true); button.disabled = false; }
                    });
                    body.append(button);
                }
                item.append(body); $('known-gallery').append(item);
            });
        } catch (error) {
            if (request === state.galleryRequest) {
                $('known-gallery').textContent = error.message;
                const retry = el('button', 'known-button', 'Retry photos'); retry.type = 'button';
                retry.addEventListener('click', loadGallery); $('known-gallery').append(retry);
            }
        }
    }
    async function openUpload(person = null) {
        // The shared component loads asynchronously with the navbar.
        for (let attempt = 0; attempt < 40 && typeof window.openUploadModal !== 'function'; attempt++) {
            await new Promise(resolve => setTimeout(resolve, 100));
        }
        if (typeof window.openUploadModal !== 'function') {
            notice('The upload dialog could not load. Refresh the page and try again.', true, !!person); return;
        }
        window.openUploadModal(person ? { identityId: person.id, displayName: person.display_name, photoUrl: person.photo_url } : undefined);
    }
    let timer;
    function operationError(message) {
        $('known-confirm-error').textContent = message;
        $('known-confirm-error').hidden = !message;
    }
    function syncConfirmation() {
        $('known-confirm-submit').disabled = !operation || operation.busy ||
            (operation.kind === 'delete' && (!operation.impact || $('known-delete-name').value !== operation.impact.display_name));
    }
    async function refreshPreview() {
        const current = operation;
        if (!current || current.kind !== 'delete' || current.busy) return;
        current.impact = null;
        $('known-delete-name').value = '';
        $('known-delete-impact').textContent = 'Calculating affected records…';
        operationError(''); syncConfirmation();
        const generation = current.generation = (current.generation || 0) + 1;
        try {
            const impact = await api(`/api/admin/known-faces/${encodeURIComponent(current.person.id)}/deletion-preview`);
            if (operation !== current || generation !== current.generation) return;
            current.impact = impact;
            $('known-delete-impact').replaceChildren();
            for (const [key, label] of [['photos', 'Enrolled photos'], ['embeddings', 'Face signatures'], ['sightings', 'Sightings'], ['files', 'Linked files'], ['watchlist_memberships', 'Watchlist entries'], ['live_alerts', 'Live alerts'], ['merged_aliases', 'Merged aliases']]) {
                const box = el('div'); box.append(el('strong', '', String(impact[key] || 0)), el('span', '', label));
                $('known-delete-impact').append(box);
            }
            if (impact.deletion_pending) operationError('A previous deletion is unfinished. Confirm again to retry the remaining cleanup. Reactivation is unavailable.');
        } catch (error) {
            if (operation === current && generation === current.generation) {
                $('known-delete-impact').textContent = ''; operationError(error.message);
            }
        }
        syncConfirmation();
    }
    function openOperation(kind) {
        if (!state.selected?.can_edit) return;
        operation = { kind, person: { ...state.selected }, impact: null, busy: false };
        const person = operation.person, deleting = kind === 'delete';
        $('known-confirm-title').textContent = deleting ? 'Permanently delete person?' : kind === 'activate' ? 'Reactivate person?' : 'Deactivate person?';
        $('known-confirm-description').textContent = deleting
            ? 'Review the affected records below. Permanent deletion cannot be undone.'
            : kind === 'activate' ? 'This person will be eligible for recognition again using their stored face signatures.'
                : 'This person will stop appearing as a known match. Their record, photos, and face signatures will remain available for reactivation.';
        $('known-confirm-person').replaceChildren(photo(person.photo_url, person.display_name, ''), el('span', '', person.display_name));
        $('known-delete-scope').hidden = $('known-delete-name-group').hidden = $('known-preview-refresh').hidden = !deleting;
        $('known-confirm-submit').textContent = deleting ? 'Delete permanently' : kind === 'activate' ? 'Reactivate person' : 'Deactivate person';
        $('known-delete-impact').replaceChildren();
        $('known-confirm-cancel').disabled = $('known-preview-refresh').disabled = false;
        operationError(''); syncConfirmation();
        $('known-confirm').hidden = false;
        window.ModalStack.open($('known-confirm'), { backdropClose: true,
            canClose: () => !operation?.busy,
            onClose: () => { $('known-confirm').hidden = true; operation = null; } });
        if (deleting) refreshPreview();
    }
    $('known-activation').addEventListener('click', () => openOperation(state.selected?.status === 'inactive' ? 'activate' : 'deactivate'));
    $('known-delete').addEventListener('click', () => openOperation('delete'));
    $('known-delete-name').addEventListener('input', syncConfirmation);
    $('known-preview-refresh').addEventListener('click', refreshPreview);
    $('known-confirm-cancel').addEventListener('click', () => window.ModalStack.close($('known-confirm')));
    $('known-confirm-submit').addEventListener('click', async () => {
        const current = operation;
        if (!current || $('known-confirm-submit').disabled) return;
        current.busy = true; syncConfirmation(); operationError('');
        $('known-confirm-cancel').disabled = $('known-preview-refresh').disabled = true;
        const deleting = current.kind === 'delete';
        $('known-confirm-submit').textContent = deleting ? 'Deleting…' : 'Saving…';
        try {
            const data = await api(`/api/admin/known-faces/${encodeURIComponent(current.person.id)}` + (deleting ? '' : '/activation'), {
                method: deleting ? 'DELETE' : 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(deleting ? { confirmation_name: $('known-delete-name').value, preview_token: current.impact.preview_token }
                    : { active: current.kind === 'activate' }) });
            current.busy = false;
            window.ModalStack.close($('known-confirm'));
            window.ModalStack.close($('known-detail'));
            notice(deleting ? (data.message || 'Person permanently deleted.') + (data.shared_files_retained ? ' Shared media used by other people was retained.' : '')
                : current.kind === 'activate' ? 'Person reactivated.' : 'Person deactivated. Their photos and face signatures were retained.');
            load();
        } catch (error) {
            current.busy = false;
            if (operation === current) {
                operationError(error.message);
                if (deleting) current.impact = null; // fresh preview + explicit confirmation before retry
                $('known-confirm-submit').textContent = deleting ? 'Delete permanently' : 'Confirm';
                $('known-confirm-cancel').disabled = $('known-preview-refresh').disabled = false;
                syncConfirmation();
            }
        }
    });
    $('known-query').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => { state.page = 1; load(); }, 250); });
    ['known-status', 'known-sort'].forEach(id => $(id).addEventListener('change', () => { state.page = 1; load(); }));
    $('known-refresh').addEventListener('click', load);
    $('known-prev').addEventListener('click', () => { state.page--; load(); });
    $('known-next').addEventListener('click', () => { state.page++; load(); });
    $('known-close').addEventListener('click', () => window.ModalStack.close($('known-detail')));
    $('known-add').addEventListener('click', () => openUpload());
    $('known-add-photo').addEventListener('click', () => { if (state.selected?.can_add_photo) openUpload(state.selected); });
    $('known-rename').addEventListener('submit', async event => {
        event.preventDefault();
        const person = state.selected;
        if (!person?.can_edit) return;
        const button = event.target.querySelector('button'); button.disabled = true;
        try {
            const data = await api('/api/admin/known-faces/' + encodeURIComponent(person.id), {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ display_name: $('known-name').value.trim() }) });
            person.display_name = data.display_name;
            if (state.selected?.id === person.id) {
                $('known-detail-name').textContent = data.display_name; $('known-name').value = data.display_name;
                notice('Name saved. Photos and person ID are unchanged.', false, true);
            }
            load();
        } catch (error) { if (state.selected?.id === person.id) notice(error.message, true, true); }
        finally { button.disabled = false; }
    });
    window.addEventListener('enrollment:saved', () => { load(); if (state.selected) loadGallery(); });
    load();
})();
