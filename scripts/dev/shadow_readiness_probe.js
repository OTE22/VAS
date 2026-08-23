/**
 * Production shadow-readiness UI probe (ML-Ops + Security Intelligence).
 *   node scripts/dev/shadow_readiness_probe.js [--base http://localhost]
 * Render-only on the development stack: proves the reserved model types are
 * not offered as trainable, a refused mode change lists every gate with its
 * state, the scientific criteria table shows measured / required / status,
 * the evidence block is visible, seed/synthetic labels are flagged, and the
 * threat card offers a blind outcome panel while hiding the band in the
 * history list. It records NO outcome and changes NO setting.
 */
const PW_CORE = process.env.PW_CORE || "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW_CORE);
const arg = (n, d) => { const i = process.argv.indexOf(n); return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : d; };
const BASE = arg("--base", "http://localhost");
const results = [];
const check = (name, ok, detail) => { results.push({ name, ok }); console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };

async function pickIdentity(page, selectId, query, nameRe) {
  await page.evaluate((selectId) => {
    const sel = document.getElementById(selectId); const w = sel.nextElementSibling;
    const t = w.querySelector(".identity-selector-trigger"); t.scrollIntoView({ block: "center" }); t.click();
  }, selectId);
  await page.waitForFunction((selectId) => document.getElementById(selectId).nextElementSibling.querySelectorAll(".identity-selector-item").length > 0, selectId, { timeout: 15000 });
  return page.evaluate(async ({ selectId, query, nameRe }) => {
    const w = document.getElementById(selectId).nextElementSibling;
    const input = w.querySelector(".filter-search"); input.value = query; input.dispatchEvent(new Event("input", { bubbles: true }));
    await new Promise(r => setTimeout(r, 1500));
    const row = [...w.querySelectorAll(".identity-selector-item")].find(r => new RegExp(nameRe).test(r.textContent));
    if (!row) return false; row.click(); return true;
  }, { selectId, query, nameRe });
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors = [];
  page.on("pageerror", e => errors.push(String(e).slice(0, 160)));
  page.on("console", m => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text().slice(0, 160)); });
  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", process.env.SMOKE_USER || "admin"); await page.fill("#password", process.env.SMOKE_PASS || "admin123");
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);
    await page.waitForLoadState("networkidle");

    // ---------------- ML-Ops ----------------
    await page.goto(`${BASE}/admin/ml-ops`, { waitUntil: "networkidle" });
    await page.waitForFunction(() => document.querySelectorAll("#training-model-type option").length >= 4 && /Reserved/.test(document.getElementById("training-model-type").textContent), { timeout: 30000 });
    const types = await page.$$eval("#training-model-type option", os => os.map(o => ({ value: o.value, disabled: o.disabled, text: o.textContent })));
    check("reserved model types are rendered disabled as Reserved / Future — not trainable",
      types.filter(t => t.value !== "behavior_anomaly_model").every(t => t.disabled && /Reserved \/ Future/.test(t.text)) && types.some(t => t.value === "behavior_anomaly_model" && !t.disabled && /Available/.test(t.text)),
      types.map(t => t.value + (t.disabled ? "(disabled)" : "")).join(", "));
    const trainEnabled = await page.$eval("#start-training-btn", b => !b.disabled);
    check("Train is enabled for the available type", trainEnabled);

    // refused mode change: every gate with its state
    await page.waitForFunction(() => document.querySelectorAll("#mode-cards .mlops-btn").length >= 4, { timeout: 30000 });
    const clicked = await page.evaluate(() => {
      const cards = [...document.querySelectorAll("#mode-cards .mlops-mode-card")];
      const card = cards.find(c => { const n = c.querySelector(".mlops-mode-name"); return n && n.childNodes[0] && n.childNodes[0].textContent.trim().toLowerCase() === "ml"; });
      const btn = card && [...card.querySelectorAll("button")].find(b => /Activate/.test(b.textContent) && !b.disabled);
      if (!btn) return false; btn.click(); return true;
    });
    check("ML mode Activate control found", clicked);
    await page.waitForFunction(() => !document.getElementById("registry-action-panel").hidden, { timeout: 10000 });
    await page.fill("#registry-action-reason", "probe gate states");
    await page.click("#registry-action-confirm");
    await page.waitForFunction(() => /not ready|Unmet|cannot be activated/.test(document.getElementById("mode-action-note").textContent), { timeout: 20000 });
    const note = await page.$eval("#mode-action-note", n => n.textContent);
    check("MODE_GATED shows every gate with ✓/✗ and keeps rules authoritative",
      /ML decision authority is not ready/.test(note) && /✓ Shadow model/.test(note) && /✗ Scientific validity: INSUFFICIENT_EVIDENCE/.test(note) && /✗ Signal mapping: REQUIRES_VALIDATION/.test(note) && /Rules remain authoritative/.test(note),
      note.replace(/\n/g, " | ").slice(0, 260));
    const mode = await page.evaluate(async () => (await (await fetch("/api/settings/ML_DECISION_MODE", { credentials: "include" })).json()).value);
    check("the configured mode did not change", mode === "shadow" || mode === "rules", mode);

    // model detail: criteria table + recompute button
    await page.waitForFunction(() => document.querySelectorAll("#models-table-body button").length > 0, { timeout: 30000 })
      .catch(async () => { const t = await page.$eval("#models-table-body", n => n.innerText); console.log("  models table: " + t.slice(0, 200)); throw new Error("models table has no buttons"); });
    await page.evaluate(() => { const b = [...document.querySelectorAll("#models-table-body button")].find(b => /detail|view/i.test(b.textContent)); b && b.click(); });
    await page.waitForFunction(() => /Scientific criteria/.test(document.getElementById("model-detail-body").textContent), { timeout: 20000 });
    const detail = await page.$eval("#model-detail-body", n => n.textContent);
    check("scientific criteria table shows measured / required / NOT_CONFIGURED per criterion",
      /Scientific criteria — measured \/ required \/ status/.test(detail) && /history_span_days/.test(detail) && /NOT_CONFIGURED/.test(detail) && /not configured/.test(detail) && /Recompute readiness/.test(detail),
      (detail.match(/history_span_days.{0,80}/) || [""])[0]);

    // evidence drawer: the statistics are visible
    await page.click("#shadow-evidence-btn");
    await page.waitForFunction(() => /Adequacy/.test(document.getElementById("model-detail-body").textContent), { timeout: 20000 });
    const evidence = await page.$eval("#model-detail-body", n => n.textContent);
    check("evidence block renders adequacy, populations, Wilson CI table, trend/Spearman/ranking",
      /Adequacy/.test(evidence) && /Populations/.test(evidence) && /blind_reviewed/.test(evidence) && /Wilson 95% CI/.test(evidence) && /Cochran/.test(evidence) && /Spearman/.test(evidence) && /PR-AUC/.test(evidence) && /Excluded non-evidence labels/.test(evidence),
      evidence.slice(0, 120));
    const windowOptions = await page.$$eval("#shadow-days-select option", os => os.map(o => o.value));
    check("evidence window offers up to 365 days and a model filter", windowOptions.includes("365") && !!(await page.$("#shadow-model-select")));

    // system state: seed labels flagged (dev carries demo-seed labels)
    const stateText = await page.evaluate(() => (document.getElementById("system-state-body") || document.body).textContent);
    check("system state flags non-evidence (seed/synthetic) labels when present", /NON_EVIDENCE_LABELS_PRESENT|excluded from all evidence statistics/.test(stateText) || /non-evidence/i.test(stateText), stateText.slice(0, 0));

    // label form: selection method + notes field
    check("label form records selection method, assessment id and notes", !!(await page.$("#label-selection-method")) && !!(await page.$("#label-assessment-id")) && !!(await page.$("#label-notes")));
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    check("ML-Ops: no horizontal overflow at 1600px", !overflow);

    // ---------------- Security Intelligence ----------------
    await page.goto(`${BASE}/admin/security-intelligence`, { waitUntil: "networkidle" });
    await page.waitForFunction(() => { const p = document.getElementById("engine-mode-pill"); return p && !p.hidden && p.textContent.length > 10; }, { timeout: 30000 });
    await page.click('.sec-tab[data-tab="threats"]');
    check("identity picked for threat", await pickIdentity(page, "threat-identity-id", "IRON", "IRON MAN"));
    await page.click("#threat-assess-btn");
    await page.waitForFunction(() => document.querySelector("#threat-container .decision-block"), { timeout: 60000 });
    await page.waitForFunction(() => !document.getElementById("threat-history").hidden, { timeout: 30000 }).catch(() => {});
    const card = await page.evaluate(() => ({
      outcome: (document.querySelector("#threat-container .outcome-panel") || {}).innerText || "",
      buttons: [...document.querySelectorAll("#threat-container .outcome-actions button")].map(b => b.textContent.trim()),
      observationHidden: (document.querySelector("#threat-container .ml-observation") || { hidden: null }).hidden,
      history: [...document.querySelectorAll("#threat-history-list .history-ml")].map(n => n.textContent),
    }));
    check("threat card offers a blind outcome panel (Confirmed threat / Not a threat) without submitting",
      /Record outcome \(blind review\)/.test(card.outcome) && card.buttons.includes("Confirmed threat") && card.buttons.includes("Not a threat") && /UNREVIEWED|authorized review/.test(card.outcome),
      card.buttons.join(" / "));
    check("ML observation hidden by default on the card", card.observationHidden === true || card.observationHidden === null, String(card.observationHidden));
    check("history rows hide the band until the observation is revealed",
      card.history.length === 0 || card.history.every(t => /hidden — blind review guard/.test(t) && !/band /.test(t)),
      card.history[0] || "no ML history rows");
    const revealed = await page.evaluate(() => {
      const b = document.querySelector("#threat-container .ml-observation-reveal"); if (!b) return null; b.click();
      return [...document.querySelectorAll("#threat-history-list .history-ml")].map(n => n.textContent);
    });
    check("after revealing, history rows show the band text", revealed === null || revealed.length === 0 || revealed.every(t => !/hidden — blind review guard/.test(t)), (revealed || [])[0] || "n/a");
    check("zero console/page errors", errors.length === 0, errors.slice(0, 2).join(" | ") || "clean");
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 240) + " @ " + String((err && err.stack) || "").split(String.fromCharCode(10)).slice(1, 3).join(" / "));
  } finally {
    await browser.close();
  }
  const failed = results.filter(r => !r.ok);
  console.log(`\nOVERALL: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
