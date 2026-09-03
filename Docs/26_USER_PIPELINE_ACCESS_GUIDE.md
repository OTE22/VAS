# User Pipeline Access and Identity Management Guide

## Overview

The system supports **pipeline-based access control** for regular users, allowing them to view and manage unknown identities from specific surveillance pipelines they have been granted access to. This enables secure delegation of identity management tasks while maintaining proper access boundaries.

---

## 🎯 Key Concepts

### Pipeline Access Control

- **Admin users**: Have full access to all pipelines and all identities
- **Regular users**: Can only access identities from pipelines they've been assigned to
- **Automatic filtering**: The system automatically filters data based on user's pipeline access

### What Users Can Do

Users with pipeline access can:
- ✅ **View unknown identities** from their assigned pipelines
- ✅ **Promote unknown identities** to known (for their pipelines)
- ✅ **Merge identities** (both must be from their accessible pipelines)
- ✅ **View merge suggestions** (only for their accessible pipelines) - **NEW: Full access to merge suggestions feature**
- ✅ **Approve merge suggestions** (for their accessible pipelines) - **NEW: Can approve suggestions**
- ✅ **Reject merge suggestions** (for their accessible pipelines) - **NEW: Can reject suggestions**
- ✅ **View identity details** (for their accessible pipelines)

---

## 🔐 Admin Setup: Granting Pipeline Access

### Step 1: Navigate to User Management

1. Log in as **admin**
2. Go to **Admin → Users** (`/admin/users`)
3. Find the user you want to grant access to
4. Click **Edit** on the user

### Step 2: Assign Pipelines

1. In the edit user modal, you'll see **Pipeline Access** section
2. Select the pipelines you want to grant access to
3. Click **Save**

**Example:**
- User: `security_guard_1`
- Assigned Pipelines: `camera_entrance`, `camera_parking_lot`
- Result: User can only see/manage identities from these two cameras

### Step 3: Verify Access

The user will now be able to:
- See detections from assigned pipelines in the dashboard
- View unknown identities from assigned pipelines
- Manage those identities (promote, merge, etc.)

> ⚠️ **Not until they have signed in once and replaced the password you gave
> them.** A newly created account — or one whose password you just reset —
> carries `must_change_password`, and until the user changes it at
> `/change-password` every one of the endpoints above answers
> `403 PASSWORD_ROTATION_REQUIRED`. Granting more pipelines will not change
> that. Admin → Users shows a **MUST CHANGE PASSWORD** badge while it applies.

---

## 👤 User Experience: Working with Pipeline Access

### Viewing Unknown Identities

**As a regular user:**

1. Navigate to **Admin → Unknown Faces** (`/admin/unknown`)
2. You'll **only see** unknown identities from your assigned pipelines
3. The interface looks the same, but data is automatically filtered

**What you'll see:**
- Unknown faces from your assigned pipelines only
- Same interface as admin, but filtered data
- All features work the same (view, promote, merge)

### Promoting Unknown Identities

**As a regular user:**

1. Find an unknown identity from your assigned pipeline
2. Click **PROMOTE**
3. Enter the person's name
4. Click **Promote**

**Security check:**
- ✅ You can only promote identities from your accessible pipelines
- ❌ If you try to promote an identity from a different pipeline, you'll get an "Access denied" error

### Merging Identities

**As a regular user:**

1. Find two identities you want to merge (both must be from your accessible pipelines)
2. Click **MERGE** on one of them
3. Select the other identity to merge with
4. Add notes (optional)
5. Click **Merge**

**Security check:**
- ✅ You can only merge identities from your accessible pipelines
- ✅ Both identities must be from your accessible pipelines
- ❌ If either identity is from a different pipeline, you'll get an "Access denied" error

### Merge Suggestions

**As a regular user:**

1. Navigate to **Admin → Merge Suggestions**
2. You'll **only see** merge suggestions for identities from your accessible pipelines
3. You can approve or reject suggestions (for your accessible pipelines only)

**Security check:**
- ✅ You can only see suggestions involving your accessible pipelines
- ✅ You can only approve/reject suggestions for your accessible pipelines
- ❌ Suggestions involving other pipelines won't appear

---

## 🔒 Security Features

### Automatic Access Control

The system automatically:
- ✅ Filters unknown identities by user's pipeline access
- ✅ Checks access before allowing promote/merge operations
- ✅ Filters merge suggestions by user's pipeline access
- ✅ Validates access on every API request
- ✅ Limits the SQL assistant (chatbot) to the user's cameras

### The assistant is scoped the same way

`can_use_chatbot` opens the assistant; pipeline access decides what it can
see. Before every turn the API binds the caller's grants onto the agent
(`prepare_turn` → `set_pipeline_scope`), and the SQL guard rewrites every
table the model reads (`pipelines`, `detections`, `faces`) into a subquery
limited to those cameras. Joins, CTEs and nested selects inherit it because
the rewrite is done on the AST, not on the prompt. The name resolver is
scoped with the same predicate the identities API uses, so a person the user
may not see is never offered as a match.

- An **admin** is unrestricted (`pipeline_scope = None`).
- A user with **no grants** gets nothing, not everything: the guard refuses
  with `NO_PIPELINE_ACCESS`.
- If the grants **cannot be read**, the scope is treated as empty for that
  turn. An authorization rule that widens on failure is the one worth
  designing against.

Revoking a pipeline takes effect on the user's next turn: the scope is read
fresh each time, independent of the `permissions_version` agent rebuild.

### Access Verification

