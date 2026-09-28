const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');

function setup() {
    function node() {return {value:'',checked:false,disabled:false,style:{},dataset:{},attrs:{},children:[],
        classList:{add(){},remove(){},toggle(){}},setAttribute(k,v){this.attrs[k]=v},removeAttribute(k){delete this.attrs[k]},
        replaceChildren(...children){this.children=children;this.canvasAttached=false},append(...children){this.children.push(...children)},
        appendChild(child){this.children.push(child)},addEventListener(){},removeEventListener(){},querySelector(){return null}}}
    const nodes=new Map();
    const document={addEventListener(){},querySelectorAll(){return []},createElement:node,createTextNode:t=>t,
        getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)}};
    const window={addEventListener(){},location:{origin:'https://fixture.invalid'},requestAnimationFrame:cb=>cb(),IdentityMap:{ready:Promise.resolve({})}};
    const context=vm.createContext({window,document,console,URL,URLSearchParams,AbortController,Map,Set,Intl,Date});
    let source=fs.readFileSync(process.env.SECURITY_UI_SOURCE||'frontend/js/admin-security-intelligence.js','utf8');
    source=source.replace(/\}\)\(\);\s*$/, 'window.subject={state,selectorRegistry,loadMapView,switchTab}; })();');
    vm.runInContext(source,context);
    const subject=window.subject, container=document.getElementById('security-map');
    container.canvasAttached=true;
    document.getElementById('map-style-select').value='light';
    document.getElementById('map-days-back').value='7';
    subject.selectorRegistry.set('map-identity-id',{getValue:()=> 'fixture-identity'});
    const counts={loads:0,overlays:0,resize:0,destroy:0};
    const ctl={container,map:{},style:'light',flags:{},isStyleAvailable:()=>true,
        async load(){counts.loads++},_restoreOverlays(){counts.overlays++},resize(){counts.resize++},destroy(){counts.destroy++}};
    subject.state.mapController=ctl;
    return {subject,container,ctl,counts,nodes};
}

test('repeated Load Map preserves the canvas and reuses data for display-only changes', async()=>{
    const {subject,container,counts,nodes}=setup();
    await subject.loadMapView();
    assert.equal(container.canvasAttached,true);
    nodes.get('map-include-popups').checked=true;
    await subject.loadMapView();
    assert.equal(container.canvasAttached,true);
    assert.equal(counts.loads,1);
    assert.equal(counts.overlays,1);
    assert.equal(nodes.get('map-load-btn').disabled,false);
    assert.equal(container.attrs['aria-busy'],undefined);
});

test('failed map update destroys stale state so a retry creates a usable map', async()=>{
    const {subject,ctl,counts,nodes,container}=setup();
    ctl.load=async()=>{throw Object.assign(new Error('Unavailable'),{status:503})};
    await subject.loadMapView();
    assert.equal(counts.destroy,1);
    assert.equal(subject.state.mapController,null);
    assert.equal(subject.state.mapDataKey,null);
    assert.equal(nodes.get('map-load-btn').disabled,false);
    assert.equal(container.attrs['aria-busy'],undefined);
});

test('returning to the map tab resizes its canvas after becoming visible',()=>{
    const {subject,counts}=setup();
    subject.switchTab('network');
    subject.switchTab('map');
    assert.equal(counts.resize,1);
});
