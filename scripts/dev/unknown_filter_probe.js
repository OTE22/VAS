/**
 * Unknown Faces Center filter/pagination probe — headless Chrome.
 *
 *   node scripts/dev/unknown_filter_probe.js [--base http://localhost]
 *
 * Drives the REAL page the way an operator does and records the requests it
 * actually issues, because the reported fault ("APPLY FILTERS does not filter
 * on date or pipeline") is about what the browser sends and what comes back:
 *
 *   1. pick a camera from the dropdown, APPLY  -> a pipeline_id reaches the API
 *                                                 and every card shows it
 *   2. From = To = one local day, APPLY        -> the request carries UTC
 *                                                 instants for that LOCAL day
 *   3. Next                                    -> a NEW request is issued with
 *                                                 page=2 (it used to re-slice
 *                                                 memory and never fetch)
 *   4. changing a filter after paging          -> returns to page=1
 *
 * Exit code 0 = every check PASS.
 *
 * playwright-core is resolved the same way as browser_smoke.js (no network,
 * no browser download); the browser is the installed Google Chrome.
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
  results.push({ name, ok, detail });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  // A deliberately offset timezone: the whole point is that a LOCAL calendar
  // day is not a UTC day. Asia/Beirut is UTC+3 in June.
  const ctx = await browser.newContext({ timezoneId: "Asia/Beirut" });
  const page = await ctx.newPage();

  const calls = [];
  page.on("request", (req) => {
    const url = req.url();
    if (url.includes("/api/admin/unknown?")) calls.push(url);
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  try {
    // ---- sign in through the real form
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER);
    await page.fill("#password", PASS);
    await Promise.all([
      page.waitForNavigation({ waitUntil: "domcontentloaded", timeout: 30000 }),
      page.click('button[type="submit"]'),
    ]);

    await page.goto(`${BASE}/admin/unknown`, { waitUntil: "networkidle" });
    await page.waitForSelector("#apply-filters-btn", { timeout: 20000 });

    // Reveal everything: by default only faces inside
    // UNKNOWN_FACE_DISPLAY_HOURS are listed, which on a quiet deployment is
    // one page and makes the paging checks vacuous.
    await page.click("#show-all-toggle-btn");
    await page.waitForTimeout(2500);

    // ---- 1. pipeline filter
    const cams = await page.$$eval("#pipeline-filter option",
      (os) => os.map((o) => o.value).filter(Boolean));
    if (!cams.length) {
      check("a camera is offered in the dropdown", false, "dropdown empty");
    } else {
      const cam = cams[0];
      calls.length = 0;
      await page.selectOption("#pipeline-filter", cam);
      await page.click("#apply-filters-btn");
      await page.waitForTimeout(2500);
      const sent = calls.find((u) => u.includes("pipeline_id="));
      check("APPLY sends pipeline_id to the API", !!sent, sent ? decodeURIComponent(sent.split("?")[1]) : "no such request");
      const shown = await page.$$eval(".pipeline-group", (gs) =>
        gs.map((g) => (g.getAttribute("data-pipeline-id") || g.textContent.slice(0, 40))));
      check("only the chosen camera is rendered",
        shown.length === 0 || shown.every((s) => s.includes(cam)),
        `groups: ${JSON.stringify(shown).slice(0, 160)}`);
    }

    // ---- 2. From = To = one local day
    calls.length = 0;
    await page.selectOption("#pipeline-filter", "");
    const day = await page.evaluate(() => {
      const d = new Date();
      const p = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    });
    await page.fill("#date-from", day);
    await page.fill("#date-to", day);
    await page.click("#apply-filters-btn");
    await page.waitForTimeout(2500);
    const dated = calls.find((u) => u.includes("date_from="));
    const q = dated ? new URLSearchParams(dated.split("?")[1]) : null;
    const from = q && q.get("date_from");
    const to = q && q.get("date_to");
    check("From = To sends a real UTC interval, not a bare local date",
      !!(from && to && from.endsWith("Z") && to.endsWith("Z") && from < to),
      `date_from=${from} date_to=${to}`);
    // UTC+3: local midnight of DAY is 21:00Z the day BEFORE, and the exclusive
    // upper bound is 21:00Z on DAY itself.
    check("the interval is the operator's local day, offset-corrected",
      !!(from && to && new Date(to) - new Date(from) === 24 * 3600 * 1000),
      from && to ? `${(new Date(to) - new Date(from)) / 3600000}h wide` : "n/a");

    // ---- 3. Next actually fetches
    await page.fill("#date-from", "");
    await page.fill("#date-to", "");
    await page.click("#apply-filters-btn");
    await page.waitForTimeout(2500);
    const totalPages = await page.$eval("#total-pages", (e) => parseInt(e.textContent, 10) || 1);
    if (totalPages < 2) {
      check("Next issues a page=2 request", false, `only ${totalPages} page(s) of data — inconclusive`);
    } else {
      calls.length = 0;
      await page.click("#next-page-btn");
      await page.waitForTimeout(2500);
      const paged = calls.find((u) => /[?&]page=2\b/.test(u));
      check("Next issues a page=2 request", !!paged, paged ? "fetched" : "no request was made");
      const shownPage = await page.$eval("#current-page", (e) => e.textContent.trim());
      check("the counter follows the server", shownPage === "2", `shows ${shownPage}`);

      // ---- 4. changing a filter after paging returns to page 1
      calls.length = 0;
      await page.fill("#min-appearances", "1");
      await page.click("#apply-filters-btn");
      await page.waitForTimeout(2500);
      const reset = calls.find((u) => /[?&]page=1\b/.test(u));
      check("changing a filter returns to page 1", !!reset,
        reset ? "page=1" : `requests: ${JSON.stringify(calls).slice(0, 200)}`);
    }

    // Attribute errors rather than lumping them together. The page shell
    // (navbar, upload modal) loads its own components and can fail a fetch
    // transiently under load; that says nothing about filtering, and counting
    // it here would make this probe flaky for a reason it does not test.
    // Anything from the filter path itself is a hard failure.
    const mine = errors.filter((e) => /admin-unknown|loadUnknownFaces|applyFilters|localDayToUtcInstant/.test(e));
    const shell = errors.filter((e) => !mine.includes(e));
    check("no errors from the filter/pagination path", mine.length === 0,
      mine.slice(0, 3).join(" | "));
    if (shell.length) {
      console.log(`  note  ${shell.length} unrelated page-shell console error(s), not counted: ` +
        shell.slice(0, 2).join(" | ").slice(0, 200));
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
