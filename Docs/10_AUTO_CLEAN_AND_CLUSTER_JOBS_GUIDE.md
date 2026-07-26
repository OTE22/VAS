# Auto Clean and Cluster Jobs - Complete Guide

## Overview

Your face recognition system has **two main automated background jobs** that run periodically to maintain data quality and help identify duplicate unknown identities:

1. **Identity Clustering Service** - Finds similar unknown identities and suggests merges
2. **Identity Retention Manager** - Cleans up old data, snapshots, and embeddings
3. **Data Retention Manager** - Cleans up old detections and face images

---

## 🔄 Identity Clustering Service

### Purpose
Automatically finds unknown identities that are likely the same person and creates merge suggestions for admin review.

### How It Works

#### 1. **Initialization** (`backend/core/identity_clustering.py`)
- **Default Interval**: Runs every **24 hours** (configurable)
- **First Run**: Waits 1 hour after application startup
- **Dependencies**: Requires `scikit-learn` or `hdbscan` library (falls back to pattern-based if not available)

#### 2. **Clustering Process**

```python
# Step 1: Find active unknown identities
- Only considers identities seen in last 90 days
- Filters: type='unknown', status='active', last_seen_at >= 90 days ago
- Minimum cluster size: 2 identities (configurable)

# Step 2: Pattern-Based Clustering (Primary Method)
Since FAISS doesn't allow direct vector retrieval, the system uses:
- Appearance pattern similarity
- Pipeline overlap (same cameras)
- Temporal overlap (within 1 hour)
- Similar appearance counts
```

#### 3. **Merge Suggestion Criteria**

A merge suggestion is created when two unknown identities have:
- **Pipeline Overlap**: ≥50% common pipelines (cameras)
- **Appearance Similarity**: Appearance counts within 5 of each other
- **Temporal Overlap**: Appearances within 1 hour of each other
- **OR** Pipeline overlap ≥70% (even without temporal overlap)

#### 4. **Suggestion Creation**

```python
MergeSuggestion {
    cluster_id: "pattern_{identity1_id}_{identity2_id}",
    identity_ids: [identity1_id, identity2_id],
    confidence: 0.5-0.75 (based on overlap ratio),
    status: "pending",
    representative_snapshots: [best_snapshot_path from both identities],
    created_at: current_time
}
```

#### 5. **Configuration**

Located in: `backend/core/identity_clustering.py`

```python
IdentityClusteringService(
    cluster_interval_hours=24,    # Run daily
    min_cluster_size=2,            # Minimum identities per cluster
    eps=0.35,                      # Similarity threshold (not used in pattern-based)
    min_samples=2                  # Minimum samples (not used in pattern-based)
)
```

#### 6. **Lifecycle**

```
Application Startup
    ↓
Wait 1 hour
    ↓
Run clustering
    ↓
Generate merge suggestions
    ↓
Save to database (merge_suggestions table)
    ↓
Wait 24 hours
    ↓
Repeat...
```

#### 7. **Admin Review**

- Suggestions appear in admin interface
- Admin can approve/reject merges
- Approved merges combine identities
- Rejected suggestions are marked as rejected

---

## 🧹 Identity Retention Manager

### Purpose
Automatically cleans up old identity data to prevent database bloat and manage storage.

### How It Works

#### 1. **Initialization** (`backend/core/identity_retention.py`)
- **Default Interval**: Runs every **24 hours** (configurable)
- **First Run**: Waits 1 hour after application startup
- **Retention Policies**:
  - Snapshots: **90 days** (default)
  - Embeddings: **12 months** (default)
  - Inactive threshold: **180 days** (default)

#### 2. **Cleanup Operations**

The retention manager performs **4 main cleanup tasks**:

##### A. **Delete Old Snapshots** (`_cleanup_old_snapshots`)
```python
# What it does:
1. Finds IdentityAppearance records older than 90 days
2. Deletes snapshot image files from disk
3. Clears best_snapshot_path in database
4. Also cleans up Identity.best_snapshot_path if identity is old

# Result:
- Old snapshot files removed from storage
- Database paths cleared
- Storage space freed
```

##### B. **Mark Inactive Identities** (`_mark_inactive_identities`)
```python
# What it does:
1. Finds identities not seen in last 180 days
2. Changes status from 'active' to 'inactive'
3. Preserves all data (just marks as inactive)

# Result:
- Inactive identities filtered out from active searches
- Can still be viewed in admin interface
- Historical data preserved
```

