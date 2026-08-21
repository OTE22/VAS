/**
 * Identity-selector probe — every "select identity" picker, both pages.
 *
 *   node scripts/dev/identity_selector_probe.js [--base http://localhost] [--viewport WxH]
 *
 * The reported defect: on /admin/intelligence the picker's search box rendered
 * 44px wide — too narrow to type in — while the identical component on
 * /admin/security-intelligence rendered it at 474px. Cause: intelligence.html
 * mounts the picker inside <div class="form-group">, and admin.css carries a
 * generic `.form-group input, .form-group select { width: 100% }` that reached
 * through the wrapper and forced the Type <select> to fill the row.
 *
 * So this measures what the user can actually use, for EVERY picker on both
 * pages: the search box is wide enough to type in, the face list is on screen
 * and scrollable, the panel is inside the viewport, and the list really can be
 * filtered and paged.
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
const [VW, VH] = arg("--viewport", "1366x768").split("x").map(Number);

// A search box narrower than this cannot be typed into comfortably; the bug
// measured 44px.
const MIN_SEARCH_WIDTH = 200;

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newContext({ viewport: { width: VW, height: VH } }).then((c) => c.newPage());

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER); await page.fill("#password", PASS);
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);

    for (const path of ["/admin/intelligence", "/admin/security-intelligence"]) {
      await page.goto(`${BASE}${path}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(3000);

      const count = await page.evaluate(() => document.querySelectorAll(".advanced-identity-selector").length);
      check(`${path}: identity pickers present`, count > 0, `${count} picker(s)`);

      for (let i = 0; i < count; i++) {
        const r = await page.evaluate(async (index) => {
          const wrappers = document.querySelectorAll(".advanced-identity-selector");
          const w = wrappers[index];
          const id = w.dataset.originalId || `#${index}`;
          const trigger = w.querySelector(".identity-selector-trigger");
          if (!trigger) return { id, noTrigger: true };
          // This page keeps most pickers on inactive tabs. A picker inside a
          // display:none ancestor has no layout at all, so measuring it would
          // report 0px and blame the component for being hidden.
          if (!w.getClientRects().length) return { id, hidden: true };

          // close any other open panel first
          document.querySelectorAll(".identity-selector-panel").forEach((p) => { p.style.display = "none"; });
          // Scroll the trigger into view first, as a user necessarily would.
          // On a narrow viewport the page is long and the trigger can sit far
          // below the fold; opening a panel attached to an off-screen trigger
          // measures the page's scroll position, not the panel's fit.
          trigger.scrollIntoView({ block: "center" });
          await new Promise((res) => setTimeout(res, 250));
          trigger.click();
          await new Promise((res) => setTimeout(res, 900));

          const panel = w.querySelector(".identity-selector-panel");
          if (!panel || getComputedStyle(panel).display === "none") return { id, notOpen: true };
          const search = panel.querySelector(".filter-search");
          const listing = panel.querySelector(".identity-selector-results");
          const status = panel.querySelector(".identity-selector-status");
          const pr = panel.getBoundingClientRect();
          const sr = search ? search.getBoundingClientRect() : null;
          const lr = listing ? listing.getBoundingClientRect() : null;

          return {
            id,
            panelW: Math.round(pr.width),
            insideViewport: pr.right <= window.innerWidth + 1 && pr.left >= -1,
            bottomOnScreen: pr.bottom <= window.innerHeight + 1,
            searchW: sr ? Math.round(sr.width) : 0,
            searchTypable: !!search && !search.disabled && !search.readOnly,
            items: panel.querySelectorAll(".identity-selector-item").length,
            listH: lr ? Math.round(lr.height) : 0,
            listScrolls: listing ? listing.scrollHeight > listing.clientHeight : false,
            status: status ? status.textContent.trim().slice(0, 60) : "",
          };
        }, i);

        if (r.noTrigger) { check(`${path} [${r.id}]: has a trigger`, false); continue; }
        if (r.hidden) { console.log(`  skip  ${path} [${r.id}] — on an inactive tab`); continue; }
        if (r.notOpen) { check(`${path} [${r.id}]: panel opens`, false, "stayed closed"); continue; }

        check(`${path} [${r.id}]: search box is usable`,
          r.searchW >= MIN_SEARCH_WIDTH && r.searchTypable,
          `${r.searchW}px (was 44px on intelligence)`);
        check(`${path} [${r.id}]: faces listed and scrollable`,
          r.items > 0 && r.listH > 0,
          `${r.items} shown, list ${r.listH}px${r.listScrolls ? " (scrolls)" : ""}, ${r.status}`);
        check(`${path} [${r.id}]: panel fits the viewport`,
          r.insideViewport && r.bottomOnScreen,
          `w=${r.panelW} inside=${r.insideViewport} bottomOnScreen=${r.bottomOnScreen}`);
      }

      // typing must actually filter, on the first picker of each page
      const typed = await page.evaluate(async () => {
        const w = document.querySelector(".advanced-identity-selector");
        const panel = w.querySelector(".identity-selector-panel");
        if (getComputedStyle(panel).display === "none") w.querySelector(".identity-selector-trigger").click();
        await new Promise((r) => setTimeout(r, 700));
        const search = panel.querySelector(".filter-search");
        const before = panel.querySelectorAll(".identity-selector-item").length;
        search.focus();
        search.value = "seed_person_1";
        search.dispatchEvent(new Event("input", { bubbles: true }));
        // Wait for the request to settle rather than guessing: the picker
        // debounces then fetches, and sampling mid-flight reads the
        // "Loading identities..." state and the previous list.
        // Settle on the RESULT, not a timer: wait until the status stops
        // saying "Loading" AND the rendered count actually changes. The picker
        // debounces, then fetches, and a fixed delay sampled mid-flight.
        for (let i = 0; i < 80; i++) {
          await new Promise((r) => setTimeout(r, 150));
          const st = panel.querySelector(".identity-selector-status");
          const n = panel.querySelectorAll(".identity-selector-item").length;
          if (st && !/loading/i.test(st.textContent) && n !== before) break;
        }
        const after = panel.querySelectorAll(".identity-selector-item").length;
        const status = panel.querySelector(".identity-selector-status");
        return { before, after, status: status ? status.textContent.trim().slice(0, 60) : "" };
      });
      check(`${path}: typing in the search box filters the list`,
        typed.after > 0 && typed.after !== typed.before,
        `${typed.before} -> ${typed.after} (${typed.status})`);
    }
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 240));
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\nOVERALL ${VW}x${VH}: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
