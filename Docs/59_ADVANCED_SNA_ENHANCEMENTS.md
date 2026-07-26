# Advanced Social Network Analysis Enhancements

## Overview

This document explains four advanced enhancements for cross-camera social network analysis:
1. Activity Correlation Analysis (xCCA)
2. Trajectory Prediction
3. Multi-Feature Person Re-ID
4. Automatic Threshold Learning

## 1. Activity Correlation Analysis (xCCA)

### What It Does

**Activity Correlation Analysis** detects **causal relationships** between activities at different cameras. It goes beyond simple co-occurrence to identify patterns like:

- "When Person A leaves Camera 1, Person B appears at Camera 2 shortly after"
- "Person A and Person B always appear in sequence across cameras"
- "Coordinated movements" (people moving together through camera network)

### How It Helps

1. **Higher Confidence Relationships**
   - Distinguishes between coincidental co-appearances and actual relationships
   - Example: Two people appearing at different cameras at the same time by chance vs. coordinated movement

2. **Detect Coordinated Activities**
   - Identifies groups moving together through camera network
   - Useful for security: detecting coordinated suspicious behavior

3. **Better Relationship Quality**
   - Reduces false positives from coincidental appearances
   - Increases confidence in genuine relationships

### Implementation Approach

```python
# Simplified xCCA approach:
# 1. Track sequences: Person A at Camera 1 → Person B at Camera 2
# 2. Calculate correlation: How often does this sequence occur?
# 3. Score relationships: Higher score = stronger causal relationship

def calculate_activity_correlation(identity_a, identity_b, camera_network):
    """
    Calculate correlation between Person A and Person B's activities.
    
    Returns:
    - correlation_score: 0.0 to 1.0 (1.0 = perfect correlation)
    - sequence_patterns: List of detected sequences
    """
    sequences = []
    
    # Find all sequences: A appears at Camera X → B appears at Camera Y
    for appearance_a in identity_a.appearances:
        nearby_cameras = get_nearby_cameras(appearance_a.camera, max_distance=500m)
        
        for appearance_b in identity_b.appearances:
            if appearance_b.camera in nearby_cameras:
                time_diff = appearance_b.time - appearance_a.time
                if 0 < time_diff < 10min:  # B appears after A
                    sequences.append({
                        'from_camera': appearance_a.camera,
                        'to_camera': appearance_b.camera,
                        'time_diff': time_diff
                    })
    
    # Calculate correlation
    if len(sequences) > 0:
        correlation_score = len(sequences) / max(len(identity_a.appearances), len(identity_b.appearances))
        return correlation_score, sequences
    
    return 0.0, []
```

### Feasibility: ✅ **HIGH** - Can implement now

**Why:**
- We have all required data (appearances, timestamps, camera locations)
- Algorithm is straightforward (sequence detection + correlation)
- No external dependencies needed

**Implementation Time:** 2-3 days

---

## 2. Trajectory Prediction

### What It Does

**Trajectory Prediction** predicts where a person will appear next based on their historical movement patterns. It learns common paths through the camera network.

### How It Helps

1. **Proactive Relationship Detection**
   - Predict: "Person A will likely appear at Camera 3 in 5 minutes"
   - Check: "Is Person B already there or likely to be there?"
   - Result: Detect relationships before they happen

2. **Better Cross-Camera Matching**
   - If Person A is predicted at Camera 2, prioritize matching at Camera 2
   - Reduces false matches at wrong cameras

3. **Anomaly Detection**
   - If person takes unusual path, flag as suspicious
   - Detect deviations from normal behavior

### Implementation Approach

