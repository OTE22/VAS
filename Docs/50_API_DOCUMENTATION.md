# Chapter 9: API Documentation
## Complete API Reference

**Version:** 6.0.0  
**Last Updated:** July 2026

---

## Overview

This chapter provides comprehensive API documentation for all endpoints, including authentication, request/response formats, and examples.

> **⚠️ Breaking-change notice (v6.0.0).** The platform hardening release changed
> several request/response contracts. If you have existing scripts, read
> **[Platform-Wide Conventions](#platform-wide-conventions)** and the
> **[Migration Checklist](#migration-checklist-v5--v6)** at the end of this
> chapter before upgrading.

---

## Base URL

```
http://localhost:8000
```

For production, replace with your production URL.

---

## Authentication

All API endpoints require authentication. See **Chapter 12.1** (`25_API_AUTHENTICATION_GUIDE.md`) for details.

**Quick Start**:
```bash
# Get token
curl -X POST "http://localhost:8000/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "password"}'

# Use token
curl -X GET "http://localhost:8000/api/identities" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## Platform-Wide Conventions

These rules apply to **every** endpoint in this chapter.

### CSRF protection (cookie clients only)

All state-changing requests (`POST`, `PUT`, `PATCH`, `DELETE`) require the
header `X-Requested-With: XMLHttpRequest` **when authenticated with the session
cookie**. Bearer-token clients are exempt — a token cannot be transmitted
cross-site by a hostile page.

```javascript
// Browser (cookie auth)
fetch('/api/watchlists', {
  method: 'POST',
  credentials: 'include',
  headers: { 'Content-Type': 'application/json',
             'X-Requested-With': 'XMLHttpRequest' },
  body: JSON.stringify({ name: 'VIP' })
});
```
```bash
# Script (bearer auth) — no extra header needed
curl -X POST "http://localhost:8000/api/watchlists" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"VIP"}'
```

**Failure**: `403 {"detail": "CSRF check failed: X-Requested-With header required"}`

### Error responses

Internal errors return a safe message and a reference ID; the detail is in the
server logs, never in the response:

```json
{"detail": "Internal error during model activation. Reference: ML-1154d5f4"}
```

Business errors return a machine-readable `error_code`. **Branch on the code,
never on message text**:

```json
{"detail": {"error_code": "VERSION_CONFLICT",
            "message": "This watchlist was modified by another administrator...",
            "current_version": 4}}
```

| Code | HTTP | Meaning |
|---|---|---|
| `DATASET_NOT_READY` | 400 | Training dataset fails readiness checks |
| `CONFIRMATION_REQUIRED` | 400 | Destructive action needs `confirm=true` |
| `TRAINING_ALREADY_RUNNING` | 409 | A training job is in progress |
| `JOB_ALREADY_RUNNING` | 409 | An equivalent background job is in progress |
| `NAME_CONFLICT` | 409 | Case-insensitive duplicate name |
| `VERSION_CONFLICT` | 409 | Optimistic-concurrency mismatch |
| `QUALITY_GATES_FAILED` | 409 | Model candidate failed its safety gates |
| `INVALID_STATUS` | 409 | Object is not in a valid state for this action |
| `ACCOUNT_BLOCKED` / `QUERY_DENIED` | 403 | SQL Agent policy decisions |

**Unknown or malformed IDs return `404`, never `500`** — and identically for
both, so IDs cannot be probed by guessing.

### Background jobs

Expensive operations return **`202 Accepted`** immediately and run in the
background. They appear in **Admin → Background Tasks** and survive browser
closure.

```
POST <schedule endpoint>  -> 202 {"accepted": true, "job_id": "...", "status": "scheduled"}
GET  <poll endpoint>/{id} -> {"status": "running|completed|failed", "progress_percent": 55, "result": {...}}
POST <schedule endpoint>  -> 409 {"error_code": "...ALREADY_RUNNING", "job_id": "<running>"}
```

| Operation | Schedule | Poll |
|---|---|---|
| Relationship calculation | `POST /api/intelligence/relationships/calculate-all` | `GET /api/intelligence/relationships/jobs/{job_id}` |
| Threshold learning | `POST /api/intelligence/thresholds/jobs` | `GET /api/intelligence/thresholds/jobs/{job_id}` |
| Model training | `POST /api/admin/merge-suggestions/training-jobs` | `GET /api/admin/merge-suggestions/training-jobs/{job_id}` |
| Alert channel test | `POST /api/live-alerts/{alert_id}/test` | `GET /api/live-alerts/test-jobs/{job_id}` |

### Pagination

List endpoints support a paginated envelope. Passing `page` switches modes
(omitting it preserves the legacy array/shape for existing consumers):

```json
{"items": [...], "total": 25000, "page": 1, "page_size": 25, "total_pages": 1000}
```

Applies to `/api/admin/identities`, `/api/watchlists`,
`/api/watchlists/{id}/entries`, and the SQL Agent history. `page_size` is
capped server-side (100 for most endpoints).

### Timestamps

All timestamps are timezone-aware ISO 8601 (`2026-07-25T20:30:00Z`). Clients
must treat invalid or missing values as unknown — never substitute the current
time.

### Cache headers

Operational/status endpoints return `Cache-Control: no-store`. Personalized
sensitive content (generated maps) returns `Cache-Control: private, no-store`
plus a sandboxing CSP.

---

## Intelligence API

### 1. Get Related Identities

**Endpoint**: `GET /api/identities/{identity_id}/related`

**Description**: Find people who frequently appear together with this identity.

**Path Parameters**:
- `identity_id` (UUID, required): Identity UUID

**Query Parameters**:
- `min_co_appearances` (integer, optional): Minimum co-appearances (default from config)
- `time_window_minutes` (integer, optional, 1-60): Time window for co-appearance (default from config)
- `limit` (integer, optional, default: 20, max: 100): Maximum results

**Response**: Envelope with `items` **and the authoritative threshold policy**
(so clients cannot display strength rules that disagree with the backend).

> **⚠️ Changed in v6.0.0**: this endpoint returned a bare array. It now returns
> `{"items": [...], "thresholds": {...}}`. Read `body.items`.

**Status Codes**:
- `200 OK`: Success
- `401 Unauthorized`: Authentication required
- `404 Not Found`: Identity does not exist (or the ID is malformed)
- `500 Internal Server Error`: Server error (with a `Reference:` ID)

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/identities/123e4567-e89b-12d3-a456-426614174000/related?limit=20" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response**:
```json
{
  "items": [
    {
      "identity_id": "456e7890-e89b-12d3-a456-426614174001",
      "display_name": "Jane Doe",
      "identity_type": "KNOWN",
      "co_appearance_count": 25,
      "co_appearance_percentage": 45.5,
      "relationship_strength": "strong",
      "common_pipelines": ["camera_1", "camera_2"],
      "first_co_appearance": "2024-01-01T10:00:00Z",
      "last_co_appearance": "2024-01-15T18:00:00Z",
      "best_snapshot_path": "/storage/snapshots/...",
      "snapshot_url": "/storage/snapshots/..."
    }
  ],
  "thresholds": {
    "strong":   {"min_percentage": 50, "min_co_appearances": 20},
    "moderate": {"min_percentage": 25, "min_co_appearances": 10},
    "weak":     {"min_percentage": 0,  "min_co_appearances": 0}
  }
}
```

### 2. Refresh Related Identities

**Endpoint**: `POST /api/identities/{identity_id}/related/refresh`

**Description**: Recalculate and cache relationship data for this identity.

**Path Parameters**:
- `identity_id` (UUID, required): Identity UUID

**Response**: Success message

### 2.5. Calculate All Relationships (Background Task)

**Endpoint**: `POST /api/intelligence/relationships/calculate-all`

**Description**: Schedule a background job that calculates and caches relationships for all active identities. This populates the `identity_relationships` cache table to improve performance of social network analysis.

**Requires**: admin role + CSRF header (cookie clients).

> **⚠️ Changed in v6.0.0**: returns **202** with a `job_id` (was 200), enforces
> single-flight (**409** while a run is active), and reports live progress in
> Background Tasks.

**Status Codes**:
- `202 Accepted`: Job scheduled
- `409 Conflict`: A calculation is already running (`job_id` of the running job returned)
- `403 Forbidden`: Missing CSRF header (cookie auth)
- `401 Unauthorized`: Authentication required

**Example Request**:
```bash
curl -X POST "http://localhost:8000/api/intelligence/relationships/calculate-all" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response (202)**:
```json
{
  "accepted": true,
  "job_id": "relationships-f67a03e6",
  "task_id": 42,
  "status": "scheduled",
  "message": "Relationship calculation scheduled. Monitor progress in Background Tasks.",
  "task_type": "relationship_calculation"
}
```

**Example Response (409)**:
```json
{"detail": {"error_code": "JOB_ALREADY_RUNNING",
            "message": "A relationship calculation is already running.",
            "job_id": "relationships-f67a03e6"}}
```

### 2.6. Poll Relationship Job

**Endpoint**: `GET /api/intelligence/relationships/jobs/{job_id}`

**Response**: Background-task record — `status`, `progress_percent`, and on
completion a `result` with `identities_processed`, `relationships_cached`,
`success_rate` and `duration_minutes`.

```json
{"job_id": "relationships-f67a03e6", "status": "completed", "progress_percent": 100,
 "success": true,
 "result": {"identities_processed": 19, "relationships_cached": 21,
            "success_rate": "100.0%", "duration_minutes": "0.0"}}
```

**Example Request**:
```bash
curl -X POST "http://localhost:8000/api/identities/123e4567-e89b-12d3-a456-426614174000/related/refresh" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### 3. Get Temporal Patterns

**Endpoint**: `GET /api/identities/{identity_id}/temporal-patterns`

**Description**: Analyze when this identity typically appears.

**Path Parameters**:
- `identity_id` (UUID, required): Identity UUID

**Query Parameters**:
- `days_back` (integer, optional, default: 90, max: 365): Days to analyze

**Response**: TemporalPatternResponse

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/identities/123e4567-e89b-12d3-a456-426614174000/temporal-patterns?days_back=90" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response**:
```json
{
  "identity_id": "123e4567-e89b-12d3-a456-426614174000",
  "hourly_distribution": {
    "0": 5, "1": 2, "2": 1, ... "23": 8
  },
  "daily_distribution": {
    "Monday": 45, "Tuesday": 52, ... "Sunday": 38
  },
  "peak_hours": [8, 9, 17, 18],
  "peak_days": ["Monday", "Tuesday", "Wednesday"],
  "most_common_pipelines": [
    {"pipeline_id": "camera_1", "count": 120, "name": "Main Entrance"}
  ],
  "total_appearances": 350,
  "first_appearance": "2024-01-01T08:00:00",
  "last_appearance": "2024-01-15T18:00:00",
  "average_appearances_per_day": 3.89
}
```

### 4. Get Cross-Camera Tracking

**Endpoint**: `GET /api/identities/{identity_id}/cross-camera`

**Description**: Track this identity's movement across cameras over time.

**Path Parameters**:
- `identity_id` (UUID, required): Identity UUID

**Query Parameters**:
- `date` (string, optional): Specific date (YYYY-MM-DD)
- `days_back` (integer, optional, default: 7, max: 30): Days to analyze if no date

**Response**: Array of CrossCameraTrackResponse

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/identities/123e4567-e89b-12d3-a456-426614174000/cross-camera?days_back=7" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response**:
```json
[
  {
    "identity_id": "123e4567-e89b-12d3-a456-426614174000",
    "display_name": "John Doe",
    "date": "2024-01-15",
    "movements": [
      {
        "pipeline_id": "camera_1",
        "pipeline_name": "Main Entrance",
        "timestamp": "2024-01-15T10:30:00",
        "snapshot_path": "/storage/snapshots/...",
        "snapshot_url": "http://localhost:8000/storage/snapshots/...",
        "duration_at_location": 120,
        "coordinates": {
          "lat": 37.7749,
          "lng": -122.4194
        }
      }
    ],
    "total_cameras": 5,
    "first_seen": "2024-01-15T10:00:00",
    "last_seen": "2024-01-15T18:00:00",
    "total_duration_minutes": 480
  }
]
```

### 5. Get Movement Timeline

**Endpoint**: `GET /api/identities/{identity_id}/timeline`

**Description**: Get a simplified recent movement timeline for dashboard display.

**Path Parameters**:
- `identity_id` (UUID, required): Identity UUID

**Query Parameters**:
- `hours_back` (integer, optional, default: 24, max: 168): Hours to look back

**Response**: MovementTimelineResponse

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/identities/123e4567-e89b-12d3-a456-426614174000/timeline?hours_back=24" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

### 6. Complete Identity Analysis

**Endpoint**: `GET /api/identities/{identity_id}/analyze`

**Description**: Perform comprehensive intelligence analysis on an identity.

**Path Parameters**:
- `identity_id` (UUID, required): Identity UUID

**Response**: Complete analysis object **with per-section statuses**.

> **⚠️ Changed in v6.0.0**: each section is now computed independently and
> reports its own status, so one failing analysis no longer hides the others —
> and the API never claims tracking data exists when it does not.

**Section statuses**: `ready` · `partial` · `unavailable` · `error`  
**Reason codes**: `NO_MOVEMENT_DATA` · `NO_COORDINATES` · `ANALYSIS_FAILED`

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/identities/123e4567-e89b-12d3-a456-426614174000/analyze" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response**:
```json
{
  "identity_id": "123e4567-...",
  "analyzed_at": "2026-07-26T12:00:00Z",
  "related_identities": [...],
  "temporal_patterns": {...},
  "cross_camera_tracks": [...],
  "sections": {
    "related":  {"status": "ready", "count": 5},
    "temporal": {"status": "ready", "total_appearances": 120, "peak_hours": [8, 17]},
    "tracking": {"status": "partial", "reason_code": "NO_COORDINATES",
                 "movement_count": 34, "days_with_activity": 6}
  }
}
```

---

## Map Service API

### 1. Generate Interactive Map

**Endpoint**: `GET /api/identities/{identity_id}/map`

**Description**: Generate an interactive HTML map showing identity movement with security intelligence features.

**Path Parameters**:
- `identity_id` (UUID, required): Identity UUID

**Query Parameters**:
- `date` (string, optional): Specific date (YYYY-MM-DD)
- `days_back` (integer, optional, default: 7): Days to analyze if no date (1-30)
- `map_style` (string, optional, default: `"light"`): Map style — **allowlisted**: `dark`, `light`, `satellite`, `terrain`. Anything else returns `400` and the rejected value is never echoed back.
- `include_popups` (boolean, optional, default: true): Include popup information on markers
- `show_routes` (boolean, optional, default: true): Draw routes between locations
- `cluster_markers` (boolean, optional, default: true): Cluster nearby markers
- `enable_security_features` (boolean, optional, **default: false**): Enable security intelligence features
- `detect_patterns` (boolean, optional, **default: false**): Detect suspicious movement patterns
- `show_risk_heatmap` (boolean, optional, **default: false**): Show risk heatmap overlay
- `show_timeline` (boolean, optional, default: false): Show timeline playback control
- `show_animated_avatar` (boolean, optional, default: false): Animate an avatar along the route

> **⚠️ Changed in v6.0.0**: the expensive/security-sensitive overlays
> (`enable_security_features`, `detect_patterns`, `show_risk_heatmap`) now
> default to **false** and must be requested explicitly. Previously a missing
> parameter silently enabled them.

**Response**: HTML page with embedded interactive map.

**Response headers** (v6.0.0):
```
Cache-Control: private, no-store
Content-Security-Policy: sandbox allow-scripts
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
```

**🔒 Embedding requirement**: this HTML contains scripts. If you embed it, you
**must** use a sandboxed iframe **without** `allow-same-origin`, otherwise the
map's scripts gain access to your session:

```javascript
const iframe = document.createElement('iframe');
iframe.setAttribute('sandbox', 'allow-scripts');   // NEVER add allow-same-origin
iframe.setAttribute('referrerpolicy', 'no-referrer');
iframe.srcdoc = mapHtml;
```

Prefer `GET /api/identities/{id}/map/geojson` and render with your own map
library when you control the frontend.

**Status Codes**:
- `200 OK`: Map generated successfully (or a safe "no tracking data" page)
- `400 Bad Request`: Invalid parameters (e.g. unsupported `map_style`)
- `401 Unauthorized`: Authentication required
- `404 Not Found`: Identity not found or malformed ID
- `500/503`: Safe error page with a reference ID — never a stack trace, internal path or dependency-install instruction

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/identities/123e4567-e89b-12d3-a456-426614174000/map?days_back=7&map_style=dark&enable_security_features=true" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response**:
```html
<!DOCTYPE html>
<html>
<head>
    <meta http-equiv="content-type" content="text/html; charset=UTF-8" />
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <!-- Map HTML and JavaScript -->
</head>
<body>
    <div id="map" style="width:100%; height:600px;"></div>
    <script>
        // Folium-generated map code
    </script>
</body>
</html>
```

### 2. Get GeoJSON Data

**Endpoint**: `GET /api/identities/{identity_id}/map/geojson`

**Description**: Get tracking data in GeoJSON format for custom frontend rendering.

**Path Parameters**:
- `identity_id` (UUID, required): Identity UUID

**Query Parameters**:
- `date` (string, optional): Specific date (YYYY-MM-DD)
- `days_back` (integer, optional, default: 7): Days to analyze (1-30)

**Response**: GeoJSON FeatureCollection

**Status Codes**:
- `200 OK`: GeoJSON generated successfully
- `400 Bad Request`: Invalid parameters
- `401 Unauthorized`: Authentication required
- `404 Not Found`: Identity not found

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/identities/123e4567-e89b-12d3-a456-426614174000/map/geojson?days_back=7" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response**:
```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {
        "type": "Point",
        "coordinates": [-122.4194, 37.7749]
      },
      "properties": {
        "date": "2024-01-15",
        "pipeline_name": "Main Entrance",
        "timestamp": "2024-01-15T10:30:00",
        "duration_at_location": 120,
        "sequence": 1,
        "is_start": true,
        "is_end": false
      }
    },
    {
      "type": "Feature",
      "geometry": {
        "type": "LineString",
        "coordinates": [[-122.4194, 37.7749], [-122.4094, 37.7849]]
      },
      "properties": {
        "type": "route",
        "date": "2024-01-15",
        "movement_count": 5
      }
    }
  ]
}
```

### 3. Get Map Service Statistics

**Endpoint**: `GET /api/map/stats`

**Description**: Get map service statistics for monitoring.

**Response**: Statistics object

**Status Codes**:
- `200 OK`: Statistics retrieved successfully
- `401 Unauthorized`: Authentication required

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/map/stats" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response**:
```json
{
  "maps_generated": 1234,
  "cache_hits": 890,
  "cache_misses": 344,
  "errors": 5,
  "timeouts": 0,
  "folium_available": true,
  "cache_available": true,
  "cache_enabled": true
}
```

---

## Security Intelligence API

### 1. Social Network Analysis

**Endpoint**: `GET /api/security/network`

**Description**: Build a social network graph showing connections between identities. **The response is always bounded** — the full graph is never returned.

**Query Parameters**:
- `identity_ids` (string, optional, max 50): Comma-separated identity IDs. One ID = `ego` scope, several = `selected` scope, **omitted = `top_risk` scope** (highest-risk slice, not the whole graph)
- `min_connections` (integer, optional, default: 1): Minimum connections to include
- `days_back` (integer, optional, default: 90, max: 365): Days to analyze
- `max_nodes` (integer, optional, default: 100, **max: 300**): Node ceiling; `>300` returns `422`

> **⚠️ Changed in v6.0.0**: omitting `identity_ids` used to build and return the
> **entire** graph, which was unbounded and expensive. It now returns the
> top-risk slice with explicit truncation metadata. Edge ceiling: 1000.

**Response**: Network graph with nodes, edges, clusters, central nodes **and truncation metadata**:

| Field | Meaning |
|---|---|
| `scope` | `ego` \| `selected` \| `top_risk` |
| `truncated` | `true` when nodes or edges were dropped |
| `total_nodes` | How many nodes matched before the cap |
| `returned_nodes` | How many are in this response |
| `max_nodes` | The cap that was applied |

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/security/network?days_back=90&min_connections=2" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response**:
```json
{
  "nodes": [
    {
      "identity_id": "123e4567-e89b-12d3-a456-426614174000",
      "display_name": "John Doe",
      "identity_type": "KNOWN",
      "appearances_count": 150,
      "risk_score": 25,
      "connections_count": 5,
      "snapshot_url": "http://localhost:8000/storage/snapshots/..."
    }
  ],
  "edges": [
    {
      "source_id": "123e4567-e89b-12d3-a456-426614174000",
      "target_id": "456e7890-e89b-12d3-a456-426614174001",
      "strength": 0.75,
      "co_appearances": 25,
      "co_appearance_percentage": 45.5,
      "first_seen_together": "2024-01-01T10:00:00",
      "last_seen_together": "2024-01-15T18:00:00",
      "common_locations": ["camera_1", "camera_2"],
      "relationship_type": "strong"
    }
  ],
  "clusters": [
    {
      "cluster_id": 1,
      "identity_ids": ["123e4567-...", "456e7890-..."],
      "size": 5
    }
  ],
  "central_nodes": ["123e4567-e89b-12d3-a456-426614174000"],
  "isolated_nodes": ["789e0123-e89b-12d3-a456-426614174002"],
  "scope": "top_risk",
  "truncated": true,
  "total_nodes": 12400,
  "returned_nodes": 100,
  "max_nodes": 100
}
```

### 1.5. Feature Capabilities

**Endpoint**: `GET /api/security/capabilities`

**Description**: Report what the backend can **actually** do right now — real
dependency availability, running jobs, and algorithm/model versions. Replaces
hard-coded "everything is enabled" status displays.

**Response headers**: `Cache-Control: no-store`

**Example Response**:
```json
{
  "capabilities": {
    "network_analysis":      {"enabled": true, "status": "ready"},
    "pattern_detection":     {"enabled": true, "status": "ready"},
    "anomaly_detection":     {"enabled": true, "status": "ready"},
    "threat_assessment":     {"enabled": true, "status": "ready"},
    "threshold_learning":    {"enabled": true, "status": "job_running",
                              "job_id": "threshold-4f2a91bc",
                              "algorithm_version": "threshold-v1"},
    "trajectory_prediction": {"enabled": true, "status": "ready",
                              "model_version": "trajectory-v1"},
    "activity_correlation":  {"enabled": true, "status": "ready",
                              "algorithm_version": "xcca-v1"},
    "map_generation":        {"enabled": true, "status": "ready"},
    "offline_maps":          {"enabled": false, "status": "disabled"}
  },
  "checked_at": "2026-07-26T12:00:00Z"
}
```

**Status values**: `ready` · `disabled` · `job_running` · `dependency_unavailable` · `model_not_trained`

### 2. Learn Optimal Thresholds (Advanced)

**Endpoint**: `POST /api/intelligence/thresholds/jobs` *(schedule)*  
**Endpoint**: `GET /api/intelligence/thresholds/jobs/{job_id}` *(poll)*

**Description**: Learn optimal distance and time thresholds for all camera pairs based on historical data. Runs as a **background job**.

**Requires**: admin role + CSRF header (cookie clients).

> **⚠️ Changed in v6.0.0**: threshold learning no longer blocks the HTTP
> request. The old synchronous `POST /api/intelligence/thresholds/learn` still
> exists for backward compatibility (now CSRF-protected) but is **deprecated**.

**Query Parameters**:
- `pipeline_ids` (string, optional, max 100): Comma-separated pipeline IDs (empty = all active pipelines)

**Status Codes**:
- `202 Accepted`: Job scheduled
- `409 Conflict`: A threshold job is already running (returns its `job_id`)
- `403 Forbidden`: Missing CSRF header (cookie auth)

**Example Request**:
```bash
curl -X POST "http://localhost:8000/api/intelligence/thresholds/jobs?pipeline_ids=camera_1,camera_2" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response (202)**:
```json
{"accepted": true, "job_id": "threshold-4f2a91bc", "task_id": 51, "status": "scheduled",
 "task_type": "threshold_learning"}
```

