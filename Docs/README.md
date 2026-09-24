# VAS operator documentation

Start with [Offline deployment — move this same server](offline-deployment.md).

These guides replace the old numbered/topic Markdown collection. Reviewed against
the working-tree HTML, JavaScript and deployment scripts on **2026-09-24**.
Each page guide uses the HTML filename, explains its controls and provides a
step-by-step demo with expected results. Demos are **not recorded browser tests**.
Use approved test identities; data-changing demos should run in a test environment.

## Web pages

| Guide | URL | Access |
|---|---|---|
| [Sign in](signin.md) | `/signin` | Anyone; valid credentials required to continue |
| [Change password](change-password.md) | `/change-password` | Signed-in account, including one awaiting password rotation |
| [Home](home.md) | `/home` | Administrator |
| [Dashboard — live feeds](dashboard.md) | `/dashboard` | Signed-in active user; data limited to permitted pipelines |
| [Known faces](known.md) | `/admin/known` | Administrator |
| [Unknown faces](unknown.md) | `/admin/unknown` | Administrator or active user with assigned pipelines; individual actions remain permission-controlled |
| [Identity profile](identity.md) | `/admin/identity/{identity_id}` | Administrator or active user with assigned pipelines; the identity API enforces record access |
| [Search](search.md) | `/admin/search` | Administrator |
| [Search history](search-history.md) | `/admin/search-history` | Administrator |
| [Watchlists](watchlists.md) | `/admin/watchlists` | Administrator |
| [Live alerts](live-alerts.md) | `/admin/live-alerts` | Administrator or active user with assigned pipelines; alert ownership/scope checked by the API |
| [Pipelines](pipelines.md) | `/admin/pipelines` | Administrator |
| [Users](users.md) | `/admin/users` | Administrator |
| [Audit log](audit.md) | `/admin/audit` | Administrator |
| [Ingest credentials](ingest-credentials.md) | `/admin/ingest-credentials` | Administrator |
| [Logs](logs.md) | `/admin/logs` | Administrator |
| [Background tasks](background-tasks.md) | `/admin/background-tasks` | Administrator |
| [Settings](settings.md) | `/admin/settings` | Administrator |
| [Intelligence](intelligence.md) | `/admin/intelligence` | Administrator |
| [Security intelligence](security-intelligence.md) | `/admin/security-intelligence` | Administrator |
| [ML similarity model](ml-model.md) | `/admin/ml-model` | Administrator |
| [ML Operations](ml-ops.md) | `/admin/ml-ops` | Administrator |
| [Tutorial](tutorial.md) | `/admin/tutorial` | Administrator |
| [Tracking people — built-in assistant](tracking-people.md) | `/tracking-people` | Account with chatbot permission |

## Shared workflows and operations

- [Shared navigation, enrollment, dialogs and map controls](shared-controls.md)
- [Offline deployment](offline-deployment.md)
- [Backup and recovery](backup-and-recovery.md)
- [Offline maps](maps.md)
- [End-to-end demo](demo.md)

The browser's title can differ from its filename: for example, execution monitoring
lives at `background-tasks.html`, so its guide is `background-tasks.md`.
Identity profiles require `/admin/identity/{identity_id}`. Components under
`frontend/components/` are shared UI, not independent pages. The internal
`frontend/maps/_verify_map.html` diagnostic is covered in the maps guide.
`/docs` and `/redoc` are generated API explorers and are disabled by the production
Compose configuration. LAF-AI and VMS are separate applications; their full page
inventories are outside this repository.

JSON inventories, OpenAPI snapshots and the database PDF have been retained as
historical/reference artifacts. They are not current operational instructions or
proof of today's runtime state. Old Markdown is recoverable from Git history;
untracked review drafts are not removed by this refresh.

To keep a guide current, compare the page HTML **and** its JavaScript-generated
controls, then verify permissions and mutations in the backend routes. Update the
demo whenever button labels or behavior change. Shared navigation and Cancel/Close
behavior are documented once and linked from every page.
