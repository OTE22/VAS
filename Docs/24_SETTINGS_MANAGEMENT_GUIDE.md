# Settings Management Guide

## Overview

The Settings Management system allows administrators to view, modify, and track all system configuration variables through a user-friendly web interface. All changes are logged in an audit trail for security and compliance.

## Accessing Settings

**Path**: Admin → Settings

**Requirements**: Admin role only

**URL**: `/admin/settings`

## What Are Settings?

Settings are configuration variables that control how the system behaves. Examples include:
- `CACHE_TTL`: How long cached entries stay valid (seconds)
- `SIMILARITY_THRESHOLD`: How similar faces must be to match
- `DATA_RETENTION_DAYS`: How long to keep old data
- `CLUSTER_INTERVAL_HOURS`: How often to generate merge suggestions

## Understanding the Settings Page

### Layout

The settings page is organized into three main sections:

1. **Category Filters** (Top)
   - Filter settings by category (e.g., "Database", "Face Recognition Models", "Data Retention")
   - "All Settings" button shows everything
   - Red "Refresh" button to reload settings

2. **Settings Cards** (Middle)
   - Each setting displayed as a compact card
   - Shows: Setting key, value, type, category, and badges
   - Edit button (if editable) or Readonly indicator

3. **Audit Log** (Bottom)
   - History of all setting changes
   - Shows: Who changed it, when, old value, new value, and reason

### Setting Card Information

Each setting card displays:

- **Setting Key**: The configuration variable name (e.g., `SIMILARITY_THRESHOLD`)
- **Value**: Current setting value (hidden if sensitive)
- **Type**: Data type (string, integer, boolean, etc.)
- **Category**: Grouping category
- **Badges**:
  - 🔒 **Sensitive**: Value is hidden for security (passwords, keys)
  - 🔒 **Readonly**: Cannot be modified (system-protected)
  - **Category Badge**: Shows the setting category

## How to View Settings

### Step 1: Access Settings Page
- Navigate to **Admin → Settings**
- Page loads all settings automatically

### Step 2: Filter by Category
- Click category buttons at the top (e.g., "Database", "Security")
- Or click "All Settings" to see everything

### Step 3: View Setting Details
- Hover over a setting card to see full information
- Click "Edit" button to view/edit (if editable)

## How to Edit Settings

### Step 1: Find the Setting
- Browse settings or use category filters
- Look for the setting you want to change

### Step 2: Click Edit
- Click the **Edit** button (pencil icon) on the setting card
- A modal form will appear

### Step 3: Review Current Value
- Modal shows:
  - Setting key (read-only)
  - Current value
  - Input field for new value
  - Type hint (what format to use)

### Step 4: Enter New Value
- Type the new value in the "New Value" field
- Follow the type hint (string, number, true/false, etc.)
- Examples:
  - String: `"production"`
  - Number: `50000`
  - Boolean: `true` or `false`
  - List: `item1,item2,item3`

### Step 5: Add Change Reason (Optional)
- Explain why you're making this change
- Helps with audit trail and troubleshooting

### Step 6: Save Changes
- Click **"Save Changes"** button
- System validates the value
- If valid: Change is saved and logged
- If invalid: Error message shows what's wrong

### Step 7: Verify Change
- Check the audit log at the bottom
- Your change should appear with timestamp
- Refresh the page to see updated value

## Understanding Setting Types

### String
- Text values
- Example: `"production"`, `"http://localhost:8000"`
- Use quotes if needed

### Integer
- Whole numbers
- Example: `50000`, `24`, `100`

### Float
- Decimal numbers
- Example: `0.4`, `3.14`, `0.95`

### Boolean
- True or false
- Example: `true`, `false`
- Also accepts: `1`, `0`, `yes`, `no`

### List
- Comma-separated values
- Example: `item1,item2,item3`
- Or: `.jpg,.png,.webp`

## Setting Categories

Settings are organized into categories:

