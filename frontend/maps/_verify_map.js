const log = (k, v) => console.log('VERIFY ' + k + ' ' + (typeof v === 'string' ? v : JSON.stringify(v)));
  window.addEventListener('error', e => log('WINDOW_ERROR', e.message));
  document.addEventListener('securitypolicyviolation', e => log('CSP_VIOLATION', {directive: e.violatedDirective, blocked: e.blockedURI}));
  try {
    const params = new URLSearchParams(location.search);
    const identity = params.get('identity');
    const style = params.get('style') || 'light';

    const login = await fetch('/api/auth/login', {method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},
      body: JSON.stringify({username:'admin', password:'admin123'})});
    log('LOGIN', login.status);

    const mod = await import('/frontend/js/identity-map.js?v=verify');
    log('MODULE', 'identity-map loaded');
    const IM = await window.IdentityMap.ready;

    const ctl = new IM.Controller(document.getElementById('m'), {style:'light',
      onError: (kind, detail) => log('CTL_ERROR', {kind, detail})});
    await ctl.init();
    log('MAP_INIT', 'ok');
    const avail = await ctl.loadAvailability(null);
    log('AVAILABILITY', avail && avail.styles);

    // Worker: MapLibre spawns it during init. Report what the page saw.
    log('WORKER_URL', IM.maplibregl.getWorkerUrl ? IM.maplibregl.getWorkerUrl() : 'n/a');

    if (identity) {
      const data = await ctl.load(identity, {days_back: 30, enable_security_features: true, detect_patterns: true, show_risk_heatmap: true},
                                  {popups:true, cluster:true, routes:true, security:true, patterns:true, risk:true, timeline:true, avatar:true});
      log('DATA_COUNTS', data.metadata.counts);
      await new Promise(r => setTimeout(r, 1500));
      const layers = ctl.map.getStyle().layers.map(l => l.id).filter(id => id.startsWith('ae-'));
      log('OVERLAY_LAYERS', layers);
      // style switch must keep overlays
      if (style !== 'light') {
        try { await ctl.setBasemap(style); log('SET_STYLE', style + ' ok'); }
        catch (e) { log('SET_STYLE_REFUSED', {style, code: e.code || e.message}); }
        await new Promise(r => setTimeout(r, 800));
        const after = ctl.map.getStyle().layers.map(l => l.id).filter(id => id.startsWith('ae-'));
        log('OVERLAYS_AFTER_STYLE', after);
      }
      // did basemap tiles actually load?
      const src = ctl.map.getSource('lebanon-streets');
      log('BASEMAP_SOURCE_LOADED', src ? ctl.map.isSourceLoaded('lebanon-streets') : 'no source');
      log('TERRAIN', {terrain: !!ctl.map.getTerrain(), source: ctl.map.getTerrain() && ctl.map.getTerrain().source,
                       hillshade: ctl.map.getStyle().layers.some(l => l.type === 'hillshade'),
                       demSourceLoaded: ctl.map.getSource('lebanon-dem') ? ctl.map.isSourceLoaded('lebanon-dem') : 'no source'});
      log('DEM_TILE_REQUESTS', performance.getEntriesByType('resource').filter(r => r.name.includes('/maps/lebanon-dem/')).length);
      log('EXTERNAL_REQUESTS', performance.getEntriesByType('resource').map(r => r.name).filter(n => !n.startsWith(location.origin)));
      log('TIMELINE_DOM', !!document.querySelector('.ae-timeline'));
      log('AVATAR_DOM', !!document.querySelector('.ae-avatar'));
      ctl._setTimelineIndex(3); await new Promise(r=>setTimeout(r,300));
      log('TIMELINE_LABEL', document.querySelector('.ae-timeline-label') && document.querySelector('.ae-timeline-label').textContent);
    }
    log('DONE', 'ok');
  } catch (e) {
    log('FATAL', (e && e.stack) || String(e));
  }
