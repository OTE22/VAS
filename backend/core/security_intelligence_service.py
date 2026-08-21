"""
Security Intelligence Service
==============================
Intelligence analysis for security operators:
- Social Network Analysis: connections between people (co-appearance graph)
- Suspicious Pattern Detection: group activity, off-hours activity, rapid movement
- Anomaly Detection: per-identity behavioural deviation (circular time statistics)
- Threat Assessment: versioned additive risk rubric

Numeric honesty rules this module follows (added in the production-hardening
pass — see tests/test_intel_algorithms.py):
- No fabricated confidence values: every confidence is derived from the data
  and the derivation is documented at the computation site.
- Hour-of-day statistics use CIRCULAR mean/std (clock arithmetic wraps:
  the mean of 23:00 and 01:00 is midnight, not noon).
- Every scan is bounded and reports truncation instead of silently clipping.
- An empty baseline is reported as "insufficient baseline", never as
  "no anomalies".
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple, Set
from dataclasses import dataclass, field
from collections import defaultdict, Counter
import math

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func, text

from db_models import (
    Identity, IdentityAppearance, IdentityRelationship, IdentityType,
    RelationshipStrength, Detection, Pipeline, Face
)
from config import settings
from backend.utils.time_utils import iso_utc

logger = logging.getLogger(__name__)

# Version strings: every scoring rubric on the security page carries one so the
# UI can label provenance and an operator can tell which algorithm produced a
# number. Threat and network-node risk are now PROFILES of the unified risk
# engine (backend/core/risk_engine.py — one 0-100 range, one severity band
# map, DB-configurable weights in risk_model_versions), so they share its
# version string.
THREAT_ALGORITHM_VERSION = "risk-engine-v1"
NETWORK_RISK_VERSION = "risk-engine-v1"
PATTERN_ALGORITHM_VERSION = "patterns-v3"
# "Together" for group detection: sightings within this many minutes of each
# other on one camera. A SLIDING window anchored on each sighting (v3);
# v2 snapped to fixed clock buckets and missed groups straddling a boundary.
GROUP_WINDOW_MINUTES = 5
ANOMALY_ALGORITHM_VERSION = "anomaly-context-v3"



def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Naive-UTC DB timestamp as an explicit UTC ISO string."""
    return iso_utc(dt)


# ---------------------------------------------------------------------------
# Circular statistics for hour-of-day.
#
# The previous implementation used the ARITHMETIC mean of clock hours, which
# is wrong on a circle: an identity active at 23:00 and 01:00 averaged to
# 12:00, so every one of its appearances was flagged "off schedule" — and a
# genuine 03:00 outlier against a 09:00 regular scored a difference of exactly
# 6, failing the `> 6` cut. The declared "standard deviations" threshold was
# never used at all.
# ---------------------------------------------------------------------------

def circular_hour_stats(hours: List[float]) -> Tuple[float, float]:
    """Mean and standard deviation of clock hours on the 24-hour circle.

    Returns (mean_hour in [0, 24), std in hours). Directional statistics:
    hours map to angles, the resultant vector gives the mean; std is the
    circular standard deviation sqrt(-2 ln R) converted back to hours.
    An empty input returns (12.0, inf) — callers must gate on sample count.
    """
    if not hours:
        return 12.0, float("inf")
    two_pi = 2.0 * math.pi
    angles = [(h % 24.0) / 24.0 * two_pi for h in hours]
    sin_sum = sum(math.sin(a) for a in angles)
    cos_sum = sum(math.cos(a) for a in angles)
    n = len(angles)
    resultant = math.sqrt(sin_sum * sin_sum + cos_sum * cos_sum) / n
    mean_angle = math.atan2(sin_sum, cos_sum) % two_pi
    mean_hour = mean_angle / two_pi * 24.0
    if resultant >= 1.0:
        std_hours = 0.0
    elif resultant <= 0.0:
        std_hours = float("inf")
    else:
        std_hours = math.sqrt(-2.0 * math.log(resultant)) / two_pi * 24.0
    return mean_hour, std_hours


def circular_hour_distance(hour_a: float, hour_b: float) -> float:
    """Shortest distance between two clock hours on the 24-hour circle."""
    diff = abs((hour_a % 24.0) - (hour_b % 24.0))
    return min(diff, 24.0 - diff)


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class NetworkNode:
    """Node in the social network graph"""
    identity_id: str
    display_name: Optional[str]
    identity_type: str
    appearances_count: int
    risk_score: float
    connections_count: int
    snapshot_url: Optional[str] = None


@dataclass
class NetworkEdge:
    """Edge connecting two nodes in the network"""
    source_id: str
    target_id: str
    strength: float  # 0.0 to 1.0
    co_appearances: int
    co_appearance_percentage: float
    first_seen_together: Optional[datetime]
    last_seen_together: Optional[datetime]
    common_locations: List[str]
    relationship_type: str  # "strong", "moderate", "weak", "suspicious"


@dataclass
class SocialNetwork:
    """Complete social network graph"""
    nodes: List[NetworkNode]
    edges: List[NetworkEdge]
    clusters: List[List[str]]  # Groups of connected identities
    central_nodes: List[str]  # Most connected identities
    isolated_nodes: List[str]  # Scoped identities with no connections


@dataclass
class SuspiciousPattern:
    """Detected suspicious pattern"""
    pattern_type: str  # "group_activity", "unusual_timing", "rapid_movement"
    description: str
    identities_involved: List[str]
    severity: str  # "high", "medium", "low"
    confidence: float  # 0.0 to 1.0 — DERIVED, never a constant
    first_detected: datetime
    evidence: Dict[str, Any]
    locations: List[str]
    time_range: Tuple[datetime, datetime]


@dataclass
class PatternReport:
    """Bounded result of a pattern scan, honest about its own limits."""
    patterns: List[SuspiciousPattern]
    truncated: bool          # partial: row-scan cap OR per-type item cap hit
    scanned_rows: int
    window_start: datetime
    window_end: datetime
    algorithm_version: str = PATTERN_ALGORITHM_VERSION
    pipeline_id: Optional[str] = None      # None = every camera
    scope_note: Optional[str] = None       # what a scoped scan cannot see


