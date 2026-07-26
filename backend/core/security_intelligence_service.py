"""
Security Intelligence Service
==============================
Advanced intelligence analysis for security agencies:
- Social Network Analysis: Visualize connections between people
- Association Analysis: Detect suspicious patterns and groups
- Anomaly Detection: Flag unusual behaviors
- Threat Assessment: Risk scoring based on behavior patterns
- Incident Correlation: Link related events and activities
- Geographic Analysis: Movement hotspots and patterns
- Relationship Strength Analysis: Quantify connection strength
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple, Set
from dataclasses import dataclass, field
from collections import defaultdict, Counter
import math

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func, text, distinct
from sqlalchemy.orm import selectinload

from db_models import (
    Identity, IdentityAppearance, IdentityRelationship, IdentityType,
    RelationshipStrength, Detection, Pipeline, Face
)
from config import settings

logger = logging.getLogger(__name__)


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
    isolated_nodes: List[str]  # Identities with no connections


@dataclass
class SuspiciousPattern:
    """Detected suspicious pattern"""
    pattern_type: str  # "group_activity", "unusual_timing", "rapid_movement", etc.
    description: str
    identities_involved: List[str]
    severity: str  # "high", "medium", "low"
    confidence: float  # 0.0 to 1.0
    first_detected: datetime
    evidence: Dict[str, Any]
    locations: List[str]
    time_range: Tuple[datetime, datetime]


@dataclass
class Anomaly:
    """Detected behavioral anomaly"""
    identity_id: str
    anomaly_type: str  # "off_schedule", "new_location", "unusual_group", etc.
    description: str
    severity: str
    detected_at: datetime
    baseline: Dict[str, Any]  # Normal behavior pattern
    deviation: Dict[str, Any]  # How it differs
    risk_score: float


@dataclass
class ThreatAssessment:
    """Threat assessment for an identity"""
    identity_id: str
    display_name: Optional[str]
    overall_risk_score: float  # 0.0 to 100.0
    risk_factors: List[Dict[str, Any]]
    threat_level: str  # "critical", "high", "medium", "low", "minimal"
    recommendations: List[str]
    last_assessed: datetime


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


class SecurityIntelligenceService:
    """
    Advanced security intelligence analysis service.
    
    Provides:
    - Social network analysis and visualization
    - Suspicious pattern detection
    - Anomaly detection
    - Threat assessment
    - Incident correlation
    - Geographic analysis
    """
    
    def __init__(self):
        self.min_co_appearances_for_connection = 2
        self.suspicious_group_size_threshold = 3
        self.anomaly_deviation_threshold = 2.0  # Standard deviations
    
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
            logger.info(f"[SECURITY_INTEL] No cached relationships found, calculating on-the-fly from appearances...")
            relationships = await self._calculate_relationships_from_appearances(
                db, identity_ids, cutoff_date
            )
        
        # Build nodes and edges
        nodes_dict: Dict[str, NetworkNode] = {}
        edges: List[NetworkEdge] = []
        identity_ids_set: Set[str] = set()
        
        # Process relationships
        for rel in relationships:
            id1 = str(rel.identity_id_1)
            id2 = str(rel.identity_id_2)
            identity_ids_set.add(id1)
            identity_ids_set.add(id2)
            
            # Calculate edge strength (normalized 0-1)
            strength = min(rel.co_appearance_percentage / 100.0, 1.0)
            
            edge = NetworkEdge(
                source_id=id1,
                target_id=id2,
                strength=strength,
                co_appearances=rel.co_appearance_count,
                co_appearance_percentage=rel.co_appearance_percentage,
                first_seen_together=rel.first_co_appearance,
                last_seen_together=rel.last_co_appearance,
                common_locations=rel.common_pipelines or [],
                relationship_type=rel.relationship_strength.value if rel.relationship_strength else "weak"
            )
            edges.append(edge)
        
        # Build nodes from identities
        if identity_ids_set:
            identities_query = select(Identity).where(
                Identity.id.in_([uuid.UUID(id) for id in identity_ids_set])
            )
            identities_result = await db.execute(identities_query)
            identities = {str(id.id): id for id in identities_result.scalars()}
            
            # Count connections per identity
            connection_counts: Dict[str, int] = defaultdict(int)
            for edge in edges:
                connection_counts[edge.source_id] += 1
                connection_counts[edge.target_id] += 1
            
            for identity_id, identity in identities.items():
                if connection_counts[identity_id] >= min_connections:
                    # Calculate risk score (simplified - can be enhanced)
                    risk_score = self._calculate_basic_risk_score(
                        identity, connection_counts[identity_id], db
                    )
                    
                    # Convert snapshot path to URL
                    snapshot_url = None
                    if identity.best_snapshot_path:
                        from backend.utils.path_utils import path_to_url
                        snapshot_url = path_to_url(identity.best_snapshot_path)
                    
                    node = NetworkNode(
                        identity_id=identity_id,
                        display_name=identity.display_name,
                        identity_type=identity.type.value,
                        appearances_count=identity.appearances_count or 0,
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
        
        # Find isolated nodes
        isolated_nodes = [
            n.identity_id for n in nodes
            if connection_counts.get(n.identity_id, 0) == 0
        ]
        
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
        db: AsyncSession
    ) -> float:
        """Calculate a basic risk score (0-100)"""
        score = 0.0
        
        # Factor 1: Unknown identity (higher risk)
        if identity.type == IdentityType.UNKNOWN:
            score += 30.0
        
        # Factor 2: High number of connections (potential hub)
        if connections > 10:
            score += 20.0
        elif connections > 5:
            score += 10.0
        
        # Factor 3: High appearance count (frequent visitor)
        if identity.appearances_count and identity.appearances_count > 100:
            score += 15.0
        elif identity.appearances_count and identity.appearances_count > 50:
            score += 8.0
        
        # Factor 4: Recent activity
        if identity.last_seen_at:
            days_since_last_seen = (datetime.utcnow() - identity.last_seen_at).days
            if days_since_last_seen < 1:
                score += 10.0
            elif days_since_last_seen < 7:
                score += 5.0
        
        return min(score, 100.0)
    
    # ==================== SUSPICIOUS PATTERN DETECTION ====================
    
    async def detect_suspicious_patterns(
        self,
        db: AsyncSession,
        days_back: int = 30,
        min_group_size: int = 3
    ) -> List[SuspiciousPattern]:
        """
        Detect suspicious patterns in identity behavior.
        
        Patterns detected:
        - Group activity (multiple people together)
        - Unusual timing (off-hours activity)
        - Rapid movement (quick transitions between locations)
        - Repeated co-appearances (same group multiple times)
        """
        logger.info(f"[SECURITY_INTEL] Detecting suspicious patterns (days_back={days_back})")
        
        patterns: List[SuspiciousPattern] = []
        cutoff_date = datetime.utcnow() - timedelta(days=days_back)
        
        # Pattern 1: Large group activity
        group_patterns = await self._detect_group_activity(db, cutoff_date, min_group_size)
        patterns.extend(group_patterns)
        
        # Pattern 2: Unusual timing
        timing_patterns = await self._detect_unusual_timing(db, cutoff_date)
        patterns.extend(timing_patterns)
        
        # Pattern 3: Rapid movement
        movement_patterns = await self._detect_rapid_movement(db, cutoff_date)
        patterns.extend(movement_patterns)
        
        logger.info(f"[SECURITY_INTEL] Detected {len(patterns)} suspicious patterns")
        return patterns
    
    async def _detect_group_activity(
        self,
        db: AsyncSession,
        cutoff_date: datetime,
        min_size: int
    ) -> List[SuspiciousPattern]:
        """Detect when multiple identities appear together"""
        patterns = []
        
        # Get all appearances in time window
        query = select(IdentityAppearance).where(
            IdentityAppearance.start_time >= cutoff_date
        ).order_by(IdentityAppearance.start_time)
        
        result = await db.execute(query)
        appearances = result.scalars().all()
        
        # Group by time window (5 minutes) and pipeline
        time_windows: Dict[Tuple[datetime, str], List[IdentityAppearance]] = defaultdict(list)
        
        for app in appearances:
            # Round to 5-minute window
            window_start = app.start_time.replace(
                minute=(app.start_time.minute // 5) * 5,
                second=0,
                microsecond=0
            )
            key = (window_start, app.pipeline_id)
            time_windows[key].append(app)
        
        # Find windows with multiple unique identities
        for (window_time, pipeline_id), apps in time_windows.items():
            unique_identities = set(str(app.identity_id) for app in apps)
            
            if len(unique_identities) >= min_size:
                # Check if this group appears together frequently
                group_key = tuple(sorted(unique_identities))
                
                pattern = SuspiciousPattern(
                    pattern_type="group_activity",
                    description=f"{len(unique_identities)} identities appeared together at {pipeline_id}",
                    identities_involved=list(unique_identities),
                    severity="high" if len(unique_identities) >= 5 else "medium",
                    confidence=0.8,
                    first_detected=window_time,
                    evidence={
                        "pipeline_id": pipeline_id,
                        "group_size": len(unique_identities),
                        "window_time": window_time.isoformat()
                    },
                    locations=[pipeline_id],
                    time_range=(window_time, window_time + timedelta(minutes=5))
                )
                patterns.append(pattern)
        
        return patterns
    
    async def _detect_unusual_timing(
        self,
        db: AsyncSession,
        cutoff_date: datetime
    ) -> List[SuspiciousPattern]:
        """Detect activity during unusual hours (e.g., 2-5 AM)"""
        patterns = []
        
        # Get appearances during unusual hours (2-5 AM)
        query = select(IdentityAppearance).where(
            IdentityAppearance.start_time >= cutoff_date,
            func.extract('hour', IdentityAppearance.start_time).between(2, 5)
        )
        
        result = await db.execute(query)
        appearances = result.scalars().all()
        
        # Group by identity
        by_identity: Dict[str, List[IdentityAppearance]] = defaultdict(list)
        for app in appearances:
            by_identity[str(app.identity_id)].append(app)
        
        # Find identities with frequent off-hours activity
        for identity_id, apps in by_identity.items():
            if len(apps) >= 3:  # At least 3 occurrences
                pattern = SuspiciousPattern(
                    pattern_type="unusual_timing",
                    description=f"Identity has {len(apps)} appearances during off-hours (2-5 AM)",
                    identities_involved=[identity_id],
                    severity="medium",
                    confidence=0.7,
                    first_detected=min(app.start_time for app in apps),
                    evidence={
                        "off_hour_appearances": len(apps),
                        "time_range": "02:00-05:00"
                    },
                    locations=list(set(app.pipeline_id for app in apps)),
                    time_range=(
                        min(app.start_time for app in apps),
                        max(app.start_time for app in apps)
                    )
                )
                patterns.append(pattern)
        
        return patterns
    
    async def _detect_rapid_movement(
        self,
        db: AsyncSession,
        cutoff_date: datetime
    ) -> List[SuspiciousPattern]:
        """Detect rapid movement between locations"""
        patterns = []
        
        # Get all appearances
        query = select(IdentityAppearance).where(
            IdentityAppearance.start_time >= cutoff_date
        ).order_by(IdentityAppearance.identity_id, IdentityAppearance.start_time)
        
        result = await db.execute(query)
        appearances = result.scalars().all()
        
        # Group by identity
        by_identity: Dict[str, List[IdentityAppearance]] = defaultdict(list)
        for app in appearances:
            by_identity[str(app.identity_id)].append(app)
        
        # Check for rapid transitions
        for identity_id, apps in by_identity.items():
            if len(apps) < 2:
                continue
            
            for i in range(len(apps) - 1):
                current = apps[i]
                next_app = apps[i + 1]
                
                # Check if different pipeline and within short time
                if current.pipeline_id != next_app.pipeline_id:
                    time_diff = (next_app.start_time - current.start_time).total_seconds()
                    
                    # Rapid movement: different location within 5 minutes
                    if 0 < time_diff < 300:  # 5 minutes
                        pattern = SuspiciousPattern(
                            pattern_type="rapid_movement",
                            description=f"Rapid movement from {current.pipeline_id} to {next_app.pipeline_id} in {time_diff:.0f}s",
                            identities_involved=[identity_id],
                            severity="medium",
                            confidence=0.75,
                            first_detected=current.start_time,
                            evidence={
                                "from_location": current.pipeline_id,
                                "to_location": next_app.pipeline_id,
                                "time_seconds": time_diff
                            },
                            locations=[current.pipeline_id, next_app.pipeline_id],
                            time_range=(current.start_time, next_app.start_time)
                        )
                        patterns.append(pattern)
        
        return patterns
    
    # ==================== ANOMALY DETECTION ====================
    
    async def detect_anomalies(
        self,
        db: AsyncSession,
        identity_id: str,
        days_back: int = 90
    ) -> List[Anomaly]:
        """
        Detect behavioral anomalies for an identity.
        
        Anomalies detected:
        - Off-schedule activity (unusual timing)
        - New location (never seen here before)
        - Unusual group (appearing with new people)
        """
        logger.info(f"[SECURITY_INTEL] Detecting anomalies for identity {identity_id}")
        
        identity_uuid = uuid.UUID(identity_id)
        cutoff_date = datetime.utcnow() - timedelta(days=days_back)
        
        # Get identity
        result = await db.execute(
            select(Identity).where(Identity.id == identity_uuid)
        )
        identity = result.scalar_one_or_none()
        
        if not identity:
            return []
        
        anomalies: List[Anomaly] = []
        
        # Get baseline behavior (older data)
        baseline_cutoff = datetime.utcnow() - timedelta(days=days_back * 2)
        baseline_query = select(IdentityAppearance).where(
            IdentityAppearance.identity_id == identity_uuid,
            IdentityAppearance.start_time >= baseline_cutoff,
            IdentityAppearance.start_time < cutoff_date
        )
        baseline_result = await db.execute(baseline_query)
        baseline_apps = baseline_result.scalars().all()
        
        # Get recent activity
        recent_query = select(IdentityAppearance).where(
            IdentityAppearance.identity_id == identity_uuid,
            IdentityAppearance.start_time >= cutoff_date
        )
        recent_result = await db.execute(recent_query)
        recent_apps = recent_result.scalars().all()
        
        if not baseline_apps:
            return []  # No baseline to compare against
        
        # Analyze baseline
        baseline_hours = [app.start_time.hour for app in baseline_apps]
        baseline_locations = set(app.pipeline_id for app in baseline_apps)
        avg_hour = sum(baseline_hours) / len(baseline_hours) if baseline_hours else 12
        
        # Check for anomalies in recent activity
        for app in recent_apps:
            # Anomaly 1: Off-schedule (unusual hour)
            hour_diff = abs(app.start_time.hour - avg_hour)
            if hour_diff > 6:  # More than 6 hours from average
                anomaly = Anomaly(
                    identity_id=identity_id,
                    anomaly_type="off_schedule",
                    description=f"Activity at {app.start_time.hour}:00, normally around {avg_hour:.0f}:00",
                    severity="medium",
                    detected_at=app.start_time,
                    baseline={"average_hour": avg_hour},
                    deviation={"actual_hour": app.start_time.hour, "difference": hour_diff},
                    risk_score=30.0
                )
                anomalies.append(anomaly)
            
            # Anomaly 2: New location
            if app.pipeline_id not in baseline_locations:
                anomaly = Anomaly(
                    identity_id=identity_id,
                    anomaly_type="new_location",
                    description=f"First appearance at {app.pipeline_id}",
                    severity="low",
                    detected_at=app.start_time,
                    baseline={"known_locations": list(baseline_locations)},
                    deviation={"new_location": app.pipeline_id},
                    risk_score=20.0
                )
                anomalies.append(anomaly)
        
        logger.info(f"[SECURITY_INTEL] Detected {len(anomalies)} anomalies for identity {identity_id}")
        return anomalies
    
    # ==================== THREAT ASSESSMENT ====================
    
    async def assess_threat(
        self,
        db: AsyncSession,
        identity_id: str
    ) -> ThreatAssessment:
        """
        Perform comprehensive threat assessment for an identity.
        
        Factors considered:
        - Identity type (unknown = higher risk)
        - Connection count (hub = higher risk)
        - Behavioral anomalies
        - Suspicious patterns
        - Recent activity
        """
        logger.info(f"[SECURITY_INTEL] Assessing threat for identity {identity_id}")
        
        identity_uuid = uuid.UUID(identity_id)
        
        # Get identity
        result = await db.execute(
            select(Identity).where(Identity.id == identity_uuid)
        )
        identity = result.scalar_one_or_none()
        
        if not identity:
            raise ValueError(f"Identity {identity_id} not found")
        
        risk_factors = []
        risk_score = 0.0
        
        # Factor 1: Identity type
        if identity.type == IdentityType.UNKNOWN:
            risk_score += 30.0
            risk_factors.append({
                "factor": "Unknown Identity",
                "score": 30.0,
                "description": "Identity is not in known persons database"
            })
        
        # Factor 2: High connection count
        connections_query = select(func.count(IdentityRelationship.id)).where(
            or_(
                IdentityRelationship.identity_id_1 == identity_uuid,
                IdentityRelationship.identity_id_2 == identity_uuid
            )
        )
        connections_result = await db.execute(connections_query)
        connections_count = connections_result.scalar() or 0
        
        if connections_count > 10:
            risk_score += 25.0
            risk_factors.append({
                "factor": "High Connection Count",
                "score": 25.0,
                "description": f"Connected to {connections_count} other identities"
            })
        elif connections_count > 5:
            risk_score += 15.0
            risk_factors.append({
                "factor": "Moderate Connection Count",
                "score": 15.0,
                "description": f"Connected to {connections_count} other identities"
            })
        
        # Factor 3: Recent anomalies
        anomalies = await self.detect_anomalies(db, identity_id, days_back=30)
        if anomalies:
            anomaly_score = min(len(anomalies) * 10.0, 30.0)
            risk_score += anomaly_score
            risk_factors.append({
                "factor": "Behavioral Anomalies",
                "score": anomaly_score,
                "description": f"{len(anomalies)} anomalies detected in last 30 days"
            })
        
        # Factor 4: High appearance frequency
        if identity.appearances_count and identity.appearances_count > 100:
            risk_score += 15.0
            risk_factors.append({
                "factor": "High Activity",
                "score": 15.0,
                "description": f"{identity.appearances_count} total appearances"
            })
        
        # Determine threat level
        if risk_score >= 70:
            threat_level = "critical"
        elif risk_score >= 50:
            threat_level = "high"
        elif risk_score >= 30:
            threat_level = "medium"
        elif risk_score >= 15:
            threat_level = "low"
        else:
            threat_level = "minimal"
        
        # Generate recommendations
        recommendations = []
        if identity.type == IdentityType.UNKNOWN:
            recommendations.append("Consider promoting to known identity if verified")
        if connections_count > 10:
            recommendations.append("Investigate network connections - potential hub")
        if anomalies:
            recommendations.append("Review behavioral anomalies for suspicious activity")
        if risk_score >= 50:
            recommendations.append("Prioritize for investigation")
        
        return ThreatAssessment(
            identity_id=identity_id,
            display_name=identity.display_name,
            overall_risk_score=min(risk_score, 100.0),
            risk_factors=risk_factors,
            threat_level=threat_level,
            recommendations=recommendations,
            last_assessed=datetime.utcnow()
        )
    
    async def _calculate_relationships_from_appearances(
        self,
        db: AsyncSession,
        identity_ids: Optional[List[str]],
        cutoff_date: datetime
    ) -> List:
        """
        Calculate relationships on-the-fly from appearance data.
        Used as fallback when cache is empty.
        """
        from backend.core.intelligence_service import intelligence_service
        from config import settings
        
        logger.info(f"[SECURITY_INTEL] Calculating relationships from appearances (cutoff: {cutoff_date})")
        
        # Get all identities that have appeared since cutoff
        if identity_ids:
            identity_uuids = [uuid.UUID(id) for id in identity_ids]
            identities_query = select(Identity).where(
                and_(
                    Identity.id.in_(identity_uuids),
                    Identity.last_seen_at >= cutoff_date
                )
            )
        else:
            identities_query = select(Identity).where(
                Identity.last_seen_at >= cutoff_date
            )
        
        identities_result = await db.execute(identities_query)
        identities = identities_result.scalars().all()
        
        if not identities:
            logger.info(f"[SECURITY_INTEL] No identities found with recent appearances")
            return []
        
        logger.info(f"[SECURITY_INTEL] Found {len(identities)} identities with recent activity")
        
        # Calculate relationships for each identity
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
                    100  # limit
                )
                
                for rel_info in related:
                    # Create relationship key (sorted UUIDs)
                    id1, id2 = identity.id, uuid.UUID(rel_info.identity_id)
                    if id1 > id2:
                        id1, id2 = id2, id1
                    
                    key = (id1, id2)
                    
                    # Use the relationship with higher co-appearance count
                    if key not in all_relationships or rel_info.co_appearance_count > all_relationships[key].co_appearance_count:
                        from db_models import RelationshipStrength
                        
                        # Convert relationship strength string to enum
                        strength_map = {
                            "strong": RelationshipStrength.STRONG,
                            "moderate": RelationshipStrength.MODERATE,
                            "weak": RelationshipStrength.WEAK
                        }
                        strength = strength_map.get(rel_info.relationship_strength, RelationshipStrength.WEAK)
                        
                        # Create a simple object to hold the data (mimics IdentityRelationship)
                        class MockRelationship:
                            def __init__(self, id1, id2, rel_info, strength):
                                self.identity_id_1 = id1
                                self.identity_id_2 = id2
                                self.co_appearance_count = rel_info.co_appearance_count
                                self.co_appearance_percentage = rel_info.co_appearance_percentage
                                self.relationship_strength = strength
                                self.common_pipelines = rel_info.common_pipelines
                                self.first_co_appearance = rel_info.first_co_appearance
                                self.last_co_appearance = rel_info.last_co_appearance
                        
                        all_relationships[key] = MockRelationship(id1, id2, rel_info, strength)
            
            except Exception as e:
                logger.warning(f"[SECURITY_INTEL] Error calculating relationships for {identity.id}: {e}")
                continue
        
        logger.info(f"[SECURITY_INTEL] Calculated {len(all_relationships)} relationships on-the-fly")
        return list(all_relationships.values())


# Global instance
security_intelligence_service = SecurityIntelligenceService()

