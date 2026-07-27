// Admin Users Management JavaScript
// BACKEND HANDLES ALL AUTHENTICATION
let allPipelines = [];
let currentUsers = [];

document.addEventListener('DOMContentLoaded', async () => {
    // Backend already authenticated user before serving this page

    // Load pipelines
    await loadPipelines();
    
    // Load users
    await loadUsers();

    // Event listeners
    const createBtn = document.getElementById('create-user-btn');
    if (createBtn) {
        createBtn.addEventListener('click', () => {
            console.log('[CREATE_BTN] Create user button clicked');
            openCreateModal();
        });
    } else {
        console.error('[INIT] Create user button not found!');
    }
    
    const userForm = document.getElementById('user-form');
    if (userForm) {
        userForm.addEventListener('submit', handleUserSubmit);
    } else {
        console.error('[INIT] User form not found!');
    }
    
    const passwordForm = document.getElementById('password-form');
    if (passwordForm) {
        passwordForm.addEventListener('submit', handlePasswordReset);
    } else {
        console.error('[INIT] Password form not found!');
    }
    
    const logoutBtn = document.getElementById('logout-btn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', () => {
            localStorage.removeItem('access_token');
            window.location.href = '/signin';
        });
    } else {
        console.error('[INIT] Logout button not found!');
    }
});

async function loadPipelines() {
    try {
        const response = await fetch('/api/pipelines', {
            credentials: 'include' // Include HttpOnly cookies
        });
        if (response.ok) {
            allPipelines = await response.json();
        }
    } catch (error) {
        console.error('Error loading pipelines:', error);
    }
}

async function loadUsers() {
    try {
        const response = await fetch('/api/users', {
            credentials: 'include' // Include HttpOnly cookies
        });
        
        if (!response.ok) throw new Error('Failed to load users');
        
        currentUsers = await response.json();
        renderUsersTable();
    } catch (error) {
        document.getElementById('users-table-body').innerHTML = 
            `<tr><td colspan="8" class="loading">Error loading users: ${error.message}</td></tr>`;
    }
}

function renderUsersTable() {
    const tbody = document.getElementById('users-table-body');
    
    if (currentUsers.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="loading">No users found</td></tr>';
        return;
    }

    tbody.innerHTML = currentUsers.map(user => {
        const isBlocked = user.blocked_reason || user.blocked_at;
        const blockedBadge = isBlocked 
            ? `<span class="badge badge-danger" title="${user.blocked_reason || 'Blocked'}">BLOCKED</span>` 
            : '';
        const statusBadge = user.is_active 
            ? `<span class="badge badge-active">Active</span>` 
            : `<span class="badge badge-inactive">Inactive</span>`;
        
        return `
        <tr ${isBlocked ? 'class="user-blocked"' : ''}>
            <td>${user.username} ${blockedBadge}</td>
            <td>${user.email}</td>
            <td>${user.full_name || '-'}</td>
            <td><span class="badge badge-${user.role}">${user.role}</span></td>
            <td><span class="badge badge-${user.can_use_chatbot ? 'yes' : 'no'}">${user.can_use_chatbot ? 'Yes' : 'No'}</span></td>
            <td>${user.pipeline_ids.length} pipeline(s)</td>
            <td>${statusBadge}</td>
            <td>
                ${isBlocked ? `<button class="btn-action btn-success" data-action="unblockUser" data-arg="${user.id}" title="Unblock user">Unblock</button>` : ''}
                <button class="btn-action" data-action="editUser" data-arg="${user.id}">Edit</button>
                <button class="btn-action" data-action="resetPassword" data-arg="${user.id}">Reset Password</button>
                <button class="btn-action btn-danger" data-action="deleteUser" data-arg="${user.id}">Delete</button>
            </td>
        </tr>
    `;
    }).join('');
}

