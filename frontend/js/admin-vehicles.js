/* Vehicle review keeps the modal open across decisions and navigation. */
'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {page:1, knownPage:1, rows:[], total:0, knownTotal:0, reviewIndex:0, review:null, known:null, references:[], imageMode:false, imageUrl:null, request:0, knownRequest:0, reviewRequest:0, knownModalRequest:0, knownBusy:false, busy:false, pipelines:[]};
const timeLabel = value => value ? new Date(value).toLocaleString() : '—';
const stateLabel = value => ({pending:'Needs review',confirmed:'Operator confirmed',rejected:'Association rejected',unreadable:'Plate unreadable',processing:'Processing',failed:'Processing needs retry'})[value] || value;

async function api(path, options={}) {
  const headers = {'X-Requested-With':'XMLHttpRequest', ...(options.headers || {})};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api/vehicles' + path, {...options, headers, credentials:'include'});
  if (!response.ok) {
    let text = 'Request failed (' + response.status + ').';
    try { const data = await response.json(); text = typeof data.detail === 'string' ? data.detail : text; } catch (_) { /* keep status */ }
    throw new Error(text);
  }
  return response.json();
}
function message(text, error=false, target='#vehicle-message') {
  const element = $(target); element.textContent = text; element.classList.toggle('error',error); element.hidden = !text;
}
function filters() {
  const query = new URLSearchParams();
  for (const [key,value] of new FormData($('#vehicle-filters'))) {
    if (!value) continue;
    query.set(key, ['start','end'].includes(key) ? new Date(value).toISOString() : value);
  }
  return query;
}
function cameraName(id) {return state.pipelines.find(p=>p.pipeline_id===id)?.name || id;}
function empty(title, text) {return `<div class="empty-state"><h3>${escapeHTML(title)}</h3><p>${escapeHTML(text)}</p></div>`;}
function sightingCard(row) {
  return `<button type="button" class="vehicle-card" data-sighting="${escapeHTML(row.id)}"><img loading="lazy" src="${escapeHTML(row.image_url)}" alt="Vehicle captured ${escapeHTML(timeLabel(row.captured_at))}"><span class="card-state ${row.review_state === 'confirmed' ? 'confirmed' : ''}">${escapeHTML(stateLabel(row.review_state))}</span><div class="card-body"><span class="plate-label">${escapeHTML(row.plate || 'No readable plate')}</span><h3>${escapeHTML(row.known_vehicle_name || cameraName(row.pipeline_id))}</h3><p>${escapeHTML(timeLabel(row.captured_at))}</p><p>${escapeHTML(row.vehicle_type)} · ${escapeHTML(row.country)}${row.visual_similarity !== undefined ? ' · Visual similarity '+Math.round(row.visual_similarity*100)+'%' : ''}</p>${row.processing_state !== 'done' ? `<p>${escapeHTML(stateLabel(row.processing_state))}</p>`:''}</div></button>`;
}
function bindCards() {
  $$('[data-sighting]').forEach(button=>button.addEventListener('click',()=>openReview(state.rows.findIndex(r=>r.id===button.dataset.sighting))));
}
function renderSightings() {
  $('#sighting-results').innerHTML = state.rows.length ? `<div class="vehicle-grid">${state.rows.map(sightingCard).join('')}</div>` : empty('No sightings in this view','Adjust the date, plate, or camera filters. Unreadable plates still appear as vehicle evidence.');
  bindCards();
  $('#sighting-page').textContent = `${state.total} sightings · Page ${state.page} of ${Math.max(1,Math.ceil(state.total/24))}`;
  $('#sighting-prev').disabled = state.page <= 1;
  $('#sighting-next').disabled = state.page*24 >= state.total;
}
async function loadSightings() {
  const ticket = ++state.request;
  const query = filters(); query.set('page',state.page);
  $('#sighting-results').setAttribute('aria-busy','true');
  try {
    const data = await api('/sightings?' + query);
    if (ticket !== state.request) return;
    state.rows = data.items; state.total = data.total;
    const labels = [['Matching sightings','total'],['Operator confirmed','confirmed'],['Awaiting review','pending'],['Unreadable plates','unreadable']];
    $('#vehicle-stats').innerHTML = labels.map(([name,key])=>`<div><span>${name}</span><strong>${data.stats[key]}</strong></div>`).join('');
    renderSightings();
  } catch (error) {message(error.message,true);} finally {if(ticket===state.request) $('#sighting-results').removeAttribute('aria-busy');}
}
async function loadKnown() {
  const ticket=++state.knownRequest;
  const form = new FormData($('#known-search'));
  try {
    const data = await api('/known?' + new URLSearchParams({q:form.get('q') || '', archived:form.has('archived'), page:state.knownPage}));
    if(ticket!==state.knownRequest) return;
    state.knownTotal = data.total;
    $('#known-results').innerHTML = data.items.length ? data.items.map(v=>`<button type="button" class="vehicle-card" data-known="${v.id}">${v.photo_url ? `<img loading="lazy" src="${v.photo_url}?v=${v.version}" alt="${escapeHTML(v.name)}">` : '<span class="no-photo">No reference photo</span>'}<div class="card-body"><span class="plate-label">${escapeHTML(v.plate)}</span><h3>${escapeHTML(v.name)}</h3><p>${escapeHTML([v.make,v.model,v.year].filter(Boolean).join(' · ') || 'Specifications not added')}</p><p>${escapeHTML(v.color || 'Color not added')} · ${escapeHTML(v.country)}</p></div></button>`).join('') : empty('Build your reference register','Add a known car with its plate, specifications, and a clear reference photo.');
    $$('[data-known]').forEach(button=>button.addEventListener('click',()=>openKnown(button.dataset.known)));
    $('#known-page').textContent=`${data.total} references · Page ${state.knownPage}`;
    $('#known-prev').disabled=state.knownPage<=1; $('#known-next').disabled=state.knownPage*24>=data.total;
  } catch(error) {message(error.message,true);}
}
function showDialog(id) { const dialog=$('#'+id); if(!dialog.open) dialog.showModal(); }
function setReviewImage(kind, override=null) {
  const row=state.review; if(!row) return;
  const url=override || ({vehicle:row.image_url, plate:row.plate_url, scene:row.scene_url})[kind];
  if(!url) {message('No plate crop is available for this sighting.',false,'#review-message'); return;}
  $('#review-image').src=url; $('#review-image-link').href=url;
}
function setReferences(references, selected='') {
  state.references=references;
  $('#review-form select[name=known_vehicle_id]').innerHTML='<option value="">No reference association</option>'+references.map(v=>`<option value="${v.id}">${escapeHTML(v.name+' · '+v.plate)}</option>`).join('');
  $('#review-form select[name=known_vehicle_id]').value=selected;
  renderReference();
}
function renderReference() {
  const id=$('#review-form select[name=known_vehicle_id]').value;
  const reference=state.references.find(r=>r.id===id);
  $('#review-reference').innerHTML=reference ? `${reference.photo_url ? `<img src="${reference.photo_url}?v=${reference.version}" alt="Known car reference">`:''}<p class="small">${escapeHTML([reference.make,reference.model,reference.color].filter(Boolean).join(' · '))}</p>` : '';
}
async function openReview(index, keepMessage=false) {
  if(index<0 || index>=state.rows.length || state.busy) return;
  const ticket=++state.reviewRequest;
  state.reviewIndex=index; const id=state.rows[index].id;
  state.review=null; showDialog('sighting-dialog');
  $('#review-form').reset(); if(!keepMessage) message('',false,'#review-message');
  $('#sighting-title').textContent='Loading sighting…';
  $$('#review-form button').forEach(b=>b.disabled=true);
  try {
    const row=await api('/sightings/'+id);
    if(ticket!==state.reviewRequest) return;
    let references=row.reference_candidates;
    if(row.known_vehicle_id && !references.some(v=>v.id===row.known_vehicle_id)) references=[await api('/known/'+row.known_vehicle_id),...references];
    if(ticket!==state.reviewRequest) return;
    state.review=row;
    $('#sighting-title').textContent=row.plate || 'Vehicle without a readable plate';
    $('#review-meta').textContent=`${cameraName(row.pipeline_id)} · ${timeLabel(row.captured_at)} · ${row.vehicle_type}`;
    $('#review-state').textContent=stateLabel(row.review_state);
    $('#review-form input[name=plate]').value=row.plate || '';
    $('#review-form input[name=country]').value=row.country;
    setReferences(references,row.known_vehicle_id || ''); setReviewImage('vehicle');
    $('#retry-vehicle').hidden=row.processing_state!=='failed';
    $('#review-position').textContent=`${index+1} of ${state.rows.length} in this view`;
    $('#review-prev').disabled=index===0; $('#review-next').disabled=index===state.rows.length-1;
    $('#review-history').innerHTML=row.plate_candidates.map(p=>`<p>OCR: ${escapeHTML(p.lines.map(l=>l.text).join(' · '))}</p>`).join('')+row.history.map(h=>`<p>${escapeHTML(timeLabel(h.created_at))} · ${escapeHTML(h.action)}</p>`).join('');
    $('#plate-candidates').innerHTML=row.plate_candidates.map((p,i)=>`<button type="button" data-plate-candidate="${i}" aria-label="View plate candidate ${i+1}"><img src="${p.image_url}" alt="Plate candidate ${i+1}"></button>`).join('');
    $$('[data-plate-candidate]').forEach(b=>b.addEventListener('click',()=>setReviewImage('plate',row.plate_candidates[Number(b.dataset.plateCandidate)].image_url)));
    $('#review-filmstrip').innerHTML=state.rows.map((r,i)=>`<button type="button" data-review-index="${i}" aria-current="${i===index}" aria-label="Review sighting ${i+1}"><img src="${r.image_url}" alt="" loading="lazy"></button>`).join('');
    $$('[data-review-index]').forEach(b=>b.addEventListener('click',()=>openReview(Number(b.dataset.reviewIndex))));
  } catch(error) {if(ticket===state.reviewRequest) message(error.message,true,'#review-message');} finally {if(ticket===state.reviewRequest) $$('#review-form button').forEach(b=>b.disabled=!state.review);}
}
async function decide(decision) {
  if(!state.review || state.busy) return;
  state.busy=true; $$('#review-form button').forEach(b=>b.disabled=true);
  const form=new FormData($('#review-form'));
  const body={version:state.review.version,decision,plate:form.get('plate') || null,country:form.get('country'),known_vehicle_id:form.get('known_vehicle_id') || null,note:form.get('note')};
  try {
    const updated=await api('/sightings/'+state.review.id+'/review',{method:'POST',body:JSON.stringify(body)});
    state.rows[state.reviewIndex]={...state.rows[state.reviewIndex],...updated}; state.review={...state.review,...updated};
    message('Review saved. '+(state.reviewIndex+1<state.rows.length ? 'Next sighting is ready.' : 'You reached the end of this view. The review remains open.'),false,'#review-message');
    state.busy=false;
    await openReview(Math.min(state.reviewIndex+1,state.rows.length-1),true);
  } catch(error) {message(error.message,true,'#review-message');} finally {state.busy=false; $$('#review-form button').forEach(b=>b.disabled=false);}
}
async function openKnown(id=null) {
  if(state.knownBusy) return;
  const ticket=++state.knownModalRequest;
  $('#known-form button[type=submit]').disabled=Boolean(id);
  message('',false,'#known-message'); $('#known-form').reset(); state.known=null;
  $('#known-photo-preview').hidden=true; $('#person-section').hidden=true; showDialog('known-dialog');
  $('#known-title').textContent=id?'Loading reference…':'Add known car';
  try {
    if(id) {const record=await api('/known/'+id); if(ticket!==state.knownModalRequest) return; state.known=record; populateKnown();}
  } catch(error) {if(ticket===state.knownModalRequest) message(error.message,true,'#known-message');} finally {if(ticket===state.knownModalRequest) $('#known-form button[type=submit]').disabled=Boolean(id && !state.known);}
}
function populateKnown() {
  const v=state.known; if(!v) return;
  $('#known-title').textContent=v.name;
  for(const field of ['name','plate','country','make','model','color','year','notes']) $('#known-form [name='+field+']').value=v[field] ?? '';
  $('#known-form [name=archived]').checked=v.archived;
  $('#known-photo-preview').hidden=!v.photo_url;
  if(v.photo_url) $('#known-photo-preview').src=v.photo_url+'?v='+v.version;
  $('#person-section').hidden=false;
  $('#person-links').innerHTML=(v.people || []).map(p=>`<div class="person-link"><div><strong>${escapeHTML(p.name)}</strong><p>${escapeHTML(p.relationship_type.replaceAll('_',' '))} · ${p.active?'Reviewed association':'Revoked'}</p><p>${escapeHTML(p.evidence)}</p></div>${p.active?`<button type="button" data-revoke="${p.id}">Revoke</button>`:''}</div>`).join('') || '<p class="muted">No person associations recorded.</p>';
  $$('[data-revoke]').forEach(b=>b.addEventListener('click',async()=>{try {await api(`/known/${v.id}/people/${b.dataset.revoke}/revoke`,{method:'POST'}); state.known=await api('/known/'+v.id); populateKnown();} catch(error){message(error.message,true,'#known-message');}}));
}
async function saveKnown(event) {
  event.preventDefault(); if(state.knownBusy) return; state.knownBusy=true; const button=$('#known-form button[type=submit]'); button.disabled=true;
  try {
    const form=new FormData($('#known-form')); const body={};
    for(const key of ['name','plate','country','make','model','color','notes']) body[key]=form.get(key) || null;
    body.year=form.get('year') ? Number(form.get('year')) : null; body.archived=form.has('archived');
    if(state.known) body.version=state.known.version;
    const result=await api('/known'+(state.known?'/'+state.known.id:''),{method:state.known?'PUT':'POST',body:JSON.stringify(body)});
    state.known={...state.known,...result};
    const photo=form.get('photo');
    if(photo && photo.size) {
      const upload=new FormData(); upload.append('photo',photo); upload.append('version',state.known.version);
      state.known={...state.known,...await api('/known/'+state.known.id+'/photo',{method:'POST',body:upload})};
      $('#known-form [name=photo]').value='';
    }
    state.known=await api('/known/'+state.known.id); populateKnown(); message('Reference saved. You can add a reviewed person association below.',false,'#known-message'); await loadKnown();
  } catch(error) {message(error.message,true,'#known-message');} finally {state.knownBusy=false; button.disabled=false;}
}
async function imageSearch(file) {
  if(!file) return;
  if(file.size>20*1024*1024) {message('Choose an image up to 20 MB.',true); return;}
  const ticket=++state.request; const body=new FormData(); body.append('photo',file);
  const query=filters(); query.set('profile',$('#image-profile').value);
  message('Searching the vehicle image…'); $('#car-image-search').disabled=true;
  try {
    const data=await api('/search/image?'+query,{method:'POST',body}); if(ticket!==state.request) return;
    state.imageMode=true; state.rows=[...data.plate_matches,...data.possible_matches]; state.total=state.rows.length;
    if(state.imageUrl) URL.revokeObjectURL(state.imageUrl); state.imageUrl=URL.createObjectURL(file);
    $('#query-preview').hidden=false; $('#query-preview').innerHTML=`<img src="${state.imageUrl}" alt="Search reference car"><div><strong>${escapeHTML(file.name)}</strong><p class="muted small">${escapeHTML(data.note)}</p></div>`;
    $('#clear-image-search').hidden=false;
    $('#sighting-results').innerHTML=`<div class="result-heading"><h2>Plate-text matches</h2><span>OCR text agrees; review still required</span></div>${data.plate_matches.length?`<div class="vehicle-grid">${data.plate_matches.map(sightingCard).join('')}</div>`:empty('No plate-text matches','Appearance candidates are listed below, including sightings without readable plates.')}<div class="result-heading"><h2>Possible appearance matches</h2><span>Ranked visually · not confirmed identity</span></div>${data.possible_matches.length?`<div class="vehicle-grid">${data.possible_matches.map(sightingCard).join('')}</div>`:empty('No indexed appearance matches','New captures may still be processing.')}`;
    bindCards(); $('#sighting-page').textContent=`${state.total} image-search candidates`; $('#sighting-prev').disabled=true; $('#sighting-next').disabled=true;
    message('Image search complete. Review the images before confirming a match.');
  } catch(error) {message(error.message,true);} finally {$('#car-image-search').disabled=false;}
}
function clearImage() {state.imageMode=false; state.request++; $('#query-preview').hidden=true; $('#clear-image-search').hidden=true; $('#car-image-search').value=''; if(state.imageUrl){URL.revokeObjectURL(state.imageUrl);state.imageUrl=null;} state.page=1;}
async function loadPipelines() {
  const data=await api('/pipelines'); state.pipelines=data.items;
  const existing=$('#camera-filter').value;
  $('#camera-filter').innerHTML='<option value="">All cameras</option>'+data.items.map(p=>`<option value="${escapeHTML(p.pipeline_id)}">${escapeHTML(p.name)}</option>`).join(''); $('#camera-filter').value=existing;
  const pilot=data.items.find(p=>p.pilot);
  $('#pipeline-settings').innerHTML=pilot?`<form id="pipeline-form" class="pipeline-setting"><div><h3>${escapeHTML(pilot.name)}</h3><p class="muted">${data.processing_enabled?'Vehicle worker: '+escapeHTML(data.worker_status || 'checking'):'Vehicle processing is not enabled in this deployment'}</p></div><label class="inline-check"><input name="enabled" type="checkbox" ${pilot.enabled?'checked':''}> Accept vehicle events</label><label>OCR language<select name="ocr_profile"><option value="en">English / numbers</option><option value="ar">Arabic</option><option value="latin">Latin languages</option></select></label><label>Country<input name="country" maxlength="8" value="${escapeHTML(pilot.country)}" required pattern="[A-Za-z]{2,8}"></label><button class="primary" type="submit">Save pipeline</button></form>`:empty('Pilot camera is not registered','The camera must exist in VAS before vehicle processing can be enabled.');
  if(pilot) {
    $('#pipeline-form select').value=pilot.ocr_profile;
    $('#pipeline-form').addEventListener('submit',async e=>{e.preventDefault();const form=new FormData(e.currentTarget);try{await api('/pipelines/'+encodeURIComponent(pilot.pipeline_id),{method:'PUT',body:JSON.stringify({enabled:form.has('enabled'),ocr_profile:form.get('ocr_profile'),country:form.get('country')})});message('Pipeline settings saved.');await loadPipelines();}catch(error){message(error.message,true);}});
  }
}
function debounce(callback, ms=350) {let timer;return(...args)=>{clearTimeout(timer);timer=setTimeout(()=>callback(...args),ms);};}