**Example Poll Result (completed)**:
```json
{
  "job_id": "threshold-4f2a91bc",
  "status": "completed",
  "progress_percent": 100,
  "result": {
    "learned_pairs": 3,
    "algorithm_version": "threshold-v1",
    "calculated_at": "2026-07-26T12:00:00Z",
    "pipelines_scoped": 8,
    "thresholds": [
      {
        "camera_1": "camera_1",
        "camera_2": "camera_2",
        "optimal_time_window_minutes": 5.2,
        "optimal_distance_meters": 240.0,
        "actual_distance_meters": 200.0,
        "confidence": 0.85,
        "sample_count": 42
      }
    ]
  }
}
```

> Always read `confidence` and `sample_count` before trusting a learned
> threshold — a value derived from 3 movements is not evidence.

### 3. Predict Next Camera (Advanced)

**Endpoint**: `GET /api/intelligence/trajectory/predict`

**Description**: Predict where a person will appear next based on historical movement patterns.

**Query Parameters**:
- `identity_id` (UUID, required): Identity UUID to predict for
- `current_camera` (string, required): Current camera/pipeline ID where identity is located
- `top_k` (integer, optional, default: 3, min: 1, max: 10): Number of top predictions to return

**Response**: List of predicted cameras with probabilities and estimated times