function editUser(userId) {
    console.log(`[EDIT_USER] Opening edit modal for user ID: ${userId}`);
    
    const user = currentUsers.find(u => u.id === userId);
    if (!user) {
        console.error(`[EDIT_USER] User with ID ${userId} not found`);
        alert('User not found');
        return;
    }

    try {
        // Update modal title - preserve the structure
        const titleElement = document.getElementById('modal-title');
        if (titleElement) {
            const titleText = titleElement.querySelector('.title-text');
            if (titleText) {
                titleText.textContent = 'USER ACCOUNT MANAGEMENT';
            }
        }
        
        // Update subtitle
        const subtitleElement = document.getElementById('modal-subtitle');
        if (subtitleElement) {
            subtitleElement.textContent = 'EDIT EXISTING USER PROFILE';
        }
        
        // Update form fields
        const userIdField = document.getElementById('user-id');
        if (userIdField) {
            userIdField.value = user.id;
        }
        
        const usernameField = document.getElementById('modal-username');
        if (usernameField) {
            usernameField.value = user.username;
        }
        
        const emailField = document.getElementById('modal-email');
        if (emailField) {
            emailField.value = user.email;
        }
        
        const fullNameField = document.getElementById('modal-full-name');
        if (fullNameField) {
            fullNameField.value = user.full_name || '';
        }
        
        const roleField = document.getElementById('modal-role');
        if (roleField) {
            roleField.value = user.role;
        }
        
        const chatbotField = document.getElementById('modal-chatbot-access');
        if (chatbotField) {
            chatbotField.checked = user.can_use_chatbot;
        }
        
        const isActiveField = document.getElementById('modal-is-active');
        if (isActiveField) {
            isActiveField.checked = user.is_active;
        }
        
        const passwordField = document.getElementById('modal-password');
        if (passwordField) {
            passwordField.required = false;
            passwordField.value = ''; // Clear password field when editing
        }
        
        // Update password badge to OPTIONAL when editing
        const passwordBadge = document.getElementById('password-badge');
        if (passwordBadge) {
            passwordBadge.textContent = 'OPTIONAL';
            passwordBadge.className = 'security-badge optional';
        }
        
        // Render pipeline checkboxes
        renderPipelineCheckboxes(user.pipeline_ids || []);
        
        // Show the modal
        const modal = document.getElementById('user-modal');
        if (modal) {
            modal.style.display = 'flex';
            console.log(`[EDIT_USER] Modal displayed for user: ${user.username}`);
        } else {
            console.error('[EDIT_USER] Modal element not found!');
            alert('Error: Modal element not found. Please refresh the page.');
        }
    } catch (error) {
        console.error('[EDIT_USER] Error opening edit modal:', error);
        alert('Error opening edit form: ' + error.message);
    }
}

function openCreateModal() {
    console.log('[CREATE_MODAL] Opening create user modal...');
    
    try {
        // Reset form
        const form = document.getElementById('user-form');
        if (form) {
            form.reset();
        }
        
        const userIdField = document.getElementById('user-id');
        if (userIdField) {
            userIdField.value = '';
        }
        
        // Update modal title - preserve the structure
        const titleElement = document.getElementById('modal-title');
        if (titleElement) {
            const titleText = titleElement.querySelector('.title-text');
            if (titleText) {
                titleText.textContent = 'USER ACCOUNT MANAGEMENT';
            }
        }
        
        // Update subtitle
        const subtitleElement = document.getElementById('modal-subtitle');
        if (subtitleElement) {
            subtitleElement.textContent = 'CREATE NEW USER PROFILE';
        }
        
        // Update password badge to REQUIRED when creating
        const passwordBadge = document.getElementById('password-badge');
        if (passwordBadge) {
            passwordBadge.textContent = 'REQUIRED';
            passwordBadge.className = 'security-badge required';
        }
        
        // Set defaults
        const isActiveCheckbox = document.getElementById('modal-is-active');
        if (isActiveCheckbox) {
            isActiveCheckbox.checked = true;
        }
        
        const passwordField = document.getElementById('modal-password');
        if (passwordField) {
            passwordField.required = true;
            passwordField.value = '';
        }
        
        renderPipelineCheckboxes([]);
        
        // Show the modal
        const modal = document.getElementById('user-modal');
        if (modal) {
            modal.style.display = 'flex';
            console.log('[CREATE_MODAL] Modal displayed successfully');
        } else {
            console.error('[CREATE_MODAL] Modal element not found!');
            alert('Error: Modal element not found. Please refresh the page.');
        }
    } catch (error) {
        console.error('[CREATE_MODAL] Error opening modal:', error);
        alert('Error opening create user form: ' + error.message);
    }
}

function resetPassword(userId) {
    document.getElementById('password-user-id').value = userId;
    document.getElementById('password-form').reset();
    document.getElementById('password-modal').style.display = 'flex';
}

let pendingDeleteUserId = null;

function showDeleteModal(userId) {
    // Find the user to get their name
    const user = currentUsers.find(u => u.id === userId);
    const userName = user ? user.username : 'this user';
    const userFullName = user && user.full_name ? user.full_name : '';
    
    // Set the user name in the modal
    const nameDisplay = userFullName ? `${userName} (${userFullName})` : userName;
    document.getElementById('delete-user-name').textContent = nameDisplay;
    
    // Store the user ID for confirmation
    pendingDeleteUserId = userId;
    
    // Show the modal
    const modal = document.getElementById('delete-user-modal');
    modal.style.display = 'flex';
    
    // Add event listener to confirm button if not already added
    const confirmBtn = document.getElementById('confirm-delete-btn');
    confirmBtn.onclick = () => confirmDeleteUser();
    
    console.log(`[DELETE_USER] Delete modal shown for user: ${userName} (ID: ${userId})`);
}

