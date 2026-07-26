# API Authentication Guide

## Overview

Most API endpoints require authentication using JWT (JSON Web Token) Bearer tokens. This guide explains how to obtain and use access tokens.

## Quick Start

1. **Login** to get a token
2. **Use the token** in API requests
3. **Token expires** after 24 hours (default) - login again when needed

---

## Step 1: Generate an Access Token

### Method 1: Login Endpoint (Recommended)

**URL:** `POST /api/auth/login`

**Request Body:**
```json
{
  "username": "your_username",
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
    "email": "admin@example.com",
    "role": "admin",
    "is_active": true
  }
}
```

**This is the standard and recommended way to get tokens.**

### Method 2: Programmatic Token Generation (For System Integration)

If you need to generate tokens programmatically in your own code (e.g., for automated scripts, system integrations), you can use the authentication service directly:

**Python Example:**
```python
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.auth.auth_service import AuthService
from db_connection import get_db
from sqlalchemy.ext.asyncio import AsyncSession

async def generate_token_for_user(username: str, password: str):
    """Generate access token for a user programmatically"""
    async for db in get_db():
        # Authenticate user
        user = await AuthService.authenticate_user(username, password, db)
        if not user:
            raise Exception("Invalid credentials")
        
        # Generate token
        token = AuthService.create_access_token(
            data={
                "sub": str(user.id),
                "username": user.username,
                "role": user.role
            }
        )
        return token
```

**Note:** This method requires direct access to the codebase. For external systems, always use the login endpoint.

### Example: Login with cURL

```bash
curl -X POST "http://localhost/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "admin",
    "password": "your_password"
  }'
```

**Save the token:**
```bash
# Save token to variable (Linux/Mac)
TOKEN=$(curl -X POST "http://localhost/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your_password"}' \
  | jq -r '.access_token')

echo $TOKEN
```

```powershell
# Save token to variable (Windows PowerShell)
$response = Invoke-RestMethod -Uri "http://localhost/api/auth/login" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{"username": "admin", "password": "your_password"}'
$TOKEN = $response.access_token
Write-Host $TOKEN
```

---

## Step 2: Use the Token in API Requests

### Method 1: Authorization Header (Recommended)

Include the token in the `Authorization` header with the format: `Bearer <token>`

#### cURL Example

```bash
curl -X GET "http://localhost/api/settings" \
  -H "Authorization: Bearer YOUR_TOKEN_HERE" \
  -H "Accept: application/json"
```

#### Using Token Variable (cURL)

```bash
# After getting token (see Step 1)
curl -X GET "http://localhost/api/settings" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json"
```

#### JavaScript/Fetch Example

```javascript
const token = "YOUR_TOKEN_HERE";

fetch('http://localhost/api/settings', {
  method: 'GET',
  headers: {
    'Authorization': `Bearer ${token}`,
    'Accept': 'application/json'
  }
})
  .then(response => response.json())
  .then(data => console.log(data))
  .catch(error => console.error('Error:', error));
```

#### JavaScript with Login Flow

```javascript
// Step 1: Login
async function login(username, password) {
  const response = await fetch('http://localhost/api/auth/login', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ username, password })
  });
  
  if (!response.ok) {
    throw new Error('Login failed');
  }
  
  const data = await response.json();
  return data.access_token;
}

// Step 2: Use token
async function getSettings(token) {
  const response = await fetch('http://localhost/api/settings', {
    method: 'GET',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Accept': 'application/json'
    }
  });
  
  if (!response.ok) {
    throw new Error('Failed to get settings');
  }
  
  return await response.json();
}

// Usage
(async () => {
  try {
    const token = await login('admin', 'your_password');
    const settings = await getSettings(token);
    console.log(settings);
  } catch (error) {
    console.error('Error:', error);
  }
})();
```

#### Python Requests Example

```python
import requests

# Step 1: Login
login_url = "http://localhost/api/auth/login"
login_data = {
    "username": "admin",
    "password": "your_password"
}

response = requests.post(login_url, json=login_data)
if response.status_code == 200:
    token = response.json()["access_token"]
    print(f"Token: {token}")
else:
    print(f"Login failed: {response.status_code}")

# Step 2: Use token
settings_url = "http://localhost/api/settings"
headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json"
}

response = requests.get(settings_url, headers=headers)
if response.status_code == 200:
    settings = response.json()
    print(settings)
else:
    print(f"Error: {response.status_code}")
```

