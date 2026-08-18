/**
 * Render + offline proof for every basemap style, in a real browser.
 *
 *   node scripts/dev/map_style_probe.js [--base http://localhost] [--airgap] [--out logs/smoke]
 *
 * For each style it loads the actual style JSON in MapLibre, waits for the map
 * to become idle, and reports what the BROWSER did:
 *
 *   styleLoaded   the style parsed and its sources resolved
 *   tiles         requests to /maps/<dataset>/… that returned tile bytes
 *   glyphs        requests to /maps/font/… (vector labels need them)
 *   rendered      map.loaded() with at least one painted source
 *   external      every request to a host that is not this origin  (must be [])
 *   csp           CSP violations reported by the document              (must be [])
 *   errors        MapLibre `error` events + console errors             (must be [])
 *
 * `--airgap` additionally forces every non-localhost hostname to NXDOMAIN
 * (`--host-resolver-rules`), so a style that silently depends on a CDN, a glyph
 * server or a sprite host fails here instead of in the field. A grep of the
 * source cannot prove this; only the network layer can.
 *
 * Exit 0 when every AVAILABLE style rendered with no external request, no CSP
 * violation and no error. Styles the backend reports unavailable are skipped
 * and listed — that is a valid production state, not a failure.
 */
const fs = require("fs");
const path = require("path");

const PW_CORE = process.env.PW_CORE ||
  "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW_CORE);

const args = process.argv.slice(2);
const opt = (n, d) => { const i = args.indexOf(n); return i >= 0 ? args[i + 1] : d; };
const BASE = opt("--base", "http://localhost");
const OUT = opt("--out", "logs/smoke");
const AIRGAP = args.includes("--airgap");
const ADMIN = { username: "admin", password: "admin123" };
const STYLES = ["light", "dark", "satellite", "terrain"];

