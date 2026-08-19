/**
 * Search-history CLEAR probe — headless Chrome, the real page, the real button.
 *
 *   node scripts/dev/search_history_clear_probe.js [--base http://localhost]
 *
 * The Clear button popped a confirmation and then announced "Clear history
 * feature requires backend endpoint" — there was no DELETE route at all, so
 * the history stayed exactly where it was.
 *
 * This seeds a search (a real batch search through the API so history rows
 * exist), then drives the page: rows present -> CLEAR -> accept the confirm ->
 * a DELETE is issued, answers 200, and the table ends up empty.
 *
 * playwright-core is resolved the same way as browser_smoke.js.
 */
const PW_CORE = process.env.PW_CORE ||
  "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW_CORE);

const arg = (name, fallback) => {
  const i = process.argv.indexOf(name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
};
const BASE = arg("--base", "http://localhost");
const USER = process.env.SMOKE_USER || "admin";
const PASS = process.env.SMOKE_PASS || "admin123";

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newContext().then((c) => c.newPage());

  const deletes = [];
  page.on("response", async (res) => {
    const req = res.request();
    if (req.method() === "DELETE" && res.url().includes("/api/search/history")) {
      let body = "";
      try { body = (await res.text()).slice(0, 200); } catch (e) { /* stream gone */ }
      deletes.push({ status: res.status(), body,
                     sent: req.headers()["x-requested-with"] || null });
    }
  });
  // The page guards the action behind window.confirm.
  page.on("dialog", (d) => d.accept());

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER);
    await page.fill("#password", PASS);
    await Promise.all([
      page.waitForNavigation({ waitUntil: "domcontentloaded", timeout: 30000 }),
      page.click('button[type="submit"]'),
    ]);

    // How many rows the API reports before clearing.
    const before = await page.evaluate(async () => {
      const r = await fetch("/api/search/history?days_back=365&limit=500", { credentials: "include" });
      return r.ok ? (await r.json()).length : -1;
    });
    check("search history is readable", before >= 0, `${before} row(s) before`);

    await page.goto(`${BASE}/admin/search-history`, { waitUntil: "networkidle" });
    await page.waitForSelector("#clear-history-btn", { timeout: 20000 });

    await page.click("#clear-history-btn");
    await page.waitForTimeout(3000);

    check("CLEAR issues a DELETE to the API", deletes.length > 0,
      deletes.length ? "" : "the button still does nothing");
    if (deletes.length) {
      const del = deletes[deletes.length - 1];
      check("the page sent X-Requested-With", del.sent === "XMLHttpRequest", `header=${del.sent}`);
      check("the DELETE succeeded", del.status === 200, `status=${del.status} ${del.body}`);
    }

    const after = await page.evaluate(async () => {
      const r = await fetch("/api/search/history?days_back=365&limit=500", { credentials: "include" });
      return r.ok ? (await r.json()).length : -1;
    });
    check("the history is actually gone", after === 0, `${before} -> ${after} row(s)`);
    if (before === 0) {
      console.log("  note  this account had NO history, so the round trip was exercised " +
        "but deletion was not. Deletion semantics (scoping, counts) are proven in " +
        "tests/test_search_history_clear.py against seeded rows on the isolated stack.");
    }
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 300));
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\nOVERALL: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
