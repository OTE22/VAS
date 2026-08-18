# Unknown Faces Center - Complete Documentation
> **Vector backend note.** Where this document says *FAISS*, the live
> system uses **PostgreSQL + pgvector**. PostgreSQL is authoritative and
> the index is a disposable acceleration layer — see
> [`70_VECTOR_INDEX_CONTRACT.md`](70_VECTOR_INDEX_CONTRACT.md). The
> surrounding explanation of *what* the index does is still accurate.

## A to Z Guide: Everything You Need to Know

---

## 📋 Table of Contents

1. [System Overview](#system-overview)
2. [Architecture & Components](#architecture--components)
3. [Complete Workflow (A to Z)](#complete-workflow-a-to-z)
4. [API Endpoints Reference](#api-endpoints-reference)
5. [Frontend Access Guide](#frontend-access-guide)
6. [Database Schema](#database-schema)
7. [FAISS Vector Indexes](#faiss-vector-indexes)
8. [Examples & Use Cases](#examples--use-cases)
9. [Troubleshooting](#troubleshooting)
10. [Best Practices](#best-practices)

---

## 🎯 System Overview

The **Unknown Faces Center** is a comprehensive identity management system that automatically detects, tracks, and manages unknown faces detected by the surveillance system. It provides administrators with powerful tools to identify, promote, merge, and search for faces across the entire system.

### Key Features

- ✅ **Automatic Unknown Face Detection**: Faces are automatically detected and assigned to identities
- ✅ **Dual FAISS Index System**: Separate indexes for KNOWN and UNKNOWN identities
- ✅ **Identity Promotion**: Convert unknown faces to known identities with names
- ✅ **Identity Merging**: Combine duplicate identities into one
- ✅ **Search by Image**: Upload an image to find matching identities
- ✅ **Merge Suggestions**: AI-powered suggestions for merging similar identities
- ✅ **Timeline Tracking**: Complete history of where and when each identity appeared
- ✅ **Comprehensive Audit Logging**: Every action is logged for forensic analysis
- ✅ **Advanced Filtering**: Filter by date, camera, appearances, and more

---

## 🏗️ Architecture & Components

### System Components

```
┌─────────────────────────────────────────────────────────────┐
│                    FRONTEND (Browser)                        │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  /admin/unknown - Unknown Faces Center UI           │   │
│  │  - Identity Grid View                               │   │
│  │  - Filters & Search                                 │   │
│  │  - Detail Modals                                    │   │
│  │  - Merge Suggestions                                │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                            ↕ HTTP/HTTPS
┌─────────────────────────────────────────────────────────────┐
│                    BACKEND (FastAPI)                        │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  /api/admin/unknown - Identity Management APIs       │   │
│  │  /api/search/by-image - Image Search                 │   │
│  │  /api/admin/merge-suggestions - Merge Operations     │   │
│  └──────────────────────────────────────────────────────┘   │
│                            ↕                                 │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  IdentityService - Core Business Logic              │   │
│  │  - find_or_create_identity()                        │   │
│  │  - promote_unknown_to_known()                       │   │
│  │  - merge_identities()                               │   │
│  └──────────────────────────────────────────────────────┘   │
│                            ↕                                 │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  IdentityIndexService - FAISS Vector Engine         │   │
│  │  - KNOWN Index (IndexFlatIP)                        │   │
│  │  - UNKNOWN Index (IndexFlatIP)                      │   │
│  │  - search_known() / search_unknown()                │   │
│  │  - add_known() / add_unknown()                      │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                            ↕
┌─────────────────────────────────────────────────────────────┐
│                    DATABASE (PostgreSQL)                    │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  identities - Core identity records                  │   │
│  │  identity_appearances - Timeline tracking            │   │
│  │  identity_embeddings - FAISS mapping                 │   │
│  │  merge_suggestions - AI suggestions                  │   │
│  │  identity_merges - Audit log                         │   │
│  │  identity_audit_log - All operations logged          │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Data Flow

1. **Face Detection** → Image Processing Pipeline
2. **Embedding Generation** → ONNX Model (w600k_r50.onnx)
3. **Identity Search** → FAISS Indexes (KNOWN → UNKNOWN)
4. **Identity Creation** → Database + FAISS Index
5. **Appearance Tracking** → Timeline Database
6. **Admin Actions** → Promotion/Merge/Search

---

## 🔄 Complete Workflow (A to Z)

### Phase 1: Automatic Detection & Identity Creation

#### Step 1: Face Detection
```
Camera Feed → Image Processing → Face Detector (det_10g.onnx)
```

**What Happens:**
- Image is received from camera pipeline
- Face detector identifies faces in the image
- Bounding boxes and landmarks are extracted

#### Step 2: Face Alignment & Embedding
```
Detected Face → Alignment → Embedding Generation (w600k_r50.onnx)
```

**What Happens:**
- Face is aligned using landmarks (112×112 pixels)
- 512-dimensional embedding vector is generated
- Embedding is L2-normalized for cosine similarity

#### Step 3: Identity Search
```
Embedding → Search KNOWN Index → Search UNKNOWN Index → Create New
```

**What Happens:**
1. **Search KNOWN Index** (threshold: 0.4)
   - If match found → Identity is KNOWN
   - Returns: `(identity_id, similarity_score)`

2. **Search UNKNOWN Index** (threshold: 0.35)
   - If match found → Identity is UNKNOWN (existing)
   - Returns: `(identity_id, similarity_score)`

3. **Create New Identity**
   - If no match → Create new UNKNOWN identity
   - Generate UUID for identity
   - Set type = UNKNOWN, status = ACTIVE

#### Step 4: Database & Index Updates
```
Identity → Save to Database → Add to FAISS Index → Track Appearance
```

**What Happens:**
- Identity record created in `identities` table
- Embedding added to UNKNOWN FAISS index
- Mapping saved in `identity_embeddings` table
- Appearance record created in `identity_appearances` table
- Face record updated with `identity_id` and `label_state`

### Phase 2: Admin Management

#### Step 5: View Unknown Faces
```
Admin → Navigate to /admin/unknown → View Identity Grid
```

**What Happens:**
- Frontend calls `GET /api/admin/unknown`
- Backend queries database for UNKNOWN identities
- Results displayed in grid with filters

#### Step 6: Filter & Search
```
Admin → Apply Filters → Refine Results
```

**Available Filters:**
- Date Range (from/to)
- Pipeline/Camera
- Minimum Appearances
- Status (active/merged/promoted/inactive)

#### Step 7: View Identity Details
```
Admin → Click Identity Card → View Details Modal
```

**What Happens:**
- Frontend calls `GET /api/admin/identity/{id}`
- Backend returns:
  - Identity information
  - Timeline of appearances
  - Snapshots gallery
  - Statistics (appearances, cameras, embeddings)

#### Step 8: Promote Unknown to Known
```
Admin → Click "PROMOTE" → Enter Name → Confirm
```

**What Happens:**
- Frontend calls `POST /api/admin/unknown/{id}/promote`
- Backend:
  1. Updates identity type: UNKNOWN → KNOWN
  2. Sets display_name
  3. Updates status: ACTIVE → PROMOTED
  4. Moves embeddings from UNKNOWN → KNOWN FAISS index
  5. Updates all related Face records
  6. Logs action to audit log

#### Step 9: Merge Identities
```
Admin → Click "MERGE" → Select Target → Confirm
```

**What Happens:**
- Frontend calls `POST /api/admin/identities/merge`
- Backend:
  1. Merges all appearances from source → target
  2. Merges all embeddings from source → target
  3. Updates Face records to point to target
  4. Marks source identity as MERGED
  5. Refreshes FAISS indexes
  6. Logs action to audit log

#### Step 10: Search by Image
```
Admin → Click "SEARCH BY IMAGE" → Upload Image → View Results
```

**What Happens:**
- Frontend uploads image to `POST /api/search/by-image`
- Backend:
  1. Detects face in uploaded image
  2. Generates embedding
  3. Searches KNOWN and/or UNKNOWN indexes
  4. Returns top matches with similarity scores
  5. Logs search to audit log

#### Step 11: Review Merge Suggestions
```
Admin → Click "MERGE SUGGESTIONS" → Review → Approve/Reject
```

**What Happens:**
- Frontend calls `GET /api/admin/merge-suggestions`
- Backend returns pending suggestions
- Admin reviews and:
  - **Approve**: Calls `POST /api/admin/merge-suggestions/{id}/approve`
  - **Reject**: Calls `POST /api/admin/merge-suggestions/{id}/reject`

#### Step 12: Create Live Alert for Unknown Person
```
Admin → Click Identity Card → Click "CREATE LIVE ALERT" → Configure → Submit
```

**What Happens:**
- Frontend calls `GET /api/live-alerts/defaults/{identity_id}` to get default settings
- Backend provides:
  - Default alert name
  - Default similarity threshold
  - Default notification settings
  - Identity ID (displayed in form)
  - User alert limits and warnings
- Admin configures alert settings
- Frontend calls `POST /api/live-alerts` with identity_id
- Backend creates alert and links it to the identity
- Alert will trigger when identity is detected again

**Important:**
- **Identity ID is always displayed** in the alert creation form
- This ensures you know exactly which identity the alert is tracking
- For unknown persons, the Identity ID is especially important since they may not have a name
- You can **copy the Identity ID** by clicking on it

#### Step 13: Add Unknown Person to Watchlist
```
Admin → Click Identity Card → Click "ADD TO WATCHLIST" → Select Watchlist → Submit
```

**What Happens:**
- Frontend calls `GET /api/watchlists/add-identity/{identity_id}/defaults`
- Backend provides:
  - Available watchlists (VIP, Threat, POI, etc.)
  - Default priority
  - Whether identity is already on a watchlist
- Admin selects watchlist and priority
- Frontend calls `POST /api/watchlists/{watchlist_id}/entries` with identity_id
- Backend adds identity to watchlist
- Identity is now monitored according to watchlist settings

**Important:**
- **Identity ID is used** to link the identity to the watchlist
- Watchlists can trigger alerts when identities are detected
- You can add the same identity to multiple watchlists

---

## 📡 API Endpoints Reference

### Base URL
All endpoints are prefixed with `/api` and require admin authentication.

### Authentication
All endpoints require:
- **Header**: `Authorization: Bearer <access_token>`
- **Role**: `admin`

---

### 1. Service Status Check

**Endpoint:** `GET /api/admin/identities/status`

**Description:** Check if the identity management service is available and get index statistics.

**Request:**
```bash
curl -X GET "http://localhost:8000/api/admin/identities/status" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Response:**
```json
{
  "status": "available",
  "details": {
    "service_available": true,
    "index_available": true,
    "model_manager_available": true,
    "index_stats": {
      "known_count": 150,
      "unknown_count": 342,
      "known_index_size": 150,
      "unknown_index_size": 342
    }
  },
  "available_endpoints": [
    "GET /api/admin/unknown",
    "GET /api/admin/identity/{id}",
    "POST /api/admin/unknown/{id}/promote",
    "POST /api/admin/identities/merge",
    "POST /api/search/by-image",
    "GET /api/admin/merge-suggestions",
    "POST /api/admin/merge-suggestions/{id}/approve",
    "POST /api/admin/merge-suggestions/{id}/reject"
  ]
}
```

**Frontend Access:**
- **Status Check**: Automatically performed on page load
- **Location**: `/admin/unknown` page
- **UI**: No direct UI, but errors shown if service unavailable

---

### 2. List Unknown Identities

**Endpoint:** `GET /api/admin/unknown`

**Description:** Get paginated list of unknown identities with optional filters.

**Query Parameters:**
- `page` (int, default: 1): Page number
- `page_size` (int, default: 20): Items per page
- `date_from` (string, optional): ISO date string (e.g., "2025-01-01T00:00:00")
- `date_to` (string, optional): ISO date string
- `pipeline_id` (string, optional): Filter by specific camera/pipeline
- `status_filter` (string, optional): Filter by status (active/merged/promoted/inactive)
- `min_appearances` (int, optional): Minimum number of appearances

**Request Example:**
```bash
curl -X GET "http://localhost:8000/api/admin/unknown?page=1&page_size=20&min_appearances=3&pipeline_id=camera_01" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Response:**
```json
{
  "identities": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "type": "unknown",
      "display_name": null,
      "status": "active",
      "first_seen_at": "2025-01-01T10:30:00",
      "last_seen_at": "2025-01-03T14:22:00",
      "appearances_count": 15,
      "best_snapshot_path": "/storage/unknown/unknown_550e8400.../snapshot_001.jpg",
      "cameras_count": 3
    },
    {
      "id": "660e8400-e29b-41d4-a716-446655440001",
      "type": "unknown",
      "display_name": null,
      "status": "active",
      "first_seen_at": "2025-01-02T08:15:00",
      "last_seen_at": "2025-01-03T16:45:00",
      "appearances_count": 8,
      "best_snapshot_path": "/storage/unknown/unknown_660e8400.../snapshot_001.jpg",
      "cameras_count": 2
    }
  ],
  "total": 342,
  "page": 1,
  "page_size": 20,
  "total_pages": 18
}
```

**Frontend Access:**
- **Location**: `/admin/unknown` page
- **UI Element**: Main identity grid (automatically loads on page load)
- **How to Use**:
  1. Navigate to `/admin/unknown` from admin navbar
  2. Page automatically loads first 20 identities
  3. Use filters to refine results
  4. Click "APPLY FILTERS" to refresh list
  5. Use pagination buttons to navigate pages

---

### 3. Get Identity Details

**Endpoint:** `GET /api/admin/identity/{identity_id}`

**Description:** Get detailed information about a specific identity including complete timeline.

**Path Parameters:**
- `identity_id` (string, UUID): The identity UUID

**Request Example:**
```bash
curl -X GET "http://localhost:8000/api/admin/identity/550e8400-e29b-41d4-a716-446655440000" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Response:**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "type": "unknown",
  "display_name": null,
  "status": "active",
  "first_seen_at": "2025-01-01T10:30:00",
  "last_seen_at": "2025-01-03T14:22:00",
  "appearances_count": 15,
  "best_snapshot_path": "/storage/unknown/unknown_550e8400.../snapshot_001.jpg",
  "cameras_count": 3,
  "appearances": [
    {
      "id": 123,
      "pipeline_id": "camera_01",
      "track_id": "track_456",
      "start_time": "2025-01-01T10:30:00",
      "end_time": "2025-01-01T10:35:00",
      "best_snapshot_path": "/storage/unknown/unknown_550e8400.../appearance_123.jpg"
    },
    {
      "id": 124,
      "pipeline_id": "camera_02",
      "track_id": "track_789",
      "start_time": "2025-01-02T14:20:00",
      "end_time": "2025-01-02T14:25:00",
      "best_snapshot_path": "/storage/unknown/unknown_550e8400.../appearance_124.jpg"
    }
  ],
  "embeddings_count": 12,
  "faces_count": 15
}
```

**Frontend Access:**
- **Location**: `/admin/unknown` page
- **UI Element**: Identity Detail Modal
- **How to Use**:
  1. Click "VIEW" button on any identity card
  2. Modal opens showing:
     - Identity information
     - Timeline of all appearances
     - Best snapshots for each appearance
     - Statistics (appearances, cameras, embeddings)
  3. Click "PROMOTE TO KNOWN" to promote
  4. Click "MERGE" to merge with another identity
  5. Click "CLOSE" or outside modal to close

---

### 4. Promote Unknown to Known

**Endpoint:** `POST /api/admin/unknown/{identity_id}/promote`

**Description:** Promote an unknown identity to known status and assign a display name.

**Path Parameters:**
- `identity_id` (string, UUID): The identity UUID to promote

**Request Body:**
```json
{
  "display_name": "John Doe",
  "person_code": "EMP-001"  // Optional
}
```

**Request Example:**
```bash
curl -X POST "http://localhost:8000/api/admin/unknown/550e8400-e29b-41d4-a716-446655440000/promote" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "John Doe",
    "person_code": "EMP-001"
  }'
```

**Response:**
```json
{
  "success": true,
  "message": "Identity promoted to known with name: John Doe",
  "identity": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "type": "known",
    "display_name": "John Doe",
    "status": "promoted"
  }
}
```

**What Happens Behind the Scenes:**
1. Identity type changed: `UNKNOWN` → `KNOWN`
2. `display_name` set to provided name
3. Status changed: `ACTIVE` → `PROMOTED`
4. All embeddings moved from UNKNOWN → KNOWN FAISS index
5. All Face records updated with new name
6. Action logged to `identity_audit_log` table

**Frontend Access:**
- **Location**: `/admin/unknown` page
- **UI Element**: Promote Modal
- **How to Use**:
  1. Click "PROMOTE" button on identity card OR
  2. Click "PROMOTE TO KNOWN" in identity detail modal
  3. Modal opens with form:
     - **Display Name** (required): Enter the person's name
     - **Person Code** (optional): Enter identifier code
  4. Click "PROMOTE" to confirm
  5. Success message shown, identity removed from unknown list

---

### 5. Merge Identities

**Endpoint:** `POST /api/admin/identities/merge`

**Description:** Merge two identities into one. All data from source identity is merged into target.

**Request Body:**
```json
{
  "from_identity_id": "550e8400-e29b-41d4-a716-446655440000",
  "to_identity_id": "660e8400-e29b-41d4-a716-446655440001",
  "notes": "These are the same person, different angles"  // Optional
}
```

**Request Example:**
```bash
curl -X POST "http://localhost:8000/api/admin/identities/merge" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "from_identity_id": "550e8400-e29b-41d4-a716-446655440000",
    "to_identity_id": "660e8400-e29b-41d4-a716-446655440001",
    "notes": "Merged duplicate identities"
  }'
```

**Response:**
```json
{
  "success": true,
  "message": "Identities merged successfully",
  "identity": {
    "id": "660e8400-e29b-41d4-a716-446655440001",
    "type": "unknown",
    "display_name": null,
    "status": "active"
  }
}
```

**What Happens Behind the Scenes:**
1. All `IdentityAppearance` records from source → target
2. All `IdentityEmbedding` records from source → target
3. All `Face` records updated to point to target
4. Source identity status set to `MERGED`
5. Target identity statistics updated (appearances_count, etc.)
6. FAISS indexes refreshed (embeddings moved)
7. Action logged to `identity_audit_log` and `identity_merges` tables

**Frontend Access:**
- **Location**: `/admin/unknown` page
- **UI Element**: Merge Modal
- **How to Use**:
  1. Click "MERGE" button on identity card OR
  2. Click "MERGE" in identity detail modal
  3. Merge modal opens:
     - **From Identity ID**: Pre-filled with selected identity
     - **Target Identity ID**: Enter or search for target
  4. Click "SEARCH" to find target identity by ID
  5. Select target from search results
  6. Enter optional notes
  7. Click "MERGE" to confirm
  8. Success message shown, source identity removed from list

---

### 6. Search by Image

**Endpoint:** `POST /api/search/by-image`

**Description:** Upload an image to search for matching identities in the system.

**Request (Form Data):**
- `image` (file): Image file (JPEG, PNG, etc.)
- `scope` (string, default: "both"): Search scope - "known", "unknown", or "both"
- `top_k` (int, default: 10): Number of top results to return
- `date_from` (string, optional): Filter results by date
- `date_to` (string, optional): Filter results by date
- `pipeline_id` (string, optional): Filter by specific camera

**Request Example:**
```bash
curl -X POST "http://localhost:8000/api/search/by-image" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -F "image=@/path/to/image.jpg" \
  -F "scope=both" \
  -F "top_k=10" \
  -F "date_from=2025-01-01T00:00:00" \
  -F "pipeline_id=camera_01"
```

**Response:**
```json
[
  {
    "identity_id": "550e8400-e29b-41d4-a716-446655440000",
    "type": "unknown",
    "display_name": null,
    "similarity": 0.87,
    "best_snapshot_path": "/storage/unknown/unknown_550e8400.../snapshot_001.jpg",
    "last_seen_at": "2025-01-03T14:22:00",
    "appearances_count": 15
  },
  {
    "identity_id": "770e8400-e29b-41d4-a716-446655440002",
    "type": "known",
    "display_name": "John Doe",
    "similarity": 0.82,
    "best_snapshot_path": "/storage/known/known_770e8400.../snapshot_001.jpg",
    "last_seen_at": "2025-01-02T10:15:00",
    "appearances_count": 42
  }
]
```

**What Happens Behind the Scenes:**
1. Image uploaded and decoded
2. Face detected using `det_10g.onnx`
3. Face aligned to 112×112 pixels
4. Embedding generated using `w600k_r50.onnx`
5. Embedding normalized (L2)
6. Searches KNOWN index (if scope includes "known")
7. Searches UNKNOWN index (if scope includes "unknown")
8. Results sorted by similarity score
9. Top K results returned
10. Search logged to audit log

**Frontend Access:**
- **Location**: `/admin/unknown` page
- **UI Element**: Search by Image Modal
- **How to Use**:
  1. Click "SEARCH BY IMAGE" button in page header
  2. Modal opens with upload form
  3. Click "Choose File" and select image
  4. Select search scope:
     - **Known Only**: Search only known identities
     - **Unknown Only**: Search only unknown identities
     - **Both**: Search all identities
  5. Optionally set date range and pipeline filter
  6. Click "SEARCH"
  7. Results displayed in grid:
     - Similarity score (0-1, higher = better match)
     - Identity preview image
     - Identity type and name
     - Last seen date
     - Appearances count
  8. Click "VIEW" on result to see full details
  9. Click "PROMOTE" to promote unknown results

---

### 7. Get Merge Suggestions

**Endpoint:** `GET /api/admin/merge-suggestions`

**Description:** Get AI-generated merge suggestions for unknown identities that might be duplicates.

**Query Parameters:**
- `status_filter` (string, optional): Filter by status - "pending", "approved", or "rejected"

**Request Example:**
```bash
curl -X GET "http://localhost:8000/api/admin/merge-suggestions?status_filter=pending" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Response:**
```json
[
  {
    "id": 1,
    "cluster_id": "cluster_001",
    "identity_ids": [
      "550e8400-e29b-41d4-a716-446655440000",
      "660e8400-e29b-41d4-a716-446655440001",
      "770e8400-e29b-41d4-a716-446655440002"
    ],
    "confidence": 0.85,
    "status": "pending",
    "representative_snapshots": [
      "/storage/unknown/unknown_550e8400.../snapshot_001.jpg",
      "/storage/unknown/unknown_660e8400.../snapshot_001.jpg"
    ],
    "created_at": "2025-01-03T10:00:00"
  }
]
```

**Frontend Access:**
- **Location**: `/admin/unknown` page
- **UI Element**: Merge Suggestions Modal
- **How to Use**:
  1. Click "MERGE SUGGESTIONS" button in page header
  2. Modal opens showing all pending suggestions
  3. Each suggestion shows:
     - Confidence score (0-1)
     - Number of identities to merge
     - Preview images of identities
  4. Click "APPROVE" to merge all identities
  5. Click "REJECT" to dismiss suggestion
  6. Click "VIEW" to see details before deciding

---

### 8. Approve Merge Suggestion

**Endpoint:** `POST /api/admin/merge-suggestions/{suggestion_id}/approve`

**Description:** Approve and execute a merge suggestion. All identities in the suggestion are merged into the first one.

**Path Parameters:**
- `suggestion_id` (int): The merge suggestion ID

**Request Example:**
```bash
curl -X POST "http://localhost:8000/api/admin/merge-suggestions/1/approve" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Response:**
```json
{
  "success": true,
  "message": "Merge suggestion approved and executed",
  "merged_identities": 2
}
```

**What Happens Behind the Scenes:**
1. All identities in suggestion merged into first identity
2. Suggestion status set to `APPROVED`
3. All merge operations logged to audit log
4. FAISS indexes refreshed

**Frontend Access:**
- **Location**: `/admin/unknown` page → Merge Suggestions Modal
- **UI Element**: "APPROVE" button on suggestion card
- **How to Use**:
  1. Open Merge Suggestions modal
  2. Review suggestion details
  3. Click "APPROVE" button
  4. Confirmation dialog appears
  5. Click "CONFIRM" to execute merge
  6. Success message shown, suggestion removed from list

---

### 9. Reject Merge Suggestion

**Endpoint:** `POST /api/admin/merge-suggestions/{suggestion_id}/reject`

**Description:** Reject a merge suggestion without executing the merge.

**Path Parameters:**
- `suggestion_id` (int): The merge suggestion ID

**Request Example:**
```bash
curl -X POST "http://localhost:8000/api/admin/merge-suggestions/1/reject" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

**Response:**
```json
{
  "success": true,
  "message": "Merge suggestion rejected"
}
```

**Frontend Access:**
- **Location**: `/admin/unknown` page → Merge Suggestions Modal
- **UI Element**: "REJECT" button on suggestion card
- **How to Use**:
  1. Open Merge Suggestions modal
  2. Review suggestion
  3. Click "REJECT" button
  4. Suggestion marked as rejected and removed from pending list

---

## 🖥️ Frontend Access Guide

### Main Access Point

**URL:** `http://localhost:8000/admin/unknown`

**Navigation:**
1. Login as admin user
2. Click "UNKNOWN FACES" in the admin navbar
3. Page loads automatically with unknown identities

### Page Layout

```
┌─────────────────────────────────────────────────────────┐
│  NAVBAR (Unified Admin Navbar)                         │
├─────────────────────────────────────────────────────────┤
│  HEADER                                                 │
│  ┌──────────────────────────────────────────────────┐  │
│  │  🕵️ Unknown Faces Center                         │  │
│  │  [MERGE SUGGESTIONS] [SEARCH BY IMAGE]           │  │
│  └──────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────┤
│  FILTERS                                                │
│  ┌──────────────────────────────────────────────────┐  │
│  │  Date From: [____]  Date To: [____]             │  │
│  │  Pipeline: [Dropdown]  Min Appearances: [____]  │  │
│  │  [APPLY FILTERS] [CLEAR]                        │  │
│  └──────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────┤
│  STATISTICS                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐            │
│  │ Total    │  │ Total    │  │ Active   │            │
│  │ Unknown  │  │ Appear.  │  │ Cameras  │            │
│  │   342    │  │  1,234   │  │    12    │            │
│  └──────────┘  └──────────┘  └──────────┘            │
├─────────────────────────────────────────────────────────┤
│  IDENTITY GRID                                          │
│  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐              │
│  │[IMG] │  │[IMG] │  │[IMG] │  │[IMG] │              │
│  │Last: │  │Last: │  │Last: │  │Last: │              │
│  │App:  │  │App:  │  │App:  │  │App:  │              │
│  │[VIEW]│  │[VIEW]│  │[VIEW]│  │[VIEW]│              │
│  │[PROM]│  │[PROM]│  │[PROM]│  │[PROM]│              │
│  │[MRGE]│  │[MRGE]│  │[MRGE]│  │[MRGE]│              │
│  └──────┘  └──────┘  └──────┘  └──────┘              │
├─────────────────────────────────────────────────────────┤
│  PAGINATION                                             │
│  [PREVIOUS]  Page 1 of 18  [NEXT]                      │
└─────────────────────────────────────────────────────────┘
```

### UI Elements & Actions

#### 1. Header Actions

**MERGE SUGGESTIONS Button:**
- **Location**: Top right of page header
- **Action**: Opens modal with AI-generated merge suggestions
- **Use Case**: Review and approve/reject automatic merge suggestions

**SEARCH BY IMAGE Button:**
- **Location**: Top right of page header (next to Merge Suggestions)
- **Action**: Opens modal to upload image and search
- **Use Case**: Find identities by uploading a photo

#### 2. Filter Section

**Date From/To:**
- **Purpose**: Filter identities by when they were last seen
- **Format**: Date picker (YYYY-MM-DD)
- **Example**: Show only identities seen between Jan 1-15, 2025

**Pipeline Dropdown:**
- **Purpose**: Filter by specific camera/pipeline
- **Options**: "All Pipelines" + list of active pipelines
- **Example**: Show only identities seen on "camera_01"

**Min Appearances:**
- **Purpose**: Filter by minimum number of appearances
- **Type**: Number input
- **Example**: Show only identities seen at least 5 times

**APPLY FILTERS Button:**
- **Action**: Applies all filters and refreshes the identity grid
- **Result**: Grid updates with filtered results

**CLEAR Button:**
- **Action**: Clears all filters and resets to default view
- **Result**: Shows all active unknown identities

#### 3. Statistics Cards

**Total Unknown:**
- **Shows**: Total count of active unknown identities
- **Updates**: Automatically when filters applied

**Total Appearances:**
- **Shows**: Total number of appearances across all unknown identities
- **Updates**: Automatically when filters applied

**Active Cameras:**
- **Shows**: Number of unique cameras/pipelines with unknown faces
- **Updates**: Automatically when filters applied

#### 4. Identity Cards

Each card shows:
- **Preview Image**: Best snapshot of the identity
- **Last Seen**: Date/time of last appearance
- **Appearances**: Number of times seen
- **Cameras**: Number of cameras where seen

**Card Actions:**
- **VIEW Button**: Opens detail modal with full timeline
- **PROMOTE Button**: Opens promote modal to assign name
- **MERGE Button**: Opens merge modal to combine with another identity

#### 5. Identity Detail Modal

**Opened by**: Clicking "VIEW" on identity card

**Shows:**
- Full identity information
- Complete timeline of appearances
- Best snapshot for each appearance
- Statistics (appearances, cameras, embeddings, faces)

**Actions:**
- **CREATE LIVE ALERT**: Create a real-time alert for this identity
- **ADD TO WATCHLIST**: Add this identity to a watchlist (VIP, Threat, POI, etc.)
- **PROMOTE TO KNOWN**: Promote this identity
- **MERGE**: Merge with another identity
- **CLOSE**: Close modal

**Important Notes:**
- **Identity ID is displayed** in the detail modal - you can copy it for reference
- **For Unknown Persons**: You can create alerts and add to watchlists even if they don't have a name yet
- **Live Alerts**: Get notified when this identity is detected again
- **Watchlists**: Organize identities into categories (VIP, Threat, POI) for monitoring

#### 6. Promote Modal

**Opened by**: Clicking "PROMOTE" on card or "PROMOTE TO KNOWN" in detail modal

**Form Fields:**
- **Display Name** (required): Person's name
- **Person Code** (optional): Identifier code

**Actions:**
- **PROMOTE**: Confirm promotion
- **CANCEL**: Close without promoting

#### 7. Merge Modal

**Opened by**: Clicking "MERGE" on card or in detail modal

**Form Fields:**
- **From Identity ID**: Pre-filled with selected identity
- **Target Identity ID**: Enter or search for target
- **Search Input**: Search for target identity
- **Notes** (optional): Reason for merge

**Actions:**
- **SEARCH**: Search for target identity
- **SELECT**: Select target from search results
- **MERGE**: Confirm merge operation
- **CANCEL**: Close without merging

#### 8. Search by Image Modal

**Opened by**: Clicking "SEARCH BY IMAGE" in header

**Form Fields:**
- **Image Upload**: Choose image file
- **Scope Dropdown**: 
  - "Known Only"
  - "Unknown Only"
  - "Both"
- **Date From/To** (optional): Filter results
- **Pipeline** (optional): Filter by camera

**Actions:**
- **SEARCH**: Execute search
- **CANCEL**: Close modal

**Results Display:**
- Grid of matching identities
- Similarity score (0-1)
- Preview image
- Identity type and name
- Last seen date
- Appearances count
- **VIEW** button: See full details
- **PROMOTE** button: Promote unknown results

#### 9. Merge Suggestions Modal

**Opened by**: Clicking "MERGE SUGGESTIONS" in header

**Shows:**
- Grid of pending merge suggestions
- Each suggestion shows:
  - Confidence score
  - Number of identities
  - Preview images
  - Created date

**Actions per Suggestion:**
- **APPROVE**: Merge all identities in suggestion
- **REJECT**: Dismiss suggestion
- **VIEW**: See detailed information

---

## 🗄️ Database Schema

### Core Tables

#### 1. `identities` Table

**Purpose:** Core identity records

**Columns:**
- `id` (UUID, PK): Unique identity identifier
- `type` (ENUM): `unknown` or `known`
- `display_name` (STRING, nullable): Person's name (if known)
- `status` (ENUM): `active`, `merged`, `promoted`, `inactive`
- `first_seen_at` (TIMESTAMP): First detection time
- `last_seen_at` (TIMESTAMP): Most recent detection time
- `best_snapshot_path` (STRING, nullable): Path to best quality snapshot
- `appearances_count` (INTEGER): Cached count of appearances
- `created_at` (TIMESTAMP): Record creation time
- `updated_at` (TIMESTAMP): Last update time

**Indexes:**
- `(type, status, last_seen_at DESC)` - For filtering and sorting

#### 2. `identity_appearances` Table

**Purpose:** Timeline tracking of when/where identities appeared

**Columns:**
- `id` (BIGINT, PK): Auto-increment ID
- `identity_id` (UUID, FK): Reference to identity
- `pipeline_id` (STRING): Camera/pipeline identifier
- `track_id` (STRING): Person tracking ID
- `start_time` (TIMESTAMP): Appearance start
- `end_time` (TIMESTAMP, nullable): Appearance end
- `best_snapshot_path` (STRING, nullable): Best snapshot for this appearance
- `created_at` (TIMESTAMP): Record creation time

**Indexes:**
- `(identity_id, start_time DESC)` - For timeline queries

#### 3. `identity_embeddings` Table

**Purpose:** Mapping between FAISS indexes and database identities

**Columns:**
- `id` (BIGINT, PK): Auto-increment ID
- `identity_id` (UUID, FK): Reference to identity
- `detection_id` (BIGINT, FK, nullable): Reference to detection
- `pipeline_id` (STRING): Camera/pipeline identifier
- `faiss_id` (INTEGER): Index in FAISS vector store
- `quality` (FLOAT): Embedding quality score (0-1)
- `faiss_index_type` (ENUM): `known` or `unknown`
- `created_at` (TIMESTAMP): Record creation time

**Indexes:**
- `(identity_id, created_at DESC)` - For identity queries
- `(faiss_id)` - For FAISS mapping lookups

#### 4. `merge_suggestions` Table

**Purpose:** AI-generated suggestions for merging duplicate identities

**Columns:**
- `id` (BIGINT, PK): Auto-increment ID
- `cluster_id` (STRING, nullable): Clustering algorithm identifier
- `identity_ids` (JSONB): Array of identity UUIDs to merge
- `confidence` (FLOAT): Confidence score (0-1)
- `status` (ENUM): `pending`, `approved`, `rejected`
- `representative_snapshots` (JSONB, nullable): Array of snapshot paths
- `created_at` (TIMESTAMP): Suggestion creation time
- `reviewed_at` (TIMESTAMP, nullable): Review time
- `reviewed_by` (INTEGER, FK, nullable): User who reviewed

#### 5. `identity_merges` Table

**Purpose:** Audit log of all merge operations

**Columns:**
- `id` (BIGINT, PK): Auto-increment ID
- `from_identity_id` (UUID, FK): Source identity (merged from)
- `to_identity_id` (UUID, FK): Target identity (merged into)
- `merged_by` (INTEGER, FK): User who performed merge
- `merged_at` (TIMESTAMP): Merge execution time
- `notes` (TEXT, nullable): Optional notes/justification

#### 6. `identity_audit_log` Table

**Purpose:** Comprehensive audit log of all identity management operations

**Columns:**
- `id` (INTEGER, PK): Auto-increment ID
- `user_id` (INTEGER, FK): User who performed action
- `username` (STRING): Username (denormalized for easier querying)
- `action_type` (STRING): Type of action (promote, merge, search, view, etc.)
- `identity_id` (UUID, FK, nullable): Target identity
- `related_identity_id` (UUID, FK, nullable): Related identity (for merges)
- `action_details` (JSONB, nullable): Additional action-specific details
- `before_state` (JSONB, nullable): State before action
- `after_state` (JSONB, nullable): State after action
- `ip_address` (STRING, nullable): Client IP (supplementary, not for identification)
- `user_agent` (STRING, nullable): Browser/client info (supplementary)
- `success` (BOOLEAN): Whether action succeeded
- `error_message` (TEXT, nullable): Error if failed
- `notes` (TEXT, nullable): Additional notes
- `created_at` (TIMESTAMP): Action timestamp

**Indexes:**
- `(user_id, action_type, created_at)` - For user activity queries
- `(identity_id, action_type, created_at)` - For identity history
- `(created_at)` - For time-based queries

#### 7. Modified `faces` Table

**New Columns:**
- `identity_id` (UUID, FK, nullable): Reference to identity
- `label_state` (ENUM): `auto_unknown`, `auto_known`, `manual_labeled`

---

## 🔍 FAISS Vector Indexes

### Architecture

The system uses **two separate FAISS indexes**:

1. **KNOWN Index**: Stable, curated identities
   - Contains embeddings for all known/promoted identities
   - Used for matching against known faces
   - Threshold: 0.4 (stricter matching)

2. **UNKNOWN Index**: Dynamic, merges happen here
   - Contains embeddings for all unknown identities
   - Used for matching against unknown faces
   - Threshold: 0.35 (slightly looser matching)

### Index Type

- **IndexFlatIP**: Inner Product (for cosine similarity with normalized vectors)
- **Dimension**: 512 (embedding vector size)
- **GPU Support**: Automatic fallback to CPU if GPU unavailable

### Operations

**Search:**
```python
# Search KNOWN index
matches = identity_index.search_known(embedding, top_k=1, threshold=0.4)
# Returns: [(identity_id_str, similarity_score), ...]

# Search UNKNOWN index
matches = identity_index.search_unknown(embedding, top_k=1, threshold=0.35)
# Returns: [(identity_id_str, similarity_score), ...]
```

**Add:**
```python
# Add to KNOWN index
faiss_id = identity_index.add_to_known(identity_id, embedding)

# Add to UNKNOWN index
faiss_id = identity_index.add_to_unknown(identity_id, embedding)
```

**Remove:**
```python
# Remove from KNOWN index
identity_index.remove_from_known(identity_id)

# Remove from UNKNOWN index
identity_index.remove_from_unknown(identity_id)
```

**Persistence:**
- Indexes saved to disk: `storage/faiss/known_index.faiss` and `storage/faiss/unknown_index.faiss`
- Auto-saved every 5 minutes
- Saved on graceful shutdown
- Loaded automatically on startup

---

## 💡 Examples & Use Cases

### Example 1: New Unknown Face Detected

**Scenario:** A new person appears on camera for the first time.

**Automatic Flow:**
1. Face detected → Embedding generated
2. Search KNOWN index → No match (similarity < 0.4)
3. Search UNKNOWN index → No match (similarity < 0.35)
4. New UNKNOWN identity created with UUID
5. Embedding added to UNKNOWN FAISS index
6. Appearance record created
7. Face record linked to identity

**Result:** Identity appears in `/admin/unknown` grid

---

### Example 2: Admin Promotes Unknown to Known

**Scenario:** Admin identifies an unknown face as "John Doe".

**Admin Actions:**
1. Navigate to `/admin/unknown`
2. Find identity in grid
3. Click "PROMOTE"
4. Enter name: "John Doe"
5. Click "PROMOTE" to confirm

**Backend Operations:**
1. Identity type: `UNKNOWN` → `KNOWN`
2. `display_name` set to "John Doe"
3. Status: `ACTIVE` → `PROMOTED`
4. All embeddings moved: UNKNOWN index → KNOWN index
5. All Face records updated with name
6. Action logged to audit log

**Result:** Identity removed from unknown list, now appears as known person

---

### Example 3: Merge Duplicate Identities

**Scenario:** Same person detected as two different identities (different angles/lighting).

**Admin Actions:**
1. Navigate to `/admin/unknown`
2. Click "MERGE" on first identity
3. Search for second identity ID
4. Select target from results
5. Enter notes: "Same person, different camera angles"
6. Click "MERGE" to confirm

**Backend Operations:**
1. All appearances from source → target
2. All embeddings from source → target
3. All Face records updated to point to target
4. Source identity status → `MERGED`
5. Target statistics updated
6. FAISS indexes refreshed
7. Action logged to audit log

**Result:** Two identities become one, all data consolidated

---

### Example 4: Search by Image

**Scenario:** Admin has a photo and wants to find if this person appears in the system.

**Admin Actions:**
1. Navigate to `/admin/unknown`
2. Click "SEARCH BY IMAGE"
3. Upload image file
4. Select scope: "Both" (search known and unknown)
5. Click "SEARCH"

**Backend Operations:**
1. Image decoded and face detected
2. Face aligned and embedding generated
3. Search KNOWN index → Find matches
4. Search UNKNOWN index → Find matches
5. Results sorted by similarity
6. Top 10 results returned
7. Search logged to audit log

**Result:** Grid of matching identities with similarity scores

---

### Example 5: Review Merge Suggestions

**Scenario:** System suggests merging 3 identities that appear similar.

**Admin Actions:**
1. Navigate to `/admin/unknown`
2. Click "MERGE SUGGESTIONS"
3. Review suggestion:
   - Confidence: 0.85
   - Identities: 3
   - Preview images shown
4. Click "APPROVE" to merge all

**Backend Operations:**
1. All 3 identities merged into first one
2. Suggestion status → `APPROVED`
3. All merge operations logged
4. FAISS indexes refreshed

**Result:** 3 identities become 1, suggestion removed from list

---

## 🔧 Troubleshooting

### Issue: "Identity service not available"

**Cause:** Identity service not initialized on startup

**Solution:**
1. Check server logs for initialization errors
2. Verify FAISS indexes can be loaded
3. Check database migrations have run
4. Restart the application

---

### Issue: "No face detected in image" (Search by Image)

**Cause:** Uploaded image has no detectable face or poor quality

**Solution:**
1. Use clear, front-facing photo
2. Ensure face is clearly visible
3. Try different image with better lighting
4. Check image format (JPEG, PNG supported)

---

### Issue: Identities not appearing in grid

**Cause:** Filters too restrictive or pagination issue

**Solution:**
1. Clear all filters
2. Check pagination (try page 1)
3. Verify identity status is "active"
4. Check database directly if needed

---

### Issue: Merge fails with "Identity not found"

**Cause:** One or both identity IDs are invalid or already merged

**Solution:**
1. Verify identity IDs are correct UUIDs
2. Check identities still exist in database
3. Ensure identities are not already merged
4. Refresh page and try again

---

### Issue: Promotion doesn't move identity to known

**Cause:** FAISS index update failed or database transaction issue

**Solution:**
1. Check server logs for errors
2. Verify FAISS indexes are writable
3. Check database connection
4. Try promoting again

---

## ✅ Best Practices

### For Administrators

1. **Regular Review**: Review unknown faces weekly to promote identified persons
2. **Merge Carefully**: Always verify identities before merging
3. **Use Search**: Use "Search by Image" to find duplicates before merging
4. **Review Suggestions**: Check merge suggestions regularly but verify before approving
5. **Audit Logs**: Review audit logs periodically for accountability

### For Developers

1. **Error Handling**: Always handle cases where identity service is unavailable
2. **Transactions**: Use database transactions for multi-step operations
3. **Index Persistence**: Ensure FAISS indexes are saved regularly
4. **Audit Logging**: Log all operations for forensic analysis
5. **Performance**: Use pagination for large result sets

### System Configuration

1. **Thresholds**: Adjust similarity thresholds based on your use case
   - Higher threshold = stricter matching (fewer false positives)
   - Lower threshold = looser matching (more matches, risk of false positives)

2. **Retention**: Configure retention policies for old data
   - Snapshots: 30-90 days
   - Embeddings: 6-12 months
   - Inactive identities: Mark after 180 days

3. **Clustering**: Run clustering job daily to generate merge suggestions

---

## 📚 Additional Resources

- **API Documentation**: `http://localhost:8000/docs` (Swagger UI)
- **Audit Logging Guide**: See `AUDIT_LOGGING_GUIDE.md`
- **Identity API Frontend Guide**: See `IDENTITY_API_FRONTEND_GUIDE.md`
- **Persistence Status**: See `PERSISTENCE_STATUS.md`

---

## 🎓 Quick Reference Card

### Endpoints Summary

| Endpoint | Method | Purpose | Frontend Access |
|----------|--------|---------|-----------------|
| `/api/admin/identities/status` | GET | Check service status | Auto on page load |
| `/api/admin/unknown` | GET | List unknown identities | Main grid (auto-load) |
| `/api/admin/identity/{id}` | GET | Get identity details | "VIEW" button |
| `/api/admin/unknown/{id}/promote` | POST | Promote to known | "PROMOTE" button |
| `/api/admin/identities/merge` | POST | Merge identities | "MERGE" button |
| `/api/search/by-image` | POST | Search by image | "SEARCH BY IMAGE" button |
| `/api/admin/merge-suggestions` | GET | Get suggestions | "MERGE SUGGESTIONS" button |
| `/api/admin/merge-suggestions/{id}/approve` | POST | Approve suggestion | "APPROVE" in suggestions modal |
| `/api/admin/merge-suggestions/{id}/reject` | POST | Reject suggestion | "REJECT" in suggestions modal |

### Frontend Navigation

```
Admin Navbar → UNKNOWN FACES → /admin/unknown
```

### Common Workflows

1. **Identify Unknown Person:**
   - View → Details → Promote → Enter Name

2. **Merge Duplicates:**
   - View → Merge → Search Target → Confirm

3. **Find Person by Photo:**
   - Search by Image → Upload → Review Results → View Details

4. **Review AI Suggestions:**
   - Merge Suggestions → Review → Approve/Reject

---

**Document Version:** 1.0  
**Last Updated:** January 2025  
**Maintained By:** ITDIR-AI DEPARTMENT

