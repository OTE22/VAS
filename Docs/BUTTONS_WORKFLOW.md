# Buttons Workflow Documentation

This document describes how each button in the system works, step by step.

---

## Add Person Button

**Location:** Navbar → MANAGEMENT → ADD PERSON

### User Flow

1. **User clicks "ADD PERSON" button**
   - Button is located in the navbar under MANAGEMENT dropdown
   - Only visible to admin users

2. **Frontend checks user permissions**
   - Calls `/api/auth/me` to verify user is admin
   - If not admin, shows alert: "Access denied. Only administrators can add persons to track."
   - If admin, opens the upload modal

3. **Modal opens**
   - Displays upload form with:
     - Person Name input field
     - File upload area (drag & drop or click to select)
   - User can see file preview once selected

4. **User fills form**
   - Enters person's name in text field
   - Selects or drags image file (JPG, PNG, WEBP)
   - Frontend validates:
     - File size (max 5MB)
     - File type (must be image)
     - Person name (not empty)

5. **User clicks "Upload Person" button**
   - Button shows loading spinner: "Uploading..."
   - Button is disabled during upload

6. **Frontend sends request**
   - POST request to `/api/upload-person`
   - Includes:
     - `person_name` (form field)
     - `photo` (file)
   - Uses HttpOnly cookies for authentication

7. **Backend validates request**
   - Checks user is admin (via `require_role(["admin"])`)
   - Validates person name (not empty, min 2 chars, alphanumeric)
   - Validates file:
     - File exists
     - Is an image (JPG, PNG, WEBP)
     - Size < MAX_FILE_SIZE
     - Valid image content (not corrupted)

8. **Backend saves file**
   - Sanitizes person name: "John Doe" → "john_doe"
   - Creates directory: `storage/faces/` (if doesn't exist)
   - Saves file with timestamp: `john_doe_20260109_221832.jpg`
   - Handles duplicates (adds counter if filename exists)

9. **Backend processes face**
   - Reads image with OpenCV
   - Detects face using SCRFD detector
   - If no face detected → returns warning, file still saved
   - Generates 512-dimensional embedding using ArcFace recognizer

10. **Backend creates/updates Identity record**
    - Checks if Identity already exists with same name
    - If exists: Updates existing Identity (updates last_seen_at, best_snapshot_path)
    - If new: Creates new Identity record in database:
      - Type: KNOWN
      - Status: ACTIVE
      - Display name: person_name
      - best_snapshot_path: path to uploaded image
      - Timestamps: first_seen_at, last_seen_at, created_at, updated_at

11. **Backend stores embedding**
    - If pgvector enabled: Stores embedding in PostgreSQL `identity_embeddings` table
    - If FAISS: Stores embedding in FAISS index (in-memory + disk)
    - Also updates FAISS for backward compatibility
    - Creates IdentityEmbedding record linking to Identity

12. **Backend commits transaction**
    - All database changes are committed
    - If error occurs, rolls back transaction

13. **Backend returns response**
    - Success response includes:
      - success: true
      - message: "Successfully added [name] to tracking database."
      - filename: uploaded filename
      - identity_id: UUID of created/updated Identity
      - identity_created: true/false
      - total_identities: count of known identities
      - total_faces: count in FAISS
      - backend: "pgvector" or "faiss"

14. **Frontend handles response**
    - If success:
      - Shows success message: "✅ [message] (Total: X faces)"
      - Waits 2 seconds
      - Resets form (clears name, file, preview)
      - Closes modal
    - If error:
      - Shows error alert with message
      - Re-enables upload button

15. **Button returns to normal**
    - Upload button re-enabled
    - Button text returns to "Upload Person"
    - Spinner removed

### Result

- Person is immediately available in the system
- Identity record created in PostgreSQL
- Embedding stored in database (pgvector) or FAISS
- Person can be recognized in live video streams
- Person appears in Intelligence Analysis searches
- Person can be found in Advanced Search

### Error Handling

- **No face detected:** File saved, but warning shown. User can try again with better image.
- **Invalid file:** Error shown, file not saved.
- **Permission denied:** Alert shown, modal doesn't open.
- **Network error:** Error message shown, user can retry.
- **Database error:** File saved, but database update failed. Person available after restart.

---

## Next Button Workflow

_To be documented..._

