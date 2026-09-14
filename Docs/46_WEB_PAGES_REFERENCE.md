# 99 — Web pages reference: every page, every control, every workflow

**Purpose.** One place that answers, for every page of the VAS web application:
what is on it, who may open it, what each button / form / modal does, which
API it calls with which payload, what the backend then does (tables written,
side effects, audit rows), and what the user sees back. Written from the code
on 2026-09-12 (`frontend/*.html`, `frontend/js/*.js`, `backend/routes/*.py`,
`sql_agent/api/routes.py`); the API summary behind it is regenerated with
`scripts/` tooling described at the end. The chatbot integration (LAF-AI) is
in section 20 and in `DEPLOYMENT.md`.

## 0. How the pages work (read this first)

- **Every page URL is a backend route** (`backend/routes/dashboard.py`) that
  checks the session and returns the HTML file from `frontend/`. Access is
  enforced there, before any HTML is sent: `require_strict_access(allowed_roles=["admin"])`
  for `/admin/*`, `get_current_user` for `/home` and `/dashboard`,
  `require_chatbot_access()` for `/tracking-people`, `get_current_user_allow_pending_rotation`
  for `/change-password`. Unauthenticated → **302 `/signin`**; forbidden →
  302 `/dashboard` (or `/change-password` when a password rotation is pending).
- **Session** = the HttpOnly cookie `__Host-access_token` set by `POST /api/auth/login`
  (JWT with `jti`; revoked on logout; invalidated by a password change). Page
  JavaScript never sees the token; every `fetch` sends the cookie
  (`credentials: 'include'`) and every state-changing call adds
  `X-Requested-With: XMLHttpRequest`, which the backend's CSRF dependencies
  (`require_auth_csrf`, `require_user_admin_csrf`, `require_upload_csrf`, …) demand.
- **Navigation** is one shared component loaded by `js/navbar-loader.js`
  (`components/admin-navbar.html` on admin pages, home, dashboard; `components/navbar.html`
  elsewhere). It calls `GET /api/auth/me/privileges`; the backend returns
  `navbar_links` and the loader shows **only** those links — the frontend never
  decides visibility. TRACKING is emitted only when the account has
  `can_use_chatbot`; admin-only pages only for `role = admin`.
- **Buttons** are declared with `data-action="name"` and dispatched by
  `js/actions.js` (`Actions.register(name, handler)`); `data-action-submit`
  for forms, `data-action-change` for selects. Modals are stacked/escaped by
  `js/modal-stack.js`. `js/page-init.js` runs the page's initialiser after the
  navbar is in place.
- **Live data**: `dashboard.html` opens the WebSocket `/ws` (cookie-authenticated,
  origin-checked, pipeline-scoped) and receives `initial_data`, `new_detection`,
  `unknown_activity`, `detection_alerts` (persisted live-alert / watchlist hits),
  `config_changed`, `background_task_notification`, `background_task_completed`, `ping`.
- **Images** are served by `GET /storage/{path}` (authenticated, path-confined).
- **"WRITE"** below means the backend changes the database in that call.

### 0.1 Page index

| URL | File | Who | Section |
|---|---|---|---|
| `/signin` | `signin.html` | anyone | 1 |
| `/change-password` | `change-password.html` | signed in (rotation pending allowed) | 2 |
| `/home` | `home.html` | signed in | 3 |
| `/dashboard` | `dashboard.html` | signed in, active | 4 |
| `/tracking-people` | `tracking-people.html` | Chatbot permission (legacy assistant page; TRACKING now opens LAF-AI) | 5 |
| `/admin/users` | `admin/users.html` | admin | 6 |
| `/admin/pipelines` | `admin/pipelines.html` | admin | 7 |
| `/admin/audit` | `admin/audit.html` | admin | 8 |
| `/admin/unknown` | `admin/unknown.html` | Unknown-faces permission | 9 |
| `/admin/identity` (+ `/admin/identity/{id}`) | `admin/identity.html` | identity permission | 10 |
| `/admin/search` | `admin/search.html` | admin | 11 |
| `/admin/search-history` | `admin/search-history.html` | admin | 12 |
| `/admin/watchlists` | `admin/watchlists.html` | admin | 13 |
| `/admin/live-alerts` | `admin/live-alerts.html` | admin | 14 |
| `/admin/intelligence` | `admin/intelligence.html` | admin | 15 |
| `/admin/security-intelligence` | `admin/security-intelligence.html` | admin | 16 |
| `/admin/ml-model` | `admin/ml-model.html` | admin | 17 |
| `/admin/ml-ops` | `admin/ml-ops.html` | admin | 18 |
| `/admin/background-tasks` | `admin/background-tasks.html` | admin | 19 |
| `/admin/ingest-credentials` | `admin/ingest-credentials.html` | admin | 21 |
| `/admin/logs` | `admin/logs.html` | admin | 22 |
| `/admin/settings` | `admin/settings.html` | admin | 23 |
| `/admin/tutorial` | `admin/tutorial.html` | admin | 24 |
| `/docs`, `/redoc` | generated | signed in | API explorer (Swagger / ReDoc) |
| **`https://armyeye-chatbot/`** | LAF-AI (separate service) | Chatbot permission, via TRACKING | 20 |

Shared pieces: the **Add-person upload modal** (section 25) and the navbar (section 0).

---

## 1. `/signin` — Sign in

**File** `signin.html` + `js/signin.js` (+ `signin-chrome.js` for the visual chrome). Open to everyone; a signed-in user landing here is redirected by the backend to the role's start page.

| Control | What it triggers | Backend | Result |
|---|---|---|---|
| Form `#signin-form` (username, password) → **Sign in** | `POST /api/auth/login` `{username, password}` with `X-Requested-With: XMLHttpRequest` (browser mode) | `login` (`backend/routes/auth.py`): origin/CSRF check, rate limits per IP + account (Redis, 429 with `RATE_LIMITED`), lockout, password verify; on success mints the JWT and sets the `__Host-access_token` cookie (no token in the body for browsers); audit event `login` success/failure | Redirects to `redirect_url` from the response: `/change-password` if a rotation is pending, else the role's page (`redirect_for_role`). Errors are mapped from the structured body: `INVALID_CREDENTIALS`, `RATE_LIMITED`, `CSRF_FAILED`, `AUTH_SERVICE_UNAVAILABLE` |

Database: **no writes** except the security log / rate-limit counters (Redis). Failed attempts feed the per-IP and per-account lockout.

## 2. `/change-password` — Change password

**File** `change-password.html` + `js/change-password.js`. Reachable with a rotation pending (bootstrap admin, admin-issued reset). The page enforces client-side: all three fields, minimum length, new = confirm; the backend enforces the real policy.

| Control | Triggers | Backend | Result |
|---|---|---|---|
| Form (current, new, confirm) → **Change password** | `POST /api/auth/change-password` `{current_password, new_password}` | `change_password` (`auth.py`) **WRITE**: verifies the current password, stores the new hash, stamps `password_changed_at` (every OTHER session becomes invalid — tokens issued before the stamp are rejected), clears `must_change_password`, revokes the presented token and issues a fresh one; audit event | On success the page sends the user to `/signin` (a fresh sign-in with the new password). Alert popup (`#alert-popup-dismiss`) shows errors such as a wrong current password or policy violations |

Also on the page: the form itself is `#change-password-form`; **Sign out** in the navbar (`#logout-btn`) calls `POST /api/auth/logout`.

## 3. `/home` — Home (system overview)

**File** `home.html` + `js/home.js`. Read-only overview; KPIs are filtered to the pipelines the caller may see.