**Status Codes**:
- `200 OK`: Trajectory predicted successfully
- `400 Bad Request`: Invalid parameters
- `500 Internal Server Error`: Server error

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/intelligence/trajectory/predict?identity_id=uuid&current_camera=camera_1&top_k=3" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response**:
```json
{
  "identity_id": "123e4567-e89b-12d3-a456-426614174000",
  "current_camera": "camera_1",
  "predictions": [
    {
      "camera_id": "camera_3",
      "probability": 0.75,
      "estimated_time": "2026-01-11T15:30:00Z",
      "confidence": "high"
    },
    {
      "camera_id": "camera_2",
      "probability": 0.20,
      "estimated_time": "2026-01-11T15:32:00Z",
      "confidence": "low"
    }
  ],
  "model_version": "trajectory-v1",
  "insufficient_evidence": false,
  "note": "Estimated times are statistical projections from historical movement, not certainties."
}
```

> **⚠️ Changed in v6.0.0**: responses now carry `model_version`, a per-prediction
> `confidence` tier (`high` ≥0.6, `moderate` ≥0.3, else `low`), and
> `insufficient_evidence`. A `404` is returned for unknown identities.
> **Do not present `estimated_time` as a certainty** — it is a projection.

### 4. Calculate Activity Correlation (Advanced)

