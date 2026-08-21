/**
 * Live Feed dashboard grid probe — 6 cards must fit the viewport.
 *
 *   node scripts/dev/dashboard_grid_probe.js [--base http://localhost]
 *
 * The dashboard previously capped its grid at calc(100vh - 400px) — a budget
 * for a stats section that was removed — and re-opened the document scroller,
 * so with 6 pipelines the second row of cards landed below the fold
 * (measured: 6th card bottom at y=1222 on a 1080px viewport).
 *
 * This is the deterministic geometry regression: it synthesizes N cards with
 * the exact DOM shape renderPipelineCard() builds (no backend data, no DB
 * writes) and measures what a user could actually see. Real live-stack
 * verification is a separate pass — this probe is necessary, not sufficient.
 *
 * Matrix: resolutions 1920×1080 / 1600×900 / 1366×768 / 1093×614 (the CSS-px
 * equivalent of 125% zoom at 1366×768) × card counts 1,2,3,4,5,6,9. Cards
 * carry 0, 1, 3 and 12 face tiles; one has an ~80-char location name.
 *
 * playwright-core resolved as in browser_smoke.js (no network).
 */
const PW_CORE = process.env.PW_CORE ||
  "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW_CORE);
const path = require("path");
const fs = require("fs");

const arg = (n, d) => { const i = process.argv.indexOf(n); return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : d; };
const BASE = arg("--base", "http://localhost");
const USER = process.env.SMOKE_USER || "admin";
const PASS = process.env.SMOKE_PASS || "admin123";

const RESOLUTIONS = [[1920, 1080], [1600, 900], [1366, 768], [1093, 614]];
const COUNTS = [1, 2, 3, 4, 5, 6, 9];
const SHOT_DIR = path.resolve(__dirname, "../../logs/smoke");

// Today's computed font sizes (dashboard.css) — the readability floor: the
// layout fix must not shrink any of these.
const FONT_FLOORS = {
  ".pipeline-title": 14.4,      // 0.9rem
  ".pipeline-badge": 9.6,       // 0.6rem
  ".face-name": 9.6,
  ".timestamp": 8,              // 0.5rem — today's actual value
};

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};
const note = (msg) => console.log(`  note  ${msg}`);

/** Build N cards exactly as renderPipelineCard() does. */
async function buildCards(page, n) {
  await page.evaluate((count) => {
    const grid = document.getElementById("pipelineGrid");
    grid.querySelectorAll(".pipeline-card, .no-data").forEach((el) => el.remove());
    const LONG = "Terminal 2 Departures Concourse North Mezzanine Overflow Camera Bank Alpha 07";
    const faceCounts = [0, 1, 3, 3, 3, 12, 3, 3, 3];

    const px = (w) => "data:image/svg+xml;base64," + btoa(
      `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${w}"><rect width="${w}" height="${w}" fill="#16324a"/></svg>`);

    for (let c = 0; c < count; c++) {
      const card = document.createElement("div");
      card.className = "pipeline-card";
      card.dataset.pipelineId = "probe-" + c;

      const header = document.createElement("div");
      header.className = "pipeline-header";
      const title = document.createElement("div");
      title.className = "pipeline-title";
      title.textContent = c === 2 ? LONG : "camera-" + (c + 1);
      header.appendChild(title);
      const badge = document.createElement("div");
      badge.className = "pipeline-badge";
      badge.textContent = faceCounts[c] + " unique persons";
      header.appendChild(badge);
      card.appendChild(header);

      const content = document.createElement("div");
      content.className = "pipeline-content";
      if (faceCounts[c] === 0) {
        const noteEl = document.createElement("div");
        noteEl.className = "unknown-only-note";
        noteEl.textContent = "Only unknown faces on this camera";
        content.appendChild(noteEl);
      }
      for (let i = 0; i < faceCounts[c]; i++) {
        const item = document.createElement("div");
        item.className = "detection-item";
        item.dataset.pipeline = "probe-" + c;
        item.dataset.face = "person-" + i;
        const ic = document.createElement("div");
        ic.className = "detection-image-container";
        const img = document.createElement("img");
        img.className = "detection-image";
        img.src = px(160);
        ic.appendChild(img);
        item.appendChild(ic);
        const cont = document.createElement("div");
        cont.className = "detection-item-content";
        const fl = document.createElement("div");
        fl.className = "faces-list";
        const fi = document.createElement("div");
        fi.className = "face-item";
        const fn = document.createElement("span");
        fn.className = "face-name";
        fn.textContent = "Person " + (i + 1);
        fi.appendChild(fn);
        fl.appendChild(fi);
        cont.appendChild(fl);
        const ts = document.createElement("div");
        ts.className = "timestamp";
        ts.textContent = "Visible for: 2m 14s";
        cont.appendChild(ts);
        item.appendChild(cont);
        content.appendChild(item);
      }
      card.appendChild(content);
      grid.appendChild(card);
    }
  }, n);
  await page.waitForTimeout(250);
}

