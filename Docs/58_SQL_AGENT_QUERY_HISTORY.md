# SQL Agent Query History and Memory Integration

## Overview

This document describes the production-ready integration of user query history, conversation sessions, and memory management into the SQL Agent system. All code follows production standards with proper error handling, async operations, and database optimization.

## Architecture

### Database Tables

The integration uses the following database tables (defined in `db_models.py`):

1. **`user_query_history`**: Stores all user queries and responses
   - Links to `users` table via `user_id`
   - Links to `user_conversation_sessions` via `session_id`
   - Stores query text, response text, timestamps, metadata (JSONB), success status, error messages, and processing time

2. **`user_conversation_sessions`**: Manages conversation sessions
   - Tracks session start/end times, activity, query counts
   - Stores context summaries for sessions
   - Links to `users` table via `user_id`

3. **`user_conversation_memory`**: Stores extracted memories
   - Memory types: `fact`, `preference`, `context`, `pattern`
   - Stores importance scores, access counts, expiration dates
   - Links to queries and sessions that created the memory

4. **`user_query_embedding`**: Stores query embeddings for semantic search
   - Supports pgvector (if available) or JSONB fallback
   - Enables similarity search across user queries

### Service Layer

**File**: `sql_agent/services/user_query_history_service.py`

The `UserQueryHistoryService` class provides production-ready methods for:

- **Query History Management**:
  - `save_query_history()`: Save queries and responses
  - `get_user_query_history()`: Retrieve query history with filters
  - `get_query_by_id()`: Get specific query details

- **Session Management**:
  - `get_or_create_session()`: Get existing or create new session
  - `update_session()`: Update session properties
  - `get_user_sessions()`: List user's sessions

- **Memory Management**:
  - `save_memory()`: Save extracted memories
  - `get_user_memories()`: Retrieve memories with filters
  - `update_memory_access()`: Track memory usage
  - `delete_memory()`: Remove memories
  - `extract_and_save_memories()`: Extract memories from queries/responses

- **Embedding Management**:
  - `save_query_embedding()`: Store query embeddings
  - `find_similar_queries()`: Semantic search for similar queries

- **Context Retrieval**:
  - `get_context_for_query()`: Get recent queries + memories for AI context

## API Endpoints

### Query History

#### `GET /api/sql-agent/history`
Get user's query history.

**Query Parameters**:
- `limit` (int, default: 50): Maximum number of results
- `offset` (int, default: 0): Pagination offset
- `session_id` (str, optional): Filter by session

**Response**:
```json
{
  "success": true,
  "history": [
    {
      "id": 1,
      "query": "Track Joey",
      "response": "SURVEILLANCE INTELLIGENCE REPORT...",
      "timestamp": "2024-01-01T12:00:00",
      "success": true,
      "processing_time_ms": 1234.5,
      "session_id": "user_1_main",
      "metadata": {}
    }
  ],
  "count": 1
}
```

#### `GET /api/sql-agent/history/{query_id}`
Get a specific query by ID.

**Response**:
```json
{
  "success": true,
  "query": {
    "id": 1,
    "query": "Track Joey",
    "response": "Full response...",
    "timestamp": "2024-01-01T12:00:00",
    "response_timestamp": "2024-01-01T12:00:01",
    "success": true,
    "error_message": null,
    "processing_time_ms": 1234.5,
    "session_id": "user_1_main",
    "metadata": {
      "sql": "SELECT ...",
      "intent": "TRACK",
      "row_count": 10
    }
  }
}
```

### Session Management

#### `GET /api/sql-agent/sessions/list`
List user's conversation sessions.

**Query Parameters**:
- `active_only` (bool, default: false): Only return active sessions

**Response**:
```json
{
  "success": true,
  "sessions": [
    {
      "session_id": "user_1_main",
      "session_name": "Session 2024-01-01 12:00",
      "started_at": "2024-01-01T12:00:00",
      "last_activity_at": "2024-01-01T12:30:00",
      "is_active": true,
      "query_count": 5,
      "context_summary": "User tracking multiple identities"
    }
  ]
}
```

#### `POST /api/sql-agent/sessions/create`
Create a new conversation session.

**Request Body**:
```json
{
  "session_name": "New Session",
  "session_id": "optional_custom_id"
}
```

#### `PUT /api/sql-agent/sessions/{session_id}`
Update a session.

**Request Body**:
```json
{
  "context_summary": "Updated summary",
  "is_active": true
}
```

### Memory Management

#### `GET /api/sql-agent/memory`
Get user's memories.

**Query Parameters**:
- `memory_type` (str, optional): Filter by type (`fact`, `preference`, `context`, `pattern`)
- `min_importance` (int, default: 0): Minimum importance score (0-100)

