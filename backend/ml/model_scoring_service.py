"""Governed on-demand scoring for the new model families.

This service is deliberately admin-facing and observational.  It never
mutates a threat assessment, alert, watchlist or decision mode.  Anomaly
models may score only from the registry's approved shadow stage; a threat
ranker may score only a validated, explicitly identified candidate and its
output is labelled as relative analyst priority.
"""

import math
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import or_, select

from backend.ml.constants import (
    MODEL_TYPE_COAPPEARANCE_ANOMALY, MODEL_TYPE_SOCIAL_GRAPH_ANOMALY,
    MODEL_TYPE_THREAT_RANKING)
from backend.ml.model_specs import get_model_spec
from backend.ml.registry_service import (
    RegistryError, preprocess_feature_vector, registry_service,
    score_with_payload, validate_artifact)


def _explain(payload: Dict[str, Any], features: Dict[str, float]) -> Dict[str, Any]:
    factors = []
    for name in payload["feature_names"]:
        if name not in features:
            continue
        median = float(payload["imputation_medians"][name])
        factors.append({"feature": name, "value": float(features[name]),
                        "deviation_from_training_median":
                            round(abs(float(features[name]) - median), 6)})
    factors.sort(key=lambda item: item["deviation_from_training_median"], reverse=True)
    return {"method": "median_deviation", "top_factors": factors[:5]}


async def _load_model(db, model_type: str, model_id: Optional[str]):
    spec = get_model_spec(model_type)
    if model_id:
        row = await registry_service.get_model(db, model_id)
        if row is None or row.model_type != model_type:
            raise RegistryError("MODEL_NOT_FOUND", "model not found for the requested type")
        allowed = ("validated", "shadow") if model_type == MODEL_TYPE_THREAT_RANKING else ("shadow",)
        if row.stage not in allowed:
            raise RegistryError("MODEL_STAGE_NOT_SERVABLE",
                                f"{model_type} must be in {allowed}, found {row.stage}")
    else:
        stage = "validated" if model_type == MODEL_TYPE_THREAT_RANKING else "shadow"
        row = await registry_service.get_stage_model(db, model_type, stage)
        if row is None:
            raise RegistryError("MODEL_NOT_AVAILABLE", f"no {stage} {model_type} is available")
    payload = validate_artifact(
        row.artifact_path, expected_hash=row.artifact_hash,
        expected_feature_names=row.feature_names,
        expected_dependencies=row.dependency_versions)
    if payload.get("feature_set_version") != spec.feature_set_version:
        raise RegistryError("FEATURE_SCHEMA_MISMATCH",
                            "model artifact does not match the model-family feature set")
    return row, payload, spec


async def _score_snapshot(db, *, model_type: str, snapshot: Dict[str, Any],
                          model_id: Optional[str] = None) -> Dict[str, Any]:
    started = time.monotonic()
    row, payload, spec = await _load_model(db, model_type, model_id)
    vector, missing = preprocess_feature_vector(payload, snapshot["features"])
    if len(missing) == len(payload["feature_names"]):
        raise RegistryError("MISSING_REQUIRED_FEATURES", "all model features are unavailable")
    score = float(score_with_payload(payload, [vector])[0])
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise RegistryError("INVALID_PREDICTION", "model produced a non-finite or out-of-range score")
    band = None
    threshold = None
    if "anomaly" in model_type:
        from backend.ml.threshold_service import threshold_service, version_label
        threshold = await threshold_service.get_active(db, model_id=row.id)
        if threshold is None:
            raise RegistryError("THRESHOLD_UNRESOLVED", "shadow model has no active threshold set")
        cp = dict(threshold.cutpoints or {})
        band = ("highly_unusual" if score >= cp["highly_unusual"] else
                "unusual" if score >= cp["unusual"] else
                "elevated" if score >= cp["elevated"] else "normal")
        threshold = version_label(threshold)
    return {"model_id": str(row.id), "model_type": model_type,
            "model_version": f"{model_type}-v{row.version}",
            "model_purpose": spec.model_purpose, "score_type": spec.score_type,
            "is_probability": False, "calibration_status": spec.calibration_status,
            "score": round(score, 6), "band": band, "threshold_version": threshold,
            "subject_type": snapshot["entity_type"], "subject_id": snapshot["entity_id"],
            "feature_set_version": snapshot["feature_set_version"],
            "features_checksum": snapshot["features_checksum"],
            "missing_features": missing,
            "unavailable_features": snapshot.get("unavailable_features") or {},
            "explanation": _explain(payload, snapshot["features"]),
            "applied_to_live_result": False,
            "latency_ms": round((time.monotonic() - started) * 1000.0, 3)}


