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

The system **automatically creates a default admin user** when the application starts for the first time, **if no users exist** in the database.

**Location**: `backend/lifespan.py` (lines 347-373)

**When it happens**:
- ✅ On first application startup
- ✅ Only if the `users` table is empty
- ✅ During the application lifespan initialization phase

**Process**:
1. System checks if any users exist in the database
2. If no users found, creates default admin automatically
3. Logs success message with credentials
4. Shows warning to change password in production

---

## 🔑 Default Admin Credentials

When the system creates the default admin automatically, use these credentials:

```
Username: admin
Password: admin123
Email: admin@example.com
Full Name: System Administrator
Role: admin
Chatbot Access: Enabled
Status: Active
```

⚠️ **SECURITY WARNING**: These are **default credentials** and should be changed immediately after first login!

---

## 📝 First Login Steps

### Step 1: Start the Application

```bash
# Using Docker Compose
docker-compose -f docker/docker-compose.cpu.yml up -d

# Or using your deployment method
```

### Step 2: Wait for Startup

Wait for the application to fully start. Check the logs for:

```
✅ Created default admin user (username: admin, password: admin123)
⚠️  PLEASE CHANGE THE DEFAULT ADMIN PASSWORD IN PRODUCTION!
```

### Step 3: Sign In

1. Navigate to: `http://localhost/signin` (or your server URL)
2. Enter credentials:
   - **Username**: `admin`
   - **Password**: `admin123`
3. Click "Sign In Securely"

### Step 4: Change Password (IMPORTANT!)

After logging in:

1. Go to **Admin Panel** → **Manage Users** (`/admin/users`)
2. Find the `admin` user
3. Click **Edit** or **Reset Password**
4. Set a **strong, unique password**
5. Save changes

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

---

## 🔐 Changing Admin Password

### Method 1: Through Admin Panel

1. Sign in as admin
2. Go to **Admin Panel** → **Manage Users** (`/admin/users`)
3. Find the admin user
4. Click **"Reset Password"** or **"Edit"**
5. Enter new password
6. Confirm and save

### Method 2: Using API

**Endpoint**: `POST /api/users/{user_id}/reset-password`

**Request Body**:
```json
{
  "new_password": "NewSecurePassword123!"
}
```

---

## 🗄️ Manual Admin Creation (Database)

If you need to create an admin user directly in the database (e.g., if you lost access):

### Step 1: Connect to Database

```bash
# Using Docker
docker-compose exec postgres psql -U postgres -d face_recognition

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
SELECT id, username, email, role, is_active FROM users WHERE role = 'admin';
```

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

- ✅ **Change default password immediately** after first login
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

1. **Start application** → Default admin is created automatically
2. **Sign in** with:
   - Username: `admin`
   - Password: `admin123`
3. **Change password** immediately!
4. **Create additional admins** as needed
5. **Assign pipelines** to users
6. **Monitor system** through admin panel

### Default Admin Credentials:
```
Username: admin
Password: admin123
```

⚠️ **Remember**: Change the default password immediately after first login!

---

**Last Updated**: January 2026  
**Maintained by**: ITDIR-AI DEPARTMENT

