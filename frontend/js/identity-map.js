/**
 * IdentityMapController — the ONE map renderer for the application.
 *
 * MapLibre GL JS (vendored, ESM) drawing GeoJSON from
 * GET /api/identities/{id}/map-data over the offline basemap Martin serves at
 * /maps/. No iframe, no backend-rendered HTML, no external host — ever.
 *
 * Loaded as `<script type="module">`; classic page scripts reach it through
 * `window.IdentityMap` after `await window.IdentityMap.ready`.
 *
 * Design rules (each one was a bug in the renderer this replaces):
 *
 *  - Application data lives in ITS OWN sources/layers, never in the basemap
 *    style. `setStyle()` wipes custom layers, so there is exactly ONE restore
 *    path (`_restoreOverlays`) run on every `style.load`; nothing is
 *    recomputed or refetched when the basemap changes.
 *  - Popups are built with the DOM (`textContent`), never innerHTML with
 *    backend strings interpolated in.
 *  - A style whose dataset is not installed is disabled from the availability
 *    API and, if requested anyway, shows OFFLINE_MAP_DATASET_UNAVAILABLE —
 *    it is never silently swapped for another style.
 *  - `destroy()` removes the map, listeners, popups and timers so re-selecting
 *    an identity does not leak a WebGL context.
 */

// Namespace import: the 6.x ESM build exposes NAMED exports only (no
// default) — `import maplibregl from` fails in the browser with "does not
// provide an export named 'default'". Verified against the vendored file.
import * as maplibregl from '/frontend/vendor/maplibre/maplibre-gl.mjs';

// Explicit, self-hosted worker (same origin) — stated, not inferred. 6.3.0
// resolves it relative to the main module anyway, but naming it here means a
// future re-vendoring cannot silently point elsewhere.
maplibregl.setWorkerUrl('/frontend/vendor/maplibre/maplibre-gl-worker.mjs');

// /frontend/ is cached `immutable, 1y` by nginx: a style change only reaches a
// browser through this version tag. Bump it whenever a style JSON changes
// (styles-3: Light and Dark are both generated from ONE Planetiler vector
// archive — same layers, same source, paint only differs — replacing the
// transitional raster Light that turned out to be placeholder images).
const STYLE_VERSION = 'styles-3';
const STYLE_URL = (name) => `/frontend/maps/styles/${name}.json?v=${STYLE_VERSION}`;
const STYLES = ['light', 'dark', 'satellite', 'terrain'];
const UNAVAILABLE = 'OFFLINE_MAP_DATASET_UNAVAILABLE';

// Overlay source/layer ids. One list, used for add AND remove.
const SRC = {
    detections: 'ae-detections',
    route: 'ae-route',
    cameras: 'ae-cameras',
    risk: 'ae-risk',
    zones: 'ae-zones',
    patterns: 'ae-patterns',
    threats: 'ae-threats',
};
const LAYER_ORDER = [
    'ae-zones-fill', 'ae-zones-line',
    'ae-risk-heat',
    'ae-route-line', 'ae-route-arrows',
    'ae-patterns-line', 'ae-patterns-point',
    'ae-detections-clusters', 'ae-detections-cluster-count', 'ae-detections-point',
    'ae-threats-point',
    'ae-cameras-point',
];

function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
}

function fmtTs(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
}

export class IdentityMapController {
    /**
     * @param {HTMLElement} container
     * @param {object} opts { style, onError(kind, detail), onReady() }
     */
    constructor(container, opts = {}) {
        this.container = container;
        this.opts = opts;
        this.map = null;
        this.style = STYLES.includes(opts.style) ? opts.style : 'light';
        this.data = null;               // last map-data payload
        this.flags = { popups: true, cluster: true, routes: true, security: false,
                       patterns: false, risk: false, timeline: false, avatar: false };
        this._timeline = null;          // { el, range, label, playBtn, timer, idx }
        this._avatarMarker = null;
        this.availability = null;
        this._popup = null;
        this._handlers = [];
        this._destroyed = false;
    }

