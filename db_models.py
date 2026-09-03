"""
Database Models for Face Recognition Service
Stores detection results in PostgreSQL for persistence
"""
from datetime import datetime
from typing import Optional, List
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey, Index, Boolean, Enum as SQLEnum, text, CheckConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import backref, relationship
from sqlalchemy.dialects.postgresql import JSONB as _PostgresJSONB, UUID, ARRAY
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

class JSONB(_PostgresJSONB):
    """JSONB where a Python ``None`` is stored as SQL NULL.

    SQLAlchemy's default for a JSON/JSONB column writes the JSON *literal*
    ``null`` instead, and the two are not interchangeable in the database:

        SELECT count(*) FROM t WHERE col IS NULL      -- misses JSON null
        SELECT count(*) FROM t WHERE col IS NOT NULL  -- COUNTS JSON null

    So a column meaning "nothing here" reads back as a present value to every
    aggregate, filter and dashboard that asks in SQL. It had already happened
    474 times across ten columns. The clearest evidence it was accidental:
    ``ml_predictions.missing_features`` held 241 SQL NULLs while its sibling
    ``ml_shadow_comparisons.missing_features`` held 241 JSON nulls — same
    subsystem, same rows, same intent, written two different ways. The
    inference writer dodged it by omitting the key entirely and left a comment
    warning about the literal; the shadow writer said
    ``missing_features=result.missing_features or None`` and got the trap.

    Applying it on the type rather than at each of the ~70 column declarations
    means a new column, or a new writer passing None, cannot reintroduce it.
    Code that genuinely wants a stored JSON null can still ask for it
    explicitly with ``sqlalchemy.JSON.NULL``; nothing in this schema does.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("none_as_null", True)
        super().__init__(*args, **kwargs)


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
    INVALIDATED = "invalidated"  # an identity in identity_ids was merged/deleted/retired


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
    timezone = Column(String(64), nullable=True,
                      comment="IANA timezone for business-hour evaluation (e.g. 'Asia/Beirut'); "
                              "NULL falls back to DEFAULT_SITE_TIMEZONE. Timestamps stay UTC in the DB.")

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
    # RESTRICT: a camera with evidence is deactivated (is_active), never
    # hard-deleted — one delete policy for every evidence table (c2d3e4f5a6b7).
    pipeline_id = Column(String(255), ForeignKey('pipelines.pipeline_id', ondelete='RESTRICT'), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Image metadata
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
    # Set on a bootstrapped administrator so the seeded credential cannot become
    # a permanent one. server_default matters: older code paths still INSERT
    # without naming this column.
    must_change_password = Column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    password_changed_at = Column(DateTime, nullable=True)
    # Bumped whenever role, can_use_chatbot, is_active or pipeline access
    # changes. Long-lived connections (SQL-agent WebSocket, in-flight SSE)
    # compare this integer instead of re-resolving permissions per message,
    # which is what lets a revocation reach an already-open session.
    permissions_version = Column(
        Integer, default=1, nullable=False, server_default=text("1")
    )

    # Relationships
    # passive_deletes=True: user_pipeline_access.user_id now carries ON DELETE
    # CASCADE (migration b0c1d2e3f4a5), so the ORM no longer needs to SELECT the
    # rows to delete them. delete-orphan keeps in-session semantics correct:
    # loaded children are DELETEd, never nullified.
    pipeline_access = relationship("UserPipelineAccess", back_populates="user",
                                   cascade="all, delete-orphan", passive_deletes=True)

    __table_args__ = (
        Index('idx_user_username_active', 'username', 'is_active'),
        Index('idx_user_role', 'role'),
    )


class DeletedUser(Base):
    """Tombstone for a permanently deleted human account.

    When a user is deleted, every preserved row (conversations, query history,
    audit logs, ...) has its live FK set to NULL and keeps the old numeric id in
    a `historical_*` column. This table is what those ids resolve against —
    an id → username map, deliberately nothing more.

    NOT an account. No email, full name, password hash, role or permissions are
    retained: this is traceability metadata, not a recoverable user. No FK
    points here (soft reference by id, like ml_audit_log.actor_user_id), so the
    tombstone can never block any other operation.

    Why the map is one-directional: `users.username` is unique only among LIVE
    rows. A deleted username can be re-registered, so username → id lookups can
    resolve to the wrong person; id → username cannot, because PostgreSQL
    sequences never reuse ids. For the same reason a recreated account with the
    same username is a NEW account and inherits nothing — matching a
    historical_* id or author_username must never grant access or ownership.

    deleted_by_* is a soft reference: the deleting administrator may themselves
    be deleted later, and this record must not prevent that or lose meaning.
    """
    __tablename__ = "deleted_users"

    user_id = Column(Integer, primary_key=True,
                     comment="The original users.id; unique forever because sequences never reuse ids")
    username = Column(String(100), nullable=False)      # idx_deleted_users_username (b0c1d2e3f4a5)
    deleted_at = Column(DateTime, default=datetime.utcnow, nullable=False)   # idx_deleted_users_deleted_at
    deleted_by_user_id = Column(Integer, nullable=True,
                                comment="Soft reference; no FK by design")
    deleted_by_username = Column(String(100), nullable=True)

    __table_args__ = (
        Index('idx_deleted_users_username', 'username'),
        Index('idx_deleted_users_deleted_at', 'deleted_at'),
    )


class UserAuthorizationAuditLog(Base):
    """Who changed whose authorization, from what, to what, and when.

    Previously nothing recorded this — the only trace was DEBUG-level log lines
    in UserService.update_user, which are not persisted in production, so a
    disputed permission change could not be reconstructed.

    Written inside the same transaction as the change itself, so an audit row
    cannot exist for a change that rolled back, nor a change exist without its
    audit row. Shaped after SettingsAuditLog rather than inventing a second
    audit pattern.

    Never records passwords, tokens or cookies.
    """
    __tablename__ = "user_authorization_audit_log"

    id = Column(Integer, primary_key=True, index=True)

    # Usernames are denormalized so the record survives user deletion — the
    # same reasoning as SettingsAuditLog.changed_by_username.
    target_user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'),
                            nullable=True, index=True)
    target_username = Column(String(100), nullable=True)
    changed_by_user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'),
                                nullable=True, index=True)
    changed_by_username = Column(String(100), nullable=True)

    # authorization_updated | user_created | user_blocked | user_unblocked |
    # user_deleted | password_reset
    action = Column(String(50), nullable=False, default="authorization_updated")

    # A NULL old/new pair means that field was not part of this change.
    old_role = Column(String(50), nullable=True)
    new_role = Column(String(50), nullable=True)
    old_can_use_chatbot = Column(Boolean, nullable=True)
    new_can_use_chatbot = Column(Boolean, nullable=True)
    old_is_active = Column(Boolean, nullable=True)
    new_is_active = Column(Boolean, nullable=True)
    old_pipeline_ids = Column(JSONB, nullable=True)
    new_pipeline_ids = Column(JSONB, nullable=True)

    permissions_version = Column(Integer, nullable=True)

    request_id = Column(String(64), nullable=True)
    ip_address = Column(String(45), nullable=True)  # IPv4 or IPv6
    user_agent = Column(Text, nullable=True)
    change_reason = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    target_user = relationship("User", foreign_keys=[target_user_id])
    changed_by = relationship("User", foreign_keys=[changed_by_user_id])

    __table_args__ = (
        Index('idx_user_authz_audit_target', 'target_user_id', 'created_at'),
        Index('idx_user_authz_audit_actor', 'changed_by_user_id', 'created_at'),
        Index('idx_user_authz_audit_created', 'created_at'),
    )


class UserPipelineAccess(Base):
    """Controls which pipelines a user can access"""
    __tablename__ = "user_pipeline_access"

    id = Column(Integer, primary_key=True, index=True)
    # CASCADE: access grants are meaningless without the account. This used to
    # have no ondelete (NO ACTION), which is why delete_user() deleted these
    # rows explicitly in Python before the DB owned it.
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
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
    # SET NULL: audit history must survive account deletion. This FK was
    # NO ACTION and NOT NULL, and was the first constraint PostgreSQL raised
    # when deleting any user who had ever used the chatbot. `username` keeps the
    # record readable; historical_user_id keeps it numerically traceable.
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    historical_user_id = Column(Integer, nullable=True,
                                comment="users.id at write/deletion time; survives account deletion")
    username = Column(String(100), nullable=False, index=True)  # Denormalized for easier querying
    query = Column(Text, nullable=False)
    response = Column(Text, nullable=True)  # May be None if query failed
    success = Column(Boolean, default=True, nullable=False)
    error_message = Column(Text, nullable=True)  # Error message if query failed
    processing_time_ms = Column(Float, nullable=True)  # Time taken to process query
    session_id = Column(String(255), nullable=True, index=True)  # intentional historical ref, no FK: written before the session row exists
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

    # The organisation's own identifier for this person — an employee or badge
    # number. Optional: most identities never have one, and an unknown face
    # cannot. Display form plus a normalized uniqueness key, the same pair used
    # by PendingEnrollment.display_name/display_name_key, so "emp-001" and
    # "EMP-001" cannot both exist and make the code ambiguous as a lookup.
    #
    # Uniqueness is enforced by a PARTIAL index (WHERE person_code_key IS NOT
    # NULL): a plain unique index would be satisfied by many NULLs in
    # PostgreSQL, but a partial one states the intent — codes are unique when
    # present, and absence is not a value that can collide.
    person_code = Column(String(100), nullable=True)
    person_code_key = Column(String(100), nullable=True)

    # Timestamps (last_seen_at index provided by idx_identity_last_seen)
    first_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    # Deliberately NO onupdate. This column means "when a camera last saw this
    # person", so it is set explicitly by the recognition path
    # (IdentityService._update_identity_seen) and by merge consolidation, and
    # by nothing else.
    #
    # With onupdate=utcnow it was restamped by ANY write to the row — adding an
    # enrollment photo, changing best_snapshot_path, a status change, a rename
    # — so a person last seen a week ago read as "seen just now" while
    # appearances_count still showed the old total. Every consumer of
    # last_seen_at (dashboard recency, the enrollment review card, and the
    # inactivity sweep in identity_retention) was reading administrative
    # activity as surveillance evidence.
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
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
    images = relationship("IdentityImage", back_populates="identity", cascade="all, delete-orphan", passive_deletes=True)
    merged_into = relationship("Identity", remote_side=[id], backref="merged_from")

    __table_args__ = (
        Index('idx_identity_type_status', 'type', 'status'),
        Index('idx_identity_last_seen', 'last_seen_at'),
        Index('idx_identity_type_status_last_seen', 'type', 'status', 'last_seen_at'),
        # Unique only where a code exists. postgresql_where keeps NULLs out of
        # the index entirely, so any number of identities may have no code
        # while no two may share one.
        Index('uq_identity_person_code_key', 'person_code_key',
              unique=True, postgresql_where=text('person_code_key IS NOT NULL')),
    )


class IdentityAppearance(Base):
    """Timeline of identity appearances for dashboard"""
    __tablename__ = "identity_appearances"

    id = Column(Integer, primary_key=True)
    # single-column indexes omitted: idx_appearance_identity_start /
    # idx_appearance_pipeline composites cover these lookups
    identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='CASCADE'), nullable=False)
    pipeline_id = Column(String(255), ForeignKey('pipelines.pipeline_id', ondelete='RESTRICT'),
                         nullable=False)  # a camera sighting; RESTRICT — evidence blocks hard delete
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
        # Serves the Advanced Search camera filter's correlated EXISTS, which
        # drives from a small candidate set of identities. Neither index above
        # covers it: the first lacks pipeline_id (heap fetch per candidate),
        # the second lacks identity_id (wrong driver). See alembic revision
        # e4f5a6b7c8d9.
        Index('idx_appearance_identity_pipeline', 'identity_id', 'pipeline_id'),
        # ML collector keyset order: WHERE created_at > x [AND (created_at, id) > cursor]
        # ORDER BY created_at, id  (rev f2b7c9d4e1a6, built CONCURRENTLY)
        Index('idx_appearance_created_at_id', 'created_at', 'id'),
    )


class IdentityEmbedding(Base):
    """
    Embedding storage. The `embedding` column IS the authoritative vector for
    both backends: pgvector searches it in place, and the FlatFaissIndex keys
    its in-memory copy by this row's id and rebuilds entirely from this table
    (`rebuild_from_db`). The old positional `faiss_id` handle — a second,
    sync-prone identity for the same vector — was dropped in c5d6e7f8a9b0.
    """
    __tablename__ = "identity_embeddings"

    id = Column(Integer, primary_key=True)
    # identity_id single index omitted: idx_embedding_identity_created covers it.
    # Embeddings die with their identity (CASCADE) but survive detection
    # deletion (detection_id is provenance only -> SET NULL).
    identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='CASCADE'), nullable=False)
    detection_id = Column(Integer, ForeignKey('detections.id', ondelete='SET NULL'), nullable=True, index=True)
    # NULL = not from a camera (enrolled photo → image_id, or preloaded gallery);
    # a real value is a FK to the camera and RESTRICTs its hard delete.
    pipeline_id = Column(String(255), ForeignKey('pipelines.pipeline_id', ondelete='RESTRICT'),
                         nullable=True, index=True)

    # Partitions vectors into the known/unknown search spaces. The NAME is a
    # relic of the retired positional-FAISS design (whose faiss_id column was
    # dropped in migration c5d6e7f8a9b0), but the DATA is load-bearing:
    # promotion, clustering and the loaders all filter on it.
    faiss_index_type = Column(String(50), nullable=True)  # 'known' or 'unknown'
    
    # pgvector backend field - stores actual embedding vector (512-dim for ArcFace)
    # Using ARRAY(Float) as fallback when pgvector is not available
    if PGVECTOR_AVAILABLE:
        embedding = Column(Vector(512), nullable=True)  # pgvector native type
    else:
        embedding = Column(ARRAY(Float), nullable=True)  # Fallback to array (no vector ops)
    
    quality = Column(Float, nullable=True)  # Quality score (blur, size, confidence, pose)

    # THE authoritative per-vector synchronization state between PostgreSQL (the
    # source of truth) and the disposable search index.
    #
    #   pending -> synced   successful synchronization
    #   pending -> failed   synchronization failed
    #   failed  -> pending  retry requested
    #   synced  -> pending  reconciliation found it missing/stale/mismatched
    #
    # Deletion is NOT a state: removing an embedding deletes this row and the
    # index entry. A committed vector is never rolled back because the index
    # failed — only this column changes.
    vector_index_sync_state = Column(String(16), default='pending', nullable=False)

    # Which embedding model produced this vector. Reconciliation compares it, so
    # a model change invalidates stale index entries. NULL for historical rows —
    # unknown provenance is recorded honestly, never fabricated.
    embedding_model_version = Column(String(64), nullable=True)

    # Which scorer produced `quality`. NULL means the legacy scorer, whose value
    # was `0.3 + 0.2 * upstream_detector_confidence` and carried no image-quality
    # information at all. Values from different scorers are NEVER compared
    # against each other — see create_appearance and the clustering features.
    quality_scorer_version = Column(String(32), nullable=True)

    # Source enrollment image, when this embedding came from an uploaded photo.
    # NULLABLE by design: every embedding produced before identity_images
    # existed (and every embedding derived from a live detection rather than an
    # upload) legitimately has no source image row.
    image_id = Column(Integer, ForeignKey('identity_images.id', ondelete='SET NULL'),
                      nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Relationships
    identity = relationship("Identity", back_populates="embeddings")
    detection = relationship("Detection", back_populates="embeddings")
    image = relationship("IdentityImage", back_populates="embeddings")

    __table_args__ = (
        Index('idx_embedding_identity_created', 'identity_id', 'created_at'),
        # Reconciliation scans for work by state.
        Index('idx_embedding_sync_state', 'vector_index_sync_state'),
        # Note: HNSW index for pgvector is created via migration, not here
        # because conditional indexes aren't well supported in SQLAlchemy
    )


class IdentityImage(Base):
    """An enrollment photo belonging to one identity.

    One Identity -> many IdentityImage -> one IdentityEmbedding per usable
    image. Files live under FACES_DIR/<identity_uuid>/, keyed by the immutable
    UUID rather than the display name, so renaming a person never moves or
    renames their folder and two people may share a display name.

    storage_path holds a NORMALIZED RELATIVE path ('storage/faces/<uuid>/...').
    Absolute filesystem paths are never stored here and never serialized.
    """
    __tablename__ = "identity_images"

    id = Column(Integer, primary_key=True)
    identity_id = Column(UUID(as_uuid=True),
                         ForeignKey('identities.id', ondelete='CASCADE'),
                         nullable=False)

    # Server-controlled relative path. The uploaded filename is NEVER used as
    # a path component; original_filename keeps it for display/audit only.
    storage_path = Column(String(512), nullable=False)
    original_filename = Column(String(255), nullable=True)

    # SHA-256 of the ORIGINAL uploaded bytes — the duplicate-upload key.
    file_checksum = Column(String(64), nullable=False)
    content_type = Column(String(100), nullable=True)
    file_size = Column(Integer, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    quality_score = Column(Float, nullable=True)
    # Provenance for quality_score, same meaning as on identity_embeddings.
    quality_scorer_version = Column(String(32), nullable=True)

    is_primary = Column(Boolean, default=False, nullable=False)

    # How this photo entered the system, for audit:
    #   upload       full photo uploaded by an administrator
    #   cropped_face pre-cropped face uploaded with is_face_image=true
    #   promotion    copied in when an unknown identity was promoted to known
    # Nullable: rows created before this column existed have no honest value.
    source_type = Column(String(32), nullable=True)

    # pending | completed | failed  (a row only reaches 'completed' once its
    # file, embedding and commit all succeeded)
    processing_status = Column(String(32), default='pending', nullable=False)
    failure_reason = Column(String(255), nullable=True)

    # NOTE: an image-level faiss_sync_state used to live here. It was written and
    # never read, and duplicated state that belongs to the vector, not the photo.
    # The authoritative per-vector state is
    # identity_embeddings.vector_index_sync_state.

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    identity = relationship("Identity", back_populates="images")
    embeddings = relationship("IdentityEmbedding", back_populates="image")

    __table_args__ = (
        # The same photo cannot be enrolled twice for the same identity.
        Index('uq_identity_image_checksum', 'identity_id', 'file_checksum', unique=True),
        Index('idx_identity_image_identity', 'identity_id'),
        Index('idx_identity_image_created', 'created_at'),
        Index('idx_identity_image_status', 'processing_status'),
        Index('idx_identity_image_primary', 'is_primary'),
        Index('idx_identity_image_checksum', 'file_checksum'),
        Index('idx_identity_image_source_type', 'source_type'),
        # At most ONE primary image per identity, enforced by the database
        # rather than by application convention.
        Index('uq_identity_image_one_primary', 'identity_id',
              unique=True, postgresql_where=text('is_primary')),
    )


class PendingEnrollment(Base):
    """An upload parked for an administrator's identity decision.

    Enrollment mints a new identity UUID for any name it has not seen before.
    Without a similarity check that meant a second photo of an already-enrolled
    person, uploaded under a new spelling, became a SECOND UUID holding a second
    embedding of the same face — and recognition then answered with whichever
    vector scored higher. This row is what lets the server stop and ask.

    While a row lives here, NOTHING durable exists for the upload: no identity,
    no identity_images row, no identity_embeddings row, no gallery folder, no
    vector-index entry. The photo sits under STORAGE_DIR/pending — deliberately
    outside FACES_DIR, which holds one directory per identity UUID and nothing
    else — and moves into the gallery only on confirmation.

    The row is CONSUMED BY DELETE, which is what makes one-time use race-free
    without a flag or a lock; see claim_pending_enrollment in
    backend/core/enrollment_service.py.
    """
    __tablename__ = "pending_enrollments"

    id = Column(Integer, primary_key=True)

    # SHA-256 of the upload token. The token itself is returned to the client
    # once and is never stored, so a database reader cannot approve anything.
    token_hash = Column(String(64), nullable=False)

    # Bound to the uploading administrator: nobody else may approve or reject.
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'),
                     nullable=False)

    # What the operator typed, and its normalized/casefolded lookup key. Both
    # are bound so a confirmation cannot quietly retarget a different person
    # than the one whose candidates were reviewed.
    display_name = Column(String(255), nullable=False)
    display_name_key = Column(String(255), nullable=False)

    # Relative path, same convention as IdentityImage.storage_path.
    storage_path = Column(String(512), nullable=False)
    original_filename = Column(String(255), nullable=True)

    # SHA-256 of the ORIGINAL bytes, re-verified at confirmation so the file
    # that gets enrolled is provably the file that was reviewed.
    file_checksum = Column(String(64), nullable=False)
    content_type = Column(String(100), nullable=True)
    file_size = Column(Integer, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)

    # Replayed verbatim at confirmation: is_face_image selects the detector's
    # padded-retry branch, so re-running detection without it could refuse a
    # photo the review phase already accepted.
    is_face_image = Column(Boolean, default=False, nullable=False)

    # Which models produced the embedding that ranked the candidates. The
    # embedding is recomputed at confirmation; if the weights changed in
    # between, the vector being approved is not the one that was reviewed.
    embedding_model_version = Column(String(64), nullable=True)
    detection_model_version = Column(String(64), nullable=True)

    # 'strong' | 'uncertain'. Nothing is parked when no candidate matched —
    # that upload enrolls directly, exactly as it did before the gate existed.
    decision = Column(String(16), nullable=False)
    top_similarity = Column(Float, nullable=True)

    # An exact-checksum hit on ANOTHER identity: deterministic duplicate
    # evidence, computed before approximate similarity is consulted at all.

    # The frozen list actually offered. A confirmation naming an identity that
    # is not in here is refused, so the choice cannot be widened client-side.
    candidates = Column(JSONB, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index('uq_pending_enrollment_token', 'token_hash', unique=True),
        Index('idx_pending_enrollment_expires', 'expires_at'),
        Index('idx_pending_enrollment_user', 'user_id'),
    )


class WebhookCredential(Base):
    """A named ingest credential issued to one external system.

    Mints like PendingEnrollment above — a SHA-256 of a token the server returns
    exactly ONCE and never stores — and revokes the same way: the row is
    CONSUMED BY DELETE.

    There is deliberately no `revoked_at` and no `expires_at`. A flag would
    create a second "unusable" state that the verifier has to tell apart from
    "absent", and any distinction the verifier can make is a distinction the 401
    can leak. Deletion collapses both into one state, so a revoked credential is
    indistinguishable from a wrong one.

    NOT bound to a pipeline. batch_writer inserts the `pipelines` row on the
    FIRST detection (see backend/security/webhook_auth.py), so at issuance time
    there is often no pipeline to bind to. This names the SENDER, not the camera.

    The environment credentials (WEBHOOK_API_KEYS / WEBHOOK_AUTH_TOKEN) keep
    working alongside these as the break-glass path, so a database outage cannot
    lock every camera out and startup never depends on this table.
    """
    __tablename__ = "webhook_credentials"

    id = Column(Integer, primary_key=True)

    # SHA-256 hex of the token. The token is returned once and never stored, so
    # a database reader cannot post frames.
    token_hash = Column(String(64), nullable=False)

    # What the operator typed, and its normalized/casefolded uniqueness key —
    # the same pair as PendingEnrollment.display_name/display_name_key.
    # Uniqueness is on the KEY so "Acme VMS" and "acme  vms" cannot both exist
    # and leave a log line ambiguous about which credential was used.
    name = Column(String(100), nullable=False)
    name_key = Column(String(100), nullable=False)

    # SET NULL, not CASCADE — the one deliberate divergence from the
    # PendingEnrollment template, and it is load-bearing. A pending enrollment
    # is meaningless without its uploader. An ingest credential belongs to a
    # camera fleet: cascading would mean deactivating a departing employee's
    # account silently blacks out every camera they provisioned, discovered as
    # an outage rather than as a decision.
    created_by_user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'),
                                nullable=True)
    # Denormalized so attribution survives that SET NULL, as SettingsAuditLog does.
    created_by_username = Column(String(100), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Advisory, not authoritative: written by a throttled batch flush on the
    # cache-refresh cycle, so it lags real use by up to
    # WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS. It answers "is anything still using
    # this, is it safe to delete", which does not need second precision. A write
    # per frame would be ~50/second onto a handful of rows.
    last_used_at = Column(DateTime, nullable=True)

    creator = relationship("User", foreign_keys=[created_by_user_id])

    __table_args__ = (
        Index('uq_webhook_credential_token', 'token_hash', unique=True),
        Index('uq_webhook_credential_name', 'name_key', unique=True),
        Index('idx_webhook_credential_created', 'created_at'),
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
    reviewed_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    invalidated_reason = Column(String(255), nullable=True)
    invalidated_at = Column(DateTime, nullable=True)

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
    # SET NULL: the merge record outlives the administrator who performed it.
    # historical_merged_by keeps the numeric identity for forensic questions.
    merged_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    historical_merged_by = Column(Integer, nullable=True,
                                  comment="users.id at deletion time; survives account deletion")
    merged_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    notes = Column(Text, nullable=True)

    # What this merge moved, recorded so a future unmerge can exist: the row ids
    # re-parented per table, and per gallery image its original/new path,
    # whether the file was copied, its old primary flag, and any checksum
    # deduplication target. NULL on rows written before the column existed —
    # nothing was recorded for them, and inventing it now would be a lie.
    provenance = Column(JSONB, nullable=True)

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
    # SET NULL: audit rows must survive account deletion. NULL means "a deleted
    # human"; the `system` principal remains the actor for MACHINE actions —
    # this change does not retire it, and the two must never be conflated.
    # `username` (NOT NULL) keeps every row readable either way.
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    historical_user_id = Column(Integer, nullable=True,
                                comment="users.id at write/deletion time; survives account deletion")
    username = Column(String(100), nullable=False, index=True)  # Denormalized for easier querying
    action_type = Column(String(50), nullable=False, index=True)  # promote, merge, search, view, approve, reject, etc.
    # SET NULL, like user_id above: an audit row must outlive both the operator
    # and the subject. action_details keeps the id as text, so a deleted
    # identity stays traceable after the pointer is cleared.
    identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='SET NULL'), nullable=True, index=True)  # Target identity
    related_identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='SET NULL'), nullable=True)  # For merges, related identity
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
    changed_by_user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
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

    # Durable queue control.  These columns are nullable because this table
    # also stores historical/non-queued background tasks.  Queue payloads are
    # never serialized by the public task-history API.
    queue_name = Column(String(32), nullable=True)
    payload = Column(JSONB, nullable=True)
    lease_owner = Column(String(100), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    cancel_requested_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, nullable=True, onupdate=datetime.utcnow)

    __table_args__ = (
        Index('idx_task_history_type_status', 'task_type', 'status'),
        Index('idx_task_history_completed', 'completed_at'),
        Index('idx_task_history_scheduled', 'scheduled_time'),
        Index('idx_task_history_job_id', 'job_id'),
        Index('idx_task_history_correlation', 'correlation_id'),
        Index('idx_task_history_queue_status', 'queue_name', 'status', 'scheduled_time'),
        Index('idx_task_history_lease_expiry', 'lease_expires_at',
              postgresql_where=text("status = 'running' AND lease_expires_at IS NOT NULL")),
        Index('uq_ml_queue_active_task_type', 'task_type', unique=True,
              postgresql_where=text(
                  "queue_name = 'ml' AND status IN ('scheduled', 'running')"
              )),
    )


class MLWorkerHeartbeat(Base):
    """Last-seen state for independently deployed ML queue workers."""
    __tablename__ = "ml_worker_heartbeats"

    worker_id = Column(String(100), primary_key=True)
    hostname = Column(String(100), nullable=False)
    process_id = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="idle")
    current_job_id = Column(String(64), nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    heartbeat_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index('idx_ml_worker_heartbeat_at', 'heartbeat_at'),
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
    created_by_user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    
    # Relationships
    identity1 = relationship("Identity", foreign_keys=[identity_id_1])
    identity2 = relationship("Identity", foreign_keys=[identity_id_2])
    user = relationship("User")
    
    __table_args__ = (
        # An association table feeding an ML training set: a duplicate pair
        # double-weights a training example. Partial, because a row with a
        # NULL member is not a pair and many NULLs must not collide. The
        # CHECK is what makes the unique index meaningful — without a fixed
        # ordering, (A,B) and (B,A) are distinct to the index and identical
        # in meaning. See b4d5e6f7a8c9.
        Index('uq_similarity_training_pair', 'identity_id_1', 'identity_id_2',
              unique=True,
              postgresql_where=text('identity_id_1 IS NOT NULL AND identity_id_2 IS NOT NULL')),
        CheckConstraint('identity_id_1 IS NULL OR identity_id_2 IS NULL '
                        'OR identity_id_1 < identity_id_2',
                        name='ck_similarity_training_ordered'),
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

    id = Column(Integer, primary_key=True)
    model_type = Column(String(50), nullable=False, default="merge_similarity")
    version = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="candidate")  # candidate|active|archived|rejected|failed
    artifact_name = Column(String(200), nullable=False)   # logical id shown to clients
    artifact_path = Column(Text, nullable=False)          # server-side only, never serialized
    artifact_hash = Column(String(64), nullable=True)     # sha256 of the artifact file
    training_job_id = Column(String(64), nullable=True)      # idx_model_registry_job (a5c6d7e8f9b0)
    dataset_version = Column(String(64), nullable=True)
    dataset_hash = Column(String(64), nullable=True)
    feature_schema_version = Column(String(64), nullable=True)
    seed = Column(Integer, nullable=True)
    metrics = Column(JSONB, nullable=True)
    quality_gates = Column(JSONB, nullable=True)
    comparison = Column(JSONB, nullable=True)             # candidate vs active at training time
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)   # idx_model_registry_created
    created_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    activated_at = Column(DateTime, nullable=True)
    activated_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    archived_at = Column(DateTime, nullable=True)
    failure_code = Column(String(64), nullable=True)
    notes = Column(Text, nullable=True)

    __table_args__ = (
        Index('idx_model_registry_type_status', 'model_type', 'status'),
        Index('idx_model_registry_created', 'created_at'),
        Index('idx_model_registry_job', 'training_job_id'),
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
    created_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Optimistic concurrency + soft deletion (watchlist hardening)
    version = Column(Integer, default=1, nullable=False)
    deleted_at = Column(DateTime, nullable=True)
    deleted_by_user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
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
    added_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
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
    search_id = Column(UUID(as_uuid=True), ForeignKey('search_history.id', ondelete='SET NULL'),
                       nullable=True)  # the search that triggered it; history keeps the alert
    detection_id = Column(Integer, ForeignKey('detections.id', ondelete='SET NULL'), nullable=True)  # If triggered by live detection
    similarity_score = Column(Float, nullable=True)
    pipeline_id = Column(String(255), ForeignKey('pipelines.pipeline_id', ondelete='SET NULL'),
                         nullable=True)  # where it fired
    snapshot_path = Column(String(512), nullable=True)
    acknowledged = Column(Boolean, default=False, nullable=False)
    acknowledged_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
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
        Index('idx_watchlist_alert_detection', 'detection_id'),
        # detection-triggered alerts are idempotent per (entry, detection)
        Index('uq_watchlist_alert_entry_detection', 'watchlist_entry_id', 'detection_id', unique=True,
              postgresql_where=text('detection_id IS NOT NULL')),
    )


class LiveSearchAlert(Base):
    """Live search alerts - notify when a searched face appears again"""
    __tablename__ = "live_search_alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(200), nullable=False)
    identity_id = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='CASCADE'), nullable=False, index=True)
    # SET NULL: the alert is operational configuration, not personal data — it
    # keeps firing after its creator's account is deleted.
    created_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    historical_created_by = Column(Integer, nullable=True,
                                   comment="users.id at deletion time; survives account deletion")
    
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
    pipeline_id = Column(String(255), ForeignKey('pipelines.pipeline_id', ondelete='SET NULL'),
                         nullable=True)  # where it fired
    similarity_score = Column(Float, nullable=True)
    snapshot_path = Column(String(512), nullable=True)
    clip_path = Column(String(512), nullable=True)
    acknowledged = Column(Boolean, default=False, nullable=False)
    acknowledged_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
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
    # SET NULL: search history is investigative/audit record, preserved when
    # the account is deleted. historical_user_id keeps the numeric identity.
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    historical_user_id = Column(Integer, nullable=True,
                                comment="users.id at deletion time; survives account deletion")
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
        # The unique index is on the ORDERED pair, so it only prevents a
        # duplicate when the ordering actually holds — without the CHECK,
        # (A,B) and (B,A) are two rows for one relationship. The comment
        # here used to claim ordering was 'ensured' when only application
        # convention did it. See b4d5e6f7a8c9.
        Index('idx_relationship_pair', 'identity_id_1', 'identity_id_2', unique=True),
        CheckConstraint('identity_id_1 < identity_id_2',
                        name='ck_identity_relationship_ordered'),
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
    # SET NULL, not CASCADE: query history is preserved when the account is
    # deleted. NULL here means "a deleted human"; historical_user_id below keeps
    # the numeric identity, resolvable against deleted_users.
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    historical_user_id = Column(Integer, nullable=True,
                                comment="users.id at write/deletion time; survives account deletion")
    session_id = Column(String(255), ForeignKey('user_conversation_sessions.session_id', ondelete='SET NULL'),
                        nullable=True, index=True, comment="Groups queries into conversation sessions")

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
    
    # passive_deletes="all": the DATABASE owns what happens to these rows on
    # user deletion (ON DELETE SET NULL). Without it, SQLAlchemy de-associates
    # loaded children itself — the exact bug that emitted
    # `UPDATE workspace_members SET user_id = NULL` and broke deletion. "all"
    # (not True) so even instances already in the session are left alone.
    user = relationship("User",
                        backref=backref("query_history", passive_deletes="all"))

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
    # SET NULL: sessions are history containers and outlive the account like
    # the history rows they group (c2d3e4f5a6b7).
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    session_id = Column(String(255), unique=True, nullable=False, index=True, comment="Unique session identifier")
    session_name = Column(String(255), nullable=True, comment="Optional user-defined session name")
    
    # Session lifecycle
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    last_activity_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False, index=True, comment="Whether the session is currently active")
    
    # Context summary for memory
    context_summary = Column(Text, nullable=True, comment="Text summary of conversation context for quick retrieval")
    query_count = Column(Integer, default=0, nullable=False, comment="Number of queries in this session")
    
    # passive_deletes="all": the DB's ON DELETE CASCADE removes these rows on
    # user deletion; the ORM must not de-associate loaded instances first.
    user = relationship("User",
                        backref=backref("conversation_sessions", passive_deletes="all"))

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
    source_session_id = Column(String(255), ForeignKey('user_conversation_sessions.session_id', ondelete='SET NULL'),
                               nullable=True, comment="Session where this memory was created")
    source_query_id = Column(Integer, ForeignKey('user_query_history.id', ondelete='SET NULL'), nullable=True, comment="Query that created this memory")
    
    # passive_deletes="all": the DB's ON DELETE CASCADE removes these rows on
    # user deletion; the ORM must not de-associate loaded instances first.
    user = relationship("User",
                        backref=backref("conversation_memory", passive_deletes="all"))
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
    # SET NULL, not CASCADE: semantic history is preserved on account deletion.
    # No historical_user_id here — query_history_id is NOT NULL onto
    # user_query_history, which carries the historical identity.
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    
    # Embedding storage - use pgvector if available, otherwise JSONB
    if PGVECTOR_AVAILABLE:
        embedding = Column(Vector(384), nullable=True, comment="Vector embedding stored as pgvector (384-dim for sentence-transformers/all-MiniLM-L6-v2)")
    else:
        embedding = Column(JSONB, nullable=True, comment="Vector embedding stored as JSONB array (fallback when pgvector not available)")
    
    # Embedding metadata
    embedding_model = Column(String(100), nullable=True, comment="Model used to generate embedding (e.g., 'sentence-transformers/all-MiniLM-L6-v2')")
    embedding_dimensions = Column(Integer, nullable=True, comment="Number of dimensions in the embedding vector")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    
    # passive_deletes="all": the DB's ON DELETE SET NULL detaches these rows on
    # user deletion; the ORM must not de-associate loaded instances first.
    query_history = relationship("UserQueryHistory", backref="embedding")
    user = relationship("User",
                        backref=backref("query_embeddings", passive_deletes="all"))

    __table_args__ = (
        Index('idx_embedding_user', 'user_id', 'created_at'),
        Index('idx_embedding_query', 'query_history_id'),
        Index('idx_embedding_model', 'embedding_model'),
    )


# =====================================================
# TENANCY + CONVERSATION DOMAIN
# =====================================================
# Organization -> Workspace -> User -> Conversation -> ConversationBranch
# -> Message. Conversations belong DIRECTLY to a Workspace (no Project layer,
# by explicit scope decision).
#
# Security note that shapes every table here: fr_readonly — the restricted
# role that executes LLM-generated SQL — inherits SELECT on all future tables
# via ALTER DEFAULT PRIVILEGES (db/roles.sql). The migration that creates
# these tables REVOKEs that grant, because a prompt-injected query must never
# be able to read anyone's private conversation content.


class Organization(Base):
    """Top-level tenant. One row ("Default Organization") is seeded by the
    migration; multi-org support is structural from day one so adding a second
    tenant is an INSERT, not a schema change."""
    __tablename__ = "organizations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, unique=True)
    is_default = Column(Boolean, default=False, nullable=False,
                        comment="Exactly one default org receives users with no explicit membership")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Workspace(Base):
    """Unit of isolation for conversations. Membership gates everything."""
    __tablename__ = "workspaces"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True),
                             ForeignKey('organizations.id', ondelete='CASCADE'),
                             nullable=False, index=True)
    name = Column(String(255), nullable=False)
    is_default = Column(Boolean, default=False, nullable=False)
    settings = Column(JSONB, nullable=True, comment="Workspace-level feature settings")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    organization = relationship("Organization", backref="workspaces")

    __table_args__ = (
        Index('idx_workspace_org', 'organization_id', 'name', unique=True),
    )


class WorkspaceMember(Base):
    """Who may act inside a workspace, and as what.

    Deliberately NOT a copy of users.role: the platform role (admin/analyzer/
    ...) is about the face-recognition application; the workspace role is about
    chat tenancy. An analyst can own one workspace and merely read another.
    """
    __tablename__ = "workspace_members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(UUID(as_uuid=True),
                          ForeignKey('workspaces.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'),
                     nullable=False, index=True)
    role = Column(String(32), nullable=False, default='member',
                  comment="workspace role: admin | member | viewer")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    workspace = relationship("Workspace", backref="members")
    # passive_deletes="all": membership rows are removed by the DB's ON DELETE
    # CASCADE when the user is deleted. Without this, SQLAlchemy de-associated
    # the children itself — `UPDATE workspace_members SET user_id = NULL`
    # against a NOT NULL column — which is the exact failure that made user
    # deletion impossible. "all" (not True) so instances already loaded into
    # the session are also left to the database.
    user = relationship("User",
                        backref=backref("workspace_memberships", passive_deletes="all"))

    __table_args__ = (
        Index('idx_ws_member_unique', 'workspace_id', 'user_id', unique=True),
    )


class Conversation(Base):
    """A chat thread. Owned by one user, scoped to one workspace."""
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(UUID(as_uuid=True),
                          ForeignKey('workspaces.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    # SET NULL, not CASCADE: chat history outlives the account. NULL means the
    # owner was a human whose account has been deleted; author_username and
    # historical_user_id below keep the attribution readable and traceable.
    # Orphaned (user_id IS NULL) conversations are readable by same-workspace
    # admins via a separate read-only path — never editable, never re-owned.
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'),
                     nullable=True, index=True)
    author_username = Column(String(100), nullable=True,
                             comment="Denormalized owner username; survives account deletion")
    historical_user_id = Column(Integer, nullable=True,
                                comment="users.id at deletion time; resolvable against deleted_users")
    title = Column(String(500), nullable=False, default='New conversation')
    pinned = Column(Boolean, default=False, nullable=False)
    archived = Column(Boolean, default=False, nullable=False)
    # Soft delete: history disappears from the UI immediately but survives
    # until a retention job hard-deletes it, so accidental deletion is
    # recoverable and audit questions remain answerable.
    deleted_at = Column(DateTime, nullable=True, index=True)
    # Bridge to the pre-existing flat history (user_query_history.session_id):
    # lets the streaming path find the right conversation without a schema
    # change on its side, and makes the backfill idempotent.
    legacy_session_id = Column(String(255), ForeignKey('user_conversation_sessions.session_id', ondelete='SET NULL'),
                               nullable=True, index=True)
    last_message_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    workspace = relationship("Workspace", backref="conversations")
    # passive_deletes="all": the DB's ON DELETE SET NULL detaches these rows on
    # user deletion; the ORM must not de-associate loaded instances first.
    user = relationship("User",
                        backref=backref("conversations", passive_deletes="all"))

    __table_args__ = (
        Index('idx_conv_owner_listing', 'user_id', 'workspace_id', 'deleted_at',
              'pinned', 'last_message_at'),
        Index('idx_conv_legacy_session', 'user_id', 'legacy_session_id'),
    )


class ConversationBranch(Base):
    """One linear message sequence within a conversation.

    Every conversation gets a primary branch at creation. Editing an earlier
    user message forks a new branch at that point (forked_from_message_id),
    leaving the original intact — the ChatGPT branching model.
    """
    __tablename__ = "conversation_branches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(UUID(as_uuid=True),
                             ForeignKey('conversations.id', ondelete='CASCADE'),
                             nullable=False, index=True)
    parent_branch_id = Column(UUID(as_uuid=True),
                              ForeignKey('conversation_branches.id', ondelete='SET NULL'),
                              nullable=True)
    forked_from_message_id = Column(UUID(as_uuid=True), nullable=True,
                                    comment="Message in the parent branch this branch diverges after")
    name = Column(String(255), nullable=True)
    is_primary = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    conversation = relationship("Conversation", backref="branches")

    __table_args__ = (
        Index('idx_branch_conversation', 'conversation_id', 'created_at'),
    )


class Message(Base):
    """One turn in a branch, with TYPED content blocks.

    content_blocks is a JSONB list of {"type": ..., ...} objects — text, sql,
    result_table, warning, error — never one pre-rendered string, so the
    frontend can render SQL with syntax highlighting and results as tables
    without re-parsing prose, and new block types need no migration.
    """
    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id = Column(UUID(as_uuid=True),
                       ForeignKey('conversation_branches.id', ondelete='CASCADE'),
                       nullable=False, index=True)
    role = Column(String(16), nullable=False, comment="user | assistant | system")
    sequence = Column(Integer, nullable=False, comment="Monotonic position within the branch")
    content_blocks = Column(JSONB, nullable=False, default=list,
                            comment='Typed blocks: [{"type":"text","text":...},{"type":"sql","sql":...},...]')
    status = Column(String(16), nullable=False, default='complete',
                    comment="complete | failed | cancelled")
    model_provider = Column(String(64), nullable=True)
    model_name = Column(String(255), nullable=True)
    processing_time_ms = Column(Float, nullable=True)
    edited_from_message_id = Column(UUID(as_uuid=True), nullable=True,
                                    comment="Set when this message is an edit of an earlier one (branch fork)")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    branch = relationship("ConversationBranch", backref="messages")

    __table_args__ = (
        Index('idx_message_branch_seq', 'branch_id', 'sequence', unique=True),
    )


class AgentArtifact(Base):
    """A document the SQL agent produced: a PDF or Word report.

    Exists so the agent can answer "the last report" / "that PDF" from a
    record instead of a guess. Before this, exports were bytes in an HTTP
    response and nothing remembered they had happened, so every follow-up
    referring to a generated document was unresolvable.

    LINEAGE, not free text. `source_query` is the human phrasing and is
    informational only; reproduction uses `source_sql` plus the immutable ids
    (`source_message_id`, `source_result_id`, `parent_artifact_id`). That is
    what makes "same report but only for camera 3" modify the report's OWN
    originating query rather than whichever SQL happened to run most recently.

    `source_content` holds the pre-render narrative so a translation is
    text -> LLM -> re-render, never PDF parsing. It carries the same
    surveillance data as the document, so it is treated like the document:
    owner-scoped, never serialized into API responses, deleted with the row.

    Attribution follows the conversation rule — chat history outlives the
    account, so user_id is SET NULL on delete and created_by_username keeps
    the record readable.
    """
    __tablename__ = "agent_artifacts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'),
                     nullable=True, index=True)
    created_by_username = Column(String(255), nullable=False,
                                 comment="Denormalized owner name; survives account deletion")
    conversation_id = Column(UUID(as_uuid=True),
                             ForeignKey('conversations.id', ondelete='SET NULL'),
                             nullable=True, index=True)

    type = Column(String(16), nullable=False, comment="pdf | word | report")
    title = Column(String(255), nullable=False)
    language = Column(String(8), nullable=False, default='en')

    # RELATIVE to settings.ARTIFACTS_DIR. Never an absolute path and never
    # client-supplied: the download route joins it to the configured root and
    # re-checks containment, so a stored value cannot escape the directory.
    storage_path = Column(String(512), nullable=False)

    source_query = Column(Text, nullable=True, comment="User phrasing (informational)")
    source_sql = Column(Text, nullable=True, comment="The query this document reports on")
    source_content = Column(Text, nullable=True,
                            comment="Pre-render narrative. Owner-scoped; never returned by an API")
    source_message_id = Column(UUID(as_uuid=True),
                               ForeignKey('messages.id', ondelete='SET NULL'), nullable=True)
    source_result_id = Column(Integer,
                              ForeignKey('user_query_history.id', ondelete='SET NULL'),
                              nullable=True)
    modification_meta = Column(JSONB, nullable=True,
                               comment="Normalized delta applied vs the parent artifact")
    parent_artifact_id = Column(UUID(as_uuid=True),
                                ForeignKey('agent_artifacts.id', ondelete='SET NULL'),
                                nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    deleted_at = Column(DateTime, nullable=True,
                        comment="Soft delete; retention removes the row and the file together")

    __table_args__ = (
        # The resolver's hot path: newest live artifacts for one owner.
        Index('idx_artifact_owner_recent', 'user_id', 'created_at'),
        Index('idx_artifact_conversation', 'conversation_id', 'created_at'),
    )


class MessageFeedback(Base):
    """Thumbs up/down + optional comment, one per user per message."""
    __tablename__ = "message_feedback"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id = Column(UUID(as_uuid=True),
                        ForeignKey('messages.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'),
                     nullable=False, index=True)
    rating = Column(Integer, nullable=False, comment="+1 or -1")
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index('idx_feedback_unique', 'message_id', 'user_id', unique=True),
    )



# =====================================================
# Risk platform: unified scoring, persisted assessments,
# learned thresholds (see backend/core/risk_engine.py)
# =====================================================

class ThreatAssessmentRecord(Base):
    """One persisted output of the unified risk engine.

    Every generated assessment lands here transactionally; the idempotency
    key (subject + model version + time bucket) makes concurrent recalculation
    across workers collapse onto one row instead of duplicating.
    """
    __tablename__ = "threat_assessments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_type = Column(String(32), nullable=False, index=True,
                          comment="identity | movement | network_node")
    subject_id = Column(String(64), nullable=False, index=True)
    person_id = Column(UUID(as_uuid=True),
                       ForeignKey('identities.id', ondelete='SET NULL'),
                       nullable=True, index=True,
                       comment="Set when the subject is (or resolves to) an identity")
    pipeline_id = Column(String(255), nullable=True, index=True)
    location_name = Column(String(255), nullable=True)
    event_id = Column(String(64), nullable=True,
                      comment="Detection/event id when the assessment was event-triggered")

    total_risk_score = Column(Float, nullable=False)
    severity = Column(String(16), nullable=False, index=True,
                      comment="low | moderate | high | critical (unified 0-100 bands)")
    confidence = Column(Float, nullable=False, default=0.0,
                        comment="0-1: how much evidence backed the signals (NOT a probability of threat)")
    signals = Column(JSONB, nullable=False, default=list,
                     comment='[{"name","score","weight","raw_value","explanation"}]')
    model_version = Column(String(64), nullable=False)
    threshold_version = Column(String(128), nullable=True,
                               comment="learned-threshold versions consumed, e.g. 'global:multi_camera_time_window_minutes@v2'")
    explanation = Column(Text, nullable=True)
    limitations = Column(JSONB, nullable=True, default=list)

    status = Column(String(16), nullable=False, default='open', index=True,
                    comment="open | acknowledged | resolved")
    source_timestamp = Column(DateTime, nullable=False,
                              comment="UTC time of the evidence the assessment was computed over")
    idempotency_key = Column(String(255), nullable=False, unique=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    acknowledged_at = Column(DateTime, nullable=True)
    acknowledged_by = Column(String(255), nullable=True,
                             comment="Username (audit identity), never a token")
    resolution_status = Column(String(32), nullable=True,
                               comment="resolved | false_positive | reopened")
    resolution_notes = Column(Text, nullable=True)
    # ML first release (additive): NULL means legacy/rules and serializes
    # exactly as before. Only rules|shadow occur this release.
    decision_mode = Column(String(16), nullable=True,
                           comment="EXECUTED mode: NULL = legacy/rules; rules | shadow | ml")
    # Decision provenance (revision c3e8a1f5d7b2) — what happened for THIS
    # assessment; NULL on rows persisted before it = not recorded.
    requested_mode = Column(String(16), nullable=True, comment="configured mode at the time")
    anomaly_signal_source = Column(String(8), nullable=True, comment="rules | ml")
    signal_mapping_version = Column(String(64), nullable=True, comment="validated ML->risk policy used")
    fallback_reason = Column(String(64), nullable=True, comment="FallbackReason when ML could not serve")
    ml_prediction_id = Column(UUID(as_uuid=True),
                              ForeignKey('ml_predictions.id', ondelete='SET NULL',
                                         use_alter=True,
                                         name='fk_threat_assessments_ml_prediction'),
                              nullable=True)

    signal_results = relationship("RiskSignalResult", back_populates="assessment",
                                  cascade="all, delete-orphan", passive_deletes=True)

    __table_args__ = (
        Index('idx_assessment_subject', 'subject_type', 'subject_id', 'created_at'),
        Index('idx_assessment_person_created', 'person_id', 'created_at'),
        Index('idx_assessment_severity_status', 'severity', 'status'),
        Index('idx_assessment_pipeline_created', 'pipeline_id', 'created_at'),
    )


class RiskSignalResult(Base):
    """Normalized per-signal rows for analytics (the JSONB copy on the
    assessment serves cheap reads; these rows serve aggregation)."""
    __tablename__ = "risk_signal_results"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assessment_id = Column(UUID(as_uuid=True),
                           ForeignKey('threat_assessments.id', ondelete='CASCADE'),
                           nullable=False, index=True)
    signal_name = Column(String(64), nullable=False, index=True)
    raw_value = Column(JSONB, nullable=True)
    score = Column(Float, nullable=False, default=0.0)
    weight = Column(Float, nullable=False, default=0.0)
    explanation = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    assessment = relationship("ThreatAssessmentRecord", back_populates="signal_results")

    __table_args__ = (
        # This table is what per-signal analytics aggregate over, so a
        # duplicate row silently doubles an aggregate. The parent guards
        # retries (children are written only when the parent's ON CONFLICT
        # actually inserted), so the exposure is two signals colliding
        # INSIDE one assessment — which the writer's signal_name[:64]
        # truncation can manufacture from two distinct long names.
        # See b4d5e6f7a8c9.
        Index('uq_risk_signal_assessment_name', 'assessment_id', 'signal_name',
              unique=True),
    )


class RiskModelVersion(Base):
    """Configuration-driven weights/thresholds per scoring profile.

    The active row for a profile is what the risk engine loads; editing
    weights is a data change, not a deploy. calibration_* is an interface for
    FUTURE validated calibration — never fabricated (scores stay labelled
    heuristic until a real calibration lands here).
    """
    __tablename__ = "risk_model_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile = Column(String(32), nullable=False,
                     comment="identity_threat | network_node | movement_map")
    version = Column(String(64), nullable=False)
    weights = Column(JSONB, nullable=False, default=dict)
    thresholds = Column(JSONB, nullable=False, default=dict)
    status = Column(String(16), nullable=False, default='active', index=True,
                    comment="draft | active | retired")
    score_type = Column(String(16), nullable=False, default='heuristic')
    calibration_status = Column(String(32), nullable=False, default='uncalibrated')
    calibration_data = Column(JSONB, nullable=True,
                              comment="Reserved for validated calibration curves; never fabricated")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    activated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('idx_risk_model_profile_version', 'profile', 'version', unique=True),
        Index('idx_risk_model_profile_status', 'profile', 'status'),
    )


class LearnedThreshold(Base):
    """Persisted learned thresholds with an explicit activation lifecycle.

    The threshold-learning job writes CANDIDATE rows; an admin activates a
    candidate after review (activation retires the previous active row for
    the same scope+signal, giving one-step rollback by re-activating it).
    scope_id is '' (empty string, NOT NULL) for global scope so the
    uniqueness constraint actually bites.
    """
    __tablename__ = "learned_thresholds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_type = Column(String(16), nullable=False,
                        comment="global | pipeline | location")
    scope_id = Column(String(255), nullable=False, default='',
                      comment="pipeline_id / location name; '' for global")
    signal_name = Column(String(64), nullable=False,
                         comment="e.g. multi_camera_time_window_minutes, multi_camera_distance_meters")
    value = Column(Float, nullable=False)
    extras = Column(JSONB, nullable=True,
                    comment="p95, spread, per-pair details, learning metadata")
    sample_count = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default='candidate', index=True,
                    comment="candidate | active | retired")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    activated_at = Column(DateTime, nullable=True)
    activated_by = Column(String(255), nullable=True)

    __table_args__ = (
        Index('idx_learned_threshold_unique', 'scope_type', 'scope_id',
              'signal_name', 'version', unique=True),
        Index('idx_learned_threshold_lookup', 'scope_type', 'scope_id',
              'signal_name', 'status'),
    )



# =====================================================
# ML pipeline (first release): features, labels, datasets,
# model registry, predictions, shadow, drift, audit.
# See backend/ml/ and docs — RULES stays the production
# decision system; anomaly models cap at admin-approved SHADOW.
# =====================================================

class MLFeatureDefinition(Base):
    """One versioned feature definition — the SAME definition drives offline
    training snapshots and online inference (no training-serving skew)."""
    __tablename__ = "ml_feature_definitions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(128), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    entity_type = Column(String(16), nullable=False, comment="person | pair | pipeline")
    value_type = Column(String(16), nullable=False, default="float")
    window = Column(String(16), nullable=True, comment="7d | 30d | 90d | all; NULL = static")
    source = Column(String(64), nullable=False,
                    comment="identity_appearances | detections | identity_relationships | identities | graph")
    computation = Column(String(64), nullable=False, comment="builder key in backend/ml/feature_builders.BUILDERS")
    params = Column(JSONB, nullable=False, default=dict)
    leakage_class = Column(String(32), nullable=False, default="safe",
                           comment="safe | target_adjacent (excluded from supervised datasets)")
    readiness_requirements = Column(JSONB, nullable=True,
                                    comment="e.g. graph gates {min_nodes, min_edges, min_observation_days}")
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(Integer, nullable=True)
    deactivated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('uq_ml_feature_def_name_version', 'name', 'version', unique=True),
        Index('idx_ml_feature_def_entity_active', 'entity_type', 'is_active'),
    )


class MLFeatureSnapshot(Base):
    """Point-in-time feature vector for one entity at one as_of cutoff.

    No source row with event time >= as_of_timestamp is visible to the
    builder. unavailable_features records WHY a feature was not computed
    (e.g. graph below readiness gates) — absence is honest, never a zero.
    """
    __tablename__ = "ml_feature_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_type = Column(String(16), nullable=False)
    entity_id = Column(String(128), nullable=False,
                       comment="identity UUID, sorted 'uuid1|uuid2' pair, or pipeline_id")
    feature_set_version = Column(String(64), nullable=False)
    as_of_timestamp = Column(DateTime, nullable=False, comment="UTC cutoff — the point in time")
    event_timestamp = Column(DateTime, nullable=True, comment="event time of the trigger, UTC")
    computed_at = Column(DateTime, nullable=False, default=datetime.utcnow, comment="processing time")
    features = Column(JSONB, nullable=False, default=dict)
    unavailable_features = Column(JSONB, nullable=False, default=dict,
                                  comment='{"feature_name": "reason"} — no misleading zeros')
    features_checksum = Column(String(64), nullable=True)
    local_timezone = Column(String(64), nullable=True, comment="IANA tz used for local-time features")
    computation_run_id = Column(String(64), nullable=True, comment="lineage -> job_id")
    source_row_counts = Column(JSONB, nullable=True, comment="lineage: rows read per source")

    __table_args__ = (
        Index('uq_ml_snapshot_identity', 'entity_type', 'entity_id',
              'feature_set_version', 'as_of_timestamp', unique=True),
        Index('idx_ml_snapshot_entity_asof', 'entity_type', 'entity_id', 'as_of_timestamp'),
        Index('idx_ml_snapshot_run', 'computation_run_id'),
        Index('idx_ml_snapshot_computed', 'computed_at'),
    )


class MLCollectionCheckpoint(Base):
    """Incremental-collection watermark — idempotent, late-data aware."""
    __tablename__ = "ml_collection_checkpoints"

    id = Column(Integer, primary_key=True)
    collector_name = Column(String(64), nullable=False, unique=True)
    watermark_event_time = Column(DateTime, nullable=True)
    watermark_id = Column(Integer, nullable=True, comment="tie-break on equal timestamps")
    late_grace_minutes = Column(Integer, nullable=False, default=120,
                                comment="reprocess window for late arrivals (snapshot uniqueness makes it idempotent)")
    last_run_id = Column(String(64), nullable=True)
    last_run_at = Column(DateTime, nullable=True)
    rows_processed_total = Column(Integer, nullable=False, default=0)
    extras = Column(JSONB, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class MLLabel(Base):
    """Reviewed-label workflow. Only manual+reviewed labels (or explicitly
    approved verified outcomes) count toward supervised-training minimums;
    an unresolved or auto-generated assessment seeds at most a WEAK label."""
    __tablename__ = "ml_labels"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_type = Column(String(16), nullable=False, default="identity")
    subject_id = Column(String(64), nullable=False)
    person_id = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='SET NULL'),
                       nullable=True, index=True)
    assessment_id = Column(UUID(as_uuid=True), ForeignKey('threat_assessments.id', ondelete='SET NULL'),
                           nullable=True)
    label = Column(String(16), nullable=False, comment="positive | negative | unknown")
    label_kind = Column(String(16), nullable=False, comment="manual | weak")
    label_definition_version = Column(String(64), nullable=False)
    confidence = Column(Float, nullable=False, default=1.0)
    source = Column(String(64), nullable=False,
                    comment="analyst_review | assessment_resolution | weak_rule:<name> | import")
    event_time = Column(DateTime, nullable=False,
                        comment="UTC time of the labeled behavior — the as-of anchor for training examples")
    status = Column(String(16), nullable=False, default="active",
                    comment="active | superseded | retracted")
    review_status = Column(String(16), nullable=False, default="unreviewed",
                           comment="unreviewed | reviewed | disputed")
    reviewed_by = Column(String(255), nullable=True)
    reviewed_by_user_id = Column(Integer, nullable=True, comment="reviewer identity (user id); NULL for CLI/seed")
    reviewed_at = Column(DateTime, nullable=True)
    # How this review was SELECTED (revision d5f9b2c7e3a1): {method, band,
    # sampling_probability, reason, selected_at}. NULL = no explicit metadata.
    selection = Column(JSONB, nullable=True)
    supersedes_id = Column(UUID(as_uuid=True), ForeignKey('ml_labels.id'), nullable=True)
    notes = Column(Text, nullable=True)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String(255), nullable=False)
    created_by_user_id = Column(Integer, nullable=True, comment="creator identity (user id); NULL for CLI/seed")

    __table_args__ = (
        Index('idx_ml_label_subject', 'subject_type', 'subject_id', 'status'),
        Index('idx_ml_label_review', 'label', 'label_kind', 'review_status'),
        Index('idx_ml_label_created', 'created_at'),
    )


class MLDataset(Base):
    """Immutable, checksummed dataset version. Parquet holds the rows
    (models/ml/datasets/, server-only path); Postgres holds every piece of
    metadata needed to reproduce or audit the build."""
    __tablename__ = "ml_datasets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(128), nullable=False)
    version = Column(Integer, nullable=False)
    kind = Column(String(16), nullable=False, comment="supervised | unsupervised")
    feature_set_version = Column(String(64), nullable=False)
    label_definition_version = Column(String(64), nullable=True)
    source_cutoff = Column(DateTime, nullable=True, comment="no source data at/after this UTC instant")
    time_range_start = Column(DateTime, nullable=True)
    time_range_end = Column(DateTime, nullable=True)
    holdout_boundary = Column(DateTime, nullable=True,
                              comment="start of the untouched final test period")
    split_config = Column(JSONB, nullable=True,
                          comment="{method, seed, fractions, group_key, boundaries}")
    row_count = Column(Integer, nullable=True)
    positive_count = Column(Integer, nullable=True)
    negative_count = Column(Integer, nullable=True)
    weak_count = Column(Integer, nullable=True)
    missing_value_report = Column(JSONB, nullable=True)
    quality_report = Column(JSONB, nullable=True, comment="data_validator output")
    checksum = Column(String(64), nullable=False)
    storage_path = Column(Text, nullable=True, comment="server-only; never serialized to clients")
    storage_bytes = Column(Integer, nullable=True)
    code_version = Column(String(64), nullable=True, comment="git commit of the builder")
    status = Column(String(16), nullable=False, default="built",
                    comment="building | built | failed | archived")
    build_job_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(Integer, nullable=True)
    lineage_summary = Column(JSONB, nullable=True,
                             comment="{snapshot_count, snapshot_id_min, snapshot_id_max, label_count} of the Parquet rows")
    # Extraction lineage (revision a9c4e2d7f1b3). NULL on rows built before
    # it: those used the legacy silent oldest-first cap and are reported so.
    definition_name = Column(String(128), nullable=True,
                             comment="typed DatasetDefinition that built this version")
    definition_version = Column(String(64), nullable=True)
    extraction = Column(JSONB, nullable=True,
                        comment="{policy_version, candidate_rows, selected_rows, excluded_rows, cap, sampling_policy, ordering, time_range}")
    parquet_sha256 = Column(String(64), nullable=True,
                            comment="sha256 of the Parquet FILE bytes; `checksum` is the canonical-row fingerprint")
    manifest_path = Column(Text, nullable=True, comment="server-only sidecar manifest; never serialized")

    __table_args__ = (
        Index('uq_ml_dataset_name_version', 'name', 'version', unique=True),
        Index('idx_ml_dataset_status', 'status'),
    )


class MLModel(Base):
    """Authoritative model registry.

    Stage graph (enforced in backend/ml/registry_service.py):
      training -> validated -> shadow -> approved -> production
      plus rejected | archived | rolled_back | failed.
    HARD RULES this release: never training->production; VALIDATED -> SHADOW
    only through explicit admin approval (approver/reason/timestamp/dataset/
    evaluation/checksum/rollback target recorded in shadow_approval);
    anomaly model types never progress beyond SHADOW.
    """
    __tablename__ = "ml_models"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_type = Column(String(64), nullable=False,
                        comment="behavior_anomaly_model | coappearance_anomaly_model | "
                                "social_graph_anomaly_model | threat_ranking_model")
    version = Column(Integer, nullable=False)
    stage = Column(String(16), nullable=False, default="training")
    algorithm = Column(String(64), nullable=False,
                       comment="mad_baseline | isolation_forest | logreg | random_forest | gradient_boosting")
    model_purpose = Column(String(64), nullable=False, default="behavioral_anomaly_detection")
    score_type = Column(String(32), nullable=False, default="anomaly_score")
    is_probability = Column(Boolean, nullable=False, default=False)
    calibration_status = Column(String(32), nullable=False, default="not_applicable")
    artifact_name = Column(String(200), nullable=False)
    artifact_path = Column(Text, nullable=False, comment="server-only; never serialized")
    artifact_hash = Column(String(64), nullable=False)
    artifact_size_bytes = Column(Integer, nullable=True)
    dependency_versions = Column(JSONB, nullable=False, default=dict,
                                 comment="{python, sklearn, numpy, ...} — verified at load")
    feature_set_version = Column(String(64), nullable=False)
    feature_names = Column(JSONB, nullable=False, default=list)
    dataset_id = Column(UUID(as_uuid=True), ForeignKey('ml_datasets.id', ondelete='SET NULL'),
                        nullable=True)
    training_job_id = Column(String(64), nullable=True)
    seed = Column(Integer, nullable=True)
    hyperparameters = Column(JSONB, nullable=True)
    training_config = Column(JSONB, nullable=True,
                             comment="complete configuration actually used: algorithm, seed, every hyperparameter, dataset id")
    code_version = Column(String(64), nullable=True, comment="git revision of the trainer")
    metrics = Column(JSONB, nullable=True, comment="ONLY what was truthfully measured")
    quality_gates = Column(JSONB, nullable=True)
    evaluation_report = Column(JSONB, nullable=True)
    # Lifecycle stamps
    submitted_at = Column(DateTime, nullable=True)
    validated_at = Column(DateTime, nullable=True)
    shadow_approval = Column(JSONB, nullable=True,
                             comment="{approved_by_user_id, approved_by, reason, approved_at, "
                                     "dataset_version, evaluation_report_ref, artifact_checksum, "
                                     "feature_set_version, intended_scope, rollback_target}")
    shadow_started_at = Column(DateTime, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(String(255), nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    rejected_by = Column(String(255), nullable=True)
    rejection_reason = Column(Text, nullable=True)
    archived_at = Column(DateTime, nullable=True)
    rolled_back_at = Column(DateTime, nullable=True)
    rollback_reason = Column(Text, nullable=True)
    previous_production_id = Column(UUID(as_uuid=True), ForeignKey('ml_models.id', ondelete='SET NULL'),
                                    nullable=True,
                                    comment="rollback target recorded at promotion time")
    failure_code = Column(String(64), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(Integer, nullable=True)

    __table_args__ = (
        Index('uq_ml_model_type_version', 'model_type', 'version', unique=True),
        Index('uq_ml_models_one_production', 'model_type', unique=True,
              postgresql_where=text("stage = 'production'")),
        Index('uq_ml_models_one_shadow', 'model_type', unique=True,
              postgresql_where=text("stage = 'shadow'")),
        Index('idx_ml_model_type_stage', 'model_type', 'stage'),
        Index('idx_ml_model_job', 'training_job_id'),
        # First-release hard rule, DB-enforced in addition to service/API
        # validation: anomaly models never reach approved/production.
        CheckConstraint(
            "NOT (model_type LIKE '%anomaly%' AND stage IN ('approved', 'production'))",
            name='ck_ml_models_anomaly_shadow_cap'),
        CheckConstraint("previous_production_id IS NULL OR previous_production_id <> id",
                        name='ck_ml_models_prev_not_self'),
    )


class MLModelThreshold(Base):
    """One decision-cutpoint SET per (model, scope, version): the three band
    cutpoints a prediction consumes at once. Lifecycle candidate → active →
    retired, at most ONE active set per (model, scope) — enforced by a partial
    unique index. Global scope is canonical: scope_type='global' ⇔ scope_id=''.
    Inference bands from the ACTIVE set and persists its id on the prediction;
    the artifact's band_cutpoints are training-time provenance only."""
    __tablename__ = "ml_model_thresholds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id = Column(UUID(as_uuid=True), ForeignKey('ml_models.id', ondelete='CASCADE'),
                      nullable=False, index=True)
    scope_type = Column(String(16), nullable=False, default="global",
                        comment="global | pipeline | location")
    scope_id = Column(String(255), nullable=False, default="", server_default="")
    version = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default="candidate",
                    comment="candidate | active | retired")
    cutpoints = Column(JSONB, nullable=False,
                       comment="{elevated, unusual, highly_unusual} anomaly-score cutpoints, non-decreasing")
    quantiles = Column(JSONB, nullable=True, comment="training quantile behind each cutpoint")
    source = Column(String(32), nullable=False, default="training", server_default="training",
                    comment="training | manual | recalibration")
    expected_metrics = Column(JSONB, nullable=True)
    sample_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    activated_at = Column(DateTime, nullable=True)
    activated_by = Column(String(255), nullable=True)   # actor label (same convention as learned_thresholds)
    retired_at = Column(DateTime, nullable=True)
    retired_by = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)

    __table_args__ = (
        Index('uq_ml_threshold_scope_version', 'model_id', 'scope_type', 'scope_id', 'version', unique=True),
        Index('uq_ml_threshold_one_active', 'model_id', 'scope_type', 'scope_id', unique=True,
              postgresql_where=text("status = 'active'")),
        Index('idx_ml_threshold_lookup', 'model_id', 'scope_type', 'scope_id', 'status'),
        CheckConstraint("(scope_type = 'global') = (scope_id = '')", name='ck_ml_threshold_scope_canonical'),
        CheckConstraint("status IN ('candidate', 'active', 'retired')", name='ck_ml_threshold_status'),
        CheckConstraint("source IN ('training', 'manual', 'recalibration')", name='ck_ml_threshold_source'),
        CheckConstraint(
            "cutpoints ?& ARRAY['elevated', 'unusual', 'highly_unusual'] "
            "AND (cutpoints->>'elevated')::float8 <= (cutpoints->>'unusual')::float8 "
            "AND (cutpoints->>'unusual')::float8 <= (cutpoints->>'highly_unusual')::float8",
            name='ck_ml_threshold_cutpoints'),
    )


