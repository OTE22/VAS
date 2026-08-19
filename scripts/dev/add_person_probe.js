/**
 * ADD PERSON probe — headless Chrome, every page that renders the navbar.
 *
 *   node scripts/dev/add_person_probe.js [--base http://localhost]
 *
 * The navbar's ADD PERSON entry is `<a href="#" data-action="openUploadModal">`
 * and the handler ships with the upload-modal component. Thirteen pages
 * injected the navbar without loading that component, so the click found no
 * handler and the bare `#` href just navigated — the URL gained a `#` and
 * nothing opened.
 *
 * Checks, per page: the item exists, clicking it opens the modal, and the URL
 * did not simply become "#".
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

// The page reported broken, plus a sample of the others that lacked the
// component, plus one that already had it (must not regress).
const PAGES = [
  "/admin/intelligence",
  "/admin/settings",
  "/admin/logs",
  "/admin/watchlists",
  "/admin/search-history",
  "/dashboard",          // already loaded the modal directly
];

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newContext().then((c) => c.newPage());

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER);
    await page.fill("#password", PASS);
    await Promise.all([
      page.waitForNavigation({ waitUntil: "domcontentloaded", timeout: 30000 }),
      page.click('button[type="submit"]'),
    ]);

    for (const path of PAGES) {
      await page.goto(`${BASE}${path}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(2500);

      const item = page.locator('.dropdown-item[data-page="add-person"]').first();
      if ((await item.count()) === 0) {
        check(`${path}: ADD PERSON present`, false, "menu item not rendered");
        continue;
      }

      // Only one modal may exist, however many loaders ran.
      const modals = await page.locator("#uploadModal").count();
      check(`${path}: exactly one upload modal`, modals === 1, `${modals} found`);

      const before = page.url();
      // Open the Management dropdown first. A forced click on an item inside a
      // CLOSED dropdown lands at its coordinates, which is whatever sits on
      // top — the event never reaches the anchor, and the probe reports a
      // failure that belongs to the probe.
      const toggle = page.locator('[data-dropdown="management"] .dropdown-toggle').first();
      if (await toggle.count()) {
        await toggle.click().catch(() => {});
        await page.waitForTimeout(400);
      }
      await item.click();

      // openUploadModal() re-checks the caller's role via /api/auth/me before
      // showing anything, so the modal appears one round-trip after the click.
      // Wait for the state instead of sleeping a guessed interval — a fixed
      // 2s passed on five pages and failed on the sixth purely because that
      // page was busy loading its own data.
      await page.waitForFunction(() => {
        const m = document.getElementById("uploadModal");
        if (!m) return false;
        const cs = getComputedStyle(m);
        return cs.display !== "none" && cs.visibility !== "hidden";
      }, { timeout: 10000 }).catch(() => {});

      const open = await page.evaluate(() => {
        const m = document.getElementById("uploadModal");
        if (!m) return "absent";
        const cs = getComputedStyle(m);
        return cs.display !== "none" && cs.visibility !== "hidden" ? "open" : "closed";
      });
      check(`${path}: ADD PERSON opens the modal`, open === "open", `modal ${open}`);
      check(`${path}: click did not just navigate to '#'`,
        !(page.url().endsWith("#") && open !== "open"),
        `${before} -> ${page.url()}`);
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
