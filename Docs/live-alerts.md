# Live alerts

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/live-alerts` at `https://face-detector.internal`. **Access:** Administrator or active user with assigned pipelines; alert ownership/scope checked by the API.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Manage person-specific detection rules, inspect triggers, acknowledge events, check delivery readiness, and receive browser notifications.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Reconnect | Reconnect the page WebSocket after a connection problem. |
| Enable Alert Sound | Opt in to browser audio; toggle again to mute. |
| SEARCH / create instructions | Open Search or show how to create an alert from an identity workflow. |
| Severity selector on a card | Immediately save Info, Warning or Critical for that alert. |
| Pause / Resume | Persist the rule state, stopping or restarting matching notifications. |
| Triggers | Open the rule's trigger history with snapshots and acknowledgement state. |
| Trigger Previous / Next | Page through the stored events. |
| Trigger Acknowledge / popup Acknowledge | Persist acknowledgement of that trigger. |
| Select trigger checkboxes + Acknowledge selected | Acknowledge the selected events. |
| Acknowledge all | Submit the bulk acknowledgement for the alert after the UI's confirmation/options. |
| Health | Display readiness of the alert and its configured channels. |
| Test (administrator) | Schedule a channel test; it can send configured notifications. Read the results rather than assuming all channels work. |
| Delete (trash) | Confirm and delete the alert rule; this is not merely hiding the card. |
| Popup View triggers | Open the corresponding alert's trigger history. |
| Trigger snapshot | Open the stored image in a new tab. |
| Close / popup dismiss | Close that view without acknowledging an event unless Acknowledge was clicked. |

## Demo

1. From a test identity, create a live alert for one test camera with dashboard notifications enabled.
2. Open Live alerts and inspect the new rule's Health.
3. Arrange a matching test detection and open Triggers.
4. Acknowledge one trigger and reload its history; expect its acknowledgement state to persist.
5. Pause the test alert when finished.

## Behavior to know

Empty camera selection is displayed as All, within server authorization. Email/SMS need working configured channels reachable from the offline network. Test can produce actual notifications. Alert sound remains subject to browser autoplay rules.

## Source

[frontend/admin/live-alerts.html](../frontend/admin/live-alerts.html), [frontend/js/admin-live-alerts.js](../frontend/js/admin-live-alerts.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
