# Backend Path Normalization - Best Practice

## Why Backend Should Handle Path Normalization

### ✅ **Best Practice: Backend Handles All Path Logic**

**Principle:** The backend should be the **single source of truth** for all business logic, including path normalization and URL generation.

### Benefits:

1. **Security**
   - Backend controls what paths are exposed
   - Prevents path traversal attacks
   - Can add authentication/authorization checks
   - Validates file existence before returning URLs

2. **Maintainability**
   - Single place to update path logic
   - Change once, works everywhere
   - Easier to debug and test
   - Consistent behavior across all endpoints

3. **Flexibility**
   - Can switch storage backends transparently
   - Can add CDN URLs, signed URLs, etc.
   - Can implement caching strategies
   - Frontend doesn't need to know storage structure

4. **Separation of Concerns**
   - Frontend focuses on UI/UX
   - Backend handles all data transformation
   - Clear API contract

## Implementation

### Centralized Utility Function

**File:** `backend/utils/path_utils.py`

```python
def path_to_url(file_path: Optional[str], storage_dir: Optional[str] = None) -> Optional[str]:
    """
    Convert a file path to a URL for serving.
    
    This is the main function to use in API responses.
    It normalizes the path and returns a ready-to-use URL.
    """
```

### API Response Format

**Backend returns:**
```json
{
    "best_snapshot_path": "/app/storage/pipeline/name/file.jpg",  // Original path (for reference)
    "snapshot_url": "/storage/pipeline/name/file.jpg"              // Ready-to-use URL
}
```

**Frontend uses:**
```javascript
// Simply use the URL provided by backend
img.src = identity.snapshot_url;
```

## Updated Endpoints

### ✅ Intelligence Routes
- `/api/identities/{id}/related` - Returns `snapshot_url`
- `/api/identities/{id}/cross-camera` - Returns `snapshot_url` in movements

### ✅ Identity Routes
- `/api/admin/identity/{id}` - Returns `snapshot_url`
- `/api/admin/unknown` - Returns `snapshot_url`

## Frontend Simplification

### Before (❌ Bad Practice):
```javascript
// Frontend had to know about Docker paths, storage structure, etc.
let imagePath = identity.best_snapshot_path;
if (imagePath.startsWith('/app/storage/')) {
    imagePath = imagePath.replace('/app/storage/', '/storage/');
}
// ... more complex logic
img.src = imagePath;
```

### After (✅ Best Practice):
```javascript
// Frontend just uses what backend provides
img.src = identity.snapshot_url || defaultAvatar;
```

## Migration Guide

### For New Endpoints:
1. Import the utility: `from backend.utils.path_utils import path_to_url`
2. Use in response: `"snapshot_url": path_to_url(file_path)`
3. Keep original path for reference: `"best_snapshot_path": file_path`

### For Frontend:
1. Always use `snapshot_url` field
2. Fallback to default avatar if `snapshot_url` is null
3. Never manipulate paths in frontend

## Example Usage

### Backend:
```python
from backend.utils.path_utils import path_to_url

# In your endpoint
return {
    "identity_id": identity.id,
    "snapshot_url": path_to_url(identity.best_snapshot_path),  # Backend handles everything
    "best_snapshot_path": identity.best_snapshot_path  # Keep for reference
}
```

### Frontend:
```javascript
// Simple and clean
const imageUrl = data.snapshot_url || defaultAvatar;
img.src = imageUrl;
```

## Benefits Summary

| Aspect | Frontend Logic | Backend Logic |
|--------|---------------|---------------|
| **Security** | ❌ Exposed to manipulation | ✅ Controlled & validated |
| **Maintainability** | ❌ Duplicated in multiple places | ✅ Single source of truth |
| **Flexibility** | ❌ Hard to change | ✅ Easy to adapt |
| **Testing** | ❌ Hard to test UI logic | ✅ Easy to unit test |
| **Consistency** | ❌ Can differ across pages | ✅ Always consistent |

## Conclusion

**Always prefer backend logic for:**
- Path normalization
- URL generation
- Data transformation
- Business rules
- Security checks

**Frontend should only:**
- Display data
- Handle user interactions
- Format for display (dates, numbers)
- Client-side validation (UX only, not security)

This approach makes the codebase more maintainable, secure, and scalable.

