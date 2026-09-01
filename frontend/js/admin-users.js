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
        logoutBtn.addEventListener('click', (e) => {
            // Defer to the ONE real logout (navbar-loader.js): it calls
            // POST /api/auth/logout, which revokes the token server-side, and
            // clears the cached navigation. This handler used to remove a
            // localStorage 'access_token' that is never written — the credential
            // is an HttpOnly cookie — so it redirected while leaving the session
            // valid.
            if (typeof window.handleLogout === 'function') {
                e.preventDefault();
                window.handleLogout();
            }
            // If navbar-loader has not loaded, fall through to the button's
            // default behaviour rather than faking a logout that did not happen.
        });
    }
    // No else: the navbar is injected asynchronously by navbar-loader.js and
    // binds its own logout handler (admin-navbar.js) when it lands. Every other
    // admin page guards with `if (logoutBtn)` and says nothing; only this one
    // logged console.error, so a late navbar — a race, not a fault — surfaced
    // as an error on a page that was working correctly.
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
        updateSystemPrincipalNotice();
    } catch (error) {
        document.getElementById('users-table-body').innerHTML =
            `<tr><td colspan="8" class="loading">Error loading users: ${error.message}</td></tr>`;
    }
}

// ---------------------------------------------------------------------------
// The `system` audit principal
//
// Not a human account. It exists so machine-initiated actions (vector index
// rebuild, reconciliation, corruption recovery) have a valid actor for
// identity_audit_log.user_id, which is NOT NULL. It cannot sign in: is_active
// is false, its password hash is the literal "!" which no password can match,
// and its role grants nothing.
//
// It is rendered read-only here. The backend refuses to edit, re-password or
// delete it regardless — this only stops the controls being offered.
// ---------------------------------------------------------------------------
const SYSTEM_USERNAME = 'system';

function isSystemAccount(user) {
    const username = String(user.username || '').trim().toLowerCase();
    const role = String(user.role || '').trim().toLowerCase();
    // Either identifies it: the two can be edited apart, and a half-tampered
    // row is still the principal.
    return username === SYSTEM_USERNAME || role === SYSTEM_USERNAME;
}

