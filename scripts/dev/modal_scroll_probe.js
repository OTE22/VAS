/**
 * Modal scrollability probe — Add Person and the Unknown Faces modals.
 *
 *   node scripts/dev/modal_scroll_probe.js [--base http://localhost] [--viewport WxH]
 *
 * A modal taller than the viewport must scroll INSIDE itself: the page behind
 * it is scroll-locked while it is open, so anything that overflows is simply
 * unreachable — you cannot read the bottom of a form or reach its buttons.
 *
 * Guessing from CSS is unreliable here (several `.modal-content` rules exist in
 * admin.css, and the Unknown page nests `.modal-content` inside
 * `.security-modal` rather than `.modal`), so this measures instead: it opens
 * each dialog, injects a tall sentinel into its body, and asks whether the
 * sentinel can actually be scrolled to.
 *
 * Writes nothing to the database — it only opens dialogs and removes the
 * sentinel afterwards.
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
const [VW, VH] = arg("--viewport", "1280x620").split("x").map(Number);

// Unknown-page dialogs that carry real content a user must read to the bottom.
const UNKNOWN_MODALS = [
  "identity-detail-modal",
  "merge-modal",
  "promote-modal",
  "search-image-modal",
  "create-live-alert-modal",
  "add-to-watchlist-modal",
  "merge-suggestions-modal",
];

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

/** Open a modal, force it to overflow, and report whether it can be scrolled. */
async function measure(page, modalId) {
  return page.evaluate(async (id) => {
    const modal = document.getElementById(id);
    if (!modal) return { missing: true };

    if (window.ModalStack && !window.ModalStack.isOpen(modal)) {
      window.ModalStack.open(modal, { backdropClose: true });
    }
    modal.classList.add("active");
    await new Promise((r) => setTimeout(r, 350));

    // The visible box inside the backdrop.
    const box = modal.querySelector(
      ".upload-modal-content, .modal-content, .security-modal-content, .modal-dialog")
      || modal.firstElementChild;
    if (!box) return { noBox: true };

    // Put the sentinel where the content lives, so we exercise the real
    // scroller rather than one we introduce.
    const body = box.querySelector(".modal-body, .upload-form, .modal-form") || box;
    const sentinel = document.createElement("div");
    sentinel.id = "__scroll_sentinel__";
    sentinel.style.cssText = "height:1400px;display:block;";
    sentinel.textContent = "sentinel";
    body.appendChild(sentinel);
    await new Promise((r) => setTimeout(r, 150));

    // Whichever element actually scrolls: the box, the body, or an ancestor.
    const candidates = [body, box, modal];
    let scroller = null;
    for (const el of candidates) {
      const style = getComputedStyle(el);
      if (/auto|scroll/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 2) {
        scroller = el;
        break;
      }
    }

    const boxRect = box.getBoundingClientRect();
    const topVisible = boxRect.top >= -1;              // header must not be cut off
    let bottomReachable = false;
    let scrolledBy = 0;

    if (scroller) {
      scroller.scrollTop = scroller.scrollHeight;
      await new Promise((r) => setTimeout(r, 120));
      scrolledBy = Math.round(scroller.scrollTop);
      const sRect = sentinel.getBoundingClientRect();
      const vRect = scroller.getBoundingClientRect();
      // The sentinel's end must come into the scroller's visible area.
      bottomReachable = sRect.bottom <= vRect.bottom + 2 && sRect.bottom > vRect.top;
      scroller.scrollTop = 0;
    }

    const answer = {
      scrollerFound: !!scroller,
      scrollerClass: scroller ? (scroller.className || scroller.tagName).toString().slice(0, 40) : null,
      boxTop: Math.round(boxRect.top),
      boxBottom: Math.round(boxRect.bottom),
      viewportH: window.innerHeight,
      topVisible,
      bottomReachable,
      scrolledBy,
    };

    sentinel.remove();
    if (window.ModalStack && window.ModalStack.isOpen(modal)) {
      window.ModalStack.close(modal, "force");
    }
    modal.classList.remove("active");
    modal.style.display = "none";
    await new Promise((r) => setTimeout(r, 200));
    return answer;
  }, modalId);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newContext({ viewport: { width: VW, height: VH } })
    .then((c) => c.newPage());

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER); await page.fill("#password", PASS);
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }),
                       page.click('button[type="submit"]')]);

    // ---- Add Person, from a page that loads it via the navbar loader
    await page.goto(`${BASE}/dashboard`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2500);
    const upload = await measure(page, "uploadModal");
    if (upload.missing) {
      check("Add Person: modal present", false, "#uploadModal not found");
    } else {
      check("Add Person: scrolls internally when taller than the viewport",
        upload.scrollerFound && upload.bottomReachable,
        `scroller=${upload.scrollerClass} scrolledBy=${upload.scrolledBy}px ` +
        `bottomReachable=${upload.bottomReachable}`);
      check("Add Person: its header stays on screen",
        upload.topVisible,
        `box top=${upload.boxTop} (viewport ${upload.viewportH})`);
    }

    // ---- Unknown Faces dialogs
    await page.goto(`${BASE}/admin/unknown`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(3000);

    for (const id of UNKNOWN_MODALS) {
      const r = await measure(page, id);
      if (r.missing) { console.log(`  ---   ${id}: not on this page`); continue; }
      if (r.noBox) { check(`${id}: has a content box`, false); continue; }
      check(`${id}: scrolls internally when taller than the viewport`,
        r.scrollerFound && r.bottomReachable,
        `scroller=${r.scrollerClass} scrolledBy=${r.scrolledBy}px ` +
        `bottomReachable=${r.bottomReachable}`);
      check(`${id}: its header stays on screen`,
        r.topVisible, `box top=${r.boxTop} (viewport ${r.viewportH})`);
    }
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 240));
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\nOVERALL ${VW}x${VH}: ${failed.length ? "FAIL" : "PASS"} ` +
              `(${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
