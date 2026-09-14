const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const source = fs.readFileSync('frontend/js/admin-unknown.js', 'utf8');
const extract = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const functions = [
    extract('async function openAdvancedMergePreview()', '// Render merge preview'),
    extract('async function executeMergeFromPreview()', '// Close preview modal'),
    extract('async function fetchIdentityDetails(', '// Remove identity from selection'),
    extract('function removeFromSelection(', '// Open merge modal'),
    extract('async function reviewSuggestion(', '// Approve merge suggestion'),
].join('\n');
const nodes = new Map();
const node = id => { if (!nodes.has(id)) nodes.set(id, {value:'', style:{}, innerHTML:'Merge', disabled:false}); return nodes.get(id); };
const requests = [], errors = [], pending = [];
let countUpdates = 0;
const context = {
    console, document: {getElementById:node, querySelector:node},
    ModalStack:{open(){},close(){}},
    showNotification: message => errors.push(message),
    loadUnknownFaces(){}, updateMultiMergeForm(){}, syncSelectionCards(){},
    updateMergeMultipleButton(){countUpdates++;},
    openMergeModal(){}, loadPipelineMergeSuggestions(){},
    fetch: () => new Promise(resolve => pending.push(resolve)),
    postMergeWithRiskGate: async (url, payload) => {requests.push(payload);return null;},
    renderMergePreview: preview => { context.rendered = preview; },
};
vm.createContext(context);
vm.runInContext(`let selectedIdentities=new Set(['a','b']); let multiSelectMode=true;
let mergeSubmitInFlight=false, activeMergePreview=null, mergePreviewSequence=0;
let activeSuggestionPipeline=null, unknownLoadSequence=5;
const selectionKey=()=>Array.from(selectedIdentities).sort().join(',');
function invalidateMergePreview(){activeMergePreview=null;++mergePreviewSequence;}
${functions}`, context);
const run = code => vm.runInContext(code, context);
(async () => {
    const first = run('openAdvancedMergePreview()');
    const second = run('openAdvancedMergePreview()');
    pending[1]({ok:true,json:async()=>({target_identity:{id:'b'}})});
    await second;
    pending[0]({ok:true,json:async()=>({target_identity:{id:'a'}})});
    await first;
    assert.equal(context.rendered.target_identity.id,'b', 'old preview cannot overwrite newer preview');
    await run('executeMergeFromPreview()');
    assert.equal(requests[0].target_identity_id,'b', 'execute pins displayed target instead of auto-selecting again');
    run("selectedIdentities.delete('b')");
    await run('executeMergeFromPreview()');
    assert.equal(requests.length,1,'changed selection must not execute');
    assert.match(errors.at(-1), /Selection changed/);
    run("removeFromSelection('a')");
    assert.equal(countUpdates,1,'removal refreshes header/button');
    const details = run("fetchIdentityDetails('a')");
    pending[2]({ok:true,json:async()=>({id:'a'})}); await details;
    assert.equal(run('unknownLoadSequence'),5,'detail reads do not cancel list loads');
    run(`selectSuggestion('["a","b","c"]')`);
    assert.equal(run('selectionKey()'),'a,b,c');
    let release, reviews=0;
    context.action = async()=>{reviews++;await new Promise(resolve=>{release=resolve;});};
    const reviewing=run("reviewSuggestion(action,'1')");
    await run("reviewSuggestion(action,'1')");
    assert.equal(reviews,1,'double review is blocked during confirmation');
    release(); await reviewing;
    assert.equal(run('mergeSubmitInFlight'),false);
    console.log('PASS: latest preview wins, target pinned, changed selection blocked, counts updated, detail/list isolation, suggestion transfer, duplicate review guard. No API writes.');
})().catch(error=>{console.error(error);process.exitCode=1;});
