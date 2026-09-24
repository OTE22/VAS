// Exercise readiness recommendations without a browser or production mutations.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2] || 'frontend/js/admin-ml-ops.js', 'utf8');
const next = source.slice(source.indexOf('    function updateNextStep()'), source.indexOf('    function stopJobPolling()'));
const status = source.slice(source.indexOf('    function workspaceStatus('), source.indexOf('    function updateRunbook('));
function fixture(overrides = {}) {
    const nodes = Object.fromEntries(['title', 'description', 'action'].map(key => ['mlops-next-step-' + key, {dataset: {}}]));
    const state = {evidence: {overview: 'ready', datasets: 'ready', models: 'ready'}, mlWorker: {status: 'healthy'}, consoleStatus: 'online', jobPollFailures: 0, activeJobs: new Map(), currentMode: 'rules', models: [], datasets: [], featureSnapshots: 0, ...overrides};
    const context = vm.createContext({state, getElement: id => nodes[id], toText: (v, fallback) => v == null ? fallback : String(v), updateRunbook() {}});
    vm.runInContext(next + status + '\nupdateNextStep();', context);
    return {state, nodes, context, title: nodes['mlops-next-step-title'].textContent, action: nodes['mlops-next-step-action']};
}
assert.equal(fixture().action.dataset.nextTarget, 'compute-features-btn');
let f = fixture({mlWorker: null});
assert.equal(f.action.disabled, true);
assert.equal(vm.runInContext("workspaceStatus('prepare').text", f.context), 'Checking readiness');
f = fixture({jobPollFailures: 1});
assert.match(f.title, /Check service health/);
assert.match(vm.runInContext("workspaceStatus('prepare').text", f.context), /unavailable/);
assert.match(fixture({evidence: {overview: 'ready', models: 'error', datasets: 'ready'}}).title, /missing evidence/);
assert.match(fixture({activeJobs: new Map([['job', {}]])}).title, /in progress/);
assert.equal(fixture({models: [{stage: 'validated'}]}).action.dataset.openMlopsView, 'review');
assert.equal(fixture({currentMode: 'shadow'}).action.dataset.openMlopsView, 'monitor');
assert.equal(fixture({datasets: [{status: 'built', parquet_sha256: 'hash'}]}).action.dataset.nextTarget, 'training-dataset-select');
assert.match(fixture({datasets: [{status: 'built', file_present: false}]}).title, /Check your saved dataset/);
assert.equal(fixture({featureSnapshots: 100}).action.dataset.nextTarget, 'build-dataset-btn');
console.log('10 readiness scenarios passed');
