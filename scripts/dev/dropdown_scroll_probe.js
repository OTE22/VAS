/**
 * Dropdown reachability, modal cycles and page-scroll probe — headless Chrome.
 *
 *   node scripts/dev/dropdown_scroll_probe.js [--base http://localhost] [--viewport WxH]
 *
 * "The option exists in the DOM" is not the bar. This drives the workflow a
 * user performs and asserts the RESULT:
 *
 *   open -> first option reachable -> scroll -> LAST option reachable
 *        -> select it -> the value actually changed
 *
 * Also covers: every navbar menu item reachable and clickable; repeated modal
 * open/close cycles leaving no stale state; a nested modal not unlocking the
 * page early; and each page scrolling to its true bottom.
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

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newContext({ viewport: { width: VW, height: VH } }).then((c) => c.newPage());
  const jsErrors = [];
  page.on("pageerror", (e) => jsErrors.push(String(e).slice(0, 140)));

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER); await page.fill("#password", PASS);
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);

    // ---------- 1. native <select>: first AND last option actually selectable
    for (const [path, sel] of [["/admin/unknown", "#pipeline-select"],
                               ["/admin/unknown", "#pipeline-filter"],
                               ["/home", "#pipeline-select"]]) {
      await page.goto(`${BASE}${path}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(2500);
      const box = page.locator(sel).first();
      if (!(await box.count())) { continue; }

      const opts = await box.evaluate((el) => [...el.options].map((o) => o.value).filter(Boolean));
      if (opts.length < 2) {
        check(`${path} ${sel}: enough options to test`, false, `${opts.length} option(s)`);
        continue;
      }
      // FIRST
      await box.selectOption(opts[0]);
      const first = await box.evaluate((el) => el.value);
      // LAST — the one a clipped menu would hide
      await box.selectOption(opts[opts.length - 1]);
      const last = await box.evaluate((el) => el.value);
      check(`${path} ${sel}: first and last option both select (${opts.length} options)`,
        first === opts[0] && last === opts[opts.length - 1],
        `first=${first === opts[0]} last=${last === opts[opts.length - 1]}`);

      // Reachable = the user can bring it into view and use it. A control
      // below the fold is normal; one that cannot be scrolled to is not.
      await box.scrollIntoViewIfNeeded().catch(() => {});
      await page.waitForTimeout(200);
      const r = await box.evaluate((el) => { const b = el.getBoundingClientRect();
        return { top: b.top, bottom: b.bottom, h: window.innerHeight }; });
      check(`${path} ${sel}: control reachable in the viewport`,
        r.bottom > 0 && r.top < r.h, `top=${Math.round(r.top)} vh=${r.h}`);
    }

    // ---------- 2. navbar menu: every item reachable, last one clickable
    await page.goto(`${BASE}/admin/settings`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2200);
    for (const menu of ["management", "system"]) {
      const toggle = page.locator(`[data-dropdown="${menu}"] .dropdown-toggle`).first();
      if (!(await toggle.count())) continue;
      await toggle.click(); await page.waitForTimeout(400);
      const info = await page.evaluate((m) => {
        const wrap = document.querySelector(`[data-dropdown="${m}"]`);
        const menuEl = wrap.querySelector(".dropdown-menu");
        const items = [...menuEl.querySelectorAll(".dropdown-item")]
          .filter((el) => getComputedStyle(el).display !== "none");
        const mr = menuEl.getBoundingClientRect();
        const firstR = items[0].getBoundingClientRect();
        const lastR = items[items.length - 1].getBoundingClientRect();
        // Scroll the menu to its end, as a user would for a long list.
        menuEl.scrollTop = menuEl.scrollHeight;
        const lastAfter = items[items.length - 1].getBoundingClientRect();
        return { count: items.length,
                 menuBottom: Math.round(mr.bottom), vh: window.innerHeight,
                 scrolls: menuEl.scrollHeight > menuEl.clientHeight + 1,
                 firstVisible: firstR.top >= 0 && firstR.bottom <= window.innerHeight + 1,
                 lastVisible: lastAfter.top >= 0 && lastAfter.bottom <= window.innerHeight + 1 };
      }, menu);
      check(`navbar "${menu}": menu stays inside the viewport`,
        info.menuBottom <= info.vh + 1, `bottom=${info.menuBottom} vh=${info.vh}`);
      check(`navbar "${menu}": first and last of ${info.count} items reachable`,
        info.firstVisible && info.lastVisible,
        `first=${info.firstVisible} last=${info.lastVisible} internalScroll=${info.scrolls}`);
      await page.keyboard.press("Escape").catch(() => {});
      await page.waitForTimeout(200);
    }

    // ---------- 3. repeated modal cycles leave nothing behind
    const openAdd = async () => {
      const t = page.locator('[data-dropdown="management"] .dropdown-toggle').first();
      if (await t.count()) { await t.click().catch(() => {}); await page.waitForTimeout(250); }
      await page.locator('.dropdown-item[data-page="add-person"]').first().click();
      await page.waitForFunction(() => {
        const m = document.getElementById("uploadModal");
        return m && getComputedStyle(m).display !== "none";
      }, { timeout: 8000 }).catch(() => {});
    };
    for (let i = 0; i < 3; i++) {
      await openAdd();
      await page.evaluate(() => window.ModalStack.close(document.getElementById("uploadModal")));
      await page.waitForTimeout(300);
    }
    const after = await page.evaluate(() => ({
      depth: window.ModalStack.depth(),
      modals: document.querySelectorAll("#uploadModal").length,
      locked: document.body.classList.contains("modal-stack-locked"),
      overflow: document.body.style.overflow,
      position: document.body.style.position,
      pad: document.body.style.paddingRight,
      inert: document.querySelectorAll("[inert]").length,
      hidden: document.querySelectorAll('[aria-hidden="true"].is-stack-under').length,
    }));
    check("3 open/close cycles leave no stale state",
      after.depth === 0 && after.modals === 1 && !after.locked &&
      after.overflow === "" && after.position === "" && after.pad === "" &&
      after.inert === 0 && after.hidden === 0,
      JSON.stringify(after));

    // ---------- 4. nested modal must not unlock the page early
    const nested = await page.evaluate(async () => {
      const a = document.getElementById("uploadModal");
      const b = document.getElementById("face-detection-alert-modal");
      if (!a || !b) return { skipped: true };
      window.ModalStack.open(a, { backdropClose: true });
      window.ModalStack.open(b, { backdropClose: true });
      const both = { depth: window.ModalStack.depth(),
                     locked: document.body.classList.contains("modal-stack-locked") };
      window.ModalStack.close(b);
      await new Promise((r) => setTimeout(r, 200));
      const afterInner = { depth: window.ModalStack.depth(),
                           locked: document.body.classList.contains("modal-stack-locked") };
      window.ModalStack.close(a);
      await new Promise((r) => setTimeout(r, 200));
      const afterAll = { depth: window.ModalStack.depth(),
                         locked: document.body.classList.contains("modal-stack-locked"),
                         overflow: document.body.style.overflow };
      return { both, afterInner, afterAll };
    });
    if (nested.skipped) {
      check("nested modal keeps the lock until the last close", false, "modals not present");
    } else {
      check("nested modal keeps the lock until the last close",
        nested.both.depth === 2 && nested.both.locked &&
        nested.afterInner.depth === 1 && nested.afterInner.locked &&
        nested.afterAll.depth === 0 && !nested.afterAll.locked &&
        nested.afterAll.overflow === "",
        `depth 2->${nested.afterInner.depth}->${nested.afterAll.depth}, locked ${nested.both.locked}->${nested.afterInner.locked}->${nested.afterAll.locked}`);
    }

    // ---------- 5. every page reaches its own bottom
    for (const path of ["/dashboard", "/admin/settings", "/admin/search",
                        "/admin/users", "/admin/pipelines", "/admin/logs",
                        "/admin/watchlists", "/admin/intelligence"]) {
      await page.goto(`${BASE}${path}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(2200);
      const reach = await page.evaluate(() => {
        const cands = [...document.querySelectorAll("*")].filter((el) => {
          if (el.scrollHeight <= el.clientHeight + 1) return false;
          const o = getComputedStyle(el).overflowY;
          return o === "auto" || o === "scroll";
        });
        const doc = document.scrollingElement;
        const docScrolls = doc.scrollHeight > doc.clientHeight + 1;
        if (!cands.length && !docScrolls) return { noScroll: true };
        // The page's MAIN scroller, not merely the first in DOM order: a
        // nested table container appears earlier in the tree, and scrolling
        // that one to its end leaves the page itself untouched — which looks
        // like "reached the bottom but the last element is off-screen".
        cands.sort((a, b) => b.clientHeight - a.clientHeight);
        const sc = cands[0] || doc;
        // Scroll EVERY scroller to its end, which is what a user can do.
        // /admin/pipelines deliberately gives its table its own vertical
        // scroller inside the page scroller: the page moves 45px and the table
        // 364px, so scrolling only the outer one leaves the last row below the
        // fold and looks like unreachable content when it is not.
        cands.forEach((el) => { el.scrollTop = el.scrollHeight; });
        const atBottom = Math.abs(sc.scrollTop + sc.clientHeight - sc.scrollHeight) < 4;
        // Is the very last element inside the scroller actually on screen?
        const kids = sc.querySelectorAll("*");
        let lastVisible = true;
        for (let i = kids.length - 1; i >= 0 && i > kids.length - 40; i--) {
          const r = kids[i].getBoundingClientRect();
          if (r.height > 0 && r.width > 0) {
            lastVisible = r.bottom <= window.innerHeight + 2 && r.bottom > 0;
            break;
          }
        }
        cands.forEach((el) => { el.scrollTop = 0; });
        const overflowPx = document.body.scrollWidth - window.innerWidth;
        // Name the widest offender so the number is actionable rather than a
        // bare boolean.
        let worst = null;
        document.querySelectorAll("*").forEach((el) => {
          const b = el.getBoundingClientRect();
          if (b.width > 0 && b.right > window.innerWidth + 2 &&
              (!worst || b.right > worst.right)) {
            worst = { right: Math.round(b.right),
                      id: (el.id || el.className || el.tagName).toString().slice(0, 30) };
          }
        });
        return { atBottom, lastVisible, overflowPx, worst };
      });
      if (reach.noScroll) {
        check(`${path}: content fits, nothing to reach`, true);
      } else {
        check(`${path}: scrolls to its true bottom`, reach.atBottom && reach.lastVisible,
          `atBottom=${reach.atBottom} lastElementVisible=${reach.lastVisible}`);
        // KNOWN, PRE-EXISTING (navbar CSS untouched by this phase): the
        // navbar is flex-wrap:nowrap with overflow-x:visible, so once its
        // content exceeds the viewport the rightmost control spills by the
        // 20px right padding. Layout/responsive work is Phase 3; reported
        // here with its measurement rather than silenced.
        check(`${path}: no unwanted horizontal overflow`, reach.overflowPx <= 2,
          reach.overflowPx > 2
            ? `${reach.overflowPx}px past the viewport, widest: ${reach.worst ? reach.worst.id : "?"} (known, deferred to Phase 3)`
            : "none");
      }
    }

    check("no uncaught page errors", jsErrors.length === 0, jsErrors.slice(0, 2).join(" | "));
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 300));
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\nOVERALL ${VW}x${VH}: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
