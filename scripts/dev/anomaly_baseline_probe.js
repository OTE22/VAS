// Opens Security Intelligence → Anomalies, picks an identity whose whole
// history is inside the window, sets Days back = 7, and checks the message
// explains WHY (all appearances inside the window, first seen N days ago).
const PW_CORE = process.env.PW_CORE || "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW_CORE);
const BASE = process.env.BASE || "http://localhost";
(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newContext({ viewport: { width: 1500, height: 900 } }).then(c => c.newPage());
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
  await page.fill("#username", "admin"); await page.fill("#password", "admin123");
  await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);
  await page.goto(`${BASE}/admin/security-intelligence`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(2500);
  await page.click('.sec-tab[data-tab="anomalies"]');
  // the anomalies tab's picker is the .advanced-identity-selector that wraps #anomaly-identity-id
  const opened = await page.evaluate(async () => {
    const sel = document.getElementById('anomaly-identity-id');
    const w = sel && sel.nextElementSibling;
    const t = w && w.querySelector('.identity-selector-trigger');
    if (!t) return false;
    t.scrollIntoView({ block: 'center' }); t.click(); return true;
  });
  await page.waitForFunction(() => document.getElementById('anomaly-identity-id').nextElementSibling.querySelectorAll('.identity-selector-item').length > 0, { timeout: 15000 });
  const picked = await page.evaluate(async () => {
    const sel = document.getElementById('anomaly-identity-id');
    const w = sel.nextElementSibling;
    const input = w.querySelector('.filter-search');
    input.value = 'IRON'; input.dispatchEvent(new Event('input', { bubbles: true }));
    await new Promise(r => setTimeout(r, 1500));
    const row = [...w.querySelectorAll('.identity-selector-item')].find(r => /IRON MAN/.test(r.textContent));
    if (!row) return false; row.click(); return true;
  });
  await page.waitForTimeout(500);
  await page.fill('#anomaly-days-back', '7');
  await page.click('#anomalies-detect-btn');
  await page.waitForFunction(() => /Insufficient baseline|No anomalies|anomaly/i.test(document.getElementById('anomalies-container').textContent), { timeout: 60000 });
  const text = await page.evaluate(() => document.getElementById('anomalies-container').textContent.replace(/\s+/g, ' ').trim());
  console.log("opened=", opened, "picked=", picked);
  console.log("MESSAGE:", text);
  const ok = /All 8 appearances of this identity fall inside the last 7 days/.test(text) && /first seen \d+ days? ago/.test(text) && /set "Days back" below \d+/.test(text);
  console.log("errors:", errors.length ? errors : "none");
  console.log(ok && !errors.length ? "PASS" : "FAIL");
  await browser.close();
  process.exit(ok && !errors.length ? 0 : 1);
})();
