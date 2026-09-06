/* Stable face previews shared by the live feed and Unknown Faces cards. */
(() => {
    'use strict';
    const states = new WeakMap();

    function update(img, source, placeholder) {
        let state = states.get(img);
        if (!state) {
            state = { requested: null, shown: null, revision: 0, loader: null };
            states.set(img, state);
            img.hidden = true;
        }
        if (source === state.requested) return;
        state.requested = source;
        const revision = ++state.revision;
        if (state.loader) {
            state.loader.onload = state.loader.onerror = null;
            state.loader.src = '';
            state.loader = null;
        }
        const failed = () => {
            if (revision !== state.revision) return;
            state.loader = null;
            // Keep the last successfully decoded image on a failed update.
            img.dataset.imageState = state.shown ? 'previous' : 'unavailable';
            if (placeholder) {
                placeholder.hidden = !!state.shown;
                placeholder.textContent = source ? 'Image unavailable' : 'No image available';
            }
        };
        if (!source || typeof source !== 'string') { failed(); return; }
        // Snapshots stay same-origin; live messages may carry raster data URLs.
        const dataImage = /^data:image\/(?:jpeg|png|webp);base64,[A-Za-z0-9+/=\s]+$/.test(source);
        let url;
        try { url = new URL(source, location.origin + '/'); } catch { failed(); return; }
        if (!dataImage && (url.origin !== location.origin || !/^https?:$/.test(url.protocol))) {
            failed(); return;
        }
        if (state.shown === url.href) {
            img.dataset.imageState = 'ready';
            return;
        }
        if (placeholder && !state.shown) {
            placeholder.hidden = false;
            placeholder.textContent = 'Loading image…';
        }
        img.dataset.imageState = 'loading';
        const loader = new Image();
        state.loader = loader;
        loader.decoding = 'async';
        loader.onerror = failed;
        loader.onload = async () => {
            try { await loader.decode(); } catch { failed(); return; }
            if (revision !== state.revision) return;
            // One image element for its entire lifetime: no clone, no measured
            // pixel heights, no fading to blank, and no late response wins.
            img.src = url.href;
            state.shown = url.href;
            state.loader = null;
            img.hidden = false;
            img.dataset.imageState = 'ready';
            if (placeholder) placeholder.hidden = true;
        };
        loader.src = url.href;
    }
    window.FaceImage = Object.freeze({ update });
})();
