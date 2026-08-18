# Webhook Endpoint Debugging Guide

> **⚠️ The ingest webhook requires a credential.** Requests without one get
> **401**, not a queued frame. See
> [Docs/21 → Authentication](21_WEBHOOK_TROUBLESHOOTING.md#-authentication-required)
> for the full contract; the short version is below.

## Authentication

Send **one** of:

```
Authorization: Bearer YOUR_WEBHOOK_TOKEN     <-- use this for external systems
X-Webhook-Key: YOUR_WEBHOOK_TOKEN            <-- for fixed-header camera firmware
```

Never as a query parameter — it is rejected, because the request line is written
to three separate logs.

## Endpoints

- `POST /webhook/{pipeline_id}`
- `POST /api/webhook/{pipeline_id}`
- `GET  /webhook/test` — credential self-check, same auth

## Test Results

```bash
# Test from inside container
curl -i -X POST http://localhost:8000/api/webhook/test123 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_WEBHOOK_TOKEN" \
  -d '{"images":["data:image/jpeg;base64,..."]}'

# HTTP/1.1 202 Accepted
# {"status":"queued","job_id":"...","request_id":"3506e68e","pipeline_id":"test123","location_name":"...","queued":1,"dropped":0}
```

Success is **202**, not 200. A `200` with `{"status":"ok","message":"No images"}`
means the request authenticated fine but carried no image the extractor
recognized — check the payload shapes below.

## How to Debug Your Pipeline

### 1. Check if Requests are Reaching the Server

Monitor logs in real-time:
```bash
docker logs face_recognition_api -f | Select-String -Pattern "WEBHOOK"
```

### 2. Verify Your Pipeline Configuration

Make sure your pipeline is sending POST requests to:
- `http://your-server-ip/api/webhook/{your_pipeline_id}`
- OR `http://your-server-ip/webhook/{your_pipeline_id}`

…and that it sets `Authorization: Bearer YOUR_WEBHOOK_TOKEN` (or the configured
`X-Webhook-Key` header). Confirm the credential in isolation first:

```bash
curl -i -H "Authorization: Bearer YOUR_WEBHOOK_TOKEN" http://your-server-ip/webhook/test
# 200 = credential good;  401 = fix the credential before debugging anything else
```

### 3. Check Request Format

The endpoint accepts JSON with one of these formats:

**Format 1: Simple images array**
```json
{
  "images": ["data:image/jpeg;base64,..."]
}
```

**Format 2: With predictions**
```json
{
  "images": ["data:image/jpeg;base64,..."],
  "predictions": [
    {
      "class_name": "person",
      "bbox": [100, 100, 300, 400],
      "confidence": 0.95
    }
  ]
}
```

**Format 3: Single image**
```json
{
  "image": "data:image/jpeg;base64,..."
}
```

**Format 4: Frames array**
```json
{
  "frames": [
    {
      "image": "data:image/jpeg;base64,..."
    }
  ]
}
```

**Format 5: Cropped detections**
```json
{
  "cropped_detections": [
    {
      "cropped_image": "data:image/jpeg;base64,..."
    }
  ]
}
```

### 4. Test from Your Pipeline Server

Test if your pipeline can reach the server:
```bash
# Replace with your actual server IP, pipeline ID and token
curl -i -X POST http://YOUR_SERVER_IP/api/webhook/YOUR_PIPELINE_ID \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_WEBHOOK_TOKEN" \
  -d '{"images":["data:image/jpeg;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="]}'
```

### 5. Check Nginx Logs

If requests aren't reaching the backend:
```bash
docker logs face_recognition_nginx --tail 100 | Select-String -Pattern "webhook|POST|404|405|500"
```

### 6. Common Issues

**Issue: 401 Unauthorized (`WEBHOOK_AUTH_REQUIRED`)** — the most common cause of
"the webhook does nothing"
- No credential sent, malformed (`Authorization: <token>` without `Bearer `,
  or `Bearer` with no token), or a token that does not match
- The response is **identical** for all three, on purpose — it must not confirm
  a guessed token shape. Get the reason from the server:
  `docker logs face_recognition_api --tail 200 2>&1 | grep "ingest credential"`
  → `missing` = nothing usable arrived, `invalid` = arrived but did not match
- Check `WWW-Authenticate: Bearer, WebhookKey` in the response for the accepted
  schemes

**Issue: 202 vs 200 confusion**
- `202` is success (queued). `200` with `{"status":"ok","message":"No images"}`
  means authentication succeeded but no image was recognized in the payload

**Issue: 413 Payload Too Large**
- Body exceeded `WEBHOOK_MAX_BODY_MB` (default 25)

**Issue: 503 with `Retry-After`**
- The ingest queue is full. Honour `Retry-After` and back off

**Issue: 404 Not Found**
- Check if you're using the correct URL path (`/api/webhook/` or `/webhook/`)
- Verify the pipeline_id is correct

**Issue: 405 Method Not Allowed**
- Ensure you're using POST method, not GET

**Issue: Connection Refused**
- Check if the server is accessible from your pipeline
- Verify firewall/network settings
- Check if nginx is running: `docker ps | grep nginx`

**Issue: Timeout**
- Check nginx timeout settings
- Verify the server has enough resources

**Issue: No Logs Appearing**
- The request might not be reaching the server at all
- Check network connectivity
- Verify DNS resolution if using domain name

### 7. Enable Detailed Logging

The webhook endpoint logs:
- `[WEBHOOK] Request received at /api/webhook/{pipeline_id}` - When request arrives
- `[WEBHOOK] 📥 Incoming request {request_id} for pipeline: {pipeline_id}` - Processing started
- `[WEBHOOK] ✅ Validated pipeline ID: {pipeline_id}` - Validation passed
- `[WEBHOOK] Extracted {count} images` - Images extracted
- `[WEBHOOK] ❌ Error...` - Any errors
- `[WEBHOOK] ingest credential missing|invalid - rejected path=... client=...` -
  a 401. `client=` is a pseudonymized fingerprint, not a raw IP, and the
  credential itself is **never** logged

Metric to watch (Prometheus): `fr_webhook_auth_total{result=...}` with
`result` in `ok | missing | invalid | would_reject | unenforced`.
`would_reject` only appears in `log_only` mode — it counts requests that were
accepted but would have been rejected under `enforce`. Drive it to zero before
switching a fleet over.

### 8. Verify Container is Running

```bash
docker ps | grep face_recognition
```

Should show:
- `face_recognition_api` - Status: Up (healthy)
- `face_recognition_nginx` - Status: Up (healthy)

## Next Steps

1. **Check your pipeline configuration** - Verify it's sending to the correct URL
2. **Monitor logs** - Watch for incoming requests
3. **Test connectivity** - Ensure your pipeline can reach the server
4. **Check network** - Verify firewall rules allow connections

If you're still not receiving requests, please provide:
- Your pipeline configuration (URL it's using)
- Any error messages from your pipeline
- Network connectivity test results



