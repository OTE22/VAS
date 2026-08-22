/**
 * Engine provenance probe (security-intelligence + intelligence pages).
 *   node scripts/dev/engine_provenance_probe.js [--base http://localhost]
 * Proves on the real pages: the pill reflects the CONFIGURED mode; a threat
 * assessment shows its own persisted provenance + ML observation; anomalies
 * and patterns carry backend engine badges; after switching the mode to
 * RULES the pill changes while a deduplicated assessment keeps its SHADOW
 * provenance. Restores the original mode. Mode switches go through the
 * settings API as an admin; the data is left as found apart from the
 * assessments such views always persist.
 */
const PW_CORE = process.env.PW_CORE || "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW_CORE);
const arg = (n, d) => { const i = process.argv.indexOf(n); return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : d; };
const BASE = arg("--base", "http://localhost");
const results = [];
const check = (name, ok, detail) => { results.push({ name, ok }); console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`); };

async function apiJson(page, method, path, body) {
  return page.evaluate(async ({ method, path, body }) => {
    const r = await fetch(path, { method, credentials: "include", headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" }, body: body ? JSON.stringify(body) : undefined });
    return { status: r.status, body: await r.json().catch(() => ({})) };
  }, { method, path, body });
}
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
  const page = await browser.newContext({ viewport: { width: 1600, height: 1000 } }).then(c => c.newPage());
  const errors = [];
  page.on("pageerror", e => errors.push(String(e).slice(0, 160)));
  page.on("console", m => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text().slice(0, 160)); });
  let originalMode = null;
  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", process.env.SMOKE_USER || "admin"); await page.fill("#password", process.env.SMOKE_PASS || "admin123");
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);
    await page.waitForLoadState("networkidle");
    const setting = await apiJson(page, "GET", "/api/settings/ML_DECISION_MODE");
    originalMode = String((setting.body && (setting.body.effective_value || setting.body.value)) || "rules").toLowerCase();
    if (originalMode !== "shadow") { await apiJson(page, "PUT", "/api/settings/ML_DECISION_MODE", { value: "shadow", change_reason: "probe" }); }

    await page.goto(`${BASE}/admin/security-intelligence`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => { const p = document.getElementById("engine-mode-pill"); return p && !p.hidden && p.textContent.length > 10; }, { timeout: 30000 });
    const pill = await page.evaluate(() => document.getElementById("engine-mode-pill").textContent);
    check("pill shows the configured mode (SHADOW), the live signal source and final scorer", /SHADOW/.test(pill) && /Statistical rules/.test(pill) && /risk-engine-v1/.test(pill) && /ML observer|Shadow model/.test(pill), pill.slice(0, 140));

    await page.click('.sec-tab[data-tab="threats"]');
    check("identity picked for threat", await pickIdentity(page, "threat-identity-id", "IRON", "IRON MAN"));
    await page.click("#threat-assess-btn");
    await page.waitForFunction(() => document.querySelector("#threat-container .decision-block"), { timeout: 60000 });
    await page.waitForFunction(() => !document.getElementById("threat-history").hidden, { timeout: 30000 }).catch(() => {});
    const threat = await page.evaluate(() => ({
      decision: document.querySelector("#threat-container .decision-block").innerText,
      observation: (document.querySelector("#threat-container .ml-observation") || {}).innerText || "",
      history: [...document.querySelectorAll("#threat-history-list .threat-history-row")].map(r => r.innerText),
    }));
    check("decision block: requested SHADOW, executed SHADOW, anomaly signal = statistical rules, final scoring risk-engine-v1",
      /Requested mode\s+SHADOW/.test(threat.decision) && /Executed mode\s+SHADOW/.test(threat.decision) && /Anomaly signal\s+Statistical rules/.test(threat.decision) && /risk-engine-v1/.test(threat.decision) && /observational/.test(threat.decision),
      threat.decision.replace(/\n/g, " | ").slice(0, 200));
    check("ML observation panel shown separately, labelled not used for the live decision, no arithmetic",
      /ML shadow signal/.test(threat.observation) && /not used for the live decision|Recorded for comparison only/.test(threat.observation) && !/delta|difference/i.test(threat.observation),
      threat.observation.replace(/\n/g, " | ").slice(0, 200));
    check("history list renders persisted provenance per row", threat.history.length > 0 && threat.history.every(r => /requested .* · executed /.test(r)),
      `${threat.history.length} row(s); first: ${(threat.history[0] || "").replace(/\n/g, " | ").slice(0, 160)}`);

    await page.click('.sec-tab[data-tab="anomalies"]');
    check("identity picked for anomalies", await pickIdentity(page, "anomaly-identity-id", "IRON", "IRON MAN"));
    await page.click("#anomalies-detect-btn");
    await page.waitForFunction(() => document.querySelector("#anomalies-container .engine-badge"), { timeout: 60000 });
    const aBadge = await page.evaluate(() => document.querySelector("#anomalies-container .engine-badge").textContent);
    check("anomalies badge: Rules engine · anomaly-context-v3 · ML does not take part", /Rules engine/.test(aBadge) && /anomaly-context-v3/.test(aBadge) && /ML does not take part/.test(aBadge), aBadge);
    await page.click('.sec-tab[data-tab="patterns"]'); await page.click("#patterns-detect-btn");
    await page.waitForFunction(() => document.querySelector("#patterns-container .engine-badge"), { timeout: 90000 });
    const pBadge = await page.evaluate(() => document.querySelector("#patterns-container .engine-badge").textContent);
    check("patterns badge: Rules engine · patterns-v3", /Rules engine/.test(pBadge) && /patterns-v3/.test(pBadge), pBadge);

    const sw = await apiJson(page, "PUT", "/api/settings/ML_DECISION_MODE", { value: "rules", change_reason: "probe rules" });
    check("mode switched to RULES via settings", sw.status === 200, String(sw.status));
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => { const p = document.getElementById("engine-mode-pill"); return p && !p.hidden && /RULES/.test(p.textContent); }, { timeout: 30000 });
    await page.click('.sec-tab[data-tab="threats"]');
    check("identity re-picked", await pickIdentity(page, "threat-identity-id", "IRON", "IRON MAN"));
    await page.click("#threat-assess-btn");
    await page.waitForFunction(() => document.querySelector("#threat-container .decision-block"), { timeout: 60000 });
    const after = await page.evaluate(() => ({ pill: document.getElementById("engine-mode-pill").textContent, decision: document.querySelector("#threat-container .decision-block").innerText }));
    const keptShadow = /Executed mode\s+SHADOW/.test(after.decision);
    const freshRules = /Requested mode\s+RULES/.test(after.decision) && /Executed mode\s+RULES/.test(after.decision);
    check("pill now says RULES", /^RULES/.test(after.pill.trim()), after.pill.slice(0, 80));
    check("assessment provenance is its OWN (deduplicated row keeps SHADOW, or a fresh row says RULES) — never relabelled by the pill", keptShadow || freshRules, (keptShadow ? "kept SHADOW (deduplicated row)" : "fresh RULES row") + " :: " + after.decision.replace(/\n/g, " | ").slice(0, 120));

    await page.goto(`${BASE}/admin/intelligence`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => { const p = document.getElementById("engine-mode-pill"); return p && !p.hidden && /RULES/.test(p.textContent); }, { timeout: 30000 });
    const ipill = await page.evaluate(() => document.getElementById("engine-mode-pill").textContent);
    check("intelligence page pill present and consistent", /RULES/.test(ipill) && /risk-engine-v1/.test(ipill), ipill.slice(0, 100));
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    check("no horizontal overflow at 1600px", !overflow);
    check("zero console/page errors", errors.length === 0, errors.slice(0, 2).join(" | ") || "clean");
  } catch (err) { check("probe completed", false, String(err).slice(0, 240)); }
  finally {
    try { if (originalMode) await apiJson(page, "PUT", "/api/settings/ML_DECISION_MODE", { value: originalMode, change_reason: "probe restore" }); } catch (_) {}
    await browser.close();
  }
  const failed = results.filter(r => !r.ok);
  console.log(`\nOVERALL: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
