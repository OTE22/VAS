/**
 * Security Intelligence redesign probe.
 *
 *   node scripts/dev/security_intelligence_design_probe.js [--base http://localhost] [--viewport WxH]
 *
 * The page was redesigned for fit: it previously set min-height:100vh plus
 * padding-top:70px inside an already-100vh app shell, scrolled
 * .security-content at calc(100vh - 180px) and scrolled #tab-advanced again
 * at calc(100vh - 230px), and hard-coded the map to 600px inline.
 *
 * So this measures what the user can actually reach, per tab:
 *   - the page does not overflow its viewport horizontally
 *   - main fits the shell, so nothing is stranded under body{overflow:hidden}
 *   - .security-content is the single scroller and its LAST element is
 *     reachable by scrolling
 *   - the map fits the viewport
 * and it re-proves the JS contract the redesign had to preserve: every id
 * still exists, tab switching still activates the right panel, and every
 * identity picker still mounts with a usable search box.
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

const TABS = ["network", "patterns", "anomalies", "threats", "advanced", "map"];

// Every id admin-security-intelligence.js addresses. The redesign rewrote the
// markup, so losing one would silently break a feature.
const REQUIRED_IDS = [
  "anomalies-container", "anomalies-detect-btn", "anomaly-days-back", "anomaly-identity-id",
  "central-nodes-list", "clusters-list", "correlation-calc-btn", "correlation-days-back",
  "correlation-identity-a", "correlation-identity-b", "correlation-results",
  "feature-help-btn", "feature-status-btn", "learn-all-thresholds-btn",
  "map-cluster-markers", "map-date", "map-days-back", "map-detect-patterns",
  "map-enable-security", "map-identity-id", "map-include-popups", "map-load-btn",
  "map-show-heatmap", "map-show-routes", "map-style-select",
  "network-analyze-btn", "network-days-back", "network-graph", "network-identity-ids",
  "network-min-connections", "patterns-container", "patterns-days-back",
  "patterns-detect-btn", "patterns-min-group", "security-map", "security-tabs",
  "stat-central", "stat-clusters", "stat-edges", "stat-nodes",
  "threat-assess-btn", "threat-container", "threat-identity-id",
  "threshold-learn-btn", "threshold-pipeline-ids", "threshold-results",
  "trajectory-current-camera", "trajectory-identity-id", "trajectory-predict-btn",
  "trajectory-results", "trajectory-top-k",
];

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newContext({ viewport: { width: VW, height: VH } }).then((c) => c.newPage());
  const consoleErrors = [];
  page.on("pageerror", (e) => consoleErrors.push(String(e).slice(0, 160)));
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 160)); });

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER); await page.fill("#password", PASS);
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);

    // Let the post-login landing page finish its own navbar privileges fetch
    // before navigating away. Leaving immediately aborts that request, the
    // navbar logs "[Navbar] Failed to get privileges", and the error lands in
    // this run and gets blamed on the page under test.
    await page.waitForTimeout(2500);
    consoleErrors.length = 0;

    await page.goto(`${BASE}/admin/security-intelligence`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(3500);

    // ---- JS contract: every addressed id survived the rewrite
    const missing = await page.evaluate(
      (ids) => ids.filter((id) => !document.getElementById(id)), REQUIRED_IDS);
    check("every id the JS addresses still exists", missing.length === 0,
      missing.length ? `missing: ${missing.join(", ")}` : `${REQUIRED_IDS.length} ids present`);

    // ---- shell fit
    const shell = await page.evaluate(() => {
      const main = document.querySelector(".security-intelligence-main");
      const content = document.querySelector(".security-content");
      const nav = document.querySelector(".military-navbar");
      const mr = main.getBoundingClientRect();
      return {
        docScrollW: document.documentElement.scrollWidth,
        innerW: window.innerWidth,
        innerH: window.innerHeight,
        bodyScrollH: document.body.scrollHeight,
        mainBottom: Math.round(mr.bottom),
        mainScrollW: main.scrollWidth,
        mainClientW: main.clientWidth,
        contentScrolls: content.scrollHeight > content.clientHeight,
        navScrollW: nav ? nav.scrollWidth : 0,
        navClientW: nav ? nav.clientWidth : 0,
      };
    });
    // The navbar is a known, pre-existing horizontal overflow that this
    // redesign did not touch, so measure MAIN rather than blaming the page.
    check("main does not overflow horizontally",
      shell.mainScrollW <= shell.mainClientW + 1,
      `scrollW=${shell.mainScrollW} clientW=${shell.mainClientW}`);
    check("page fits the app shell vertically",
      shell.bodyScrollH <= shell.innerH + 1 && shell.mainBottom <= shell.innerH + 1,
      `bodyScrollH=${shell.bodyScrollH} mainBottom=${shell.mainBottom} viewportH=${shell.innerH}`);
    if (shell.navScrollW > shell.navClientW + 1) {
      console.log(`  note  navbar overflows by ${shell.navScrollW - shell.navClientW}px (pre-existing, not this page)`);
    }

    // ---- per tab
    for (const tab of TABS) {
      await page.click(`.sec-tab[data-tab="${tab}"]`);
      await page.waitForTimeout(500);

      const r = await page.evaluate((t) => {
        const btn = document.querySelector(`.sec-tab[data-tab="${t}"]`);
        const panel = document.getElementById(`tab-${t}`);
        const content = document.querySelector(".security-content");
        const activePanels = [...document.querySelectorAll(".sec-tab-content.active")].map((p) => p.id);

        // widest element inside the panel, to find horizontal overflow
        let worst = null;
        panel.querySelectorAll("*").forEach((el) => {
          if (!el.getClientRects().length) return;
          const rc = el.getBoundingClientRect();
          const over = Math.round(rc.right - window.innerWidth);
          if (over > 1 && (!worst || over > worst.over)) {
            worst = { over, tag: el.tagName.toLowerCase(), cls: (el.className || "").toString().slice(0, 40) };
          }
        });

        return {
          tabActive: btn.classList.contains("active"),
          panelActive: panel.classList.contains("active"),
          activePanels,
          contentScrollH: content.scrollHeight,
          contentClientH: content.clientHeight,
          worst,
          // nested scrollers inside this panel: a second scroller is what made
          // the old #tab-advanced unreachable
          nested: [...panel.querySelectorAll("*")].filter((el) => {
            const st = getComputedStyle(el);
            return /auto|scroll/.test(st.overflowY) && el.scrollHeight > el.clientHeight + 1;
          }).map((el) => (el.id || el.className || el.tagName).toString().slice(0, 40)),
        };
      }, tab);

      check(`[${tab}] tab switching activates exactly this panel`,
        r.tabActive && r.panelActive && r.activePanels.length === 1 && r.activePanels[0] === `tab-${tab}`,
        `active=[${r.activePanels.join(",")}]`);
      check(`[${tab}] nothing overflows the viewport horizontally`,
        !r.worst, r.worst ? `${r.worst.tag}.${r.worst.cls} by ${r.worst.over}px` : "clean");

      // the last element of the panel must be reachable by scrolling
      const reach = await page.evaluate((t) => {
        const content = document.querySelector(".security-content");
        const panel = document.getElementById(`tab-${t}`);
        content.scrollTop = content.scrollHeight;
        const kids = [...panel.children];
        const last = kids.length ? kids[kids.length - 1] : panel;
        const deepest = last.querySelectorAll("*");
        const el = deepest.length ? deepest[deepest.length - 1] : last;
        const rc = el.getBoundingClientRect();
        const cr = content.getBoundingClientRect();
        return {
          visible: rc.bottom <= cr.bottom + 2 && rc.top < cr.bottom,
          elBottom: Math.round(rc.bottom), contentBottom: Math.round(cr.bottom),
          scrolled: content.scrollTop,
        };
      }, tab);
      check(`[${tab}] bottom of the tab is reachable`,
        reach.visible,
        `last element bottom=${reach.elBottom} scroller bottom=${reach.contentBottom} (scrollTop=${Math.round(reach.scrolled)})`);

      if (r.nested.length) console.log(`    note  nested scrollers: ${r.nested.join(" | ")}`);
      await page.evaluate(() => { document.querySelector(".security-content").scrollTop = 0; });
    }

    // ---- map fits
    await page.click('.sec-tab[data-tab="map"]');
    await page.waitForTimeout(400);
    const map = await page.evaluate(() => {
      const m = document.getElementById("security-map");
      const rc = m.getBoundingClientRect();
      return { h: Math.round(rc.height), inline: m.getAttribute("style") || "", vh: window.innerHeight };
    });
    check("map height is viewport-relative, not a hard-coded 600px",
      map.h <= map.vh && !/height\s*:/.test(map.inline),
      `height=${map.h}px viewport=${map.vh}px inlineStyle=${map.inline ? "present" : "none"}`);

    // ---- identity pickers still mount and are usable
    const pickers = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll(".advanced-identity-selector").forEach((w) => {
        out.push({ id: w.dataset.originalId || "?", hasTrigger: !!w.querySelector(".identity-selector-trigger") });
      });
      return out;
    });
    check("all 8 identity pickers mounted", pickers.length === 8 && pickers.every((p) => p.hasTrigger),
      `${pickers.length} mounted: ${pickers.map((p) => p.id).join(", ")}`);

    // open one on the visible map tab and measure its search box
    const picker = await page.evaluate(async () => {
      const w = document.querySelector("#tab-map .advanced-identity-selector");
      if (!w) return { none: true };
      const trigger = w.querySelector(".identity-selector-trigger");
      trigger.scrollIntoView({ block: "center" });
      await new Promise((r) => setTimeout(r, 250));
      trigger.click();
      await new Promise((r) => setTimeout(r, 900));
      const panel = w.querySelector(".identity-selector-panel");
      const search = panel.querySelector(".filter-search");
      const pr = panel.getBoundingClientRect();
      const sr = search.getBoundingClientRect();
      return {
        searchW: Math.round(sr.width),
        panelInside: pr.right <= window.innerWidth + 1 && pr.left >= -1,
        items: panel.querySelectorAll(".identity-selector-item").length,
      };
    });
    check("identity picker search box still wide enough to type in",
      !picker.none && picker.searchW >= 200 && picker.panelInside,
      `search=${picker.searchW}px insideViewport=${picker.panelInside} items=${picker.items}`);

    check("no console/page errors", consoleErrors.length === 0,
      consoleErrors.length ? consoleErrors.slice(0, 3).join(" | ") : "clean");
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 240));
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\nOVERALL ${VW}x${VH}: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
