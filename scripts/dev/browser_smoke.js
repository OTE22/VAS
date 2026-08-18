/**
 * Browser smoke matrix (plan §14.6) — headless Chrome driven by playwright-core.
 *
 *   node scripts/dev/browser_smoke.js [--base http://localhost] [--out logs/smoke]
 *
 * Logs in as the bootstrap admin through the REAL sign-in page, then visits
 * every page of the matrix and records, per page:
 *   - JS console errors + uncaught page errors (must be 0)
 *   - every same-origin API request with a status >= 400 (only the expected
 *     ones are tolerated, listed per page below)
 *   - a DOM readiness probe (the page rendered its main content)
 * On the dashboard a REAL detection is injected through the webhook (bearer
 * ingest credential minted for the run) and the WebSocket frames the page
 * receives are captured (window.WebSocket is wrapped before any page script
 * runs) — `new_detection` must arrive; `detection_alerts` arrives only when
 * the identity has a live alert / watchlist membership (reported, not required).
 * Finally /openapi.json, /docs, /redoc must be 200 with 0 console errors.
 *
 * Exit code 0 = every page PASS. The JSON report is written to --out.
 *
 * playwright-core is resolved from the n8n global install (no network,
 * no browser download); the browser is the installed Google Chrome.
 */
const fs = require("fs");
const path = require("path");

const PW_CORE = process.env.PW_CORE ||
  "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW_CORE);

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : dflt; };
const BASE = opt("--base", "http://localhost");
const OUT = opt("--out", "logs/smoke");
const ADMIN = { username: "admin", password: "admin123" };
const FACE = path.resolve("tests/fixtures/faces/face_a.jpg");
const SMOKE_PIPELINE = "smoke-cam-01";

// page → { url, ready: css selector that proves the page rendered, tolerate: [status regexps] }
const PAGES = [
  { name: "Login",          url: "/signin",             ready: "#username", noauth: true },
  { name: "Home",           url: "/home",               ready: "body" },
  { name: "Dashboard",      url: "/dashboard",          ready: "body", inject: true },
  { name: "Known Persons",  url: "/tracking-people",    ready: "body" },
  { name: "Unknown Persons",url: "/admin/unknown",      ready: "body" },
  { name: "Search by Image",url: "/admin/search",       ready: "body" },
  { name: "Watchlists",     url: "/admin/watchlists",   ready: "body" },
  { name: "Live Alerts",    url: "/admin/live-alerts",  ready: "body" },
  { name: "Audit Logs",     url: "/admin/audit",        ready: "body" },
  { name: "ML-Ops",         url: "/admin/ml-ops",       ready: "body" },
  { name: "User Management",url: "/admin/users",        ready: "body" },
  { name: "Identity detail",url: "IDENTITY",            ready: "body" },
  { name: "openapi.json",   url: "/openapi.json",       ready: null, expectJson: true },
  { name: "docs",           url: "/docs",               ready: "body" },
  { name: "redoc",          url: "/redoc",              ready: "body" },
];
// requests that legitimately answer >= 400 on a fresh admin session
const TOLERATED = [
  /\/api\/auth\/me.*401/,                       // pre-login probe on the sign-in page
  /\/api\/auth\/refresh.*401/,
  /\/favicon\.ico.*404/,
  /\/api\/chatbot\/access.*40[13]/,             // optional module
  /\/api\/ml\/training-jobs\/.*404/,            // no job selected yet
];
// console messages that are the CSP doing its job (external host blocked BY DESIGN), per page
const TOLERATED_CONSOLE = {
  redoc: [/cdn\.redoc\.ly\/redoc\/logo-mini\.svg/],   // Redoc's own external logo; no external hosts allowed
};

