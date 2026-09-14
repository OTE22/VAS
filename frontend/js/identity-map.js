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
    progress: 'ae-route-progress',
};
const LAYER_ORDER = [
    'ae-zones-fill', 'ae-zones-line',
    'ae-risk-heat',
    'ae-route-casing', 'ae-route-glow', 'ae-route-line', 'ae-route-arrows',
    'ae-route-progress-glow', 'ae-route-progress-line',
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
        this._timeline = null;          // playback HUD + current detection index
        this._avatarMarker = null;
        this._playbackFrame = null;
        this._playbackResolve = null;
        this._autoPlayTimer = null;
        this._playbackRun = 0;
        this._overlayHandlers = [];
        this._autoPlayedData = null;
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
        this._clearOverlayHandlers();
        for (const [target, evt, fn] of this._handlers) {
            try { target.off ? target.off(evt, fn) : target.removeEventListener(evt, fn); } catch (_) { /* gone */ }
        }
        this._handlers = [];
        if (this.map) { try { this.map.remove(); } catch (_) { /* already removed */ } }
        this.map = null;
        this.data = null;
        this.container.classList.remove('ae-map');
    }

    /** Recalculate the canvas after a hidden map view becomes visible. */
    resize() {
        if (!this.map) return;
        this.map.resize();
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
        this._autoPlayedData = null;
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
        this._clearOverlayHandlers();
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
            this.map.addLayer({ id: 'ae-route-casing', type: 'line', source: SRC.route,
                layout: { 'line-join': 'round', 'line-cap': 'round' },
                paint: { 'line-color': '#03151d', 'line-width': 9, 'line-opacity': 0.72 } });
            this.map.addLayer({ id: 'ae-route-glow', type: 'line', source: SRC.route,
                layout: { 'line-join': 'round', 'line-cap': 'round' },
                paint: { 'line-color': '#00e5ff', 'line-width': 8, 'line-blur': 7, 'line-opacity': 0.32 } });
            this.map.addLayer({ id: 'ae-route-line', type: 'line', source: SRC.route,
                layout: { 'line-join': 'round', 'line-cap': 'round' },
                paint: { 'line-color': '#25b8ff', 'line-width': 4, 'line-opacity': 0.82 } });
            // Direction: a chevron symbol along the line (built-in text glyph
            // would need a glyph source; a small circle-dash keeps it local).
            this.map.addLayer({ id: 'ae-route-arrows', type: 'line', source: SRC.route,
                paint: { 'line-color': '#d9fbff', 'line-width': 1.4, 'line-dasharray': [0.8, 2.8], 'line-opacity': 0.9 } });
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

    _clearOverlayHandlers() {
        for (const remove of this._overlayHandlers.splice(0)) {
            try { remove(); } catch (_) { /* style or map already gone */ }
        }
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
            this._overlayHandlers.push(() => map.off('mouseenter', id, enter),
                                       () => map.off('mouseleave', id, leave),
                                       () => map.off('click', id, click));
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

    // ---------------------------------------------------------------- movement playback
    // Smooth, requestAnimationFrame-driven interpolation over the chronological
    // detections already returned by the API. Playback time is deliberately
    // compressed: real gaps can span hours, while every segment remains long
    // enough to read and short enough to investigate interactively.
    _syncTimeline() {
        const want = (this.flags.timeline || this.flags.avatar) &&
                     this.data && this.data.detections.features.length > 1;
        if (!want) { this._teardownTimeline(); return; }
        if (!this.flags.avatar && this._avatarMarker) {
            try { this._avatarMarker.remove(); } catch (_) { /* already gone */ }
            this._avatarMarker = null;
        }
        if (!this._timeline) this._buildTimeline();
        this._ensureProgressLayers();
        this._setTimelineIndex(this._timeline.idx, /*silent*/ true);

        const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (!reduced && this._autoPlayedData !== this.data) {
            this._autoPlayedData = this.data;
            if (this._autoPlayTimer) window.clearTimeout(this._autoPlayTimer);
            this._autoPlayTimer = window.setTimeout(() => {
                this._autoPlayTimer = null;
                if (this._timeline && !this._timeline.playing) this._startPlayback();
            }, 650);
        }
    }

    _buildTimeline() {
        const wrap = el('div', 'ae-timeline');
        wrap.setAttribute('role', 'region');
        wrap.setAttribute('aria-label', 'Movement playback');
        const summary = el('div', 'ae-timeline-summary');
        const status = el('div', 'ae-timeline-status', 'Ready');
        status.setAttribute('aria-live', 'polite');
        summary.append(el('span', 'ae-timeline-kicker', 'MOVEMENT REPLAY'), status);

        const controls = el('div', 'ae-timeline-controls');
        const prevBtn = el('button', 'ae-timeline-skip', '‹');
        prevBtn.type = 'button'; prevBtn.title = 'Previous detection'; prevBtn.setAttribute('aria-label', 'Previous detection');
        const playBtn = el('button', 'ae-timeline-play', '▶');
        playBtn.type = 'button'; playBtn.title = 'Play movement'; playBtn.setAttribute('aria-label', 'Play movement');
        const nextBtn = el('button', 'ae-timeline-skip', '›');
        nextBtn.type = 'button'; nextBtn.title = 'Next detection'; nextBtn.setAttribute('aria-label', 'Next detection');
        const range = el('input', 'ae-timeline-range');
        range.type = 'range'; range.min = '0'; range.step = '1'; range.setAttribute('aria-label', 'Detection timeline');
        const speed = el('select', 'ae-timeline-speed');
        speed.setAttribute('aria-label', 'Playback speed');
        for (const value of ['0.5', '1', '2', '4']) {
            const option = el('option', '', `${value}×`);
            option.value = value;
            if (value === '1') option.selected = true;
            speed.appendChild(option);
        }
        const overviewBtn = el('button', 'ae-timeline-overview', 'Overview');
        overviewBtn.type = 'button'; overviewBtn.title = 'Fit the complete route';
        const label = el('div', 'ae-timeline-label', '');
        controls.append(prevBtn, playBtn, nextBtn, range, speed, overviewBtn);
        wrap.append(summary, controls, label);
        this.container.appendChild(wrap);

        const onInput = () => {
            this._cancelPlayback();
            this._setTimelineIndex(parseInt(range.value, 10) || 0);
        };
        const onPlay = () => this._toggleTimelinePlay();
        const onPrev = () => { this._cancelPlayback(); this._setTimelineIndex(this._timeline.idx - 1); };
        const onNext = () => { this._cancelPlayback(); this._setTimelineIndex(this._timeline.idx + 1); };
        const onOverview = () => this.fitToData();
        range.addEventListener('input', onInput);
        playBtn.addEventListener('click', onPlay);
        prevBtn.addEventListener('click', onPrev);
        nextBtn.addEventListener('click', onNext);
        overviewBtn.addEventListener('click', onOverview);
        this._timeline = { el: wrap, range, label, status, playBtn, prevBtn, nextBtn,
                           speed, overviewBtn, idx: 0, playing: false, follow: true };
    }

    _teardownTimeline() {
        this._cancelPlayback();
        if (this._autoPlayTimer) { window.clearTimeout(this._autoPlayTimer); this._autoPlayTimer = null; }
        if (this._timeline) {
            this._timeline.el.remove();
            this._timeline = null;
        }
        if (this._avatarMarker) { try { this._avatarMarker.remove(); } catch (_) { /* gone */ } this._avatarMarker = null; }
    }

    _toggleTimelinePlay() {
        const t = this._timeline;
        if (!t) return;
        if (t.playing) this._cancelPlayback(); else this._startPlayback();
    }

    _startPlayback() {
        const t = this._timeline;
        const feats = this.data && this.data.detections && this.data.detections.features;
        if (!t || !feats || feats.length < 2) return;
        this._cancelPlayback();
        if (t.idx >= feats.length - 1) this._setTimelineIndex(0, true);
        t.playing = true;
        t.playBtn.textContent = 'Ⅱ';
        t.playBtn.title = 'Pause movement';
        t.playBtn.setAttribute('aria-label', 'Pause movement');
        t.status.textContent = 'Playing';
        const run = ++this._playbackRun;
        const advance = async () => {
            while (this._timeline === t && t.playing && run === this._playbackRun && t.idx < feats.length - 1) {
                const completed = await this._animateSegment(t.idx, t.idx + 1, run);
                if (!completed) return;
            }
            if (this._timeline === t && run === this._playbackRun) {
                t.playing = false;
                this._setPlayButtonIdle();
                t.status.textContent = 'Replay complete';
            }
        };
        advance();
    }

    _cancelPlayback() {
        this._playbackRun += 1;
        if (this._playbackFrame !== null) window.cancelAnimationFrame(this._playbackFrame);
        this._playbackFrame = null;
        if (this._playbackResolve) { this._playbackResolve(false); this._playbackResolve = null; }
        if (this._timeline) {
            this._timeline.playing = false;
            this._setPlayButtonIdle();
            this._timeline.status.textContent = 'Paused';
        }
    }

    _setPlayButtonIdle() {
        if (!this._timeline) return;
        this._timeline.playBtn.textContent = '▶';
        this._timeline.playBtn.title = this._timeline.idx >= Number(this._timeline.range.max) ? 'Replay movement' : 'Play movement';
        this._timeline.playBtn.setAttribute('aria-label', this._timeline.playBtn.title);
    }

    _segmentDuration(from, to) {
        const a = new Date(from.properties.timestamp).getTime();
        const b = new Date(to.properties.timestamp).getTime();
        const minutes = Number.isFinite(a) && Number.isFinite(b) ? Math.max(0, (b - a) / 60000) : 1;
        const base = Math.min(3200, 1200 + Math.log2(minutes + 1) * 260);
        const speed = this._timeline ? Number(this._timeline.speed.value) || 1 : 1;
        return Math.max(320, base / speed);
    }

    _animateSegment(fromIdx, toIdx, run) {
        const feats = this.data.detections.features;
        const from = feats[fromIdx];
        const to = feats[toIdx];
        const a = from.geometry.coordinates;
        const b = to.geometry.coordinates;
        const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        const duration = reduced ? 0 : this._segmentDuration(from, to);
        if (this._timeline && this._timeline.follow && (a[0] !== b[0] || a[1] !== b[1])) {
            this.map.easeTo({ center: b, duration, essential: false });
        }
        if (!duration) { this._setTimelineIndex(toIdx, true); return Promise.resolve(true); }
        return new Promise((resolve) => {
            this._playbackResolve = resolve;
            const started = performance.now();
            const frame = (now) => {
                if (run !== this._playbackRun || !this._timeline || !this._timeline.playing) {
                    this._playbackFrame = null;
                    this._playbackResolve = null;
                    resolve(false);
                    return;
                }
                const raw = Math.min(1, (now - started) / duration);
                const eased = raw < 0.5 ? 4 * raw * raw * raw : 1 - Math.pow(-2 * raw + 2, 3) / 2;
                const coord = [a[0] + (b[0] - a[0]) * eased, a[1] + (b[1] - a[1]) * eased];
                this._setAvatarPosition(coord, from, to, eased);
                this._setProgressLine(fromIdx, coord);
                if (raw < 1) {
                    this._playbackFrame = window.requestAnimationFrame(frame);
                } else {
                    this._playbackFrame = null;
                    this._playbackResolve = null;
                    this._setTimelineIndex(toIdx, true);
                    resolve(true);
                }
            };
            this._playbackFrame = window.requestAnimationFrame(frame);
        });
    }

    _ensureProgressLayers() {
        if (!this.map || !this.data || this.map.getSource(SRC.progress)) return;
        const first = this.data.detections.features[0].geometry.coordinates;
        this.map.addSource(SRC.progress, { type: 'geojson', data: {
            type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: [first, first] }
        } });
        this.map.addLayer({ id: 'ae-route-progress-glow', type: 'line', source: SRC.progress,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': '#00ff96', 'line-width': 12, 'line-blur': 9, 'line-opacity': 0.42 } });
        this.map.addLayer({ id: 'ae-route-progress-line', type: 'line', source: SRC.progress,
            layout: { 'line-cap': 'round', 'line-join': 'round' },
            paint: { 'line-color': '#7dffc6', 'line-width': 4.5, 'line-opacity': 1 } });
    }

    _setProgressLine(idx, current) {
        const source = this.map && this.map.getSource(SRC.progress);
        if (!source || !this.data) return;
        const coords = this.data.detections.features.slice(0, idx + 1).map((f) => f.geometry.coordinates);
        coords.push(current);
        if (coords.length === 1) coords.push(current);
        source.setData({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: coords } });
    }

    _setAvatarPosition(coord, from, to, progress) {
        if (!this.flags.avatar) return;
        if (!this._avatarMarker) {
            const pin = el('div', 'ae-avatar');
            pin.setAttribute('aria-label', 'Current tracked position');
            pin.append(el('span', 'ae-avatar-ring'), el('span', 'ae-avatar-core'));
            this._avatarMarker = new maplibregl.Marker({ element: pin, anchor: 'center' })
                .setLngLat(coord).addTo(this.map);
        } else {
            this._avatarMarker.setLngLat(coord);
        }
        const name = progress < 0.55 ? from.properties.pipeline_name : to.properties.pipeline_name;
        this._avatarMarker.getElement().title = `Tracking · ${name || 'camera'}`;
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
        const cameraCount = this.data.metadata && this.data.metadata.counts ? this.data.metadata.counts.cameras : null;
        const unique = new Set(feats.map((feat) => feat.geometry.coordinates.join(','))).size;
        const mapped = Number.isFinite(Number(cameraCount)) ? Number(cameraCount) : unique;
        const cameraWord = mapped === 1 ? 'camera' : 'cameras';
        const activity = t.playing ? 'Playing · ' : '';
        t.label.textContent = `${String(idx + 1).padStart(2, '0')} / ${String(n).padStart(2, '0')}  ·  ${p.pipeline_name || 'Unknown camera'}  ·  ${fmtTs(p.timestamp)}`;
        t.status.textContent = unique < 2
            ? `${activity}${n} detections · ${mapped} mapped ${cameraWord} · no geographic transition`
            : `${activity}${n} detections · ${mapped} mapped ${cameraWord} · ${unique} locations`;
        t.prevBtn.disabled = idx === 0;
        t.nextBtn.disabled = idx === n - 1;

        // Timeline mode dims detections after the cursor via a filter on the
        // point layer (a style expression, so it survives nothing — but
        // _restoreOverlays calls _syncTimeline, so it is re-applied).
        if (this.flags.timeline && this.map.getLayer('ae-detections-point')) {
            this.map.setPaintProperty('ae-detections-point', 'circle-opacity',
                ['case', ['<=', ['get', 'seq'], p.seq], 1, 0.15]);
            this.map.setPaintProperty('ae-detections-point', 'circle-stroke-opacity',
                ['case', ['<=', ['get', 'seq'], p.seq], 1, 0.15]);
        }
        this._setAvatarPosition(f.geometry.coordinates, f, f, 1);
        this._setProgressLine(idx, f.geometry.coordinates);
        if (!silent && this.flags.avatar) this.map.easeTo({ center: f.geometry.coordinates, duration: 650, essential: false });
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
