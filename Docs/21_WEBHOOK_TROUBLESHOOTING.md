# Webhook Endpoint Troubleshooting Guide

> **⚠️ THE INGEST WEBHOOK REQUIRES A CREDENTIAL.**
>
> Earlier revisions of this guide showed `curl` with no authentication, because
> they predate ingest auth. An unauthenticated request now gets **401**, not a
> queued frame. If you are debugging "the webhook does nothing", check the
> credential **first** — see [Authentication](#-authentication-required) below.

## 🔐 Authentication (required)

Every ingest request must carry a credential in **one** of two forms:

| Form | Header | Value |
|---|---|---|
| Bearer (use this for external systems) | `Authorization` | `Bearer YOUR_WEBHOOK_TOKEN` |
| Custom header (camera firmware) | `X-Webhook-Key` | `YOUR_WEBHOOK_TOKEN` |

The custom header's *name* is configurable with `WEBHOOK_AUTH_HEADER` if the
sender can only emit one fixed header. `Authorization: Bearer` is always
accepted regardless of that setting.

**Never send the credential as a query parameter.** It is rejected, deliberately:
nginx, gunicorn and the application access log all record the request line, so a
credential in the URL is published to three logs on the first request.

**To give an external system a token**, mint one at **Admin → Ingest
Credentials** (`/admin/ingest-credentials`). It is shown once and cannot be
recovered — if it is lost, revoke it and issue another. Deleting the credential
revokes it; that takes effect on every worker within
`WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS` (default 30s).

The environment credential — `WEBHOOK_AUTH_TOKEN`, or `WEBHOOK_API_KEYS` for a
comma-separated set — still works and is the break-glass path. Both are
restart-only: they are security-critical and cannot be changed from the Settings
page. Production requires one, so ingest keeps working even if the credentials
table is unreachable.

Note that a `401` looks identical for a missing, malformed, wrong, or revoked
credential. "It used to work" is not something the response will confirm — check
whether the credential still exists in the admin page.

### Verify your credential before debugging anything else

```bash
curl -i -H "Authorization: Bearer YOUR_WEBHOOK_TOKEN" \
  http://YOUR_SERVER_IP/webhook/test
```

* **200** — the token is valid. Any remaining problem is payload or connectivity.
* **401** — the token is wrong, missing, or malformed. Nothing else you change
  will help until this returns 200.

## ✅ Server Status

If the self-check above returns 200, the endpoint is reachable and your
credential works — the remaining causes are the pipeline not sending, sending to
the wrong URL, or sending a payload shape the extractor does not recognize.

## 🔍 Server Configuration

- **Public URL**: `http://localhost` (port 80) - via Nginx
- **Direct Backend**: `http://localhost:8000` (internal, not exposed)
- **Webhook Endpoints Available**:
  - `http://localhost/webhook/{pipeline_id}`
  - `http://localhost/api/webhook/{pipeline_id}`

## 📋 What Your Pipeline Should Use

Your pipeline MUST send POST requests to one of these URLs:

```
http://YOUR_SERVER_IP/webhook/YOUR_PIPELINE_ID
```

OR

```
http://YOUR_SERVER_IP/api/webhook/YOUR_PIPELINE_ID
```

**Replace:**
- `YOUR_SERVER_IP` = The IP address or hostname where your server is running
- `YOUR_PIPELINE_ID` = Your actual pipeline identifier

## 🔎 Diagnostic Steps

### Step 1: Check if Pipeline is Running

Verify your pipeline process is actually running and sending requests.

### Step 2: Check Pipeline Configuration

Look at your pipeline configuration file and verify:
- ✅ Webhook URL is set correctly
- ✅ Using POST method (not GET)
- ✅ Content-Type header is `application/json`
- ✅ **`Authorization: Bearer YOUR_WEBHOOK_TOKEN` is set** (or `X-Webhook-Key`)
- ✅ Pipeline ID matches what you expect

### Step 3: Test Connectivity from Pipeline Server

**From the machine running your pipeline**, test if it can reach the server:

```bash
# Replace YOUR_SERVER_IP and YOUR_WEBHOOK_TOKEN with real values
curl -i -X POST http://YOUR_SERVER_IP/webhook/test123 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_WEBHOOK_TOKEN" \
  -d '{"images":["data:image/jpeg;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="]}'
```

**Expected Response — `202 Accepted`** (not 200; the frame is queued, not yet
processed):
```json
{"status":"queued","job_id":"...","request_id":"...","pipeline_id":"test123","location_name":"...","queued":1,"dropped":0}
```

Other responses you may legitimately get:

| Status | Body | Meaning |
|---|---|---|
| `202` | `{"status":"queued",...}` | accepted — the normal success |
| `200` | `{"status":"ok","message":"No images"}` | the payload carried no image the extractor recognized |
| `200` | duplicate | same frame within `WEBHOOK_DEDUP_TTL_SECONDS` |
| `401` | `WEBHOOK_AUTH_REQUIRED` | credential missing, malformed or wrong |
| `413` | — | body exceeded `WEBHOOK_MAX_BODY_MB` |
| `503` + `Retry-After` | — | ingest queue full; back off and retry |

### Step 4: Monitor Logs in Real-Time

Open a terminal and run:

```powershell
docker logs face_recognition_api -f | Select-String -Pattern "REQUEST|WEBHOOK"
```

Then trigger your pipeline. You should see:
```
[REQUEST] POST /webhook/YOUR_PIPELINE_ID from ...
[WEBHOOK-DEBUG] Incoming webhook request: ...
```

**If you see NO logs**, the requests are NOT reaching the server.

### Step 5: Check Nginx Logs

If requests reach Nginx but not the backend:

```powershell
docker logs face_recognition_nginx -f | Select-String -Pattern "webhook|POST"
```

### Step 6: Check Network/Firewall

- Is your pipeline server on the same network?
- Are there firewall rules blocking port 80?
- Can the pipeline server ping/reach your server?

## 🚨 Common Issues

### Issue 0: `401 Unauthorized` — `WEBHOOK_AUTH_REQUIRED`

**Symptom**: every request is rejected; nothing is queued.

```json
{"detail":{"error":{"code":"WEBHOOK_AUTH_REQUIRED","message":"A valid ingest key is required."}}}
```

Three causes, all producing this identical response:

1. **No credential sent** — neither `Authorization` nor the configured header.
2. **Malformed** — `Authorization: <token>` without the `Bearer ` scheme,
   `Bearer` with no token, or the wrong scheme (`Basic`, `Token`).
   Note `bearer` in lowercase and extra spaces after `Bearer` are **valid**.
3. **Wrong token** — sent correctly, but does not match any configured key.

**The response deliberately does not tell you which.** Distinguishing them would
let anyone probing the endpoint confirm a guessed token shape without knowing the
value. To find out which it is, look at the server side:

```bash
# the reason, per rejected request
docker logs face_recognition_api --tail 200 2>&1 | grep "ingest credential"
# -> [WEBHOOK] ingest credential missing - rejected path=/webhook/... client=...
# -> [WEBHOOK] ingest credential invalid - rejected path=/webhook/... client=...

# the aggregate counter
curl -s http://localhost/metrics | grep fr_webhook_auth_total
# result="missing" | "invalid" | "ok" | "would_reject" | "unenforced"
```

`missing` means nothing usable arrived (cause 1 or 2). `invalid` means a
credential arrived and did not match (cause 3).

The `WWW-Authenticate: Bearer, WebhookKey` response header lists the schemes the
server accepts.

### Issue 1: Pipeline Sending to Wrong URL
**Symptom**: No logs appear when pipeline runs
**Solution**: Check pipeline config, verify URL is `http://SERVER_IP/webhook/PIPELINE_ID`

### Issue 2: Pipeline Not Running
**Symptom**: No requests at all
**Solution**: Check if pipeline process is active, check pipeline logs

### Issue 3: Network/Firewall Blocking
**Symptom**: Connection timeout or refused
**Solution**: Check firewall rules, network connectivity

### Issue 4: Wrong HTTP Method
**Symptom**: 405 Method Not Allowed
**Solution**: Ensure pipeline uses POST, not GET

### Issue 5: Wrong Content-Type
**Symptom**: 400 Bad Request or empty payload
**Solution**: Ensure `Content-Type: application/json` header is set

## 📊 Current Server Status

- **Nginx**: ✅ Running on port 80
- **Backend API**: ✅ Running on port 8000 (internal)
- **Webhook Endpoint**: ✅ Working (tested successfully)
- **Database**: ✅ Connected
- **Queue Workers**: ✅ Running (15 workers)

## 🎯 Next Steps

1. **Check your pipeline configuration** - What URL is it configured to use?
2. **Verify pipeline is running** - Is the process active?
3. **Test connectivity** - Can your pipeline server reach this server?
4. **Check pipeline logs** - What errors (if any) appear in pipeline logs?
5. **Monitor server logs** - Run the monitoring command above and trigger pipeline

## 📝 What We Need From You

To help debug further, please provide:

1. **Pipeline Configuration**: What URL is your pipeline configured to use?
2. **Pipeline Logs**: Any errors or messages from your pipeline?
3. **Network Test**: Can you reach the server from your pipeline machine?
4. **Pipeline Status**: Is your pipeline actually running and processing images?

The webhook endpoint is working perfectly - the issue is that requests from your pipeline are not reaching the server. This is typically a configuration or network issue on the pipeline side.