async function apiLogin() {
  const r = await fetch(`${BASE}/api/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(ADMIN) });
  if (!r.ok) throw new Error(`api login ${r.status}`);
  return (await r.json()).access_token;
}

async function mintCredential(token) {
  const r = await fetch(`${BASE}/api/admin/webhook-credentials`, {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ name: `smoke-${Date.now()}` }) });
  if (r.status !== 201) throw new Error(`mint credential ${r.status} ${await r.text()}`);
  const body = await r.json();
  return { id: body.id || (body.credential && body.credential.id), token: body.token || body.secret || (body.credential && body.credential.token), raw: body };
}

async function revokeCredential(token, id) {
  if (!id) return;
  await fetch(`${BASE}/api/admin/webhook-credentials/${id}`, { method: "DELETE", headers: { Authorization: `Bearer ${token}` } });
}

function jpegSize(buf) {
  // minimal JPEG SOF parser (the webhook contract carries a full-frame person bbox)
  let i = 2;
  while (i < buf.length) {
    if (buf[i] !== 0xff) { i++; continue; }
    const marker = buf[i + 1];
    if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
      return { height: buf.readUInt16BE(i + 5), width: buf.readUInt16BE(i + 7) };
    }
    i += 2 + buf.readUInt16BE(i + 2);
  }
  return { height: 0, width: 0 };
}

async function injectDetection(credentialToken) {
  const raw = fs.readFileSync(FACE);
  const { width, height } = jpegSize(raw);
  // the ingest contract: an upstream person detection with a bbox; the API
  // then runs face detection/recognition inside that box
  const r = await fetch(`${BASE}/api/webhook/${SMOKE_PIPELINE}`, {
    method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${credentialToken}` },
    body: JSON.stringify({ image: raw.toString("base64"), location_name: "Smoke Camera",
      predictions: [{ class_name: "person", confidence: 0.9, bbox: [0, 0, width, height] }],
      request_id: `smoke-${Date.now()}`, timestamp: new Date().toISOString() }) });
  return { status: r.status, body: (await r.text()).slice(0, 300) };
}