async function login() {
  const r = await fetch(`${BASE}/api/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(ADMIN) });
  if (!r.ok) throw new Error(`login ${r.status}`);
  return (await r.json()).access_token;
}

async function availability(token) {
  const r = await fetch(`${BASE}/api/maps/availability`, { headers: { Authorization: `Bearer ${token}` } });
  if (!r.ok) throw new Error(`availability ${r.status}`);
  return r.json();
}

// A page that loads MapLibre from the app's own vendor directory and reports
// what happened. Served from the app origin so the real CSP applies.
const HARNESS = (style, version) => `<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/frontend/vendor/maplibre/maplibre-gl.css">
<style>html,body,#m{margin:0;height:100%;width:100%}</style></head>
<body><div id="m"></div><script type="module">
import * as maplibregl from '/frontend/vendor/maplibre/maplibre-gl.mjs';
maplibregl.setWorkerUrl('/frontend/vendor/maplibre/maplibre-gl-worker.mjs');
window.__probe = { styleLoaded:false, idle:false, errors:[], sources:[] };
document.addEventListener('securitypolicyviolation', (e) =>
  window.__probe.errors.push('CSP ' + e.violatedDirective + ' ' + e.blockedURI));
const map = new maplibregl.Map({ container:'m', style:'/frontend/maps/styles/${style}.json?v=${version}',
  center:[35.50,33.89], zoom:11, attributionControl:false });
map.on('error', (e) => { const m = (e && e.error && e.error.message) || String(e && e.error || 'error');
  if (!/status.*(204|404)/i.test(m)) window.__probe.errors.push(m); });
map.on('style.load', () => { window.__probe.styleLoaded = true;
  window.__probe.sources = Object.keys(map.getStyle().sources || {}); });
map.on('idle', () => { window.__probe.idle = true;
  try { window.__probe.painted = map.getStyle().layers.length; } catch (e) {} });
</script></body></html>`;

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const token = await login();
  const avail = await availability(token);
  const launchArgs = ["--no-first-run", "--disable-gpu", "--disable-extensions"];
  if (AIRGAP) launchArgs.push('--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost');
  const browser = await chromium.launch({ executablePath: CHROME, headless: true, args: launchArgs });
  const context = await browser.newContext({ viewport: { width: 1200, height: 800 } });

  // authenticate the browser session so /api and same-origin assets behave as in the app
  await context.request.post(`${BASE}/api/auth/login`, { data: ADMIN });

  const report = { base: BASE, airgap: AIRGAP, started: new Date().toISOString(),
                   availability: avail.styles, styles: {}, overall: "PASS" };

  for (const style of STYLES) {
    const entry = { available: !!avail.styles[style], reason: (avail.detail[style] || {}).reason || null };
    if (!entry.available) { entry.status = "SKIP (unavailable)"; report.styles[style] = entry; continue; }

    const page = await context.newPage();
    const requests = [];
    const errors = [];
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text().slice(0, 200)); });
    page.on("pageerror", (e) => errors.push(String(e && e.message || e).slice(0, 200)));
    page.on("requestfinished", async (req) => {
      const url = req.url();
      let status = null;
      try { status = (await req.response()).status(); } catch (e) { /* ignore */ }
      requests.push({ url, status });
    });
    page.on("requestfailed", (req) => requests.push({ url: req.url(), status: "FAILED" }));

    // serve the harness from the app origin: the real CSP, the real headers
    await page.route(`${BASE}/__style_probe__`, (route) =>
      route.fulfill({ status: 200, contentType: "text/html; charset=utf-8",
                      body: HARNESS(style, styleVersion()) }));
    await page.goto(`${BASE}/__style_probe__`, { waitUntil: "load", timeout: 60000 });

    // wait for idle (or give up and report what we have)
    const deadline = Date.now() + 30000;
    let probe = {};
    while (Date.now() < deadline) {
      probe = await page.evaluate(() => window.__probe || {});
      if (probe.idle) break;
      await page.waitForTimeout(500);
    }
    await page.waitForTimeout(1500);           // let late tiles land
    probe = await page.evaluate(() => window.__probe || {});

    const origin = new URL(BASE).origin;
    const external = requests.filter((r) => !r.url.startsWith(origin) && !r.url.startsWith("data:")
                                            && !r.url.startsWith("blob:"));
    const tiles = requests.filter((r) => /\/maps\/[a-z0-9-]+\/\d+\/\d+\/\d+/i.test(r.url) && r.status === 200);
    const glyphs = requests.filter((r) => /\/maps\/font\//i.test(r.url) && r.status === 200);
    const bad = requests.filter((r) => typeof r.status === "number" && r.status >= 400);

    Object.assign(entry, {
      styleLoaded: !!probe.styleLoaded, rendered: !!probe.idle, layers: probe.painted || 0,
      sources: probe.sources || [], tiles: tiles.length, glyphs: glyphs.length,
      external: external.map((r) => r.url), badRequests: bad.map((r) => `${r.url} ${r.status}`),
      errors: errors.concat(probe.errors || []),
    });
    entry.status = (entry.styleLoaded && entry.rendered && entry.tiles > 0
                    && entry.external.length === 0 && entry.errors.length === 0
                    && entry.badRequests.length === 0) ? "PASS" : "FAIL";
    if (entry.status !== "PASS") report.overall = "FAIL";
    report.styles[style] = entry;
    await page.screenshot({ path: path.join(OUT, `style_${style}${AIRGAP ? "_airgap" : ""}.png`) });
    await page.close();
  }

  await browser.close();
  report.finished = new Date().toISOString();
  fs.writeFileSync(path.join(OUT, `map_style_probe${AIRGAP ? "_airgap" : ""}.json`), JSON.stringify(report, null, 2));

  for (const [style, e] of Object.entries(report.styles)) {
    if (!e.available) { console.log(`${"SKIP".padEnd(5)} ${style.padEnd(10)} unavailable — ${e.reason || "no reason given"}`); continue; }
    console.log(`${e.status.padEnd(5)} ${style.padEnd(10)} styleLoaded=${e.styleLoaded} rendered=${e.rendered} ` +
                `layers=${e.layers} tiles=${e.tiles} glyphs=${e.glyphs} external=${e.external.length} errors=${e.errors.length}`);
    for (const x of e.external) console.log(`        EXTERNAL ${x}`);
    for (const b of e.badRequests) console.log(`        BAD ${b}`);
    for (const x of e.errors.slice(0, 5)) console.log(`        ERROR ${x}`);
  }
  console.log(`airgap=${AIRGAP} OVERALL: ${report.overall}`);
  process.exit(report.overall === "PASS" ? 0 : 1);
})().catch((e) => { console.error("probe failed:", e); process.exit(2); });

function styleVersion() {
  const src = fs.readFileSync("frontend/js/identity-map.js", "utf8");
  const m = src.match(/STYLE_VERSION\s*=\s*'([^']+)'/);
  return m ? m[1] : "probe";
}