#### Python with Session

```python
import requests

# Create session
session = requests.Session()

# Step 1: Login
login_url = "http://localhost/api/auth/login"
login_data = {
    "username": "admin",
    "password": "your_password"
}

response = session.post(login_url, json=login_data)
if response.status_code == 200:
    token = response.json()["access_token"]
    # Set token in session headers
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    })
    print("Logged in successfully")
else:
    print(f"Login failed: {response.status_code}")
    exit(1)

# Step 2: Use session for all requests
response = session.get("http://localhost/api/settings")
if response.status_code == 200:
    settings = response.json()
    print(settings)
```

---

## Method 2: Using Swagger UI (/docs)

### Step 1: Open Swagger UI

Navigate to: `http://localhost/docs`

### Step 2: Login to Get Token

1. Find the **`POST /api/auth/login`** endpoint
2. Click **"Try it out"**
3. Enter your username and password:
   ```json
   {
     "username": "admin",
     "password": "your_password"
   }
   ```
4. Click **"Execute"**
5. Copy the `access_token` from the response

### Step 3: Authorize in Swagger UI

1. Click the **"Authorize"** button (🔓 lock icon) at the top right
2. In the **"Value"** field, enter: `Bearer YOUR_TOKEN_HERE`
   - **Important:** Include the word "Bearer" and a space before your token
   - Example: `Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...`
3. Click **"Authorize"**
4. Click **"Close"**

### Step 4: Use Authenticated Endpoints

Now all authenticated endpoints will automatically include your token. Try any endpoint:
- `GET /api/settings`
- `GET /api/settings/categories`
- `PUT /api/settings/{setting_key}`
- etc.

---

## Token Management

### Token Expiration

- **Default expiration:** 24 hours (1440 minutes)
- **Configurable:** Set `ACCESS_TOKEN_EXPIRE_MINUTES` in `.env` file
- **After expiration:** Login again to get a new token

### Check Token Validity

```bash
# Try to access a protected endpoint
curl -X GET "http://localhost/api/auth/me" \
  -H "Authorization: Bearer YOUR_TOKEN_HERE"
```

**Valid token response:**
```json
{
  "id": 1,
  "username": "admin",
  "email": "admin@example.com",
  "role": "admin"
}
```

**Invalid/expired token response:**
```json
{
  "detail": "Invalid authentication credentials"
}
```

### Store Token Securely

**JavaScript (Browser):**
```javascript
// Store in localStorage (not recommended for sensitive apps)
localStorage.setItem('access_token', token);

// Retrieve
const token = localStorage.getItem('access_token');

// Remove
localStorage.removeItem('access_token');
```

**JavaScript (Node.js):**
```javascript
// Use environment variables
process.env.ACCESS_TOKEN = token;

// Or use a secure token store
```

**Python:**
```python
import os

# Use environment variables
os.environ['ACCESS_TOKEN'] = token

# Or use a secure config file
```

---

## Common Issues and Solutions

### Issue 1: "401 Unauthorized - Invalid or missing token"

**Causes:**
- No Authorization header provided
- Token format incorrect (missing "Bearer" prefix)
- Token expired
- Token invalid

**Solutions:**
1. Check you're including the Authorization header
2. Ensure format is: `Authorization: Bearer <token>`
3. Login again to get a fresh token
4. Verify token is copied completely (no truncation)

**Correct format:**
```bash
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

**Incorrect formats:**
```bash
Authorization: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...  # Missing "Bearer"
Authorization: Bearer: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...  # Extra colon
```

### Issue 2: "403 Forbidden - Admin access required"

**Causes:**
- User doesn't have admin role
- User account is inactive or blocked

**Solutions:**
1. Login with an admin account
2. Contact system administrator to grant admin access
3. Check user account status

### Issue 3: Token Works in Swagger but Not in cURL/Code

**Causes:**
- Token not properly included in request
- CORS issues (for browser requests)
- Header format incorrect

**Solutions:**
1. Verify header format: `Authorization: Bearer <token>`
2. Check CORS settings if making requests from browser
3. Use browser developer tools to inspect actual request headers

### Issue 4: Token Expires Too Quickly

**Solution:**
Update `ACCESS_TOKEN_EXPIRE_MINUTES` in `.env` file:
```env
ACCESS_TOKEN_EXPIRE_MINUTES=2880  # 48 hours
```

Then restart the server.

---

## Complete Example: Full Workflow

### cURL Complete Example

```bash
#!/bin/bash