async function firstIdentityId(token) {
  const r = await fetch(`${BASE}/api/admin/identities?page=1&page_size=1`, { headers: { Authorization: `Bearer ${token}` } });
  if (!r.ok) return null;
  const body = await r.json();
  const list = Array.isArray(body) ? body : (body.items || body.identities || body.results || []);
  return list.length ? (list[0].id || list[0].identity_id) : null;
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const report = { base: BASE, started: new Date().toISOString(), pages: [], websocket: null, overall: "PASS" };
  const apiToken = await apiLogin();
  const identityId = await firstIdentityId(apiToken);
  const browser = await chromium.launch({ executablePath: CHROME, headless: true,
    args: ["--no-first-run", "--disable-gpu", "--disable-extensions"] });
  const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1400, height: 900 } });
  await context.addInitScript(() => {
    // capture every WebSocket frame the page receives, before any page script runs
    window.__wsMessages = [];
    const Native = window.WebSocket;
    window.WebSocket = new Proxy(Native, {
      construct(target, argsList) {
        const ws = new target(...argsList);
        ws.addEventListener("message", (ev) => {
          try { const m = JSON.parse(ev.data); window.__wsMessages.push({ type: m.type, at: Date.now() }); }
          catch (e) { window.__wsMessages.push({ type: "(non-json)", at: Date.now() }); }
        });
        return ws;
      },
    });
  });
  const page = await context.newPage();
  let consoleErrors = [], pageErrors = [], badResponses = [];
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const loc = (msg.location() || {}).url || "";
    consoleErrors.push({ text: msg.text().slice(0, 300), url: loc });
  });
  page.on("pageerror", (err) => pageErrors.push(String(err && err.message || err).slice(0, 300)));
  page.on("response", (res) => {
    const url = res.url();
    if (!url.startsWith(BASE)) return;
    if (res.status() >= 400) badResponses.push(`${res.request().method()} ${url.replace(BASE, "")} ${res.status()}`);
  });
  const reset = () => { consoleErrors = []; pageErrors = []; badResponses = []; };

  // ---- login through the real page
  reset();
  await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
  await page.fill("#username", ADMIN.username);
  await page.fill("#password", ADMIN.password);
  await Promise.all([page.waitForNavigation({ waitUntil: "networkidle", timeout: 30000 }), page.click("button[type=submit]")]);
  const loginResult = { name: "Login", url: "/signin", landed: page.url().replace(BASE, ""),
    consoleErrors: consoleErrors.map((c) => c.text), pageErrors,
    badResponses: badResponses.filter((b) => !TOLERATED.some((t) => t.test(b))) };
  loginResult.status = (loginResult.consoleErrors.length + loginResult.pageErrors.length + loginResult.badResponses.length === 0
    && !/signin/.test(loginResult.landed)) ? "PASS" : "FAIL";
  report.pages.push(loginResult);

  let credential = null;
  try {
    for (const p of PAGES) {
      if (p.name === "Login") continue;
      let url = p.url;
      if (url === "IDENTITY") { if (!identityId) { report.pages.push({ name: p.name, status: "SKIP", reason: "no identity" }); continue; } url = `/admin/identity/${identityId}`; }
      reset();
      const entry = { name: p.name, url };
      try {
        const resp = await page.goto(`${BASE}${url}`, { waitUntil: "networkidle", timeout: 60000 });
        entry.httpStatus = resp ? resp.status() : null;
        if (p.expectJson) {
          const text = await page.evaluate(() => document.body.innerText);
          entry.jsonOk = (() => { try { const j = JSON.parse(text); return !!(j.openapi && j.paths); } catch (e) { return false; } })();
        } else if (p.ready) {
          await page.waitForSelector(p.ready, { timeout: 15000 });
        }
        await page.waitForTimeout(1500);   // let deferred fetches settle
        entry.title = await page.title();
        entry.redirectedToLogin = /signin/.test(page.url());
        if (p.inject) {
          credential = credential || await mintCredential(apiToken);
          entry.webhook = await injectDetection(credential.token);
          // wait up to 20 s for the WS frames
          const deadline = Date.now() + 20000; let msgs = [];
          while (Date.now() < deadline) {
            msgs = await page.evaluate(() => window.__wsMessages || []);
            if (msgs.some((m) => ["new_detection", "new_unknown_detection", "unknown_activity"].includes(m.type))) break;
            await page.waitForTimeout(500);
          }
          const types = msgs.map((m) => m.type);
          report.websocket = { framesSeen: types, hasNewDetection: types.some((t) => ["new_detection", "new_unknown_detection", "unknown_activity"].includes(t)),
            hasDetectionAlerts: types.includes("detection_alerts"), webhook: entry.webhook };
          await page.waitForTimeout(2500);   // alert broadcast follows the batch flush (≤1 s) + persistence
          const after = await page.evaluate(() => (window.__wsMessages || []).map((m) => m.type));
          report.websocket.framesSeen = after;
          report.websocket.hasDetectionAlerts = after.includes("detection_alerts");
        }
      } catch (e) {
        entry.error = String(e && e.message || e).slice(0, 300);
      }
      const tolerated = TOLERATED_CONSOLE[p.name] || [];
      // a message emitted by a script the current document never loaded belongs to the
      // PREVIOUS document tearing down during navigation (e.g. its WebSocket closing)
      let loadedScripts = [];
      try { loadedScripts = await page.evaluate(() => Array.from(document.scripts).map((s) => s.src).filter(Boolean)); } catch (e) { /* ignore */ }
      const belongsHere = (c) => !c.url || !/\.js(\?|$)/.test(c.url) || loadedScripts.some((u) => u.split("?")[0] === c.url.split("?")[0]);
      const own = consoleErrors.filter(belongsHere);
      entry.staleConsole = consoleErrors.filter((c) => !belongsHere(c)).map((c) => `${c.text} [${c.url}]`);
      entry.toleratedConsole = own.filter((c) => tolerated.some((t) => t.test(c.text))).map((c) => c.text);
      entry.consoleErrors = own.filter((c) => !tolerated.some((t) => t.test(c.text))).map((c) => c.text);
      entry.pageErrors = pageErrors.slice();
      entry.badResponses = badResponses.filter((b) => !TOLERATED.some((t) => t.test(b)));
      entry.status = (!entry.error && !entry.redirectedToLogin && (entry.httpStatus === 200)
        && entry.consoleErrors.length === 0 && entry.pageErrors.length === 0 && entry.badResponses.length === 0
        && (p.expectJson ? entry.jsonOk : true)) ? "PASS" : "FAIL";
      try { await page.screenshot({ path: path.join(OUT, `${p.name.replace(/[^a-z0-9]+/gi, "_").toLowerCase()}.png`), fullPage: false }); } catch (e) { /* ignore */ }
      report.pages.push(entry);
    }
    if (report.websocket && !report.websocket.hasNewDetection) report.overall = "FAIL";
  } finally {
    if (credential) await revokeCredential(apiToken, credential.id);
    await browser.close();
  }
  if (report.pages.some((p) => p.status === "FAIL")) report.overall = "FAIL";
  report.finished = new Date().toISOString();
  fs.writeFileSync(path.join(OUT, "browser_smoke_report.json"), JSON.stringify(report, null, 2));
  for (const p of report.pages) {
    console.log(`${(p.status || "?").padEnd(5)} ${p.name.padEnd(16)} ${p.url || ""} http=${p.httpStatus || ""} console=${(p.consoleErrors || []).length} pageerr=${(p.pageErrors || []).length} bad=${(p.badResponses || []).length}${p.error ? " error=" + p.error : ""}`);
    for (const b of (p.badResponses || [])) console.log(`      bad: ${b}`);
    for (const c of (p.consoleErrors || [])) console.log(`      console: ${c}`);
    for (const c of (p.pageErrors || [])) console.log(`      pageerror: ${c}`);
  }
  console.log("websocket:", JSON.stringify(report.websocket));
  console.log("OVERALL:", report.overall);
  process.exit(report.overall === "PASS" ? 0 : 1);
})().catch((e) => { console.error("smoke harness failed:", e); process.exit(2); });
