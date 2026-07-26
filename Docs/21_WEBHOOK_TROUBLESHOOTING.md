# Webhook Endpoint Troubleshooting Guide

## ✅ Server Status: RUNNING AND WORKING

Your webhook endpoint is **working correctly**. Test results show:
- Endpoint receives requests: ✅
- Payload parsing works: ✅  
- Queue processing works: ✅

**The issue is that your pipeline is NOT sending requests to the server.**

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
- ✅ Pipeline ID matches what you expect

### Step 3: Test Connectivity from Pipeline Server

**From the machine running your pipeline**, test if it can reach the server:

```bash
# Replace YOUR_SERVER_IP with actual server IP
curl -X POST http://YOUR_SERVER_IP/webhook/test123 \
  -H "Content-Type: application/json" \
  -d '{"images":["data:image/jpeg;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="]}'
```

**Expected Response:**
```json
{"status":"queued","request_id":"...","pipeline_id":"test123","queued":1,"dropped":0}
```

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



