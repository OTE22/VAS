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
                if (request.method() === 'POST') { await new Promise(resolve => setTimeout(resolve, 100)); return json({ id: 'new-conversation', primary_branch_id: 'branch-1' }); }
                return json({ conversations: [{ id: 'conversation-1', title: 'Today’s detection summary', pinned: true, last_message_at: new Date().toISOString() }, { id: 'conversation-1', title: 'Today’s detection summary' }, { id: 'conversation-2', title: 'Today’s detection summary', last_message_at: new Date().toISOString() }], has_more: false });
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
        assert.equal(await page.locator('.conv-item').count(), 2, 'duplicate IDs removed, separate chats retained');
        assert.equal(await page.locator('.conv-chat-reference').count(), 0);
        assert.equal(await page.locator('.history-item-meta').first().isVisible(), true);
        assert.equal(await page.locator('#historySidebar').evaluate(el => el.inert), false);
        assert.equal(await page.locator('.military-navbar').evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(23, 23, 23)');
        const desktop = await fit();
        assert.ok(desktop.input.x >= desktop.sidebar.right, 'desktop sidebar does not cover composer');
        const handle = page.locator('#sidebarResizeHandle');
        const edge = await handle.boundingBox();
        await page.mouse.move(edge.x + 3, edge.y + 100);
        await page.mouse.down();
        await page.mouse.move(edge.x + 143, edge.y + 100);
        await page.mouse.up();
        assert.equal(await handle.getAttribute('aria-valuenow'), '400');
        await fit();
        await page.reload({ waitUntil: 'networkidle' });
        assert.equal(await handle.getAttribute('aria-valuenow'), '400', 'width persists after reload');
        await handle.focus();
        await page.keyboard.press('ArrowLeft');
        assert.equal(await handle.getAttribute('aria-valuenow'), '380');
        await handle.dblclick();
        assert.equal(await handle.getAttribute('aria-valuenow'), '260');
        await page.screenshot({ path: path.join(os.tmpdir(), 'tracking-welcome-desktop.png') });
        await page.locator('.example-queries button').first().focus();
        await page.keyboard.press('Enter');
        assert.equal(await page.locator('#chatInput').inputValue(), 'Help me find a person');
        assert.equal(writes.length, 0, 'suggestions never auto-submit');
        await page.locator('.conv-item').first().click();
        await page.waitForSelector('.chat-message.user');
        assert.equal(await page.locator('.chat-message.user .message-text').evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(48, 48, 48)');
        assert.equal(await page.locator('.sql-block code').textContent(), messages[1].content_blocks[1].sql);
        assert.equal(await page.evaluate(() => !!window.injected), false);
        assert.equal(await page.locator('.assistant .copy-btn').count(), 1, 'saved reply has copy');
        assert.equal(await page.locator('.assistant .regen-btn').count(), 1, 'saved reply has retry');
        assert.equal(await page.locator('.assistant .export-pdf').count(), 0, 'short saved reply has no export');
        const reportChecks = await page.evaluate(() => {
            const check = window.trackingUI.isExportableReport;
            const english = '## Report\n' + 'The camera recorded detections during the selected time period. '.repeat(30);
            const arabic = '\u062a\u0642\u0631\u064a\u0631\n' + '\u0647\u0630\u0647 \u0646\u062a\u0627\u0626\u062c \u062a\u062d\u0644\u064a\u0644 \u0627\u0644\u0643\u0627\u0645\u064a\u0631\u0627\u062a \u0644\u0647\u0630\u0627 \u0627\u0644\u064a\u0648\u0645. '.repeat(60);
            const target = document.querySelector('.assistant .message-text');
            document.querySelector('.assistant .export-buttons').remove();
            window.trackingUI.addResponseTools({ responseEl: target, raw: arabic, query: 'Report' });
            return [check('Short report'), check(english), check(arabic), check('ordinary text '.repeat(200))];
        });
        assert.deepEqual(reportChecks, [false, true, true, false]);
        assert.equal(await page.locator('.assistant .export-pdf').count(), 1);
        assert.equal(await page.locator('.assistant .export-word').count(), 1);
        await page.locator('.conv-menu summary').first().click();
        assert.equal(await page.getByRole('button', { name: 'Rename', exact: true }).isVisible(), true);
        await page.keyboard.press('Escape');
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
        await page.locator('#chatInput').dispatchEvent('keydown', { key: 'Enter' });
        await page.waitForFunction(() => document.querySelector('.chat-message.assistant')?.textContent.includes('24 detections') && !document.querySelector('#chatInput').disabled);
        const sent = writes.filter(w => w.path.endsWith('/query/stream'));
        assert.equal(sent.length, 1, 'one request per send');
        assert.equal(writes.filter(w => w.path === '/api/v1/conversations').length, 1, 'rapid sends create one conversation');
        assert.equal(await page.locator('.assistant .export-pdf').count(), 0, 'short live reply has no export');
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
                await page.waitForFunction(() => document.activeElement.id === 'sidebarCloseBtn');
                await page.keyboard.press('Escape');
                await page.waitForFunction(() => document.querySelector('#historySidebar').inert);
                assert.equal(await page.locator('#sidebarToggleBtn').evaluate(el => el === document.activeElement), true);
                assert.equal(await page.locator('#historySidebar').evaluate(el => el.inert), true);
                await page.locator('#sidebarToggleBtn').click();
                await page.locator('.conv-item').first().click();
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