@dataclass
class Anomaly:
    """Detected behavioural anomaly"""
    identity_id: str
    anomaly_type: str  # "off_schedule", "new_location"
    description: str
    severity: str
    detected_at: datetime
    baseline: Dict[str, Any]  # Normal behaviour pattern
    deviation: Dict[str, Any]  # How it differs
    risk_score: float


@dataclass
class AnomalyReport:
    """Anomaly result carrying baseline sufficiency.

    An empty baseline used to be returned as a bare [] — indistinguishable
    from "behaviour is normal". The UI must be able to say WHICH of the two
    it is showing.
    """
    anomalies: List[Anomaly]
    baseline_sufficient: bool
    baseline_samples: int
    baseline_start: Optional[datetime]
    baseline_end: Optional[datetime]
    recent_count: int
    truncated: bool = False
    algorithm_version: str = ANOMALY_ALGORITHM_VERSION
    # anomaly-context-v3: timezone + day-bucket configuration and per-bucket
    # baseline statistics the evaluation actually used.
    context: Optional[Dict] = None


@dataclass
class ThreatAssessment:
    """Threat assessment for an identity (unified risk engine output)."""
    identity_id: str
    display_name: Optional[str]
    overall_risk_score: float  # 0.0 to 100.0
    risk_factors: List[Dict[str, Any]]
    threat_level: str  # unified severity: "low" | "moderate" | "high" | "critical"
    recommendations: List[str]
    last_assessed: datetime
    algorithm_version: str = THREAT_ALGORITHM_VERSION
    severity: str = ""              # same value as threat_level (unified bands)
    confidence: float = 0.0         # 0-1 evidence confidence, NOT a probability
    engine: Optional[Dict[str, Any]] = None  # full RiskResult payload (signals, weights, labeling)


@dataclass
class Incident:
    """Correlated incident/event"""
    incident_id: str
    incident_type: str
    description: str
    identities_involved: List[str]
    locations: List[str]
    time_range: Tuple[datetime, datetime]
    severity: str
    related_incidents: List[str]  # IDs of related incidents
    evidence: List[Dict[str, Any]]


# Threat rubric — every weight and band in ONE place, versioned. These were
# scattered bare literals; the numbers are unchanged from threat-v1 except
# where a factor's INPUT was corrected (real appearance counts, deduplicated
# anomalies, live connection fallback).
# Threat weights/thresholds moved to the unified risk engine: the active
# risk_model_versions row per profile is authoritative (editable data, not a
# deploy), with compiled fallbacks in risk_engine.DEFAULT_MODELS. Severity
# bands are unified there too: 0-24 low, 25-49 moderate, 50-74 high,
# 75-100 critical.


