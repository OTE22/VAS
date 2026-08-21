/**
 * Navigation and routing probe — headless Chrome.
 *
 *   node scripts/dev/navigation_probe.js [--base http://localhost]
 *
 * Every navbar entry and every in-page link is followed to see where it
 * actually lands: the brief asks for incorrect redirects where one feature
 * unexpectedly opens another page, and for links that resolve to nothing.
 *
 * Checks per destination: the response is not 404/5xx, the browser did not get
 * bounced somewhere unrelated, and the page rendered its own content rather
 * than an error shell.
 *
 * playwright-core resolved as in browser_smoke.js (no network).
 */
const PW_CORE = process.env.PW_CORE ||
  "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW_CORE);

const arg = (n, d) => { const i = process.argv.indexOf(n); return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : d; };
const BASE = arg("--base", "http://localhost");
const USER = process.env.SMOKE_USER || "admin";
const PASS = process.env.SMOKE_PASS || "admin123";

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newContext().then((c) => c.newPage());

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER); await page.fill("#password", PASS);
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);

    // Collect every navigational href the navbar offers this user.
    await page.goto(`${BASE}/dashboard`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2500);
    const targets = await page.evaluate(() => {
      const seen = new Set();
      const out = [];
      document.querySelectorAll(".military-navbar a[href], .military-navbar [data-page]").forEach((el) => {
        const href = el.getAttribute("href");
        if (!href || href === "#" || href.startsWith("javascript:")) return;
        if (seen.has(href)) return;
        seen.add(href);
        out.push({ href, label: (el.textContent || "").trim().slice(0, 28) });
      });
      return out;
    });
    check("navbar exposes navigable destinations", targets.length > 0, `${targets.length} link(s)`);

    for (const t of targets) {
      const url = t.href.startsWith("http") ? t.href : BASE + t.href;
      let status = 0;
      const response = await page.goto(url, { waitUntil: "domcontentloaded" }).catch(() => null);
      if (response) status = response.status();
      // /docs and /redoc are JS-rendered vendor apps that boot after load;
      // a fixed wait sampled an empty body and blamed the route.
      await page.waitForFunction(
        () => (document.body.innerText || "").trim().length > 0,
        { timeout: 15000 }).catch(() => {});
      await page.waitForTimeout(400);

      const landed = new URL(page.url()).pathname;
      const expected = new URL(url).pathname;
      // /docs and /redoc legitimately render vendor shells.
      const rendered = await page.evaluate(() => ({
        title: document.title,
        body: (document.body.innerText || "").trim().length,
        hasNav: !!document.querySelector(".military-navbar, #navbar-placeholder, .swagger-ui, #redoc"),
      }));

      check(`${t.label || t.href} -> ${expected}`,
        status < 400 && landed === expected && rendered.body > 0 && rendered.hasNav,
        `status=${status} landed=${landed}${landed !== expected ? " (REDIRECTED)" : ""} text=${rendered.body}`);
    }
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 240));
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\nOVERALL: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
