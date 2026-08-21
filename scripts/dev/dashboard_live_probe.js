/**
 * Live Feed dashboard — REAL-data verification against the isolated stack.
 *
 *   node scripts/dev/dashboard_live_probe.js --base http://localhost:8090
 *
 * dashboard_grid_probe.js proves the geometry with synthetic cards; this pass
 * proves the same layout holds when the APPLICATION renders the cards: six
 * pipelines seeded through the real webhook ingest path, cards created by the
 * page's own WebSocket -> renderPipelineCard() flow, live updates streaming
 * while the layout, layering and interactions are measured.
 *
 * Run it ONLY against an isolated stack (it creates identities/detections).
 *
 * playwright-core resolved as in browser_smoke.js (no network).
 */
const PW_CORE = process.env.PW_CORE ||
  "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW_CORE);
const fs = require("fs");
const path = require("path");

const arg = (n, d) => { const i = process.argv.indexOf(n); return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : d; };
const BASE = arg("--base", "http://localhost:8090");
const ADMIN = { username: process.env.SMOKE_USER || "admin",
                password: process.env.SMOKE_PASS || "admin123" };

if (!/8090|localhost:8\d\d\d/.test(BASE)) {
  console.error("Refusing: this probe seeds real detections; point --base at an isolated stack.");
  process.exit(2);
}

const FIXTURES = path.resolve(__dirname, "../../tests/fixtures/faces");
const FACES = ["face_a.jpg", "face_b.jpg", "face_c.png",
               "face_a.jpg", "face_b.jpg", "face_c.png"];
const PIPELINES = [
  "live-cam-01", "live-cam-02", "live-cam-03", "live-cam-04", "live-cam-05",
  "live-cam-06-terminal-two-departures-concourse-north-mezzanine-overflow-bank-alpha",
];

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

async function apiLogin() {
  const r = await fetch(`${BASE}/api/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(ADMIN) });
  if (!r.ok) throw new Error(`api login ${r.status}`);
  return (await r.json()).access_token;
}

async function mintCredential(token) {
  const r = await fetch(`${BASE}/api/admin/webhook-credentials`, {
    method: "POST", headers: { "Content-Type": "application/json",
                               Authorization: `Bearer ${token}` },
    body: JSON.stringify({ name: `dashfix-${Date.now()}` }) });
  if (r.status !== 201) throw new Error(`mint credential ${r.status} ${await r.text()}`);
  const body = await r.json();
  return { id: body.id || (body.credential && body.credential.id),
           token: body.token || body.secret || (body.credential && body.credential.token) };
}

function jpegSize(buf) {
  let i = 2;
  while (i < buf.length) {
    if (buf[i] !== 0xff) { i++; continue; }
    const marker = buf[i + 1];
    if (marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc) {
      return { height: buf.readUInt16BE(i + 5), width: buf.readUInt16BE(i + 7) };
    }
    i += 2 + buf.readUInt16BE(i + 2);
  }
  return { width: 800, height: 600 };
}

async function inject(credentialToken, pipelineId, fixture, tag) {
  const raw = fs.readFileSync(path.join(FIXTURES, fixture));
  const { width, height } = jpegSize(raw);
  const r = await fetch(`${BASE}/api/webhook/${pipelineId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json",
               Authorization: `Bearer ${credentialToken}` },
    body: JSON.stringify({
      image: raw.toString("base64"),
      location_name: pipelineId,
      predictions: [{ class_name: "person", confidence: 0.9, bbox: [0, 0, width, height] }],
      request_id: `dashfix-${tag}-${Date.now()}`,
      timestamp: new Date().toISOString() }) });
  return r.status;
}

async function snapshot(page) {
  return page.evaluate(() => {
    const grid = document.getElementById("pipelineGrid");
    const cards = [...document.querySelectorAll(".pipeline-card")];
    return {
      n: cards.length,
      innerH: innerHeight, innerW: innerWidth,
      bodyScrollH: document.body.scrollHeight,
      docScrollW: document.documentElement.scrollWidth,
      gridScrollTop: grid ? grid.scrollTop : null,
      rects: cards.map((c) => {
        const r = c.getBoundingClientRect();
        return { id: c.dataset.pipelineId, top: Math.round(r.top),
                 bottom: Math.round(r.bottom), h: Math.round(r.height),
                 w: Math.round(r.width) };
      }),
      headerHeights: cards.map((c) =>
        Math.round(c.querySelector(".pipeline-header").getBoundingClientRect().height)),
      tileCounts: cards.map((c) => c.querySelectorAll(".detection-item").length),
    };
  });
}

