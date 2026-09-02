"""
Knowledge Base Module
=====================
ChromaDB-based knowledge base for RAG-enhanced SQL query generation.
"""

import hashlib
import json
import logging
from typing import Optional, List, Dict
from datetime import datetime

import chromadb
from chromadb.config import Settings

from .config import Config

# Setup logger
logger = logging.getLogger(__name__)


class SQLKnowledgeBase:
    """
    ChromaDB-based knowledge base for storing and retrieving
    question-SQL pairs for RAG-enhanced query generation.
    """

    # Seed examples for the face detection system
    SEED_EXAMPLES = [
        {
            "question": "Show me all detected faces",
            "sql": "SELECT id, name, similarity, detection_id FROM faces ORDER BY id DESC LIMIT 100",
            "purpose": "Retrieve all face detection records with their recognition details"
        },
        {
            "question": "Track Joey",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, f.similarity, d.timestamp, f.face_image_path
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%Joey%')
ORDER BY d.timestamp ASC""",
            "purpose": "Track all detections of a person named Joey including which camera detected them, ordered chronologically for story generation"
        },
        {
            "question": "Where is Joey",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, f.similarity, d.timestamp, f.face_image_path
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%Joey%')
ORDER BY d.timestamp DESC LIMIT 10""",
            "purpose": "Find the latest camera location where Joey was detected"
        },
        {
            "question": "Find faces with high similarity score",
            "sql": "SELECT name, similarity, detection_id FROM faces WHERE similarity > 0.8 ORDER BY similarity DESC",
            "purpose": "Find high-confidence face matches"
        },
        {
            "question": "How many faces were detected",
            "sql": "SELECT COUNT(*) as total_faces FROM faces",
            "purpose": "Count total face detections"
        },
        {
            "question": "Show all active pipelines",
            "sql": "SELECT id, pipeline_id, created_at, total_detections FROM pipelines WHERE is_active = true",
            "purpose": "List all currently active detection pipelines"
        },
        {
            "question": "Which pipeline has the most detections",
            "sql": "SELECT pipeline_id, total_detections FROM pipelines ORDER BY total_detections DESC LIMIT 1",
            "purpose": "Find the most productive pipeline"
        },
        {
            "question": "Show recent detections",
            "sql": "SELECT id, pipeline_id, timestamp, uuid, processing_time_ms FROM detections ORDER BY timestamp DESC LIMIT 20",
            "purpose": "Get the most recent detection events"
        },
        {
            "question": "Get system CPU usage",
            "sql": "SELECT timestamp, cpu_percent, memory_percent FROM system_metrics ORDER BY timestamp DESC LIMIT 50",
            "purpose": "Retrieve recent CPU and memory usage metrics"
        },
        {
            "question": "Show system performance over time",
            "sql": """SELECT
    DATE_TRUNC('hour', timestamp) as hour,
    AVG(cpu_percent) as avg_cpu,
    AVG(memory_percent) as avg_memory,
    AVG(queue_size) as avg_queue
FROM system_metrics
GROUP BY DATE_TRUNC('hour', timestamp)
ORDER BY hour DESC LIMIT 24""",
            "purpose": "Aggregate system metrics by hour"
        },
        {
            "question": "Find detections by pipeline",
            "sql": """SELECT p.pipeline_id, COUNT(d.id) as detection_count
FROM pipelines p
LEFT JOIN detections d ON p.pipeline_id = d.pipeline_id
GROUP BY p.pipeline_id, p.location_name
ORDER BY detection_count DESC""",
            "purpose": "Count detections per pipeline"
        },
        {
            "question": "Show faces detected in the last hour",
            "sql": """SELECT f.name, f.similarity, d.timestamp
FROM faces f
JOIN detections d ON f.detection_id = d.id
WHERE d.timestamp > NOW() - INTERVAL '1 hour'
ORDER BY d.timestamp DESC""",
            "purpose": "Get recent face detections within the last hour"
        },
        {
            "question": "Find a specific person by name",
            "sql": """SELECT f.name, f.similarity, d.timestamp, f.face_image_path
FROM faces f
JOIN detections d ON f.detection_id = d.id
WHERE LOWER(f.name) LIKE LOWER('%{name}%')
ORDER BY d.timestamp DESC""",
            "purpose": "Search for detections of a specific person"
        },
        {
            "question": "Get average similarity score per person",
            "sql": """SELECT name, COUNT(*) as detection_count, AVG(similarity) as avg_similarity
FROM faces
WHERE name IS NOT NULL
GROUP BY name
ORDER BY detection_count DESC""",
            "purpose": "Aggregate face recognition statistics by person"
        },
        {
            "question": "Show queue size trends",
            "sql": """SELECT timestamp, queue_size, processing_count
FROM system_metrics
ORDER BY timestamp DESC LIMIT 100""",
            "purpose": "Monitor processing queue status"
        },
        {
            "question": "Find unrecognized faces",
            "sql": "SELECT id, detection_id, similarity FROM faces WHERE name IS NULL OR name = '' ORDER BY id DESC",
            "purpose": "Find faces that were not recognized"
        },
        {
            "question": "Get detection count by day",
            "sql": """SELECT DATE(timestamp) as date, COUNT(*) as detections
FROM detections
GROUP BY DATE(timestamp)
ORDER BY date DESC LIMIT 30""",
            "purpose": "Daily detection statistics"
        },
        {
            "question": "Show pipelines created this week",
            "sql": """SELECT id, pipeline_id, is_active, created_at, total_detections
FROM pipelines
WHERE created_at > NOW() - INTERVAL '7 days'
ORDER BY created_at DESC""",
            "purpose": "List recently created pipelines"
        },
        {
            "question": "Get face bounding box sizes",
            "sql": """SELECT name,
    (bbox_x2 - bbox_x1) as width,
    (bbox_y2 - bbox_y1) as height,
    similarity
FROM faces
WHERE bbox_x1 IS NOT NULL
ORDER BY (bbox_x2 - bbox_x1) * (bbox_y2 - bbox_y1) DESC
LIMIT 50""",
            "purpose": "Analyze face sizes in detections"
        },
        {
            "question": "How many times was someone detected",
            "sql": """SELECT name, COUNT(*) as times_detected,
    MIN(d.timestamp) as first_seen,
    MAX(d.timestamp) as last_seen
FROM faces f
JOIN detections d ON f.detection_id = d.id
WHERE f.name IS NOT NULL
GROUP BY f.name
ORDER BY times_detected DESC""",
            "purpose": "Track detection frequency per person"
        },
        {
            "question": "Show high resource usage periods",
            "sql": """SELECT timestamp, cpu_percent, memory_percent, queue_size
FROM system_metrics
WHERE cpu_percent > 80 OR memory_percent > 80
ORDER BY timestamp DESC LIMIT 50""",
            "purpose": "Find system stress periods"
        },
        {
            "question": "Which camera detected Joey",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, d.timestamp, f.similarity
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%Joey%')
ORDER BY d.timestamp DESC""",
            "purpose": "Track which cameras/pipelines detected a specific person"
        },
        {
            "question": "Show all cameras that detected someone",
            "sql": """SELECT DISTINCT COALESCE(p.location_name, p.pipeline_id) as camera_name, f.name, COUNT(*) as detection_count
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE f.name IS NOT NULL
GROUP BY p.pipeline_id, p.location_name, f.name
ORDER BY detection_count DESC""",
            "purpose": "List all cameras and the people they detected"
        },
        {
            "question": "Track person movement across cameras",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, d.timestamp, f.face_image_path
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%{name}%')
ORDER BY d.timestamp ASC""",
            "purpose": "Show chronological movement of a person across different cameras"
        },
        {
            "question": "Which camera is most active",
            "sql": """SELECT COALESCE(p.location_name, p.pipeline_id) as camera_name, COUNT(d.id) as total_detections,
    COUNT(DISTINCT f.name) as unique_people,
    MAX(d.timestamp) as last_detection
FROM pipelines p
LEFT JOIN detections d ON p.pipeline_id = d.pipeline_id
LEFT JOIN faces f ON d.id = f.detection_id
GROUP BY p.pipeline_id, p.location_name
ORDER BY total_detections DESC""",
            "purpose": "Compare camera activity and find the most active camera"
        },
        {
            "question": "Show detections by camera in the last hour",
            "sql": """SELECT COALESCE(p.location_name, p.pipeline_id) as camera_name, f.name, d.timestamp, f.similarity
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE d.timestamp > NOW() - INTERVAL '1 hour'
ORDER BY d.timestamp DESC""",
            "purpose": "Recent detections grouped by camera"
        },
        {
            "question": "Find which cameras detected a person today ",
            "sql": """SELECT DISTINCT COALESCE(p.location_name, p.pipeline_id) as camera_name,
    MIN(d.timestamp) as first_seen,
    MAX(d.timestamp) as last_seen,
    COUNT(*) as times_detected
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%{name}%')
    AND DATE(d.timestamp) = CURRENT_DATE
GROUP BY p.pipeline_id, p.location_name
ORDER BY first_seen ASC""",
            "purpose": "Show which cameras saw a person today and when"
        },
        {
            "question": "Compare camera detection rates",
            "sql": """SELECT COALESCE(p.location_name, p.pipeline_id) as camera_name,
    COUNT(DISTINCT DATE(d.timestamp)) as active_days,
    COUNT(d.id) as total_detections,
    COUNT(DISTINCT f.name) as unique_people,
    ROUND(COUNT(d.id)::numeric / NULLIF(COUNT(DISTINCT DATE(d.timestamp)), 0), 2) as avg_detections_per_day
FROM pipelines p
LEFT JOIN detections d ON p.pipeline_id = d.pipeline_id
LEFT JOIN faces f ON d.id = f.detection_id
WHERE p.is_active = true
GROUP BY p.pipeline_id, p.location_name
ORDER BY total_detections DESC""",
            "purpose": "Analyze and compare detection rates across all cameras"
        },
        {
            "question": "Show person's path through cameras",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, d.timestamp,
    LAG(p.pipeline_id) OVER (PARTITION BY f.name ORDER BY d.timestamp) as previous_camera,
    d.timestamp - LAG(d.timestamp) OVER (PARTITION BY f.name ORDER BY d.timestamp) as time_since_last
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%{name}%')
ORDER BY d.timestamp DESC LIMIT 20""",
            "purpose": "Track a person's movement path between cameras with timing"
        },
        {
            "question": "Which cameras have not detected anyone recently",
            "sql": """SELECT COALESCE(p.location_name, p.pipeline_id) as camera_name,
    p.is_active,
    MAX(d.timestamp) as last_detection,
    NOW() - MAX(d.timestamp) as time_since_last_detection
FROM pipelines p
LEFT JOIN detections d ON p.pipeline_id = d.pipeline_id
GROUP BY p.pipeline_id, p.location_name, p.is_active
HAVING MAX(d.timestamp) < NOW() - INTERVAL '1 hour' OR MAX(d.timestamp) IS NULL
ORDER BY last_detection DESC NULLS LAST""",
            "purpose": "Find inactive or idle cameras"
        },
        {
            "question": "Get all detections from a specific camera",
            "sql": """SELECT f.name, f.similarity, d.timestamp, f.face_image_path
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(p.pipeline_id) LIKE LOWER('%{camera_name}%')
ORDER BY d.timestamp DESC LIMIT 50""",
            "purpose": "Retrieve all detections from a specific camera/pipeline"
        },
        # ========== SURVEILLANCE & TRACKING EXAMPLES ==========
        {
            "question": "Track Monica",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, f.similarity, d.timestamp, f.face_image_path
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%Monica%')
ORDER BY d.timestamp ASC""",
            "purpose": "Track all detections of Monica across cameras chronologically"
        },
        {
            "question": "Track Ross",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, f.similarity, d.timestamp, f.face_image_path
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%Ross%')
ORDER BY d.timestamp ASC""",
            "purpose": "Track all detections of Ross across cameras chronologically"
        },
        {
            "question": "Track Rachel",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, f.similarity, d.timestamp, f.face_image_path
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%Rachel%')
ORDER BY d.timestamp ASC""",
            "purpose": "Track all detections of Rachel across cameras chronologically"
        },
        {
            "question": "Track Chandler",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, f.similarity, d.timestamp, f.face_image_path
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%Chandler%')
ORDER BY d.timestamp ASC""",
            "purpose": "Track all detections of Chandler across cameras chronologically"
        },
        {
            "question": "Track Phoebe",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, f.similarity, d.timestamp, f.face_image_path
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%Phoebe%')
ORDER BY d.timestamp ASC""",
            "purpose": "Track all detections of Phoebe across cameras chronologically"
        },
        {
            "question": "Who was at camera entrance today",
            "sql": """SELECT f.name, COUNT(*) as detection_count, MIN(d.timestamp) as first_seen, MAX(d.timestamp) as last_seen
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(p.pipeline_id) LIKE LOWER('%entrance%')
    AND DATE(d.timestamp) = CURRENT_DATE
    AND f.name IS NOT NULL
GROUP BY f.name
ORDER BY detection_count DESC""",
            "purpose": "List all people detected at entrance camera today"
        },
        {
            "question": "Show person's complete journey today",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, d.timestamp,
    LAG(p.pipeline_id) OVER (PARTITION BY f.name ORDER BY d.timestamp) as previous_camera,
    d.timestamp - LAG(d.timestamp) OVER (PARTITION BY f.name ORDER BY d.timestamp) as time_between
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%{name}%')
    AND DATE(d.timestamp) = CURRENT_DATE
ORDER BY d.timestamp ASC""",
            "purpose": "Show complete movement path of a person throughout today with time gaps"
        },
        {
            "question": "Find people who visited multiple cameras today",
            "sql": """SELECT f.name, COUNT(DISTINCT p.pipeline_id) as cameras_visited, COUNT(*) as total_detections
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE DATE(d.timestamp) = CURRENT_DATE
    AND f.name IS NOT NULL
GROUP BY f.name
HAVING COUNT(DISTINCT p.pipeline_id) > 1
ORDER BY cameras_visited DESC, total_detections DESC""",
            "purpose": "Identify people who moved between multiple cameras today"
        },
        {
            "question": "Show last seen location for all people",
            "sql": """SELECT f.name, p.pipeline_id as last_camera, MAX(d.timestamp) as last_seen, f.similarity
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE f.name IS NOT NULL
GROUP BY f.name, p.pipeline_id, p.location_name, f.similarity, d.timestamp
HAVING d.timestamp = (SELECT MAX(d2.timestamp) FROM detections d2 JOIN faces f2 ON d2.id = f2.detection_id WHERE f2.name = f.name)
ORDER BY last_seen DESC""",
            "purpose": "Find the most recent camera location for each person"
        },
        {
            "question": "Track person between specific time range",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, d.timestamp, f.similarity
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%{name}%')
    AND d.timestamp >= '{start_time}'::timestamp
    AND d.timestamp <= '{end_time}'::timestamp
ORDER BY d.timestamp ASC""",
            "purpose": "Track person's movement within a specific time window"
        },
        {
            "question": "Find suspicious activity multiple detections same person short time",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, COUNT(*) as rapid_detections,
    MIN(d.timestamp) as first_detection, MAX(d.timestamp) as last_detection,
    MAX(d.timestamp) - MIN(d.timestamp) as time_span
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE f.name IS NOT NULL
    AND d.timestamp > NOW() - INTERVAL '1 hour'
GROUP BY f.name, p.pipeline_id, p.location_name
HAVING COUNT(*) > 5 AND MAX(d.timestamp) - MIN(d.timestamp) < INTERVAL '10 minutes'
ORDER BY rapid_detections DESC""",
            "purpose": "Detect suspicious rapid repeated detections of same person"
        },
        {
            "question": "Show all people currently in building last 15 minutes",
            "sql": """SELECT DISTINCT f.name, p.pipeline_id as current_camera, MAX(d.timestamp) as last_seen
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE d.timestamp > NOW() - INTERVAL '15 minutes'
    AND f.name IS NOT NULL
GROUP BY f.name, p.pipeline_id, p.location_name
ORDER BY last_seen DESC""",
            "purpose": "List people detected in the last 15 minutes (currently present)"
        },
        {
            "question": "Find people who entered but never left",
            "sql": """SELECT f.name, MIN(d.timestamp) as first_detection, MAX(d.timestamp) as last_detection,
    COUNT(DISTINCT p.pipeline_id) as cameras_visited
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE DATE(d.timestamp) = CURRENT_DATE
    AND f.name IS NOT NULL
GROUP BY f.name
HAVING MAX(d.timestamp) > NOW() - INTERVAL '2 hours'
ORDER BY last_detection DESC""",
            "purpose": "Find people detected today who may still be in the building"
        },
        {
            "question": "Show camera coverage gaps",
            "sql": """SELECT COALESCE(p.location_name, p.pipeline_id) as camera_name,
    MAX(d.timestamp) as last_detection,
    NOW() - MAX(d.timestamp) as time_since_detection,
    COUNT(d.id) as total_detections_today
FROM pipelines p
LEFT JOIN detections d ON p.pipeline_id = d.pipeline_id AND DATE(d.timestamp) = CURRENT_DATE
WHERE p.is_active = true
GROUP BY p.pipeline_id, p.location_name
HAVING MAX(d.timestamp) < NOW() - INTERVAL '30 minutes' OR MAX(d.timestamp) IS NULL
ORDER BY time_since_detection DESC NULLS FIRST""",
            "purpose": "Identify cameras with no recent activity (potential issues)"
        },
        {
            "question": "Track person's path with dwell time at each camera",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name,
    MIN(d.timestamp) as arrival_time,
    MAX(d.timestamp) as departure_time,
    MAX(d.timestamp) - MIN(d.timestamp) as dwell_time,
    COUNT(*) as detections_at_camera
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%{name}%')
    AND DATE(d.timestamp) = CURRENT_DATE
GROUP BY f.name, p.pipeline_id, p.location_name
ORDER BY arrival_time ASC""",
            "purpose": "Show how long a person spent at each camera location"
        },
        {
            "question": "Find people detected at same time same camera",
            "sql": """SELECT DISTINCT COALESCE(p1.location_name, p1.pipeline_id) as camera_name, 
    d1.timestamp as detection_time,
    f1.name as person1,
    f2.name as person2,
    ABS(EXTRACT(EPOCH FROM (d1.timestamp - d2.timestamp))) as time_difference_seconds
FROM faces f1
JOIN detections d1 ON f1.detection_id = d1.id
JOIN pipelines p1 ON d1.pipeline_id = p1.pipeline_id
JOIN faces f2 ON f2.detection_id != f1.detection_id
JOIN detections d2 ON f2.detection_id = d2.id
JOIN pipelines p2 ON d2.pipeline_id = p2.pipeline_id
WHERE f1.name IS NOT NULL 
    AND f2.name IS NOT NULL
    AND f1.name != f2.name
    AND p1.pipeline_id = p2.pipeline_id
    AND ABS(EXTRACT(EPOCH FROM (d1.timestamp - d2.timestamp))) <= 5
    AND d1.timestamp > NOW() - INTERVAL '24 hours'
ORDER BY d1.timestamp DESC""",
            "purpose": "Find instances where multiple people were detected within 5 seconds of each other at the same camera using time windows"
        },
        {
            "question": "Find two specific people detected together at same camera with time window",
            "sql": """SELECT COALESCE(p1.location_name, p1.pipeline_id) as camera_name, 
    d1.timestamp as person1_timestamp,
    d2.timestamp as person2_timestamp,
    f1.name as person1_name,
    f2.name as person2_name,
    ABS(EXTRACT(EPOCH FROM (d1.timestamp - d2.timestamp))) as time_difference_seconds
FROM faces f1
JOIN detections d1 ON f1.detection_id = d1.id
JOIN pipelines p1 ON d1.pipeline_id = p1.pipeline_id
JOIN faces f2 ON f2.detection_id != f1.detection_id
JOIN detections d2 ON f2.detection_id = d2.id
JOIN pipelines p2 ON d2.pipeline_id = p2.pipeline_id
WHERE LOWER(f1.name) LIKE LOWER('%{person1}%')
    AND LOWER(f2.name) LIKE LOWER('%{person2}%')
    AND p1.pipeline_id = p2.pipeline_id
    AND ABS(EXTRACT(EPOCH FROM (d1.timestamp - d2.timestamp))) <= 5
ORDER BY d1.timestamp DESC""",
            "purpose": "Find when two specific people (e.g., Ross and Joey) were detected together at the same camera within a 5-second time window"
        },
        {
            "question": "Find Ross and Joey detected together at same camera",
            "sql": """SELECT COALESCE(p1.location_name, p1.pipeline_id) as camera_name, 
    d1.timestamp as ross_timestamp,
    d2.timestamp as joey_timestamp,
    ABS(EXTRACT(EPOCH FROM (d1.timestamp - d2.timestamp))) as time_difference_seconds
FROM faces f1
JOIN detections d1 ON f1.detection_id = d1.id
JOIN pipelines p1 ON d1.pipeline_id = p1.pipeline_id
JOIN faces f2 ON f2.detection_id != f1.detection_id
JOIN detections d2 ON f2.detection_id = d2.id
JOIN pipelines p2 ON d2.pipeline_id = p2.pipeline_id
WHERE LOWER(f1.name) LIKE LOWER('%Ross%')
    AND LOWER(f2.name) LIKE LOWER('%Joey%')
    AND p1.pipeline_id = p2.pipeline_id
    AND ABS(EXTRACT(EPOCH FROM (d1.timestamp - d2.timestamp))) <= 5
ORDER BY d1.timestamp DESC""",
            "purpose": "Find when Ross and Joey were detected together at the same camera within a 5-second time window"
        },
        # ========== EDGE CASES & ADVANCED QUERIES ==========
        {
            "question": "Find detections with missing data",
            "sql": """SELECT d.id, d.timestamp, d.pipeline_id, 
    COUNT(f.id) as face_count,
    COUNT(CASE WHEN f.name IS NULL THEN 1 END) as unnamed_faces
FROM detections d
LEFT JOIN faces f ON d.id = f.detection_id
GROUP BY d.id, d.timestamp, d.pipeline_id
HAVING COUNT(f.id) = 0 OR COUNT(CASE WHEN f.name IS NULL THEN 1 END) > 0
ORDER BY d.timestamp DESC LIMIT 50""",
            "purpose": "Find detections with no faces or unrecognized faces"
        },
        {
            "question": "Show detections with low confidence scores",
            "sql": """SELECT f.name, f.similarity, COALESCE(p.location_name, p.pipeline_id) as camera_name, d.timestamp
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE f.similarity < 0.5 OR f.similarity IS NULL
ORDER BY d.timestamp DESC LIMIT 100""",
            "purpose": "Find low-confidence face recognition matches"
        },
        {
            "question": "Get detections from yesterday",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, d.timestamp, f.similarity
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE DATE(d.timestamp) = CURRENT_DATE - INTERVAL '1 day'
ORDER BY d.timestamp DESC""",
            "purpose": "Retrieve all detections from yesterday"
        },
        {
            "question": "Show detections from last week",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, d.timestamp, f.similarity
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE d.timestamp >= CURRENT_DATE - INTERVAL '7 days'
ORDER BY d.timestamp DESC""",
            "purpose": "Get all detections from the past week"
        },
        {
            "question": "Find people detected more than 10 times today",
            "sql": """SELECT f.name, COUNT(*) as detection_count,
    COUNT(DISTINCT p.pipeline_id) as cameras_visited,
    MIN(d.timestamp) as first_seen, MAX(d.timestamp) as last_seen
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE DATE(d.timestamp) = CURRENT_DATE
    AND f.name IS NOT NULL
GROUP BY f.name
HAVING COUNT(*) > 10
ORDER BY detection_count DESC""",
            "purpose": "Find frequently detected people today"
        },
        {
            "question": "Show detections by hour of day",
            "sql": """SELECT EXTRACT(HOUR FROM d.timestamp) as hour_of_day,
    COUNT(*) as detection_count,
    COUNT(DISTINCT f.name) as unique_people,
    COUNT(DISTINCT p.pipeline_id) as active_cameras
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE DATE(d.timestamp) = CURRENT_DATE
GROUP BY EXTRACT(HOUR FROM d.timestamp)
ORDER BY hour_of_day""",
            "purpose": "Analyze detection patterns by hour of day"
        },
        {
            "question": "Show average detection rate per camera",
            "sql": """SELECT COALESCE(p.location_name, p.pipeline_id) as camera_name,
    COUNT(d.id) as total_detections,
    COUNT(DISTINCT DATE(d.timestamp)) as active_days,
    ROUND(COUNT(d.id)::numeric / NULLIF(COUNT(DISTINCT DATE(d.timestamp)), 0), 2) as avg_per_day,
    COUNT(DISTINCT f.name) as unique_people_detected
FROM pipelines p
LEFT JOIN detections d ON p.pipeline_id = d.pipeline_id
LEFT JOIN faces f ON d.id = f.detection_id
WHERE p.is_active = true
GROUP BY p.pipeline_id, p.location_name
ORDER BY total_detections DESC""",
            "purpose": "Calculate average detection statistics per camera"
        },
        {
            "question": "Find people detected at specific camera today",
            "sql": """SELECT f.name, COUNT(*) as detection_count,
    MIN(d.timestamp) as first_seen, MAX(d.timestamp) as last_seen,
    AVG(f.similarity) as avg_confidence
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(p.pipeline_id) LIKE LOWER('%{camera}%')
    AND DATE(d.timestamp) = CURRENT_DATE
    AND f.name IS NOT NULL
GROUP BY f.name
ORDER BY detection_count DESC""",
            "purpose": "List all people detected at a specific camera today"
        },
        {
            "question": "Show detection timeline for person",
            "sql": """SELECT d.timestamp, COALESCE(p.location_name, p.pipeline_id) as camera_name, f.similarity,
    d.timestamp - LAG(d.timestamp) OVER (ORDER BY d.timestamp) as time_since_previous
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%{name}%')
ORDER BY d.timestamp ASC""",
            "purpose": "Show chronological timeline of all detections for a person"
        },
        {
            "question": "Find cameras with no detections today",
            "sql": """SELECT COALESCE(p.location_name, p.pipeline_id) as camera_name, p.is_active,
    MAX(d.timestamp) as last_detection_ever
FROM pipelines p
LEFT JOIN detections d ON p.pipeline_id = d.pipeline_id AND DATE(d.timestamp) = CURRENT_DATE
WHERE p.is_active = true
GROUP BY p.pipeline_id, p.location_name, p.is_active
HAVING COUNT(d.id) = 0
ORDER BY p.pipeline_id""",
            "purpose": "Identify active cameras with no detections today"
        },
        {
            "question": "Show top detected people this week",
            "sql": """SELECT f.name, COUNT(*) as detection_count,
    COUNT(DISTINCT p.pipeline_id) as cameras_visited,
    MIN(d.timestamp) as first_seen, MAX(d.timestamp) as last_seen
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE d.timestamp >= CURRENT_DATE - INTERVAL '7 days'
    AND f.name IS NOT NULL
GROUP BY f.name
ORDER BY detection_count DESC
LIMIT 20""",
            "purpose": "Find most frequently detected people in the past week"
        },
        {
            "question": "Track person with time gaps between detections",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, d.timestamp,
    d.timestamp - LAG(d.timestamp) OVER (PARTITION BY f.name ORDER BY d.timestamp) as gap_duration
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE LOWER(f.name) LIKE LOWER('%{name}%')
    AND DATE(d.timestamp) = CURRENT_DATE
ORDER BY d.timestamp ASC""",
            "purpose": "Show time gaps between detections to identify movement patterns"
        },
        {
            "question": "Find duplicate detections same person same time",
            "sql": """SELECT f.name, d.timestamp, COALESCE(p.location_name, p.pipeline_id) as camera_name, COUNT(*) as duplicate_count
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE f.name IS NOT NULL
GROUP BY f.name, d.timestamp, p.pipeline_id
HAVING COUNT(*) > 1
ORDER BY d.timestamp DESC""",
            "purpose": "Find duplicate face detections (same person, same time, same camera)"
        },
        {
            "question": "Show system health last 24 hours",
            "sql": """SELECT 
    DATE_TRUNC('hour', timestamp) as hour,
    AVG(cpu_percent) as avg_cpu,
    AVG(memory_percent) as avg_memory,
    AVG(queue_size) as avg_queue,
    MAX(queue_size) as max_queue
FROM system_metrics
WHERE timestamp > NOW() - INTERVAL '24 hours'
GROUP BY DATE_TRUNC('hour', timestamp)
ORDER BY hour DESC""",
            "purpose": "Monitor system health metrics over the past 24 hours"
        },
        {
            "question": "Find detections during specific hours",
            "sql": """SELECT f.name, COALESCE(p.location_name, p.pipeline_id) as camera_name, d.timestamp, f.similarity
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE EXTRACT(HOUR FROM d.timestamp) BETWEEN {start_hour} AND {end_hour}
    AND DATE(d.timestamp) = CURRENT_DATE
ORDER BY d.timestamp ASC""",
            "purpose": "Find detections during specific hours of the day"
        },
        {
            "question": "Show person's first and last detection today",
            "sql": """SELECT f.name,
    MIN(d.timestamp) as first_detection,
    MAX(d.timestamp) as last_detection,
    MAX(d.timestamp) - MIN(d.timestamp) as total_time_span,
    COUNT(*) as total_detections
FROM faces f
JOIN detections d ON f.detection_id = d.id
WHERE LOWER(f.name) LIKE LOWER('%{name}%')
    AND DATE(d.timestamp) = CURRENT_DATE
GROUP BY f.name""",
            "purpose": "Show when a person first and last appeared today"
        },
        {
            "question": "Find unrecognized faces from last hour",
            "sql": """SELECT d.id, d.timestamp, COALESCE(p.location_name, p.pipeline_id) as camera_name, f.similarity
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE (f.name IS NULL OR f.name = '')
    AND d.timestamp > NOW() - INTERVAL '1 hour'
ORDER BY d.timestamp DESC""",
            "purpose": "Find faces that couldn't be recognized in the last hour"
        },
        {
            "question": "Show camera activity heatmap",
            "sql": """SELECT COALESCE(p.location_name, p.pipeline_id) as camera_name,
    EXTRACT(HOUR FROM d.timestamp) as hour,
    COUNT(*) as detections
FROM pipelines p
JOIN detections d ON p.pipeline_id = d.pipeline_id
WHERE DATE(d.timestamp) = CURRENT_DATE
GROUP BY p.pipeline_id, EXTRACT(HOUR FROM d.timestamp)
ORDER BY p.pipeline_id, hour""",
            "purpose": "Create hourly activity heatmap for each camera"
        },
        {
            "question": "Find people who visited all cameras",
            "sql": """SELECT f.name, COUNT(DISTINCT p.pipeline_id) as cameras_visited
FROM faces f
JOIN detections d ON f.detection_id = d.id
JOIN pipelines p ON d.pipeline_id = p.pipeline_id
WHERE DATE(d.timestamp) = CURRENT_DATE
    AND f.name IS NOT NULL
GROUP BY f.name
HAVING COUNT(DISTINCT p.pipeline_id) = (SELECT COUNT(*) FROM pipelines WHERE is_active = true)
ORDER BY cameras_visited DESC""",
            "purpose": "Find people who visited every active camera today"
        },
        {
            "question": "Show detection frequency by person",
            "sql": """SELECT f.name,
    COUNT(*) as total_detections,
    COUNT(DISTINCT DATE(d.timestamp)) as days_active,
    COUNT(*)::numeric / NULLIF(COUNT(DISTINCT DATE(d.timestamp)), 0) as avg_detections_per_day,
    MIN(d.timestamp) as first_seen, MAX(d.timestamp) as last_seen
FROM faces f
JOIN detections d ON f.detection_id = d.id
WHERE f.name IS NOT NULL
GROUP BY f.name
ORDER BY total_detections DESC""",
            "purpose": "Calculate detection frequency statistics per person"
        }
    ]

    def __init__(self, config: Config):
        self.config = config

        # Initialize ChromaDB client with persistence
        self.client = chromadb.PersistentClient(
            path=config.chroma_persist_dir,
            settings=Settings(anonymized_telemetry=False)
        )

        # Get or create collection
        self.collection = self.client.get_or_create_collection(
            name=config.chroma_collection_name,
            metadata={"description": "SQL query examples for face detection system"}
        )

        # Auto-detect changes and re-initialize if needed
        self._auto_initialize_seed_examples()

    def _generate_id(self, text: str) -> str:
        """Generate a unique ID from text."""
        return hashlib.md5(text.encode()).hexdigest()

    def _calculate_seed_hash(self) -> str:
        """
        Calculate a hash of all seed examples to detect changes.

        Returns:
            MD5 hash of the seed examples content
        """
        # Create a stable string representation of seed examples
        seed_content = json.dumps(self.SEED_EXAMPLES, sort_keys=True)
        return hashlib.md5(seed_content.encode()).hexdigest()

    def _get_stored_seed_hash(self) -> Optional[str]:
        """
        Get the hash of seed examples from collection metadata.

        Returns:
            Stored hash or None if not found
        """
        try:
            metadata = self.collection.metadata
            return metadata.get("seed_hash") if metadata else None
        except Exception:
            return None

    def _update_seed_hash(self, seed_hash: str):
        """
        Update the stored seed hash in collection metadata.

        Args:
            seed_hash: The new hash to store
        """
        try:
            # Modify collection metadata
            self.collection.modify(metadata={"seed_hash": seed_hash})
        except Exception as e:
            logger.warning(f"⚠️ Warning: Could not update seed hash: {e}")

    def _clear_seed_examples(self):
        """Remove all seed examples from the collection."""
        try:
            # Get all seed examples
            all_data = self.collection.get(include=["metadatas"])
            seed_ids = [
                all_data["ids"][i]
                for i, meta in enumerate(all_data.get("metadatas", []))
                if meta.get("source") == "seed"
            ]

            if seed_ids:
                self.collection.delete(ids=seed_ids)
                logger.info(f"🗑️  Removed {len(seed_ids)} old seed examples")
        except Exception as e:
            logger.warning(f"⚠️ Warning: Could not clear old examples: {e}")

    def _auto_initialize_seed_examples(self):
        """
        Automatically detect changes in seed examples and re-initialize if needed.

        This method:
        1. Calculates hash of current SEED_EXAMPLES
        2. Compares with stored hash from last initialization
        3. If different, clears old seed examples and loads new ones
        4. Updates the stored hash
        """
        current_hash = self._calculate_seed_hash()
        stored_hash = self._get_stored_seed_hash()

        # Check if we need to re-initialize
        if stored_hash is None:
            # First time initialization
            logger.info("🌱 First-time initialization of knowledge base...")
            self._load_seed_examples()
            self._update_seed_hash(current_hash)
            logger.info(f"✅ Knowledge base initialized with {len(self.SEED_EXAMPLES)} seed examples")

        elif current_hash != stored_hash:
            # Seed examples have changed - re-initialize
            logger.info("🔄 SEED EXAMPLES CHANGED - Auto-updating knowledge base...")
            logger.info(f"   Old hash: {stored_hash[:8]}...")
            logger.info(f"   New hash: {current_hash[:8]}...")

            # Clear old seed examples
            self._clear_seed_examples()

            # Load new seed examples
            self._load_seed_examples()

            # Update hash
            self._update_seed_hash(current_hash)

            logger.info(f"✅ Knowledge base updated with {len(self.SEED_EXAMPLES)} seed examples")

        else:
            # No changes - skip initialization
            logger.info(f"✓ Knowledge base up-to-date ({self.collection.count()} total examples)")

    def _load_seed_examples(self):
        """Load all seed examples into the knowledge base."""
        for example in self.SEED_EXAMPLES:
            self.add_example(
                question=example["question"],
                sql=example["sql"],
                purpose=example["purpose"],
                source="seed"
            )



    def add_example(
        self,
        question: str,
        sql: str,
        purpose: str = "",
        source: str = "user",
        metadata: Optional[Dict] = None
    ) -> str:
        """
        Add a new question-SQL pair to the knowledge base.

        Args:
            question: Natural language question
            sql: Corresponding SQL query
            purpose: Description of what the query does
            source: Where this example came from (seed, user, learned)
            metadata: Additional metadata

        Returns:
            ID of the added document
        """
        doc_id = self._generate_id(question)

        # Check if already exists
        existing = self.collection.get(ids=[doc_id])
        if existing and existing['ids']:
            return doc_id

        # Prepare metadata
        doc_metadata = {
            "sql": sql,
            "purpose": purpose,
            "source": source,
            "added_at": datetime.utcnow().isoformat(),  # naive UTC (storage convention)
            **(metadata or {})
        }

        # Add to collection
        self.collection.add(
            documents=[question],
            metadatas=[doc_metadata],
            ids=[doc_id]
        )

        return doc_id

    def search_similar(
        self,
        query: str,
        top_k: Optional[int] = None,
        min_similarity: Optional[float] = None,
        user_id: Optional[int] = None,
    ) -> List[Dict]:
        """
        Search for similar questions in the knowledge base.

        Args:
            query: The question to search for
            top_k: Number of results to return
            min_similarity: Minimum similarity threshold (0-1)

        Returns:
            List of matching examples with their SQL and metadata
        """
        top_k = top_k or self.config.rag_top_k
        min_similarity = min_similarity or self.config.rag_similarity_threshold

        logger.debug(f"[KB] Searching for similar examples (top_k={top_k}, min_similarity={min_similarity})")

        # Tenant scoping.
        #
        # Retrieved examples are interpolated into the SQL-generation system
        # prompt, so anything reachable here is effectively injected into
        # another user's prompt. Learned entries carry the raw text of the
        # question that produced them — which in this deployment routinely
        # names people — so an unscoped search leaked one user's queries into
        # another's context, and made a crafted question a persistent
        # prompt-injection vector.
        #
        # Curated seed examples are shared by design; learned ones are visible
        # only to the user who produced them.
        # An absent user_id means curated seed examples ONLY. It must not mean
        # "no filter": the shared global agent instance carries no user id, so
        # an unfiltered search there would return every user's learned entries.
        if user_id is None:
            where = {"source": {"$eq": "seed"}}
        else:
            where = {"$or": [
                {"source": {"$eq": "seed"}},
                {"user_id": {"$eq": str(user_id)}},
            ]}

        # Query ChromaDB
        query_kwargs = {
            "query_texts": [query],
            "n_results": top_k,
            "include": ["documents", "metadatas", "distances"],
            "where": where,
        }

        try:
            results = self.collection.query(**query_kwargs)
        except Exception as e:
            # A malformed filter must not silently widen the search to every
            # user's entries — return nothing rather than everything.
            logger.error("[KB] Scoped search failed (%s); returning no examples", e)
            return []

        # Process results
        examples = []
        if results and results['documents'] and results['documents'][0]:
            for i, doc in enumerate(results['documents'][0]):
                # ChromaDB returns L2 distance, convert to similarity
                distance = results['distances'][0][i] if results['distances'] else 0
                similarity = 1 / (1 + distance)  # Convert distance to similarity

                if similarity >= min_similarity:
                    metadata = results['metadatas'][0][i] if results['metadatas'] else {}
                    examples.append({
                        "question": doc,
                        "sql": metadata.get("sql", ""),
                        "purpose": metadata.get("purpose", ""),
                        "similarity": round(similarity, 3),
                        "source": metadata.get("source", "unknown")
                    })

        logger.info(f"[KB] Found {len(examples)} similar examples (threshold: {min_similarity})")
        if examples:
            logger.debug("[KB] Top match similarity=%s",
                         examples[0]["similarity"])

        return examples

    def learn_from_success(
        self,
        question: str,
        sql: str,
        purpose: str = "",
        user_feedback: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> str:
        """
        Learn from a successful query execution.

        "Success" here means the database did not raise — not that the answer
        was correct — so learned entries are provenance-tagged and scoped to
        the user who produced them. They are retrievable only by that user;
        see search_similar. Without the owner tag an entry would be visible to
        everyone, which is how one user's question text reached another user's
        prompt.

        Args:
            question: The original user question
            sql: The SQL that successfully answered it
            purpose: Description of the query
            user_feedback: Optional user feedback
            user_id: Owner. Required — an untagged entry cannot be scoped.

        Returns:
            ID of the learned example, or "" when it was not stored.
        """
        if user_id is None:
            # Refuse rather than store an entry every user can retrieve.
            logger.warning(
                "[KB] Refusing to learn an example with no owner: it would be "
                "retrievable by every user."
            )
            return ""

        logger.info("[KB] Learning from a successful query for user %s", user_id)
        metadata = {"learned": True, "user_id": str(user_id)}
        if user_feedback:
            metadata["user_feedback"] = user_feedback

        example_id = self.add_example(
            question=question,
            sql=sql,
            purpose=purpose,
            source="learned",
            metadata=metadata
        )
        logger.info(f"[KB] Successfully learned new example (ID: {example_id})")
        return example_id

    def get_stats(self) -> Dict:
        """Get statistics about the knowledge base."""
        total = self.collection.count()

        # Get source breakdown
        all_data = self.collection.get(include=["metadatas"])
        sources = {}
        for meta in all_data.get("metadatas", []):
            source = meta.get("source", "unknown")
            sources[source] = sources.get(source, 0) + 1

        return {
            "total_examples": total,
            "by_source": sources
        }

    def format_examples_for_prompt(self, examples: List[Dict]) -> str:
        """Format retrieved examples for inclusion in LLM prompt."""
        if not examples:
            return "No similar examples found."

        lines = ["SIMILAR EXAMPLES FROM KNOWLEDGE BASE:", "=" * 40]

        for i, ex in enumerate(examples, 1):
            lines.append(f"\nExample {i} (similarity: {ex['similarity']}):")
            lines.append(f"  Question: {ex['question']}")
            lines.append(f"  Purpose: {ex['purpose']}")
            lines.append(f"  SQL:")
            # Indent SQL
            for sql_line in ex['sql'].strip().split('\n'):
                lines.append(f"    {sql_line}")

        lines.append("\n" + "=" * 40)
        return "\n".join(lines)
