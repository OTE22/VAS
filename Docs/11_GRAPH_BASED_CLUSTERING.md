# Graph-Based Clustering for Merge Suggestions

## Overview

The system now uses **graph-based clustering** as the primary method for finding merge suggestions, with the hybrid approach as a reliable fallback. This advanced technique automatically finds clusters of 3+ identities that should be merged together.

---

## How It Works - Simple Explanation

### The Problem
When the same person appears on different cameras or at different times, the system creates separate identities:
- Identity A (Camera 1)
- Identity B (Camera 2) 
- Identity C (Camera 3)

All three are actually the same person, but the system doesn't know that yet.

### The Solution: Graph-Based Clustering

Think of identities as **people at a party**, and connections as **"they know each other"**:

1. **Build Connections**: Check if Identity A is similar to Identity B
   - If yes → draw a line between them (they're connected)
   - If no → no line

2. **Find Groups**: Look for groups where everyone is connected to everyone else
   - If A↔B and B↔C, then A, B, C are all in the same group
   - This group = a cluster of identities that should be merged

3. **Create Suggestion**: Suggest merging all identities in the cluster

---

## Step-by-Step Process

### Step 1: Build Similarity Graph

**What happens:**
- System checks every pair of identities
- For each pair, asks: "Are these two identities similar?"

**How it decides:**
1. **Pattern Check (Fast)**: 
   - Do they appear in same cameras? (≥50% overlap)
   - Do they have similar appearance counts? (difference ≤5)
   - Do they appear at similar times? (within 1 hour)

2. **Face Verification (Accurate)**:
   - If pattern matches → verify with FAISS face recognition
   - Face similarity must be ≥35% (same threshold as hybrid)

3. **Add Edge**:
   - If both pattern AND face match → add connection (edge) in graph
   - Connection is bidirectional (A↔B means B↔A)

**Result**: A graph where identities are nodes, and edges connect similar identities.

**Example:**
```
Identity A ↔ Identity B (similar)
Identity B ↔ Identity C (similar)  
Identity C ↔ Identity D (similar)
Identity E ↔ Identity F (similar)
Identity G (no connections)
```

### Step 2: Find Connected Components (Clusters)

**What happens:**
- System finds all groups of identities that are connected to each other
- Uses Depth-First Search (DFS) algorithm to traverse the graph

**How it works:**
1. Start at any identity
2. Follow all connections to find all connected identities
3. This group = one cluster
4. Repeat for all identities

**Result**: List of clusters (groups of identities)

**Example from above:**
- **Cluster 1**: [A, B, C, D] - all connected
- **Cluster 2**: [E, F] - connected to each other
- **Cluster 3**: [G] - single identity (not a cluster, skip)

### Step 3: Create Merge Suggestions

**What happens:**
- For each cluster with 2+ identities, create a merge suggestion
- Calculate confidence score for the cluster
- Save suggestion to database

**Confidence Calculation:**
1. Check all pairs within the cluster
2. Calculate average face similarity
3. Add bonus for larger clusters (more identities = more confidence)
4. Formula: `avg_similarity + cluster_size_bonus` (capped at 95%)

**Example:**
- Cluster [A, B, C, D] has 4 identities
- Average similarity: 0.72 (72%)
- Cluster size bonus: +0.04 (4 identities)
- Final confidence: 76%

---

## Benefits Over Pair-Wise Approach

### 1. Finds Larger Clusters Automatically
- **Pair-wise**: Finds A=B, B=C, C=D separately (3 suggestions)
- **Graph-based**: Finds A=B=C=D in one suggestion (1 suggestion)

### 2. Handles Transitive Relationships
- If A=B and B=C, automatically finds A=B=C
- No need to manually merge multiple times

### 3. More Efficient
- One suggestion for 5 identities instead of 10 pair-wise suggestions
- Less work for admins to review

### 4. Better Confidence Scores
- Larger clusters get confidence boost
- More identities agreeing = more likely to be correct

---

## Fallback Strategy

The system uses a **smart fallback approach**:

```
1. Try graph-based clustering first
   ↓
2. If successful → also run hybrid for any pairs not in clusters
   ↓
3. If graph-based fails or finds nothing → fallback to hybrid approach
   ↓
4. If hybrid fails → fallback to pattern-based only
```

**Why this works:**
- Graph-based finds large clusters (3+ identities)
- Hybrid finds pairs that aren't in clusters
- Pattern-based is last resort (no FAISS available)

---

## Real-World Example

### Scenario
Same person detected on 5 different cameras:
- Identity A: Camera 1, 10 appearances
- Identity B: Camera 2, 8 appearances
- Identity C: Camera 3, 12 appearances
- Identity D: Camera 1, 9 appearances
- Identity E: Camera 2, 11 appearances

### Graph Building
System checks all pairs:
- A↔B: Similar (same person) ✅
- A↔C: Similar ✅
- B↔D: Similar ✅
- C↔E: Similar ✅
- D↔E: Similar ✅

**Graph:**
```
A ── B ── D
│    │    │
C ── E ───┘
```

### Cluster Finding
DFS finds one connected component: [A, B, C, D, E]

### Result
**One merge suggestion created:**
- Cluster: [A, B, C, D, E]
- Confidence: 78%
- Action: Merge all 5 identities into one

**Instead of:**
- 10 separate pair-wise suggestions (A=B, A=C, B=D, etc.)
- Manual work to merge them all

---

## Technical Details

### Algorithm: Depth-First Search (DFS)

```python
def dfs(identity_id, cluster):
    if identity_id already visited:
        return
    mark as visited
    add to cluster
    for each neighbor:
        dfs(neighbor, cluster)
```

**Time Complexity**: O(V + E) where:
- V = number of identities (nodes)
- E = number of similarity edges

**Space Complexity**: O(V) for visited set and graph storage

### Graph Structure

```python
similarity_graph = {
    "identity_A": {"identity_B", "identity_C"},
    "identity_B": {"identity_A", "identity_D"},
    "identity_C": {"identity_A"},
    "identity_D": {"identity_B"}
}
```

- **Key**: Identity ID (string)
- **Value**: Set of similar identity IDs

### Confidence Scoring

```python
# Average similarity within cluster
avg_similarity = sum(all_pair_similarities) / number_of_pairs

# Cluster size bonus (larger = more confident)
cluster_size_bonus = min(0.1, (cluster_size - 2) * 0.02)

# Final confidence
confidence = min(0.95, avg_similarity + cluster_size_bonus)
```

**Example:**
- Cluster of 4 identities
- Average similarity: 0.70
- Cluster bonus: 0.04
- Final: 0.74 (74%)

---

## Configuration

No additional configuration needed! The system automatically:
- Tries graph-based first
- Falls back to hybrid if needed
- Uses same thresholds as hybrid approach

**Thresholds (same as hybrid):**
- Pipeline overlap: ≥50%
- Appearance count difference: ≤5
- Temporal overlap: within 1 hour
- Face similarity: ≥35%

---

## Logging

The system logs detailed information:

```
🔗 Starting graph-based clustering for 50 identities...
Step 1: Building similarity graph (checking identity pairs)...
Step 1 complete: Graph built with 23 edges from 1225 pairs checked
Step 2: Finding connected components (clusters)...
Step 2 complete: Found 5 clusters
Step 3: Creating merge suggestions for clusters...
✅ Graph-based clustering complete: 5 clusters found → 5 suggestions created
```

---

## Performance

**Typical Performance:**
- 50 identities: ~2-5 seconds
- 100 identities: ~5-10 seconds
- 500 identities: ~30-60 seconds

**Optimizations:**
- Pattern-based pre-filtering (fast check before expensive FAISS)
- Only checks pairs that pass pattern criteria
- DFS is efficient (O(V+E))

---

## When to Use Each Approach

### Graph-Based (Primary)
✅ **Use when:**
- You have many identities (50+)
- You expect clusters of 3+ identities
- You want to find large groups automatically

### Hybrid (Fallback)
✅ **Use when:**
- Graph-based finds nothing
- You want to catch pairs not in clusters
- Graph-based fails (error handling)

### Pattern-Based (Last Resort)
✅ **Use when:**
- FAISS index not available
- Need basic suggestions without face verification

---

## Summary

**Graph-based clustering** is a powerful technique that:
1. Finds clusters of 3+ identities automatically
2. Handles transitive relationships (A=B, B=C → A=B=C)
3. Creates fewer, better suggestions
4. Falls back gracefully if it fails

**The system now:**
- Tries graph-based first (finds large clusters)
- Falls back to hybrid (finds pairs)
- Falls back to pattern-based (basic suggestions)

**Result**: Better merge suggestions with less manual work! 🎯