# Configuration
API_URL="http://localhost"
USERNAME="admin"
PASSWORD="your_password"

# Step 1: Login
echo "Logging in..."
LOGIN_RESPONSE=$(curl -s -X POST "${API_URL}/api/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\": \"${USERNAME}\", \"password\": \"${PASSWORD}\"}")

# Extract token
TOKEN=$(echo $LOGIN_RESPONSE | jq -r '.access_token')

if [ "$TOKEN" == "null" ] || [ -z "$TOKEN" ]; then
  echo "Login failed!"
  echo $LOGIN_RESPONSE
  exit 1
fi

echo "Login successful!"
echo "Token: ${TOKEN:0:50}..."

# Step 2: Use token to get settings
echo "Fetching settings..."
SETTINGS=$(curl -s -X GET "${API_URL}/api/settings" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Accept: application/json")

echo "Settings retrieved:"
echo $SETTINGS | jq '.total_count'
```

### Python Complete Example

```python
#!/usr/bin/env python3
import requests
import json

# Configuration
API_URL = "http://localhost"
USERNAME = "admin"
PASSWORD = "your_password"

# Step 1: Login
print("Logging in...")
login_url = f"{API_URL}/api/auth/login"
login_data = {
    "username": USERNAME,
    "password": PASSWORD
}

response = requests.post(login_url, json=login_data)
if response.status_code != 200:
    print(f"Login failed: {response.status_code}")
    print(response.text)
    exit(1)

token = response.json()["access_token"]
print(f"Login successful! Token: {token[:50]}...")

# Step 2: Use token
print("Fetching settings...")
settings_url = f"{API_URL}/api/settings"
headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json"
}

response = requests.get(settings_url, headers=headers)
if response.status_code == 200:
    settings = response.json()
    print(f"Settings retrieved: {settings['total_count']} settings")
    print(json.dumps(settings, indent=2))
else:
    print(f"Error: {response.status_code}")
    print(response.text)
```

### JavaScript Complete Example

```javascript
// Configuration
const API_URL = 'http://localhost';
const USERNAME = 'admin';
const PASSWORD = 'your_password';

// Step 1: Login
async function login() {
  console.log('Logging in...');
  const response = await fetch(`${API_URL}/api/auth/login`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      username: USERNAME,
      password: PASSWORD
    })
  });

  if (!response.ok) {
    throw new Error(`Login failed: ${response.status}`);
  }

  const data = await response.json();
  console.log('Login successful! Token:', data.access_token.substring(0, 50) + '...');
  return data.access_token;
}

// Step 2: Get settings
async function getSettings(token) {
  console.log('Fetching settings...');
  const response = await fetch(`${API_URL}/api/settings`, {
    method: 'GET',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Accept': 'application/json'
    }
  });

  if (!response.ok) {
    throw new Error(`Failed to get settings: ${response.status}`);
  }

  return await response.json();
}

// Main execution
(async () => {
  try {
    const token = await login();
    const settings = await getSettings(token);
    console.log(`Settings retrieved: ${settings.total_count} settings`);
    console.log(settings);
  } catch (error) {
    console.error('Error:', error.message);
  }
})();
```

---

## Security Best Practices

1. **Never commit tokens to version control**
   - Use environment variables
   - Use `.env` files (add to `.gitignore`)
   - Use secure secret management tools

2. **Store tokens securely**
   - Don't log tokens
   - Don't expose tokens in URLs
   - Use HTTPS in production

3. **Handle token expiration**
   - Implement token refresh logic
   - Re-login when token expires
   - Show user-friendly error messages

4. **Use HTTPS in production**
   - Tokens are sensitive data
   - HTTP is not secure for production

5. **Rotate tokens regularly**
   - Change passwords periodically
   - Logout and re-login to get new tokens

---

## Generating Tokens for System Integration

### Quick Token Generation

**Using the utility script:**
```bash
# Generate token
python utils/generate_token.py admin your_password