| Category | Covers |
|---|---|
| `server` | Host, port, workers, debug, logging |
| `security` | JWT secret, algorithm, token expiry |
| `database` | Connection URL, pool sizing, statement timeouts |
| `cache` | Redis URL, max connections, cache TTL |
| `models` | Detection/recognition model paths, match thresholds |
| `processing` | Queue size, workers, concurrency, pipeline batch size |
| `storage` | Storage root, image-saving flags, max upload size |
| `tracking` | Face tracking window, memory, dashboard display hours |
| `identity` | Enrollment bands, clustering, retention, vector backend |
| `retention` | Data/audit/task-history retention, backup cadence, batch writes |
| `ollama` | Local LLM base URL, model, temperature, timeout |
| `sql_agent` | Chatbot RAG and concurrency limits |
| `advanced_search` | Result depth, quality gates, confidence bands, live alerts, notification transports |
| `ml_ops` | ML decision mode, drift, retention, feature flags |
| `advanced` | Auto-filled. Any key in `SETTINGS_REGISTRY` that is not in the hand-maintained map above lands here, so a registered setting can never render nowhere. ~30 keys currently. |

## Sensitive Settings

Some settings are marked as **Sensitive**:
- Values are hidden (shows `***HIDDEN***`)
- Examples: Passwords, API keys, secret keys
- Still editable, but value is masked for security

