# How to Grant Users Access to Unknown Faces

## Overview

Users can access the "Unknown Faces" page **ONLY** if they have been assigned **at least one pipeline** by an administrator. This access is granted through the **User Management** page.

---

## Step-by-Step: Granting Access

### Step 1: Log in as Admin

1. Log in to the system as an **admin** user
2. Navigate to **Admin → Users** (`/admin/users`)

### Step 2: Find the User

1. In the users table, find the user you want to grant access to
2. Click the **"Edit"** button for that user

### Step 3: Assign Pipelines

1. In the edit user modal, scroll down to **"VIDEO PIPELINE ACCESS"** section
2. You'll see checkboxes for all available pipelines
3. **Check the pipelines** you want to grant access to
   - Example: Check "camera_entrance", "camera_parking"
4. Click **"Save"** or **"Update User"**

### Step 4: Verify Access

After assigning pipelines:
- The user will now see the **"UNKNOWN FACES"** button in their navbar
- They can click it to access the Unknown Faces page
- They will only see unknown identities from their assigned pipelines

> ⚠️ **If you also typed a new password in that same edit form**, the user is
> now required to change it: they will be redirected to `/change-password`
> instead of `/admin/unknown` until they do. Assigning pipelines alone does not
> have this effect — only setting a password does.

---

## How It Works (Technical)

### Database Structure

Access is stored in the `user_pipeline_access` table:

```sql
user_pipeline_access
├── user_id (references users.id)
├── pipeline_id (references pipelines.pipeline_id)
└── granted_at (timestamp)
```

### Access Check Flow

1. **User clicks "UNKNOWN FACES" button**
   ↓
2. **Backend route `/admin/unknown` is called**
   ↓
3. **Backend validates token and gets user**
   ↓
3b. **Backend checks `must_change_password`** — if set, 403
    `PASSWORD_ROTATION_REQUIRED` → redirect to `/change-password`. This happens
    *before* any pipeline check, and it is special-cased in the error handler
    because `/dashboard` is itself gated and would otherwise loop
   ↓
4. **Backend checks: `AuthService.get_user_pipelines(user_id, db)`**
   ↓
5. **Backend queries `user_pipeline_access` table**
   ↓
6. **If pipelines found → Allow access**
   **If no pipelines → Redirect to `/dashboard`**

### Key Backend Functions

**`backend/auth/auth_service.py`:**
- `get_user_pipelines(user_id, db)` - Queries `user_pipeline_access` table

**`backend/routes/dashboard.py`:**
- `/admin/unknown` route - Checks pipeline access before serving page

**`backend/routes/users.py`:**
- `PUT /api/users/{user_id}` - Updates user including `pipeline_ids`
- `UserService.update_user()` - Saves pipeline access to database

---

## Troubleshooting

### Problem: User Still Redirected to Dashboard After Assigning Pipelines

**Possible Causes:**
1. **Pipelines not saved properly**
   - Check: Did you click "Save" after checking pipelines?
   - Verify: Check the users table - does it show pipeline count > 0?

2. **User logged out and back in**
   - Solution: User may need to refresh page or log out/in

3. **Database not updated**
   - Check: Look at server logs for `[UNKNOWN]` messages
   - Verify: Check `user_pipeline_access` table directly

4. **No pipelines exist in system**
   - Solution: Ensure pipelines exist in `pipelines` table first

### Problem: User is redirected to `/change-password`, not `/dashboard`

Different cause entirely, and no amount of pipeline access will fix it. The
account still holds a password an administrator set, so it is gated until the
user replaces it. Check for the **MUST CHANGE PASSWORD** badge in Admin → Users,
or:

```sql
SELECT username, must_change_password FROM users WHERE username = '<user>';
```

### How to Verify Pipeline Access

**Option 1: Check Users Table**
- Go to Admin → Users
- Look at "Pipelines" column
- Should show "X pipeline(s)" where X > 0

**Option 2: Check API**
- Call `GET /api/users/me/pipelines` (as the user)
- Should return array of pipeline IDs

**Option 3: Check Database**
```sql
SELECT * FROM user_pipeline_access WHERE user_id = <user_id>;
```

**Option 4: Check Server Logs**
- Look for `[UNKNOWN]` log messages
- Should show: "User X has access to Y pipelines: [list]"

---

## Important Notes

1. **Pipeline Access = Unknown Faces Access**
   - If user has ANY pipeline access → Can see Unknown Faces
   - If user has NO pipeline access → Cannot see Unknown Faces

2. **Access is Backend-Enforced**
   - Frontend cannot override backend decisions
   - All access checks happen server-side

3. **Admin Always Has Access**
   - Admins don't need pipeline assignments
   - They automatically see all pipelines and all identities

4. **Access is Immediate**
   - Once pipelines are assigned and saved, access is immediate
   - No server restart needed

---

## Example: Granting Access

**Scenario:** You want user "security_guard_1" to manage unknown faces from "camera_entrance" and "camera_parking".

**Steps:**
1. Admin → Users
2. Find "security_guard_1"
3. Click "Edit"
4. Scroll to "VIDEO PIPELINE ACCESS"
5. Check: ☑ camera_entrance
6. Check: ☑ camera_parking
7. Click "Save"

**Result:**
- User can now see "UNKNOWN FACES" button
- User can access `/admin/unknown` page
- User sees only unknown identities from those 2 pipelines

---

## Related Documentation

- **26_USER_PIPELINE_ACCESS_GUIDE.md** - Complete guide on user pipeline access
- **03_ADMIN_SETUP_GUIDE.md** - How to set up admin users
- **25_API_AUTHENTICATION_GUIDE.md** - API authentication guide

---

## Quick Reference

**Where to grant access:** Admin → Users → Edit User → Video Pipeline Access

**What grants access:** Assigning at least one pipeline to the user

**How to verify:** Check "Pipelines" column in users table (should show count > 0)

**Backend check:** `AuthService.get_user_pipelines(user_id, db)` returns non-empty list

---

**Last Updated:** January 2025