// Single row renderer, used by BOTH the full table and the blocked-only filter.
// They previously carried separate copies of this markup, so any change had to
// be made twice — and a protected row applied to only one of them would still
// offer Delete on `system` in the other view.
function renderUserRow(user) {
    const isBlocked = user.blocked_reason || user.blocked_at;
    const blockedBadge = isBlocked
        ? `<span class="badge badge-danger" title="${user.blocked_reason || 'Blocked'}">BLOCKED</span>`
        : '';

    if (isSystemAccount(user)) {
        return `
        <tr class="user-system">
            <td>${user.username} <i class="fas fa-lock" aria-hidden="true" title="Protected account"></i></td>
            <td>${user.email}</td>
            <td>${user.full_name || '-'}</td>
            <td><span class="badge badge-system">System</span></td>
            <td><span class="badge badge-no">No</span></td>
            <td>${user.pipeline_ids.length} pipeline(s)</td>
            <td><span class="badge badge-nonlogin">Non-login</span></td>
            <td>
                <span class="protected-note" title="Audit principal for automated actions. It cannot sign in and is not editable.">Protected 🔒</span>
            </td>
        </tr>
    `;
    }

    const statusBadge = user.is_active
        ? `<span class="badge badge-active">Active</span>`
        : `<span class="badge badge-inactive">Inactive</span>`;

    // The account still carries the password an administrator assigned, so it
    // has not been claimed yet: the user cannot do anything until they change
    // it at sign-in. Worth seeing at a glance when an account "does not work".
    const rotationBadge = user.must_change_password
        ? `<span class="badge badge-warning" title="This user must change their password at next sign-in before the account can be used.">MUST CHANGE PASSWORD</span>`
        : '';

    return `
        <tr ${isBlocked ? 'class="user-blocked"' : ''}>
            <td>${user.username} ${blockedBadge} ${rotationBadge}</td>
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
}

// Shown only when the principal is absent, which means machine-generated audit
// rows are currently being dropped.
function updateSystemPrincipalNotice() {
    const notice = document.getElementById('system-principal-notice');
    if (!notice) return;
    const present = currentUsers.some(isSystemAccount);
    notice.style.display = present ? 'none' : 'flex';
    if (!present) {
        console.warn('[SYSTEM_PRINCIPAL] absent — machine-generated audit rows are not being written');
    }
}

async function restoreSystemPrincipal() {
    if (!confirm(
        'Restore the "system" audit principal?\n\n' +
        'This recreates the non-login account that automated actions are ' +
        'recorded against. It cannot sign in and no existing user is changed.'
    )) return;

    try {
        const response = await fetch('/api/users/system/restore', {
            method: 'POST',
            credentials: 'include',
            headers: {
                // Required by require_user_admin_csrf.
                'X-Requested-With': 'XMLHttpRequest'
            }
        });

        if (!response.ok) {
            const error = await response.json().catch(() => ({ detail: 'Failed to restore system principal' }));
            throw new Error(error.detail || `HTTP ${response.status}`);
        }

        const result = await response.json();
        console.log('[SYSTEM_PRINCIPAL] restore result:', result);
        await loadUsers();
        alert(result.message || 'System audit principal restored.');
    } catch (error) {
        console.error('[SYSTEM_PRINCIPAL] restore failed:', error);
        alert('Error restoring system principal: ' + error.message);
    }
}

function renderUsersTable() {
    const tbody = document.getElementById('users-table-body');

    if (currentUsers.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="loading">No users found</td></tr>';
        return;
    }

    tbody.innerHTML = currentUsers.map(renderUserRow).join('');
}

function editUser(userId) {
    console.log(`[EDIT_USER] Opening edit modal for user ID: ${userId}`);
    
    const user = currentUsers.find(u => u.id === userId);
    if (!user) {
        console.error(`[EDIT_USER] User with ID ${userId} not found`);
        alert('User not found');
        return;
    }

    // The protected row offers no Edit button, but the action is delegated by
    // data-action and reachable from the console, so refuse here as well. The
    // modal must never open for the principal: its role matches no <option>,
    // which leaves the select blank and invites an accidental change.
    if (isSystemAccount(user)) {
        console.warn('[EDIT_USER] refused: the system audit principal is not editable');
        alert('The "system" account is a protected audit principal and cannot be edited.');
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
            // Roles are stored canonically lowercase; normalise here too so an
            // older row written as "Observer" still selects the right option
            // instead of leaving the select blank.
            const role = String(user.role || '').toLowerCase();
            roleField.value = role;

            // If the value still did not match an option, the select is now
            // blank and saving would submit "" — which the backend skips,
            // making the form appear to do nothing. Surface it instead.
            if (!roleField.value) {
                console.warn('[USER_FORM] Unrecognised role from server:', user.role);
            }

            // An administrator's role cannot be changed here. Disabling the
            // whole select (rather than omitting the option) keeps the true
            // role visible and makes the restriction obvious.
            const isAdmin = role === 'admin';
            roleField.disabled = isAdmin;
            roleField.title = isAdmin
                ? 'Administrator role cannot be changed from this form.'
                : '';
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
            // Shared lifecycle (Escape, backdrop, focus, scroll lock). This
            // page had none of it.
            window.ModalStack.open(modal, { backdropClose: true, onClose: () => closeModal() });
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
            window.ModalStack.open(modal, { backdropClose: true, onClose: () => closeModal() });
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
    window.ModalStack.open(document.getElementById('password-modal'), {
        backdropClose: true, onClose: () => closePasswordModal()
    });
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
    window.ModalStack.open(modal, { backdropClose: true, onClose: () => closeDeleteModal() });
    
    // Add event listener to confirm button if not already added
    const confirmBtn = document.getElementById('confirm-delete-btn');
    confirmBtn.onclick = () => confirmDeleteUser();
    
    console.log(`[DELETE_USER] Delete modal shown for user: ${userName} (ID: ${userId})`);
}

function closeDeleteModal() {
    const modal = document.getElementById('delete-user-modal');
    if (!modal) return;
    if (window.ModalStack.isOpen(modal)) {
        window.ModalStack.close(modal);   // re-enters here via onClose
        return;
    }
    modal.style.display = 'none';
    // Business cleanup, preserved: a stale id here would delete the wrong
    // user on the next confirm.
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
            credentials: 'include',
            headers: {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest'
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
    // Double-submit guard: this form creates and updates users, and a second
    // click while the first request is in flight sends the whole payload
    // again (a duplicate POST creates a second account).
    const submitButton = e.target.querySelector('button[type="submit"], input[type="submit"]');
    if (submitButton) {
        if (submitButton.disabled) return;
        submitButton.disabled = true;
    }
    
    const userId = document.getElementById('user-id').value;
    const passwordValue = document.getElementById('modal-password').value.trim();
    
    const roleSelect = document.getElementById('modal-role');

    const formData = {
        username: document.getElementById('modal-username').value,
        email: document.getElementById('modal-email').value,
        full_name: document.getElementById('modal-full-name').value,
        // Unchecked boxes are sent as explicit false, not omitted, so the
        // backend removes the permission rather than leaving the old value.
        can_use_chatbot: document.getElementById('modal-chatbot-access').checked,
        is_active: document.getElementById('modal-is-active').checked,
        pipeline_ids: Array.from(document.querySelectorAll('#pipeline-checkboxes input:checked'))
            .map(cb => cb.value)
    };

    // Role is omitted when the select is disabled (the target is an
    // administrator). Sending it would be rejected by the backend's
    // "cannot assign admin" rule and would block edits to this user's other
    // fields; omitting it leaves the role untouched, which is the intent.
    if (roleSelect && !roleSelect.disabled && roleSelect.value) {
        formData.role = roleSelect.value;
    }

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
            credentials: 'include',
            headers: {
                'Content-Type': 'application/json',
                // Required by require_user_admin_csrf: proves the request was
                // issued by our own page rather than a cross-site form.
                'X-Requested-With': 'XMLHttpRequest'
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
    } finally {
        // Re-enable however it ended. Without this the button stays dead after
        // a failed save and the admin has to reopen the dialog.
        if (submitButton) submitButton.disabled = false;
    }
}

async function handlePasswordReset(e) {
    e.preventDefault();
    const submitButton = e.target.querySelector('button[type="submit"], input[type="submit"]');
    if (submitButton) {
        if (submitButton.disabled) return;
        submitButton.disabled = true;
    }
    
    const userId = document.getElementById('password-user-id').value;
    const newPassword = document.getElementById('new-password').value;

    try {
        const response = await fetch(`/api/users/${userId}/reset-password`, {
            method: 'POST',
            credentials: 'include',
            headers: {
                'Content-Type': 'application/json',
                // Required by require_user_admin_csrf: proves the request was
                // issued by our own page rather than a cross-site form.
                'X-Requested-With': 'XMLHttpRequest'
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
    } finally {
        if (submitButton) submitButton.disabled = false;
    }
}

function closeModal() {
    const modal = document.getElementById('user-modal');
    if (!modal) return;
    if (window.ModalStack.isOpen(modal)) { window.ModalStack.close(modal); return; }
    modal.style.display = 'none';
}

function closePasswordModal() {
    const modal = document.getElementById('password-modal');
    if (!modal) return;
    if (window.ModalStack.isOpen(modal)) { window.ModalStack.close(modal); return; }
    modal.style.display = 'none';
}

async function unblockUser(userId) {
    if (!confirm('Are you sure you want to unblock this user?')) return;

    try {
        const response = await fetch(`/api/users/${userId}/unblock`, {
            method: 'POST',
            credentials: 'include', // Include HttpOnly cookies
            headers: {
                'X-Requested-With': 'XMLHttpRequest'
            }
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
    // Same renderer as the full table, so the protected treatment cannot be
    // present in one view and missing in the other.
    tbody.innerHTML = blockedUsers.map(renderUserRow).join('');
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
    restoreSystemPrincipal,
    closeModal,
    closeDeleteModal,
    closePasswordModal,
    unblockUser: (el) => { const id = Actions.intFrom(el, 'arg'); if (id !== null) unblockUser(id); },
    editUser: (el) => { const id = Actions.intFrom(el, 'arg'); if (id !== null) editUser(id); },
    resetPassword: (el) => { const id = Actions.intFrom(el, 'arg'); if (id !== null) resetPassword(id); },
    deleteUser: (el) => { const id = Actions.intFrom(el, 'arg'); if (id !== null) deleteUser(id); },
});
