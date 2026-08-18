/**
 * Global Upload Modal JavaScript
 * ===============================
 * Handles the upload person modal functionality across all pages.
 */

// Global functions for upload modal
function openUploadModal() {
    // Backend handles authentication - just check if user is admin
    fetch('/api/auth/me', {
        credentials: 'include' // Include HttpOnly cookies
    })
    .then(response => {
        if (!response.ok) throw new Error('Authentication failed');
        return response.json();
    })
    .then(user => {
        if (user.role !== 'admin') {
            alert('Access denied. Only administrators can add persons to track.');
            return;
        }
        const modal = document.getElementById('uploadModal');
        if (modal) {
            if (window.ModalStack) {
                // Pages with the modal stack (Unknown Faces): the stack owns
                // layering, Escape and backdrop, so an error alert raised
                // during upload lands ABOVE this dialog instead of under it.
                window.ModalStack.open(modal, {
                    backdropClose: true,
                    onClose: () => closeUploadModal()
                });
            } else {
                modal.style.display = 'flex';
            }
            modal.classList.add('active');
        }
    })
    .catch(error => {
        console.error('Error checking admin status:', error);
        alert('Error verifying permissions. Please try again.');
    });
}

/**
 * Show alert popup for upload errors/success
 */
// 'error' | 'info' | anything else (treated as success).
//
// The 'info' state exists because a duplicate upload is neither: the request
// succeeded and nothing was stored. Rendering that green with a tick is what
// sent operators looking for a file the server had correctly declined to write.
const _ALERT_PALETTE = {
    error: { bg: '#f8d7da', border: '#f5c6cb', text: '#721c24', icon: '⚠️' },
    info:  { bg: '#cce5ff', border: '#b8daff', text: '#004085', icon: 'ℹ️' },
    success: { bg: '#d4edda', border: '#c3e6cb', text: '#155724', icon: '✅' },
};


