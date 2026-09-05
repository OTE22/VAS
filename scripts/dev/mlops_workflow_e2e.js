/** Full real-page browser test with intercepted fixture APIs. No live requests. */
const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const { chromium } = require(process.env.PW_CORE || 'C:/Users/Raven/AppData/Roaming/npm/node_modules/n8n/node_modules/playwright-core');
const root = path.resolve(__dirname, '../..');
const did = '00000000-0000-0000-0000-000000000001';
const mid = '00000000-0000-0000-0000-000000000002';
const ds = { id: did, name: 'Fixture dataset', version: 1, kind: 'unsupervised', status: 'built', row_count: 40,
    checksum: 'fixture-checksum', parquet_sha256: 'fixture-bytes', feature_set_version: 'fixture-features',
    definition_name: 'fixture-source', quality_report: { passed: true, checks: {} }, extraction: {}, split_config: {}, manifest: {} };
const model = { id: mid, model_type: 'behavior_anomaly_model', algorithm: 'mad_baseline', version: 1, stage: 'validated',
    dataset_id: did, seed: 42, artifact_name: 'fixture.pkl', artifact_hash: 'fixture-model-hash', training_job_id: 'fixture-job',
    training_config: { algorithm: 'mad_baseline', seed: 42, preprocessor_version: 'fixture-preprocessor', feature_set_version: 'fixture-features', hyperparameters: {} },
    evaluation_report: { splits: { test: { rows: 8, score_p50: 0.2, score_p90: 0.8 } }, engineering_gate: { status: 'PASS' }, scientific_gate: { status: 'INSUFFICIENT_EVIDENCE' } } };
