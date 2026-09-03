"""Point-in-time pair and social-graph feature computation.

These families are intentionally separate from behavioural person features.
Snapshots are taken from the relationship cache as it exists at collection
time; the code never rewrites historical graphs from today's mutable edges.
Every missing readiness condition is persisted as a reason, never a zero.
"""

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Tuple

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ml.feature_builders import FeatureUnavailable, build_pair_co_appearance_count
from backend.ml.model_specs import COAPPEARANCE_FEATURE_SET, SOCIAL_GRAPH_FEATURE_SET
from config import settings


def canonical_pair(identity_a: str, identity_b: str) -> Tuple[str, str, str]:
    left, right = sorted((str(identity_a), str(identity_b)))
    if left == right:
        raise ValueError("a coappearance pair requires two different identities")
    return left, right, f"{left}|{right}"


def _checksum(features: Dict[str, float]) -> str:
    raw = json.dumps(features, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


async def _persist_snapshot(db: AsyncSession, *, entity_type: str, entity_id: str,
                            feature_set_version: str, as_of: datetime,
                            event_ts: datetime, features: Dict[str, float],
                            unavailable: Dict[str, str], run_id: str,
                            source_counts: Dict[str, int]) -> Dict[str, Any]:
    from db_models import MLFeatureSnapshot

    checksum = _checksum(features)
    stmt = pg_insert(MLFeatureSnapshot).values(
        entity_type=entity_type, entity_id=entity_id,
        feature_set_version=feature_set_version, as_of_timestamp=as_of,
        event_timestamp=event_ts, computed_at=datetime.utcnow(), features=features,
        unavailable_features=unavailable, features_checksum=checksum,
        computation_run_id=run_id, source_row_counts=source_counts,
    ).on_conflict_do_nothing(index_elements=[
        "entity_type", "entity_id", "feature_set_version", "as_of_timestamp"])
    result = await db.execute(stmt)
    row = (await db.execute(select(MLFeatureSnapshot).where(
        MLFeatureSnapshot.entity_type == entity_type,
        MLFeatureSnapshot.entity_id == entity_id,
        MLFeatureSnapshot.feature_set_version == feature_set_version,
        MLFeatureSnapshot.as_of_timestamp == as_of,
    ))).scalar_one()
    return {"snapshot_id": row.id, "deduplicated": not bool(result.rowcount),
            "features": dict(row.features or {}),
            "unavailable_features": dict(row.unavailable_features or {}),
            "features_checksum": row.features_checksum,
            "feature_set_version": feature_set_version,
            "as_of_timestamp": as_of}


async def compute_pair_snapshot(db: AsyncSession, relationship, *, as_of: datetime,
                                run_id: str, persist: bool = True) -> Dict[str, Any]:
    left, right, pair_id = canonical_pair(
        relationship.identity_id_1, relationship.identity_id_2)
    unavailable: Dict[str, str] = {}
    try:
        count_30d = await build_pair_co_appearance_count(
            db, left, right, as_of, {"days": 30})
    except FeatureUnavailable as exc:
        count_30d = None
        unavailable["pair_co_appearance_count_30d"] = exc.reason

    count = max(0, int(relationship.co_appearance_count or 0))
    first = relationship.first_co_appearance
    last = relationship.last_co_appearance
    span_days = max(0.0, (last - first).total_seconds() / 86400.0) \
        if first and last else 0.0
    recency = max(0.0, (as_of - last).total_seconds() / 86400.0) if last else None
    percentage = relationship.co_appearance_percentage
    features = {
        "pair_co_appearance_count_90d": float(count),
        "pair_co_appearance_rate_per_day": float(count / max(1.0, span_days)),
        "pair_common_pipeline_count": float(len(set(relationship.common_pipelines or []))),
        "pair_co_appearance_percentage": min(1.0, max(0.0, float(percentage or 0.0) / 100.0)),
        "pair_relationship_span_days": float(span_days),
    }
    if count_30d is not None:
        features["pair_co_appearance_count_30d"] = float(count_30d)
    if recency is None:
        unavailable["pair_days_since_last_coappearance"] = "last_coappearance_missing"
    else:
        features["pair_days_since_last_coappearance"] = float(recency)
    if not persist:
        return {"entity_type": "pair", "entity_id": pair_id,
                "feature_set_version": COAPPEARANCE_FEATURE_SET,
                "as_of_timestamp": as_of, "features": features,
                "unavailable_features": unavailable, "features_checksum": _checksum(features)}
    return await _persist_snapshot(
        db, entity_type="pair", entity_id=pair_id,
        feature_set_version=COAPPEARANCE_FEATURE_SET, as_of=as_of,
        event_ts=as_of, features=features, unavailable=unavailable,
        run_id=run_id, source_counts={"identity_relationships": 1})


def _graph_metrics(edges: Iterable[Any]) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Any]]:
    adjacency: Dict[str, Dict[str, float]] = defaultdict(dict)
    first_times, last_times = [], []
    edge_count = 0
    for edge in edges:
        left, right, _ = canonical_pair(edge.identity_id_1, edge.identity_id_2)
        weight = max(1.0, float(edge.co_appearance_count or 1))
        adjacency[left][right] = weight
        adjacency[right][left] = weight
        edge_count += 1
        if edge.first_co_appearance:
            first_times.append(edge.first_co_appearance)
        if edge.last_co_appearance:
            last_times.append(edge.last_co_appearance)
    nodes = sorted(adjacency)
    n = len(nodes)
    span_days = ((max(last_times) - min(first_times)).total_seconds() / 86400.0
                 if first_times and last_times else 0.0)
    stats = {"nodes": n, "edges": edge_count, "span_days": round(max(0.0, span_days), 3)}
    reasons = []
    if n < int(settings.ML_GRAPH_MIN_NODES):
        reasons.append(f"graph_below_min_nodes({n}<{int(settings.ML_GRAPH_MIN_NODES)})")
    if edge_count < int(settings.ML_GRAPH_MIN_EDGES):
        reasons.append(f"graph_below_min_edges({edge_count}<{int(settings.ML_GRAPH_MIN_EDGES)})")
    if span_days < int(settings.ML_GRAPH_MIN_OBSERVATION_DAYS):
        reasons.append("graph_observation_span_too_short"
                       f"({span_days:.1f}d<{int(settings.ML_GRAPH_MIN_OBSERVATION_DAYS)}d)")
    stats["ready"] = not reasons
    stats["reasons"] = reasons
    if reasons:
        return {}, stats

    damping = 0.85
    ranks = {node: 1.0 / n for node in nodes}
    strengths = {node: sum(adjacency[node].values()) for node in nodes}
    for _ in range(30):
        ranks = {node: (1.0 - damping) / n + damping * sum(
            ranks[other] * weight / strengths[other]
            for other, weight in adjacency[node].items()) for node in nodes}

    metrics: Dict[str, Dict[str, float]] = {}
    for node in nodes:
        neighbours = set(adjacency[node])
        possible = len(neighbours) * (len(neighbours) - 1) / 2
        linked = sum(1 for a in neighbours for b in neighbours
                     if a < b and b in adjacency[a])
        clustering = linked / possible if possible else 0.0
        weights = list(adjacency[node].values())
        metrics[node] = {
            "graph_degree_centrality_90d": len(neighbours) / max(1, n - 1),
            "graph_weighted_degree_log_90d": math.log1p(sum(weights)),
            "graph_pagerank_90d": ranks[node],
            "graph_clustering_coefficient_90d": clustering,
            "graph_bridge_ratio_90d": 1.0 - clustering if possible else 0.0,
            "graph_mean_edge_weight_90d": sum(weights) / len(weights),
        }
    return metrics, stats


