# 👑 Admin User Setup Guide

**Face Recognition Surveillance System**  
**ITDIR-AI DEPARTMENT**

---

## 📋 Table of Contents

1. [Automatic Admin Creation](#automatic-admin-creation)
2. [Default Admin Credentials](#default-admin-credentials)
3. [First Login Steps](#first-login-steps)
4. [Creating Additional Admins](#creating-additional-admins)
5. [Changing Admin Password](#changing-admin-password)
6. [Manual Admin Creation (Database)](#manual-admin-creation-database)

---

## 🚀 Automatic Admin Creation

### How It Works

The system **bootstraps one administrator** when the application starts and
**no active administrator exists**. Note "no active administrator", not "no
users": a database full of ordinary users still gets one.

**Location**: `backend/services/bootstrap_admin.py`, called from
`backend/lifespan.py`.

**When it happens**:
- ✅ On startup, when no active admin account exists
- ✅ Guarded by an advisory lock, so concurrent workers create exactly one
- ❌ **Refused** in production if human users exist but no active admin — that
  combination usually means someone deactivated the last admin, and silently
  minting a new one would be a privilege-escalation path

**Process**:
1. Resolve the credential from `BOOTSTRAP_ADMIN_PASSWORD_FILE` (preferred) or
   `BOOTSTRAP_ADMIN_PASSWORD`
2. Assess its strength; a weak or known-default value aborts a production start
3. Create the account with `must_change_password` set, unless
   `BOOTSTRAP_ADMIN_REQUIRE_ROTATION` is false
4. Log the event **without the password** — it is never written to a log

---

## 🔑 Bootstrap Admin Credentials

**Production** reads the password from a file and never prints it:

```bash
cat secrets/bootstrap_admin_password
```

**The development CPU stack** (`docker/docker-compose.cpu.yml`) seeds a fixed
convenience credential instead, and deliberately turns rotation off there:

```
Username: admin
Password: admin123          # dev stack ONLY — BOOTSTRAP_ADMIN_REQUIRE_ROTATION=false
```

⚠️ `admin123` is **rejected by the production config guard** (12+ characters,
6+ distinct, not a known default). It exists so the dev stack is usable, not as
a default to change later.

---

## 📝 First Login Steps

### Step 1: Start the Application

```bash
# Using Docker Compose
docker-compose -f docker/docker-compose.cpu.yml up -d

# Or using your deployment method
```

### Step 2: Wait for Startup

Wait for the application to fully start. Check the logs for the bootstrap
event — the password is **never** among them:

```bash
docker compose -f docker/docker-compose.cpu.yml logs face_recognition | grep -i bootstrap
```

```
[AUDIT] bootstrap administrator created: username=admin credential_source=file rotation_required=True
The bootstrap administrator must change its password on first login.
```

### Step 3: Sign In

1. Navigate to: `http://localhost/signin` (or your server URL)
2. Enter the username and the password from the secret file
3. Click "Sign In Securely"

### Step 4: Change the Password — the system will not let you skip it

**This is enforced, not advisory.** The sign-in succeeds and you get a real
session, but the login response carries `rotation_required: true` and
`redirect_url: "/change-password"`, and **every other endpoint answers
`403 PASSWORD_ROTATION_REQUIRED`** until the password is changed. Only four
things work in that state: reading your own identity (`GET /api/auth/me`),
logging out, the change-password page, and the change itself.

So the old procedure — *Admin Panel → Manage Users → Reset Password* — **cannot
be followed**: `/admin/users` is itself gated, and requesting it redirects you
straight back to `/change-password`.

Instead, on the page you land on:

1. Enter your **current** password (the one you just signed in with)
2. Enter a **new** password: at least 12 characters, at least 6 distinct
   characters, not a known default, and not the one you are replacing
3. Confirm it and submit

On success the flag clears, every **other** session for that account is ended
immediately, your own session is replaced with a fresh one, and you are sent to
your role's landing page.

Programmatically that is `POST /api/auth/change-password` with
`{"current_password": "...", "new_password": "..."}` — see
`25_API_AUTHENTICATION_GUIDE.md`.

---

## 👥 Creating Additional Admins

### Method 1: Through Admin Panel (Recommended)

1. **Sign in** as the default admin
2. Navigate to: **Admin Panel** → **Manage Users** (`/admin/users`)
3. Click **"Create User"** button
4. Fill in the form:
   - **Username**: Choose a unique username
   - **Email**: Valid email address
   - **Password**: Strong password
   - **Full Name**: (Optional)
   - **Role**: Select **"Admin"** from dropdown
   - **Can Use Chatbot**: Check if needed
   - **Active Account**: Check to enable
5. Click **"Create User"**

### Method 2: Using API (Programmatic)

**Endpoint**: `POST /api/users`

**Authentication**: Requires admin token

**Request Body**:
```json
{
  "username": "newadmin",
  "email": "newadmin@example.com",
  "password": "SecurePassword123!",
  "full_name": "New Administrator",
  "role": "admin",
  "can_use_chatbot": true,
  "pipeline_ids": []  // Optional: assign specific pipelines
}
```

**Example using cURL**:
```bash
curl -X POST "http://localhost/api/users" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -d '{
    "username": "newadmin",
    "email": "newadmin@example.com",
    "password": "SecurePassword123!",
    "full_name": "New Administrator",
    "role": "admin",
    "can_use_chatbot": true
  }'
```

### ⚠️ Every user you create must change the password you typed

The password an administrator types is a **hand-over credential**: the admin
knows it, so it is not yet the user's own. Accounts created either way come out
with `must_change_password: true` (visible in the `POST /api/users` response and
in `GET /api/users`), and the new user **cannot use a single endpoint** until
they sign in and replace it. Their login succeeds; everything else answers
`403 PASSWORD_ROTATION_REQUIRED`.

In **Admin Panel → Manage Users** such an account carries a
**MUST CHANGE PASSWORD** badge next to its username until the user has done so.
If someone reports "my new account does not work", that badge is the first
thing to look at.

---

## 🔐 Changing a Password

Which method to use depends on **whose** password it is. The distinction is not
cosmetic: resetting someone else's password forces them to rotate it, and
resetting your own does not.

### Your own password: `/change-password`

The self-service page, and the **only** method available while a rotation is
pending.

**Endpoint**: `POST /api/auth/change-password`

```json
{
  "current_password": "the one you have now",
  "new_password": "at least 12 chars, 6+ distinct, not a known default"
}
```

The current password is required even though you already hold a session — a
cookie alone must not be enough to take permanent ownership of an account.
Success clears the flag, stamps `password_changed_at`, **ends every other
session for that account**, and issues you a fresh token.

### Someone else's password: admin reset

1. Sign in as admin
2. Go to **Admin Panel** → **Manage Users** (`/admin/users`)
3. Find the user, click **"Reset Password"** or **"Edit"**
4. Enter the new password and save

**Endpoint**: `POST /api/users/{user_id}/reset-password`

```json
{
  "new_password": "NewSecurePassword123!"
}
```

The target user is **forced to change it at their next sign-in**, and their
existing sessions end immediately — a reset is usually a response to a
compromise, so leaving those alive would defeat the point.

**Exception**: if `{user_id}` is your own, rotation is *not* forced. Otherwise
an admin resetting their own password would be sent straight to
`/change-password` to change it again, with no way out of the loop.

---

## 🗄️ Manual Admin Creation (Database)

If you need to create an admin user directly in the database (e.g., if you lost access):

### Step 1: Connect to Database

```bash
# Using Docker
docker compose -f docker/docker-compose.cpu.yml exec postgres psql -U postgres -d face_recognition

# Or directly
psql -U postgres -d face_recognition
```

### Step 2: Hash the Password

You need to hash the password first. Use Python:

```python
from backend.auth.password import hash_password

# Hash your password
password_hash = hash_password("YourSecurePassword123!")
print(password_hash)
```

### Step 3: Insert Admin User

```sql
INSERT INTO users (
    username,
    email,
    password_hash,
    full_name,
    role,
    can_use_chatbot,
    is_active,
    created_at,
    updated_at
) VALUES (
    'admin',
    'admin@example.com',
    '$2b$12$YOUR_HASHED_PASSWORD_HERE',  -- Replace with actual hash
    'System Administrator',
    'admin',
    true,
    true,
    NOW(),
    NOW()
);
```

### Step 4: Verify

```sql
SELECT id, username, email, role, is_active, must_change_password
FROM users WHERE role = 'admin';
```

> **Note**: this INSERT does not name `must_change_password`, so it defaults to
> `false` and the hand-made account is **not** forced to rotate. That is the
> right behaviour for a recovery account you created yourself with a password
> only you know. If you want the rotation gate applied to it anyway, add
> `must_change_password` to the column list and pass `true`.

---

## 🔍 Verifying Admin Setup

### Check if Admin Exists

**Using Database**:
```sql
SELECT * FROM users WHERE role = 'admin';
```

**Using API** (requires admin token):
```bash
curl -X GET "http://localhost/api/users" \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN"
```

**Using Admin Panel**:
1. Sign in as admin
2. Go to **Admin Panel** → **Manage Users**
3. Look for users with **Role: Admin**

---

## 🛡️ Security Best Practices

### ✅ DO:

- ✅ **Change the seeded password** — the system now enforces this; the account
  can sign in but cannot do anything else until you do
- ✅ **Use strong passwords** (min 12 characters, mixed case, numbers, symbols)
- ✅ **Create separate admin accounts** for different administrators
- ✅ **Regularly review admin users** and remove unused accounts
- ✅ **Use unique usernames** (not generic like "admin")
- ✅ **Enable chatbot access** only if needed
- ✅ **Monitor admin activity** through audit logs

### ❌ DON'T:

- ❌ **Don't share admin credentials** between team members
- ❌ **Don't use default password** in production
- ❌ **Don't create too many admins** (principle of least privilege)
- ❌ **Don't use weak passwords** (e.g., "password123")
- ❌ **Don't leave inactive admin accounts** enabled

---

## 📊 Admin Capabilities

Once logged in as admin, you have access to:

### Dashboard Features
- ✅ View all system statistics
- ✅ See all pipelines and detections
- ✅ Add persons to tracking database
- ✅ Access home page with full system overview

### User Management (`/admin/users`)
- ✅ Create new users (admin or regular)
- ✅ Edit user information
- ✅ Assign pipelines to users (grants access to view/manage identities from those pipelines)
- ✅ Grant/revoke chatbot access
- ✅ Activate/deactivate accounts
- ✅ Reset passwords
- ✅ Block/unblock users

**Pipeline Access Note:** When you assign pipelines to a user, they can:
- View unknown identities from those pipelines
- Promote unknown identities to known (for their pipelines)
- Merge identities (both must be from their accessible pipelines)
- View and act on merge suggestions (for their accessible pipelines)

See **26_USER_PIPELINE_ACCESS_GUIDE.md** for complete details.

### Pipeline Management (`/admin/pipelines`)
- ✅ View all active pipelines
- ✅ Monitor pipeline status
- ✅ View pipeline statistics

### Audit & Monitoring (`/admin/audit`)
- ✅ View chatbot audit logs
- ✅ Monitor user activity
- ✅ Review query history
- ✅ Analyze system usage

### System Settings
- ✅ Access cache management
- ✅ View system metrics
- ✅ Monitor performance

---

## 🚨 Troubleshooting

### Problem: Login works, but every page bounces me to `/change-password`

**This is the rotation gate, not a fault.** The account still holds a seeded or
admin-assigned password, so every gated endpoint answers
`403 PASSWORD_ROTATION_REQUIRED` and the browser is redirected. Change the
password on that page and it stops.

Confirm it from the logs (`[AUTH] Blocked: password rotation pending for
user=…`) or from the database:

```sql
SELECT username, must_change_password, password_changed_at FROM users;
```

### Problem: a script gets 401 "Password was changed. Please log in again."

The token was issued **before** that account's password changed, so it is
refused. This is how "ending every other session" looks to an API client. Log
in again to get a current token.

### Problem: Can't Login with Default Credentials

**Possible Causes**:
1. Admin user wasn't created (check logs)
2. Database connection issue
3. User table doesn't exist

**Solution**:
1. Check application logs for admin creation message
2. Verify database connection
3. Run migrations if needed
4. Manually create admin (see Manual Creation section)

### Problem: "No Users Found" but Can't Create Admin

**Solution**:
1. Check database connection
2. Verify `users` table exists
3. Check application logs for errors
4. Manually insert admin user (see Manual Creation section)

### Problem: Forgot Admin Password

**Solution**:
1. If you have another admin account, reset password through admin panel
2. If no other admins, manually update password in database (see Manual Creation section)
3. Or contact system administrator

---

## 📝 Summary

### Quick Start:

1. **Start application** → one administrator is bootstrapped if none is active
2. **Sign in** with the username and the password from
   `secrets/bootstrap_admin_password` (dev CPU stack: `admin` / `admin123`)
3. **Change the password** — you are redirected to `/change-password` and
   nothing else works until you do
4. **Create additional admins** as needed — each must also change the password
   you gave them at their first sign-in
5. **Assign pipelines** to users
6. **Monitor system** through admin panel

### Bootstrap Admin Credentials:
```bash
cat secrets/bootstrap_admin_password     # production: never logged, never printed
```
```
Username: admin
Password: admin123                        # dev CPU stack only; rotation disabled there
```

⚠️ **Remember**: in production the password change is **compulsory**, not a
reminder. See `61_DEPLOYMENT_RUNBOOK.md` §7.

---

**Last Updated**: January 2026  
**Maintained by**: ITDIR-AI DEPARTMENT