let jobs = [], models = [], explorerError = 0, denyEvidence = false, slowDataset = false;
const writes = [], errors = [];
let savedPipelines = [];
(async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
    try {
        const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
        const page = await context.newPage();
        page.on('pageerror', e => errors.push(String(e)));
        await page.route('**/*', async route => {
            const url = new URL(route.request().url()), p = url.pathname;
            const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
            if (p.startsWith('/api/')) {
                if (route.request().method() !== 'GET') {
                    writes.push({ path: p, body: route.request().postDataJSON() });
                    if (p === '/api/ml/pipelines') { const body = route.request().postDataJSON(); const saved = { ...body, id: '00000000-0000-0000-0000-000000000003', version: savedPipelines.length + 1 }; savedPipelines.unshift(saved); return reply(saved, 201); }
                    if (p.endsWith('/promote')) { model.stage = 'approved'; return reply(model); }
                    if (p === '/api/ml/training-jobs') {
                        jobs = [{ job_id: 'fixture-job', kind: 'training', status: 'running', progress_percent: 45,
                            started_at: new Date(Date.now() - 60000).toISOString(), details: { stage: 'training', dataset_id: did,
                                resource_usage: { memory_mb: 128, cpu_seconds: 3, scope: 'fixture' }, stage_history: [{ stage: 'preprocessing', duration_seconds: 2 }] } }];
                        return reply({ accepted: true, job_id: 'fixture-job', status: 'scheduled' });
                    }
                    if (p.endsWith('/shadow-approve')) { model.stage = 'shadow'; return reply({ model }); }
                    return reply({});
                }
                if (p === '/api/auth/me') return reply({ id: 1, username: 'fixture-admin', role: 'admin', is_active: true });
                if (p === '/api/auth/me/privileges') return reply({ role: 'admin', privileges: [], navbar_links: [] });
                if (p === '/api/ml/capabilities') return reply({ items: { mlflow: { status: 'Available', action: 'Ready' }, optuna: { status: 'Available', action: 'Ready' }, shap: { status: 'Available', action: 'Ready' }, xgboost: { status: 'Available', action: 'Ready' } }, limits: { optuna_trials: 30, optuna_timeout_seconds: 600 } });
                if (p === '/api/ml/pipelines') return reply({ items: savedPipelines });
                if (p.endsWith('/explanations')) return reply({ feature_names: ['x'], output_space: 'Fixture raw output', sample: { base_value: 2, contributions: [3], features: [4] } });
                if (p === '/api/ml/overview') return reply({ mode: { current_mode: 'rules', modes: [] }, model_types: [{ model_type: 'behavior_anomaly_model', trainable: true, algorithms: ['mad_baseline'], label: 'Behavior anomaly', serving_mode: 'shadow' }, { model_type: 'tabular_regression_model', trainable: true, dataset_kind: 'unsupervised', algorithms: ['xgboost_regressor'], default_algorithm: 'xgboost_regressor', serving_mode: 'offline_regression' }] });
                if (p === '/api/ml/datasets') return reply({ items: [ds], total: 1 });
                if (p === '/api/ml/models') return reply({ items: models, total: models.length });
                if (p === '/api/ml/jobs') return reply({ items: jobs, worker: { status: 'healthy' } });
                if (p === '/api/ml/models/' + mid) return reply(model);
                if (p === '/api/ml/datasets/' + did) {
                    if (slowDataset) await new Promise(resolve => setTimeout(resolve, 250));
                    return denyEvidence ? reply({ detail: { message: 'Permission denied' } }, 403) : reply(ds);
                }
                if (p.endsWith('/validation-report')) return reply({ dataset_id: did, version: 1, validation_report: ds.quality_report });
                if (p.endsWith('/explorer')) {
                    if (explorerError) return reply({ detail: { message: 'Fixture artifact unavailable' } }, explorerError);
                    const items = url.searchParams.get('q') === 'absent' ? [] : [{ entity_id: '<script>unsafe</script>', as_of: '2026-01-01', label: 'positive', split: 'train', features: { x: 1 } }];
                    return reply({ total_rows: 40, scanned_rows: 40, truncated: false, column_count: 8, feature_count: 2,
                        duplicates: 0, invalid_rows: 0, schema: [{ name: 'x', type: 'float' }], missing_values: { x: 1 },
                        class_distribution: { positive: 20, negative: 20 }, items, filtered_rows: items.length, page_size: 25 });
                }
                return reply({ items: [], total: 0, definitions: [], enabled: false });
            }
            const rel = p === '/admin/ml-ops' ? 'frontend/admin/ml-ops.html' : p.replace(/^\//, '');
            const file = path.resolve(root, rel);
            if (file.startsWith(root + path.sep) && fs.existsSync(file) && fs.statSync(file).isFile()) {
                const type = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.svg': 'image/svg+xml' }[path.extname(file)] || 'application/octet-stream';
                return route.fulfill({ contentType: type, body: fs.readFileSync(file) });
            }
            return route.fulfill({ status: 204 });
        });
        await page.goto('http://workflow.test/admin/ml-ops');
        await page.waitForFunction(() => document.querySelectorAll('#workflow-dataset option').length === 2);
        assert.equal(await page.locator('#workflow-pipeline button').count(), 7);
        await page.selectOption('#workflow-dataset', did);
        await page.getByRole('heading', { name: 'Sample records', exact: true }).waitFor();
        assert.match(await page.locator('#workflow-explorer-body').innerText(), /<script>unsafe<\/script>/);
        assert.equal(await page.locator('#workflow-explorer-body script').count(), 0);
        await page.getByLabel('Search sample records').fill('absent');
        await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
        await page.getByText('No records match these filters.', { exact: false }).waitFor();
        const downloadPromise = page.waitForEvent('download');
        await page.getByRole('button', { name: 'Download validation report', exact: true }).click();
        const download = await downloadPromise;
        assert.match(download.suggestedFilename(), /^validation-/);
        await page.locator('#workflow-pipeline button').nth(4).click();
        await page.getByRole('button', { name: 'Configure training', exact: true }).click();
        await page.locator('#start-training-btn').click();
        await page.waitForFunction(() => document.querySelectorAll('#workflow-job option').length === 2);
        assert.equal(writes[0].body.dataset_id, did);
        await page.selectOption('#workflow-job', 'fixture-job');
        await page.waitForFunction(() => document.querySelector('#workflow-progress').textContent.includes('45%'));
        assert.match(await page.locator('#workflow-progress').innerText(), /128/);
        jobs[0].status = 'failed'; jobs[0].error_message = 'Fixture training failed';
        await page.locator('[data-mlops-view="overview"]').click();
        await page.locator('#jobs-refresh-btn').click();
        await page.waitForFunction(() => document.querySelector('#workflow-progress').textContent.includes('Fixture training failed'));
        await page.locator('#workflow-pipeline button').nth(4).click();
        await page.getByRole('button', { name: 'Review configuration and retry training', exact: true }).waitFor();
        models = [model]; jobs[0] = { ...jobs[0], status: 'completed', progress_percent: 100, duration_seconds: 90, error_message: null, result: { model_id: mid, dataset_id: did } };
        await page.locator('#refresh-console-btn').click();
        await page.waitForFunction(() => document.querySelectorAll('#workflow-model option').length === 2);
        await page.selectOption('#workflow-model', mid);
        await page.waitForFunction(() => document.querySelector('#workflow-notice').textContent.includes('linked to this model'));
        await page.locator('#workflow-pipeline button').nth(5).click();
        await page.getByRole('heading', { name: 'Results dashboard', exact: true }).waitFor();
        assert.match(await page.locator('#workflow-stage').innerText(), /0.8000|0.8/);
        await page.locator('#workflow-pipeline button').nth(6).click();
        assert.equal(await page.locator('.workflow-lineage > li').count(), 6);
        assert.match(await page.locator('#workflow-stage').innerText(), /INSUFFICIENT_EVIDENCE/);
        await page.getByRole('button', { name: 'Review registration and deployment controls', exact: true }).click();
        await page.getByRole('button', { name: 'Approve for SHADOW (observation only)', exact: true }).click();
        await page.locator('#registry-action-confirm').click();
        assert.equal(writes.length, 1, 'Missing reason must not submit approval');
        await page.locator('#registry-action-reason').fill('Fixture review approval');
        await page.locator('#registry-action-confirm').click();
        await page.waitForFunction(() => document.querySelector('#workflow-stage').textContent.includes('Registry: shadow'));
        await page.locator('#workflow-summary-button').click();
        assert.match(await page.locator('#workflow-summary').innerText(), /What data was used/);
        assert.match(await page.locator('#workflow-summary').innerText(), /90th percentile score/);
        if (process.env.WORKFLOW_SCREENSHOT) {
            await page.setViewportSize({ width: 1440, height: 1200 });
            await page.evaluate(() => document.querySelector('.intelligence-content').scrollTop = 0);
            await page.screenshot({ path: process.env.WORKFLOW_SCREENSHOT });
        }
        await page.getByRole('button', { name: 'Return to selected stage', exact: true }).click();
        // Exercise optional measured-report shapes independently of the absent-history state.
        Object.assign(model.evaluation_report, {
            confusion_matrix: [[8, 2], [1, 9]], confusion_matrix_context: { labels: ['negative', 'positive'], threshold: 0.5, meaning: 'Fixture diagnostic only' },
            feature_importance: { x: 0.7, y: 0.3 }, feature_importance_method: 'Fixture importance',
            training_curves: [{ iteration: 1, training_loss: 0.8 }, { iteration: 2, training_loss: 0.4 }], training_curves_context: 'Fixture loss',
            incumbent_comparison: { status: 'computed', incumbent: { algorithm: 'mad_baseline' }, note: 'Same held-out records',
                splits: { test: { latency_ms_per_row: { candidate: 2, incumbent: 3 }, score_quantiles: { candidate: { p90: 0.8 }, incumbent: { p90: 0.7 } } } } }
        });
        await page.locator('#workflow-pipeline button').nth(5).click();
        await page.locator('#workflow-refresh').click();
        await page.getByRole('img', { name: 'Training loss by iteration; exact values in the table below.' }).waitFor();
        assert.equal(await page.locator('.workflow-result meter').count(), 2);
        assert.match(await page.locator('.workflow-results').innerText(), /negative|positive/);
        assert.match(await page.locator('.workflow-results').innerText(), /Candidate.*Incumbent/si);
        await page.locator('#workflow-pipeline button').nth(0).click();
        explorerError = 500;
        await page.getByRole('button', { name: 'Apply filters', exact: true }).click();
        await page.getByRole('button', { name: 'Retry loading records', exact: true }).waitFor();
        explorerError = 0;
        await page.getByRole('button', { name: 'Retry loading records', exact: true }).click();
        await page.getByRole('heading', { name: 'Sample records', exact: true }).waitFor();
        denyEvidence = true;
        await page.locator('#workflow-refresh').click();
        await page.waitForFunction(() => /Permission denied/.test(document.querySelector('#workflow-notice').textContent));
        assert.equal(await page.locator('.workflow-lineage').count(), 0);
        denyEvidence = false;
        slowDataset = true;
        await page.selectOption('#workflow-dataset', did);
        await page.selectOption('#workflow-dataset', '');
        await page.waitForTimeout(400);
        assert.match(await page.locator('#workflow-stage').innerText(), /Choose a dataset/);
        await page.setViewportSize({ width: 390, height: 844 });
        await page.locator('#workflow-pipeline button').nth(1).focus();
        await page.keyboard.press('Enter');
        assert.equal(await page.locator('#workflow-pipeline button').nth(1).getAttribute('aria-pressed'), 'true');
        assert.equal(await page.evaluate(() => document.querySelector('.mlops-workflow').scrollWidth <= document.querySelector('.mlops-workflow').clientWidth + 1), true);
        assert.deepEqual(errors, []);
        assert.deepEqual(writes.map(w => w.path), ['/api/ml/training-jobs', '/api/ml/models/' + mid + '/shadow-approve']);
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page.locator('#workflow-pipeline button').nth(4).click();
        await page.getByRole('button', { name: 'Configure training', exact: true }).click();
        await page.selectOption('#training-model-type', 'tabular_regression_model');
        await page.locator('#platform-refresh').click();
        await page.waitForFunction(() => !document.querySelector('#platform-optuna').disabled);
        await page.getByText('Save a reusable pipeline version', { exact: true }).click();
        await page.locator('#platform-name').fill('Fixture regression');
        await page.locator('#platform-target').fill('y');
        await page.locator('#platform-features').fill('x');
        await page.locator('#platform-metrics').fill('mae, rmse');
        await page.locator('#platform-save').click();
        await page.waitForFunction(() => document.querySelector('#platform-note').textContent.includes('Saved Fixture regression v1'));
        await page.selectOption('#training-dataset-select', did);
        await page.locator('#platform-optuna').check();
        await page.locator('#platform-shap').check();
        await page.locator('#platform-trials').fill('3');
        await page.locator('#start-training-btn').click();
        await page.waitForFunction(() => document.querySelector('#training-action-note').textContent.includes('Training scheduled'));
        const run = writes.filter(w => w.path === '/api/ml/training-jobs').at(-1).body;
        assert.equal(run.pipeline_id, savedPipelines[0].id);
        assert.equal(run.run_options.optuna.trials, 3); assert.equal(run.run_options.shap, true);
        model.model_type = 'tabular_regression_model'; model.algorithm = 'xgboost_regressor'; model.stage = 'validated';
        model.artifact_hash = 'a'.repeat(64);
        model.training_config.reproducibility = { dataset: { id: did, version: 1 }, seed: 42, git_commit: 'fixture' };
        model.tracking = { status: 'synchronized', run_id: 'fixture-mlflow-run', registered_name: 'fixture-model', registered_version: '1' };
        model.evaluation_report.shap = { status: 'completed', rows: 2, global_importance: { x: 0.7 }, exports: { 'shap.json': 'fixture' } };
        await page.locator('#workflow-refresh').click();
        await page.selectOption('#workflow-model', mid);
        await page.locator('#workflow-pipeline button').nth(5).click();
        await page.getByText('fixture-mlflow-run', { exact: true }).waitFor();
        assert.equal(await page.locator('.workflow-shap-bar meter').count(), 1);
        await page.getByRole('button', { name: 'Explain selected prediction', exact: true }).click();
        await page.getByText('Fixture raw output', { exact: false }).waitFor();
        const manifestDownload = page.waitForEvent('download');
        await page.getByRole('button', { name: 'Download reproducibility manifest', exact: true }).click();
        assert.match((await manifestDownload).suggestedFilename(), /^reproducibility-/);
        await page.getByRole('button', { name: 'Approve artifact for offline use', exact: true }).click();
        await page.locator('#registry-action-reason').fill('Reviewed fixture regression');
        await page.locator('#registry-action-confirm').click();
        await page.waitForFunction(() => document.querySelector('#workflow-stage').textContent.includes('approved'));
        assert.equal(writes.at(-1).body.artifact_checksum, 'a'.repeat(64));
        assert.deepEqual(errors, []);
        console.log('PASS: full-page dataset → validation → configuration → training → failure/retry guidance → evaluation → lineage/summary; downloads, errors, permission denial, stale responses, mobile and keyboard. Pipeline versioning, bounded Optuna, SHAP contributions/exports and checksum-bound offline approval also verified. All API requests intercepted; no user data touched.');
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
