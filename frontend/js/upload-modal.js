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
            modal.style.display = 'flex';
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
function showUploadAlert(title, message, type = 'error') {
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
        background: ${type === 'error' ? '#f8d7da' : '#d4edda'};
        border: 1px solid ${type === 'error' ? '#f5c6cb' : '#c3e6cb'};
        color: ${type === 'error' ? '#721c24' : '#155724'};
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
                ${type === 'error' ? '⚠️' : '✅'}
            </div>
            <div style="flex: 1;">
                <div style="font-weight: 600; font-size: 16px; margin-bottom: 4px;">${title}</div>
                <div style="font-size: 14px; line-height: 1.4; opacity: 0.9;">${message}</div>
            </div>
            <button onclick="this.parentElement.parentElement.remove()" style="
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
    if (event.target === document.getElementById('uploadModal')) {
        closeUploadModal();
    }
}

function handleGlobalFileSelect(event) {
    const file = event.target.files[0];
    if (!file) return;

    // Validate file size (5MB max)
    const maxSize = 5 * 1024 * 1024; // 5MB in bytes
    if (file.size > maxSize) {
        alert('File size exceeds 5MB limit. Please choose a smaller file.');
        event.target.value = '';
        return;
    }

    // Validate file type
    const allowedTypes = ['image/png', 'image/jpeg', 'image/jpg', 'image/webp'];
    if (!allowedTypes.includes(file.type)) {
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


// Show face detection alert modal (popup in front of user)
function showFaceDetectionAlert(message) {
    const modal = document.getElementById('face-detection-alert-modal');
    const messageElement = document.getElementById('face-alert-message');
    
    if (modal && messageElement) {
        messageElement.textContent = message;
        modal.style.display = 'flex';
        modal.style.zIndex = '99999'; // Ensure it's on top of everything
        
        // Add pulse animation
        const icon = modal.querySelector('.fa-user-slash');
        if (icon && icon.parentElement) {
            icon.parentElement.style.animation = 'pulse 2s infinite';
        }
    } else {
        // Fallback to notification if modal not found
        showUploadAlert('No Face Detected', message, 'error');
    }
}

// Close face detection alert modal
function closeFaceDetectionAlert() {
    const modal = document.getElementById('face-detection-alert-modal');
    if (modal) {
        modal.style.display = 'none';
    }
}

// Initialize face detection alert modal event listeners
function initFaceDetectionAlertModal() {
    const faceAlertModal = document.getElementById('face-detection-alert-modal');
    if (faceAlertModal) {
        // Close button
        const closeBtn = document.getElementById('close-face-alert-modal');
        if (closeBtn) {
            closeBtn.addEventListener('click', closeFaceDetectionAlert);
        }
        
        // OK button
        const okBtn = document.getElementById('face-alert-ok-btn');
        if (okBtn) {
            okBtn.addEventListener('click', closeFaceDetectionAlert);
        }
        
        // Close on background click
        faceAlertModal.addEventListener('click', (e) => {
            if (e.target === faceAlertModal) {
                closeFaceDetectionAlert();
            }
        });
        
        // Close on Escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && faceAlertModal.style.display === 'flex') {
                closeFaceDetectionAlert();
            }
        });
    }
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initFaceDetectionAlertModal);
} else {
    initFaceDetectionAlertModal();
}

// Make functions globally available
window.openUploadModal = openUploadModal;
window.closeUploadModal = closeUploadModal;
window.closeUploadModalOnOutsideClick = closeUploadModalOnOutsideClick;
window.handleGlobalFileSelect = handleGlobalFileSelect;
window.handleGlobalUpload = handleGlobalUpload;
window.showFaceDetectionAlert = showFaceDetectionAlert;
window.closeFaceDetectionAlert = closeFaceDetectionAlert;