    // ---------------------------------------------------------------- lifecycle
    async init() {
        this.container.replaceChildren();
        this.container.classList.add('ae-map');
        // Decide the OPENING style from availability, not from a hard-coded
        // default: the transitional raster street archive turned out to be
        // 145,718 copies of OpenStreetMap's "Access blocked" placeholder, so
        // opening on Light painted that image across the viewport before the
        // dropdown had a chance to disable it. Availability is fetched once
        // here; loadAvailability(selectEl) later reuses it to paint the picker.
        if (!this.availability) await this.loadAvailability();
        this.style = this._firstUsableStyle(this.style);
        this.map = new maplibregl.Map({
            container: this.container,
            style: STYLE_URL(this.style),
            center: [35.85, 33.87],
            zoom: 8,
            minZoom: 6,
            maxZoom: 18,
            attributionControl: { compact: true },
            // Everything is same-origin; no transformRequest needed and none
            // is defined, so a stray absolute URL cannot be rewritten to pass.
        });
        this.map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
        this.map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');

        // THE single restore path. Fires on first load and after every
        // setStyle(); overlays are re-attached from cached data.
        this._on(this.map, 'style.load', () => this._restoreOverlays());
        this._on(this.map, 'error', (e) => {
            const err = e && e.error;
            const msg = err && err.message ? err.message : String(err || 'map error');
            // Tile 204s/404s are coverage gaps, not failures; report the rest.
            if (/status.*(204|404)/i.test(msg)) return;
            console.warn('[IdentityMap]', msg);
            if (this.opts.onError) this.opts.onError('map', msg);
        });
        await new Promise((resolve) => this.map.once('load', resolve));
        if (this.opts.onReady) this.opts.onReady();
        return this;
    }

    destroy() {
        this._destroyed = true;
        this._closePopup();
        this._teardownTimeline();
        for (const [target, evt, fn] of this._handlers) {
            try { target.off ? target.off(evt, fn) : target.removeEventListener(evt, fn); } catch (_) { /* gone */ }
        }
        this._handlers = [];
        if (this.map) { try { this.map.remove(); } catch (_) { /* already removed */ } }
        this.map = null;
        this.data = null;
        this.container.classList.remove('ae-map');
    }

    _on(target, evt, fn) {
        target.on ? target.on(evt, fn) : target.addEventListener(evt, fn);
        this._handlers.push([target, evt, fn]);
    }

    // ---------------------------------------------------------------- basemap
    /** Fetch availability once; disable unavailable options in a <select>. */
    async loadAvailability(selectEl) {
        try {
            const resp = await fetch('/api/maps/availability', { credentials: 'include', cache: 'no-store',
                                                                 headers: { Accept: 'application/json' } });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            this.availability = await resp.json();
        } catch (err) {
            console.warn('[IdentityMap] availability unknown:', err && err.message);
            this.availability = null;
        }
        if (selectEl && this.availability && this.availability.styles) {
            const detail = this.availability.detail || {};
            for (const opt of Array.from(selectEl.options)) {
                const ok = !!this.availability.styles[opt.value];
                opt.disabled = !ok;
                // Say WHY, from the server. This used to read "dataset not
                // installed" for every failure — which was wrong for the case
                // that mattered most: an archive that was installed and served
                // an error image. The backend now names the cause.
                opt.title = ok ? '' : this.unavailableReason(opt.value);
                if (!ok && !/unavailable/i.test(opt.textContent)) opt.textContent += ' (unavailable)';
            }
        }
        return this.availability;
    }

    /** A human sentence for why `name` cannot be shown, with its code. */
    unavailableReason(name) {
        const detail = (this.availability && this.availability.detail
                        && this.availability.detail[name]) || {};
        if (!detail.reason) return `${UNAVAILABLE}: reason unavailable`;
        return detail.reason_text
            ? `${detail.reason}: ${detail.reason_text}`
            : `${UNAVAILABLE}: ${detail.reason}`;
    }

