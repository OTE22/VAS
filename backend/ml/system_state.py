"""
ML system state — the facts an administrator needs to see at a glance on the
ML-Ops page, each read from the database/settings at request time (never
typed into the page), plus the release notes of the core changes so the
page explains WHY those facts look the way they do.

Path-free by construction: file names only, never directories.
"""

import os
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from backend.ml.constants import (
    FEATURE_SET_VERSION, MODEL_TYPE_BEHAVIOR_ANOMALY, PREVIOUS_FEATURE_SET_VERSION)
from backend.ml.dataset_definitions import (
    EXTRACTION_POLICY_VERSION, LEGACY_EXTRACTION_POLICY_VERSION, feature_set_limitations,
    list_definitions)

# Core changes, newest first. Each entry names the contract it introduced so
# the page can point the administrator at the right place.
RELEASE_NOTES: List[Dict[str, Any]] = [
    {"id": "relational-models", "title": "Relational and threat-ranking model families",
     "detail": "Coappearance pairs and social-graph nodes now have isolated point-in-time "
               "feature sets, typed datasets and shadow-only anomaly trainers. Threat ranking "
               "uses reviewed labels for offline analyst priority and is never called a threat probability."},
    {"id": "call-log", "title": "Every ML-Ops call is logged",
     "detail": "One structured record per /api/ml/* request (request id, actor, route, status, "
               "error code, duration, produced ids) in the application log, tagged [MLOPS_CALL]; "
               "readable in the Recent Calls card and via GET /api/ml/calls."},
    {"id": "features-v2", "title": "Feature set secintel-features-v2",
     "detail": "is_unknown_identity is now the type AS OF the snapshot (promote/merge audit "
               "trail); days_since_last_seen is exact; windowed features refuse beyond the "
               "5000-row window cap instead of undercounting. Models trained under v1 never "
               "score v2 snapshots (FEATURE_SCHEMA_MISMATCH) — train and approve a v2 model."},
    {"id": "dataset-lifecycle", "title": "Datasets are provenance: verify or archive, never sweep",
     "detail": "Legacy datasets gain a file hash only after their rows reproduce the registered "
               "checksum; archive is an explicit action refused for datasets a model was trained from."},
    {"id": "shadow-evidence", "title": "Shadow evidence report",
     "detail": "Per band: predictions, reviewed outcomes and their split, severity x band, "
               "disagreement mix, score quantiles. Descriptive; the mapping decision stays "
               "REQUIRES_VALIDATION."},
    {"id": "pipeline", "title": "Reusable dataset + training pipeline",
     "detail": "Typed extraction definitions with an explicit cap policy (refuse by default, "
               "counts recorded), Parquet file hash + sidecar manifest, one dataset for many "
               "experiments, complete training_config and code revision on every model, "
               "task-aware candidate-vs-incumbent comparison, python -m backend.ml.pipeline CLI."},
    {"id": "preprocessing", "title": "One train/serve preprocessing rule",
     "detail": "Missing features take the artifact's training median in training AND inference; "
               "artifacts without a median per feature are refused."},
    {"id": "stage-lookup", "title": "Overview survives several validated candidates",
     "detail": "The stage lookup returns the newest model per non-unique stage instead of failing "
               "after the second training."},
    {"id": "persistence", "title": "Persistent artifact storage in production compose",
     "detail": "ml_artifacts_data volume for models, datasets and manifests; the registry reports "
               "artifact_present / file_present so a vanished file is never silently served."},
]


def _gate_of(model_row, gate: str) -> Optional[str]:
    """The gate status the trainer recorded on a model (None when the model
    predates gate recording — reported as such, never assumed)."""
    if model_row is None:
        return None
    report = getattr(model_row, "evaluation_report", None) or {}
    block = report.get(gate) if isinstance(report, dict) else None
    return block.get("status") if isinstance(block, dict) else "NOT_RECORDED"