async function measure(page) {
  return page.evaluate(() => {
    const grid = document.getElementById("pipelineGrid");
    const cards = [...document.querySelectorAll(".pipeline-card")];
    const rects = cards.map((c) => {
      const r = c.getBoundingClientRect();
      return { top: Math.round(r.top), bottom: Math.round(r.bottom),
               left: Math.round(r.left), right: Math.round(r.right),
               w: Math.round(r.width), h: Math.round(r.height) };
    });
    const gr = grid.getBoundingClientRect();
    const overlap = (() => {
      for (let a = 0; a < rects.length; a++)
        for (let b = a + 1; b < rects.length; b++) {
          const A = rects[a], B = rects[b];
          if (A.left < B.right - 1 && B.left < A.right - 1 &&
              A.top < B.bottom - 1 && B.top < A.bottom - 1) return [a, b];
        }
      return null;
    })();

    // header/content clipped by card?
    let clipped = null;
    cards.forEach((c, i) => {
      const cr = c.getBoundingClientRect();
      const h = c.querySelector(".pipeline-header").getBoundingClientRect();
      if (h.bottom > cr.bottom + 1 || h.top < cr.top - 1) clipped = `header of card ${i}`;
    });

    const imgs = [...document.querySelectorAll(".detection-image")].slice(0, 6)
      .map((im) => { const r = im.getBoundingClientRect();
        return { w: Math.round(r.width), h: Math.round(r.height) }; });

    const fonts = {};
    for (const sel of [".pipeline-title", ".pipeline-badge", ".face-name", ".timestamp"]) {
      const el = document.querySelector(sel);
      fonts[sel] = el ? parseFloat(getComputedStyle(el).fontSize) : null;
    }

    const longTitle = document.querySelector('[data-pipeline-id="probe-2"] .pipeline-title');
    const shortHeader = document.querySelector('[data-pipeline-id="probe-0"] .pipeline-header');
    const longHeader = document.querySelector('[data-pipeline-id="probe-2"] .pipeline-header');

    return {
      innerW: innerWidth, innerH: innerHeight,
      bodyScrollH: document.body.scrollHeight, bodyScrollW: document.body.scrollWidth,
      docScrollW: document.documentElement.scrollWidth,
      gridTop: Math.round(gr.top), gridH: Math.round(grid.clientHeight),
      gridScrollH: grid.scrollHeight,
      cols: getComputedStyle(grid).gridTemplateColumns.split(" ").length,
      rects, overlap, clipped, imgs, fonts,
      longEllipsised: longTitle
        ? longTitle.scrollWidth > longTitle.clientWidth + 1 : null,
      headerHeights: shortHeader && longHeader
        ? [Math.round(shortHeader.getBoundingClientRect().height),
           Math.round(longHeader.getBoundingClientRect().height)] : null,
    };
  });
}

