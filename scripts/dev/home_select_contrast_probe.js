/**
 * Home-page pipeline dropdown contrast probe — headless Chrome.
 *
 *   node scripts/dev/home_select_contrast_probe.js [--base http://localhost]
 *
 * The option list is drawn by the OS in its own layer, so a TRANSLUCENT
 * background on <option> composites onto the system's light popup rather than
 * onto the page. `--hp-surface-2` is rgba(255,255,255,.060) and `--hp-text` is
 * #e8f1ec, so the pipeline names came out near-white on near-white.
 *
 * This reads the COMPUTED styles the browser actually resolved and checks the
 * contrast ratio, which is what a screenshot could only suggest.
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

  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", USER);
    await page.fill("#password", PASS);
    await Promise.all([
      page.waitForNavigation({ waitUntil: "domcontentloaded", timeout: 30000 }),
      page.click('button[type="submit"]'),
    ]);

    await page.goto(`${BASE}/home`, { waitUntil: "networkidle" });
    await page.waitForSelector("#pipeline-select", { timeout: 20000 });
    await page.waitForTimeout(1500);

    const probe = await page.evaluate(() => {
      const sel = document.getElementById("pipeline-select");
      const opts = Array.from(sel.options);
      const real = opts.filter((o) => o.value);
      const target = real[0] || opts[0];
      if (!target) return null;
      const cs = getComputedStyle(target);
      const selCs = getComputedStyle(sel);
      return {
        optionCount: opts.length,
        realCount: real.length,
        label: (target.textContent || "").trim().slice(0, 40),
        bg: cs.backgroundColor,
        fg: cs.color,
        colorScheme: selCs.colorScheme,
      };
    });

    if (!probe) {
      check("the dropdown has options", false, "no <option> elements at all");
    } else {
      check("the dropdown lists pipelines", probe.realCount > 0,
        `${probe.realCount} pipeline(s), e.g. "${probe.label}"`);

      const parse = (c) => (c.match(/[\d.]+/g) || []).map(Number);
      const [br, bgc, bb, ba = 1] = parse(probe.bg);
      const [fr, fg2, fb] = parse(probe.fg);

      check("the option background is OPAQUE", ba === 1,
        `${probe.bg} (alpha ${ba}) — a translucent colour falls back to the ` +
        `system's light popup`);

      const lum = (r, g, b) => {
        const f = (v) => {
          v /= 255;
          return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
        };
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
      };
      const l1 = lum(br, bgc, bb);
      const l2 = lum(fr, fg2, fb);
      const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
      check("option text meets WCAG AA (4.5:1)", ratio >= 4.5,
        `${ratio.toFixed(1)}:1  bg=${probe.bg} fg=${probe.fg}`);

      check("the control declares a dark color-scheme",
        /dark/.test(probe.colorScheme || ""), `color-scheme: ${probe.colorScheme}`);
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
