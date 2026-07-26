# Multi-Identity Merge Guide - Complete Documentation

## Overview

The multi-identity merge feature allows you to merge **3 or more identities** into one efficiently. This is a powerful tool for consolidating duplicate identities that represent the same person.

**Key Features:**
- ✅ Merge 2+ identities in a single operation
- ✅ **Smart target selection** - Automatically finds the best identity to merge into
- ✅ **O(n) time complexity** - Efficient algorithm for large merges
- ✅ **Manual override** - Optionally specify target identity
- ✅ **Access control** - Respects pipeline-based permissions

---

## When to Use Multi-Merge

### Use Multi-Merge When:
- ✅ You have 3+ identities that are the same person
- ✅ You want to merge multiple identities quickly
- ✅ You trust the system's smart selection algorithm
- ✅ You need to merge identities immediately (before daily suggestions run)

### Use Merge Suggestions When:
- ✅ You want the system to automatically find duplicates
- ✅ You prefer reviewing suggestions before merging
- ✅ You want graph-based clustering (finds larger clusters automatically)

### Use Single Merge When:
- ✅ You only have 2 identities to merge
- ✅ You need precise control over which identity becomes the target

---

## How It Works

### Smart Target Selection Algorithm (Production-Grade)

The system uses an **AI-powered scoring algorithm** to automatically select the best target identity:

**Scoring Criteria (weighted):**
| Criteria | Weight | Description |
|----------|--------|-------------|
| **KNOWN Type** | 5000 | Preserves KNOWN identities as targets |
| **Appearances** | 1000 per appearance | More appearances = higher score |
| **Pipeline Diversity** | 200 per pipeline | Multi-camera visibility rewarded |
| **Has Snapshot** | 100 | Quality indicator |
| **Age** | 1 per day | Older identities slightly preferred |

**Example:**
```
Identity A (UNKNOWN): 50 appearances, 1 pipeline, has snapshot, 10 days old
  Score = 0 + 50,000 + 200 + 100 + 10 = 50,310

Identity B (KNOWN): 30 appearances, 3 pipelines, has snapshot, 5 days old
  Score = 5,000 + 30,000 + 600 + 100 + 5 = 35,705

Identity C (UNKNOWN): 45 appearances, 2 pipelines, has snapshot, 8 days old
  Score = 0 + 45,000 + 400 + 100 + 8 = 45,508

Result: Identity A is selected as target (highest score: 50,310)
```

### Production Features

#### 1. Type Promotion
When merging **UNKNOWN + KNOWN** identities:
- Target becomes **KNOWN** automatically
- Inherits `display_name` from the known source
- Embeddings are moved from UNKNOWN to KNOWN FAISS index

#### 2. Best Snapshot Selection
- Compares quality scores across all identities
- Automatically selects the highest quality snapshot
- Updates target's `best_snapshot_path` if better found

#### 3. Pipeline Preservation
- All `pipeline_id` values are preserved during merge
- Merged identity appears on all relevant pipeline dashboards
- Complete cross-camera tracking maintained

#### 4. Enhanced FAISS Management
- Embeddings properly migrated between indexes on type changes
- Source identities removed from FAISS (marked, not deleted)
- Database records updated with correct `faiss_index_type`

### Time Complexity

- **Finding best identity**: O(n) - Single pass with pipeline queries
- **Merging identities**: O(n) - Batch database operations
- **FAISS updates**: O(n) - Metadata updates only
- **Total**: O(n) where n = number of identities

This is **much more efficient** than merging pairs sequentially (which would be O(n²)).

---

## Step-by-Step Guide

### Method 1: Using Multi-Select Mode (Web Interface)

1. **Enable Multi-Select Mode**
   - Click the **"MULTI-SELECT"** button in the header
   - Button changes to **"MULTI-SELECT ON"** (highlighted in green)

2. **Select Identities**
   - Click on identity cards to select them
   - Selected cards are highlighted with green border and glow
   - You need at least **2 identities** selected

3. **Open Merge Modal**
   - Click **"MERGE SELECTED (N)"** button (appears when 2+ selected)
   - Or click any identity's MERGE button to open merge modal