```python
def predict_next_camera(identity_id, current_camera, current_time):
    """
    Predict which camera the person will appear at next.
    
    Returns:
    - predicted_cameras: List of (camera_id, probability, estimated_time)
    """
    # Get historical trajectories
    trajectories = get_historical_trajectories(identity_id)
    
    # Find similar starting points
    similar_trajectories = [
        t for t in trajectories 
        if t.start_camera == current_camera
    ]
    
    # Count next cameras
    next_camera_counts = defaultdict(int)
    for traj in similar_trajectories:
        if len(traj.cameras) > 1:
            next_camera = traj.cameras[1]
            next_camera_counts[next_camera] += 1
    
    # Calculate probabilities
    total = sum(next_camera_counts.values())
    predictions = []
    for camera, count in next_camera_counts.items():
        probability = count / total
        avg_time = calculate_avg_travel_time(current_camera, camera)
        predictions.append((camera, probability, current_time + avg_time))
    
    return sorted(predictions, key=lambda x: x[1], reverse=True)
```

### Feasibility: ✅ **HIGH** - Can implement now

**Why:**
- We have historical appearance data
- We have camera coordinates for distance calculation
- Simple pattern matching algorithm

**Implementation Time:** 3-4 days

---

## 3. Multi-Feature Person Re-ID

### What It Does

**Multi-Feature Person Re-ID** uses multiple features beyond face recognition:
- **Clothing**: Color, style, patterns
- **Gait**: Walking pattern, stride length
- **Body Shape**: Height, build, silhouette
- **Accessories**: Bags, hats, distinctive items

### How It Helps

1. **Better Cross-Camera Matching**
   - Face recognition may fail due to angles, lighting, occlusions
   - Clothing/gait features work even when face is not visible
   - Example: Person wearing red shirt + blue jeans is easier to track

2. **Higher Accuracy**
   - Combining multiple features = more robust matching
   - Reduces false positives and false negatives

3. **Works in Challenging Conditions**
   - Low light (clothing colors still visible)
   - Far distances (gait pattern visible)
   - Partial occlusions (body shape still visible)

### Implementation Approach

```python
# Option 1: Use existing models (easier)
# - DeepSORT for person tracking (includes appearance features)
# - Re-ID models (ResNet-based)

# Option 2: Extract features from existing detections
def extract_multi_features(image, bbox):
    """
    Extract multiple features from person detection.
    """
    features = {}
    
    # 1. Face features (already have)
    face_features = extract_face_embedding(image, bbox)
    
    # 2. Clothing features (color histogram)
    clothing_features = extract_clothing_colors(image, bbox)
    
    # 3. Body shape (height, width ratio)
    body_features = extract_body_shape(bbox)
    
    # 4. Gait (if video available, analyze walking pattern)
    # gait_features = analyze_gait(video_clip)
    
    return {
        'face': face_features,
        'clothing': clothing_features,
        'body': body_features
    }

def match_multi_features(query_features, candidate_features):
    """
    Match using multiple features with weighted scoring.
    """
    scores = {}
    
    # Face similarity (weight: 0.6)
    if query_features['face'] and candidate_features['face']:
        scores['face'] = cosine_similarity(
            query_features['face'], 
            candidate_features['face']
        ) * 0.6
    
    # Clothing similarity (weight: 0.3)
    if query_features['clothing'] and candidate_features['clothing']:
        scores['clothing'] = compare_clothing(
            query_features['clothing'],
            candidate_features['clothing']
        ) * 0.3
    
    # Body shape similarity (weight: 0.1)
    if query_features['body'] and candidate_features['body']:
        scores['body'] = compare_body_shape(
            query_features['body'],
            candidate_features['body']
        ) * 0.1
    
    return sum(scores.values())
```

### Feasibility: ⚠️ **MEDIUM** - Requires additional models

**Why:**
- Need to add feature extraction models
- Requires storing additional features in database
- More complex than current face-only approach

**Implementation Time:** 1-2 weeks (if using existing models)

**Simpler Alternative:**
- Start with clothing color extraction (easier, no ML model needed)
- Add body shape features (simple geometric calculations)
- Add gait analysis later (requires video analysis)

---

## 4. Automatic Threshold Learning

### What It Does

