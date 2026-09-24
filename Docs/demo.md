# End-to-end operator demo

[All guides](README.md) · [Offline move](offline-deployment.md)

This is a reproducible walkthrough with expected results, **not a claim that a
browser test was executed**. Use an isolated test deployment for account, identity,
watchlist and alert writes. Prepare an administrator, a consenting test subject's
photo, a test camera pipeline and local map assets. If any prerequisite is missing,
record that stage as blocked rather than inventing sample output.

| Step | Do this | Expected result |
|---|---|---|
| 1 | [Sign in](signin.md); rotate password if prompted | Authorized landing page |
| 2 | [Home](home.md) → Refresh → System status | Current component states and pipeline activity |
| 3 | [Known faces](known.md) → Add person; enroll Demo Person | New record or an explicit duplicate-review decision |
| 4 | [Pipelines](pipelines.md) → Coordinates → Save Location for the test camera | Saved camera coordinates |
| 5 | Send an approved test frame using the existing camera integration | Detection appears in [Dashboard](dashboard.md) |
| 6 | Click the face image, then open its profile | Preview and [Identity](identity.md) evidence agree on person/camera/time |
| 7 | [Search](search.md) → choose test photo → Quality Check → Search | Actual matching evidence or an honest no-match result |
| 8 | [Watchlists](watchlists.md) → create Demo Watchlist → View → add test identity | Membership saved |
| 9 | Arrange another matching test event | Watchlist activity and Dashboard persistent alert inbox update |
| 10 | Dashboard → Review alerts → Acknowledge → Refresh alerts | Displayed group acknowledged on server; newer events remain eligible |
| 11 | Identity → Create Live Alert; inspect [Live alerts](live-alerts.md) → Health | New rule with explicit channel readiness |
| 12 | Trigger once, open Triggers, acknowledge the test event | Persistent acknowledged trigger |
| 13 | [Intelligence](intelligence.md) → Track Movement → Map | Available camera evidence on a local basemap |
| 14 | [Background tasks](background-tasks.md) → retention Dry Test | Prospective cleanup report without actual cleanup |
| 15 | [ML Operations](ml-ops.md) → Guide me → Overview | Readiness and gates; no training merely from opening the tour |
| 16 | Open Tracking and ask a stored-data question; inspect [Audit](audit.md) | Local answer/error and available question audit |
| 17 | Pause the demo live alert and deactivate the demo watchlist | Demonstration monitoring no longer active |

For a read-only tour on production, use existing approved records and skip
creation, enrollment, acknowledgements and all other saves. Merge, deletion,
retention execution and model activation are deliberately outside this basic
demo. Their guides explain previews and confirmations.

Record the actual date, operator, test identity/camera IDs and pass/fail/blocked
outcome for each step. Do not use example names or imagined screenshots as
production acceptance evidence. After the network move, repeat key steps from
another intranet PC and again after an offline reboot.