Before any operation, the system:
1. Checks if user is admin (full access)
2. If not admin, retrieves user's accessible pipelines
3. Verifies the identity's pipeline_ids overlap with user's accessible pipelines
4. Allows or denies the operation accordingly

### Error Messages

If a user tries to access something they don't have permission for:
- **403 Forbidden**: "Access denied to this identity"
- **403 Forbidden**: "Access denied to one or both identities"
- **403 Forbidden**: "Access denied to one or more identities in this merge suggestion"

A fourth 403 is **not** an authorization failure and is not fixed by granting
pipelines — the account has not yet replaced its admin-assigned password:

```json
{"detail": {"code": "PASSWORD_ROTATION_REQUIRED",
            "message": "You must change your password before continuing.",
            "redirect_url": "/change-password"}}
```

---

## 📊 API Endpoints

All identity management endpoints now support pipeline-based access:

### List Unknown Identities
```
GET /api/admin/unknown
```
- **Admin**: Sees all unknown identities
- **User**: Only sees identities from accessible pipelines

### Get Identity Details
```
GET /api/admin/identity/{identity_id}
```
- **Admin**: Can view any identity
- **User**: Can only view identities from accessible pipelines

### Promote Unknown to Known
```
POST /api/admin/unknown/{identity_id}/promote
```
- **Admin**: Can promote any unknown identity
- **User**: Can only promote identities from accessible pipelines

### Merge Identities
```
POST /api/admin/identities/merge
```
- **Admin**: Can merge any identities
- **User**: Can only merge identities from accessible pipelines (both must be accessible)

### Get Merge Suggestions
```
GET /api/admin/merge-suggestions
```
- **Admin**: Sees all merge suggestions
- **User**: Only sees suggestions for accessible pipelines

### Approve/Reject Merge Suggestions
```
POST /api/admin/merge-suggestions/{id}/approve
POST /api/admin/merge-suggestions/{id}/reject
```
- **Admin**: Can approve/reject any suggestion
- **User**: Can only approve/reject suggestions for accessible pipelines

---

## 🎓 Best Practices

### For Administrators

✅ **DO:**
- Grant pipeline access based on user's responsibilities
- Review pipeline access regularly
- Use descriptive pipeline names
- Document which users have access to which pipelines

❌ **DON'T:**
- Grant access to all pipelines unless necessary
- Share admin credentials with regular users
- Forget to revoke access when users change roles

### For Regular Users

✅ **DO:**
- Only work with identities from your assigned pipelines
- Report any access issues to your administrator
- Use merge suggestions to help identify duplicates
- Add clear notes when promoting or merging identities

❌ **DON'T:**
- Try to access identities from pipelines you don't have access to
- Share your credentials with others
- Promote identities without verifying they're correct

---

## 🔍 Troubleshooting

### Problem: User Can't See Any Unknown Identities

**Possible Causes:**
1. User has no pipeline access assigned
2. No unknown identities exist in assigned pipelines
3. User is looking at wrong page
4. **The account has never replaced its admin-assigned password** — the most
   likely cause for an account created recently. It is bounced to
   `/change-password` and sees nothing else

**Solution:**
1. Check user's pipeline access in Admin → Users
2. Verify there are unknown identities in those pipelines
3. Ensure user is on `/admin/unknown` page
4. Look for the **MUST CHANGE PASSWORD** badge in Admin → Users, or check
   `must_change_password` on `GET /api/users`; if set, the user must sign in
   and change their password before anything else works

### Problem: "Access Denied" When Promoting

**Possible Causes:**
1. Identity is from a pipeline user doesn't have access to
2. User's pipeline access was revoked
3. Identity was moved to a different pipeline

**Solution:**
1. Check identity's pipeline_ids
2. Verify user's current pipeline access
3. Contact admin to grant access if needed

### Problem: Merge Suggestion Not Appearing

**Possible Causes:**
1. Suggestion involves pipelines user doesn't have access to
2. Suggestion was already processed
3. No suggestions exist for user's pipelines

**Solution:**
1. Verify suggestion's identity pipeline_ids
2. Check suggestion status (should be PENDING)
3. Admin can see all suggestions to verify

---

## 📝 Example Scenarios

### Scenario 1: Security Guard at Entrance

**Setup:**
- User: `guard_entrance`
- Assigned Pipeline: `camera_main_entrance`

**What User Can Do:**
- See all unknown faces detected at main entrance
- Promote regular visitors to known
- Merge duplicate entries from entrance camera
- Review merge suggestions for entrance identities

**What User Cannot Do:**
- See unknown faces from parking lot camera
- Access identities from other pipelines
- Merge identities from different pipelines

### Scenario 2: Multi-Camera Access

**Setup:**
- User: `security_supervisor`
- Assigned Pipelines: `camera_entrance`, `camera_parking`, `camera_lobby`

**What User Can Do:**
- See unknown faces from all three cameras
- Merge identities that appear across multiple cameras
- Manage all identities from assigned cameras

**What User Cannot Do:**
- Access identities from cameras not in their list
- See identities from restricted areas

---

## 🔗 Related Documentation

- **03_ADMIN_SETUP_GUIDE.md** - How to set up users and grant pipeline access
- **06_PROMOTE_AND_MERGE_GUIDE.md** - Detailed guide on promoting and merging
- **07_UNKNOWN_FACES_CENTER_COMPLETE_GUIDE.md** - Complete unknown faces guide
- **25_API_AUTHENTICATION_GUIDE.md** - API authentication and token usage

---

## 📅 Last Updated

**January 2025**  
**Version:** 1.0  
**Status:** Active

---

**Need Help?** Check the tutorial endpoint at `/admin/tutorial` for step-by-step guides!