(async () => {
  const token = await apiLogin();

  // Enroll face_a as a KNOWN person first: recognised faces are what produce
  // image tiles in a card; an un-enrolled face renders as an unknown-only
  // card with a note and no tiles (correct behaviour, but it would leave the
  // alert-overlay interaction with nothing to click).
  const raw = fs.readFileSync(path.join(FIXTURES, "face_a.jpg"));
  const boundary = "----dashfix" + Date.now();
  // multipart requires CRLF; JS template literals normalize raw CRLF to LF,
  // so the separator is built from char codes that nothing can translate
  const CRLF = String.fromCharCode(13, 10);
  const bodyParts = Buffer.concat([
    Buffer.from("--" + boundary + CRLF +
      'Content-Disposition: form-data; name="person_name"' + CRLF + CRLF +
      "qa_dashfix_person" + CRLF),
    Buffer.from("--" + boundary + CRLF +
      'Content-Disposition: form-data; name="is_face_image"' + CRLF + CRLF +
      "false" + CRLF),
    Buffer.from("--" + boundary + CRLF +
      'Content-Disposition: form-data; name="photo"; filename="a.jpg"' + CRLF +
      "Content-Type: image/jpeg" + CRLF + CRLF),
    raw,
    Buffer.from(CRLF + "--" + boundary + "--" + CRLF)]);
  const enrol = await fetch(`${BASE}/api/upload-person`, {
    method: "POST",
    headers: { "Content-Type": `multipart/form-data; boundary=${boundary}`,
               Authorization: `Bearer ${token}` },
    body: bodyParts });
  const enrolBody = await enrol.json().catch(() => ({}));
  if (enrol.status === 202 && enrolBody.upload_token) {
    await fetch(`${BASE}/api/enrollment/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json",
                 Authorization: `Bearer ${token}`,
                 "X-Requested-With": "XMLHttpRequest" },
      body: JSON.stringify({ action: "create_new",
                             upload_token: enrolBody.upload_token,
                             display_name: "qa_dashfix_person",
                             confirm_create_new: true }) });
  }
  console.log(`Enrolled known person (status ${enrol.status})`);

  const credential = await mintCredential(token);
  console.log(`Seeding 6 pipelines through the real webhook path at ${BASE} ...`);
  for (let i = 0; i < PIPELINES.length; i++) {
    const status = await inject(credential.token, PIPELINES[i], FACES[i], "seed");
    if (status >= 300) { console.error(`  webhook ${PIPELINES[i]} -> ${status}`); }
  }
  await new Promise((r) => setTimeout(r, 4000));   // let ingestion settle

  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("pageerror", (e) => consoleErrors.push(String(e).slice(0, 160)));
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 160)); });

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", ADMIN.username);
    await page.fill("#password", ADMIN.password);
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }),
                       page.click('button[type="submit"]')]);
    await page.goto(`${BASE}/dashboard`, { waitUntil: "domcontentloaded" });

    // wait for the application itself to render 6 cards from its store
    await page.waitForFunction(
      () => document.querySelectorAll(".pipeline-card").length >= 6,
      { timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(1500);
    consoleErrors.length = 0;

    for (const [w, h] of [[1920, 1080], [1366, 768]]) {
      await page.setViewportSize({ width: w, height: h });
      await page.waitForTimeout(400);
      const label = `${w}x${h}`;
      const before = await snapshot(page);

      check(`[${label}] the application rendered 6 real cards`,
        before.n === 6, `cards=${before.n}`);
      check(`[${label}] all 6 fit — zero page scroll (both axes)`,
        before.rects.every((r) => r.bottom <= before.innerH + 1) &&
        before.bodyScrollH <= before.innerH + 1 &&
        before.docScrollW <= before.innerW + 1,
        `last bottom=${Math.max(...before.rects.map((r) => r.bottom))} ` +
        `viewportH=${before.innerH} bodyH=${before.bodyScrollH}`);
      check(`[${label}] real long name did not grow its header`,
        Math.max(...before.headerHeights) - Math.min(...before.headerHeights) <= 1,
        `headers=${JSON.stringify(before.headerHeights)}`);

      // live updates: 3 more detections into pipeline 1 while watching layout
      const trackBefore = before.rects.map((r) => `${r.top}:${r.h}`).join("|");
      for (let k = 0; k < 3; k++) {
        await inject(credential.token, PIPELINES[0], FACES[k % 3], `live${k}`);
      }
      await page.waitForTimeout(5000);
      const after = await snapshot(page);
      const trackAfter = after.rects.map((r) => `${r.top}:${r.h}`).join("|");

      check(`[${label}] live updates caused no track-height change / layout shift`,
        trackBefore === trackAfter,
        trackBefore === trackAfter ? "rects identical across updates"
          : `before=${trackBefore} after=${trackAfter}`);
      check(`[${label}] second row still above the fold after updates`,
        after.rects.every((r) => r.bottom <= after.innerH + 1),
        `last bottom=${Math.max(...after.rects.map((r) => r.bottom))}`);
      check(`[${label}] accumulation stayed inside the card`,
        after.bodyScrollH <= after.innerH + 1 && (after.gridScrollTop || 0) === 0,
        `bodyH=${after.bodyScrollH} gridScrollTop=${after.gridScrollTop}`);
    }

    // layering while updates stream: dropdown, Add Person, alert overlay
    await page.setViewportSize({ width: 1920, height: 1080 });
    const layering = await page.evaluate(async () => {
      const out = {};
      const dropdown = document.querySelector("[data-dropdown]");
      if (dropdown) {
        dropdown.querySelector(".dropdown-toggle").click();
        await new Promise((r) => setTimeout(r, 300));
        const menu = dropdown.querySelector(".dropdown-menu");
        if (menu && menu.getClientRects().length) {
          const r2 = menu.getBoundingClientRect();
          out.dropdownTop = menu.contains(
            document.elementFromPoint(r2.left + r2.width / 2, r2.top + 15));
        }
        document.body.click();
      }
      if (window.openUploadModal) {
        window.openUploadModal();
        await new Promise((r) => setTimeout(r, 1200));
        const modal = document.getElementById("uploadModal");
        out.modalOpen = modal && getComputedStyle(modal).display !== "none";
        if (out.modalOpen) {
          const box = modal.querySelector(".upload-modal-content").getBoundingClientRect();
          out.modalTop = modal.contains(document.elementFromPoint(
            box.left + box.width / 2, box.top + 20));
          window.closeUploadModal && window.closeUploadModal();
          await new Promise((r) => setTimeout(r, 300));
        }
      }
      const img = document.querySelector(".detection-image");
      if (img) {
        img.click();
        await new Promise((r) => setTimeout(r, 400));
        const overlay = document.getElementById("alertOverlay");
        out.alertShown = overlay && overlay.classList.contains("show");
        if (out.alertShown) {
          const r3 = overlay.getBoundingClientRect();
          out.alertTop = overlay.contains(document.elementFromPoint(
            r3.left + r3.width / 2, r3.top + r3.height / 2));
          document.getElementById("alertBackdrop")?.classList.remove("show");
          overlay.classList.remove("show");
        }
      }
      return out;
    });
    check("navbar dropdown above the live cards",
      layering.dropdownTop === true, JSON.stringify(layering));
    check("Add Person modal opens above the live cards",
      layering.modalOpen === true && layering.modalTop === true,
      `open=${layering.modalOpen} top=${layering.modalTop}`);
    if (layering.alertShown === undefined) {
      check("clicking a REAL detection opens the alert overlay on top", false,
        "no .detection-image tile rendered — the known face was not recognised");
    } else {
      check("clicking a REAL detection opens the alert overlay on top",
        layering.alertShown === true && layering.alertTop === true,
        `shown=${layering.alertShown} top=${layering.alertTop}`);
    }

    check("zero console/page errors during live updates",
      consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | ") || "clean");

    await page.screenshot({
      path: path.resolve(__dirname, "../../logs/smoke/dash_live_6cards.png") });
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 240));
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\nOVERALL: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