class MLPrediction(Base):
    """One persisted model evaluation. Anomaly semantics are explicit and
    NEVER conflated with threat severity: behavioral_anomaly_score +
    ml_anomaly_band (normal|elevated|unusual|highly_unusual). The full
    feature vector is NOT duplicated here — snapshot_id + checksum point at
    it (full_features only under the explicitly-enabled sampling knob)."""
    __tablename__ = "ml_predictions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_type = Column(String(16), nullable=False, default="identity")
    subject_id = Column(String(64), nullable=False)
    person_id = Column(UUID(as_uuid=True), ForeignKey('identities.id', ondelete='SET NULL'),
                       nullable=True, index=True)
    pipeline_id = Column(String(255), nullable=True)
    model_id = Column(UUID(as_uuid=True), ForeignKey('ml_models.id', ondelete='RESTRICT'),
                      nullable=True)
    model_type = Column(String(64), nullable=False)
    model_version_label = Column(String(128), nullable=False, comment="survives model deletion")
    model_purpose = Column(String(64), nullable=False, default="behavioral_anomaly_detection")
    requested_mode = Column(String(16), nullable=False)
    actual_mode_used = Column(String(16), nullable=False)
    fallback_reason = Column(String(64), nullable=True)
    snapshot_id = Column(Integer, ForeignKey('ml_feature_snapshots.id', ondelete='SET NULL'),
                         nullable=True)
    feature_set_version = Column(String(64), nullable=True)
    features_checksum = Column(String(64), nullable=True)
    missing_features = Column(JSONB, nullable=True)
    unavailable_features = Column(JSONB, nullable=True)
    full_features = Column(JSONB, nullable=True,
                           comment="ONLY when ML_FEATURE_SAMPLED_FULL_VECTOR_RATE > 0 (default 0.0) "
                                   "with documented retention/privacy justification")
    behavioral_anomaly_score = Column(Float, nullable=True, comment="raw model output")
    normalized_anomaly_score = Column(Float, nullable=True, comment="0-1 within-model normalization")
    ml_anomaly_band = Column(String(16), nullable=True,
                             comment="normal | elevated | unusual | highly_unusual — NOT threat severity")
    score_type = Column(String(32), nullable=False, default="anomaly_score")
    is_probability = Column(Boolean, nullable=False, default=False)
    calibration_status = Column(String(32), nullable=False, default="not_applicable")
    threshold_id = Column(UUID(as_uuid=True), ForeignKey('ml_model_thresholds.id', ondelete='RESTRICT'),
                          nullable=True)
    threshold_version = Column(String(64), nullable=True)
    explanation = Column(JSONB, nullable=True,
                         comment='{"method", "top_factors": [{"feature","value","contribution"}]}')
    assessment_id = Column(UUID(as_uuid=True), ForeignKey('threat_assessments.id', ondelete='SET NULL'),
                           nullable=True)
    event_time = Column(DateTime, nullable=True, comment="UTC source event time")
    as_of_timestamp = Column(DateTime, nullable=False, comment="UTC feature cutoff")
    latency_ms = Column(Float, nullable=True)
    outcome_label_id = Column(UUID(as_uuid=True), ForeignKey('ml_labels.id', ondelete='SET NULL'),
                              nullable=True)
    outcome_label = Column(String(16), nullable=True)
    outcome_recorded_at = Column(DateTime, nullable=True)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index('idx_ml_pred_subject', 'subject_type', 'subject_id', 'created_at'),
        Index('idx_ml_pred_model_created', 'model_id', 'created_at'),
        Index('idx_ml_pred_mode', 'actual_mode_used', 'created_at'),
        Index('idx_ml_pred_created', 'created_at'),
        Index('idx_ml_pred_fallback', 'fallback_reason',
              postgresql_where=text("fallback_reason IS NOT NULL")),
        Index('idx_ml_pred_assessment', 'assessment_id',
              postgresql_where=text("assessment_id IS NOT NULL")),
        Index('idx_ml_pred_subject_event', 'subject_id', 'event_time',
              postgresql_where=text("event_time IS NOT NULL")),
        Index('idx_ml_pred_outcome_label', 'outcome_label_id',
              postgresql_where=text("outcome_label_id IS NOT NULL")),
    )


