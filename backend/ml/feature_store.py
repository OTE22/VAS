"""
Feature store: versioned definitions + point-in-time snapshots.

One inventory drives BOTH offline snapshots (training) and online compute
(inference) — the same builders, the same definitions, no training-serving
skew. Definitions are seeded idempotently (never silently mutated: a
changed definition means a NEW version row).

Honesty rules: features a builder cannot compute are recorded in
unavailable_features with the reason; deferred features are seeded
INACTIVE with the reason in their description. Snapshots are idempotent on
(entity_type, entity_id, feature_set_version, as_of).
"""

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ml.constants import FEATURE_SET_VERSION
from backend.ml.feature_builders import (
    BUILDERS, FeatureUnavailable, load_person_context)
from config import settings

logger = logging.getLogger(__name__)

_DEF_CACHE_TTL = 60.0


class MLConfigurationError(RuntimeError):
    """The ML feature layer is not configured (no seeded definitions) — a
    boot/config defect, never something to paper over with empty data."""

# ---------------------------------------------------------------------------
# The v1 inventory. entity/computation/params/leakage/active — deferred
# features carry their reason in the description and is_active=False.
# ---------------------------------------------------------------------------
FEATURE_INVENTORY: List[Dict[str, Any]] = [
    # --- ACTIVE person features -------------------------------------------
    {"name": "appearance_count_7d", "entity_type": "person", "window": "7d",
     "source": "identity_appearances", "computation": "appearance_count",
     "params": {"days": 7}, "description": "Appearances in the 7 days before as_of"},
    {"name": "appearance_count_30d", "entity_type": "person", "window": "30d",
     "source": "identity_appearances", "computation": "appearance_count",
     "params": {"days": 30}, "description": "Appearances in the 30 days before as_of"},
    {"name": "distinct_pipelines_7d", "entity_type": "person", "window": "7d",
     "source": "identity_appearances", "computation": "distinct_pipelines",
     "params": {"days": 7}, "description": "Unique cameras in the last 7 days"},
    {"name": "distinct_pipelines_30d", "entity_type": "person", "window": "30d",
     "source": "identity_appearances", "computation": "distinct_pipelines",
     "params": {"days": 30}, "description": "Unique cameras in the last 30 days"},
    {"name": "days_since_first_seen", "entity_type": "person", "window": None,
     "source": "identity_appearances", "computation": "days_since_first_seen",
     "params": {}, "description": "Age of the identity's history at as_of (days)"},
    {"name": "days_since_last_seen", "entity_type": "person", "window": None,
     "source": "identity_appearances", "computation": "days_since_last_seen",
     "params": {}, "description": "Gap between the last pre-as_of appearance and as_of (days)"},
    {"name": "active_days_ratio_30d", "entity_type": "person", "window": "30d",
     "source": "identity_appearances", "computation": "active_days_ratio",
     "params": {"days": 30}, "description": "Share of the last 30 days with >=1 appearance"},
    {"name": "off_hours_ratio_30d", "entity_type": "person", "window": "30d",
     "source": "identity_appearances", "computation": "off_hours_ratio",
     "params": {"days": 30},
     "description": "Share of appearances in the configured LOCAL off-hours window (per-pipeline IANA tz)"},
    {"name": "night_count_30d", "entity_type": "person", "window": "30d",
     "source": "identity_appearances", "computation": "night_count",
     "params": {"days": 30}, "description": "Appearances between 00:00-04:59 local"},
    {"name": "weekend_holiday_ratio_30d", "entity_type": "person", "window": "30d",
     "source": "identity_appearances", "computation": "weekend_holiday_ratio",
     "params": {"days": 30},
     "description": "Share of appearances on weekend/holiday day buckets (local)"},
    {"name": "hour_sin_30d", "entity_type": "person", "window": "30d",
     "source": "identity_appearances", "computation": "hour_sin",
     "params": {"days": 30}, "description": "sin of the circular-mean local hour"},
    {"name": "hour_cos_30d", "entity_type": "person", "window": "30d",
     "source": "identity_appearances", "computation": "hour_cos",
     "params": {"days": 30}, "description": "cos of the circular-mean local hour"},
    {"name": "hour_std_30d", "entity_type": "person", "window": "30d",
     "source": "identity_appearances", "computation": "hour_std",
     "params": {"days": 30}, "description": "Circular std of local appearance hours"},
    {"name": "baseline_hour_deviation_last", "entity_type": "person", "window": None,
     "source": "identity_appearances", "computation": "baseline_hour_deviation_last",
     "params": {},
     "description": "Bucketed-baseline deviation ratio of the latest pre-as_of appearance (capped at 10)"},
    {"name": "max_hourly_burst_30d", "entity_type": "person", "window": "30d",
     "source": "identity_appearances", "computation": "max_hourly_burst",
     "params": {"days": 30}, "description": "Max appearances within any single hour"},
    {"name": "new_pipeline_flag_7d", "entity_type": "person", "window": "7d",
     "source": "identity_appearances", "computation": "new_pipeline_flag",
     "params": {"recent_days": 7},
     "description": "1.0 when a camera appears in the last 7d that is absent from the prior 83d"},
    {"name": "is_unknown_identity", "entity_type": "person", "window": None,
     "source": "identities", "computation": "is_unknown_identity",
     "params": {},
     "description": "Identity type at compute time (LIMITATION: type is mutable)"},
    # --- ACTIVE pair feature (readiness-gated at compute time) -------------
    {"name": "pair_co_appearance_count_30d", "entity_type": "pair", "window": "30d",
     "source": "identity_appearances", "computation": "pair_co_appearance_count",
     "params": {"days": 30},
     "readiness_requirements": {"min_pair_appearances": "ML_GRAPH_MIN_PAIR_APPEARANCES"},
     "description": "Windowed same-camera co-appearances; unavailable below the pair floor"},
    # --- DEFERRED (seeded inactive, honest reasons) ------------------------
    {"name": "degree_centrality_90d", "entity_type": "person", "window": "90d",
     "source": "graph", "computation": "graph_degree_centrality", "params": {},
     "is_active": False,
     "readiness_requirements": {"min_nodes": "ML_GRAPH_MIN_NODES",
                                "min_edges": "ML_GRAPH_MIN_EDGES",
                                "min_observation_days": "ML_GRAPH_MIN_OBSERVATION_DAYS"},
     "description": "DEFERRED: graph below readiness floors (nodes/edges/span); "
                    "activating requires the gates to pass"},
    {"name": "pagerank_90d", "entity_type": "person", "window": "90d",
     "source": "graph", "computation": "graph_pagerank", "params": {},
     "is_active": False,
     "readiness_requirements": {"min_nodes": "ML_GRAPH_MIN_NODES",
                                "min_edges": "ML_GRAPH_MIN_EDGES"},
     "description": "DEFERRED: graph below readiness floors"},
    {"name": "clustering_coeff_90d", "entity_type": "person", "window": "90d",
     "source": "graph", "computation": "graph_clustering", "params": {},
     "is_active": False,
     "readiness_requirements": {"min_nodes": "ML_GRAPH_MIN_NODES",
                                "min_edges": "ML_GRAPH_MIN_EDGES"},
     "description": "DEFERRED: graph below readiness floors"},
    {"name": "mean_recognition_confidence_30d", "entity_type": "person", "window": "30d",
     "source": "detections", "computation": "mean_recognition_confidence",
     "params": {"days": 30}, "is_active": False,
     "description": "DEFERRED: no identity-linked confidence column exists in the "
                    "current schema — activating requires a real source, not a proxy"},
    {"name": "pipeline_known_ratio_30d", "entity_type": "pipeline", "window": "30d",
     "source": "identity_appearances", "computation": "pipeline_known_ratio",
     "params": {"days": 30}, "is_active": False,
     "description": "DEFERRED: v1 anomaly model is person-entity only"},
    {"name": "assessment_count_30d", "entity_type": "person", "window": "30d",
     "source": "threat_assessments", "computation": "assessment_count",
     "params": {"days": 30}, "is_active": False, "leakage_class": "target_adjacent",
     "description": "DEFERRED + target_adjacent: past assessments are outcome-"
                    "adjacent; excluded until a supervised design justifies them"},
]