async def score_relational_subject(db, *, model_type: str, identity_id: str,
                                   related_identity_id: Optional[str] = None,
                                   model_id: Optional[str] = None) -> Dict[str, Any]:
    now = datetime.utcnow().replace(second=0, microsecond=0)
    run_id = f"on-demand-{uuid.uuid4().hex[:12]}"
    if model_type == MODEL_TYPE_COAPPEARANCE_ANOMALY:
        if not related_identity_id:
            raise RegistryError("PAIR_ID_REQUIRED", "related_identity_id is required for pair scoring")
        from db_models import IdentityRelationship
        from backend.ml.relational_feature_service import canonical_pair, compute_pair_snapshot
        left, right, _ = canonical_pair(identity_id, related_identity_id)
        relationship = (await db.execute(select(IdentityRelationship).where(or_(
            (IdentityRelationship.identity_id_1 == uuid.UUID(left)) &
            (IdentityRelationship.identity_id_2 == uuid.UUID(right)),
            (IdentityRelationship.identity_id_1 == uuid.UUID(right)) &
            (IdentityRelationship.identity_id_2 == uuid.UUID(left)),
        )))).scalar_one_or_none()
        if relationship is None:
            raise RegistryError("PAIR_NOT_FOUND", "no cached relationship exists for this pair")
        snapshot = await compute_pair_snapshot(
            db, relationship, as_of=now, run_id=run_id, persist=True)
    elif model_type == MODEL_TYPE_SOCIAL_GRAPH_ANOMALY:
        from backend.ml.relational_feature_service import compute_social_graph_snapshot
        snapshot = await compute_social_graph_snapshot(
            db, identity_id, as_of=now, run_id=run_id, persist=True)
    else:
        raise RegistryError("MODEL_TYPE_NOT_RELATIONAL", "requested model is not a relational anomaly model")
    await db.commit()
    return await _score_snapshot(db, model_type=model_type, snapshot=snapshot, model_id=model_id)


async def rank_identities(db, identity_ids: Iterable[str], *,
                          model_id: Optional[str] = None) -> Dict[str, Any]:
    from backend.ml.feature_store import feature_store

    items = []
    for identity_id in dict.fromkeys(str(value) for value in identity_ids):
        try:
            snapshot = await feature_store.compute_online_features(db, identity_id)
            scored = await _score_snapshot(
                db, model_type=MODEL_TYPE_THREAT_RANKING,
                snapshot={**snapshot, "entity_type": "person", "entity_id": identity_id},
                model_id=model_id)
            items.append(scored)
        except Exception as exc:
            code = exc.code if isinstance(exc, RegistryError) else "FEATURE_COMPUTATION_FAILED"
            items.append({"subject_type": "person", "subject_id": identity_id,
                          "error_code": code, "message": str(exc)[:300],
                          "applied_to_live_result": False})
    successful = [item for item in items if item.get("score") is not None]
    successful.sort(key=lambda item: (-item["score"], item["subject_id"]))
    failed = [item for item in items if item.get("score") is None]
    return {"items": successful + failed, "total": len(items),
            "scored": len(successful), "failed": len(failed),
            "semantics": "relative analyst review priority; not a threat probability",
            "applied_to_live_result": False}