class SecurityIntelligenceService:
    """
    Security intelligence analysis service.

    Provides social network analysis, suspicious-pattern detection,
    anomaly detection (circular time statistics) and versioned threat
    assessment. All scans are bounded; all reports carry truncation and
    baseline-sufficiency flags.
    """

    def __init__(self):
        self.min_co_appearances_for_connection = 2

    # The k in "flag when the circular distance from the baseline mean exceeds
    # k circular standard deviations". Finally consumed (the old attribute of
    # the same name was declared and never read).
    @property
    def anomaly_deviation_threshold(self) -> float:
        return float(settings.ANOMALY_DEVIATION_SIGMA)

    # ==================== SOCIAL NETWORK ANALYSIS ====================

    async def build_social_network(
        self,
        db: AsyncSession,
        identity_ids: Optional[List[str]] = None,
        min_connections: int = 1,
        days_back: int = 90
    ) -> SocialNetwork:
        """
        Build a social network graph of connected identities.

        Args:
            identity_ids: Specific identities to analyze (None = all)
            min_connections: Minimum connections to include
            days_back: How far back to analyze

        Returns:
            SocialNetwork with nodes, edges, and clusters
        """
        logger.info(f"[SECURITY_INTEL] Building social network (days_back={days_back})")

        cutoff_date = datetime.utcnow() - timedelta(days=days_back)

        # Get all relationships in the time window
        query = select(IdentityRelationship).where(
            IdentityRelationship.last_co_appearance >= cutoff_date
        )

        if identity_ids:
            identity_uuids = [uuid.UUID(id) for id in identity_ids]
            query = query.where(
                or_(
                    IdentityRelationship.identity_id_1.in_(identity_uuids),
                    IdentityRelationship.identity_id_2.in_(identity_uuids)
                )
            )

        result = await db.execute(query)
        relationships = result.scalars().all()

        # If cache is empty, calculate relationships on-the-fly from appearances
        if not relationships:
            logger.info("[SECURITY_INTEL] No cached relationships found, calculating on-the-fly from appearances...")
            relationships = await self._calculate_relationships_from_appearances(
                db, identity_ids, cutoff_date
            )

        # Build nodes and edges
        nodes_dict: Dict[str, NetworkNode] = {}
        edges: List[NetworkEdge] = []
        identity_ids_set: Set[str] = set()
        # Hoisted out of the identity-lookup block: it used to be defined
        # inside `if identity_ids_set:` yet referenced after it — surviving
        # only because both later uses were no-ops on an empty node list.
        connection_counts: Dict[str, int] = defaultdict(int)

        # Process relationships
        for rel in relationships:
            id1 = str(rel.identity_id_1)
            id2 = str(rel.identity_id_2)
            identity_ids_set.add(id1)
            identity_ids_set.add(id2)

            # Edge strength, normalized 0-1. The percentage is bounded 0-100
            # at the source now (distinct-appearance counting); the min() is
            # defence in depth for stale cache rows written by the old code.
            strength = min(max(rel.co_appearance_percentage, 0.0) / 100.0, 1.0)

            edge = NetworkEdge(
                source_id=id1,
                target_id=id2,
                strength=strength,
                co_appearances=rel.co_appearance_count,
                co_appearance_percentage=min(max(rel.co_appearance_percentage, 0.0), 100.0),
                first_seen_together=rel.first_co_appearance,
                last_seen_together=rel.last_co_appearance,
                common_locations=rel.common_pipelines or [],
                relationship_type=rel.relationship_strength.value if rel.relationship_strength else "weak"
            )
            edges.append(edge)

        for edge in edges:
            connection_counts[edge.source_id] += 1
            connection_counts[edge.target_id] += 1

        # Build nodes from identities
        if identity_ids_set:
            identities_query = select(Identity).where(
                Identity.id.in_([uuid.UUID(id) for id in identity_ids_set])
            )
            identities_result = await db.execute(identities_query)
            identities = {str(id.id): id for id in identities_result.scalars()}

            # REAL appearance counts in one grouped query. The denormalized
            # `identities.appearances_count` counter has drifted badly in live
            # data (34 recorded vs 3 actual rows on one identity; 18 vs 0 on
            # another) and risk scoring must not run on fiction.
            counts_result = await db.execute(
                select(
                    IdentityAppearance.identity_id,
                    func.count(IdentityAppearance.id)
                )
                .where(IdentityAppearance.identity_id.in_(
                    [uuid.UUID(id) for id in identity_ids_set]))
                .group_by(IdentityAppearance.identity_id)
            )
            real_counts = {str(row[0]): int(row[1]) for row in counts_result}

            # Node risk via the UNIFIED engine (profile network_node) — the
            # model config is loaded once and reused for every node.
            from backend.core.risk_engine import risk_engine
            node_model, node_model_version = await risk_engine.get_model(db, "network_node")

            for identity_id, identity in identities.items():
                if connection_counts[identity_id] >= min_connections:
                    real_count = real_counts.get(identity_id, 0)
                    risk_score = risk_engine.score_network_node_sync(
                        node_model, node_model_version,
                        is_unknown=identity.type == IdentityType.UNKNOWN,
                        connections=connection_counts[identity_id],
                        real_appearance_count=real_count,
                        last_seen_at=identity.last_seen_at,
                    ).total_score

                    # Convert snapshot path to URL
                    snapshot_url = None
                    if identity.best_snapshot_path:
                        from backend.utils.path_utils import path_to_url
                        snapshot_url = path_to_url(identity.best_snapshot_path)

                    node = NetworkNode(
                        identity_id=identity_id,
                        display_name=identity.display_name,
                        identity_type=identity.type.value,
                        appearances_count=real_count,
                        risk_score=risk_score,
                        connections_count=connection_counts[identity_id],
                        snapshot_url=snapshot_url
                    )
                    nodes_dict[identity_id] = node

        nodes = list(nodes_dict.values())

        # Find clusters (connected components)
        clusters = self._find_network_clusters(nodes, edges)

        # Find central nodes (most connected)
        central_nodes = sorted(
            [n.identity_id for n in nodes],
            key=lambda id: connection_counts.get(id, 0),
            reverse=True
        )[:10]

        # Isolated nodes: only meaningful for an explicit scope. The old
        # computation filtered edge-derived nodes for zero connections — a
        # structurally empty set, since every edge endpoint has >= 1. When the
        # caller names identities, the honest answer is "which of the ones you
        # asked about have no connections at all".
        if identity_ids:
            isolated_nodes = [
                requested for requested in identity_ids
                if requested not in identity_ids_set
            ]
        else:
            isolated_nodes = []

        logger.info(f"[SECURITY_INTEL] Network built: {len(nodes)} nodes, {len(edges)} edges, {len(clusters)} clusters")

        if len(nodes) == 0 and len(edges) == 0:
            logger.info(f"[SECURITY_INTEL] ⚠️ Empty network - this could mean:")
            logger.info(f"[SECURITY_INTEL]    1. No identities have appeared together in the last {days_back} days")
            logger.info(f"[SECURITY_INTEL]    2. Relationship cache is empty (call /api/identities/{{id}}/related/refresh to populate)")
            logger.info(f"[SECURITY_INTEL]    3. All co-appearances are older than {days_back} days")

        return SocialNetwork(
            nodes=nodes,
            edges=edges,
            clusters=clusters,
            central_nodes=central_nodes,
            isolated_nodes=isolated_nodes
        )

    def _find_network_clusters(self, nodes: List[NetworkNode], edges: List[NetworkEdge]) -> List[List[str]]:
        """Find connected components (clusters) in the network"""
        # Build adjacency list
        adj: Dict[str, Set[str]] = defaultdict(set)
        for edge in edges:
            adj[edge.source_id].add(edge.target_id)
            adj[edge.target_id].add(edge.source_id)

        # DFS to find connected components
        visited: Set[str] = set()
        clusters: List[List[str]] = []

        for node in nodes:
            if node.identity_id not in visited:
                cluster = []
                stack = [node.identity_id]

                while stack:
                    current = stack.pop()
                    if current not in visited:
                        visited.add(current)
                        cluster.append(current)
                        for neighbor in adj.get(current, set()):
                            if neighbor not in visited:
                                stack.append(neighbor)

                if len(cluster) > 1:  # Only clusters with 2+ nodes
                    clusters.append(cluster)

        return clusters

    def _calculate_basic_risk_score(
        self,
        identity: Identity,
        connections: int,
        real_appearance_count: int
    ) -> float:
        """Network-tab node risk (0-100), version NETWORK_RISK_VERSION.

        Thin delegate to the unified risk engine (profile ``network_node``)
        kept for API stability; inputs are the connection count from the
        current graph and the REAL appearance row count — never the drifted
        denormalized counter.
        """
        from backend.core.risk_engine import risk_engine
        config, version = risk_engine.peek_model("network_node")
        return risk_engine.score_network_node_sync(
            config, version,
            is_unknown=identity.type == IdentityType.UNKNOWN,
            connections=connections,
            real_appearance_count=real_appearance_count,
            last_seen_at=identity.last_seen_at,
        ).total_score

    # ==================== SUSPICIOUS PATTERN DETECTION ====================

    async def detect_suspicious_patterns(
        self,
        db: AsyncSession,
        days_back: int = 30,
        min_group_size: int = 3,
        pipeline_id: Optional[str] = None,
    ) -> PatternReport:
        """
        Detect suspicious patterns in identity behaviour.

        ONE bounded scan of identity_appearances feeds all three detectors
        (the previous implementation issued three full-table scans per
        request, one of them unindexable via extract('hour')).

        Patterns detected:
        - Group activity (multiple people together in one 5-minute bucket)
        - Unusual timing (repeated activity in the configured off-hours window)
        - Rapid movement (camera-to-camera transition faster than plausible)
        """
        logger.info(f"[SECURITY_INTEL] Detecting suspicious patterns "
                    f"(days_back={days_back}, pipeline={pipeline_id or 'all'})")

        window_end = datetime.utcnow()
        cutoff_date = window_end - timedelta(days=days_back)
        scan_limit = int(settings.PATTERN_SCAN_LIMIT)

        # Optional camera scope. Applied in SQL so the scan cap budgets the
        # chosen camera alone — filtering after the cap would silently drop a
        # quiet camera's history behind a busy one's. Group activity and
        # unusual timing are per-camera by nature; rapid movement compares
        # TWO cameras, so scoping to one camera cannot produce it and the
        # caller is told so (see `scope_note`).
        query = (select(IdentityAppearance)
                 .where(IdentityAppearance.start_time >= cutoff_date))
        if pipeline_id:
            query = query.where(IdentityAppearance.pipeline_id == pipeline_id)
        result = await db.execute(
            query.order_by(IdentityAppearance.start_time.desc()).limit(scan_limit)
        )
        appearances = list(result.scalars().all())
        truncated = len(appearances) >= scan_limit
        # Detectors want chronological order; the scan takes the NEWEST rows
        # when truncating (an old-first scan would silently analyse ancient
        # history and drop the present).
        appearances.sort(key=lambda a: a.start_time)

        # Pipeline metadata: coordinates for the rapid-movement speed check,
        # IANA timezones for LOCAL business-hour evaluation (timestamps stay
        # UTC in the DB; the off-hours question is a local-time question).
        pipes_result = await db.execute(
            select(Pipeline.pipeline_id, Pipeline.latitude, Pipeline.longitude,
                   Pipeline.timezone))
        pipe_coords: Dict[str, Tuple[float, float]] = {}
        pipe_timezones: Dict[str, Optional[str]] = {}
        for row in pipes_result:
            pipe_timezones[row[0]] = row[3]
            if row[1] is not None and row[2] is not None:
                pipe_coords[row[0]] = (row[1], row[2])

        by_identity: Dict[str, List[IdentityAppearance]] = defaultdict(list)
        for app in appearances:
            by_identity[str(app.identity_id)].append(app)

        patterns: List[SuspiciousPattern] = []
        patterns.extend(self._detect_group_activity(appearances, min_group_size))
        patterns.extend(self._detect_unusual_timing(by_identity, pipe_timezones))
        patterns.extend(self._detect_rapid_movement(by_identity, pipe_coords))

        # Central per-type cap, NEWEST first. Capping inside the detectors
        # (chronological iteration + break) kept the OLDEST patterns — a busy
        # site's response was 30-day-old incidents presented as complete,
        # exactly the drop-the-present failure the scan cap above avoids.
        max_per_type = int(settings.PATTERN_MAX_PER_TYPE)
        by_type: Dict[str, List[SuspiciousPattern]] = defaultdict(list)
        for p in patterns:
            by_type[p.pattern_type].append(p)
        items_capped = False
        kept: List[SuspiciousPattern] = []
        for plist in by_type.values():
            plist.sort(key=lambda p: p.first_detected, reverse=True)
            if len(plist) > max_per_type:
                items_capped = True
            kept.extend(plist[:max_per_type])
        kept.sort(key=lambda p: p.first_detected, reverse=True)

        logger.info(f"[SECURITY_INTEL] Detected {len(kept)} suspicious patterns "
                    f"(scanned={len(appearances)} truncated={truncated} capped={items_capped})")
        return PatternReport(
            patterns=kept,
            truncated=truncated or items_capped,
            scanned_rows=len(appearances),
            window_start=cutoff_date,
            window_end=window_end,
            pipeline_id=pipeline_id,
            scope_note=("Scoped to one camera: rapid movement between cameras is not "
                        "evaluated in this view." if pipeline_id else None),
        )

    def _detect_group_activity(
        self,
        appearances: List[IdentityAppearance],
        min_size: int
    ) -> List[SuspiciousPattern]:
        """Multiple identities within one GROUP_WINDOW of each other on a camera.

        SLIDING window, not fixed buckets. The previous version snapped every
        sighting to a 5-minute wall (14:10:00, 14:15:00, ...), so three people
        seen at 14:09:50 / 14:10:10 / 14:10:20 — together by any reasonable
        reading — were split 1+2 across two buckets and never reported. Here
        a window is anchored on each sighting and extends GROUP_WINDOW
        forward, so "within five minutes of each other" means exactly that,
        wherever the clock happens to be.

        One pattern per OCCURRENCE, not per window: consecutive anchors whose
        member set is the same, or a subset, of the occurrence already being
        built are folded into it (the subset rule stops {A,B,C,D} at 14:10
        being followed by a spurious {B,C,D} at 14:11 once A has left).

        Confidence derivation: how far the group exceeds the operator's
        threshold, plus a recurrence bonus when the SAME group occurs again
        later (a recurring group is stronger evidence than a one-off crowd).
        """
        window = timedelta(minutes=GROUP_WINDOW_MINUTES)

        per_camera: Dict[str, List[IdentityAppearance]] = defaultdict(list)
        for app in appearances:
            per_camera[app.pipeline_id].append(app)

        # occurrence = (pipeline_id, start, end, frozenset(identity ids))
        occurrences: List[Tuple[str, datetime, datetime, frozenset]] = []
        for pipeline_id, rows in per_camera.items():
            rows.sort(key=lambda a: a.start_time)
            n = len(rows)
            right = 0
            current: Optional[List[Any]] = None   # [start, end, frozenset]
            for left in range(n):
                anchor = rows[left].start_time
                while right < n and rows[right].start_time < anchor + window:
                    right += 1
                members = frozenset(str(a.identity_id) for a in rows[left:right])
                if len(members) < min_size:
                    continue
                last_in_window = rows[right - 1].start_time
                if current is not None and members <= current[2] and anchor <= current[1]:
                    # same group (or the tail of it) still together: extend
                    current[1] = max(current[1], last_in_window)
                    continue
                if current is not None:
                    occurrences.append((pipeline_id, current[0], current[1], current[2]))
                current = [anchor, last_in_window, members]
            if current is not None:
                occurrences.append((pipeline_id, current[0], current[1], current[2]))

        # Recurrence: how many separate occurrences does each exact group have?
        group_recurrence: Counter = Counter(occ[3] for occ in occurrences)

        patterns: List[SuspiciousPattern] = []
        for pipeline_id, start, end, members in sorted(occurrences, key=lambda o: o[1]):
            group_size = len(members)
            recurrence = group_recurrence[members]
            # size term: at threshold -> 0.5, saturating at 2x threshold.
            size_term = min(1.0, group_size / (2.0 * min_size))
            # recurrence bonus: same group seen together on 2+ occasions.
            recurrence_bonus = 0.25 if recurrence >= 2 else 0.0
            confidence = round(min(1.0, 0.25 + size_term / 2.0 + recurrence_bonus), 2)
            patterns.append(SuspiciousPattern(
                pattern_type="group_activity",
                description=f"{group_size} identities appeared together at {pipeline_id}",
                identities_involved=sorted(members),
                severity="high" if group_size >= 5 else "medium",
                confidence=confidence,
                first_detected=start,
                evidence={
                    "pipeline_id": pipeline_id,
                    "group_size": group_size,
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "window_minutes": GROUP_WINDOW_MINUTES,
                    "group_recurrence": recurrence,
                },
                locations=[pipeline_id],
                time_range=(start, max(end, start + window))
            ))
        return patterns

    def _detect_unusual_timing(
        self,
        by_identity: Dict[str, List[IdentityAppearance]],
        pipe_timezones: Optional[Dict[str, Optional[str]]] = None,
    ) -> List[SuspiciousPattern]:
        """Repeated activity inside the configured off-hours window.

        The window is evaluated in each camera's LOCAL time: UTC timestamps
        are converted through the pipeline's IANA timezone (fallback:
        DEFAULT_SITE_TIMEZONE), so `PATTERN_OFF_HOURS_START/END` mean the
        same wall-clock hours at every site and daylight-saving shifts are
        handled by the timezone database, not by anyone re-tuning offsets.
        Confidence derivation: the share of the identity's activity that
        falls in the window — someone whose every appearance is off-hours is
        stronger evidence than someone with 3 off-hours rows out of 300.
        """
        from backend.core.time_context import local_hour

        pipe_timezones = pipe_timezones or {}
        patterns: List[SuspiciousPattern] = []
        hour_start = int(settings.PATTERN_OFF_HOURS_START)
        hour_end = int(settings.PATTERN_OFF_HOURS_END)
        min_occurrences = 3

        # Both bounds are INCLUSIVE whole hours: (2, 5) covers 02:00-05:59.
        def in_window(hour: int) -> bool:
            if hour_start <= hour_end:
                return hour_start <= hour <= hour_end
            return hour >= hour_start or hour <= hour_end  # wraps midnight

        for identity_id, apps in by_identity.items():
            off_hours = [
                a for a in apps
                if in_window(int(local_hour(a.start_time,
                                            pipe_timezones.get(a.pipeline_id))))
            ]
            if len(off_hours) < min_occurrences:
                continue
            off_share = len(off_hours) / len(apps)
            confidence = round(min(1.0, 0.4 + off_share * 0.6), 2)
            timezones_used = sorted(set(
                pipe_timezones.get(a.pipeline_id) or
                str(settings.DEFAULT_SITE_TIMEZONE)
                for a in off_hours))

            patterns.append(SuspiciousPattern(
                pattern_type="unusual_timing",
                description=(f"Identity has {len(off_hours)} appearances during "
                             f"local off-hours ({hour_start:02d}:00-{hour_end:02d}:59)"),
                identities_involved=[identity_id],
                severity="medium",
                confidence=confidence,
                first_detected=min(a.start_time for a in off_hours),
                evidence={
                    "off_hour_appearances": len(off_hours),
                    "total_appearances": len(apps),
                    "off_hours_share": round(off_share, 3),
                    "time_range": f"{hour_start:02d}:00-{hour_end:02d}:59",
                    "timezones": timezones_used,
                },
                locations=list(set(a.pipeline_id for a in off_hours)),
                time_range=(
                    min(a.start_time for a in off_hours),
                    max(a.start_time for a in off_hours)
                )
            ))

        return patterns

    def _detect_rapid_movement(
        self,
        by_identity: Dict[str, List[IdentityAppearance]],
        pipe_coords: Dict[str, Tuple[float, float]]
    ) -> List[SuspiciousPattern]:
        """Camera-to-camera transitions faster than plausible.

        When both cameras have coordinates, the implied speed gates the
        pattern (two cameras 5 m apart are NOT suspicious 30 s apart; the old
        code treated every pair identically). Without coordinates it falls
        back to the pure time rule. Confidence derivation: transit-time margin
        below the window, scaled by implied speed when known.
        """
        patterns: List[SuspiciousPattern] = []
        window_seconds = float(settings.PATTERN_RAPID_WINDOW_SECONDS)
        min_speed_kmh = float(settings.PATTERN_RAPID_MIN_SPEED_KMH)

        for identity_id, apps in by_identity.items():
            if len(apps) < 2:
                continue

            for i in range(len(apps) - 1):
                current, next_app = apps[i], apps[i + 1]
                if current.pipeline_id == next_app.pipeline_id:
                    continue
                time_diff = (next_app.start_time - current.start_time).total_seconds()
                if not (0 < time_diff < window_seconds):
                    continue

                implied_speed_kmh = None
                coords_a = pipe_coords.get(current.pipeline_id)
                coords_b = pipe_coords.get(next_app.pipeline_id)
                if coords_a and coords_b:
                    distance_m = _haversine_meters(coords_a[0], coords_a[1],
                                                   coords_b[0], coords_b[1])
                    implied_speed_kmh = (distance_m / max(time_diff, 1.0)) * 3.6
                    if implied_speed_kmh < min_speed_kmh:
                        continue  # nearby cameras — normal walking, not suspicious

                time_margin = 1.0 - (time_diff / window_seconds)  # faster -> higher
                confidence = 0.3 + 0.5 * time_margin
                if implied_speed_kmh is not None:
                    confidence += 0.2  # corroborated by geometry
                confidence = round(min(1.0, confidence), 2)

                evidence = {
                    "from_location": current.pipeline_id,
                    "to_location": next_app.pipeline_id,
                    "time_seconds": time_diff,
                }
                if implied_speed_kmh is not None:
                    evidence["implied_speed_kmh"] = round(implied_speed_kmh, 1)

                patterns.append(SuspiciousPattern(
                    pattern_type="rapid_movement",
                    description=(f"Rapid movement from {current.pipeline_id} to "
                                 f"{next_app.pipeline_id} in {time_diff:.0f}s"),
                    identities_involved=[identity_id],
                    severity="high" if (implied_speed_kmh or 0) > 100 else "medium",
                    confidence=confidence,
                    first_detected=current.start_time,
                    evidence=evidence,
                    locations=[current.pipeline_id, next_app.pipeline_id],
                    time_range=(current.start_time, next_app.start_time)
                ))

        return patterns

    # ==================== ANOMALY DETECTION ====================

    async def detect_anomalies(
        self,
        db: AsyncSession,
        identity_id: str,
        days_back: int = 90
    ) -> AnomalyReport:
        """
        Detect behavioural anomalies for an identity.

        Anomalies detected:
        - Off-schedule activity: circular distance of the appearance's LOCAL
          hour from its day-context bucket's circular mean exceeds k circular
          standard deviations (k = ANOMALY_DEVIATION_SIGMA). Buckets:
          workday / weekend (WEEKEND_DAYS) / holiday (ANOMALY_HOLIDAYS) —
          09:00 on a Sunday is not judged by the Monday-Friday routine. A
          bucket without enough history falls back to the overall baseline at
          REDUCED confidence; each anomaly carries its own confidence value.
        - New location: a camera never present in the baseline — reported ONCE
          per location, not once per visit.

        Timezone: hours are evaluated in each camera's IANA timezone
        (pipelines.timezone, fallback DEFAULT_SITE_TIMEZONE); storage stays
        UTC. Baseline = history BEFORE the recent window, bounded to the last
        ANOMALY_BASELINE_MAX_DAYS (rolling, so year-old habits age out). If
        it has fewer than ANOMALY_MIN_BASELINE_SAMPLES rows the report says
        so explicitly instead of returning an empty list that reads as
        "normal". The heuristic lives in time_context.BucketedHourBaseline —
        swap that class to replace it with a statistical/ML model.
        """
        from backend.core.time_context import (
            BucketedHourBaseline, day_bucket, parse_holidays,
            parse_weekend_days, to_local)

        logger.info(f"[SECURITY_INTEL] Detecting anomalies for identity {identity_id}")

        identity_uuid = uuid.UUID(identity_id)
        cutoff_date = datetime.utcnow() - timedelta(days=days_back)
        baseline_floor = datetime.utcnow() - timedelta(
            days=int(settings.ANOMALY_BASELINE_MAX_DAYS))
        min_baseline = int(settings.ANOMALY_MIN_BASELINE_SAMPLES)
        max_items = int(settings.ANOMALY_MAX_ITEMS)
        # Floor on the circular std: a perfectly regular baseline (std -> 0)
        # must not make a 20-minute shift an "anomaly".
        std_floor = float(settings.ANOMALY_MIN_STD_HOURS)
        k_sigma = self.anomaly_deviation_threshold

        result = await db.execute(
            select(Identity).where(Identity.id == identity_uuid))
        identity = result.scalar_one_or_none()
        if not identity:
            return AnomalyReport([], False, 0, None, None, 0)

        # Baseline: ALL history before the recent window (bounded, newest
        # first, and no older than the rolling baseline horizon). The old
        # code required baseline rows to be younger than 2*days_back, which
        # silently emptied the baseline on any deployment younger than
        # days_back.
        baseline_result = await db.execute(
            select(IdentityAppearance)
            .where(
                IdentityAppearance.identity_id == identity_uuid,
                IdentityAppearance.start_time < cutoff_date,
                IdentityAppearance.start_time >= baseline_floor,
            )
            .order_by(IdentityAppearance.start_time.desc())
            # ANOMALY_MAX_ITEMS, like every sibling scan in this module. These
            # two caps were the ones missed when the rest were parameterised.
            .limit(int(settings.ANOMALY_MAX_ITEMS))
        )
        baseline_apps = list(baseline_result.scalars().all())

        # Pipeline timezones for local-hour evaluation.
        tz_result = await db.execute(select(Pipeline.pipeline_id, Pipeline.timezone))
        pipe_timezones: Dict[str, Optional[str]] = {row[0]: row[1] for row in tz_result}
        weekend_days = parse_weekend_days()
        holidays = parse_holidays()

        # Recent window: NEWEST rows under the cap (an ascending scan with a
        # LIMIT keeps the oldest slice and never examines the present), then
        # re-sorted chronologically for the walk below.
        recent_limit = int(settings.PATTERN_SCAN_LIMIT)
        recent_result = await db.execute(
            select(IdentityAppearance)
            .where(
                IdentityAppearance.identity_id == identity_uuid,
                IdentityAppearance.start_time >= cutoff_date,
            )
            .order_by(IdentityAppearance.start_time.desc())
            .limit(recent_limit)
        )
        recent_apps = list(recent_result.scalars().all())
        recent_truncated = len(recent_apps) >= recent_limit
        recent_apps.sort(key=lambda a: a.start_time)

        baseline_start = min((a.start_time for a in baseline_apps), default=None)
        baseline_end = max((a.start_time for a in baseline_apps), default=None)

        if len(baseline_apps) < min_baseline:
            return AnomalyReport(
                anomalies=[],
                baseline_sufficient=False,
                baseline_samples=len(baseline_apps),
                baseline_start=baseline_start,
                baseline_end=baseline_end,
                recent_count=len(recent_apps),
            )

        baseline_locations = set(a.pipeline_id for a in baseline_apps)
        baseline = BucketedHourBaseline(
            min_bucket_samples=min_baseline, std_floor=std_floor, k_sigma=k_sigma)
        for a in baseline_apps:
            local_dt = to_local(a.start_time, pipe_timezones.get(a.pipeline_id))
            baseline.add(local_dt.hour + local_dt.minute / 60.0,
                         day_bucket(local_dt, weekend_days, holidays))

        anomalies: List[Anomaly] = []
        seen_new_locations: Set[str] = set()

        for app in recent_apps:
            if len(anomalies) >= max_items:
                break

            # Anomaly 1: off-schedule — the appearance's LOCAL hour judged
            # against its day-context bucket (workday/weekend/holiday), with
            # documented fallback to the overall baseline at reduced
            # confidence. Infinite ratio (zero-variance baseline with the
            # std floor tuned to 0) scores the cap, never crashes.
            local_dt = to_local(app.start_time, pipe_timezones.get(app.pipeline_id))
            app_hour = local_dt.hour + local_dt.minute / 60.0
            bucket = day_bucket(local_dt, weekend_days, holidays)
            evaluation = baseline.evaluate(app_hour, bucket)
            if evaluation.is_anomalous:
                deviation_ratio = evaluation.deviation_ratio
                mean_hour = evaluation.mean_hour
                anomalies.append(Anomaly(
                    identity_id=identity_id,
                    anomaly_type="off_schedule",
                    description=(f"Activity at {local_dt.hour:02d}:{local_dt.minute:02d} "
                                 f"local ({bucket}), normally around "
                                 f"{int(mean_hour) % 24:02d}:{int(mean_hour % 1 * 60):02d} "
                                 f"±{evaluation.effective_std:.1f}h"),
                    severity="high" if deviation_ratio >= 1.5 else "medium",
                    detected_at=app.start_time,
                    baseline={
                        "mean_hour": round(mean_hour, 2),
                        "effective_std_hours": round(evaluation.effective_std, 2),
                        "samples": baseline.total_samples,
                        "bucket_used": evaluation.bucket_used,
                        "buckets": baseline.stats_summary(),
                    },
                    deviation={
                        "actual_hour": round(app_hour, 2),
                        "local_time": local_dt.isoformat(),
                        "utc_time": _iso_utc(app.start_time),
                        "day_bucket": bucket,
                        "circular_distance_hours": round(evaluation.distance_hours, 2),
                        "sigma_threshold": k_sigma,
                        "confidence": evaluation.confidence,
                        "timezone": str(local_dt.tzinfo),
                    },
                    # Scales with how far beyond the threshold the deviation is
                    # (the old score was a constant 30.0 for every case).
                    risk_score=(90.0 if not math.isfinite(deviation_ratio)
                                else round(min(90.0, 30.0 + 20.0 * (deviation_ratio - 1.0)), 1)),
                ))

            # Anomaly 2: new location — once per location, not per visit.
            if (app.pipeline_id not in baseline_locations
                    and app.pipeline_id not in seen_new_locations):
                seen_new_locations.add(app.pipeline_id)
                anomalies.append(Anomaly(
                    identity_id=identity_id,
                    anomaly_type="new_location",
                    description=f"First appearance at {app.pipeline_id}",
                    severity="low",
                    detected_at=app.start_time,
                    baseline={"known_location_count": len(baseline_locations)},
                    deviation={"new_location": app.pipeline_id},
                    risk_score=20.0,
                ))

        logger.info(f"[SECURITY_INTEL] Detected {len(anomalies)} anomalies for identity {identity_id}")
        timezones_used = sorted(set(
            (pipe_timezones.get(a.pipeline_id) or str(settings.DEFAULT_SITE_TIMEZONE))
            for a in (baseline_apps + recent_apps)))
        return AnomalyReport(
            anomalies=anomalies,
            baseline_sufficient=True,
            baseline_samples=len(baseline_apps),
            baseline_start=baseline_start,
            baseline_end=baseline_end,
            recent_count=len(recent_apps),
            truncated=len(anomalies) >= max_items or recent_truncated,
            context={
                "timezones": timezones_used,
                "weekend_days": sorted(weekend_days),
                "holidays_configured": len(holidays),
                "buckets": baseline.stats_summary(),
                "baseline_max_days": int(settings.ANOMALY_BASELINE_MAX_DAYS),
            },
        )

    # ==================== THREAT ASSESSMENT ====================

    async def assess_threat(
        self,
        db: AsyncSession,
        identity_id: str
    ) -> ThreatAssessment:
        """
        Threat assessment via the UNIFIED risk engine (profile
        ``identity_threat``, version THREAT_ALGORITHM_VERSION). This service
        gathers the raw evidence; the engine owns weights (DB-configurable
        via risk_model_versions), severity bands, confidence and the honesty
        labelling — no endpoint calculates risk independently anymore.

        Evidence gathered here:
        - Identity type (unknown = higher risk)
        - Connection count (relationship cache, with a live bounded fallback
          when the cache is empty — the old code silently scored 0 exactly
          when the cache was cold)
        - Behavioural anomalies (deduplicated, bucketed circular-statistics
          detector; count uses REAL appearance rows via func.count, never the
          drifted counter)
        - Activity level (real appearance count)
        """
        logger.info(f"[SECURITY_INTEL] Assessing threat for identity {identity_id}")

        identity_uuid = uuid.UUID(identity_id)

        result = await db.execute(
            select(Identity).where(Identity.id == identity_uuid))
        identity = result.scalar_one_or_none()
        if not identity:
            raise ValueError(f"Identity {identity_id} not found")

        # Evidence 1: Connection count — cache first, live fallback when empty.
        connections_result = await db.execute(
            select(func.count(IdentityRelationship.id)).where(
                or_(
                    IdentityRelationship.identity_id_1 == identity_uuid,
                    IdentityRelationship.identity_id_2 == identity_uuid
                )
            )
        )
        connections_count = connections_result.scalar() or 0
        connections_source = "relationship_cache"

        if connections_count == 0:
            # Bounded live estimate: co-occurring identities on the same
            # camera within the co-appearance window, last 90 days. A partner
            # only counts after RELATED_IDENTITY_MIN_CO_APPEARANCES shared
            # events — the same bar the relationship cache applies. Without
            # the HAVING clause, one pass through a busy lobby counted every
            # one-time passerby as a "connection" and pushed an unknown
            # identity into the 'high' threat band on cache warmth alone.
            window_min = int(settings.RELATED_IDENTITY_TIME_WINDOW_MINUTES)
            min_co = int(settings.RELATED_IDENTITY_MIN_CO_APPEARANCES)
            live_cutoff = datetime.utcnow() - timedelta(days=90)
            a = IdentityAppearance.__table__.alias("a")
            b = IdentityAppearance.__table__.alias("b")
            qualifying_partners = (
                select(b.c.identity_id)
                .select_from(
                    a.join(b, and_(
                        a.c.pipeline_id == b.c.pipeline_id,
                        b.c.identity_id != a.c.identity_id,
                        b.c.start_time.between(
                            a.c.start_time - timedelta(minutes=window_min),
                            a.c.start_time + timedelta(minutes=window_min)),
                    ))
                )
                .where(
                    a.c.identity_id == identity_uuid,
                    a.c.start_time >= live_cutoff,
                )
                .group_by(b.c.identity_id)
                .having(func.count() >= min_co)
            ).subquery()
            live_result = await db.execute(
                select(func.count()).select_from(qualifying_partners))
            connections_count = live_result.scalar() or 0
            connections_source = "live_co_occurrence"

        # Evidence 2: Recent anomalies (deduplicated, bucketed detector — the
        # old per-appearance emission saturated this factor with 3 visits).
        anomaly_report = await self.detect_anomalies(db, identity_id, days_back=30)
        anomaly_count = len(anomaly_report.anomalies)

        # Evidence 3: Activity level — REAL count via func.count, never the
        # drifted identities.appearances_count counter.
        real_count_result = await db.execute(
            select(func.count(IdentityAppearance.id))
            .where(IdentityAppearance.identity_id == identity_uuid))
        real_appearances = real_count_result.scalar() or 0

        # The unified engine turns the evidence into score/severity/signals.
        from backend.core.risk_engine import risk_engine
        risk = await risk_engine.score_identity_threat(
            db,
            is_unknown=identity.type == IdentityType.UNKNOWN,
            connections_count=connections_count,
            connections_source=connections_source,
            anomaly_count=anomaly_count,
            baseline_sufficient=anomaly_report.baseline_sufficient,
            baseline_samples=anomaly_report.baseline_samples,
            real_appearance_count=real_appearances,
        )

        factor_labels = {
            "unknown_identity": "Unknown Identity",
            "connections": "Connection Count",
            "behavioral_anomalies": "Behavioral Anomalies",
            "activity_level": "Activity Level",
        }
        risk_factors = [
            {
                "factor": factor_labels.get(s.name, s.name.replace("_", " ").title()),
                "score": s.score,
                "description": s.explanation,
            }
            for s in risk.signals
        ]

        # Recommendations (operational hints, not part of the score)
        model_config, _ = await risk_engine.get_model(db, "identity_threat")
        conn_high = model_config["thresholds"].get("connections_high", 10)
        recommendations = []
        if identity.type == IdentityType.UNKNOWN:
            recommendations.append("Consider promoting to known identity if verified")
        if connections_count > conn_high:
            recommendations.append("Investigate network connections - potential hub")
        if anomaly_count:
            recommendations.append("Review behavioral anomalies for suspicious activity")
        if risk.total_score >= 50.0:
            recommendations.append("Prioritize for investigation")

        return ThreatAssessment(
            identity_id=identity_id,
            display_name=identity.display_name,
            overall_risk_score=risk.total_score,
            risk_factors=risk_factors,
            threat_level=risk.severity,
            recommendations=recommendations,
            last_assessed=datetime.utcnow(),
            algorithm_version=risk.model_version,
            severity=risk.severity,
            confidence=risk.confidence,
            engine=risk.as_payload(),
        )

    async def _calculate_relationships_from_appearances(
        self,
        db: AsyncSession,
        identity_ids: Optional[List[str]],
        cutoff_date: datetime
    ) -> List:
        """
        Calculate relationships on-the-fly from appearance data.
        Used as fallback when cache is empty. BOUNDED: at most
        NETWORK_FALLBACK_MAX_IDENTITIES most-recently-seen identities are
        considered (the unbounded version scanned every identity in the
        database inside one HTTP request).
        """
        from backend.core.intelligence_service import intelligence_service

        logger.info(f"[SECURITY_INTEL] Calculating relationships from appearances (cutoff: {cutoff_date})")
        max_identities = int(settings.NETWORK_FALLBACK_MAX_IDENTITIES)

        if identity_ids:
            identity_uuids = [uuid.UUID(id) for id in identity_ids]
            identities_query = select(Identity).where(
                and_(
                    Identity.id.in_(identity_uuids),
                    Identity.last_seen_at >= cutoff_date
                )
            ).order_by(Identity.last_seen_at.desc()).limit(max_identities)
        else:
            identities_query = select(Identity).where(
                Identity.last_seen_at >= cutoff_date
            ).order_by(Identity.last_seen_at.desc()).limit(max_identities)

        identities_result = await db.execute(identities_query)
        identities = identities_result.scalars().all()

        if not identities:
            logger.info(f"[SECURITY_INTEL] No identities found with recent appearances")
            return []

        logger.info(f"[SECURITY_INTEL] Found {len(identities)} identities with recent activity")

        all_relationships = {}
        time_window = settings.RELATED_IDENTITY_TIME_WINDOW_MINUTES
        min_co_appearances = settings.RELATED_IDENTITY_MIN_CO_APPEARANCES

        for identity in identities:
            try:
                related = await intelligence_service._calculate_co_appearances(
                    db,
                    identity.id,
                    time_window,
                    min_co_appearances,
                    100,  # limit
                    cutoff_date=cutoff_date,
                )

                for rel_info in related:
                    # Create relationship key (sorted UUIDs)
                    id1, id2 = identity.id, uuid.UUID(rel_info.identity_id)
                    if id1 > id2:
                        id1, id2 = id2, id1

                    key = (id1, id2)

                    # Use the relationship with higher co-appearance count
                    if key not in all_relationships or rel_info.co_appearance_count > all_relationships[key].co_appearance_count:
                        strength_map = {
                            "strong": RelationshipStrength.STRONG,
                            "moderate": RelationshipStrength.MODERATE,
                            "weak": RelationshipStrength.WEAK
                        }
                        strength = strength_map.get(rel_info.relationship_strength, RelationshipStrength.WEAK)
                        all_relationships[key] = _RelationshipView(id1, id2, rel_info, strength)

            except Exception as e:
                logger.warning(f"[SECURITY_INTEL] Error calculating relationships for {identity.id}: {e}")
                continue

        logger.info(f"[SECURITY_INTEL] Calculated {len(all_relationships)} relationships on-the-fly")
        return list(all_relationships.values())


class _RelationshipView:
    """Ad-hoc view mirroring IdentityRelationship's read surface, for
    fallback-computed (uncached) relationships. Formerly named
    'MockRelationship' — it is real computed data, not a mock."""

    def __init__(self, id1, id2, rel_info, strength):
        self.identity_id_1 = id1
        self.identity_id_2 = id2
        self.co_appearance_count = rel_info.co_appearance_count
        self.co_appearance_percentage = rel_info.co_appearance_percentage
        self.relationship_strength = strength
        self.common_pipelines = rel_info.common_pipelines
        self.first_co_appearance = rel_info.first_co_appearance
        self.last_co_appearance = rel_info.last_co_appearance


# Global instance
security_intelligence_service = SecurityIntelligenceService()