##### C. **Clean Up Excess Embeddings** (`_cleanup_excess_embeddings`)
```python
# What it does:
1. For each active identity:
   - Gets all embeddings ordered by quality (best first)
   - Keeps only top 10 embeddings (configurable)
   - Removes lower quality embeddings
2. Removes embeddings from FAISS index
3. Deletes embedding records from database
4. Saves FAISS index after cleanup

# Result:
- Only best quality embeddings kept
- Database size reduced
- FAISS index optimized
```

##### D. **Clean Up Merged Identities** (Optional)
```python
# What it does:
- Finds identities merged more than 365 days ago
- Currently: Keeps them for audit trail
- Can be configured to archive/delete

# Result:
- Audit trail maintained
- Can be manually cleaned if needed
```

#### 3. **Configuration**

Located in: `backend/core/identity_retention.py`

```python
IdentityRetentionManager(
    snapshot_retention_days=90,        # Keep snapshots for 90 days
    embedding_retention_months=12,     # Keep embeddings for 12 months
    inactive_threshold_days=180,       # Mark inactive after 180 days
    cleanup_interval_hours=24,        # Run daily
    max_embeddings_per_identity=10     # Keep top 10 embeddings per identity
)
```

#### 4. **Lifecycle**

```
Application Startup
    ↓
Wait 1 hour
    ↓
Run cleanup
    ↓
Delete old snapshots
    ↓
Mark inactive identities
    ↓
Clean up excess embeddings
    ↓
Save FAISS index
    ↓
Wait 24 hours
    ↓
Repeat...
```

#### 5. **Safety Features**

- **Transactional**: All operations are database transactions
- **Error Handling**: Errors in one operation don't stop others
- **FAISS Sync**: FAISS index saved after embedding cleanup
- **File Safety**: Checks file existence before deletion
- **Audit Trail**: Merged identities kept for audit

---

## 🗑️ Data Retention Manager

### Purpose
Cleans up old detection records and face images to manage storage space.

### How It Works

#### 1. **Initialization** (`backend/core/data_retention.py`)
- **Default Interval**: Runs every **24 hours** (configurable)
- **First Run**: Waits 60 seconds after application startup
- **Retention Policy**: **90 days** (configurable via `DATA_RETENTION_DAYS`)

#### 2. **Cleanup Operations**

##### A. **Delete Old Detections**
```python
# What it does:
1. Finds Detection records older than retention_days
2. Processes in batches of 1000
3. For each detection:
   - Deletes associated face image files from disk
   - Calculates freed storage space
   - Deletes detection record (cascade deletes faces)
4. Commits transaction

# Result:
- Old detections removed
- Face images deleted
- Storage space freed
```

##### B. **Clean Up Empty Directories**
```python
# What it does:
1. Scans storage directory
2. Finds empty pipeline directories
3. Removes empty directories

# Result:
- Clean storage structure
- No orphaned directories
```

#### 3. **Configuration**

Located in: `backend/config.py` or `config.py`

```python
DATA_RETENTION_DAYS = 90              # Keep detections for 90 days
CLEANUP_INTERVAL_HOURS = 24           # Run daily
```

#### 4. **Storage Statistics**

The manager also provides storage stats:
```python
{
    "total_size_mb": 1024.5,
    "total_size_gb": 1.0,
    "file_count": 5000,
    "max_storage_gb": 100,
    "usage_percent": 1.0
}
```

---

## 📊 Job Coordination

### Startup Sequence (`backend/lifespan.py`)

All jobs are started during application startup:

```python
# Phase 3: Core Services
1. Face Tracker (if enabled)
2. Batch Database Writer
3. Data Retention Manager          ← Starts here
4. Identity Clustering Service      ← Starts here
5. Identity Retention Manager      ← Starts here
6. System Metrics Collector
7. Cache Metrics Updater
```

### Shutdown Sequence

Jobs are stopped gracefully during shutdown:

```python
# Reverse order of startup
1. Workers
2. Metrics Collector
3. Data Retention Manager          ← Stops here
4. Identity Retention Manager      ← Stops here
5. Identity Clustering Service      ← Stops here
6. Identity Index Auto-Save
7. Batch Writer
8. Face Tracker
9. Production Cache
10. Redis Cache
11. Models
12. Database
```

