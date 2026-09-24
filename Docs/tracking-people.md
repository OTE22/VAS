# Tracking people — built-in assistant

[All guides](README.md) · [Shared navigation and dialogs](shared-controls.md)

**Open:** `/tracking-people` at `https://face-detector.internal`. **Access:** Account with chatbot permission.
**Source review:** 2026-09-24, current working tree. Demo below is a walkthrough with expected results, not a recorded execution.

## Features

The built-in data assistant supports questions about stored VAS data and conversation history. The main Tracking navigation normally launches the separate LAF-AI chatbot through `/api/sso/laf-ai/launch`.

## Buttons and controls

| Control | What clicking or changing it does |
|---|---|
| New chat (sidebar or top) | Create/start a new conversation. |
| Open sidebar / Close sidebar / backdrop | Show or hide conversation history. |
| Question suggestions | Insert the selected example prompt for a stored-data question. |
| Message box + Send message | Submit a question; display streamed progress/answer where supported. |
| Stop/cancel (while offered) | Request cancellation of the active query. |
| Conversation entry | Load its messages and branches. |
| Conversation rename / pin / archive / delete / branch controls (when offered) | Persist the corresponding conversation operation; delete removes the conversation from normal history. |
| Feedback controls | Save feedback on an answer where offered. |
| Export PDF / Word (when offered) | Request an export of the supported answer/report content. |
| Help | Open instructions for asking questions. |

## Demo

1. Use Tracking from the navbar and note whether it opens LAF-AI or this built-in page.
2. For the built-in demo, open `/tracking-people` directly with a permitted account.
3. Start New chat and ask “How many detections are stored for today?”
4. Read the response and any query/error evidence; verify the date scope against available data.
5. Reopen the conversation from the sidebar.

## Behavior to know

The separate chatbot is deployed outside this repository. Its complete page controls are not verified by this guide. Offline answers require installed local models and reachable local services; internet search and cloud APIs are unavailable. Questions may be recorded in audit history.

## Source

[frontend/tracking-people.html](../frontend/tracking-people.html), [frontend/js/tracking.js](../frontend/js/tracking.js), [frontend/js/conversations.js](../frontend/js/conversations.js). Page access: [dashboard routes](../backend/routes/dashboard.py).