function showUploadAlert(title, message, type = 'error') {
    const palette = _ALERT_PALETTE[type] || _ALERT_PALETTE.success;
    // Create or get alert container
    let alertContainer = document.getElementById('uploadAlertContainer');
    if (!alertContainer) {
        alertContainer = document.createElement('div');
        alertContainer.id = 'uploadAlertContainer';
        alertContainer.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 10000;
            max-width: 400px;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
        `;
        document.body.appendChild(alertContainer);
    }
    
    // Create alert element
    const alert = document.createElement('div');
    alert.style.cssText = `
        background: ${palette.bg};
        border: 1px solid ${palette.border};
        color: ${palette.text};
        padding: 16px 20px;
        border-radius: 8px;
        margin-bottom: 12px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        animation: slideInRight 0.3s ease-out;
        cursor: pointer;
    `;
    
    // Add animation
    if (!document.getElementById('uploadAlertStyles')) {
        const style = document.createElement('style');
        style.id = 'uploadAlertStyles';
        style.textContent = `
            @keyframes slideInRight {
                from {
                    transform: translateX(100%);
                    opacity: 0;
                }
                to {
                    transform: translateX(0);
                    opacity: 1;
                }
            }
            @keyframes slideOutRight {
                from {
                    transform: translateX(0);
                    opacity: 1;
                }
                to {
                    transform: translateX(100%);
                    opacity: 0;
                }
            }
        `;
        document.head.appendChild(style);
    }
    
    alert.innerHTML = `
        <div style="display: flex; align-items: flex-start; gap: 12px;">
            <div style="font-size: 20px; flex-shrink: 0;">
                ${palette.icon}
            </div>
            <div style="flex: 1;">
                <div style="font-weight: 600; font-size: 16px; margin-bottom: 4px;">${title}</div>
                <div style="font-size: 14px; line-height: 1.4; opacity: 0.9;">${message}</div>
            </div>
            <button data-action="removeGrandparent" style="
                background: none;
                border: none;
                color: inherit;
                font-size: 20px;
                cursor: pointer;
                padding: 0;
                width: 24px;
                height: 24px;
                display: flex;
                align-items: center;
                justify-content: center;
                opacity: 0.7;
                flex-shrink: 0;
            ">&times;</button>
        </div>
    `;
    
    // Add click to dismiss
    alert.addEventListener('click', () => {
        alert.style.animation = 'slideOutRight 0.3s ease-out';
        setTimeout(() => alert.remove(), 300);
    });
    
    alertContainer.appendChild(alert);
    
    // Auto-remove after 8 seconds for errors, 5 seconds for success
    setTimeout(() => {
        if (alert.parentElement) {
            alert.style.animation = 'slideOutRight 0.3s ease-out';
            setTimeout(() => alert.remove(), 300);
        }
    }, type === 'error' ? 8000 : 5000);
}

function closeUploadModal() {
    const modal = document.getElementById('uploadModal');
    if (modal) {
        if (window.ModalStack && window.ModalStack.isOpen(modal)) {
            // The stack's onClose hook re-enters this function once the
            // entry has left the stack; the cleanup below then runs once.
            window.ModalStack.close(modal);
            return;
        }
        // An upload awaiting a decision has a file and a claim ticket on the
        // server. Closing the dialog is an answer — "not now" — so say so
        // rather than leaving it to expire.
        if (typeof pendingDecision !== 'undefined' && pendingDecision) {
            cancelPendingUpload(pendingDecision.token);
            hideEnrollmentDecision();
        }
        modal.style.display = 'none';
        modal.classList.remove('active');
        // Reset form
        const form = modal.querySelector('.upload-form');
        if (form) form.reset();
        const preview = document.getElementById('globalFilePreview');
        if (preview) preview.classList.remove('show');
        const uploadArea = document.getElementById('globalFileUploadArea');
        if (uploadArea) uploadArea.classList.remove('active');
        const successMsg = document.getElementById('uploadSuccessMessage');
        if (successMsg) successMsg.style.display = 'none';
    }
}

function closeUploadModalOnOutsideClick(event) {
    // With the modal stack present, backdrop clicks are handled centrally
    // (the modal opts in via backdropClose at open time) — reacting here too
    // would double-close. Kept registered so the data-action name resolves.
    if (window.ModalStack) { return; }
    if (event.target === document.getElementById('uploadModal')) {
        closeUploadModal();
    }
}

// Upload limits, fetched once from GET /api/dashboard/config.
//
// These were literals: a 5 MB ceiling against an enforced MAX_FILE_SIZE of
// 10 MB, with the message "File size exceeds 5MB limit". Half of every legal
// upload was refused in the browser and the user was told a limit that was
// not the real one. Null until the fetch lands; a client-side check that has
// not loaded yet simply defers to the server, which enforces it anyway.
const uploadLimits = { maxFileSizeBytes: null, allowedExtensions: null };

async function loadUploadLimits() {
    try {
        const response = await fetch('/api/dashboard/config', { credentials: 'same-origin' });
        if (!response.ok) return;
        const payload = await response.json();
        const cfg = (payload && payload.config) || {};
        if (Number.isFinite(cfg.max_file_size_bytes)) {
            uploadLimits.maxFileSizeBytes = cfg.max_file_size_bytes;
        }
        if (Array.isArray(cfg.allowed_extensions) && cfg.allowed_extensions.length) {
            uploadLimits.allowedExtensions = cfg.allowed_extensions.map(
                ext => String(ext).replace(/^\./, '').toLowerCase());
        }
    } catch (err) {
        // The server enforces the real limit; a failed pre-check is not fatal.
        console.warn('[UPLOAD] Could not load upload limits, deferring to server:', err);
    }
}

if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', loadUploadLimits, { once: true });
    } else {
        loadUploadLimits();
    }
}

function formatMegabytes(bytes) {
    return `${Math.round((bytes / (1024 * 1024)) * 10) / 10}MB`;
}

function handleGlobalFileSelect(event) {
    const file = event.target.files[0];
    if (!file) return;

    // Size limit from the server, never a literal.
    const maxSize = uploadLimits.maxFileSizeBytes;
    if (maxSize && file.size > maxSize) {
        alert(`File size exceeds the ${formatMegabytes(maxSize)} limit. Please choose a smaller file.`);
        event.target.value = '';
        return;
    }

    // Extension list from the server when available, with a MIME-type check
    // as the always-available fallback.
    const extension = (file.name.split('.').pop() || '').toLowerCase();
    if (uploadLimits.allowedExtensions) {
        if (!uploadLimits.allowedExtensions.includes(extension)) {
            alert(`Invalid file type. Allowed: ${uploadLimits.allowedExtensions.join(', ')}.`);
            event.target.value = '';
            return;
        }
    } else if (!['image/png', 'image/jpeg', 'image/jpg', 'image/webp'].includes(file.type)) {
        alert('Invalid file type. Please upload a PNG, JPG, or WEBP image.');
        event.target.value = '';
        return;
    }

    // Show preview
    const preview = document.getElementById('globalFilePreview');
    const previewImage = document.getElementById('globalPreviewImage');
    const fileInfo = document.getElementById('globalFileInfo');
    const uploadArea = document.getElementById('globalFileUploadArea');
    const submitBtn = document.getElementById('globalUploadSubmitBtn');

    if (preview && previewImage && fileInfo && uploadArea && submitBtn) {
        const reader = new FileReader();
        reader.onload = function(e) {
            previewImage.src = e.target.result;
            preview.classList.add('show');
            uploadArea.classList.add('active');
            fileInfo.textContent = `${file.name} (${(file.size / 1024).toFixed(2)} KB)`;
            submitBtn.disabled = false;
        };
        reader.readAsDataURL(file);
    }
}

function handleGlobalUpload(event) {
    event.preventDefault();
    
    const personNameInput = document.getElementById('globalPersonName');
    const fileInput = document.getElementById('globalFileInput');
    const submitBtn = document.getElementById('globalUploadSubmitBtn');
    const successMsg = document.getElementById('uploadSuccessMessage');
    const successText = document.getElementById('uploadSuccessText');

    if (!personNameInput || !fileInput) {
        alert('Form elements not found. Please refresh the page.');
        return;
    }

    const personName = personNameInput.value.trim();
    const file = fileInput.files[0];

    if (!personName) {
        alert('Please enter a person name.');
        return;
    }

    if (!file) {
        alert('Please select a photo to upload.');
        return;
    }

    // Disable submit button
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Uploading...';
    }

    // Create form data
    // Get face image toggle value
    const isFaceImageCheckbox = document.getElementById('globalIsFaceImage');
    const isFaceImage = isFaceImageCheckbox ? isFaceImageCheckbox.checked : false;
    
    const formData = new FormData();
    formData.append('person_name', personName);
    formData.append('photo', file);
    formData.append('is_face_image', isFaceImage.toString());

    // Send to server - backend authenticates via cookies
    fetch('/api/upload-person', {
        method: 'POST',
        credentials: 'include', // Include HttpOnly cookies
        body: formData
    })
    .then(async response => {
        const data = await response.json();

        // The server recognised this face as somebody already on file and is
        // asking who it is, rather than quietly creating a second record for
        // the same person. Checked BEFORE the error handling below: this is a
        // 202 and an expected outcome, not a failure.
        if (response.status === 202 && data.decision_required) {
            showEnrollmentDecision(data);
            return;
        }

        if (!response.ok || !data.success) {
            // Handle different error types with specific alerts
            let errorTitle = 'Upload Failed';
            let errorMessage = data.message || data.details || `HTTP error! status: ${response.status}`;
            
            if (response.status === 403) {
                if (errorMessage.includes('blocked') || errorMessage.includes('forbidden')) {
                    showUploadAlert('Access Denied', 'Only administrators can add persons to track.', 'error');
                    return;
                }
            }
            
            // Handle specific embedding errors
            if (data.error) {
                switch(data.error) {
                    case 'no_face':
                        // Use popup modal for face detection errors
                        const faceMessage = data.details || data.message || 'The image does not contain a detectable face. This may happen if the image was processed before face detection was enabled. Please try a different image with better lighting and a clear view of the person\'s face.';
                        showFaceDetectionAlert(faceMessage);
                        return; // Don't show regular alert for face detection errors
                    case 'embedding_failed':
                        errorTitle = 'Embedding Generation Failed';
                        errorMessage = data.details || data.message || 'Failed to generate face embedding.';
                        break;
                    case 'invalid_embedding':
                        errorTitle = 'Invalid Embedding';
                        errorMessage = data.details || data.message || 'Invalid face embedding generated.';
                        break;
                    case 'embedding_save_failed':
                        errorTitle = 'Database Save Failed';
                        errorMessage = data.details || data.message || 'Failed to save embedding to database.';
                        break;
                    case 'embedding_save_error':
                        errorTitle = 'Save Error';
                        errorMessage = data.details || data.message || 'Error saving embedding.';
                        break;
                    case 'service_unavailable':
                        errorTitle = 'Service Unavailable';
                        errorMessage = data.details || data.message || 'Identity service is not available.';
                        break;
                }
            }
            
            showUploadAlert(errorTitle, errorMessage, 'error');
            return;
        }
        
        // Success
        if (data.success) {
            // Same rule as the review flow: a duplicate is a successful no-op.
            // The server stored nothing, so this must not look like an add.
            if (data.duplicate === true || data.image_created === false) {
                showUploadAlert(
                    'Nothing was added',
                    (data.message || 'This exact image is already stored for that '
                     + 'person.')
                    + ' Nothing was added. Upload a different photo of them to add '
                    + 'another image.',
                    'info');
                setTimeout(closeUploadModal, 500);
                return;
            }
            // Show success message
            if (successMsg && successText) {
                successText.textContent = `✅ ${data.message} (Total: ${data.total_faces || 0} faces)`;
                successMsg.style.display = 'block';
            }

            // Reset form after short delay
            setTimeout(() => {
                if (personNameInput) personNameInput.value = '';
                if (fileInput) fileInput.value = '';
                const preview = document.getElementById('globalFilePreview');
                if (preview) preview.classList.remove('show');
                const uploadArea = document.getElementById('globalFileUploadArea');
                if (uploadArea) uploadArea.classList.remove('active');
                if (successMsg) successMsg.style.display = 'none';
                closeUploadModal();
            }, 2000);
        }
    })
    .catch(error => {
        console.error('Upload error:', error);
        alert(error.message || 'Failed to upload person. Please try again.');
    })
    .finally(() => {
        // Re-enable submit button
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = '<i class="fas fa-upload"></i> Upload Person';
        }
    });
}


// Face detection alert — FALLBACK implementation only.
//
// This script is injected at runtime and therefore executes AFTER any page
// script. It used to declare these two as plain function declarations, which
// silently REPLACED the page's own implementations at global scope (the
// Unknown Faces page ships the canonical pair in admin-unknown.js). The
// guards make the page's copy win: exactly one implementation is live after
// all scripts load — the page's where one exists, this fallback elsewhere.
if (typeof window.showFaceDetectionAlert !== 'function') {
    window.showFaceDetectionAlert = function (message) {
        const modal = document.getElementById('face-detection-alert-modal');
        const messageElement = document.getElementById('face-alert-message');

        if (modal && messageElement) {
            messageElement.textContent = message;
            if (window.ModalStack) {
                window.ModalStack.open(modal, { backdropClose: true });
            } else {
                modal.style.display = 'flex';
            }

            // Add pulse animation
            const icon = modal.querySelector('.fa-user-slash');
            if (icon && icon.parentElement) {
                icon.parentElement.style.animation = 'pulse 2s infinite';
            }
        } else {
            // Fallback to notification if modal not found
            showUploadAlert('No Face Detected', message, 'error');
        }
    };
}

if (typeof window.closeFaceDetectionAlert !== 'function') {
    window.closeFaceDetectionAlert = function () {
        const modal = document.getElementById('face-detection-alert-modal');
        if (modal) {
            if (window.ModalStack) {
                window.ModalStack.close(modal);
            } else {
                modal.style.display = 'none';
            }
        }
    };
}

// Initialize face detection alert modal event listeners
function initFaceDetectionAlertModal() {
    const faceAlertModal = document.getElementById('face-detection-alert-modal');
    if (faceAlertModal) {
        // The Unknown Faces page wires these same buttons itself; a second
        // listener would only repeat an idempotent close. Bind only when the
        // page has NOT provided its own implementation (i.e. we installed
        // the fallback above and own this modal's wiring).
        const pageOwnsAlert = window.__pageOwnsFaceAlertWiring === true;
        if (!pageOwnsAlert) {
            const closeBtn = document.getElementById('close-face-alert-modal');
            if (closeBtn) {
                closeBtn.addEventListener('click', window.closeFaceDetectionAlert);
            }

            const okBtn = document.getElementById('face-alert-ok-btn');
            if (okBtn) {
                okBtn.addEventListener('click', window.closeFaceDetectionAlert);
            }
        }

        // Backdrop-click and Escape belong to ModalStack when it is present
        // (bound exactly once at document level). These legacy bindings exist
        // only for pages without the stack.
        if (!window.ModalStack) {
            faceAlertModal.addEventListener('click', (e) => {
                if (e.target === faceAlertModal) {
                    window.closeFaceDetectionAlert();
                }
            });

            document.addEventListener('keydown', (e) => {
                if (e.key === 'Escape' && faceAlertModal.style.display === 'flex') {
                    window.closeFaceDetectionAlert();
                }
            });
        }
    }
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initFaceDetectionAlertModal);
} else {
    initFaceDetectionAlertModal();
}

// ---------------------------------------------------------------------------
// Identity review
//
// POST /api/upload-person answers 202 + decision_required when the uploaded
// face matches somebody already enrolled. At that point NOTHING has been
// saved: the photo is held server-side under a single-use token with a short
// expiry, and one of the three actions below resolves it. Closing the modal
// cancels, so an abandoned dialog does not leave an upload in limbo.
// ---------------------------------------------------------------------------

// The token for the upload currently under review, or null. Also the flag that
// tells closeUploadModal() there is something to cancel.
let pendingDecision = null;

function decisionPanel() {
    return document.getElementById('enrollmentDecision');
}

function showEnrollmentDecision(data) {
    const panel = decisionPanel();
    const form = document.getElementById('uploadPersonForm');
    const list = document.getElementById('enrollmentCandidates');
    const message = document.getElementById('enrollmentDecisionMessage');
    const duplicate = document.getElementById('enrollmentDuplicateNotice');
    if (!panel || !list) {
        // No panel on this page: cancel server-side rather than strand the
        // upload, and fall back to telling the operator what happened.
        cancelPendingUpload(data.upload_token);
        showUploadAlert('Person Already On File',
            data.message || 'This photo matches someone already enrolled.', 'error');
        return;
    }

    pendingDecision = {
        token: data.upload_token,
        displayName: data.display_name || '',
        strong: data.match_confidence === 'strong'
    };

    const candidates = data.candidate_identities || [];
    // The person who already holds these EXACT bytes. Adding to them is a
    // correct no-op, so that action is withdrawn rather than offered.
    const duplicateId = data.duplicate_of_identity_id || null;
    const duplicateOf = duplicateId
        ? candidates.find(c => c.identity_id === duplicateId)
        : null;
    const duplicateName = duplicateOf
        ? (duplicateOf.display_name || 'that person')
        : null;

    if (message) {
        // The server's own message recommends adding to the best match. When
        // the best match is the one that already has this exact file, that
        // recommendation is wrong, so it does not get shown.
        message.textContent = duplicateName
            ? 'This upload already exists on file. Choose a different person, '
              + 'or cancel.'
            : (data.message || '');
    }
    if (duplicate) {
        duplicate.style.display = duplicateName ? 'flex' : 'none';
        const duplicateText = document.getElementById('enrollmentDuplicateText');
        if (duplicateText && duplicateName) {
            duplicateText.textContent =
                `This exact image is already stored for ${duplicateName}. `
                + 'Nothing will be added.';
        }
    }

    const label = document.getElementById('enrollmentCreateNewLabel');
    if (label) {
        label.textContent = pendingDecision.strong
            ? 'No, this is a different person'
            : 'Create as a new person';
    }

    list.textContent = '';
    candidates.forEach((candidate, index) => {
        const holdsThisFile = Boolean(duplicateId)
            && candidate.identity_id === duplicateId;
        // Never recommend the duplicate holder, even when it ranks first.
        const recommended = index === 0 && pendingDecision.strong && !holdsThisFile;
        list.appendChild(buildCandidateRow(candidate, recommended, holdsThisFile));
    });

    if (form) form.style.display = 'none';
    panel.style.display = 'flex';
}

function buildCandidateRow(candidate, recommended, holdsThisFile) {
    const row = document.createElement('div');
    row.className = 'enrollment-candidate'
        + (recommended ? ' enrollment-candidate--recommended' : '')
        + (holdsThisFile ? ' enrollment-candidate--duplicate' : '');

    // Built with DOM calls rather than innerHTML: display_name is operator-
    // supplied text, and textContent cannot execute it.
    const avatar = document.createElement('img');
    avatar.className = 'enrollment-candidate__avatar';
    avatar.alt = '';
    if (candidate.preview_image) avatar.src = candidate.preview_image;
    row.appendChild(avatar);

    const body = document.createElement('div');
    body.className = 'enrollment-candidate__body';

    const name = document.createElement('div');
    name.className = 'enrollment-candidate__name';
    name.textContent = candidate.display_name || 'Unnamed person';
    if (holdsThisFile) {
        const badge = document.createElement('span');
        badge.className = 'enrollment-candidate__badge enrollment-candidate__badge--duplicate';
        badge.textContent = 'Already has this photo';
        name.appendChild(badge);
    } else if (recommended) {
        const badge = document.createElement('span');
        badge.className = 'enrollment-candidate__badge';
        badge.textContent = 'Best match';
        name.appendChild(badge);
    }
    body.appendChild(name);

    const meta = document.createElement('div');
    meta.className = 'enrollment-candidate__meta';
    const percent = Math.round((candidate.similarity || 0) * 100);
    const band = (candidate.confidence_band || '').replace(/_/g, ' ').toLowerCase();
    meta.textContent = holdsThisFile
        ? 'This is the same file, so adding it stores nothing new'
        : `${percent}% match${band ? ' · ' + band + ' confidence' : ''}`;
    body.appendChild(meta);
    row.appendChild(body);

    const choose = document.createElement('button');
    choose.type = 'button';
    choose.className = 'enrollment-candidate__choose';

    if (holdsThisFile) {
        // Withdrawn, not relabelled. "Add to this person" here would store
        // nothing, and offering an action that cannot do what it says is what
        // sent operators looking under storage/faces/<uuid>/ for a file the
        // server had correctly declined to write.
        //
        // `disabled` is load-bearing twice: it stops the click, and actions.js
        // refuses to dispatch for a disabled element (Actions.isDisabled), so
        // the handler cannot fire even if something re-enables the pointer.
        choose.textContent = 'Already stored';
        choose.disabled = true;
        choose.setAttribute('aria-disabled', 'true');
        // Permanently disabled, not busy-disabled. setDecisionBusy() re-enables
        // every button when a recoverable refusal returns, and without this
        // marker it would hand the withdrawn action back to the operator.
        choose.dataset.permanentlyDisabled = 'true';
        choose.title = 'This exact image is already stored for this person.';
    } else {
        choose.textContent = 'Add to this person';
        choose.dataset.action = 'enrollmentAddToExisting';
        choose.dataset.identityId = candidate.identity_id;
    }
    row.appendChild(choose);

    return row;
}

function hideEnrollmentDecision() {
    const panel = decisionPanel();
    const form = document.getElementById('uploadPersonForm');
    if (panel) panel.style.display = 'none';
    if (form) form.style.display = '';
    const list = document.getElementById('enrollmentCandidates');
    if (list) list.textContent = '';
    pendingDecision = null;
}

function setDecisionBusy(busy) {
    const panel = decisionPanel();
    if (!panel) return;
    panel.querySelectorAll('button').forEach(button => {
        if (button.dataset.permanentlyDisabled === 'true') return;
        button.disabled = busy;
    });
}

function submitDecision(payload) {
    setDecisionBusy(true);
    return fetch('/api/enrollment/confirm', {
        method: 'POST',
        credentials: 'include',
        headers: {
            'Content-Type': 'application/json',
            // Cookie-authenticated mutation: required by require_upload_csrf.
            'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify(payload)
    })
    .then(async response => {
        const data = await response.json();

        // A strong match needs the "different person" answer twice. The second
        // press carries confirm_create_new, so this asks once and only once.
        if (response.status === 409 && data.requires_confirmation) {
            setDecisionBusy(false);
            if (window.confirm(data.message)) {
                return submitDecision(Object.assign({}, payload,
                    { confirm_create_new: true }));
            }
            return;
        }

        if (!response.ok || !data.success) {
            setDecisionBusy(false);
            // The upload is gone in these cases; let the operator start over
            // rather than leave a panel whose buttons cannot work.
            if (response.status === 410) {
                hideEnrollmentDecision();
                closeUploadModal();
            }
            showUploadAlert('Could Not Save',
                data.message || 'The decision could not be applied.', 'error');
            return;
        }

        // A duplicate is a SUCCESSFUL no-op: the request worked and the photo
        // was already on file, so nothing was stored. Reporting that with the
        // same green tick as a real addition sent operators looking for an
        // image_002.jpg that, correctly, was never written. `image_created`
        // is the authoritative flag; `duplicate` is the reason.
        const nothingStored = data.duplicate === true || data.image_created === false;
        hideEnrollmentDecision();

        if (nothingStored) {
            showUploadAlert(
                'Already On File',
                (data.message || 'This photo is already stored for that person.')
                + ' Nothing was added. Upload a different photo of them to add '
                + 'another image.',
                'info');
            setTimeout(closeUploadModal, 500);
            return;
        }

        const successMsg = document.getElementById('uploadSuccessMessage');
        const successText = document.getElementById('uploadSuccessText');
        if (successMsg && successText) {
            successText.textContent = `✅ ${data.message}`;
            successMsg.style.display = 'block';
        }
        setTimeout(closeUploadModal, 1500);
    })
    .catch(error => {
        console.error('Enrollment decision error:', error);
        setDecisionBusy(false);
        showUploadAlert('Could Not Save',
            'The decision could not be applied. Please try again.', 'error');
    });
}

function cancelPendingUpload(token) {
    if (!token) return;
    // keepalive so the cancel still reaches the server when this fires from a
    // modal close that is followed by a navigation.
    fetch('/api/enrollment/cancel', {
        method: 'POST',
        credentials: 'include',
        keepalive: true,
        headers: {
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify({ upload_token: token })
    }).catch(() => { /* the upload expires on its own within minutes */ });
}

// Make functions globally available
window.openUploadModal = openUploadModal;
window.closeUploadModal = closeUploadModal;
window.closeUploadModalOnOutsideClick = closeUploadModalOnOutsideClick;
window.handleGlobalFileSelect = handleGlobalFileSelect;
window.handleGlobalUpload = handleGlobalUpload;
window.showFaceDetectionAlert = showFaceDetectionAlert;
window.closeFaceDetectionAlert = closeFaceDetectionAlert;



// ---------------------------------------------------------------------------
// CSP-safe event registration
//
// These handlers were previously reached through inline onclick/onchange/
// onsubmit attributes, which `script-src 'self'` blocks. Registered rather
// than looked up on window, so only these names are invocable; delegated in
// actions.js, so dynamically rendered elements work without rebinding.
// ---------------------------------------------------------------------------
Actions.register({
    closeUploadModal,
    // The inner .upload-modal-content used to call event.stopPropagation() to
    // avoid this. Comparing event.target to the overlay is equivalent and does
    // not need a second handler: a click on the content never matches.
    closeUploadModalOnOutsideClick: (el, event) => {
        if (event.target === el) closeUploadModal();
    },
    handleGlobalUpload: (el, event) => handleGlobalUpload(event),
    handleGlobalFileSelect: (el, event) => handleGlobalFileSelect(event),
    // Identity review. The candidate rows are rendered at runtime, which is
    // exactly why these are delegated actions rather than bound listeners.
    enrollmentAddToExisting: (el) => {
        if (!pendingDecision) return;
        submitDecision({
            action: 'add_to_existing',
            identity_id: el.dataset.identityId,
            upload_token: pendingDecision.token
        });
    },
    enrollmentCreateNew: () => {
        if (!pendingDecision) return;
        submitDecision({
            action: 'create_new',
            display_name: pendingDecision.displayName,
            upload_token: pendingDecision.token
        });
    },
    enrollmentCancel: () => {
        if (!pendingDecision) return;
        const token = pendingDecision.token;
        hideEnrollmentDecision();
        cancelPendingUpload(token);
        closeUploadModal();
    },
    triggerFileInput: (el) => {
        const input = document.getElementById(el.dataset.targetInput);
        if (input) input.click();
    },
    removeGrandparent: (el) => {
        const target = el.parentElement && el.parentElement.parentElement;
        if (target) target.remove();
    },
    openUploadModal: (el, event) => {
        // Was an <a href="#">, so the default jump must still be suppressed.
        event.preventDefault();
        if (typeof openUploadModal === 'function') {
            openUploadModal();
        } else {
            console.warn('Upload modal not loaded yet');
        }
    },
});
