/**
 * ML-Ops page probe for the dataset/training lineage work (mlops-3).
 *
 *   node scripts/dev/mlops_lineage_probe.js [--base http://localhost]
 *
 * Drives the real page: the dataset definition dropdown and the feature-set
 * limitation note come from the backend; the datasets list shows extraction
 * audit + both hashes and opens a detail drawer; the training form offers
 * built datasets as a picker plus seed/hyperparameters; model detail shows
 * training_config / code version / artifact presence. Read-only: nothing is
 * built or trained.
 */
const PW_CORE = process.env.PW_CORE ||
  "C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core";
const CHROME = process.env.CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const { chromium } = require(PW_CORE);

const arg = (n, d) => { const i = process.argv.indexOf(n); return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : d; };
const BASE = arg("--base", "http://localhost");
const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newContext({ viewport: { width: 1600, height: 1000 } }).then(c => c.newPage());
  const errors = [];
  page.on("pageerror", e => errors.push(String(e).slice(0, 160)));
  page.on("console", m => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text().slice(0, 160)); });
  try {
    await page.goto(`${BASE}/signin`, { waitUntil: "networkidle" });
    await page.fill("#username", process.env.SMOKE_USER || "admin");
    await page.fill("#password", process.env.SMOKE_PASS || "admin123");
    await Promise.all([page.waitForNavigation({ waitUntil: "domcontentloaded" }), page.click('button[type="submit"]')]);
    // let the landing page's navbar finish its auth fetches before leaving it
    // (navigating mid-fetch logs a spurious "[Navbar] Failed to get privileges")
    await page.waitForLoadState("networkidle");
    await page.goto(`${BASE}/admin/ml-ops`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.querySelectorAll("#dataset-definition-select option").length > 1, { timeout: 30000 });
    await page.waitForFunction(() => /datasets \(/.test(document.getElementById("datasets-body").textContent), { timeout: 30000 });
    await page.waitForTimeout(1500);

    const form = await page.evaluate(() => ({
      definitions: [...document.querySelectorAll("#dataset-definition-select option")].map(o => o.textContent),
      note: document.getElementById("dataset-definition-note").textContent,
      policies: [...document.querySelectorAll("#dataset-sampling-policy option")].map(o => o.value),
      trainingDatasets: [...document.querySelectorAll("#training-dataset-select option")].map(o => o.textContent),
      seed: !!document.getElementById("training-seed-input"),
      hp: !!document.getElementById("training-hyperparameters-input"),
      rows: [...document.querySelectorAll("#datasets-body .mlops-dataset-row")].map(r => r.textContent),
    }));
    check("definition dropdown is populated from the backend", form.definitions.length >= 3 && /behavior_anomaly_person v1/.test(form.definitions.join("|")), form.definitions.slice(1).join(" ; "));
    check("feature-set limitations note is rendered (from backend)", /is_unknown_identity/.test(form.note) && /REQUIRES_VALIDATION/.test(form.note), form.note.slice(0, 120));
    check("cap policy choices are explicit (refuse default, newest, oldest)", form.policies.join(",") === ",newest_first,oldest_first");
    check("training form offers an existing-dataset picker + seed + hyperparameters", form.trainingDatasets.length >= 1 && form.seed && form.hp, `${form.trainingDatasets.length} option(s)`);
    check("dataset rows show extraction audit and both hashes", form.rows.length > 0 && form.rows.every(r => /excluded/.test(r) && /rows#/.test(r) && /bytes#/.test(r)), form.rows[0] ? form.rows[0].slice(0, 140) : "no rows");
    check("legacy datasets are labelled as such, never rewritten", form.rows.some(r => /legacy build|legacy-oldest-first-cap-v0/.test(r)) || form.rows.length === 0, "");

    // open the first dataset detail
    await page.click("#datasets-body button[data-dataset-id]");
    await page.waitForFunction(() => /Extraction policy/.test(document.getElementById("model-detail-body").textContent), { timeout: 20000 });
    const detail = await page.evaluate(() => document.getElementById("model-detail-body").textContent);
    check("dataset detail drawer states the split strategy (temporal_group entity isolation | temporal with measured overlap)", /Split strategy/.test(detail) && /(entity isolation|entities recur across splits)/.test(detail), (detail.match(/Split strategy.{0,120}/) || [""])[0]);
    check("build form offers a declared split strategy (definition default, temporal, temporal_group)", (await page.$$eval("#dataset-split-strategy option", o => o.map(x => x.value))).join(",") === ",temporal,temporal_group", "");
    check("dataset detail drawer shows policy, counts, hashes, limitations and models", /Candidate \/ selected \/ excluded/.test(detail) && /Parquet sha256/.test(detail) && /Feature-set limitations/.test(detail) && /Models trained from this dataset/.test(detail), detail.slice(0, 100));
    check("detail exposes no filesystem path", !/\/app\/|models\/ml|storage_path|manifest_path/.test(detail));

    // open the first model detail
    const hasModels = await page.evaluate(() => !!document.querySelector("#models-table-body button, #models-table-body tr"));
    if (hasModels) {
      const opened = await page.evaluate(() => {
        const btn = [...document.querySelectorAll("#models-table-body button")].find(b => /detail|view/i.test(b.textContent));
        if (btn) { btn.click(); return true; }
        return false;
      });
      if (opened) {
        await page.waitForFunction(() => /Artifact file present/.test(document.getElementById("model-detail-body").textContent), { timeout: 20000 });
        const mdetail = await page.evaluate(() => document.getElementById("model-detail-body").textContent);
        check("model detail shows code version, artifact presence and training config/lineage", /Code version/.test(mdetail) && /Artifact file present/.test(mdetail) && /Dataset checksum/.test(mdetail), mdetail.match(/Code version[^A]{0,30}/)?.[0]);
      } else {
        check("model detail opened", false, "no detail button found");
      }
    }
    // evidence report drawer (read-only) and the archive/verify controls
    await page.click("#shadow-evidence-btn");
    await page.waitForFunction(() => /Mapping decision/.test(document.getElementById("model-detail-body").textContent), { timeout: 20000 });
    const evidence = await page.evaluate(() => document.getElementById("model-detail-body").textContent);
    check("shadow evidence drawer renders with REQUIRES_VALIDATION and no delta", /REQUIRES_VALIDATION/.test(evidence) && !/score_delta|score_diff|rules_ml/i.test(evidence), evidence.slice(0, 80));
    const controls = await page.evaluate(() => ({
      archive: document.querySelectorAll("#datasets-body button[data-archive-dataset-id]").length,
      verify: !!document.getElementById("backfill-dataset-hashes-btn"),
      rowsLegacyUnverified: [...document.querySelectorAll("#datasets-body .mlops-dataset-row")].filter(r => /bytes#N\/A/.test(r.textContent) && / · built · /.test(r.textContent)).length,
    }));
    check("built datasets offer an explicit Archive control (server refuses referenced ones)", controls.archive > 0, `${controls.archive} archive button(s)`);
    check("verify-legacy control appears only while unverified legacy datasets exist", controls.verify === (controls.rowsLegacyUnverified > 0), `unverified=${controls.rowsLegacyUnverified} button=${controls.verify}`);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    check("no horizontal overflow at 1600px", !overflow);
    check("zero console/page errors", errors.length === 0, errors.slice(0, 2).join(" | ") || "clean");
  } catch (err) {
    check("probe completed", false, String(err).slice(0, 240));
  } finally {
    await browser.close();
  }
  const failed = results.filter(r => !r.ok);
  console.log(`\nOVERALL: ${failed.length ? "FAIL" : "PASS"} (${results.length - failed.length}/${results.length})`);
  process.exit(failed.length ? 1 : 0);
})();
