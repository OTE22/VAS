# Pipeline-Aware ML Clustering for Merge Suggestions

## Overview

The **Pipeline-Aware ML Clustering** system is an advanced merge suggestion workflow that uses machine learning algorithms to generate merge suggestions based on:

1. **User's accessible pipelines** - Only suggests merges for identities visible to the user
2. **Pipeline-specific embeddings** - Uses embeddings grouped by pipeline for better accuracy
3. **Weighted similarity scoring** - Combines pipeline overlap and embedding similarity
4. **Cross-pipeline detection** - Finds duplicate identities across different cameras/pipelines

## How It Works

### 1. Pipeline Filtering

The system first filters identities based on the user's accessible pipelines:

```python
# Admin users: See all pipelines
# Regular users: Only see identities from their assigned pipelines
user_pipelines = await AuthService.get_user_pipelines(user_id, db)
```

### 2. Feature Extraction

For each identity, the system builds **pipeline-aware feature vectors**:

- **Pipeline-specific embeddings**: Groups embeddings by pipeline_id
- **Representative embeddings**: Selects best quality embedding per pipeline
- **Global representative**: Best overall embedding across all pipelines
- **Quality scores**: Tracks embedding quality per pipeline

```python
@dataclass
class IdentityPipelineFeatures:
    identity_id: str
    pipeline_embeddings: Dict[str, PipelineEmbeddingData]  # pipeline_id -> data
    all_pipelines: Set[str]
    total_appearances: int
    avg_embedding_quality: float
    global_representative_embedding: np.ndarray
```

### 3. ML-Based Similarity Calculation

The similarity score combines:

1. **Pipeline Overlap (30% weight)**: How many pipelines are shared
2. **Embedding Similarity (70% weight)**: FAISS cosine similarity between embeddings

**For same-pipeline matches:**
```python
similarity = (embedding_sim * 0.7) + (pipeline_overlap * 0.3)
threshold = 0.35
```

**For cross-pipeline matches:**
```python
similarity = (embedding_sim * 0.9) + (pipeline_overlap * 0.1)
threshold = 0.50  # Stricter threshold
```

### 4. Quality-Weighted Similarity

Embeddings are weighted by quality:

```python
# Use best quality embeddings from common pipelines
for pipeline_id in common_pipelines:
    emb1 = features1.pipeline_embeddings[pipeline_id].representative_embedding
    emb2 = features2.pipeline_embeddings[pipeline_id].representative_embedding
    similarity = np.dot(emb1, emb2)
    quality_weight = (avg_quality1 + avg_quality2) / 2.0
```

## API Usage

### Generate Pipeline-Aware Suggestions

**Endpoint:** `POST /api/admin/merge-suggestions/generate-pipeline-aware`

**Authentication:** Admin only

**Request:**
```bash
curl -X POST "http://localhost:8000/api/admin/merge-suggestions/generate-pipeline-aware" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Response:**
```json
{
  "message": "Pipeline-aware merge suggestions generated successfully",
  "suggestions_created": 15,
  "user_pipelines": "all"  // or list of pipeline IDs for regular users
}
```

### Get Merge Suggestions

The existing endpoint automatically filters by user's pipelines:

**Endpoint:** `GET /api/admin/merge-suggestions`

**Request:**
```bash
curl "http://localhost:8000/api/admin/merge-suggestions" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Response:**
```json
[
  {
    "id": 123,
    "cluster_id": "pipeline_ml_abc123_def456",
    "identity_ids": ["uuid1", "uuid2"],
    "confidence": 0.85,
    "confidence_percent": 85.0,
    "status": "pending",
    "is_cross_camera": false,
    "pipelines": ["pipeline1", "pipeline2"],
    "representative_snapshots": ["/storage/...", "/storage/..."],
    "created_at": "2026-01-05T12:00:00Z"
  }
]
```

## Configuration

Add these variables to `config.py`:

