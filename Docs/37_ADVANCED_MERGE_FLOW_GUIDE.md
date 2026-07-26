# Advanced Multi-Pipeline Identity Merge Flow - Complete Guide

## Table of Contents
1. [Overview](#overview)
2. [Quick Start - How to Use](#quick-start---how-to-use)
3. [Complete Merge Process Step-by-Step](#complete-merge-process-step-by-step)
4. [Pipeline Preservation Mechanism](#pipeline-preservation-mechanism)
5. [Database State Transformations](#database-state-transformations)
6. [FAISS Index Behavior](#faiss-index-behavior)
7. [Dashboard Visibility Logic](#dashboard-visibility-logic)
8. [Real-World Scenarios](#real-world-scenarios)
9. [Edge Cases and Solutions](#edge-cases-and-solutions)
10. [Performance and Optimization](#performance-and-optimization)
11. [Best Practices](#best-practices)

---

## Overview

This guide explains the complete flow of merging multiple identities from different pipelines, including all data transformations, state changes, and system behaviors.

### Key Concepts

- **Target Identity**: The identity that survives the merge (receives all data)
- **Source Identity**: The identity that gets merged into the target (marked as MERGED)
- **Pipeline Preservation**: Original `pipeline_id` values are maintained in all related records
- **FAISS Marking**: Vectors are marked as removed, not deleted (for performance)
- **Multi-Pipeline Visibility**: Merged identity appears on all relevant pipeline dashboards

---

## Quick Start - How to Use

### Web Interface (Recommended for Non-Technical Users)

#### Step 1: Select Identities
1. Go to **Unknown Faces Center** (`/admin/unknown`)
2. Click **"MULTI-SELECT"** button (top right)
3. Click on identity cards to select them (they highlight in green)
4. Select at least **2 identities** to merge

#### Step 2: Preview Merge (Recommended)
1. Click **"MERGE SELECTED"** button to open merge modal
2. Click the **"PREVIEW"** button (blue) to see what will happen:
   - **Target Identity**: Which identity will survive (auto-selected by AI)
   - **Source Identities**: Which identities will be merged into target
   - **Type Promotion**: Will the target become KNOWN?
   - **Snapshot Selection**: Will a better quality image be used?
   - **Statistics**: Total appearances, embeddings, pipelines
   - **Warnings**: Cross-pipeline alerts, type changes
   - **AI Scoring Table**: Why each identity scored as it did

#### Step 3: Execute Merge
1. Review the preview information
2. Add optional notes about the merge
3. Click **"EXECUTE MERGE"** button
4. Done! The identities are now merged.

### API Usage (For Developers)

#### Preview Before Merge
```bash
# Step 1: Preview merge to see what will happen
curl -X POST "http://localhost/api/admin/identities/merge-preview" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "identity_ids": ["uuid-1", "uuid-2", "uuid-3"],
    "target_identity_id": null
  }'
```

#### Execute Merge
```bash
# Step 2: Execute the merge
curl -X POST "http://localhost/api/admin/identities/merge-multiple" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "identity_ids": ["uuid-1", "uuid-2", "uuid-3"],
    "target_identity_id": null,
    "notes": "Merging duplicate identities"
  }'
```

### Production Features Available

| Feature | Description | How to Use |
|---------|-------------|------------|
| **AI Target Selection** | Scores identities by type, appearances, pipeline diversity | Automatic - leave `target_identity_id` as null |
| **Type Promotion** | UNKNOWN + KNOWN → KNOWN | Automatic - preview shows if it will happen |
| **Best Snapshot** | Selects highest quality image | Automatic - preview shows if better snapshot found |
| **Pipeline Preservation** | Keeps all `pipeline_id` values | Automatic - all pipelines are preserved |
| **Enhanced Audit** | Full logging with pipeline stats | Automatic - check audit logs for details |

---

## Complete Merge Process Step-by-Step

### Example 1: Merging 3 Unknown Identities from Different Pipelines

#### Initial State

```
┌─────────────────────────────────────────────────────────────────┐
│ Identity A (UUID: abc-123-def-456)                             │
│ ├── Type: UNKNOWN                                              │
│ ├── Status: ACTIVE                                             │
│ ├── Display Name: null                                         │
│ ├── Appearances Count: 10                                      │
│ ├── Best Snapshot: /storage/CAMERA-1/person_abc/snapshot.jpg  │
│ ├── Created At: 2024-01-01 08:00:00                           │
│ └── First/Last Seen: 2024-01-01 08:00 / 2024-01-03 14:30     │
│                                                               │
│ Related Data:                                                  │
│ ├── IdentityAppearance: 10 records                            │
│ │   └── All with pipeline_id = "CAMERA-1"                    │
│ ├── IdentityEmbedding: 5 records                              │
│ │   └── All with pipeline_id = "CAMERA-1"                    │
│ │   └── FAISS IDs: 100, 101, 102, 103, 104                   │
│ └── Face: 10 records                                           │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Identity B (UUID: def-456-ghi-789)                             │
│ ├── Type: UNKNOWN                                              │
│ ├── Status: ACTIVE                                             │
│ ├── Display Name: null                                         │
│ ├── Appearances Count: 8                                       │
│ ├── Best Snapshot: /storage/CAMERA-2/person_def/snapshot.jpg  │
│ ├── Created At: 2024-01-02 10:00:00                           │
│ └── First/Last Seen: 2024-01-02 10:00 / 2024-01-04 09:15     │
│                                                               │
│ Related Data:                                                  │
│ ├── IdentityAppearance: 8 records                              │
│ │   └── All with pipeline_id = "CAMERA-2"                     │
│ ├── IdentityEmbedding: 4 records                              │
│ │   └── All with pipeline_id = "CAMERA-2"                     │
│ │   └── FAISS IDs: 200, 201, 202, 203                         │
│ └── Face: 8 records                                            │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Identity C (UUID: ghi-789-jkl-012) - TARGET                    │
│ ├── Type: UNKNOWN                                              │
│ ├── Status: ACTIVE                                             │
│ ├── Display Name: null                                         │
│ ├── Appearances Count: 12                                      │
│ ├── Best Snapshot: /storage/CAMERA-3/person_ghi/snapshot.jpg  │
│ ├── Created At: 2024-01-01 12:00:00                           │
│ └── First/Last Seen: 2024-01-01 12:00 / 2024-01-04 16:45     │
│                                                               │
│ Related Data:                                                  │
│ ├── IdentityAppearance: 12 records                             │
│ │   └── All with pipeline_id = "CAMERA-3"                     │
│ ├── IdentityEmbedding: 6 records                              │
│ │   └── All with pipeline_id = "CAMERA-3"                     │
│ │   └── FAISS IDs: 300, 301, 302, 303, 304, 305              │
│ └── Face: 12 records                                           │
└─────────────────────────────────────────────────────────────────┘
```

#### Step 1: Target Selection Algorithm

The system evaluates all identities using a scoring system:

```python
# Scoring Formula:
score = (appearances_count × 1000) + 
        (has_best_snapshot × 100) + 
        (age_in_days × 1)

# Identity A Calculation:
score_A = (10 × 1000) + (1 × 100) + (3 × 1) = 10,103 points

# Identity B Calculation:
score_B = (8 × 1000) + (1 × 100) + (2 × 1) = 8,102 points

# Identity C Calculation:
score_C = (12 × 1000) + (1 × 100) + (3 × 1) = 12,103 points

# Result: Identity C selected as target (highest score)
```

**Why Identity C?**
- Most appearances (12 > 10 > 8)
- Has best snapshot (all three do, so tie-breaker)
- Older than Identity B (3 days vs 2 days)

#### Step 2: Database Transaction Begins

```python
async with db.begin():  # Start transaction
    # All operations are atomic - all succeed or all fail
```

#### Step 3: Process Identity A (Source)

**3.1 Update Identity A Status**
```sql
UPDATE identities 
SET 
    status = 'MERGED',
    merged_into_id = 'ghi-789-jkl-012',
    updated_at = '2024-01-04 17:00:00'
WHERE id = 'abc-123-def-456';
```

**Result:**
```
Identity A:
  status: ACTIVE → MERGED
  merged_into_id: null → 'ghi-789-jkl-012'
```

**3.2 Move Appearances (Pipeline Preserved!)**
```sql
UPDATE identity_appearances 
SET identity_id = 'ghi-789-jkl-012'
WHERE identity_id = 'abc-123-def-456';
```

**Before:**
```
identity_appearances table:
  id | identity_id      | pipeline_id | start_time
  1  | abc-123-def-456 | CAMERA-1    | 2024-01-01 08:00
  2  | abc-123-def-456 | CAMERA-1    | 2024-01-01 09:15
  3  | abc-123-def-456 | CAMERA-1    | 2024-01-02 10:30
  ... (7 more rows)
```

**After:**
```
identity_appearances table:
  id | identity_id      | pipeline_id | start_time
  1  | ghi-789-jkl-012 | CAMERA-1    | 2024-01-01 08:00  ← MOVED
  2  | ghi-789-jkl-012 | CAMERA-1    | 2024-01-01 09:15  ← MOVED
  3  | ghi-789-jkl-012 | CAMERA-1    | 2024-01-02 10:30  ← MOVED
  ... (7 more rows)                                      ← MOVED
  
  Note: pipeline_id column UNCHANGED - still "CAMERA-1"
```

**3.3 Move Embeddings (Pipeline Preserved!)**
```sql
UPDATE identity_embeddings 
SET identity_id = 'ghi-789-jkl-012'
WHERE identity_id = 'abc-123-def-456';
```

**Before:**
```
identity_embeddings table:
  id | identity_id      | pipeline_id | faiss_id | faiss_index_type
  1  | abc-123-def-456 | CAMERA-1    | 100      | unknown
  2  | abc-123-def-456 | CAMERA-1    | 101      | unknown
  3  | abc-123-def-456 | CAMERA-1    | 102      | unknown
  4  | abc-123-def-456 | CAMERA-1    | 103      | unknown
  5  | abc-123-def-456 | CAMERA-1    | 104      | unknown
```

**After:**
```
identity_embeddings table:
  id | identity_id      | pipeline_id | faiss_id | faiss_index_type
  1  | ghi-789-jkl-012 | CAMERA-1    | 100      | unknown  ← MOVED
  2  | ghi-789-jkl-012 | CAMERA-1    | 101      | unknown  ← MOVED
  3  | ghi-789-jkl-012 | CAMERA-1    | 102      | unknown  ← MOVED
  4  | ghi-789-jkl-012 | CAMERA-1    | 103      | unknown  ← MOVED
  5  | ghi-789-jkl-012 | CAMERA-1    | 104      | unknown  ← MOVED
  
  Note: pipeline_id and faiss_id columns UNCHANGED
```

**3.4 Move Face Records**
```sql
UPDATE faces 
SET identity_id = 'ghi-789-jkl-012'
WHERE identity_id = 'abc-123-def-456';
```

**3.5 Create Audit Record**
```sql
INSERT INTO identity_merges (
    from_identity_id,
    to_identity_id,
    merged_by,
    merged_at,
    notes
) VALUES (
    'abc-123-def-456',
    'ghi-789-jkl-012',
    5,  -- user_id
    '2024-01-04 17:00:00',
    'Multi-merge: 3 identities'
);
```

**3.6 Update FAISS Index**
```python
# Mark Identity A's embeddings as removed in FAISS metadata
identity_index.remove_from_unknown("abc-123-def-456")

# What happens internally:
# 1. Find all FAISS vectors with identity_id="abc-123-def-456" in metadata
# 2. Update metadata: {"identity_id": "abc-123-def-456", "removed": true}
# 3. Vectors remain in index (not deleted - for performance)
# 4. These vectors are skipped during similarity searches
```

**FAISS State:**
```
FAISS UNKNOWN Index:
  Vector 100: embedding=[...], metadata={"identity_id": "abc-123-def-456", "removed": true}
  Vector 101: embedding=[...], metadata={"identity_id": "abc-123-def-456", "removed": true}
  Vector 102: embedding=[...], metadata={"identity_id": "abc-123-def-456", "removed": true}
  Vector 103: embedding=[...], metadata={"identity_id": "abc-123-def-456", "removed": true}
  Vector 104: embedding=[...], metadata={"identity_id": "abc-123-def-456", "removed": true}
  
  Vector 200: embedding=[...], metadata={"identity_id": "def-456-ghi-789"}  ← Still active
  ...
  
  Vector 300: embedding=[...], metadata={"identity_id": "ghi-789-jkl-012"}  ← Still active
  ...
```

#### Step 4: Process Identity B (Source)

Same process as Identity A:
- Update status to MERGED
- Move appearances (pipeline_id="CAMERA-2" preserved)
- Move embeddings (pipeline_id="CAMERA-2" preserved)
- Move faces
- Create audit record
- Mark FAISS vectors as removed

#### Step 5: Update Target Identity Cache

```sql
-- Recalculate total appearances count
UPDATE identities 
SET appearances_count = (
    SELECT COUNT(*) 
    FROM identity_appearances 
    WHERE identity_id = 'ghi-789-jkl-012'
)
WHERE id = 'ghi-789-jkl-012';

-- Result: appearances_count = 30 (10 + 8 + 12)
```

#### Step 6: Update Timestamps

```sql
-- Update target identity's last_seen_at to most recent
UPDATE identities 
SET 
    last_seen_at = (
        SELECT MAX(start_time) 
        FROM identity_appearances 
        WHERE identity_id = 'ghi-789-jkl-012'
    ),
    updated_at = '2024-01-04 17:00:00'
WHERE id = 'ghi-789-jkl-012';

-- Result: last_seen_at = 2024-01-04 16:45 (from Identity C's most recent)
```

#### Step 7: Commit Transaction

```python
await db.commit()  # All changes are now permanent
```

#### Final State

```
┌─────────────────────────────────────────────────────────────────┐
│ Identity C (UUID: ghi-789-jkl-012) - TARGET (ACTIVE)           │
│ ├── Type: UNKNOWN (unchanged)                                  │
│ ├── Status: ACTIVE (unchanged)                                │
│ ├── Display Name: null                                          │
│ ├── Appearances Count: 30 (10 + 8 + 12) ← UPDATED             │
│ ├── Best Snapshot: /storage/CAMERA-3/person_ghi/snapshot.jpg   │
│ ├── First Seen: 2024-01-01 08:00 (earliest from all)          │
│ └── Last Seen: 2024-01-04 16:45 (most recent)                 │
│                                                               │
│ Related Data (ALL MERGED):                                     │
│ ├── IdentityAppearance: 30 records total                       │
│ │   ├── 10 records with pipeline_id = "CAMERA-1"              │
│ │   ├── 8 records with pipeline_id = "CAMERA-2"                │
│ │   └── 12 records with pipeline_id = "CAMERA-3"               │
│ ├── IdentityEmbedding: 15 records total                        │
│ │   ├── 5 records with pipeline_id = "CAMERA-1"                │
│ │   ├── 4 records with pipeline_id = "CAMERA-2"                │
│ │   └── 6 records with pipeline_id = "CAMERA-3"                │
│ └── Face: 30 records total                                      │
│                                                               │
│ Pipeline Distribution:                                         │
│ ├── CAMERA-1: 10 appearances (33.3%)                          │
│ ├── CAMERA-2: 8 appearances (26.7%)                            │
│ └── CAMERA-3: 12 appearances (40.0%)                          │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Identity A (UUID: abc-123-def-456) - MERGED (INVISIBLE)        │
│ ├── Type: UNKNOWN (unchanged)                                  │
│ ├── Status: MERGED ← Changed                                  │
│ ├── merged_into_id: ghi-789-jkl-012 ← Link to target         │
│ ├── Appearances Count: 0 (all moved)                           │
│ └── All data moved to Identity C                               │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Identity B (UUID: def-456-ghi-789) - MERGED (INVISIBLE)       │
│ ├── Type: UNKNOWN (unchanged)                                  │
│ ├── Status: MERGED ← Changed                                  │
│ ├── merged_into_id: ghi-789-jkl-012 ← Link to target          │
│ ├── Appearances Count: 0 (all moved)                           │
│ └── All data moved to Identity C                               │
└─────────────────────────────────────────────────────────────────┘
```

---

## Pipeline Preservation Mechanism

### How Pipeline Information is Preserved

The system maintains original `pipeline_id` values in three key tables:

#### 1. IdentityAppearance Table

Each appearance record maintains its original pipeline:

```sql
-- Before Merge
SELECT id, identity_id, pipeline_id, start_time 
FROM identity_appearances 
WHERE identity_id = 'abc-123-def-456';

Result:
  id | identity_id      | pipeline_id | start_time
  1  | abc-123-def-456 | CAMERA-1    | 2024-01-01 08:00
  2  | abc-123-def-456 | CAMERA-1    | 2024-01-01 09:15

-- After Merge
SELECT id, identity_id, pipeline_id, start_time 
FROM identity_appearances 
WHERE identity_id = 'ghi-789-jkl-012';

Result:
  id | identity_id      | pipeline_id | start_time
  1  | ghi-789-jkl-012 | CAMERA-1    | 2024-01-01 08:00  ← From Identity A
  2  | ghi-789-jkl-012 | CAMERA-1    | 2024-01-01 09:15  ← From Identity A
  10 | ghi-789-jkl-012 | CAMERA-2    | 2024-01-02 10:00  ← From Identity B
  11 | ghi-789-jkl-012 | CAMERA-2    | 2024-01-02 11:30  ← From Identity B
  20 | ghi-789-jkl-012 | CAMERA-3    | 2024-01-01 12:00  ← From Identity C
  21 | ghi-789-jkl-012 | CAMERA-3    | 2024-01-04 16:45  ← From Identity C
```

#### 2. IdentityEmbedding Table

Each embedding record maintains its original pipeline:

```sql
-- Query all embeddings for merged identity
SELECT id, identity_id, pipeline_id, faiss_id, faiss_index_type
FROM identity_embeddings 
WHERE identity_id = 'ghi-789-jkl-012'
ORDER BY pipeline_id, faiss_id;

Result:
  id | identity_id      | pipeline_id | faiss_id | faiss_index_type
  1  | ghi-789-jkl-012 | CAMERA-1    | 100      | unknown
  2  | ghi-789-jkl-012 | CAMERA-1    | 101      | unknown
  3  | ghi-789-jkl-012 | CAMERA-1    | 102      | unknown
  4  | ghi-789-jkl-012 | CAMERA-1    | 103      | unknown
  5  | ghi-789-jkl-012 | CAMERA-1    | 104      | unknown
  6  | ghi-789-jkl-012 | CAMERA-2    | 200      | unknown
  7  | ghi-789-jkl-012 | CAMERA-2    | 201      | unknown
  8  | ghi-789-jkl-012 | CAMERA-2    | 202      | unknown
  9  | ghi-789-jkl-012 | CAMERA-2    | 203      | unknown
  10 | ghi-789-jkl-012 | CAMERA-3    | 300      | unknown
  11 | ghi-789-jkl-012 | CAMERA-3    | 301      | unknown
  ... (4 more from CAMERA-3)
```

#### 3. Pipeline Aggregation Query

When displaying the merged identity, the system aggregates pipeline information:

```python
# Backend query logic
pipeline_ids_set = set()

# 1. From IdentityAppearance
appearance_result = await db.execute(
    select(IdentityAppearance.pipeline_id)
    .where(IdentityAppearance.identity_id == identity_uuid)
    .distinct()
)
for row in appearance_result:
    pipeline_ids_set.add(row[0])  # Add "CAMERA-1", "CAMERA-2", "CAMERA-3"

# 2. From IdentityEmbedding (fallback)
if not pipeline_ids_set:
    embedding_result = await db.execute(
        select(IdentityEmbedding.pipeline_id)
        .where(IdentityEmbedding.identity_id == identity_uuid)
        .distinct()
    )
    for row in embedding_result:
        pipeline_ids_set.add(row[0])

# Result: pipeline_ids = ["CAMERA-1", "CAMERA-2", "CAMERA-3"]
```

---

## Database State Transformations

### Complete Data Flow Diagram

```
BEFORE MERGE - Database State:

┌──────────────────────────────────────────────────────────────┐
│ identities table                                             │
├──────────────────────────────────────────────────────────────┤
│ id: abc-123 | type: UNKNOWN | status: ACTIVE | count: 10    │
│ id: def-456 | type: UNKNOWN | status: ACTIVE | count: 8     │
│ id: ghi-789 | type: UNKNOWN | status: ACTIVE | count: 12    │
└──────────────────────────────────────────────────────────────┘
         │              │              │
         ├──────────────┼──────────────┤
         │              │              │
         ▼              ▼              ▼
┌──────────────────────────────────────────────────────────────┐
│ identity_appearances table                                   │
├──────────────────────────────────────────────────────────────┤
│ id: 1-10   | identity_id: abc-123 | pipeline_id: CAMERA-1  │
│ id: 11-18  | identity_id: def-456  | pipeline_id: CAMERA-2  │
│ id: 19-30  | identity_id: ghi-789 | pipeline_id: CAMERA-3  │
└──────────────────────────────────────────────────────────────┘
         │              │              │
         ├──────────────┼──────────────┤
         │              │              │
         ▼              ▼              ▼
┌──────────────────────────────────────────────────────────────┐
│ identity_embeddings table                                    │
├──────────────────────────────────────────────────────────────┤
│ id: 1-5    | identity_id: abc-123 | pipeline_id: CAMERA-1   │
│ id: 6-9    | identity_id: def-456 | pipeline_id: CAMERA-2   │
│ id: 10-15  | identity_id: ghi-789 | pipeline_id: CAMERA-3   │
└──────────────────────────────────────────────────────────────┘

DURING MERGE - Transaction Updates:

┌──────────────────────────────────────────────────────────────┐
│ Step 1: Update Identity A status                            │
│   UPDATE identities SET status='MERGED',                      │
│   merged_into_id='ghi-789' WHERE id='abc-123'                │
└──────────────────────────────────────────────────────────────┘
         │
         ├─→ UPDATE identity_appearances 
         │   SET identity_id='ghi-789' 
         │   WHERE identity_id='abc-123'
         │   (pipeline_id stays "CAMERA-1")
         │
         ├─→ UPDATE identity_embeddings 
         │   SET identity_id='ghi-789' 
         │   WHERE identity_id='abc-123'
         │   (pipeline_id stays "CAMERA-1")
         │
         └─→ INSERT INTO identity_merges 
             (from_identity_id, to_identity_id, ...)

┌──────────────────────────────────────────────────────────────┐
│ Step 2: Update Identity B status                            │
│   UPDATE identities SET status='MERGED',                      │
│   merged_into_id='ghi-789' WHERE id='def-456'                │
└──────────────────────────────────────────────────────────────┘
         │
         ├─→ UPDATE identity_appearances 
         │   SET identity_id='ghi-789' 
         │   WHERE identity_id='def-456'
         │   (pipeline_id stays "CAMERA-2")
         │
         ├─→ UPDATE identity_embeddings 
         │   SET identity_id='ghi-789' 
         │   WHERE identity_id='def-456'
         │   (pipeline_id stays "CAMERA-2")
         │
         └─→ INSERT INTO identity_merges 
             (from_identity_id, to_identity_id, ...)

┌──────────────────────────────────────────────────────────────┐
│ Step 3: Update Identity C cache                              │
│   UPDATE identities SET appearances_count=30                 │
│   WHERE id='ghi-789'                                         │
└──────────────────────────────────────────────────────────────┘

AFTER MERGE - Final Database State:

┌──────────────────────────────────────────────────────────────┐
│ identities table                                             │
├──────────────────────────────────────────────────────────────┤
│ id: abc-123 | type: UNKNOWN | status: MERGED | count: 0    │
│ id: def-456 | type: UNKNOWN | status: MERGED | count: 0     │
│ id: ghi-789 | type: UNKNOWN | status: ACTIVE | count: 30   │
└──────────────────────────────────────────────────────────────┘
         │              │              │
         │              │              │
         │              │              └───┐
         │              │                  │
         │              └──────────────────┼───┐
         │                                 │   │
         └─────────────────────────────────┼───┼───┐
                                           │   │   │
                                           ▼   ▼   ▼
┌──────────────────────────────────────────────────────────────┐
│ identity_appearances table                                   │
├──────────────────────────────────────────────────────────────┤
│ id: 1-10   | identity_id: ghi-789 | pipeline_id: CAMERA-1  │
│ id: 11-18  | identity_id: ghi-789 | pipeline_id: CAMERA-2  │
│ id: 19-30  | identity_id: ghi-789 | pipeline_id: CAMERA-3  │
└──────────────────────────────────────────────────────────────┘
                                           │   │   │
                                           ▼   ▼   ▼
┌──────────────────────────────────────────────────────────────┐
│ identity_embeddings table                                    │
├──────────────────────────────────────────────────────────────┤
│ id: 1-5    | identity_id: ghi-789 | pipeline_id: CAMERA-1   │
│ id: 6-9    | identity_id: ghi-789 | pipeline_id: CAMERA-2   │
│ id: 10-15  | identity_id: ghi-789 | pipeline_id: CAMERA-3   │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│ identity_merges table (Audit Trail)                          │
├──────────────────────────────────────────────────────────────┤
│ id: 1 | from: abc-123 | to: ghi-789 | merged_by: 5 | ...    │
│ id: 2 | from: def-456 | to: ghi-789 | merged_by: 5 | ...   │
└──────────────────────────────────────────────────────────────┘
```

---

## FAISS Index Behavior

### How FAISS Handles Merged Identities

#### FAISS Index Structure

FAISS stores vectors (embeddings), not identities. Each vector has metadata linking it to an identity:

```python
# FAISS UNKNOWN Index Structure
Index:
  Vector 100: 
    embedding: [0.123, -0.456, 0.789, ...]  # 512-dimensional vector
    metadata: {
      "identity_id": "abc-123-def-456",
      "pipeline_id": "CAMERA-1",
      "quality": 0.85,
      "removed": false  # Active vector
    }
  
  Vector 101:
    embedding: [0.234, -0.567, 0.890, ...]
    metadata: {
      "identity_id": "abc-123-def-456",
      "pipeline_id": "CAMERA-1",
      "quality": 0.82,
      "removed": false
    }
  
  ... (more vectors)
```

#### Removal Process

When an identity is merged, its vectors are marked as removed (not deleted):

```python
# When Identity A is merged
identity_index.remove_from_unknown("abc-123-def-456")

# Internal process:
# 1. Query all vectors with identity_id="abc-123-def-456" in metadata
# 2. Update metadata: {"removed": true}
# 3. Vectors remain in index (FAISS doesn't support efficient deletion)
# 4. During searches, filter out vectors with "removed": true
```

**Before Removal:**
```
FAISS UNKNOWN Index:
  Vector 100: metadata={"identity_id": "abc-123", "removed": false}
  Vector 101: metadata={"identity_id": "abc-123", "removed": false}
  Vector 102: metadata={"identity_id": "abc-123", "removed": false}
  Vector 103: metadata={"identity_id": "abc-123", "removed": false}
  Vector 104: metadata={"identity_id": "abc-123", "removed": false}
  
  Vector 200: metadata={"identity_id": "def-456", "removed": false}
  ...
  
  Vector 300: metadata={"identity_id": "ghi-789", "removed": false}
  ...
```

**After Removal:**
```
FAISS UNKNOWN Index:
  Vector 100: metadata={"identity_id": "abc-123", "removed": true}  ← Marked
  Vector 101: metadata={"identity_id": "abc-123", "removed": true}  ← Marked
  Vector 102: metadata={"identity_id": "abc-123", "removed": true}  ← Marked
  Vector 103: metadata={"identity_id": "abc-123", "removed": true}  ← Marked
  Vector 104: metadata={"identity_id": "abc-123", "removed": true}  ← Marked
  
  Vector 200: metadata={"identity_id": "def-456", "removed": true}  ← Marked
  ...
  
  Vector 300: metadata={"identity_id": "ghi-789", "removed": false}  ← Active
  ...
```

#### Search Behavior After Merge

When searching for similar faces:

```python
# 1. Query FAISS index
similar_vectors = faiss_index.search(query_embedding, k=10)

# 2. Filter out removed vectors
active_vectors = [
    v for v in similar_vectors 
    if v.metadata.get("removed", False) == False
]

# 3. Return only active vectors
# Result: Only vectors from Identity C (ghi-789) are returned
# Vectors from Identity A and B are filtered out
```

#### Why Not Delete Vectors?

1. **Performance**: FAISS doesn't support efficient deletion
2. **Rebuild Cost**: Rebuilding entire index is expensive (O(n log n))
3. **History**: Preserves historical data for audit
4. **Speed**: Marking as removed is O(1) vs O(n) for deletion

---

## Dashboard Visibility Logic

### How Merged Identities Appear on Dashboards

#### Example: Security Office with 3 Cameras

**Setup:**
- **CAMERA-1**: Main Entrance
- **CAMERA-2**: Parking Lot  
- **CAMERA-3**: Back Entrance

**After Merge:**
- Identity C (ghi-789) has appearances from all 3 cameras

#### Dashboard Query Logic

**1. CAMERA-1 Dashboard Query:**
```sql
-- Find identities that have appearances in CAMERA-1
SELECT DISTINCT i.*
FROM identities i
INNER JOIN identity_appearances ia ON i.id = ia.identity_id
WHERE ia.pipeline_id = 'CAMERA-1'
  AND i.status = 'ACTIVE'  -- Only active identities
ORDER BY i.last_seen_at DESC;
```

**Result:**
- ✅ Identity C appears (has 10 appearances with pipeline_id="CAMERA-1")
- ❌ Identity A does not appear (status=MERGED)
- ❌ Identity B does not appear (status=MERGED)

**2. CAMERA-2 Dashboard Query:**
```sql
SELECT DISTINCT i.*
FROM identities i
INNER JOIN identity_appearances ia ON i.id = ia.identity_id
WHERE ia.pipeline_id = 'CAMERA-2'
  AND i.status = 'ACTIVE';
```

**Result:**
- ✅ Identity C appears (has 8 appearances with pipeline_id="CAMERA-2")

**3. CAMERA-3 Dashboard Query:**
```sql
SELECT DISTINCT i.*
FROM identities i
INNER JOIN identity_appearances ia ON i.id = ia.identity_id
WHERE ia.pipeline_id = 'CAMERA-3'
  AND i.status = 'ACTIVE';
```

**Result:**
- ✅ Identity C appears (has 12 appearances with pipeline_id="CAMERA-3")

#### Unknown Page Filtering

**Query:**
```sql
SELECT *
FROM identities
WHERE type = 'UNKNOWN'
  AND status = 'ACTIVE'  -- Filters out MERGED identities
ORDER BY last_seen_at DESC;
```

**Result:**
- ✅ Identity C appears (type=UNKNOWN, status=ACTIVE)
- ❌ Identity A does not appear (status=MERGED)
- ❌ Identity B does not appear (status=MERGED)

#### Visual Dashboard Representation

```
┌─────────────────────────────────────────────────────────────┐
│ CAMERA-1 Dashboard (Main Entrance)                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Identity C (ghi-789)                                       │
│  ├── Appearances: 30 total                                  │
│  ├── In this camera: 10 appearances                         │
│  ├── First seen: 2024-01-01 08:00                          │
│  └── Last seen: 2024-01-04 16:45                           │
│                                                             │
│  [Shows snapshot from CAMERA-3 - target's best snapshot]    │
│                                                             │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ CAMERA-2 Dashboard (Parking Lot)                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Identity C (ghi-789)                                       │
│  ├── Appearances: 30 total                                  │
│  ├── In this camera: 8 appearances                          │
│  ├── First seen: 2024-01-01 08:00                          │
│  └── Last seen: 2024-01-04 16:45                           │
│                                                             │
│  [Shows snapshot from CAMERA-3 - target's best snapshot]    │
│                                                             │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ CAMERA-3 Dashboard (Back Entrance)                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Identity C (ghi-789)                                       │
│  ├── Appearances: 30 total                                  │
│  ├── In this camera: 12 appearances                        │
│  ├── First seen: 2024-01-01 08:00                          │
│  └── Last seen: 2024-01-04 16:45                           │
│                                                             │
│  [Shows snapshot from CAMERA-3 - target's best snapshot]    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Real-World Scenarios

### Scenario 1: Employee Tracking Across Buildings

**Setup:**
- **Building A - Entrance**: CAMERA-1
- **Building B - Lobby**: CAMERA-2
- **Building C - Parking**: CAMERA-3

**Person:** John Doe (employee)

**Day 1 - Monday:**
- John enters Building A → Identity "unknown_001" created (CAMERA-1)
- 3 appearances recorded

**Day 2 - Tuesday:**
- John enters Building B → Identity "unknown_002" created (CAMERA-2)
- 5 appearances recorded

**Day 3 - Wednesday:**
- John parks at Building C → Identity "unknown_003" created (CAMERA-3)
- 4 appearances recorded

**Day 4 - Thursday:**
- Admin reviews and merges all 3 identities
- Target: unknown_003 (most appearances: 4)

**After Merge:**
```
Identity unknown_003:
  - Total appearances: 12 (3 + 5 + 4)
  - Seen in: Building A (3x), Building B (5x), Building C (4x)
  - Appears on all 3 building dashboards
  - Timeline shows: "Seen at Building A (3x), Building B (5x), Building C (4x)"
```

**When Promoted to KNOWN:**
- Display name: "John Doe"
- Appears on all 3 building dashboards as known person
- Security can track: "John Doe was at Building A on Monday, Building B on Tuesday, Building C on Wednesday"

### Scenario 2: Visitor Tracking

**Setup:**
- **Reception**: CAMERA-1
- **Conference Room**: CAMERA-2
- **Exit**: CAMERA-3

**Person:** Sarah Smith (visitor)

**Timeline:**
- 09:00 - Enters reception → Identity "visitor_001" (CAMERA-1)
- 09:15 - Enters conference room → Identity "visitor_002" (CAMERA-2)
- 10:30 - Exits building → Identity "visitor_003" (CAMERA-3)

**Merge:**
- All 3 identities merged into visitor_003

**Result:**
```
Identity visitor_003:
  - Complete visit timeline preserved
  - Reception: 09:00 (1 appearance)
  - Conference Room: 09:15 (1 appearance)
  - Exit: 10:30 (1 appearance)
  - Total visit duration: 1.5 hours
  - All pipeline_ids preserved for accurate tracking
```

### Scenario 3: Cross-Pipeline Person Recognition

**Setup:**
- **Store Entrance**: CAMERA-1
- **Checkout Counter**: CAMERA-2
- **Parking Lot**: CAMERA-3

**Person:** Regular customer

**Multiple Visits:**
- Visit 1: Entrance (CAMERA-1) → Identity A
- Visit 2: Checkout (CAMERA-2) → Identity B
- Visit 3: Parking (CAMERA-3) → Identity C

**Merge:**
- All 3 identities merged

**Analytics:**
```
Merged Identity:
  - Total visits: 3
  - Locations: Entrance, Checkout, Parking
  - Customer journey: Entrance → Checkout → Parking
  - Can analyze: "This customer always parks after shopping"
```

---

## Edge Cases and Solutions

### Edge Case 1: Merging UNKNOWN + KNOWN

**Scenario:**
```
Identity A: UNKNOWN, 10 appearances, CAMERA-1
Identity B: KNOWN (display_name="John"), 5 appearances, CAMERA-2
```

**Current Behavior:**
- If Identity B (KNOWN) is target → Result: KNOWN identity with 15 appearances
- If Identity A (UNKNOWN) is target → Result: UNKNOWN identity with 15 appearances ⚠️

**Issue:**
- Merging UNKNOWN into KNOWN should keep KNOWN type
- Merging KNOWN into UNKNOWN should promote to KNOWN

**Best Practice:**
```python
# After merge, check types
if target.type == UNKNOWN and any(source.type == KNOWN for source in sources):
    # Promote target to KNOWN
    target.type = KNOWN
    target.display_name = known_source.display_name  # Use known source's name
```

### Edge Case 2: Identities with No Embeddings

**Scenario:**
```
Identity A: 10 appearances, 0 embeddings (no FAISS entry)
Identity B: 5 appearances, 3 embeddings (in FAISS)
```

**Current Behavior:**
- Identity A's appearances moved to Identity B
- Identity A's embeddings: nothing to move (0 embeddings)
- Identity B keeps its 3 embeddings
- FAISS: Only Identity B's embeddings remain active

**Result:** ✅ Works correctly - no issues

### Edge Case 3: Conflicting Best Snapshots

**Scenario:**
```
Identity A: best_snapshot_path = "/path/to/high_quality.jpg" (quality: 0.95)
Identity B: best_snapshot_path = "/path/to/low_quality.jpg" (quality: 0.60)
Identity C: best_snapshot_path = "/path/to/medium_quality.jpg" (quality: 0.75)
```

**Current Behavior:**
- Target identity keeps its original best_snapshot_path
- Source identities' best_snapshot_path is not evaluated

**Issue:**
- Identity A has better quality snapshot but it's lost

**Best Practice:**
```python
# After merge, compare snapshot qualities
best_quality = target.best_snapshot_quality
for source in sources:
    if source.best_snapshot_quality > best_quality:
        target.best_snapshot_path = source.best_snapshot_path
        target.best_snapshot_quality = source.best_snapshot_quality
```

### Edge Case 4: Circular Merge Prevention

**Scenario:**
```
Identity A merged into Identity B
Identity B merged into Identity C
```

**Current Protection:**
```python
if from_identity.status == IdentityStatus.MERGED:
    raise ValueError(f"Identity {from_identity_id} is already merged")
```

**Result:** ✅ Prevents circular merges

### Edge Case 5: Merging Same Identity Twice

**Scenario:**
- User tries to merge Identity A into Identity B
- Then tries to merge Identity A into Identity C

**Current Protection:**
```python
if from_identity.status == IdentityStatus.MERGED:
    raise ValueError(f"Identity {from_identity_id} is already merged")
```

**Result:** ✅ Prevents duplicate merges

### Edge Case 6: Target Identity Already Merged

**Scenario:**
- Identity A is already merged into Identity B
- User tries to use Identity A as target for new merge

**Current Protection:**
```python
if target_identity.status == IdentityStatus.MERGED:
    raise ValueError(f"Target identity {target_id} is already merged")
```

**Result:** ✅ Prevents using merged identity as target

---

## Performance and Optimization

### Batch Operations

**Efficient Approach (Current):**
```python
# Single UPDATE query for all appearances
await db.execute(
    update(IdentityAppearance)
    .where(IdentityAppearance.identity_id == source_id)
    .values(identity_id=target_id)
)
# Time: O(1) database operation, O(n) rows updated
```

**Inefficient Approach (Avoid):**
```python
# Individual UPDATE for each appearance
for appearance in appearances:
    await db.execute(
        update(IdentityAppearance)
        .where(IdentityAppearance.id == appearance.id)
        .values(identity_id=target_id)
    )
# Time: O(n) database operations
```

### Transaction Safety

**All operations in single transaction:**
```python
async with db.begin():  # Start transaction
    # 1. Update identities
    # 2. Move appearances
    # 3. Move embeddings
    # 4. Update FAISS
    # 5. Create audit logs
    
    await db.commit()  # All succeed or all fail
```

**Benefits:**
- Atomicity: All changes succeed or all fail
- Consistency: Database always in valid state
- No partial merges: Either complete or rolled back

### FAISS Index Updates

**Current Approach:**
```python
# Mark vectors as removed (metadata update only)
identity_index.remove_from_unknown(str(source_id))
# Time: O(1) - just metadata update
# Space: Vectors remain in index (marked inactive)
```

**Alternative (Not Used - Too Expensive):**
```python
# Rebuild entire index
faiss_index.rebuild()  # Remove all merged vectors
# Time: O(n log n) - very expensive for large indexes
# Space: Smaller index, but rebuild cost is high
```

### Query Optimization

**Indexed Columns:**
```sql
-- All these columns are indexed for fast lookups
CREATE INDEX idx_identity_appearances_identity ON identity_appearances(identity_id);
CREATE INDEX idx_identity_appearances_pipeline ON identity_appearances(pipeline_id);
CREATE INDEX idx_identity_embeddings_identity ON identity_embeddings(identity_id);
CREATE INDEX idx_identity_embeddings_pipeline ON identity_embeddings(pipeline_id);
CREATE INDEX idx_identities_status ON identities(status);
CREATE INDEX idx_identities_type_status ON identities(type, status);
```

**Query Performance:**
- Finding appearances: O(log n) with index
- Moving appearances: O(n) rows updated, but single query
- Aggregating pipelines: O(n) with index scan

---

## Best Practices

### 1. Target Selection

**Current:**
- Selects identity with most appearances
- Considers snapshot quality
- Considers age

**Recommended Enhancement:**
```python
# Consider pipeline diversity
score = (appearances_count × 1000) + 
        (has_best_snapshot × 100) + 
        (pipeline_diversity × 50) +  # NEW: Reward multi-pipeline
        (age_in_days × 1)

# Example:
# Identity A: 10 appearances, 1 pipeline → Score: 10,100
# Identity B: 8 appearances, 3 pipelines → Score: 8,250 (better diversity!)
```

### 2. Type Promotion Logic

**Current:**
- Target keeps its original type

**Recommended:**
```python
# If merging UNKNOWN + KNOWN, result should be KNOWN
if target.type == UNKNOWN:
    for source in sources:
        if source.type == KNOWN:
            target.type = KNOWN
            target.display_name = source.display_name
            break
```

### 3. Best Snapshot Selection

**Current:**
- Target keeps its original snapshot

**Recommended:**
```python
# Compare and select best quality snapshot
best_snapshot = target.best_snapshot_path
best_quality = target.best_snapshot_quality or 0.0

for source in sources:
    source_quality = source.best_snapshot_quality or 0.0
    if source_quality > best_quality:
        best_snapshot = source.best_snapshot_path
        best_quality = source_quality

target.best_snapshot_path = best_snapshot
```

### 4. Pipeline Aggregation Display

**Current:**
- Pipeline IDs are preserved but not prominently displayed

**Recommended:**
```python
# Display pipeline distribution
pipeline_stats = {}
for appearance in appearances:
    pipeline = appearance.pipeline_id
    pipeline_stats[pipeline] = pipeline_stats.get(pipeline, 0) + 1

# Display: "Seen in 3 pipelines: CAMERA-1 (10x), CAMERA-2 (8x), CAMERA-3 (12x)"
```

### 5. Merge Confirmation

**Current:**
- Direct merge without confirmation

**Recommended:**
```python
# Show merge preview
preview = {
    "target": target_identity,
    "sources": source_identities,
    "pipeline_distribution": {
        "CAMERA-1": 10,
        "CAMERA-2": 8,
        "CAMERA-3": 12
    },
    "total_appearances": 30,
    "warning": "Merging identities from 3 different pipelines"
}

# Require user confirmation for cross-pipeline merges
if len(pipelines) > 1:
    require_confirmation = True
```

### 6. Audit Trail Enhancement

**Current:**
- Basic merge records

**Recommended:**
```python
# Enhanced audit log
audit_entry = {
    "action": "merge",
    "target_identity": target_id,
    "source_identities": source_ids,
    "pipeline_distribution": pipeline_stats,
    "before_state": {
        "target": {"appearances": 12, "pipelines": ["CAMERA-3"]},
        "sources": [
            {"id": "abc-123", "appearances": 10, "pipelines": ["CAMERA-1"]},
            {"id": "def-456", "appearances": 8, "pipelines": ["CAMERA-2"]}
        ]
    },
    "after_state": {
        "merged": {"appearances": 30, "pipelines": ["CAMERA-1", "CAMERA-2", "CAMERA-3"]}
    },
    "user": user_id,
    "timestamp": datetime.utcnow()
}
```

---

## Summary

### What Works Well ✅

1. **Pipeline Preservation**: Original `pipeline_id` values are perfectly maintained
2. **Data Integrity**: All related data is correctly moved to target
3. **Audit Trail**: Complete history of all merges
4. **Performance**: Efficient batch operations and indexed queries
5. **Visibility**: Merged identity appears on all relevant dashboards
6. **Transaction Safety**: Atomic operations prevent partial merges

### Potential Improvements 🔧

1. **Smart Target Selection**: Consider pipeline diversity, not just count
2. **Best Snapshot**: Compare and update if source has better quality
3. **Type Promotion**: UNKNOWN + KNOWN → should become KNOWN
4. **Pipeline Aggregation UI**: Show "Seen in 3 pipelines" badge
5. **Merge Confirmation**: Warn when merging cross-pipeline identities
6. **Enhanced Audit**: Include pipeline distribution in audit logs

### Key Takeaways

- **Pipeline information is never lost** - each appearance/embedding keeps its original `pipeline_id`
- **Merged identities disappear from unknown page** - they have `status=MERGED` which is filtered out
- **Multi-pipeline visibility works** - merged identity appears on all relevant dashboards
- **FAISS vectors are marked, not deleted** - for performance and history preservation
- **All operations are atomic** - transaction ensures data consistency

---

*Last Updated: 2024-01-04*
*Version: 1.0*

