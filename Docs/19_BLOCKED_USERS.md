# Blocked Users - Database Storage

## Overview
When a user attempts to perform forbidden database operations (DELETE, UPDATE, INSERT, ALTER, etc.), the system automatically blocks them and stores the blocking information in the database.

## Database Schema

### User Table Fields for Blocking

The `users` table has the following fields related to blocking:

```sql
blocked_reason TEXT NULLABLE        -- Reason for blocking (e.g., "Attempted forbidden SQL operation")
blocked_at TIMESTAMP NULLABLE       -- When the user was blocked (UTC timestamp)
is_active BOOLEAN DEFAULT TRUE      -- Set to FALSE when blocked
can_use_chatbot BOOLEAN DEFAULT FALSE -- Set to FALSE when blocked
```

## How Blocking Works

### 1. Detection
When a user attempts a forbidden operation, the security system detects it at multiple layers:
- **STEP 0**: Natural language malicious intent detection
- **Layer 0.5**: SQL validation after generation
- **Layer 1**: Pre-execution SQL validation
- **Layer 2**: Response content analysis

### 2. Blocking Process
When malicious intent is detected:

```python
# In backend/services/user_service.py
async def block_user(user_id: int, reason: str, db: AsyncSession) -> User:
    user.is_active = False              # Disables login
    user.can_use_chatbot = False        # Disables chatbot access
    user.blocked_reason = reason         # Stores the reason
    user.blocked_at = datetime.utcnow()  # Records timestamp
```

### 3. Database Storage Example

```sql
-- Example blocked user record
SELECT 
    id,
    username,
    email,
    is_active,           -- FALSE
    can_use_chatbot,     -- FALSE
    blocked_reason,      -- "Attempted database alteration operation detected in natural language query: DELETE operation. Query: delete all database"
    blocked_at,          -- '2026-01-01 06:53:00.607000'
    created_at,
    updated_at
FROM users
WHERE blocked_reason IS NOT NULL;
```

## Example Blocked User Record

```json
{
    "id": 5,
    "username": "malicious_user",
    "email": "user@example.com",
    "is_active": false,
    "can_use_chatbot": false,
    "blocked_reason": "Attempted database alteration operation detected in natural language query: DELETE operation. Query: delete all database",
    "blocked_at": "2026-01-01T06:53:00.607000",
    "created_at": "2025-12-15T10:00:00.000000",
    "updated_at": "2026-01-01T06:53:00.607000"
}
```

## Unblocking Users

### Via Admin Panel
1. Go to `/admin/users`
2. Find the blocked user (shows red "BLOCKED" badge)
3. Click "Unblock" button
4. User is unblocked via `/api/users/{user_id}/unblock` endpoint

### Via API
```python
# In backend/services/user_service.py
async def unblock_user(user_id: int, db: AsyncSession) -> User:
    user.is_active = True        # Restores login access
    user.blocked_reason = None   # Clears reason
    user.blocked_at = None       # Clears timestamp
    # Note: can_use_chatbot is NOT automatically restored
    # Admin must explicitly grant chatbot access if needed
```

## Query Examples

### Find All Blocked Users
```sql
SELECT 
    id,
    username,
    email,
    blocked_reason,
    blocked_at,
    created_at
FROM users
WHERE blocked_reason IS NOT NULL
ORDER BY blocked_at DESC;
```

### Find Users Blocked Today
```sql
SELECT 
    id,
    username,
    blocked_reason,
    blocked_at
FROM users
WHERE blocked_at >= CURRENT_DATE
ORDER BY blocked_at DESC;
```

### Count Blocked Users by Reason
```sql
SELECT 
    blocked_reason,
    COUNT(*) as count,
    MAX(blocked_at) as latest_block
FROM users
WHERE blocked_reason IS NOT NULL
GROUP BY blocked_reason
ORDER BY count DESC;
```

## Security Features

1. **Automatic Detection**: Multiple security layers detect malicious intent
2. **Immediate Blocking**: User is blocked as soon as malicious intent is detected
3. **Audit Trail**: All blocking actions are logged with reason and timestamp
4. **Admin Notification**: Admins are notified when users are blocked
5. **Persistent Storage**: Blocking information persists across system restarts

## Related Files

- **Database Model**: `db_models.py` - User model with `blocked_reason` and `blocked_at` fields
- **Blocking Service**: `backend/services/user_service.py` - `block_user()` and `unblock_user()` methods
- **Security Detection**: `sql_agent/tools/agent_tools.py` - `detect_malicious_intent()` tool
- **API Routes**: `backend/routes/users.py` - `/api/users/{user_id}/unblock` endpoint
- **Frontend**: `frontend/js/admin-users.js` - User management interface with blocking display

