"""Risk platform: persisted threat assessments, unified risk-model versions,
learned thresholds with activation lifecycle, pipeline timezones.

Additive only — no existing table is altered destructively and no data is
deleted. Seeds one active risk_model_versions row per scoring profile whose
weights mirror the previously hard-coded rubrics, so scores are unchanged at
cutover.

Revision ID: a7b8c9d0e1f2
Revises: e4f5a6b7c8d9
Create Date: 2026-08-01
"""
import uuid
from datetime import datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "a7b8c9d0e1f2"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- pipelines.timezone (nullable — NULL falls back to config) --------
    op.add_column(
        "pipelines",
        sa.Column("timezone", sa.String(64), nullable=True,
                  comment="IANA timezone for business-hour evaluation "
                          "(e.g. 'Asia/Beirut'); NULL falls back to "
                          "DEFAULT_SITE_TIMEZONE. Timestamps stay UTC."))

    # ---- threat_assessments ----------------------------------------------
    op.create_table(
        "threat_assessments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("person_id", UUID(as_uuid=True),
                  sa.ForeignKey("identities.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("pipeline_id", sa.String(255), nullable=True),
        sa.Column("location_name", sa.String(255), nullable=True),
        sa.Column("event_id", sa.String(64), nullable=True),
        sa.Column("total_risk_score", sa.Float, nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("signals", JSONB, nullable=False, server_default="[]"),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("threshold_version", sa.String(128), nullable=True),
        sa.Column("explanation", sa.Text, nullable=True),
        sa.Column("limitations", JSONB, nullable=True, server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("source_timestamp", sa.DateTime, nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime, nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("acknowledged_at", sa.DateTime, nullable=True),
        sa.Column("acknowledged_by", sa.String(255), nullable=True),
        sa.Column("resolution_status", sa.String(32), nullable=True),
        sa.Column("resolution_notes", sa.Text, nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_assessment_idempotency"),
    )
    op.create_index("ix_threat_assessments_subject_type", "threat_assessments", ["subject_type"])
    op.create_index("ix_threat_assessments_subject_id", "threat_assessments", ["subject_id"])
    op.create_index("ix_threat_assessments_person_id", "threat_assessments", ["person_id"])
    op.create_index("ix_threat_assessments_pipeline_id", "threat_assessments", ["pipeline_id"])
    op.create_index("ix_threat_assessments_severity", "threat_assessments", ["severity"])
    op.create_index("ix_threat_assessments_status", "threat_assessments", ["status"])
    op.create_index("ix_threat_assessments_created_at", "threat_assessments", ["created_at"])
    op.create_index("idx_assessment_subject", "threat_assessments",
                    ["subject_type", "subject_id", "created_at"])
    op.create_index("idx_assessment_person_created", "threat_assessments",
                    ["person_id", "created_at"])
    op.create_index("idx_assessment_severity_status", "threat_assessments",
                    ["severity", "status"])
    op.create_index("idx_assessment_pipeline_created", "threat_assessments",
                    ["pipeline_id", "created_at"])

    # ---- risk_signal_results ---------------------------------------------
    op.create_table(
        "risk_signal_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("assessment_id", UUID(as_uuid=True),
                  sa.ForeignKey("threat_assessments.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("signal_name", sa.String(64), nullable=False),
        sa.Column("raw_value", JSONB, nullable=True),
        sa.Column("score", sa.Float, nullable=False, server_default="0"),
        sa.Column("weight", sa.Float, nullable=False, server_default="0"),
        sa.Column("explanation", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_risk_signal_results_assessment_id", "risk_signal_results", ["assessment_id"])
    op.create_index("ix_risk_signal_results_signal_name", "risk_signal_results", ["signal_name"])

    # ---- risk_model_versions ---------------------------------------------
    op.create_table(
        "risk_model_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("profile", sa.String(32), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("weights", JSONB, nullable=False, server_default="{}"),
        sa.Column("thresholds", JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("score_type", sa.String(16), nullable=False, server_default="heuristic"),
        sa.Column("calibration_status", sa.String(32), nullable=False,
                  server_default="uncalibrated"),
        sa.Column("calibration_data", JSONB, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("activated_at", sa.DateTime, nullable=True),
    )
    op.create_index("idx_risk_model_profile_version", "risk_model_versions",
                    ["profile", "version"], unique=True)
    op.create_index("idx_risk_model_profile_status", "risk_model_versions",
                    ["profile", "status"])
    op.create_index("ix_risk_model_versions_status", "risk_model_versions", ["status"])

    # ---- learned_thresholds ----------------------------------------------
    op.create_table(
        "learned_thresholds",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("signal_name", sa.String(64), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("extras", JSONB, nullable=True),
        sa.Column("sample_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="candidate"),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("activated_at", sa.DateTime, nullable=True),
        sa.Column("activated_by", sa.String(255), nullable=True),
    )
    op.create_index("idx_learned_threshold_unique", "learned_thresholds",
                    ["scope_type", "scope_id", "signal_name", "version"], unique=True)
    op.create_index("idx_learned_threshold_lookup", "learned_thresholds",
                    ["scope_type", "scope_id", "signal_name", "status"])
    op.create_index("ix_learned_thresholds_status", "learned_thresholds", ["status"])

    # ---- seed: one active model row per profile, weights = the previously
    # hard-coded rubrics (cutover changes provenance, not scores) -----------
    model_table = sa.table(
        "risk_model_versions",
        sa.column("id", UUID(as_uuid=True)),
        sa.column("profile", sa.String),
        sa.column("version", sa.String),
        sa.column("weights", JSONB),
        sa.column("thresholds", JSONB),
        sa.column("status", sa.String),
        sa.column("notes", sa.Text),
        sa.column("activated_at", sa.DateTime),
    )
    op.bulk_insert(model_table, [
        {
            "id": uuid.uuid4(),
            "profile": "identity_threat",
            "version": "risk-engine-v1",
            "weights": {
                "unknown_identity": 30.0,
                "connections_high": 25.0,
                "connections_moderate": 15.0,
                "anomaly_per_item": 10.0,
                "anomaly_cap": 30.0,
                "activity_high": 15.0,
            },
            "thresholds": {
                "connections_high": 10,
                "connections_moderate": 5,
                "activity_high": 100,
            },
            "status": "active",
            "notes": "Seeded from the threat-v2 hard-coded rubric (heuristic, uncalibrated).",
            "activated_at": datetime.utcnow(),
        },
        {
            "id": uuid.uuid4(),
            "profile": "network_node",
            "version": "risk-engine-v1",
            "weights": {
                "unknown_identity": 30.0,
                "connections_high": 20.0,
                "connections_moderate": 10.0,
                "activity_high": 15.0,
                "activity_moderate": 8.0,
                "recent_activity_day": 10.0,
                "recent_activity_week": 5.0,
            },
            "thresholds": {
                "connections_high": 10,
                "connections_moderate": 5,
                "activity_high": 100,
                "activity_moderate": 50,
            },
            "status": "active",
            "notes": "Seeded from the network-risk-v2 hard-coded rubric (heuristic, uncalibrated).",
            "activated_at": datetime.utcnow(),
        },
        {
            "id": uuid.uuid4(),
            "profile": "movement_map",
            "version": "risk-engine-v1",
            "weights": {
                "watchlist_cap": 50.0,
                "pattern_cap": 50.0,
                "zone_cap": 50.0,
                "speed_cap": 5.0,
                "base": 1.0,
                "scale": 5.0,
            },
            "thresholds": {},
            "status": "active",
            "notes": "Seeded from the map movement-risk rubric (heuristic, uncalibrated).",
            "activated_at": datetime.utcnow(),
        },
    ])


def downgrade() -> None:
    op.drop_table("learned_thresholds")
    op.drop_table("risk_model_versions")
    op.drop_table("risk_signal_results")
    op.drop_table("threat_assessments")
    op.drop_column("pipelines", "timezone")
