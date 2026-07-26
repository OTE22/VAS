# Identity Management API - Frontend Access Guide

This document explains how to access all identity management API endpoints through the frontend interface.

## Access Location

All identity management features are accessible from the **Unknown Faces Center** page at:
```
/admin/unknown
```

**Note:** This page is **admin-only**. Non-admin users will be redirected.

---

## Available Endpoints & Frontend Access

### 1. **GET /api/admin/unknown** - List Unknown Identities
**Frontend Access:**
- **Location:** Main page (`/admin/unknown`)
- **How to Use:** 
  - The page automatically loads unknown identities on page load
  - Use the **filters** section to filter by:
    - Date range (from/to)
    - Camera/Pipeline
    - Status
    - Minimum appearances
  - Click **"APPLY FILTERS"** to apply filters
  - Click **"CLEAR"** to reset filters
  - Use **"PREVIOUS"** and **"NEXT"** buttons for pagination

**UI Elements:**
- Statistics cards showing: Total Unknown, Total Appearances, Active Cameras
- Identity grid displaying all unknown faces with:
  - Best snapshot image
  - First seen / Last seen dates
  - Number of appearances
  - Number of cameras
  - Action buttons: VIEW, PROMOTE, MERGE

---

### 2. **GET /api/admin/identity/{id}** - Get Identity Details
**Frontend Access:**
- **Location:** Identity detail modal
- **How to Use:**
  1. Click **"VIEW"** button on any identity card
  2. Or click **"VIEW"** button on search results
  3. Modal displays:
     - Identity snapshot
     - Identity information (type, status, appearances, cameras)
     - Appearance timeline (all appearances with timestamps)
     - Action buttons: PROMOTE TO KNOWN, MERGE

**UI Elements:**
- Modal with dark military theme
- Timeline view showing all appearances
- Action buttons at the bottom

---

### 3. **POST /api/admin/unknown/{id}/promote** - Promote Unknown to Known
**Frontend Access:**
- **Location:** Multiple places
- **How to Use:**
  
  **Option A - From Identity Card:**
  1. Click **"PROMOTE"** button on any identity card
  2. Enter display name (required)
  3. Enter person code (optional)
  4. Click **"PROMOTE"** button
  
  **Option B - From Identity Details Modal:**
  1. Click **"VIEW"** on an identity card
  2. Click **"PROMOTE TO KNOWN"** button in the modal
  3. Fill in the form and submit
  
  **Option C - From Search Results:**
  1. Perform a search by image
  2. Click **"PROMOTE"** on any unknown identity in results

**UI Elements:**
- Promote modal with form
- Display name input (required)
- Person code input (optional)
- CANCEL and PROMOTE buttons

---

### 4. **POST /api/admin/identities/merge** - Merge Identities
**Frontend Access:**
- **Location:** Merge modal
- **How to Use:**
  
  **Option A - From Identity Card:**
  1. Click **"MERGE"** button on any identity card
  2. The "From Identity ID" is automatically filled
  3. Enter or search for the target identity ID in "To Identity ID"
  4. Click **"SEARCH"** to find and preview the target identity
  5. Click **"SELECT"** on the search result to choose it
  6. (Optional) Add notes about the merge
  7. Click **"MERGE"** button to execute
  
  **Option B - From Identity Details Modal:**
  1. Click **"VIEW"** on an identity card
  2. Click **"MERGE"** button in the modal
  3. Follow the same process as above

**UI Elements:**
- Merge modal with form
- From Identity ID (read-only, auto-filled)
- To Identity ID input with search functionality
- Search results preview with identity snapshot
- Notes textarea (optional)
- CANCEL and MERGE buttons

---

### 5. **POST /api/search/by-image** - Search by Image
**Frontend Access:**
- **Location:** Search by Image modal
- **How to Use:**
  1. Click **"SEARCH BY IMAGE"** button in the page header
  2. Click **"Choose File"** or drag and drop an image
  3. Preview the image (if uploaded)
  4. Select search scope:
     - Both Known & Unknown (default)
     - Known Only
     - Unknown Only
  5. Click **"SEARCH"** button
  6. Results appear in a grid below the form
  7. Click **"VIEW"** on any result to see details
  8. Click **"PROMOTE"** on unknown results to promote them

**UI Elements:**
- Search modal with file upload
- Image preview
- Search scope dropdown
- Results grid with similarity scores
- Action buttons on each result card

---

### 6. **GET /api/admin/merge-suggestions** - Get Merge Suggestions
**Frontend Access:**
- **Location:** Merge Suggestions modal
- **How to Use:**
  1. Click **"MERGE SUGGESTIONS"** button in the page header
  2. Modal displays all pending merge suggestions
  3. Each suggestion shows:
     - Cluster ID
     - Confidence percentage
     - Number of identities to merge
     - Identity IDs (truncated)
     - Representative snapshots (if available)
  4. Review each suggestion
  5. Click **"APPROVE"** to execute the merge
  6. Click **"REJECT"** to dismiss the suggestion

