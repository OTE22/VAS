/* Persistent detection alerts. The server owns grouping and acknowledgement. */
(() => {
    'use strict';
    const severity = {info: 0, warning: 1, critical: 2};
    function node(tag, text, className) {
        const item = document.createElement(tag);
        if (text != null) item.textContent = String(text);
        if (className) item.className = className;
        return item;
    }
    function time(value) { const d = new Date(value); return Number.isNaN(d.getTime()) ? 'Unavailable' : d.toLocaleString(); }
    class DetectionAlertInbox {
        constructor(host, playSound) {
            this.host = host; this.playSound = playSound; this.offset = 0;
            this.soundsSince = null; this.seen = new Set(); this.initialized = false; this.loading = false;
            this.revision = 0; this.reload = false; this.stopped = false; this.timer = null;
            this.heading = node('h2', 'Detection alerts');
            this.count = node('span', '…', 'watchlist-inbox-count');
            this.summary = node('span', 'Checking for alerts…', 'watchlist-inbox-summary');
            this.summary.setAttribute('role', 'status');
            this.toggle = node('button', null, 'watchlist-inbox-toggle');
            this.toggle.type = 'button';
            this.toggleLabel = node('span', 'Review alerts');
            this.toggle.append(this.toggleLabel, node('span', '⌄', 'watchlist-inbox-chevron'));
            this.toggle.setAttribute('aria-expanded', 'false');
            this.toggle.setAttribute('aria-controls', `${host.id}-body`);
            this.body = node('div', null, 'watchlist-inbox-body');
            this.body.id = `${host.id}-body`;
            this.body.hidden = true;
            this.toggle.addEventListener('click', () => {
                this.body.hidden = !this.body.hidden;
                this.toggle.setAttribute('aria-expanded', String(!this.body.hidden));
                this.toggleLabel.textContent = this.body.hidden ? 'Review alerts' : 'Hide alerts';
            });
            this.openDetails = new Set();
            this.status = node('p', 'Loading alerts…'); this.status.setAttribute('role', 'status');
            this.list = node('div', null, 'watchlist-inbox-list');
            this.previous = node('button', 'Previous'); this.next = node('button', 'Next');
            this.refreshButton = node('button', 'Refresh alerts');
            for (const button of [this.previous, this.next, this.refreshButton]) button.type = 'button';
            this.previous.addEventListener('click', () => { this.offset = Math.max(0, this.offset - 50); this.refresh(); });
            this.next.addEventListener('click', () => { this.offset += 50; this.refresh(); });
            this.refreshButton.addEventListener('click', () => this.refresh());
            const controls = node('div', null, 'watchlist-inbox-controls');
            controls.append(this.previous, this.next, this.refreshButton);
            const header = node('div', null, 'watchlist-inbox-header');
            const title = node('div', null, 'watchlist-inbox-title');
            title.append(this.heading, this.count);
            header.append(title, this.summary, this.toggle);
            this.body.append(node('p', 'Live alerts & watchlist matches · Acknowledge an alert once reviewed.', 'watchlist-inbox-help'), this.list, this.status, controls);
            host.replaceChildren(header, this.body);
            host.hidden = false;
            this.visibility = () => { if (!document.hidden) this.refresh(); };
            document.addEventListener('visibilitychange', this.visibility);
            this.refresh();
        }
        async json(url, options = {}) {
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), 12000);
            try {
                const response = await fetch(url, {credentials: 'include', cache: 'no-store', ...options, signal: controller.signal});
                const body = await response.json();
                if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Could not load detection alerts.');
                return body;
            } finally { clearTimeout(timeout); }
        }
        async refresh() {
            if (this.stopped) return;
            if (this.loading) { this.reload = true; return; }
            clearTimeout(this.timer);
            this.loading = true;
            this.previous.disabled = this.next.disabled = true;
            const requestedOffset = this.offset;
            const requestedRevision = this.revision;
            try {
                const soundQuery = this.soundsSince ? `&sounds_since=${encodeURIComponent(this.soundsSince)}` : '';
                const data = await this.json(`/api/detection-alerts/inbox?limit=50&offset=${requestedOffset}${soundQuery}`);
                if (this.stopped) return;
                if (requestedOffset !== this.offset || requestedRevision !== this.revision) { this.reload = true; return; }
                if (!Array.isArray(data.items) || !Number.isInteger(data.total)) throw new Error('Invalid alert response.');
                if (!data.items.length && data.total > 0 && this.offset > 0) { this.offset = 0; this.reload = true; return; }
                // The server checks new groups across all pages. Initial history
                // is silent; repeated sightings in one group do not sound again.
                let loudest = null;
                for (const item of (data.sound_candidates || [])) {
                    const key = `${item.source}:${item.first_id}`;
                    const level = Object.hasOwn(severity, item.alert_level) ? item.alert_level : 'info';
                    if (this.initialized && !this.seen.has(key) &&
                        (loudest === null || severity[level] > severity[loudest])) loudest = level;
                    this.seen.add(key);
                }
                if (!this.soundsSince) this.soundsSince = data.observed_at;
                this.initialized = true;
                this.count.textContent = String(data.total);
                this.summary.textContent = data.total ? `${data.total} unacknowledged · Live & watchlist` : 'All clear · No pending alerts';
                this.host.classList.remove('watchlist-inbox-stale');
                this.status.textContent = data.total ? `Showing ${requestedOffset + 1}–${requestedOffset + data.items.length} of ${data.total} alert groups. Updated ${new Date().toLocaleTimeString()}.` : 'No unacknowledged detection alerts.';
                this.status.classList.remove('watchlist-inbox-error');
                const scrollTop = this.body.scrollTop;
                this.list.replaceChildren(...data.items.map(item => this.card(item)));
                this.body.scrollTop = scrollTop;
                const keys = new Set(data.items.map(item => `${item.source}:${item.first_id}`));
                for (const key of this.openDetails) if (!keys.has(key)) this.openDetails.delete(key);
                this.previous.disabled = this.offset === 0;
                this.next.disabled = this.offset + data.items.length >= data.total;
                if (loudest !== null) this.playSound(loudest);
            } catch (error) {
                if (!this.stopped) {
                    // Keep the last confirmed cards during a failed refresh.
                    this.status.textContent = `Alert refresh failed: ${error.message}. Displayed alerts may be out of date. Retry with Refresh alerts.`;
                    this.status.classList.add('watchlist-inbox-error');
                    this.summary.textContent = 'Refresh failed · Review alerts to retry';
                    this.host.classList.add('watchlist-inbox-stale');
                }
            } finally {
                this.loading = false;
                if (!this.stopped) {
                    if (this.reload) { this.reload = false; this.refresh(); }
                    else this.timer = setTimeout(() => { if (!document.hidden) this.refresh(); }, 15000);
                }
            }
        }
        card(item) {
            const level = Object.hasOwn(severity, item.alert_level) ? item.alert_level : 'info';
            const card = node('article', null, `watchlist-inbox-card ${level}`);
            const title = node('h3', item.identity_name || 'Unknown person');
            card.append(title, node('span', level, 'watchlist-inbox-severity'), node('p', `${item.source === 'live' ? 'Live alert' : 'Watchlist'} · ${item.rule_name}`, 'watchlist-inbox-rule'));
            if (typeof item.snapshot_url === 'string' && /^\/api\/detection-alerts\/(watchlist|live)\/[0-9a-f-]+\/snapshot$/i.test(item.snapshot_url)) {
                const image = node('img'); image.src = item.snapshot_url; image.alt = 'Detection snapshot'; image.loading = 'lazy';
                image.addEventListener('error', () => image.replaceWith(node('p', 'Snapshot unavailable')));
                card.append(image);
            }
            card.append(node('p', item.location_name || item.pipeline_id || 'Location unavailable', 'watchlist-inbox-location'));
            const details = node('details', null, 'watchlist-inbox-details');
            const key = `${item.source}:${item.first_id}`;
            details.open = this.openDetails.has(key);
            details.addEventListener('toggle', () => {
                if (details.open) this.openDetails.add(key); else this.openDetails.delete(key);
            });
            const confidence = Number.isFinite(item.similarity_score) ? `${(item.similarity_score * 100).toFixed(1)}%` : 'Unavailable';
            details.append(node('summary', `${item.sightings} sightings · Details`), node('p', `First alert: ${time(item.first_seen_at)}`), node('p', `Last alert: ${time(item.last_seen_at)}`), node('p', `Latest match confidence: ${confidence}`));
            if (item.action_instructions) card.append(node('p', `Instructions: ${item.action_instructions}`, 'watchlist-inbox-instructions'));
            card.append(details);
            const actions = node('div', null, 'watchlist-inbox-controls');
            if (item.identity_id) {
                const profile = node('a', 'View person'); profile.href = `/admin/identity/${encodeURIComponent(item.identity_id)}?from=/dashboard`; actions.append(profile);
            }
            const acknowledge = node('button', 'Acknowledge'); acknowledge.type = 'button';
            const feedback = node('p'); feedback.setAttribute('role', 'status');
            acknowledge.addEventListener('click', async () => {
                acknowledge.disabled = true; acknowledge.textContent = 'Acknowledging…';
                try {
                    await this.json(`/api/detection-alerts/inbox/${encodeURIComponent(item.first_id)}/acknowledge`, {
                        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
                        body: JSON.stringify({source: item.source, latest_id: item.latest_id})
                    });
                    this.revision += 1;
                    card.remove(); this.offset = 0; await this.refresh();
                } catch (error) {
                    feedback.textContent = error.message;
                    acknowledge.disabled = false; acknowledge.textContent = 'Acknowledge';
                    this.refresh();
                }
            });
            actions.append(acknowledge); card.append(actions, feedback);
            return card;
        }
        stop() { this.stopped = true; clearTimeout(this.timer); document.removeEventListener('visibilitychange', this.visibility); }
    }
    window.DetectionAlertInbox = DetectionAlertInbox;
})();
