/* Shared chronological evidence viewer. No inferred paths or interpolated frames. */
(function () {
    'use strict';
    const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    function imageUrl(value) {
        if (typeof value !== 'string' || !value.trim()) return '';
        try {
            const url = new URL(value, location.origin);
            return url.origin === location.origin && /^https?:$/.test(url.protocol) ? url.pathname + url.search : '';
        } catch (_) { return ''; }
    }
    function gap(ms) {
        if (!Number.isFinite(ms)) return 'Time unavailable';
        const seconds = Math.max(0, Math.floor(ms / 1000));
        if (seconds < 60) return `${seconds}s`;
        if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
        if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
        return `${Math.floor(seconds / 86400)}d ${Math.floor(seconds % 86400 / 3600)}h`;
    }
    // Group only consecutive, known-camera sightings within five minutes on
    // the same local date. This is a display grouping, never a dwell estimate.
    function groupVisits(rows) {
        const visits = [];
        rows.forEach((row, index) => {
            const last = visits[visits.length - 1];
            const previous = index ? rows[index - 1] : null;
            const merge = last && row.app.pipeline_id && previous.app.pipeline_id === row.app.pipeline_id
                && Number.isFinite(row.time) && Number.isFinite(previous.time)
                && row.time - previous.time <= 300000
                && new Date(row.time).toDateString() === new Date(previous.time).toDateString();
            if (merge) { last.end = index; last.rows.push(row); }
            else visits.push({start:index, end:index, rows:[row]});
        });
        return visits;
    }
    function journeyOverview(rows, camera) {
        const visits = groupVisits(rows);
        const known = new Set(rows.map(row => row.app.pipeline_id).filter(Boolean));
        const timed = rows.filter(row => Number.isFinite(row.time));
        const span = timed.length ? gap(timed[timed.length - 1].time - timed[0].time) : 'Unknown';
        return `<div class="journey-metrics"><div><strong>${visits.length}</strong><span>Visit groups</span></div><div><strong>${known.size}</strong><span>Known cameras</span></div><div><strong>${rows.length}</strong><span>Recorded sightings</span></div><div><strong>${span}</strong><span>Recorded time span</span></div></div>
            <div class="journey-overview" aria-label="Camera visit overview">
            <div class="journey-overview-caption"><strong>The journey at a glance</strong><span>Start → latest recorded sighting</span></div>
            <div class="journey-visits" tabindex="0" aria-label="Camera visits, in recorded order">${visits.map((visit, i) => {
                const first = visit.rows[0], last = visit.rows[visit.rows.length - 1];
                const previous = i ? visits[i-1].rows.at(-1) : null;
                const returning = i && first.app.pipeline_id && previous.app.pipeline_id && previous.app.pipeline_id !== first.app.pipeline_id && visits.slice(0,i).some(v => v.rows[0].app.pipeline_id === first.app.pipeline_id);
                const date = Number.isFinite(first.time) ? new Date(first.time) : null;
                const url = imageUrl(first.app.snapshot_url || first.app.best_snapshot_path);
                const text = date ? date.toLocaleString([], {month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit'}) : 'Time unavailable';
                return `<div class="journey-visit-block" data-visit-start="${visit.start}" data-visit-end="${visit.end}">
                    ${previous ? `<div class="journey-gap"><span aria-hidden="true">↓</span><span>${gap(first.time - previous.time)} since previous sighting</span></div>` : ''}
                    <button type="button" class="journey-visit ${i===0?'is-active':''}" data-visit-select="${visit.start}" aria-pressed="${i===0}">
                        <span class="journey-visit-marker">${String(i+1).padStart(2,'0')}</span><span class="journey-visit-photo">${url ? `<img src="${escape(url)}" alt="First snapshot in visit ${i+1}" loading="lazy">` : '<span>No image</span>'}</span>
                        <span class="journey-visit-copy"><span class="journey-visit-kicker">${i===0?'FIRST RECORDED':returning?'RETURN TO CAMERA':previous.app.pipeline_id===first.app.pipeline_id?'LATER SIGHTINGS':'NEXT CAMERA VISIT'}</span><strong>${escape(camera(first.app))}</strong><time>${escape(text)}</time><span class="journey-visit-count">${visit.rows.length} sighting${visit.rows.length===1?'':'s'}${visit.rows.length>1?` · ${gap(last.time-first.time)} span`:''}</span></span><span class="journey-visit-open" aria-hidden="true">↗</span>
                    </button></div>`;
            }).join('')}</div><p class="journey-group-note">Consecutive sightings on the same camera, no more than 5 minutes apart, are grouped within each day. A group does not confirm continuous presence.</p></div>`;
    }
    function updateVisit(root, index, follow) {
        const blocks = [...root.querySelectorAll('[data-visit-start]')];
        let selected = null;
        blocks.forEach((block, number) => {
            const active = index >= Number(block.dataset.visitStart) && index <= Number(block.dataset.visitEnd);
            const button = block.querySelector('.journey-visit');
            button.classList.toggle('is-active',active); button.setAttribute('aria-pressed',String(active));
            if(active) selected={block,number};
        });
        if(!selected)return;
        const {block,number}=selected;
        root.querySelector('[data-graph-number]').textContent=`VISIT ${number+1} · SIGHTING ${index+1}`;
        const photos=[...root.querySelectorAll('.movement-list [data-movement-index]')];
        const sheet=root.querySelector('.journey-contact-sheet');
        if(sheet.dataset.visit!==String(number)) {
            sheet.dataset.visit=String(number);
            sheet.innerHTML=photos.slice(Number(block.dataset.visitStart),Number(block.dataset.visitEnd)+1).map((photo,offset)=>{
                const position=Number(block.dataset.visitStart)+offset;
                return `<button type="button" data-journey-photo="${position}" aria-label="${escape(photo.dataset.caption)}" aria-pressed="${position===index}">${photo.dataset.snapshot?`<img src="${escape(photo.dataset.snapshot)}" alt="Sighting ${position+1}" loading="lazy">`:`<span>${position+1}</span>`}<small>${position+1}</small></button>`;
            }).join('');
        }
        sheet.querySelectorAll('button').forEach(button=>button.setAttribute('aria-pressed',String(Number(button.dataset.journeyPhoto)===index)));
        const current=sheet.querySelector(`[data-journey-photo="${index}"]`);
        if(current)sheet.scrollLeft=Math.max(0,current.offsetLeft-sheet.offsetLeft-sheet.clientWidth/2);
        if(follow&&root.dataset.movementView!=='lanes') {
            const scroll=root.querySelector('.journey-visits');
            scroll.scrollTop=Math.max(0,block.offsetTop-scroll.offsetTop-18);
        }
    }

    function graph(rows, camera) {
        const lanes = [...new Set(rows.map(row => row.app.pipeline_id || 'unknown'))];
        const width = Math.max(680, 170 + rows.length * 132), height = 78 + lanes.length * 116;
        const point = (row, index) => ({x:210 + index * 132, y:92 + lanes.indexOf(row.app.pipeline_id || 'unknown') * 116});
        const changes = rows.filter((row, i) => i && row.app.pipeline_id && rows[i-1].app.pipeline_id && row.app.pipeline_id !== rows[i-1].app.pipeline_id).length;
        const first = rows[0];
        return `<div class="movement-graph-workspace">
          <div class="movement-graph-heading"><div><span class="movement-number">MOVEMENT EXPLORER</span><h5>Understand the journey</h5></div><span class="movement-graph-badge">${changes} camera ${changes===1?'change':'changes'}</span></div>
          <div class="journey-view-controls" role="group" aria-label="Movement view"><button type="button" data-journey-view="overview" aria-pressed="true">Journey overview</button><button type="button" data-journey-view="lanes" aria-pressed="false">Every sighting · camera lanes</button></div><div class="movement-graph-layout"><div class="movement-graph-main">${journeyOverview(rows, camera)}
            <div class="movement-graph-legend"><span><b class="graph-dot"></b> Sighting</span><span><b class="graph-line"></b> Observation order</span><span>→ Earlier to later</span></div>
            <div class="movement-graph-scroll" tabindex="0" aria-label="Camera lanes. Scroll horizontally to explore sightings.">
              <div class="movement-graph-canvas" style="width:${width}px;height:${height}px">
                <svg width="${width}" height="${height}" aria-hidden="true" focusable="false">
                  ${lanes.map((lane,i) => `<rect x="0" y="${42+i*116}" width="${width}" height="100" rx="10" class="graph-lane"/><line x1="170" y1="${92+i*116}" x2="${width-20}" y2="${92+i*116}" class="graph-guide"/>`).join('')}
                  ${rows.slice(1).map((row,j) => {const a=point(rows[j],j),b=point(row,j+1),mid=(a.x+b.x)/2;return `<path data-graph-edge="${j+1}" d="M ${a.x+21} ${a.y} C ${mid} ${a.y}, ${mid} ${b.y}, ${b.x-24} ${b.y}" class="graph-edge"/><path d="M ${b.x-30} ${b.y-4} L ${b.x-24} ${b.y} L ${b.x-30} ${b.y+4}" class="graph-arrow"/>`;}).join('')}
                </svg>
                <div class="graph-lane-labels">${lanes.map((lane,i) => `<div class="graph-camera" style="top:${55+i*116}px"><span>CAMERA ${i+1}</span><strong>${escape(camera(rows.find(row=>(row.app.pipeline_id||'unknown')===lane).app))}</strong><small>${rows.filter(row=>(row.app.pipeline_id||'unknown')===lane).length} sightings</small></div>`).join('')}</div>
                ${rows.map((row,i) => {const p=point(row,i),date=Number.isFinite(row.time)?new Date(row.time):null;return `<button type="button" class="graph-sighting ${i===0?'is-selected':''}" data-graph-select="${i}" style="left:${p.x-23}px;top:${p.y-23}px" aria-pressed="${i===0}" aria-label="${escape(`Sighting ${i+1}, ${camera(row.app)}, ${date?date.toLocaleString():'time unavailable'}`)}"><span>${i+1}</span></button><div class="graph-time" style="left:${p.x-54}px;top:${p.y+30}px">${date?escape(date.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})):'Unknown time'}<small>${date?escape(date.toLocaleDateString([], {month:'short',day:'numeric'})):''}</small></div>`;}).join('')}
              </div>
            </div>
            <div class="graph-navigation"><button type="button" data-graph-step="-1" disabled aria-label="Previous sighting">←</button><button type="button" data-graph-play aria-pressed="false">▶ Review sequence</button><button type="button" data-graph-step="1" ${rows.length===1?'disabled':''} aria-label="Next sighting">→</button><span>2 seconds per sighting</span></div>
            <label class="graph-scrubber">Sighting <input type="range" min="0" max="${rows.length-1}" value="0" step="1" data-graph-scrub aria-label="Selected sighting"><output data-graph-counter>1 / ${rows.length}</output></label>
          </div><aside class="graph-inspector" aria-label="Selected sighting">
            <div class="graph-inspector-title"><span class="movement-number" data-graph-number>SIGHTING 01</span><span class="graph-inspector-live">RECORDED</span></div>
            <button type="button" class="graph-photo" data-graph-enlarge aria-label="Enlarge selected sighting photo">${imageUrl(first.app.snapshot_url||first.app.best_snapshot_path)?`<img src="${escape(imageUrl(first.app.snapshot_url||first.app.best_snapshot_path))}" alt="Selected recorded sighting">`:''}<span class="graph-photo-missing" ${imageUrl(first.app.snapshot_url||first.app.best_snapshot_path)?'hidden':''}>Image unavailable</span><span class="movement-photo-label">Enlarge ↗</span></button>
            <h5 data-graph-camera>${escape(camera(first.app))}</h5><p data-graph-date>${Number.isFinite(first.time)?escape(new Date(first.time).toLocaleString()):'Time unavailable'}</p>
            <p class="graph-selected-transition" data-graph-transition aria-live="polite">First recorded sighting</p><div class="journey-contact-label">Photos from this visit</div><div class="journey-contact-sheet" aria-label="Photos in selected visit"></div><p class="graph-disclaimer">Camera lanes show observation order, not a physical route. Activity between sightings is not recorded here.</p>
          </aside></div>
        </div>`;
    }
    let playback = null;
    function stopPlayback() {
        if (!playback) return;
        clearInterval(playback.timer);
        const button=playback.root.querySelector('[data-graph-play]');
        if(button){button.textContent='▶ Review sequence';button.setAttribute('aria-pressed','false');}
        playback=null;
    }
    function selectSighting(root,index,follow=false) {
        const photos=[...root.querySelectorAll('.movement-list [data-movement-index]')];
        index=Math.max(0,Math.min(photos.length-1,index));
        const photo=photos[index], card=photo.closest('.movement-card');
        root.dataset.graphActive=String(index);
        root.querySelectorAll('[data-graph-select]').forEach(node=>{const selected=Number(node.dataset.graphSelect)===index;node.classList.toggle('is-selected',selected);node.setAttribute('aria-pressed',String(selected));});
        root.querySelectorAll('[data-graph-edge]').forEach(edge=>edge.classList.toggle('is-traversed',Number(edge.dataset.graphEdge)<=index));
        const panel=root.querySelector('.graph-photo');let image=panel.querySelector('img');
        if(!image){image=document.createElement('img');image.alt='Selected recorded sighting';panel.prepend(image);}
        image.hidden=!photo.dataset.snapshot;
        if(photo.dataset.snapshot)image.src=photo.dataset.snapshot;else image.removeAttribute('src');
        panel.querySelector('.graph-photo-missing').hidden=!!photo.dataset.snapshot;
        root.querySelector('[data-graph-number]').textContent=`SIGHTING ${String(index+1).padStart(2,'0')}`;
        root.querySelector('[data-graph-camera]').textContent=card.querySelector('h5').textContent;
        root.querySelector('[data-graph-date]').textContent=card.querySelector('time').textContent;
        root.querySelector('[data-graph-transition]').textContent=photo.dataset.transition;
        root.querySelector('[data-graph-counter]').textContent=`${index+1} / ${photos.length}`;
        root.querySelector('[data-graph-scrub]').value=String(index);
        root.querySelector('[data-graph-step="-1"]').disabled=index===0;
        root.querySelector('[data-graph-step="1"]').disabled=index===photos.length-1;
        updateVisit(root,index,follow);
        if(follow && root.dataset.movementView==='lanes'){const node=root.querySelector(`[data-graph-select="${index}"]`),scroll=root.querySelector('.movement-graph-scroll');scroll.scrollLeft=Math.max(0,node.offsetLeft-(scroll.clientWidth+160)/2);scroll.scrollTop=Math.max(0,node.offsetTop-scroll.clientHeight/2);}
    }
    document.addEventListener('click',event=>{
        const root=event.target.closest('.movement-evidence');if(!root)return;
        const view=event.target.closest('[data-journey-view]');
        if(view){stopPlayback();root.dataset.movementView=view.dataset.journeyView;root.querySelectorAll('[data-journey-view]').forEach(button=>button.setAttribute('aria-pressed',String(button===view)));selectSighting(root,Number(root.dataset.graphActive||0),true);}
        const visit=event.target.closest('[data-visit-select]'),thumb=event.target.closest('[data-journey-photo]');
        if(visit||thumb){stopPlayback();selectSighting(root,Number(visit?visit.dataset.visitSelect:thumb.dataset.journeyPhoto),!!visit);}
        const node=event.target.closest('[data-graph-select]'),step=event.target.closest('[data-graph-step]');
        if(node||step){stopPlayback();selectSighting(root,node?Number(node.dataset.graphSelect):Number(root.dataset.graphActive||0)+Number(step.dataset.graphStep),true);}
        if(event.target.closest('[data-graph-enlarge]')){stopPlayback();root.querySelectorAll('.movement-list [data-movement-index]')[Number(root.dataset.graphActive||0)].click();}
        if(event.target.closest('[data-graph-play]')){
            const wasPlaying=playback&&playback.root===root;stopPlayback();if(wasPlaying)return;
            const count=root.querySelectorAll('[data-graph-select]').length;
            if(Number(root.dataset.graphActive||0)>=count-1)selectSighting(root,0,true);
            const button=root.querySelector('[data-graph-play]');button.textContent='Ⅱ Pause';button.setAttribute('aria-pressed','true');
            playback={root,timer:setInterval(()=>{
                if(!root.isConnected||!root.getClientRects().length||document.hidden){stopPlayback();return;}
                const next=Number(root.dataset.graphActive||0)+1;
                if(next>=count){stopPlayback();return;} selectSighting(root,next,true);
                if(next===count-1)stopPlayback();
            },2000)};
        }
    });
    document.addEventListener('input',event=>{if(event.target.matches('[data-graph-scrub]')){stopPlayback();selectSighting(event.target.closest('.movement-evidence'),Number(event.target.value),true);}});
    document.addEventListener('visibilitychange',()=>{if(document.hidden)stopPlayback();});

    function render(appearances, label) {
        if (!Array.isArray(appearances) || !appearances.length) return '<p class="timeline-empty">No recorded appearances available.</p>';
        const rows = appearances.map((app, index) => ({app, index, time: Date.parse(app.start_time)}))
            .sort((a, b) => (Number.isFinite(a.time) ? a.time : Infinity) - (Number.isFinite(b.time) ? b.time : Infinity) || a.index - b.index);
        const cameras = new Set(rows.map(({app}) => app.pipeline_id).filter(Boolean));
        const camera = app => app.location_name || (app.pipeline_id ? label(app.pipeline_id) : 'Unknown camera');
        return `<section class="movement-evidence" data-movement-view="overview" aria-label="Recorded appearance sequence">
            <div class="movement-intro"><strong>${rows.length} recorded appearances</strong><span>${cameras.size === 1 ? 'One camera • chronological sightings' : `${cameras.size} cameras • chronological sightings`}</span></div>
            <p class="movement-hint">Select a photo to inspect it. Arrows show the order of recorded sightings; spacing does not represent elapsed time. Times use your local timezone.</p>
            ${graph(rows, camera)}
            <details class="movement-evidence-details"><summary>Browse all ${rows.length} appearance photos</summary><ol class="movement-list">${rows.map(({app, time}, index) => {
                const previous = rows[index - 1];
                const changed = previous && previous.app.pipeline_id && app.pipeline_id && previous.app.pipeline_id !== app.pipeline_id;
                const date = Number.isFinite(time) ? new Date(time) : null;
                const stamp = date ? date.toLocaleString(undefined, {year:'numeric', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit'}) : 'Time unavailable';
                const url = imageUrl(app.snapshot_url || app.best_snapshot_path);
                const title = `${camera(app)} · ${stamp}`;
                const elapsed = previous ? gap(time - previous.time) : '';
                return `<li class="movement-item">
                    <div class="movement-transition ${changed ? 'is-camera-change' : ''}"><span aria-hidden="true">${index ? '↓' : '●'}</span> ${index ? `${changed ? 'Camera change' : (previous.app.pipeline_id && app.pipeline_id ? 'Same camera' : 'Camera unknown')} · ${elapsed} since previous sighting` : 'First recorded sighting'}</div>
                    <article class="movement-card"><button type="button" class="movement-photo" data-movement-index="${index}" data-snapshot="${escape(url)}" data-caption="${escape(title)}" data-transition="${escape(index ? `${changed ? 'Camera change' : 'Next sighting'} · ${elapsed} since previous sighting` : 'First recorded sighting')}" aria-label="${escape(`Inspect appearance ${index + 1}: ${title}`)}">
                        ${url ? `<img src="${escape(url)}" alt="Recorded appearance ${index + 1}" loading="lazy" decoding="async">` : '<span class="movement-missing">No saved image</span>'}
                        <span class="movement-photo-label">${url ? 'Enlarge photo ↗' : 'View details'}</span></button>
                    <div class="movement-details"><span class="movement-number">APPEARANCE ${String(index + 1).padStart(2, '0')}</span><h5>${escape(camera(app))}</h5>
                        <time ${date ? `datetime="${date.toISOString()}"` : ''}>${escape(stamp)}</time>
                        <dl>${app.track_id !== null && app.track_id !== undefined ? `<div><dt>Camera track</dt><dd>${escape(app.track_id)}</dd></div>` : ''}
                        ${app.end_time && Date.parse(app.end_time) >= time ? `<div><dt>Observed duration</dt><dd>${gap(Date.parse(app.end_time) - time)}</dd></div>` : ''}</dl>
                        <p class="movement-context">${changed ? `Previously seen at ${escape(camera(previous.app))}` : 'Recorded snapshot • select photo for a larger view'}</p>
                    </div></article></li>`;
            }).join('')}</ol></details></section>`;
    }
    let viewer, items = [], active = 0;
    function show(index) {
        active = Math.max(0, Math.min(items.length - 1, index));
        const item = items[active];
        const root = item.closest('.movement-evidence');
        if (root) selectSighting(root, active, false);
        const image = viewer.querySelector('img');
        image.hidden = !item.dataset.snapshot;
        if (item.dataset.snapshot) image.src = item.dataset.snapshot; else image.removeAttribute('src');
        image.alt = item.dataset.caption;
        viewer.querySelector('.movement-viewer-caption').textContent = item.dataset.caption;
        viewer.querySelector('.movement-viewer-status').textContent = `Appearance ${active + 1} of ${items.length} · ${item.dataset.transition}`;
        viewer.querySelector('.movement-viewer-missing').hidden = !!item.dataset.snapshot;
        viewer.querySelector('[data-movement-prev]').disabled = active === 0;
        viewer.querySelector('[data-movement-next]').disabled = active === items.length - 1;
    }
    function open(button) {
        stopPlayback();
        if (!viewer) {
            viewer = document.createElement('div');
            viewer.className = 'security-modal movement-viewer';
            viewer.id = 'movement-photo-viewer';
            viewer.style.display = 'none';
            viewer.setAttribute('role', 'dialog'); viewer.setAttribute('aria-modal', 'true');
            viewer.setAttribute('aria-label', 'Appearance photo viewer');
            viewer.innerHTML = `<div class="movement-viewer-panel"><header><strong>Recorded appearance</strong><button type="button" data-movement-close aria-label="Close photo viewer">Close ×</button></header><div class="movement-viewer-image"><img alt=""><p class="movement-viewer-missing" hidden>Image unavailable</p></div><p class="movement-viewer-caption"></p><p class="movement-viewer-status" aria-live="polite"></p><footer><button type="button" data-movement-prev>← Previous</button><span>Use ← / → to follow the sequence</span><button type="button" data-movement-next>Next →</button></footer></div>`;
            document.body.appendChild(viewer);
            viewer.addEventListener('click', event => {
                if (event.target.closest('[data-movement-close]')) window.ModalStack.close(viewer);
                if (event.target.closest('[data-movement-prev]')) show(active - 1);
                if (event.target.closest('[data-movement-next]')) show(active + 1);
            });
            viewer.addEventListener('keydown', event => {
                if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); show(active + (event.key === 'ArrowRight' ? 1 : -1)); }
            });
        }
        items = [...button.closest('.movement-evidence').querySelectorAll('[data-movement-index]')];
        show(items.indexOf(button));
        window.ModalStack.open(viewer, {backdropClose:true, onClose:() => { items = []; viewer.querySelector('img').removeAttribute('src'); }});
    }
    document.addEventListener('click', event => {
        const button = event.target.closest('[data-movement-index]');
        if (button) open(button);
    });
    document.addEventListener('error', event => {
        if (!event.target.matches('img')) return;
        if (event.target.closest('.journey-visit-photo, .journey-contact-sheet')) {
            event.target.hidden = true;
            const missing = document.createElement('span'); missing.className='journey-image-missing'; missing.textContent='No image'; event.target.parentElement.appendChild(missing);
        } else if (event.target.closest('.graph-photo')) {
            event.target.hidden = true; event.target.parentElement.querySelector('.graph-photo-missing').hidden = false;
        } else if (event.target.closest('.movement-photo')) {
            event.target.hidden = true;
            const parent = event.target.parentElement;
            if (!parent.querySelector('.movement-missing')) { const missing = document.createElement('span'); missing.className = 'movement-missing'; missing.textContent = 'Image unavailable'; parent.prepend(missing); }
        } else if (viewer && event.target === viewer.querySelector('img')) {
            event.target.hidden = true; viewer.querySelector('.movement-viewer-missing').hidden = false;
        }
    }, true);
    if (typeof MutationObserver !== 'undefined') {
        const initialize = () => document.querySelectorAll('.movement-evidence:not([data-journey-ready])').forEach(root => {
            root.dataset.journeyReady = 'true'; updateVisit(root, 0, false);
        });
        new MutationObserver(initialize).observe(document.documentElement, {childList:true, subtree:true});
        initialize();
    }
    window.MovementTimeline = {render};
})();