(async () => {
  fs.mkdirSync(SHOT_DIR, { recursive: true });
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("pageerror", (e) => consoleErrors.push(String(e).slice(0, 140)));
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 140)); });

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER); await page.fill("#password", PASS);
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }),
                       page.click('button[type="submit"]')]);
    await page.goto(`${BASE}/dashboard`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2500);
    consoleErrors.length = 0;

    const dimensionReport = [];

    for (const [w, h] of RESOLUTIONS) {
      await page.setViewportSize({ width: w, height: h });
      await page.waitForTimeout(300);
      const label = `${w}x${h}`;

      for (const n of COUNTS) {
        await buildCards(page, n);
        const m = await measure(page);

        // The 3x2 viewport fit is the >=1100px desktop contract. Below that
        // (including the 125%-zoom equivalent) the grid scrolls by design.
        const fitRequired = n <= 6 && w >= 1100;
        const allVisible = m.rects.every((r) => r.bottom <= m.innerH + 1 && r.top >= -1);
        if (fitRequired) {
          check(`[${label}] ${n} card(s): all fully inside the viewport`,
            allVisible,
            `last bottom=${m.rects[m.rects.length - 1].bottom} viewportH=${m.innerH}`);
        } else if (n > 6) {
          check(`[${label}] ${n} cards: grid scrolls (page does not)`,
            m.gridScrollH > m.gridH + 4,
            `gridScroll=${m.gridScrollH} gridH=${m.gridH}`);
        } else {
          // sub-desktop width: fit not required, but scrolling must stay in
          // the grid and rows must stay readable
          const rowH = m.rects.length ? m.rects[0].h : 0;
          check(`[${label}] ${n} card(s): readable rows, grid-contained scroll`,
            rowH >= 280,
            `rowH=${rowH} (grid scrolls at this width by design)`);
        }

        check(`[${label}] ${n} card(s): no page scroll in either axis`,
          m.bodyScrollH <= m.innerH + 1 && m.docScrollW <= m.innerW + 1,
          `bodyH=${m.bodyScrollH}/${m.innerH} docW=${m.docScrollW}/${m.innerW}`);
        check(`[${label}] ${n} card(s): no overlap, nothing clipped`,
          !m.overlap && !m.clipped,
          m.overlap ? `cards ${m.overlap} overlap` : (m.clipped || "clean"));

        const nonSquare = m.imgs.find((im) => Math.abs(im.w - im.h) > 1);
        check(`[${label}] ${n} card(s): tile images stay square`,
          !nonSquare, nonSquare ? `${nonSquare.w}x${nonSquare.h}` : `${m.imgs.length} checked`);

        if (n === 6) {
          dimensionReport.push({
            resolution: label, cols: m.cols, gridTop: m.gridTop, gridH: m.gridH,
            cardW: m.rects[0].w, cardH: m.rects[0].h,
          });
          const floorFail = Object.entries(FONT_FLOORS)
            .find(([sel, floor]) => m.fonts[sel] !== null && m.fonts[sel] < floor - 0.1);
          check(`[${label}] readability floor holds`,
            !floorFail,
            floorFail ? `${floorFail[0]} = ${m.fonts[floorFail[0]]}px < ${floorFail[1]}px`
                      : JSON.stringify(m.fonts));
          check(`[${label}] long name ellipsises, header height unchanged`,
            m.longEllipsised === true && m.headerHeights &&
            Math.abs(m.headerHeights[0] - m.headerHeights[1]) <= 1,
            `ellipsised=${m.longEllipsised} headers=${JSON.stringify(m.headerHeights)}`);
          check(`[${label}] 3 columns on desktop`,
            w >= 1100 ? m.cols === 3 : m.cols >= 1, `cols=${m.cols}`);

          // scroll containment: wheel inside the 12-face card
          const contained = await page.evaluate(async () => {
            const card = document.querySelector('[data-pipeline-id="probe-5"] .pipeline-content');
            if (!card) return { skip: true };
            const grid = document.getElementById("pipelineGrid");
            const before = { grid: grid.scrollTop, body: document.body.scrollTop || window.scrollY };
            card.scrollTop = card.scrollHeight;
            await new Promise((r) => setTimeout(r, 120));
            const canReachEnd = card.scrollTop > 0 &&
              Math.abs(card.scrollTop + card.clientHeight - card.scrollHeight) < 4;
            return { canReachEnd,
                     gridMoved: grid.scrollTop !== before.grid,
                     bodyMoved: (document.body.scrollTop || window.scrollY) !== before.body };
          });
          check(`[${label}] 12-face card scrolls internally, nothing else moves`,
            contained.canReachEnd && !contained.gridMoved && !contained.bodyMoved,
            JSON.stringify(contained));
        }

        if ((w === 1920 || w === 1366) && [1, 2, 3, 6].includes(n)) {
          await page.screenshot({
            path: path.join(SHOT_DIR, `dash_grid_${w}x${h}_${n}cards.png`) });
        }
      }
    }

    // definite-height chain guard at each resolution (6 cards); the 50% row
    // calc is only active on >=1100px desktop
    for (const [w, h] of RESOLUTIONS.filter(([w]) => w >= 1100)) {
      await page.setViewportSize({ width: w, height: h });
      await buildCards(page, 6);
      const chain = await page.evaluate(() => {
        const grid = document.getElementById("pipelineGrid");
        const row = document.querySelector(".pipeline-card").getBoundingClientRect().height;
        const gap = parseFloat(getComputedStyle(grid).rowGap) || 0;
        return { gridH: grid.clientHeight, row: Math.round(row), gap,
                 pad: parseFloat(getComputedStyle(grid).paddingTop) +
                      parseFloat(getComputedStyle(grid).paddingBottom) };
      });
      const expected = (chain.gridH - chain.pad - chain.gap) / 2;
      check(`[${w}x${h}] %-rows resolve against a definite grid height`,
        Math.abs(chain.row - expected) <= 2,
        `row=${chain.row} expected=${Math.round(expected)} gridH=${chain.gridH}`);
    }

    // interactions: image click opens the alert overlay; dropdown above cards
    await page.setViewportSize({ width: 1920, height: 1080 });
    await buildCards(page, 6);
    const overlayOpens = await page.evaluate(async () => {
      // the delegated handler needs a faceStore entry; simulate the DOM click
      // path only as far as the overlay elements the page owns
      document.querySelector(".detection-image").click();
      await new Promise((r) => setTimeout(r, 300));
      const overlay = document.getElementById("alertOverlay");
      return { overlayExists: !!overlay,
               overlayShown: overlay ? overlay.classList.contains("show") : false };
    });
    // synthetic tiles have no store entry, so the handler exits before showing
    // the overlay; what we CAN assert deterministically is that the click was
    // dispatched into the delegated handler without throwing.
    check("image click reaches the delegated handler without errors",
      overlayOpens.overlayExists && consoleErrors.length === 0,
      `overlayShown=${overlayOpens.overlayShown} (synthetic tiles have no store entry)`);

    const dropdown = await page.evaluate(async () => {
      const trigger = document.querySelector(".military-navbar .dropdown-toggle, .military-navbar .dropdown > a, .military-navbar .dropdown-item")
        ?.closest(".dropdown")?.querySelector("a, button");
      const anyDropdown = document.querySelector(".military-navbar .dropdown");
      if (!anyDropdown) return { skip: "no dropdown" };
      const toggle = anyDropdown.querySelector("a, button");
      toggle.click();
      await new Promise((r) => setTimeout(r, 350));
      const menu = anyDropdown.querySelector(".dropdown-menu");
      if (!menu || !menu.getClientRects().length) return { open: false };
      const r2 = menu.getBoundingClientRect();
      const el = document.elementFromPoint(r2.left + r2.width / 2, r2.top + Math.min(20, r2.height / 2));
      return { open: true, topmostIsMenu: menu.contains(el) };
    });
    check("navbar dropdown opens above the cards",
      dropdown.skip ? true : (dropdown.open && dropdown.topmostIsMenu),
      JSON.stringify(dropdown));

    // resize responsiveness: 1920 -> 1000 -> 650 -> 1920
    const colSeq = [];
    for (const [w, h] of [[1920, 1080], [1000, 800], [650, 800], [1920, 1080]]) {
      await page.setViewportSize({ width: w, height: h });
      await page.waitForTimeout(300);
      colSeq.push(await page.evaluate(() =>
        getComputedStyle(document.getElementById("pipelineGrid"))
          .gridTemplateColumns.split(" ").length));
    }
    check("columns respond 3 -> 2 -> 1 -> 3 across resizes",
      colSeq.join(",") === "3,2,1,3", `got ${colSeq.join(",")}`);

    check("no console/page errors across the whole run",
      consoleErrors.length === 0,
      consoleErrors.slice(0, 3).join(" | ") || "clean");

    console.log("\nMEASURED DIMENSIONS (6 cards):");
    dimensionReport.forEach((d) => console.log(
      `  ${d.resolution}: grid starts y=${d.gridTop}, gridH=${d.gridH}px, ` +
      `card=${d.cardW}x${d.cardH}px, cols=${d.cols}`));
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 240));
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\nOVERALL: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