    isStyleAvailable(name) {
        // Availability unknown (endpoint down): allow the requested style and
        // let map errors surface rather than crippling the picker. Assuming
        // "only light works" was the pre-migration default and is now exactly
        // backwards — light is the style whose archive can be a placeholder.
        if (!this.availability || !this.availability.styles) return STYLES.includes(name);
        return !!this.availability.styles[name];
    }

    /** The preferred style if its data is real, else the first one that is. */
    _firstUsableStyle(preferred) {
        if (this.isStyleAvailable(preferred)) return preferred;
        const usable = STYLES.find((name) => this.isStyleAvailable(name));
        if (usable) {
            console.warn(`[IdentityMap] ${preferred} is ${UNAVAILABLE}; opening ${usable}`);
            if (this.opts.onError) this.opts.onError('dataset', { code: UNAVAILABLE, style: preferred, opened: usable });
            return usable;
        }
        // Nothing installed: keep the request so the failure is visible and
        // reported once, instead of silently pretending a basemap exists.
        if (this.opts.onError) this.opts.onError('dataset', { code: UNAVAILABLE, style: preferred, opened: null });
        return preferred;
    }

    /**
     * Change the basemap. Overlays are restored by the style.load handler —
     * this method never touches them directly, so there is one restore path.
     * A style whose dataset is missing is REFUSED with a deterministic state.
     */
    async setBasemap(name) {
        if (!STYLES.includes(name)) throw new Error(`unknown style: ${name}`);
        if (!this.isStyleAvailable(name)) {
            const detail = { code: UNAVAILABLE, style: name };
            if (this.opts.onError) this.opts.onError('dataset', detail);
            throw Object.assign(new Error(UNAVAILABLE), detail);
        }
        if (name === this.style) return;
        this.style = name;
        // diff:false — a full swap; MapLibre's diff mode cannot carry our
        // overlay layers across styles reliably, the restore path can.
        this.map.setStyle(STYLE_URL(name), { diff: false });
        await new Promise((resolve) => this.map.once('style.load', resolve));
    }

