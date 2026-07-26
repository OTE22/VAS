# FAISS Index Repair and Synchronization Guide

**Face Recognition Surveillance System**  
**ITDIR-AI DEPARTMENT**

---

## 📋 Table of Contents

1. [Overview](#overview)
2. [Understanding FAISS Indexes](#understanding-faiss-indexes)
3. [Repair System Architecture](#repair-system-architecture)
4. [Configuration Options](#configuration-options)
5. [Repair Strategies by Scale](#repair-strategies-by-scale)
6. [Monitoring and Verification](#monitoring-and-verification)
7. [Troubleshooting](#troubleshooting)
8. [Best Practices](#best-practices)

---

## 🎯 Overview

The FAISS Index Repair and Synchronization system ensures that your FAISS vector indexes stay synchronized with the PostgreSQL database, preventing data corruption and ensuring accurate face recognition.

### Why Repair is Needed

FAISS indexes can become out of sync with the database due to:
- **Orphaned entries**: FAISS vectors that don't have corresponding database records
- **Missing entries**: Database records that don't have FAISS vectors
- **Size mismatches**: FAISS index has more vectors than metadata entries
- **Data corruption**: Indexes corrupted during crashes or improper shutdowns

### Key Features

✅ **Automatic Repair on Startup** - Detects and fixes issues automatically  
✅ **Background Repair** - Periodic repair without blocking the system  
✅ **Efficient for Large Scale** - Optimized for millions of embeddings  
✅ **Lazy Marking** - Fast approach for small mismatches  
✅ **Smart Rebuild** - Only rebuilds when necessary  
✅ **Configurable** - All settings available in Admin UI

---

## 🔍 Understanding FAISS Indexes

### Dual Index System

The system maintains two separate FAISS indexes:

1. **KNOWN Index** (`known_faiss_index.bin`)
   - Contains embeddings for known identities
   - Used for recognizing known persons
   - Typically smaller, more stable

2. **UNKNOWN Index** (`unknown_faiss_index.bin`)
   - Contains embeddings for unknown identities
   - Used for clustering and merge suggestions
   - Grows dynamically as new faces are detected

### Index Components

Each FAISS index consists of:

- **FAISS Index File** (`.bin`): Binary file containing vector data
- **Metadata File** (`.json`): Maps FAISS IDs to identity UUIDs
- **Reverse Mapping**: Maps identity UUIDs to FAISS IDs

### Synchronization Points

The system synchronizes indexes with the database at:

1. **Startup**: Automatic repair after loading known faces
2. **Background**: Periodic repair every 24 hours (configurable)
3. **On-Demand**: When orphaned entries are detected during search
4. **After Operations**: After promotion, merge, or deletion

---

## 🏗️ Repair System Architecture

### Three-Tier Repair Strategy

The repair system uses a smart three-tier approach based on index size and mismatch severity:

#### Tier 1: Lazy Marking (Small Mismatches)

**When Used:**
- Mismatch < 1% of index size OR < 100 entries
- Example: 1M vectors with 50 orphaned entries

**How It Works:**
- Marks orphaned vectors in metadata
- Skips them during search operations
- No index rebuild required
- **Performance**: O(1) - No impact

**Benefits:**
- ✅ Instant repair
- ✅ No downtime
- ✅ No memory overhead
- ✅ Perfect for large scale

#### Tier 2: Immediate Rebuild (Medium Indexes)

**When Used:**
- Index size < 50,000 vectors
- Mismatch ≥ 1% or ≥ 100 entries

**How It Works:**
- Rebuilds index from database embeddings
- Reconstructs valid vectors from current index
- Creates new clean index
- Updates metadata and database records

**Performance:**
- ✅ Acceptable startup delay (1-5 seconds)
- ✅ Ensures clean state from start
- ✅ One-time operation

#### Tier 3: Background Rebuild (Large Indexes)

**When Used:**
- Index size ≥ 50,000 vectors
- Mismatch ≥ 1% or ≥ 100 entries

**How It Works:**
- Schedules rebuild in background queue
- System continues operating with old index
- Rebuilds without blocking
- Switches to new index when ready

**Performance:**
- ✅ Zero startup delay
- ✅ No runtime impact
- ✅ Automatic completion

### Repair Process Flow

```
Startup
  ↓
Load FAISS Indexes
  ↓
Load Known Faces from assets/faces
  ↓
Verify Indexes (check counts)
  ↓
Repair Orphaned Entries
  ├─→ Check identity IDs against database
  ├─→ Check FAISS IDs against database
  └─→ Detect size mismatches
       ├─→ Small mismatch? → Lazy Marking
       ├─→ Medium index? → Immediate Rebuild
       └─→ Large index? → Background Rebuild
  ↓
Save Repaired Indexes
  ↓
Re-verify (confirm fix)
```

---

## ⚙️ Configuration Options

### Settings Available in Admin UI

Navigate to **Admin → Settings** and filter by **"identity"** category to find:

#### 1. REPAIR_FAISS_ON_STARTUP

**Type:** Boolean  
**Default:** `true`  
**Description:** Enable/disable FAISS index repair on application startup.

**When to Disable:**
- Very large indexes (> 1M vectors) where startup time is critical
- When you prefer background repair only
- For faster startup in development

**Recommendation:** Keep enabled for most deployments. Only disable if you have > 1M vectors and startup time is critical.

#### 2. REPAIR_FAISS_INTERVAL_HOURS

**Type:** Integer  
**Default:** `24` (hours)  
**Description:** Background repair interval in hours. FAISS indexes are automatically repaired in the background to remove orphaned entries.

**Recommendations:**
- **Small deployments** (< 10k vectors): 12 hours
- **Medium deployments** (10k-100k vectors): 24 hours (default)
- **Large deployments** (> 100k vectors): 48 hours
- **Set to 0** to disable background repair

**Note:** Background repair runs 1 hour after startup, then at the specified interval.

### Environment Variables

You can also configure via `.env` file:

```bash
# Enable/disable repair on startup
REPAIR_FAISS_ON_STARTUP=true

# Background repair interval (hours)
REPAIR_FAISS_INTERVAL_HOURS=24
```

---

## 📊 Repair Strategies by Scale

### Small Scale (< 10,000 vectors)

**Characteristics:**
- Fast startup (< 1 second)
- Quick repair (< 1 second)
- Low memory usage

**Recommended Settings:**
```bash
REPAIR_FAISS_ON_STARTUP=true
REPAIR_FAISS_INTERVAL_HOURS=12
```

**Strategy:**
- Immediate rebuild for any mismatch
- Fast and ensures clean state

### Medium Scale (10,000 - 100,000 vectors)

**Characteristics:**
- Moderate startup time (1-5 seconds)
- Repair time: 1-10 seconds
- Acceptable memory usage

**Recommended Settings:**
```bash
REPAIR_FAISS_ON_STARTUP=true
REPAIR_FAISS_INTERVAL_HOURS=24
```

**Strategy:**
- Lazy marking for small mismatches (< 1%)
- Immediate rebuild for larger mismatches
- Background repair for very large mismatches

### Large Scale (100,000 - 1,000,000 vectors)

**Characteristics:**
- Startup time: 5-30 seconds
- Repair time: 10-60 seconds
- Higher memory usage

**Recommended Settings:**
```bash
REPAIR_FAISS_ON_STARTUP=true  # Or false for faster startup
REPAIR_FAISS_INTERVAL_HOURS=24
```

**Strategy:**
- Lazy marking for small mismatches
- Background rebuild for large mismatches
- Non-blocking operations

### Very Large Scale (> 1,000,000 vectors)

**Characteristics:**
- Startup time: 30+ seconds
- Repair time: 1-10 minutes
- High memory usage

**Recommended Settings:**
```bash
REPAIR_FAISS_ON_STARTUP=false  # Disable for faster startup
REPAIR_FAISS_INTERVAL_HOURS=48
```

**Strategy:**
- Always use lazy marking for small mismatches
- Always use background rebuild for large mismatches
- Never block startup

---

## 📈 Monitoring and Verification

### Startup Logs

When the system starts, you'll see repair logs like:

```
🔧 Repairing orphaned FAISS entries (efficient mode)...
[FAISS_REPAIR] No orphaned entries found. All 9 KNOWN and 0 UNKNOWN entries are valid.
[FAISS_REPAIR] All 9 KNOWN embeddings have valid database records
✅ No orphaned entries found - indexes are clean
```

### Index Verification

After repair, the system verifies indexes:

```
📊 Index Verification AFTER Repair:
   KNOWN: FAISS=9, DB=9, Match=True
   UNKNOWN: FAISS=0, DB=0, Match=True
```

### What to Look For

**✅ Healthy System:**
```
KNOWN: FAISS=1000, DB=1000, Match=True
```

**⚠️ Small Mismatch (Auto-Fixed):**
```
KNOWN: FAISS=1005, DB=1000, Match=False
[FAISS_REPAIR] Lazy-marked 5 orphaned vectors
```

**❌ Large Mismatch (Rebuild Required):**
```
KNOWN: FAISS=1200, DB=1000, Match=False
[FAISS_REPAIR] Rebuilding index from database...
[FAISS_REPAIR] ✅ Successfully rebuilt KNOWN index
```

### Monitoring via API

You can check index status via the identity service API:

```bash
GET /api/admin/identities/verify-indexes
```

Returns:
```json
{
  "known_index": {
    "faiss_count": 1000,
    "database_count": 1000,
    "match": true,
    "issues": []
  },
  "unknown_index": {
    "faiss_count": 500,
    "database_count": 500,
    "match": true,
    "issues": []
  }
}
```

---

## 🔧 Troubleshooting

### Issue: FAISS count != Database count

**Symptoms:**
```
KNOWN: FAISS=18, DB=9, Match=False
```

**Causes:**
1. Orphaned vectors in FAISS (no database records)
2. Duplicate entries from previous loads
3. Index corruption

**Solution:**
The repair system should automatically fix this. If it persists:

1. **Check repair logs** - Look for repair messages
2. **Verify settings** - Ensure `REPAIR_FAISS_ON_STARTUP=true`
3. **Manual repair** - Restart the application to trigger repair
4. **Force rebuild** - If needed, delete index files and reload known faces

### Issue: Repair Taking Too Long

**Symptoms:**
- Startup takes minutes
- System appears frozen

**Causes:**
- Very large index (> 1M vectors)
- Rebuilding entire index synchronously

**Solution:**
1. **Disable startup repair:**
   ```bash
   REPAIR_FAISS_ON_STARTUP=false
   ```
2. **Rely on background repair** - Runs automatically every 24 hours
3. **Use lazy marking** - For small mismatches, this is instant

### Issue: Background Rebuild Not Completing

**Symptoms:**
- Rebuild scheduled but never completes
- Index mismatch persists

**Causes:**
- Database connection issues
- Insufficient memory
- Index file corruption

**Solution:**
1. **Check logs** - Look for rebuild errors
2. **Verify database** - Ensure database is accessible
3. **Check memory** - Ensure sufficient RAM available
4. **Manual rebuild** - Delete index files and restart

### Issue: Known Faces Not Recognized

**Symptoms:**
- Known faces appear as unknown
- FAISS search returns no matches

**Causes:**
1. Known faces not loaded into FAISS index
2. Index corruption
3. Embedding mismatch

**Solution:**
1. **Verify known faces loaded:**
   ```
   ✅ Loaded 9 known faces from assets/faces
   ```
2. **Check index size:**
   ```
   KNOWN index: 9 vectors, 9 identities
   ```
3. **Reload known faces:**
   - Delete index files
   - Restart application
   - Known faces will be reloaded automatically

---

## 💡 Best Practices

### 1. Regular Monitoring

**Check indexes weekly:**
- Review startup logs for repair messages
- Verify index counts match database counts
- Monitor for increasing mismatches

### 2. Configuration Tuning

**For Production:**
```bash
# Enable startup repair for data integrity
REPAIR_FAISS_ON_STARTUP=true

# Run background repair daily
REPAIR_FAISS_INTERVAL_HOURS=24
```

**For Development:**
```bash
# Disable for faster iteration
REPAIR_FAISS_ON_STARTUP=false

# Less frequent background repair
REPAIR_FAISS_INTERVAL_HOURS=48
```

### 3. Backup Strategy

**Before major operations:**
- Backup FAISS index files
- Backup metadata JSON files
- Backup database

**Index files location:**
```
./database/identity_indexes/
  ├── known_faiss_index.bin
  ├── known_metadata.json
  ├── unknown_faiss_index.bin
  └── unknown_metadata.json
```

### 4. Large Scale Deployment

**For 1M+ vectors:**
1. **Disable startup repair** - Faster startup
2. **Use background repair** - Non-blocking
3. **Monitor repair queue** - Check logs for scheduled rebuilds
4. **Plan maintenance windows** - For manual rebuilds if needed

### 5. Adding New Known Faces

**Best Practice:**
1. Add images to `assets/faces/` directory
2. Restart application (faces auto-load)
3. Verify in logs: `✅ Loaded X known faces`
4. Check index verification: `Match=True`

**After Adding:**
- Indexes are automatically saved
- Repair runs automatically
- No manual intervention needed

### 6. Handling Index Corruption

**If indexes are corrupted:**
1. **Stop the application**
2. **Backup current indexes** (for recovery)
3. **Delete index files:**
   ```bash
   rm database/identity_indexes/*.bin
   rm database/identity_indexes/*.json
   ```
4. **Restart application** - Indexes will be rebuilt from database
5. **Reload known faces** - They'll be re-indexed automatically

---

## 🔄 Repair Operations Explained

### 1. Orphaned Identity Removal

**What it does:**
- Finds FAISS entries for identities that don't exist in database
- Removes them from metadata
- Keeps FAISS vectors (lazy marking approach)

**When it runs:**
- On startup (if enabled)
- In background (periodic)
- During search (on-demand)

### 2. Orphaned Embedding Removal

**What it does:**
- Finds FAISS vectors that don't have database records
- Marks them as orphaned
- Skips them during search

**When it runs:**
- On startup (if enabled)
- In background (periodic)

### 3. Index Rebuild

**What it does:**
- Reconstructs valid embeddings from current index
- Creates new clean index
- Updates all metadata and database records

**When it runs:**
- On startup (for medium indexes)
- In background (for large indexes)
- When size mismatch is detected

---

## 📝 Summary

The FAISS Repair and Synchronization system ensures:

✅ **Data Integrity** - Indexes stay synchronized with database  
✅ **Performance** - Efficient strategies for all scales  
✅ **Reliability** - Automatic detection and repair  
✅ **Scalability** - Works with millions of vectors  
✅ **Flexibility** - Configurable via Admin UI  

**Key Takeaways:**

1. **Small mismatches** (< 1%) are handled instantly with lazy marking
2. **Medium indexes** (< 50k) are rebuilt immediately
3. **Large indexes** (> 50k) are rebuilt in background
4. **All operations** are automatic and non-blocking
5. **Configuration** is available in Admin → Settings

---

## 🔗 Related Documentation

- **30_FAISS_PRODUCTION_SCALING.md** - FAISS scaling for production
- **31_DYNAMIC_PROMOTION_FLOW.md** - How promotion works
- **24_SETTINGS_MANAGEMENT_GUIDE.md** - Settings management
- **16_PERSISTENCE_STATUS.md** - Data persistence

---

**Last Updated:** January 2025  
**Version:** 1.0.0

