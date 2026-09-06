/** UI-only regression: real settings HTML/CSS/JS, intercepted API, no server or credentials.
 * Run: node scripts/dev/settings_ui_test.js
 * Uses the same PW_CORE / CHROME overrides as browser_smoke.js.
 * Navigation-shell JS is stubbed; shared actions, modal and page-init are real.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { chromium } = require(process.env.PW_CORE ||
    'C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core');
const root = path.resolve(__dirname, '../..');
const fixture = (key, value, extra = {}) => ({
    key, stored_value: value, effective_value: value, value_type: 'integer',
    category: 'recognition', description: 'A setting description.', can_edit: true,
    is_sensitive: false, is_readonly: false, source: 'default', apply_mode: 'immediate', ...extra,
});
const all = [
    fixture('SIMILARITY_THRESHOLD', 0, { default_value: 0.4, value_type: 'float', minimum: 0, maximum: 1 }),
    fixture('SAVE_IMAGES', false, { default_value: true, value_type: 'boolean', category: 'storage' }),
    fixture('DATA_RETENTION_DAYS', 30, { category: 'retention', apply_mode: 'next_job_run' }),
    fixture('SQL_AGENT_MAX_CONCURRENT', 2, { stored_value: 5, default_value: 1, source: 'database', category: 'sql_agent', apply_mode: 'api_restart' }),
    fixture('FUTURE_SETTING', 4, { effective_value: undefined, default_value: 7, category: 'new_category', is_readonly: true, can_edit: false }),
    fixture('API_SECRET', 'secret-not-searchable', { category: 'security', is_sensitive: true, value_type: 'string' }),
    fixture('UNTRUSTED_"_KEY', '<img src=x onerror="window.injected=true">', {
        category: 'untrusted" data-bad="yes', description: '<script>window.injected=true</script>', value_type: 'string',
    }),
];
const payload = () => ({ all_settings: all, categories: [...new Set(all.map(s => s.category))],
    settings_by_category: Object.fromEntries(all.map(s => [s.category, all.filter(item => item.category === s.category)])) });

(async () => {
    const browser = await chromium.launch({ headless: true,
        executablePath: process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const writes = [], errors = [], unexpected = [];
    let failSave = false, failLoad = false;
    page.on('pageerror', err => errors.push(err.message));
    await page.route('**/*', async route => {
        const request = route.request();
        const pathname = new URL(request.url()).pathname;
        const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
        if (pathname.startsWith('/api/')) {
            if (request.method() !== 'GET') {
                writes.push({ path: pathname, method: request.method(), body: request.postDataJSON(), headers: request.headers() });
                if (failSave) return json({ detail: 'Test refusal: configuration unchanged' }, 403);
                return json({ applied: false, restart_required: true, message: 'Saved — RESTART REQUIRED' });
            }
            if (pathname === '/api/settings') return failLoad ? json({ detail: 'Test unavailable' }, 503) : json(payload());
            if (pathname === '/api/settings/audit/log') return json({ logs: [] });
            if (pathname === '/api/ml/capabilities') return json({ items: {} });
            unexpected.push(pathname);
            return json({});
        }
        if (pathname.endsWith('/navbar-loader.js')) return route.fulfill({ contentType: 'text/javascript', body: '' });
        const relative = pathname === '/admin/settings' ? 'frontend/admin/settings.html' : pathname.slice(1);
        const file = path.resolve(root, relative);
        if (!file.startsWith(root + path.sep)) return route.abort();
        try {
            const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2' };
            return route.fulfill({ body: await fs.readFile(file), contentType: types[path.extname(file)] || 'application/octet-stream' });
        } catch { return route.fulfill({ status: 404, body: '' }); }
    });
    const count = async n => {
        await page.waitForFunction(expected => document.querySelectorAll('.setting-card').length === expected, n);
    };
    const search = text => page.fill('#settings-search', text);
    const card = key => page.locator(`.setting-card[data-key="${key}"]`);
    const clear = () => page.click('#settings-reset-filters');
    try {
        await page.goto('https://settings.test/admin/settings', { waitUntil: 'networkidle' });
        await count(3);
        assert.match(await card('SAVE_IMAGES').innerText(), /false/);
        assert.match(await card('SIMILARITY_THRESHOLD').innerText(), /\b0\b/);
        assert.equal(await card('SAVE_IMAGES').locator('.setting-in-use-value').innerText(), 'false');
        assert.equal(await card('SIMILARITY_THRESHOLD').locator('.setting-in-use-value').innerText(), '0');
        assert.equal(await page.locator('.settings-disclosure[open]').count(), 0);
        await search('save detection'); await count(1);
        await search('SQL_AGENT'); await count(0);
        await page.click('[data-show-advanced]'); await count(1);
        assert.equal(await page.getAttribute('[data-view="advanced"]', 'aria-pressed'), 'true');
        await clear(); await count(7);
        assert.equal(await card('SQL_AGENT_MAX_CONCURRENT').locator('.setting-in-use-value').innerText(), '2');
        assert.match(await card('SQL_AGENT_MAX_CONCURRENT').locator('.setting-saved-difference').innerText(), /Saved value \(not in use\): 5/);
        assert.equal(await card('FUTURE_SETTING').locator('.setting-in-use-value').innerText(), 'Not reported by API');
        assert.equal(await card('API_SECRET').locator('.setting-in-use-value').innerText(), '***HIDDEN***');
        assert.equal(await card('FUTURE_SETTING').locator('button').isDisabled(), true);
        assert.equal(await card('API_SECRET').innerText().then(s => s.includes('secret-not-searchable')), false);
        await search('secret-not-searchable'); await count(0);
        await clear();
        assert.equal(await page.locator('.setting-card img, .setting-card script, [data-bad]').count(), 0);
        assert.equal(await page.evaluate(() => window.injected), undefined);
        await page.click('[data-category="sql_agent"]'); await count(1);
        await search('concurrent');
        await page.click('#refresh-settings-btn'); await count(1);
        assert.equal(await page.inputValue('#settings-search'), 'concurrent');
        assert.equal(await page.getAttribute('[data-category="sql_agent"]', 'aria-pressed'), 'true');
        assert.equal(writes.length, 0, 'view, category, search and refresh must not write');

        await card('SQL_AGENT_MAX_CONCURRENT').locator('.edit').click();
        assert.equal(await page.inputValue('#setting-old-value'), '2');
        assert.equal(await page.inputValue('#setting-new-value'), '2');
        await page.fill('#setting-new-value', '3');
        await page.fill('#change-reason', 'UI regression only');
        await page.click('#setting-form button[type="submit"]');
        await page.waitForSelector('#setting-modal', { state: 'hidden' });
        await count(1);
        assert.equal(await card('SQL_AGENT_MAX_CONCURRENT').locator('.setting-in-use-value').innerText(), '2', 'saving must not invent a new runtime value');
        assert.deepEqual(writes[0].body, { value: '3', change_reason: 'UI regression only' });
        assert.equal(writes[0].method, 'PUT');
        assert.equal(writes[0].path, '/api/settings/SQL_AGENT_MAX_CONCURRENT');
        assert.equal(writes[0].headers['x-requested-with'], 'XMLHttpRequest');
        assert.match(await page.locator('#settings-notice-area').innerText(), /RESTART REQUIRED/);
        assert.equal(await page.inputValue('#settings-search'), 'concurrent');
        assert.equal(await page.getAttribute('[data-category="sql_agent"]', 'aria-pressed'), 'true');

        await clear();
        for (const [key, value] of [['SAVE_IMAGES', 'false'], ['SIMILARITY_THRESHOLD', '0']]) {
            await card(key).locator('.edit').click();
            assert.equal(await page.inputValue('#setting-new-value'), value);
            await page.click('#setting-form button[type="submit"]');
            await page.waitForSelector('#setting-modal', { state: 'hidden' });
            assert.deepEqual(writes.at(-1).body, { value, change_reason: null });
        }
        failSave = true;
        await card('SAVE_IMAGES').locator('.edit').click();
        await page.click('#setting-form button[type="submit"]');
        await page.waitForFunction(() => document.getElementById('settings-notice-area').textContent.includes('Test refusal'));
        assert.equal(await page.locator('#setting-modal').isVisible(), true);
        assert.equal(await page.locator('#setting-form button[type="submit"]').isEnabled(), true);
        await page.keyboard.press('Escape');
        await page.waitForSelector('#setting-modal', { state: 'hidden' });
        await page.locator('.retention-tools-section summary').click();
        page.once('dialog', dialog => dialog.dismiss());
        const beforeCancel = writes.length;
        await page.click('#retention-run-btn');
        assert.equal(writes.length, beforeCancel, 'cancelled retention must not write');
        await page.locator('.retention-tools-section summary').click();

        await page.click('[data-view="basic"]');
        await count(3);
        await page.evaluate(() => document.getElementById('settings-notice-area')?.remove());
        await page.screenshot({ path: path.join(os.tmpdir(), 'settings-basic-desktop.png'), fullPage: true });
        await page.setViewportSize({ width: 390, height: 844 });
        for (const mode of ['basic', 'advanced']) {
            await page.click(`[data-view="${mode}"]`);
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${mode}: no horizontal page overflow`);
            assert.equal(await page.locator('.setting-card').evaluateAll(cards => cards.every(c => c.scrollHeight <= c.clientHeight + 1)), true, 'cards do not clip content');
        }
        await page.click('[data-view="basic"]');
        await page.screenshot({ path: path.join(os.tmpdir(), 'settings-basic-mobile.png'), fullPage: true });
        await page.locator('[data-view="advanced"]').focus();
        await page.keyboard.press('Enter'); await count(7);
        failLoad = true;
        await page.click('#refresh-settings-btn');
        await page.waitForSelector('#settings-container .error');
        await search('SAVE_IMAGES');
        assert.equal(await page.locator('#settings-container .error').count(), 1, 'failed load must not restore stale controls');
        await clear();
        failLoad = false;
        await page.click('#refresh-settings-btn'); await count(7);
        assert.deepEqual(errors, []);
        assert.deepEqual(unexpected, []);
        console.log('PASS: views, search, categories, refresh/save state, permissions, masking, escaping, PUT contract, 0/false, errors, retention cancellation, keyboard and mobile layout. All API requests mocked.');
        console.log('Screenshots:', path.join(os.tmpdir(), 'settings-basic-desktop.png'), path.join(os.tmpdir(), 'settings-basic-mobile.png'));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
