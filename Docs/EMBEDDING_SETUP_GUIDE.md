# Embedding Setup Guide

## Overview

The SQL Agent uses embeddings to enable semantic search across user query history. This allows the system to find similar queries and provide better context-aware responses.

## When Embeddings Work

Embeddings will work automatically when:

1. ✅ **`sentence-transformers` library is installed**
   ```bash
   pip install sentence-transformers
   ```

2. ✅ **Application is restarted** after installation

3. ✅ **User makes queries** - embeddings are generated and saved automatically

## Installation

### Option 1: Add to requirements (Recommended)

The `sentence-transformers` package has been added to `requirements-cpu.txt`. Install it:

```bash
pip install sentence-transformers
```

Or if using Docker:
```bash
docker-compose exec face_recognition_api pip install sentence-transformers
docker-compose restart face_recognition_api
```

### Option 2: Manual Installation

```bash
pip install sentence-transformers
```

## How It Works

1. **Automatic Generation**: When a user submits a query, the system:
   - Generates an embedding using `sentence-transformers/all-MiniLM-L6-v2` model
   - Saves the embedding to `user_query_embeddings` table
   - Uses it for semantic search later

2. **Model Loading**: The model is loaded on first use and cached for performance
   - First query may take a few seconds to download/load the model
   - Subsequent queries are fast (model is cached in memory)

3. **Semantic Search**: The `find_similar_queries()` method uses:
   - **HNSW index** (if pgvector is available) for fast approximate nearest neighbor search
   - **Cosine similarity** (fallback) if pgvector is not available

## Verification

### Check if Embeddings are Working

1. **Check Logs**: Look for these log messages:
   ```
   [EMBEDDING] Loaded embedding model: sentence-transformers/all-MiniLM-L6-v2
   [EMBEDDING] Generated embedding of dimension 384
   [EMBEDDING] Saved embedding for query {id}
   ```

2. **Check Database**: Query the `user_query_embeddings` table:
   ```sql
   SELECT COUNT(*) FROM user_query_embeddings;
   SELECT * FROM user_query_embeddings LIMIT 5;
   ```

3. **Test a Query**: Make a query through the chatbot, then check:
   - Logs should show embedding generation
   - Database should have a new entry in `user_query_embeddings`

### If Embeddings Don't Work

If you see this warning in logs:
```
[EMBEDDING] sentence-transformers not available, embeddings will not be generated
```

**Solution**: Install the package:
```bash
pip install sentence-transformers
```

Then restart the application.

## Performance Notes

- **First Query**: May take 5-10 seconds (model download/loading)
- **Subsequent Queries**: Fast (~50-100ms for embedding generation)
- **Model Size**: ~90MB (downloaded automatically on first use)
- **Memory Usage**: ~200-300MB for the model in memory

## Model Details

- **Model**: `sentence-transformers/all-MiniLM-L6-v2`
- **Dimensions**: 384
- **Use Case**: General-purpose semantic similarity
- **Language**: English (works well for SQL queries and natural language)

## Troubleshooting

### Issue: Embeddings not being saved

**Check**:
1. Is `sentence-transformers` installed?
2. Are there any errors in logs?
3. Is the database connection working?

**Solution**: Check application logs for `[EMBEDDING]` messages

### Issue: Slow first query

**Normal**: The model needs to be downloaded and loaded on first use. This is expected.

**Solution**: Wait for the first query to complete. Subsequent queries will be fast.

### Issue: Out of memory

**Solution**: The model uses ~200-300MB. If memory is constrained, consider:
- Using a smaller model
- Running on a machine with more RAM

## Integration with Query History

Embeddings are automatically:
- ✅ Generated for every query
- ✅ Saved to `user_query_embeddings` table
- ✅ Used for semantic search in `find_similar_queries()`
- ✅ Used for context retrieval in `get_context_for_query()`

No additional configuration needed - it works automatically once installed!