4. **Preview Before Merge (Recommended)**
   - Click the **"PREVIEW"** button (blue) to see detailed preview
   - Preview shows:
     - **Target Identity**: Auto-selected with scoring breakdown
     - **Source Identities**: List of identities to merge
     - **Type Promotion**: Will target become KNOWN?
     - **Snapshot Selection**: Will a better snapshot be used?
     - **Statistics**: Total appearances, embeddings, pipelines
     - **Warnings**: Cross-pipeline merges, type changes, etc.
     - **AI Selection Table**: Scoring breakdown for all candidates

5. **Execute Merge**
   - From preview modal: Click **"EXECUTE MERGE"**
   - From merge modal: Click **"MERGE"**
   - Optionally specify target identity (or leave empty for auto-selection)
   - Add notes if needed

6. **Result**
   - All selected identities are merged into the best target
   - System shows success message with details:
     - Number of identities merged
     - Type promotion (if any)
     - Pipeline count
   - Identity list refreshes automatically

### Method 2: Using API

#### Step 1: Preview Merge (Recommended)

**Endpoint:** `POST /api/admin/identities/merge-preview`

**Request:**
```json
{
  "identity_ids": ["uuid-1", "uuid-2", "uuid-3"],
  "target_identity_id": null  // Optional: null = auto-select
}
```

**Response:**
```json
{
  "success": true,
  "target_identity": {
    "id": "uuid-1",
    "type": "unknown",
    "display_name": null,
    "appearances_count": 50,
    "pipelines": ["CAMERA-1", "CAMERA-2"],
    "auto_selected": true
  },
  "source_identities": [
    {"id": "uuid-2", "type": "unknown", "appearances_count": 30, "pipelines": ["CAMERA-2"]},
    {"id": "uuid-3", "type": "known", "display_name": "John", "appearances_count": 20, "pipelines": ["CAMERA-3"]}
  ],
  "type_promotion": {
    "will_change": true,
    "from_type": "unknown",
    "to_type": "known",
    "reason": "Source identity uuid-3 is KNOWN",
    "inherited_name": "John"
  },
  "snapshot_selection": {
    "current_path": "/storage/CAMERA-1/snapshot.jpg",
    "will_change": false
  },
  "statistics": {
    "total_identities": 3,
    "total_appearances": 100,
    "total_embeddings": 15,
    "total_pipelines": 3,
    "pipeline_list": ["CAMERA-1", "CAMERA-2", "CAMERA-3"]
  },
  "warnings": [
    "⚠️ Cross-pipeline merge: Identities span 3 different pipelines",
    "ℹ️ Type promotion: Target will be promoted from UNKNOWN to KNOWN"
  ],
  "selection_details": {
    "auto_selected": true,
    "candidates": [
      {"id": "uuid-1", "type": "unknown", "score": 50310, "appearances": 50, "pipeline_count": 2},
      {"id": "uuid-3", "type": "known", "score": 25705, "appearances": 20, "pipeline_count": 1},
      {"id": "uuid-2", "type": "unknown", "score": 30200, "appearances": 30, "pipeline_count": 1}
    ]
  }
}
```

#### Step 2: Execute Merge

**Endpoint:** `POST /api/admin/identities/merge-multiple`

**Request:**
```json
{
  "identity_ids": [
    "uuid-1",
    "uuid-2",
    "uuid-3",
    "uuid-4"
  ],
  "target_identity_id": null,  // Optional: null = auto-select, or specify UUID
  "notes": "Merging 4 duplicate identities"
}
```

