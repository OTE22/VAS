"""ML pipeline first release: feature store, labels, datasets, model
registry, predictions, shadow comparisons, drift reports, retraining
policies, ML audit log.

Additive only. Generated with alembic --autogenerate against the ORM (so
schema and models cannot diverge) and pruned to the ML scope; a schema-
consistency test re-verifies ORM == database after upgrade.
threat_assessments gains two NULLable columns (decision_mode,
ml_prediction_id); NULL means legacy/rules and serializes exactly as today.
Seeds: the four DISABLED retraining policies only — feature definitions are
seeded by idempotent code (backend/ml/feature_store.seed_definitions).
Anomaly models are DB-capped at shadow via ck_ml_models_anomaly_shadow_cap.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b8c9d0e1f2a3'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('ml_feature_definitions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('entity_type', sa.String(length=16), nullable=False, comment='person | pair | pipeline'),
    sa.Column('value_type', sa.String(length=16), nullable=False),
    sa.Column('window', sa.String(length=16), nullable=True, comment='7d | 30d | 90d | all; NULL = static'),
    sa.Column('source', sa.String(length=64), nullable=False, comment='identity_appearances | detections | identity_relationships | identities | graph'),
    sa.Column('computation', sa.String(length=64), nullable=False, comment='builder key in backend/ml/feature_builders.BUILDERS'),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('leakage_class', sa.String(length=32), nullable=False, comment='safe | target_adjacent (excluded from supervised datasets)'),
    sa.Column('readiness_requirements', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='e.g. graph gates {min_nodes, min_edges, min_observation_days}'),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('deactivated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ml_feature_snapshots',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('entity_type', sa.String(length=16), nullable=False),
    sa.Column('entity_id', sa.String(length=128), nullable=False, comment="identity UUID, sorted 'uuid1|uuid2' pair, or pipeline_id"),
    sa.Column('feature_set_version', sa.String(length=64), nullable=False),
    sa.Column('as_of_timestamp', sa.DateTime(), nullable=False, comment='UTC cutoff — the point in time'),
    sa.Column('event_timestamp', sa.DateTime(), nullable=True, comment='event time of the trigger, UTC'),
    sa.Column('computed_at', sa.DateTime(), nullable=False, comment='processing time'),
    sa.Column('features', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('unavailable_features', postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment='{"feature_name": "reason"} — no misleading zeros'),
    sa.Column('features_checksum', sa.String(length=64), nullable=True),
    sa.Column('local_timezone', sa.String(length=64), nullable=True, comment='IANA tz used for local-time features'),
    sa.Column('computation_run_id', sa.String(length=64), nullable=True, comment='lineage -> job_id'),
    sa.Column('source_row_counts', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='lineage: rows read per source'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ml_collection_checkpoints',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('collector_name', sa.String(length=64), nullable=False),
    sa.Column('watermark_event_time', sa.DateTime(), nullable=True),
    sa.Column('watermark_id', sa.Integer(), nullable=True, comment='tie-break on equal timestamps'),
    sa.Column('late_grace_minutes', sa.Integer(), nullable=False, comment='reprocess window for late arrivals (snapshot uniqueness makes it idempotent)'),
    sa.Column('last_run_id', sa.String(length=64), nullable=True),
    sa.Column('last_run_at', sa.DateTime(), nullable=True),
    sa.Column('rows_processed_total', sa.Integer(), nullable=False),
    sa.Column('extras', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('collector_name')
    )
    op.create_table('ml_labels',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('subject_type', sa.String(length=16), nullable=False),
    sa.Column('subject_id', sa.String(length=64), nullable=False),
    sa.Column('person_id', sa.UUID(), nullable=True),
    sa.Column('assessment_id', sa.UUID(), nullable=True),
    sa.Column('label', sa.String(length=16), nullable=False, comment='positive | negative | unknown'),
    sa.Column('label_kind', sa.String(length=16), nullable=False, comment='manual | weak'),
    sa.Column('label_definition_version', sa.String(length=64), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=False, comment='analyst_review | assessment_resolution | weak_rule:<name> | import'),
    sa.Column('event_time', sa.DateTime(), nullable=False, comment='UTC time of the labeled behavior — the as-of anchor for training examples'),
    sa.Column('status', sa.String(length=16), nullable=False, comment='active | superseded | retracted'),
    sa.Column('review_status', sa.String(length=16), nullable=False, comment='unreviewed | reviewed | disputed'),
    sa.Column('reviewed_by', sa.String(length=255), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('supersedes_id', sa.UUID(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.String(length=255), nullable=False),
    sa.ForeignKeyConstraint(['assessment_id'], ['threat_assessments.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['person_id'], ['identities.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['supersedes_id'], ['ml_labels.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    op.create_table('ml_datasets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False, comment='supervised | unsupervised'),
    sa.Column('feature_set_version', sa.String(length=64), nullable=False),
    sa.Column('label_definition_version', sa.String(length=64), nullable=True),
    sa.Column('source_cutoff', sa.DateTime(), nullable=True, comment='no source data at/after this UTC instant'),
    sa.Column('time_range_start', sa.DateTime(), nullable=True),
    sa.Column('time_range_end', sa.DateTime(), nullable=True),
    sa.Column('holdout_boundary', sa.DateTime(), nullable=True, comment='start of the untouched final test period'),
    sa.Column('split_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='{method, seed, fractions, group_key, boundaries}'),
    sa.Column('row_count', sa.Integer(), nullable=True),
    sa.Column('positive_count', sa.Integer(), nullable=True),
    sa.Column('negative_count', sa.Integer(), nullable=True),
    sa.Column('weak_count', sa.Integer(), nullable=True),
    sa.Column('missing_value_report', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('quality_report', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='data_validator output'),
    sa.Column('checksum', sa.String(length=64), nullable=False),
    sa.Column('storage_path', sa.Text(), nullable=True, comment='server-only; never serialized to clients'),
    sa.Column('storage_bytes', sa.Integer(), nullable=True),
    sa.Column('code_version', sa.String(length=64), nullable=True, comment='git commit of the builder'),
    sa.Column('status', sa.String(length=16), nullable=False, comment='building | built | failed | archived'),
    sa.Column('build_job_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ml_models',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('model_type', sa.String(length=64), nullable=False, comment='behavior_anomaly_model | coappearance_anomaly_model | social_graph_anomaly_model | threat_ranking_model'),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('stage', sa.String(length=16), nullable=False),
    sa.Column('algorithm', sa.String(length=64), nullable=False, comment='mad_baseline | isolation_forest | logreg | random_forest | gradient_boosting'),
    sa.Column('model_purpose', sa.String(length=64), nullable=False),
    sa.Column('score_type', sa.String(length=32), nullable=False),
    sa.Column('is_probability', sa.Boolean(), nullable=False),
    sa.Column('calibration_status', sa.String(length=32), nullable=False),
    sa.Column('artifact_name', sa.String(length=200), nullable=False),
    sa.Column('artifact_path', sa.Text(), nullable=False, comment='server-only; never serialized'),
    sa.Column('artifact_hash', sa.String(length=64), nullable=False),
    sa.Column('artifact_size_bytes', sa.Integer(), nullable=True),
    sa.Column('dependency_versions', postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment='{python, sklearn, numpy, ...} — verified at load'),
    sa.Column('feature_set_version', sa.String(length=64), nullable=False),
    sa.Column('feature_names', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('dataset_id', sa.UUID(), nullable=True),
    sa.Column('training_job_id', sa.String(length=64), nullable=True),
    sa.Column('seed', sa.Integer(), nullable=True),
    sa.Column('hyperparameters', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='ONLY what was truthfully measured'),
    sa.Column('quality_gates', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('evaluation_report', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('submitted_at', sa.DateTime(), nullable=True),
    sa.Column('validated_at', sa.DateTime(), nullable=True),
    sa.Column('shadow_approval', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='{approved_by_user_id, approved_by, reason, approved_at, dataset_version, evaluation_report_ref, artifact_checksum, feature_set_version, intended_scope, rollback_target}'),
    sa.Column('shadow_started_at', sa.DateTime(), nullable=True),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('approved_by', sa.String(length=255), nullable=True),
    sa.Column('rejected_at', sa.DateTime(), nullable=True),
    sa.Column('rejected_by', sa.String(length=255), nullable=True),
    sa.Column('rejection_reason', sa.Text(), nullable=True),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.Column('rolled_back_at', sa.DateTime(), nullable=True),
    sa.Column('rollback_reason', sa.Text(), nullable=True),
    sa.Column('previous_production_id', sa.UUID(), nullable=True, comment='rollback target recorded at promotion time'),
    sa.Column('failure_code', sa.String(length=64), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.CheckConstraint("NOT (model_type LIKE '%%anomaly%%' AND stage IN ('approved', 'production'))", name='ck_ml_models_anomaly_shadow_cap'),
    sa.ForeignKeyConstraint(['dataset_id'], ['ml_datasets.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ml_model_thresholds',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('model_id', sa.UUID(), nullable=False),
    sa.Column('scope_type', sa.String(length=16), nullable=False, comment='global | pipeline | location'),
    sa.Column('scope_id', sa.String(length=255), nullable=False),
    sa.Column('threshold', sa.Float(), nullable=False),
    sa.Column('objective', sa.String(length=64), nullable=True, comment='e.g. band_elevated | band_unusual | band_highly_unusual'),
    sa.Column('expected_metrics', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False, comment='candidate | active | retired'),
    sa.Column('sample_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('activated_at', sa.DateTime(), nullable=True),
    sa.Column('activated_by', sa.String(length=255), nullable=True),
    sa.ForeignKeyConstraint(['model_id'], ['ml_models.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ml_predictions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('subject_type', sa.String(length=16), nullable=False),
    sa.Column('subject_id', sa.String(length=64), nullable=False),
    sa.Column('person_id', sa.UUID(), nullable=True),
    sa.Column('pipeline_id', sa.String(length=255), nullable=True),
    sa.Column('model_id', sa.UUID(), nullable=True),
    sa.Column('model_type', sa.String(length=64), nullable=False),
    sa.Column('model_version_label', sa.String(length=128), nullable=False, comment='survives model deletion'),
    sa.Column('model_purpose', sa.String(length=64), nullable=False),
    sa.Column('requested_mode', sa.String(length=16), nullable=False),
    sa.Column('actual_mode_used', sa.String(length=16), nullable=False),
    sa.Column('fallback_reason', sa.String(length=64), nullable=True),
    sa.Column('snapshot_id', sa.Integer(), nullable=True),
    sa.Column('feature_set_version', sa.String(length=64), nullable=True),
    sa.Column('features_checksum', sa.String(length=64), nullable=True),
    sa.Column('missing_features', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('unavailable_features', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('full_features', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='ONLY when ML_FEATURE_SAMPLED_FULL_VECTOR_RATE > 0 (default 0.0) with documented retention/privacy justification'),
    sa.Column('behavioral_anomaly_score', sa.Float(), nullable=True, comment='raw model output'),
    sa.Column('normalized_anomaly_score', sa.Float(), nullable=True, comment='0-1 within-model normalization'),
    sa.Column('ml_anomaly_band', sa.String(length=16), nullable=True, comment='normal | elevated | unusual | highly_unusual — NOT threat severity'),
    sa.Column('score_type', sa.String(length=32), nullable=False),
    sa.Column('is_probability', sa.Boolean(), nullable=False),
    sa.Column('calibration_status', sa.String(length=32), nullable=False),
    sa.Column('threshold_id', sa.UUID(), nullable=True),
    sa.Column('threshold_version', sa.String(length=64), nullable=True),
    sa.Column('explanation', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='{"method", "top_factors": [{"feature","value","contribution"}]}'),
    sa.Column('assessment_id', sa.UUID(), nullable=True),
    sa.Column('event_time', sa.DateTime(), nullable=True, comment='UTC source event time'),
    sa.Column('as_of_timestamp', sa.DateTime(), nullable=False, comment='UTC feature cutoff'),
    sa.Column('latency_ms', sa.Float(), nullable=True),
    sa.Column('outcome_label_id', sa.UUID(), nullable=True),
    sa.Column('outcome_label', sa.String(length=16), nullable=True),
    sa.Column('outcome_recorded_at', sa.DateTime(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['assessment_id'], ['threat_assessments.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['model_id'], ['ml_models.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['outcome_label_id'], ['ml_labels.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['person_id'], ['identities.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['snapshot_id'], ['ml_feature_snapshots.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['threshold_id'], ['ml_model_thresholds.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    op.create_table('ml_shadow_comparisons',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('prediction_id', sa.UUID(), nullable=False),
    sa.Column('model_id', sa.UUID(), nullable=True),
    sa.Column('assessment_id', sa.UUID(), nullable=True),
    sa.Column('subject_id', sa.String(length=64), nullable=False),
    sa.Column('pipeline_id', sa.String(length=255), nullable=True),
    sa.Column('rule_threat_score', sa.Float(), nullable=False, comment='heuristic 0-100 (risk engine)'),
    sa.Column('rule_threat_severity', sa.String(length=16), nullable=False, comment='low|moderate|high|critical'),
    sa.Column('behavioral_anomaly_score', sa.Float(), nullable=True),
    sa.Column('ml_anomaly_band', sa.String(length=16), nullable=True, comment='normal|elevated|unusual|highly_unusual — different concept from threat severity, shown side by side only'),
    sa.Column('rule_would_alert', sa.Boolean(), nullable=False, comment="the RULES engine's severity crossed its alerting bar"),
    sa.Column('ml_would_flag_anomaly', sa.Boolean(), nullable=False, comment='the anomaly band crossed the review-flag bar'),
    sa.Column('operational_disagreement', sa.String(length=16), nullable=False, comment='both_flagged | rules_only | anomaly_only | neither'),
    sa.Column('ml_failed', sa.Boolean(), nullable=False),
    sa.Column('failure_reason', sa.String(length=64), nullable=True),
    sa.Column('ml_latency_ms', sa.Float(), nullable=True),
    sa.Column('missing_features', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['assessment_id'], ['threat_assessments.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['model_id'], ['ml_models.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['prediction_id'], ['ml_predictions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ml_drift_reports',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('report_kind', sa.String(length=32), nullable=False, comment='data_drift | prediction_drift'),
    sa.Column('model_id', sa.UUID(), nullable=True),
    sa.Column('scope_type', sa.String(length=16), nullable=False),
    sa.Column('scope_id', sa.String(length=255), nullable=False),
    sa.Column('baseline_start', sa.DateTime(), nullable=True),
    sa.Column('baseline_end', sa.DateTime(), nullable=True),
    sa.Column('baseline_stats', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('baseline_sample_count', sa.Integer(), nullable=True),
    sa.Column('window_start', sa.DateTime(), nullable=False),
    sa.Column('window_end', sa.DateTime(), nullable=False),
    sa.Column('sample_count', sa.Integer(), nullable=False),
    sa.Column('insufficient_data', sa.Boolean(), nullable=False),
    sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment='per-feature {psi, ks_stat, ks_p, js_divergence}; prediction {score_hist_shift, volume, shadow_failure_rate, disagreement, latency_p95, fallback_rate, pipeline_mix}'),
    sa.Column('severity', sa.String(length=16), nullable=False, comment='normal | warning | critical'),
    sa.Column('job_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['model_id'], ['ml_models.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('ml_retraining_policies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('model_type', sa.String(length=64), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('schedule_interval_hours', sa.Integer(), nullable=False),
    sa.Column('min_new_labels', sa.Integer(), nullable=False),
    sa.Column('min_total_labels', sa.Integer(), nullable=False),
    sa.Column('cooldown_hours', sa.Integer(), nullable=False),
    sa.Column('min_drift_reports', sa.Integer(), nullable=False, comment='never retrain on one weak statistical signal'),
    sa.Column('promotion_criteria', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='advisory only; promotion is manual'),
    sa.Column('last_triggered_at', sa.DateTime(), nullable=True),
    sa.Column('last_trigger_reason', sa.String(length=128), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('updated_by', sa.String(length=255), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('model_type')
    )
    op.create_table('ml_audit_log',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False, comment='mode_change | shadow_approve | model_reject | model_rollback | threshold_activate | label_create | label_review | policy_update | training_requested | training_cancelled | pause | ...'),
    sa.Column('object_type', sa.String(length=32), nullable=True),
    sa.Column('object_id', sa.String(length=64), nullable=True),
    sa.Column('actor_user_id', sa.Integer(), nullable=True),
    sa.Column('actor_username', sa.String(length=100), nullable=True),
    sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('ip_address', sa.String(length=45), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_ml_audit_action', 'ml_audit_log', ['action', 'created_at'], unique=False)
    op.create_index('idx_ml_audit_object', 'ml_audit_log', ['object_type', 'object_id', 'created_at'], unique=False)
    op.create_index('idx_ml_dataset_status', 'ml_datasets', ['status'], unique=False)
    op.create_index('uq_ml_dataset_name_version', 'ml_datasets', ['name', 'version'], unique=True)
    op.create_index('idx_ml_feature_def_entity_active', 'ml_feature_definitions', ['entity_type', 'is_active'], unique=False)
    op.create_index('uq_ml_feature_def_name_version', 'ml_feature_definitions', ['name', 'version'], unique=True)
    op.create_index('idx_ml_snapshot_computed', 'ml_feature_snapshots', ['computed_at'], unique=False)
    op.create_index('idx_ml_snapshot_entity_asof', 'ml_feature_snapshots', ['entity_type', 'entity_id', 'as_of_timestamp'], unique=False)
    op.create_index('idx_ml_snapshot_run', 'ml_feature_snapshots', ['computation_run_id'], unique=False)
    op.create_index('uq_ml_snapshot_identity', 'ml_feature_snapshots', ['entity_type', 'entity_id', 'feature_set_version', 'as_of_timestamp'], unique=True)
    op.create_index('idx_ml_model_job', 'ml_models', ['training_job_id'], unique=False)
    op.create_index('idx_ml_model_type_stage', 'ml_models', ['model_type', 'stage'], unique=False)
    op.create_index('uq_ml_model_type_version', 'ml_models', ['model_type', 'version'], unique=True)
    op.create_index('uq_ml_models_one_production', 'ml_models', ['model_type'], unique=True, postgresql_where=sa.text("stage = 'production'"))
    op.create_index('uq_ml_models_one_shadow', 'ml_models', ['model_type'], unique=True, postgresql_where=sa.text("stage = 'shadow'"))
    op.create_index('idx_ml_drift_model_created', 'ml_drift_reports', ['model_id', 'created_at'], unique=False)
    op.create_index('idx_ml_drift_severity', 'ml_drift_reports', ['severity', 'created_at'], unique=False)
    op.create_index('idx_ml_label_created', 'ml_labels', ['created_at'], unique=False)
    op.create_index('idx_ml_label_review', 'ml_labels', ['label', 'label_kind', 'review_status'], unique=False)
    op.create_index('idx_ml_label_subject', 'ml_labels', ['subject_type', 'subject_id', 'status'], unique=False)
    op.create_index(op.f('ix_ml_labels_person_id'), 'ml_labels', ['person_id'], unique=False)
    op.create_index('idx_ml_threshold_lookup', 'ml_model_thresholds', ['model_id', 'scope_type', 'scope_id', 'status'], unique=False)
    op.create_index(op.f('ix_ml_model_thresholds_model_id'), 'ml_model_thresholds', ['model_id'], unique=False)
    op.create_index('uq_ml_threshold_scope_version', 'ml_model_thresholds', ['model_id', 'scope_type', 'scope_id', 'objective', 'version'], unique=True)
    op.create_index('idx_ml_pred_created', 'ml_predictions', ['created_at'], unique=False)
    op.create_index('idx_ml_pred_fallback', 'ml_predictions', ['fallback_reason'], unique=False, postgresql_where=sa.text('fallback_reason IS NOT NULL'))
    op.create_index('idx_ml_pred_mode', 'ml_predictions', ['actual_mode_used', 'created_at'], unique=False)
    op.create_index('idx_ml_pred_model_created', 'ml_predictions', ['model_id', 'created_at'], unique=False)
    op.create_index('idx_ml_pred_subject', 'ml_predictions', ['subject_type', 'subject_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_ml_predictions_person_id'), 'ml_predictions', ['person_id'], unique=False)
    op.create_index('idx_ml_shadow_created', 'ml_shadow_comparisons', ['created_at'], unique=False)
    op.create_index('idx_ml_shadow_disagreement', 'ml_shadow_comparisons', ['operational_disagreement', 'created_at'], unique=False)
    op.create_index('idx_ml_shadow_model_created', 'ml_shadow_comparisons', ['model_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_ml_shadow_comparisons_prediction_id'), 'ml_shadow_comparisons', ['prediction_id'], unique=False)
    op.add_column('threat_assessments', sa.Column('decision_mode', sa.String(length=16), nullable=True, comment='NULL = legacy/rules; rules | shadow (hybrid/ml reserved)'))
    op.add_column('threat_assessments', sa.Column('ml_prediction_id', sa.UUID(), nullable=True))
    op.create_foreign_key('fk_threat_assessments_ml_prediction', 'threat_assessments', 'ml_predictions', ['ml_prediction_id'], ['id'], ondelete='SET NULL', use_alter=True)

    # Seed the four model-type retraining policies, DISABLED by default.
    # Every NOT NULL column is supplied explicitly — the ORM defaults are
    # Python-side and do not exist as server defaults.
    from datetime import datetime as _dt
    policies = sa.table(
        "ml_retraining_policies",
        sa.column("model_type", sa.String), sa.column("enabled", sa.Boolean),
        sa.column("schedule_interval_hours", sa.Integer),
        sa.column("min_new_labels", sa.Integer),
        sa.column("min_total_labels", sa.Integer),
        sa.column("cooldown_hours", sa.Integer),
        sa.column("min_drift_reports", sa.Integer),
        sa.column("updated_at", sa.DateTime),
    )
    _now = _dt.utcnow()
    op.bulk_insert(policies, [
        {"model_type": name, "enabled": False, "schedule_interval_hours": 168,
         "min_new_labels": 25, "min_total_labels": 100, "cooldown_hours": 168,
         "min_drift_reports": 2, "updated_at": _now}
        for name in ("behavior_anomaly_model", "coappearance_anomaly_model",
                     "social_graph_anomaly_model", "threat_ranking_model")
    ])


def downgrade() -> None:
    op.drop_constraint('fk_threat_assessments_ml_prediction',
                       'threat_assessments', type_='foreignkey')
    op.drop_column('threat_assessments', 'ml_prediction_id')
    op.drop_column('threat_assessments', 'decision_mode')
    op.drop_table('ml_audit_log')
    op.drop_table('ml_retraining_policies')
    op.drop_table('ml_drift_reports')
    op.drop_table('ml_shadow_comparisons')
    op.drop_table('ml_predictions')
    op.drop_table('ml_model_thresholds')
    op.drop_table('ml_models')
    op.drop_table('ml_datasets')
    op.drop_table('ml_labels')
    op.drop_table('ml_collection_checkpoints')
    op.drop_table('ml_feature_snapshots')
    op.drop_table('ml_feature_definitions')