**Automatic Threshold Learning** automatically learns optimal distance and time thresholds for each camera pair based on historical data. Instead of fixed thresholds, it adapts to actual travel patterns.

### How It Helps

1. **No Manual Configuration**
   - System learns: "Camera 1 to Camera 2: average travel time is 3 minutes"
   - Automatically adjusts time windows per camera pair
   - No need to manually set thresholds

2. **More Accurate Detection**
   - Different camera pairs have different optimal thresholds
   - Example: Urban cameras (200m apart, 2min walk) vs. facility cameras (800m apart, 8min walk)
   - System adapts to each pair

3. **Handles Network Changes**
   - If new camera added, system learns its patterns automatically
   - If camera moved, thresholds update automatically

### Implementation Approach

```python
class ThresholdLearner:
    """
    Learns optimal thresholds for each camera pair.
    """
    
    def learn_thresholds(self, camera_pair, historical_data):
        """
        Learn optimal distance and time thresholds for a camera pair.
        """
        # Get all cross-camera movements between these two cameras
        movements = get_cross_camera_movements(camera_pair)
        
        if len(movements) < 10:  # Need minimum data
            return None  # Use default thresholds
        
        # Calculate statistics
        travel_times = [m.time_diff for m in movements]
        distances = [m.distance for m in movements]
        
        # Learn optimal time window (95th percentile of travel times)
        optimal_time_window = np.percentile(travel_times, 95)
        
        # Learn optimal distance (actual distance + buffer)
        optimal_distance = max(distances) * 1.2  # 20% buffer
        
        return {
            'camera_pair': camera_pair,
            'optimal_time_window_minutes': optimal_time_window,
            'optimal_distance_meters': optimal_distance,
            'confidence': len(movements) / 100.0  # More data = higher confidence
        }
    
    def get_thresholds(self, camera_1, camera_2):
        """
        Get learned thresholds for a camera pair, or use defaults.
        """
        learned = self.learned_thresholds.get((camera_1, camera_2))
        
        if learned and learned['confidence'] > 0.5:
            return learned['optimal_time_window_minutes'], learned['optimal_distance_meters']
        
        # Fallback to defaults
        return (
            settings.MULTI_CAMERA_TIME_WINDOW_MINUTES,
            settings.MULTI_CAMERA_DISTANCE_METERS
        )
```

### Feasibility: ✅ **HIGH** - Can implement now

**Why:**
- We have historical appearance data
- Simple statistical analysis (percentiles)
- No external dependencies

**Implementation Time:** 2-3 days

---

## Implementation Priority

### Phase 1: Quick Wins (1 week)
1. ✅ **Automatic Threshold Learning** - High impact, easy to implement
2. ✅ **Trajectory Prediction** - High impact, medium complexity

### Phase 2: Medium-Term (2-3 weeks)
3. ✅ **Activity Correlation Analysis (xCCA)** - Medium impact, medium complexity
4. ⚠️ **Multi-Feature Person Re-ID** - High impact, high complexity (start with simple features)

### Phase 3: Advanced (1-2 months)
5. **Full Multi-Feature Re-ID** - Complete implementation with ML models
6. **Real-Time Streaming** - Process relationships in real-time

---

## Summary

| Enhancement | Impact | Feasibility | Time | Status |
|------------|--------|-------------|------|--------|
| **Activity Correlation (xCCA)** | High | ✅ High | 2-3 days | Ready |
| **Trajectory Prediction** | High | ✅ High | 3-4 days | Ready |
| **Multi-Feature Re-ID** | Very High | ⚠️ Medium | 1-2 weeks | Partial |
| **Automatic Threshold Learning** | Medium | ✅ High | 2-3 days | Ready |

**Recommendation:** Start with **Automatic Threshold Learning** and **Trajectory Prediction** (quick wins), then add **Activity Correlation Analysis**, and finally enhance with **Multi-Feature Re-ID**.