**Response (Enhanced with Production Details):**
```json
{
  "success": true,
  "message": "Successfully merged 3 identities into target identity",
  "identity": {
    "id": "uuid-1",
    "type": "known",  // May change if type promotion occurred
    "display_name": "John",  // Inherited from known source
    "status": "active",
    "appearances_count": 120,
    "best_snapshot_path": "/storage/CAMERA-1/snapshot.jpg"
  },
  "merged_count": 3,
  "auto_selected_target": true,
  
  // Production-grade additional fields:
  "statistics": {
    "appearances_moved": 70,
    "embeddings_moved": 10,
    "faces_moved": 70,
    "total_appearances": 120,
    "pipeline_count": 3,
    "pipelines": ["CAMERA-1", "CAMERA-2", "CAMERA-3"]
  },
  "type_promotion": {
    "changed": true,
    "from": "unknown",
    "to": "known",
    "inherited_name": "John"
  },
  "snapshot_selection": {
    "source": "uuid-2",
    "quality": 0.85,
    "path": "/storage/CAMERA-2/high_quality.jpg"
  },
  "pipeline_distribution": {
    "uuid-1": ["CAMERA-1"],
    "uuid-2": ["CAMERA-2", "CAMERA-3"],
    "uuid-3": ["CAMERA-1", "CAMERA-3"]
  },
  "selection_details": {
    "auto_selected": true,
    "selected_id": "uuid-1",
    "selected_score": 50310,
    "candidates": [...]
  },
  "timestamps": {
    "first_seen_at": "2024-01-01T08:00:00",
    "last_seen_at": "2024-01-04T16:45:00"
  }
}
```

**cURL Example:**
```bash
curl -X POST "http://localhost/api/admin/identities/merge-multiple" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "identity_ids": [
      "550e8400-e29b-41d4-a716-446655440000",
      "550e8400-e29b-41d4-a716-446655440001",
      "550e8400-e29b-41d4-a716-446655440002"
    ],
    "target_identity_id": null,
    "notes": "Merging duplicate identities"
  }'
```

---

## Access Control

### Admin Users
- ✅ Full access to merge any identities
- ✅ Can merge identities from any pipeline

### Regular Users with Pipeline Access
- ✅ Can merge identities from their assigned pipelines only
- ✅ System validates access before allowing merge
- ✅ Returns 403 Forbidden if access denied

**Example:**
```json
{
  "detail": "Access denied to identity 550e8400-e29b-41d4-a716-446655440000"
}
```

---

## What Gets Merged

When identities are merged, the following data is moved to the target identity:

1. **Appearances** - All detection records
2. **Embeddings** - Face recognition embeddings
3. **Faces** - Face detection records
4. **Best Snapshot** - If source has better quality, it updates target

**Source identities are:**
- Marked as `status: MERGED`
- Linked to target via `merged_into_id`
- Removed from FAISS indexes
- All data preserved for audit trail

---

## Best Practices

### 1. Review Before Merging
- Always review identity snapshots before merging
- Check appearance counts and timelines
- Verify identities are actually the same person

### 2. Use Smart Selection
- Let the system auto-select the best target (most appearances, best quality)
- Only override if you have a specific reason

### 3. Add Notes
- Always add notes explaining why identities were merged
- Helps with audit trail and future reference

### 4. Check Merge Suggestions First
- Review automatic merge suggestions before manual merge
- System may have already found your duplicates

### 5. Batch Operations
- Use multi-merge for clusters of 3+ identities
- More efficient than merging pairs sequentially

### 6. Use Preview Before Merge (Production Best Practice)
- **ALWAYS** preview before executing a merge
- Click the blue PREVIEW button to see exactly what will happen
- Review:
  - Target selection scoring (why this identity was chosen)
  - Type promotion (will target become KNOWN?)
  - Best snapshot selection (will a better image be used?)
  - Pipeline distribution (which cameras are affected)
  - Warnings (cross-pipeline merges, type changes)
- API: Call `/api/admin/identities/merge-preview` before `/api/admin/identities/merge-multiple`

### 7. Cross-Pipeline Merges
- When merging identities from different pipelines:
  - All `pipeline_id` values are preserved
  - Merged identity appears on all relevant dashboards
  - Preview shows warnings about cross-pipeline merge
  - Audit trail includes pipeline statistics

### 8. Type Promotion Awareness
- Merging UNKNOWN + KNOWN → target becomes KNOWN
- The `display_name` is inherited from the KNOWN source
- Embeddings are moved from UNKNOWN to KNOWN FAISS index
- Preview shows if type promotion will occur

---

## API Reference - New Endpoints

### Merge Preview (Recommended)

```
POST /api/admin/identities/merge-preview
```