class MLShadowComparison(Base):
    """Side-by-side record of the live RULE decision and the shadow model
    output. The two values are DIFFERENT CONCEPTS (threat severity vs
    behavioral anomaly) and are never numerically merged; comparison is on
    would-alert crossings and band-vs-severity placement."""
    __tablename__ = "ml_shadow_comparisons"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prediction_id = Column(UUID(as_uuid=True), ForeignKey('ml_predictions.id', ondelete='CASCADE'),
                           nullable=False, index=True)
    model_id = Column(UUID(as_uuid=True), ForeignKey('ml_models.id', ondelete='RESTRICT'),
                      nullable=True)
    assessment_id = Column(UUID(as_uuid=True), ForeignKey('threat_assessments.id', ondelete='SET NULL'),
                           nullable=True)
    subject_id = Column(String(64), nullable=False)
    pipeline_id = Column(String(255), nullable=True)
    rule_threat_score = Column(Float, nullable=False, comment="heuristic 0-100 (risk engine)")
    rule_threat_severity = Column(String(16), nullable=False, comment="low|moderate|high|critical")
    behavioral_anomaly_score = Column(Float, nullable=True)
    ml_anomaly_band = Column(String(16), nullable=True,
                             comment="normal|elevated|unusual|highly_unusual — different concept "
                                     "from threat severity, shown side by side only")
    # Descriptive review-signal comparison ONLY — never a numeric diff between
    # a threat score and an anomaly score (different concepts).
    rule_would_alert = Column(Boolean, nullable=False, default=False,
                              comment="the RULES engine's severity crossed its alerting bar")
    ml_would_flag_anomaly = Column(Boolean, nullable=False, default=False,
                                   comment="the anomaly band crossed the review-flag bar")
    operational_disagreement = Column(String(16), nullable=False, default="neither",
                                      comment="both_flagged | rules_only | anomaly_only | neither")
    ml_failed = Column(Boolean, nullable=False, default=False)
    failure_reason = Column(String(64), nullable=True)
    ml_latency_ms = Column(Float, nullable=True)
    missing_features = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        # One comparison per prediction. ml_predictions is protected by
        # idempotency_key; without this its child was not, and two shadow
        # evaluations of the same prediction could both persist (observed:
        # two rows 3s apart with different contents). See b4d5e6f7a8c9.
        Index('uq_ml_shadow_comparison_prediction', 'prediction_id', unique=True),
        Index('idx_ml_shadow_model_created', 'model_id', 'created_at'),
        Index('idx_ml_shadow_created', 'created_at'),
        Index('idx_ml_shadow_disagreement', 'operational_disagreement', 'created_at'),
    )


