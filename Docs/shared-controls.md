# Shared controls and enrollment

[All guides](README.md) · Source review: 2026-09-24

## Navigation

The navbar uses permissions returned by the backend. A missing menu entry may
be a permission restriction. Page and API checks still apply to direct URLs.

| Control | Action |
|---|---|
| Home / Dashboard and workspace links | Open the named page; see the index for URLs and permissions |
| Dropdown / mobile menu toggle | Expand or collapse navigation |
| Add person | Open the shared enrollment dialog, where permitted |
| Tracking | Launch `/api/sso/laf-ai/launch`; usually redirects to the separate local chatbot |
| Refresh in navbar | Reload the current page |
| API Docs | Open the API explorer; production disables it, so do not expect it to be available |
| Logout / Sign out | Request session logout and return to sign-in |
| Skip to main content | Move keyboard focus to the page's main content |

## Add person / Add photo

Available from Home, navigation and Known faces. This is a real enrollment
workflow, not a search-only action.

| Control | Action |
|---|---|
| Person name | Set the intended display name for enrollment |
| Choose/drop photo | Select an image and preview it; server validates actual content |
| Face-crop option | Indicate that the file is already a face crop |
| Submit / upload | Submit the photo for detection/embedding and enrollment review |
| Candidate choice / Add to existing | Resolve a pending upload to the selected existing person |
| Create new person | Confirm creating a separate identity from a pending upload |
| Cancel in enrollment decision | Cancel the pending upload; do not create a person |
| Close / permitted backdrop click | Close the modal; pending enrollment cancellation is handled by the component |
| Face alert OK / close | Dismiss the validation alert and correct the image |

A new photo may be accepted immediately, or the backend may return a
**decision required** result if similar people or an identical image exist.
Do not assume a successful upload automatically means a new person was created.
Adding a photo from Known faces targets that selected person's gallery.

Demo: use a consenting test subject's photo, enter `Demo Person`, submit, then
review any duplicate candidates before making a choice. Open Known faces to
verify the resulting record/photo. This walkthrough is not an executed upload.

## Common dialogs, tables and maps

Close, Cancel and Escape dismiss supported dialogs. They do not reverse saves
already submitted. A confirmation button sends the pending action; read the
person/list/job and impact before confirming. A disabled button usually reflects
an unavailable action, missing input, active request or server capability.

Previous/Next and page-size controls affect pagination. Filters select the data
to display; they do not delete rows unless the action explicitly says so.
Map zoom/pan and timeline zoom/fit change the view, not saved observations.
Selecting a new camera location is only saved by Save Location. Basemap styles
need their local archives; see [Maps](maps.md).

Sources: [navbar loader](../frontend/js/navbar-loader.js),
[admin navbar](../frontend/components/admin-navbar.html),
[upload component](../frontend/components/upload-modal.html),
[upload behavior](../frontend/js/upload-modal.js),
[modal lifecycle](../frontend/js/modal-stack.js).
