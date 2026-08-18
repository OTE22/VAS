# Advanced Search Intelligence System - Complete Guide

> **🎉 Implementation Status: COMPLETE**
> 
> All backend services and API endpoints have been implemented and are ready to use.

## Table of Contents
1. [Overview](#overview)
2. [Feature Summary](#feature-summary)
3. [Implementation Status](#implementation-status)
4. [Multi-Face Detection](#1-multi-face-detection)
5. [Face Quality Scoring](#2-face-quality-scoring)
6. [Watchlist Mode](#3-watchlist-mode)
7. [Live Search Alerts](#4-live-search-alerts)
8. [Related Identities](#5-related-identities)
9. [Temporal Patterns](#6-temporal-patterns)
10. [Cross-Camera Tracking](#7-cross-camera-tracking)
11. [Batch Search](#8-batch-search)
12. [Search History](#9-search-history)
13. [Export Results](#10-export-results)
14. [Confidence Bands](#11-confidence-bands)
15. [Negative Search](#12-negative-search)
16. [Database Schema](#database-schema)
17. [API Reference](#api-reference)
18. [Frontend Integration](#frontend-integration)

---

## Implementation Status

| Feature | Backend | API | Frontend | Status |
|---------|---------|-----|----------|--------|
| Multi-Face Detection | ✅ | ✅ | ⏳ | Ready |
| Face Quality Scoring | ✅ | ✅ | ⏳ | Ready |
| Watchlist Management | ✅ | ✅ | ⏳ | Ready |
| Live Search Alerts | ✅ | ✅ | ⏳ | Ready |
| Related Identities | ✅ | ✅ | ⏳ | Ready |
| Temporal Patterns | ✅ | ✅ | ⏳ | Ready |
| Cross-Camera Tracking | ✅ | ✅ | ⏳ | Ready |
| Batch Search | ✅ | ✅ | ⏳ | Ready |
| Search History | ✅ | ✅ | ⏳ | Ready |
| Export Results | ✅ | ✅ | ⏳ | Ready |
| Confidence Bands | ✅ | ✅ | ⏳ | Ready |
| Negative Search | ✅ | ✅ | ⏳ | Ready |

**Legend**: ✅ Complete | ⏳ Pending | ❌ Not Started

---

## Overview

The **Advanced Search Intelligence System** transforms basic face search into a production-grade investigation and monitoring platform. This system is designed for agencies, security operations, and enterprise surveillance needs.

### Key Capabilities

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    ADVANCED SEARCH INTELLIGENCE                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  🔍 SEARCH          🎯 WATCHLIST       📊 ANALYTICS      🔔 ALERTS     │
│  ─────────          ───────────       ───────────      ────────        │
│  • Multi-face       • VIP list        • Temporal       • Live alerts   │
│  • Batch upload     • Threat list     • Heat maps      • Email/SMS     │
│  • Quality score    • Auto-check      • Patterns       • Webhook       │
│  • History          • Instant alert   • Co-appearance  • Dashboard     │
│                                                                         │
│  🗂️ ORGANIZATION    🔗 INTELLIGENCE   📤 EXPORT        ⚙️ CONFIG       │
│  ──────────────     ──────────────   ─────────        ─────────        │
│  • Saved searches   • Related IDs    • CSV/Excel      • Thresholds    │
│  • Confidence bands • Cross-camera   • PDF reports    • Retention     │
│  • Tags/notes       • Movement path  • JSON/API       • Permissions   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Feature Summary

| Feature | Description | Use Case |
|---------|-------------|----------|
| **Multi-Face Detection** | Detect and search all faces in one image | Group photos, CCTV frames |
| **Face Quality Scoring** | Rate image quality before search | Improve results, warn users |
| **Watchlist Mode** | Check against VIP/threat lists | Instant POI detection |
| **Live Search Alerts** | Notify when searched face appears again | Ongoing monitoring |
| **Related Identities** | Show who appears with the person | Network analysis |
| **Temporal Patterns** | When/where person typically appears | Behavioral intelligence |
| **Cross-Camera Tracking** | Track movement across cameras | Path reconstruction |
| **Batch Search** | Search multiple images at once | Bulk investigation |
| **Search History** | View and rerun past searches | Audit, comparison |
| **Export Results** | Download as CSV/PDF/JSON | Reports, evidence |
| **Confidence Bands** | Group results by confidence | Prioritize review |
| **Negative Search** | Exclude known faces from results | Filter out irrelevant |

---

## 1. Multi-Face Detection

### What It Does
When you upload an image containing multiple people, the system detects ALL faces and searches for each one simultaneously.

### Example Scenario

**Input**: CCTV frame showing 4 people at entrance

```
┌─────────────────────────────────────────────────────┐
│                    CCTV FRAME                       │
│                                                     │
│      👤          👤          👤          👤         │
│    Face 1      Face 2      Face 3      Face 4      │
│   (Quality:   (Quality:   (Quality:   (Quality:    │
│    0.95)       0.67)       0.89)       0.42)       │
│                                                     │
└─────────────────────────────────────────────────────┘
```

**API Request**:
```bash
POST /api/search/by-image/multi
Content-Type: multipart/form-data

image: [CCTV frame file]
scope: both
top_k: 5
min_quality: 0.5
```

**Response**:
```json
{
  "search_id": "srch_abc123",
  "image_info": {
    "dimensions": "1920x1080",
    "faces_detected": 4,
    "faces_searchable": 3
  },
  "faces": [
    {
      "face_index": 0,
      "bounding_box": {"x1": 100, "y1": 50, "x2": 200, "y2": 180},
      "quality_score": 0.95,
      "quality_details": {
        "blur": 0.92,
        "lighting": 0.98,
        "face_size": 0.95,
        "angle": 0.94
      },
      "matches": [
        {
          "identity_id": "550e8400-e29b-41d4-a716-446655440000",
          "display_name": "John Smith",
          "type": "known",
          "similarity": 0.91,
          "confidence_band": "HIGH",
          "watchlist_match": null
        }
      ]
    },
    {
      "face_index": 1,
      "bounding_box": {"x1": 300, "y1": 60, "x2": 380, "y2": 170},
      "quality_score": 0.67,
      "quality_details": {
        "blur": 0.55,
        "lighting": 0.80,
        "face_size": 0.70,
        "angle": 0.62
      },
      "quality_warning": "Moderate blur detected - results may be less accurate",
      "matches": [
        {
          "identity_id": "660e8400-e29b-41d4-a716-446655440001",
          "display_name": null,
          "type": "unknown",
          "similarity": 0.72,
          "confidence_band": "MEDIUM"
        }
      ]
    },
    {
      "face_index": 2,
      "bounding_box": {"x1": 500, "y1": 45, "x2": 590, "y2": 165},
      "quality_score": 0.89,
      "matches": [
        {
          "identity_id": "770e8400-e29b-41d4-a716-446655440002",
          "display_name": "Ahmed Hassan",
          "type": "known",
          "similarity": 0.88,
          "confidence_band": "HIGH",
          "watchlist_match": {
            "list_name": "VIP",
            "priority": "high",
            "notes": "Board member - notify reception"
          }
        }
      ]
    },
    {
      "face_index": 3,
      "bounding_box": {"x1": 700, "y1": 70, "x2": 760, "y2": 150},
      "quality_score": 0.42,
      "skipped": true,
      "skip_reason": "Quality below minimum threshold (0.42 < 0.50)",
      "matches": []
    }
  ],
  "watchlist_alerts": [
    {
      "face_index": 2,
      "list_name": "VIP",
      "identity_name": "Ahmed Hassan",
      "action_required": "Notify reception"
    }
  ],
  "processing_time_ms": 342
}
```

### Frontend Display

```
┌─────────────────────────────────────────────────────────────────────┐
│  MULTI-FACE SEARCH RESULTS                           [Export] [Save]│
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  📷 Uploaded Image                    📊 Summary                    │
│  ┌─────────────────────┐              Faces Detected: 4             │
│  │  [Image with face   │              Faces Searched: 3             │
│  │   boxes drawn]      │              Watchlist Alerts: 1 ⚠️         │
│  │                     │              Processing: 342ms             │
│  └─────────────────────┘                                            │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  FACE 1 ✅ Quality: 95%                                             │
│  ┌──────┐  Best Match: John Smith (91% - HIGH)                      │
│  │ 👤   │  Type: KNOWN | Last seen: 2 hours ago                     │
│  └──────┘  [View Profile] [Create Alert]                            │
├─────────────────────────────────────────────────────────────────────┤
│  FACE 2 ⚠️ Quality: 67% (Blur detected)                             │
│  ┌──────┐  Best Match: Unknown #abc123 (72% - MEDIUM)               │
│  │ 👤   │  Type: UNKNOWN | First seen: 3 days ago                   │
│  └──────┘  [View Profile] [Promote] [Create Alert]                  │
├─────────────────────────────────────────────────────────────────────┤
│  FACE 3 🚨 WATCHLIST ALERT - VIP                                    │
│  ┌──────┐  Match: Ahmed Hassan (88% - HIGH)                         │
│  │ 👤   │  ⭐ VIP: Board member - notify reception                   │
│  └──────┘  [View Profile] [Acknowledge Alert] [Create Alert]        │
├─────────────────────────────────────────────────────────────────────┤
│  FACE 4 ❌ Skipped - Quality too low (42%)                          │
│  ┌──────┐  Face detected but quality below threshold.               │
│  │ 👤   │  Try uploading a clearer image of this person.            │
│  └──────┘                                                           │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Face Quality Scoring

### Quality Metrics

| Metric | Weight | Description | Good Score |
|--------|--------|-------------|------------|
| **Blur** | 30% | Sharpness of facial features | > 0.7 |
| **Lighting** | 25% | Even illumination, no shadows | > 0.6 |
| **Face Size** | 20% | Face pixels relative to image | > 0.5 |
| **Angle** | 25% | Frontal vs profile | > 0.6 |

### Quality Bands

```
┌─────────────────────────────────────────────────────────────────┐
│                    QUALITY SCORE BANDS                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  EXCELLENT (0.85 - 1.00)  ████████████████████  ✅ Best results │
│  GOOD      (0.70 - 0.84)  ██████████████████    ✅ Reliable     │
│  MODERATE  (0.50 - 0.69)  ████████████          ⚠️ May vary     │
│  POOR      (0.30 - 0.49)  ████████              ⚠️ Unreliable   │
│  UNUSABLE  (0.00 - 0.29)  ████                  ❌ Skip search   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Example Quality Assessment

**Input**: Blurry surveillance image

```json
{
  "quality_assessment": {
    "overall_score": 0.58,
    "band": "MODERATE",
    "details": {
      "blur": {
        "score": 0.45,
        "issue": "Motion blur detected",
        "recommendation": "Use frame with less motion"
      },
      "lighting": {
        "score": 0.72,
        "issue": null
      },
      "face_size": {
        "score": 0.65,
        "issue": "Face is relatively small in frame",
        "recommendation": "Crop image closer to face"
      },
      "angle": {
        "score": 0.51,
        "issue": "Face is turned approximately 25°",
        "recommendation": "Use more frontal image if available"
      }
    },
    "warnings": [
      "⚠️ Moderate quality - search results may be less accurate",
      "💡 Tip: Motion blur detected - try a different frame from the video"
    ],
    "proceed_recommendation": true
  }
}
```

---

## 3. Watchlist Mode

### What It Does
Automatically checks every search result against configured watchlists (VIP, Threat, POI, Custom lists).

### Watchlist Types

| Type | Icon | Purpose | Alert Level |
|------|------|---------|-------------|
| **VIP** | ⭐ | Important guests, executives | Info |
| **THREAT** | 🚨 | Security threats, banned persons | Critical |
| **POI** | 🔍 | Persons of Interest (investigation) | High |
| **EMPLOYEE** | 👔 | Staff members | Info |
| **CUSTOM** | 🏷️ | User-defined lists | Configurable |

### Watchlist Database Schema

```sql
-- Watchlist definitions
CREATE TABLE watchlists (
    id UUID PRIMARY KEY,
    name VARCHAR(100) NOT NULL,          -- "VIP", "THREAT", etc.
    description TEXT,
    color VARCHAR(7),                     -- Hex color for UI
    icon VARCHAR(50),                     -- Icon identifier
    alert_level VARCHAR(20),              -- "info", "warning", "critical"
    notify_webhook BOOLEAN DEFAULT FALSE,
    notify_email BOOLEAN DEFAULT FALSE,
    notify_sms BOOLEAN DEFAULT FALSE,
    webhook_url TEXT,
    email_recipients TEXT[],
    sms_recipients TEXT[],
    is_active BOOLEAN DEFAULT TRUE,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW()
);

-- Identities on watchlists
CREATE TABLE watchlist_entries (
    id UUID PRIMARY KEY,
    watchlist_id UUID REFERENCES watchlists(id),
    identity_id UUID REFERENCES identities(id),
    priority VARCHAR(20) DEFAULT 'normal', -- "low", "normal", "high", "critical"
    notes TEXT,                            -- Why they're on the list
    action_instructions TEXT,              -- What to do when detected
    added_by UUID REFERENCES users(id),
    added_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP,                  -- Optional expiration
    is_active BOOLEAN DEFAULT TRUE,
    
    UNIQUE(watchlist_id, identity_id)
);

-- Watchlist alert history
CREATE TABLE watchlist_alerts (
    id UUID PRIMARY KEY,
    watchlist_entry_id UUID REFERENCES watchlist_entries(id),
    triggered_by VARCHAR(50),              -- "search", "detection", "batch"
    search_id UUID,                        -- If triggered by search
    detection_id UUID,                     -- If triggered by live detection
    similarity_score FLOAT,
    pipeline_id VARCHAR(100),              -- Where detected
    acknowledged BOOLEAN DEFAULT FALSE,
    acknowledged_by UUID REFERENCES users(id),
    acknowledged_at TIMESTAMP,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
```

### Example: Adding to Watchlist

**API Request**:
```bash
POST /api/watchlist/entries
Content-Type: application/json
Authorization: Bearer {token}

{
  "watchlist_id": "wl_threat_001",
  "identity_id": "550e8400-e29b-41d4-a716-446655440000",
  "priority": "critical",
  "notes": "Banned from premises - trespassing incident 2024-12-15",
  "action_instructions": "Do not allow entry. Contact security immediately. Call police if necessary.",
  "expires_at": null
}
```

### Example: Watchlist Alert During Search

```
┌─────────────────────────────────────────────────────────────────────┐
│  🚨 WATCHLIST ALERT - THREAT                                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────┐   MATCH FOUND: 89% Similarity                         │
│  │          │                                                       │
│  │  [Face]  │   Name: Michael Torres                                │
│  │          │   List: THREAT (Critical Priority)                    │
│  └──────────┘                                                       │
│                                                                     │
│  📋 NOTES:                                                          │
│  Banned from premises - trespassing incident 2024-12-15             │
│                                                                     │
│  ⚡ ACTION REQUIRED:                                                 │
│  Do not allow entry. Contact security immediately.                  │
│  Call police if necessary.                                          │
│                                                                     │
│  Added to list by: Admin John (2024-12-16)                          │
│  Last detected: Never                                               │
│                                                                     │
│  [View Full Profile] [Acknowledge] [Dismiss] [Contact Security]     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. Live Search Alerts

### What It Does
After searching for a face, you can create a "Live Alert" that notifies you whenever that person is detected again in the future.

### Use Cases
- 🔍 Investigation: Track suspect movements
- 👤 VIP arrival: Know when important guest arrives
- 🚨 Security: Alert when banned person returns
- 👨‍👩‍👧 Personal: Know when family member arrives

### Alert Configuration

```json
{
  "alert_id": "alert_xyz789",
  "name": "Track John Doe",
  "identity_id": "550e8400-e29b-41d4-a716-446655440000",
  "created_by": "admin@company.com",
  
  "trigger_conditions": {
    "min_similarity": 0.75,
    "pipelines": ["ENTRANCE-CAM", "LOBBY-CAM"],  // null = all cameras
    "time_window": {
      "enabled": true,
      "start_time": "08:00",
      "end_time": "18:00",
      "days": ["monday", "tuesday", "wednesday", "thursday", "friday"]
    },
    "cooldown_minutes": 30  // Don't alert again within 30 min
  },
  
  "notifications": {
    "dashboard": true,
    "email": ["security@company.com", "manager@company.com"],
    "sms": ["+1234567890"],
    "webhook": "https://slack.webhook.url/xxx",
    "sound_alert": true
  },
  
  "actions": {
    "auto_capture_snapshot": true,
    "auto_record_clip": true,  // Record 30s before/after
    "priority": "high"
  },
  
  "expiration": {
    "type": "date",  // "date", "detections", "never"
    "value": "2025-02-01T00:00:00Z"  // or count for "detections"
  },
  
  "status": "active",  // "active", "paused", "expired", "triggered"
  "created_at": "2025-01-05T12:00:00Z",
  "triggers_count": 0,
  "last_triggered": null
}
```

### Live Alert Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    LIVE ALERT FLOW                              │
└─────────────────────────────────────────────────────────────────┘

User Creates Alert                    System Monitors
─────────────────                    ────────────────
       │                                    │
       ▼                                    ▼
┌─────────────────┐               ┌─────────────────┐
│ Search by Image │               │ Live Detection  │
│ Find: John Doe  │               │ Every Camera    │
└────────┬────────┘               └────────┬────────┘
         │                                 │
         ▼                                 │
┌─────────────────┐                        │
│ Click "Create   │                        │
│ Live Alert"     │                        │
└────────┬────────┘                        │
         │                                 │
         ▼                                 ▼
┌─────────────────────────────────────────────────────┐
│                  ALERT ENGINE                        │
│  • Stores embedding in Redis                         │
│  • Registers notification preferences                │
│  • Monitors detection pipeline                       │
└────────────────────────┬────────────────────────────┘
                         │
            ┌────────────┴────────────┐
            │   When Detection        │
            │   Matches Alert:        │
            └────────────┬────────────┘
                         │
      ┌──────────────────┼──────────────────┐
      ▼                  ▼                  ▼
┌───────────┐     ┌───────────┐     ┌───────────┐
│ Dashboard │     │   Email   │     │  Webhook  │
│   Toast   │     │  + SMS    │     │  (Slack)  │
│   Alert   │     │   Alert   │     │   Alert   │
└───────────┘     └───────────┘     └───────────┘
```

### Frontend: Create Live Alert Modal

```
┌─────────────────────────────────────────────────────────────────────┐
│  🔔 CREATE LIVE ALERT                                         [X]  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Alert for: John Doe (Known Identity)                               │
│                                                                     │
│  ─────────────────────────────────────────────────                  │
│  📋 BASIC SETTINGS                                                  │
│  ─────────────────────────────────────────────────                  │
│                                                                     │
│  Alert Name: [Track John Doe - Investigation #123    ]              │
│                                                                     │
│  Minimum Similarity: [====●=====] 75%                               │
│                                                                     │
│  ─────────────────────────────────────────────────                  │
│  📍 WHERE TO MONITOR                                                │
│  ─────────────────────────────────────────────────                  │
│                                                                     │
│  ☑ All Cameras                                                      │
│  ☐ Specific Cameras:                                                │
│    ☐ ENTRANCE-CAM    ☐ LOBBY-CAM    ☐ PARKING-CAM                  │
│    ☐ ELEVATOR-CAM    ☐ CAFETERIA    ☐ RECEPTION                     │
│                                                                     │
│  ─────────────────────────────────────────────────                  │
│  ⏰ WHEN TO ALERT                                                   │
│  ─────────────────────────────────────────────────                  │
│                                                                     │
│  ☑ 24/7 Monitoring                                                  │
│  ☐ Specific Hours: [08:00] to [18:00]                               │
│                                                                     │
│  ☑ Mon ☑ Tue ☑ Wed ☑ Thu ☑ Fri ☐ Sat ☐ Sun                         │
│                                                                     │
│  Cooldown: [30] minutes (don't repeat alert within)                 │
│                                                                     │
│  ─────────────────────────────────────────────────                  │
│  📣 NOTIFICATIONS                                                   │
│  ─────────────────────────────────────────────────                  │
│                                                                     │
│  ☑ Dashboard notification (toast + sound)                           │
│  ☑ Email: [security@company.com, manager@company.com]               │
│  ☐ SMS: [+1234567890                                   ]            │
│  ☐ Webhook: [https://hooks.slack.com/...              ]             │
│                                                                     │
│  ─────────────────────────────────────────────────                  │
│  ⏱️ EXPIRATION                                                      │
│  ─────────────────────────────────────────────────                  │
│                                                                     │
│  ● Never expires                                                    │
│  ○ Expires on: [2025-02-01]                                         │
│  ○ After [5] detections                                             │
│                                                                     │
│  ─────────────────────────────────────────────────────────────────  │
│                                                                     │
│               [Cancel]                    [Create Alert] 🔔          │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 5. Related Identities

### What It Does
Shows identities that frequently appear together with the searched person (co-appearances). Useful for understanding social networks and associations.

### How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                  RELATED IDENTITIES ANALYSIS                    │
└─────────────────────────────────────────────────────────────────┘

Input: Search for "John Doe"
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  STEP 1: Find all appearances of John Doe                       │
│  ─────────────────────────────────────────                      │
│  • Jan 5, 09:15 - ENTRANCE-CAM                                  │
│  • Jan 5, 09:17 - LOBBY-CAM                                     │
│  • Jan 5, 12:30 - CAFETERIA                                     │
│  • Jan 4, 08:45 - ENTRANCE-CAM                                  │
│  • ... (45 total appearances)                                   │
└─────────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  STEP 2: Find other faces detected within ±5 minutes            │
│  ─────────────────────────────────────────────────              │
│  Time Window: `RELATED_IDENTITY_TIME_WINDOW_MINUTES` (default: ±30 minutes)                │
│  Same Camera: Yes (spatial proximity)                           │
└─────────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  STEP 3: Calculate co-appearance frequency                      │
│  ─────────────────────────────────────────                      │
│                                                                 │
│  Related Identity    │ Co-appearances │ % of John's visits      │
│  ────────────────────┼────────────────┼─────────────────────    │
│  Sarah Johnson       │      32        │      71%                │
│  Unknown #abc123     │      18        │      40%                │
│  Michael Chen        │      12        │      27%                │
│  Unknown #def456     │       8        │      18%                │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  STEP 4: Build relationship graph                               │
│  ─────────────────────────────────                              │
│                                                                 │
│                     Sarah Johnson                               │
│                    (71% co-appear)                              │
│                          │                                      │
│                    ┌─────┴─────┐                                │
│                    │           │                                │
│              Michael Chen   Unknown#abc                         │
│              (27%)          (40%)                               │
│                    │           │                                │
│                    └─────┬─────┘                                │
│                          │                                      │
│                     [JOHN DOE]                                  │
│                          │                                      │
│                     Unknown#def                                 │
│                     (18%)                                       │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### API Response

```json
{
  "identity_id": "john-doe-uuid",
  "display_name": "John Doe",
  "total_appearances": 45,
  "analysis_period": {
    "from": "2024-12-01T00:00:00Z",
    "to": "2025-01-05T23:59:59Z"
  },
  "co_appearance_window_minutes": 5,
  
  "related_identities": [
    {
      "identity_id": "sarah-johnson-uuid",
      "display_name": "Sarah Johnson",
      "type": "known",
      "co_appearances": 32,
      "co_appearance_percentage": 71.1,
      "relationship_strength": "strong",
      "common_locations": ["ENTRANCE-CAM", "CAFETERIA", "MEETING-ROOM-1"],
      "common_times": ["09:00-09:30", "12:00-13:00"],
      "first_co_appearance": "2024-12-05T09:15:00Z",
      "last_co_appearance": "2025-01-05T09:17:00Z"
    },
    {
      "identity_id": "unknown-abc123",
      "display_name": null,
      "type": "unknown",
      "co_appearances": 18,
      "co_appearance_percentage": 40.0,
      "relationship_strength": "moderate",
      "common_locations": ["ENTRANCE-CAM", "LOBBY-CAM"],
      "common_times": ["09:00-09:30"],
      "first_co_appearance": "2024-12-10T09:22:00Z",
      "last_co_appearance": "2025-01-04T09:18:00Z"
    }
  ],
  
  "network_insights": {
    "total_related": 4,
    "strong_relationships": 1,
    "moderate_relationships": 2,
    "weak_relationships": 1,
    "most_common_companion": "Sarah Johnson",
    "average_group_size": 2.3
  }
}
```

### Frontend Display

```
┌─────────────────────────────────────────────────────────────────────┐
│  🔗 RELATED IDENTITIES                          Analysis: 35 days   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Searching: John Doe (45 appearances analyzed)                      │
│  Co-appearance window: ±5 minutes                                   │
│                                                                     │
│  ═══════════════════════════════════════════════════════════════    │
│                                                                     │
│  🟢 STRONG RELATIONSHIP (>50% co-appearance)                        │
│  ───────────────────────────────────────────                        │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  👤 Sarah Johnson                                             │   │
│  │  ─────────────────────────────────────────────────────────   │   │
│  │  Co-appearances: 32 times (71% of John's visits)              │   │
│  │  Common places: Entrance, Cafeteria, Meeting Room 1           │   │
│  │  Common times: 9:00-9:30 AM, 12:00-1:00 PM                    │   │
│  │  First seen together: Dec 5, 2024                             │   │
│  │                                                               │   │
│  │  [View Profile] [View Timeline Together] [Create Alert]       │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  🟡 MODERATE RELATIONSHIP (25-50% co-appearance)                    │
│  ───────────────────────────────────────────────                    │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  👤 Unknown #abc123                                           │   │
│  │  ─────────────────────────────────────────────────────────   │   │
│  │  Co-appearances: 18 times (40%)                               │   │
│  │  Common places: Entrance, Lobby                               │   │
│  │                                                               │   │
│  │  [View] [Promote] [Investigate]                               │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  👤 Michael Chen                                              │   │
│  │  Co-appearances: 12 times (27%)                               │   │
│  │  [View Profile]                                               │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  🔵 WEAK RELATIONSHIP (<25% co-appearance)                          │
│  ─────────────────────────────────────────                          │
│  • Unknown #def456 - 8 times (18%)                                  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 6. Temporal Patterns

### What It Does
Analyzes when and where a person typically appears, revealing behavioral patterns.

### Pattern Analysis

```
┌─────────────────────────────────────────────────────────────────┐
│                    TEMPORAL PATTERN ANALYSIS                    │
│                        John Doe                                 │
└─────────────────────────────────────────────────────────────────┘

WEEKLY HEATMAP (Last 30 days)
═════════════════════════════

         Mon   Tue   Wed   Thu   Fri   Sat   Sun
 6:00    ░░    ░░    ░░    ░░    ░░    ░░    ░░
 7:00    ░░    ░░    ░░    ░░    ░░    ░░    ░░
 8:00    ▓▓    ▓▓    ▓▓    ▓▓    ▓▓    ░░    ░░    ← Arrives 8-9 AM
 9:00    ██    ██    ██    ██    ██    ░░    ░░       on weekdays
10:00    ▒▒    ▒▒    ▒▒    ▒▒    ▒▒    ░░    ░░
11:00    ░░    ░░    ░░    ░░    ░░    ░░    ░░
12:00    ██    ██    ██    ██    ██    ░░    ░░    ← Lunch at noon
13:00    ▓▓    ▓▓    ▓▓    ▓▓    ▓▓    ░░    ░░
14:00    ▒▒    ▒▒    ▒▒    ▒▒    ▒▒    ░░    ░░
15:00    ░░    ░░    ░░    ░░    ░░    ░░    ░░
16:00    ░░    ░░    ░░    ░░    ░░    ░░    ░░
17:00    ▓▓    ▓▓    ▓▓    ▓▓    ▓▓    ░░    ░░    ← Leaves 5-6 PM
18:00    ██    ██    ██    ██    ██    ░░    ░░
19:00    ░░    ░░    ░░    ░░    ░░    ░░    ░░

Legend: ░░ None  ▒▒ Low  ▓▓ Medium  ██ High


LOCATION DISTRIBUTION
═════════════════════

ENTRANCE-CAM     ████████████████████████████  45%
LOBBY-CAM        ██████████████                22%
CAFETERIA        █████████████                 20%
MEETING-ROOM-1   █████                          8%
PARKING-CAM      ███                            5%


BEHAVIORAL INSIGHTS
═══════════════════

📅 Typical Schedule:
   • Arrives: 8:30 AM - 9:15 AM (weekdays)
   • Lunch: 12:00 PM - 12:45 PM
   • Leaves: 5:30 PM - 6:15 PM
   • Weekend activity: None

📍 Primary Locations:
   • Most time: Entrance → Lobby → Cafeteria
   • Meeting attendance: Moderate (8% in meeting rooms)

🔄 Routine Stability: 87%
   • Very consistent schedule
   • Deviation days: Dec 24, Dec 31 (holidays)

⚠️ Anomalies Detected:
   • Jan 3: Arrived at 11:45 AM (unusual - typically 8:30 AM)
   • Dec 20: Detected at Parking at 10:30 PM (unusual hour)
```

### API Response

```json
{
  "identity_id": "john-doe-uuid",
  "display_name": "John Doe",
  "analysis_period": {
    "from": "2024-12-01",
    "to": "2025-01-05",
    "total_days": 35,
    "active_days": 24
  },
  
  "weekly_pattern": {
    "monday": {"peak_hours": ["08:30", "12:15", "17:45"], "total_detections": 42},
    "tuesday": {"peak_hours": ["08:45", "12:00", "18:00"], "total_detections": 38},
    "wednesday": {"peak_hours": ["09:00", "12:30", "17:30"], "total_detections": 45},
    "thursday": {"peak_hours": ["08:30", "12:00", "18:15"], "total_detections": 40},
    "friday": {"peak_hours": ["09:15", "12:45", "17:00"], "total_detections": 35},
    "saturday": {"peak_hours": [], "total_detections": 0},
    "sunday": {"peak_hours": [], "total_detections": 0}
  },
  
  "hourly_heatmap": [
    {"hour": 0, "mon": 0, "tue": 0, "wed": 0, "thu": 0, "fri": 0, "sat": 0, "sun": 0},
    {"hour": 8, "mon": 8, "tue": 7, "wed": 9, "thu": 8, "fri": 6, "sat": 0, "sun": 0},
    {"hour": 9, "mon": 12, "tue": 11, "wed": 13, "thu": 12, "fri": 10, "sat": 0, "sun": 0}
  ],
  
  "location_distribution": [
    {"pipeline_id": "ENTRANCE-CAM", "percentage": 45, "count": 90},
    {"pipeline_id": "LOBBY-CAM", "percentage": 22, "count": 44},
    {"pipeline_id": "CAFETERIA", "percentage": 20, "count": 40},
    {"pipeline_id": "MEETING-ROOM-1", "percentage": 8, "count": 16},
    {"pipeline_id": "PARKING-CAM", "percentage": 5, "count": 10}
  ],
  
  "behavioral_insights": {
    "typical_arrival": {"time": "08:45", "variance_minutes": 30},
    "typical_departure": {"time": "17:45", "variance_minutes": 45},
    "lunch_pattern": {"time": "12:15", "duration_minutes": 45},
    "routine_stability_score": 0.87,
    "is_weekend_active": false
  },
  
  "anomalies": [
    {
      "date": "2025-01-03",
      "type": "late_arrival",
      "expected": "08:45",
      "actual": "11:45",
      "deviation_hours": 3
    },
    {
      "date": "2024-12-20",
      "type": "unusual_hour",
      "time": "22:30",
      "location": "PARKING-CAM",
      "note": "Detection outside normal hours"
    }
  ]
}
```

---

## 7. Cross-Camera Tracking

### What It Does
Shows the movement path of a person across multiple cameras over time.

### Movement Path Visualization

```
┌─────────────────────────────────────────────────────────────────────┐
│  📍 CROSS-CAMERA TRACKING                     Jan 5, 2025          │
│     John Doe - Morning Arrival                                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  TIMELINE VIEW                                                      │
│  ─────────────                                                      │
│                                                                     │
│  08:32:15 ──┬── PARKING-CAM ─────────────────────────────────────   │
│             │   [📷 Snapshot]  Confidence: 94%                      │
│             │                                                       │
│  08:33:42 ──┼── ENTRANCE-CAM ────────────────────────────────────   │
│             │   [📷 Snapshot]  Confidence: 97%                      │
│             │   ↓ 1 min 27 sec                                      │
│             │                                                       │
│  08:34:18 ──┼── LOBBY-CAM ───────────────────────────────────────   │
│             │   [📷 Snapshot]  Confidence: 95%                      │
│             │   ↓ 36 sec                                            │
│             │                                                       │
│  08:35:05 ──┼── ELEVATOR-CAM (Floor 1) ──────────────────────────   │
│             │   [📷 Snapshot]  Confidence: 91%                      │
│             │   ↓ 47 sec                                            │
│             │                                                       │
│  08:37:22 ──┴── FLOOR-3-CAM ─────────────────────────────────────   │
│                 [📷 Snapshot]  Confidence: 89%                      │
│                 ⏱️ Total transit: 5 min 7 sec                        │
│                                                                     │
│  ═══════════════════════════════════════════════════════════════    │
│                                                                     │
│  MAP VIEW                                                           │
│  ────────                                                           │
│                                                                     │
│       FLOOR 3                                                       │
│    ┌─────────────────────────────┐                                  │
│    │           ⑤                 │   ⑤ Final: Floor 3 Corridor     │
│    │         08:37               │                                  │
│    └─────────────────────────────┘                                  │
│                 ↑                                                   │
│    ┌─────────────────────────────┐                                  │
│    │ ELEVATOR  ④                 │   ④ Elevator (08:35)            │
│    │         08:35               │                                  │
│    └─────────────────────────────┘                                  │
│                 ↑                                                   │
│    ┌─────────────────────────────┐                                  │
│    │ LOBBY     ③      →CAFE     │   ③ Lobby (08:34)               │
│    │         08:34               │                                  │
│    └─────────────────────────────┘                                  │
│                 ↑                                                   │
│    ┌─────────────────────────────┐                                  │
│    │ ENTRANCE  ②                 │   ② Entrance (08:33)            │
│    │         08:33               │                                  │
│    └─────────────────────────────┘                                  │
│                 ↑                                                   │
│    ┌─────────────────────────────┐                                  │
│    │ PARKING   ①                 │   ① Parking (08:32)             │
│    │         08:32               │                                  │
│    └─────────────────────────────┘                                  │
│                                                                     │
│  [Export Path] [Create Movement Report] [Play Animation]            │
└─────────────────────────────────────────────────────────────────────┘
```

### API Response

```json
{
  "identity_id": "john-doe-uuid",
  "display_name": "John Doe",
  "tracking_date": "2025-01-05",
  "tracking_period": {
    "from": "2025-01-05T08:00:00Z",
    "to": "2025-01-05T09:00:00Z"
  },
  
  "movement_path": [
    {
      "sequence": 1,
      "pipeline_id": "PARKING-CAM",
      "pipeline_name": "Parking Lot Camera",
      "timestamp": "2025-01-05T08:32:15Z",
      "confidence": 0.94,
      "snapshot_path": "/storage/PARKING-CAM/...",
      "coordinates": {"x": 120, "y": 450},  // If available
      "dwell_time_seconds": 87
    },
    {
      "sequence": 2,
      "pipeline_id": "ENTRANCE-CAM",
      "pipeline_name": "Main Entrance",
      "timestamp": "2025-01-05T08:33:42Z",
      "confidence": 0.97,
      "transit_from_previous_seconds": 87,
      "snapshot_path": "/storage/ENTRANCE-CAM/...",
      "dwell_time_seconds": 36
    },
    {
      "sequence": 3,
      "pipeline_id": "LOBBY-CAM",
      "pipeline_name": "Main Lobby",
      "timestamp": "2025-01-05T08:34:18Z",
      "confidence": 0.95,
      "transit_from_previous_seconds": 36,
      "dwell_time_seconds": 47
    },
    {
      "sequence": 4,
      "pipeline_id": "ELEVATOR-CAM",
      "pipeline_name": "Elevator Floor 1",
      "timestamp": "2025-01-05T08:35:05Z",
      "confidence": 0.91,
      "transit_from_previous_seconds": 47,
      "dwell_time_seconds": 137
    },
    {
      "sequence": 5,
      "pipeline_id": "FLOOR-3-CAM",
      "pipeline_name": "Floor 3 Corridor",
      "timestamp": "2025-01-05T08:37:22Z",
      "confidence": 0.89,
      "transit_from_previous_seconds": 137
    }
  ],
  
  "summary": {
    "total_cameras": 5,
    "total_duration_seconds": 307,
    "start_location": "PARKING-CAM",
    "end_location": "FLOOR-3-CAM",
    "average_confidence": 0.93,
    "gaps": []  // Time gaps where person wasn't detected
  }
}
```

---

## 8. Batch Search

### What It Does
Upload multiple images at once and search for all of them in a single operation.

### Example Use Case
**Investigation**: You have 20 photos from an incident and need to identify all individuals.

### API Request

```bash
POST /api/search/batch
Content-Type: multipart/form-data
Authorization: Bearer {token}

images[]: [file1.jpg]
images[]: [file2.jpg]
images[]: [file3.jpg]
...
scope: both
top_k: 5
include_quality: true
check_watchlist: true
```

### Response

```json
{
  "batch_id": "batch_abc123",
  "status": "completed",
  "total_images": 20,
  "processed": 20,
  "failed": 2,
  "processing_time_ms": 4250,
  
  "results": [
    {
      "image_index": 0,
      "filename": "suspect1.jpg",
      "faces_detected": 1,
      "quality_score": 0.88,
      "matches": [
        {
          "identity_id": "uuid-1",
          "display_name": "Unknown #xyz",
          "similarity": 0.82,
          "watchlist_match": null
        }
      ]
    },
    {
      "image_index": 1,
      "filename": "group_photo.jpg",
      "faces_detected": 4,
      "matches_per_face": [...]
    },
    {
      "image_index": 5,
      "filename": "blurry.jpg",
      "error": "No face detected - image too blurry",
      "quality_score": 0.23
    }
  ],
  
  "summary": {
    "total_faces_detected": 28,
    "unique_identities_found": 15,
    "known_matches": 8,
    "unknown_matches": 7,
    "watchlist_alerts": 1,
    "failed_images": ["blurry.jpg", "noface.jpg"]
  },
  
  "watchlist_alerts": [
    {
      "image_index": 12,
      "identity_name": "Michael Torres",
      "list_name": "THREAT",
      "similarity": 0.91
    }
  ]
}
```

---

## 9. Search History

### What It Does
Tracks all searches performed, allowing users to review, rerun, and audit search activity.

### Database Schema

```sql
CREATE TABLE search_history (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    search_type VARCHAR(50),        -- "single", "multi", "batch"
    
    -- Search parameters
    scope VARCHAR(20),              -- "known", "unknown", "both"
    top_k INTEGER,
    filters JSONB,                  -- date_from, date_to, pipeline_id, etc.
    
    -- Input
    input_image_hash VARCHAR(64),   -- SHA256 of uploaded image
    input_faces_count INTEGER,
    input_quality_scores FLOAT[],
    
    -- Results
    results_count INTEGER,
    results_summary JSONB,          -- Top matches with scores
    watchlist_alerts_count INTEGER,
    
    -- Metadata
    processing_time_ms INTEGER,
    ip_address INET,
    user_agent TEXT,
    
    created_at TIMESTAMP DEFAULT NOW()
);

-- Index for fast user queries
CREATE INDEX idx_search_history_user ON search_history(user_id, created_at DESC);
```

### Frontend: Search History Page

```
┌─────────────────────────────────────────────────────────────────────┐
│  📜 SEARCH HISTORY                               [Export] [Clear]   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Filter: [All Types ▼] [Last 7 days ▼] [All Users ▼]  [🔍 Search]   │
│                                                                     │
│  ═══════════════════════════════════════════════════════════════    │
│                                                                     │
│  📅 Today                                                           │
│  ─────────                                                          │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  🔍 Single Search                           Jan 5, 10:32 AM  │   │
│  │  ────────────────────────────────────────────────────────────│   │
│  │  User: admin@company.com                                     │   │
│  │  Scope: Both | Quality: 0.88 | Results: 5                    │   │
│  │  Top Match: John Doe (91%)                                   │   │
│  │  ⚠️ Watchlist Alert: 1                                        │   │
│  │                                                              │   │
│  │  [View Results] [Rerun Search] [Delete]                      │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  📦 Batch Search (20 images)                Jan 5, 09:15 AM  │   │
│  │  ────────────────────────────────────────────────────────────│   │
│  │  User: investigator@company.com                              │   │
│  │  Scope: Both | Faces Found: 28 | Unique IDs: 15              │   │
│  │  Processing: 4.2s                                            │   │
│  │                                                              │   │
│  │  [View Results] [Download Report] [Delete]                   │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  📅 Yesterday                                                       │
│  ─────────────                                                      │
│  • 3 searches by admin@company.com                                  │
│  • 1 search by security@company.com                                 │
│                                                                     │
│  [Show More...]                                                     │
│                                                                     │
│  ═══════════════════════════════════════════════════════════════    │
│  Total Searches: 47 | This Week: 23 | Watchlist Alerts: 3           │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 10. Export Results

### Export Formats

| Format | Use Case | Contents |
|--------|----------|----------|
| **CSV** | Spreadsheet analysis | Tabular data, IDs, scores |
| **PDF** | Formal reports, evidence | Formatted with images |
| **JSON** | API integration | Full structured data |
| **Excel** | Advanced analysis | Multiple sheets, charts |

### PDF Report Example

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│                    FACE RECOGNITION SEARCH REPORT                   │
│                    ═══════════════════════════════                  │
│                                                                     │
│  Report ID: RPT-2025-01-05-001                                      │
│  Generated: January 5, 2025 at 10:45 AM                             │
│  Generated By: admin@company.com                                    │
│                                                                     │
│  ───────────────────────────────────────────────────────────────    │
│                                                                     │
│  SEARCH PARAMETERS                                                  │
│  ─────────────────                                                  │
│  Search Type: Single Image                                          │
│  Scope: Both (Known + Unknown)                                      │
│  Date Filter: None                                                  │
│  Pipeline Filter: None                                              │
│  Results Requested: 10                                              │
│                                                                     │
│  INPUT IMAGE                           QUALITY ASSESSMENT           │
│  ┌─────────────┐                       Overall: 88% (Good)          │
│  │             │                       Blur: 92%                    │
│  │   [Image]   │                       Lighting: 85%                │
│  │             │                       Face Size: 90%               │
│  └─────────────┘                       Angle: 84%                   │
│                                                                     │
│  ───────────────────────────────────────────────────────────────    │
│                                                                     │
│  SEARCH RESULTS (5 matches found)                                   │
│  ────────────────────────────────                                   │
│                                                                     │
│  RANK 1 - HIGH CONFIDENCE (91%)                                     │
│  ┌─────────────┐   Name: John Doe                                   │
│  │             │   Type: KNOWN                                      │
│  │   [Image]   │   ID: 550e8400-e29b-41d4...                        │
│  │             │   Last Seen: Jan 5, 2025 09:17 AM                  │
│  └─────────────┘   Appearances: 45                                  │
│                    Watchlist: None                                  │
│                                                                     │
│  RANK 2 - MEDIUM CONFIDENCE (76%)                                   │
│  ┌─────────────┐   Name: Unknown #abc123                            │
│  │             │   Type: UNKNOWN                                    │
│  │   [Image]   │   ID: 660e8400-e29b-41d4...                        │
│  │             │   First Seen: Dec 10, 2024                         │
│  └─────────────┘   Appearances: 12                                  │
│                                                                     │
│  ... (additional results)                                           │
│                                                                     │
│  ───────────────────────────────────────────────────────────────    │
│                                                                     │
│  WATCHLIST ALERTS: None                                             │
│                                                                     │
│  ───────────────────────────────────────────────────────────────    │
│                                                                     │
│  AUDIT INFORMATION                                                  │
│  Searched By: admin@company.com                                     │
│  Search Time: January 5, 2025 10:32:15 AM                           │
│  Processing Duration: 342ms                                         │
│  IP Address: 192.168.1.100                                          │
│                                                                     │
│  ───────────────────────────────────────────────────────────────    │
│  Page 1 of 2                          Confidential - Internal Use   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 11. Confidence Bands

### What It Does
Groups search results into confidence categories for easier review prioritization.

### Bands Configuration

```python
CONFIDENCE_BANDS = {
    "VERY_HIGH": {
        "min": 0.90,
        "max": 1.00,
        "label": "Almost Certain Match",
        "color": "#22c55e",  # Green
        "icon": "✅",
        "action": "Can proceed with high confidence"
    },
    "HIGH": {
        "min": 0.75,
        "max": 0.89,
        "label": "Strong Match",
        "color": "#3b82f6",  # Blue
        "icon": "🔵",
        "action": "Recommended for verification"
    },
    "MEDIUM": {
        "min": 0.60,
        "max": 0.74,
        "label": "Possible Match",
        "color": "#f59e0b",  # Yellow
        "icon": "🟡",
        "action": "Requires manual review"
    },
    "LOW": {
        "min": 0.40,
        "max": 0.59,
        "label": "Weak Match",
        "color": "#ef4444",  # Red
        "icon": "🔴",
        "action": "Likely different person"
    }
}
```

### Frontend Grouped Display

```
┌─────────────────────────────────────────────────────────────────────┐
│  SEARCH RESULTS BY CONFIDENCE                                       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ✅ VERY HIGH (90-100%) - 1 match                                   │
│  ────────────────────────────────                                   │
│  Almost certain - can proceed with confidence                       │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ 👤 John Doe        91% ████████████████████░░  KNOWN         │   │
│  │    Last seen: 2 hours ago | 45 appearances                   │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  🔵 HIGH (75-89%) - 2 matches                                       │
│  ────────────────────────────                                       │
│  Strong matches - verification recommended                          │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ 👤 Sarah Johnson   82% ████████████████░░░░░░  KNOWN         │   │
│  └──────────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ 👤 Unknown #xyz    78% ███████████████░░░░░░░  UNKNOWN       │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  🟡 MEDIUM (60-74%) - 3 matches                                     │
│  ─────────────────────────────                                      │
│  Possible matches - manual review required                          │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ 👤 Michael Chen    68% ████████████░░░░░░░░░░  KNOWN         │   │
│  └──────────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ 👤 Unknown #abc    65% ███████████░░░░░░░░░░░  UNKNOWN       │   │
│  └──────────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ 👤 Unknown #def    62% ██████████░░░░░░░░░░░░  UNKNOWN       │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  🔴 LOW (40-59%) - 4 matches [Collapsed - Click to expand]          │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 12. Negative Search

### What It Does
Exclude specific identities from search results. Useful when you want to find "everyone except these people."

### Use Cases
- Find unknown faces, excluding all employees
- Search for visitors only (exclude staff)
- Find who else was present besides known attendees

### API Request

```bash
POST /api/search/by-image
Content-Type: multipart/form-data

image: [file]
scope: both
top_k: 20
exclude_identity_ids: ["uuid-1", "uuid-2", "uuid-3"]
exclude_watchlists: ["EMPLOYEE"]
```

### Example Scenario

**Task**: Find all non-employees in this CCTV frame

```
Input: CCTV frame with 6 faces
Exclusion: All identities on "EMPLOYEE" watchlist

Results:
- Face 1: John Smith (EMPLOYEE) → EXCLUDED
- Face 2: Unknown #abc → INCLUDED ✅
- Face 3: Sarah Johnson (EMPLOYEE) → EXCLUDED  
- Face 4: Unknown #xyz → INCLUDED ✅
- Face 5: Michael Chen (EMPLOYEE) → EXCLUDED
- Face 6: Visitor Ahmed (KNOWN, not employee) → INCLUDED ✅

Final Results: 3 faces (non-employees)
```

---

## Database Schema

### Complete Schema for Advanced Search

```sql
-- ═══════════════════════════════════════════════════════════════════
-- WATCHLISTS
-- ═══════════════════════════════════════════════════════════════════

CREATE TABLE watchlists (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    color VARCHAR(7) DEFAULT '#6366f1',
    icon VARCHAR(50) DEFAULT 'list',
    alert_level VARCHAR(20) DEFAULT 'info',  -- info, warning, critical
    
    -- Notification settings
    notify_dashboard BOOLEAN DEFAULT TRUE,
    notify_email BOOLEAN DEFAULT FALSE,
    notify_sms BOOLEAN DEFAULT FALSE,
    notify_webhook BOOLEAN DEFAULT FALSE,
    email_recipients TEXT[],
    sms_recipients TEXT[],
    webhook_url TEXT,
    
    is_active BOOLEAN DEFAULT TRUE,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE watchlist_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    watchlist_id UUID REFERENCES watchlists(id) ON DELETE CASCADE,
    identity_id UUID REFERENCES identities(id) ON DELETE CASCADE,
    priority VARCHAR(20) DEFAULT 'normal',
    notes TEXT,
    action_instructions TEXT,
    added_by UUID REFERENCES users(id),
    added_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP,
    is_active BOOLEAN DEFAULT TRUE,
    
    UNIQUE(watchlist_id, identity_id)
);

CREATE INDEX idx_watchlist_entries_identity ON watchlist_entries(identity_id);
CREATE INDEX idx_watchlist_entries_active ON watchlist_entries(is_active, expires_at);

-- ═══════════════════════════════════════════════════════════════════
-- LIVE SEARCH ALERTS
-- ═══════════════════════════════════════════════════════════════════

CREATE TABLE live_search_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    identity_id UUID REFERENCES identities(id) ON DELETE CASCADE,
    created_by UUID REFERENCES users(id),
    
    -- Trigger conditions
    min_similarity FLOAT DEFAULT 0.75,
    pipeline_ids TEXT[],  -- null = all pipelines
    time_window_enabled BOOLEAN DEFAULT FALSE,
    time_window_start TIME,
    time_window_end TIME,
    active_days INTEGER[],  -- 0=Sun, 1=Mon, etc.
    cooldown_minutes INTEGER DEFAULT 30,
    
    -- Notifications
    notify_dashboard BOOLEAN DEFAULT TRUE,
    notify_email BOOLEAN DEFAULT FALSE,
    notify_sms BOOLEAN DEFAULT FALSE,
    notify_webhook BOOLEAN DEFAULT FALSE,
    email_recipients TEXT[],
    sms_recipients TEXT[],
    webhook_url TEXT,
    sound_alert BOOLEAN DEFAULT TRUE,
    
    -- Auto actions
    auto_capture_snapshot BOOLEAN DEFAULT TRUE,
    auto_record_clip BOOLEAN DEFAULT FALSE,
    clip_duration_seconds INTEGER DEFAULT 60,
    
    -- Expiration
    expiration_type VARCHAR(20) DEFAULT 'never',  -- never, date, detections
    expiration_date TIMESTAMP,
    expiration_detections INTEGER,
    
    -- Status
    status VARCHAR(20) DEFAULT 'active',  -- active, paused, expired, triggered
    triggers_count INTEGER DEFAULT 0,
    last_triggered_at TIMESTAMP,
    
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE live_alert_triggers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id UUID REFERENCES live_search_alerts(id) ON DELETE CASCADE,
    detection_id UUID,
    pipeline_id VARCHAR(100),
    similarity_score FLOAT,
    snapshot_path TEXT,
    clip_path TEXT,
    acknowledged BOOLEAN DEFAULT FALSE,
    acknowledged_by UUID REFERENCES users(id),
    acknowledged_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_live_alerts_identity ON live_search_alerts(identity_id);
CREATE INDEX idx_live_alerts_status ON live_search_alerts(status);

-- ═══════════════════════════════════════════════════════════════════
-- SEARCH HISTORY
-- ═══════════════════════════════════════════════════════════════════

CREATE TABLE search_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    search_type VARCHAR(50) NOT NULL,  -- single, multi, batch
    
    -- Parameters
    scope VARCHAR(20),
    top_k INTEGER,
    filters JSONB,
    exclude_identities UUID[],
    exclude_watchlists UUID[],
    
    -- Input
    input_hash VARCHAR(64),
    input_faces_count INTEGER,
    input_quality_scores FLOAT[],
    
    -- Results
    results_count INTEGER,
    results_summary JSONB,
    watchlist_alerts_count INTEGER,
    unique_identities_count INTEGER,
    
    -- Metadata
    processing_time_ms INTEGER,
    ip_address INET,
    user_agent TEXT,
    
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_search_history_user ON search_history(user_id, created_at DESC);
CREATE INDEX idx_search_history_date ON search_history(created_at DESC);

-- ═══════════════════════════════════════════════════════════════════
-- SAVED SEARCHES
-- ═══════════════════════════════════════════════════════════════════

CREATE TABLE saved_searches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    name VARCHAR(200) NOT NULL,
    description TEXT,
    
    -- Search configuration
    search_type VARCHAR(50),
    scope VARCHAR(20),
    top_k INTEGER,
    min_quality FLOAT,
    filters JSONB,
    exclude_identities UUID[],
    exclude_watchlists UUID[],
    
    -- Optional: saved embedding for re-search
    embedding_hash VARCHAR(64),
    
    use_count INTEGER DEFAULT 0,
    last_used_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ═══════════════════════════════════════════════════════════════════
-- RELATED IDENTITIES CACHE
-- ═══════════════════════════════════════════════════════════════════

CREATE TABLE identity_relationships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id_1 UUID REFERENCES identities(id) ON DELETE CASCADE,
    identity_id_2 UUID REFERENCES identities(id) ON DELETE CASCADE,
    
    co_appearance_count INTEGER DEFAULT 0,
    co_appearance_percentage FLOAT,
    relationship_strength VARCHAR(20),  -- weak, moderate, strong
    common_pipelines TEXT[],
    common_time_patterns JSONB,
    
    first_co_appearance TIMESTAMP,
    last_co_appearance TIMESTAMP,
    
    calculated_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(identity_id_1, identity_id_2),
    CHECK(identity_id_1 < identity_id_2)  -- Ensure consistent ordering
);

CREATE INDEX idx_relationships_identity ON identity_relationships(identity_id_1);
```

---

## API Reference

> **All endpoints below are IMPLEMENTED and ready to use.**

### Search Endpoints

| Endpoint | Method | Description | Status |
|----------|--------|-------------|--------|
| `/api/search/by-image` | POST | Single image search (enhanced) | ✅ |
| `/api/search/advanced` | POST | Advanced multi-face detection search | ✅ |
| `/api/search/batch` | POST | Batch image search (multiple files) | ✅ |
| `/api/search/quality-check` | POST | Check image quality without searching | ✅ |
| `/api/search/config` | GET | Get search configuration | ✅ |
| `/api/search/history` | GET | Get search history | ✅ |
| `/api/search/history/export` | GET | Export search history | ✅ |
| `/api/search/export` | POST | Export search results (CSV/JSON/PDF) | ✅ |
| `/api/search/batch/export` | POST | Export batch results | ✅ |

### Watchlist Endpoints

| Endpoint | Method | Description | Status |
|----------|--------|-------------|--------|
| `/api/watchlists` | GET | List all watchlists | ✅ |
| `/api/watchlists` | POST | Create watchlist | ✅ |
| `/api/watchlists/{id}` | GET | Get specific watchlist | ✅ |
| `/api/watchlists/{id}` | PUT | Update watchlist | ✅ |
| `/api/watchlists/{id}` | DELETE | Delete/deactivate watchlist | ✅ |
| `/api/watchlists/{id}/stats` | GET | Get watchlist statistics | ✅ |
| `/api/watchlists/{id}/entries` | GET | List entries in watchlist | ✅ |
| `/api/watchlists/{id}/entries` | POST | Add identity to watchlist | ✅ |
| `/api/watchlists/{id}/entries/{identity_id}` | DELETE | Remove identity from list | ✅ |
| `/api/identities/{id}/watchlists` | GET | Get all watchlists for an identity | ✅ |
| `/api/watchlist-alerts` | GET | Get all watchlist alerts | ✅ |
| `/api/watchlist-alerts/{id}/acknowledge` | POST | Acknowledge alert | ✅ |

### Live Alert Endpoints

| Endpoint | Method | Description | Status |
|----------|--------|-------------|--------|
| `/api/live-alerts` | GET | List live alerts | ✅ |
| `/api/live-alerts` | POST | Create live alert | ✅ |
| `/api/live-alerts/{id}` | GET | Get specific alert | ✅ |
| `/api/live-alerts/{id}` | PUT | Update alert | ✅ |
| `/api/live-alerts/{id}` | DELETE | Delete alert | ✅ |
| `/api/live-alerts/{id}/pause` | POST | Pause alert | ✅ |
| `/api/live-alerts/{id}/resume` | POST | Resume alert | ✅ |
| `/api/live-alerts/{id}/triggers` | GET | Get trigger history | ✅ |
| `/api/live-alerts/triggers/{id}/acknowledge` | POST | Acknowledge trigger | ✅ |

### Intelligence Endpoints

| Endpoint | Method | Description | Status |
|----------|--------|-------------|--------|
| `/api/identities/{id}/related` | GET | Get related identities (co-appearance) | ✅ |
| `/api/identities/{id}/related/refresh` | POST | Refresh relationship cache | ✅ |
| `/api/identities/{id}/temporal-patterns` | GET | Get temporal patterns | ✅ |
| `/api/identities/{id}/cross-camera` | GET | Get cross-camera tracking | ✅ |
| `/api/identities/{id}/timeline` | GET | Get movement timeline | ✅ |
| `/api/identities/{id}/analyze` | GET | Complete intelligence analysis | ✅ |
| `/api/alerts/live/{id}` | DELETE | Delete alert |
| `/api/alerts/live/{id}/pause` | POST | Pause alert |
| `/api/alerts/live/{id}/resume` | POST | Resume alert |
| `/api/alerts/live/{id}/triggers` | GET | Get trigger history |

### Intelligence Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/intelligence/related/{identity_id}` | GET | Get related identities |
| `/api/intelligence/temporal/{identity_id}` | GET | Get temporal patterns |
| `/api/intelligence/tracking/{identity_id}` | GET | Get cross-camera tracking |

---

## Frontend Integration

### New Pages/Components Needed

```
frontend/
├── admin/
│   ├── search-advanced.html       # Advanced search page
│   ├── watchlists.html            # Watchlist management
│   ├── live-alerts.html           # Live alerts management
│   └── search-history.html        # Search history
├── js/
│   ├── admin-search-advanced.js   # Advanced search logic
│   ├── admin-watchlists.js        # Watchlist management
│   ├── admin-live-alerts.js       # Live alerts
│   ├── admin-search-history.js    # History management
│   └── components/
│       ├── quality-indicator.js   # Face quality display
│       ├── confidence-bands.js    # Confidence grouping
│       ├── movement-map.js        # Cross-camera tracking
│       └── temporal-heatmap.js    # Pattern visualization
└── css/
    ├── search-advanced.css
    ├── watchlists.css
    └── intelligence.css
```

---

## Implementation Priority

### Phase 1 (Core - 1-2 weeks)
1. ✅ Multi-face detection in search
2. ✅ Face quality scoring
3. ✅ Search history logging
4. ✅ Confidence bands display

### Phase 2 (Watchlists - 1 week)
5. ✅ Watchlist CRUD
6. ✅ Watchlist checking during search
7. ✅ Watchlist alerts UI

### Phase 3 (Intelligence - 1-2 weeks)
8. ✅ Live search alerts
9. ✅ Related identities
10. ✅ Temporal patterns
11. ✅ Cross-camera tracking

### Phase 4 (Operations - 1 week)
12. ✅ Batch search
13. ✅ Export (CSV, PDF, JSON)
14. ✅ Saved searches
15. ✅ Negative search

---

## Configuration (config.py additions)

```python
# =====================================================
# Advanced Search Configuration
# =====================================================

# Quality Scoring
SEARCH_MIN_QUALITY_THRESHOLD: float = Field(default=0.3, description="Minimum quality to attempt search")
SEARCH_QUALITY_WARNING_THRESHOLD: float = Field(default=0.6, description="Show warning below this quality")

# Confidence Bands
CONFIDENCE_VERY_HIGH_MIN: float = Field(default=0.90)
CONFIDENCE_HIGH_MIN: float = Field(default=0.75)
CONFIDENCE_MEDIUM_MIN: float = Field(default=0.60)
CONFIDENCE_LOW_MIN: float = Field(default=0.40)

# Live Alerts
LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES: int = Field(default=30)
LIVE_ALERT_MAX_PER_USER: int = Field(default=50)

# Search History
SEARCH_HISTORY_RETENTION_DAYS: int = Field(default=90)
SEARCH_HISTORY_MAX_PER_USER: int = Field(default=1000)   # enforced by the retention sweep

# Related Identities
RELATED_IDENTITY_TIME_WINDOW_MINUTES: int = Field(default=30)
RELATED_IDENTITY_MIN_CO_APPEARANCES: int = Field(default=3)

# Batch Search
BATCH_SEARCH_MAX_IMAGES: int = Field(default=20)
BATCH_SEARCH_TIMEOUT_SECONDS: int = Field(default=300)

# Export
EXPORT_PDF_MAX_RESULTS: int = Field(default=100)
EXPORT_INCLUDE_IMAGES: bool = Field(default=True)
```

---

**Last Updated:** January 2025  
**Version:** 5.1.0 (Advanced Search Intelligence)

---

## Implementation Status

### ✅ Implemented (Backend Ready)

| Feature | Status | API Endpoint |
|---------|--------|--------------|
| Multi-Face Detection | ✅ Complete | `POST /api/search/advanced` |
| Face Quality Scoring | ✅ Complete | `POST /api/search/quality-check` |
| Search Configuration | ✅ Complete | `GET /api/search/config` |
| Watchlist Management | ✅ Complete | `GET/POST /api/watchlists` |
| Watchlist Entries | ✅ Complete | `GET/POST /api/watchlists/{id}/entries` |
| Watchlist Alerts | ✅ Complete | `GET /api/watchlist-alerts` |
| Live Search Alerts | ✅ Complete | `GET/POST /api/live-alerts` |
| Alert Triggers | ✅ Complete | `GET /api/live-alerts/{id}/triggers` |
| Search History | ✅ Complete | Automatically logged |

### 🔧 Database Tables Added

```sql
-- New tables (auto-created on startup):
watchlists              -- VIP, Threat, POI lists
watchlist_entries       -- Identities on watchlists
watchlist_alerts        -- Alert history
live_search_alerts      -- Live notification alerts
live_alert_triggers     -- Trigger history
search_history          -- Search audit log
saved_searches          -- Saved search configs
identity_relationships  -- Co-appearance cache
```

### ⏳ Pending Implementation

| Feature | Status |
|---------|--------|
| Related Identities Analysis | Pending |
| Temporal Pattern Analysis | Pending |
| Cross-Camera Tracking | Pending |
| Batch Search | Pending |
| Export (CSV/PDF) | Pending |
| Frontend Pages | Pending |

### 🚀 Quick Test

```bash
# Test Advanced Search (upload image with multiple faces)
curl -X POST "http://localhost/api/search/advanced" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "image=@group_photo.jpg" \
  -F "scope=both" \
  -F "top_k=10" \
  -F "check_watchlist=true"

# Create a Watchlist
curl -X POST "http://localhost/api/watchlists" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "VIP", "alert_level": "info", "color": "#22c55e"}'

# Create Live Alert for an Identity
curl -X POST "http://localhost/api/live-alerts" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Track John", "identity_id": "UUID-HERE", "min_similarity": 0.75}'
```

