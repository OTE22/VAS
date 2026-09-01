# Navbar Component System

## Overview
A reusable navbar component system that allows you to include the same navbar across all admin pages with minimal code changes.

## How It Works

### 1. Component File
The navbar HTML is stored in: `frontend/components/navbar.html`

### 2. Loader Script
The JavaScript loader (`frontend/js/navbar-loader.js`) automatically:
- Loads the navbar HTML from the component file
- Injects it into the page where `#navbar-placeholder` is located
- Sets the active link based on the current page URL
- Attaches logout functionality
- Adds hover effects and interactions
- Sends the user to `/change-password` if `GET /api/auth/me` answers
  `rotation_required: true` — a courtesy, since every gated request on the
  current page would answer 403 anyway; the server is what enforces it

**It does not run at all on `/signin` or `/change-password`.** Both are outside
the navigated application, and on the change-password page a navbar would fire
`/api/auth/me/privileges`, which *is* gated and returns 403 during a pending
rotation.

### 3. Usage in HTML Pages

**Minimal Code Changes Required:**

Replace your existing navbar HTML with:
```html
<!-- Navbar Component (loaded dynamically) -->
<div id="navbar-placeholder"></div>
```

Add the loader script before your page-specific scripts:
```html
<script src="/frontend/js/navbar-loader.js"></script>
<script src="/frontend/js/your-page-script.js"></script>
```

## Example

### Before:
```html
<nav class="military-navbar">
    <!-- 60+ lines of navbar HTML -->
</nav>
```

### After:
```html
<!-- Navbar Component (loaded dynamically) -->
<div id="navbar-placeholder"></div>

<!-- Scripts -->
<script src="/frontend/js/navbar-loader.js"></script>
<script src="/frontend/js/your-page-script.js"></script>
```

## Benefits

1. **Single Source of Truth**: Update navbar once in `navbar.html`, changes apply everywhere
2. **Minimal Code Changes**: Just replace navbar HTML with a placeholder div
3. **Automatic Active Link**: The loader automatically highlights the current page
4. **Consistent Behavior**: Logout and navigation work the same across all pages
5. **Easy Maintenance**: Add/remove navbar items in one place

## Updating the Navbar

To add or modify navbar links:

1. Edit `frontend/components/navbar.html`
2. Changes will automatically appear on all pages that use the component

## Pages Using This System

- ✅ `frontend/home.html`
- ✅ `frontend/admin/unknown.html`
- ✅ `frontend/admin/users.html`
- ✅ `frontend/admin/pipelines.html`
- ✅ `frontend/admin/audit.html`
- ❌ `frontend/signin.html` — deliberately not (would interfere with login redirects)
- ❌ `frontend/change-password.html` — deliberately not (see above)

## Technical Details

- **Loading Method**: Fetch API to load HTML component
- **Injection**: Replaces `#navbar-placeholder` div with navbar HTML
- **Active Link Detection**: Based on `window.location.pathname`
- **Error Handling**: Shows fallback navbar if component fails to load
- **Performance**: Loads asynchronously, doesn't block page rendering

