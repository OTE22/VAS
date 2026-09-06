/** Real page scripts + mocked API/WebSocket/images. Never writes application data.
 * node scripts/dev/face_cards_ui_test.js
 * PW_CORE and CHROME follow the existing browser probes.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { chromium } = require(process.env.PW_CORE || 'C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core');
const root = path.resolve(__dirname, '../..');
const user = { role: 'admin', user: { role: 'admin' }, username: 'test', permissions: ['admin.users.manage'], can_access_unknown_faces: true, pipelines: [], privileges_summary: 'Test administrator' };

(async () => {
    const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
    try {
        const generator = await browser.newPage();
        const images = await generator.evaluate(() => {
            const result = {};
            for (const [name, w, h, color] of [['portrait', 180, 360, '#176c90'], ['landscape', 480, 180, '#816324'], ['square', 240, 240, '#286d45']]) {
                const canvas = document.createElement('canvas'); canvas.width = w; canvas.height = h;
                const ctx = canvas.getContext('2d'); ctx.fillStyle = color; ctx.fillRect(0, 0, w, h);
                ctx.strokeStyle = '#fff'; ctx.lineWidth = 10; ctx.strokeRect(6, 6, w - 12, h - 12);
                ctx.fillStyle = '#fff'; ctx.font = '20px sans-serif'; ctx.fillText(name, 20, h / 2);
                result[name] = canvas.toDataURL('image/jpeg').split(',')[1];
            }
            return result;
        });
        await generator.close();
        for (const target of ['/dashboard', '/admin/unknown']) {
            const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
            const errors = [], writes = [];
            page.on('pageerror', err => errors.push(err.message));
            await page.addInitScript(() => {
                window.WebSocket = class {
                    static CONNECTING = 0; static OPEN = 1; static CLOSED = 3;
                    constructor() { window.testSocket = this; this.readyState = 0; setTimeout(() => { this.readyState = 1; this.onopen?.({}); }, 10); }
                    send() {}
                    close() { this.readyState = 3; }
                };
                window.emitTestEvent = message => window.testSocket.onmessage({ data: JSON.stringify(message) });
            });
            const identities = ['portrait', 'landscape', 'square', 'missing'].map((shape, index) => ({
                id: `00000000-0000-4000-8000-00000000000${index}`, type: 'unknown', display_name: `Test ${shape}`,
                snapshot_url: images[shape] ? `/test-images/${shape}.jpg` : null,
                first_seen_at: new Date().toISOString(), last_seen_at: new Date().toISOString(),
                appearances_count: 1, pipeline_ids: ['camera-1'],
            }));
            await page.route('**/*', async route => {
                const request = route.request(), url = new URL(request.url());
                const json = data => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
                if (!['GET', 'HEAD'].includes(request.method())) writes.push(url.pathname);
                if (url.pathname.startsWith('/test-images/')) {
                    const name = path.basename(url.pathname, '.jpg');
                    if (name === 'slow') await new Promise(resolve => setTimeout(resolve, 350));
                    const data = images[name === 'slow' ? 'portrait' : name];
                    return route.fulfill({ status: data ? 200 : 404, contentType: 'image/jpeg', body: data ? Buffer.from(data, 'base64') : '' });
                }
                if (url.pathname === '/api/dashboard/config') return json({ success: true, config: { face_display_ms: 10800000, alert_notification_window_ms: 3600000, database_retention_days: 30 } });
                if (url.pathname === '/api/dashboard/pipelines') return json({ complete: true, pipelines: [{ pipeline_id: 'camera-1', display_name: 'Test camera' }] });
                if (url.pathname === '/api/pipelines') return json([{ pipeline_id: 'camera-1', location_name: 'Test camera' }]);
                if (url.pathname === '/api/admin/unknown') return json({ identities, total: identities.length, total_pages: 1, stats: { total_unknown: 4, total_appearances: 4, active_cameras: 1 } });
                if (url.pathname.startsWith('/api/auth/me')) return json(user);
                if (url.pathname.startsWith('/api/')) return json({});
                if (url.pathname.endsWith('/navbar-loader.js')) return route.fulfill({ contentType: 'text/javascript', body: `window.getAuthMe = window.getAuthPrivileges = async () => (${JSON.stringify(user)}); const logout = document.createElement('button'); logout.id = 'logout-btn'; logout.hidden = true; document.getElementById('navbar-placeholder').append(logout);` });
                if (url.pathname.endsWith('/upload-modal-loader.js')) return route.fulfill({ contentType: 'text/javascript', body: '' });
                const relative = url.pathname === '/dashboard' ? 'frontend/dashboard.html' : url.pathname === '/admin/unknown' ? 'frontend/admin/unknown.html' : url.pathname.slice(1);
                const file = path.resolve(root, relative);
                if (!file.startsWith(root + path.sep)) return route.abort();
                try {
                    return route.fulfill({ body: await fs.readFile(file), contentType: ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2' })[path.extname(file)] || 'application/octet-stream' });
                } catch { return route.fulfill({ status: 404, body: '' }); }
            });
            try {
                await page.goto('https://faces.test' + target, { waitUntil: 'networkidle' });
                await page.waitForFunction(() => window.testSocket?.readyState === 1 && window.testSocket.onmessage);
                const dashboard = target === '/dashboard';
                const emit = message => page.evaluate(message => window.emitTestEvent(message), message);
                if (dashboard) await emit({ type: 'initial_data', data: [{ pipeline_id: 'camera-1', timestamp: new Date().toISOString(), faces: ['portrait', 'landscape', 'square', 'missing'].map(name => ({ name, similarity: 0.5, image: images[name] || null })) }] });
                await page.waitForFunction(() => document.querySelectorAll('.face-preview[data-image-state="ready"]').length === 3);
                const missing = dashboard ? '.detection-item[data-face="missing"]' : `[data-identity-id="${identities[3].id}"]`;
                assert.equal(await page.locator(`${missing} .face-placeholder`).isVisible(), true);
                const arrival = (shape, similarity) => dashboard
                    ? { type: 'new_detection', data: { pipeline_id: 'camera-1', event_id: `event-${similarity}`, timestamp: new Date().toISOString(), should_show_alert: false, faces: [{ name: 'missing', similarity, image: images[shape] }] } }
                    : { type: 'new_unknown_detection', data: { pipeline_id: 'camera-1', identity_id: identities[3].id, timestamp: new Date().toISOString(), face: { name: 'Unknown', image: images[shape] } } };
                await emit(arrival('portrait', 0.6));
                await page.waitForFunction(selector => document.querySelector(selector + ' img').naturalHeight === 360, missing);
                assert.equal(await page.locator(`${missing} .face-placeholder`).isVisible(), false, 'missing image replaced on live arrival');
                await page.evaluate(selector => { window.originalImageNode = document.querySelector(selector + ' img'); }, missing);
                await emit(arrival('landscape', 0.7));
                await emit(arrival('square', 0.8));
                await page.waitForFunction(selector => document.querySelector(selector + ' img').naturalWidth === 240, missing);
                assert.equal(await page.locator(`${missing} img`).count(), 1);
                assert.equal(await page.evaluate(selector => window.originalImageNode === document.querySelector(selector + ' img'), missing), true, 'live updates preserve the image element');

                // Force slow, fast and failed downloads through the shared loader.
                await page.evaluate(selector => {
                    const frame = document.querySelector(selector + ' .face-media');
                    FaceImage.update(frame.querySelector('img'), '/test-images/slow.jpg', frame.querySelector('.face-placeholder'));
                    FaceImage.update(frame.querySelector('img'), '/test-images/landscape.jpg', frame.querySelector('.face-placeholder'));
                }, missing);
                await page.waitForFunction(selector => document.querySelector(selector + ' img').naturalWidth === 480, missing);
                await page.waitForTimeout(450);
                assert.equal(await page.locator(`${missing} img`).evaluate(img => img.naturalWidth), 480, 'late image cannot overwrite newest');
                await page.evaluate(selector => {
                    const frame = document.querySelector(selector + ' .face-media');
                    FaceImage.update(frame.querySelector('img'), '/test-images/broken.jpg', frame.querySelector('.face-placeholder'));
                }, missing);
                await page.waitForFunction(selector => document.querySelector(selector + ' img').dataset.imageState === 'previous', missing);
                assert.equal(await page.locator(`${missing} img`).evaluate(img => img.naturalWidth), 480, 'broken update keeps last good image');
                // Initially broken image also keeps a visible, correctly-sized fallback.
                await page.evaluate(() => {
                    const frame = document.createElement('div'); frame.className = 'face-media'; frame.id = 'broken-frame';
                    const img = document.createElement('img'); img.className = 'face-preview';
                    const fallback = document.createElement('div'); fallback.className = 'face-placeholder';
                    frame.append(img, fallback); document.body.append(frame);
                    FaceImage.update(img, '/test-images/broken.jpg', fallback);
                });
                await page.waitForFunction(() => document.querySelector('#broken-frame img').dataset.imageState === 'unavailable');
                assert.equal(await page.locator('#broken-frame .face-placeholder').innerText(), 'Image unavailable');
                await page.locator('#broken-frame').evaluate(frame => frame.remove());

                for (const [width, height] of [[1920, 1080], [1366, 768], [390, 844]]) {
                    await page.setViewportSize({ width, height });
                    const geometry = await page.locator('.face-media').evaluateAll(frames => frames.map(frame => {
                        const img = frame.querySelector('img'), box = frame.getBoundingClientRect(), rect = img.getBoundingClientRect(), style = getComputedStyle(img);
                        return { fit: style.objectFit, filter: style.filter, transform: style.transform, square: Math.abs(box.width - box.height) < 2,
                            fits: Math.abs(rect.width - box.width) < 2 && Math.abs(rect.height - box.height) < 2,
                            width: box.width, images: frame.querySelectorAll('img').length };
                    }));
                    for (const item of geometry) {
                        assert.equal(item.fit, 'contain'); assert.equal(item.filter, 'none'); assert.equal(item.transform, 'none');
                        assert.equal(item.square && item.fits, true, `${target} ${width}: stable fitted frame`);
                        assert.ok(item.width >= 100, `${target}: readable image width`); assert.equal(item.images, 1);
                    }
                    await page.locator('.face-media').first().scrollIntoViewIfNeeded();
                    assert.equal(await page.locator('.face-media').first().evaluate(frame => {
                        const rect = frame.getBoundingClientRect();
                        const footer = document.querySelector('.military-footer');
                        return rect.top >= 0 && rect.left >= 0 && rect.right <= innerWidth &&
                            rect.bottom <= (footer ? footer.getBoundingClientRect().top : innerHeight) + 1;
                    }), true, `${target} ${width}: first image can be fully viewed above footer`);
                    await page.screenshot({ path: path.join(os.tmpdir(), `face-cards-${dashboard ? 'dashboard' : 'unknown'}-${width}.png`), fullPage: true });
                }
                assert.deepEqual(errors, []); assert.deepEqual(writes, []);
                console.log(`PASS ${target}: aspect ratios, missing-to-live image, rapid updates, stale loads, failed loads, stable nodes, desktop/mobile geometry; no API writes.`);
            } finally { await page.close(); }
        }
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