**UI Elements:**
- Merge suggestions modal
- List of suggestion cards
- Confidence badges
- Identity ID tags
- Snapshot previews
- APPROVE and REJECT buttons on each card

---

### 7. **POST /api/admin/merge-suggestions/{id}/approve** - Approve Merge Suggestion
**Frontend Access:**
- **Location:** Merge Suggestions modal
- **How to Use:**
  1. Open Merge Suggestions modal
  2. Review a merge suggestion
  3. Click **"APPROVE"** button on the suggestion card
  4. Confirm the action in the confirmation dialog
  5. The merge is executed automatically
  6. Success notification appears
  7. The suggestion is removed from the list
  8. Unknown faces list is refreshed

**UI Elements:**
- APPROVE button on each suggestion card
- Confirmation dialog
- Success/error notifications

---

### 8. **POST /api/admin/merge-suggestions/{id}/reject** - Reject Merge Suggestion
**Frontend Access:**
- **Location:** Merge Suggestions modal
- **How to Use:**
  1. Open Merge Suggestions modal
  2. Review a merge suggestion
  3. Click **"REJECT"** button on the suggestion card
  4. Confirm the action in the confirmation dialog
  5. The suggestion is marked as rejected
  6. Success notification appears
  7. The suggestion is removed from the list

**UI Elements:**
- REJECT button on each suggestion card
- Confirmation dialog
- Success/error notifications

---

## Navigation Summary

### Main Page (`/admin/unknown`)
- **Header Actions:**
  - **MERGE SUGGESTIONS** button → Opens merge suggestions modal
  - **SEARCH BY IMAGE** button → Opens search by image modal

- **Filters Section:**
  - Date range inputs
  - Camera/Pipeline dropdown
  - Status dropdown
  - Minimum appearances input
  - **APPLY FILTERS** button
  - **CLEAR** button

- **Statistics Cards:**
  - Total Unknown
  - Total Appearances
  - Active Cameras

- **Identity Grid:**
  - Each card has:
    - **VIEW** button → Opens identity details modal
    - **PROMOTE** button → Opens promote modal
    - **MERGE** button → Opens merge modal

- **Pagination:**
  - **PREVIOUS** button
  - Page info display
  - **NEXT** button

### Modals

1. **Identity Details Modal:**
   - Opened by clicking VIEW
   - Shows full identity information
   - Contains: PROMOTE TO KNOWN, MERGE buttons

2. **Promote Modal:**
   - Opened by clicking PROMOTE
   - Form to enter display name and person code

3. **Merge Modal:**
   - Opened by clicking MERGE
   - Form to select target identity and add notes

4. **Merge Suggestions Modal:**
   - Opened by clicking MERGE SUGGESTIONS
   - Lists all pending merge suggestions
   - Each suggestion has APPROVE/REJECT buttons

5. **Search by Image Modal:**
   - Opened by clicking SEARCH BY IMAGE
   - File upload form
   - Search scope selection
   - Results grid

---

## Authentication

All endpoints require authentication:
- User must be logged in
- User must have `admin` role
- Token is automatically included in all requests from `localStorage.getItem('access_token')`

---

## Error Handling

The frontend handles errors gracefully:
- API errors are displayed as notifications
- Failed requests show error messages in the UI
- Retry buttons are available for failed operations
- Detailed error messages are logged to console

---

## Notes

- All UI elements follow the **military intelligence aesthetic** theme
- Dark backgrounds with green accents (#00ff96)
- Glassmorphism effects on cards and modals
- Smooth animations and transitions
- Responsive design for different screen sizes
- All buttons have icons and clear labels
- Loading states are shown during API calls
- Success/error notifications appear after actions

---

## Quick Reference

| Endpoint | Frontend Button/Location | Action |
|----------|-------------------------|--------|
| `GET /api/admin/unknown` | Page load / Apply filters | Lists unknown identities |
| `GET /api/admin/identity/{id}` | VIEW button | Shows identity details |
| `POST /api/admin/unknown/{id}/promote` | PROMOTE button | Promotes unknown to known |
| `POST /api/admin/identities/merge` | MERGE button | Merges two identities |
| `POST /api/search/by-image` | SEARCH BY IMAGE button | Searches by uploaded image |
| `GET /api/admin/merge-suggestions` | MERGE SUGGESTIONS button | Lists merge suggestions |
| `POST /api/admin/merge-suggestions/{id}/approve` | APPROVE button (in suggestions) | Approves a merge suggestion |
| `POST /api/admin/merge-suggestions/{id}/reject` | REJECT button (in suggestions) | Rejects a merge suggestion |

---

**Last Updated:** 2025-01-27

