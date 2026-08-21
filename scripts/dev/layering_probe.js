/**
 * Layering / modal / dropdown / scrolling probe — headless Chrome.
 *
 *   node scripts/dev/layering_probe.js [--base http://localhost] [--viewport 1366x768]
 *
 * Asserts the application's INTENDED hierarchy as rendered, not the presence
 * of CSS rules:
 *
 *   content overlay < nav < nav dropdown/page popover
 *                   < modal < modal-owned popover < toast
 *
 * The decisive tool is document.elementFromPoint(): whatever the stylesheets
 * say, that is what the user's pointer actually hits. Note the probe does NOT
 * assert "the modal is topmost everywhere" — --z-toast sits deliberately above
 * --z-modal-base so a notification raised from inside a modal flow is visible,
 * so the toast layer is allowed BY NAME.
 *
 * playwright-core is resolved the same way as browser_smoke.js (no network,
 * no browser download).
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
const [VW, VH] = arg("--viewport", "1366x768").split("x").map(Number);

// Pages chosen for what they put on screen, not for coverage's sake:
// dashboard = the 14997-15000 offenders; intelligence = the reported page and
// the 10000-10002 selectors; live-alerts = notification tier + video/canvas;
// search = big tables; settings = long forms; unknown = the one page that
// already had the stack (must not regress).
const PAGES = [
  "/dashboard",
  "/admin/intelligence",
  "/admin/live-alerts",
  "/admin/search",
  "/admin/settings",
  "/admin/unknown",
];

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

async function login(page) {
  await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
  await page.fill("#username", USER);
  await page.fill("#password", PASS);
  await Promise.all([
    page.waitForNavigation({ waitUntil: "domcontentloaded", timeout: 30000 }),
    page.click('button[type="submit"]'),
  ]);
}

async function openAddPerson(page) {
  const toggle = page.locator('[data-dropdown="management"] .dropdown-toggle').first();
  if (await toggle.count()) { await toggle.click().catch(() => {}); await page.waitForTimeout(300); }
  const item = page.locator('.dropdown-item[data-page="add-person"]').first();
  if (!(await item.count())) return false;
  await item.click();
  await page.waitForFunction(() => {
    const m = document.getElementById("uploadModal");
    return m && getComputedStyle(m).display !== "none";
  }, { timeout: 10000 }).catch(() => {});
  return page.evaluate(() => {
    const m = document.getElementById("uploadModal");
    return !!m && getComputedStyle(m).display !== "none";
  });
}

/** Sample a grid; report every topmost element that is NOT inside the modal
 *  and NOT part of an allowed-above layer. */
