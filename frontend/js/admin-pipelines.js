// Admin Pipelines Management JavaScript
// BACKEND HANDLES ALL AUTHENTICATION
let currentPipelines = [];
let coordinatesMap = null;
let coordinatesMarker = null;
let currentEditingPipelineId = null;

// Function to setup refresh button (can be called multiple times safely)
function setupRefreshButton() {
    const refreshBtn = document.getElementById('refresh-btn');
    if (refreshBtn && !refreshBtn.dataset.listenerAttached) {
        refreshBtn.dataset.listenerAttached = 'true';
        refreshBtn.addEventListener('click', async (e) => {
            e.preventDefault();
            e.stopPropagation();
            
            // Add loading state
            const icon = refreshBtn.querySelector('.fa-sync-alt');
            if (icon) {
                icon.classList.add('fa-spin');
            }
            refreshBtn.disabled = true;
            refreshBtn.style.opacity = '0.7';
            refreshBtn.style.cursor = 'wait';
            
            try {
                await loadPipelines();
            } catch (error) {
                console.error('Error refreshing pipelines:', error);
                showNotification('Failed to refresh pipelines: ' + error.message, 'error');
            } finally {
                // Remove loading state
                if (icon) {
                    icon.classList.remove('fa-spin');
                }
                refreshBtn.disabled = false;
                refreshBtn.style.opacity = '1';
                refreshBtn.style.cursor = 'pointer';
            }
        });
        console.log('✅ Refresh button event listener attached');
    } else if (!refreshBtn) {
        console.warn('⚠️ Refresh button not found, will retry...');
        // Retry after a short delay in case navbar is still loading
        setTimeout(setupRefreshButton, 500);
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    // Backend already authenticated user before serving this page

    // Setup refresh button (with retry logic)
    setupRefreshButton();
    
    // Also try after a delay in case navbar loads asynchronously
    setTimeout(setupRefreshButton, 1000);

    // Load pipelines
    await loadPipelines();
    
    // Coordinate inputs sync with map
    const latInput = document.getElementById('latitude-input');
    const lngInput = document.getElementById('longitude-input');
    if (latInput) {
        latInput.addEventListener('input', syncMapFromInputs);
    }
    if (lngInput) {
        lngInput.addEventListener('input', syncMapFromInputs);
    }
});