| Control | Triggers | Backend | Result |
|---|---|---|---|
| Page load / **Refresh** (`data-action="refreshStats"`) | `GET /api/stats`, `GET /api/dashboard/pipelines`, `GET /api/auth/me` | `get_stats` (`stats.py`): counts of pipelines/detections/faces, queue, processed, storage, cache, tracker, retention, database health, config (all scoped to the user's pipeline access); `get_dashboard_pipelines`: the authoritative pipeline list | KPI cards `#kpi-pipelines/detections/faces/queue/processed/storage/cache/tracker`, health summary, freshness times |
| **Open dashboard** (link) | navigation | — | `/dashboard` |
| **Add person** (`data-action="openUploadModal"`) | opens the shared upload modal (section 25) | — | — |
| **Unknown faces**, **Pipelines**, **Users** cards (links) | navigation | — | `/admin/unknown`, `/admin/pipelines`, `/admin/users` |
| **Tracking people** card (`#tracking-link`) | `GET /api/sso/laf-ai/launch` | one-time ticket → LAF-AI (section 20) | the chatbot opens, already signed in |
| **Health** (`data-action="focusHealth"`) | scrolls to the health block | — | — |

Database: no writes (the stats endpoint may refresh cached aggregates).

Also on the page: `#hp-refresh` (same as **Refresh**), `#pipeline-select` / `data-action="selectPipeline"` (scopes the KPI figures to one camera, client-side), the navbar **Sign out** (`#logout-btn` → `POST /api/auth/logout`) and **Refresh** (`#navbar-refresh-btn`, reloads the privileges/navbar).

## 4. `/dashboard` — Live pipeline feeds

**File** `dashboard.html` + `js/dashboard.js`, `face-image.js`. Real-time detections per pipeline for the pipelines the user may see.

**Load sequence:** `GET /api/dashboard/config` (display windows, alert settings; falls back to built-in defaults if unavailable) → `GET /api/dashboard/pipelines` (authoritative list; cards for pipelines missing from it are pruned) → for each pipeline `GET /api/detections/{pipeline_id}?limit=50` (recent faces) → WebSocket `/ws` (cookie auth; server sends `initial_data`, then pushes).

| Control / event | Triggers | Backend | Result |
|---|---|---|---|
| WebSocket `new_detection` | pushed by `services/image_processing.py` after a webhook frame is processed | — (already persisted: `detections`, `faces`, `identity_appearances`) | a face card appears on the pipeline's feed; known persons show name + similarity; toast `showRealtimeNotification` |
| `unknown_activity` | pushed when an unknown face is stored | — | unknown counter / card (only if `show_unknown_on_dashboard`) |
| `detection_alerts` | pushed by `core/detection_evidence.py` when a persisted **live-alert trigger** or **watchlist** hit is written | tables `live_alert_triggers`, `watchlist_alerts` (written by the pipeline, not by this page) | the **advanced alert modal** opens (`showAdvancedAlert`: person, pipeline, time, similarity; sound if configured; per-person cooldown) |
| **Acknowledge** (`#alert-ack-btn`) | `closeAdvancedAlert()` | none (client-side only) | closes the modal |
| **History** (`#alert-history-btn`, `#alert-toggle-btn`, close `#alert-history-close-btn`) | `toggleAlertHistory()` — the session's alert list kept in memory; `replayHistoryEntry(i)` reopens one | none | side panel of alerts seen in this browser session |
| **Enable alert sound** | browser audio unlock | none | sounds on later alerts |
| Click a known face | `viewKnownPersonIdentity(name)` → `GET /api/admin/identities/search?query=<name>&limit=1` | `identities.py` search | opens the identity details (`/admin/identity/{id}`) |
| `config_changed` | pushed when admin settings change | — | reload of dashboard config |
| `background_task_notification` / `background_task_completed` | pushed by `core/background_task_notifier.py` | — | toast about a finished job (cluster, cleanup, …) |

Database: **read only** from this page (the WebSocket handler may record subscription state; detection rows are written by the ingest pipeline).

Also on the page: `#sound-toggle-btn` (mute/unmute alert sounds, remembered in `localStorage`).

## 5. `/tracking-people` — Legacy data assistant (SQL agent)

**File** `tracking-people.html` + `js/tracking.js`, `js/conversations.js`. Requires the Chatbot permission (`require_chatbot_access()`). Since 2026-09-12 the **TRACKING** menu item opens **LAF-AI** instead (section 20); this page remains reachable by URL and keeps working unchanged.

| Control | Triggers | Backend (`sql_agent/api/routes.py`, prefix `/api/sql-agent`; `backend/routes/conversations.py`, prefix `/api/v1`) | Result |
|---|---|---|---|
| Question box → **Send** (`#sendBtn`) | `POST /api/sql-agent/query/stream` (SSE, preferred) — falls back to `POST /api/sql-agent/query` (REST); WebSocket `/ws/sql-agent` is opt-in (`localStorage.sqlAgentTransport = websocket`) | `sql_agent_query_stream`: LangGraph agent → generated SQL runs as the agent's read-only DB role → streamed `status/sql/content/complete/error` events; every question is written to `chatbot_audit_log` and `user_query_history` (WRITE) | Answer rendered incrementally; SQL and results shown |
| **New chat** (`#newChatBtn`, `#newChatTopBtn`) | `POST /api/v1/conversations` | `create_conversation` (WRITE `conversations`, `conversation_branches`) | fresh conversation |
| History sidebar (`#sidebarToggleBtn`, close, backdrop) | `GET /api/v1/conversations`, `GET …/{id}/messages`, `GET …/{id}/branches` | read | list, open, pin/archive (`PATCH …/flags`), rename (`PATCH …/{id}`), soft-delete (`DELETE …/{id}`, stamps `deleted_at`), fork (`POST …/{id}/branches`), rate (`POST …/{id}/feedback`) |
| **Instructions** (`#showInstructionsBtn`) | client-side panel | — | usage hints |
| Cancel a running question | `POST /api/sql-agent/requests/{id}/cancel` (CSRF header) | stops the worker thread | stream ends with `cancelled` |
| Export | `POST /api/sql-agent/export/pdf` / `export/word` (CSRF) | bounded document build | file download; artifacts via `GET /api/sql-agent/artifacts/{id}` |
| History | `GET /api/sql-agent/history`, `DELETE /api/sql-agent/history/{id}` (CSRF) | owner-scoped rows of `user_query_history` | list / delete own entries |
| Sign out (top-right) | `POST /api/auth/logout` | revokes the token (and any linked LAF-AI session) | `/signin` |

Also used: `GET /api/pipelines` (scope pickers), `GET /api/auth/me`, `GET /api/sql-agent/schema`, `/context`, `/memory` (per-user memories, WRITE on create/delete).

Also on the page: `#sidebarBackdrop` and `#sidebarCloseBtn` close the history sidebar (client-side only).

## 6. `/admin/users` — User management (admin)

**File** `admin/users.html` + `js/admin-users.js`. Loads `GET /api/users` (all accounts) and `GET /api/pipelines` (for camera assignment).

| Control | Triggers | Backend (`backend/routes/users.py`, all `require_role(["admin"])` + CSRF header) | Result |
|---|---|---|---|
| **Create user** (`#create-user-btn`) → modal `#user-modal` (fields: `modal-username`, `modal-email`, `modal-full-name`, `modal-password`, `modal-role`, **Active** `is_active`, **Chatbot** `chatbot_access`, pipeline checkboxes) → **Save** | `POST /api/users` `{username, email, password, full_name, role, can_use_chatbot, pipeline_ids}` | `create_user` **WRITE** `users` (+ `user_pipeline_access` rows for the selected cameras). Role must be an assignable non-admin role: **admin cannot be granted through the UI** | new row in the table |
| **Edit** (`data-action="editUser"`) → same modal → **Save** | `PUT /api/users/{id}` with only the changed fields (`email, password, full_name, role, is_active, can_use_chatbot, pipeline_ids`) | `update_user` **WRITE**: replaces the camera assignments, bumps `permissions_version` (the user's open sessions re-evaluate permissions), and — this is what enables/disables the chatbot — sets `can_use_chatbot`; LAF-AI notices within ~10 s | row updated; the affected user's navbar gains/loses TRACKING |
| **Reset password** (`data-action="resetPassword"`) → modal `#password-modal` → **Reset** | `POST /api/users/{id}/reset-password` `{new_password}` | `reset_password` **WRITE** `password_hash` + `password_changed_at` (all of that user's sessions become invalid), sets the rotation flag so the user must choose their own password at next sign-in | message "Password reset successfully" |
| **Unblock** (`data-action="unblockUser"`) | `POST /api/users/{id}/unblock` | `unblock_user` **WRITE**: `is_active = true`, clears `blocked_reason` (accounts get blocked automatically by the SQL agent after forbidden SQL, see `44_BLOCKED_USERS.md`) | user can sign in again |
| **Delete** (`data-action="deleteUser"`) → modal `#delete-user-modal` → **Confirm** | `DELETE /api/users/{id}` | `delete_user` **WRITE**: removes the `users` row; conversations, query/search history and audit rows survive with `user_id = NULL` and `historical_user_id` stamped for attribution; you cannot delete yourself or the protected `system` principal | row removed |
| **Blocked only** / **All users** (`filterBlockedUsers` / `showAllUsers`) | client-side filter on `blocked_reason` | — | filtered table |
| **Restore system principal** (`restoreSystemPrincipal`) | `POST /api/users/system/restore` | recreates/repairs the protected `system` account that machine-written audit rows belong to | — |

Also on the page: the forms are `#user-form` and `#password-form`; `#confirm-delete-btn` confirms the delete; `#btn-all-users`, `#btn-blocked-only`, `#btn-restore-system-principal` are the buttons behind `showAllUsers`, `filterBlockedUsers`, `restoreSystemPrincipal`; `closeModal`, `closePasswordModal`, `closeDeleteModal` only close their modal (no request).

## 7. `/admin/pipelines` — Cameras / pipelines (admin)

**File** `admin/pipelines.html` + `js/admin-pipelines.js`. Pipelines are created by the ingest side (a webhook from a camera pipeline registers `pipelines.pipeline_id`); this page manages what operators see.

| Control | Triggers | Backend | Result |
|---|---|---|---|
| Page load / **Refresh** (`#refresh-btn`) | `GET /api/pipelines` | `get_all_pipelines` (scoped; admins see all) | table `#pipelines-table-body`: id, display name, location, status, last webhook, detections |
| **Rename** (`data-action="renamePipeline"`, inline prompt) | `PUT /api/pipelines/{id}/rename` `{new_name}` — sanitised like a pipeline id: anything outside `A-Za-z0-9_-` becomes `_`, so "Gate Camera" is stored as `Gate_Camera` | `rename_pipeline` **WRITE**: this **re-keys the camera**, it does not set a label — there is no `display_name` column. In one transaction it copies the row to the new `pipeline_id`, re-points `detections`, `user_pipeline_access`, `identity_embeddings`, `identity_appearances`, `watchlist_alerts` and `live_alert_triggers`, deletes the old row, and writes a `pipeline_aliases` row (old → new) **so a camera still posting under the old id keeps working**. 409 if the new name is taken | the camera appears under the new id everywhere, with its history intact |
| **Coordinates** (`data-action="openCoordinatesModal"`) → modal `#coordinates-modal` (map picker `#coordinates-map`, latitude, longitude, location name) → **Save** (`saveCoordinates`) | `PUT /api/pipelines/{id}/coordinates` `{latitude, longitude, location_name}` | `update_pipeline_coordinates` **WRITE** `pipelines.latitude/longitude/location_name` | used by the map features (identity map, security intelligence) |

Camera **access per user** is not set here but on the Users page (pipeline checkboxes → `user_pipeline_access`).

Also on the page: `closeCoordinatesModal` closes the modal without saving (no request).

## 8. `/admin/audit` — Audit Log (admin)

**File** `admin/audit.html` + `js/admin-audit.js`. Shows the **chatbot audit log** (`chatbot_audit_log`): every question asked to the legacy assistant and, since 2026-09-12, every question asked to **LAF-AI** plus the acknowledgement of its responsible-use notice.

| Control | Triggers | Backend (`backend/routes/audit.py`, admin only) | Result |
|---|---|---|---|
| Page load / **Refresh** (`refreshData`) | `GET /api/audit/chatbot/stats`, `GET /api/audit/chatbot?limit&offset[&user_id&username&success&start_date&end_date]`, `GET /api/users` (username filter list) | reads; no writes | KPI cards (total / successful / failed / unique users / avg time) and the table |
| Filters form (`applyFilters`: user, username, success, dates) / **Clear** (`clearFilters`) | same list call with parameters | — | filtered rows |
| **Details** (`data-action="viewDetails"`) → modal `#details-modal` | `GET /api/audit/chatbot/{log_id}` | one row: username, question, response, success, error, processing time, session id, time | full text |
| Pagination (`previousPage`, `nextPage`, `#page-limit`) | offset/limit | — | — |

Row meaning: `query` = the question; `response` = the legacy agent's answer, or `[laf-ai] answered in the chatbot session` for LAF-AI rows (the answer itself stays in the chatbot's own session logs); `session_id` = the chatbot session; `[notice] Responsible-use notice acknowledged` rows record that the user saw the notice. Written by: the legacy agent (`sql_agent/api/routes.py::log_chatbot_query`) and the LAF-AI gate through `POST /api/audit/chatbot` (internal-only endpoint, identity from the user's own token — a client cannot write rows for another user). Rows of deleted users keep `historical_user_id`.

Also on the page: filter fields `#filter-username`, `#filter-success`; paging buttons `#prev-btn` / `#next-btn`; `changeLimit` (rows per page, `#page-limit`); `closeDetailsModal` closes the details modal (no request).

## 9. `/admin/unknown` — Unknown Faces Center

**File** `admin/unknown.html` + `js/admin-unknown.js` (the largest page). Requires the Unknown-faces permission (`require_unknown_faces_access`). Lists faces the pipeline stored as **unknown identities** (`identities.status = unknown`), grouped per camera, with the tools to turn them into known people or merge them.

**Load:** `GET /api/auth/me`, `GET /api/auth/me/privileges` (which tools to show), `GET /api/pipelines` (filter), then `GET /api/admin/unknown?page&limit&pipeline_id&…` (paginated; images via `/storage/...`).

| Control | Triggers | Backend (`backend/routes/identities.py`, prefix `/api/admin`) | Result |
|---|---|---|---|
| Filters (`#pipeline-filter`, dates…) → **Apply** / **Clear**; pagination `#prev-page-btn` / `#next-page-btn` | `GET /api/admin/unknown?…` | `list_unknown_identities` (scoped to the caller's cameras) | grid refresh |
| Card click → **Identity detail modal** (`#identity-detail-modal`) | `GET /api/admin/identity/{id}` | `get_identity_details`: identity, images, appearances per pipeline, timeline | detail view with the actions below |
| **Promote** → modal `#promote-modal` (`#promote-form`: display name, optional person code; candidate list from `GET /api/admin/unknown/{id}/match-candidates` — "looking must never change anything") → **Promote** | `POST /api/admin/unknown/{id}/promote` `{display_name, person_code?, decision}` — `decision` is either *create a new known person* or *merge into the suggested known person* (`mergeIntoKnownCandidate` → `POST /api/admin/identities/merge`) | `promote_unknown_to_known` **WRITE**: `identities.status → known`, `display_name`, moves the vectors from the unknown to the known index (pgvector/FAISS), audit row (`user_authorization_audit_log`) | the person now appears on the dashboard by name |
| **Merge** → modal `#merge-modal` (`#merge-form`: from/to identity, search box `searchIdentityForMerge` → `GET /api/admin/identities/search?query=`) → **Merge** | `POST /api/admin/identities/merge` `{from_identity_id, to_identity_id, notes?, decision}`; a **risk gate** (`confirmMergeRisk`) asks for confirmation when the preview flags cross-pipeline or type conflicts | `merge_identities` **WRITE**: the loser gets `merged_into_id`, its appearances/faces/embeddings are re-parented to the winner, an `identity_merges` row records it (reversible with `POST /api/admin/identities/merges/{merge_id}/unmerge`) | one identity |
| **Multi-select** (`#multi-select-toggle`) → **Merge selected** (`#merge-multiple-btn`) → **Preview** (`openAdvancedMergePreview`, modal `#merge-preview-modal`) → **Execute** (`executeMergeFromPreview`) | `POST /api/admin/identities/merge-preview` `{identity_ids, target_identity_id?}` → `POST /api/admin/identities/merge-multiple` `{identity_ids, target_identity_id?, notes?, confirm_merge_risk}` | `preview_merge` (read: target choice, pipeline distribution, type promotion, snapshot selection, statistics) → `merge_multiple_identities` **WRITE** (same effects as merge, N-to-1) | one identity |
| **Merge suggestions** (`#merge-suggestions-btn`, modal `#merge-suggestions-modal`) | `GET /api/admin/merge-suggestions` (ML similarity model / clustering) → per suggestion **Approve** `POST …/{id}/approve` `{confirm_merge_risk}` or **Reject** `POST …/{id}/reject` | `approve_merge_suggestion` **WRITE** (executes the merge), `reject_merge_suggestion` **WRITE** (`merge_suggestions.status`) | list shrinks |
| **Pipeline suggestions** (per camera, modal `#pipeline-merge-suggestions-modal`) | `GET /api/admin/merge-suggestions/pipeline/{pipeline_id}` (DBSCAN clustering on that camera's unknowns) | read | clusters to approve/reject as above |
| **Search by image** (`#search-by-image-btn`, modal `#search-image-modal`, form: image, scope known/unknown/both, top-k, dates, pipeline) | `POST /api/search/by-image` (multipart) | `search_by_image` (admin): embeds the face, nearest neighbours; audit row | ranked matches |
| **Add to watchlist** (modal `#add-to-watchlist-modal`: watchlist, priority, notes) | `GET /api/watchlists/add-identity/{id}/defaults` → `POST /api/watchlists/{watchlist_id}/entries` `{identity_id, priority, notes?, action_instructions?, expires_at?}` | `add_to_watchlist` **WRITE** `watchlist_entries` (+ audit) | future sightings raise watchlist alerts (dashboard `detection_alerts`) |
| **Create live alert** (modal `#create-live-alert-modal`) | `GET /api/live-alerts/defaults/{id}` → `POST /api/live-alerts` (fields in section 14) | `create_live_alert` **WRITE** `live_search_alerts` (+ audit) | sightings trigger notifications |
| Face-detection alert modal (`#face-detection-alert-modal`) | shown when a face was detected in an uploaded search image | — | information |
| **Your live alerts** (`#your-live-alerts-btn`) | navigation | — | `/admin/live-alerts` |

Also on the page: `#apply-filters-btn` / `#clear-filters-btn` (filters), `#show-all-toggle-btn` (show every camera group expanded), `#preview-merge-btn` (= `openAdvancedMergePreview`), `#promote-none-of-these` (in the promote modal: ignore the suggested candidates and create a new person), the modal forms `#promote-form`, `#merge-form`, `#search-image-form`, `#create-live-alert-form`, `#add-to-watchlist-form` (with `#watchlist-select`, `#watchlist-priority`), `copyIdentityId` / `copyIdentityIdFromAlert` (clipboard), and the close/cancel buttons of every modal (`#close-detail-modal`, `#close-promote-modal`, `#cancel-promote-btn`, `#close-merge-modal`, `#cancel-merge-btn`, `closeMergePreviewModal`, `#close-suggestions-modal`, `#close-pipeline-suggestions-modal`, `#close-search-modal`, `#cancel-search-btn`, `#close-live-alert-modal`, `#cancel-live-alert-btn`, `#close-watchlist-modal`, `#cancel-watchlist-btn`, `#close-face-alert-modal`) — closing never sends a request.

## 10. `/admin/identity` and `/admin/identity/{id}` — Identity profile

**File** `admin/identity.html` + `js/admin-identity.js`, `js/identity-map.js`. Opened from the dashboard (click a known face), search results and the Unknown Faces center (`?from=` keeps a safe back link).

| Element | Triggers | Backend | Result |
|---|---|---|---|
| Page load | `GET /api/admin/identity/{id}`, `GET /api/dashboard/pipelines` (names), `GET /api/identities/{id}/watchlists`, `GET /api/maps/availability` → if maps are available `GET /api/identities/{id}/map-data` (GeoJSON) | reads | header (name, status/type badges, snapshot), facts, watchlists, **timeline** of appearances (`renderAdvancedTimeline`, scale buttons), map of sightings by camera coordinates |
| **Analyze** (`#profile-analyze-link`) | navigation | — | `/admin/intelligence?identity=…` (section 15) |
| **Manage** (`#profile-manage-link`, admins) | navigation | — | Unknown Faces center with this identity |
| **Add to watchlist** (`openWatchlistModal`) | as section 9 | **WRITE** `watchlist_entries` | — |
| **Create live alert** (`openLiveAlertModal`) | as section 14 | **WRITE** `live_search_alerts` | — |
| Copy identity id | clipboard | — | — |

Photos of a known person are managed by the API `GET/POST /api/identities/{id}/images` and `PUT …/images/{image_id}/primary` (the upload modal, section 25, calls the POST when an existing name is chosen).

Also on the page: the two modal forms `#add-to-watchlist-form` (`#watchlist-select`, `#watchlist-priority`) and `#create-live-alert-form` with their cancel/close buttons (`#cancel-watchlist-btn`, `#close-watchlist-modal`, `#cancel-live-alert-btn`, `#close-live-alert-modal` — no request); `copyProfileIdentityId` / `copyIdentityIdFromAlert` copy the UUID to the clipboard.

## 11. `/admin/search` — Advanced face search (admin)

**File** `admin/search.html` + `js/admin-search.js`. Multi-face search of an uploaded image against known and/or unknown identities, with quality scoring and watchlist checks.

**Load:** `GET /api/search/config` (limits: sizes, top-k caps, quality threshold), `GET /api/pipelines`, `GET /api/dashboard/pipelines`, `GET /api/admin/identities` and `GET /api/watchlists` (for the exclusion pickers).

| Control | Triggers | Backend (`backend/routes/advanced_search.py`, admin + `require_search_csrf`) | Result |
|---|---|---|---|
| Drop zone / file input, **Remove image** | client validation (`validateImageFile`: type, size from config) | — | preview |
| **Quality check** (`#quality-check-btn`) | `POST /api/search/quality-check` (multipart `image`) | `check_image_quality` (read) | blur/size/pose score, warning below threshold |
| **Search** (`#search-btn`; options `#search-scope`, `#top-k`, `#pipeline-filter`, `#exclude-identities`, `#exclude-watchlists`, dates) | `POST /api/search/advanced` (multipart: `image, scope, top_k, min_quality, check_watchlist, exclude_identity_ids, exclude_watchlist_ids, date_from, date_to, pipeline_id`) | `advanced_search`: detects every face in the image, embeds each, nearest neighbours per face, watchlist hits; writes a row to `search_history` | tabs **Matches** / **Alerts** (watchlist hits) / **Summary** |
| Multiple files → batch | `POST /api/search/batch` (rate-limited; progress modal `#close-batch-progress-btn`) | `batch_search` | per-image results |
| **Export** (`#export-btn` → modal: `#export-format`) → **Confirm** | `POST /api/search/export` or `/api/search/batch/export` (rate-limited) | document build | download |
| Match click → identity panel (`#close-identity-modal`, `#identity-analyze`) | `GET /api/admin/identity/{id}`, `GET /api/identities/{id}/watchlists` | reads | profile panel; Analyze → intelligence page |
| **Clear results** | client | — | — |

Also on the page: `#upload-area` (drop zone), `#remove-image`, `#clear-results-btn`, the export modal buttons `#export-btn` → `#confirm-export-btn` / `#cancel-export-btn` / `#close-export-modal`, and `#identity-modal-done` (closes the identity panel).

## 12. `/admin/search-history` — Search history (admin)

**File** `admin/search-history.html` + `js/admin-search-history.js`.

| Control | Triggers | Backend | Result |
|---|---|---|---|
| Filters `#filter-type`, `#days-back` → **Apply**; **Load more** | `GET /api/search/history?…` | `get_search_history` (caller's rows of `search_history`) | list |
| **Export** (modal: format, days) → **Confirm** | `GET /api/search/history/export?…` | file build | download |
| **Clear history** (`#clear-history-btn`) | `DELETE /api/search/history` (CSRF) | `clear_search_history` **WRITE** (caller's rows) | empty list |

Also on the page: `#apply-filters-btn`, `#load-more-btn`, `#export-history-btn` → modal with `#export-days-back`, `#export-format`, `#confirm-export-btn`, `#cancel-export-btn`, `#close-export-modal`.

## 13. `/admin/watchlists` — Watchlists (admin)

**File** `admin/watchlists.html` + `js/admin-watchlists.js`. A watchlist is a named group of identities (VIP, threat, person of interest…) with an alert level and notification channels; when a listed person is detected, the pipeline writes a `watchlist_alerts` row and the dashboard receives `detection_alerts`.

**Load:** `GET /api/watchlists` (with batched statistics). Cards are built by `buildWatchlistCard`; every card action below is rendered by the script.

| Control | Triggers | Backend (`backend/routes/watchlists.py`, admin + `require_watchlist_csrf`) | Result |
|---|---|---|---|
| **New watchlist** (`openCreateModal`, form `#watchlist-form`: name, description, alert level `#watchlist-alert-level`, colour/icon, channels) → **Save** | `POST /api/watchlists` `{name, description, color, icon, alert_level, notify_dashboard, notify_email, notify_sms, notify_webhook, email_recipients, …}` | `create_watchlist` **WRITE** `watchlists` (409 if the name exists); audit | new card |
| **Edit** (`openEditModal`) → **Save** | `PUT /api/watchlists/{id}` (same fields + `version` for optimistic concurrency) | `update_watchlist` **WRITE** (409 on a stale version) | card updated |
| **Activate / Deactivate** (`toggleWatchlistStatus`) | `PATCH /api/watchlists/{id}/status` `{is_active, reason}` | `change_watchlist_status` **WRITE** + audit | inactive lists raise no alerts |
| **Delete** (`deleteWatchlistFlow`) | `GET /api/watchlists/{id}/deletion-impact` (what would be affected) → `DELETE /api/watchlists/{id}` (soft delete by default; `?hard=true` after explicit confirmation) | `delete_watchlist` **WRITE** (`deleted_at`, or physical removal of the list and its entries) + audit | card gone; **Restore** (`restoreWatchlist` → `POST …/restore`) undoes a soft delete |
| Card → **Details drawer** (`openDetailDrawer`) | `GET /api/watchlists/{id}`, `GET …/stats?period`, `GET …/entries?page` | reads | members, hit statistics |
| Drawer → **Add identity** (`buildAddEntrySection`: search `GET /api/admin/identities`) | `POST /api/watchlists/{id}/entries` `{identity_id, priority, notes?, action_instructions?, expires_at?}` | `add_to_watchlist` **WRITE** `watchlist_entries` (duplicate-safe) + audit | member added |
| Drawer → **Remove** | `DELETE /api/watchlists/{id}/entries/{identity_id}` | `remove_from_watchlist` **WRITE** + audit | member removed |

Also on the page: `#modal-close-btn` closes the create/edit modal (no request).

## 14. `/admin/live-alerts` — Live search alerts (admin)

**File** `admin/live-alerts.html` + `js/admin-live-alerts.js`. A live alert watches for **one identity** (optionally on chosen cameras, in a time window, on chosen days) and notifies (dashboard, e-mail, SMS, webhook, sound, snapshot). Alerts are created here or from the Unknown Faces / Identity pages.

**Load:** `GET /api/live-alerts` (the caller's alerts; admins see all), `GET /api/search/config` (similarity bands), `GET /api/auth/me/privileges`; WebSocket `/ws?view=alerts` streams new triggers live (`handleWsMessage`, `playAlertSound`); **Reconnect** (`#ws-reconnect-btn`) reopens it.

| Control (per alert card) | Triggers | Backend (`backend/routes/live_alerts.py`, owner or admin, `require_unknown_faces_access`) | Result |
|---|---|---|---|
| **Pause** / **Resume** (`pauseAlert`, `resumeAlert`) | `POST /api/live-alerts/{id}/pause` / `…/resume` | `pause_live_alert` / `resume_live_alert` **WRITE** `live_search_alerts.is_active` + audit | no / again notifications |
| **Delete** (`deleteAlert`) | `DELETE /api/live-alerts/{id}` | `delete_live_alert` **WRITE** (+ audit) | card removed |
| **Health** (`showHealth`) | `GET /api/live-alerts/{id}/health` | trigger counts, last trigger, channel state | panel |
| **Test channels** (`runChannelTest`, admin) | `POST /api/live-alerts/{id}/test` → poll `GET /api/live-alerts/test-jobs/{job_id}` | `test_alert_channels`: asynchronous send on each configured channel; results per channel | pass/fail per channel |
| **Triggers** (`viewTriggers`, paginated toolbar) | `GET /api/live-alerts/{id}/triggers?page&filters` | rows of `live_alert_triggers` (who was seen where, when, similarity, snapshot) | history |
| **Acknowledge** one / **Acknowledge all** (`acknowledgeTrigger`, `bulkAcknowledge`) | `POST /api/live-alerts/triggers/{trigger_id}/acknowledge` / `POST /api/live-alerts/{id}/triggers/acknowledge-all` `{trigger_ids?}` | **WRITE** `acknowledged_at/by` + audit | cleared from the unacknowledged count |
| Create (from other pages) | `GET /api/live-alerts/defaults/{identity_id}` → `POST /api/live-alerts` `{name, identity_id, min_similarity, pipeline_ids?, time_window_enabled, time_window_start/end, active_days, cooldown_minutes, notify_dashboard/email/sms/webhook, email_recipients, sms_recipients, webhook_url, sound_alert, auto_capture_snapshot}` | `create_live_alert` **WRITE** `live_search_alerts` + audit (identity access enforced server-side) | alert card |
| **Search** (`#search-btn`) | navigation | — | `/admin/search` |

Also on the page: `#sound-toggle-btn` mutes/unmutes the trigger sound (client-side).

## 15. `/admin/intelligence` — Intelligence analysis (admin)

**File** `admin/intelligence.html` + `js/admin-intelligence.js`. Per-identity analytics: who appears with whom, when, and where.

**Load:** `GET /api/admin/identities` (picker `#identity-select`), `GET /api/pipelines`, `GET /api/dashboard/config`, `GET /api/security/capabilities` (which features are enabled), optional `POST /api/search/by-image` to pick an identity from a photo.

| Control | Triggers | Backend (`backend/routes/intelligence.py`, admin; every call is audited) | Result |
|---|---|---|---|
| Select identity → **Complete analysis** (`#refresh-complete-btn`) | `GET /api/identities/{id}/analyze` (rate-limited) | `analyze_identity`: all sections with per-section status | dashboard of cards |
| **Related identities** (`#refresh-related-btn`) | `GET /api/identities/{id}/related` | `get_related_identities` (co-appearance cache, `identity_relationships`) | list with counts |
| **Temporal patterns** (`#refresh-temporal-btn`, `#temporal-days-back`) | `GET /api/identities/{id}/temporal-patterns?days` | hour/day histograms | charts |
| **Cross-camera tracking** (`#refresh-tracking-btn`) | `GET /api/identities/{id}/cross-camera` | movement between cameras | path list |
| **Timeline** (`#timeline-btn`) | `GET /api/identities/{id}/timeline` | movement timeline | timeline |
| **Map** (`#map-btn`, `#refresh-map-btn`, `#map-style-select`) | `GET /api/maps/availability` → `GET /api/identities/{id}/map-data` | GeoJSON of sightings at camera coordinates | MapLibre map (offline tiles from the `martin` service) |
| **Calculate all relationships** (`#calc-all-relationships-btn`) | `POST /api/intelligence/relationships/calculate-all` (CSRF) → poll `GET /api/intelligence/relationships/jobs/{job_id}` | schedules a **background job** (202) that recomputes `identity_relationships` for everyone; progress in Background Tasks (section 19) | cache refreshed |
| **Security analysis** (`#analyze-security-btn`) | navigation | — | `/admin/security-intelligence` |

## 16. `/admin/security-intelligence` — Security intelligence (admin)

**File** `admin/security-intelligence.html` + `js/admin-security-intelligence.js`. Six tabs; every call is admin-only, rate-limited and audited (`backend/routes/intelligence.py`, `risk_assessments.py`).

| Tab / control | Triggers | Backend | Result |
|---|---|---|---|
| **Network** — pick identities `#network-identity-ids` → **Analyze** (`#network-analyze-btn`) | `GET /api/security/network?identity_ids&…` | `get_social_network` (bounded graph of co-appearances) | graph + communities |
| **Patterns** — camera `#patterns-pipeline-id` → **Detect** (`#patterns-detect-btn`) | `GET /api/security/patterns?pipeline_id&…` | `get_suspicious_patterns` (enveloped; truncation visible) **WRITE** (persists findings) | list of patterns |
| **Anomalies** — identity → **Detect** (`#anomalies-detect-btn`) | `GET /api/security/anomalies/{id}` | `get_anomalies` (rule-based; ML shadow, see section 18) | anomalies or "not enough data" |
| **Threats** — identity → **Assess** (`#threat-assess-btn`) | `GET /api/security/threat/{id}` **WRITE** (`threat_assessments` + `risk_signal_results` rows) then `GET /api/security/assessments/history/identity/{id}`; per assessment `POST /api/security/assessments/{id}/acknowledge` / `resolve` / `reopen` | risk score with calibration status; lifecycle open → acknowledged → resolved | assessment cards |
| **Map** (`#map-load-btn`, `#map-identity-id`, `#map-style-select`) | as section 15 | — | map |
| **Advanced** — **Predict next camera** (`#trajectory-predict-btn`) | `GET /api/intelligence/trajectory/predict?identity_id&current_camera` | Markov-style prediction | ranked cameras |
| **Advanced** — **Correlation** (`#correlation-calc-btn`, identities A/B) | `GET /api/intelligence/correlation/calculate?a&b` | activity association | score |
| **Advanced** — **Learn thresholds** (`#threshold-learn-btn`, `#threshold-pipeline-ids`) / **Learn all** (`#learn-all-thresholds-btn`) | `POST /api/intelligence/thresholds/jobs` (CSRF) → poll `GET …/jobs/{job_id}` | background job (single-flight) that writes `learned_thresholds`; activation via `POST /api/security/learned-thresholds/{id}/activate` **WRITE** + audit | thresholds proposed → activated |
| **Feature status** (`#feature-status-btn`) / **Help** | `GET /api/security/capabilities` | honest per-feature status (enabled, data sufficiency) | dialog |

Also on the page: the identity/camera pickers of each tab (`#anomaly-identity-id`, `#threat-identity-id`, `#trajectory-identity-id`, `#trajectory-current-camera`, `#correlation-identity-a`, `#correlation-identity-b`), `#feature-help-btn` (help text), `#close-pattern-detail-modal` (closes a pattern's detail view).

## 17. `/admin/ml-model` — Merge-suggestion similarity model (admin)

**File** `admin/ml-model.html` + `js/admin-ml-model.js`. Trains and manages the model that scores merge suggestions (section 9). Training never runs inside a request: it is a staged **background job**.

| Control | Triggers | Backend (`backend/routes/identities.py`, admin + `_require_ml_csrf`) | Result |
|---|---|---|---|
| Page load / **Refresh** (`#refresh-status-btn`) | `GET /api/admin/merge-suggestions/model-status`, `GET …/models`, `GET …/training-jobs` | status (sample counts from the persistent dataset), model registry history, recent jobs | status cards |
| **Train** (`#train-btn`) | `POST /api/admin/merge-suggestions/training-jobs` → poll `GET …/training-jobs/{job_id}` | `create_training_job`: schedules the job (Background Tasks), audit | progress; the new model appears as a **candidate** |
| **Cancel** (`#cancel-train-btn`) | `POST …/training-jobs/{job_id}/cancel` | best-effort cancel between stages | — |
| Model row → **Activate** / **Reject** / **Rollback** | `POST …/models/{model_id}/activate` (gates + artifact hash + load test, archives the previous active) / `…/reject` `{reason?}` / `…/rollback` | **WRITE** model registry (`ml_similarity_models` status) + audit | active model used for future suggestions |

## 18. `/admin/ml-ops` — ML operations (admin)

**File** `admin/ml-ops.html` + `js/admin-ml-ops.js`, `js/mlops-workflow.js` (guided runbook), `js/mlops-platform.js`. The anomaly/relational ML platform: features → labels → datasets → training → shadow evaluation → (optional) promotion, with drift monitoring. **Rules remain the decision system**; ML runs in shadow unless explicitly promoted. Every mutating call is audited (`backend/routes/ml_ops.py`, admin).

| View / control | Triggers | Backend | Result |
|---|---|---|---|
| **Overview** (`#refresh-overview-btn`), guided **Workflow / runbook** (`#workflow-refresh`, `#mlops-runbook-*`, `#mlops-next-step-action`) | `GET /api/ml/overview`, `GET /api/ml/capabilities` | readiness of each stage | next-step guidance |
| **Mode** / **Pause** (`#pause-ml-btn`) | `PUT /api/ml/config/mode`, `POST /api/ml/pause` | switches shadow/off; pauses jobs | — |
| **Compute features** (`#compute-features-btn`) | `POST /api/ml/features/compute` (definitions from `GET /api/ml/features/definitions`) | background job **WRITE** feature tables | job in Background Tasks |
| **Labels** (`#create-label-btn`, `#label-kind`, `#label-selection-method`, `#label-event-time`; paging) | `GET /api/ml/labels`, `GET /api/ml/labels/stats`, `POST /api/ml/labels`, `POST /api/ml/labels/{id}/review`, `…/supersede` | **WRITE** `ml_labels` | labelled events for training |
| **Datasets** (`#build-dataset-btn`, definition / kind / sampling / split / date range; `POST …/backfill-hashes`; archive) | `GET /api/ml/datasets/definitions`, `GET /api/ml/datasets`, `POST /api/ml/datasets`, `GET /api/ml/datasets/{id}` (+ `/explorer`, `/validation-report`), `POST …/{id}/archive` | **WRITE** `ml_datasets` (content-hashed, validated) | immutable dataset versions |
| **Training** (`#start-training-btn`, `#cancel-training-btn`, `#jobs-refresh-btn`) | `POST /api/ml/training-jobs`, `GET /api/ml/jobs`, `GET /api/ml/jobs/{id}`, `POST /api/ml/jobs/{id}/cancel` | background training; `ml_training_jobs` | model candidates |
| **Models / registry** (`#models-refresh-btn`, `#model-detail-close`, `#registry-action-confirm/cancel`, `#run-model-evaluation-btn`, `#evaluation-model-type`) | `GET /api/ml/models`, `GET /api/ml/models/{id}`, `POST …/{id}/readiness`, `…/shadow-approve`, `…/reject`, `…/promote`, `GET …/{id}/explanations` | **WRITE** model status; promotion is explicit and audited | lifecycle candidate → shadow → promoted |
| **Shadow** (`#shadow-evidence-btn`, `#stop-shadow-btn`, predictions paging) | `GET /api/ml/shadow/summary`, `GET /api/ml/shadow/evidence`, `GET /api/ml/predictions`, `POST /api/ml/shadow/stop` | evidence of ML vs rules on live data | agreement stats |
| **Drift** (`#run-drift-btn`) | `POST /api/ml/drift/run`, `GET /api/ml/drift/reports` | **WRITE** drift reports | report |
| **Retraining policy** | `GET/PUT /api/ml/retraining-policy/{model_type}` | **WRITE** policy | — |
| **Console / calls / audit** (`#refresh-console-btn`, `#calls-refresh-btn`, `#audit-prev/next`) | `GET /api/ml/calls`, `GET /api/ml/audit` | ML call log, audit trail | tables |
| **Platform** (`#platform-name`, `#platform-target`, `#platform-features`, `#platform-metrics`, `#platform-save`, `#platform-refresh`) | `GET/POST /api/ml/pipelines` (ML pipeline versions), `GET /api/ml/experiments`, `GET /api/ml/comparisons`, `POST /api/ml/experiments/{job_id}/retry` | **WRITE** pipeline version definitions | experiment tracking |
| Scoring helpers | `POST /api/ml/score/relational`, `POST /api/ml/rank/threat-review` | on-demand scoring (audited) | scores |

Also on the page: pickers that only parameterise the calls above — datasets (`#dataset-definition-select`, `#dataset-kind-select`, `#dataset-sampling-policy`, `#dataset-split-strategy`), labels (`#label-value`, `#labels-filter-review`, paging `#labels-prev` / `#labels-next`), training (`#training-model-type`, `#training-algorithm`, `#training-dataset-select`), shadow (`#shadow-model-select`, `#shadow-days-select`, paging `#predictions-prev` / `#predictions-next`), policy (`#policy-model-type`), platform (`#platform-pipeline`), the guided workflow (`#workflow-dataset`, `#workflow-job`, `#workflow-model`, `#workflow-summary-button`, `#mlops-runbook-previous`, `#mlops-runbook-next`, `#mlops-runbook-primary`), `#audit-next`, `#system-notes-btn` (operator notes), `#registry-action-cancel` and `#mlops-help-close` (close dialogs, no request).

## 19. `/admin/background-tasks` — Background tasks (admin)

**File** `admin/background-tasks.html` + `js/admin-background-tasks.js`. History and control of every scheduled or on-demand job (clustering, retention, relationship calculation, ML training, feature computation, …). Backend `backend/routes/task_history.py`, `retention.py`.

| Control | Triggers | Backend | Result |
|---|---|---|---|
| Page load / **Refresh** (`#refresh-btn`); filter `#task-type-filter`; paging | `GET /api/tasks/stats`, `GET /api/tasks/history?type&page`, `GET /api/tasks/alerts` (+ `GET /api/tasks/running`, `/upcoming`) | rows of `background_task_history` | table + KPIs + alerts for failed jobs |
| Row → **Task modal** (`#task-modal`) | `GET /api/tasks/{task_id}` | detail: status, timings, result, error | — |
| **Cancel** / **Retry** (in the modal) | `POST /api/tasks/{id}/cancel`, `POST /api/tasks/{id}/retry` | cooperative cancel; re-schedule | — |
| **Run retention** → confirm modal (`#retention-confirm-modal`, `#retention-confirm-go`) | `GET /api/admin/retention/status` → `POST /api/admin/retention/run` | `retention_run` **WRITE**: deletes data past the configured retention (detections, faces, images…) — see `Docs/18_BACKGROUND_TASKS.md`; audited | job scheduled; results appear as a task |

Jobs also notify the dashboard live (`background_task_notification` / `background_task_completed`).

Also on the page: `#clear-filters-btn`, `#enable-alerts-btn` (browser notification permission for failed-job alerts, client-side), `#task-modal-close` and `#retention-confirm-cancel` (close, no request).

## 20. LAF-AI chatbot integration (`https://armyeye-chatbot/`)

LAF-AI is a **separate service** (`~/vas-assistant`, its own runbook `DEPLOYMENT.md` there) that answers questions over the VAS database with a local model. VAS integrates it in four places; nothing else in VAS changed.

```
VAS sign-in ─▶ TRACKING ─▶ GET /api/sso/laf-ai/launch ─▶ 303 https://armyeye-chatbot/auth/vas?ticket=…
                                                             │  gate ─▶ POST /api/sso/laf-ai/consume (server-to-server, once)
                                                             ▼
                                       first visit: responsible-use notice (EN/AR) ─▶ LAF-AI chat
                          every question ─▶ POST /api/audit/chatbot ─▶ Admin → Audit Log (section 8)
                          VAS sign-out ─▶ chatbot token revoked + gate told ─▶ chatbot locked
```

| Piece | Where | What happens |
|---|---|---|
| **TRACKING** menu item / home card | `components/*navbar.html`, `home.html`, `backend/routes/auth.py` (`navbar_links`) | points at `GET /api/sso/laf-ai/launch`; emitted only for accounts with **Chatbot** (`users.can_use_chatbot`, set on the Users page) |
| **Launch** | `backend/routes/sso.py` | `require_chatbot_access`; refuses cross-site navigations; mints a **one-time ticket** (256-bit random, stored in Redis only as a SHA-256 hash, 60 s, single use — `backend/auth/laf_ai_sso.py`) bound to the user and the browser session's token id; **303** to the chatbot with the ticket. Redis down → the chatbot's own sign-in page. `LAF_AI_SSO_ENABLED=false` → the legacy `/tracking-people` page instead |
| **Consume** | `POST /api/sso/laf-ai/consume` `{ticket}` | internal only (refused when `X-Forwarded-For` is present — the public proxy always sets it; optional shared secret `LAF_AI_SSO_SECRET`); re-reads the account (active, Chatbot permission, no rotation pending) and returns identity + a fresh VAS token the gate keeps server-side; the token is linked to the browser session (`auth:laf-ai:child:<jti>`) |
| **Gate** (chatbot side) | `~/vas-assistant/gate/gate.cjs` | server-side session per user; on **first visit** a bilingual responsible-use notice must be acknowledged (recorded in the Audit Log); every request is re-validated against `GET /api/auth/me` (10 s cache) so revoking Chatbot or deactivating the account cuts the user off within seconds; password fallback sign-in remains for direct visitors |
| **Question audit** | `POST /api/audit/chatbot` `{session_id, question, source}` (`backend/routes/audit.py`) | internal only; identity from the caller's own token; writes `chatbot_audit_log` (`response = "[laf-ai] answered in the chatbot session"`); the SQL, results and answers stay in the chatbot's session logs |
| **Shared logout** | `POST /api/auth/logout` | revokes the browser token **and** the linked chatbot token, then `POST http://vas-assistant-gate:3081/_gate/revoke` so the chatbot session ends at once |
| **Database access of the chatbot** | `db/laf_ai_readonly.sql` | role `laf_ai_readonly`: SELECT on a whitelist, read-only transactions, 30 s statement timeout, 5 connections; cannot see `users`, credentials, settings, audit logs |
| **Settings** | `config.py` | `LAF_AI_SSO_ENABLED`, `LAF_AI_CHATBOT_URL`, `LAF_AI_GATE_URL`, `LAF_AI_SSO_TICKET_TTL_SECONDS`, `LAF_AI_SSO_SECRET[_FILE]` — all runtime-immutable, documented in `.env.example` |
| **Tests** | `tests/test_laf_ai_sso.py` (22) | tickets, single use, expiry, internal-only, logout propagation, audit attribution and spoof-resistance; run with `scripts/run_regression_isolated.sh` |

## 21. `/admin/ingest-credentials` — Camera ingest credentials (admin)

**File** `admin/ingest-credentials.html` + `js/admin-ingest-credentials.js`. Credentials the camera pipelines present on `POST /webhook` (the detection ingest endpoint, `backend/routes/webhook.py`, authenticated by `backend/security/webhook_auth.py`).

| Control | Triggers | Backend (`backend/routes/webhook_credentials.py`, prefix `/api/admin/webhook-credentials`) | Result |
|---|---|---|---|
| Page load | `GET /api/admin/webhook-credentials` | list (id, label, created, last used, revoked) — never the secret | table |
| **Issue** (`#issue-btn`) | `POST /api/admin/webhook-credentials` `{label}` | **WRITE** `webhook_credentials` (hash stored) + audit | the token is shown **once** (`#copy-token-btn`, `#dismiss-token-btn`) |
| **Revoke** → confirm (`#confirm-revoke-btn`, `#cancel-revoke-btn`) | `DELETE /api/admin/webhook-credentials/{credential_id}` | `revoke_credential` **WRITE** (revoked_at) + audit | the camera can no longer post |

## 22. `/admin/logs` — Error logs (admin)

**File** `admin/logs.html` + `js/admin-logs.js`. Backend `backend/routes/logs.py`.

| Control | Triggers | Backend | Result |
|---|---|---|---|
| Page load / **Refresh** (`#refresh-logs-btn`); `#level-filter`, `#page-size`, **Apply** / **Clear**; paging | `GET /api/logs/config`, `GET /api/logs?level&page&size`, `GET /api/logs/stats` | reads the application log store (rotated files / table, see config) | table + counts per level |
| (API only) cleanup | `POST /api/logs/cleanup` | deletes old log files | — |

The buttons are `#apply-filters-btn` and `#clear-filters-btn`.

## 23. `/admin/settings` — Runtime settings (admin)

**File** `admin/settings.html` + `js/admin-settings.js`. Edits the **runtime-mutable** settings (`backend/routes/settings.py`, prefix `/api/settings`; `backend/core/runtime_settings.py`). Security-critical keys (`SECURITY_CRITICAL_KEYS` in `backend/security/config_guard.py` — JWT, database, offline policy, LLM endpoints, the LAF-AI SSO keys, …) are **read-only** here and change only through the environment (`docker/.env`) and a redeploy.

| Control | Triggers | Backend | Result |
|---|---|---|---|
| Page load / **Refresh** (`#refresh-settings-btn`); category filter, **Reset filters** | `GET /api/settings`, `GET /api/settings/categories`, `GET /api/ml/capabilities` | current effective values (secrets redacted per `backend/security/redaction.py`) | grouped table |
| Row → **Edit** modal (`#setting-modal`, `#setting-form`) → **Save** | `PUT /api/settings/{setting_key}` `{value}` | `update_setting` **WRITE** `settings` + `settings_audit_log`, applies to the running process, broadcasts `config_changed` to dashboards | new value live |
| **Audit** panel | `GET /api/settings/audit/log` | who changed what, when | history |
| **Retention: status / dry run / run** (`#retention-status-btn`, `#retention-dryrun-btn`, `#retention-run-btn`) | `GET /api/admin/retention/status`, `POST /api/admin/retention/run?dry_run=true`, `POST /api/admin/retention/run` | dry run reports what would be deleted; run **WRITE** (deletes) + audit | — |

Also on the page: `#settings-reset-filters` (= **Reset filters**), `closeSettingModal` (closes the edit modal, no request).

## 24. `/admin/tutorial` — Admin tutorial (admin)

**File** `admin/tutorial.html` + `js/admin-tutorial.js`. Static learning guide with tabbed sections (`data-action="showSection"`) whose content and examples come from `GET /api/admin/tutorial` and `GET /api/admin/tutorial/examples` (`backend/routes/admin_tutorial.py`, admin, read-only). No database writes.

## 25. Shared: Add-person upload modal (enrollment)

**Component** `components/upload-modal.html`, loaded on demand by `js/upload-modal-loader.js` (home, dashboard, navbar "Add person"), driven by `js/upload-modal.js`. Admin only (`require_role(["admin"])` + `require_upload_csrf`). Everything goes through `backend/core/enrollment_service.py` — one enrollment path for the whole system.

| Step | Triggers | Backend (`backend/routes/upload.py`) | Result |
|---|---|---|---|
| Open (`data-action="openUploadModal"`) | `GET /api/dashboard/config` (accepted types, max size), `GET /api/auth/me` | — | form `#uploadPersonForm`: person name, photo, "this is a face crop" (`is_face_image`) |
| **Submit** (`#globalUploadSubmitBtn`, `data-action-submit="handleGlobalUpload"`) | `POST /api/upload-person` multipart `person_name, photo, is_face_image` | `upload_person`: validates the image, detects and embeds the face, then **decides**: existing name → adds the photo to that person (`POST /api/identities/{id}/images` path); new name → looks for similar faces (`find_similar_identities`, bands from `ENROLL_STRONG_MATCH_MIN`…): **none** → creates the identity + image + embedding (**WRITE** `identities`, `identity_images`, embeddings; audit); **strong / uncertain match or identical file (checksum)** → nothing is created yet: a **pending enrollment** row is parked and **202 `decision_required`** is returned with the candidates and an `upload_token` | success toast, or the **decision dialog** |
| Decision dialog → **Create new person** / **Add to existing** (pick a candidate) | `POST /api/enrollment/confirm` `{upload_token, decision, target_identity_id?}` | `confirm_enrollment` **WRITE**: applies the choice (new identity, or a new photo on the chosen one); the parked file is consumed; audit. Identities are **never merged automatically** | person enrolled |
| Decision dialog → **Cancel** (or closing the modal — `keepalive`) | `POST /api/enrollment/cancel` `{upload_token}` | `cancel_enrollment`: drops the parked upload and its file; nothing was created | — |
| Face-detection alert (`#face-alert-ok-btn`) | shown when no / several faces are found | — | fix the photo |

Reviewing parked uploads later is also possible through `backend/routes/enrollment_review.py`.

Also in the modal: `triggerFileInput` / `handleGlobalFileSelect` (choose or drop the photo; client validation), `closeUploadModal` / `closeUploadModalOnOutsideClick` (close; a parked upload is cancelled with `keepalive`), the decision buttons `enrollmentCreateNew` (`#enrollmentCreateNewBtn`) and `enrollmentCancel`, and `#close-face-alert-modal`.

## 26. Appendix — how this reference is kept accurate

- **API index:** `Docs/47_API_INDEX.md` is generated from the route files (method, path, handler, auth dependencies, whether the handler writes to the database, whether it audits, first docstring line). Regenerate after changing routes: `python3 scripts/generate_api_index.py > Docs/47_API_INDEX.md`.
- **Tables most pages touch:** `users`, `user_pipeline_access`, `pipelines`, `detections`, `faces`, `identities`, `identity_images`, `identity_appearances`, `identity_relationships`, `identity_merges`, `merge_suggestions`, `pending_enrollments`, `watchlists`, `watchlist_entries`, `watchlist_alerts`, `live_search_alerts`, `live_alert_triggers`, `live_alert_audit_log`, `threat_assessments`, `risk_signal_results`, `risk_model_versions`, `learned_thresholds`, `search_history`, `chatbot_audit_log`, `user_authorization_audit_log`, `settings`, `settings_audit_log`, `background_task_history`, `webhook_credentials`, `ml_*`. Relationships: `Docs/29_DATABASE_RELATIONSHIPS.md`.
- **Conventions used above:** "WRITE" = the handler changes the database; "audit" = it also records who did it (`user_authorization_audit_log`, `chatbot_audit_log`, `settings_audit_log`, or the structured security log via `auth_security.audit`).