**Endpoint**: `GET /api/intelligence/correlation/calculate`

**Description**: Measure the **temporal and spatial association** between two identities' activities using Cross-Camera Correlation Analysis (xCCA).

> **⚠️ Correlation does not prove causation.** Earlier documentation described
> this endpoint as detecting "causal relationships" — that was incorrect. A high
> score means the two identities' movements are associated, not that one caused
> the other or that they know each other.

**Query Parameters**:
- `identity_a` (UUID, required): First identity UUID
- `identity_b` (UUID, required): Second identity UUID
- `days_back` (integer, optional, default: 90, min: 1, max: 365): Days to analyze

**Response**: Association score, strength, sequences, and evidence metadata

**Status Codes**:
- `200 OK`: Calculated successfully
- `404 Not Found`: Either identity does not exist (or the ID is malformed)
- `400 Bad Request`: Invalid parameters

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/intelligence/correlation/calculate?identity_a=uuid1&identity_b=uuid2&days_back=90" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response**:
```json
{
  "identity_a": "123e4567-e89b-12d3-a456-426614174000",
  "identity_b": "456e7890-e89b-12d3-a456-426614174001",
  "correlation_score": 0.75,
  "correlation_strength": "strong",
  "sequence_count": 15,
  "days_back": 90,
  "insufficient_evidence": false,
  "algorithm_version": "xcca-v1",
  "sequences": [
    {
      "from_camera": "camera_1",
      "to_camera": "camera_2",
      "time_diff_minutes": 3.5,
      "from_time": "2026-01-10T10:00:00Z",
      "to_time": "2026-01-10T10:03:30Z"
    }
  ],
  "note": "Measures temporal and spatial association between two identities. Correlation does not prove causation."
}
```

`insufficient_evidence: true` (fewer than 3 sequences) means the score is not
statistically meaningful and must not be relied upon.

### 5. Detect Suspicious Patterns

**Endpoint**: `GET /api/security/patterns`

**Description**: Detect suspicious behavioral patterns across all identities.

**Query Parameters**:
- `days_back` (integer, optional, default: 30, max: 90): Days to analyze
- `min_group_size` (integer, optional, default: 3, min: 2, max: 20): Minimum group size for detection

**Response**: Array of detected patterns

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/security/patterns?days_back=30&min_group_size=3" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response**:
```json
[
  {
    "pattern_type": "group_activity",
    "description": "Multiple identities appearing together repeatedly",
    "identities_involved": [
      "123e4567-e89b-12d3-a456-426614174000",
      "456e7890-e89b-12d3-a456-426614174001"
    ],
    "severity": 8,
    "confidence": 0.85,
    "first_detected": "2024-01-10T10:00:00",
    "evidence": {
      "co_appearances": 15,
      "locations": ["camera_1", "camera_2"]
    },
    "locations": ["camera_1", "camera_2"],
    "time_range": [
      "2024-01-10T10:00:00",
      "2024-01-15T18:00:00"
    ]
  }
]
```

### 3. Detect Behavioral Anomalies

**Endpoint**: `GET /api/security/anomalies/{identity_id}`

**Description**: Detect behavioral anomalies for a specific identity.

**Path Parameters**:
- `identity_id` (UUID, required): Identity UUID

**Query Parameters**:
- `days_back` (integer, optional, default: 90, max: 365): Days to analyze