async function loadPipelines() {
    const tbody = document.getElementById('pipelines-table-body');
    if (!tbody) {
        console.error('Pipelines table body not found!');
        return;
    }
    
    try {
        // Show loading state
        tbody.innerHTML = '<tr><td colspan="7" class="loading"><i class="fas fa-spinner fa-spin"></i> Loading pipelines...</td></tr>';
        
        const response = await fetch('/api/pipelines', {
            credentials: 'include' // Include HttpOnly cookies
        });
        
        if (!response.ok) {
            const errorText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errorText}`);
        }
        
        currentPipelines = await response.json();
        renderPipelinesTable();
        console.log(`✅ Loaded ${currentPipelines.length} pipelines`);
    } catch (error) {
        console.error('Error loading pipelines:', error);
        if (tbody) {
            tbody.innerHTML = 
                `<tr><td colspan="7" class="loading" style="color: #ff6b6b;"><i class="fas fa-exclamation-triangle"></i> Error loading pipelines: ${error.message}</td></tr>`;
        }
        showNotification('Failed to load pipelines: ' + error.message, 'error');
    }
}

function renderPipelinesTable() {
    const tbody = document.getElementById('pipelines-table-body');
    
    if (currentPipelines.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="loading">No pipelines found</td></tr>';
        return;
    }

    tbody.innerHTML = currentPipelines.map(pipeline => {
        // Handle different date formats
        const createdDate = pipeline.created_at 
            ? (typeof pipeline.created_at === 'string' 
                ? new Date(pipeline.created_at).toLocaleString() 
                : new Date(pipeline.created_at * 1000).toLocaleString())
            : 'N/A';
        const updatedDate = pipeline.updated_at 
            ? (typeof pipeline.updated_at === 'string' 
                ? new Date(pipeline.updated_at).toLocaleString() 
                : new Date(pipeline.updated_at * 1000).toLocaleString())
            : 'N/A';
        
        // Handle is_active - can be 1/0 (Integer) or true/false (Boolean)
        const isActive = pipeline.is_active === 1 || pipeline.is_active === true || pipeline.is_active === '1';
        
        // Location display
        const hasCoordinates = pipeline.latitude !== null && pipeline.longitude !== null;
        const locationDisplay = pipeline.location_name 
            ? `<span class="location-name">${escapeHtml(pipeline.location_name)}</span>`
            : (hasCoordinates 
                ? `<span class="location-coords">${pipeline.latitude.toFixed(6)}, ${pipeline.longitude.toFixed(6)}</span>`
                : '<span class="location-none">Not set</span>');
        
        return `
        <tr>
            <td><strong>${escapeHtml(pipeline.pipeline_id)}</strong></td>
            <td><span class="badge badge-${isActive ? 'active' : 'inactive'}">${isActive ? 'Active' : 'Inactive'}</span></td>
            <td class="location-cell">
                ${locationDisplay}
                ${hasCoordinates ? '<i class="fas fa-map-marker-alt location-icon" title="Coordinates set"></i>' : ''}
            </td>
            <td>${pipeline.total_detections || 0}</td>
            <td>${createdDate}</td>
            <td>${updatedDate}</td>
            <td>
                <button class="btn-action btn-location" data-action="openCoordinatesModal" data-arg="${escapeHtml(pipeline.pipeline_id)}" title="Set location coordinates">
                    <i class="fas fa-map-marker-alt" aria-hidden="true"></i>
                    ${hasCoordinates ? 'Edit Location' : 'Set Location'}
                </button>
                <button class="btn-action btn-rename" data-action="renamePipeline" data-arg="${escapeHtml(pipeline.pipeline_id)}" title="Rename this pipeline (old id becomes an alias)">
                    <i class="fas fa-pen" aria-hidden="true"></i>
                    Rename
                </button>
            </td>
        </tr>
    `;
    }).join('');
}

// Rename a pipeline: cascades everywhere and creates an alias so webhooks that
// still use the old id automatically land in the renamed pipeline.
async function renamePipeline(pipelineId) {
    const newName = prompt(
        `Rename pipeline "${pipelineId}" to:\n\n` +
        `(3-100 chars: letters, digits, dashes, underscores. Spaces become underscores.)`,
        pipelineId
    );
    if (!newName || newName.trim() === '' || newName.trim() === pipelineId) return;

    try {
        const response = await fetch(`/api/pipelines/${encodeURIComponent(pipelineId)}/rename`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ new_name: newName.trim() })
        });
        const data = await response.json();
        if (response.ok && data.success) {
            showNotification(`Pipeline renamed: ${data.old_name} → ${data.new_name}`, 'success');
            loadPipelines();
        } else {
            showNotification(data.detail || 'Rename failed', 'error');
        }
    } catch (err) {
        console.error('[PIPELINES] Rename error:', err);
        showNotification('Rename failed: ' + err.message, 'error');
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function togglePipelineStatus(pipelineId, currentStatus) {
    const action = currentStatus ? 'deactivate' : 'activate';
    if (!confirm(`Are you sure you want to ${action} pipeline "${pipelineId}"?`)) return;

    try {
        // Note: Pipeline status toggle requires a backend API endpoint
        // For now, pipelines are managed through the detection system automatically
        // This is informational - actual pipeline management happens when detections are received
        alert(`Pipeline "${pipelineId}" status: ${currentStatus ? 'Active' : 'Inactive'}\n\nNote: Pipeline status is automatically managed based on detection activity.`);
        
        // Refresh to show latest status
        await loadPipelines();
    } catch (error) {
        alert('Error: ' + error.message);
    }
}

function openCoordinatesModal(pipelineId) {
    currentEditingPipelineId = pipelineId;
    const pipeline = currentPipelines.find(p => p.pipeline_id === pipelineId);
    
    // Set modal title
    document.getElementById('coordinates-modal-subtitle').textContent = `Pipeline: ${pipelineId}`;
    
    // Populate form. Nullish, not falsy-OR: a stored coordinate of exactly 0
    // is a real place, and `|| ''` erased it from the form.
    document.getElementById('location-name-input').value = pipeline?.location_name ?? '';
    document.getElementById('latitude-input').value = pipeline?.latitude ?? '';
    document.getElementById('longitude-input').value = pipeline?.longitude ?? '';
    
    // Show modal via the shared lifecycle. Escape and backdrop now close it
    // too — and both route through closeCoordinatesModal(), so the WebGL map
    // is disposed however the dialog is dismissed. Before, only the explicit
    // close button released it.
    window.ModalStack.open(document.getElementById('coordinates-modal'), {
        backdropClose: true,
        onClose: () => closeCoordinatesModal()
    });
    
    // Initialize map (will be recreated each time to ensure clean state)
    setTimeout(() => {
        if (coordinatesMap) {
            coordinatesMap.remove();
            coordinatesMap = null;
            coordinatesMarker = null;
        }
        initCoordinatesMap();
    }, 100);
}

function closeCoordinatesModal() {
    const modal = document.getElementById('coordinates-modal');
    if (!modal) return;
    if (window.ModalStack.isOpen(modal)) {
        // Re-enters here through onClose once the entry has left the stack,
        // so the disposal below runs exactly once per close.
        window.ModalStack.close(modal);
        return;
    }
    modal.style.display = 'none';
    currentEditingPipelineId = null;
    // Dispose the WebGL map; it is rebuilt on next open with fresh inputs.
    // Leaking a MapLibre context per open is a real resource leak, so this
    // cleanup is preserved exactly and is now reached from Escape and
    // backdrop dismissal as well.
    if (coordinatesMarker) { coordinatesMarker.remove(); coordinatesMarker = null; }
    if (coordinatesMap) { coordinatesMap.remove(); coordinatesMap = null; }
}

async function initCoordinatesMap() {
    const mapContainer = document.getElementById('coordinates-map');
    if (!mapContainer || coordinatesMap) return;

    // Existing coordinates, if any. Number.isFinite instead of truthiness:
    // 0 is a valid coordinate and `lat && lng` treated it as "unset".
    const lat = parseFloat(document.getElementById('latitude-input').value);
    const lng = parseFloat(document.getElementById('longitude-input').value);
    const hasCoords = Number.isFinite(lat) && Number.isFinite(lng);

    // MapLibre GL JS over the offline Martin basemap — the same renderer and
    // the same Light style as the intelligence maps (one map stack). The
    // style carries the tileset's bounds and zoom range, so this file no
    // longer hard-codes Lebanon's bbox/zooms/centre — they were drifting from
    // config.py. Bounds are still enforced here so panning cannot leave the
    // dataset into blank space.
    const IM = window.IdentityMap;
    if (!IM || !IM.maplibregl) {
        console.error('[PIPELINES] map module not loaded (identity-map.js)');
        return;
    }
    const maplibregl = IM.maplibregl;

    // Which basemap is REAL right now, asked of the backend — never a
    // hard-coded style. This picker used to open Light unconditionally, so
    // when the Light archive turned out to be placeholder images it painted
    // "Access blocked" tiles here with no dropdown and no error path. The
    // style URL (and its cache-busting version) comes from the one map module
    // instead of a literal copied into this file.
    const style = await IM.firstUsableStyleUrl('light', (detail) => {
        const reason = (detail && detail.reason) || IM.UNAVAILABLE;
        showNotification(`Basemap unavailable: ${reason}`, 'warning');
    });
    if (!style) {
        mapContainer.textContent = 'No offline basemap is installed — coordinates can still be typed in.';
        return;
    }

    coordinatesMap = new maplibregl.Map({
        container: 'coordinates-map',
        style,
        center: hasCoords ? [lng, lat] : [35.85, 33.87],
        zoom: hasCoords ? 13 : 10,
        minZoom: 8,
        maxZoom: 17,
        maxBounds: [[34.60, 32.70], [37.10, 35.00]],
        attributionControl: { compact: true }
    });
    coordinatesMap.addControl(new maplibregl.NavigationControl(), 'top-right');

    // Click to set
    coordinatesMap.on('click', (e) => {
        const { lat, lng } = e.lngLat;
        document.getElementById('latitude-input').value = lat.toFixed(6);
        document.getElementById('longitude-input').value = lng.toFixed(6);
        updateMapMarker(lat, lng);
    });

    // Initialize marker if coordinates exist (zero is a valid coordinate)
    if (hasCoords) {
        updateMapMarker(lat, lng);
    }
}

function updateMapMarker(lat, lng) {
    if (!coordinatesMap) return;
    const maplibregl = window.IdentityMap.maplibregl;

    if (coordinatesMarker) {
        coordinatesMarker.setLngLat([lng, lat]);
    } else {
        // DOM element built with createElement — no HTML string injection.
        const pin = document.createElement('div');
        pin.className = 'coordinates-marker';
        const icon = document.createElement('i');
        icon.className = 'fas fa-map-marker-alt';
        pin.appendChild(icon);

        coordinatesMarker = new maplibregl.Marker({ element: pin, draggable: true, anchor: 'bottom' })
            .setLngLat([lng, lat])
            .addTo(coordinatesMap);

        // Update inputs when marker is dragged
        coordinatesMarker.on('dragend', () => {
            const ll = coordinatesMarker.getLngLat();
            document.getElementById('latitude-input').value = ll.lat.toFixed(6);
            document.getElementById('longitude-input').value = ll.lng.toFixed(6);
        });
    }

    // Center map on marker
    coordinatesMap.easeTo({ center: [lng, lat], zoom: Math.max(coordinatesMap.getZoom(), 13) });
}

function updateMapFromInputs() {
    const lat = parseFloat(document.getElementById('latitude-input').value);
    const lng = parseFloat(document.getElementById('longitude-input').value);

    if (Number.isFinite(lat) && Number.isFinite(lng)) {
        if (coordinatesMap) {
            updateMapMarker(lat, lng);
        }
    } else if (coordinatesMarker) {
        coordinatesMarker.remove();
        coordinatesMarker = null;
    }
}

function syncMapFromInputs() {
    // Debounce map updates
    clearTimeout(syncMapFromInputs.timeout);
    syncMapFromInputs.timeout = setTimeout(updateMapFromInputs, 500);
}

async function saveCoordinates() {
    if (!currentEditingPipelineId) return;
    
    const locationName = document.getElementById('location-name-input').value.trim();
    const latitude = document.getElementById('latitude-input').value;
    const longitude = document.getElementById('longitude-input').value;
    
    // Validate
    if (latitude && (isNaN(latitude) || parseFloat(latitude) < -90 || parseFloat(latitude) > 90)) {
        showNotification('Invalid latitude. Must be between -90 and 90.', 'error');
        return;
    }
    
    if (longitude && (isNaN(longitude) || parseFloat(longitude) < -180 || parseFloat(longitude) > 180)) {
        showNotification('Invalid longitude. Must be between -180 and 180.', 'error');
        return;
    }
    
    try {
        const response = await fetch(`/api/pipelines/${encodeURIComponent(currentEditingPipelineId)}/coordinates`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            credentials: 'include',
            body: JSON.stringify({
                latitude: latitude ? parseFloat(latitude) : null,
                longitude: longitude ? parseFloat(longitude) : null,
                location_name: locationName || null
            })
        });
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to save coordinates');
        }
        
        showNotification('Location coordinates saved successfully!', 'success');
        closeCoordinatesModal();
        await loadPipelines();
    } catch (error) {
        showNotification(`Error: ${error.message}`, 'error');
    }
}

function showNotification(message, type = 'info') {
    const notification = document.createElement('div');
    notification.className = `notification notification-${type}`;
    notification.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        padding: 1rem 1.5rem;
        background: ${type === 'error' ? 'rgba(255, 0, 0, 0.9)' : 
                     type === 'success' ? 'rgba(0, 255, 150, 0.9)' : 
                     'rgba(0, 150, 255, 0.9)'};
        color: ${type === 'success' ? '#000' : '#fff'};
        border-radius: 8px;
        z-index: 10000;
        font-weight: 600;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
        animation: slideIn 0.3s ease;
    `;
    notification.textContent = message;
    document.body.appendChild(notification);

    setTimeout(() => {
        notification.style.opacity = '0';
        notification.style.transition = 'opacity 0.3s';
        setTimeout(() => notification.remove(), 300);
    }, 3000);
}




// ---------------------------------------------------------------------------
// CSP-safe event registration
//
// These handlers were previously reached through inline onclick/onchange/
// onsubmit attributes, which `script-src 'self'` blocks. Registered rather
// than looked up on window, so only these names are invocable; delegated in
// actions.js, so dynamically rendered elements work without rebinding.
// ---------------------------------------------------------------------------
Actions.register({
    closeCoordinatesModal,
    saveCoordinates,
    openCoordinatesModal: (el) => openCoordinatesModal(el.dataset.arg),
    renamePipeline: (el) => renamePipeline(el.dataset.arg),
});
