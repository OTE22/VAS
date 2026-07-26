# Dashboard Retention Period Fix

## Problem

After refreshing the dashboard, known faces (and unknown faces if enabled) were disappearing even though they should stay visible for the configured retention period (default: 3 hours).

## Root Cause

The backend WebSocket endpoint was only loading detections from the last `DASHBOARD_FACE_DISPLAY_HOURS` (default: 3 hours) based on `Detection.timestamp`. If a face was detected more than 3 hours ago, it wouldn't be included in the initial data, even if the identity's `last_seen_at` was within the retention period.

## Solution

Updated `backend/routes/websocket.py` to:

1. **Load recent detections** (last 3 hours) - as before
2. **Also load KNOWN identities** that have been seen within the retention period (based on `Identity.last_seen_at`)
3. **Also load UNKNOWN identities** (if `SHOW_UNKNOWN_FACES_ON_DASHBOARD=True`) that have been seen within the retention period
4. **Get their most recent detections** even if older than 3 hours
5. **Apply user pipeline filters** to all detections

## How It Works

### For KNOWN Faces:
```python
# Step 1: Load detections from last 3 hours
detections = load_detections_since(cutoff_time)

# Step 2: Find KNOWN identities seen within retention period
known_identities = load_identities_where(
    type=KNOWN,
    status=ACTIVE,
    last_seen_at >= cutoff_time
)

# Step 3: Get their most recent detections (even if older)
additional_detections = get_most_recent_detections_for(known_identities)

# Step 4: Combine
all_detections = detections + additional_detections
```

### For UNKNOWN Faces (if enabled):
```python
if SHOW_UNKNOWN_FACES_ON_DASHBOARD:
    # Also load UNKNOWN identities seen within retention period
    unknown_identities = load_identities_where(
        type=UNKNOWN,
        status=ACTIVE,
        last_seen_at >= cutoff_time
    )
    
    # Get their most recent detections
    additional_detections.extend(get_most_recent_detections_for(unknown_identities))
```

## Configuration

- **`DASHBOARD_FACE_DISPLAY_HOURS`** (default: 3): Retention period in hours
- **`SHOW_UNKNOWN_FACES_ON_DASHBOARD`** (default: False): Whether to show unknown faces on dashboard

## Result

✅ **Known faces** stay visible for the full retention period (3 hours) based on `Identity.last_seen_at`  
✅ **Unknown faces** (if enabled) also stay visible for the full retention period  
✅ Faces remain visible after dashboard refresh  
✅ Retention period is properly respected  

## Example

If `DASHBOARD_FACE_DISPLAY_HOURS=3`:
- Face detected 4 hours ago, but identity `last_seen_at` is 2 hours ago → **Will appear on dashboard**
- Face detected 2 hours ago → **Will appear on dashboard**
- Face detected 4 hours ago, and identity `last_seen_at` is 4 hours ago → **Will NOT appear** (outside retention period)

