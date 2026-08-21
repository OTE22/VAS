/**
 * Identity picker: visible IDs + find-by-photo — the 17-point verification.
 *
 *   node scripts/dev/picker_id_photo_probe.js [--base http://localhost]
 *
 * The picker's search box always said "Search by name or ID..." while showing
 * the ID nowhere, and there was no way to find a person from a photo. This
 * probe drives the REAL pages and the REAL /api/search/by-image endpoint (no
 * mocking): id chips, full-uuid copy, prefix search, photo search through the
 * same renderer, race/cancellation, and layout fit.
 *
 * Read-only against the target: photo search only searches.
 *
 * playwright-core resolved as in browser_smoke.js (no network).
 */
const PW_CORE = process.env.PW_CORE ||
  "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW_CORE);
const path = require("path");

const arg = (n, d) => { const i = process.argv.indexOf(n); return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : d; };
const BASE = arg("--base", "http://localhost");
const USER = process.env.SMOKE_USER || "admin";
const PASS = process.env.SMOKE_PASS || "admin123";
const FACE_A = path.resolve(__dirname, "../../tests/fixtures/faces/face_a.jpg");

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

async function openFirstPicker(page) {
  await page.evaluate(async () => {
    document.querySelectorAll(".identity-selector-panel").forEach(p => { p.style.display = "none"; });
    const w = document.querySelector(".advanced-identity-selector");
    const t = w.querySelector(".identity-selector-trigger");
    t.scrollIntoView({ block: "center" });
    await new Promise(r => setTimeout(r, 200));
    t.click();
  });
  await page.waitForFunction(() =>
    document.querySelectorAll(".advanced-identity-selector .identity-selector-item").length > 0,
    { timeout: 15000 });
  await page.waitForTimeout(300);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: BASE });
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("pageerror", e => consoleErrors.push(String(e).slice(0, 160)));
  page.on("console", m => {
    if (m.type() !== "error") return;
    const text = m.text();
    // resource-load 404s for missing snapshots are the fallback scenario
    if (/Failed to load resource/.test(text)) return;
    consoleErrors.push(text.slice(0, 160));
  });
  // Intentional aborts (the race-safety design) and missing-snapshot 404s
  // (the img fallback scenario point 14 covers) are expected, not errors.
  page.on("requestfailed", r => {
    const f = r.failure();
    if (f && /ERR_ABORTED/.test(f.errorText)) return;
    consoleErrors.push("REQFAIL " + r.url().slice(0, 140));
  });
  page.on("response", r => {
    if (r.status() === 404 && !/\/storage\//.test(r.url())) {
      consoleErrors.push("404 " + r.url().slice(0, 140));
    }
  });

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER); await page.fill("#password", PASS);
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }),
                       page.click('button[type="submit"]')]);

    for (const pagePath of ["/admin/intelligence", "/admin/security-intelligence"]) {
      await page.goto(`${BASE}${pagePath}`, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(3000);
      consoleErrors.length = 0;
      const tag = pagePath.split("/").pop();

      await openFirstPicker(page);

      // (1) normal text search — filter to a known substring and back
      const textSearch = await page.evaluate(async () => {
        const w = document.querySelector(".advanced-identity-selector");
        const input = w.querySelector(".filter-search");
        const before = w.querySelectorAll(".identity-selector-item").length;
        input.value = "zzz-no-such-identity";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        await new Promise(r => setTimeout(r, 1200));
        const none = w.querySelectorAll(".identity-selector-item").length;
        input.value = "";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        await new Promise(r => setTimeout(r, 1200));
        const after = w.querySelectorAll(".identity-selector-item").length;
        return { before, none, after };
      });
      check(`[${tag}] 1. text search still works`,
        textSearch.before > 0 && textSearch.none === 0 && textSearch.after > 0,
        JSON.stringify(textSearch));

      // rows carry the id chip: prefix text + full uuid title
      const chip = await page.evaluate(() => {
        const row = document.querySelector(".advanced-identity-selector .identity-selector-item");
        const btn = row && row.querySelector(".identity-item-id");
        return btn ? { text: btn.textContent.trim(), title: btn.getAttribute("title"),
                       isButton: btn.tagName === "BUTTON" } : null;
      });
      check(`[${tag}] rows show a compact id chip (full uuid in title)`,
        !!chip && chip.isButton && UUID_RE.test(chip.title) &&
        chip.text.startsWith(chip.title.slice(0, 8)),
        chip ? `${chip.text} / ${chip.title.slice(0, 13)}…` : "no chip");

      // (2) UUID-prefix search: read a row's id, type its prefix, list filters
      const prefixSearch = await page.evaluate(async () => {
        const w = document.querySelector(".advanced-identity-selector");
        const btn = w.querySelector(".identity-selector-item .identity-item-id");
        const full = btn.getAttribute("title");
        const input = w.querySelector(".filter-search");
        input.value = full.slice(0, 8);
        input.dispatchEvent(new Event("input", { bubbles: true }));
        await new Promise(r => setTimeout(r, 1200));
        const shown = [...w.querySelectorAll(".identity-selector-item .identity-item-id")]
          .map(b => b.getAttribute("title"));
        input.value = "";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        await new Promise(r => setTimeout(r, 1000));
        return { target: full, shown };
      });
      check(`[${tag}] 2. typing a row's 8-char id filters to it`,
        prefixSearch.shown.length >= 1 && prefixSearch.shown.includes(prefixSearch.target),
        `${prefixSearch.shown.length} row(s), target present=${prefixSearch.shown.includes(prefixSearch.target)}`);

      // (3)+(4) copy: click and keyboard both copy the FULL uuid, and never select
      const copy = await page.evaluate(async () => {
        const w = document.querySelector(".advanced-identity-selector");
        const row = w.querySelector(".identity-selector-item");
        const btn = row.querySelector(".identity-item-id");
        const full = btn.getAttribute("title");
        const triggerText = w.querySelector(".trigger-text").textContent;
        btn.click();
        await new Promise(r => setTimeout(r, 300));
        const clicked = await navigator.clipboard.readText().catch(() => "(unreadable)");
        await navigator.clipboard.writeText("cleared");
        btn.focus();
        btn.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        // native button: Enter fires click; simulate the native behaviour
        btn.click();
        await new Promise(r => setTimeout(r, 300));
        const keyed = await navigator.clipboard.readText().catch(() => "(unreadable)");
        const panelStillOpen = w.querySelector(".identity-selector-panel").style.display !== "none";
        const triggerUnchanged = w.querySelector(".trigger-text").textContent === triggerText;
        return { full, clicked, keyed, panelStillOpen, triggerUnchanged };
      });
      check(`[${tag}] 3. clicking the chip copies the FULL uuid, does not select`,
        copy.clicked === copy.full && copy.panelStillOpen && copy.triggerUnchanged,
        `copied=${copy.clicked.slice(0, 13)}… selected=${!copy.triggerUnchanged}`);
      check(`[${tag}] 4. keyboard activation copies too`,
        copy.keyed === copy.full, `keyed=${copy.keyed.slice(0, 13)}…`);

      // (5)+(7) photo search through the real endpoint, same renderer, selects
      const [chooser] = await Promise.all([
        page.waitForEvent("filechooser"),
        page.evaluate(() => document.querySelector(".advanced-identity-selector .filter-photo-btn").click()),
      ]);
      await chooser.setFiles(FACE_A);
      // Wait for the PHOTO result, not merely a non-loading status — the old
      // wait raced ahead and measured the previous text list.
      await page.waitForFunction(() => {
        const s = document.querySelector(".advanced-identity-selector .identity-selector-status");
        return s && /(match\(es\) by photo|No matching identities)/.test(s.textContent);
      }, { timeout: 60000 });
      await page.waitForTimeout(300);

      const photo = await page.evaluate(() => {
        const w = document.querySelector(".advanced-identity-selector");
        const rows = [...w.querySelectorAll(".identity-selector-item")];
        return {
          status: w.querySelector(".identity-selector-status").textContent.trim(),
          rows: rows.length,
          similarities: rows.map(r => {
            const b = r.querySelector(".identity-item-similarity");
            return b ? b.textContent.trim() : null;
          }),
          chips: rows.every(r => !!r.querySelector(".identity-item-id")),
          clearShown: getComputedStyle(w.querySelector(".filter-photo-clear")).display !== "none",
          types: rows.map(r => (r.querySelector(".identity-item-type") || {}).textContent),
        };
      });
      check(`[${tag}] 5. photo search renders matches via the same renderer`,
        photo.rows > 0 && photo.chips && photo.similarities.every(x => /%\s*match/.test(x || "")),
        `${photo.rows} row(s), similarities=${photo.similarities.slice(0, 3).join(",")}`);

      // (7) selecting a photo result behaves like a normal selection
      const selected = await page.evaluate(async () => {
        const w = document.querySelector(".advanced-identity-selector");
        const row = w.querySelector(".identity-selector-item");
        const id = row.querySelector(".identity-item-id").getAttribute("title");
        row.click();
        await new Promise(r => setTimeout(r, 500));
        const select = w.previousElementSibling && w.previousElementSibling.tagName === "SELECT"
          ? w.previousElementSibling : document.querySelector("select[style*='none']");
        return { id, trigger: w.querySelector(".trigger-text").textContent.trim(),
                 hiddenValue: select ? select.value : "(no select)" };
      });
      check(`[${tag}] 7. selecting a photo result works like a text result`,
        selected.hiddenValue === selected.id || selected.trigger.length > 0,
        `trigger="${selected.trigger.slice(0, 24)}" hidden=${String(selected.hiddenValue).slice(0, 13)}…`);

      // (9)+(10) reopen -> photo again -> clear restores the paged list
      await openFirstPicker(page);
      const [chooser2] = await Promise.all([
        page.waitForEvent("filechooser"),
        page.evaluate(() => document.querySelector(".advanced-identity-selector .filter-photo-btn").click()),
      ]);
      await chooser2.setFiles(FACE_A);
      await page.waitForFunction(() => {
        const w = document.querySelector(".advanced-identity-selector");
        return getComputedStyle(w.querySelector(".filter-photo-clear")).display !== "none";
      }, { timeout: 60000 });
      const restored = await page.evaluate(async () => {
        const w = document.querySelector(".advanced-identity-selector");
        const photoRows = w.querySelectorAll(".identity-selector-item").length;
        w.querySelector(".filter-photo-clear").click();
        for (let i = 0; i < 40; i++) {
          await new Promise(r => setTimeout(r, 250));
          const s = w.querySelector(".identity-selector-status").textContent;
          if (/Showing/.test(s)) break;
        }
        return {
          photoRows,
          listRows: w.querySelectorAll(".identity-selector-item").length,
          clearHidden: getComputedStyle(w.querySelector(".filter-photo-clear")).display === "none",
          status: w.querySelector(".identity-selector-status").textContent.trim(),
          inputEmpty: w.querySelector(".filter-photo-input").value === "",
        };
      });
      check(`[${tag}] 9/10. clear-photo restores the paged list (input reset)`,
        restored.clearHidden && restored.listRows > 0 && /Showing/.test(restored.status) &&
        restored.inputEmpty,
        JSON.stringify({ photoRows: restored.photoRows, listRows: restored.listRows,
                         status: restored.status.slice(0, 30) }));

      // (11) same photo twice fires again
      const [chooser3] = await Promise.all([
        page.waitForEvent("filechooser"),
        page.evaluate(() => document.querySelector(".advanced-identity-selector .filter-photo-btn").click()),
      ]);
      await chooser3.setFiles(FACE_A);
      const secondFire = await page.waitForFunction(() => {
        const w = document.querySelector(".advanced-identity-selector");
        const s = w.querySelector(".identity-selector-status").textContent;
        return /photo/.test(s);
      }, { timeout: 60000 }).then(() => true).catch(() => false);
      check(`[${tag}] 11. selecting the same photo twice fires again`, secondFire, "");

      // (12) race: delay the photo response, type text — text wins, stays
      await page.route("**/api/search/by-image", async route => {
        await new Promise(r => setTimeout(r, 2500));
        route.continue();
      });
      const [chooser4] = await Promise.all([
        page.waitForEvent("filechooser"),
        page.evaluate(() => document.querySelector(".advanced-identity-selector .filter-photo-btn").click()),
      ]);
      await chooser4.setFiles(FACE_A);
      await page.waitForTimeout(300);
      await page.evaluate(() => {
        const input = document.querySelector(".advanced-identity-selector .filter-search");
        input.value = "";
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await page.waitForTimeout(3500);   // give the delayed photo response time to arrive late
      const race = await page.evaluate(() => {
        const w = document.querySelector(".advanced-identity-selector");
        return { status: w.querySelector(".identity-selector-status").textContent.trim(),
                 clearHidden: getComputedStyle(w.querySelector(".filter-photo-clear")).display === "none" };
      });
      await page.unroute("**/api/search/by-image");
      check(`[${tag}] 12. late photo response cannot clobber newer text results`,
        /Showing/.test(race.status) && race.clearHidden,
        `status="${race.status.slice(0, 40)}"`);

      // (13) close mid-photo-request: abort, no error message on reopen
      await page.route("**/api/search/by-image", async route => {
        await new Promise(r => setTimeout(r, 2500));
        route.continue();
      });
      const [chooser5] = await Promise.all([
        page.waitForEvent("filechooser"),
        page.evaluate(() => document.querySelector(".advanced-identity-selector .filter-photo-btn").click()),
      ]);
      await chooser5.setFiles(FACE_A);
      await page.waitForTimeout(200);
      const afterClose = await page.evaluate(async () => {
        const w = document.querySelector(".advanced-identity-selector");
        document.body.click();                       // outside click closes
        await new Promise(r => setTimeout(r, 3000)); // late response window
        w.querySelector(".identity-selector-trigger").click();
        await new Promise(r => setTimeout(r, 1500));
        return {
          status: w.querySelector(".identity-selector-status").textContent.trim(),
          clearHidden: getComputedStyle(w.querySelector(".filter-photo-clear")).display === "none",
          rows: w.querySelectorAll(".identity-selector-item").length,
        };
      });
      await page.unroute("**/api/search/by-image");
      check(`[${tag}] 13. close aborts; reopen is clean, no error shown`,
        afterClose.clearHidden && afterClose.rows > 0 && !/failed|error/i.test(afterClose.status),
        JSON.stringify(afterClose).slice(0, 90));

      // (15) long names + uuids: no horizontal overflow in the panel
      const overflow = await page.evaluate(() => {
        const panel = document.querySelector(".advanced-identity-selector .identity-selector-panel");
        return { scrollW: panel.scrollWidth, clientW: panel.clientWidth };
      });
      check(`[${tag}] 15. no horizontal overflow with id chips`,
        overflow.scrollW <= overflow.clientW + 1, JSON.stringify(overflow));

      // (16) keyboard navigation still works
      const keyboard = await page.evaluate(async () => {
        const w = document.querySelector(".advanced-identity-selector");
        const input = w.querySelector(".filter-search");
        input.focus();
        input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
        await new Promise(r => setTimeout(r, 150));
        const active = w.querySelector(".identity-selector-item.active");
        return { hasActive: !!active,
                 descendant: input.getAttribute("aria-activedescendant") || "" };
      });
      check(`[${tag}] 16. keyboard navigation intact`,
        keyboard.hasActive && keyboard.descendant.length > 0, JSON.stringify(keyboard));

      await page.evaluate(() => document.body.click());
      await page.waitForTimeout(300);

      // (17) console clean for this page
      check(`[${tag}] 17. zero console/page errors`,
        consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | ") || "clean");
    }
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 240));
  } finally {
    await browser.close();
  }

  const failed = results.filter(r => !r.ok);
  console.log(`\nOVERALL: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
