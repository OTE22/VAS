# 🚀 Quick Start: Using Advanced SNA Enhancements

## ⚡ 5-Minute Setup

### 1. Enable Features (Add to `.env`)

```env
AUTO_THRESHOLD_LEARNING_ENABLED=true
TRAJECTORY_PREDICTION_ENABLED=true
ACTIVITY_CORRELATION_ENABLED=true
```

### 2. Restart Server

```bash
# Restart your FastAPI server
# The enhancements are now active!
```

### 3. Learn Thresholds (One-Time)

```bash
# Using curl
curl -X POST "http://localhost:8000/api/intelligence/thresholds/learn" \
  -H "Cookie: session=YOUR_SESSION"

# Or in browser console
fetch('/api/intelligence/thresholds/learn', {
    method: 'POST',
    credentials: 'include'
}).then(r => r.json()).then(console.log);
```

**Done!** Your existing endpoints now use enhancements automatically.

---

## ✅ What Works Automatically

### Your Existing Endpoints Are Enhanced:

1. **`/api/security/network`** - Social Network Analysis
   - ✅ Uses learned thresholds
   - ✅ Activity correlation included
   - ✅ Better relationship detection

2. **`/api/identities/{id}/related`** - Related Identities
   - ✅ Cross-camera relationships
   - ✅ Correlation-boosted strength
   - ✅ More accurate results

**No code changes needed!** Just works better.

---

## 🎯 New API Endpoints (Optional)

### 1. Predict Trajectory
```javascript
GET /api/intelligence/trajectory/predict?identity_id=UUID&current_camera=camera_1
```

### 2. Calculate Correlation
```javascript
GET /api/intelligence/correlation/calculate?identity_a=UUID1&identity_b=UUID2
```

### 3. Learn Thresholds
```javascript
POST /api/intelligence/thresholds/learn
```

---

## 📖 Full Documentation

See `Docs/62_HOW_TO_USE_ENHANCEMENTS.md` for:
- Detailed setup instructions
- UI integration examples
- Troubleshooting guide
- Best practices

---

## ✅ Checklist

- [ ] Add config to `.env`
- [ ] Restart server
- [ ] Run threshold learning
- [ ] Test existing endpoints (they work better now!)
- [ ] (Optional) Add UI controls

**That's it!** 🎉

