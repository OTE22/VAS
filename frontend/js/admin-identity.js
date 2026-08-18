/**
 * Identity Profile page — /admin/identity/{id}
 * =============================================
 * Reads the identity id from location.pathname (the server injects nothing),
 * renders the profile from GET /api/admin/identity/{id} plus watchlist
 * membership, and hosts the Add-to-Watchlist / Create-Live-Alert flows that
 * previously existed only inside the Unknown Faces Center's modal.
 *
 * Promote / Merge are deliberately NOT here: they are unknown-triage
 * workflows, reachable via the "Manage in Unknown Faces Center" link that
 * renders for unknown identities.
 */

(function () {
    'use strict';

    // ============================================
    // Constants & tiny helpers (page-local copies —
    // this codebase has no module system; each page IIFE owns its helpers)
    // ============================================

    const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

    const PLACEHOLDER_AVATAR =
        'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="100" height="100"%3E' +
        '%3Crect fill="%23333" width="100" height="100"/%3E' +
        '%3Ccircle cx="50" cy="35" r="15" fill="%23999"/%3E' +
        '%3Cpath d="M 25 70 Q 25 60 35 60 L 65 60 Q 75 60 75 70 L 75 85 L 25 85 Z" fill="%23999"/%3E' +
        '%3C/svg%3E';

    /** Escape all five HTML-significant characters (text AND attribute safe). */
    function escapeHtml(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /** Create an element; `text` goes through textContent, never parsed. */
    function buildEl(tag, className, text) {
        const el = document.createElement(tag);
        if (className) el.className = className;
        if (text !== undefined && text !== null) el.textContent = String(text);
        return el;
    }

    /** Only same-origin relative image paths may reach an img src. */
    function safeImageUrl(url) {
        if (typeof url !== 'string' || !url) return '';
        const trimmed = url.trim();
        if (trimmed.startsWith('//')) return '';
        if (!trimmed.startsWith('/')) return '';
        if (trimmed.includes('..')) return '';
        return trimmed;
    }

    /** Same-origin path only, stray view/from params stripped. */
    function sanitizeReferrerPath(value) {
        if (typeof value !== 'string' || !/^\/(?!\/)/.test(value)) return null;
        return value.split('?view=')[0].split('&view=')[0];
    }

    function formatDateTime(value) {
        if (!value) return '—';
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return String(value);
        return parsed.toLocaleString();
    }

    function formatDuration(ms) {
        const seconds = Math.floor(ms / 1000);
        const minutes = Math.floor(seconds / 60);
        const hours = Math.floor(minutes / 60);
        const days = Math.floor(hours / 24);
        if (days > 0) return `${days}d ${hours % 24}h`;
        if (hours > 0) return `${hours}h ${minutes % 60}m`;
        if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
        return `${seconds}s`;
    }

    function showNotification(message, type = 'info') {
        const notification = document.createElement('div');
        notification.className = `notification notification-${type}`;
        notification.style.cssText = `
            position: fixed; top: 20px; right: 20px; padding: 1rem 1.5rem;
            background: ${type === 'error' ? 'rgba(255, 0, 0, 0.9)' :
                         type === 'success' ? 'rgba(0, 255, 150, 0.9)' :
                         'rgba(0, 150, 255, 0.9)'};
            color: ${type === 'success' ? '#000' : '#fff'};
            border-radius: 8px; z-index: 10000; font-weight: 600;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);`;
        notification.textContent = message;
        document.body.appendChild(notification);
        setTimeout(() => notification.remove(), 3500);
    }

    // ============================================
    // State
    // ============================================

    const state = {
        identityId: null,
        from: null,
        identity: null,
        pipelinesById: new Map()
    };

    function pipelineLabel(pipelineId) {
        const id = String(pipelineId || '');
        if (!id) return 'Unknown camera';
        return state.pipelinesById.get(id) || id;
    }

    // ============================================
    // Data loading
    // ============================================

    function parsePath() {
        // /admin/identity/<id>
        const parts = window.location.pathname.split('/').filter(Boolean);
        return parts.length === 3 ? parts[2] : null;
    }

    async function loadPipelineNames() {
        // Human names for cameras (location_name with id fallback) — same
        // source and precedence as the search page and home dashboard.
        try {
            const response = await fetch('/api/dashboard/pipelines', {
                credentials: 'include', cache: 'no-store'
            });
            if (!response.ok) return;
            const payload = await response.json();
            (payload.pipelines || []).forEach(pipeline => {
                if (pipeline && pipeline.pipeline_id) {
                    state.pipelinesById.set(
                        String(pipeline.pipeline_id),
                        String(pipeline.display_name || '').trim() || String(pipeline.pipeline_id));
                }
            });
        } catch (error) {
            console.warn('Pipeline names unavailable:', error);
        }
    }

    function showError(title, message) {
        document.getElementById('identity-loading').hidden = true;
        document.getElementById('identity-profile').hidden = true;
        const error = document.getElementById('identity-error');
        document.getElementById('identity-error-title').textContent = title;
        document.getElementById('identity-error-message').textContent = message || '';
        error.hidden = false;
    }

    async function loadIdentity() {
        const response = await fetch(
            `/api/admin/identity/${encodeURIComponent(state.identityId)}`,
            { credentials: 'include' });
        if (!response.ok) {
            let detail = '';
            try {
                const body = await response.json();
                detail = typeof body?.detail === 'string' ? body.detail : '';
            } catch (_) { /* non-JSON body */ }
            const message = response.status === 404
                ? 'This identity does not exist — it may have been merged or deleted.'
                : response.status === 403
                    ? 'You do not have access to this identity.'
                    : (detail || `The server answered ${response.status}.`);
            throw Object.assign(new Error(message), { status: response.status });
        }
        return response.json();
    }

    async function loadWatchlists() {
        // Non-fatal: a failure here must not blank the profile.
        try {
            const response = await fetch(
                `/api/identities/${encodeURIComponent(state.identityId)}/watchlists`,
                { credentials: 'include' });
            if (!response.ok) return [];
            const payload = await response.json();
            return Array.isArray(payload?.watchlists) ? payload.watchlists : [];
        } catch (error) {
            console.warn('Watchlist membership unavailable:', error);
            return [];
        }
    }

    // ============================================
    // Rendering (DOM-built; timeline is the one string renderer, fully escaped)
    // ============================================

    function renderHeader(identity) {
        const snapshot = document.getElementById('profile-snapshot');
        snapshot.src = safeImageUrl(identity.snapshot_url) || PLACEHOLDER_AVATAR;
        snapshot.dataset.fallbackSrc = PLACEHOLDER_AVATAR;
        snapshot.alt = identity.display_name || 'Identity snapshot';

        const name = identity.display_name || 'Unidentified person';
        document.getElementById('profile-name').textContent = name;
        document.title = `${name} - Identity Profile | Face Recognition Service`;

        const type = String(identity.type || '').toLowerCase();
        const typeBadge = document.getElementById('profile-type-badge');
        typeBadge.textContent = (type || 'untyped').toUpperCase();
        typeBadge.className = 'profile-badge ' +
            (type === 'known' ? 'type-known' : type === 'unknown' ? 'type-unknown' : '');

        const statusBadge = document.getElementById('profile-status-badge');
        statusBadge.textContent = String(identity.status || '').toUpperCase();
        statusBadge.hidden = !identity.status;

        document.getElementById('profile-id').textContent = identity.id;
    }

    function renderActions(identity) {
        const analyze = document.getElementById('profile-analyze-link');
        analyze.href = `/admin/security-intelligence`
            + `?identity_id=${encodeURIComponent(identity.id)}&tab=threats`;

        // Promote / Merge live in the Unknown Faces Center; only meaningful
        // for unknown identities.
        const manage = document.getElementById('profile-manage-link');
        const isUnknown = String(identity.type).toLowerCase() === 'unknown';
        manage.hidden = !isUnknown;
        if (isUnknown) {
            manage.href = `/admin/unknown?view=${encodeURIComponent(identity.id)}`
                + `&from=${encodeURIComponent(window.location.pathname)}`;
        }
    }

    function factRow(label, value) {
        const row = buildEl('div', 'profile-fact');
        row.appendChild(buildEl('span', 'profile-fact-label', label));
        row.appendChild(buildEl('span', 'profile-fact-value', value));
        return row;
    }

    function renderFacts(identity) {
        const sightings = Array.isArray(identity.appearances) ? identity.appearances : [];
        const cameras = Array.isArray(identity.pipeline_ids) ? identity.pipeline_ids : [];
        const grid = document.getElementById('profile-facts');

        // `appearances_count` is a denormalised counter and has drifted from
        // the row count in live data — label the two separately.
        grid.replaceChildren(
            factRow('Detections', String(identity.appearances_count ?? 0)),
            factRow('Recorded sightings', String(sightings.length)),
            factRow('First seen', formatDateTime(identity.first_seen_at)),
            factRow('Last seen', formatDateTime(identity.last_seen_at)),
            factRow(cameras.length === 1 ? 'Camera' : 'Cameras',
                cameras.length ? cameras.map(pipelineLabel).join(', ') : 'None recorded'),
            factRow('Face embeddings', String(identity.embeddings_count ?? 0)),
            factRow('Face records', String(identity.faces_count ?? 0))
        );
    }

    function renderWatchlists(watchlists) {
        const container = document.getElementById('profile-watchlists');
        if (!watchlists.length) {
            container.replaceChildren(
                buildEl('p', 'profile-empty', 'Not on any watchlist.'));
            return;
        }
        const list = buildEl('ul', 'profile-watchlists-list');
        watchlists.forEach(entry => {
            const level = String(entry.alert_level || 'info').toLowerCase();
            const item = buildEl('li', `profile-watchlist alert-level-${
                ['critical', 'warning', 'info'].includes(level) ? level : 'info'}`);
            const icon = buildEl('i', 'fas fa-list-alt');
            icon.setAttribute('aria-hidden', 'true');
            item.appendChild(icon);
            item.appendChild(buildEl('span', 'watchlist-name', entry.name || 'Untitled watchlist'));
            item.appendChild(buildEl('span', 'watchlist-level', level.toUpperCase()));
            list.appendChild(item);
        });
        container.replaceChildren(list);
    }

    // --------------------------------------------
    // Movement timeline — ported from the Unknown Faces Center modal.
    // Returns an HTML string (kept, to reuse admin.css's timeline styling
    // verbatim); every interpolated value is escaped, and snapshot URLs go
    // through safeImageUrl. Zoom controls reuse the data-action delegation.
    // --------------------------------------------

    let currentTimelineScale = 0.3;

    function renderAdvancedTimeline(appearances) {
        if (!appearances || appearances.length === 0) {
            return '<div class="timeline-empty"><i class="fas fa-info-circle"></i> No appearance data available</div>';
        }

        const sorted = [...appearances].sort(
            (a, b) => new Date(a.start_time) - new Date(b.start_time));
        const uniquePipelines = [...new Set(sorted.map(a => a.pipeline_id || 'Unknown'))];

        const hoursInDay = 24;
        const firstTime = new Date(sorted[0].start_time);
        const lastTime = new Date(sorted[sorted.length - 1].start_time);
        const totalDuration = lastTime - firstTime;

        const hourWidth = 120;
        const timelineWidth = hoursInDay * hourWidth;
        const containerWidth = 1000;
        const defaultScale = Math.max(0.3, Math.min(1, (containerWidth - 100) / timelineWidth));
        currentTimelineScale = defaultScale;

        let html = `
            <div class="timeline-controls">
                <div class="timeline-legend">
                    <div class="legend-item"><div class="legend-color" style="background: #00ff96;"></div><span>Location</span></div>
                    <div class="legend-item"><div class="legend-color" style="background: rgba(0, 255, 150, 0.3);"></div><span>Movement Path</span></div>
                    <div class="legend-item"><div class="legend-color" style="background: #ffc107;"></div><span>Time Gap</span></div>
                </div>
                <div class="timeline-zoom-controls">
                    <button class="zoom-btn" data-action="timelineZoomOut" title="Zoom Out"><i class="fas fa-search-minus"></i></button>
                    <input type="range" id="timeline-scale-slider" min="0.1" max="2" step="0.1" value="${defaultScale}"
                           data-action-input="updateTimelineScale" style="width: 150px; margin: 0 0.5rem;">
                    <span class="zoom-value" id="zoom-value">${Math.round(defaultScale * 100)}%</span>
                    <button class="zoom-btn" data-action="timelineZoomIn" title="Zoom In"><i class="fas fa-search-plus"></i></button>
                    <button class="zoom-btn" data-action="timelineFitToView" title="Fit to View"><i class="fas fa-compress-arrows-alt"></i></button>
                </div>
            </div>
            <div class="timeline-track" id="timeline-track" style="transform: scale(${defaultScale}); transform-origin: left center; width: ${timelineWidth}px;">
        `;

        sorted.forEach((app, index) => {
            const startTime = new Date(app.start_time);
            const endTime = app.end_time ? new Date(app.end_time) : null;
            const duration = endTime ? Math.round((endTime - startTime) / 1000) : null;

            const decimalHours = startTime.getHours()
                + startTime.getMinutes() / 60 + startTime.getSeconds() / 3600;
            const perHour = timelineWidth / hoursInDay;
            let leftPosition = decimalHours * perHour;

            const prevApp = index > 0 ? sorted[index - 1] : null;
            if (prevApp) {
                const prev = new Date(prevApp.start_time);
                const prevLeft = (prev.getHours() + prev.getMinutes() / 60 + prev.getSeconds() / 3600) * perHour;
                if (Math.abs(leftPosition - prevLeft) < 50) leftPosition = prevLeft + 50;
            }

            const nextApp = index < sorted.length - 1 ? sorted[index + 1] : null;
            const isPipelineChange = prevApp && prevApp.pipeline_id !== app.pipeline_id;
            const timeSincePrev = prevApp
                ? Math.round((startTime - new Date(prevApp.start_time)) / 1000 / 60) : null;

            let gapLeft = 0, gapWidth = 0;
            if (isPipelineChange && timeSincePrev > 5 && prevApp) {
                const prev = new Date(prevApp.start_time);
                gapLeft = (prev.getHours() + prev.getMinutes() / 60 + prev.getSeconds() / 3600) * perHour;
                gapWidth = leftPosition - gapLeft;
            }

            const snapshot = safeImageUrl(app.snapshot_url || app.best_snapshot_path || '');

            html += `
                ${isPipelineChange && timeSincePrev > 5 && gapWidth > 0 ? `
                    <div class="timeline-gap" style="left: ${gapLeft}px; width: ${gapWidth}px;">
                        <div class="gap-indicator"><i class="fas fa-arrow-right"></i><span>${Number(timeSincePrev)} min</span></div>
                    </div>` : ''}
                <div class="timeline-node ${index === 0 ? 'first' : ''} ${index === sorted.length - 1 ? 'last' : ''} ${isPipelineChange ? 'pipeline-change' : ''}"
                     style="left: ${leftPosition}px;" data-index="${index}">
                    <div class="timeline-node-connector"></div>
                    <div class="timeline-node-content">
                        <div class="node-image">
                            ${snapshot
                                ? `<img src="${escapeHtml(snapshot)}" alt="Appearance ${index + 1}" data-fallback-class="node-image-placeholder" data-fallback-icon="fas fa-user">`
                                : '<div class="node-image-placeholder"><i class="fas fa-user"></i></div>'}
                        </div>
                        <div class="node-info">
                            <div class="node-pipeline" title="${escapeHtml(app.pipeline_id || '')}">
                                <i class="fas fa-video"></i>
                                <span>${escapeHtml(app.pipeline_id ? pipelineLabel(app.pipeline_id) : 'Unknown')}</span>
                            </div>
                            <div class="node-time"><i class="fas fa-clock"></i><span>${escapeHtml(startTime.toLocaleTimeString())}</span></div>
                            ${duration ? `<div class="node-duration"><i class="fas fa-hourglass-half"></i><span>${Number(duration)}s</span></div>` : ''}
                            ${app.track_id ? `<div class="node-track"><i class="fas fa-fingerprint"></i><span>${escapeHtml(String(app.track_id).substring(0, 8))}...</span></div>` : ''}
                        </div>
                        <div class="node-date">${escapeHtml(startTime.toLocaleDateString())}</div>
                    </div>
                    ${nextApp && nextApp.pipeline_id !== app.pipeline_id ? `
                        <div class="timeline-connection">
                            <div class="connection-line"></div>
                            <div class="connection-label" title="${escapeHtml(nextApp.pipeline_id || '')}">
                                <i class="fas fa-arrow-right"></i>
                                <span>${escapeHtml(nextApp.pipeline_id ? pipelineLabel(nextApp.pipeline_id) : 'Unknown')}</span>
                            </div>
                        </div>` : ''}
                </div>
            `;
        });

        let hourMarkers = '';
        for (let hour = 0; hour <= hoursInDay; hour++) {
            hourMarkers += `
                <div class="hour-marker" style="left: ${(hour / hoursInDay) * timelineWidth}px;">
                    <div class="hour-marker-line"></div>
                    <div class="hour-marker-label">${hour}:00</div>
                </div>`;
        }

        html += `
                ${hourMarkers}
            </div>
            <div class="timeline-summary">
                <div class="summary-item"><i class="fas fa-route"></i><div>
                    <div class="summary-value">${uniquePipelines.length}</div>
                    <div class="summary-label">Cameras Visited</div></div></div>
                <div class="summary-item"><i class="fas fa-clock"></i><div>
                    <div class="summary-value">${escapeHtml(formatDuration(totalDuration))}</div>
                    <div class="summary-label">Total Time Span</div></div></div>
                <div class="summary-item"><i class="fas fa-map-marker-alt"></i><div>
                    <div class="summary-value">${sorted.length}</div>
                    <div class="summary-label">Total Stops</div></div></div>
            </div>
        `;
        return html;
    }

    function applyTimelineScale(scale) {
        currentTimelineScale = Math.max(0.1, Math.min(2, scale));
        const track = document.getElementById('timeline-track');
        const readout = document.getElementById('zoom-value');
        const slider = document.getElementById('timeline-scale-slider');
        if (track) track.style.transform = `scale(${currentTimelineScale})`;
        if (readout) readout.textContent = `${Math.round(currentTimelineScale * 100)}%`;
        if (slider) slider.value = String(currentTimelineScale);
    }

    // ============================================
    // Add to Watchlist (ported; the select is DOM-built now — the original
    // interpolated the watchlist NAME into innerHTML unescaped)
    // ============================================

    async function openWatchlistModal() {
        const modal = document.getElementById('add-to-watchlist-modal');
        modal.style.display = 'flex';
        document.getElementById('watchlist-identity-name').textContent = 'Loading...';

        const select = document.getElementById('watchlist-select');
        select.replaceChildren(new Option('Loading watchlists...', ''));

        try {
            const response = await fetch(
                `/api/watchlists/add-identity/${encodeURIComponent(state.identityId)}/defaults`,
                { credentials: 'include' });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'Failed to load defaults');
            }
            const defaults = await response.json();

            document.getElementById('watchlist-identity-name').textContent =
                defaults.identity_name || 'Unknown';

            const available = defaults.available_watchlists || [];
            if (available.length === 0) {
                select.replaceChildren(
                    new Option('No watchlists available. Create a watchlist first.', ''));
                select.disabled = true;
            } else {
                select.replaceChildren(...available.map(wl => {
                    const label = wl.is_already_added ? `${wl.name} (Already Added)` : wl.name;
                    const option = new Option(label, wl.id);
                    option.disabled = Boolean(wl.is_already_added);
                    return option;
                }));
                select.disabled = false;
            }

            document.getElementById('watchlist-priority').value =
                defaults.default_priority || 'normal';
            document.getElementById('watchlist-notes').value = '';
            document.getElementById('watchlist-action-instructions').value = '';

        } catch (error) {
            console.error('Error loading watchlist defaults:', error);
            showNotification(error.message || 'Error loading defaults', 'error');
            modal.style.display = 'none';
        }
    }

    async function addToWatchlist() {
        const watchlistId = document.getElementById('watchlist-select').value.trim();
        if (!watchlistId) {
            showNotification('Please select a watchlist', 'error');
            return;
        }

        const submitBtn = document.getElementById('add-to-watchlist-form')
            .querySelector('button[type="submit"]');
        submitBtn.disabled = true;

        try {
            const response = await fetch(
                `/api/watchlists/${encodeURIComponent(watchlistId)}/entries`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    credentials: 'include',
                    body: JSON.stringify({
                        identity_id: state.identityId,
                        priority: document.getElementById('watchlist-priority').value,
                        notes: document.getElementById('watchlist-notes').value.trim() || null,
                        action_instructions:
                            document.getElementById('watchlist-action-instructions').value.trim() || null,
                        expires_at: null
                    })
                });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || error.message || 'Failed to add to watchlist');
            }

            showNotification('Identity added to watchlist', 'success');
            document.getElementById('add-to-watchlist-modal').style.display = 'none';
            // Refresh the membership section so the page reflects the change.
            renderWatchlists(await loadWatchlists());

        } catch (error) {
            console.error('Error adding to watchlist:', error);
            showNotification(error.message || 'Error adding to watchlist', 'error');
        } finally {
            submitBtn.disabled = false;
        }
    }

    // ============================================
    // Create Live Alert (ported; warnings are DOM-built now, and the hard
    // redirect to /admin/live-alerts after success is dropped — a profile
    // page should stay put and say what happened)
    // ============================================

    async function openLiveAlertModal() {
        const modal = document.getElementById('create-live-alert-modal');
        modal.style.display = 'flex';
        document.getElementById('live-alert-identity-name').textContent = 'Loading...';
        document.getElementById('live-alert-identity-id').textContent = state.identityId;

        try {
            const response = await fetch(
                `/api/live-alerts/defaults/${encodeURIComponent(state.identityId)}`,
                { credentials: 'include' });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'Failed to load default settings');
            }
            const defaults = await response.json();

            document.getElementById('live-alert-identity-name').textContent =
                defaults.identity_name || 'Unknown';
            document.getElementById('live-alert-name').value = defaults.default_name || '';
            document.getElementById('live-alert-min-similarity').value =
                defaults.default_min_similarity;
            document.getElementById('live-alert-similarity-value').textContent =
                `${Math.round(defaults.default_min_similarity * 100)}%`;
            document.getElementById('live-alert-notify-dashboard').checked =
                Boolean(defaults.default_notify_dashboard);
            document.getElementById('live-alert-sound-alert').checked =
                Boolean(defaults.default_sound_alert);
            document.getElementById('live-alert-auto-capture').checked =
                Boolean(defaults.default_auto_capture);

            // Warnings: DOM-built (the original interpolated backend strings
            // into innerHTML).
            let warningsDiv = document.getElementById('live-alert-warnings');
            if (!warningsDiv) {
                warningsDiv = buildEl('div');
                warningsDiv.id = 'live-alert-warnings';
                warningsDiv.style.marginBottom = '1rem';
                const form = document.getElementById('create-live-alert-form');
                form.insertBefore(warningsDiv, form.firstChild);
            }
            const warnings = (defaults.warnings || []).map(w => {
                const row = buildEl('div', 'live-alert-warning');
                const icon = buildEl('i', 'fas fa-exclamation-triangle');
                icon.setAttribute('aria-hidden', 'true');
                row.appendChild(icon);
                row.appendChild(buildEl('span', null, ` ${w}`));
                return row;
            });
            warningsDiv.replaceChildren(...warnings);

            const submitBtn = document.getElementById('create-live-alert-form')
                .querySelector('button[type="submit"]');
            submitBtn.disabled = !defaults.can_create;
            if (!defaults.can_create) {
                showNotification(
                    `Cannot create alert: maximum alerts per user (${defaults.max_alerts}) reached`,
                    'error');
            }

        } catch (error) {
            console.error('Error loading live-alert defaults:', error);
            document.getElementById('live-alert-identity-name').textContent =
                state.identity?.display_name || 'Unknown';
            showNotification(error.message || 'Error loading default settings', 'error');
        }
    }

    async function createLiveAlert() {
        const name = document.getElementById('live-alert-name').value.trim();
        const submitBtn = document.getElementById('create-live-alert-form')
            .querySelector('button[type="submit"]');
        submitBtn.disabled = true;

        try {
            const response = await fetch('/api/live-alerts', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                credentials: 'include',
                body: JSON.stringify({
                    name: name,
                    identity_id: state.identityId,
                    min_similarity:
                        parseFloat(document.getElementById('live-alert-min-similarity').value),
                    notify_dashboard:
                        document.getElementById('live-alert-notify-dashboard').checked,
                    sound_alert: document.getElementById('live-alert-sound-alert').checked,
                    auto_capture_snapshot:
                        document.getElementById('live-alert-auto-capture').checked,
                    expiration_type: 'never'
                })
            });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'Failed to create live alert');
            }

            // No hard redirect (the Unknown-page original bounced to
            // /admin/live-alerts after 800 ms): stay on the profile, report,
            // and let the user decide.
            showNotification(
                `Live alert "${name}" created — see it on the Live Alerts page`, 'success');
            document.getElementById('create-live-alert-modal').style.display = 'none';

        } catch (error) {
            console.error('Error creating live alert:', error);
            showNotification(error.message || 'Error creating live alert', 'error');
        } finally {
            submitBtn.disabled = false;
        }
    }

    function copyIdentityIdToClipboard() {
        const id = state.identityId;
        if (!id || !navigator.clipboard) return;
        navigator.clipboard.writeText(id)
            .then(() => showNotification('Identity ID copied', 'success'))
            .catch(() => showNotification('Could not copy the identity ID', 'error'));
    }

    // ============================================
    // Wiring
    // ============================================

    function setupModalWiring() {
        const pairs = [
            ['close-watchlist-modal', 'add-to-watchlist-modal'],
            ['cancel-watchlist-btn', 'add-to-watchlist-modal'],
            ['close-live-alert-modal', 'create-live-alert-modal'],
            ['cancel-live-alert-btn', 'create-live-alert-modal']
        ];
        pairs.forEach(([buttonId, modalId]) => {
            const button = document.getElementById(buttonId);
            if (button) {
                button.addEventListener('click', () => {
                    document.getElementById(modalId).style.display = 'none';
                });
            }
        });

        document.getElementById('add-to-watchlist-form').addEventListener('submit', (e) => {
            e.preventDefault();
            addToWatchlist();
        });
        document.getElementById('create-live-alert-form').addEventListener('submit', (e) => {
            e.preventDefault();
            createLiveAlert();
        });

        document.getElementById('live-alert-min-similarity').addEventListener('input', (e) => {
            document.getElementById('live-alert-similarity-value').textContent =
                `${Math.round(parseFloat(e.target.value) * 100)}%`;
        });

        // Escape closes whichever modal is open.
        document.addEventListener('keydown', (e) => {
            if (e.key !== 'Escape') return;
            ['add-to-watchlist-modal', 'create-live-alert-modal'].forEach(id => {
                const modal = document.getElementById(id);
                if (modal && modal.style.display === 'flex') modal.style.display = 'none';
            });
        });
    }

    // ============================================
    // Init
    // ============================================

    async function init() {
        state.identityId = parsePath();
        state.from = sanitizeReferrerPath(
            new URLSearchParams(window.location.search).get('from'));

        const back = document.getElementById('identity-back');
        if (state.from && back) {
            back.href = state.from;
            back.hidden = false;
        }

        if (!state.identityId || !UUID_RE.test(state.identityId)) {
            showError('Invalid identity link',
                'The identity reference in this address is malformed.');
            return;
        }

        setupModalWiring();

        // Camera names first so the profile renders labels, not UUIDs.
        await loadPipelineNames();

        try {
            const [identity, watchlists] = await Promise.all([
                loadIdentity(), loadWatchlists()]);
            state.identity = identity;

            renderHeader(identity);
            renderActions(identity);
            renderFacts(identity);
            renderWatchlists(watchlists);
            document.getElementById('identity-timeline').innerHTML =
                renderAdvancedTimeline(identity.appearances || []);

            document.getElementById('identity-loading').hidden = true;
            document.getElementById('identity-profile').hidden = false;

        } catch (error) {
            console.error('Identity profile error:', error);
            showError(
                error.status === 404 ? 'Identity not found'
                    : error.status === 403 ? 'Access denied'
                        : 'Could not load this identity',
                error.message);
        }
    }

    // Registered inside the IIFE — none of these are window globals.
    Actions.register({
        openWatchlistModal: () => openWatchlistModal(),
        openLiveAlertModal: () => openLiveAlertModal(),
        copyProfileIdentityId: () => copyIdentityIdToClipboard(),
        copyIdentityIdFromAlert: () => copyIdentityIdToClipboard(),
        timelineZoomIn: () => applyTimelineScale(currentTimelineScale + 0.1),
        timelineZoomOut: () => applyTimelineScale(currentTimelineScale - 0.1),
        timelineFitToView: () => applyTimelineScale(0.3),
        updateTimelineScale: (el) => applyTimelineScale(parseFloat(el.value)),
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
