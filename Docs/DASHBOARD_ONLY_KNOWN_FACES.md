# Dashboard: Only KNOWN Faces

## Overview

The dashboard (home page) now **ONLY** displays faces that are explicitly marked as **KNOWN**. All UNKNOWN faces are completely filtered out and will not appear on the dashboard.

## Backend Filtering

### WebSocket Endpoint (`backend/routes/websocket.py`)

**Initial Data Loading:**
- Only loads KNOWN identities within retention period
- UNKNOWN identities are completely excluded from initial data
- Logs: `"Dashboard filter: Loading ONLY KNOWN identities (UNKNOWN excluded)"`

**Face Filtering Logic:**
1. **Check 1: `label_state == AUTO_KNOWN`** → Include face
2. **Check 2: `identity.type == KNOWN`** → Include face (PRIMARY CHECK)
3. **Check 3: Explicit UNKNOWN checks** → Exclude face:
   - `label_state == AUTO_UNKNOWN` → Exclude
   - `name == "Unknown"` → Exclude
   - `identity.type == UNKNOWN` → Exclude

**Result:**
- Only faces with `identity.type == IdentityType.KNOWN` OR `label_state == LabelState.AUTO_KNOWN` are sent to frontend
- All UNKNOWN faces are filtered out regardless of `SHOW_UNKNOWN_FACES_ON_DASHBOARD` setting
- Logs clearly indicate which faces are included/excluded

## Frontend Filtering

The frontend (`frontend/js/dashboard.js`) has additional safety checks:
- Filters out faces with `name == "Unknown"`
- Only processes faces sent from backend (which are already filtered)

## Configuration

- **`SHOW_UNKNOWN_FACES_ON_DASHBOARD`**: This setting is **ignored** for the dashboard
- Dashboard **always** shows only KNOWN faces
- UNKNOWN faces are only visible on the "Unknown Faces" page (`/admin/unknown`)

## Logging

The backend logs clearly show:
- `✅ INCLUDED KNOWN face '{name}' for dashboard`
- `❌ EXCLUDED face '{name}' - not marked as KNOWN`
- `🔒 Dashboard filter: ONLY KNOWN faces will be sent to frontend`

## Result

✅ **Dashboard shows ONLY KNOWN faces**  
✅ **UNKNOWN faces are completely excluded**  
✅ **No duplicate identities (KNOWN and UNKNOWN)**  
✅ **Clear logging for debugging**

