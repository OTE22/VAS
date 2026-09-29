// Pure service-scoped recommendations; no network or production mutations.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2] || 'frontend/js/admin-ml-ops.js', 'utf8');
function extract(name) {
    const start = source.indexOf('    function ' + name + '(');
    assert(start >= 0, name + ' must exist');
    const next = source.indexOf('\n    function ', start + 5);
    const asyncNext = source.indexOf('\n    async function ', start + 5);
    return source.slice(start, Math.min(...[next, asyncNext].filter(index => index >= 0)));
}
const functions = ['updateNextStep', 'workspaceStatus', 'serviceModels', 'selectedServiceContract', 'serviceStatus', 'compatibleDataset'].map(extract).join('\n');
const family = 'coappearance_anomaly_model';
const contract = {model_type: family, dataset_kind: 'unsupervised', dataset_definition: 'coappearance_pair', feature_set_version: 'coappearance-features-v1'};
const compatible = {status: 'built', kind: 'unsupervised', definition_name: 'coappearance_pair', feature_set_version: 'coappearance-features-v1', parquet_sha256: 'hash', file_present: true};
function fixture(overrides = {}) {
    const nodes = Object.fromEntries(['title', 'description', 'action'].map(key => ['mlops-next-step-' + key, {dataset: {}}]));
    const state = {selectedService: family, servicesState: 'ready', services: [{model_type: family}], modelTypes: [contract], evidence: {overview: 'ready', datasets: 'ready', models: 'ready'}, mlWorker: {status: 'healthy'}, consoleStatus: 'online', jobPollFailures: 0, activeJobs: new Map(), currentMode: 'rules', models: [], datasets: [], ...overrides};
    const context = vm.createContext({state, getElement: id => nodes[id], toText: (v, fallback) => v == null ? fallback : String(v), updateRunbook() {}});
    vm.runInContext(functions + '\nupdateNextStep();', context);
    return {state, nodes, context, title: nodes['mlops-next-step-title'].textContent, action: nodes['mlops-next-step-action']};
}
assert.equal(fixture().action.dataset.journeyStage, 'prepare');
assert.equal(fixture({mlWorker: null}).action.disabled, true);
assert.equal(fixture({servicesState: 'loading'}).action.disabled, true);
assert.match(fixture({servicesState: 'error'}).title, /Check service health/);
assert.match(fixture({jobPollFailures: 1}).title, /Check service health/);
assert.match(fixture({evidence: {overview: 'ready', models: 'error', datasets: 'ready'}}).title, /missing evidence/);
assert.match(fixture({activeJobs: new Map([['job', {}]])}).title, /in progress/);
assert.equal(fixture({models: [{model_type: family, stage: 'validated'}]}).action.dataset.journeyStage, 'test');
assert.equal(fixture({models: [{model_type: 'behavior_anomaly_model', stage: 'validated'}]}).action.dataset.journeyStage, 'prepare');
assert.equal(fixture({currentMode: 'shadow'}).action.dataset.journeyStage, 'prepare', 'Behavior shadow cannot advance the pair-model journey');
assert.match(fixture({datasets: [compatible]}).title, /compatible dataset/);
for (const mismatch of [{file_present: false}, {kind: 'supervised'}, {feature_set_version: 'secintel-features-v2'}, {definition_name: 'behavior_anomaly_person'}, {parquet_sha256: null}]) {
    const f = fixture({datasets: [{...compatible, ...mismatch}]});
    assert.doesNotMatch(f.title, /compatible dataset/);
    assert.equal(vm.runInContext('compatibleDataset(state.datasets[0], state.modelTypes[0])', f.context), false);
}
assert.equal(fixture({services: [{model_type: family, selected_model: {id: 'pair-v1'}}]}).action.dataset.journeyStage, 'monitor');
assert.equal(fixture({selectedService: 'behavior_anomaly_model', services: [{model_type: 'behavior_anomaly_model', selected_model: {id: 'behavior-v1'}, decision_mode: 'rules'}]}).action.dataset.journeyStage, 'activate');
assert.match(fixture({selectedService: 'tabular_regression_model'}).title, /offline numeric experiment/);
console.log('19 scoped readiness and dataset compatibility scenarios passed');

const policyFunctions = ['serviceConnectionBlockers', 'serviceTrainingBlocked'].map(extract).join('\n');
const policy = vm.createContext({}); vm.runInContext(policyFunctions, policy);
assert.equal(vm.runInContext('serviceConnectionBlockers(null).length', policy), 1);
assert.equal(vm.runInContext('serviceConnectionBlockers({}).length', policy), 1);
assert.equal(vm.runInContext('serviceConnectionBlockers({connection_blockers: []}).length', policy), 0);
assert.equal(vm.runInContext('serviceTrainingBlocked(null, "saved")', policy), true);
assert.equal(vm.runInContext('serviceTrainingBlocked({training_readiness:{blockers:[{code:"SOURCE_HISTORY_REQUIRED"}]}}, "saved")', policy), false);
assert.equal(vm.runInContext('serviceTrainingBlocked({training_readiness:{blockers:[{code:"INSUFFICIENT_REVIEWED_LABELS"}]}}, "saved")', policy), true);
assert.match(fixture({services:[{model_type:family,training_readiness:{blockers:[{code:'SOURCE_HISTORY_REQUIRED',message:'More camera history needed'}]}}]}).title,/data requirements/);
console.log('7 connection and prerequisite scenarios passed');