async def collect_relational_snapshots(db: AsyncSession, *, run_id: str,
                                       as_of: datetime) -> Dict[str, Any]:
    """Capture pair and graph state once per collection run.

    Only edges already calculated at ``as_of`` and observed in the trailing
    90 days are visible.  A graph below configured readiness floors writes no
    misleading vectors and reports the exact reasons to the job result.
    """
    from db_models import IdentityRelationship

    floor = as_of - timedelta(days=90)
    edges = list((await db.execute(select(IdentityRelationship).where(
        IdentityRelationship.calculated_at <= as_of,
        or_(IdentityRelationship.last_co_appearance.is_(None),
            IdentityRelationship.last_co_appearance >= floor),
    ).order_by(IdentityRelationship.identity_id_1,
               IdentityRelationship.identity_id_2))).scalars().all())
    pair_written = pair_dedup = 0
    for edge in edges:
        snap = await compute_pair_snapshot(db, edge, as_of=as_of,
                                           run_id=run_id, persist=True)
        pair_dedup += int(snap["deduplicated"])
        pair_written += int(not snap["deduplicated"])

    graph, graph_stats = _graph_metrics(edges)
    graph_written = graph_dedup = 0
    if graph_stats["ready"]:
        for identity_id, features in graph.items():
            snap = await _persist_snapshot(
                db, entity_type="person", entity_id=identity_id,
                feature_set_version=SOCIAL_GRAPH_FEATURE_SET, as_of=as_of,
                event_ts=as_of, features=features, unavailable={}, run_id=run_id,
                source_counts={"identity_relationships": len(edges)})
            graph_dedup += int(snap["deduplicated"])
            graph_written += int(not snap["deduplicated"])
    return {"relationship_edges": len(edges), "pair_snapshots_written": pair_written,
            "pair_snapshots_deduplicated": pair_dedup,
            "graph_snapshots_written": graph_written,
            "graph_snapshots_deduplicated": graph_dedup,
            "graph_readiness": graph_stats}


async def compute_social_graph_snapshot(db: AsyncSession, identity_id: str, *,
                                        as_of: datetime, run_id: str,
                                        persist: bool = True) -> Dict[str, Any]:
    """Compute one node against one consistent graph read for on-demand shadow scoring."""
    from db_models import IdentityRelationship

    floor = as_of - timedelta(days=90)
    edges = list((await db.execute(select(IdentityRelationship).where(
        IdentityRelationship.calculated_at <= as_of,
        or_(IdentityRelationship.last_co_appearance.is_(None),
            IdentityRelationship.last_co_appearance >= floor),
    ))).scalars().all())
    graph, stats = _graph_metrics(edges)
    if not stats["ready"]:
        raise FeatureUnavailable(";".join(stats["reasons"]))
    features = graph.get(str(identity_id))
    if features is None:
        raise FeatureUnavailable("identity_not_present_in_ready_graph")
    if not persist:
        return {"entity_type": "person", "entity_id": str(identity_id),
                "feature_set_version": SOCIAL_GRAPH_FEATURE_SET,
                "as_of_timestamp": as_of, "features": features,
                "unavailable_features": {}, "features_checksum": _checksum(features)}
    return await _persist_snapshot(
        db, entity_type="person", entity_id=str(identity_id),
        feature_set_version=SOCIAL_GRAPH_FEATURE_SET, as_of=as_of,
        event_ts=as_of, features=features, unavailable={}, run_id=run_id,
        source_counts={"identity_relationships": len(edges)})