---

## 🔍 Monitoring and Logs

### Log Messages

#### Clustering Service
```
🔄 Starting identity clustering for merge suggestions...
Clustering 50 unknown identities...
Performing pairwise similarity searches for clustering...
Created 5 pattern-based merge suggestions
✅ Clustering completed in 2.34s
```

#### Identity Retention Manager
```
🔄 Starting identity retention cleanup...
✅ Identity cleanup completed: 120 snapshots deleted, 5 identities marked inactive, 45 excess embeddings removed in 1.23s
```

#### Data Retention Manager
```
Starting cleanup of data older than 2025-10-03 12:00:00
Cleanup completed: 500 detections, 1200 files, 250.50 MB freed in 5.67s
```

### Metrics

All cleanup operations emit metrics:
- `metrics_cleanup_operations` - Counter for cleanup runs
- Database query counts
- File deletion counts
- Storage space freed

---

## ⚙️ Configuration Examples

### Change Clustering Interval

```python
# In backend/core/identity_clustering.py
clustering_service = IdentityClusteringService(
    cluster_interval_hours=12  # Run twice daily
)
```

### Change Retention Periods

```python
# In backend/core/identity_retention.py
identity_retention_manager = IdentityRetentionManager(
    snapshot_retention_days=180,      # Keep snapshots for 6 months
    embedding_retention_months=24,    # Keep embeddings for 2 years
    inactive_threshold_days=365,      # Mark inactive after 1 year
    max_embeddings_per_identity=20    # Keep top 20 embeddings
)
```

### Change Data Retention

```python
# In backend/config.py or config.py
DATA_RETENTION_DAYS = 180  # Keep detections for 6 months
CLEANUP_INTERVAL_HOURS = 12  # Run twice daily
```

---

## 🚨 Troubleshooting

### Clustering Not Running

**Symptoms**: No merge suggestions appearing

**Checks**:
1. Check if clustering libraries are installed:
   ```bash
   pip install scikit-learn
   # OR
   pip install hdbscan
   ```
2. Check logs for clustering errors
3. Verify clustering service is enabled in logs:
   ```
   ✅ Identity clustering started (interval: 24 hours)
   ```
4. Check if there are enough unknown identities (minimum 2)

### Retention Not Cleaning Up

**Symptoms**: Old data accumulating, storage growing

**Checks**:
1. Verify retention managers are started:
   ```
   ✅ Identity retention started
   ✅ Data retention started (keep: 90 days)
   ```
2. Check for errors in logs
3. Verify database permissions
4. Check file system permissions for deletion
5. Verify retention periods are set correctly

### High Database Load

**Symptoms**: Database slow during cleanup

**Solutions**:
1. Increase cleanup interval (run less frequently)
2. Reduce batch sizes
3. Run cleanup during off-peak hours
4. Add database indexes on timestamp columns

---

## 📈 Best Practices

1. **Monitor Storage**: Regularly check storage usage
2. **Adjust Retention**: Tune retention periods based on your needs
3. **Review Suggestions**: Regularly review merge suggestions
4. **Backup Before Cleanup**: Ensure backups before major cleanup
5. **Monitor Logs**: Watch for errors in cleanup operations
6. **Test Changes**: Test retention policy changes in staging first

---

## 🔗 Related Files

- `backend/core/identity_clustering.py` - Clustering service
- `backend/core/identity_retention.py` - Identity retention manager
- `backend/core/data_retention.py` - Data retention manager
- `backend/lifespan.py` - Job startup/shutdown
- `db_models.py` - Database models (MergeSuggestion, Identity, etc.)

---

## 📝 Summary

Your system has **three automated background jobs**:

1. **Clustering** (24h): Finds duplicate unknown identities → Creates merge suggestions
2. **Identity Retention** (24h): Cleans old snapshots, marks inactive, limits embeddings
3. **Data Retention** (24h): Deletes old detections and face images

All jobs:
- Run automatically in background
- Start 1 hour after application startup
- Run every 24 hours by default
- Handle errors gracefully
- Log all operations
- Can be configured via code

These jobs work together to keep your database clean, storage manageable, and help identify duplicate unknown faces automatically! 🎯