function closeDeleteModal() {
    const modal = document.getElementById('delete-user-modal');
    modal.style.display = 'none';
    pendingDeleteUserId = null;
    console.log('[DELETE_USER] Delete modal closed');
}

async function confirmDeleteUser() {
    if (!pendingDeleteUserId) {
        console.error('[DELETE_USER] No pending user ID for deletion');
        return;
    }
    
    const userId = pendingDeleteUserId;
    const user = currentUsers.find(u => u.id === userId);
    const userName = user ? user.username : 'this user';
    
    console.log(`[DELETE_USER] Admin confirmed deletion of user: ${userName} (ID: ${userId})`);
    
    // Close modal immediately
    closeDeleteModal();
    
    // Disable the confirm button to prevent double-clicks
    const confirmBtn = document.getElementById('confirm-delete-btn');
    confirmBtn.disabled = true;
    confirmBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i><span>Deleting...</span>';

    try {
        const response = await fetch(`/api/users/${userId}`, {
            method: 'DELETE',
            headers: { 
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Failed to delete user' }));
            throw new Error(errorData.detail || `HTTP ${response.status}: Failed to delete user`);
        }
        
        const result = await response.json();
        console.log(`[DELETE_USER] User deleted successfully: ${userName}`);
        
        // Show success message
        alert(`✅ User "${userName}" has been permanently deleted.\n\nAll related data (chatbot logs, activity records, etc.) has been removed.`);
        
        await loadUsers();
    } catch (error) {
        console.error('[DELETE_USER] Error:', error);
        alert('❌ Error deleting user: ' + error.message);
    } finally {
        // Re-enable button
        confirmBtn.disabled = false;
        confirmBtn.innerHTML = '<i class="fas fa-trash-alt"></i><span>Delete User</span>';
    }
}

async function deleteUser(userId) {
    showDeleteModal(userId);
}

// function renderPipelineCheckboxes(selectedPipelines) {
//     const container = document.getElementById('pipeline-checkboxes');
//     container.innerHTML = allPipelines.map(pipeline => `
//         <label>
//             <input type="checkbox" value="${pipeline.pipeline_id}" 
//                    ${selectedPipelines.includes(pipeline.pipeline_id) ? 'checked' : ''}>
//             ${pipeline.pipeline_id}
//         </label>
//     `).join('');
// }
function renderPipelineCheckboxes(selectedPipelines = []) {
    const container = document.getElementById('pipeline-checkboxes');

    if (!container) {
        console.error('[PIPELINES] Checkbox container not found');
        return;
    }

    container.innerHTML = allPipelines.map(pipeline => {
        const displayName =
            pipeline.location_name ||
            pipeline.pipeline_name ||
            pipeline.pipeline_id;

        return `
            <label>
                <input
                    type="checkbox"
                    value="${pipeline.pipeline_id}"
                    ${selectedPipelines.includes(pipeline.pipeline_id) ? 'checked' : ''}
                >

                ${displayName}
            </label>
        `;
    }).join('');
}

async function handleUserSubmit(e) {
    e.preventDefault();
    
    const userId = document.getElementById('user-id').value;
    const passwordValue = document.getElementById('modal-password').value.trim();
    
    const formData = {
        username: document.getElementById('modal-username').value,
        email: document.getElementById('modal-email').value,
        full_name: document.getElementById('modal-full-name').value,
        role: document.getElementById('modal-role').value,
        can_use_chatbot: document.getElementById('modal-chatbot-access').checked,
        is_active: document.getElementById('modal-is-active').checked,
        pipeline_ids: Array.from(document.querySelectorAll('#pipeline-checkboxes input:checked'))
            .map(cb => cb.value)
    };

    // Handle password: only include if provided (for both create and update)
    if (passwordValue) {
        formData.password = passwordValue;
        console.log('[USER_FORM] Password provided, will be updated');
    } else if (userId) {
        // Editing existing user and password is empty - don't include it (keep current password)
        console.log('[USER_FORM] Editing user - password empty, keeping current password');
    } else {
        // Creating new user - password is required
        alert('Password is required for new users');
        return;
    }

    try {
        const url = userId ? `/api/users/${userId}` : '/api/users';
        const method = userId ? 'PUT' : 'POST';
        
        console.log(`[USER_FORM] Submitting ${method} request to ${url}`);
        console.log('[USER_FORM] Form data:', { ...formData, password: formData.password ? '***' : undefined });

        const response = await fetch(url, {
            method,
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(formData)
        });

        if (!response.ok) {
            const error = await response.json();
            console.error('[USER_FORM] Error response:', error);
            throw new Error(error.detail || 'Failed to save user');
        }

        const result = await response.json();
        console.log('[USER_FORM] Success:', result);
        
        closeModal();
        await loadUsers();
        
        // Show success message
        if (userId && passwordValue) {
            alert('User updated successfully! Password has been changed.');
        } else if (userId) {
            alert('User updated successfully!');
        } else {
            alert('User created successfully!');
        }
    } catch (error) {
        console.error('[USER_FORM] Error:', error);
        alert('Error saving user: ' + error.message);
    }
}

async function handlePasswordReset(e) {
    e.preventDefault();
    
    const userId = document.getElementById('password-user-id').value;
    const newPassword = document.getElementById('new-password').value;

    try {
        const response = await fetch(`/api/users/${userId}/reset-password`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ new_password: newPassword })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to reset password');
        }

        closePasswordModal();
        alert('Password reset successfully');
    } catch (error) {
        alert('Error resetting password: ' + error.message);
    }
}