**Response**:
```json
{
  "success": true,
  "memories": [
    {
      "id": 1,
      "type": "preference",
      "key": "preferred_date_range",
      "value": {"range": "7_days"},
      "importance": 60,
      "created_at": "2024-01-01T12:00:00",
      "last_accessed_at": "2024-01-01T12:30:00",
      "access_count": 5,
      "expires_at": null
    }
  ],
  "count": 1
}
```

#### `POST /api/sql-agent/memory`
Create a new memory.

**Request Body**:
```json
{
  "memory_type": "preference",
  "memory_key": "preferred_date_range",
  "memory_value": {"range": "7_days"},
  "importance_score": 60,
  "expires_at": "2024-12-31T23:59:59"
}
```

#### `DELETE /api/sql-agent/memory/{memory_id}`
Delete a memory.

### Context Retrieval

#### `GET /api/sql-agent/context`
Get context for AI agent (recent queries + memories).

**Query Parameters**:
- `session_id` (str, optional): Filter by session

**Response**:
```json
{
  "success": true,
  "context": {
    "recent_queries": [
      {
        "id": 1,
        "query": "Track Joey",
        "response": "Response...",
        "timestamp": "2024-01-01T12:00:00",
        "success": true,
        "session_id": "user_1_main"
      }
    ],
    "memories": [
      {
        "type": "preference",
        "key": "preferred_date_range",
        "value": {"range": "7_days"},
        "importance": 60
      }
    ],
    "session_summary": "User tracking multiple identities"
  }
}
```

## Integration Points

### Query Endpoint Integration

The `/api/sql-agent/query` endpoint now automatically:

1. **Saves query history** after successful queries
2. **Creates/updates sessions** in the database
3. **Extracts and saves memories** from queries/responses
4. **Stores metadata** (SQL, intent, row counts) for analysis

All database operations run asynchronously and non-blocking to avoid impacting query response times.

### Memory Extraction

The system automatically extracts memories from queries and responses:

- **Preferences**: Detects user preferences (e.g., date ranges, query patterns)
- **Patterns**: Identifies common query patterns
- **Facts**: Extracts important facts from responses
- **Context**: Captures conversation context

Memory extraction runs in the background and doesn't block query responses.

## Production Features

### Error Handling

- All database operations wrapped in try-catch blocks
- Errors logged but don't fail main requests
- Graceful degradation if database is unavailable

### Performance

- Async/await throughout for non-blocking operations
- Database queries optimized with proper indexes
- Background tasks for non-critical operations
- Pagination support for large result sets

### Security

- All endpoints require authentication
- User isolation (users can only access their own data)
- Input validation and sanitization
- SQL injection protection via SQLAlchemy ORM

### Scalability

- Database indexes on frequently queried columns
- JSONB for flexible metadata storage
- Support for pgvector for efficient semantic search
- Session management for multi-user scenarios

## Usage Examples

### Frontend Integration

```javascript
// Get query history
const response = await fetch('/api/sql-agent/history?limit=20', {
  headers: { 'Authorization': `Bearer ${token}` }
});
const data = await response.json();

// Get user memories
const memories = await fetch('/api/sql-agent/memory?min_importance=50', {
  headers: { 'Authorization': `Bearer ${token}` }
});

// Get context for AI agent
const context = await fetch('/api/sql-agent/context?session_id=user_1_main', {
  headers: { 'Authorization': `Bearer ${token}` }
});
```

### Backend Integration

```python
from sql_agent.services.user_query_history_service import user_query_history_service
from db_connection import get_db

# Save query history
async for db in get_db():
    query_history = await user_query_history_service.save_query_history(
        db=db,
        user_id=user_id,
        query_text="Track Joey",
        response_text="Response...",
        session_id="user_1_main",
        success=True,
        processing_time_ms=1234.5,
        metadata={"sql": "SELECT ...", "intent": "TRACK"}
    )
    await db.commit()

# Get context for AI agent
async for db in get_db():
    context = await user_query_history_service.get_context_for_query(
        db=db,
        user_id=user_id,
        session_id="user_1_main"
    )
```

## Database Migration

To create the database tables, run:

```bash
# Using Alembic (recommended)
./docker/run_alembic_migration.sh revision --autogenerate -m "Add SQL agent user query history and memory tables"
./docker/run_alembic_migration.sh upgrade head

# Or manually create tables (not recommended for production)
# The tables are defined in db_models.py
```

## Future Enhancements

1. **Advanced Memory Extraction**: Use LLM-based extraction for more sophisticated memory creation
2. **Semantic Search**: Full pgvector integration for efficient similarity search
3. **Memory Summarization**: Automatically summarize and consolidate memories
4. **Analytics Dashboard**: Visualize query patterns and user behavior
5. **Export Functionality**: Export query history and memories to various formats

## Notes

- All timestamps are stored in UTC
- Memory extraction is simplified and can be enhanced with LLM-based extraction
- Embedding generation requires additional setup (sentence-transformers or similar)
- The system maintains backward compatibility with existing ConversationMemory file-based storage