**Response**: Array of detected anomalies

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/security/anomalies/123e4567-e89b-12d3-a456-426614174000?days_back=90" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response**:
```json
[
  {
    "identity_id": "123e4567-e89b-12d3-a456-426614174000",
    "anomaly_type": "off_schedule",
    "description": "Activity detected outside normal schedule",
    "severity": 6,
    "detected_at": "2024-01-15T02:00:00",
    "baseline": {
      "normal_hours": [8, 9, 17, 18],
      "normal_days": ["Monday", "Tuesday", "Wednesday"]
    },
    "deviation": {
      "detected_hour": 2,
      "detected_day": "Sunday"
    },
    "risk_score": 35
  }
]
```

### 4. Threat Assessment

**Endpoint**: `GET /api/security/threat/{identity_id}`

**Description**: Perform comprehensive threat assessment for an identity.

**Path Parameters**:
- `identity_id` (UUID, required): Identity UUID

**Response**: Threat assessment object

**Example Request**:
```bash
curl -X GET "http://localhost:8000/api/security/threat/123e4567-e89b-12d3-a456-426614174000" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response**:
```json
{
  "identity_id": "123e4567-e89b-12d3-a456-426614174000",
  "display_name": "John Doe",
  "overall_risk_score": 65,
  "threat_level": "high",
  "risk_factors": [
    {
      "factor": "identity_type",
      "score": 20,
      "description": "Unknown identity type"
    },
    {
      "factor": "connection_count",
      "score": 15,
      "description": "High number of connections (hub)"
    },
    {
      "factor": "behavioral_anomalies",
      "score": 20,
      "description": "Multiple behavioral anomalies detected"
    },
    {
      "factor": "suspicious_patterns",
      "score": 10,
      "description": "Involved in suspicious patterns"
    }
  ],
  "recommendations": [
    "Monitor closely",
    "Review recent activity",
    "Consider watchlist addition"
  ]
}
```

---

## Identity Search API

### Search / List Identities

**Endpoint**: `GET /api/admin/identities`

**Description**: Server-side identity search with pagination. Use this instead
of downloading the full identity list into a client.

> **⚠️ Changed in v6.0.0**: added a paginated mode and per-identity pipeline IDs
> are now fetched in one batched query (previously one query per identity).
> Omitting `page` preserves the legacy `{identities: [...]}` shape.

**Query Parameters**:
- `page` (integer, optional): **Presence switches to the paginated envelope**
- `page_size` (integer, optional, default: 25, **max: 100**)
- `q` (string, optional, max 200): Search by display name or ID prefix (LIKE wildcards are escaped)
- `type` (string, optional): `known` · `unknown` · `both`
- `pipeline_id` (string, optional): Only identities seen on this pipeline
- `last_seen_within_days` (integer, optional, 1-3650): Rolling UTC window; future timestamps are excluded
- `sort_by` (string, optional, default `last_seen_at`): `last_seen_at` · `first_seen_at` · `display_name` · `appearances_count`
- `sort_order` (string, optional, default `desc`): `asc` · `desc`
- `limit` (integer, optional, legacy mode only, max 1000)

**Example Request**:
```bash
curl "http://localhost:8000/api/admin/identities?page=1&page_size=25&q=john&type=known&last_seen_within_days=7" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Example Response (paginated)**:
```json
{
  "items": [
    {"id": "123e4567-...", "display_name": "John Doe", "type": "known",
     "status": "active", "appearances_count": 150,
     "first_seen_at": "2026-01-01T08:00:00Z", "last_seen_at": "2026-07-25T18:30:00Z",
     "snapshot_url": "/storage/snapshots/...", "pipeline_ids": ["camera_1", "camera_2"]}
  ],
  "total": 25000, "page": 1, "page_size": 25, "total_pages": 1000
}
```

---

## Watchlist API

All mutating watchlist endpoints require the **CSRF header** (cookie clients)
and are audited.

### 1. List Watchlists

**Endpoint**: `GET /api/watchlists`

**Query Parameters**: `page`, `page_size` (max 100), `search`, `alert_level`,
`is_active`, `include_deleted`, `sort_by`, `sort_order`, `include_inactive`

**Response**: Paginated envelope (when `page` is given) with **real statistics**
and the exact reporting window used for "today":

```json
{
  "items": [
    {"id": "uuid", "name": "VIP", "description": "...", "alert_level": "critical",
     "color": "#dc2626", "icon": "shield-alt", "is_active": true, "version": 4,
     "entries_count": 25, "alerts_today": 7, "total_alerts": 145,
     "last_alert_at": "2026-07-26T09:12:00Z",
     "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-07-20T10:00:00Z",
     "deleted_at": null}
  ],
  "total": 12, "page": 1, "page_size": 20, "total_pages": 1,
  "stats_period": {"period_start": "2026-07-26T00:00:00Z",
                   "period_end": "2026-07-27T00:00:00Z", "timezone": "UTC"}
}
```

> **⚠️ Changed in v6.0.0**: `alerts_today` was previously hard-coded to `0` in
> the UI. It is now a real count over an explicit reporting period.

### 2. Create / Update Watchlist

**Endpoints**: `POST /api/watchlists` · `PUT /api/watchlists/{watchlist_id}`

**Validation** (returns `422` on violation):
| Field | Rule |
|---|---|
| `name` | 2–100 chars, whitespace-normalized, **case-insensitively unique among live watchlists** |
| `description` | ≤1000 chars |
| `color` | six-digit hex (`^#[0-9a-fA-F]{6}$`) |
| `icon` | allowlist: `list`, `shield-alt`, `user-shield`, `exclamation-triangle`, `eye`, `users`, `star`, `ban`, `user-secret`, `crosshairs` |
| `alert_level` | `info` · `warning` · `critical` |

**Optimistic concurrency**: include the `version` you read. If another admin
changed the record you receive `409 VERSION_CONFLICT` with `current_version`
instead of silently overwriting their edit.

```bash
curl -X PUT "http://localhost:8000/api/watchlists/$ID" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"alert_level": "critical", "version": 4}'
```

**`409 NAME_CONFLICT`** is returned for duplicate names (case-insensitive).

### 3. Activate / Deactivate

**Endpoint**: `PATCH /api/watchlists/{watchlist_id}/status`

```json
{"is_active": false, "reason": "Temporarily disabled during review", "version": 4}
```

An inactive watchlist **stops matching detections** — no new alerts are
generated — but its entries and history are preserved.

### 4. Deletion Impact, Soft Delete, Restore

**Endpoints**:
- `GET /api/watchlists/{watchlist_id}/deletion-impact`
- `DELETE /api/watchlists/{watchlist_id}` *(soft by default)*
- `POST /api/watchlists/{watchlist_id}/restore`

> **⚠️ Changed in v6.0.0**: `DELETE` now performs a **soft delete** — matching
> stops immediately, but entries, alert history and audit records are kept and
> the watchlist can be restored. Permanent deletion requires
> `?hard_delete=true&confirm=true` and returns the impact it destroyed.

```bash
# 1. What am I about to remove?
curl "http://localhost:8000/api/watchlists/$ID/deletion-impact" -H "Authorization: Bearer $TOKEN"
# -> {"entries": 25, "active_entries": 22, "alerts": 145}

# 2. Soft delete (reversible)
curl -X DELETE "http://localhost:8000/api/watchlists/$ID?reason=under+review" \
  -H "Authorization: Bearer $TOKEN"
# -> {"success": true, "action": "soft_deleted", "impact": {...}, "deleted_at": "..."}

# 3. Undo
curl -X POST "http://localhost:8000/api/watchlists/$ID/restore" -H "Authorization: Bearer $TOKEN"
```

