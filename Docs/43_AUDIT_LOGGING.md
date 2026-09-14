# Identity Management Audit Logging Guide

## Overview

**All identity management operations are now comprehensively logged in the database** for forensic analysis, accountability, and compliance purposes.

---

## Database Schema

### `identity_audit_log` Table

A comprehensive audit log table that tracks all identity management operations:

**⚠️ IMPORTANT: PRIMARY IDENTIFIER IS USERNAME, NOT IP ADDRESS**

```sql
CREATE TABLE identity_audit_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),  -- PRIMARY: User ID (required)
    username VARCHAR(100) NOT NULL,  -- PRIMARY: Username (required) - MAIN IDENTIFIER FOR ACCOUNTABILITY
    action_type VARCHAR(50) NOT NULL,  -- promote, merge, search, view, approve, reject, etc.
    identity_id UUID REFERENCES identities(id),  -- Target identity
    related_identity_id UUID REFERENCES identities(id),  -- For merges, related identity
    action_details JSON,  -- Flexible metadata
    before_state JSON,  -- State before action (for tracking changes)
    after_state JSON,  -- State after action (for tracking changes)
    ip_address VARCHAR(45),  -- SUPPLEMENTARY: IPv4 or IPv6 (optional, for context only - NOT for identification)
    user_agent VARCHAR(500),  -- SUPPLEMENTARY: Browser/client info (optional, for context only)
    success BOOLEAN NOT NULL DEFAULT TRUE,
    error_message TEXT,  -- Error if action failed
    notes TEXT,  -- Additional notes/justification
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);
```

**Key Points:**
- ✅ **PRIMARY IDENTIFIER: `username`** (required, indexed) - This is the source of truth for accountability
- ✅ **PRIMARY IDENTIFIER: `user_id`** (required, indexed) - Links to users table
- ⚠️ **SUPPLEMENTARY: `ip_address`** (optional) - For additional context only, NOT for identification
- ⚠️ **SUPPLEMENTARY: `user_agent`** (optional) - For additional context only

**Why Username is Primary:**
- Username is unique and tied to authentication
- IP addresses can change (dynamic IPs, VPNs, NAT, etc.)
- Multiple users can share the same IP address
- Username provides clear accountability
- IP address is unreliable for identification

**Indexes:**
- `idx_identity_audit_user_action` - (user_id, action_type, created_at)
- `idx_identity_audit_identity` - (identity_id, created_at)
- `idx_identity_audit_created` - (created_at)
- `idx_identity_audit_action` - (action_type, created_at)

---

## Logged Operations

### 1. **Promote Unknown to Known**
**Action Type:** `promote`

**Logged Information:**
- User who performed the action
- Identity ID being promoted
- Display name assigned
- Before state (type, status, display_name, appearances_count)
- After state (type, status, display_name, appearances_count)
- IP address and user agent
- Timestamp
- Notes (optional person code)

**Example:**
```json
{
  "action_type": "promote",
  "identity_id": "123e4567-e89b-12d3-a456-426614174000",
  "action_details": {
    "display_name": "John Doe",
    "promotion_type": "unknown_to_known"
  },
  "before_state": {
    "type": "unknown",
    "status": "active",
    "display_name": null,
    "appearances_count": 5
  },
  "after_state": {
    "type": "known",
    "status": "promoted",
    "display_name": "John Doe",
    "appearances_count": 5
  }
}
```

---

### 2. **Merge Identities**
**Action Type:** `merge`

**Logged Information:**
- User who performed the action
- From identity ID (source)
- To identity ID (target)
- Before state (both identities)
- After state (merged identity)
- IP address and user agent
- Timestamp
- Notes/justification

**Example:**
```json
{
  "action_type": "merge",
  "identity_id": "target-uuid",
  "related_identity_id": "source-uuid",
  "action_details": {
    "merge_type": "manual",
    "from_identity_id": "source-uuid",
    "to_identity_id": "target-uuid"
  },
  "before_state": {
    "from_identity": {...},
    "to_identity": {...}
  },
  "after_state": {
    "merged_identity": {...}
  }
}
```

