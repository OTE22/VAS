/* Browser-only reference photos for image-search matches. Never persisted as evidence. */
(() => {
    'use strict';
    const references = new WeakMap();
    const active = new Set();
    const handlers = new WeakMap();

    function createReference(file) {
        if (!(file instanceof Blob)) return null;
        const data = { file, url: '', released: false };
        const reference = Object.freeze({
            release() {
                if (data.url) URL.revokeObjectURL(data.url);
                data.url = '';
                data.file = null;
                data.released = true;
                active.delete(reference);
            }
        });
        references.set(reference, data);
        active.add(reference);
        return reference;
    }

    function referenceUrl(reference) {
        const data = references.get(reference);
        if (!data || data.released) return '';
        // Allocate one URL per submitted image only when a stored portrait is
        // unavailable, including HTTP/decode errors; share it across matches.
        if (!data.url) {
            try { data.url = URL.createObjectURL(data.file); } catch { return ''; }
        }
        return data.url;
    }

    function storedImageUrl(source) {
        if (typeof source !== 'string' || !source) return '';
        try {
            const url = new URL(source, location.origin + '/');
            return /^https?:$/.test(url.protocol) && url.origin === location.origin ? url.href : '';
        } catch { return ''; }
    }

    function render(img, caption, storedSource, reference, placeholder = '') {
        const previous = handlers.get(img);
        if (previous) img.removeEventListener('error', previous);
        // Own the error chain; actions.js must not replace this fallback first.
        ['data-fallback-src', 'data-fallback-icon', 'data-fallback-hide'].forEach(key => img.removeAttribute(key));
        const storedAlt = img.alt || 'Stored identity image';
        const storedFit = img.style.objectFit;
        const storedDisplay = img.style.display;
        const show = (source, kind) => {
            img.dataset.searchImageSource = kind;
            img.hidden = !source;
            img.style.display = source ? storedDisplay : 'none';
            caption.hidden = kind === 'stored';
            caption.style.display = kind === 'stored' ? 'none' : '';
            caption.textContent = kind === 'uploaded' ? 'Uploaded search image'
                : kind === 'unavailable' ? 'Image unavailable — history retained' : '';
            caption.title = kind === 'uploaded' ? 'Reference photo supplied for this search; not a stored camera image.' : '';
            img.alt = kind === 'stored' ? storedAlt : caption.textContent;
            img.style.objectFit = kind === 'uploaded' ? 'contain' : storedFit;
            if (source) img.src = source;
            else img.removeAttribute('src');
        };
        const unavailable = () => show(placeholder, 'unavailable');
        const uploaded = () => {
            const url = referenceUrl(reference);
            if (url) show(url, 'uploaded');
            else unavailable();
        };
        const onError = () => {
            if (img.dataset.searchImageSource === 'stored') uploaded();
            else if (img.dataset.searchImageSource === 'uploaded') unavailable();
            else { img.hidden = true; img.style.display = 'none'; img.removeAttribute('src'); }
        };
        handlers.set(img, onError);
        img.addEventListener('error', onError);
        const stored = storedImageUrl(storedSource);
        if (stored) show(stored, 'stored');
        else uploaded();
    }

    // Preserve URLs for pages in the back/forward cache. Owners also release
    // references when results are replaced, cleared, or a picker closes.
    window.addEventListener('pagehide', event => {
        if (!event.persisted) for (const reference of active) reference.release();
    });
    window.SearchImage = Object.freeze({ createReference, render });
})();