class MLDriftReport(Base):
    """Periodic monitoring report. The baseline is EMBEDDED (period + stats)
    so every report is self-describing. Drift never auto-triggers deployment
    or retraining; insufficient data is stated, not papered over."""
    __tablename__ = "ml_drift_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_kind = Column(String(32), nullable=False, comment="data_drift | prediction_drift")
    # NOT NULL: a drift report is always about one model (the shadow model of
    # its type at computation time); with no shadow model nothing is written.
    model_id = Column(UUID(as_uuid=True), ForeignKey('ml_models.id', ondelete='CASCADE'),
                      nullable=False)
    scope_type = Column(String(16), nullable=False, default="global")
    scope_id = Column(String(255), nullable=False, default="")
    baseline_start = Column(DateTime, nullable=True)
    baseline_end = Column(DateTime, nullable=True)
    baseline_stats = Column(JSONB, nullable=True)
    baseline_sample_count = Column(Integer, nullable=True)
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False)
    sample_count = Column(Integer, nullable=False, default=0)
    insufficient_data = Column(Boolean, nullable=False, default=False)
    metrics = Column(JSONB, nullable=False, default=dict,
                     comment="per-feature {psi, ks_stat, ks_p, js_divergence}; prediction "
                             "{score_hist_shift, volume, shadow_failure_rate, disagreement, "
                             "latency_p95, fallback_rate, pipeline_mix}")
    severity = Column(String(16), nullable=False, default="normal",
                      comment="normal | warning | critical")
    job_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index('idx_ml_drift_model_created', 'model_id', 'created_at'),
        Index('idx_ml_drift_severity', 'severity', 'created_at'),
    )