---

### 3. **Search by Image**
**Action Type:** `search_by_image`

**Logged Information:**
- User who performed the search
- Search scope (known, unknown, both)
- Number of results
- First 5 search results (for reference)
- Processing time
- IP address and user agent
- Timestamp

**Example:**
```json
{
  "action_type": "search_by_image",
  "action_details": {
    "scope": "both",
    "results_count": 10,
    "search_results": [...],  // First 5 results
    "processing_time_ms": 234.5
  }
}
```

---

### 4. **View Identity Details**
**Action Type:** `view_details`

**Logged Information:**
- User who viewed the details
- Identity ID viewed
- IP address and user agent
- Timestamp

**Note:** This logs access to sensitive identity information for security auditing.

---

### 5. **Approve Merge Suggestion**
**Action Type:** `approve_merge_suggestion`

**Logged Information:**
- User who approved
- Suggestion ID
- Identity IDs involved
- IP address and user agent
- Timestamp
- Notes

---

### 6. **Reject Merge Suggestion**
**Action Type:** `reject_merge_suggestion`

**Logged Information:**
- User who rejected
- Suggestion ID
- Identity IDs involved
- IP address and user agent
- Timestamp
- Notes

---

### 7. **List Unknown Identities**
**Action Type:** `list_unknown`

**Logged Information:**
- User who accessed the list
- Filters applied
- Number of results
- IP address and user agent
- Timestamp

---

### 8. **Error Logging**
**Action Type:** (varies by operation)

**Logged Information:**
- User who attempted the action
- Action type that failed
- Error message
- Identity ID (if applicable)
- IP address and user agent
- Timestamp

**Note:** All failed operations are logged with `success=false` and the error message.

---

## Implementation Details

### Audit Logger Utility

**File:** `backend/utils/identity_audit.py`

**Main Class:** `IdentityAuditLogger`

**Methods:**
- `log_action()` - Generic logging method
- `log_promote()` - Log promotion actions
- `log_merge()` - Log merge actions
- `log_search_by_image()` - Log search actions
- `log_view_details()` - Log view actions
- `log_approve_merge_suggestion()` - Log approval actions
- `log_reject_merge_suggestion()` - Log rejection actions
- `log_list_unknown()` - Log list access
- `log_error()` - Log failed operations

**Helper Function:**
- `get_client_info(request)` - Extracts IP address and user agent from FastAPI request

---

## Integration Points

### All Identity Management Endpoints

Audit logging is integrated into:

1. **`POST /api/admin/unknown/{id}/promote`**
   - Logs before/after state
   - Captures client info
   - Logs errors on failure

2. **`POST /api/admin/identities/merge`**
   - Logs both identities before merge
   - Logs merged identity after merge
   - Captures client info
   - Logs errors on failure

3. **`POST /api/search/by-image`**
   - Logs search parameters
   - Logs results count
   - Logs processing time
   - Captures client info

4. **`GET /api/admin/identity/{id}`**
   - Logs access to sensitive identity details
   - Captures client info

5. **`POST /api/admin/merge-suggestions/{id}/approve`**
   - Logs approval with suggestion details
   - Captures client info

6. **`POST /api/admin/merge-suggestions/{id}/reject`**
   - Logs rejection with suggestion details
   - Captures client info

7. **`GET /api/admin/unknown`**
   - Logs list access with filters
   - Captures client info

---

## Querying Audit Logs

### Example Queries

**Get all actions by a specific user (PRIMARY IDENTIFIER - RECOMMENDED):**
```sql
-- Use username (PRIMARY IDENTIFIER)
SELECT * FROM identity_audit_log 
WHERE username = 'admin_user' 
ORDER BY created_at DESC;

-- Or use user_id (PRIMARY IDENTIFIER)
SELECT * FROM identity_audit_log 
WHERE user_id = 1 
ORDER BY created_at DESC;
```