def features_checksum(features: Dict[str, Any]) -> str:
    canonical = json.dumps(features, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class FeatureStore:

    def __init__(self):
        self._def_cache: Dict[str, Tuple[float, List[dict]]] = {}

    def invalidate(self) -> None:
        self._def_cache.clear()

    async def verify_definitions(self, db: AsyncSession) -> Dict[str, Any]:
        """Boot-time contract check, FAIL-CLOSED in every environment.

        Feature definitions are seeded by Alembic from a FROZEN literal
        (migration d4e5f6a7b8c9), never by application code at runtime. This
        verifies that every entry of FEATURE_INVENTORY exists in
        ml_feature_definitions with an identical spec. A missing definition or a
        drifted spec is a configuration error: it means the code was changed
        without the migration that must accompany it. Raises RuntimeError.
        """
        from db_models import MLFeatureDefinition
        rows = (await db.execute(select(MLFeatureDefinition))).scalars().all()
        by_key = {(r.name, int(r.version)): r for r in rows}
        missing, drift = [], []
        for item in FEATURE_INVENTORY:
            row = by_key.get((item["name"], 1))
            if row is None:
                missing.append(item["name"])
                continue
            expected = {
                "entity_type": item["entity_type"], "value_type": "float",
                "window": item.get("window"), "source": item["source"],
                "computation": item["computation"], "params": dict(item.get("params", {})),
                "leakage_class": item.get("leakage_class", "safe"),
                "is_active": bool(item.get("is_active", True)),
            }
            actual = {
                "entity_type": row.entity_type, "value_type": row.value_type,
                "window": row.window, "source": row.source,
                "computation": row.computation, "params": dict(row.params or {}),
                "leakage_class": row.leakage_class, "is_active": bool(row.is_active),
            }
            if expected != actual:
                drift.append({"name": item["name"], "expected": expected, "actual": actual})
        if missing or drift:
            raise RuntimeError(
                "ML feature definitions do not match the migrated table — "
                f"missing={missing} drifted={[d['name'] for d in drift]}. "
                "Definitions are seeded ONLY by Alembic (frozen literal); a changed "
                "definition needs a new version row in a new migration.")
        self.invalidate()
        return {"verified": len(FEATURE_INVENTORY), "rows": len(rows)}

    async def get_active_definitions(self, db: AsyncSession,
                                     entity_type: Optional[str] = None) -> List[dict]:
        cache_key = entity_type or "*"
        cached = self._def_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < _DEF_CACHE_TTL:
            return cached[1]
        from db_models import MLFeatureDefinition
        query = select(MLFeatureDefinition).where(
            MLFeatureDefinition.is_active.is_(True))
        if entity_type:
            query = query.where(MLFeatureDefinition.entity_type == entity_type)
        rows = (await db.execute(query.order_by(MLFeatureDefinition.name))).scalars().all()
        defs = [{
            "name": r.name, "version": r.version, "entity_type": r.entity_type,
            "computation": r.computation, "params": dict(r.params or {}),
            "leakage_class": r.leakage_class, "window": r.window,
            "readiness_requirements": r.readiness_requirements,
        } for r in rows]
        self._def_cache[cache_key] = (now, defs)
        return defs

    # ------------------------------------------------------------------
    # Snapshots (offline + online share this exact code path)
    # ------------------------------------------------------------------

    async def compute_person_snapshot(self, db: AsyncSession, identity_id: str,
                                      as_of: datetime, *,
                                      run_id: Optional[str] = None,
                                      event_ts: Optional[datetime] = None,
                                      persist: bool = True) -> Dict[str, Any]:
        """Compute (and by default persist, idempotently) one person snapshot.

        Point-in-time correctness lives in load_person_context (start_time <
        as_of). Unavailable features carry reasons — no fake zeros.
        """
        definitions = await self.get_active_definitions(db, "person")
        if not definitions:
            # A snapshot with zero features is meaningless ML data. This is a
            # configuration error (definitions are migration-seeded), so it
            # fails loudly at the collection boundary — never a silent empty row.
            raise MLConfigurationError(
                "NO_ACTIVE_FEATURE_DEFINITIONS: ml_feature_definitions has no active "
                "person definitions; the ML migration (d4e5f6a7b8c9) has not been applied")
        ctx = await load_person_context(db, identity_id, as_of)
        features: Dict[str, float] = {}
        unavailable: Dict[str, str] = {}
        for definition in definitions:
            builder = BUILDERS.get(definition["computation"])
            if builder is None:
                unavailable[definition["name"]] = "builder_not_implemented"
                continue
            try:
                features[definition["name"]] = round(float(builder(ctx, definition["params"])), 6)
            except FeatureUnavailable as e:
                unavailable[definition["name"]] = e.reason
            except Exception as e:
                logger.warning("[ML_OPS] builder %s failed for %s: %s",
                               definition["name"], identity_id, e)
                unavailable[definition["name"]] = f"builder_error:{type(e).__name__}"

        tz_used = None
        if ctx["rows"]:
            last_pipe = ctx["rows"][-1][0]
            tz_used = ctx["pipe_timezones"].get(last_pipe) or str(
                settings.DEFAULT_SITE_TIMEZONE)

        if not features:
            raise MLConfigurationError(
                "NO_FEATURES_COMPUTED: every active definition was unavailable or its "
                "builder is not implemented — refusing to persist an empty snapshot")
        checksum = features_checksum(features)
        payload = {
            "entity_type": "person",
            "entity_id": str(identity_id),
            "feature_set_version": FEATURE_SET_VERSION,
            "as_of_timestamp": as_of,
            "features": features,
            "unavailable_features": unavailable,
            "features_checksum": checksum,
            "local_timezone": tz_used,
            "snapshot_id": None,
            "deduplicated": False,
        }
        if not persist:
            return payload

        from db_models import MLFeatureSnapshot
        stmt = pg_insert(MLFeatureSnapshot).values(
            entity_type="person",
            entity_id=str(identity_id),
            feature_set_version=FEATURE_SET_VERSION,
            as_of_timestamp=as_of,
            event_timestamp=event_ts,
            computed_at=datetime.utcnow(),
            features=features,
            unavailable_features=unavailable,
            features_checksum=checksum,
            local_timezone=tz_used,
            computation_run_id=run_id,
            source_row_counts=ctx["source_row_counts"],
        ).on_conflict_do_nothing(
            index_elements=["entity_type", "entity_id",
                            "feature_set_version", "as_of_timestamp"])
        result = await db.execute(stmt)
        inserted = bool(result.rowcount)
        row = (await db.execute(
            select(MLFeatureSnapshot).where(
                MLFeatureSnapshot.entity_type == "person",
                MLFeatureSnapshot.entity_id == str(identity_id),
                MLFeatureSnapshot.feature_set_version == FEATURE_SET_VERSION,
                MLFeatureSnapshot.as_of_timestamp == as_of,
            ))).scalar_one()
        payload["snapshot_id"] = row.id
        payload["deduplicated"] = not inserted
        if not inserted:
            # The stored snapshot is authoritative for this (entity, as_of).
            payload["features"] = dict(row.features or {})
            payload["unavailable_features"] = dict(row.unavailable_features or {})
            payload["features_checksum"] = row.features_checksum
        return payload

    async def get_features_as_of(self, db: AsyncSession, entity_type: str,
                                 entity_id: str, as_of: datetime) -> Optional[Dict[str, Any]]:
        """Latest stored snapshot at or before as_of (training/audit path)."""
        from db_models import MLFeatureSnapshot
        row = (await db.execute(
            select(MLFeatureSnapshot)
            .where(
                MLFeatureSnapshot.entity_type == entity_type,
                MLFeatureSnapshot.entity_id == str(entity_id),
                MLFeatureSnapshot.feature_set_version == FEATURE_SET_VERSION,
                MLFeatureSnapshot.as_of_timestamp <= as_of,
            )
            .order_by(MLFeatureSnapshot.as_of_timestamp.desc())
            .limit(1)
        )).scalar_one_or_none()
        if row is None:
            return None
        return {
            "snapshot_id": row.id,
            "as_of_timestamp": row.as_of_timestamp,
            "features": dict(row.features or {}),
            "unavailable_features": dict(row.unavailable_features or {}),
            "features_checksum": row.features_checksum,
        }

    async def compute_online_features(self, db: AsyncSession,
                                      identity_id: str) -> Dict[str, Any]:
        """Fresh point-in-time compute for inference — SAME builders and
        definitions as training. as_of rounds to the minute so repeated
        calls within a minute dedup onto one snapshot row."""
        as_of = datetime.utcnow().replace(second=0, microsecond=0)
        return await self.compute_person_snapshot(
            db, identity_id, as_of, run_id="online", event_ts=None, persist=True)

    @staticmethod
    def feature_vector(features: Dict[str, float],
                       feature_names: List[str]) -> Tuple[List[float], List[str]]:
        """Canonical ordering against a model's feature_names; missing
        features are reported, never silently imputed here (the model
        artifact records its own imputation policy)."""
        vector: List[float] = []
        missing: List[str] = []
        for name in feature_names:
            value = features.get(name)
            if value is None or (isinstance(value, float) and not (value == value)):
                missing.append(name)
                vector.append(float("nan"))
            else:
                vector.append(float(value))
        return vector, missing


# Global instance
feature_store = FeatureStore()
