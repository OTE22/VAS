# Multiple Images Per Person - How Detection Works

## Overview

When each person has multiple images (from different angles, lighting conditions, etc.), the face recognition system automatically finds the **best match** among all available embeddings for that person.

## How It Works

### 1. **Storage Structure**
```
faces/
  ├── John_Doe/
  │   ├── image1.jpg  (front-facing, good lighting)
  │   ├── image2.jpg  (side angle)
  │   ├── image3.jpg  (different lighting)
  │   └── image4.jpg  (profile view)
  └── Jane_Smith/
      ├── image1.jpg
      └── image2.jpg
```

Each image gets its own embedding stored in the database:
- `John_Doe/image1.jpg` → Embedding #1
- `John_Doe/image2.jpg` → Embedding #2
- `John_Doe/image3.jpg` → Embedding #3
- `John_Doe/image4.jpg` → Embedding #4

All embeddings are linked to the same Identity record (same `identity_id`).

### 2. **Detection Process**

When a face is detected in a live camera feed:

#### Step 1: Generate Embedding
- SCRFD detects the face
- ArcFace generates a 512-dim embedding from the detected face

#### Step 2: Search All Embeddings
The system searches **ALL embeddings** in the database, not just one per person:

```sql
SELECT 
    ie.identity_id,
    1 - (ie.embedding <=> query_embedding) as similarity,
    ie.quality
FROM identity_embeddings ie
JOIN identities i ON ie.identity_id = i.id
WHERE 
    i.type = 'KNOWN'
    AND 1 - (ie.embedding <=> query_embedding) >= threshold
ORDER BY ie.embedding <=> query_embedding  -- Best match first
LIMIT top_k
```

#### Step 3: Find Best Match
- The query searches through **ALL embeddings** (including all images for each person)
- Returns the **best similarity score** among all matches
- If `John_Doe` has 4 images, the system compares against all 4 embeddings
- Returns the **highest similarity** from any of those 4 embeddings

### 3. **Example Scenario**

**Person: John Doe**
- Has 4 images: front, side, profile, different lighting
- Each image has an embedding stored in database

**Live Detection:**
- Camera captures John from a side angle
- System generates embedding from live feed
- Searches database and finds:
  - `image1.jpg` (front): similarity = 0.75
  - `image2.jpg` (side): similarity = 0.92 ← **BEST MATCH**
  - `image3.jpg` (profile): similarity = 0.68
  - `image4.jpg` (lighting): similarity = 0.71

**Result:**
- System recognizes as "John Doe" with similarity = 0.92
- Uses the best match from `image2.jpg` (side angle)
- This is why multiple images improve accuracy!

## Benefits of Multiple Images

### ✅ **Better Recognition Accuracy**
- Different angles/lighting conditions are covered
- System finds the closest match from any available image
- Reduces false negatives (missed recognitions)

### ✅ **Handles Variations**
- **Angles**: Front, side, profile views
- **Lighting**: Bright, dim, shadow conditions
- **Expressions**: Smiling, neutral, serious
- **Accessories**: With/without glasses, hats, etc.

### ✅ **Automatic Best Match Selection**
- No manual selection needed
- System automatically uses the best matching embedding
- Works seamlessly with existing detection pipeline

## Technical Details

### pgvector Search
- Searches all embeddings using HNSW index (fast approximate search)
- Returns top_k results sorted by similarity
- Each result includes: `(identity_id, similarity_score)`
- System takes the best match (highest similarity)

### FAISS Search
- Similar behavior: searches all embeddings in the index
- Returns best matches sorted by similarity
- Multiple embeddings per identity are all indexed

### Database Structure
```sql
identity_embeddings table:
- id
- identity_id (links to identities table)
- embedding (vector type, 512 dimensions)
- quality (score 0-1)
- faiss_index_type ('known' or 'unknown')
```

Multiple rows can have the same `identity_id` (one per image).

## Best Practices

### 1. **Upload Multiple Angles**
- Front-facing (most important)
- Side angles (left/right)
- Profile views
- Different lighting conditions

### 2. **Quality Matters**
- Use clear, high-quality images
- Good lighting
- Face clearly visible
- Avoid blurry or low-resolution images

### 3. **Cover Variations**
- Different expressions
- With/without accessories
- Different times of day (if applicable)
- Different clothing (if needed)

### 4. **Don't Overdo It**
- 3-10 images per person is usually sufficient
- Too many similar images don't add much value
- Focus on variety (angles, lighting, expressions)

## Summary

**The system automatically handles multiple images per person:**
- ✅ All embeddings are searched during detection
- ✅ Best match is selected automatically
- ✅ No code changes needed
- ✅ Improves recognition accuracy
- ✅ Handles variations in angles, lighting, expressions

**You just need to:**
- Upload multiple images when adding a person
- System handles the rest automatically!

