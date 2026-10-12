const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
const {JSDOM}=require('jsdom');
function setup(){
 const dom=new JSDOM('<div id="map"></div>',{url:'https://vas.test',runScripts:'outside-only'}),w=dom.window;
 class Marker{constructor(opts){this.node=opts.element;}setLngLat(coords){this.coords=coords;return this;}addTo(){w.document.getElementById('map').append(this.node);return this;}remove(){this.node.remove();}getElement(){return this.node;}}
 w.maplibregl={Marker,setWorkerUrl(){}};
 const code=fs.readFileSync('frontend/js/identity-map.js','utf8').replace(/^import .*;$/m,'').replace(/export /g,'');
 w.eval(code);
 const ctl=Object.create(w.IdentityMap.Controller.prototype);ctl.map={};ctl._cameraLabels=[];
 const camera=(id,name,coords)=>({type:'Feature',geometry:{type:'Point',coordinates:coords},properties:{pipeline_id:id,pipeline_name:name}});
 return {w,dom,ctl,camera};
}
test('real camera names stay readable, co-located cameras share a label, text is escaped',()=>{
 const h=setup();try{
 h.ctl._drawCameraLabels({features:[h.camera('c1','Lobby',[35.5,33.8]),h.camera('c2','Gate <img src=x onerror=alert(1)>',[35.5,33.8]),h.camera('c3','',[35.51,33.81]),h.camera('bad','Bad',[200,500])]});
 const labels=h.w.document.querySelectorAll('.ae-camera-label');assert.equal(labels.length,2);
 assert.equal(labels[0].querySelectorAll('.ae-camera-label-name').length,2);
 assert.match(labels[0].textContent,/Gate <img/);assert.equal(labels[0].querySelector('img'),null);
 assert.equal(labels[1].textContent,'c3');
 assert.equal(h.ctl._cameraLabels[0].ids.size,2);
 h.ctl._clearCameraLabels();assert.equal(h.w.document.querySelectorAll('.ae-camera-label').length,0);
 assert.equal(h.ctl._cameraLabels.length,0);
 }finally{h.dom.window.close();}
});
