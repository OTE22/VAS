# Live Search Alerts - Complete Guide

> **🔔 Real-Time Monitoring System**
> 
> Get notified instantly when a tracked person is detected again in your surveillance system.

## Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [How It Works](#how-it-works)
4. [Creating Live Alerts](#creating-live-alerts)
5. [Alert Configuration](#alert-configuration)
6. [Managing Alerts](#managing-alerts)
7. [Notifications](#notifications)
8. [API Reference](#api-reference)
9. [Configuration](#configuration)
10. [Best Practices](#best-practices)
11. [Troubleshooting](#troubleshooting)

---

## Overview

**Live Search Alerts** allow you to track specific individuals and receive real-time notifications whenever they are detected by any camera in your surveillance system.

### Key Features

- ✅ **Real-Time Detection**: Instant notifications when tracked person appears
- ✅ **Multi-Channel Alerts**: Dashboard, Email, SMS, Webhook notifications
- ✅ **Smart Filtering**: Time windows, camera selection, similarity thresholds
- ✅ **Cooldown Management**: Prevent alert spam with configurable cooldown periods
- ✅ **Auto Actions**: Automatic snapshot capture and video recording
- ✅ **Expiration Options**: Set alerts to expire after date or number of detections
- ✅ **User Limits**: Configurable maximum alerts per user

### Use Cases

```
┌─────────────────────────────────────────────────────────────┐
│                    LIVE ALERT USE CASES                     │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  🚨 SECURITY          👤 VIP TRACKING      🔍 INVESTIGATION │
│  ──────────          ────────────        ──────────────    │
│  • Track suspects     • VIP arrival       • Monitor POI     │
│  • Threat detection   • Guest welcome     • Case tracking   │
│  • Access control     • Event security   • Evidence        │
│                                                             │
│  📊 MONITORING        ⏰ SCHEDULED         🎯 TARGETED       │
│  ────────────        ───────────         ──────────         │
│  • Behavior watch    • Time windows      • Specific cams   │
│  • Pattern analysis  • Day restrictions  • High precision   │
│  • Anomaly detection • Business hours    • Custom rules     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### Step 1: Search for the Person

1. Go to **Advanced Search** (`/admin/search`)
2. Upload an image or search for the person
3. Review the match results

### Step 2: Create Live Alert

1. Click on a **match result** to view identity details
2. Click **"CREATE LIVE ALERT"** button
3. Fill in alert name and settings
4. Click **"CREATE ALERT"**

### Step 3: Receive Notifications

When the person is detected:
- 🔔 **Dashboard notification** appears
- 📧 **Email alert** sent (if configured)
- 📱 **SMS alert** sent (if configured)
- 🔗 **Webhook** triggered (if configured)

---

## How It Works

### Detection Flow

```
┌──────────────┐
│ Face Detected│
└──────┬───────┘
       │
       ▼
┌──────────────────┐
│ Identity Matched │
└──────┬────────────┘
       │
       ▼
┌─────────────────────────┐
│ Check Active Alerts     │
│ for this Identity       │
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Apply Filters:          │
│ • Similarity threshold  │
│ • Time window           │
│ • Camera filter         │
│ • Cooldown check        │
└──────┬──────────────────┘
       │
       ▼
┌─────────────────────────┐
│ Trigger Alert           │
│ • Create trigger record │
│ • Send notifications    │
│ • Capture snapshot      │
│ • Record video (if on)  │
└─────────────────────────┘
```

### Alert Lifecycle

```
ACTIVE → TRIGGERED → (Cooldown) → ACTIVE
   │
   ├─→ PAUSED (manual pause)
   │
   └─→ EXPIRED (date/detections reached)
```

---

## Creating Live Alerts

### Method 1: From Advanced Search (Recommended)

1. **Search for person** in Advanced Search
2. **Click match result** → Opens identity details
3. **Click "CREATE LIVE ALERT"** button
4. **Configure settings** (backend provides defaults)
5. **Submit** → Alert created

### Method 2: From Identity Details (Unknown or Known Persons)

1. Navigate to **Unknown Faces** (`/admin/unknown`) or **Dashboard** (`/dashboard`)
2. **Click identity card** (or image for known persons) → Opens details modal
3. **Click "CREATE LIVE ALERT"** button
4. **View Identity ID** in the form (displayed automatically)
5. **Configure settings** (backend provides defaults)
6. **Submit** → Alert created

**Important Notes:**
- **Identity ID is displayed** in the alert creation form so you know exactly which identity the alert is for
- **For Unknown Persons**: The identity name may show "Unknown Identity", but the unique Identity ID is always visible
- **Copy Identity ID**: Click the Identity ID or copy button to copy it for reference
- **Backend Validation**: All validation is handled by the backend - frontend only collects form data

### Method 3: Via API

```bash
curl -X POST "http://localhost:8000/api/live-alerts" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Track John Doe - Investigation #123",
    "identity_id": "uuid-of-identity",
    "min_similarity": 0.75,
    "notify_dashboard": true,
    "sound_alert": true,
    "auto_capture_snapshot": true
  }'
```

### Backend Defaults

When creating an alert, the backend automatically provides:

- **Default Name**: `"Track {Identity Name} - {Date}"`
- **Default Similarity**: `0.75` (75%)
- **Default Cooldown**: From config (`LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES`)
- **Notifications**: Dashboard enabled, others disabled
- **Auto Actions**: Snapshot capture enabled
- **Identity ID**: Automatically displayed in the form (read-only)

**Identity ID Display:**
- The unique Identity ID is **always shown** in the alert creation form
- This ensures you know exactly which identity the alert is tracking
- For unknown persons, this is especially important since they may not have a name yet
- You can **copy the Identity ID** by clicking on it or the copy button

The backend also checks:
- ✅ User alert limit (max alerts per user)
- ✅ Identity existence
- ✅ Existing alerts for same identity
- ✅ All validation rules
- ✅ Identity ID format and validity

---

## Alert Configuration

### Basic Settings

| Setting | Description | Default |
|---------|-------------|---------|
| **Alert Name** | Descriptive name for the alert | Auto-generated |
| **Minimum Similarity** | Minimum match confidence to trigger | 0.75 (75%) |
| **Cooldown Minutes** | Time between alerts (prevents spam) | 30 minutes |

### Camera Filtering

- **All Cameras**: Monitor all pipelines (default)
- **Specific Cameras**: Select specific pipeline IDs
- **Exclude Cameras**: Coming soon

### Time Windows

Enable time-based filtering:

- **Start Time**: `HH:MM` format (e.g., "09:00")
- **End Time**: `HH:MM` format (e.g., "17:00")
- **Active Days**: Select days (0=Sunday, 6=Saturday)

**Example**: Alert only during business hours (Mon-Fri, 9 AM - 5 PM)

### Expiration Options

| Type | Description | Use Case |
|------|-------------|----------|
| **Never** | Alert stays active until deleted | Long-term monitoring |
| **Date** | Expires on specific date | Temporary investigations |
| **Detections** | Expires after N detections | Event-based tracking |

---

## Managing Alerts

### View All Alerts

Navigate to **Live Alerts** page (`/admin/live-alerts`)

### Alert Actions

| Action | Description | API Endpoint |
|--------|-------------|--------------|
| **Pause** | Temporarily disable alert | `POST /api/live-alerts/{id}/pause` |
| **Resume** | Re-enable paused alert | `POST /api/live-alerts/{id}/resume` |
| **Update** | Modify alert settings | `PUT /api/live-alerts/{id}` |
| **Delete** | Remove alert permanently | `DELETE /api/live-alerts/{id}` |
| **View Triggers** | See alert history | `GET /api/live-alerts/{id}/triggers` |

### Alert Status

- **ACTIVE**: Alert is monitoring and will trigger
- **PAUSED**: Alert is temporarily disabled
- **EXPIRED**: Alert reached expiration (date/detections)
- **TRIGGERED**: Alert was recently triggered (cooldown active)

---

## Notifications

### Notification Channels

#### 1. Dashboard Notification

- **Real-time popup** in admin dashboard
- **Sound alert** (if enabled)
- **Visual indicator** with person's snapshot
- **Click to view** identity details

#### 2. Email Notification

- **Recipients**: List of email addresses
- **Content**: Alert name, identity info, detection details
- **Attachments**: Snapshot (if auto-capture enabled)

#### 3. SMS Notification

- **Recipients**: List of phone numbers
- **Content**: Short alert message with identity name
- **Format**: `"Alert: {Alert Name} - {Identity Name} detected at {Camera}"`

#### 4. Webhook Notification

- **URL**: Your webhook endpoint
- **Method**: POST
- **Payload**: JSON with alert and detection details
- **Use Case**: Integrate with external systems (Slack, Teams, etc.)

### Notification Payload Example

```json
{
  "alert_id": "uuid",
  "alert_name": "Track John Doe - Investigation #123",
  "identity_id": "uuid",
  "identity_name": "John Doe",
  "similarity": 0.92,
  "pipeline_id": "camera-01",
  "detection_id": 12345,
  "snapshot_path": "/storage/snapshots/...",
  "triggered_at": "2025-01-05T10:30:00Z",
  "location": "Main Entrance"
}
```

---

## API Reference

### Get Default Alert Settings

**Endpoint**: `GET /api/live-alerts/defaults/{identity_id}`

**Description**: Get default settings for creating an alert (backend provides all defaults)

**Response**:
```json
{
  "identity_id": "uuid",
  "identity_name": "John Doe",
  "identity_type": "known",
  "default_name": "Track John Doe - 2025-01-05",
  "default_min_similarity": 0.75,
  "default_notify_dashboard": true,
  "default_sound_alert": true,
  "default_auto_capture": true,
  "default_cooldown_minutes": 30,
  "can_create": true,
  "user_alert_count": 5,
  "max_alerts": 50,
  "existing_alerts_count": 0,
  "warnings": []
}
```

### Create Live Alert

**Endpoint**: `POST /api/live-alerts`

**Request Body**:
```json
{
  "name": "Track John Doe - Investigation #123",
  "identity_id": "uuid-of-identity",
  "min_similarity": 0.75,
  "pipeline_ids": ["camera-01", "camera-02"],  // null = all cameras
  "time_window_enabled": false,
  "time_window_start": "09:00",
  "time_window_end": "17:00",
  "active_days": [1, 2, 3, 4, 5],  // Mon-Fri
  "cooldown_minutes": 30,
  "notify_dashboard": true,
  "notify_email": false,
  "notify_sms": false,
  "notify_webhook": false,
  "email_recipients": ["admin@example.com"],
  "sms_recipients": ["+1234567890"],
  "webhook_url": "https://your-webhook.com/alert",
  "sound_alert": true,
  "auto_capture_snapshot": true,
  "auto_record_clip": false,
  "clip_duration_seconds": 60,
  "expiration_type": "never",  // "never", "date", "detections"
  "expiration_date": null,  // ISO format if expiration_type="date"
  "expiration_detections": null  // Number if expiration_type="detections"
}
```

**Response**:
```json
{
  "id": "alert-uuid",
  "name": "Track John Doe - Investigation #123",
  "identity_id": "uuid",
  "identity_name": "John Doe",
  "status": "active",
  "min_similarity": 0.75,
  "triggers_count": 0,
  "last_triggered_at": null,
  "created_at": "2025-01-05T10:00:00Z"
}
```

### List Live Alerts

**Endpoint**: `GET /api/live-alerts`

**Query Parameters**:
- `include_inactive` (boolean): Include expired/paused alerts (default: false)

**Response**: Array of alert objects

### Get Alert Details

**Endpoint**: `GET /api/live-alerts/{alert_id}`

**Response**: Single alert object with full details

### Update Alert

**Endpoint**: `PUT /api/live-alerts/{alert_id}`

**Request Body**: Same as create, but all fields optional

### Pause Alert

**Endpoint**: `POST /api/live-alerts/{alert_id}/pause`

**Response**: Updated alert object with `status: "paused"`

### Resume Alert

**Endpoint**: `POST /api/live-alerts/{alert_id}/resume`

**Response**: Updated alert object with `status: "active"`

### Delete Alert

**Endpoint**: `DELETE /api/live-alerts/{alert_id}`

**Response**: `{"message": "Alert deleted successfully"}`

### Get Alert Triggers

**Endpoint**: `GET /api/live-alerts/{alert_id}/triggers`

**Query Parameters**:
- `limit` (int): Max results (default: 100, max: 500)
- `offset` (int): Pagination offset (default: 0)

**Response**: Array of trigger records with detection details

---

## Configuration

### Environment Variables

Add to `config.py` or `.env`:

```python
# Live Alerts Configuration
LIVE_ALERTS_ENABLED = True  # Enable/disable live alerts feature
LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES = 30  # Default cooldown between alerts
LIVE_ALERT_MAX_PER_USER = 50  # Maximum alerts per user
```

### Settings Page

Configure via web interface:
1. Go to **Settings** (`/admin/settings`)
2. Find **"Live Alerts"** category
3. Adjust values as needed
4. Save changes

### Configuration Variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `LIVE_ALERTS_ENABLED` | boolean | `True` | Enable/disable live alerts feature |
| `LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES` | int | `30` | Default cooldown period (minutes) |
| `LIVE_ALERT_MAX_PER_USER` | int | `50` | Maximum active alerts per user |

---

## Best Practices

### 1. Alert Naming

Use descriptive names:
- ✅ `"Track John Doe - Investigation #123"`
- ✅ `"VIP Alert - CEO Arrival"`
- ❌ `"Alert 1"` or `"Test"`

### 2. Similarity Thresholds

- **High Precision** (0.90+): Critical alerts, VIP tracking
- **Standard** (0.75-0.90): General monitoring
- **Lower** (0.60-0.75): Broader detection (more false positives)

### 3. Cooldown Management

- **Short** (5-15 min): High-priority alerts
- **Standard** (30 min): General monitoring
- **Long** (60+ min): Low-priority tracking

### 4. Time Windows

Use time windows to:
- Reduce false positives during off-hours
- Focus on business hours only
- Monitor specific events

### 5. Expiration Strategy

- **Never**: Long-term monitoring (suspects, VIPs)
- **Date**: Temporary investigations (events, cases)
- **Detections**: One-time alerts (arrival notifications)

### 6. Notification Channels

- **Dashboard**: Always enabled for real-time awareness
- **Email**: Important alerts, reports
- **SMS**: Critical alerts only (cost consideration)
- **Webhook**: Integration with external systems

### 7. User Limits

Monitor your alert count:
- Check `user_alert_count` vs `max_alerts`
- Delete expired/unused alerts
- Use expiration for temporary alerts

---

## Troubleshooting

### Alert Not Triggering

**Check**:
1. ✅ Alert status is `ACTIVE` (not paused/expired)
2. ✅ Detection similarity >= `min_similarity`
3. ✅ Current time within time window (if enabled)
4. ✅ Camera in `pipeline_ids` list (if specified)
5. ✅ Cooldown period has passed
6. ✅ `LIVE_ALERTS_ENABLED = True` in config

### Too Many Alerts

**Solutions**:
1. Increase `cooldown_minutes`
2. Raise `min_similarity` threshold
3. Enable time windows
4. Filter by specific cameras
5. Use expiration (date/detections)

### Notifications Not Received

**Check**:
1. ✅ Notification channel enabled in alert settings
2. ✅ Email/SMS recipients configured correctly
3. ✅ Webhook URL is valid and accessible
4. ✅ Dashboard notifications enabled
5. ✅ Browser allows notifications

### Backend Errors

**Common Issues**:
- `"Maximum alerts per user reached"`: Delete old alerts or increase limit
- `"Identity not found"`: Identity may have been deleted/merged
- `"Invalid expiration_date format"`: Use ISO format (YYYY-MM-DDTHH:MM:SS)

---

## Related Documentation

- **Advanced Search Guide**: `39_ADVANCED_SEARCH_INTELLIGENCE_GUIDE.md` - Search features
- **Configuration Guide**: `36_CONFIGURATION_GUIDE.md` - System settings
- **API Authentication**: `25_API_AUTHENTICATION_GUIDE.md` - API usage

---

**Last Updated:** January 2025  
**Version:** 1.0.0

