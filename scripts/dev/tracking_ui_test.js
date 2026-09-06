/** Real tracking HTML/CSS/JS with intercepted APIs. No server, model, or database writes.
 * Run: node scripts/dev/tracking_ui_test.js (optional PW_CORE / CHROME overrides).
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { chromium } = require(process.env.PW_CORE || 'C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core');
const root = path.resolve(__dirname, '../..');
const user = { username: 'test', full_name: 'Test administrator', role: 'admin', permissions: [], can_access_sql_agent: true,
    navbar_links: ['home', 'dashboard', 'users', 'pipelines', 'unknown', 'tracking', 'settings'].map(page => ({ page, visible: true })) };
const messages = [
    { id: 'm1', role: 'user', content_blocks: [{ type: 'text', text: 'Summarize today’s detections <img src=x onerror="window.injected=true">' }] },
    { id: 'm2', role: 'assistant', content_blocks: [
        { type: 'text', text: '## Detection summary\nHere are the results from your workspace.\n\n| Camera | Detections |\n| --- | --- |\n| Entrance | 24 |\n| Reception | 12 |' },
        { type: 'sql', sql: 'SELECT ' + 'camera_name, '.repeat(60) + 'COUNT(*) FROM detections;' },
    ] },
];

(async () => {
    const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const writes = [], errors = [];
    let holdStream = false, releaseStream;
    page.on('pageerror', err => errors.push(err.message));
    await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        const json = data => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
        if (url.pathname.startsWith('/api/')) {
            if (request.method() !== 'GET') writes.push({ path: url.pathname, body: request.postDataJSON(), headers: request.headers() });
            if (url.pathname.startsWith('/api/auth/')) return json(user);
            if (url.pathname === '/api/pipelines') return json([{ pipeline_name: 'Entrance', total_detections: 24 }]);
            if (url.pathname === '/api/sql-agent/health') return json({ status: 'operational' });
            if (url.pathname === '/api/v1/conversations') {
                if (request.method() === 'POST') return json({ id: 'new-conversation', primary_branch_id: 'branch-1' });
                return json({ conversations: [{ id: 'conversation-1', title: 'Today’s detection summary', pinned: true, last_message_at: new Date().toISOString() }], has_more: false });
            }
            if (url.pathname.endsWith('/messages')) return json({ branch_id: 'branch-1', messages });
            if (url.pathname.endsWith('/branches')) return json({ branches: [{ id: 'branch-1' }] });
            if (url.pathname.endsWith('/cancel')) return json({ success: true });
            if (url.pathname === '/api/sql-agent/query/stream') {
                if (holdStream) await new Promise(resolve => { releaseStream = resolve; });
                const id = request.postDataJSON().request_id;
                const events = [
                    { type: 'status', message: 'Reviewing your question' },
                    { type: 'content', content: 'Test response: **24 detections**.' },
                    { type: 'complete', success: true, response: 'Test response: **24 detections**.' },
                ].map((event, index) => 'data: ' + JSON.stringify({ ...event, request_id: id, sequence: index + 1 }) + '\n\n').join('');
                return route.fulfill({ contentType: 'text/event-stream', body: events }).catch(() => {});
            }
            return json({});
        }
        const relative = url.pathname === '/tracking-people' ? 'frontend/tracking-people.html' : url.pathname.slice(1);
        const file = path.resolve(root, relative);
        if (!file.startsWith(root + path.sep)) return route.abort();
        try {
            return route.fulfill({ body: await fs.readFile(file), contentType: ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2' })[path.extname(file)] || 'application/octet-stream' });
        } catch { return route.fulfill({ status: 404, body: '' }); }
    });
    const fit = async () => {
        const rects = await page.evaluate(() => {
            const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return { x: r.x, y: r.y, right: r.right, bottom: r.bottom }; };
            return { input: rect('.input-wrapper'), main: rect('main'), sidebar: rect('#historySidebar'), width: innerWidth, height: innerHeight, scrollWidth: document.documentElement.scrollWidth };
        });
        assert.ok(rects.input.x >= 0 && rects.input.right <= rects.width + 1, JSON.stringify(rects));
        assert.ok(rects.input.y >= rects.main.y && rects.input.bottom <= rects.height, JSON.stringify(rects));
        assert.ok(rects.scrollWidth <= rects.width, 'no page-level horizontal overflow');
        assert.ok(rects.main.y <= 140, 'navigation leaves room for the chat');
        return rects;
    };
    try {
        await page.goto('https://tracking.test/tracking-people', { waitUntil: 'networkidle' });
        await page.waitForSelector('.conv-item');
        assert.equal(await page.locator('#historySidebar').evaluate(el => el.inert), false);
        assert.equal(await page.locator('.military-navbar').evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(23, 23, 23)');
        const desktop = await fit();
        assert.ok(desktop.input.x >= desktop.sidebar.right, 'desktop sidebar does not cover composer');
        await page.screenshot({ path: path.join(os.tmpdir(), 'tracking-welcome-desktop.png') });
        await page.locator('.example-queries button').first().focus();
        await page.keyboard.press('Enter');
        assert.equal(await page.locator('#chatInput').inputValue(), 'Help me find a person');
        assert.equal(writes.length, 0, 'suggestions never auto-submit');
        await page.locator('.conv-item').click();
        await page.waitForSelector('.chat-message.user');
        assert.equal(await page.locator('.chat-message.user .message-text').evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(48, 48, 48)');
        assert.equal(await page.locator('.sql-block code').textContent(), messages[1].content_blocks[1].sql);
        assert.equal(await page.evaluate(() => !!window.injected), false);
        await fit();
        await page.screenshot({ path: path.join(os.tmpdir(), 'tracking-conversation-desktop.png') });
        await page.locator('#newChatTopBtn').click();
        assert.equal(await page.locator('.chat-message').count(), 0);
        assert.equal(await page.locator('#welcomeMessage').isVisible(), true);
        assert.equal(writes.length, 0, 'new-chat navigation has no server writes');
        await page.locator('#chatInput').fill('Test query');
        await page.locator('#chatInput').dispatchEvent('keydown', { key: 'Enter', isComposing: true });
        assert.equal(writes.length, 0, 'IME confirmation does not submit');
        await page.locator('#chatInput').press('Shift+Enter');
        assert.ok((await page.locator('#chatInput').inputValue()).includes('\n'));
        await page.locator('#chatInput').press('Enter');
        await page.waitForFunction(() => document.querySelector('.chat-message.assistant')?.textContent.includes('24 detections') && !document.querySelector('#chatInput').disabled);
        const sent = writes.filter(w => w.path.endsWith('/query/stream'));
        assert.equal(sent.length, 1, 'one request per send');
        assert.equal(sent[0].body.query, 'Test query');
        assert.equal(sent[0].body.conversation_id, 'new-conversation');
        assert.equal(sent[0].headers['x-requested-with'], 'XMLHttpRequest');
        holdStream = true;
        await page.locator('#chatInput').fill('Stop test');
        await page.locator('#sendBtn').click();
        await page.waitForFunction(() => document.querySelector('#sendBtn').getAttribute('aria-label').toLowerCase().includes('stop'));
        await page.locator('#sendBtn').click();
        await page.waitForFunction(() => !document.querySelector('#chatInput').disabled);
        assert.equal(writes.filter(w => w.path.endsWith('/cancel')).length, 1);
        releaseStream?.();
        holdStream = false;
        for (const [width, height] of [[1366, 768], [390, 844], [390, 500]]) {
            await page.setViewportSize({ width, height });
            await page.reload({ waitUntil: 'networkidle' });
            await fit();
            if (width < 969) {
                assert.equal(await page.locator('#historySidebar').evaluate(el => el.inert), true);
                await page.locator('#sidebarToggleBtn').click();
                assert.equal(await page.locator('#sidebarBackdrop').isVisible(), true);
                await page.keyboard.press('Escape');
                assert.equal(await page.locator('#sidebarToggleBtn').evaluate(el => el === document.activeElement), true);
                assert.equal(await page.locator('#historySidebar').evaluate(el => el.inert), true);
                await page.locator('#sidebarToggleBtn').click();
                await page.locator('.conv-item').click();
                await page.waitForSelector('.chat-message.user');
                assert.equal(await page.locator('#historySidebar').evaluate(el => el.inert), true);
                await fit();
                await page.screenshot({ path: path.join(os.tmpdir(), `tracking-conversation-${width}-${height}.png`) });
                await page.locator('#newChatTopBtn').click();
            }
            await page.screenshot({ path: path.join(os.tmpdir(), `tracking-welcome-${width}-${height}.png`) });
        }
        assert.deepEqual(errors, [], 'no uncaught browser errors');
        console.log('PASS: responsive layout, native prompts, sidebar keyboard/inert state, saved messages/SQL, safe rendering, new chat, IME, Shift+Enter, send and Stop. All API traffic mocked.');
    } catch (error) {
        console.error({ errors, writes, messages: await page.locator('#chatMessages').innerText() });
        throw error;
    } finally {
        releaseStream?.();
        await browser.close();
    }
})().catch(err => { console.error(err); process.exitCode = 1; });