function closeModal() {
    document.getElementById('user-modal').style.display = 'none';
}

function closePasswordModal() {
    document.getElementById('password-modal').style.display = 'none';
}

async function unblockUser(userId) {
    if (!confirm('Are you sure you want to unblock this user?')) return;

    try {
        const response = await fetch(`/api/users/${userId}/unblock`, {
            method: 'POST',
            credentials: 'include' // Include HttpOnly cookies
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to unblock user');
        }
        
        await loadUsers();
        alert('User unblocked successfully');
    } catch (error) {
        alert('Error unblocking user: ' + error.message);
    }
}

function filterBlockedUsers() {
    const blockedUsers = currentUsers.filter(user => user.blocked_reason || user.blocked_at);
    
    // Update button states
    const btnBlocked = document.getElementById('btn-blocked-only');
    const btnAll = document.getElementById('btn-all-users');
    if (btnBlocked) {
        btnBlocked.classList.add('btn-active');
    }
    if (btnAll) {
        btnAll.classList.remove('btn-active');
    }
    
    if (blockedUsers.length === 0) {
        const tbody = document.getElementById('users-table-body');
        tbody.innerHTML = '<tr><td colspan="8" class="loading">No blocked users found</td></tr>';
        return;
    }
    const tbody = document.getElementById('users-table-body');
    tbody.innerHTML = blockedUsers.map(user => {
        const isBlocked = true;
        const blockedBadge = `<span class="badge badge-danger" title="${user.blocked_reason || 'Blocked'}">BLOCKED</span>`;
        const statusBadge = user.is_active 
            ? `<span class="badge badge-active">Active</span>` 
            : `<span class="badge badge-inactive">Inactive</span>`;
        
        return `
        <tr class="user-blocked">
            <td>${user.username} ${blockedBadge}</td>
            <td>${user.email}</td>
            <td>${user.full_name || '-'}</td>
            <td><span class="badge badge-${user.role}">${user.role}</span></td>
            <td><span class="badge badge-${user.can_use_chatbot ? 'yes' : 'no'}">${user.can_use_chatbot ? 'Yes' : 'No'}</span></td>
            <td>${user.pipeline_ids.length} pipeline(s)</td>
            <td>${statusBadge}</td>
            <td>
                <button class="btn-action btn-success" data-action="unblockUser" data-arg="${user.id}" title="Unblock user">Unblock</button>
                <button class="btn-action" data-action="editUser" data-arg="${user.id}">Edit</button>
                <button class="btn-action" data-action="resetPassword" data-arg="${user.id}">Reset Password</button>
                <button class="btn-action btn-danger" data-action="deleteUser" data-arg="${user.id}">Delete</button>
            </td>
        </tr>
    `;
    }).join('');
}

function showAllUsers() {
    // Update button states
    const btnBlocked = document.getElementById('btn-blocked-only');
    const btnAll = document.getElementById('btn-all-users');
    if (btnBlocked) {
        btnBlocked.classList.remove('btn-active');
    }
    if (btnAll) {
        btnAll.classList.add('btn-active');
    }
    
    renderUsersTable();
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
    filterBlockedUsers,
    showAllUsers,
    closeModal,
    closeDeleteModal,
    closePasswordModal,
    unblockUser: (el) => { const id = Actions.intFrom(el, 'arg'); if (id !== null) unblockUser(id); },
    editUser: (el) => { const id = Actions.intFrom(el, 'arg'); if (id !== null) editUser(id); },
    resetPassword: (el) => { const id = Actions.intFrom(el, 'arg'); if (id !== null) resetPassword(id); },
    deleteUser: (el) => { const id = Actions.intFrom(el, 'arg'); if (id !== null) deleteUser(id); },
});