Requesting `hard_delete=true` without `confirm=true` returns
`400 CONFIRMATION_REQUIRED` **with the impact summary** so a client can show it
before proceeding.

### 5. Watchlist Entries

**Endpoints**:
- `GET /api/watchlists/{watchlist_id}/entries` — paginated (`page`, `page_size` max 200)
- `POST /api/watchlists/{watchlist_id}/entries` — **idempotent**: adding an identity already on the list updates that entry instead of creating a duplicate
- `DELETE /api/watchlists/{watchlist_id}/entries/{identity_id}`

`priority` is validated against `low` · `normal` · `high` · `critical`.
A non-existent identity returns `404`.

### 6. Statistics

**Endpoint**: `GET /api/watchlists/{watchlist_id}/stats`

```json
{"total_entries": 25, "active_entries": 22, "expired_entries": 3,
 "alerts_today": 7, "alerts_total": 145, "unacknowledged_alerts": 4,
 "period_start": "2026-07-26T00:00:00Z", "period_end": "2026-07-27T00:00:00Z",
 "timezone": "UTC"}
```

### 7. Real-time events

Every watchlist mutation publishes an idempotent WebSocket event so live
consumers refresh their matching configuration without a restart:

```json
{"type": "watchlist_changed", "event_id": "b3f1...", "action": "watchlist_status_changed",
 "watchlist_id": "uuid", "timestamp": "2026-07-26T12:00:00Z", "is_active": false}
```

Actions: `watchlist_created` · `watchlist_updated` · `watchlist_status_changed`
· `watchlist_deleted` · `watchlist_restored` · `watchlist_entry_added` ·
`watchlist_entry_removed`

---

## ML Model Lifecycle API

The merge-suggestion similarity model uses a **candidate → review → active**
lifecycle. Training never replaces the live model.

### 1. Model Status

**Endpoint**: `GET /api/admin/merge-suggestions/model-status`

**Response headers**: `Cache-Control: no-store`

> **⚠️ Changed in v6.0.0**: all fields are now real JSON types (booleans /
> integers, never `"false"` or `"50"` strings); sample counts come from the
> **persistent database** (they used to come from an in-memory list that reset
> on every restart); `model_path` was **removed** — use `active_model.artifact_name`.

```json
{
  "is_trained": true, "runtime_loaded": true, "sklearn_available": true,
  "training_samples": 75, "approved_samples": 65, "rejected_samples": 10,
  "unique_identity_pairs": 41, "min_samples": 50,
  "ready_to_train": false,
  "readiness_reason": "insufficient_class_balance",
  "readiness_checks": {
    "total_samples":         {"passed": true,  "current": 75, "required": 50},
    "approved_samples":      {"passed": true,  "current": 65, "required": 5},
    "rejected_samples":      {"passed": true,  "current": 10, "required": 5},
    "class_balance":         {"passed": false, "ratio": 0.13, "minimum_ratio": 0.2},
    "unique_identity_pairs": {"passed": true,  "current": 41, "required": 10}
  },
  "active_model": {"id": 9, "version": 3, "status": "active",
                   "artifact_name": "similarity-model-v3",
                   "artifact_hash": "5f2c...", "metrics": {...},
                   "activated_at": "2026-07-20T10:00:00Z"},
  "candidate_model": null,
  "training_job_running": null,
  "configuration": {"auto_train": false, "minimum_samples": 50,
                    "feature_schema_version": "similarity-features-v1",
                    "quality_gates": {...},
                    "configuration_source": "config",
                    "effective_at": "2026-07-26T12:00:00Z"}
}
```

### 2. Training Jobs

**Endpoints**:
- `POST /api/admin/merge-suggestions/training-jobs` *(schedule)*
- `GET /api/admin/merge-suggestions/training-jobs/{job_id}` *(poll)*
- `GET /api/admin/merge-suggestions/training-jobs` *(history)*
- `POST /api/admin/merge-suggestions/training-jobs/{job_id}/cancel`

> **⚠️ Changed in v6.0.0**: `POST /api/admin/merge-suggestions/train-model` no
> longer trains inside the request. It is a deprecated shim that schedules a
> job and returns `202` — it no longer returns metrics directly.

**Status Codes**:
- `202 Accepted`: scheduled
- `400 DATASET_NOT_READY`: includes `readiness_reason` + `readiness_checks`
- `409 TRAINING_ALREADY_RUNNING`: includes the running `job_id`
- `403`: missing CSRF header (cookie auth)

**Training stages** (reported via `progress_percent` + `details.stage`):
`collecting_dataset` → `validating_dataset` → `splitting_dataset` → `training`
→ `evaluating` → `saving_candidate` → `validating_artifact`

**Completed job result**:
```json
{
  "model_id": 12, "version": 4, "artifact_name": "similarity-model-v4",
  "artifact_hash": "9ab3...", "dataset_hash": "1c77...", "sample_total": 240,
  "split": {"split_method": "identity_pair_grouped", "seed": 42,
            "group_count": 48, "train_count": 192, "validation_count": 48},
  "training_metrics":   {"r2": 0.91, "mse": 0.02, "precision": 0.99, ...},
  "validation_metrics": {"r2": 0.88, "mse": 0.031, "rmse": 0.176, "mae": 0.12,
                         "precision": 0.991, "recall": 0.964, "f1": 0.977,
                         "false_merge_rate": 0.006, "missed_merge_rate": 0.036,
                         "confusion_matrix": {"tp": 54, "fp": 1, "tn": 61, "fn": 4},
                         "decision_threshold": 0.5, "sample_count": 120},
  "quality_gates": {"passed": true, "gates": {...}},
  "comparison": {"active_available": true, "differences": {...},
                 "recommendation": "promote"},
  "awaiting_approval": true
}
```

**Why precision and false-merge rate, not just R²**: a false merge combines two
different people into one identity, corrupting downstream data. Quality gates
enforce a minimum precision, a maximum false-merge rate, and a minimum
validation sample count. **A candidate failing its gates cannot be activated.**

**Leakage-safe splitting**: the train/validation split is grouped by identity
pair with a stored deterministic seed, so rows about the same pair can never
appear on both sides.

### 3. Candidate Review and Activation

**Endpoints**:
- `GET /api/admin/merge-suggestions/models` — version history (registry)
- `POST /api/admin/merge-suggestions/models/{model_id}/activate`
- `POST /api/admin/merge-suggestions/models/{model_id}/reject`
- `POST /api/admin/merge-suggestions/models/{model_id}/rollback`

All accept an optional `reason` query parameter (audited).

**Activation is atomic and verified**: the artifact hash is re-checked, the
model is loaded and smoke-tested, then the previous active version is archived
and the candidate promoted in one transaction — a database constraint permits
**exactly one active model per type**. The runtime is refreshed in place; if the
reload fails the previous model keeps serving and `runtime_degraded: true` is
returned.

```json
{"success": true, "model_id": 12, "version": 4, "artifact_name": "similarity-model-v4",
 "previous_model_id": 9, "previous_version": 3, "runtime_degraded": false}
```

**Status Codes**: `409 QUALITY_GATES_FAILED` (unsafe candidate) ·
`409 INVALID_STATUS` (not a candidate / not archived for rollback) ·
`400 ARTIFACT_HASH_MISMATCH` · `400 FEATURE_SCHEMA_MISMATCH`

**Rollback** re-activates a previously active (`archived`) version after
revalidating its artifact. Archived artifacts are never deleted automatically.

---

## SQL Agent API (People Tracking Intelligence)

### Query (streaming and REST)

