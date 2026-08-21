/**
 * Suspicious-pattern detail popup probe.
 *
 *   node scripts/dev/pattern_detail_probe.js [--base http://localhost]
 *
 * Pattern cards were summaries with no way to see who was involved or why
 * the detector fired. Clicking a card now opens a popup. This drives the real
 * page: runs detection, opens the popup from a card by click AND keyboard,
 * and asserts the content (severity, evidence, every involved identity with
 * thumbnail-or-placeholder, id chip and profile link), the layering (modal
 * above the page, focus inside, page locked), the close paths (button,
 * Escape, backdrop) and that three open/close cycles leave no stale state.
 *
 * Read-only: detection only reads.
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
  const page = await browser.newContext({ viewport: { width: 1600, height: 900 } }).then(c => c.newPage());
  const errors = [];
  page.on("pageerror", e => errors.push(String(e).slice(0, 140)));
  page.on("console", m => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text().slice(0, 140)); });

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER); await page.fill("#password", PASS);
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);
    await page.goto(`${BASE}/admin/security-intelligence`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(3000);
    errors.length = 0;

    await page.click('.sec-tab[data-tab="patterns"]');
    await page.click("#patterns-detect-btn");
    await page.waitForFunction(() => document.querySelectorAll("#patterns-container .pattern-card").length > 0, { timeout: 60000 });

    const cards = await page.evaluate(() => {
      const all = [...document.querySelectorAll("#patterns-container .pattern-card-clickable")];
      return { count: all.length, roles: all.every(c => c.getAttribute("role") === "button" && c.tabIndex === 0) };
    });
    check("cards are openable controls (role=button, focusable)", cards.count > 0 && cards.roles, `${cards.count} card(s)`);

    // open the first card by click
    await page.click("#patterns-container .pattern-card-clickable");
    await page.waitForFunction(() => {
      const m = document.getElementById("pattern-detail-modal");
      return m && getComputedStyle(m).display !== "none";
    }, { timeout: 10000 });
    await page.waitForTimeout(2500);   // identity tiles fill in

    const detail = await page.evaluate(() => {
      const card = document.querySelector("#patterns-container .pattern-card-clickable");
      const involved = Number(card.querySelector(".pattern-detail-value:nth-of-type(1)") ? 0 : 0);
      const m = document.getElementById("pattern-detail-modal");
      const tiles = [...m.querySelectorAll(".pattern-identity-tile")];
      const rect = m.querySelector(".modal-content").getBoundingClientRect();
      const topmost = document.elementFromPoint(rect.left + rect.width / 2, rect.top + 20);
      return {
        title: document.getElementById("pattern-detail-title").textContent.trim(),
        severity: !!m.querySelector(".pattern-severity"),
        evidenceRows: m.querySelectorAll(".pattern-evidence-row").length,
        tiles: tiles.length,
        tilesWithImageOrIcon: tiles.filter(t => t.querySelector(".identity-item-thumbnail img, .identity-item-thumbnail i")).length,
        tilesWithChip: tiles.filter(t => t.querySelector(".identity-item-id[title]")).length,
        tilesWithLink: tiles.filter(t => /\/admin\/identity\//.test((t.querySelector(".pattern-identity-link") || {}).href || "")).length,
        namedTiles: tiles.filter(t => !/^Unknown #/.test(t.querySelector(".identity-item-name").textContent)).length,
        involvedFromCard: Number((card.textContent.match(/Identities Involved(\d+)/) || [])[1] || 0),
        modalAbovePage: m.contains(topmost),
        focusInside: m.contains(document.activeElement),
        bodyLocked: getComputedStyle(document.body).overflow === "hidden",
        bodyScrolls: (() => { const b = m.querySelector(".modal-body"); return getComputedStyle(b).overflowY; })(),
      };
    });
    check("popup shows type, severity and evidence", detail.title.length > 0 && detail.severity && detail.evidenceRows > 0,
      `title="${detail.title}" evidence=${detail.evidenceRows}`);
    check("every involved identity has a tile with thumbnail/icon, id chip and profile link",
      detail.tiles === detail.involvedFromCard && detail.tilesWithImageOrIcon === detail.tiles &&
      detail.tilesWithChip === detail.tiles && detail.tilesWithLink === detail.tiles,
      `tiles=${detail.tiles} involved=${detail.involvedFromCard} named=${detail.namedTiles}`);
    check("popup is on the modal layer with focus inside and the page locked",
      detail.modalAbovePage && detail.focusInside && detail.bodyLocked, JSON.stringify({ above: detail.modalAbovePage, focus: detail.focusInside, locked: detail.bodyLocked }));
    check("popup body is its own scroller", /auto|scroll/.test(detail.bodyScrolls), `overflow-y=${detail.bodyScrolls}`);

    // close via button, reopen via keyboard, close via Escape, reopen, close via backdrop
    const cycles = await page.evaluate(async () => {
      const m = document.getElementById("pattern-detail-modal");
      const isOpen = () => getComputedStyle(m).display !== "none";
      const out = {};
      document.getElementById("close-pattern-detail-modal").click();
      await new Promise(r => setTimeout(r, 300));
      out.closedByButton = !isOpen();

      const card = document.querySelector("#patterns-container .pattern-card-clickable");
      card.focus();
      card.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      await new Promise(r => setTimeout(r, 300));
      out.openedByKeyboard = isOpen();
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await new Promise(r => setTimeout(r, 300));
      out.closedByEscape = !isOpen();

      card.click();
      await new Promise(r => setTimeout(r, 300));
      const r = m.getBoundingClientRect();
      // click the backdrop: the modal container itself, away from the content box
      m.dispatchEvent(new MouseEvent("click", { bubbles: true, clientX: r.left + 5, clientY: r.top + 5 }));
      await new Promise(r2 => setTimeout(r2, 300));
      out.closedByBackdrop = !isOpen();

      out.bodyOverflowAfter = document.body.style.overflow;
      out.copies = document.querySelectorAll("#pattern-detail-modal").length;
      return out;
    });
    check("closes by button, opens by keyboard, closes by Escape, closes by backdrop",
      cycles.closedByButton && cycles.openedByKeyboard && cycles.closedByEscape && cycles.closedByBackdrop, JSON.stringify(cycles));
    check("three cycles leave no stale lock or duplicate modal", cycles.bodyOverflowAfter === "" && cycles.copies === 1,
      `overflow='${cycles.bodyOverflowAfter}' copies=${cycles.copies}`);

    check("zero console/page errors", errors.length === 0, errors.slice(0, 2).join(" | ") || "clean");
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 240));
  } finally {
    await browser.close();
  }
  const failed = results.filter(r => !r.ok);
  console.log(`\nOVERALL: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