**To edit sensitive settings:**
1. Click Edit button
2. Enter new value (you won't see current value)
3. Save changes
4. Value is updated but remains hidden

## Readonly Settings

Some settings are marked as **Readonly**:
- Cannot be modified through the interface
- System-protected settings
- Usually set at startup or by system processes
- Edit button is disabled

**To change readonly settings:**
- Modify the `.env` file (or compose environment) directly
- Recreate the container

> **Precedence is admin DB value → environment → default.** A value saved on
> this page **outranks `.env`** and is re-applied on every startup. Editing
> `.env` will NOT override a setting an admin has already changed here — clear
> the stored value first if you want the environment to win again. The API
> exposes `env_value` and `overridden` whenever the two disagree, so the
> divergence is visible rather than mysterious.

## Audit Log

The audit log tracks all setting changes:

### Information Shown
- **Setting**: Which setting was changed
- **Old Value**: Previous value
- **New Value**: Updated value
- **Changed By**: Username who made the change
- **Reason**: Why the change was made (if provided)
- **Date & Time**: When the change occurred

### Use Cases
- **Security**: Track who changed what and when
- **Troubleshooting**: See what changed before an issue
- **Compliance**: Maintain change history
- **Rollback**: Know what to revert if needed

## Common Settings to Modify

### Performance Tuning
- `WORKERS`: Number of worker processes (default: 4)
- `PIPELINE_BATCH_SIZE`: Frames processed per pipeline batch (default: 5)
- `REDIS_MAX_CONNECTIONS`: Redis connection pool size (default: 100)
- `DB_POOL_SIZE`: Database connection pool (default: 50)

### Face Recognition
- `SIMILARITY_THRESHOLD`: Face matching threshold (default: 0.4)
- `CONFIDENCE_THRESHOLD`: Detection confidence (default: 0.5)
- `CLUSTER_EPS`: Clustering similarity (default: 0.35)

### Data Retention
- `DATA_RETENTION_DAYS`: How long to keep data (default: 30)
- `SNAPSHOT_RETENTION_DAYS`: Photo retention (default: 90)
- `EMBEDDING_RETENTION_MONTHS`: Face data retention (default: 12)

### Clustering
- `CLUSTER_INTERVAL_HOURS`: How often to generate suggestions (default: 24)
- `CLUSTER_MIN_SIZE`: Minimum cluster size (default: 2)
- `CLUSTER_MIN_SAMPLES`: Minimum samples per cluster (default: 2)

## Best Practices

### Before Changing Settings
1. **Understand the impact**: Know what the setting controls
2. **Check documentation**: Read about the setting first
3. **Test in development**: Try changes in test environment first
4. **Backup current values**: Note current values before changing

### When Changing Settings
1. **Add a reason**: Always provide a change reason in audit log
2. **Change one at a time**: Make one change, test, then proceed
3. **Verify type**: Ensure value matches the expected type
4. **Check dependencies**: Some settings affect others

### After Changing Settings
1. **Verify the change**: Check audit log to confirm
2. **Test functionality**: Ensure system still works correctly
3. **Monitor performance**: Watch for any negative impacts
4. **Document changes**: Note important changes for team

## Troubleshooting

### Setting Won't Save
- **Check type**: Ensure value matches setting type
- **Check readonly**: Setting might be readonly
- **Check validation**: Value might be invalid
- **Check permissions**: Ensure you're logged in as admin

### Setting Not Taking Effect
- **Check `apply_mode` in the PUT response.** `immediate` / `next_request` /
  `next_job_run` are live already; `api_restart` and `index_rebuild` take effect
  when the API container next starts; `container_recreate` needs the environment
  changed and the container recreated.
- **Restart-required settings really do apply now.** Startup hydration loads every
  stored value regardless of apply_mode, before any component is built. This was
  previously broken — hydration skipped all non-dynamic keys, so a setting
  labelled "requires restart" was stored and then ignored forever.
- **403 on save?** Cookie-authenticated callers must send
  `X-Requested-With: XMLHttpRequest`. Bearer-token clients are exempt.
- **422 on save?** The value is out of range; the message names the field and the
  bound. Values are refused, never silently corrected.
- **Check logs**: Look for `[SETTINGS]` lines in the system logs
- **Verify value**: Check if the value was actually saved (audit log)

### Can't See Setting Value
- **Sensitive setting**: Value is hidden for security
- **Check edit modal**: Value shown when editing
- **Check audit log**: See value in change history

### Settings Page Not Loading
- **Check authentication**: Ensure you're logged in as admin
- **Check network**: Verify connection to server
- **Check browser console**: Look for JavaScript errors
- **Refresh page**: Try reloading the page

## API Access

Settings can also be managed via API:

### Get All Settings
```bash
GET /api/settings
Authorization: Bearer YOUR_TOKEN
```

### Get Setting by Key
```bash
GET /api/settings/{setting_key}
Authorization: Bearer YOUR_TOKEN
```

### Update Setting
```bash
PUT /api/settings/{setting_key}
Authorization: Bearer YOUR_TOKEN
Content-Type: application/json
X-Requested-With: XMLHttpRequest      # required for cookie auth; harmless with a Bearer token

{
  "value": "new_value",
  "change_reason": "Updating for performance tuning"
}
```

### Get Audit Log
```bash
GET /api/settings/audit/log?limit=50
Authorization: Bearer YOUR_TOKEN
```

## Settings Sync

Three layers, with the database on top:

1. **`config.py`**: declares every setting and its default. The only interface —
   nothing else reads the environment for a setting.
2. **Environment / `.env`**: overrides the default at process start.
3. **Database (`settings` table)**: an admin edit here outranks both, and is
   re-applied at every startup so it survives restarts.

**What happens when:**
- **On startup** — `hydrate_from_db` applies every admin-modified stored value
  to the running configuration, before any component is constructed.
- **On save** — the value is validated, persisted, and (for dynamic modes)
  pushed into the running process immediately.
- **On a settings-page load** — `sync_settings_from_config` *seeds* rows for any
  newly declared setting and removes rows for settings that no longer exist. It
  is seed-only: it never overwrites a stored value. There is no `.env` file
  watcher.

## Related Documentation

- **Configuration Guide**: See `config.py` for all available settings
- **Environment Variables**: See `.env` file for current values
- **API Documentation**: See API docs for programmatic access
- **Audit Logging**: See audit logging guide for more details

## Quick Reference

| Action | Steps |
|--------|-------|
| View all settings | Admin → Settings → "All Settings" |
| Filter by category | Click category button (e.g., "Database") |
| Edit a setting | Click Edit button → Enter new value → Save |
| View change history | Scroll to Audit Log section |
| Find a setting | Use category filter or search visually |
| Refresh settings | Click red "Refresh" button |

## Summary

The Settings Management system provides:
- ✅ Easy-to-use interface for all configuration
- ✅ Category-based organization
- ✅ Secure handling of sensitive values
- ✅ Complete audit trail
- ✅ Type validation
- ✅ Readonly protection for system settings
- ✅ API access for automation

Use this system to safely manage your Face Recognition Service configuration without editing files directly.