document.addEventListener('DOMContentLoaded',async()=>{
  $('#time-note').textContent=`Date/time filters use ${Intl.DateTimeFormat().resolvedOptions().timeZone}. Excel timestamps are UTC. Results preserve the capture time.`;
  $$('[data-tab]').forEach(b=>b.addEventListener('click',()=>{$$('[data-tab]').forEach(t=>t.setAttribute('aria-selected',String(t===b))); for(const tab of ['sightings','known','pipelines']) $('#'+tab+'-panel').hidden=tab!==b.dataset.tab; if(b.dataset.tab==='known')loadKnown();}));
  $$('[data-close]').forEach(b=>b.addEventListener('click',()=>$('#'+b.dataset.close).close()));
  $('#sighting-dialog').addEventListener('close',()=>{if(!state.imageMode)loadSightings();});
  $('#vehicle-filters').addEventListener('submit',e=>{e.preventDefault();clearImage();loadSightings();});
  $('#reset-filters').addEventListener('click',()=>{$('#vehicle-filters').reset();clearImage();loadSightings();});
  $('#known-search').addEventListener('submit',e=>{e.preventDefault();state.knownPage=1;loadKnown();});
  $('#new-car').addEventListener('click',()=>openKnown()); $('#known-form').addEventListener('submit',saveKnown);
  for(const [id,delta] of [['sighting-prev',-1],['sighting-next',1]]) $('#'+id).addEventListener('click',()=>{state.page+=delta;loadSightings();});
  for(const [id,delta] of [['known-prev',-1],['known-next',1]]) $('#'+id).addEventListener('click',()=>{state.knownPage+=delta;loadKnown();});
  $('#review-prev').addEventListener('click',()=>openReview(state.reviewIndex-1)); $('#review-next').addEventListener('click',()=>openReview(state.reviewIndex+1));
  $$('[data-image-kind]').forEach(b=>b.addEventListener('click',()=>setReviewImage(b.dataset.imageKind)));
  $('#review-form').addEventListener('submit',e=>{e.preventDefault();decide('confirm');});
  $('#reject-car').addEventListener('click',()=>decide('reject')); $('#unreadable-plate').addEventListener('click',()=>decide('unreadable'));
  $('#retry-vehicle').addEventListener('click',async()=>{if(!state.review)return;try{await api('/sightings/'+state.review.id+'/retry',{method:'POST'});message('Processing retry queued.',false,'#review-message');await openReview(state.reviewIndex,true);}catch(error){message(error.message,true,'#review-message');}});
  $('#review-form select[name=known_vehicle_id]').addEventListener('change',renderReference);
  $('#review-form input[name=plate]').addEventListener('input',debounce(async()=>{const id=state.review?.id,q=$('#review-form input[name=plate]').value;if(!id || q.length<2)return;try{const data=await api('/known?q='+encodeURIComponent(q));if(state.review?.id===id && $('#review-form input[name=plate]').value===q)setReferences(data.items,$('#review-form select[name=known_vehicle_id]').value);}catch(error){message(error.message,true,'#review-message');}}));
  $('#person-search').addEventListener('input',debounce(async()=>{const q=$('#person-search').value;if(q.length<2)return;try{const data=await api('/people?q='+encodeURIComponent(q));if($('#person-search').value!==q)return;$('#person-link-form select[name=identity_id]').innerHTML='<option value="">Select person</option>'+data.items.map(p=>`<option value="${p.id}">${escapeHTML(p.name)}${p.person_code?' · '+escapeHTML(p.person_code):''}</option>`).join('');}catch(error){message(error.message,true,'#known-message');}}));
  $('#person-link-form').addEventListener('submit',async e=>{e.preventDefault();if(!state.known)return;try{const body=Object.fromEntries(new FormData(e.currentTarget));await api('/known/'+state.known.id+'/people',{method:'POST',body:JSON.stringify(body)});state.known=await api('/known/'+state.known.id);populateKnown();e.target.reset();message('Reviewed association saved.',false,'#known-message');}catch(error){message(error.message,true,'#known-message');}});
  $('#known-form input[name=photo]').addEventListener('change',e=>{const file=e.target.files[0];if(!file)return;const preview=$('#known-photo-preview');if(preview.dataset.localUrl)URL.revokeObjectURL(preview.dataset.localUrl);preview.dataset.localUrl=URL.createObjectURL(file);preview.src=preview.dataset.localUrl;preview.hidden=false;});
  $('#car-image-search').addEventListener('change',e=>imageSearch(e.target.files[0])); $('#clear-image-search').addEventListener('click',()=>{clearImage();loadSightings();});
  $('#export-vehicles').addEventListener('click',async()=>{const b=$('#export-vehicles');b.disabled=true;try{const query=filters();if(state.imageMode){if(!state.rows.length)throw new Error('No image matches to export.');state.rows.forEach(r=>query.append('ids',r.id));}const response=await fetch('/api/vehicles/export.xlsx?'+query,{credentials:'include'});if(!response.ok){const error=await response.json();throw new Error(error.detail || 'Export failed.');}const url=URL.createObjectURL(await response.blob()),link=document.createElement('a');link.href=url;link.download='vehicle-sightings.xlsx';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);message('Excel report exported with the current filters.');}catch(error){message(error.message,true);}finally{b.disabled=false;}});
  $('#sighting-dialog').addEventListener('keydown',e=>{if(['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName))return;if(e.key==='ArrowRight'){e.preventDefault();openReview(state.reviewIndex+1);}if(e.key==='ArrowLeft'){e.preventDefault();openReview(state.reviewIndex-1);}});
  try {await loadPipelines();await loadSightings();} catch(error){message(error.message,true);}
});