async function escapees(page) {
  return page.evaluate(() => {
    const modal = document.getElementById("uploadModal");
    if (!modal) return ["no modal"];
    // Layers legitimately allowed above a modal, named explicitly.
    const ALLOWED = [".background-task-alerts-container", "#alert-popup-container",
                     ".realtime-notification", ".live-alert-notification",
                     "#bt-notice-area", ".alert-trigger-popup", ".skip-to-main"];
    const bad = new Map();
    const stepX = Math.max(40, Math.floor(window.innerWidth / 24));
    const stepY = Math.max(40, Math.floor(window.innerHeight / 16));
    for (let x = 4; x < window.innerWidth; x += stepX) {
      for (let y = 4; y < window.innerHeight; y += stepY) {
        const el = document.elementFromPoint(x, y);
        if (!el || modal.contains(el) || el === modal) continue;
        if (ALLOWED.some((sel) => el.closest(sel))) continue;
        const id = el.id ? "#" + el.id
          : (el.className && typeof el.className === "string"
              ? "." + el.className.trim().split(/\s+/).slice(0, 2).join(".")
              : el.tagName.toLowerCase());
        bad.set(id, (bad.get(id) || 0) + 1);
      }
    }
    return [...bad.entries()].map(([k, v]) => `${k}x${v}`);
  });
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const ctx = await browser.newContext({ viewport: { width: VW, height: VH } });
  const page = await ctx.newPage();
  const jsErrors = [];
  page.on("pageerror", (e) => jsErrors.push(String(e).slice(0, 160)));

  try {
    await login(page);
    console.log(`\n=== viewport ${VW}x${VH} ===`);

    for (const path of PAGES) {
      await page.goto(`${BASE}${path}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(2200);
      const label = path.replace("/admin/", "").replace("/", "");

      // --- the stack must actually be present now
      const hasStack = await page.evaluate(() => !!window.ModalStack);
      check(`${label}: ModalStack present`, hasStack);
      if (!hasStack) continue;

      // --- scroll the page first: the brief asks for "already scrolled"
      const scrollInfo = await page.evaluate(() => {
        const cands = [document.scrollingElement, ...document.querySelectorAll("*")].filter(
          (el) => el && el.scrollHeight > el.clientHeight + 1);
        const sc = cands.find((el) => {
          const o = getComputedStyle(el).overflowY;
          return o === "auto" || o === "scroll";
        }) || document.scrollingElement;
        sc.scrollTop = Math.floor(sc.scrollHeight / 2);
        // Pin the element itself for the rest of the cycle. Re-finding it
        // later cannot work: once the lock freezes it to overflow:hidden it no
        // longer matches an "is scrollable" search, so the restore check would
        // silently measure a different element (or none) and pass vacuously.
        window.__probeScroller = sc;
        window.__probeScrollTop = sc.scrollTop;
        return { top: sc.scrollTop, tag: sc.id || sc.className || sc.tagName };
      });

      // --- Add Person over a scrolled page
      const opened = await openAddPerson(page);
      check(`${label}: Add Person opens`, opened, `page pre-scrolled to ${scrollInfo.top}`);
      if (!opened) continue;

      const bad = await escapees(page);
      check(`${label}: nothing escapes above the modal`, bad.length === 0,
        bad.length ? bad.slice(0, 4).join(", ") : `grid clean`);

      // --- background must be inert AND not scrollable
      // A scroll lock stops the USER scrolling. Setting scrollTop from script
      // still moves an overflow:hidden element, so a programmatic nudge proves
      // nothing — this dispatches a real wheel over the background instead.
      const beforeWheel = await page.evaluate(
        () => (window.__probeScroller ? window.__probeScroller.scrollTop : -1));
      await page.mouse.move(8, Math.floor(VH / 2));
      await page.mouse.wheel(0, 600);
      await page.waitForTimeout(350);
      const locked = await page.evaluate(() => ({
        cls: document.body.classList.contains("modal-stack-locked"),
        overflow: document.body.style.overflow,
        frozen: window.__probeScroller ? getComputedStyle(window.__probeScroller).overflowY : "n/a",
        after: window.__probeScroller ? window.__probeScroller.scrollTop : -1,
      }));
      check(`${label}: background does not scroll under the modal`,
        locked.cls && locked.overflow === "hidden" && locked.after === beforeWheel,
        `overflow=${locked.overflow} inner=${locked.frozen} scrollTop ${beforeWheel} -> ${locked.after}`);

      // --- navbar dropdown must NOT come out above an open modal
      // The dropdown must already BE open before the modal, then stay under
      // it. Clicking the navbar while the modal is open is not a valid way to
      // test this: that click lands on the modal's backdrop and (correctly,
      // via backdropClose) closes the modal, after which any hit-test finds
      // page content and the probe accuses the product of its own mistake.
      const navAbove = await page.evaluate(() => {
        const wrap = document.querySelector('[data-dropdown="management"]');
        const dd = wrap && wrap.querySelector(".dropdown-menu");
        const modal = document.getElementById("uploadModal");
        if (!dd || !modal) return null;
        // Force it visible without a click, so the modal stays open.
        wrap.setAttribute("data-open", "true");
        dd.style.visibility = "visible";
        dd.style.opacity = "1";
        const r = dd.getBoundingClientRect();
        const hit = document.elementFromPoint(
          Math.min(window.innerWidth - 2, Math.max(2, r.left + r.width / 2)),
          Math.min(window.innerHeight - 2, Math.max(2, r.top + r.height / 2)));
        const inside = !!hit && (modal === hit || modal.contains(hit));
        const zNav = parseInt(getComputedStyle(wrap.closest(".military-navbar") || wrap).zIndex, 10) || 0;
        const zModal = parseInt(getComputedStyle(modal).zIndex, 10) || 0;
        dd.style.visibility = "";
        dd.style.opacity = "";
        wrap.removeAttribute("data-open");
        return { inside, zNav, zModal,
                 hitId: hit ? (hit.id || hit.className || hit.tagName) : "none" };
      });
      check(`${label}: an OPEN navbar dropdown stays under the modal`,
        !navAbove || (navAbove.inside && navAbove.zNav < navAbove.zModal),
        navAbove ? `nav=${navAbove.zNav} modal=${navAbove.zModal} hit=${String(navAbove.hitId).slice(0, 32)}` : "n/a");

      // --- close and verify restoration
      const restored = await page.evaluate(async () => {
        const body = document.body;
        const sc = window.__probeScroller;
        const beforeTop = window.__probeScrollTop || 0;
        window.ModalStack.close(document.getElementById("uploadModal"));
        await new Promise((r) => setTimeout(r, 400));
        return {
          cls: body.classList.contains("modal-stack-locked"),
          overflow: body.style.overflow,
          position: body.style.position,
          paddingRight: body.style.paddingRight,
          scrollTop: sc ? sc.scrollTop : 0,
          keptScroll: sc ? Math.abs(sc.scrollTop - beforeTop) < 4 : true,
          stale: document.querySelectorAll("#uploadModal").length,
        };
      });
      check(`${label}: unlock restores body styles`,
        !restored.cls && restored.overflow === "" && restored.position === "" &&
        restored.paddingRight === "",
        `overflow='${restored.overflow}' position='${restored.position}' pad='${restored.paddingRight}'`);
      check(`${label}: scroll position preserved`, restored.keptScroll,
        `scrollTop=${restored.scrollTop}`);
      check(`${label}: no duplicate modal left behind`, restored.stale === 1,
        `${restored.stale} #uploadModal node(s)`);
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
