# Webhook Endpoint Debugging Guide

## ✅ Endpoint Status: WORKING

The webhook endpoint has been tested and is working correctly. Both endpoints are available:
- `/webhook/{pipeline_id}`
- `/api/webhook/{pipeline_id}`

## Test Results

```bash
# Test from inside container
curl -X POST http://localhost:8000/api/webhook/test123 \
  -H "Content-Type: application/json" \
  -d '{"images":["data:image/jpeg;base64,..."]}'

# Response: {"status":"queued","request_id":"3506e68e","pipeline_id":"test123","queued":1,"dropped":0}
```

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
# Replace with your actual server IP and pipeline ID
curl -X POST http://YOUR_SERVER_IP/api/webhook/YOUR_PIPELINE_ID \
  -H "Content-Type: application/json" \
  -d '{"images":["data:image/jpeg;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="]}'
```

### 5. Check Nginx Logs

If requests aren't reaching the backend:
```bash
docker logs face_recognition_nginx --tail 100 | Select-String -Pattern "webhook|POST|404|405|500"
```

### 6. Common Issues

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