**Endpoints**:
- `POST /api/sql-agent/query` — REST
- `POST /api/sql-agent/query/stream` — SSE stream
- `WS /ws/sql-agent` — WebSocket transport

Every request carries a client-generated `request_id`, and **every streamed
event repeats `request_id` and `sequence`**, so out-of-order or stale events
from an abandoned query can be discarded. Terminal events are explicit:
`complete` · `error` · `cancelled`.

The same `request_id` acts as an **idempotency key** — resubmitting it returns
`DUPLICATE_REQUEST` rather than executing the query twice.

### Cancel a running query

**Endpoint**: `POST /api/sql-agent/requests/{request_id}/cancel`

> **⚠️ New in v6.0.0**: cancellation is now real server-side cancellation.
> Previously, "stopping" only closed the browser stream while the query kept
> running.

```json
{"success": true, "request_id": "3f2a...", "status": "cancelling"}
```

### Security model

- Queries execute on a **read-only** connection (`default_transaction_read_only=on`), single statement, 30s statement timeout, 500-row cap
- A denied query returns `QUERY_DENIED` with a safe explanation and is audited — **it does not block your account**
- `ACCOUNT_BLOCKED` is only applied after repeated explicit violations (3 within one hour)
- Rejected SQL is written to the audit log, never returned to the browser

### History and health

- `GET /api/sql-agent/history?page=1&page_size=25` — paginated, owner-scoped, `no-store`
- `DELETE /api/sql-agent/history/{id}` — owner-verified, CSRF-protected
- `GET /api/sql-agent/health` — component status (`model`, `database`, `history`) with a `checked_at` timestamp

---

## Live Alerts API

- `GET /api/live-alerts` · `POST /api/live-alerts` · `PUT|DELETE /api/live-alerts/{alert_id}`
- `POST /api/live-alerts/{alert_id}/pause` · `/resume`
- `GET /api/live-alerts/{alert_id}/triggers?page=1&page_size=50` — server-side paginated envelope
- `POST /api/live-alerts/{alert_id}/triggers/acknowledge-all` — **bulk acknowledgement in one request** (never one request per trigger)
- `POST /api/live-alerts/{alert_id}/test` → `202 {"job_id": ...}`; poll `GET /api/live-alerts/test-jobs/{job_id}` for honest per-channel delivery results
- `GET /api/live-alerts/health` — alert subsystem health

All mutations are ownership-checked (an alert you do not own returns `404`),
CSRF-protected, and written to `live_alert_audit_log`. WebSocket alert events
carry an `event_id` for idempotent client-side de-duplication.

---

## Configuration

All API endpoints respect configuration from `config.py` and `.env`:

**Map Service Configuration**:
```bash
MAP_CACHE_TTL=3600
MAP_CACHE_ENABLED=true
MAP_MAX_COORDINATES=10000
MAP_GENERATION_TIMEOUT=30
MAP_MAX_TRACKS=100
MAP_DEFAULT_STYLE=dark
MAP_ENABLE_SECURITY_FEATURES=true
MAP_DETECT_PATTERNS=true
MAP_SHOW_RISK_HEATMAP=true
MAP_SHOW_TIMELINE=false
```
> Note: the per-request API parameters `enable_security_features`,
> `detect_patterns` and `show_risk_heatmap` default to **false** regardless of
> these server settings — a caller must opt in explicitly.

**Display windows (display-only — nothing is deleted)**:
```bash
DASHBOARD_FACE_DISPLAY_HOURS=3     # known faces on the dashboard
UNKNOWN_FACE_DISPLAY_HOURS=24      # unknown faces page (0 = show all)
```
`GET /api/admin/unknown?show_all=true` bypasses the window. Actual data removal
is governed separately by the retention/cleanup jobs.

**ML model**:
```bash
SIMILARITY_MODEL_MIN_SAMPLES=50
SIMILARITY_MODEL_AUTO_TRAIN=true
```

---

## Error Responses

Endpoints return one of two shapes.

**1. Safe internal error** — a generic message plus a reference ID. The real
exception (with stack trace) is in the server logs only:

```json
{"detail": "Internal error during model activation. Reference: ML-1154d5f4"}
```

Reference prefixes: `INTEL-` (intelligence) · `WL-` (watchlists) · `ML-` (model
lifecycle) · `SEC-` (SQL Agent security audit).

**2. Structured business error** — a machine-readable code plus context:

```json
{"detail": {"error_code": "VERSION_CONFLICT",
            "message": "This watchlist was modified by another administrator...",
            "current_version": 4}}
```

> **⚠️ Changed in v6.0.0**: endpoints no longer return raw exception text,
> SQL fragments, filesystem paths or dependency-install instructions. **Branch
> on `detail.error_code`, never on message text.**

**Common Status Codes**:
- `400 Bad Request`: Invalid parameters, or a structured precondition failure (`DATASET_NOT_READY`, `CONFIRMATION_REQUIRED`)
- `401 Unauthorized`: Authentication required
- `403 Forbidden`: Insufficient permissions **or missing CSRF header** on a cookie-authenticated mutation
- `404 Not Found`: Resource not found **or malformed ID** (identical response for both — IDs cannot be probed)
- `409 Conflict`: `*_ALREADY_RUNNING`, `NAME_CONFLICT`, `VERSION_CONFLICT`, `QUALITY_GATES_FAILED`, `INVALID_STATUS`
- `422 Unprocessable Entity`: Validation failure (bad color/icon/alert level, `page_size` above cap, `max_nodes` above ceiling)
- `500 Internal Server Error`: Server error (safe message + reference ID)
- `503 Service Unavailable`: Dependency unavailable — check `GET /api/security/capabilities`

---

## Rate Limiting

Rate limiting can be configured via `config.py`:
```bash
RATE_LIMIT_ENABLED=false
RATE_LIMIT_INTERVAL=1.0
```

---

## Caching

Map endpoints use Redis caching:
- **TTL**: Configurable via `MAP_CACHE_TTL` (default: 3600 seconds)
- **Cache Key**: SHA256 hash of request parameters
- **Cache Status**: Available via `/api/map/stats`

---

## Complete Endpoint Summary

🔒 = requires CSRF header for cookie clients · 📄 = supports pagination · ⏱ = background job

### Intelligence Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/identities/{id}/related` | GET | Related identities — **envelope `{items, thresholds}`** |
| `/api/identities/{id}/related/refresh` | POST 🔒 | Refresh relationship cache |
| `/api/intelligence/relationships/calculate-all` | POST 🔒⏱ | Schedule relationship calculation (202 + `job_id`) |
| `/api/intelligence/relationships/jobs/{job_id}` | GET | Poll relationship job |
| `/api/identities/{id}/temporal-patterns` | GET | Get temporal patterns |
| `/api/identities/{id}/cross-camera` | GET | Get cross-camera tracking |
| `/api/identities/{id}/timeline` | GET | Get movement timeline |
| `/api/identities/{id}/analyze` | GET | Complete analysis — **per-section statuses** |

### Map Service Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/identities/{id}/map` | GET | Interactive map (HTML — **embed sandboxed**) |
| `/api/identities/{id}/map/geojson` | GET | Map data as GeoJSON (preferred for custom UIs) |
| `/api/map/stats` | GET | Map service statistics |

### Security Intelligence Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/security/network` | GET | Social network analysis — **always bounded** |
| `/api/security/capabilities` | GET | Backend-verified feature readiness |
| `/api/security/patterns` | GET | Detect suspicious patterns |
| `/api/security/anomalies/{id}` | GET | Detect behavioral anomalies |
| `/api/security/threat/{id}` | GET | Threat assessment |