    // ---------------------------------------------------------------- data
    /**
     * Load map data for an identity and draw it. `params` are the exact
     * server-side flags (defaults OFF for security overlays); `flags` are the
     * client-side display switches.
     */
    async load(identityId, params = {}, flags = {}) {
        Object.assign(this.flags, flags);
        const url = new URL(`/api/identities/${encodeURIComponent(identityId)}/map-data`, window.location.origin);
        for (const k of Object.keys(params)) {
            const v = params[k];
            if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v));
        }
        const resp = await fetch(url.pathname + url.search, {
            credentials: 'include', cache: 'no-store', headers: { Accept: 'application/json' },
        });
        if (resp.status === 401) { window.location.href = '/login'; return null; }
        if (!resp.ok) {
            const body = await resp.json().catch(() => ({}));
            const e = new Error(body.detail || `HTTP ${resp.status}`);
            e.status = resp.status;
            throw e;
        }
        this.data = await resp.json();
        this._restoreOverlays();
        this.fitToData();
        return this.data;
    }

    fitToData() {
        const b = this.data && this.data.metadata && this.data.metadata.bounds;
        if (!b || !this.map) return;
        const [[w, s], [e, n]] = b;
        if (w === e && s === n) { this.map.easeTo({ center: [w, s], zoom: 15 }); return; }
        this.map.fitBounds([[w, s], [e, n]], { padding: 60, maxZoom: 16, duration: 600 });
    }

    // ---------------------------------------------------------------- overlays
    _clearOverlays() {
        if (!this.map || !this.map.getStyle()) return;
        for (const id of LAYER_ORDER) if (this.map.getLayer(id)) this.map.removeLayer(id);
        for (const id of Object.values(SRC)) if (this.map.getSource(id)) this.map.removeSource(id);
    }

    _restoreOverlays() {
        if (!this.map || !this.data || this._destroyed) return;
        // Deliberately NOT gated on map.isStyleLoaded(): that stays false until
        // every source has fetched its tiles, which on `style.load` for a real
        // basemap swap is never yet the case — and `style.load` does not fire
        // a second time. addSource/addLayer are legal as soon as the style JSON
        // is parsed, i.e. exactly when `style.load` fires. Proven in headless
        // Chrome: light → terrain kept all 9 overlay layers only after this
        // gate was removed (it emptied them before).
        this._clearOverlays();
        const d = this.data;
        const f = this.flags;

        // Zones (below everything)
        if (f.security && d.security_zones && d.security_zones.features.length) {
            this.map.addSource(SRC.zones, { type: 'geojson', data: d.security_zones });
            this.map.addLayer({ id: 'ae-zones-fill', type: 'fill', source: SRC.zones,
                paint: { 'fill-color': ['match', ['get', 'zone_type'],
                            'restricted', '#e53935', 'high_security', '#fb8c00', 'monitored', '#fdd835', 'safe', '#43a047', '#9e9e9e'],
                         'fill-opacity': ['case', ['<', ['get', 'risk_level'], 5], 0.18, 0.32] } });
            this.map.addLayer({ id: 'ae-zones-line', type: 'line', source: SRC.zones,
                paint: { 'line-color': '#212121', 'line-width': 1, 'line-opacity': 0.5 } });
        }
        // Risk heatmap
        if (f.risk && d.risk_points && d.risk_points.length) {
            this.map.addSource(SRC.risk, { type: 'geojson', data: {
                type: 'FeatureCollection',
                features: d.risk_points.map(([lng, lat, w]) => ({
                    type: 'Feature', geometry: { type: 'Point', coordinates: [lng, lat] },
                    properties: { w: w } })) } });
            this.map.addLayer({ id: 'ae-risk-heat', type: 'heatmap', source: SRC.risk,
                paint: { 'heatmap-weight': ['get', 'w'], 'heatmap-radius': 25, 'heatmap-opacity': 0.75,
                         'heatmap-color': ['interpolate', ['linear'], ['heatmap-density'],
                            0, 'rgba(0,0,255,0)', 0.2, 'blue', 0.4, 'lime', 0.6, 'orange', 1, 'red'] } });
        }
        // Route
        if (f.routes && d.route && d.route.features.length) {
            this.map.addSource(SRC.route, { type: 'geojson', data: d.route });
            this.map.addLayer({ id: 'ae-route-line', type: 'line', source: SRC.route,
                layout: { 'line-join': 'round', 'line-cap': 'round' },
                paint: { 'line-color': '#1e88e5', 'line-width': 3, 'line-opacity': 0.85 } });
            // Direction: a chevron symbol along the line (built-in text glyph
            // would need a glyph source; a small circle-dash keeps it local).
            this.map.addLayer({ id: 'ae-route-arrows', type: 'line', source: SRC.route,
                paint: { 'line-color': '#ffffff', 'line-width': 1.2, 'line-dasharray': [0.5, 2.5], 'line-opacity': 0.9 } });
        }
        // Patterns
        if (f.patterns && d.patterns && d.patterns.features.length) {
            this.map.addSource(SRC.patterns, { type: 'geojson', data: d.patterns });
            this.map.addLayer({ id: 'ae-patterns-line', type: 'line', source: SRC.patterns,
                filter: ['==', ['geometry-type'], 'LineString'],
                paint: { 'line-color': ['match', ['get', 'pattern_type'], 'backtracking', '#d32f2f',
                            'rapid_movement', '#f57c00', '#7b1fa2'], 'line-width': 4, 'line-dasharray': [1, 1] } });
            this.map.addLayer({ id: 'ae-patterns-point', type: 'circle', source: SRC.patterns,
                filter: ['==', ['geometry-type'], 'Point'],
                paint: { 'circle-radius': 18, 'circle-color': '#7b1fa2', 'circle-opacity': 0.25,
                         'circle-stroke-color': '#7b1fa2', 'circle-stroke-width': 2 } });
        }
        // Detections (clustered)
        if (d.detections && d.detections.features.length) {
            this.map.addSource(SRC.detections, { type: 'geojson', data: d.detections,
                cluster: !!f.cluster && d.detections.features.length > 5,
                clusterRadius: 45, clusterMaxZoom: 15 });
            this.map.addLayer({ id: 'ae-detections-clusters', type: 'circle', source: SRC.detections,
                filter: ['has', 'point_count'],
                paint: { 'circle-color': ['step', ['get', 'point_count'], '#66bb6a', 10, '#ffa726', 30, '#ef5350'],
                         'circle-radius': ['step', ['get', 'point_count'], 16, 10, 22, 30, 28],
                         'circle-stroke-width': 2, 'circle-stroke-color': '#ffffff' } });
            this.map.addLayer({ id: 'ae-detections-cluster-count', type: 'circle', source: SRC.detections,
                filter: ['has', 'point_count'],
                paint: { 'circle-radius': 4, 'circle-color': '#ffffff', 'circle-opacity': 0.9 } });
            this.map.addLayer({ id: 'ae-detections-point', type: 'circle', source: SRC.detections,
                filter: ['!', ['has', 'point_count']],
                paint: { 'circle-radius': 7, 'circle-color': '#1e88e5',
                         'circle-stroke-width': 2, 'circle-stroke-color': '#ffffff' } });
        }
        // Threats
        if (f.security && d.threats && d.threats.features.length) {
            this.map.addSource(SRC.threats, { type: 'geojson', data: d.threats });
            this.map.addLayer({ id: 'ae-threats-point', type: 'circle', source: SRC.threats,
                paint: { 'circle-radius': ['interpolate', ['linear'], ['get', 'threat_level'], 3, 9, 9, 15],
                         'circle-color': '#d50000', 'circle-opacity': 0.35,
                         'circle-stroke-color': '#d50000', 'circle-stroke-width': 2 } });
        }
        // Cameras (top)
        if (d.cameras && d.cameras.features.length) {
            this.map.addSource(SRC.cameras, { type: 'geojson', data: d.cameras });
            this.map.addLayer({ id: 'ae-cameras-point', type: 'circle', source: SRC.cameras,
                paint: { 'circle-radius': 5, 'circle-color': '#212121',
                         'circle-stroke-width': 2, 'circle-stroke-color': '#ffeb3b' } });
        }
        this._wireInteractions();
        this._syncTimeline();
    }

    _wireInteractions() {
        const map = this.map;
        const clickable = ['ae-detections-point', 'ae-cameras-point', 'ae-detections-clusters',
                           'ae-threats-point', 'ae-patterns-point'];
        for (const id of clickable) {
            if (!map.getLayer(id)) continue;
            // MapLibre dedupes identical (layer, handler) pairs poorly across
            // style swaps, so handlers are tracked and removed in destroy().
            const enter = () => { map.getCanvas().style.cursor = 'pointer'; };
            const leave = () => { map.getCanvas().style.cursor = ''; };
            const click = (e) => this._onFeatureClick(id, e);
            map.on('mouseenter', id, enter); map.on('mouseleave', id, leave); map.on('click', id, click);
            this._handlers.push([{ off: (evt, fn) => map.off(evt, id, fn) }, 'mouseenter', enter],
                                [{ off: (evt, fn) => map.off(evt, id, fn) }, 'mouseleave', leave],
                                [{ off: (evt, fn) => map.off(evt, id, fn) }, 'click', click]);
        }
    }

    _onFeatureClick(layerId, e) {
        const feat = e.features && e.features[0];
        if (!feat) return;
        if (layerId === 'ae-detections-clusters') {
            const src = this.map.getSource(SRC.detections);
            src.getClusterExpansionZoom(feat.properties.cluster_id).then((zoom) => {
                this.map.easeTo({ center: feat.geometry.coordinates, zoom });
            }).catch(() => {});
            return;
        }
        if (!this.flags.popups) return;
        this._closePopup();
        this._popup = new maplibregl.Popup({ closeButton: true, maxWidth: '320px' })
            .setLngLat(feat.geometry.coordinates)
            .setDOMContent(this._popupContent(layerId, feat.properties))
            .addTo(this.map);
    }

    _popupContent(layerId, p) {
        const box = el('div', 'ae-popup');
        if (layerId === 'ae-cameras-point') {
            box.appendChild(el('div', 'ae-popup-title', p.pipeline_name || p.pipeline_id));
            box.appendChild(el('div', 'ae-popup-row', `Camera • ${p.visits} detection(s)`));
        } else if (layerId === 'ae-threats-point') {
            box.appendChild(el('div', 'ae-popup-title', `Watchlist: ${p.list_name}`));
            box.appendChild(el('div', 'ae-popup-row', `Threat level ${p.threat_level}`));
            box.appendChild(el('div', 'ae-popup-row', fmtTs(p.timestamp)));
        } else if (layerId === 'ae-patterns-point') {
            box.appendChild(el('div', 'ae-popup-title', p.pattern_type));
            box.appendChild(el('div', 'ae-popup-row', p.description || ''));
        } else {
            box.appendChild(el('div', 'ae-popup-title', p.pipeline_name || 'Detection'));
            box.appendChild(el('div', 'ae-popup-row', fmtTs(p.timestamp)));
            if (p.duration_at_location) box.appendChild(el('div', 'ae-popup-row', `Duration: ${p.duration_at_location} min`));
            if (p.snapshot_url) {
                const img = el('img', 'ae-popup-img');
                img.alt = 'Detection snapshot';
                img.loading = 'lazy';
                // Same-origin app URL from the backend; assigned as a property,
                // not interpolated into markup.
                img.src = p.snapshot_url;
                box.appendChild(img);
            }
        }
        return box;
    }

    // ---------------------------------------------------------------- timeline / avatar
    //
    // Replaces Folium's TimestampedGeoJson + the animated avatar. Purely
    // client-side over the chronological `detections` the API already
    // returns — no extra request, and the basemap style can change underneath
    // it because the avatar is a DOM Marker, not a style layer.
    _syncTimeline() {
        const want = (this.flags.timeline || this.flags.avatar) &&
                     this.data && this.data.detections.features.length > 1;
        if (!want) { this._teardownTimeline(); return; }
        if (!this._timeline) this._buildTimeline();
        this._setTimelineIndex(this._timeline.idx, /*silent*/ true);
    }

    _buildTimeline() {
        const wrap = el('div', 'ae-timeline');
        const playBtn = el('button', 'ae-timeline-play', '▶');
        playBtn.type = 'button'; playBtn.title = 'Play / pause';
        const range = el('input', 'ae-timeline-range');
        range.type = 'range'; range.min = '0'; range.step = '1';
        const label = el('div', 'ae-timeline-label', '');
        wrap.append(playBtn, range, label);
        this.container.appendChild(wrap);

        const onInput = () => this._setTimelineIndex(parseInt(range.value, 10) || 0);
        const onPlay = () => this._toggleTimelinePlay();
        range.addEventListener('input', onInput);
        playBtn.addEventListener('click', onPlay);
        this._handlers.push([range, 'input', onInput], [playBtn, 'click', onPlay]);
        this._timeline = { el: wrap, range, label, playBtn, timer: null, idx: 0 };
    }

    _teardownTimeline() {
        if (this._timeline) {
            if (this._timeline.timer) window.clearInterval(this._timeline.timer);
            this._timeline.el.remove();
            this._timeline = null;
        }
        if (this._avatarMarker) { try { this._avatarMarker.remove(); } catch (_) { /* gone */ } this._avatarMarker = null; }
    }

    _toggleTimelinePlay() {
        const t = this._timeline;
        if (!t) return;
        if (t.timer) { window.clearInterval(t.timer); t.timer = null; t.playBtn.textContent = '▶'; return; }
        t.playBtn.textContent = '❚❚';
        t.timer = window.setInterval(() => {
            const n = this.data.detections.features.length;
            const next = (t.idx + 1) % n;
            this._setTimelineIndex(next);
            if (next === n - 1) { window.clearInterval(t.timer); t.timer = null; t.playBtn.textContent = '▶'; }
        }, 900);
    }

    _setTimelineIndex(idx, silent) {
        const t = this._timeline;
        if (!t || !this.map || !this.data) return;
        const feats = this.data.detections.features;
        const n = feats.length;
        idx = Math.max(0, Math.min(n - 1, idx | 0));
        t.idx = idx;
        t.range.max = String(n - 1);
        t.range.value = String(idx);
        const f = feats[idx];
        const p = f.properties;
        t.label.textContent = `${idx + 1}/${n} · ${p.pipeline_name || ''} · ${fmtTs(p.timestamp)}`;

        // Timeline mode dims detections after the cursor via a filter on the
        // point layer (a style expression, so it survives nothing — but
        // _restoreOverlays calls _syncTimeline, so it is re-applied).
        if (this.flags.timeline && this.map.getLayer('ae-detections-point')) {
            this.map.setPaintProperty('ae-detections-point', 'circle-opacity',
                ['case', ['<=', ['get', 'seq'], p.seq], 1, 0.15]);
            this.map.setPaintProperty('ae-detections-point', 'circle-stroke-opacity',
                ['case', ['<=', ['get', 'seq'], p.seq], 1, 0.15]);
        }
        // Avatar: a DOM marker at the current position.
        if (this.flags.avatar) {
            if (!this._avatarMarker) {
                const pin = el('div', 'ae-avatar');
                pin.title = 'Current position';
                this._avatarMarker = new maplibregl.Marker({ element: pin, anchor: 'center' })
                    .setLngLat(f.geometry.coordinates).addTo(this.map);
            } else {
                this._avatarMarker.setLngLat(f.geometry.coordinates);
            }
            if (!silent) this.map.easeTo({ center: f.geometry.coordinates, duration: 500 });
        }
    }

    _closePopup() {
        if (this._popup) { try { this._popup.remove(); } catch (_) { /* fine */ } this._popup = null; }
    }
}