class MLRetrainingPolicy(Base):
    """Scaffolded, DISABLED by default. Any triggered output is a CANDIDATE
    requiring approval — never an automatic replacement."""
    __tablename__ = "ml_retraining_policies"

    id = Column(Integer, primary_key=True)
    model_type = Column(String(64), nullable=False, unique=True)
    enabled = Column(Boolean, nullable=False, default=False)
    schedule_interval_hours = Column(Integer, nullable=False, default=168)
    min_new_labels = Column(Integer, nullable=False, default=25)
    min_total_labels = Column(Integer, nullable=False, default=100)
    cooldown_hours = Column(Integer, nullable=False, default=168)
    min_drift_reports = Column(Integer, nullable=False, default=2,
                               comment="never retrain on one weak statistical signal")
    promotion_criteria = Column(JSONB, nullable=True, comment="advisory only; promotion is manual")
    last_triggered_at = Column(DateTime, nullable=True)
    last_trigger_reason = Column(String(128), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    updated_by = Column(String(255), nullable=True)


class MLAuditLog(Base):
    """Immutable-by-convention ML admin audit (no update/delete code paths).
    Writer joins the caller's transaction (flush, not commit) and swallows
    its own failures — audit must never break the operation. Paired with
    [MLOPS_AUDIT] structured log lines."""
    __tablename__ = "ml_audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    action = Column(String(64), nullable=False,
                    comment="mode_change | shadow_approve | model_reject | model_rollback | "
                            "threshold_activate | label_create | label_review | policy_update | "
                            "training_requested | training_cancelled | pause | ...")
    object_type = Column(String(32), nullable=True)
    object_id = Column(String(64), nullable=True)
    actor_user_id = Column(Integer, nullable=True)
    actor_username = Column(String(100), nullable=True)
    before = Column(JSONB, nullable=True)
    after = Column(JSONB, nullable=True)
    reason = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=True)
    request_id = Column(String(64), nullable=True,
                        comment="HTTP request id (matches [MLOPS_CALL] log lines); NULL for CLI/background")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index('idx_ml_audit_object', 'object_type', 'object_id', 'created_at'),
        Index('idx_ml_audit_action', 'action', 'created_at'),
        Index('idx_ml_audit_request', 'request_id'),
    )