**Get all promotions:**
```sql
SELECT * FROM identity_audit_log 
WHERE action_type = 'promote' 
ORDER BY created_at DESC;
```

**Get all actions on a specific identity:**
```sql
SELECT * FROM identity_audit_log 
WHERE identity_id = '123e4567-e89b-12d3-a456-426614174000' 
ORDER BY created_at DESC;
```

**Get failed operations:**
```sql
SELECT * FROM identity_audit_log 
WHERE success = false 
ORDER BY created_at DESC;
```

**Get actions from a specific IP (SUPPLEMENTARY - for context only, NOT for identification):**
```sql
-- ⚠️ WARNING: IP address is supplementary only. Use username for identification!
-- This query is for additional context/investigation, not for accountability
SELECT * FROM identity_audit_log 
WHERE ip_address = '192.168.1.100' 
ORDER BY created_at DESC;
```

**Get actions in a date range:**
```sql
SELECT * FROM identity_audit_log 
WHERE created_at >= '2025-01-01' 
  AND created_at < '2025-02-01' 
ORDER BY created_at DESC;
```

---

## Forensic Analysis

### What Can Be Traced

1. **Who did what (PRIMARY IDENTIFIERS):**
   - ✅ **Username** (REQUIRED) - Main identifier for accountability
   - ✅ **User ID** (REQUIRED) - Links to users table
   - ⚠️ IP address (SUPPLEMENTARY) - For context only, NOT for identification
   - ⚠️ User agent (SUPPLEMENTARY) - For context only

2. **When:**
   - Precise timestamp
   - Can correlate with other system events

3. **What changed:**
   - Before state (what it was)
   - After state (what it became)
   - Action details (specific parameters)

4. **Why:**
   - Notes field (justification/reason)
   - Action context (merge suggestion approval, etc.)

5. **Success/Failure:**
   - Whether action succeeded
   - Error messages if failed

**⚠️ IMPORTANT: Always use USERNAME for accountability, not IP address!**

---

## Compliance & Security

### Benefits

1. **Accountability (PRIMARY):**
   - ✅ Every action is attributed to a specific **USERNAME** (primary identifier)
   - ✅ User ID links to authenticated user account
   - ✅ Cannot be modified after creation (immutable log)
   - ⚠️ IP address is supplementary context only, NOT used for identification

2. **Forensic Analysis:**
   - Complete audit trail for investigations
   - Can reconstruct what happened and when
   - Username provides clear accountability (not IP address)

3. **Compliance:**
   - Meets requirements for data access logging
   - Supports GDPR, HIPAA, and other regulations
   - Username-based accountability (not IP-based)

4. **Security:**
   - Detects unauthorized access by username
   - Tracks suspicious patterns by user
   - IP address is supplementary context for investigations (not for identification)

5. **Data Integrity:**
   - Before/after states show exactly what changed
   - Can verify data modifications
   - All changes attributed to specific username

---

## Best Practices

1. **Never Delete Audit Logs:**
   - Audit logs should be retained indefinitely or per policy
   - Consider archival for old logs

2. **Regular Review:**
   - Review audit logs periodically
   - Look for suspicious patterns
   - Verify compliance

3. **Access Control:**
   - Only admins should access audit logs
   - Consider read-only access for auditors

4. **Backup:**
   - Include audit logs in database backups
   - Consider separate backup for audit data

5. **Performance:**
   - Indexes are in place for common queries
   - Consider partitioning for very large tables
   - Archive old logs if needed

---

## Migration

To create the audit log table, run Alembic migrations:

```bash
cd alembic
python -m alembic revision --autogenerate -m "Add identity audit log table"
python -m alembic upgrade head
```

---

## Summary

✅ **All identity management operations are logged**
✅ **Complete audit trail with before/after states**
✅ **IP address and user agent tracking**
✅ **Error logging for failed operations**
✅ **Forensic-ready for investigations**
✅ **Compliance-ready for regulations**

**The system now provides comprehensive audit logging for all identity management operations!** 🎉

---

**Last Updated:** 2025-01-27

