const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/js/admin-unknown.js', 'utf8');
const helper = source.slice(source.indexOf('async function loadPipelinePages('), source.indexOf('let unknownLoadSequence = 0;'));
function harness(fetch, pipelinePages = new Map()) {
    const context = vm.createContext({fetch, pipelinePages, URLSearchParams});
    vm.runInContext(helper, context);
    return context.loadPipelinePages;
}
test('a busy pipeline does not crowd out another pipeline; shared identities stay on their own pages', async () => {
    const calls = [];
    const load = harness(async url => {
        const q = new URL(url, 'http://localhost').searchParams;
        calls.push(q);
        const id = q.get('pipeline_id');
        return {ok: true, json: async () => ({total: 40, total_pages: 2,
            identities: [{id: id === 'busy' ? 'shared' : 'previously-missing', pipeline_ids: ['busy', 'quiet']}]})};
    }, new Map([['busy', 2]]));
    const data = {pipeline_totals: {busy: 40, quiet: 40}, identities: [{id: 'global-page-only'}]};
    await load(data, new URLSearchParams({page_size: 20, show_all: 'true', date_from: '2026-01-01'}), () => true);
    assert.deepEqual(Array.from(data.identities, i => i.id), ['shared', 'previously-missing']);
    assert.deepEqual(Array.from(data.identities, i => Array.from(i.pipeline_ids)), [['busy'], ['quiet']]);
    assert.equal(calls[0].get('page'), '2');
    assert.equal(calls[1].get('page'), '1');
    for (const q of calls) {
        assert.equal(q.get('page_size'), '20');
        assert.equal(q.get('show_all'), 'true');
        assert.equal(q.get('date_from'), '2026-01-01');
    }
});
test('shrinking results clamp only the affected camera page', async () => {
    const pages = [];
    const load = harness(async url => {
        const page = new URL(url, 'http://localhost').searchParams.get('page');
        pages.push(page);
        return {ok: true, json: async () => ({total: 1, total_pages: 1, identities: page === '1' ? [{id: 'remaining'}] : []})};
    }, new Map([['camera', 3]]));
    const data = {pipeline_totals: {camera: 1}};
    await load(data, new URLSearchParams(), () => true);
    assert.deepEqual(pages, ['3', '1']);
    assert.equal(data.pipeline_pages[0].page, 1);
    assert.equal(data.identities[0].id, 'remaining');
});
test('stale requests cannot replace the current results', async () => {
    let current = true;
    const load = harness(async () => {
        current = false;
        return {ok: true, json: async () => ({identities: [{id: 'stale'}]})};
    });
    const data = {pipeline_totals: {camera: 1}, identities: []};
    await load(data, new URLSearchParams(), () => current);
    assert.equal(data.identities.length, 0);
    assert.equal(data.pipeline_pages, undefined);
});
test('pipeline load failures are reported instead of silently hiding identities', async () => {
    const load = harness(async () => ({ok: false}));
    await assert.rejects(load({pipeline_totals: {camera: 1}}, new URLSearchParams(), () => true), /Failed to load pipeline/);
});
test('missing count metadata reports an error instead of displaying an empty list', async () => {
    const load = harness(async () => { throw new Error('unexpected request'); });
    await assert.rejects(load({identities: [{id: 'existing'}]}, new URLSearchParams(), () => true), /Pipeline counts unavailable/);
});
test('embedding-only identities render without inventing a camera sighting', () => {
    const start = source.indexOf('        const groupedByPipeline = {};');
    const end = source.indexOf('        // Store all pipeline groups', start);
    const context = vm.createContext({
        currentFilters: {pipeline_id: 'camera'},
        unknownIdentities: [
            {id: 'legacy', pipeline_ids: ['camera'], pipeline_evidence_only: true, camera_events: {}},
            {id: 'seen', pipeline_ids: ['camera'], camera_events: {camera: {
                timestamp: '2026-01-01T12:00:00Z', appearances_count: 1
            }}}
        ]
    });
    vm.runInContext(source.slice(start, end) + '\nthis.groups = groupedByPipeline;', context);
    assert.equal(context.groups.camera.length, 2);
    const fallback = context.groups.camera[0];
    assert.equal(fallback.pipeline_evidence_only, true);
    assert.equal(fallback.appearances_count, 0);
    assert.equal(fallback.pipeline_id, 'camera');
    assert.equal(context.groups.camera[1].last_seen_at, '2026-01-01T12:00:00Z');
});
