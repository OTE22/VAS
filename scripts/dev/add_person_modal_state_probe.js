/**
 * ADD PERSON modal — client-side state and honesty probe.
 *
 *   node scripts/dev/add_person_modal_state_probe.js [--base http://localhost]
 *
 * add_person_probe.js stops at "the modal opened". This one inspects what the
 * modal TELLS the user and what it LEAVES BEHIND, without ever submitting — it
 * performs no upload, so it creates nothing and is safe against a live stack.
 *
 * Checks:
 *   - the advertised limit matches the limit the server actually enforces
 *   - the advertised "drag and drop" is really implemented
 *   - whether more than one photo can be chosen
 *   - close -> reopen leaves no stale preview / filename / enabled submit
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
const FIXTURE = path.resolve(__dirname, "../../tests/fixtures/faces/face_a.jpg");

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newContext({ viewport: { width: 1366, height: 768 } })
    .then((c) => c.newPage());

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER); await page.fill("#password", PASS);
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }),
                       page.click('button[type="submit"]')]);
    await page.goto(`${BASE}/dashboard`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2500);

    // What limit does the server actually enforce?
    const serverLimit = await page.evaluate(async () => {
      const r = await fetch("/api/dashboard/config", { credentials: "include" });
      const j = await r.json();
      const c = j.config || j;
      return { max: c.max_file_size_bytes, exts: c.allowed_extensions };
    });

    const openModal = async () => {
      await page.evaluate(() => window.openUploadModal && window.openUploadModal());
      await page.waitForFunction(() => {
        const m = document.getElementById("uploadModal");
        return m && getComputedStyle(m).display !== "none";
      }, { timeout: 10000 });
    };

    await openModal();

    // ---- what the modal claims vs what is true
    const claims = await page.evaluate(() => {
      const hint = document.querySelector("#uploadModal .file-upload-text, #uploadModal .file-upload-area");
      const input = document.getElementById("globalFileInput");
      return {
        hintText: (hint ? hint.textContent : "").replace(/\s+/g, " ").trim(),
        multiple: input ? input.hasAttribute("multiple") : null,
        accept: input ? input.getAttribute("accept") : null,
      };
    });

    const advertisedMb = (claims.hintText.match(/up to\s*(\d+)\s*MB/i) || [])[1];
    const realMb = serverLimit.max ? Math.round(serverLimit.max / 1048576) : null;
    check("advertised size limit matches the enforced limit",
      advertisedMb != null && realMb != null && Number(advertisedMb) === realMb,
      `modal says ${advertisedMb}MB, server enforces ${realMb}MB`);

    // Does the advertised drag-and-drop exist? Drop a REAL file and check the
    // page accepted it — dragover must be cancelled (or the browser never
    // fires drop) and the file must reach the input the form submits.
    const advertisesDnd = /drag and drop/i.test(claims.hintText);
    const dnd = await page.evaluate(async () => {
      const area = document.getElementById("globalFileUploadArea");
      const input = document.getElementById("globalFileInput");
      if (!area || !input) return { missing: true };

      const transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array([0xff, 0xd8, 0xff, 0xdb, 0x00])],
                                  "dropped.jpg", { type: "image/jpeg" }));

      const over = new DragEvent("dragover",
        { bubbles: true, cancelable: true, dataTransfer: transfer });
      area.dispatchEvent(over);
      const cancelled = over.defaultPrevented;   // required for a drop to follow
      const highlighted = area.className.includes("drag");

      area.dispatchEvent(new DragEvent("drop",
        { bubbles: true, cancelable: true, dataTransfer: transfer }));
      await new Promise((r) => setTimeout(r, 400));

      return {
        cancelled,
        highlighted,
        accepted: input.files.length === 1,
        name: input.files.length ? input.files[0].name : "",
      };
    });
    check("advertised drag-and-drop is implemented",
      !advertisesDnd || (dnd.cancelled && dnd.accepted),
      advertisesDnd
        ? `dragover cancelled=${dnd.cancelled} highlighted=${dnd.highlighted} fileAccepted=${dnd.accepted} (${dnd.name})`
        : "not advertised");

    // Informational: the backend supports many photos per person, but this
    // modal deliberately submits one at a time. Reported, not failed.
    console.log(`  note  one photo per submit (input multiple=${claims.multiple}); ` +
                `the backend's per-identity multi-image endpoint has no UI`);

    // ---- close -> reopen must not leak the previous selection
    await page.setInputFiles("#globalFileInput", FIXTURE);
    await page.waitForTimeout(600);
    const afterPick = await page.evaluate(() => ({
      info: (document.getElementById("globalFileInfo") || {}).textContent || "",
      submitDisabled: (document.getElementById("globalUploadSubmitBtn") || {}).disabled,
    }));

    await page.evaluate(() => window.closeUploadModal && window.closeUploadModal());
    await page.waitForTimeout(500);
    await openModal();
    await page.waitForTimeout(400);

    const reopened = await page.evaluate(() => {
      const img = document.getElementById("globalPreviewImage");
      return {
        name: (document.getElementById("globalPersonName") || {}).value,
        file: (document.getElementById("globalFileInput") || {}).value,
        info: (document.getElementById("globalFileInfo") || {}).textContent || "",
        previewSrc: img ? (img.getAttribute("src") || "").slice(0, 24) : "",
        previewShown: !!document.querySelector("#globalFilePreview.show"),
        submitDisabled: (document.getElementById("globalUploadSubmitBtn") || {}).disabled,
      };
    });

    check("reopening clears the name and file input",
      !reopened.name && !reopened.file,
      `name=${JSON.stringify(reopened.name)} file=${JSON.stringify(reopened.file)}`);
    check("reopening clears the previous photo preview",
      !reopened.previewSrc && !reopened.previewShown,
      `previewSrc="${reopened.previewSrc}..." shown=${reopened.previewShown}`);
    check("reopening clears the previous filename caption",
      reopened.info.trim() === "",
      `still reads ${JSON.stringify(reopened.info.trim().slice(0, 60))}`);
    check("reopening leaves submit disabled until a photo is chosen",
      reopened.submitDisabled === true,
      `disabled=${reopened.submitDisabled} (was ${afterPick.submitDisabled} with a file chosen)`);

    await page.evaluate(() => window.closeUploadModal && window.closeUploadModal());
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 240));
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\nOVERALL: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
