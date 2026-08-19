/**
 * Settings save probe — headless Chrome, the real page, the real Save button.
 *
 *   node scripts/dev/settings_save_probe.js [--base http://localhost] [--key KEY]
 *
 * The reported failure was "Error updating setting: CSRF check failed:
 * X-Requested-With header required" on every save. The backend guard exempts
 * bearer-token callers, so no API-level test could see it — only a browser
 * driving the page can. This opens a setting, saves it back to the value it
 * already has, and asserts the PUT returned 200 rather than 403.
 *
 * playwright-core is resolved the same way as browser_smoke.js.
 */
const PW_CORE = process.env.PW_CORE ||
  "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW_CORE);

const arg = (name, fallback) => {
  const i = process.argv.indexOf(name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
};
const BASE = arg("--base", "http://localhost");
const USER = process.env.SMOKE_USER || "admin";
const PASS = process.env.SMOKE_PASS || "admin123";

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newContext().then((c) => c.newPage());

  const puts = [];
  page.on("response", async (res) => {
    const req = res.request();
    if (req.method() === "PUT" && res.url().includes("/api/settings/")) {
      let body = "";
      try { body = (await res.text()).slice(0, 200); } catch (e) { /* stream gone */ }
      puts.push({ status: res.status(), url: res.url(), body,
                  sent: req.headers()["x-requested-with"] || null });
    }
  });

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER);
    await page.fill("#password", PASS);
    await Promise.all([
      page.waitForNavigation({ waitUntil: "domcontentloaded", timeout: 30000 }),
      page.click('button[type="submit"]'),
    ]);

    await page.goto(`${BASE}/admin/settings`, { waitUntil: "networkidle" });
    await page.waitForTimeout(2000);

    // Open the first editable setting the page offers.
    const editor = page.locator(".setting-card .setting-btn.edit").first();
    const count = await editor.count();
    check("an editable setting is offered", count > 0, `${count} control(s)`);
    if (!count) throw new Error("no edit control on the settings page");

    await editor.click();
    await page.waitForTimeout(1200);

    // Save it back unchanged: this exercises the write path without moving
    // any deployment-wide value.
    const save = page.locator('#setting-form button[type="submit"]').first();
    check("the save control is present", (await save.count()) > 0);
    await save.click();
    await page.waitForTimeout(3000);

    check("a PUT to /api/settings was issued", puts.length > 0,
      puts.length ? "" : "the form never reached the API");
    if (puts.length) {
      const put = puts[puts.length - 1];
      check("the page sent X-Requested-With", put.sent === "XMLHttpRequest",
        `header=${put.sent}`);
      check("the save was NOT refused by CSRF", put.status !== 403,
        `status=${put.status} ${put.status === 403 ? put.body : ""}`);
      check("the save succeeded", put.status === 200,
        `status=${put.status}${put.status === 200 ? "" : " " + put.body}`);
    }
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 300));
  } finally {
    await browser.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\nOVERALL: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
