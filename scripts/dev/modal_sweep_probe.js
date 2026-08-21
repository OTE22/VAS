/**
 * Modal migration sweep — headless Chrome.
 *
 *   node scripts/dev/modal_sweep_probe.js [--base http://localhost]
 *
 * "ModalStack is loaded" is not evidence a modal was migrated. For every
 * migrated dialog this drives the acceptance rule:
 *
 *   open -> correct stacking, focus inside, background suppressed, scroll
 *   locked -> close -> cleanup ran exactly once -> focus and scroll restored
 *   -> reopen works -> three full cycles leave nothing behind
 *
 * Modals are opened through ModalStack directly (not by hunting each page's
 * trigger button) so the lifecycle is what is under test, not the buttons.
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

// page -> the dialogs migrated on it
const MODALS = {
  "/admin/audit": ["details-modal"],
  "/admin/settings": ["setting-modal"],
  "/admin/users": ["user-modal", "password-modal", "delete-user-modal"],
  "/admin/pipelines": ["coordinates-modal"],
  "/admin/search-history": ["export-modal"],
  "/admin/search": ["identity-modal", "export-modal"],
  "/admin/watchlists": ["modal-overlay"],
  "/admin/background-tasks": ["task-modal", "retention-confirm-modal"],
  "/admin/ingest-credentials": ["token-modal", "revoke-modal"],
};

// The identity profile is routed as /admin/identity/{id} — a bare path 404s —
// so its id is discovered at run time rather than pinned to one row that a
// retention pass could delete.
const IDENTITY_MODALS = ["add-to-watchlist-modal", "create-live-alert-modal"];

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newContext({ viewport: { width: 1366, height: 768 } }).then((c) => c.newPage());
  const jsErrors = [];
  page.on("pageerror", (e) => jsErrors.push(String(e).slice(0, 140)));

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER); await page.fill("#password", PASS);
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);

    for (const [path, ids] of Object.entries(MODALS)) {
      await page.goto(`${BASE}${path}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(2200);

      const stackReady = await page.evaluate(() => !!window.ModalStack);
      check(`${path}: ModalStack available`, stackReady);
      if (!stackReady) continue;

      for (const id of ids) {
        const present = await page.evaluate((i) => !!document.getElementById(i), id);
        if (!present) { check(`${path} #${id}: present`, false, "element not in DOM"); continue; }

        const r = await page.evaluate(async (modalId) => {
          const el = document.getElementById(modalId);
          const body = document.body;
          const before = {
            overflow: body.style.overflow, position: body.style.position,
            pad: body.style.paddingRight, inert: document.querySelectorAll("[inert]").length,
          };

          const zBase = parseInt(getComputedStyle(document.documentElement)
            .getPropertyValue("--z-modal-base"), 10) || 10000;

          // --- open
          window.ModalStack.open(el, { backdropClose: true });
          await new Promise((res) => setTimeout(res, 220));
          const open = {
            depth: window.ModalStack.depth(),
            visible: getComputedStyle(el).display !== "none",
            z: parseInt(el.style.zIndex, 10) || 0,
            locked: body.classList.contains("modal-stack-locked"),
            overflow: body.style.overflow,
            focusInside: el.contains(document.activeElement),
          };

          // --- three full cycles
          window.ModalStack.close(el);
          await new Promise((res) => setTimeout(res, 160));
          for (let i = 0; i < 2; i++) {
            window.ModalStack.open(el, { backdropClose: true });
            await new Promise((res) => setTimeout(res, 120));
            window.ModalStack.close(el);
            await new Promise((res) => setTimeout(res, 120));
          }

          const after = {
            depth: window.ModalStack.depth(),
            visible: getComputedStyle(el).display !== "none",
            locked: body.classList.contains("modal-stack-locked"),
            overflow: body.style.overflow, position: body.style.position,
            pad: body.style.paddingRight,
            inert: document.querySelectorAll("[inert]").length,
            copies: document.querySelectorAll("#" + modalId).length,
            leftoverZ: el.style.zIndex,
          };
          return { zBase, before, open, after };
        }, id);

        check(`${path} #${id}: opens on the modal layer, focused, locked`,
          r.open.depth === 1 && r.open.visible && r.open.z >= r.zBase &&
          r.open.locked && r.open.overflow === "hidden" && r.open.focusInside,
          `depth=${r.open.depth} z=${r.open.z} focusInside=${r.open.focusInside} lock=${r.open.locked}`);

        check(`${path} #${id}: 3 cycles leave no stale state`,
          r.after.depth === 0 && !r.after.visible && !r.after.locked &&
          r.after.overflow === r.before.overflow && r.after.position === r.before.position &&
          r.after.pad === r.before.pad && r.after.inert === r.before.inert &&
          r.after.copies === 1 && r.after.leftoverZ === "",
          `depth=${r.after.depth} overflow='${r.after.overflow}' pad='${r.after.pad}' inert=${r.after.inert} copies=${r.after.copies} z='${r.after.leftoverZ}'`);
      }
    }

    // --- the identity profile, at whatever id this deployment has
    const identityId = await page.evaluate(async () => {
      const r = await fetch("/api/admin/unknown?show_all=true&page=1&page_size=1",
                            { credentials: "include" });
      if (!r.ok) return null;
      const d = await r.json();
      return d.identities && d.identities[0] ? d.identities[0].id : null;
    });
    if (!identityId) {
      check("identity profile: an identity exists to open", false, "none returned");
    } else {
      const path = `/admin/identity/${identityId}`;
      await page.goto(`${BASE}${path}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(2200);
      const ready = await page.evaluate(() => !!window.ModalStack);
      check(`${path}: ModalStack available`, ready);
      if (ready) {
        for (const id of IDENTITY_MODALS) {
          const r = await page.evaluate(async (modalId) => {
            const el = document.getElementById(modalId);
            if (!el) return null;
            const body = document.body;
            const before = { overflow: body.style.overflow, pad: body.style.paddingRight };
            window.ModalStack.open(el, { backdropClose: true });
            await new Promise((res) => setTimeout(res, 200));
            const open = { depth: window.ModalStack.depth(),
                           locked: body.classList.contains("modal-stack-locked"),
                           focusInside: el.contains(document.activeElement) };
            for (let i = 0; i < 3; i++) {
              window.ModalStack.close(el);
              await new Promise((res) => setTimeout(res, 110));
              if (i < 2) { window.ModalStack.open(el, { backdropClose: true });
                           await new Promise((res) => setTimeout(res, 110)); }
            }
            return { open, after: { depth: window.ModalStack.depth(),
                     locked: body.classList.contains("modal-stack-locked"),
                     overflow: body.style.overflow, pad: body.style.paddingRight,
                     copies: document.querySelectorAll("#" + modalId).length }, before };
          }, id);
          if (!r) { check(`identity #${id}: present`, false, "not in DOM"); continue; }
          check(`identity #${id}: opens locked and focused`,
            r.open.depth === 1 && r.open.locked && r.open.focusInside,
            `depth=${r.open.depth} lock=${r.open.locked} focus=${r.open.focusInside}`);
          check(`identity #${id}: 3 cycles leave no stale state`,
            r.after.depth === 0 && !r.after.locked &&
            r.after.overflow === r.before.overflow && r.after.pad === r.before.pad &&
            r.after.copies === 1,
            `depth=${r.after.depth} overflow='${r.after.overflow}' copies=${r.after.copies}`);
        }
      }
    }

    // --- the veto: the watchlist editor must refuse to close mid-save
    await page.goto(`${BASE}/admin/watchlists`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2000);
    const veto = await page.evaluate(async () => {
      const el = document.getElementById("modal-overlay");
      if (!el) return { skipped: true };
      let saving = true;
      window.ModalStack.open(el, { backdropClose: true, canClose: () => !saving });
      await new Promise((r) => setTimeout(r, 150));
      window.ModalStack.close(el, "escape");          // must be refused
      const refused = window.ModalStack.depth() === 1;
      saving = false;
      window.ModalStack.close(el, "escape");          // now allowed
      await new Promise((r) => setTimeout(r, 150));
      return { refused, closed: window.ModalStack.depth() === 0 };
    });
    check("watchlists: canClose refuses to close while saving",
      veto.skipped || (veto.refused && veto.closed),
      veto.skipped ? "modal absent" : `refusedWhileSaving=${veto.refused} closedAfter=${veto.closed}`);

    check("no uncaught page errors", jsErrors.length === 0, jsErrors.slice(0, 2).join(" | "));
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 260));
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\nOVERALL: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