### Advanced SNA Enhancement Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/intelligence/thresholds/jobs` | POST 🔒⏱ | Schedule threshold learning (202 + `job_id`) |
| `/api/intelligence/thresholds/jobs/{job_id}` | GET | Poll threshold job |
| `/api/intelligence/thresholds/learn` | POST 🔒 | *Deprecated* synchronous threshold learning |
| `/api/intelligence/trajectory/predict` | GET | Predict next cameras (+ `model_version`, `confidence`) |
| `/api/intelligence/correlation/calculate` | GET | Activity **association** analysis (xCCA) |

### Identity Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/admin/identities` | GET 📄 | Server-side identity search + pagination |
| `/api/admin/identity/{id}` | GET | Identity detail |
| `/api/admin/unknown` | GET 📄 | Unknown faces (honors `UNKNOWN_FACE_DISPLAY_HOURS`, `show_all`) |

### Watchlist Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/watchlists` | GET 📄 | List/search watchlists with real statistics |
| `/api/watchlists` | POST 🔒 | Create watchlist |
| `/api/watchlists/{id}` | GET | Watchlist detail + stats period |
| `/api/watchlists/{id}` | PUT 🔒 | Update (send `version` for concurrency safety) |
| `/api/watchlists/{id}/status` | PATCH 🔒 | Activate / deactivate |
| `/api/watchlists/{id}/deletion-impact` | GET | Impact summary before deletion |
| `/api/watchlists/{id}` | DELETE 🔒 | **Soft** delete (add `hard_delete=true&confirm=true` for permanent) |
| `/api/watchlists/{id}/restore` | POST 🔒 | Restore a soft-deleted watchlist |
| `/api/watchlists/{id}/stats` | GET | Statistics with explicit reporting period |
| `/api/watchlists/{id}/entries` | GET 📄 | List entries |
| `/api/watchlists/{id}/entries` | POST 🔒 | Add identity (idempotent) |
| `/api/watchlists/{id}/entries/{identity_id}` | DELETE 🔒 | Remove identity |
| `/api/watchlist-alerts` | GET | List watchlist alerts |
| `/api/watchlist-alerts/{id}/acknowledge` | POST 🔒 | Acknowledge an alert |

### ML Model Lifecycle Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/admin/merge-suggestions/model-status` | GET | Typed status + dataset readiness checks |
| `/api/admin/merge-suggestions/training-jobs` | POST 🔒⏱ | Schedule training (202 + `job_id`) |
| `/api/admin/merge-suggestions/training-jobs/{job_id}` | GET | Poll training job |
| `/api/admin/merge-suggestions/training-jobs` | GET | Training history |
| `/api/admin/merge-suggestions/training-jobs/{job_id}/cancel` | POST 🔒 | Cancel a running job |
| `/api/admin/merge-suggestions/models` | GET | Model version registry |
| `/api/admin/merge-suggestions/models/{id}/activate` | POST 🔒 | Promote a candidate (atomic) |
| `/api/admin/merge-suggestions/models/{id}/reject` | POST 🔒 | Reject a candidate |
| `/api/admin/merge-suggestions/models/{id}/rollback` | POST 🔒 | Roll back to an archived version |
| `/api/admin/merge-suggestions/train-model` | POST 🔒⏱ | *Deprecated* — now schedules a job |

### SQL Agent Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/sql-agent/query` | POST 🔒 | REST query |
| `/api/sql-agent/query/stream` | POST 🔒 | SSE streaming query |
| `/api/sql-agent/requests/{request_id}/cancel` | POST 🔒 | **Real server-side cancellation** |
| `/api/sql-agent/history` | GET 📄 | Owner-scoped query history |
| `/api/sql-agent/history/{id}` | DELETE 🔒 | Delete a history entry |
| `/api/sql-agent/health` | GET | Component health |

### Live Alert Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/live-alerts` | GET / POST 🔒 | List / create alerts |
| `/api/live-alerts/{id}` | PUT 🔒 / DELETE 🔒 | Update / delete |
| `/api/live-alerts/{id}/pause` · `/resume` | POST 🔒 | Pause / resume |
| `/api/live-alerts/{id}/triggers` | GET 📄 | Paginated triggers |
| `/api/live-alerts/{id}/triggers/acknowledge-all` | POST 🔒 | Bulk acknowledge (single request) |
| `/api/live-alerts/{id}/test` | POST 🔒⏱ | Channel test job (202 + `job_id`) |
| `/api/live-alerts/test-jobs/{job_id}` | GET | Poll channel test |
| `/api/live-alerts/health` | GET | Alert subsystem health |

---

## Migration Checklist (v5 → v6)

| # | If your client… | Do this |
|---|---|---|
| 1 | Uses **Bearer tokens** | ✅ Nothing to change for CSRF |
| 2 | Uses **cookies** from a browser | ⚠️ Add `X-Requested-With: XMLHttpRequest` to every POST/PUT/PATCH/DELETE |
| 3 | Reads `GET /api/identities/{id}/related` as an array | ⚠️ Read `body.items` (envelope now includes `thresholds`) |
| 4 | Calls `POST .../relationships/calculate-all` | ⚠️ Expect **202** + `job_id`; handle **409** with the running job's id |
| 5 | Calls `POST .../thresholds/learn` | ⚠️ Switch to `POST /api/intelligence/thresholds/jobs` + poll |
| 6 | Calls `POST .../train-model` and reads metrics | ⚠️ Now returns **202** + `job_id`; poll the job, then activate the candidate |
| 7 | Reads `model_path` from model status | ⚠️ Removed — use `active_model.artifact_name` |
| 8 | Assumes `GET /api/security/network` returns everything | ⚠️ Check `truncated`/`scope`; pass `identity_ids` or raise `max_nodes` (≤300) |
| 9 | Relies on map security overlays being on | ⚠️ Pass `enable_security_features=true` etc. explicitly (now default **false**) |
| 10 | Embeds map HTML in an iframe | 🔒 Add `sandbox="allow-scripts"` **without** `allow-same-origin` |
| 11 | Expects `DELETE /api/watchlists/{id}` to erase data | ⚠️ Now a soft delete; add `?hard_delete=true&confirm=true` for permanence |
| 12 | Updates watchlists concurrently | ⚠️ Send `version`; handle **409 VERSION_CONFLICT** |
| 13 | Parses error message strings | ⚠️ Branch on `detail.error_code` instead |
| 14 | Downloads full identity lists | ⚠️ Use `?page=&page_size=&q=` server-side search |

---

## Related Documentation

- **In-app tutorial**: Admin → Tutorial → *"Platform Hardening: What Changed"* (live, always matches the running build)
- **Chapter 8.1**: Map Service Guide (`46_MAP_SERVICE_GUIDE.md`)
- **Chapter 8.2**: Map Service Data Flow (`47_MAP_SERVICE_DATA_FLOW.md`)
- **Chapter 8.3**: Security Intelligence Map Features (`48_SECURITY_INTELLIGENCE_MAP_FEATURES.md`)
- **Chapter 7.4**: Security Intelligence Guide (`45_SECURITY_INTELLIGENCE_GUIDE.md`)
- **Chapter 12.1**: API Authentication Guide (`25_API_AUTHENTICATION_GUIDE.md`)
- **Chapter 6**: Configuration Guide (`36_CONFIGURATION_GUIDE.md`)
- **Background Tasks**: `BACKGROUND_TASKS.md`

---

## Interactive API Documentation

For interactive API documentation, visit:
- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

---

## Conclusion

This API documentation covers all intelligence, map service, and security intelligence endpoints with examples, error handling, and configuration. All endpoints are production-ready with proper authentication, validation, and error handling.