async def ml_system_state(db: AsyncSession) -> Dict[str, Any]:
    from db_models import MLDataset, MLFeatureDefinition, MLModel
    from backend.ml import call_log
    from backend.ml.decision_service import decision_service
    from backend.ml.registry_service import registry_service

    shadow = await registry_service.get_stage_model(db, MODEL_TYPE_BEHAVIOR_ANOMALY, "shadow")
    active_defs = (await db.execute(
        select(MLFeatureDefinition.name, MLFeatureDefinition.version)
        .where(MLFeatureDefinition.is_active.is_(True),
               MLFeatureDefinition.entity_type == "person")
        .order_by(MLFeatureDefinition.name))).all()
    by_stage = {row[0]: int(row[1]) for row in (await db.execute(
        select(MLModel.stage, sa_func.count(MLModel.id)).group_by(MLModel.stage))).all()}
    ds_total = int((await db.execute(select(sa_func.count(MLDataset.id)))).scalar() or 0)
    ds_built = int((await db.execute(
        select(sa_func.count(MLDataset.id)).where(MLDataset.status == "built"))).scalar() or 0)
    ds_legacy_unverified = int((await db.execute(
        select(sa_func.count(MLDataset.id)).where(MLDataset.status == "built",
                                                  MLDataset.parquet_sha256.is_(None)))).scalar() or 0)
    ds_archived = int((await db.execute(
        select(sa_func.count(MLDataset.id)).where(MLDataset.status == "archived"))).scalar() or 0)
    ds_legacy_policy = int((await db.execute(
        select(sa_func.count(MLDataset.id)).where(MLDataset.extraction.is_(None)))).scalar() or 0)
    models_missing_file = 0
    for path in (await db.execute(
            select(MLModel.artifact_path).where(MLModel.stage.in_(("shadow", "validated"))))).scalars():
        if path and not os.path.exists(path):
            models_missing_file += 1

    shadow_fs = shadow.feature_set_version if shadow else None
    shadow_compatible = (shadow_fs == FEATURE_SET_VERSION) if shadow else None
    alerts: List[Dict[str, str]] = []
    if shadow and not shadow_compatible:
        alerts.append({
            "code": "SHADOW_MODEL_FEATURE_SET_MISMATCH", "level": "warn",
            "message": (f"The shadow model v{shadow.version} was trained under {shadow_fs}; current "
                        f"snapshots are {FEATURE_SET_VERSION}. Every live assessment falls back "
                        "(FEATURE_SCHEMA_MISMATCH) until a model trained on the current feature "
                        "set is approved into shadow.")})
    if shadow is None:
        alerts.append({"code": "NO_SHADOW_MODEL", "level": "info",
                       "message": "No model is in shadow; rules decide alone and nothing is observed."})
    if ds_legacy_unverified:
        alerts.append({"code": "LEGACY_DATASETS_UNVERIFIED", "level": "info",
                       "message": f"{ds_legacy_unverified} built dataset(s) predate file hashing; "
                                  "verify them to reuse them for training."})
    if models_missing_file:
        alerts.append({"code": "ARTIFACT_FILES_MISSING", "level": "warn",
                       "message": f"{models_missing_file} registered shadow/validated model(s) point at an "
                                  "artifact file that is absent — they cannot be served or approved."})

    # Non-evidence labels present (demo seed / synthetic corpus): they are
    # EXCLUDED from every evidence statistic by the canonical filter, and
    # their presence is stated - never silently cleaned up.
    non_evidence_by_source: Dict[str, int] = {}
    try:
        from sqlalchemy import or_ as sa_or
        from db_models import MLLabel
        from backend.ml.evidence_grade import NON_EVIDENCE_SOURCE_PREFIXES
        rows = (await db.execute(
            select(MLLabel.source, sa_func.count())
            .where(sa_or(*[MLLabel.source.ilike(p + "%") for p in NON_EVIDENCE_SOURCE_PREFIXES]))
            .group_by(MLLabel.source))).all()
        non_evidence_by_source = {str(src): int(n) for src, n in rows}
    except Exception:
        non_evidence_by_source = {}
    if non_evidence_by_source:
        alerts.append({"code": "NON_EVIDENCE_LABELS_PRESENT", "level": "warn",
                       "message": (f"{sum(non_evidence_by_source.values())} label(s) from demo-seed / "
                                   "synthetic sources exist (" +
                                   ", ".join(f"{k}: {v}" for k, v in sorted(non_evidence_by_source.items())) +
                                   "). They are excluded from all evidence statistics; remove them "
                                   "with the generating script before production use.")})

    # ACTIVE feature definitions whose computation has no registered builder
    # cannot be computed by anything (today: the seeded pair feature). Reported
    # as a fact - never silently deactivated.
    unreachable_definitions = []
    try:
        from backend.ml.feature_builders import BUILDERS
        rows = (await db.execute(
            select(MLFeatureDefinition.name, MLFeatureDefinition.entity_type, MLFeatureDefinition.computation)
            .where(MLFeatureDefinition.is_active.is_(True)))).all()
        relational_builders = {
            "pair_co_appearance_count", "pair_relationship_count",
            "pair_relationship_rate", "pair_common_pipelines", "pair_percentage",
            "pair_span_days", "pair_recency_days", "graph_degree_centrality_v1",
            "graph_weighted_degree_log_v1", "graph_pagerank_v1",
            "graph_clustering_v1", "graph_bridge_ratio_v1",
            "graph_mean_edge_weight_v1",
        }
        unreachable_definitions = [
            {"name": name, "entity_type": etype, "computation": comp}
            for name, etype, comp in rows
            if comp not in BUILDERS and comp not in relational_builders]
    except Exception:
        unreachable_definitions = []
    if unreachable_definitions:
        alerts.append({"code": "FEATURE_DEFINITION_UNREACHABLE", "level": "info",
                       "message": ("active feature definition(s) with no registered builder - declared, "
                                   "never computed: " +
                                   ", ".join(f"{d['name']} ({d['entity_type']}/{d['computation']})"
                                             for d in unreachable_definitions))})

    # LIVE scientific readiness is projected without persistence. Query
    # endpoints must never change governance state merely because an operator
    # opened a dashboard.
    live_readiness = None
    if shadow is not None:
        try:
            from backend.ml.readiness import compute_model_readiness
            live_readiness = await compute_model_readiness(db, shadow, persist=False, light=True)
        except Exception:
            logger.warning("[ML_OPS] live readiness computation failed", exc_info=True)
            live_readiness = None

    availability = await decision_service.mode_availability(db)
    try:
        from sqlalchemy import text as sa_text
        migration_head = (await db.execute(sa_text("SELECT version_num FROM alembic_version"))).scalar()
    except Exception:
        migration_head = None

    # ---- the ML contract, in the exact vocabulary operators read ----------
    ml_mode_available = availability["modes"]["ml"]["available"]
    mapping = await decision_service._mapping_policy(db)
    contract = {
        "dataset": "VALID_FOR_EXPERIMENTATION",
        "feature_set": "ACTIVE",
        "model": ("SHADOW_APPROVED" if shadow and shadow_compatible
                  else "SHADOW_INCOMPATIBLE" if shadow else "NONE"),
        "engineering_readiness": _gate_of(shadow, "engineering_gate"),
        "scientific_validity": ((live_readiness or {}).get("scientific_gate") or {}).get("status")
                               or _gate_of(shadow, "scientific_gate") or "INSUFFICIENT_EVIDENCE",
        "signal_mapping": "VALIDATED" if mapping else "REQUIRES_VALIDATION",
        "ml_decision_authority": "ENABLED" if (ml_mode_available and availability["current_mode"] == "ml") else "DISABLED",
        "rules": "AUTHORITATIVE",
        "fallback": "RULES",
        "evidence_collection": "ACTIVE" if (shadow and shadow_compatible and availability["current_mode"] == "shadow") else "INACTIVE",
        "note": ("a model may run in SHADOW (engineering PASS) while its scientific validity is "
                 "INSUFFICIENT_EVIDENCE; only a validated, scope-matched signal mapping plus "
                 "ML mode makes ML supply an operational input — and even then the rules engine "
                 "computes the final score"),
    }
    try:
        from backend.ml import metrics as ml_metrics
        coverage = ((live_readiness or {}).get("reviewed_outcome_coverage") or {}).get("review_coverage_overall")
        ml_metrics.set_state(ml_authority=contract["ml_decision_authority"] == "ENABLED",
                             mapping_validated=bool(mapping), shadow_model=shadow,
                             review_coverage=(float(coverage) if coverage is not None else None))
    except Exception:
        pass

    return {
        "ml_contract": contract,
        "feature_set": {
            "current": FEATURE_SET_VERSION,
            "previous": PREVIOUS_FEATURE_SET_VERSION,
            "active_person_features": [{"name": n, "version": int(v)} for n, v in active_defs],
            "limitations": feature_set_limitations(FEATURE_SET_VERSION),
        },
        "decision": {
            "requested_mode": availability["current_mode"],
            "live_engine": "rules",
            "ml_role": ("observational" if availability["current_mode"] == "shadow" and shadow else "none"),
            "gated": {m: info["reasons"] for m, info in availability["modes"].items() if not info["available"]},
        },
        "shadow_model": ({"version": shadow.version, "algorithm": shadow.algorithm,
                          "feature_set_version": shadow_fs,
                          "compatible_with_current_features": shadow_compatible,
                          "artifact_present": bool(shadow.artifact_path and os.path.exists(shadow.artifact_path))}
                         if shadow else None),
        "datasets": {"total": ds_total, "built": ds_built, "archived": ds_archived,
                     "legacy_unverified": ds_legacy_unverified,
                     "legacy_extraction_policy": ds_legacy_policy,
                     "extraction_policy_version": EXTRACTION_POLICY_VERSION,
                     "legacy_extraction_policy_version": LEGACY_EXTRACTION_POLICY_VERSION,
                     "definitions": [d.key for d in list_definitions()]},
        "models": {"by_stage": by_stage, "missing_artifact_files": models_missing_file},
        "call_log": {"enabled": True, "sink": "application log (tag [MLOPS_CALL])",
                     "readable_via": "/api/ml/calls"},
        "scientific_readiness": ((live_readiness or {}).get("scientific_gate")
                                 if live_readiness else None),
        "evidence_populations": (((live_readiness or {}).get("reviewed_outcome_coverage") or {})
                                 .get("populations") if live_readiness else None),
        "non_evidence_labels": non_evidence_by_source,
        "unreachable_feature_definitions": unreachable_definitions,
        "migration_head": migration_head,
        "alerts": alerts,
        "release_notes": RELEASE_NOTES,
    }
