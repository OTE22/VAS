"""ML-Ops lineage: threshold SETS with an explicit lifecycle, prediction
lineage indexes, drift reports bound to a model, dataset lineage summary, and
the feature definitions seeded from a FROZEN literal.

Revision ID: d4e5f6a7b8c9
Revises: c2d3e4f5a6b7

Threshold sets. `ml_model_thresholds` was one row per (model, scope, objective,
version) — three rows per model, `status` never leaving 'candidate', nothing
reading them at inference. A prediction consumes all three cutpoints at once,
so the grain becomes ONE row per (model, scope, version) holding the full set:

    DROP  threshold, objective
    ADD   cutpoints JSONB NOT NULL {elevated, unusual, highly_unusual}
          quantiles JSONB NULL
          source VARCHAR(32) NOT NULL DEFAULT 'training'
          retired_at TIMESTAMP NULL, retired_by VARCHAR(255) NULL, notes TEXT NULL
    KEEP  expected_metrics, version, status, sample_count, created_at,
          activated_at, activated_by
    constraints:
          uq_ml_threshold_scope_version (model_id, scope_type, scope_id, version)
          uq_ml_threshold_one_active    (model_id, scope_type, scope_id) WHERE status='active'
          ck_ml_threshold_scope_canonical ((scope_type='global') = (scope_id=''))
          ck_ml_threshold_status, ck_ml_threshold_cutpoints, ck_ml_threshold_source

Existing objective rows (none in any known database) are collapsed with
jsonb_object_agg before the old columns are dropped — nothing referenced is
deleted, and the migration refuses if any prediction still points at an
objective-grain row.

Feature definitions. `feature_store.seed_definitions()` had no caller, so an
unseeded database produced feature-less snapshots. The 24 v1 definitions are
seeded here from FROZEN_FEATURE_DEFINITIONS — a literal copied into this file,
NOT imported from application code, so a later code edit cannot change what an
already-applied migration seeded. Changing a definition = a new version row in
a new migration; boot verifies the runtime inventory against the table.

Drift reports must name the model they were computed for: model_id becomes
NOT NULL, guarded by a precondition that refuses (never deletes) if NULL rows
exist — the operator repair script removes them.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd4e5f6a7b8c9'
down_revision = 'c2d3e4f5a6b7'
branch_labels = None
depends_on = None

REPAIR = "python scripts/repair_relationship_integrity.py --apply --yes-i-understand (dry-run first; dev/demo only)"


def _require_zero(bind, sql: str, what: str) -> None:
    n = bind.execute(sa.text(sql)).scalar() or 0
    if n:
        raise RuntimeError(
            f"migration d4e5f6a7b8c9 refuses: {n} row(s) {what}. "
            f"This migration never deletes data; repair first: {REPAIR}")


FROZEN_FEATURE_DEFINITIONS = [
    {
        "name": "appearance_count_7d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "7d",
        "source": "identity_appearances",
        "computation": "appearance_count",
        "params": {
            "days": 7
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Appearances in the 7 days before as_of",
        "is_active": True
    },
    {
        "name": "appearance_count_30d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "30d",
        "source": "identity_appearances",
        "computation": "appearance_count",
        "params": {
            "days": 30
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Appearances in the 30 days before as_of",
        "is_active": True
    },
    {
        "name": "distinct_pipelines_7d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "7d",
        "source": "identity_appearances",
        "computation": "distinct_pipelines",
        "params": {
            "days": 7
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Unique cameras in the last 7 days",
        "is_active": True
    },
    {
        "name": "distinct_pipelines_30d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "30d",
        "source": "identity_appearances",
        "computation": "distinct_pipelines",
        "params": {
            "days": 30
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Unique cameras in the last 30 days",
        "is_active": True
    },
    {
        "name": "days_since_first_seen",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": None,
        "source": "identity_appearances",
        "computation": "days_since_first_seen",
        "params": {},
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Age of the identity's history at as_of (days)",
        "is_active": True
    },
    {
        "name": "days_since_last_seen",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": None,
        "source": "identity_appearances",
        "computation": "days_since_last_seen",
        "params": {},
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Gap between the last pre-as_of appearance and as_of (days)",
        "is_active": True
    },
    {
        "name": "active_days_ratio_30d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "30d",
        "source": "identity_appearances",
        "computation": "active_days_ratio",
        "params": {
            "days": 30
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Share of the last 30 days with >=1 appearance",
        "is_active": True
    },
    {
        "name": "off_hours_ratio_30d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "30d",
        "source": "identity_appearances",
        "computation": "off_hours_ratio",
        "params": {
            "days": 30
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Share of appearances in the configured LOCAL off-hours window (per-pipeline IANA tz)",
        "is_active": True
    },
    {
        "name": "night_count_30d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "30d",
        "source": "identity_appearances",
        "computation": "night_count",
        "params": {
            "days": 30
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Appearances between 00:00-04:59 local",
        "is_active": True
    },
    {
        "name": "weekend_holiday_ratio_30d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "30d",
        "source": "identity_appearances",
        "computation": "weekend_holiday_ratio",
        "params": {
            "days": 30
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Share of appearances on weekend/holiday day buckets (local)",
        "is_active": True
    },
    {
        "name": "hour_sin_30d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "30d",
        "source": "identity_appearances",
        "computation": "hour_sin",
        "params": {
            "days": 30
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "sin of the circular-mean local hour",
        "is_active": True
    },
    {
        "name": "hour_cos_30d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "30d",
        "source": "identity_appearances",
        "computation": "hour_cos",
        "params": {
            "days": 30
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "cos of the circular-mean local hour",
        "is_active": True
    },
    {
        "name": "hour_std_30d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "30d",
        "source": "identity_appearances",
        "computation": "hour_std",
        "params": {
            "days": 30
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Circular std of local appearance hours",
        "is_active": True
    },
    {
        "name": "baseline_hour_deviation_last",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": None,
        "source": "identity_appearances",
        "computation": "baseline_hour_deviation_last",
        "params": {},
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Bucketed-baseline deviation ratio of the latest pre-as_of appearance (capped at 10)",
        "is_active": True
    },
    {
        "name": "max_hourly_burst_30d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "30d",
        "source": "identity_appearances",
        "computation": "max_hourly_burst",
        "params": {
            "days": 30
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Max appearances within any single hour",
        "is_active": True
    },
    {
        "name": "new_pipeline_flag_7d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "7d",
        "source": "identity_appearances",
        "computation": "new_pipeline_flag",
        "params": {
            "recent_days": 7
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "1.0 when a camera appears in the last 7d that is absent from the prior 83d",
        "is_active": True
    },
    {
        "name": "is_unknown_identity",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": None,
        "source": "identities",
        "computation": "is_unknown_identity",
        "params": {},
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "Identity type at compute time (LIMITATION: type is mutable)",
        "is_active": True
    },
    {
        "name": "pair_co_appearance_count_30d",
        "version": 1,
        "entity_type": "pair",
        "value_type": "float",
        "window": "30d",
        "source": "identity_appearances",
        "computation": "pair_co_appearance_count",
        "params": {
            "days": 30
        },
        "leakage_class": "safe",
        "readiness_requirements": {
            "min_pair_appearances": "ML_GRAPH_MIN_PAIR_APPEARANCES"
        },
        "description": "Windowed same-camera co-appearances; unavailable below the pair floor",
        "is_active": True
    },
    {
        "name": "degree_centrality_90d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "90d",
        "source": "graph",
        "computation": "graph_degree_centrality",
        "params": {},
        "leakage_class": "safe",
        "readiness_requirements": {
            "min_nodes": "ML_GRAPH_MIN_NODES",
            "min_edges": "ML_GRAPH_MIN_EDGES",
            "min_observation_days": "ML_GRAPH_MIN_OBSERVATION_DAYS"
        },
        "description": "DEFERRED: graph below readiness floors (nodes/edges/span); activating requires the gates to pass",
        "is_active": False
    },
    {
        "name": "pagerank_90d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "90d",
        "source": "graph",
        "computation": "graph_pagerank",
        "params": {},
        "leakage_class": "safe",
        "readiness_requirements": {
            "min_nodes": "ML_GRAPH_MIN_NODES",
            "min_edges": "ML_GRAPH_MIN_EDGES"
        },
        "description": "DEFERRED: graph below readiness floors",
        "is_active": False
    },
    {
        "name": "clustering_coeff_90d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "90d",
        "source": "graph",
        "computation": "graph_clustering",
        "params": {},
        "leakage_class": "safe",
        "readiness_requirements": {
            "min_nodes": "ML_GRAPH_MIN_NODES",
            "min_edges": "ML_GRAPH_MIN_EDGES"
        },
        "description": "DEFERRED: graph below readiness floors",
        "is_active": False
    },
    {
        "name": "mean_recognition_confidence_30d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "30d",
        "source": "detections",
        "computation": "mean_recognition_confidence",
        "params": {
            "days": 30
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "DEFERRED: no identity-linked confidence column exists in the current schema — activating requires a real source, not a proxy",
        "is_active": False
    },
    {
        "name": "pipeline_known_ratio_30d",
        "version": 1,
        "entity_type": "pipeline",
        "value_type": "float",
        "window": "30d",
        "source": "identity_appearances",
        "computation": "pipeline_known_ratio",
        "params": {
            "days": 30
        },
        "leakage_class": "safe",
        "readiness_requirements": None,
        "description": "DEFERRED: v1 anomaly model is person-entity only",
        "is_active": False
    },
    {
        "name": "assessment_count_30d",
        "version": 1,
        "entity_type": "person",
        "value_type": "float",
        "window": "30d",
        "source": "threat_assessments",
        "computation": "assessment_count",
        "params": {
            "days": 30
        },
        "leakage_class": "target_adjacent",
        "readiness_requirements": None,
        "description": "DEFERRED + target_adjacent: past assessments are outcome-adjacent; excluded until a supervised design justifies them",
        "is_active": False
    }
]



def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------ ml_model_thresholds
    op.execute("ALTER TABLE ml_model_thresholds ADD COLUMN IF NOT EXISTS cutpoints JSONB")
    op.execute("ALTER TABLE ml_model_thresholds ADD COLUMN IF NOT EXISTS quantiles JSONB")
    op.execute("ALTER TABLE ml_model_thresholds ADD COLUMN IF NOT EXISTS source VARCHAR(32) NOT NULL DEFAULT 'training'")
    op.execute("ALTER TABLE ml_model_thresholds ADD COLUMN IF NOT EXISTS retired_at TIMESTAMP")
    op.execute("ALTER TABLE ml_model_thresholds ADD COLUMN IF NOT EXISTS retired_by VARCHAR(255)")
    op.execute("ALTER TABLE ml_model_thresholds ADD COLUMN IF NOT EXISTS notes TEXT")

    # Collapse any objective rows into sets: one surviving row per
    # (model, scope, version) receives the aggregated cutpoints; the other rows
    # of the same set are dropped only after their values were folded in and
    # only if nothing references them (ml_predictions.threshold_id was never
    # written by any code path; the migration refuses otherwise).
    has_objective = bind.execute(sa.text(
        "SELECT 1 FROM information_schema.columns WHERE table_name='ml_model_thresholds' "
        "AND column_name='objective'")).scalar()
    if has_objective:
        _require_zero(bind,
            "SELECT count(*) FROM ml_predictions p WHERE p.threshold_id IS NOT NULL",
            "in ml_predictions reference objective-grain threshold rows that are being regrained")
        op.execute("""
            WITH sets AS (
                SELECT model_id, scope_type, scope_id, version,
                       jsonb_object_agg(replace(objective, 'band_', ''), threshold) AS cp,
                       min(id::text)::uuid AS keep_id
                FROM ml_model_thresholds GROUP BY model_id, scope_type, scope_id, version)
            UPDATE ml_model_thresholds t SET cutpoints = s.cp
            FROM sets s WHERE t.id = s.keep_id
        """)
        op.execute("""
            DELETE FROM ml_model_thresholds t
            USING (SELECT model_id, scope_type, scope_id, version, min(id::text)::uuid AS keep_id
                   FROM ml_model_thresholds GROUP BY model_id, scope_type, scope_id, version) s
            WHERE t.model_id = s.model_id AND t.scope_type = s.scope_type
              AND t.scope_id = s.scope_id AND t.version = s.version AND t.id <> s.keep_id
        """)
        op.execute("DROP INDEX IF EXISTS uq_ml_threshold_scope_version")
        op.execute("ALTER TABLE ml_model_thresholds DROP COLUMN objective")
        op.execute("ALTER TABLE ml_model_thresholds DROP COLUMN threshold")

    _require_zero(bind, "SELECT count(*) FROM ml_model_thresholds WHERE cutpoints IS NULL",
                  "in ml_model_thresholds have no cutpoints after regraining")
    op.execute("ALTER TABLE ml_model_thresholds ALTER COLUMN cutpoints SET NOT NULL")
    op.execute("ALTER TABLE ml_model_thresholds ALTER COLUMN scope_id SET NOT NULL")
    op.execute("ALTER TABLE ml_model_thresholds ALTER COLUMN scope_id SET DEFAULT ''")

    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ml_threshold_scope_version "
               "ON ml_model_thresholds (model_id, scope_type, scope_id, version)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ml_threshold_one_active "
               "ON ml_model_thresholds (model_id, scope_type, scope_id) WHERE status = 'active'")
    for name, check in (
        ("ck_ml_threshold_scope_canonical", "(scope_type = 'global') = (scope_id = '')"),
        ("ck_ml_threshold_status", "status IN ('candidate', 'active', 'retired')"),
        ("ck_ml_threshold_source", "source IN ('training', 'manual', 'recalibration')"),
        ("ck_ml_threshold_cutpoints",
         "cutpoints ?& ARRAY['elevated', 'unusual', 'highly_unusual'] "
         "AND (cutpoints->>'elevated')::float8 <= (cutpoints->>'unusual')::float8 "
         "AND (cutpoints->>'unusual')::float8 <= (cutpoints->>'highly_unusual')::float8"),
    ):
        op.execute(f"ALTER TABLE ml_model_thresholds DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE ml_model_thresholds ADD CONSTRAINT {name} CHECK ({check})")

    # ------------------------------------------------------ ml_predictions
    op.execute("CREATE INDEX IF NOT EXISTS idx_ml_pred_assessment ON ml_predictions (assessment_id) "
               "WHERE assessment_id IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ml_pred_subject_event ON ml_predictions (subject_id, event_time) "
               "WHERE event_time IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ml_pred_outcome_label ON ml_predictions (outcome_label_id) "
               "WHERE outcome_label_id IS NOT NULL")

    # ---------------------------------------------------- ml_drift_reports
    # Precondition proven HERE, immediately before SET NOT NULL — never
    # assumed from the operator having run the repair.
    _require_zero(bind, "SELECT count(*) FROM ml_drift_reports WHERE model_id IS NULL",
                  "in ml_drift_reports have NULL model_id")
    op.execute("ALTER TABLE ml_drift_reports ALTER COLUMN model_id SET NOT NULL")
    # NOT NULL + SET NULL would make a model delete fail; a report about a
    # deleted model is meaningless, so it follows the model.
    op.execute("ALTER TABLE ml_drift_reports DROP CONSTRAINT IF EXISTS ml_drift_reports_model_id_fkey")
    op.execute("ALTER TABLE ml_drift_reports ADD CONSTRAINT ml_drift_reports_model_id_fkey "
               "FOREIGN KEY (model_id) REFERENCES ml_models (id) ON DELETE CASCADE")

    # -------------------------------------------------------- ml_datasets
    op.execute("ALTER TABLE ml_datasets ADD COLUMN IF NOT EXISTS lineage_summary JSONB")

    # ------------------------------------------- feature definitions (frozen)
    defs = sa.table(
        'ml_feature_definitions',
        sa.column('id', postgresql.UUID(as_uuid=True)),
        sa.column('name', sa.String), sa.column('version', sa.Integer),
        sa.column('entity_type', sa.String), sa.column('value_type', sa.String),
        sa.column('window', sa.String), sa.column('source', sa.String),
        sa.column('computation', sa.String), sa.column('params', postgresql.JSONB),
        sa.column('leakage_class', sa.String),
        sa.column('readiness_requirements', postgresql.JSONB),
        sa.column('description', sa.Text), sa.column('is_active', sa.Boolean),
        sa.column('created_at', sa.DateTime),
    )
    import uuid as _uuid
    from datetime import datetime as _dt
    for row in FROZEN_FEATURE_DEFINITIONS:
        stmt = postgresql.insert(defs).values(
            id=_uuid.uuid4(), created_at=_dt.utcnow(), **row
        ).on_conflict_do_nothing(index_elements=['name', 'version'])
        bind.execute(stmt)


def downgrade() -> None:
    """Undo the DDL; never restores the objective grain from data (there is no
    lossless inverse of the set) and never deletes seeded definitions."""
    op.execute("ALTER TABLE ml_datasets DROP COLUMN IF EXISTS lineage_summary")
    op.execute("ALTER TABLE ml_drift_reports ALTER COLUMN model_id DROP NOT NULL")
    op.execute("ALTER TABLE ml_drift_reports DROP CONSTRAINT IF EXISTS ml_drift_reports_model_id_fkey")
    op.execute("ALTER TABLE ml_drift_reports ADD CONSTRAINT ml_drift_reports_model_id_fkey "
               "FOREIGN KEY (model_id) REFERENCES ml_models (id) ON DELETE SET NULL")
    for idx in ("idx_ml_pred_assessment", "idx_ml_pred_subject_event", "idx_ml_pred_outcome_label"):
        op.execute(f"DROP INDEX IF EXISTS {idx}")
    for name in ("ck_ml_threshold_scope_canonical", "ck_ml_threshold_status",
                 "ck_ml_threshold_source", "ck_ml_threshold_cutpoints"):
        op.execute(f"ALTER TABLE ml_model_thresholds DROP CONSTRAINT IF EXISTS {name}")
    op.execute("DROP INDEX IF EXISTS uq_ml_threshold_one_active")
    op.execute("DROP INDEX IF EXISTS uq_ml_threshold_scope_version")
    op.execute("ALTER TABLE ml_model_thresholds ADD COLUMN IF NOT EXISTS objective VARCHAR(32)")
    op.execute("ALTER TABLE ml_model_thresholds ADD COLUMN IF NOT EXISTS threshold DOUBLE PRECISION")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_ml_threshold_scope_version "
               "ON ml_model_thresholds (model_id, scope_type, scope_id, objective, version)")
    for col in ("notes", "retired_by", "retired_at", "source", "quantiles", "cutpoints"):
        op.execute(f"ALTER TABLE ml_model_thresholds DROP COLUMN IF EXISTS {col}")