/**
 * The style URL of the first basemap whose data is REAL, or null when none is.
 *
 * For simple pickers (the pipelines coordinate map) that want a basemap without
 * the whole overlay controller. Availability decides; the caller never names a
 * style file, so a style whose archive is missing or placeholder-backed is
 * never requested. `onUnavailable` receives the backend's own reason.
 */
async function firstUsableStyleUrl(preferred = 'light', onUnavailable = null) {
    let availability = null;
    try {
        const resp = await fetch('/api/maps/availability', { credentials: 'include', cache: 'no-store',
                                                             headers: { Accept: 'application/json' } });
        if (resp.ok) availability = await resp.json();
    } catch (err) {
        console.warn('[IdentityMap] availability unknown:', err && err.message);
    }
    const styles = availability && availability.styles;
    // Availability unknown (endpoint down): honour the request and let map
    // errors surface, rather than refusing to draw anything.
    if (!styles) return STYLE_URL(STYLES.includes(preferred) ? preferred : 'light');
    const order = [preferred, ...STYLES.filter((s) => s !== preferred)];
    const usable = order.find((name) => styles[name]);
    if (!usable) {
        const detail = (availability.detail && availability.detail[preferred]) || {};
        if (onUnavailable) onUnavailable({ code: UNAVAILABLE, style: preferred, reason: detail.reason || null });
        return null;
    }
    if (usable !== preferred && onUnavailable) {
        const detail = (availability.detail && availability.detail[preferred]) || {};
        onUnavailable({ code: UNAVAILABLE, style: preferred, opened: usable, reason: detail.reason || null });
    }
    return STYLE_URL(usable);
}

// Classic-script bridge.
window.IdentityMap = { Controller: IdentityMapController, STYLES, UNAVAILABLE, maplibregl,
                       firstUsableStyleUrl, STYLE_URL };
window.IdentityMap.ready = Promise.resolve(window.IdentityMap);
document.dispatchEvent(new CustomEvent('identity-map:ready'));
