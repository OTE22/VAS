# Security Intelligence System - Complete Guide

> **🎯 Advanced Intelligence for Security Agencies**
> 
> Sophisticated analysis tools to understand how people are connected and why.

## Table of Contents
1. [Overview](#overview)
2. [Features](#features)
3. [Social Network Analysis](#social-network-analysis)
4. [Suspicious Pattern Detection](#suspicious-pattern-detection)
5. [Anomaly Detection](#anomaly-detection)
6. [Threat Assessment](#threat-assessment)
7. [API Reference](#api-reference)
8. [Use Cases](#use-cases)

---

## Overview

The **Security Intelligence System** provides advanced analytical capabilities for security agencies to:

- **Map Connections**: Visualize social networks and relationships
- **Detect Patterns**: Identify suspicious group activities
- **Flag Anomalies**: Spot unusual behavioral changes
- **Assess Threats**: Calculate risk scores and prioritize investigations

### Key Capabilities

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    SECURITY INTELLIGENCE SYSTEM                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  🔗 NETWORK ANALYSIS    🚨 PATTERN DETECTION   ⚠️ ANOMALY DETECTION    │
│  ──────────────────     ───────────────────   ────────────────────    │
│  • Social graphs        • Group activities      • Off-schedule          │
│  • Connection maps      • Unusual timing        • New locations         │
│  • Cluster detection    • Rapid movement        • Behavior changes      │
│  • Hub identification  • Coordinated acts      • Deviation analysis    │
│                                                                         │
│  🎯 THREAT ASSESSMENT   📊 RISK SCORING        🔍 INVESTIGATION         │
│  ───────────────────    ───────────────        ─────────────            │
│  • Risk calculation     • Multi-factor          • Evidence linking      │
│  • Threat levels        • Weighted scoring       • Timeline building     │
│  • Recommendations      • Priority ranking      • Correlation engine    │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Features

### 1. Social Network Analysis
**Purpose**: Understand how people are connected

- **Network Graph**: Visual representation of relationships
- **Connection Strength**: Quantified relationship metrics
- **Clusters**: Identify groups and communities
- **Central Nodes**: Find key influencers/hubs
- **Risk Scoring**: Calculate risk based on connections

### 2. Suspicious Pattern Detection
**Purpose**: Identify coordinated or unusual activities

- **Group Activity**: Detect when multiple people appear together
- **Unusual Timing**: Flag off-hours activity (e.g., 2-5 AM)
- **Rapid Movement**: Identify quick location transitions
- **Repeated Co-appearances**: Find consistent group patterns

### 3. Anomaly Detection
**Purpose**: Flag deviations from normal behavior

- **Off-Schedule**: Activity at unusual times
- **New Locations**: First-time appearances at locations
- **Unusual Groups**: New associations with different people
- **Baseline Comparison**: Compare against historical patterns

### 4. Threat Assessment
**Purpose**: Prioritize investigations based on risk

- **Risk Scoring**: 0-100 score based on multiple factors
- **Threat Levels**: Critical/High/Medium/Low/Minimal
- **Risk Factors**: Detailed breakdown of contributing factors
- **Recommendations**: Actionable next steps

### 5. Advanced SNA Features (Optional)
**Purpose**: Enhanced relationship detection and prediction

- **Automatic Threshold Learning**: Dynamically learn optimal distance and time thresholds for camera pairs
- **Trajectory Prediction**: Predict next likely camera locations for an identity
- **Activity Correlation Analysis (xCCA)**: Detect causal relationships and coordinated activities between identities

**Note**: These features are documented in detail in **Chapter 9** (`59_ADVANCED_SNA_ENHANCEMENTS.md`).

---

## Social Network Analysis

### What It Does

Builds a graph showing:
- **Nodes**: Identities (people)
- **Edges**: Connections (relationships)
- **Clusters**: Groups of connected people
- **Central Nodes**: Most connected individuals

### Use Cases

1. **Investigation**: Map suspect networks
2. **Security**: Identify potential threats
3. **Analysis**: Understand social structures
4. **Monitoring**: Track relationship changes

### API Endpoint

```http
GET /api/security/network
```

**Parameters:**
- `identity_ids` (optional): Comma-separated IDs to analyze (empty = all)
- `min_connections` (default: 1): Minimum connections to include
- `days_back` (default: 90): Analysis window

**Response:**
```json
{
  "nodes": [
    {
      "identity_id": "uuid",
      "display_name": "John Doe",
      "identity_type": "known",
      "appearances_count": 45,
      "risk_score": 35.0,
      "connections_count": 8,
      "snapshot_url": "/storage/..."
    }
  ],
  "edges": [
    {
      "source_id": "uuid1",
      "target_id": "uuid2",
      "strength": 0.75,
      "co_appearances": 15,
      "co_appearance_percentage": 75.0,
      "first_seen_together": "2025-01-01T10:00:00",
      "last_seen_together": "2025-01-15T14:30:00",
      "common_locations": ["pipeline1", "pipeline2"],
      "relationship_type": "strong"
    }
  ],
  "clusters": [
    ["uuid1", "uuid2", "uuid3"]
  ],
  "central_nodes": ["uuid1", "uuid2"],
  "isolated_nodes": ["uuid4"]
}
```

### Example Usage

```python
# Get network for specific identities
response = requests.get(
    "http://localhost/api/security/network",
    params={
        "identity_ids": "uuid1,uuid2,uuid3",
        "min_connections": 2,
        "days_back": 30
    },
    headers={"Authorization": "Bearer token"}
)

network = response.json()
# Visualize network graph with nodes and edges
```

---

## Suspicious Pattern Detection

### What It Does

Automatically detects:
- **Group Activity**: 3+ people together
- **Unusual Timing**: Activity during 2-5 AM
- **Rapid Movement**: Location changes within 5 minutes

### Use Cases

1. **Security**: Detect coordinated activities
2. **Investigation**: Identify suspicious groups
3. **Monitoring**: Flag unusual behaviors
4. **Alerting**: Trigger alerts for patterns

### API Endpoint

```http
GET /api/security/patterns
```

**Parameters:**
- `days_back` (default: 30): Analysis window
- `min_group_size` (default: 3): Minimum group size

**Response:**
```json
[
  {
    "pattern_type": "group_activity",
    "description": "5 identities appeared together at pipeline1",
    "identities_involved": ["uuid1", "uuid2", "uuid3", "uuid4", "uuid5"],
    "severity": "high",
    "confidence": 0.8,
    "first_detected": "2025-01-10T14:30:00",
    "evidence": {
      "pipeline_id": "pipeline1",
      "group_size": 5,
      "window_time": "2025-01-10T14:30:00"
    },
    "locations": ["pipeline1"],
    "time_range": [
      "2025-01-10T14:30:00",
      "2025-01-10T14:35:00"
    ]
  }
]
```

---

## Anomaly Detection

### What It Does

Compares recent behavior against baseline to detect:
- **Off-Schedule**: Activity at unusual times
- **New Locations**: First-time appearances
- **Behavioral Changes**: Deviations from normal patterns

### Use Cases

1. **Security**: Flag suspicious behavior changes
2. **Investigation**: Identify unusual activities
3. **Monitoring**: Track behavioral shifts
4. **Alerting**: Notify on anomalies

### API Endpoint

```http
GET /api/security/anomalies/{identity_id}
```

**Parameters:**
- `identity_id`: Identity to analyze
- `days_back` (default: 90): Analysis window

**Response:**
```json
[
  {
    "identity_id": "uuid",
    "anomaly_type": "off_schedule",
    "description": "Activity at 03:00, normally around 14:00",
    "severity": "medium",
    "detected_at": "2025-01-15T03:00:00",
    "baseline": {
      "average_hour": 14.0
    },
    "deviation": {
      "actual_hour": 3,
      "difference": 11
    },
    "risk_score": 30.0
  }
]
```

---

## Threat Assessment

### What It Does

Calculates comprehensive risk score based on:
- Identity type (unknown = higher risk)
- Connection count (hub = higher risk)
- Behavioral anomalies
- Suspicious patterns
- Activity levels

### Use Cases

1. **Prioritization**: Rank investigations by risk
2. **Resource Allocation**: Focus on high-risk cases
3. **Decision Making**: Support security decisions
4. **Reporting**: Generate threat assessments

### API Endpoint

```http
GET /api/security/threat/{identity_id}
```

**Response:**
```json
{
  "identity_id": "uuid",
  "display_name": "John Doe",
  "overall_risk_score": 65.0,
  "risk_factors": [
    {
      "factor": "Unknown Identity",
      "score": 30.0,
      "description": "Identity is not in known persons database"
    },
    {
      "factor": "High Connection Count",
      "score": 25.0,
      "description": "Connected to 12 other identities"
    },
    {
      "factor": "Behavioral Anomalies",
      "score": 10.0,
      "description": "3 anomalies detected in last 30 days"
    }
  ],
  "threat_level": "high",
  "recommendations": [
    "Consider promoting to known identity if verified",
    "Investigate network connections - potential hub",
    "Review behavioral anomalies for suspicious activity",
    "Prioritize for investigation"
  ],
  "last_assessed": "2025-01-15T10:00:00"
}
```

---

## API Reference

### Base URL
```
/api/security
```

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/network` | GET | Social network analysis |
| `/patterns` | GET | Suspicious pattern detection |
| `/anomalies/{id}` | GET | Behavioral anomaly detection |
| `/threat/{id}` | GET | Threat assessment |

### Authentication

All endpoints require admin authentication:
```http
Authorization: Bearer <token>
```

---

## Use Cases

### 1. Investigation: Mapping Suspect Networks

**Scenario**: Investigating a security incident, need to understand connections

```python
# Get network for suspects
network = get_social_network(identity_ids=["suspect1", "suspect2"])

# Identify clusters
for cluster in network["clusters"]:
    print(f"Group: {cluster}")

# Find central nodes (potential leaders)
print(f"Key individuals: {network['central_nodes']}")
```

### 2. Security: Detecting Coordinated Activities

**Scenario**: Monitoring for suspicious group activities

```python
# Detect patterns
patterns = get_suspicious_patterns(days_back=7, min_group_size=4)

# Filter high-severity patterns
high_risk = [p for p in patterns if p["severity"] == "high"]

# Alert security team
if high_risk:
    send_alert(f"Detected {len(high_risk)} high-severity patterns")
```

### 3. Monitoring: Flagging Behavioral Changes

**Scenario**: Tracking known individuals for unusual behavior

```python
# Check for anomalies
anomalies = get_anomalies(identity_id="known_person", days_back=30)

# Review off-schedule activity
off_schedule = [a for a in anomalies if a["anomaly_type"] == "off_schedule"]

if off_schedule:
    investigate(identity_id, reason="Off-schedule activity detected")
```

### 4. Prioritization: Risk-Based Investigation

**Scenario**: Limited resources, need to prioritize cases

```python
# Assess threats for multiple identities
threats = []
for identity_id in suspect_list:
    assessment = get_threat_assessment(identity_id)
    threats.append(assessment)

# Sort by risk score
threats.sort(key=lambda x: x["overall_risk_score"], reverse=True)

# Investigate top 10
for threat in threats[:10]:
    if threat["threat_level"] in ["critical", "high"]:
        assign_investigator(threat["identity_id"])
```

---

## Integration Examples

### Python Client

```python
import requests

BASE_URL = "http://localhost/api/security"
HEADERS = {"Authorization": "Bearer <token>"}

def get_network(identity_ids=None, days_back=90):
    params = {"days_back": days_back}
    if identity_ids:
        params["identity_ids"] = ",".join(identity_ids)
    
    response = requests.get(f"{BASE_URL}/network", params=params, headers=HEADERS)
    return response.json()

def detect_patterns(days_back=30):
    response = requests.get(
        f"{BASE_URL}/patterns",
        params={"days_back": days_back},
        headers=HEADERS
    )
    return response.json()

def assess_threat(identity_id):
    response = requests.get(
        f"{BASE_URL}/threat/{identity_id}",
        headers=HEADERS
    )
    return response.json()
```

### JavaScript/Frontend

```javascript
async function getSocialNetwork(identityIds = null, daysBack = 90) {
    const params = new URLSearchParams({ days_back: daysBack });
    if (identityIds) {
        params.append('identity_ids', identityIds.join(','));
    }
    
    const response = await fetch(`/api/security/network?${params}`, {
        credentials: 'include'
    });
    return await response.json();
}

async function detectPatterns(daysBack = 30) {
    const response = await fetch(`/api/security/patterns?days_back=${daysBack}`, {
        credentials: 'include'
    });
    return await response.json();
}

async function assessThreat(identityId) {
    const response = await fetch(`/api/security/threat/${identityId}`, {
        credentials: 'include'
    });
    return await response.json();
}
```

---

## Summary

The Security Intelligence System provides powerful analytical tools for security agencies:

✅ **Social Network Analysis** - Map connections and relationships  
✅ **Pattern Detection** - Identify suspicious activities  
✅ **Anomaly Detection** - Flag behavioral changes  
✅ **Threat Assessment** - Prioritize investigations  

All features are **production-ready** and available via REST API endpoints.