**Purpose:** See exactly what will happen before merging

**Request:**
```json
{
  "identity_ids": ["uuid-1", "uuid-2", "uuid-3"],
  "target_identity_id": null
}
```

**Response includes:**
- Target identity with AI selection scores
- Source identities list
- Type promotion details
- Snapshot selection changes
- Pipeline distribution
- Merge statistics
- Warnings and alerts
- Full scoring breakdown for all candidates

---

## Error Handling

### Common Errors

**1. Not Enough Identities**
```json
{
  "detail": "At least 2 identity IDs required"
}
```
**Solution:** Select at least 2 identities

**2. Duplicate IDs**
```json
{
  "detail": "At least 2 unique identity IDs required"
}
```
**Solution:** Remove duplicate IDs from the list

**3. Identity Not Found**
```json
{
  "detail": "Some identities not found: ['uuid-1', 'uuid-2']"
}
```
**Solution:** Verify identity IDs are correct

**4. Already Merged**
```json
{
  "detail": "Some identities are already merged: ['uuid-1']"
}
```
**Solution:** Remove already-merged identities from the list

**5. Access Denied**
```json
{
  "detail": "Access denied to identity uuid-1"
}
```
**Solution:** Ensure you have pipeline access to all identities

---

## Comparison: Single vs Multi-Merge

| Feature | Single Merge | Multi-Merge |
|---------|-------------|-------------|
| **Identities** | 2 only | 2+ |
| **Target Selection** | Manual | Auto or Manual |
| **Time Complexity** | O(1) | O(n) |
| **Use Case** | Precise control | Batch operations |
| **Efficiency** | Good | Better for 3+ |

---

## Technical Details

### Backend Implementation

**File:** `backend/core/identity_service.py`

**Key Functions:**
- `find_best_identity()` - O(n) selection algorithm
- `merge_multiple_identities()` - O(n) merge operation

**Database Operations:**
- Batch updates for appearances, embeddings, faces
- Single transaction for atomicity
- Audit logging for all merges

### Frontend Implementation

**File:** `frontend/js/admin-unknown.js`

**Key Features:**
- Multi-select mode toggle
- Visual selection feedback
- Smart form switching (single vs multi)
- Real-time selection count

---

## Examples

### Example 1: Merge 5 Identities (Auto-Select Target)

**Request:**
```json
{
  "identity_ids": [
    "id-1", "id-2", "id-3", "id-4", "id-5"
  ],
  "target_identity_id": null
}
```

**Result:**
- System selects `id-3` (has 45 appearances, best snapshot)
- Merges `id-1`, `id-2`, `id-4`, `id-5` into `id-3`
- Final identity has 120 total appearances

### Example 2: Merge 3 Identities (Manual Target)

**Request:**
```json
{
  "identity_ids": [
    "id-1", "id-2", "id-3"
  ],
  "target_identity_id": "id-2"
}
```

**Result:**
- System uses `id-2` as target (as specified)
- Merges `id-1` and `id-3` into `id-2`
- Final identity has combined appearances

---

## Related Documentation

- **[09_HOW_MERGE_SUGGESTIONS_WORK.md](./09_HOW_MERGE_SUGGESTIONS_WORK.md)** - Automatic merge suggestions
- **[11_GRAPH_BASED_CLUSTERING.md](./11_GRAPH_BASED_CLUSTERING.md)** - Graph-based clustering algorithm
- **[06_PROMOTE_AND_MERGE_GUIDE.md](./06_PROMOTE_AND_MERGE_GUIDE.md)** - General merge guide
- **[07_UNKNOWN_FACES_CENTER_COMPLETE_GUIDE.md](./07_UNKNOWN_FACES_CENTER_COMPLETE_GUIDE.md)** - Unknown faces management

---

## Summary

Multi-identity merge is a powerful feature that:
- ✅ Saves time by merging multiple identities at once
- ✅ Uses smart algorithms to find the best target
- ✅ Maintains data integrity with atomic transactions
- ✅ Provides full audit trail for all operations
- ✅ Respects access control and permissions

**Use it when you need to merge 3+ identities efficiently!**

