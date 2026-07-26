"""
Database Models for Face Recognition Service
Stores detection results in PostgreSQL for persistence
"""
from datetime import datetime
from typing import Optional, List
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey, Index, Boolean, Enum as SQLEnum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB, UUID, ARRAY
import uuid
import enum
import logging

# Try to import pgvector - graceful fallback if not installed
try:
    from pgvector.sqlalchemy import Vector
    PGVECTOR_AVAILABLE = True
except ImportError:
    PGVECTOR_AVAILABLE = False
    Vector = None  # Placeholder

logger = logging.getLogger(__name__)

Base = declarative_base()


# Enums for identity management
class IdentityType(str, enum.Enum):
    UNKNOWN = "unknown"
    KNOWN = "known"


class IdentityStatus(str, enum.Enum):
    ACTIVE = "active"
    MERGED = "merged"
    PROMOTED = "promoted"
    INACTIVE = "inactive"


class LabelState(str, enum.Enum):
    AUTO_UNKNOWN = "auto_unknown"
    AUTO_KNOWN = "auto_known"
    MANUAL_LABELED = "manual_labeled"


class MergeSuggestionStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# Enums for SQL Agent Memory System
class MemoryType(str, enum.Enum):
    FACT = "fact"
    PREFERENCE = "preference"
    CONTEXT = "context"
    PATTERN = "pattern"


