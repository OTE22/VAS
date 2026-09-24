# Security intelligence

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/admin/security-intelligence` at `https://face-detector.internal`. **Access:** Administrator.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

Social-network analysis, suspicious-pattern review, anomaly detection, threat assessment, learned thresholds, trajectory/correlation analysis and maps.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| Social Network / Suspicious Patterns / Anomalies / Threat Assessment / Advanced Features / Map View | Switch analysis sections. |
| Identity/camera/time/threshold inputs | Scope the next analysis; selecting a value alone is not the analysis result. |
| Analyze Network | Build the selected identities' relationship network. |
| Detect Patterns | Run pattern analysis for the selected camera/time scope; click a returned pattern to inspect detail. |
| Detect Anomalies | Analyze the selected identity's behavior. |
| Assess Threat | Request an assessment and inspect its evidence, limitations and decision provenance. |
| Learn Thresholds | Learn/store thresholds for the selected pipelines. |
| Predict Trajectory | Estimate likely movement from the selected identity/current camera and available history. |
| Calculate Correlation | Compare the selected identity pair's temporal relationship. |
| Learn All Camera Thresholds | Request learning across cameras; this changes derived configuration/data. |
| Check Feature Status | Read readiness of the advanced features. |
| View Help | Show explanatory guidance. |
| Load Map / map style | Load the selected identity's spatial evidence with the chosen offline basemap. |
| Close pattern details | Dismiss the detail modal. |
| Decision engine badge | Open ML Operations; consult per-result provenance for historical results. |

## Demo

1. Choose an approved test identity in Anomalies and click Detect Anomalies.
2. Read the available history and any insufficient-data message.
3. Open Threat Assessment for the same identity and inspect evidence/provenance.
4. Open Map View, choose the identity and Load Map.
5. Leave threshold-learning buttons for an intentional configuration experiment in test data.

## Behavior to know

Scores, correlations and predicted trajectories require human review and can be unreliable with sparse or biased observations. Empty or gated output is not proof of safety or wrongdoing. Local map archives are required offline.

## Source

[frontend/admin/security-intelligence.html](../frontend/admin/security-intelligence.html), [frontend/js/admin-security-intelligence.js](../frontend/js/admin-security-intelligence.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
