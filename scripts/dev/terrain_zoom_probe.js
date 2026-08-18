/**
 * Loads the terrain style in headless Chrome, zooms in past the DEM's max zoom
 * and out past its min, and reports every /maps/lebanon-dem/{z}/... request by
 * zoom with its HTTP status. With the corrected style there must be no request
 * outside z6-12 and no 404.
 */
const PW = process.env.PW_CORE || "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW);
const BASE = process.env.BASE || "http://localhost";

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ["--no-first-run", "--disable-gpu"] });
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  const tiles = new Map();          // "z" -> {ok, notFound}
  page.on("response", (r) => {
    const m = r.url().match(/\/maps\/lebanon-dem\/(\d+)\/\d+\/\d+/);
    if (!m) return;
    const z = m[1];
    const e = tiles.get(z) || { ok: 0, notFound: 0, other: 0 };
    if (r.status() === 200) e.ok++; else if (r.status() === 404) e.notFound++; else e.other++;
    tiles.set(z, e);
  });
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 200)); });

  const html = `<!doctype html><html><head><link rel="stylesheet" href="/frontend/vendor/maplibre/maplibre-gl.css"></head>
<body style="margin:0"><div id="m" style="width:1200px;height:800px"></div>
<script type="module">
  import * as maplibregl from '/frontend/vendor/maplibre/maplibre-gl.mjs';
  maplibregl.setWorkerUrl('/frontend/vendor/maplibre/maplibre-gl-worker.mjs');
  const map = new maplibregl.Map({ container: 'm', style: '/frontend/maps/styles/terrain.json?v=styles-3',
    center: [35.5018, 33.8938], zoom: 10 });
  window.__ready = false;
  map.on('load', () => { window.__map = map; window.__ready = true; });
</script></body></html>`;
  await page.route("**/probe.html", (route) => route.fulfill({ status: 200, contentType: "text/html", body: html }));
  await page.goto(`${BASE}/probe.html`, { waitUntil: "networkidle" });
  await page.waitForFunction(() => window.__ready === true, { timeout: 30000 });

  for (const z of [12, 13, 14, 15, 16, 8, 6, 5, 4, 3]) {
    await page.evaluate((zz) => window.__map.jumpTo({ zoom: zz, center: [35.5018, 33.8938] }), z);
    await page.waitForTimeout(1200);
  }
  await page.waitForTimeout(2000);
  const rows = [...tiles.entries()].sort((a, b) => a[0] - b[0]);
  console.log("zoom  200  404  other");
  for (const [z, e] of rows) console.log(String(z).padStart(4), String(e.ok).padStart(4), String(e.notFound).padStart(4), String(e.other).padStart(5));
  const outOfRange = rows.filter(([z]) => Number(z) < 6 || Number(z) > 12);
  const failures = rows.filter(([, e]) => e.notFound > 0 || e.other > 0);
  console.log("requests outside z6-12:", JSON.stringify(outOfRange));
  console.log("non-200 responses:", JSON.stringify(failures));
  console.log("console errors:", consoleErrors.length, consoleErrors.slice(0, 3));
  await browser.close();
  process.exit(outOfRange.length === 0 && failures.length === 0 && consoleErrors.length === 0 ? 0 : 1);
})().catch((e) => { console.error("probe failed:", e); process.exit(2); });