class Pipeline(Base):
    """Pipeline tracking table"""
    __tablename__ = "pipelines"

    id = Column(Integer, primary_key=True)
    pipeline_id = Column(String(255), unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    total_detections = Column(Integer, default=0)
    is_active = Column(Integer, default=1)

    # Location coordinates for map visualization
    latitude = Column(Float, nullable=True, comment="Latitude coordinate for camera location")
    longitude = Column(Float, nullable=True, comment="Longitude coordinate for camera location")
    location_name = Column(String(255), nullable=True, comment="Human-readable location name (e.g., 'Main Entrance', 'Parking Lot')")

    # Relationships (passive_deletes: the DB cascades, ORM doesn't load rows to delete them)
    detections = relationship("Detection", back_populates="pipeline", cascade="all, delete-orphan", passive_deletes=True)
    user_access = relationship("UserPipelineAccess", back_populates="pipeline")

    __table_args__ = (
        Index('idx_pipeline_id_active', 'pipeline_id', 'is_active'),
        Index('idx_pipeline_coordinates', 'latitude', 'longitude'),
    )


class PipelineAlias(Base):
    """Maps a renamed pipeline's OLD id to its NEW id.

    Webhooks that still post with the old id (e.g. a tool-generated UUID URL)
    are transparently routed to the renamed pipeline.
    """
    __tablename__ = "pipeline_aliases"

    old_pipeline_id = Column(String(255), primary_key=True)
    new_pipeline_id = Column(String(255), ForeignKey('pipelines.pipeline_id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Detection(Base):
    """Face detection events table"""
    __tablename__ = "detections"

    id = Column(Integer, primary_key=True)
    uuid = Column(String(36), unique=True, default=lambda: str(uuid.uuid4()), index=True)
    # single-column indexes omitted: idx_detection_pipeline_timestamp /
    # idx_detection_timestamp in __table_args__ cover these lookups
    pipeline_id = Column(String(255), ForeignKey('pipelines.pipeline_id', ondelete='CASCADE'), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Image metadata
    image_path = Column(String(512), nullable=True)
    image_size_bytes = Column(Integer, nullable=True)

    # Processing metadata
    processing_time_ms = Column(Float, nullable=True)
    worker_id = Column(Integer, nullable=True)

    # Relationships (DB cascades: faces CASCADE, embeddings.detection_id SET NULL)
    pipeline = relationship("Pipeline", back_populates="detections")
    faces = relationship("Face", back_populates="detection", cascade="all, delete-orphan", passive_deletes=True)
    embeddings = relationship("IdentityEmbedding", back_populates="detection", passive_deletes=True)

    __table_args__ = (
        Index('idx_detection_pipeline_timestamp', 'pipeline_id', 'timestamp'),
        Index('idx_detection_timestamp', 'timestamp'),
    )


class Face(Base):
    """
    Recognized faces table

    DUPLICATE PREVENTION STRATEGY:
    - The FaceTracker in app_production.py prevents the same person from being
      saved multiple times within a short time window (default: 30 seconds)
    - This is an IN-MEMORY tracking system that uses embedding similarity
    - The database itself does NOT enforce uniqueness constraints on (name, detection_id)
    - The same person CAN appear in multiple detections across time
    - This is INTENTIONAL to maintain a historical record of all face appearances
    - If you need stricter uniqueness, consider:
      1. Increasing FACE_TRACKING_WINDOW_SECONDS in config
      2. Adding a unique constraint on (name, detection_id) - but this may cause errors
      3. Implementing a post-processing deduplication job
    """
    __tablename__ = "faces"

    id = Column(Integer, primary_key=True)
    # single-column index omitted: idx_face_detection composite covers it
    detection_id = Column(Integer, ForeignKey('detections.id', ondelete='CASCADE'), nullable=False)

    # Face information (name index provided by idx_face_name in __table_args__)
    name = Column(String(255), nullable=False)
    similarity = Column(Float, nullable=False)

    # Identity management: face history survives identity deletion (SET NULL)
    identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='SET NULL'), nullable=True, index=True)
    label_state = Column(SQLEnum(LabelState), nullable=True, index=True)

    # Face crop image
    # face_image_base64 = Column(Text, nullable=True)

    # Face image file path
    face_image_path = Column(String(512), nullable=True)

    # Bounding box coordinates (relative)
    bbox_x1 = Column(Float, nullable=True)
    bbox_y1 = Column(Float, nullable=True)
    bbox_x2 = Column(Float, nullable=True)
    bbox_y2 = Column(Float, nullable=True)

    # Relationships
    detection = relationship("Detection", back_populates="faces")
    identity = relationship("Identity", back_populates="faces")

    __table_args__ = (
        Index('idx_face_name', 'name'),
        Index('idx_face_detection', 'detection_id', 'name'),
    )


class SystemMetrics(Base):
    """System performance metrics table"""
    __tablename__ = "system_metrics"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Queue metrics
    queue_size = Column(Integer, default=0)
    processing_count = Column(Integer, default=0)
    total_received = Column(Integer, default=0)
    total_processed = Column(Integer, default=0)
    total_skipped = Column(Integer, default=0)

    # Performance metrics
    avg_processing_time_ms = Column(Float, nullable=True)
    active_pipelines = Column(Integer, default=0)
    total_faces_detected = Column(Integer, default=0)

    # Resource usage
    cpu_percent = Column(Float, nullable=True)
    memory_percent = Column(Float, nullable=True)
    disk_usage_gb = Column(Float, nullable=True)

    __table_args__ = (
        Index('idx_metrics_timestamp', 'timestamp'),
    )


class User(Base):
    """User accounts for authentication and authorization"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=True)
    role = Column(String(50), default="user", nullable=False, index=True)  # admin, user
    is_active = Column(Boolean, default=True, nullable=False)
    can_use_chatbot = Column(Boolean, default=False, nullable=False)
    blocked_reason = Column(Text, nullable=True)  # Reason for blocking (e.g., "Attempted forbidden SQL operation")
    blocked_at = Column(DateTime, nullable=True)  # When the user was blocked
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)

    # Relationships
    pipeline_access = relationship("UserPipelineAccess", back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        Index('idx_user_username_active', 'username', 'is_active'),
        Index('idx_user_role', 'role'),
    )


class UserPipelineAccess(Base):
    """Controls which pipelines a user can access"""
    __tablename__ = "user_pipeline_access"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    pipeline_id = Column(String(255), ForeignKey('pipelines.pipeline_id'), nullable=False, index=True)
    granted_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    user = relationship("User", back_populates="pipeline_access")
    pipeline = relationship("Pipeline", back_populates="user_access")

    __table_args__ = (
        Index('idx_user_pipeline', 'user_id', 'pipeline_id', unique=True),
    )


class ChatbotAuditLog(Base):
    """Audit log for chatbot queries and responses (admin-only access)"""
    __tablename__ = "chatbot_audit_log"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    username = Column(String(100), nullable=False, index=True)  # Denormalized for easier querying
    query = Column(Text, nullable=False)
    response = Column(Text, nullable=True)  # May be None if query failed
    success = Column(Boolean, default=True, nullable=False)
    error_message = Column(Text, nullable=True)  # Error message if query failed
    processing_time_ms = Column(Float, nullable=True)  # Time taken to process query
    session_id = Column(String(255), nullable=True, index=True)  # Conversation session ID
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Relationships
    user = relationship("User")

    __table_args__ = (
        Index('idx_audit_user_created', 'user_id', 'created_at'),
        Index('idx_audit_created', 'created_at'),
    )


# =====================================================
# IDENTITY MANAGEMENT TABLES
# =====================================================

class Identity(Base):
    """Identity layer - tracks known and unknown faces"""
    __tablename__ = "identities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type = Column(SQLEnum(IdentityType), nullable=False, index=True)  # unknown | known
    display_name = Column(String(255), nullable=True, index=True)  # Filled when known
    status = Column(SQLEnum(IdentityStatus), default=IdentityStatus.ACTIVE, nullable=False, index=True)

    # Timestamps (last_seen_at index provided by idx_identity_last_seen)
    first_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Cached fields for performance
    best_snapshot_path = Column(String(512), nullable=True)
    appearances_count = Column(Integer, default=0, nullable=False)
    
    # For merged identities
    merged_into_id = Column(UUID(as_uuid=True), ForeignKey('identities.id'), nullable=True, index=True)

    # Relationships
    # Note: faces relationship has no cascade - we preserve Face records even if Identity is deleted
    # (DB enforces this: faces.identity_id is ON DELETE SET NULL)
    faces = relationship("Face", back_populates="identity", passive_deletes=True)
    appearances = relationship("IdentityAppearance", back_populates="identity", cascade="all, delete-orphan", passive_deletes=True)
    embeddings = relationship("IdentityEmbedding", back_populates="identity", cascade="all, delete-orphan", passive_deletes=True)
    merged_into = relationship("Identity", remote_side=[id], backref="merged_from")

    __table_args__ = (
        Index('idx_identity_type_status', 'type', 'status'),
        Index('idx_identity_last_seen', 'last_seen_at'),
        Index('idx_identity_type_status_last_seen', 'type', 'status', 'last_seen_at'),
    )


class IdentityAppearance(Base):
    """Timeline of identity appearances for dashboard"""
    __tablename__ = "identity_appearances"

    id = Column(Integer, primary_key=True)
    # single-column indexes omitted: idx_appearance_identity_start /
    # idx_appearance_pipeline composites cover these lookups
    identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='CASCADE'), nullable=False)
    pipeline_id = Column(String(255), nullable=False)
    track_id = Column(String(255), nullable=True)  # Person tracking ID

    start_time = Column(DateTime, nullable=False, index=True)
    end_time = Column(DateTime, nullable=True)
    best_snapshot_path = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    identity = relationship("Identity", back_populates="appearances")

    __table_args__ = (
        Index('idx_appearance_identity_start', 'identity_id', 'start_time'),
        Index('idx_appearance_pipeline', 'pipeline_id', 'start_time'),
    )


class IdentityEmbedding(Base):
    """
    Embedding storage with dual backend support:
    - FAISS: Uses faiss_id to reference in-memory FAISS index (fast, requires sync)
    - pgvector: Stores embedding directly in PostgreSQL (simpler, ACID compliant)
    
    The 'embedding' column stores the actual 512-dim vector when using pgvector backend.
    FAISS backend ignores this column and uses faiss_id instead.
    """
    __tablename__ = "identity_embeddings"

    id = Column(Integer, primary_key=True)
    # identity_id single index omitted: idx_embedding_identity_created covers it.
    # Embeddings die with their identity (CASCADE) but survive detection
    # deletion (detection_id is provenance only -> SET NULL).
    identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='CASCADE'), nullable=False)
    detection_id = Column(Integer, ForeignKey('detections.id', ondelete='SET NULL'), nullable=True, index=True)
    pipeline_id = Column(String(255), nullable=False, index=True)

    # FAISS backend fields (faiss_id index provided by idx_embedding_faiss_id)
    faiss_id = Column(Integer, nullable=True)  # Index in FAISS (can be null if using pgvector)
    faiss_index_type = Column(String(50), nullable=True)  # 'known' or 'unknown'
    
    # pgvector backend field - stores actual embedding vector (512-dim for ArcFace)
    # Using ARRAY(Float) as fallback when pgvector is not available
    if PGVECTOR_AVAILABLE:
        embedding = Column(Vector(512), nullable=True)  # pgvector native type
    else:
        embedding = Column(ARRAY(Float), nullable=True)  # Fallback to array (no vector ops)
    
    quality = Column(Float, nullable=True)  # Quality score (blur, size, confidence, pose)
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Relationships
    identity = relationship("Identity", back_populates="embeddings")
    detection = relationship("Detection", back_populates="embeddings")

    __table_args__ = (
        Index('idx_embedding_identity_created', 'identity_id', 'created_at'),
        Index('idx_embedding_faiss_id', 'faiss_id'),
        Index('idx_embedding_faiss_id_type', 'faiss_id', 'faiss_index_type'),
        # Note: HNSW index for pgvector is created via migration, not here
        # because conditional indexes aren't well supported in SQLAlchemy
    )


class MergeSuggestion(Base):
    """Clustering-based merge suggestions"""
    __tablename__ = "merge_suggestions"

    id = Column(Integer, primary_key=True, index=True)
    cluster_id = Column(String(255), nullable=True, index=True)
    identity_ids = Column(JSONB, nullable=False)  # Array of identity UUIDs
    confidence = Column(Float, nullable=False)
    status = Column(SQLEnum(MergeSuggestionStatus), default=MergeSuggestionStatus.PENDING, nullable=False, index=True)
    representative_snapshots = Column(JSONB, nullable=True)  # Array of snapshot paths
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(Integer, ForeignKey('users.id'), nullable=True)

    # Relationships
    reviewed_by_user = relationship("User", foreign_keys=[reviewed_by])
    
    __table_args__ = (
        Index('idx_merge_suggestion_status', 'status', 'created_at'),
    )


class IdentityMerge(Base):
    """Audit log for identity merges"""
    __tablename__ = "identity_merges"

    id = Column(Integer, primary_key=True, index=True)
    from_identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id'), nullable=False, index=True)
    to_identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id'), nullable=False, index=True)
    merged_by = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    merged_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    notes = Column(Text, nullable=True)

    # Relationships
    from_identity = relationship("Identity", foreign_keys=[from_identity_id])
    to_identity = relationship("Identity", foreign_keys=[to_identity_id])
    user = relationship("User")

    __table_args__ = (
        Index('idx_merge_merged_at', 'merged_at'),
        Index('idx_merge_from_to', 'from_identity_id', 'to_identity_id'),
    )


class IdentityAuditLog(Base):
    """
    Comprehensive audit log for all identity management operations.
    Tracks who did what, when, and why for forensic and accountability purposes.
    
    PRIMARY IDENTIFIER: username (required, indexed) - This is the main identifier for accountability.
    IP address is supplementary/optional and should NEVER be used as the primary identifier.
    """
    __tablename__ = "identity_audit_log"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    username = Column(String(100), nullable=False, index=True)  # Denormalized for easier querying
    action_type = Column(String(50), nullable=False, index=True)  # promote, merge, search, view, approve, reject, etc.
    identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id'), nullable=True, index=True)  # Target identity
    related_identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id'), nullable=True)  # For merges, related identity
    action_details = Column(JSONB, nullable=True)  # JSON field for flexible metadata
    before_state = Column(JSONB, nullable=True)  # State before action (for changes)
    after_state = Column(JSONB, nullable=True)  # State after action (for changes)
    ip_address = Column(String(45), nullable=True)  # SUPPLEMENTARY: IPv4 or IPv6 (optional, for context only - NOT for identification)
    user_agent = Column(String(500), nullable=True)  # SUPPLEMENTARY: Browser/client info (optional, for context only)
    success = Column(Boolean, default=True, nullable=False, index=True)
    error_message = Column(Text, nullable=True)  # Error if action failed
    notes = Column(Text, nullable=True)  # Additional notes/justification
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    
    # Relationships
    user = relationship("User")
    identity = relationship("Identity", foreign_keys=[identity_id])
    related_identity = relationship("Identity", foreign_keys=[related_identity_id])
    
    __table_args__ = (
        Index('idx_identity_audit_user_action', 'user_id', 'action_type', 'created_at'),
        Index('idx_identity_audit_identity', 'identity_id', 'created_at'),
        Index('idx_identity_audit_created', 'created_at'),
        Index('idx_identity_audit_action', 'action_type', 'created_at'),
    )


class Setting(Base):
    """System settings table - stores current configuration values"""
    __tablename__ = "settings"
    
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(255), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=True)  # Store as text, parse based on type
    value_type = Column(String(50), nullable=False, default="string")  # string, int, float, bool, json
    category = Column(String(100), nullable=False, index=True)  # server, database, cache, etc.
    description = Column(Text, nullable=True)
    is_sensitive = Column(Boolean, default=False, nullable=False)  # Hide value in UI if sensitive
    is_readonly = Column(Boolean, default=False, nullable=False)  # Cannot be changed via UI
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    __table_args__ = (
        Index('idx_setting_key', 'key'),
        Index('idx_setting_category', 'category'),
    )


class SettingsAuditLog(Base):
    """Audit log for settings changes - tracks who changed what and when"""
    __tablename__ = "settings_audit_log"
    
    id = Column(Integer, primary_key=True, index=True)
    setting_key = Column(String(255), nullable=False, index=True)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    value_type = Column(String(50), nullable=False)
    changed_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    changed_by_username = Column(String(100), nullable=True)  # Store username for reference even if user deleted
    change_reason = Column(Text, nullable=True)  # Optional reason for the change
    # What actually happened: value_saved | value_applied | application_failed |
    # retention_dry_run | retention_executed | setting_reverted
    action = Column(String(50), nullable=True, default="value_saved")
    ip_address = Column(String(45), nullable=True)  # IPv4 or IPv6
    user_agent = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    
    # Relationships
    user = relationship("User")
    
    __table_args__ = (
        Index('idx_settings_audit_setting', 'setting_key', 'created_at'),
        Index('idx_settings_audit_user', 'changed_by_user_id', 'created_at'),
        Index('idx_settings_audit_created', 'created_at'),
    )


class BackgroundTaskHistory(Base):
    """Stores history of background task executions."""
    __tablename__ = "background_task_history"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(String(64), nullable=True, unique=True)  # stable external id, e.g. "retention-a1b2c3d4"
    task_type = Column(String(50), nullable=False, index=True)
    task_name = Column(String(200), nullable=False)
    status = Column(String(20), nullable=False, index=True)  # 'scheduled', 'running', 'completed', 'failed', 'cancelled'
    description = Column(Text, nullable=True)
    scheduled_time = Column(DateTime, nullable=True, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True, index=True)
    duration_seconds = Column(Float, nullable=True)
    progress_percent = Column(Integer, nullable=True)  # 0-100 while running
    success = Column(Boolean, nullable=True)
    details = Column(JSONB, nullable=True)  # live/progress details while running
    result = Column(JSONB, nullable=True)  # final structured result (counts only — never sensitive values)
    retry_count = Column(Integer, nullable=True, default=0)
    max_retries = Column(Integer, nullable=True, default=0)
    error_code = Column(String(50), nullable=True)
    error_message = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    request_id = Column(String(64), nullable=True)
    correlation_id = Column(String(64), nullable=True)
    worker_name = Column(String(100), nullable=True)
    hostname = Column(String(100), nullable=True)
    notify_all_users = Column(Boolean, default=False)  # Whether this task affects all users

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, nullable=True, onupdate=datetime.utcnow)

    __table_args__ = (
        Index('idx_task_history_type_status', 'task_type', 'status'),
        Index('idx_task_history_completed', 'completed_at'),
        Index('idx_task_history_scheduled', 'scheduled_time'),
        Index('idx_task_history_job_id', 'job_id'),
        Index('idx_task_history_correlation', 'correlation_id'),
    )


class SimilarityTrainingData(Base):
    """Training data for ML similarity model - stores user feedback on merge suggestions"""
    __tablename__ = "similarity_training_data"
    
    id = Column(Integer, primary_key=True, index=True)
    identity_id_1 = Column(UUID(as_uuid=True), ForeignKey('identities.id'), nullable=True, index=True)
    identity_id_2 = Column(UUID(as_uuid=True), ForeignKey('identities.id'), nullable=True, index=True)
    
    # Feature values
    embedding_similarity = Column(Float, nullable=False)  # Cosine similarity between embeddings (0.0-1.0)
    pipeline_overlap = Column(Float, nullable=False)  # Ratio of common pipelines (0.0-1.0)
    quality_score_1 = Column(Float, nullable=False)  # Average quality of identity 1 (0.0-1.0)
    quality_score_2 = Column(Float, nullable=False)  # Average quality of identity 2 (0.0-1.0)
    appearances_diff = Column(Float, nullable=False)  # Normalized difference in appearance counts (0.0-1.0)
    is_cross_pipeline = Column(Boolean, nullable=False, default=False)  # Whether identities are from different pipelines
    
    # Target label: 1.0 for approved, 0.0 for rejected, or actual confidence
    label = Column(Float, nullable=False)
    
    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    
    # Relationships
    identity1 = relationship("Identity", foreign_keys=[identity_id_1])
    identity2 = relationship("Identity", foreign_keys=[identity_id_2])
    user = relationship("User")
    
    __table_args__ = (
        Index('idx_similarity_training_label', 'label', 'created_at'),
        Index('idx_similarity_training_created', 'created_at'),
        Index('idx_similarity_training_user', 'created_by_user_id', 'created_at'),
    )


class SimilarityModelRegistry(Base):
    """Versioned registry of similarity-model artifacts.

    The candidate/active separation lives here: training produces a
    'candidate' row + immutable artifact; activation atomically archives
    the previous 'active' row and promotes the candidate. A partial
    unique index (see model registry migration) guarantees at most one
    active row per model_type.
    """
    __tablename__ = "similarity_model_registry"

    id = Column(Integer, primary_key=True, index=True)
    model_type = Column(String(50), nullable=False, default="merge_similarity")
    version = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="candidate")  # candidate|active|archived|rejected|failed
    artifact_name = Column(String(200), nullable=False)   # logical id shown to clients
    artifact_path = Column(Text, nullable=False)          # server-side only, never serialized
    artifact_hash = Column(String(64), nullable=True)     # sha256 of the artifact file
    training_job_id = Column(String(64), nullable=True, index=True)
    dataset_version = Column(String(64), nullable=True)
    dataset_hash = Column(String(64), nullable=True)
    feature_schema_version = Column(String(64), nullable=True)
    seed = Column(Integer, nullable=True)
    metrics = Column(JSONB, nullable=True)
    quality_gates = Column(JSONB, nullable=True)
    comparison = Column(JSONB, nullable=True)             # candidate vs active at training time
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    activated_at = Column(DateTime, nullable=True)
    activated_by = Column(Integer, nullable=True)
    archived_at = Column(DateTime, nullable=True)
    failure_code = Column(String(64), nullable=True)
    notes = Column(Text, nullable=True)

    __table_args__ = (
        Index('idx_model_registry_type_status', 'model_type', 'status'),
        Index('idx_model_registry_created', 'created_at'),
    )


# =====================================================
# ADVANCED SEARCH INTELLIGENCE TABLES
# =====================================================

class WatchlistAlertLevel(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class WatchlistEntryPriority(str, enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class LiveAlertStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    EXPIRED = "expired"
    TRIGGERED = "triggered"


class LiveAlertExpirationType(str, enum.Enum):
    NEVER = "never"
    DATE = "date"
    DETECTIONS = "detections"


class SearchType(str, enum.Enum):
    SINGLE = "single"
    MULTI = "multi"
    BATCH = "batch"


class RelationshipStrength(str, enum.Enum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


class Watchlist(Base):
    """Watchlist definitions for VIP, Threat, POI, etc."""
    __tablename__ = "watchlists"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    # Uniqueness is enforced case-insensitively among NON-deleted watchlists
    # by partial index uq_watchlists_name_live (see watchlist hardening migration)
    name = Column(String(100), nullable=False, index=True)
    description = Column(Text, nullable=True)
    color = Column(String(7), default="#6366f1")  # Hex color for UI
    icon = Column(String(50), default="list")  # Icon identifier
    alert_level = Column(SQLEnum(WatchlistAlertLevel), default=WatchlistAlertLevel.INFO, nullable=False)

    # Notification settings
    notify_dashboard = Column(Boolean, default=True, nullable=False)
    notify_email = Column(Boolean, default=False, nullable=False)
    notify_sms = Column(Boolean, default=False, nullable=False)
    notify_webhook = Column(Boolean, default=False, nullable=False)
    email_recipients = Column(JSONB, nullable=True)  # Array of emails
    sms_recipients = Column(JSONB, nullable=True)  # Array of phone numbers
    webhook_url = Column(Text, nullable=True)

    is_active = Column(Boolean, default=True, nullable=False, index=True)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Optimistic concurrency + soft deletion (watchlist hardening)
    version = Column(Integer, default=1, nullable=False)
    deleted_at = Column(DateTime, nullable=True)
    deleted_by_user_id = Column(Integer, nullable=True)
    deletion_reason = Column(Text, nullable=True)

    # Relationships
    entries = relationship("WatchlistEntry", back_populates="watchlist", cascade="all, delete-orphan")
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        Index('idx_watchlist_name_active', 'name', 'is_active'),
    )


class WatchlistEntry(Base):
    """Identities on watchlists"""
    __tablename__ = "watchlist_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    watchlist_id = Column(UUID(as_uuid=True), ForeignKey('watchlists.id', ondelete='CASCADE'), nullable=False, index=True)
    identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='CASCADE'), nullable=False, index=True)
    priority = Column(SQLEnum(WatchlistEntryPriority), default=WatchlistEntryPriority.NORMAL, nullable=False)
    notes = Column(Text, nullable=True)  # Why they're on the list
    action_instructions = Column(Text, nullable=True)  # What to do when detected
    added_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)  # Optional expiration
    is_active = Column(Boolean, default=True, nullable=False, index=True)

    # Relationships
    watchlist = relationship("Watchlist", back_populates="entries")
    identity = relationship("Identity")
    added_by_user = relationship("User", foreign_keys=[added_by])

    __table_args__ = (
        Index('idx_watchlist_entry_identity', 'identity_id'),
        Index('idx_watchlist_entry_active', 'is_active', 'expires_at'),
        # Unique constraint: one identity per watchlist
        Index('idx_watchlist_entry_unique', 'watchlist_id', 'identity_id', unique=True),
    )


class WatchlistAlert(Base):
    """Watchlist alert history"""
    __tablename__ = "watchlist_alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    watchlist_entry_id = Column(UUID(as_uuid=True), ForeignKey('watchlist_entries.id', ondelete='CASCADE'), nullable=False, index=True)
    triggered_by = Column(String(50), nullable=False)  # "search", "detection", "batch"
    search_id = Column(UUID(as_uuid=True), nullable=True)  # If triggered by search
    detection_id = Column(Integer, ForeignKey('detections.id', ondelete='SET NULL'), nullable=True)  # If triggered by live detection
    similarity_score = Column(Float, nullable=True)
    pipeline_id = Column(String(255), nullable=True)  # Where detected
    snapshot_path = Column(String(512), nullable=True)
    acknowledged = Column(Boolean, default=False, nullable=False)
    acknowledged_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Relationships
    entry = relationship("WatchlistEntry")
    detection = relationship("Detection")
    acknowledged_by_user = relationship("User", foreign_keys=[acknowledged_by])

    __table_args__ = (
        Index('idx_watchlist_alert_entry', 'watchlist_entry_id', 'created_at'),
        Index('idx_watchlist_alert_acknowledged', 'acknowledged', 'created_at'),
    )


class LiveSearchAlert(Base):
    """Live search alerts - notify when a searched face appears again"""
    __tablename__ = "live_search_alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(200), nullable=False)
    identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='CASCADE'), nullable=False, index=True)
    created_by = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    
    # Trigger conditions
    min_similarity = Column(Float, default=0.75, nullable=False)
    pipeline_ids = Column(JSONB, nullable=True)  # null = all pipelines
    time_window_enabled = Column(Boolean, default=False, nullable=False)
    time_window_start = Column(String(5), nullable=True)  # "HH:MM"
    time_window_end = Column(String(5), nullable=True)  # "HH:MM"
    active_days = Column(JSONB, nullable=True)  # [0,1,2,3,4,5,6] = Sun-Sat
    cooldown_minutes = Column(Integer, default=30, nullable=False)
    
    # Notifications
    notify_dashboard = Column(Boolean, default=True, nullable=False)
    notify_email = Column(Boolean, default=False, nullable=False)
    notify_sms = Column(Boolean, default=False, nullable=False)
    notify_webhook = Column(Boolean, default=False, nullable=False)
    email_recipients = Column(JSONB, nullable=True)
    sms_recipients = Column(JSONB, nullable=True)
    webhook_url = Column(Text, nullable=True)
    sound_alert = Column(Boolean, default=True, nullable=False)
    
    # Auto actions
    auto_capture_snapshot = Column(Boolean, default=True, nullable=False)
    auto_record_clip = Column(Boolean, default=False, nullable=False)
    clip_duration_seconds = Column(Integer, default=60, nullable=False)
    
    # Expiration
    expiration_type = Column(SQLEnum(LiveAlertExpirationType), default=LiveAlertExpirationType.NEVER, nullable=False)
    expiration_date = Column(DateTime, nullable=True)
    expiration_detections = Column(Integer, nullable=True)
    
    # Status
    status = Column(SQLEnum(LiveAlertStatus), default=LiveAlertStatus.ACTIVE, nullable=False, index=True)
    triggers_count = Column(Integer, default=0, nullable=False)
    last_triggered_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    identity = relationship("Identity")
    creator = relationship("User", foreign_keys=[created_by])
    triggers = relationship("LiveAlertTrigger", back_populates="alert", cascade="all, delete-orphan")

    __table_args__ = (
        Index('idx_live_alert_identity', 'identity_id'),
        Index('idx_live_alert_status', 'status'),
        Index('idx_live_alert_creator', 'created_by'),
    )


class LiveAlertTrigger(Base):
    """History of live alert triggers"""
    __tablename__ = "live_alert_triggers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    alert_id = Column(UUID(as_uuid=True), ForeignKey('live_search_alerts.id', ondelete='CASCADE'), nullable=False, index=True)
    detection_id = Column(Integer, ForeignKey('detections.id', ondelete='SET NULL'), nullable=True)
    pipeline_id = Column(String(255), nullable=True)
    similarity_score = Column(Float, nullable=True)
    snapshot_path = Column(String(512), nullable=True)
    clip_path = Column(String(512), nullable=True)
    acknowledged = Column(Boolean, default=False, nullable=False)
    acknowledged_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Relationships
    alert = relationship("LiveSearchAlert", back_populates="triggers")
    detection = relationship("Detection")
    acknowledged_by_user = relationship("User", foreign_keys=[acknowledged_by])

    __table_args__ = (
        Index('idx_alert_trigger_alert', 'alert_id', 'created_at'),
        Index('idx_alert_trigger_alert_ack', 'alert_id', 'acknowledged', 'created_at'),
        # Idempotency: one trigger per (alert, detection). Partial unique index
        # (WHERE detection_id IS NOT NULL) is created by migration e2f3a4b5c6d7.
    )


class LiveAlertAuditLog(Base):
    """Audit trail for live-alert lifecycle actions.

    Actions: alert_created | alert_updated | alert_paused | alert_resumed |
    alert_deleted | trigger_acknowledged | bulk_acknowledged | channel_test |
    delivery_failure. `details` holds counts/ids only — never tokens,
    embeddings, cookies or other sensitive values.
    """
    __tablename__ = "live_alert_audit_log"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=True)
    username = Column(String(100), nullable=True)
    alert_id = Column(UUID(as_uuid=True), nullable=True)
    action = Column(String(50), nullable=False)
    details = Column(JSONB, nullable=True)
    result = Column(String(20), nullable=True)  # success | failed | partial
    request_id = Column(String(64), nullable=True)
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index('idx_la_audit_alert', 'alert_id', 'created_at'),
        Index('idx_la_audit_user', 'user_id', 'created_at'),
    )


class SearchHistory(Base):
    """Search history for audit and rerun capability"""
    __tablename__ = "search_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    search_type = Column(SQLEnum(SearchType), nullable=False)
    
    # Search parameters
    scope = Column(String(20), nullable=True)  # "known", "unknown", "both"
    top_k = Column(Integer, nullable=True)
    filters = Column(JSONB, nullable=True)  # date_from, date_to, pipeline_id, etc.
    exclude_identity_ids = Column(JSONB, nullable=True)  # Array of excluded UUIDs
    exclude_watchlist_ids = Column(JSONB, nullable=True)  # Array of excluded watchlist IDs
    
    # Input info
    input_image_hash = Column(String(64), nullable=True)  # SHA256 of uploaded image
    input_faces_count = Column(Integer, nullable=True)
    input_quality_scores = Column(JSONB, nullable=True)  # Array of quality scores
    
    # Results summary
    results_count = Column(Integer, nullable=True)
    results_summary = Column(JSONB, nullable=True)  # Top matches with scores
    watchlist_alerts_count = Column(Integer, default=0, nullable=False)
    unique_identities_count = Column(Integer, nullable=True)
    
    # Metadata
    processing_time_ms = Column(Integer, nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Relationships
    user = relationship("User")

    __table_args__ = (
        Index('idx_search_history_user', 'user_id', 'created_at'),
        Index('idx_search_history_date', 'created_at'),
    )


class SavedSearch(Base):
    """Saved search configurations for reuse"""
    __tablename__ = "saved_searches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    
    # Search configuration
    search_type = Column(SQLEnum(SearchType), nullable=True)
    scope = Column(String(20), nullable=True)
    top_k = Column(Integer, nullable=True)
    min_quality = Column(Float, nullable=True)
    filters = Column(JSONB, nullable=True)
    exclude_identity_ids = Column(JSONB, nullable=True)
    exclude_watchlist_ids = Column(JSONB, nullable=True)
    
    # Optional: saved embedding hash for re-search
    embedding_hash = Column(String(64), nullable=True)
    
    use_count = Column(Integer, default=0, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User")

    __table_args__ = (
        Index('idx_saved_search_user', 'user_id'),
    )


class IdentityRelationship(Base):
    """Cached co-appearance relationships between identities"""
    __tablename__ = "identity_relationships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    identity_id_1 = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='CASCADE'), nullable=False, index=True)
    identity_id_2 = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='CASCADE'), nullable=False, index=True)
    
    co_appearance_count = Column(Integer, default=0, nullable=False)
    co_appearance_percentage = Column(Float, nullable=True)  # % of identity_1's appearances
    relationship_strength = Column(SQLEnum(RelationshipStrength), nullable=True)
    common_pipelines = Column(JSONB, nullable=True)  # Array of pipeline IDs
    common_time_patterns = Column(JSONB, nullable=True)  # Time pattern analysis
    
    first_co_appearance = Column(DateTime, nullable=True)
    last_co_appearance = Column(DateTime, nullable=True)
    
    calculated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    identity1 = relationship("Identity", foreign_keys=[identity_id_1])
    identity2 = relationship("Identity", foreign_keys=[identity_id_2])

    __table_args__ = (
        Index('idx_relationship_identity1', 'identity_id_1'),
        Index('idx_relationship_identity2', 'identity_id_2'),
        # Ensure consistent ordering (id1 < id2) with unique constraint
        Index('idx_relationship_pair', 'identity_id_1', 'identity_id_2', unique=True),
    )


# =====================================================
# SQL AGENT USER QUERY HISTORY AND MEMORY TABLES
# =====================================================

class UserQueryHistory(Base):
    """
    Stores all user queries and AI agent responses for history display and context retrieval.
    Each user has their own query history that persists across sessions.
    """
    __tablename__ = "user_query_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    session_id = Column(String(255), nullable=True, index=True, comment="Groups queries into conversation sessions")
    
    # Query and response
    query_text = Column(Text, nullable=False, comment="The user's question/query")
    response_text = Column(Text, nullable=True, comment="The AI agent's response (may be None if query failed)")
    
    # Timestamps
    query_timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    response_timestamp = Column(DateTime, nullable=True, comment="When the response was generated")
    
    # Query metadata (renamed from 'metadata' to avoid SQLAlchemy reserved word conflict)
    query_metadata = Column(JSONB, nullable=True, comment="Additional metadata: pipeline_ids, tables_queried, SQL generated, etc.")
    success = Column(Boolean, default=True, nullable=False, index=True, comment="Whether the query was successful")
    error_message = Column(Text, nullable=True, comment="Error message if query failed")
    processing_time_ms = Column(Float, nullable=True, comment="Time taken to process query in milliseconds")
    
    # Relationships
    user = relationship("User", backref="query_history")

    __table_args__ = (
        Index('idx_query_user_timestamp', 'user_id', 'query_timestamp'),
        Index('idx_query_session', 'session_id', 'query_timestamp'),
        Index('idx_query_user_session', 'user_id', 'session_id', 'query_timestamp'),
        Index('idx_query_success', 'success', 'query_timestamp'),
    )


class UserConversationSession(Base):
    """
    Groups queries into conversation sessions for better organization.
    Users can have multiple active sessions and switch between them.
    """
    __tablename__ = "user_conversation_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    session_id = Column(String(255), unique=True, nullable=False, index=True, comment="Unique session identifier")
    session_name = Column(String(255), nullable=True, comment="Optional user-defined session name")
    
    # Session lifecycle
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    last_activity_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False, index=True, comment="Whether the session is currently active")
    
    # Context summary for memory
    context_summary = Column(Text, nullable=True, comment="Text summary of conversation context for quick retrieval")
    query_count = Column(Integer, default=0, nullable=False, comment="Number of queries in this session")
    
    # Relationships
    user = relationship("User", backref="conversation_sessions")

    __table_args__ = (
        Index('idx_session_user_active', 'user_id', 'is_active', 'last_activity_at'),
        Index('idx_session_user_started', 'user_id', 'started_at'),
    )


class UserConversationMemory(Base):
    """
    Stores important facts, preferences, and context for each user.
    Enables personalized, context-aware responses by remembering user-specific information.
    """
    __tablename__ = "user_conversation_memory"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    
    # Memory classification
    memory_type = Column(SQLEnum(MemoryType), nullable=False, index=True, comment="Type of memory: fact, preference, context, pattern")
    memory_key = Column(String(255), nullable=False, index=True, comment="Key identifier for the memory (e.g., 'user_interests', 'common_queries')")
    memory_value = Column(JSONB, nullable=False, comment="The actual memory content (flexible JSON structure)")
    
    # Memory importance and lifecycle
    importance_score = Column(Integer, default=50, nullable=False, index=True, comment="Importance score 0-100, determines retention priority")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    last_accessed_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True, comment="Last time this memory was used")
    access_count = Column(Integer, default=0, nullable=False, comment="Number of times this memory has been accessed")
    expires_at = Column(DateTime, nullable=True, index=True, comment="Optional expiration date for temporary context")
    
    # Source tracking
    source_session_id = Column(String(255), nullable=True, comment="Session where this memory was created")
    source_query_id = Column(Integer, ForeignKey('user_query_history.id', ondelete='SET NULL'), nullable=True, comment="Query that created this memory")
    
    # Relationships
    user = relationship("User", backref="conversation_memory")
    source_query = relationship("UserQueryHistory", foreign_keys=[source_query_id])

    __table_args__ = (
        Index('idx_memory_user_type', 'user_id', 'memory_type', 'importance_score'),
        Index('idx_memory_user_key', 'user_id', 'memory_key'),
        Index('idx_memory_user_accessed', 'user_id', 'last_accessed_at'),
        Index('idx_memory_expires', 'expires_at'),
        Index('idx_memory_user_importance', 'user_id', 'importance_score', 'last_accessed_at'),
    )


class UserQueryEmbedding(Base):
    """
    Stores vector embeddings of user queries for semantic search.
    Enables finding similar past queries to provide context-aware responses.
    Uses pgvector if available, otherwise stores as JSONB array.
    """
    __tablename__ = "user_query_embeddings"

    id = Column(Integer, primary_key=True, index=True)
    query_history_id = Column(Integer, ForeignKey('user_query_history.id', ondelete='CASCADE'), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    
    # Embedding storage - use pgvector if available, otherwise JSONB
    if PGVECTOR_AVAILABLE:
        embedding = Column(Vector(384), nullable=True, comment="Vector embedding stored as pgvector (384-dim for sentence-transformers/all-MiniLM-L6-v2)")
    else:
        embedding = Column(JSONB, nullable=True, comment="Vector embedding stored as JSONB array (fallback when pgvector not available)")
    
    # Embedding metadata
    embedding_model = Column(String(100), nullable=True, comment="Model used to generate embedding (e.g., 'sentence-transformers/all-MiniLM-L6-v2')")
    embedding_dimensions = Column(Integer, nullable=True, comment="Number of dimensions in the embedding vector")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    
    # Relationships
    query_history = relationship("UserQueryHistory", backref="embedding")
    user = relationship("User", backref="query_embeddings")

    __table_args__ = (
        Index('idx_embedding_user', 'user_id', 'created_at'),
        Index('idx_embedding_query', 'query_history_id'),
        Index('idx_embedding_model', 'embedding_model'),
    )
