const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('frontend/js/dashboard.js', 'utf8');
function setup(failResume = false) {
    const nodes = {};
    function node(id) { return nodes[id] ||= {classList: {toggle() {}}, querySelector() { return node(id + '-label'); }, setAttribute(key, value) {this[key] = value;}}; }
    class Audio {
        constructor() { this.state = 'suspended'; this.currentTime = 0; }
        addEventListener() {}
        async resume() { if (failResume) throw Error('blocked'); this.state = 'running'; }
        async suspend() { this.state = 'suspended'; }
        createOscillator() { return {connect(){}, frequency:{}, start(){}, stop(){}}; }
        createGain() {return {connect(){}, gain:{setValueAtTime(){},linearRampToValueAtTime(){},exponentialRampToValueAtTime(){}}};}
    }
    const context = vm.createContext({window:{AudioContext:Audio}, document:{getElementById:node}, localStorage:{setItem(){}}, trackTimeout(){}});
    vm.runInContext(source.slice(source.indexOf('    const SOUND_PREF_KEY'), source.indexOf('    // Background-task notifications')),context);
    return {context,nodes,run: code=>vm.runInContext(code,context)};
}
test('remembered preference activates on first click; suspended audio can retry; running audio mutes', async () => {
    const h = setup();
    h.run('soundEnabled = true; updateSoundButton()');
    assert.equal(h.run('soundState()'), 'blocked');
    await h.run('toggleSound()');
    assert.equal(h.run('soundState()'), 'enabled');
    assert.equal(h.nodes['sound-toggle-btn']['aria-pressed'], 'true');
    await h.run('audioContext.suspend()');
    await h.run('toggleSound()');
    assert.equal(h.run('soundState()'), 'enabled');
    await h.run('toggleSound()');
    assert.equal(h.run('soundState()'), 'disabled');
    assert.equal(h.nodes['sound-test-btn'].disabled, true);
});
test('blocked resume does not claim successful sound activation', async () => {
    const h = setup(true);
    await h.run('toggleSound()');
    assert.equal(h.run('soundState()'), 'blocked');
    assert.equal(h.nodes['sound-toggle-btn'].disabled, false);
    assert.equal(h.nodes['sound-toggle-btn']['aria-pressed'], 'false');
});
test('inbox treats legacy UTC and explicit UTC identically, never invents missing times', () => {
    const code = fs.readFileSync('frontend/js/detection-alert-inbox.js','utf8');
    const context = vm.createContext({});
    vm.runInContext(code.slice(code.indexOf('    function time('), code.indexOf('    class DetectionAlertInbox')),context);
    assert.equal(vm.runInContext("time('2026-09-24T09:00:00')",context),vm.runInContext("time('2026-09-24T09:00:00Z')",context));
    assert.equal(vm.runInContext('time(null)',context),'Unavailable');
});