# Use in script
TOKEN=$(python utils/generate_token.py admin your_password)
curl -X GET "http://localhost/api/settings" \
  -H "Authorization: Bearer $TOKEN"
```

### Automated Token Generation Script

If you need to generate tokens for automated scripts or system integrations, here's a complete example:

**Python Script (`generate_token.py`):**
```python
#!/usr/bin/env python3
"""
Generate access token for API usage
Usage: python generate_token.py <username> <password>
"""
import sys
import os
import asyncio

# Add project root to path
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from backend.auth.auth_service import AuthService
from db_connection import get_db

async def generate_token(username: str, password: str):
    """Generate and return access token"""
    async for db in get_db():
        # Authenticate user
        user = await AuthService.authenticate_user(username, password, db)
        if not user:
            print("ERROR: Invalid username or password", file=sys.stderr)
            sys.exit(1)
        
        # Check if user is active
        if not user.is_active:
            print("ERROR: User account is inactive", file=sys.stderr)
            sys.exit(1)
        
        # Generate token
        token = AuthService.create_access_token(
            data={
                "sub": str(user.id),
                "username": user.username,
                "role": user.role
            }
        )
        
        print(token)
        return token

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python generate_token.py <username> <password>", file=sys.stderr)
        sys.exit(1)
    
    username = sys.argv[1]
    password = sys.argv[2]
    
    token = asyncio.run(generate_token(username, password))
```

**Usage:**
```bash
python generate_token.py admin your_password
# Output: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

### Using Token in Your System

Once you have a token, you can use it in your system in several ways:

**1. Environment Variable:**
```bash
export API_TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
```

**2. Configuration File:**
```python
# config.py
API_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
API_BASE_URL = "http://localhost"
```

**3. Secure Storage:**
```python
import keyring  # For secure credential storage

# Store token
keyring.set_password("face_recognition_api", "admin", token)

# Retrieve token
token = keyring.get_password("face_recognition_api", "admin")
```

**4. Token Refresh Function:**
```python
import requests
from datetime import datetime, timedelta

class APIClient:
    def __init__(self, base_url, username, password):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.token = None
        self.token_expires = None
    
    def login(self):
        """Login and get new token"""
        response = requests.post(
            f"{self.base_url}/api/auth/login",
            json={
                "username": self.username,
                "password": self.password
            }
        )
        if response.status_code == 200:
            data = response.json()
            self.token = data["access_token"]
            # Token expires in 24 hours (default)
            self.token_expires = datetime.now() + timedelta(hours=24)
            return True
        return False
    
    def get_token(self):
        """Get valid token, refresh if needed"""
        if not self.token or datetime.now() >= self.token_expires:
            self.login()
        return self.token
    
    def request(self, method, endpoint, **kwargs):
        """Make authenticated API request"""
        token = self.get_token()
        headers = kwargs.get("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        kwargs["headers"] = headers
        
        response = requests.request(
            method,
            f"{self.base_url}{endpoint}",
            **kwargs
        )
        return response

# Usage
client = APIClient("http://localhost", "admin", "password")
response = client.request("GET", "/api/settings")
settings = response.json()
```

## Summary

1. **Generate token:** `POST /api/auth/login` with username/password
2. **Use token:** Include `Authorization: Bearer <token>` header
3. **Token expires:** After 24 hours (default), login again
4. **Swagger UI:** Click "Authorize" button and enter `Bearer <token>`
5. **System integration:** Use login endpoint or programmatic token generation

**Quick Reference:**
```bash
# Generate token
curl -X POST "http://localhost/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "password"}'

# Use token
curl -X GET "http://localhost/api/settings" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**For System Integration:**
- Use the login endpoint for external systems
- Use programmatic token generation for internal scripts
- Implement token refresh logic for long-running applications
- Store tokens securely (environment variables, secure vaults)

For more information, see the API documentation at `/docs` or the tutorial at `/admin/tutorial`.