```python
# Pipeline-Aware ML Clustering
PIPELINE_AWARE_CLUSTERING_ENABLED: bool = True
PIPELINE_SIMILARITY_WEIGHT: float = 0.3  # Weight for pipeline overlap (0.0-1.0)
EMBEDDING_SIMILARITY_WEIGHT: float = 0.7  # Weight for embedding similarity (0.0-1.0)
CROSS_PIPELINE_SIMILARITY_THRESHOLD: float = 0.50  # Minimum similarity for cross-pipeline matches
```

## Benefits

### 1. **User-Specific Suggestions**
- Regular users only see suggestions for identities they can access
- Reduces noise and improves relevance

### 2. **Pipeline Context Awareness**
- Uses pipeline-specific embeddings for better accuracy
- Considers pipeline overlap as a similarity signal

### 3. **Quality-Weighted Matching**
- Prioritizes high-quality embeddings
- Reduces false positives from low-quality images

### 4. **Cross-Pipeline Detection**
- Finds duplicates across different cameras
- Uses stricter thresholds to maintain accuracy

## Workflow Example

```
1. User requests merge suggestions
   ↓
2. System gets user's accessible pipelines: ["CAM-01", "CAM-02"]
   ↓
3. Filters identities appearing in these pipelines
   ↓
4. Builds pipeline-aware features:
   - Identity A: {CAM-01: [emb1, emb2], CAM-02: [emb3]}
   - Identity B: {CAM-01: [emb4], CAM-02: [emb5, emb6]}
   ↓
5. Calculates similarity:
   - Common pipelines: [CAM-01, CAM-02] → overlap = 1.0
   - Embedding similarity: 0.82 (from best embeddings)
   - Combined: (0.82 * 0.7) + (1.0 * 0.3) = 0.874
   ↓
6. Creates suggestion (confidence: 0.874 > 0.35 threshold)
```

## Integration with Existing System

The pipeline-aware clustering **complements** the existing clustering system:

- **Existing system**: Runs periodically, finds all duplicates
- **Pipeline-aware system**: On-demand, user-specific, ML-enhanced

Both systems can run simultaneously. The pipeline-aware system is ideal for:
- User-specific dashboards
- On-demand suggestion generation
- Pipeline-restricted environments

## Technical Details

### Embedding Retrieval

Embeddings are retrieved from FAISS index:

```python
# Reconstruct embedding from FAISS
vector = identity_index.unknown_index.reconstruct(faiss_id)
vector = vector / np.linalg.norm(vector)  # Normalize
```

### Similarity Calculation

```python
# Cosine similarity (dot product of normalized vectors)
similarity = np.dot(emb1, emb2)

# Quality-weighted average
weighted_sim = sum(sim * quality for sim, quality in similarities) / total_weight
```

### Pipeline Overlap

```python
common_pipelines = features1.all_pipelines & features2.all_pipelines
all_pipelines = features1.all_pipelines | features2.all_pipelines
overlap_ratio = len(common_pipelines) / len(all_pipelines)
```

## Best Practices

1. **Run periodically**: Generate suggestions daily or weekly
2. **Review thresholds**: Adjust `CROSS_PIPELINE_SIMILARITY_THRESHOLD` based on accuracy needs
3. **Monitor quality**: Check embedding quality scores in logs
4. **User access**: Ensure users have correct pipeline assignments

## Troubleshooting

### No suggestions generated
- Check if user has pipeline access
- Verify identities exist in accessible pipelines
- Check embedding quality (low quality = fewer matches)

### Too many false positives
- Increase `CROSS_PIPELINE_SIMILARITY_THRESHOLD`
- Increase `EMBEDDING_SIMILARITY_WEIGHT`
- Decrease `PIPELINE_SIMILARITY_WEIGHT`

### Too few suggestions
- Decrease similarity thresholds
- Check if embeddings exist for identities
- Verify pipeline assignments

## Future Enhancements

Potential improvements:
- **Deep learning models**: Train custom similarity models
- **Temporal patterns**: Consider time-based features
- **Image quality features**: Use additional image metrics
- **Active learning**: Learn from user feedback

