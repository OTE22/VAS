"""
Admin Tutorial and Learning Routes
===================================
Comprehensive learning endpoint for admins with examples and guides.
"""

import os
import sys
import logging
from typing import Dict, List, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_connection import get_db
from db_models import User
from backend.auth.auth_service import require_role
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Create router
router = APIRouter(
    prefix="/api/admin",
    tags=["Admin Tutorial"],
    responses={
        401: {"description": "Unauthorized"},
        403: {"description": "Forbidden - Admin access required"},
    }
)


class TutorialSection(BaseModel):
    title: str
    description: str
    content: str
    examples: List[Dict[str, Any]]
    api_endpoints: List[Dict[str, Any]]


class TutorialResponse(BaseModel):
    sections: List[TutorialSection]
    quick_start: Dict[str, Any]
    common_tasks: List[Dict[str, Any]]


@router.get("/tutorial", response_model=TutorialResponse, summary="Admin Tutorial", description="Comprehensive learning guide for admins with examples (Admin only)")
async def get_admin_tutorial(
    current_user: User = Depends(require_role(["admin"])),
    db: AsyncSession = Depends(get_db)
):
    """
    Get comprehensive tutorial and learning materials for admin users.
    Includes examples, API endpoints, and step-by-step guides.
    """
    
    tutorial_data = {
        "sections": [
            {
                "title": "API Authentication",
                "description": "Learn how to authenticate and use API endpoints with access tokens",
                "content": '''
# API Authentication Guide

## Overview

All API endpoints (except login) require authentication using JWT Bearer tokens. This guide explains how to get and use access tokens.

## Step 1: Login to Get Token

**Endpoint:** `POST /api/auth/login`

**Request:**
```json
{
  "username": "admin",
  "password": "your_password"
}
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "user": {
    "id": 1,
    "username": "admin",
    "role": "admin"
  }
}
```

## Step 2: Use Token in Requests

Include the token in the `Authorization` header:

**Format:** `Authorization: Bearer <token>`

**Example:**
```bash
curl -X GET "http://localhost/api/settings" \\
  -H "Authorization: Bearer YOUR_TOKEN_HERE" \\
  -H "Accept: application/json"
```

## Generating Tokens for Your System

### Quick Method: Use Token Generation Utility

A utility script is provided for easy token generation:

**Location:** `utils/generate_token.py`

**Usage:**
```bash
# Generate token
python utils/generate_token.py admin your_password

# Save to variable
TOKEN=$(python utils/generate_token.py admin your_password)

# Use token
curl -X GET "http://localhost/api/settings" \\
  -H "Authorization: Bearer $TOKEN"
```

**With environment variables:**
```bash
export API_USERNAME="admin"
export API_PASSWORD="your_password"
TOKEN=$(python utils/generate_token.py)
```

### For External Systems/Integrations

Always use the login endpoint - it's the standard and secure way:

```bash
# Get token
TOKEN=$(curl -X POST "http://localhost/api/auth/login" \\
  -H "Content-Type: application/json" \\
  -d '{"username": "admin", "password": "password"}' \\
  | jq -r '.access_token')

# Use token
curl -X GET "http://localhost/api/settings" \\
  -H "Authorization: Bearer $TOKEN"
```

### For Internal Scripts/Automation

You can also create your own token generation function:

**Python Example:**
```python
import requests

def get_api_token(username, password, base_url="http://localhost"):
    # Generate API token for system integration
    try:
    response = requests.post(
            f"{base_url}/api/auth/login",
            json={"username": username, "password": password}
        )
        if response.status_code == 200:
            return response.json()["access_token"]
        else:
            raise Exception(f"Login failed: {response.status_code}")
    except Exception as e:
        raise Exception(f"Failed to generate token: {str(e)}")

# Usage
token = get_api_token("admin", "your_password")
print(f"Token: {token}")
```

**JavaScript Example:**
```javascript
async function getApiToken(username, password, baseUrl = 'http://localhost') {
  const response = await fetch(`${baseUrl}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password })
  });
  
  if (!response.ok) {
    throw new Error(`Login failed: ${response.status}`);
  }
  
  const data = await response.json();
  return data.access_token;
}

// Usage
const token = await getApiToken('admin', 'password');
console.log(`Token: ${token}`);
```

### Token Management in Your System

**Store token securely:**
- Environment variables: `export API_TOKEN="your_token"`
- Configuration files: Store in `.env` or config files
- Secure vaults: Use keyring, AWS Secrets Manager, etc.

**Token refresh pattern:**
```python
class APIClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.token = None
    
    def get_token(self):
        """Get token, refresh if needed"""
        if not self.token:
            # Login to get new token
            response = requests.post(
                "http://localhost/api/auth/login",
                json={"username": self.username, "password": self.password}
            )
            self.token = response.json()["access_token"]
        return self.token
    
    def api_request(self, method, endpoint, **kwargs):
        """Make authenticated request"""
        token = self.get_token()
        headers = kwargs.get("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        kwargs["headers"] = headers
        return requests.request(method, endpoint, **kwargs)
```

## Using Swagger UI (/docs)

1. Open `http://localhost/docs`
2. Click **"Authorize"** button (🔓 lock icon)
3. Enter: `Bearer YOUR_TOKEN_HERE` (include "Bearer" and space)
4. Click **"Authorize"**
5. All endpoints will now use your token automatically

## Token Expiration

- **Default:** 24 hours (1440 minutes)
- **Configurable:** Set `ACCESS_TOKEN_EXPIRE_MINUTES` in `.env`
- **After expiration:** Login again to get a new token

## Common Issues

**401 Unauthorized:**
- Missing Authorization header
- Token format incorrect (must be: `Bearer <token>`)
- Token expired - login again

**403 Forbidden:**
- User doesn't have admin role
- Account is inactive or blocked

## Security Best Practices

✅ **Never commit tokens** to version control
✅ **Use HTTPS** in production
✅ **Store tokens securely** (environment variables)
✅ **Handle token expiration** gracefully
                ''',
                "examples": [
                    {
                        "title": "Login with cURL",
                        "description": "Get access token using cURL",
                        "code": """curl -X POST "http://localhost/api/auth/login" \\
  -H "Content-Type: application/json" \\
  -d '{
    "username": "admin",
    "password": "your_password"
  }'"""
                    },
                    {
                        "title": "Use Token in Request",
                        "description": "Include token in Authorization header",
                        "code": """curl -X GET "http://localhost/api/settings" \\
  -H "Authorization: Bearer YOUR_TOKEN_HERE" \\
  -H "Accept: application/json" """
                    },
                    {
                        "title": "JavaScript Example",
                        "description": "Complete authentication flow in JavaScript",
                        "code": """// Login
async function login(username, password) {
  const response = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password })
  });
  const data = await response.json();
  return data.access_token;
}

// Use token
async function getSettings(token) {
  const response = await fetch('/api/settings', {
    headers: { 'Authorization': `Bearer ${token}` }
  });
  return await response.json();
}

// Usage
const token = await login('admin', 'password');
const settings = await getSettings(token);"""
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "POST",
                        "path": "/api/auth/login",
                        "description": "Login to get an access token",
                        "body": {
                            "username": "string (required)",
                            "password": "string (required)"
                        },
                        "response": {
                            "access_token": "JWT token string",
                            "token_type": "bearer",
                            "user": "User object"
                        }
                    }
                ]
            },
            {
                "title": "Understanding Unknown Faces",
                "description": "Learn how the system handles unknown faces and how to manage them",
                "content": """
# Understanding Unknown Faces

## What Are Unknown Faces?

When the system sees a face it doesn't recognize, it automatically creates an **Unknown Identity** record. Think of it like a security camera that spots someone new - the system saves their face and tracks where and when they appear.

## How It Works (Simple Explanation)

**Step 1: Face Detected**
- A camera captures an image with a face
- The system checks: "Do I know this person?"
- If not recognized → Creates an "Unknown" record

**Step 2: Tracking**
- Every time this same face appears, the system records:
  - **Where**: Which camera/pipeline
  - **When**: Date and time
  - **How many times**: Appearance count
  - **Best photo**: The clearest image saved

**Step 3: Grouping**
- If the same person appears multiple times, the system groups them together
- All appearances are linked to one identity record

## What You'll See

When you open the **Unknown Faces** page, you'll see:
- **Face cards**: Each showing a person's photo
- **Information**: When they were first seen, last seen, how many times
- **Camera locations**: Which cameras detected them
- **Action buttons**: VIEW, PROMOTE, MERGE

## Identity Types (Simple)

- **Unknown**: Person not yet identified (shown in Unknown Faces page)
- **Known**: Person you've given a name to (shown in dashboard)

## Identity Status

- **Active**: Recently seen (within last 6 months)
- **Inactive**: Not seen recently (system marks automatically)
- **Merged**: Combined with another identity (duplicate removed)
- **Promoted**: You gave them a name (unknown → known)

## What Happens to Unknown Faces?

1. **They stay unknown** until you identify them
2. **You can promote them** when you recognize who they are
3. **You can merge them** if the system created duplicates
4. **System suggests merges** automatically (daily)

## User Access Control

**For Regular Users:**
- If you've been granted pipeline access by an admin, you can:
  - See unknown identities from your assigned pipelines
  - Promote unknown identities to known (for your pipelines)
  - Merge identities (both must be from your accessible pipelines)
  - View and act on merge suggestions (for your accessible pipelines)
- You'll only see data from pipelines you have access to
- The interface works the same, but data is automatically filtered

**For Admins:**
- You have full access to all pipelines and all identities
- You can see and manage everything in the system
                """,
                "examples": [
                    {
                        "title": "View Unknown Faces",
                        "description": "See all unknown faces detected in the system",
                        "steps": [
                            "1. Navigate to Admin → Unknown Faces",
                            "2. Browse faces grouped by pipeline (camera)",
                            "3. Click on a face card to see details",
                            "4. View appearance timeline and snapshots"
                        ],
                        "api_example": {
                            "method": "GET",
                            "url": "/api/admin/unknown?page=1&page_size=20",
                            "response": {
                                "identities": [
                                    {
                                        "id": "uuid-here",
                                        "type": "unknown",
                                        "appearances_count": 5,
                                        "first_seen_at": "2025-01-01T10:00:00Z",
                                        "last_seen_at": "2025-01-03T15:30:00Z",
                                        "best_snapshot_path": "storage/pipeline1/unknown/unknown_20250101_100000.jpg",
                                        "pipeline_ids": ["pipeline1"]
                                    }
                                ],
                                "total": 50,
                                "stats": {
                                    "total_unknown": 50,
                                    "total_appearances": 250,
                                    "active_cameras": 5
                                }
                            }
                        }
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/admin/unknown",
                        "description": "List all unknown identities with pagination and filters",
                        "parameters": {
                            "page": "Page number (default: 1)",
                            "page_size": "Items per page (default: 20)",
                            "pipeline_id": "Filter by pipeline (optional)",
                            "date_from": "Filter from date (optional)",
                            "date_to": "Filter to date (optional)"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/admin/identity/{identity_id}",
                        "description": "Get detailed information about a specific identity",
                        "parameters": {
                            "identity_id": "UUID of the identity"
                        }
                    }
                ]
            },
            {
                "title": "Promoting Unknown to Known",
                "description": "Learn how to identify and promote unknown faces to known persons",
                "content": """
# Promoting Unknown to Known

## What Does "Promote" Mean?

**Promoting** means giving an unknown face a name. Once promoted, the system will recognize this person in future detections and show their name instead of "Unknown".

## When Should You Promote?

✅ **Promote when:**
- You recognize who the person is
- You want the system to track them by name
- You want them to appear in the dashboard with their name

❌ **Don't promote when:**
- You're not sure who it is
- It might be a duplicate (check merge suggestions first)
- You haven't verified it's the same person in all appearances

## Step-by-Step: How to Promote

**Step 1: Find the Unknown Face**
- Go to **Admin → Unknown Faces**
- Browse through the face cards
  - **Admin**: Sees all unknown identities
  - **Regular Users**: Only see identities from assigned pipelines
- Or use "Search by Image" if you have a photo

**Step 2: Review the Details**
- Click **VIEW** on the face card
- Check the timeline: When and where did they appear?
- Look at the snapshots: Is it the same person in all photos?
- Verify: Make sure all appearances are the same person

**Step 3: Promote**
- Click the **"PROMOTE TO KNOWN"** button
- A form will appear asking for:
  - **Display Name** (required): Enter the person's name (e.g., "John Doe")
  - **Person Code** (optional): Employee ID, badge number, etc.
- Click **"PROMOTE"** to confirm

**Step 4: What Happens Next**
- ✅ The face disappears from Unknown Faces page
- ✅ The person now has a name in the system
- ✅ Future detections will show their name
- ✅ They appear in the dashboard (if enabled)

## Tips for Success

1. **Use full names**: "John Doe" is better than "John"
2. **Be consistent**: Use the same format for all names
3. **Check first**: Look at merge suggestions - might already exist
4. **Verify**: Make sure all appearances show the same person
5. **Add codes**: Employee IDs or badge numbers help with tracking
                """,
                "examples": [
                    {
                        "title": "Promote via Frontend",
                        "description": "Use the admin interface to promote an unknown face",
                        "steps": [
                            "1. Go to Admin → Unknown Faces",
                            "2. Find the unknown face you want to promote",
                            "3. Click on the face card to open details",
                            "4. Click 'Promote to Known' button",
                            "5. Enter the person's name (e.g., 'John Doe')",
                            "6. Click 'Confirm'",
                            "7. The identity is now known and will appear in dashboard"
                        ],
                        "api_example": {
                            "method": "POST",
                            "url": "/api/admin/unknown/{identity_id}/promote",
                            "body": {
                                "display_name": "John Doe",
                                "notes": "Employee ID: 12345"
                            },
                            "response": {
                                "success": True,
                                "identity": {
                                    "id": "uuid-here",
                                    "type": "known",
                                    "display_name": "John Doe",
                                    "status": "promoted"
                                }
                            }
                        }
                    },
                    {
                        "title": "Promote via API (cURL)",
                        "description": "Example using cURL command",
                        "code": """curl -X POST "https://your-domain.com/api/admin/unknown/{identity_id}/promote" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "display_name": "John Doe",
    "notes": "Employee ID: 12345"
  }'"""
                    },
                    {
                        "title": "Promote via JavaScript",
                        "description": "Example using fetch API",
                        "code": """async function promoteIdentity(identityId, displayName) {
  const response = await fetch(`/api/admin/unknown/${identityId}/promote`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${getAuthToken()}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      display_name: displayName,
      notes: 'Promoted via admin interface'
    })
  });
  
  if (response.ok) {
    const result = await response.json();
    console.log('Promoted:', result.identity);
    return result;
  } else {
    throw new Error('Promotion failed');
  }
}"""
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "POST",
                        "path": "/api/admin/unknown/{identity_id}/promote",
                        "description": "Promote an unknown identity to known status",
                        "parameters": {
                            "identity_id": "UUID of the unknown identity"
                        },
                        "body": {
                            "display_name": "string (required) - Name to assign",
                            "notes": "string (optional) - Additional notes"
                        },
                        "response": {
                            "success": "boolean",
                            "identity": "Identity object with updated type and status"
                        }
                    }
                ]
            },
            {
                "title": "Merging Identities",
                "description": "Learn how to merge duplicate identities",
                "content": """
# Merging Identities

## What Does "Merge" Mean?

**Merging** means combining two identity records into one. This is useful when the system created separate records for the same person (duplicates).

## When Should You Merge?

✅ **Merge when:**
- Two unknown faces are the same person
- An unknown face matches a known person
- The system suggests a merge (check suggestions first!)
- You notice duplicate entries manually

❌ **Don't merge when:**
- You're not sure they're the same person
- The faces look different
- The confidence score is low (<70%)

## Step-by-Step: How to Merge

**Step 1: Find Duplicates**
- Check **"MERGE SUGGESTIONS"** button (system finds them automatically)
  - **Admin**: Sees all merge suggestions
  - **Regular Users with Pipeline Access**: Can view and manage merge suggestions for identities from their assigned pipelines
  - **Note**: Regular users can access merge suggestions if they have pipeline access (same access as Unknown Faces page)
- Or search by image to find similar faces
- Or manually browse and notice duplicates
  - **Note**: Regular users can only merge identities from their accessible pipelines

**Step 2: Review Both Identities**
- Click **VIEW** on both face cards
- Check the timelines: Do they appear at similar times?
- Compare snapshots: Do they look like the same person?
- Verify: Make absolutely sure they're the same person

**Step 3: Merge**
- Click **MERGE** on one of the identities
- A form will appear:
  - **From Identity**: The one you clicked (will be merged)
  - **To Identity**: Search for the target identity (will keep all data)
- Click **SEARCH** to find the target identity
- Select the target from results
- Add notes (optional): Why you're merging
- Click **"MERGE"** to confirm

**Step 4: What Happens**
- ✅ Both identities become one
- ✅ All appearances combined
- ✅ All photos combined
- ✅ One identity record remains
- ✅ The other is marked as "merged"

## Multi-Identity Merge (3+ Identities)

**NEW FEATURE:** You can now merge **3 or more identities** at once efficiently!

### When to Use Multi-Merge

✅ **Use Multi-Merge when:**
- You have 3+ identities that are the same person
- You want to merge multiple identities quickly
- You trust the system's smart selection algorithm

✅ **Use Merge Suggestions when:**
- You want the system to automatically find duplicates
- You prefer reviewing suggestions before merging

✅ **Use Single Merge when:**
- You only have 2 identities to merge
- You need precise control over the target

### How Multi-Merge Works

**Smart Target Selection:**
- System automatically finds the **best target identity**
- Selection based on:
  1. **Most appearances** (weight: 1000) - Most detection records
  2. **Best quality snapshot** (weight: 100) - Has high-quality image
  3. **Age bonus** (weight: 1 per day) - Older identities preferred
- **Time complexity:** O(n) - Very efficient!

### Step-by-Step: Multi-Merge

**Method 1: Using Multi-Select Mode (Web Interface)**

1. **Enable Multi-Select**
   - Click **"MULTI-SELECT"** button in header
   - Button changes to **"MULTI-SELECT ON"** (highlighted)

2. **Select Identities**
   - Click on identity cards to select them
   - Selected cards show green border and glow
   - Need at least **2 identities** selected

3. **Merge Selected**
   - Click **"MERGE SELECTED (N)"** button (appears when 2+ selected)
   - Or open merge modal and use multi-merge form

4. **Review and Confirm**
   - Review selected identities in merge modal
   - Optionally specify target identity (or leave empty for auto-selection)
   - Add notes if needed
   - Click **"MERGE"** to execute

5. **Result**
   - All selected identities merged into best target
   - Success message shown
   - Identity list refreshes automatically

**Method 2: Using API**

**Step 1: Preview (Recommended)**
```bash
curl -X POST "http://localhost/api/admin/identities/merge-preview" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"identity_ids": ["uuid-1", "uuid-2", "uuid-3"], "target_identity_id": null}'
```

**Step 2: Execute Merge**
```bash
curl -X POST "http://localhost/api/admin/identities/merge-multiple" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "identity_ids": ["uuid-1", "uuid-2", "uuid-3"],
    "target_identity_id": null,
    "notes": "Merging duplicate identities"
  }'
```

**Response (Production-Grade):**
```json
{
  "success": True,
  "message": "Successfully merged 2 identities into target identity",
  "identity": {
    "id": "uuid-1",
    "type": "known",
    "appearances_count": 120
  },
  "merged_count": 2,
  "statistics": {"pipeline_count": 3, "appearances_moved": 70},
  "type_promotion": {"changed": True, "from": "unknown", "to": "known"},
  "auto_selected_target": True
}
```

### Access Control

- **Admin**: Can merge any identities
- **Regular Users with Pipeline Access**: Can only merge identities from their assigned pipelines
- System validates access before allowing merge

### Best Practices

✅ **Review before merging** - Check snapshots and timelines
✅ **Use smart selection** - Let system auto-select best target
✅ **Add notes** - Explain why identities were merged
✅ **Check suggestions first** - System may have already found duplicates

**See also:** [Multi-Identity Merge Guide](./Docs/28_MULTI_IDENTITY_MERGE_GUIDE.md) for complete documentation.

## Merge Suggestions (Automatic)

The system **automatically finds duplicates** and suggests merges for you!

### When Does It Run?

- **First time**: Configurable delay after system starts (default: 1 hour, set via `CLUSTER_STARTUP_DELAY_HOURS`)
- **Then**: Every 24 hours (daily, configurable via `CLUSTER_INTERVAL_HOURS`)
- **Runs automatically**: You don't need to do anything

**Example (default settings):**
- System starts Monday at 10:00 AM
- First suggestions: Monday at 11:00 AM (1 hour delay)
- Next suggestions: Tuesday at 11:00 AM (24 hours later)
- Continues daily

**Configuration:**
- `CLUSTER_STARTUP_DELAY_HOURS`: Hours to wait after startup before first run (default: 1.0)
- `CLUSTER_INTERVAL_HOURS`: Hours between clustering runs (default: 24)

### How Does It Find Duplicates?

The system uses a smart two-step process:

**Step 1: Quick Filtering**
- Checks if faces appear in the same cameras
- Checks if they appear at similar times
- Checks if they have similar appearance counts
- This quickly finds potential matches

**Step 2: Face Verification**
- Actually compares the face images
- Verifies they look similar (not just same location)
- Only suggests if faces actually match

**Step 3: Confidence Score**
- Each suggestion gets a confidence percentage
- Higher = more likely to be the same person
- 85%+ = Very confident
- 70-85% = Review carefully
- <70% = Probably not the same person

### How to Use Suggestions

1. Go to **Admin → Unknown Faces** (or **UNKNOWN FACES** for regular users with pipeline access)
2. Click **"MERGE SUGGESTIONS"** button
3. Review each suggestion:
   - Look at both face photos
   - Check the confidence score
   - Review where/when they appeared
   - **Regular Users**: Only see suggestions for identities from your assigned pipelines
4. Decide:
   - **APPROVE**: If you agree they're the same person
   - **REJECT**: If they're different people
   - **VIEW**: To see more details first

**Access Control:**
- **Admin**: Can view and manage all merge suggestions
- **Regular Users with Pipeline Access**: Can view and manage merge suggestions only for identities from their assigned pipelines
- **Regular Users without Pipeline Access**: Cannot access merge suggestions

### Why This Works Well

✅ **85-92% accurate**: Most suggestions are correct
✅ **Checks actual faces**: Not just location patterns
✅ **Fewer mistakes**: Much better than guessing
✅ **Saves time**: Finds duplicates automatically

### ML-Powered Similarity Model (Advanced)

**NEW FEATURE:** The system now uses a trainable neural network to improve merge suggestion accuracy!

**How It Works:**
1. **Initial State**: Uses heuristic similarity (weighted combination of features)
2. **Learning Phase**: Automatically collects training data when you approve/reject suggestions
3. **Training**: After 50+ samples, train the model via API
4. **Prediction**: Uses trained model for more accurate confidence scores
5. **Continuous Learning**: Retrain periodically with new feedback

**Model Architecture:**
- **Type**: Multi-Layer Perceptron (Neural Network)
- **Input Features**: 6 features (embedding similarity, pipeline overlap, quality scores, etc.)
- **Architecture**: 6 inputs → 64 neurons → 32 neurons → 1 output
- **Output**: Predicted confidence score (0.0-1.0)

**Training the Model:**

**Step 1: Collect Training Data**
- Approve/reject merge suggestions (automatic collection)
- Each approval = positive sample (label=1.0)
- Each rejection = negative sample (label=0.0)
- Samples are stored in the database permanently (they survive restarts)

**Step 2: Check Model Status and Dataset Readiness**
```bash
GET /api/admin/merge-suggestions/model-status
```

The response tells you exactly what is blocking training. Sample count is
NOT the only rule — the dataset must also be balanced:

```json
{
  "ready_to_train": false,
  "readiness_reason": "insufficient_rejected_samples",
  "readiness_checks": {
    "total_samples":   {"passed": true,  "current": 75, "required": 50},
    "approved_samples":{"passed": true,  "current": 65, "required": 5},
    "rejected_samples":{"passed": false, "current": 10, "required": 5},
    "class_balance":   {"passed": false, "ratio": 0.13, "minimum_ratio": 0.2}
  },
  "active_model": {"artifact_name": "similarity-model-v3", "version": 3},
  "candidate_model": null
}
```

**Step 3: Schedule a Training Job (runs in the background)**

Training does NOT run inside the HTTP request. You schedule a job and
poll it:

```bash
# Schedule (202 Accepted). Cookie clients must send the CSRF header.
curl -X POST "http://localhost/api/admin/merge-suggestions/training-jobs" \\
  -H "Authorization: Bearer $TOKEN"
# -> {"accepted": true, "job_id": "simtrain-ab12cd34", "status": "scheduled"}

# Poll progress (stages: collecting_dataset, validating_dataset,
# splitting_dataset, training, evaluating, saving_candidate,
# validating_artifact)
curl "http://localhost/api/admin/merge-suggestions/training-jobs/simtrain-ab12cd34" \\
  -H "Authorization: Bearer $TOKEN"

# Cancel a running job
curl -X POST "http://localhost/api/admin/merge-suggestions/training-jobs/simtrain-ab12cd34/cancel" \\
  -H "Authorization: Bearer $TOKEN"
```

If a job is already running you get `409 TRAINING_ALREADY_RUNNING` with the
running `job_id`. If the dataset is not usable you get `400 DATASET_NOT_READY`
with the readiness checks above.

**Step 4: Review the Candidate — training never replaces the live model**

A finished job produces a **candidate** model version. The active model keeps
serving until you approve the candidate:

```json
{
  "model_id": 12,
  "version": 4,
  "artifact_name": "similarity-model-v4",
  "validation_metrics": {
    "precision": 0.991, "recall": 0.964, "f1": 0.977,
    "false_merge_rate": 0.006, "missed_merge_rate": 0.036,
    "r2": 0.88, "mse": 0.031, "sample_count": 120,
    "confusion_matrix": {"tp": 54, "fp": 1, "tn": 61, "fn": 4}
  },
  "quality_gates": {"passed": true, "gates": {...}},
  "comparison": {"active_available": true, "recommendation": "promote"},
  "awaiting_approval": true
}
```

Why precision and false-merge rate matter more than R²: **a false merge
combines two different people into one identity**, which corrupts data and is
much more harmful than a missed suggestion. Quality gates enforce a minimum
precision and a maximum false-merge rate — a candidate that fails them
**cannot be activated at all**.

**Step 5: Activate, Reject or Roll Back**
```bash
# Promote the candidate (atomic; previous active becomes 'archived')
curl -X POST "http://localhost/api/admin/merge-suggestions/models/12/activate?reason=better+precision" \\
  -H "Authorization: Bearer $TOKEN"

# Reject a candidate you do not want
curl -X POST "http://localhost/api/admin/merge-suggestions/models/12/reject?reason=degraded+recall" \\
  -H "Authorization: Bearer $TOKEN"

# Roll back to a previously active (archived) version
curl -X POST "http://localhost/api/admin/merge-suggestions/models/11/rollback?reason=regression" \\
  -H "Authorization: Bearer $TOKEN"

# Full version history
curl "http://localhost/api/admin/merge-suggestions/models" -H "Authorization: Bearer $TOKEN"
```

Activation verifies the artifact hash, loads the model and runs an inference
smoke test **before** swapping anything. If any check fails, the current model
keeps serving and nothing changes.

**Benefits:**
- ✅ Learns from your feedback (persistent dataset — survives restarts)
- ✅ Train/validation split is grouped by identity pair (no data leakage)
- ✅ Every version is recorded with metrics, dataset hash and artifact hash
- ✅ Safe: candidates are reviewed, gated, and rollback is one call

**Configuration:**
- `SIMILARITY_MODEL_MIN_SAMPLES`: Minimum samples before training (default: 50)
- `SIMILARITY_MODEL_AUTO_TRAIN`: Auto-train when enough samples (default: true)
- Model artifact locations are managed server-side; the API only exposes
  logical names such as `similarity-model-v4` (never filesystem paths).
                """,
                "examples": [
                    {
                        "title": "Multi-Select & Preview Merge (Recommended)",
                        "description": "Use production-grade merge with preview in admin interface",
                        "steps": [
                            "1. Go to Admin → Unknown Faces",
                            "2. Click 'MULTI-SELECT' button (top right)",
                            "3. Click on 2+ identity cards to select (they highlight green)",
                            "4. Click 'MERGE SELECTED' button",
                            "5. Click 'PREVIEW' button (blue) to see what will happen",
                            "6. Review: target identity, type promotion, snapshot selection, statistics",
                            "7. Add notes (optional)",
                            "8. Click 'EXECUTE MERGE'",
                            "9. All identities merged with full audit trail"
                        ]
                    },
                    {
                        "title": "Preview Merge via API (Production Best Practice)",
                        "description": "Always preview before merging!",
                        "api_example": {
                            "method": "POST",
                            "url": "/api/admin/identities/merge-preview",
                            "body": {
                                "identity_ids": ["uuid-1", "uuid-2", "uuid-3"],
                                "target_identity_id": None
                            },
                            "response": {
                                "success": True,
                                "target_identity": {"id": "uuid-1", "type": "unknown", "auto_selected": True},
                                "type_promotion": {"will_change": True, "to_type": "known"},
                                "statistics": {"total_identities": 3, "total_appearances": 100},
                                "warnings": ["Cross-pipeline merge warning"]
                            }
                        }
                    },
                    {
                        "title": "Execute Multi-Merge via API",
                        "description": "Merge multiple identities with AI target selection",
                        "api_example": {
                            "method": "POST",
                            "url": "/api/admin/identities/merge-multiple",
                            "body": {
                                "identity_ids": ["uuid-1", "uuid-2", "uuid-3"],
                                "target_identity_id": None,
                                "notes": "Merging duplicate identities"
                            },
                            "response": {
                                "success": True,
                                "merged_count": 2,
                                "identity": {"id": "uuid-1", "type": "known"},
                                "type_promotion": {"changed": True},
                                "statistics": {"pipeline_count": 3}
                            }
                        }
                    },
                    {
                        "title": "Simple Two-Identity Merge",
                        "description": "Basic merge for just 2 identities",
                        "api_example": {
                            "method": "POST",
                            "url": "/api/admin/identities/merge",
                            "body": {
                                "from_identity_id": "source-uuid",
                                "to_identity_id": "target-uuid",
                                "notes": "These are the same person"
                            },
                            "response": {
                                "success": True,
                                "merged_identity": {
                                    "id": "target-uuid",
                                    "appearances_count": 15,
                                    "type": "known"
                                }
                            }
                        }
                    },
                    {
                        "title": "Approve Merge Suggestion",
                        "description": "Approve an automatically generated merge suggestion",
                        "api_example": {
                            "method": "POST",
                            "url": "/api/admin/merge-suggestions/{suggestion_id}/approve",
                            "response": {
                                "success": True,
                                "message": "Merge executed successfully"
                            }
                        }
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "POST",
                        "path": "/api/admin/identities/merge-preview",
                        "description": "Preview what will happen when merging (RECOMMENDED: Always call before merge)",
                        "body": {
                            "identity_ids": "Array of UUIDs to merge",
                            "target_identity_id": "Optional: null for auto-select, or specify UUID"
                        },
                        "features": "Shows: AI target selection with scores, type promotion, pipeline distribution, best snapshot selection, warnings"
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/identities/merge-multiple",
                        "description": "Merge 2+ identities with production-grade features (AI selection, type promotion, FAISS management)",
                        "body": {
                            "identity_ids": "Array of UUIDs to merge",
                            "target_identity_id": "Optional: null for auto-select (recommended)",
                            "notes": "Optional notes"
                        }
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/identities/merge",
                        "description": "Simple merge of two identities into one",
                        "body": {
                            "from_identity_id": "UUID of source identity (will be merged)",
                            "to_identity_id": "UUID of target identity (will receive data)",
                            "notes": "Optional notes about the merge"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/admin/merge-suggestions",
                        "description": "Get pending merge suggestions generated by clustering",
                        "access": "Admin: all suggestions. Regular users: only suggestions for identities from their assigned pipelines"
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/merge-suggestions/{suggestion_id}/approve",
                        "description": "Approve and execute a merge suggestion",
                        "access": "Admin: can approve any. Regular users: only suggestions for identities from their assigned pipelines"
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/merge-suggestions/{suggestion_id}/reject",
                        "description": "Reject a merge suggestion (also collects training data for ML model)",
                        "access": "Admin: can reject any. Regular users: only suggestions for identities from their assigned pipelines"
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/merge-suggestions/generate-pipeline-aware",
                        "description": "Generate merge suggestions using pipeline-aware ML clustering (filters by user pipelines, uses ML similarity model)",
                        "features": "Pipeline filtering, ML-based similarity, user-specific suggestions"
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/merge-suggestions/train-model",
                        "description": "Train the ML similarity model using collected user feedback",
                        "query_params": {
                            "min_samples": "Minimum samples required (default: 50)"
                        },
                        "response": {
                            "success": "boolean",
                            "metrics": {
                                "train_r2_score": "Training accuracy (R² score)",
                                "val_r2_score": "Validation accuracy",
                                "train_mse": "Training mean squared error",
                                "val_mse": "Validation mean squared error"
                            }
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/admin/merge-suggestions/model-status",
                        "description": "Get status of the ML similarity model",
                        "response": {
                            "is_trained": "Whether model is trained",
                            "training_samples": "Number of collected samples",
                            "ready_to_train": "Whether enough samples for training"
                        }
                    }
                ]
            },
            {
                "title": "Quick Search",
                "description": "Learn how to quickly search for identities using an image",
                "content": """
# Searching by Image

## What Is Image Search?

**Image Search** lets you upload a photo of a person to find if they already exist in the system. It's like a reverse lookup - you have a photo and want to find their record.

## When to Use Image Search

✅ **Use when:**
- You have a photo of someone and want to find their record
- You want to check for duplicates before promoting
- You want to identify an unknown face
- You want to verify if someone is already in the system

## Step-by-Step: How to Search

**Step 1: Open Search**
- Go to **Admin → Unknown Faces**
- Click **"SEARCH BY IMAGE"** button (top right)
- A search form will appear

**Step 2: Upload Image**
- Click **"Choose File"** or drag-drop an image
- Supported formats: JPG, PNG
- Make sure the image shows a clear face

**Step 3: Choose Search Scope**
- **Known Only**: Search only people you've named
- **Unknown Only**: Search only unknown faces
- **Both** (recommended): Search everything

**Step 4: Set Number of Results**
- Default: 10 results
- You can change this if needed

**Step 5: Search**
- Click **"SEARCH"** button
- Wait a few seconds for results

**Step 6: Review Results**
- Results show faces sorted by similarity
- Each result shows:
  - Face photo
  - Similarity percentage (higher = better match)
  - Name (if known) or "Unknown"
  - When last seen
  - How many appearances

## Understanding Similarity Scores

**What the numbers mean:**
- **90-100%**: Almost certainly the same person
- **80-90%**: Very likely the same person
- **70-80%**: Probably the same person (review carefully)
- **60-70%**: Might be the same person (check photos)
- **Below 60%**: Probably different people

**What to look for:**
- Higher scores = better matches
- Compare the photos visually
- Check where/when they appeared
- Use your judgment - the system helps, but you decide

## Tips for Best Results

1. **Use clear photos**: Front-facing, good lighting
2. **Show the face clearly**: No sunglasses, hats covering face
3. **Good quality**: Not blurry or pixelated
4. **Search both**: Use "Both" scope for best results
5. **Review carefully**: Don't just trust the score - look at photos

## Technical Documentation

📖 For complete technical details on how Quick Search works internally (FAISS, embeddings, thresholds), see:
**Docs/38_SEARCH_BY_IMAGE_GUIDE.md**
                """,
                "examples": [
                    {
                        "title": "Search via Frontend",
                        "description": "Use the admin interface to quickly search by image",
                        "steps": [
                            "1. Go to Admin → Unknown Faces",
                            "2. Click 'QUICK SEARCH' button",
                            "3. Upload or drag-drop an image",
                            "4. Select scope: 'Both' (recommended)",
                            "5. Set number of results (default: 10)",
                            "6. Click 'Search'",
                            "7. Review matches with similarity scores",
                            "8. Click on a match to view identity details"
                        ]
                    },
                    {
                        "title": "Search via API (cURL)",
                        "description": "Example using cURL with file upload",
                        "code": """curl -X POST "https://your-domain.com/api/search/by-image" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -F "image=@/path/to/face.jpg" \\
  -F "scope=both" \\
  -F "top_k=10" """
                    },
                    {
                        "title": "Search via JavaScript",
                        "description": "Example using FormData",
                        "code": """async function searchByImage(imageFile) {
  const formData = new FormData();
  formData.append('image', imageFile);
  formData.append('scope', 'both');
  formData.append('top_k', '10');
  
  const response = await fetch('/api/search/by-image', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${getAuthToken()}`
    },
    body: formData
  });
  
  const results = await response.json();
  return results; // Array of SearchResult objects
}"""
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "POST",
                        "path": "/api/search/by-image",
                        "description": "Search for identities by uploading a face image",
                        "content_type": "multipart/form-data",
                        "parameters": {
                            "image": "File (required) - Face image file",
                            "scope": "string (optional) - 'known', 'unknown', or 'both' (default: 'both')",
                            "top_k": "integer (optional) - Number of results (default: 10)",
                            "date_from": "string (optional) - Filter from date",
                            "date_to": "string (optional) - Filter to date",
                            "pipeline_id": "string (optional) - Filter by pipeline"
                        },
                        "response": {
                            "results": [
                                {
                                    "identity_id": "uuid",
                                    "similarity": 0.85,
                                    "type": "known",
                                    "display_name": "John Doe",
                                    "best_snapshot_path": "path/to/snapshot.jpg"
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "title": "Advanced Search Intelligence",
                "description": "Production-grade search with multi-face detection, watchlists, intelligence analytics, and export",
                "content": """
# Advanced Search Intelligence System

## Overview

The **Advanced Search Intelligence System** provides production-grade face search capabilities with:
- 🔍 **Multi-Face Detection**: Search all faces in one image simultaneously
- 📊 **Face Quality Scoring**: Automatic quality assessment before search
- 🎯 **Watchlist Checking**: Automatic VIP/Threat/POI detection
- 📜 **Search History**: Track and review all past searches
- 🔗 **Intelligence Analysis**: Related identities, temporal patterns, cross-camera tracking
- 📤 **Export Results**: Download as CSV, JSON, or PDF
- 🚫 **Negative Search**: Exclude specific identities or watchlists

## Accessing Advanced Search

**Navigation:** Admin Panel → **SEARCH & INTELLIGENCE** → **Advanced Search**

## Features

### 1. Multi-Face Detection

Upload an image with multiple people, and the system will:
- Detect ALL faces automatically
- Search each face independently
- Show results grouped by face
- Display quality scores for each face

**Use Case:** CCTV frames, group photos, surveillance footage

### 2. Face Quality Scoring

Before searching, the system assesses:
- **Blur** (30% weight): Sharpness of facial features
- **Lighting** (25% weight): Even illumination
- **Face Size** (20% weight): Face pixels relative to image
- **Angle** (25% weight): Frontal vs profile

**Quality Bands:**
- **Excellent** (85-100%): Best results ✅
- **Good** (70-84%): Reliable ✅
- **Moderate** (50-69%): May vary ⚠️
- **Poor** (30-49%): Unreliable ⚠️
- **Unusable** (0-29%): Skip search ❌

### 3. Watchlist Checking

Automatically checks all search results against:
- **VIP Lists**: Important guests, executives
- **Threat Lists**: Security threats, banned persons
- **POI Lists**: Persons of Interest
- **Custom Lists**: Your own watchlists

**Watchlist Alerts** appear prominently in results with:
- List name and priority
- Action instructions
- Notes and context

### 4. Negative Search (Exclude)

**Exclude Identities:**
- Select specific identities to exclude from results
- Useful when searching for "everyone except these people"

**Exclude Watchlists:**
- Exclude all identities on specific watchlists
- Example: Find visitors only (exclude all employees)

**Use Cases:**
- Find unknown faces, excluding all employees
- Search for visitors only
- Find who else was present besides known attendees

### 5. Batch Search

Upload multiple images at once:
- Process up to 100 images in parallel
- Progress tracking per image
- Aggregated results with unique identity tracking
- Watchlist alerts across all images

**Use Case:** Bulk investigation, folder of photos, forensic analysis

### 6. Search History

**View Past Searches:**
- All searches are automatically logged
- Filter by type (single, multi, batch)
- Filter by date range
- View search details and results summary

**Export History:**
- Download search history as CSV or JSON
- Useful for audit and reporting

### 7. Export Results

**Formats Available:**
- **CSV**: Spreadsheet-compatible format
- **JSON**: Structured data format
- **PDF**: Formatted report with images

**Export Options:**
- Include/exclude quality scores
- Include images (JSON only)
- Custom filename with date

### 8. Intelligence Analysis

Access via: **SEARCH & INTELLIGENCE** → **Intelligence Analysis**

**Related Identities:**
- Find people who frequently appear together
- Relationship strength (strong, moderate, weak)
- Co-appearance statistics
- Common locations

**Temporal Patterns:**
- Hourly distribution (when do they appear)
- Daily patterns (weekday vs weekend)
- Location distribution
- Peak hours and days
- Behavioral insights

**Cross-Camera Tracking:**
- Movement path across cameras
- Timeline view with timestamps
- Map view (if coordinates available)
- Transit times between locations

**Complete Analysis:**
- Combined view of all intelligence data
- One-click comprehensive report

## Step-by-Step: Advanced Search

**Step 1: Navigate to Advanced Search**
- Click **SEARCH & INTELLIGENCE** in navbar
- Select **Advanced Search**

**Step 2: Upload Image**
- Drag & drop or click to browse
- Supports: JPG, PNG, WEBP
- Enable **Batch Mode** for multiple images

**Step 3: Configure Search**
- **Scope**: Known, Unknown, or Both
- **Max Results**: 5, 10, 20, or 50 per face
- **Check Watchlists**: Enable/disable
- **Exclude**: Select identities/watchlists to exclude

**Step 4: Set Filters (Optional)**
- Date range (from/to)
- Pipeline/Camera filter
- Minimum quality threshold

**Step 5: Search**
- Click **Search** button
- View progress for batch operations
- Results appear grouped by face

**Step 6: Review Results**
- Results grouped by confidence bands:
  - ✅ Very High (90-100%)
  - 🔵 High (75-89%)
  - 🟡 Medium (60-74%)
  - 🔴 Low (40-59%)
- Watchlist alerts highlighted
- Quality warnings shown

**Step 7: Export (Optional)**
- Click **Export** button
- Choose format (CSV/JSON/PDF)
- Select options
- Download file

## Step-by-Step: Intelligence Analysis

**Step 1: Navigate to Intelligence**
- Click **SEARCH & INTELLIGENCE** → **Intelligence Analysis**

**Step 2: Select Identity**
- Search or select from dropdown
- Identity info displayed

**Step 3: Choose Analysis Tab**
- **Related Identities**: Co-appearance analysis
- **Temporal Patterns**: When/where they appear
- **Cross-Camera Tracking**: Movement paths
- **Complete Analysis**: All data combined

**Step 4: Configure Analysis**
- Set filters (co-appearances, time window, date range)
- Click **Refresh** or **Analyze**

**Step 5: Review Results**
- Visual charts and graphs
- Timeline views
- Map views (if coordinates available)
- Export data if needed

## API Endpoints

### Advanced Search
- `POST /api/search/advanced` - Multi-face search with all features
- `POST /api/search/batch` - Batch search multiple images
- `POST /api/search/quality-check` - Check quality without searching
- `GET /api/search/config` - Get search configuration

### Search History
- `GET /api/search/history` - Get search history with filters
- `GET /api/search/history/export` - Export history

### Export
- `POST /api/search/export` - Export search results
- `POST /api/search/batch/export` - Export batch results

### Intelligence
- `GET /api/identities/{id}/related` - Get related identities
- `GET /api/identities/{id}/temporal-patterns` - Get temporal patterns
- `GET /api/identities/{id}/cross-camera` - Get cross-camera tracking
- `GET /api/identities/{id}/analyze` - Complete analysis

## Technical Documentation

📖 For complete technical details, see:
- **Docs/39_ADVANCED_SEARCH_INTELLIGENCE_GUIDE.md** - Complete feature guide
- **Docs/43_ADVANCED_SEARCH_IMPLEMENTATION_STATUS.md** - Implementation status
                """,
                "examples": [
                    {
                        "title": "Advanced Search via Frontend",
                        "description": "Use the admin interface for advanced search",
                        "steps": [
                            "1. Navigate to SEARCH & INTELLIGENCE → Advanced Search",
                            "2. Upload image (or enable Batch Mode for multiple)",
                            "3. Configure scope, filters, and exclude options",
                            "4. Click Search",
                            "5. Review results grouped by confidence bands",
                            "6. Check watchlist alerts if any",
                            "7. Export results if needed (CSV/JSON/PDF)"
                        ]
                    },
                    {
                        "title": "View Search History",
                        "description": "Review past searches",
                        "steps": [
                            "1. Navigate to SEARCH & INTELLIGENCE → Search History",
                            "2. Filter by type (single/multi/batch) or date range",
                            "3. View search details and statistics",
                            "4. Export history if needed"
                        ]
                    },
                    {
                        "title": "Intelligence Analysis",
                        "description": "Analyze identity patterns and relationships",
                        "steps": [
                            "1. Navigate to SEARCH & INTELLIGENCE → Intelligence Analysis",
                            "2. Select identity to analyze",
                            "3. Choose tab: Related Identities, Temporal Patterns, or Cross-Camera Tracking",
                            "4. Configure analysis parameters",
                            "5. Review visualizations and insights",
                            "6. Use Complete Analysis for combined view"
                        ]
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "POST",
                        "path": "/api/search/advanced",
                        "description": "Advanced multi-face search with quality scoring and watchlist checking",
                        "content_type": "multipart/form-data",
                        "parameters": {
                            "image": "File (required) - Image file",
                            "scope": "string (optional) - 'known', 'unknown', or 'both'",
                            "top_k": "integer (optional) - Max results per face",
                            "min_quality": "float (optional) - Minimum quality threshold",
                            "check_watchlist": "boolean (optional) - Check watchlists",
                            "exclude_identity_ids": "string (optional) - Comma-separated UUIDs",
                            "exclude_watchlist_ids": "string (optional) - Comma-separated watchlist IDs",
                            "date_from": "string (optional) - Filter from date",
                            "date_to": "string (optional) - Filter to date",
                            "pipeline_id": "string (optional) - Filter by pipeline"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/search/history",
                        "description": "Get search history with filters",
                        "query_params": {
                            "days_back": "integer (default: 30)",
                            "search_type": "string (optional: single|multi|batch)",
                            "limit": "integer (default: 100, max: 500)",
                            "offset": "integer (default: 0)"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/identities/{identity_id}/related",
                        "description": "Get related identities (co-appearance analysis)",
                        "query_params": {
                            "min_co_appearances": "integer (optional)",
                            "time_window_minutes": "integer (optional, default: 5)",
                            "limit": "integer (default: 20, max: 100)"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/identities/{identity_id}/temporal-patterns",
                        "description": "Get temporal patterns (when/where identity appears)",
                        "query_params": {
                            "days_back": "integer (default: 90, max: 365)"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/identities/{identity_id}/cross-camera",
                        "description": "Get cross-camera tracking (movement path)",
                        "query_params": {
                            "date": "string (optional, YYYY-MM-DD)",
                            "days_back": "integer (default: 7, max: 30)"
                        }
                    }
                ]
            },
            {
                "title": "Live Alerts and Watchlists for Unknown Persons",
                "description": "Learn how to create alerts and add unknown persons to watchlists for monitoring",
                "content": """
# Live Alerts and Watchlists for Unknown Persons

## Overview

You can create **live alerts** and add **watchlists** for unknown persons even before they are identified. This allows you to monitor and track specific individuals as soon as they are detected.

## What Are Live Alerts?

**Live Alerts** notify you in real-time when a tracked person is detected again. You can create alerts for:
- ✅ **Known persons** (people with names)
- ✅ **Unknown persons** (faces not yet identified)

## What Are Watchlists?

**Watchlists** are organized lists of identities (VIP, Threat, POI, etc.) for monitoring. You can add:
- ✅ **Known persons** to watchlists
- ✅ **Unknown persons** to watchlists (even without names)

## Creating Live Alerts for Unknown Persons

### Step-by-Step

**Step 1: Find the Unknown Person**
- Go to **Admin → Unknown Faces**
- Browse through the identity cards
- Click on the identity card you want to track

**Step 2: Open Identity Details**
- Click **"VIEW"** button on the identity card
- Identity detail modal opens showing:
  - Identity information
  - Timeline of appearances
  - Snapshots gallery
  - **Identity ID** (displayed automatically)

**Step 3: Create Live Alert**
- Click **"CREATE LIVE ALERT"** button in the detail modal
- Alert creation form opens with:
  - **Identity ID** (automatically displayed - you can copy it)
  - **Identity Name** (may show "Unknown Identity" for unknown persons)
  - Default settings (provided by backend)
- Configure alert settings:
  - Alert name
  - Minimum similarity threshold
  - Notification channels (dashboard, email, SMS, webhook)
  - Cooldown period
  - Time windows (optional)
  - Expiration options
- Click **"CREATE ALERT"** to submit

**Step 4: Receive Notifications**
- When the person is detected again:
  - 🔔 Dashboard notification appears
  - 📧 Email alert sent (if configured)
  - 📱 SMS alert sent (if configured)
  - 🔗 Webhook triggered (if configured)

### Important Notes

- **Identity ID is always displayed** in the alert creation form
- This ensures you know exactly which identity the alert is tracking
- For unknown persons, the Identity ID is especially important since they may not have a name yet
- You can **copy the Identity ID** by clicking on it or the copy button
- **All validation is handled by the backend** - frontend only collects form data

## Adding Unknown Persons to Watchlists

### Step-by-Step

**Step 1: Find the Unknown Person**
- Go to **Admin → Unknown Faces**
- Click on the identity card you want to add to a watchlist

**Step 2: Open Identity Details**
- Click **"VIEW"** button
- Identity detail modal opens

**Step 3: Add to Watchlist**
- Click **"ADD TO WATCHLIST"** button in the detail modal
- Watchlist selection form opens with:
  - **Identity Name** (may show "Unknown Identity")
  - Available watchlists (VIP, Threat, POI, etc.)
  - Default priority (provided by backend)
  - Whether identity is already on a watchlist
- Select watchlist from dropdown
- Set priority (low, normal, high, critical)
- Add notes (optional)
- Add action instructions (optional)
- Click **"ADD TO WATCHLIST"** to submit

**Step 4: Monitor**
- Identity is now on the selected watchlist
- Watchlist can trigger alerts when identity is detected
- You can add the same identity to multiple watchlists

### Important Notes

- **Identity ID is used** to link the identity to the watchlist
- Watchlists can trigger alerts when identities are detected
- You can add the same identity to multiple watchlists
- **All validation is handled by the backend** - frontend only collects form data

## Use Cases

### Use Case 1: Track Suspicious Unknown Person

**Scenario:** You see an unknown person repeatedly appearing at odd hours.

**Solution:**
1. Find the unknown person in Unknown Faces
2. Create a live alert with:
   - Alert name: "Suspicious Person - Odd Hours"
   - Time window: Only during off-hours
   - High priority notifications
3. Add to "Threat" watchlist
4. Get notified whenever they appear

### Use Case 2: Monitor VIP Arrival (Unknown)

**Scenario:** You expect a VIP but don't have their photo in the system yet.

**Solution:**
1. When they first appear, find them in Unknown Faces
2. Add them to "VIP" watchlist immediately
3. Create a live alert for their arrival
4. When they're identified, promote them to known
5. Alert and watchlist remain active

### Use Case 3: Investigation Tracking

**Scenario:** You're investigating an incident and need to track an unknown person.

**Solution:**
1. Find the unknown person from the incident time
2. Create a live alert with:
   - Alert name: "Investigation #123 - Person of Interest"
   - All cameras monitored
   - Email notifications to investigation team
3. Add to "POI" (Person of Interest) watchlist
4. Track all appearances until investigation complete

## API Examples

### Get Default Alert Settings

```bash
curl -X GET "http://localhost:8000/api/live-alerts/defaults/{identity_id}" \\
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Response includes:**
- Identity ID (always included)
- Identity name
- Default alert name
- Default similarity threshold
- Default notification settings
- User alert limits

### Create Live Alert

```bash
curl -X POST "http://localhost:8000/api/live-alerts" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "name": "Track Unknown Person - Investigation",
    "identity_id": "uuid-of-unknown-identity",
    "min_similarity": 0.75,
    "notify_dashboard": true,
    "sound_alert": true
  }'
```

### Get Watchlist Defaults

```bash
curl -X GET "http://localhost:8000/api/watchlists/add-identity/{identity_id}/defaults" \\
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Response includes:**
- Available watchlists
- Default priority
- Whether identity is already on a watchlist

### Add to Watchlist

```bash
curl -X POST "http://localhost:8000/api/watchlists/{watchlist_id}/entries" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "identity_id": "uuid-of-unknown-identity",
    "priority": "normal",
    "notes": "Monitoring for investigation"
  }'
```

## Best Practices

✅ **Create alerts early**: Don't wait to identify someone - create alerts for unknown persons immediately  
✅ **Use descriptive names**: "Suspicious Person - Main Entrance" is better than "Alert 1"  
✅ **Set appropriate thresholds**: Higher similarity (0.85+) for critical alerts  
✅ **Use time windows**: Reduce false positives by monitoring only during relevant hours  
✅ **Add to watchlists**: Organize identities into categories for better management  
✅ **Copy Identity ID**: Keep Identity ID for reference when creating alerts or adding to watchlists  

## Related Documentation

- **Live Alerts Guide**: `40_LIVE_ALERTS_GUIDE.md` - Complete guide to live alerts
- **Unknown Faces Guide**: `07_UNKNOWN_FACES_CENTER_COMPLETE_GUIDE.md` - Unknown faces management
                """,
                "examples": [
                    {
                        "title": "Create Alert for Unknown Person",
                        "description": "Track an unknown person with a live alert",
                        "steps": [
                            "1. Go to Admin → Unknown Faces",
                            "2. Click on identity card",
                            "3. Click 'VIEW' to open details",
                            "4. Click 'CREATE LIVE ALERT'",
                            "5. Review Identity ID (displayed automatically)",
                            "6. Configure alert settings",
                            "7. Click 'CREATE ALERT'",
                            "8. Receive notifications when person is detected"
                        ]
                    },
                    {
                        "title": "Add Unknown Person to Watchlist",
                        "description": "Add an unknown person to a watchlist for monitoring",
                        "steps": [
                            "1. Go to Admin → Unknown Faces",
                            "2. Click on identity card",
                            "3. Click 'VIEW' to open details",
                            "4. Click 'ADD TO WATCHLIST'",
                            "5. Select watchlist from dropdown",
                            "6. Set priority and add notes",
                            "7. Click 'ADD TO WATCHLIST'",
                            "8. Identity is now monitored via watchlist"
                        ]
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/live-alerts/defaults/{identity_id}",
                        "description": "Get default settings for creating a live alert (includes Identity ID)",
                        "note": "Identity ID is always displayed in the response"
                    },
                    {
                        "method": "POST",
                        "path": "/api/live-alerts",
                        "description": "Create a live alert for an identity (known or unknown)",
                        "body": {
                            "name": "string (required)",
                            "identity_id": "string (required, UUID)",
                            "min_similarity": "float (default: 0.75)",
                            "notify_dashboard": "boolean (default: true)"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/watchlists/add-identity/{identity_id}/defaults",
                        "description": "Get default settings and available watchlists for adding an identity",
                        "note": "Shows which watchlists the identity is already on"
                    },
                    {
                        "method": "POST",
                        "path": "/api/watchlists/{watchlist_id}/entries",
                        "description": "Add an identity to a watchlist",
                        "body": {
                            "identity_id": "string (required, UUID)",
                            "priority": "string (low|normal|high|critical)",
                            "notes": "string (optional)"
                        }
                    }
                ]
            },
            {
                "title": "System Settings Management",
                "description": "Learn how to view, modify, and track system configuration settings",
                "content": """
# System Settings Management

## What Are Settings?

Settings are configuration variables that control how the system behaves. Think of them as the "control panel" for your Face Recognition Service. Examples include cache size, similarity thresholds, data retention periods, and clustering intervals.

## Accessing Settings

**Path**: Admin → Settings

**Requirements**: Admin role only

## Understanding the Settings Page

The settings page has three main sections:

**1. Category Filters (Top)**
- Filter settings by category (Database, Security, Face Recognition, etc.)
- "All Settings" shows everything
- Red "Refresh" button reloads settings

**2. Settings Cards (Middle)**
- Each setting shown as a compact card
- Displays: Setting key, value, type, category, badges
- Edit button (if editable) or Readonly indicator

**3. Audit Log (Bottom)**
- Complete history of all setting changes
- Shows: Who changed it, when, old/new values, reason

## How to View Settings

**Step 1**: Navigate to Admin → Settings

**Step 2**: Browse settings or use category filters

**Step 3**: Click "Edit" to see full details (if editable)

## How to Edit Settings

**Step 1**: Find the setting you want to change

**Step 2**: Click the **Edit** button (pencil icon)

**Step 3**: Review current value in the modal

**Step 4**: Enter new value (follow type hints: string, number, True/false)

**Step 5**: Add change reason (optional but recommended)

**Step 6**: Click **"Save Changes"**

**Step 7**: Verify in audit log at bottom of page

## Setting Types

- **String**: Text values (e.g., `"production"`)
- **Integer**: Whole numbers (e.g., `50000`)
- **Float**: Decimal numbers (e.g., `0.4`)
- **Boolean**: True/false (e.g., `True`, `false`)
- **List**: Comma-separated values (e.g., `item1,item2,item3`)

## Setting Badges

- 🔒 **Sensitive**: Value hidden for security (passwords, keys)
- 🔒 **Readonly**: Cannot be modified (system-protected)
- **Category Badge**: Shows the setting category

## Common Settings

**Performance:**
- `WORKERS`: Number of worker processes
- `BATCH_SIZE`: Processing batch size
- `CACHE_LOCAL_SIZE`: Cache size in memory

**Face Recognition:**
- `SIMILARITY_THRESHOLD`: Face matching threshold (0.0-1.0)
- `CONFIDENCE_THRESHOLD`: Detection confidence (0.0-1.0)

**Data Retention:**
- `DATA_RETENTION_DAYS`: How long to keep data
- `SNAPSHOT_RETENTION_DAYS`: Photo retention period

**Clustering:**
- `CLUSTER_INTERVAL_HOURS`: How often to generate merge suggestions
- `CLUSTER_EPS`: Clustering similarity threshold

## Best Practices

✅ **Before changing**: Understand the impact, test in development
✅ **When changing**: Add a reason, change one at a time, verify type
✅ **After changing**: Verify in audit log, test functionality, monitor

## Audit Log

The audit log tracks:
- Which setting changed
- Old and new values
- Who made the change
- When it changed
- Why (if reason provided)

Use it for security, troubleshooting, compliance, and rollback planning.

## Troubleshooting

**Setting won't save?**
- Check type matches (string, number, etc.)
- Check if readonly
- Check validation rules

**Setting not taking effect?**
- Some settings require system restart
- Check if value was actually saved
- Verify in audit log

**Can't see value?**
- Sensitive settings are hidden
- Check edit modal or audit log
                """,
                "examples": [
                    {
                        "title": "View All Settings",
                        "description": "Browse all system settings",
                        "steps": [
                            "1. Go to Admin → Settings",
                            "2. Click 'All Settings' (or specific category)",
                            "3. Browse settings cards",
                            "4. Click 'Edit' to see details"
                        ]
                    },
                    {
                        "title": "Edit a Setting",
                        "description": "Change a configuration value",
                        "steps": [
                            "1. Find the setting you want to change",
                            "2. Click 'Edit' button",
                            "3. Review current value",
                            "4. Enter new value",
                            "5. Add change reason (optional)",
                            "6. Click 'Save Changes'",
                            "7. Verify in audit log"
                        ]
                    },
                    {
                        "title": "View Audit Log",
                        "description": "See history of setting changes",
                        "steps": [
                            "1. Go to Admin → Settings",
                            "2. Scroll to 'Audit Log' section",
                            "3. Review change history",
                            "4. See who changed what and when"
                        ]
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/settings",
                        "description": "Get all settings (filtered by category if specified)",
                        "parameters": {
                            "category": "Optional - Filter by category"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/settings/{setting_key}",
                        "description": "Get a specific setting by key"
                    },
                    {
                        "method": "PUT",
                        "path": "/api/settings/{setting_key}",
                        "description": "Update a setting value",
                        "body": {
                            "value": "New value (must match setting type)",
                            "change_reason": "Optional reason for the change"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/settings/audit/log",
                        "description": "Get audit log of setting changes",
                        "parameters": {
                            "limit": "Number of entries to return (default: 50)"
                        }
                    }
                ]
            },
            {
                "title": "FAISS Index Repair and Synchronization",
                "description": "Learn about the automatic FAISS index repair system and how to configure it",
                "content": """
# FAISS Index Repair and Synchronization

## What is FAISS?

FAISS (Facebook AI Similarity Search) is the vector database that stores face embeddings. The system uses two FAISS indexes:
- **KNOWN Index**: Stores embeddings for known persons (people you've named)
- **UNKNOWN Index**: Stores embeddings for unknown faces

## Why Repair is Needed

FAISS indexes can become out of sync with the database due to:
- Orphaned entries (vectors without database records)
- Missing entries (database records without vectors)
- Size mismatches (FAISS has more vectors than metadata)
- Data corruption from crashes or improper shutdowns

## Automatic Repair System

The system **automatically repairs** FAISS indexes to keep them synchronized with the database.

### When Repair Runs

**1. On Startup (Optional)**
- Runs after loading known faces from `storage/faces`
- Detects and fixes orphaned entries
- Rebuilds indexes if needed
- **Configurable**: Can be disabled for faster startup

**2. Background Repair (Periodic)**
- Runs automatically every 24 hours (configurable)
- Runs after configurable startup delay (default: 1 hour), then at specified interval
- Non-blocking - doesn't affect system performance
- **Configurable**: Set interval in hours (0 = disabled)

### Repair Strategies by Scale

The system uses smart strategies based on index size:

**Small Mismatches (< 1% or < 100 entries):**
- Uses **Lazy Marking** approach
- Marks orphaned vectors, skips them during search
- **Instant** - No performance impact
- Perfect for large scale

**Medium Indexes (< 50k vectors):**
- **Immediate Rebuild** if mismatch is large
- Rebuilds index from database
- Acceptable startup delay (1-5 seconds)
- Ensures clean state from start

**Large Indexes (≥ 50k vectors):**
- **Background Rebuild** for large mismatches
- Schedules rebuild without blocking
- System continues operating with old index
- Switches to new index when ready

## Configuration

### Settings Available in Admin UI

Navigate to **Admin → Settings** and filter by **"identity"** category:

**1. REPAIR_FAISS_ON_STARTUP**
- **Type**: Boolean (True/false)
- **Default**: `True`
- **Description**: Enable/disable FAISS index repair on application startup
- **When to Disable**: Very large indexes (> 1M vectors) where startup time is critical
- **Recommendation**: Keep enabled for most deployments

**2. REPAIR_FAISS_INTERVAL_HOURS**
- **Type**: Integer
- **Default**: `24` (hours)
- **Description**: Background repair interval in hours
- **Recommendations**:
  - Small deployments (< 10k vectors): 12 hours
  - Medium deployments (10k-100k vectors): 24 hours (default)
  - Large deployments (> 100k vectors): 48 hours
  - Set to `0` to disable background repair

### How to Configure

**Via Admin UI:**
1. Go to **Admin → Settings**
2. Filter by **"identity"** category
3. Find `REPAIR_FAISS_ON_STARTUP` or `REPAIR_FAISS_INTERVAL_HOURS`
4. Click **Edit** button
5. Enter new value
6. Add change reason (optional)
7. Click **Save Changes**

**Via Environment Variables:**
```bash
# Enable/disable repair on startup
REPAIR_FAISS_ON_STARTUP=True

# Background repair interval (hours)
REPAIR_FAISS_INTERVAL_HOURS=24
```

## Monitoring and Verification

### Startup Logs

When the system starts, you'll see repair logs:

**Healthy System:**
```
✅ No orphaned entries found - indexes are clean
📊 Index Verification:
   KNOWN: FAISS=1000, DB=1000, Match=True
```

**Repair in Action:**
```
🔧 Repairing orphaned FAISS entries (efficient mode)...
[FAISS_REPAIR] Removed 5 orphaned KNOWN embeddings
💾 Saved repaired indexes to disk
📊 Index Verification AFTER Repair:
   KNOWN: FAISS=1000, DB=1000, Match=True
```

**Large Mismatch (Background Rebuild):**
```
⚠️ FAISS KNOWN index has 1200 vectors but metadata only has 1000 entries!
[FAISS_REPAIR] Large index detected. Scheduling background rebuild...
[FAISS_REPAIR] Background rebuild scheduled. Will rebuild without blocking.
```

### Verification via API

Check index status:

```bash
GET /api/admin/identities/verify-indexes
```

**Response:**
```json
{
  "known_index": {
    "faiss_count": 1000,
    "database_count": 1000,
    "match": True,
    "issues": []
  },
  "unknown_index": {
    "faiss_count": 500,
    "database_count": 500,
    "match": True,
    "issues": []
  }
}
```

## Troubleshooting

### Issue: FAISS count != Database count

**Symptoms:**
```
KNOWN: FAISS=18, DB=9, Match=False
```

**Solution:**
- Repair should run automatically on startup
- Check `REPAIR_FAISS_ON_STARTUP=True` in settings
- Restart application to trigger repair
- If persists, check repair logs for errors

### Issue: Repair Taking Too Long

**Symptoms:**
- Startup takes minutes
- System appears frozen

**Solution:**
1. **Disable startup repair:**
   - Set `REPAIR_FAISS_ON_STARTUP=false`
2. **Rely on background repair** - Runs automatically every 24 hours
3. **For very large indexes** - Background rebuild is automatic

### Issue: Known Faces Not Recognized

**Symptoms:**
- Known faces appear as unknown
- FAISS search returns no matches

**Solution:**
1. **Verify known faces loaded:**
   - Check logs: `✅ Loaded X known faces from storage/faces`
2. **Check index size:**
   - Verify: `KNOWN index: X vectors, Y identities`
3. **Reload known faces:**
   - Delete index files and restart
   - Known faces will be reloaded automatically

## Best Practices

### For Production

**Recommended Settings:**
```bash
REPAIR_FAISS_ON_STARTUP=True      # Enable for data integrity
REPAIR_FAISS_INTERVAL_HOURS=24    # Daily background repair
```

### For Development

**Faster Iteration:**
```bash
REPAIR_FAISS_ON_STARTUP=false     # Disable for faster startup
REPAIR_FAISS_INTERVAL_HOURS=48    # Less frequent background repair
```

### For Large Scale (1M+ vectors)

**Optimized Settings:**
```bash
REPAIR_FAISS_ON_STARTUP=false     # Disable for faster startup
REPAIR_FAISS_INTERVAL_HOURS=48    # Less frequent background repair
```

**Why:**
- Startup repair can take minutes for very large indexes
- Background repair handles everything automatically
- System uses lazy marking for small mismatches (instant)

## What Happens During Repair

### 1. Orphaned Identity Removal
- Finds FAISS entries for identities that don't exist in database
- Removes them from metadata
- Keeps FAISS vectors (lazy marking approach)

### 2. Orphaned Embedding Removal
- Finds FAISS vectors that don't have database records
- Marks them as orphaned
- Skips them during search

### 3. Index Rebuild (if needed)
- Reconstructs valid embeddings from current index
- Creates new clean index
- Updates all metadata and database records

## Summary

✅ **Automatic**: Repair runs automatically on startup and in background  
✅ **Efficient**: Smart strategies for all scales (lazy marking, background rebuild)  
✅ **Configurable**: All settings available in Admin UI  
✅ **Non-Blocking**: Background operations don't affect system performance  
✅ **Scalable**: Works with millions of vectors  

**Key Takeaways:**
1. Repair is **automatic** - you don't need to do anything
2. **Small mismatches** are handled instantly (lazy marking)
3. **Large indexes** are rebuilt in background (non-blocking)
4. **Configuration** is available in Admin → Settings
5. **Monitor** via startup logs and verification API

**See Documentation:** Check **33_FAISS_REPAIR_AND_SYNCHRONIZATION.md** for complete details.
                """,
                "examples": [
                    {
                        "title": "View Repair Settings",
                        "description": "Check current repair configuration",
                        "steps": [
                            "1. Go to Admin → Settings",
                            "2. Filter by 'identity' category",
                            "3. Find REPAIR_FAISS_ON_STARTUP",
                            "4. Find REPAIR_FAISS_INTERVAL_HOURS",
                            "5. Review current values"
                        ]
                    },
                    {
                        "title": "Change Repair Interval",
                        "description": "Update background repair frequency",
                        "steps": [
                            "1. Go to Admin → Settings",
                            "2. Filter by 'identity' category",
                            "3. Find REPAIR_FAISS_INTERVAL_HOURS",
                            "4. Click 'Edit' button",
                            "5. Enter new value (e.g., 48 for 48 hours)",
                            "6. Add change reason (optional)",
                            "7. Click 'Save Changes'",
                            "8. Verify in audit log"
                        ]
                    },
                    {
                        "title": "Verify Index Status",
                        "description": "Check if indexes are synchronized",
                        "api_example": {
                            "method": "GET",
                            "url": "/api/admin/identities/verify-indexes",
                            "response": {
                                "known_index": {
                                    "faiss_count": 1000,
                                    "database_count": 1000,
                                    "match": True,
                                    "issues": []
                                },
                                "unknown_index": {
                                    "faiss_count": 500,
                                    "database_count": 500,
                                    "match": True,
                                    "issues": []
                                }
                            }
                        }
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/admin/identities/verify-indexes",
                        "description": "Verify FAISS index synchronization with database",
                        "authentication": "Required - Admin role",
                        "response": {
                            "known_index": "Index statistics and issues",
                            "unknown_index": "Index statistics and issues"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/settings",
                        "description": "Get all settings (filter by category='identity' for repair settings)",
                        "query_parameters": {
                            "category": "Optional - 'identity' for repair settings"
                        }
                    },
                    {
                        "method": "PUT",
                        "path": "/api/settings/REPAIR_FAISS_ON_STARTUP",
                        "description": "Enable/disable repair on startup",
                        "body": {
                            "value": "True or false",
                            "change_reason": "Optional reason"
                        }
                    },
                    {
                        "method": "PUT",
                        "path": "/api/settings/REPAIR_FAISS_INTERVAL_HOURS",
                        "description": "Set background repair interval in hours",
                        "body": {
                            "value": "Integer (0 = disabled, 24 = daily, etc.)",
                            "change_reason": "Optional reason"
                        }
                    }
                ]
            },
            {
                "title": "Complete Configuration Guide",
                "description": "Learn about all system configuration options and how to customize the system for your needs",
                "content": '''
# Complete Configuration Guide

## Overview

The Face Recognition System has **100+ configuration settings** that control every aspect of system behavior. This guide explains the most important settings and common configuration scenarios.

## Configuration Methods

### 1. Web Interface (Easiest)

**Path:** Admin → Settings

**Advantages:**
- ✅ No technical knowledge required
- ✅ Visual interface with validation
- ✅ Change tracking (audit log)
- ✅ Instant feedback

**How to Use:**
1. Navigate to `/admin/settings`
2. Filter by category (e.g., "identity", "storage", "tracking")
3. Find the setting you want to change
4. Click "Edit"
5. Enter new value
6. Add reason (optional)
7. Click "Save"

### 2. Environment Variables (.env file)

Create a `.env` file in the project root:

```bash
# .env
SHOW_UNKNOWN_FACES_ON_DASHBOARD=True
SIMILARITY_THRESHOLD=0.4
LOG_LEVEL=DEBUG
```

**Advantages:**
- ✅ Easy to manage
- ✅ Works with Docker
- ✅ Version control friendly

## Important Settings by Category

### Dashboard & Visibility

**SHOW_UNKNOWN_FACES_ON_DASHBOARD**
- **Default:** `false`
- **Description:** If `True`, unknown faces appear on main dashboard. If `false`, they only appear in "Unknown Faces Center"
- **When to Change:** Set to `True` if you want to see all faces on dashboard
- **Location:** Settings → Filter by "tracking"

### Debug Settings

**SAVE_WEBHOOK_IMAGES**
- **Default:** `True`
- **Description:** Save all images received via webhook for debugging
- **Location:** Settings → Filter by "storage"

**SAVE_CROPPED_IMAGES**
- **Default:** `True`
- **Description:** Save cropped person images for debugging
- **Location:** Settings → Filter by "storage"

**When to Enable:** When debugging recognition issues or investigating why faces aren't detected

### FAISS Index Configuration

**KNOWN_INDEX_TYPE**
- **Default:** `flat`
- **Options:** `flat`, `ivf`, `hnsw`, `ivfpq`
- **Description:** Index type for known faces
  - `flat`: Best accuracy, good for <100K faces
  - `ivf`: Fast, good for 100K-1M faces
  - `hnsw`: Fastest, good for 1M-10M faces
  - `ivfpq`: Smallest memory, good for 10M+ faces
- **Location:** Settings → Filter by "identity"

**FAISS_LAZY_MARKING_THRESHOLD**
- **Default:** `1` (for demo)
- **Production:** `100` or higher
- **Description:** Threshold for lazy marking orphaned vectors
- **Location:** Settings → Filter by "identity"

### Face Recognition Accuracy

**SIMILARITY_THRESHOLD**
- **Default:** `0.4`
- **Range:** `0.0` to `1.0`
- **Description:** Minimum similarity to match faces
  - Lower (0.3-0.4): More matches, may have false positives
  - Higher (0.5-0.6): Fewer matches, more accurate
- **Location:** Settings → Filter by "models"

## Common Configuration Scenarios

### Scenario 1: Show Unknown Faces on Dashboard

**Goal:** See all faces (known and unknown) on main dashboard

**Steps:**
1. Go to Admin → Settings
2. Filter by "tracking" category
3. Find `SHOW_UNKNOWN_FACES_ON_DASHBOARD`
4. Click "Edit"
5. Change value to `True`
6. Save

**Result:** Unknown faces now appear on main dashboard alongside known faces

### Scenario 2: Debug Recognition Issues

**Goal:** Investigate why faces aren't being recognized

**Steps:**
1. Enable debug logging:
   - Find `LOG_LEVEL` in "server" category
   - Change to `DEBUG`
2. Enable image saving:
   - Find `SAVE_WEBHOOK_IMAGES` in "storage" category
   - Set to `True`
   - Find `SAVE_CROPPED_IMAGES` in "storage" category
   - Set to `True`
3. Lower similarity threshold (temporarily):
   - Find `SIMILARITY_THRESHOLD` in "models" category
   - Change to `0.3`
4. Check saved images in:
   - `debug/webhook_images/{pipeline_id}/`
   - `debug/cropped/{pipeline_id}/`

**Result:** You can now see exactly what images are being processed and why recognition might be failing

### Scenario 3: Optimize for 50+ Cameras

**Goal:** Handle high load from many cameras

**Steps:**
1. Increase workers:
   - `WORKERS=12` (for 8 CPU cores)
2. Increase queue workers:
   - `QUEUE_WORKERS=100`
3. Increase database pool:
   - `DB_POOL_SIZE=75`
   - `DB_MAX_OVERFLOW=150`
4. Increase batch size:
   - `BATCH_SIZE=50`
   - `MAX_QUEUE_SIZE=50000`

**Result:** System can handle much higher load

## Settings Categories

All settings are organized into categories:

- **server**: Basic server configuration
- **security**: Authentication and security
- **database**: PostgreSQL settings
- **cache**: Redis cache settings
- **models**: AI model configuration
- **processing**: Queue and batch processing
- **storage**: Image storage settings
- **tracking**: Face tracking optimization
- **identity**: Identity management and FAISS
- **retention**: Data cleanup settings

## Best Practices

1. **Start with Defaults**: Begin with default values and adjust only what's needed
2. **Document Changes**: Always add a reason when changing settings via web interface
3. **Test First**: Test changes in development before production
4. **Monitor Performance**: Watch system metrics after changes
5. **Backup Configuration**: Keep backups of your `.env` file

## Related Documentation

- **Complete Configuration Guide**: `36_CONFIGURATION_GUIDE.md`
- **Settings Management**: `24_SETTINGS_MANAGEMENT_GUIDE.md`
- **FAISS Scaling**: `30_FAISS_PRODUCTION_SCALING.md`
                ''',
                "examples": [
                    {
                        "title": "Change Dashboard Visibility",
                        "description": "Show unknown faces on main dashboard",
                        "steps": [
                            "1. Go to Admin → Settings",
                            "2. Filter by 'tracking' category",
                            "3. Find SHOW_UNKNOWN_FACES_ON_DASHBOARD",
                            "4. Click 'Edit'",
                            "5. Change value to 'True'",
                            "6. Add reason: 'Want to see all faces on dashboard'",
                            "7. Click 'Save'",
                            "8. Refresh dashboard to see changes"
                        ]
                    },
                    {
                        "title": "Enable Debug Mode",
                        "description": "Enable debugging features for troubleshooting",
                        "steps": [
                            "1. Go to Admin → Settings",
                            "2. Filter by 'server' category",
                            "3. Find LOG_LEVEL, change to 'DEBUG'",
                            "4. Filter by 'storage' category",
                            "5. Find SAVE_WEBHOOK_IMAGES, set to 'True'",
                            "6. Find SAVE_CROPPED_IMAGES, set to 'True'",
                            "7. Save all changes",
                            "8. Check debug directories for saved images"
                        ]
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "GET",
                        "url": "/api/settings",
                        "description": "Get all settings (filtered by category if specified)"
                    },
                    {
                        "method": "PUT",
                        "url": "/api/settings/{setting_id}",
                        "description": "Update a setting value"
                    }
                ]
            },
            {
                "title": "System Workflow",
                "description": "Understand how the system processes faces and creates identities",
                "content": """
# How the System Works

## Simple Overview

Think of the system like a smart security guard that:
1. **Watches** cameras 24/7
2. **Recognizes** faces it knows
3. **Tracks** new faces it doesn't know
4. **Helps you** identify and manage people

## What Happens When a Face Is Detected?

**Step 1: Camera Captures Image**
- A camera sends an image to the system
- The system looks for faces in the image

**Step 2: System Checks: "Do I Know This Person?"**
- First, it checks against **known people** (people you've named)
- If found → Shows their name
- If not found → Checks **unknown faces** (faces it's seen before but not named)
- If found → Links to that unknown identity
- If not found → Creates a **new unknown identity**

**Step 3: System Saves Information**
- Saves the photo
- Records which camera
- Records date and time
- Links to the identity (known or unknown)

**Step 4: You See It**
- If known → Appears in dashboard with name
- If unknown → Appears in Unknown Faces page

## The Lifecycle of a Face

**New Face Detected**
↓
**System asks: "Is this a known person?"**
↓ (No)
**System asks: "Is this an unknown face I've seen before?"**
↓ (No)
**System creates: "New Unknown Identity"**
↓
**You review it**
↓
**You can:**
- **Promote it** (give it a name) → Becomes known
- **Merge it** (combine with another) → Becomes one identity
- **Leave it** (stays unknown)

## Automated Background Jobs

The system runs automatic tasks in the background:

**1. Merge Suggestions (Daily)**
- **When**: Runs every 24 hours (first time: configurable delay after startup, default: 1 hour)
- **What it does**: Finds duplicate unknown faces automatically
- **How**: Compares faces using smart matching
- **Result**: Creates suggestions for you to review
- **You do**: Review and approve/reject suggestions

**2. Cleanup (Daily)**
- **What it does**: Removes old data to keep system fast
- **Removes**: Old photos (after 90 days), old detections
- **Marks inactive**: Faces not seen in 6 months

**3. Data Retention (Daily)**
- **What it does**: Deletes very old data
- **Keeps**: Recent data for fast access
- **Deletes**: Very old data to save space

## Your Daily Workflow

**Recommended routine:**
1. **Check Unknown Faces** (daily or weekly)
2. **Review Merge Suggestions** (when available)
3. **Promote recognized faces** (give them names)
4. **Merge duplicates** (combine same person)
5. **Search before promoting** (avoid duplicates)

## Tips for Success

✅ **Review regularly**: Check unknown faces often
✅ **Use suggestions**: System finds duplicates for you
✅ **Search first**: Before promoting, search to avoid duplicates
✅ **Verify always**: Check photos and timelines before merging
✅ **Be consistent**: Use same name format for all people

## User Pipeline Access

**For Administrators:**
- You can grant pipeline access to regular users via Admin → Users
- Users with pipeline access can manage identities from their assigned pipelines
- This allows secure delegation of identity management tasks

**For Regular Users:**
- If you have pipeline access, you can:
  - View unknown identities from your assigned pipelines
  - Promote unknown identities to known (for your pipelines)
  - Merge identities (both must be from your accessible pipelines)
  - View and act on merge suggestions (for your accessible pipelines)
- You'll only see data from pipelines you have access to
- If you try to access an identity from a different pipeline, you'll get an "Access denied" error

**See Documentation:** Check **26_USER_PIPELINE_ACCESS_GUIDE.md** for complete details on user pipeline access.
                """,
                "examples": [],
                "api_endpoints": []
            },
            {
                "title": "Live Search Alerts",
                "description": "Create real-time alerts to get notified when tracked individuals are detected",
                "content": '''
# Live Search Alerts

## Overview

Live Search Alerts allow you to track specific individuals and receive real-time notifications whenever they are detected by any camera in your surveillance system.

## Quick Start

### Step 1: Search for the Person

1. Go to **Advanced Search** (`/admin/search`)
2. Upload an image or search for the person
3. Review the match results

### Step 2: Create Live Alert

1. Click on a **match result** to view identity details
2. Click **"CREATE LIVE ALERT"** button
3. Backend automatically provides:
   - Default alert name: "Track {Identity Name} - {Date}"
   - Default similarity threshold: 75%
   - Default cooldown: 30 minutes
   - Default notifications: Dashboard enabled
4. Adjust settings as needed
5. Click **"CREATE ALERT"**

### Step 3: Receive Notifications

When the person is detected:
- 🔔 **Dashboard notification** appears instantly
- 📧 **Email alert** sent (if configured)
- 📱 **SMS alert** sent (if configured)
- 🔗 **Webhook** triggered (if configured)

## Key Features

- **Backend-Driven Defaults**: All default values come from backend (no frontend logic)
- **Smart Filtering**: Time windows, camera selection, similarity thresholds
- **Cooldown Management**: Prevents alert spam with configurable cooldown periods
- **Multi-Channel Alerts**: Dashboard, Email, SMS, Webhook notifications
- **Auto Actions**: Automatic snapshot capture and video recording
- **Expiration Options**: Set alerts to expire after date or number of detections

## API Endpoints

### Get Default Alert Settings

**Endpoint:** `GET /api/live-alerts/defaults/{identity_id}`

**Description:** Backend provides all default settings for creating an alert

**Response:**
```json
{
  "identity_id": "uuid",
  "identity_name": "John Doe",
  "default_name": "Track John Doe - 2025-01-05",
  "default_min_similarity": 0.75,
  "default_notify_dashboard": true,
  "can_create": true,
  "user_alert_count": 5,
  "max_alerts": 50,
  "warnings": []
}
```

### Create Live Alert

**Endpoint:** `POST /api/live-alerts`

**Request:**
```json
{
  "name": "Track John Doe - Investigation #123",
  "identity_id": "uuid-of-identity",
  "min_similarity": 0.75,
  "cooldown_minutes": 30,
  "notify_dashboard": true,
  "sound_alert": true,
  "auto_capture_snapshot": true,
  "expiration_type": "never"
}
```

### Manage Alerts

- **List Alerts**: `GET /api/live-alerts`
- **Get Alert**: `GET /api/live-alerts/{id}`
- **Update Alert**: `PUT /api/live-alerts/{id}`
- **Pause Alert**: `POST /api/live-alerts/{id}/pause`
- **Resume Alert**: `POST /api/live-alerts/{id}/resume`
- **Delete Alert**: `DELETE /api/live-alerts/{id}`
- **View Triggers**: `GET /api/live-alerts/{id}/triggers`

## Configuration

Settings available in **Settings** page (`/admin/settings`):

- `LIVE_ALERTS_ENABLED`: Enable/disable live alerts (default: true)
- `LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES`: Default cooldown period (default: 30)
- `LIVE_ALERT_MAX_PER_USER`: Maximum alerts per user (default: 50)

## Best Practices

1. **Use Descriptive Names**: "Track John Doe - Investigation #123"
2. **Set Appropriate Similarity**: 0.75-0.90 for general monitoring
3. **Configure Cooldown**: 30 minutes prevents spam
4. **Use Time Windows**: Reduce false positives during off-hours
5. **Set Expiration**: Use date/detections for temporary alerts

**See Documentation:** Check **40_LIVE_ALERTS_GUIDE.md** for complete guide to live alerts.
                ''',
                "examples": [
                    {
                        "title": "Create Alert from Search Result",
                        "description": "Search for person, click match, create alert",
                        "steps": [
                            "1. Go to Advanced Search",
                            "2. Upload image and search",
                            "3. Click on match result",
                            "4. Click 'CREATE LIVE ALERT'",
                            "5. Backend provides defaults automatically",
                            "6. Adjust settings and submit"
                        ]
                    },
                    {
                        "title": "Get Default Settings (Backend)",
                        "curl": """curl -X GET "http://localhost:8000/api/live-alerts/defaults/{identity_id}" \\
  -H "Authorization: Bearer YOUR_TOKEN" """,
                        "javascript": """async function getDefaultAlertSettings(identityId, token) {
  const response = await fetch(`/api/live-alerts/defaults/${identityId}`, {
    headers: {
      'Authorization': `Bearer ${token}`
    }
  });
  return await response.json();
}""",
                        "python": """import requests

def get_default_alert_settings(identity_id, token):
    url = f"http://localhost:8000/api/live-alerts/defaults/{identity_id}"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    return response.json()"""
                    },
                    {
                        "title": "Create Live Alert",
                        "curl": """curl -X POST "http://localhost:8000/api/live-alerts" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "name": "Track John Doe - Investigation #123",
    "identity_id": "uuid-of-identity",
    "min_similarity": 0.75,
    "notify_dashboard": true,
    "sound_alert": true,
    "auto_capture_snapshot": true
  }'""",
                        "javascript": """async function createLiveAlert(alertData, token) {
  const response = await fetch('/api/live-alerts', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(alertData)
  });
  return await response.json();
}""",
                        "python": """import requests

def create_live_alert(alert_data, token):
    url = "http://localhost:8000/api/live-alerts"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    response = requests.post(url, headers=headers, json=alert_data)
    return response.json()"""
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/live-alerts/defaults/{identity_id}",
                        "description": "Get default settings for creating alert (backend provides all defaults)"
                    },
                    {
                        "method": "POST",
                        "path": "/api/live-alerts",
                        "description": "Create a new live alert"
                    },
                    {
                        "method": "GET",
                        "path": "/api/live-alerts",
                        "description": "List all live alerts for current user"
                    },
                    {
                        "method": "GET",
                        "path": "/api/live-alerts/{id}",
                        "description": "Get specific alert details"
                    },
                    {
                        "method": "PUT",
                        "path": "/api/live-alerts/{id}",
                        "description": "Update alert settings"
                    },
                    {
                        "method": "POST",
                        "path": "/api/live-alerts/{id}/pause",
                        "description": "Pause an active alert"
                    },
                    {
                        "method": "POST",
                        "path": "/api/live-alerts/{id}/resume",
                        "description": "Resume a paused alert"
                    },
                    {
                        "method": "DELETE",
                        "path": "/api/live-alerts/{id}",
                        "description": "Delete an alert"
                    },
                    {
                        "method": "GET",
                        "path": "/api/live-alerts/{id}/triggers",
                        "description": "Get trigger history for an alert"
                    }
                ]
            },
            {
                "title": "Advanced SNA Features",
                "description": "Learn how to use advanced social network analysis features with machine learning capabilities",
                "content": """
# Advanced Social Network Analysis Features

## Overview

The system now includes three powerful enhancements for social network analysis:

1. **Automatic Threshold Learning** - System learns optimal settings automatically
2. **Trajectory Prediction** - Predict where people will appear next
3. **Activity Correlation Analysis** - Measure temporal and spatial association between activities

## Feature 1: Automatic Threshold Learning

### What It Does

The system automatically learns optimal distance and time thresholds for each camera pair based on historical data.

**Benefits:**
- No manual configuration needed
- Adapts to your specific camera network
- More accurate relationship detection
- Handles network changes automatically

### How to Use

**Step 1: Enable Feature**

Add to your `.env` file:
```env
AUTO_THRESHOLD_LEARNING_ENABLED=true
```

**Step 2: Learn Thresholds (runs as a background job)**

**Via Web UI:**
1. Go to **Security Intelligence** → **Advanced Features** tab
2. Click **"Learn All Camera Thresholds"** button
3. The page schedules a job and shows live progress (it does not freeze)
4. View learned thresholds in the results table when the job completes

**Via API:**
```bash
# Schedule the job (202 Accepted)
curl -X POST "http://localhost:8000/api/intelligence/thresholds/jobs" \\
  -H "Authorization: Bearer YOUR_TOKEN"
# -> {"accepted": true, "job_id": "threshold-4f2a91bc", "status": "scheduled"}

# Poll it
curl "http://localhost:8000/api/intelligence/thresholds/jobs/threshold-4f2a91bc" \\
  -H "Authorization: Bearer YOUR_TOKEN"
```

Scheduling while a job is already running returns `409` with the running
`job_id`. The completed job result includes `algorithm_version` and
`calculated_at`, and every threshold carries its `confidence` and
`sample_count` so you can judge how much evidence it is based on.

> The old synchronous `POST /api/intelligence/thresholds/learn` still exists
> for backward compatibility but is deprecated — it blocks the request while
> learning runs. Use the job endpoint.

**Step 3: System Uses Learned Thresholds Automatically**

Once learned, the system automatically uses these thresholds for all relationship calculations. No further action needed!

**When to Re-Learn:**
- After adding new cameras
- Monthly (patterns may change)
- After moving cameras to new locations

### Requirements

- Pipelines must have coordinates (latitude/longitude) set
- Need at least 10 cross-camera movements per pair
- Historical data: 90+ days recommended

---

## Feature 2: Trajectory Prediction

### What It Does

Predicts where a person will appear next based on their historical movement patterns.

**Benefits:**
- Proactive relationship detection
- Better cross-camera matching
- Anomaly detection (unusual paths)
- Security: predict suspicious movements

### How to Use

**Step 1: Enable Feature**

Add to your `.env` file:
```env
TRAJECTORY_PREDICTION_ENABLED=true
```

**Step 2: Predict Trajectory**

**Via Web UI:**
1. Go to **Security Intelligence** → **Advanced Features** tab
2. Enter Identity ID and Current Camera
3. Click **"Predict Trajectory"** button
4. View predictions with probabilities

**Via API:**
```bash
curl -X GET "http://localhost:8000/api/intelligence/trajectory/predict?identity_id=UUID&current_camera=camera_1&top_k=3" \\
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Response:**
```json
{
  "identity_id": "uuid",
  "current_camera": "camera_1",
  "predictions": [
    {
      "camera_id": "camera_3",
      "probability": 0.75,
      "estimated_time": "2026-01-11T15:30:00Z",
      "confidence": "high"
    }
  ],
  "model_version": "trajectory-v1",
  "insufficient_evidence": false,
  "note": "Estimated times are statistical projections from historical movement, not certainties."
}
```

**Reading the response honestly:**
- `confidence` is `high` (p≥0.6), `moderate` (p≥0.3) or `low` — treat low
  confidence predictions as hints, not facts.
- `insufficient_evidence: true` means there was not enough history to predict
  anything. The UI shows an "Insufficient Evidence" panel instead of inventing
  a prediction.
- `estimated_time` is a **projection**, never a guarantee of arrival.

### Use Cases

1. **Proactive Relationship Detection:**
   - Predict: "Person A will likely appear at Camera 3 in 5 minutes"
   - Check: "Is Person B already there?"
   - Result: Detect relationships before they happen

2. **Anomaly Detection:**
   - If person takes path not in predictions → Flag as suspicious

3. **Security Coordination:**
   - Predict where suspect will be
   - Coordinate response teams

### Requirements

- Identity must have at least 3 historical trajectories
- Identity must have appeared at current camera before
- Historical data: 90+ days recommended

---

## Feature 3: Activity Correlation Analysis (xCCA)

### What It Does

Measures the **temporal and spatial association** between two identities'
activities — how often their appearances line up in time and place.

> ⚠️ **Correlation does not prove causation.** A high score means the two
> identities' movements are associated, not that one caused the other, and not
> that they know each other. Always corroborate with evidence before acting on
> it in an investigation.

**Benefits:**
- Higher confidence relationship detection
- Distinguish coincidental vs. coordinated appearances
- Detect coordinated activities (security use case)
- Better relationship quality assessment

### How to Use

**Step 1: Enable Feature**

Add to your `.env` file:
```env
ACTIVITY_CORRELATION_ENABLED=true
```

**Step 2: Calculate Correlation**

**Via Web UI:**
1. Go to **Security Intelligence** → **Advanced Features** tab
2. Enter two Identity IDs
3. Click **"Calculate Correlation"** button
4. View correlation score and sequences

**Via API:**
```bash
curl -X GET "http://localhost:8000/api/intelligence/correlation/calculate?identity_a=UUID1&identity_b=UUID2&days_back=90" \\
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Response:**
```json
{
  "identity_a": "uuid1",
  "identity_b": "uuid2",
  "correlation_score": 0.75,
  "correlation_strength": "strong",
  "sequence_count": 15,
  "days_back": 90,
  "insufficient_evidence": false,
  "algorithm_version": "xcca-v1",
  "sequences": [...],
  "note": "Measures temporal and spatial association between two identities. Correlation does not prove causation."
}
```

`insufficient_evidence: true` (fewer than 3 activity sequences) means the score
is statistically meaningless — the UI shows a low-sample warning and you should
not rely on the result.

### Correlation Strength Guide

- **Strong** (≥0.7): High confidence relationship, likely coordinated
- **Moderate** (≥0.4): Medium confidence, some correlation
- **Weak** (≥0.1): Low confidence, may be coincidental
- **None** (<0.1): No significant correlation

### Use Cases

1. **Relationship Quality Assessment:**
   - High correlation (0.7+) = Strong relationship
   - Low correlation (<0.3) = May be coincidental

2. **Coordinated Activity Detection:**
   - Detect groups moving together
   - Security: identify suspicious patterns

3. **Investigation:**
   - Understand relationship dynamics
   - Filter false positives

### Requirements

- Both identities must have appearance data
- Need at least 3 activity sequences for meaningful correlation
- Historical data: 90+ days recommended

---

## Automatic Integration

**Good News:** All features are automatically integrated into your existing endpoints!

### Enhanced Endpoints

**1. Social Network Analysis** (`/api/security/network`)
- ✅ Uses learned thresholds automatically
- ✅ Activity correlation included
- ✅ Better relationship detection

**2. Related Identities** (`/api/identities/{id}/related`)
- ✅ Cross-camera relationships included
- ✅ Correlation-boosted strength
- ✅ More accurate results

**No code changes needed!** Just enable features and they work automatically.

---

## Configuration Summary

Add to your `.env` file:

```env
# Enable Advanced SNA Features
AUTO_THRESHOLD_LEARNING_ENABLED=true
TRAJECTORY_PREDICTION_ENABLED=true
ACTIVITY_CORRELATION_ENABLED=true

# Multi-Camera Settings
MULTI_CAMERA_CO_APPEARANCE_ENABLED=true
MULTI_CAMERA_DISTANCE_METERS=500
MULTI_CAMERA_TIME_WINDOW_MINUTES=10
MULTI_CAMERA_MIN_CO_APPEARANCES=2
```

---

## Quick Start Workflow

1. **Enable Features** - Add config to `.env`
2. **Restart Server** - Features are now active
3. **Learn Thresholds** - Run once (1-5 minutes)
4. **Use Existing Endpoints** - They work better automatically!
5. **Optional: Use New Endpoints** - For specific use cases

---

## Best Practices

1. **Learn Thresholds Monthly** - Patterns may change
2. **Use Trajectory Prediction** - For proactive security monitoring
3. **Check Correlation Scores** - Filter out coincidental relationships
4. **Monitor Logs** - See enhancement usage in action
5. **Start with Defaults** - They work well for most cases

---

## Troubleshooting

**No Learned Thresholds:**
- Ensure pipelines have coordinates set
- Need at least 10 cross-camera movements per pair
- Check historical data (90+ days recommended)

**Low Correlation Scores:**
- Normal if people don't move together
- Check if cameras are too far apart
- Increase `days_back` parameter

**Trajectory Prediction Returns Empty:**
- Need at least 3 historical trajectories
- Identity must have appeared at current camera before

---

**See Documentation:**
- `Docs/57_MULTI_CAMERA_SOCIAL_NETWORK_ANALYSIS.md` - Multi-camera analysis guide
- `Docs/59_ADVANCED_SNA_ENHANCEMENTS.md` - Detailed enhancement explanations
- `Docs/60_ENHANCEMENTS_IMPLEMENTATION_SUMMARY.md` - Implementation summary
- `Docs/61_API_ENHANCEMENTS_GUIDE.md` - API usage guide
- `Docs/62_HOW_TO_USE_ENHANCEMENTS.md` - Step-by-step usage guide
                """,
                "examples": [
                    {
                        "title": "Initial Setup: Learn Thresholds",
                        "description": "One-time setup to learn optimal thresholds for your camera network",
                        "steps": [
                            "1. Ensure all pipelines have coordinates set (latitude/longitude)",
                            "2. Go to Security Intelligence → Advanced Features tab",
                            "3. Click 'Learn All Camera Thresholds' button",
                            "4. Wait 1-5 minutes for learning to complete",
                            "5. Review learned thresholds in results table",
                            "6. System now uses these thresholds automatically!"
                        ],
                        "api_example": {
                            "method": "POST",
                            "url": "/api/intelligence/thresholds/learn"
                        }
                    },
                    {
                        "title": "Predict Next Camera Location",
                        "description": "Predict where a person will appear next for proactive monitoring",
                        "steps": [
                            "1. Get Identity ID from Unknown Faces or Intelligence page",
                            "2. Get Current Camera ID (where person is now)",
                            "3. Go to Security Intelligence → Advanced Features tab",
                            "4. Enter Identity ID and Current Camera",
                            "5. Click 'Predict Trajectory' button",
                            "6. Review predictions with probabilities",
                            "7. Use for proactive relationship detection or security coordination"
                        ],
                        "api_example": {
                            "method": "GET",
                            "url": "/api/intelligence/trajectory/predict?identity_id=UUID&current_camera=camera_1&top_k=3"
                        }
                    },
                    {
                        "title": "Check Relationship Quality",
                        "description": "Calculate correlation to assess relationship quality",
                        "steps": [
                            "1. Get two Identity IDs (from Related Identities or Social Network)",
                            "2. Go to Security Intelligence → Advanced Features tab",
                            "3. Enter both Identity IDs",
                            "4. Click 'Calculate Correlation' button",
                            "5. Review correlation score and strength",
                            "6. Use score to filter relationships (strong = high confidence)"
                        ],
                        "api_example": {
                            "method": "GET",
                            "url": "/api/intelligence/correlation/calculate?identity_a=UUID1&identity_b=UUID2&days_back=90"
                        }
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "POST",
                        "path": "/api/intelligence/thresholds/jobs",
                        "description": "Schedule threshold learning as a background job (202 Accepted; 409 if one is already running)",
                        "parameters": {
                            "pipeline_ids": "Optional: Comma-separated pipeline IDs (empty = all)"
                        },
                        "response": {
                            "accepted": True,
                            "job_id": "threshold-4f2a91bc",
                            "status": "scheduled"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/intelligence/thresholds/jobs/{job_id}",
                        "description": "Poll a threshold-learning job (progress, stage, learned thresholds)",
                        "parameters": {
                            "job_id": "Required: Job ID returned when scheduling"
                        },
                        "response": {
                            "job_id": "threshold-4f2a91bc",
                            "status": "running | completed | failed",
                            "progress_percent": 100,
                            "result": "Learned thresholds + algorithm_version + calculated_at"
                        }
                    },
                    {
                        "method": "POST",
                        "path": "/api/intelligence/thresholds/learn",
                        "description": "DEPRECATED synchronous variant — blocks the request. Use /thresholds/jobs instead.",
                        "parameters": {
                            "pipeline_ids": "Optional: Comma-separated pipeline IDs (empty = all)"
                        },
                        "response": {
                            "status": "success",
                            "learned_pairs": 3,
                            "thresholds": "Array of learned threshold data"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/intelligence/trajectory/predict",
                        "description": "Predict next camera locations for an identity",
                        "parameters": {
                            "identity_id": "Required: Identity UUID",
                            "current_camera": "Required: Current camera/pipeline ID",
                            "top_k": "Optional: Number of predictions (1-10, default: 3)"
                        },
                        "response": {
                            "identity_id": "Identity UUID",
                            "current_camera": "Current camera ID",
                            "predictions": "Array of predictions with probabilities"
                        }
                    },
                    {
                        "method": "GET",
                        "path": "/api/intelligence/correlation/calculate",
                        "description": "Calculate activity correlation between two identities",
                        "parameters": {
                            "identity_a": "Required: First identity UUID",
                            "identity_b": "Required: Second identity UUID",
                            "days_back": "Optional: Days to analyze (1-365, default: 90)"
                        },
                        "response": {
                            "identity_a": "First identity UUID",
                            "identity_b": "Second identity UUID",
                            "correlation_score": "Score from 0.0 to 1.0",
                            "correlation_strength": "strong/moderate/weak/none",
                            "sequence_count": "Number of activity sequences",
                            "insufficient_evidence": "True when fewer than 3 sequences (score not meaningful)",
                            "algorithm_version": "Algorithm version used (e.g. xcca-v1)",
                            "sequences": "Array of detected sequences"
                        }
                    }
                ]
            },
            {
                "title": "Platform Hardening: What Changed",
                "description": "Required reading for anyone calling the API or reviewing admin pages: CSRF, background jobs, versioned lifecycles, structured errors and safe defaults",
                "content": """
# Platform Hardening: What Changed

The admin surfaces (Live Alerts, Dashboard, Unknown Faces, People Tracking /
SQL Agent, Intelligence Analysis, Security Intelligence, Watchlists and ML
Model Management) were overhauled for security, correctness and scale.

If you write scripts against this API, **sections 1 and 2 below are required
reading** — some request shapes changed.

---

## 1. CSRF: browser (cookie) clients must send one extra header

Every state-changing request (`POST`, `PUT`, `PATCH`, `DELETE`) now requires
the header `X-Requested-With: XMLHttpRequest` **when you authenticate with the
session cookie**.

```javascript
fetch('/api/watchlists', {
    method: 'POST',
    credentials: 'include',
    headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest'   // <-- required for cookie auth
    },
    body: JSON.stringify({ name: 'VIP' })
});
```

**Bearer-token clients (curl, scripts, integrations) are exempt** — a token
cannot be sent cross-site by a malicious page, so nothing changes for them:

```bash
curl -X POST "http://localhost/api/watchlists" \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"name": "VIP"}'
```

Missing the header on a cookie request returns:
`403 {"detail": "CSRF check failed: X-Requested-With header required"}`

**Why:** the auth cookie is `SameSite=lax`, which blocks most cross-site
attacks; requiring a custom header closes the remainder, because a cross-site
page cannot set custom headers without a CORS preflight this API never grants.

---

## 2. Expensive work is now a background job, not a long request

Operations that used to block an HTTP request (sometimes for minutes) now
return **`202 Accepted` with a `job_id`** and run in the background. The
pattern is identical everywhere:

```bash
# 1. Schedule
POST <endpoint>            -> 202 {"accepted": true, "job_id": "...", "status": "scheduled"}
# 2. Poll
GET  <endpoint>/{job_id}   -> {"status": "running|completed|failed", "progress_percent": 55, ...}
# 3. Conflict while one runs
POST <endpoint>            -> 409 {"error_code": "..._ALREADY_RUNNING", "job_id": "<running id>"}
```

| Operation | Schedule | Poll |
|---|---|---|
| Relationship calculation | `POST /api/intelligence/relationships/calculate-all` | `GET /api/intelligence/relationships/jobs/{job_id}` |
| Threshold learning | `POST /api/intelligence/thresholds/jobs` | `GET /api/intelligence/thresholds/jobs/{job_id}` |
| Similarity-model training | `POST /api/admin/merge-suggestions/training-jobs` | `GET /api/admin/merge-suggestions/training-jobs/{job_id}` |
| Alert channel test | `POST /api/live-alerts/{id}/test` | `GET /api/live-alerts/test-jobs/{job_id}` |

All of these also appear in **Admin → Background Tasks**, so a job keeps
running (and stays visible) even if you close the browser.

---

## 3. Structured errors with reference IDs

Server errors no longer return raw exception text, SQL fragments or filesystem
paths. You get a safe message plus a reference ID, and the real detail is in
the Docker logs:

```json
{"detail": "Internal error during model activation. Reference: ML-1154d5f4"}
```

Business errors return a machine-readable code your script can branch on:

```json
{"detail": {"error_code": "VERSION_CONFLICT",
            "message": "This watchlist was modified by another administrator...",
            "current_version": 4}}
```

Common codes: `DATASET_NOT_READY`, `TRAINING_ALREADY_RUNNING`,
`JOB_ALREADY_RUNNING`, `NAME_CONFLICT`, `VERSION_CONFLICT`,
`QUALITY_GATES_FAILED`, `CONFIRMATION_REQUIRED`, `ACCOUNT_BLOCKED`,
`QUERY_DENIED`, `INVALID_STATUS`.

**Unknown or malformed IDs now return `404`, never `500`** — and the same 404
for both, so you cannot probe which identities exist by guessing IDs.

---

## 4. Watchlists: soft delete, versions and real statistics

**Deleting is now reversible.** `DELETE /api/watchlists/{id}` performs a *soft*
delete: matching stops immediately, but entries, alert history and audit
records are preserved and the watchlist can be restored.

```bash
# See what you are about to remove BEFORE deleting
curl "http://localhost/api/watchlists/$ID/deletion-impact" -H "Authorization: Bearer $TOKEN"
# -> {"entries": 25, "active_entries": 22, "alerts": 145}

curl -X DELETE "http://localhost/api/watchlists/$ID?reason=under+review" -H "Authorization: Bearer $TOKEN"
curl -X POST   "http://localhost/api/watchlists/$ID/restore"            -H "Authorization: Bearer $TOKEN"
```

Permanent deletion still exists but must be explicitly confirmed:
`DELETE /api/watchlists/{id}?hard_delete=true&confirm=true`.

**Activation is now a first-class action** (separate from deletion):
`PATCH /api/watchlists/{id}/status` with `{"is_active": false, "reason": "..."}`.
An inactive watchlist stops matching detections; history is kept.

**Concurrent edits are detected.** Every watchlist has an integer `version`.
Send the version you read; if someone else changed it meanwhile you get
`409 VERSION_CONFLICT` instead of silently overwriting their work:

```bash
curl -X PUT "http://localhost/api/watchlists/$ID" -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"alert_level": "critical", "version": 4}'
```

**"Alerts Today" is now a real number.** The list and detail endpoints return
`entries_count`, `alerts_today`, `total_alerts` and `last_alert_at`, plus the
exact reporting window used:

```json
{"stats_period": {"period_start": "2026-07-26T00:00:00Z",
                  "period_end": "2026-07-27T00:00:00Z", "timezone": "UTC"}}
```

Names are unique **case-insensitively among live watchlists** — "VIP" and "vip"
conflict (`409 NAME_CONFLICT`), but a deleted watchlist's name can be reused.

---

## 5. Lists are paginated — do not download everything

Endpoints that used to return unbounded lists now support a paginated
envelope. Pass `page` to switch modes:

```bash
GET /api/admin/identities?page=1&page_size=25&q=john&type=known&last_seen_within_days=7
```
```json
{"items": [...], "total": 25000, "page": 1, "page_size": 25, "total_pages": 1000}
```

The same envelope is used by `/api/watchlists?page=1`,
`/api/watchlists/{id}/entries?page=1` and the SQL Agent history. Identity
dropdowns in the admin UI now search server-side instead of loading thousands
of records into the browser.

**The social-network graph is always bounded.** `GET /api/security/network`
without explicit `identity_ids` returns the **top-risk slice**, not the whole
graph, and tells you so:

```json
{"nodes": [...], "edges": [...], "scope": "top_risk",
 "truncated": true, "total_nodes": 12400, "returned_nodes": 100, "max_nodes": 100}
```

Pass `identity_ids=<uuid>` for an ego network, or raise `max_nodes` (server
ceiling: 300).

---

## 6. Analysis endpoints tell you what they could NOT do

`GET /api/identities/{id}/analyze` now reports per-section status instead of
claiming everything worked:

```json
{"sections": {
    "related":  {"status": "ready", "count": 5},
    "temporal": {"status": "ready", "total_appearances": 120},
    "tracking": {"status": "partial", "reason_code": "NO_COORDINATES",
                 "movement_count": 34}}}
```

Statuses: `ready`, `partial`, `unavailable`, `error`. A failure in one section
no longer hides the others, and the UI never claims "tracking data available"
when there is none.

`GET /api/identities/{id}/related` now returns an envelope with the
authoritative threshold policy so the UI cannot display rules that disagree
with the backend:

```json
{"items": [...], "thresholds": {"strong": {"min_percentage": 50}, "moderate": {"min_percentage": 25}}}
```

---

## 7. Feature status is verified, not assumed

`GET /api/security/capabilities` reports what the backend can **actually** do
right now — dependency availability, running jobs, model/algorithm versions:

```json
{"capabilities": {
    "threshold_learning": {"enabled": true, "status": "job_running", "job_id": "threshold-4f2a91bc"},
    "map_generation": {"enabled": true, "status": "ready"},
    "offline_maps": {"enabled": false, "status": "disabled"}},
 "checked_at": "2026-07-26T12:00:00Z"}
```

Statuses you may see: `ready`, `disabled`, `job_running`,
`dependency_unavailable`, `model_not_trained`.

---

## 8. Maps and generated HTML are sandboxed

Backend-generated maps (`GET /api/identities/{id}/map`) are served with
`Cache-Control: private, no-store` and a sandboxing CSP, and the admin pages
embed them in a sandboxed iframe that cannot reach your session, cookies or the
parent page.

**Expensive and security-sensitive map overlays are now opt-in.** These
parameters default to `false` and must be requested explicitly:
`enable_security_features`, `detect_patterns`, `show_risk_heatmap`,
`show_timeline`, `show_animated_avatar`. A missing checkbox in the UI is
treated as OFF — it can no longer silently enable expensive analysis.

`map_style` is validated against an allowlist (`light`, `dark`, `satellite`,
`terrain`); anything else returns `400`.

---

## 9. SQL Agent (People Tracking): real cancellation

Stopping a query now actually stops the work on the server:

```bash
POST /api/sql-agent/requests/{request_id}/cancel
```

Every streamed event carries `request_id` and `sequence`, so late or duplicated
events from an abandoned query can never overwrite a newer answer. A stopped
query ends with a terminal `cancelled` event rather than looking like a failure.

**Denied SQL no longer blocks your account.** Asking for something the read-only
policy forbids returns a `QUERY_DENIED` explanation and is audited. Accounts are
only blocked after repeated explicit violations (3 within an hour) — and asking
"why was I blocked?" can never itself block you.

---

## 10. Display windows are display-only

The dashboard and Unknown Faces page hide old faces from the *view* after a
configurable window. **Nothing is deleted** — it is purely visual.

- `DASHBOARD_FACE_DISPLAY_HOURS` — known faces on the dashboard
- `UNKNOWN_FACE_DISPLAY_HOURS` — unknown faces (default 24h; `0` = show all)

On the Unknown Faces page, the **Show all** toggle reveals everything
regardless of the window (`?show_all=true` on the API). Actual deletion is
governed separately by the retention/cleanup jobs.

---

## Migration checklist for existing scripts

1. ✅ Using a **Bearer token**? Nothing to change.
2. ⚠️ Using **cookies** from a browser page? Add
   `X-Requested-With: XMLHttpRequest` to every POST/PUT/PATCH/DELETE.
3. ⚠️ Calling `POST /api/admin/merge-suggestions/train-model`? It now schedules
   a job and returns `202 + job_id` instead of blocking and returning metrics.
   Poll the job, then activate the candidate.
4. ⚠️ Calling `POST /api/intelligence/thresholds/learn`? Switch to
   `POST /api/intelligence/thresholds/jobs` and poll.
5. ⚠️ Reading `model_path` from model status? It is gone — use `artifact_name`.
6. ⚠️ Expecting `GET /api/security/network` to return the entire graph? Check
   `truncated`/`scope` and pass `identity_ids` or `max_nodes`.
7. ⚠️ Expecting `DELETE /api/watchlists/{id}` to erase everything? It now soft
   deletes; add `?hard_delete=true&confirm=true` if you truly need permanence.
8. ⚠️ Parsing error strings? Switch to `detail.error_code`.
                """,
                "examples": [
                    {
                        "title": "Call a mutating endpoint from a browser page (CSRF)",
                        "description": "Cookie-authenticated requests need the X-Requested-With header",
                        "steps": [
                            "1. Keep credentials: 'include' so the session cookie is sent",
                            "2. Add the header X-Requested-With: XMLHttpRequest",
                            "3. Handle 403 (CSRF), 409 (conflict codes) and 404 (unknown/malformed id)",
                            "4. Branch on detail.error_code — never on the message text"
                        ],
                        "api_example": {
                            "method": "PATCH",
                            "url": "/api/watchlists/{watchlist_id}/status",
                            "headers": {"X-Requested-With": "XMLHttpRequest"},
                            "body": {"is_active": False, "reason": "Paused during review", "version": 4},
                            "response": {"id": "uuid", "is_active": False, "version": 5}
                        }
                    },
                    {
                        "title": "Run a background job end-to-end",
                        "description": "Schedule, poll, and read the result of any long-running operation",
                        "steps": [
                            "1. POST the schedule endpoint -> 202 with job_id",
                            "2. If you get 409, read detail.job_id and poll THAT job instead",
                            "3. Poll GET .../{job_id} until status is 'completed' or 'failed'",
                            "4. Read the job's result payload (metrics, counts, learned data)",
                            "5. Watch the same job in Admin -> Background Tasks"
                        ],
                        "api_example": {
                            "method": "POST",
                            "url": "/api/admin/merge-suggestions/training-jobs",
                            "response": {"accepted": True, "job_id": "simtrain-ab12cd34", "status": "scheduled"}
                        }
                    },
                    {
                        "title": "Delete a watchlist safely (impact -> soft delete -> restore)",
                        "description": "Review the impact, soft delete, and undo if it was a mistake",
                        "steps": [
                            "1. GET /api/watchlists/{id}/deletion-impact to see entries and alert counts",
                            "2. DELETE /api/watchlists/{id}?reason=... (soft: matching stops, history kept)",
                            "3. Confirm it disappeared from the default list",
                            "4. POST /api/watchlists/{id}/restore to bring it back active",
                            "5. Only use hard_delete=true&confirm=true when permanence is intended"
                        ],
                        "api_example": {
                            "method": "DELETE",
                            "url": "/api/watchlists/{watchlist_id}?reason=Temporarily+retired",
                            "response": {"success": True, "action": "soft_deleted",
                                         "impact": {"entries": 25, "alerts": 145},
                                         "deleted_at": "2026-07-26T12:00:00Z"}
                        }
                    },
                    {
                        "title": "Promote a similarity-model candidate",
                        "description": "Train, review the quality gates, then activate — with rollback available",
                        "steps": [
                            "1. GET model-status and confirm ready_to_train is true",
                            "2. POST training-jobs and poll until completed",
                            "3. Review validation precision and false_merge_rate in the result",
                            "4. If quality_gates.passed is false, activation is blocked — reject it",
                            "5. POST models/{model_id}/activate?reason=... to promote",
                            "6. If the new model misbehaves: POST models/{previous_id}/rollback"
                        ],
                        "api_example": {
                            "method": "POST",
                            "url": "/api/admin/merge-suggestions/models/{model_id}/activate?reason=Higher+precision",
                            "response": {"success": True, "version": 4, "previous_version": 3,
                                         "runtime_degraded": False}
                        }
                    }
                ],
                "api_endpoints": [
                    {
                        "method": "GET",
                        "path": "/api/security/capabilities",
                        "description": "Backend-verified feature readiness (dependencies, running jobs, model versions)",
                        "parameters": {},
                        "response": {"capabilities": "Per-feature enabled + status", "checked_at": "ISO 8601 Z"}
                    },
                    {
                        "method": "PATCH",
                        "path": "/api/watchlists/{watchlist_id}/status",
                        "description": "Activate or deactivate a watchlist (inactive = stops matching, history kept)",
                        "parameters": {"is_active": "Required boolean", "reason": "Optional audit reason",
                                       "version": "Optional: version you read (409 on conflict)"},
                        "response": {"id": "uuid", "is_active": False, "version": 5}
                    },
                    {
                        "method": "GET",
                        "path": "/api/watchlists/{watchlist_id}/deletion-impact",
                        "description": "What a deletion would affect — shown before you confirm",
                        "parameters": {},
                        "response": {"entries": 25, "active_entries": 22, "alerts": 145}
                    },
                    {
                        "method": "POST",
                        "path": "/api/watchlists/{watchlist_id}/restore",
                        "description": "Restore a soft-deleted watchlist",
                        "parameters": {},
                        "response": {"id": "uuid", "is_active": True, "deleted_at": None}
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/merge-suggestions/training-jobs",
                        "description": "Schedule similarity-model training (202 + job_id; 409 if already running; 400 DATASET_NOT_READY)",
                        "parameters": {"min_samples": "Optional: override the minimum sample requirement"},
                        "response": {"accepted": True, "job_id": "simtrain-ab12cd34", "status": "scheduled"}
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/merge-suggestions/models/{model_id}/activate",
                        "description": "Promote a candidate model atomically (hash + load test first; previous version archived)",
                        "parameters": {"reason": "Optional audit reason"},
                        "response": {"success": True, "version": 4, "previous_version": 3, "runtime_degraded": False}
                    },
                    {
                        "method": "POST",
                        "path": "/api/admin/merge-suggestions/models/{model_id}/rollback",
                        "description": "Roll back to a previously active (archived) model version",
                        "parameters": {"reason": "Optional audit reason"},
                        "response": {"success": True, "version": 3, "previous_version": 4}
                    },
                    {
                        "method": "POST",
                        "path": "/api/sql-agent/requests/{request_id}/cancel",
                        "description": "Really cancel a running SQL Agent query (server-side, not just closing the stream)",
                        "parameters": {},
                        "response": {"success": True, "status": "cancelling"}
                    }
                ]
            },
        ],
        "quick_start": {
            "title": "Quick Start Guide",
            "steps": [
                {
                    "step": 1,
                    "title": "Access Admin Panel",
                    "description": "Navigate to Admin → Unknown Faces in the web interface"
                },
                {
                    "step": 2,
                    "title": "Browse Unknown Faces",
                    "description": "View faces grouped by pipeline (camera). Each card shows the best snapshot."
                },
                {
                    "step": 3,
                    "title": "Review Details",
                    "description": "Click on a face card to see: appearance timeline, all snapshots, pipeline locations"
                },
                {
                    "step": 4,
                    "title": "Promote or Merge",
                    "description": "If you recognize the person: Promote to Known. If duplicate: Merge with existing identity."
                },
                {
                    "step": 5,
                    "title": "Search by Image",
                    "description": "Use 'Search by Image' to find if a person already exists before promoting."
                }
            ],
            "common_workflows": [
                {
                    "scenario": "New Employee Detected",
                    "steps": [
                        "1. Face appears in Unknown Faces",
                        "2. Review appearance timeline",
                        "3. Verify it's the employee",
                        "4. Click 'Promote to Known'",
                        "5. Enter employee name",
                        "6. Employee now tracked as known"
                    ]
                },
                {
                    "scenario": "Duplicate Identities Found",
                    "steps": [
                        "1. Notice similar faces in Unknown Faces",
                        "2. Check merge suggestions (auto-generated)",
                        "3. Review both identities",
                        "4. Verify they're the same person",
                        "5. Approve merge suggestion OR manually merge",
                        "6. Identities combined"
                    ]
                },
                {
                    "scenario": "Identify Unknown Person",
                    "steps": [
                        "1. Have a photo of the person",
                        "2. Go to Admin → Unknown Faces",
                        "3. Click 'Search by Image'",
                        "4. Upload the photo",
                        "5. Review search results",
                        "6. If match found: Promote or Merge",
                        "7. If no match: Wait for detection or manually add"
                    ]
                }
            ]
        },
        "common_tasks": [
            {
                "task": "API Authentication",
                "endpoint": "POST /api/auth/login",
                "description": "Login to get an access token for API requests",
                "example_request": {
                    "username": "admin",
                    "password": "your_password"
                },
                "example_response": {
                    "access_token": "JWT token string",
                    "token_type": "bearer",
                    "user": "User object with id, username, role, etc."
                },
                "notes": "All API endpoints require authentication. Include token in Authorization header: 'Bearer <token>'"
            },
            {
                "task": "View All Unknown Faces",
                "endpoint": "GET /api/admin/unknown",
                "description": "Get paginated list of all unknown identities",
                "authentication": "Required - Admin role",
                "example_response": {
                    "identities": "Array of identity objects",
                    "total": "Total count",
                    "stats": "Statistics (total_unknown, total_appearances, active_cameras)"
                }
            },
            {
                "task": "Get Identity Details",
                "endpoint": "GET /api/admin/identity/{identity_id}",
                "description": "Get detailed information including timeline and appearances",
                "authentication": "Required - Admin role",
                "example_response": {
                    "id": "Identity UUID",
                    "type": "unknown or known",
                    "appearances": "Array of appearance records",
                    "timeline": "Chronological appearance data"
                }
            },
            {
                "task": "Promote Unknown to Known",
                "endpoint": "POST /api/admin/unknown/{identity_id}/promote",
                "description": "Convert unknown identity to known with display name",
                "authentication": "Required - Admin role",
                "example_request": {
                    "display_name": "John Doe",
                    "notes": "Optional notes"
                }
            },
            {
                "task": "Merge Two Identities",
                "endpoint": "POST /api/admin/identities/merge",
                "description": "Merge source identity into target identity",
                "authentication": "Required - Admin role or pipeline access",
                "example_request": {
                    "from_identity_id": "Source UUID",
                    "to_identity_id": "Target UUID",
                    "notes": "Optional notes"
                }
            },
            {
                "task": "Preview Merge (Production Feature)",
                "endpoint": "POST /api/admin/identities/merge-preview",
                "description": "Preview what will happen when merging identities BEFORE executing. Shows target selection, type promotion, pipeline distribution, warnings, and AI scoring breakdown.",
                "authentication": "Required - Admin role or pipeline access",
                "example_request": {
                    "identity_ids": ["uuid-1", "uuid-2", "uuid-3"],
                    "target_identity_id": None
                },
                "example_response": {
                    "success": True,
                    "target_identity": {
                        "id": "uuid-1",
                        "type": "unknown",
                        "appearances_count": 50,
                        "pipelines": ["CAMERA-1", "CAMERA-2"],
                        "auto_selected": True
                    },
                    "source_identities": ["...list of source identities..."],
                    "type_promotion": {
                        "will_change": True,
                        "from_type": "unknown",
                        "to_type": "known",
                        "reason": "Source identity is KNOWN"
                    },
                    "statistics": {
                        "total_identities": 3,
                        "total_appearances": 100,
                        "total_pipelines": 3
                    },
                    "warnings": ["Cross-pipeline merge warning"],
                    "selection_details": {"candidates": ["...scoring breakdown..."]}
                },
                "notes": "ALWAYS preview before merging! Shows exactly what will happen. Use the PREVIEW button in the UI."
            },
            {
                "task": "Merge Multiple Identities (3+) - Production Grade",
                "endpoint": "POST /api/admin/identities/merge-multiple",
                "description": "Production-grade merge with: (1) AI-powered target selection using pipeline diversity, (2) Type promotion (UNKNOWN+KNOWN→KNOWN), (3) Best snapshot selection, (4) Enhanced FAISS management, (5) Full audit trail with pipeline stats.",
                "authentication": "Required - Admin role or pipeline access",
                "example_request": {
                    "identity_ids": ["uuid-1", "uuid-2", "uuid-3", "uuid-4"],
                    "target_identity_id": None,
                    "notes": "Merging duplicate identities"
                },
                "example_response": {
                    "success": True,
                    "message": "Successfully merged 3 identities into target identity",
                    "identity": {
                        "id": "uuid-1",
                        "type": "known",
                        "display_name": "John",
                        "status": "active",
                        "appearances_count": 120
                    },
                    "merged_count": 3,
                    "auto_selected_target": True,
                    "statistics": {
                        "appearances_moved": 70,
                        "embeddings_moved": 10,
                        "pipeline_count": 3,
                        "pipelines": ["CAMERA-1", "CAMERA-2", "CAMERA-3"]
                    },
                    "type_promotion": {
                        "changed": True,
                        "from": "unknown",
                        "to": "known",
                        "inherited_name": "John"
                    },
                    "snapshot_selection": {
                        "source": "uuid-2",
                        "quality": 0.85
                    }
                },
                "notes": "Production-grade merge with AI scoring (KNOWN=5000, appearances=1000, pipeline_diversity=200). See Docs/28_MULTI_IDENTITY_MERGE_GUIDE.md and Docs/37_ADVANCED_MERGE_FLOW_GUIDE.md for details."
            },
            {
                "task": "Search by Image",
                "endpoint": "POST /api/search/by-image",
                "description": "Search for identities using a face image. Uses FAISS vector similarity search on face embeddings extracted by ArcFace model.",
                "authentication": "Required",
                "content_type": "multipart/form-data",
                "example_request": {
                    "image": "File upload (JPG, PNG, WEBP)",
                    "scope": "both | known | unknown",
                    "top_k": 10,
                    "date_from": "2025-01-01T00:00:00 (optional)",
                    "date_to": "2025-01-05T23:59:59 (optional)",
                    "pipeline_id": "CAMERA-1 (optional)"
                },
                "example_response": {
                    "results": [
                        {
                            "identity_id": "uuid",
                            "similarity": 0.89,
                            "type": "known",
                            "display_name": "John Doe",
                            "best_snapshot_path": "/storage/...",
                            "last_seen_at": "2025-01-05T10:30:00",
                            "appearances_count": 45
                        }
                    ]
                },
                "notes": "Complete technical guide: Docs/38_SEARCH_BY_IMAGE_GUIDE.md. Similarity thresholds: KNOWN=0.4, UNKNOWN=0.35."
            },
            {
                "task": "Get Merge Suggestions",
                "endpoint": "GET /api/admin/merge-suggestions",
                "description": "Get automatically generated merge suggestions",
                "authentication": "Required - Admin role",
                "example_response": {
                    "suggestions": "Array of merge suggestion objects with confidence scores"
                }
            },
            {
                "task": "View All Settings",
                "endpoint": "GET /api/settings",
                "description": "Get all system settings (filtered by category if specified)",
                "authentication": "Required - Admin role",
                "example_response": {
                    "all_settings": "Array of setting objects",
                    "settings_by_category": "Object with category keys",
                    "categories": "Array of category names"
                }
            },
            {
                "task": "Get Setting Categories",
                "endpoint": "GET /api/settings/categories",
                "description": "Get all available setting categories",
                "authentication": "Required - Admin role",
                "example_response": ["database", "security", "models", "processing"]
            },
            {
                "task": "Get Specific Setting",
                "endpoint": "GET /api/settings/{setting_key}",
                "description": "Get a specific setting by its key name",
                "authentication": "Required - Admin role",
                "example_path": "/api/settings/CACHE_LOCAL_SIZE"
            },
            {
                "task": "Update Setting",
                "endpoint": "PUT /api/settings/{setting_key}",
                "description": "Update a system setting value with audit logging",
                "authentication": "Required - Admin role",
                "example_request": {
                    "value": "new_value",
                    "change_reason": "Optional reason for the change"
                },
                "notes": "Readonly settings cannot be updated. Value must match setting type (int, float, bool, string, json)."
            },
            {
                "task": "Get Settings Audit Log",
                "endpoint": "GET /api/settings/audit/log",
                "description": "Get history of all setting changes with pagination",
                "authentication": "Required - Admin role",
                "query_parameters": {
                    "setting_key": "Optional - Filter by specific setting",
                    "limit": "Number of entries (default: 100)",
                    "offset": "Pagination offset (default: 0)"
                },
                "example_response": {
                    "logs": "Array of audit log entries with user, timestamp, old/new values",
                    "total_count": "Total number of log entries",
                    "has_more": "Boolean indicating more entries available"
                }
            }
        ]
    }
    
    
    return TutorialResponse(**tutorial_data)


@router.get("/tutorial/examples", summary="API Examples", description="Get code examples for all admin API endpoints (Admin only)")
async def get_api_examples(
    current_user: User = Depends(require_role(["admin"]))
):
    """
    Get practical code examples for all admin API endpoints.
    Includes cURL, JavaScript, Python, and other examples.
    """
    
    examples = {
        "promote_identity": {
            "title": "Promote Unknown to Known",
            "curl": """curl -X POST "https://your-domain.com/api/admin/unknown/{identity_id}/promote" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "display_name": "John Doe",
    "notes": "Employee ID: 12345"
  }'""",
            "javascript": """async function promoteIdentity(identityId, displayName) {
  const response = await fetch(`/api/admin/unknown/${identityId}/promote`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${getAuthToken()}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      display_name: displayName,
      notes: 'Promoted via admin interface'
    })
  });
  return await response.json();
}""",
            "python": """import requests

def promote_identity(identity_id, display_name, token):
    url = f"https://your-domain.com/api/admin/unknown/{identity_id}/promote"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    data = {
        "display_name": display_name,
        "notes": "Promoted via API"
    }
    response = requests.post(url, headers=headers, json=data)
    return response.json()"""
        },
        "merge_identities": {
            "title": "Merge Two Identities",
            "curl": """curl -X POST "https://your-domain.com/api/admin/identities/merge" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "from_identity_id": "source-uuid",
    "to_identity_id": "target-uuid",
    "notes": "These are the same person"
  }'""",
            "javascript": """async function mergeIdentities(fromId, toId, notes) {
  const response = await fetch('/api/admin/identities/merge', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${getAuthToken()}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      from_identity_id: fromId,
      to_identity_id: toId,
      notes: notes
    })
  });
  return await response.json();
}""",
            "python": """import requests

def merge_identities(from_id, to_id, token, notes=None):
    url = "https://your-domain.com/api/admin/identities/merge"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    data = {
        "from_identity_id": from_id,
        "to_identity_id": to_id,
        "notes": notes or "Merged via API"
    }
    response = requests.post(url, headers=headers, json=data)
    return response.json()"""
        },
        "search_by_image": {
            "title": "Search by Image",
            "curl": """curl -X POST "https://your-domain.com/api/search/by-image" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -F "image=@/path/to/face.jpg" \\
  -F "scope=both" \\
  -F "top_k=10" """,
            "javascript": """async function searchByImage(imageFile) {
  const formData = new FormData();
  formData.append('image', imageFile);
  formData.append('scope', 'both');
  formData.append('top_k', '10');
  
  const response = await fetch('/api/search/by-image', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${getAuthToken()}`
    },
    body: formData
  });
  return await response.json();
}""",
            "python": """import requests

def search_by_image(image_path, token, scope='both', top_k=10):
    url = "https://your-domain.com/api/search/by-image"
    headers = {
        "Authorization": f"Bearer {token}"
    }
    files = {
        'image': open(image_path, 'rb')
    }
    data = {
        'scope': scope,
        'top_k': top_k
    }
    response = requests.post(url, headers=headers, files=files, data=data)
    return response.json()"""
        },
        "advanced_search": {
            "title": "Advanced Multi-Face Search",
            "curl": """curl -X POST "https://your-domain.com/api/search/advanced" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -F "image=@/path/to/image.jpg" \\
  -F "scope=both" \\
  -F "top_k=10" \\
  -F "check_watchlist=true" \\
  -F "exclude_identity_ids=uuid1,uuid2" \\
  -F "exclude_watchlist_ids=watchlist1" """,
            "javascript": """async function advancedSearch(imageFile, options = {}) {
  const formData = new FormData();
  formData.append('image', imageFile);
  formData.append('scope', options.scope || 'both');
  formData.append('top_k', options.topK || '10');
  formData.append('check_watchlist', options.checkWatchlist !== false);
  if (options.excludeIds) {
    formData.append('exclude_identity_ids', options.excludeIds.join(','));
  }
  if (options.excludeWatchlists) {
    formData.append('exclude_watchlist_ids', options.excludeWatchlists.join(','));
  }
  
  const response = await fetch('/api/search/advanced', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${getAuthToken()}`
    },
    body: formData
  });
  return await response.json();
}""",
            "python": """import requests

def advanced_search(image_path, token, scope='both', top_k=10, 
                   exclude_ids=None, exclude_watchlists=None):
    url = "https://your-domain.com/api/search/advanced"
    headers = {"Authorization": f"Bearer {token}"}
    files = {'image': open(image_path, 'rb')}
    data = {'scope': scope, 'top_k': top_k, 'check_watchlist': True}
    if exclude_ids:
        data['exclude_identity_ids'] = ','.join(exclude_ids)
    if exclude_watchlists:
        data['exclude_watchlist_ids'] = ','.join(exclude_watchlists)
    response = requests.post(url, headers=headers, files=files, data=data)
    return response.json()"""
        },
        "batch_search": {
            "title": "Batch Search Multiple Images",
            "curl": """curl -X POST "https://your-domain.com/api/search/batch" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -F "images=@/path/to/image1.jpg" \\
  -F "images=@/path/to/image2.jpg" \\
  -F "images=@/path/to/image3.jpg" \\
  -F "scope=both" \\
  -F "top_k=5" """,
            "javascript": """async function batchSearch(imageFiles, options = {}) {
  const formData = new FormData();
  imageFiles.forEach(file => formData.append('images', file));
  formData.append('scope', options.scope || 'both');
  formData.append('top_k', options.topK || '5');
  
  const response = await fetch('/api/search/batch', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${getAuthToken()}`
    },
    body: formData
  });
  return await response.json();
}""",
            "python": """import requests

def batch_search(image_paths, token, scope='both', top_k=5):
    url = "https://your-domain.com/api/search/batch"
    headers = {"Authorization": f"Bearer {token}"}
    files = [('images', open(path, 'rb')) for path in image_paths]
    data = {'scope': scope, 'top_k': top_k}
    response = requests.post(url, headers=headers, files=files, data=data)
    return response.json()"""
        },
        "get_search_history": {
            "title": "Get Search History",
            "curl": """curl -X GET "https://your-domain.com/api/search/history?days_back=30&search_type=batch&limit=50" \\
  -H "Authorization: Bearer YOUR_TOKEN" """,
            "javascript": """async function getSearchHistory(token, daysBack=30, searchType=null) {
  const params = new URLSearchParams({
    days_back: daysBack,
    limit: 50
  });
  if (searchType) params.append('search_type', searchType);
  
  const response = await fetch(`/api/search/history?${params}`, {
    headers: {
      'Authorization': `Bearer ${token}`
    }
  });
  return await response.json();
}""",
            "python": """import requests

def get_search_history(token, days_back=30, search_type=None):
    url = "https://your-domain.com/api/search/history"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"days_back": days_back, "limit": 50}
    if search_type:
        params["search_type"] = search_type
    response = requests.get(url, headers=headers, params=params)
    return response.json()"""
        },
        "export_search_results": {
            "title": "Export Search Results",
            "curl": """curl -X POST "https://your-domain.com/api/search/export?format=pdf&include_quality=true" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"search_id": "...", "faces": [...]}' """,
            "javascript": """async function exportSearchResults(results, format='csv') {
  const response = await fetch(`/api/search/export?format=${format}`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${getAuthToken()}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify(results)
  });
  const blob = await response.blob();
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `search-results.${format}`;
  a.click();
}""",
            "python": """import requests

def export_search_results(results, token, format='csv'):
    url = f"https://your-domain.com/api/search/export?format={format}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    response = requests.post(url, headers=headers, json=results)
    with open(f'search-results.{format}', 'wb') as f:
        f.write(response.content)
    return f'search-results.{format}'"""
        },
        "get_related_identities": {
            "title": "Get Related Identities",
            "curl": """curl -X GET "https://your-domain.com/api/identities/{identity_id}/related?min_co_appearances=3&time_window_minutes=5&limit=20" \\
  -H "Authorization: Bearer YOUR_TOKEN" """,
            "javascript": """async function getRelatedIdentities(identityId, options = {}) {
  const params = new URLSearchParams({
    min_co_appearances: options.minCoApp || 3,
    time_window_minutes: options.timeWindow || 5,
    limit: options.limit || 20
  });
  
  const response = await fetch(`/api/identities/${identityId}/related?${params}`, {
    headers: {
      'Authorization': `Bearer ${getAuthToken()}`
    }
  });
  return await response.json();
}""",
            "python": """import requests

def get_related_identities(identity_id, token, min_co_app=3, time_window=5, limit=20):
    url = f"https://your-domain.com/api/identities/{identity_id}/related"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "min_co_appearances": min_co_app,
        "time_window_minutes": time_window,
        "limit": limit
    }
    response = requests.get(url, headers=headers, params=params)
    return response.json()"""
        },
        "get_temporal_patterns": {
            "title": "Get Temporal Patterns",
            "curl": """curl -X GET "https://your-domain.com/api/identities/{identity_id}/temporal-patterns?days_back=90" \\
  -H "Authorization: Bearer YOUR_TOKEN" """,
            "javascript": """async function getTemporalPatterns(identityId, daysBack=90) {
  const response = await fetch(
    `/api/identities/${identityId}/temporal-patterns?days_back=${daysBack}`,
    {
      headers: {
        'Authorization': `Bearer ${getAuthToken()}`
      }
    }
  );
  return await response.json();
}""",
            "python": """import requests

def get_temporal_patterns(identity_id, token, days_back=90):
    url = f"https://your-domain.com/api/identities/{identity_id}/temporal-patterns"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"days_back": days_back}
    response = requests.get(url, headers=headers, params=params)
    return response.json()"""
        },
        "get_cross_camera_tracking": {
            "title": "Get Cross-Camera Tracking",
            "curl": """curl -X GET "https://your-domain.com/api/identities/{identity_id}/cross-camera?date=2025-01-05" \\
  -H "Authorization: Bearer YOUR_TOKEN" """,
            "javascript": """async function getCrossCameraTracking(identityId, date=null, daysBack=7) {
  const url = date 
    ? `/api/identities/${identityId}/cross-camera?date=${date}`
    : `/api/identities/${identityId}/cross-camera?days_back=${daysBack}`;
  
  const response = await fetch(url, {
    headers: {
      'Authorization': `Bearer ${getAuthToken()}`
    }
  });
  return await response.json();
}""",
            "python": """import requests

def get_cross_camera_tracking(identity_id, token, date=None, days_back=7):
    url = f"https://your-domain.com/api/identities/{identity_id}/cross-camera"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"date": date} if date else {"days_back": days_back}
    response = requests.get(url, headers=headers, params=params)
    return response.json()"""
        },
        "get_complete_analysis": {
            "title": "Get Complete Intelligence Analysis",
            "curl": """curl -X GET "https://your-domain.com/api/identities/{identity_id}/analyze" \\
  -H "Authorization: Bearer YOUR_TOKEN" """,
            "javascript": """async function getCompleteAnalysis(identityId) {
  const response = await fetch(`/api/identities/${identityId}/analyze`, {
    headers: {
      'Authorization': `Bearer ${getAuthToken()}`
    }
  });
  return await response.json();
}""",
            "python": """import requests

def get_complete_analysis(identity_id, token):
    url = f"https://your-domain.com/api/identities/{identity_id}/analyze"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    return response.json()"""
        },
        "list_unknown": {
            "title": "List Unknown Identities",
            "curl": """curl -X GET "https://your-domain.com/api/admin/unknown?page=1&page_size=20" \\
  -H "Authorization: Bearer YOUR_TOKEN" """,
            "javascript": """async function listUnknownFaces(page = 1, pageSize = 20) {
  const response = await fetch(
    `/api/admin/unknown?page=${page}&page_size=${pageSize}`,
    {
      headers: {
        'Authorization': `Bearer ${getAuthToken()}`
      }
    }
  );
  return await response.json();
}""",
            "python": """import requests

def list_unknown_faces(token, page=1, page_size=20):
    url = "https://your-domain.com/api/admin/unknown"
    headers = {
        "Authorization": f"Bearer {token}"
    }
    params = {
        "page": page,
        "page_size": page_size
    }
    response = requests.get(url, headers=headers, params=params)
    return response.json()"""
        },
        "get_identity_details": {
            "title": "Get Identity Details",
            "curl": """curl -X GET "https://your-domain.com/api/admin/identity/{identity_id}" \\
  -H "Authorization: Bearer YOUR_TOKEN" """,
            "javascript": """async function getIdentityDetails(identityId) {
  const response = await fetch(`/api/admin/identity/${identityId}`, {
    headers: {
      'Authorization': `Bearer ${getAuthToken()}`
    }
  });
  return await response.json();
}""",
            "python": """import requests

def get_identity_details(identity_id, token):
    url = f"https://your-domain.com/api/admin/identity/{identity_id}"
    headers = {
        "Authorization": f"Bearer {token}"
    }
    response = requests.get(url, headers=headers)
    return response.json()"""
        },
        "get_all_settings": {
            "title": "Get All Settings",
            "curl": """curl -X GET "http://localhost/api/settings" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -H "Accept: application/json" """,
            "javascript": """async function getAllSettings(token) {
  const response = await fetch('/api/settings', {
    method: 'GET',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Accept': 'application/json'
    }
  });
  return await response.json();
}""",
            "python": """import requests

def get_all_settings(token):
    url = "http://localhost/api/settings"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }
    response = requests.get(url, headers=headers)
    return response.json()"""
        },
        "update_setting": {
            "title": "Update Setting",
            "curl": """curl -X PUT "http://localhost/api/settings/CACHE_LOCAL_SIZE" \\
  -H "Authorization: Bearer YOUR_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "value": "75000",
    "change_reason": "Performance optimization"
  }'""",
            "javascript": """async function updateSetting(settingKey, newValue, token, reason) {
  const response = await fetch(`/api/settings/${settingKey}`, {
    method: 'PUT',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      value: newValue,
      change_reason: reason
    })
  });
  return await response.json();
}""",
            "python": """import requests

def update_setting(setting_key, new_value, token, reason=None):
    url = f"http://localhost/api/settings/{setting_key}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    data = {
        "value": new_value,
        "change_reason": reason
    }
    response = requests.put(url, headers=headers, json=data)
    return response.json()"""
        },
        "get_settings_audit_log": {
            "title": "Get Settings Audit Log",
            "curl": """curl -X GET "http://localhost/api/settings/audit/log?limit=50" \\
  -H "Authorization: Bearer YOUR_TOKEN" """,
            "javascript": """async function getSettingsAuditLog(token, limit=50) {
  const response = await fetch(`/api/settings/audit/log?limit=${limit}`, {
    method: 'GET',
    headers: {
      'Authorization': `Bearer ${token}`
    }
  });
  return await response.json();
}""",
            "python": """import requests

def get_settings_audit_log(token, limit=50):
    url = f"http://localhost/api/settings/audit/log?limit={limit}"
    headers = {
        "Authorization": f"Bearer {token}"
    }
    response = requests.get(url, headers=headers)
    return response.json()"""
        },
        "train_similarity_model": {
            "title": "Train ML Similarity Model (background job)",
            "curl": """# 1. Schedule the job (202 Accepted). 409 if one is already running.
curl -X POST "http://localhost/api/admin/merge-suggestions/training-jobs" \\
  -H "Authorization: Bearer YOUR_TOKEN"

# 2. Poll it until status is completed or failed
curl -X GET "http://localhost/api/admin/merge-suggestions/training-jobs/simtrain-ab12cd34" \\
  -H "Authorization: Bearer YOUR_TOKEN"

# 3. Promote the resulting candidate (only if quality gates passed)
curl -X POST "http://localhost/api/admin/merge-suggestions/models/12/activate?reason=Higher+precision" \\
  -H "Authorization: Bearer YOUR_TOKEN" """,
            "javascript": """// Training runs in the background: schedule, then poll the job.
async function trainSimilarityModel(minSamples) {
  const params = minSamples ? `?min_samples=${minSamples}` : '';
  const response = await fetch(`/api/admin/merge-suggestions/training-jobs${params}`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'X-Requested-With': 'XMLHttpRequest' }  // CSRF (cookie auth)
  });

  if (response.status === 409) {
    const body = await response.json();
    return pollTrainingJob(body.detail.job_id);   // already running — follow that job
  }
  if (response.status === 400) {
    const body = await response.json();
    throw new Error(`Dataset not ready: ${body.detail.readiness_reason}`);
  }
  const { job_id } = await response.json();
  return pollTrainingJob(job_id);
}

async function pollTrainingJob(jobId) {
  for (;;) {
    const res = await fetch(`/api/admin/merge-suggestions/training-jobs/${jobId}`,
                            { credentials: 'include' });
    const task = await res.json();
    if (task.status === 'completed') return task.result;   // candidate + metrics + gates
    if (task.status === 'failed') throw new Error(task.error_code);
    await new Promise(r => setTimeout(r, 1500));
  }
}""",
            "python": """import time
import requests

def train_similarity_model(token, min_samples=None, base_url="http://localhost"):
    \"\"\"Schedule training, wait for the candidate, return its metrics.

    Training never runs inside the HTTP request and never replaces the live
    model: it produces a candidate that you review and activate.
    \"\"\"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"min_samples": min_samples} if min_samples else {}

    response = requests.post(f"{base_url}/api/admin/merge-suggestions/training-jobs",
                             headers=headers, params=params)

    if response.status_code == 409:            # a job is already running
        job_id = response.json()["detail"]["job_id"]
    elif response.status_code == 400:          # dataset cannot train yet
        detail = response.json()["detail"]
        raise RuntimeError(f"Dataset not ready: {detail['readiness_reason']}")
    else:
        response.raise_for_status()
        job_id = response.json()["job_id"]

    while True:                                 # poll the job
        task = requests.get(
            f"{base_url}/api/admin/merge-suggestions/training-jobs/{job_id}",
            headers=headers).json()
        if task["status"] == "completed":
            return task["result"]               # metrics, quality_gates, model_id
        if task["status"] == "failed":
            raise RuntimeError(task.get("error_code", "TRAINING_FAILED"))
        time.sleep(2)


def activate_model(model_id, token, reason=None, base_url="http://localhost"):
    \"\"\"Promote a candidate. Blocked with 409 if its quality gates failed.\"\"\"
    response = requests.post(
        f"{base_url}/api/admin/merge-suggestions/models/{model_id}/activate",
        headers={"Authorization": f"Bearer {token}"},
        params={"reason": reason} if reason else {})
    response.raise_for_status()
    return response.json()"""
        },
        "get_model_status": {
            "title": "Get ML Model Status (typed + readiness checks)",
            "curl": """curl -X GET "http://localhost/api/admin/merge-suggestions/model-status" \\
  -H "Authorization: Bearer YOUR_TOKEN" """,
            "javascript": """async function getModelStatus() {
  const response = await fetch('/api/admin/merge-suggestions/model-status', {
    credentials: 'include',
    cache: 'no-store'
  });
  const status = await response.json();

  // All fields are real JSON types (booleans/integers), so no string parsing.
  if (!status.ready_to_train) {
    // Sample count is not the only rule — show every failing check.
    for (const [name, check] of Object.entries(status.readiness_checks)) {
      if (!check.passed) console.warn(`Blocked by ${name}`, check);
    }
  }
  // Filesystem paths are never returned — use the logical artifact name.
  console.log('Active artifact:', status.active_model?.artifact_name ?? 'none');
  return status;
}""",
            "python": """import requests

def get_model_status(token, base_url="http://localhost"):
    \"\"\"Typed status: readiness checks, active/candidate versions, runtime health.\"\"\"
    response = requests.get(f"{base_url}/api/admin/merge-suggestions/model-status",
                            headers={"Authorization": f"Bearer {token}"})
    response.raise_for_status()
    status = response.json()

    if not status["ready_to_train"]:
        blocked = [name for name, check in status["readiness_checks"].items()
                   if not check["passed"]]
        print(f"Not ready to train — failing checks: {', '.join(blocked)}")

    active = status.get("active_model")
    print("Active model:", active["artifact_name"] if active else "none (heuristic)")
    return status"""
        },
        "generate_pipeline_aware_suggestions": {
            "title": "Generate Pipeline-Aware Merge Suggestions",
            "curl": """curl -X POST "http://localhost/api/admin/merge-suggestions/generate-pipeline-aware" \\
  -H "Authorization: Bearer YOUR_TOKEN" """,
            "javascript": """async function generatePipelineAwareSuggestions(token) {
  const response = await fetch('/api/admin/merge-suggestions/generate-pipeline-aware', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`
    }
  });
  return await response.json();
}""",
            "python": """import requests

def generate_pipeline_aware_suggestions(token):
    url = "http://localhost/api/admin/merge-suggestions/generate-pipeline-aware"
    headers = {
        "Authorization": f"Bearer {token}"
    }
    response = requests.post(url, headers=headers)
    return response.json()"""
        }
    }
    
    return {"examples": examples}

