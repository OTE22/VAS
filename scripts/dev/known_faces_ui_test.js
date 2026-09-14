/* Browser checks against mocked APIs. Never writes to application records. */
const fs = require('fs');
const path = require('path');
const os = require('os');
const assert = require('assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core');
const root = path.resolve(__dirname, '../..');
const id = '00000000-0000-0000-0000-000000000001';
const other = '00000000-0000-0000-0000-000000000002';
const user = { username: 'test-admin', role: 'admin', privileges: [],
    navbar_links: [{ page: 'home', href: '/home', label: 'HOME', visible: true },
        { page: 'known', href: '/admin/known', label: 'KNOWN FACES', visible: true, parent_page: 'management' },
        { page: 'add-person', href: '#', label: 'ADD PERSON', visible: true, parent_page: 'management' }] };
(async () => {
    const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [], writes = [], queries = [];
    let name = 'Alex Morgan', primary = 1, failDirectory = false, active = true, deleted = false, failDelete = true;
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', async route => {
        const req = route.request(), url = new URL(req.url());
        const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
        if (url.pathname.startsWith('/api/')) {
            if (req.method() !== 'GET') writes.push({ path: url.pathname, method: req.method(), body: req.postData(), headers: req.headers() });
            if (url.pathname.startsWith('/api/auth/')) return json(user);
            if (url.pathname === '/api/dashboard/config') return json({});
            if (url.pathname === '/api/admin/known-faces') {
                queries.push(url.search);
                if (failDirectory) return json({ detail: 'Directory temporarily unavailable.' }, 500);
                const q = url.searchParams.get('q');
                return json({ items: q === 'missing' ? [] : Array.from({ length: 8 }, (_, i) => ({
                    id: i === 0 ? id : other.slice(0, -1) + (i + 1), display_name: i === 0 ? name : ['','Sam Rivera','Nour Haddad','Maya Chen','Omar Khalil','Leila Nasr','Daniel Park','Sara Rahal'][i],
                    status: i === 7 || (i === 0 && !active) ? 'inactive' : 'active', can_edit: true, can_add_photo: i !== 7 && (i !== 0 || active),
                    photo_count: 2, photo_url: '/tests/fixtures/faces/face_a.jpg', last_seen_at: i === 7 ? null : '2026-09-12T10:00:00Z'
                })).filter(p => !deleted || p.id !== id), total: q === 'missing' ? 0 : 30, page: Number(url.searchParams.get('page')), total_pages: q === 'missing' ? 1 : 2 });
            }
            if (url.pathname === '/api/admin/known-faces/' + id + '/activation') { active = req.postDataJSON().active; return json({ success: true, status: active ? 'active' : 'inactive' }); }
            if (url.pathname === '/api/admin/known-faces/' + id + '/deletion-preview') return json({ display_name: name, photos: 2, embeddings: 3, sightings: 7, files: 4, preview_token: 'a'.repeat(64), deletion_pending: !failDelete });
            if (url.pathname === '/api/admin/known-faces/' + id) {
                if (req.method() === 'DELETE') {
                    if (failDelete) { failDelete = false; return json({ detail: { message: 'Deletion is unfinished. Refresh the preview and retry.' } }, 503); }
                    deleted = true; return json({ success: true, deleted: true, message: 'Person deleted.' });
                }
                name = req.postDataJSON().display_name; return json({ success: true, display_name: name });
            }
            if (url.pathname.endsWith('/primary')) { primary = 2; return json({ success: true }); }
            if (url.pathname === `/api/identities/${id}/images`) {
                if (req.method() === 'POST') return json({ success: false, decision_required: true, upload_token: 'mock-ticket', display_name: name,
                    target_identity_id: id, match_confidence: 'uncertain', message: 'Review this photo before adding it to Alex.',
                    candidate_identities: [{ identity_id: id, display_name: name, similarity: .4, preview_image: '/tests/fixtures/faces/face_a.jpg', confidence_band: 'uncertain' }] }, 202);
                return json({ total: 2, images: [1, 2].map(n => ({ image_id: n, url: '/tests/fixtures/faces/face_a.jpg', is_primary: primary === n,
                    source_type: 'upload', created_at: '2026-09-12T10:00:00Z' })) });
            }
            if (url.pathname === '/api/enrollment/confirm') return json({ success: true, identity_id: id, image_created: true, message: 'Photo added.' });
            if (url.pathname === '/api/enrollment/cancel') return json({ success: true });
            return json({});
        }
        const relative = url.pathname === '/admin/known' ? 'frontend/admin/known.html' : url.pathname.slice(1);
        const file = path.resolve(root, relative);
        if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404, body: '' });
        const types = { '.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript', '.jpg': 'image/jpeg', '.png': 'image/png', '.woff2': 'font/woff2' };
        return route.fulfill({ contentType: types[path.extname(file)] || 'application/octet-stream', body: fs.readFileSync(file) });
    });
    try {
        await page.goto('http://known.test/admin/known');
        await page.waitForSelector('.known-card');
        await page.waitForFunction(() => typeof window.openUploadModal === 'function');
        await page.screenshot({ path: path.join(os.tmpdir(), 'known-faces-desktop.png'), fullPage: true });
        assert(await page.locator('.known-page').evaluate(n => n.scrollHeight > n.clientHeight), 'directory scrolls inside app shell');
        await page.locator('#known-next').click();
        await page.waitForFunction(() => document.getElementById('known-page-label').textContent.includes('Page 2'));
        assert(queries.some(q => q.includes('page=2')));
        await page.locator('#known-query').fill('missing');
        await page.waitForFunction(() => document.getElementById('known-count').textContent === '0');
        await page.getByRole('button', { name: 'Clear filters', exact: true }).click();
        await page.waitForSelector('.known-card');
        await page.getByRole('button', { name: 'Manage Alex Morgan', exact: true }).click();
        await page.waitForSelector('.known-photo');
        await page.getByRole('button', { name: 'Set as primary', exact: true }).click();
        await page.waitForFunction(() => document.getElementById('known-detail-notice').textContent.includes('Primary photo updated'));
        await page.locator('#known-name').fill('Alex <img src=x onerror=alert(1)>');
        await page.getByRole('button', { name: 'Save name', exact: true }).click();
        await page.waitForFunction(() => document.getElementById('known-detail-name').textContent.includes('<img'));
        assert.equal(await page.locator('#known-detail-name img').count(), 0);
        await page.locator('#known-add-photo').click();
        await page.waitForSelector('#uploadModal.active');
        assert(await page.locator('#globalPersonName').evaluate(n => n.readOnly));
        await page.locator('#globalPhotoFile').count();
        await page.locator('#uploadPersonForm input[type=file]').setInputFiles(path.join(root, 'tests/fixtures/faces/face_c.png'));
        await page.waitForFunction(() => !document.getElementById('globalUploadSubmitBtn').disabled);
        await page.locator('#globalUploadSubmitBtn').click();
        await page.waitForSelector('#enrollmentDecision', { state: 'visible' });
        assert(writes.some(w => w.path === `/api/identities/${id}/images` && w.method === 'POST'));
        await page.locator('[data-action="enrollmentAddToExisting"]').click();
        await page.waitForFunction(() => !document.getElementById('uploadModal').classList.contains('active'));
        assert(writes.some(w => w.path === '/api/enrollment/confirm'));
        await page.keyboard.press('Escape');
        await page.waitForFunction(() => document.getElementById('known-detail').hidden);
        await page.locator('#known-add').click();
        await page.waitForSelector('#uploadModal.active');
        assert.equal(await page.locator('#globalPersonName').inputValue(), '');
        assert.equal(await page.locator('#globalPersonName').evaluate(n => n.readOnly), false);
        await page.keyboard.press('Escape');
        failDirectory = true; await page.locator('#known-refresh').click();
        await page.getByRole('button', { name: 'Try again', exact: true }).waitFor();
        failDirectory = false; await page.getByRole('button', { name: 'Try again', exact: true }).click();
        await page.waitForSelector('.known-card');
        await page.setViewportSize({ width: 390, height: 844 });
        assert(await page.locator('.known-page').evaluate(n => n.scrollWidth <= n.clientWidth + 1), 'no mobile horizontal overflow');
        await page.screenshot({ path: path.join(os.tmpdir(), 'known-faces-mobile.png') });
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page.getByRole('button', { name: 'Manage ' + name, exact: true }).click();
        await page.locator('#known-activation').click();
        await page.locator('#known-confirm-cancel').click();
        assert(!writes.some(w => w.path.endsWith('/activation')), 'cancelling is read-only');
        await page.locator('#known-activation').click();
        await page.locator('#known-confirm-submit').click();
        await page.waitForFunction(() => document.getElementById('known-detail').hidden);
        await page.getByRole('button', { name: 'Manage ' + name, exact: true }).click();
        assert.equal(await page.locator('#known-activation').innerText(), 'Reactivate person');
        assert(await page.locator('#known-add-photo').isDisabled());
        await page.locator('#known-activation').click(); await page.locator('#known-confirm-submit').click();
        await page.waitForFunction(() => document.getElementById('known-detail').hidden);
        await page.getByRole('button', { name: 'Manage ' + name, exact: true }).click();
        await page.locator('#known-delete').click();
        await page.waitForFunction(() => document.getElementById('known-delete-impact').textContent.includes('Face signatures'));
        assert(await page.locator('#known-confirm-submit').isDisabled());
        await page.locator('#known-delete-name').fill('wrong name');
        assert(await page.locator('#known-confirm-submit').isDisabled());
        await page.locator('#known-delete-name').fill(name);
        await page.screenshot({ path: path.join(os.tmpdir(), 'known-faces-delete-confirmation.png') });
        await page.locator('#known-confirm-submit').click();
        await page.waitForFunction(() => document.getElementById('known-confirm-error').textContent.includes('unfinished'));
        assert(await page.locator('#known-confirm-submit').isDisabled(), 'retry requires a fresh preview');
        await page.locator('#known-preview-refresh').click();
        await page.waitForFunction(() => document.getElementById('known-delete-impact').textContent.includes('Face signatures'));
        await page.locator('#known-delete-name').fill(name);
        await page.locator('#known-confirm-submit').click();
        await page.waitForFunction(() => document.getElementById('known-detail').hidden);
        await page.waitForFunction(() => document.getElementById('known-grid').textContent.indexOf('Alex') < 0);
        assert.equal(writes.filter(w => w.method === 'DELETE').length, 2);
        assert(writes.every(w => w.headers['x-requested-with'] === 'XMLHttpRequest'));
        assert.deepEqual(errors, []);
        console.log('PASS: directory, photos, upload review, responsive layout, deactivation/reactivation, typed deletion confirmation, cancel, and failed-cleanup retry. All APIs mocked.');
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
