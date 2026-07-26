# Search by Image - Complete Guide

## Table of Contents
1. [Overview](#overview)
2. [How It Works (Technical Flow)](#how-it-works-technical-flow)
3. [Step-by-Step Usage](#step-by-step-usage)
4. [API Reference](#api-reference)
5. [Understanding Results](#understanding-results)
6. [Best Practices](#best-practices)
7. [Troubleshooting](#troubleshooting)

---

## Overview

**Search by Image** allows you to upload a photo of a person's face and find matching identities in the system. It works like a reverse lookup - you have a photo and want to find who this person is.

### Key Features

| Feature | Description |
|---------|-------------|
| **Dual Index Search** | Searches both KNOWN and UNKNOWN identities |
| **FAISS-Powered** | Uses high-performance vector similarity search |
| **Configurable Scope** | Search known only, unknown only, or both |
| **Date Filtering** | Filter results by date range |
| **Pipeline Filtering** | Filter results by specific camera/pipeline |
| **Similarity Scores** | Returns matches with confidence percentages |

### When to Use

✅ **Use Search by Image when:**
- You have a photo and want to identify the person
- Checking if someone is already in the system before adding them
- Finding duplicates before promoting an unknown identity
- Verifying a person's identity across multiple cameras
- Investigating who was at a location at a specific time

---

## How It Works (Technical Flow)

### Complete Processing Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│                    SEARCH BY IMAGE FLOW                             │
└─────────────────────────────────────────────────────────────────────┘

Step 1: IMAGE UPLOAD
┌──────────────────┐
│  User uploads    │
│  face image      │
│  (JPG/PNG)       │
└────────┬─────────┘
         │
         ▼
Step 2: FACE DETECTION (SCRFD Model)
┌──────────────────────────────────────────────────────────────────┐
│  SCRFD Detector analyzes the image:                              │
│  • Locates face bounding box (x1, y1, x2, y2)                    │
│  • Detects 5 facial landmarks (eyes, nose, mouth corners)        │
│  • Returns: bboxes[], kpss[] (keypoints)                         │
│                                                                  │
│  If no face detected → Return error "No face detected"           │
└────────┬─────────────────────────────────────────────────────────┘
         │
         ▼
Step 3: FACE ALIGNMENT
┌──────────────────────────────────────────────────────────────────┐
│  Align face to standard position:                                │
│  • Use 5 keypoints to calculate transformation matrix            │
│  • Apply affine transformation                                   │
│  • Crop to 112x112 pixels                                        │
│  • Convert BGR → RGB                                             │
│                                                                  │
│  Result: Normalized, aligned face ready for recognition          │
└────────┬─────────────────────────────────────────────────────────┘
         │
         ▼
Step 4: EMBEDDING GENERATION (ArcFace Model)
┌──────────────────────────────────────────────────────────────────┐
│  ArcFace Recognizer generates embedding:                         │
│  • Input: 112x112 RGB aligned face                               │
│  • Process through deep neural network                           │
│  • Output: 512-dimensional float vector                          │
│  • Normalize: embedding / ||embedding||                          │
│                                                                  │
│  Result: [0.023, -0.145, 0.089, ..., 0.034] (512 floats)         │
└────────┬─────────────────────────────────────────────────────────┘
         │
         ▼
Step 5: FAISS SIMILARITY SEARCH
┌──────────────────────────────────────────────────────────────────┐
│  Search FAISS indexes for similar embeddings:                    │
│                                                                  │
│  If scope = "known" or "both":                                   │
│  ├── Search KNOWN index (threshold: 0.4)                         │
│  └── Returns: [(identity_id, similarity), ...]                   │
│                                                                  │
│  If scope = "unknown" or "both":                                 │
│  ├── Search UNKNOWN index (threshold: 0.35)                      │
│  └── Returns: [(identity_id, similarity), ...]                   │
│                                                                  │
│  FAISS uses Inner Product (cosine similarity) for fast matching  │
└────────┬─────────────────────────────────────────────────────────┘
         │
         ▼
Step 6: DATABASE ENRICHMENT
┌──────────────────────────────────────────────────────────────────┐
│  For each matched identity_id:                                   │
│  • Fetch Identity record from PostgreSQL                         │
│  • Get: display_name, type, best_snapshot_path, appearances      │
│                                                                  │
│  Apply optional filters:                                         │
│  • date_from / date_to: Filter by appearance dates               │
│  • pipeline_id: Filter by specific camera                        │
└────────┬─────────────────────────────────────────────────────────┘
         │
         ▼
Step 7: RESPONSE
┌──────────────────────────────────────────────────────────────────┐
│  Return sorted results:                                          │
│  [                                                               │
│    {                                                             │
│      "identity_id": "abc-123-def",                               │
│      "similarity": 0.89,                                         │
│      "type": "known",                                            │
│      "display_name": "John Doe",                                 │
│      "best_snapshot_path": "/storage/CAM1/John/snap.jpg",        │
│      "last_seen_at": "2025-01-05T10:30:00",                      │
│      "appearances_count": 45                                     │
│    },                                                            │
│    ...                                                           │
│  ]                                                               │
└──────────────────────────────────────────────────────────────────┘
```

### Similarity Thresholds

| Index Type | Threshold | Meaning |
|------------|-----------|---------|
| **KNOWN** | 0.4 (40%) | Higher threshold for verified identities |
| **UNKNOWN** | 0.35 (35%) | Lower threshold to catch potential matches |

### Why Different Thresholds?

- **KNOWN identities** have verified, high-quality reference images → require higher confidence
- **UNKNOWN identities** may have lower quality images from surveillance → allow lower confidence to find potential matches

---

## Step-by-Step Usage

### Method 1: Web Interface (Admin Panel)

#### Step 1: Open Search
1. Go to **Admin → Unknown Faces** (`/admin/unknown`)
2. Click the **"SEARCH BY IMAGE"** button (magnifying glass icon)
3. A search modal will appear

#### Step 2: Upload Image
1. Click **"Choose File"** or drag-drop an image
2. Supported formats: **JPG, JPEG, PNG, WEBP**
3. **Important**: Image must contain a clear, visible face

#### Step 3: Configure Search
| Option | Values | Recommendation |
|--------|--------|----------------|
| **Scope** | Known, Unknown, Both | Use "Both" for comprehensive search |
| **Results** | 1-50 | Default: 10 is usually enough |
| **Date From** | Date picker | Optional - filter by start date |
| **Date To** | Date picker | Optional - filter by end date |
| **Pipeline** | Dropdown | Optional - filter by specific camera |

#### Step 4: Execute Search
1. Click **"SEARCH"** button
2. Wait for processing (1-3 seconds)
3. Results appear below

#### Step 5: Review Results
- Results are sorted by **similarity score** (highest first)
- Each result shows:
  - Face thumbnail
  - Name (or "Unknown" + ID)
  - Similarity percentage
  - Type badge (KNOWN/UNKNOWN)
  - Last seen date
  - Appearance count

#### Step 6: Take Action
- Click a result to view full identity details
- From there you can:
  - **Promote** (if unknown)
  - **Merge** (if duplicate found)
  - **View appearances** (timeline)

### Method 2: API (cURL)

```bash
curl -X POST "http://localhost/api/search/by-image" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -F "image=@/path/to/face.jpg" \
  -F "scope=both" \
  -F "top_k=10"
```

### Method 3: API (JavaScript)

```javascript
async function searchByImage(imageFile, options = {}) {
    const formData = new FormData();
    formData.append('image', imageFile);
    formData.append('scope', options.scope || 'both');
    formData.append('top_k', options.topK || 10);
    
    if (options.dateFrom) formData.append('date_from', options.dateFrom);
    if (options.dateTo) formData.append('date_to', options.dateTo);
    if (options.pipelineId) formData.append('pipeline_id', options.pipelineId);
    
    const response = await fetch('/api/search/by-image', {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${localStorage.getItem('access_token')}`
        },
        body: formData
    });
    
    if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'Search failed');
    }
    
    return await response.json();
}

// Usage
const fileInput = document.getElementById('imageInput');
const results = await searchByImage(fileInput.files[0], {
    scope: 'both',
    topK: 10
});
console.log(`Found ${results.length} matches`);
```

### Method 4: API (Python)

```python
import requests

def search_by_image(image_path, token, scope='both', top_k=10, 
                    date_from=None, date_to=None, pipeline_id=None):
    """
    Search for identities by uploading a face image.
    
    Args:
        image_path: Path to the image file
        token: JWT access token
        scope: 'known', 'unknown', or 'both'
        top_k: Number of results to return
        date_from: Optional start date filter (ISO format)
        date_to: Optional end date filter (ISO format)
        pipeline_id: Optional camera/pipeline filter
    
    Returns:
        List of matching identities with similarity scores
    """
    url = "http://localhost/api/search/by-image"
    headers = {
        "Authorization": f"Bearer {token}"
    }
    
    with open(image_path, 'rb') as f:
        files = {'image': f}
        data = {
            'scope': scope,
            'top_k': top_k
        }
        
        if date_from:
            data['date_from'] = date_from
        if date_to:
            data['date_to'] = date_to
        if pipeline_id:
            data['pipeline_id'] = pipeline_id
        
        response = requests.post(url, headers=headers, files=files, data=data)
    
    if response.status_code != 200:
        raise Exception(f"Search failed: {response.json().get('detail', 'Unknown error')}")
    
    return response.json()

# Usage
results = search_by_image(
    image_path='/path/to/face.jpg',
    token='your_access_token',
    scope='both',
    top_k=10
)

for result in results:
    print(f"{result['display_name'] or 'Unknown'}: {result['similarity']*100:.1f}%")
```

---

## API Reference

### Endpoint

```
POST /api/search/by-image
```

### Authentication

**Required**: Bearer token with `admin` role

```
Authorization: Bearer <access_token>
```

### Request

**Content-Type**: `multipart/form-data`

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `image` | File | ✅ Yes | - | Face image file (JPG, PNG, WEBP) |
| `scope` | String | No | `"both"` | Search scope: `"known"`, `"unknown"`, or `"both"` |
| `top_k` | Integer | No | `10` | Number of results to return (1-100) |
| `date_from` | String | No | - | Filter: ISO date string (e.g., `"2025-01-01T00:00:00"`) |
| `date_to` | String | No | - | Filter: ISO date string |
| `pipeline_id` | String | No | - | Filter by specific camera/pipeline ID |

### Response

**Success (200 OK)**:

```json
[
    {
        "identity_id": "550e8400-e29b-41d4-a716-446655440000",
        "type": "known",
        "display_name": "John Doe",
        "similarity": 0.892,
        "best_snapshot_path": "/storage/CAMERA-1/John_Doe/snapshot_001.jpg",
        "last_seen_at": "2025-01-05T10:30:00.000000",
        "appearances_count": 45
    },
    {
        "identity_id": "660e8400-e29b-41d4-a716-446655440001",
        "type": "unknown",
        "display_name": null,
        "similarity": 0.756,
        "best_snapshot_path": "/storage/CAMERA-2/unknown_abc123/snapshot.jpg",
        "last_seen_at": "2025-01-04T15:20:00.000000",
        "appearances_count": 12
    }
]
```

### Error Responses

| Status | Error | Cause |
|--------|-------|-------|
| **400** | `"No face detected in image"` | Image doesn't contain a detectable face |
| **400** | `"Invalid image file"` | Corrupt or unsupported image format |
| **401** | `"Not authenticated"` | Missing or invalid token |
| **403** | `"Admin access required"` | User is not an admin |
| **503** | `"Identity search service not available"` | System not fully initialized |

---

## Understanding Results

### Similarity Scores

| Score Range | Interpretation | Action |
|-------------|----------------|--------|
| **0.90 - 1.00** | Very high match | Almost certainly the same person |
| **0.75 - 0.89** | High match | Very likely the same person |
| **0.60 - 0.74** | Medium match | Possibly the same person - verify manually |
| **0.40 - 0.59** | Low match | Might be similar - check carefully |
| **< 0.40** | Not returned | Below threshold - not shown |

### Result Fields Explained

| Field | Description |
|-------|-------------|
| `identity_id` | Unique identifier (UUID) |
| `type` | `"known"` (named/verified) or `"unknown"` (unidentified) |
| `display_name` | Person's name (null for unknown) |
| `similarity` | Match confidence (0.0 to 1.0) |
| `best_snapshot_path` | Path to best quality image |
| `last_seen_at` | Most recent detection timestamp |
| `appearances_count` | Total number of times detected |

---

## Best Practices

### Image Quality

| ✅ Good | ❌ Bad |
|---------|--------|
| Clear, frontal face | Blurry or low resolution |
| Good lighting | Very dark or overexposed |
| Face fills 30-70% of image | Face too small or too close |
| Single face in image | Multiple faces (uses first detected) |
| Recent photo | Very old photo (appearance may have changed) |

### Search Strategy

1. **Start with "Both" scope** - Gets the most comprehensive results
2. **Use filters sparingly** - Start without filters, add if needed
3. **Check multiple results** - Don't just trust the top match
4. **Verify visually** - Always compare photos before taking action
5. **Consider context** - Same person on different cameras may have lower scores

### Performance Tips

- **Image size**: Keep under 5MB for faster uploads
- **Resolution**: 640x480 to 1920x1080 is ideal
- **Format**: JPG is fastest, PNG for quality
- **top_k**: Use 10-20 for most cases, increase only if needed

---

## Troubleshooting

### "No face detected in image"

**Causes:**
- Face is too small in the image
- Face is at extreme angle (profile)
- Image is too dark or blurry
- Face is partially obscured

**Solutions:**
1. Crop image to focus on face
2. Use a clearer, frontal photo
3. Improve image lighting/contrast
4. Ensure face is fully visible

### "Identity search service not available" (503)

**Causes:**
- System is still initializing
- FAISS indexes not loaded

**Solutions:**
1. Wait 30-60 seconds after system start
2. Check backend logs for errors
3. Verify FAISS indexes exist in `/app/database/identity_indexes/`

### Low similarity scores for known matches

**Causes:**
- Different lighting conditions
- Different angles
- Facial changes (glasses, beard, aging)
- Low quality reference images

**Solutions:**
1. Add multiple reference images per person
2. Use "Unknown" scope to find potential matches
3. Lower expectations - 0.6+ is often a good match

### No results returned

**Causes:**
- Person not in system
- Filters too restrictive
- Wrong scope selected

**Solutions:**
1. Try "Both" scope
2. Remove date/pipeline filters
3. Increase `top_k` to 20-50
4. Verify person should be in system

---

## Related Documentation

- **Unknown Faces Center Guide**: `07_UNKNOWN_FACES_CENTER_COMPLETE_GUIDE.md`
- **Identity API Guide**: `08_IDENTITY_API_FRONTEND_GUIDE.md`
- **FAISS Production Scaling**: `30_FAISS_PRODUCTION_SCALING.md`
- **SCRFD/ArcFace Integration**: `34_SCRFD_ARCFACE_INTEGRATION_PIPELINE.md`

---

**Last Updated:** January 2025  
**API Version:** 5.0.0

