/**
 * ML-Ops guided workflow/help/accessibility probe.
 *   node scripts/dev/mlops_help_probe.js [--base http://localhost]
 * Walks every lifecycle workspace, verifies the run/verify/recover guidance,
 * opens every section's help modal, checks accessible control descriptions,
 * validates the shared confirmation without executing it, and checks the call
 * log. Read-only except the application's normal GET sequence.
 */
const PW_CORE = process.env.PW_CORE || "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW_CORE);
const arg = (n, d) => { const i = process.argv.indexOf(n); return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : d; };
const BASE = arg("--base", "http://localhost");
const results = [];
const check = (name, ok, detail) => { results.push({ name, ok }); console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };
(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newContext({ viewport: { width: 1600, height: 1000 } }).then(c => c.newPage());
  const errors = [];
  page.on("pageerror", e => errors.push(String(e).slice(0, 160)));
  page.on("console", m => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text().slice(0, 160)); });
  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.fill("#username", process.env.SMOKE_USER || "admin"); await page.fill("#password", process.env.SMOKE_PASS || "admin123");
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);
    // let the landing page's navbar finish its auth fetches before leaving it
    // (navigating mid-fetch logs a spurious "[Navbar] Failed to get privileges")
    await page.waitForLoadState("networkidle");
    await page.goto(`${BASE}/admin/ml-ops`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.querySelectorAll(".mlops-help-btn").length >= 13, { timeout: 30000 });
    await page.waitForFunction(() => /request id|No calls|Failed/i.test(document.getElementById("calls-body").textContent) || document.querySelectorAll("#calls-body .mlops-call-row").length > 0, { timeout: 30000 });

    const helpCount = await page.evaluate(() => document.querySelectorAll(".mlops-card[data-help] .mlops-help-btn").length);
    check("every card has a visible Guide control", helpCount >= 13, `${helpCount} buttons`);

    const workspaces = await page.evaluate(async () => {
      const out = [];
      for (const button of [...document.querySelectorAll("[data-mlops-view]")]) {
        button.click();
        await new Promise(r => setTimeout(r, 40));
        const name = button.dataset.mlopsView;
        out.push({
          name,
          active: button.getAttribute("aria-pressed") === "true",
          current: document.querySelector(`.mlops-lifecycle [data-open-mlops-view="${name}"]`).getAttribute("aria-current") === "step",
          visibleCards: [...document.querySelectorAll(`[data-mlops-panel="${name}"]`)].filter(card => !card.hidden).length,
          run: document.querySelectorAll("#mlops-runbook-run li").length,
          verify: document.querySelectorAll("#mlops-runbook-verify li").length,
          recover: document.querySelectorAll("#mlops-runbook-recover li").length,
          purpose: document.getElementById("mlops-runbook-purpose").textContent.length,
          status: document.getElementById("mlops-runbook-status").textContent
        });
      }
      return out;
    });
    check("all five workspaces expose purpose/run/verify/recover guidance",
      workspaces.length === 5 && workspaces.every(w => w.active && w.current && w.visibleCards > 0 && w.run > 0 && w.verify > 0 && w.recover > 0 && w.purpose > 30 && w.status),
      workspaces.map(w => `${w.name}:${w.visibleCards}`).join(" "));
    check("workspace navigation leaves a clean URL", !(await page.evaluate(() => location.hash)), await page.url());

    await page.click('[data-mlops-view="overview"]');
    await page.click("#pause-ml-btn");
    await page.waitForFunction(() => !document.getElementById("registry-action-panel").hidden);
    const confirmOpen = await page.evaluate(() => {
      const modal = document.getElementById("registry-action-panel");
      return getComputedStyle(modal).display !== "none" && modal.contains(document.activeElement)
        && modal.getAttribute("role") === "alertdialog";
    });
    await page.fill("#registry-action-reason", "x");
    await page.click("#registry-action-confirm");
    const localValidation = await page.locator("#registry-action-note").textContent();
    check("shared lifecycle confirmation traps focus and validates beside the input",
      confirmOpen && /at least 3 characters/i.test(localValidation));
    await page.keyboard.press("Escape");
    check("Escape closes confirmation", await page.evaluate(() => document.getElementById("registry-action-panel").hidden));

    // open each help modal, verify content sections, close with Escape
    const opened = await page.evaluate(async () => {
      const out = [];
      for (const btn of [...document.querySelectorAll(".mlops-help-btn")]) {
        btn.click();
        await new Promise(r => setTimeout(r, 150));
        const m = document.getElementById("mlops-help-modal");
        const visible = getComputedStyle(m).display !== "none" && !m.hidden;
        const text = document.getElementById("mlops-help-body").textContent;
        const title = document.getElementById("mlops-help-title").textContent;
        out.push({ key: btn.dataset.helpKey, visible, title, hasWhat: /What it is/.test(text), hasRead: /How to read it/.test(text), focusInside: m.contains(document.activeElement) });
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        await new Promise(r => setTimeout(r, 150));
        out[out.length - 1].closed = getComputedStyle(m).display === "none";
      }
      return out;
    });
    const bad = opened.filter(o => !(o.visible && o.hasWhat && o.hasRead && o.closed && o.title));
    check("each help modal opens with title/what/how-to-read, focus inside, and closes on Escape", bad.length === 0, bad.length ? JSON.stringify(bad[0]) : `${opened.length} sections ok`);

    const tips = await page.evaluate(() => {
      const ids = ["start-training-btn", "build-dataset-btn", "dataset-sampling-policy", "shadow-evidence-btn", "pause-ml-btn", "calls-errors-only"];
      return ids.map(id => ({ id, title: (document.getElementById(id) || {}).title || "" }));
    });
    check("controls carry tooltips", tips.every(t => t.title.length > 20), tips.map(t => t.id + ":" + (t.title ? "ok" : "MISSING")).join(" "));
    const accessibleTips = await page.evaluate(() => ["start-training-btn", "run-drift-btn", "pause-ml-btn"].every(id => {
      const node = document.getElementById(id); const described = node && node.getAttribute("aria-describedby");
      return described && document.getElementById(described);
    }));
    check("tooltips also have screen-reader descriptions", accessibleTips);

    const calls = await page.evaluate(() => {
      const rows = [...document.querySelectorAll("#calls-body .mlops-call-row:not(.mlops-call-header)")];
      return { n: rows.length, first: rows[0] ? rows[0].textContent : "", withRid: rows.filter(r => r.querySelector("code") && r.querySelector("code").textContent.length >= 8).length };
    });
    check("recent calls card lists calls with request ids", calls.n > 0 && calls.withRid === calls.n, `${calls.n} rows; first: ${calls.first.slice(0, 100)}`);
    await page.click('[data-mlops-view="audit"]');
    await page.click("#calls-errors-only");
    await page.waitForFunction(() => {
      const rows = [...document.querySelectorAll("#calls-body .mlops-call-row:not(.mlops-call-header)")];
      return (rows.length === 0 && /No calls/.test(document.getElementById("calls-body").textContent)) || (rows.length > 0 && rows.every(r => r.classList.contains("call-error")));
    }, { timeout: 15000 });
    const errRows = await page.evaluate(() => [...document.querySelectorAll("#calls-body .mlops-call-row:not(.mlops-call-header)")].map(r => r.classList.contains("call-error")));
    check("errors-only filter shows only error rows", errRows.every(Boolean), `${errRows.length} row(s)`);

    const strip = await page.evaluate(() => {
      const src = [...document.scripts].map(s => s.src).find(s => /admin-ml-ops\.js/.test(s));
      return !!src;
    });
    check("page script loaded", strip);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    check("no horizontal overflow at 1600px", !overflow);
    await page.setViewportSize({ width: 390, height: 844 });
    const mobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    check("no horizontal overflow at 390px", !mobileOverflow);
    check("zero console/page errors", errors.length === 0, errors.slice(0, 2).join(" | ") || "clean");
  } catch (err) { check("probe completed", false, String(err).slice(0, 240)); }
  finally { await browser.close(); }
  const failed = results.filter(r => !r.ok);
  console.log(`\nOVERALL: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
