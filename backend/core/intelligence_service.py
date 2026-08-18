"""
Intelligence Service
=====================
Provides advanced analytics for identity intelligence:
- Related Identities: Find people who frequently appear together
- Temporal Patterns: Analyze when people typically appear
- Cross-Camera Tracking: Track movement across cameras
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func, text
from sqlalchemy.orm import selectinload

from db_models import (
    Identity, IdentityAppearance, IdentityRelationship, IdentityType,
    RelationshipStrength, Detection, Pipeline
)
from config import settings

logger = logging.getLogger(__name__)


@dataclass
class RelatedIdentityInfo:
    """Information about a related identity"""
    identity_id: str
    display_name: Optional[str]
    identity_type: str
    co_appearance_count: int
    co_appearance_percentage: float
    relationship_strength: str
    common_pipelines: List[str]
    first_co_appearance: Optional[datetime]
    last_co_appearance: Optional[datetime]
    best_snapshot_path: Optional[str]


@dataclass
class TemporalPattern:
    """Temporal pattern for an identity"""
    identity_id: str
    hourly_distribution: Dict[int, int]  # hour -> count
    daily_distribution: Dict[int, int]  # day of week -> count
    peak_hours: List[int]
    peak_days: List[str]
    most_common_pipelines: List[Dict]  # [{pipeline_id, count, name}]
    total_appearances: int
    first_appearance: Optional[datetime]
    last_appearance: Optional[datetime]
    average_appearances_per_day: float


@dataclass
class CrossCameraMovement:
    """Single camera movement event"""
    pipeline_id: str
    pipeline_name: Optional[str]
    timestamp: datetime
    snapshot_path: Optional[str]
    duration_at_location: Optional[int]  # seconds
    coordinates: Optional[Dict[str, float]] = None  # {"lat": float, "lng": float}


@dataclass
class CrossCameraTrack:
    """Complete cross-camera track for an identity"""
    identity_id: str
    display_name: Optional[str]
    date: str
    movements: List[CrossCameraMovement]
    total_cameras: int
    first_seen: datetime
    last_seen: datetime
    total_duration_minutes: int


class IntelligenceService:
    """
    Service for identity intelligence analysis.

    Provides:
    - Co-appearance analysis (who appears with whom)
    - Temporal patterns (when do they appear)
    - Cross-camera tracking (movement across locations)
    """

    # ==================== RELATED IDENTITIES ====================

    async def get_related_identities(
        self,
        db: AsyncSession,
        identity_id: str,
        min_co_appearances: int = None,
        time_window_minutes: int = None,
        limit: int = 20
    ) -> List[RelatedIdentityInfo]:
        """
        Find identities that frequently appear together with the given identity.

        Args:
            identity_id: Target identity UUID
            min_co_appearances: Minimum number of co-appearances (uses config default)
            time_window_minutes: Time window to consider co-appearance (uses config default)
            limit: Maximum results to return

        Returns:
            List of related identities sorted by co-appearance count
        """
        if min_co_appearances is None:
            min_co_appearances = settings.RELATED_IDENTITY_MIN_CO_APPEARANCES
        if time_window_minutes is None:
            time_window_minutes = settings.RELATED_IDENTITY_TIME_WINDOW_MINUTES

        target_uuid = uuid.UUID(identity_id)

        # First check if we have cached relationships
        cached = await self._get_cached_relationships(db, target_uuid, limit)
        if cached:
            return cached

        # Calculate relationships from appearances
        relationships = await self._calculate_co_appearances(
            db, target_uuid, time_window_minutes, min_co_appearances, limit
        )

        return relationships

    async def _get_cached_relationships(
        self,
        db: AsyncSession,
        identity_id: uuid.UUID,
        limit: int
    ) -> List[RelatedIdentityInfo]:
        """Get cached relationships from database."""
        query = select(IdentityRelationship).options(
            selectinload(IdentityRelationship.identity2)
        ).where(
            IdentityRelationship.identity_id_1 == identity_id
        ).order_by(
            IdentityRelationship.co_appearance_count.desc()
        ).limit(limit)

        result = await db.execute(query)
        relationships = result.scalars().all()

        if not relationships:
            # Also check reverse direction
            query = select(IdentityRelationship).options(
                selectinload(IdentityRelationship.identity1)
            ).where(
                IdentityRelationship.identity_id_2 == identity_id
            ).order_by(
                IdentityRelationship.co_appearance_count.desc()
            ).limit(limit)

            result = await db.execute(query)
            relationships = result.scalars().all()

            if not relationships:
                return []

            # Swap identity reference for reverse relationships
            return [
                RelatedIdentityInfo(
                    identity_id=str(r.identity_id_1),
                    display_name=r.identity1.display_name if r.identity1 else None,
                    identity_type=r.identity1.type.value if r.identity1 else "unknown",
                    co_appearance_count=r.co_appearance_count,
                    co_appearance_percentage=r.co_appearance_percentage or 0,
                    relationship_strength=r.relationship_strength.value if r.relationship_strength else "weak",
                    common_pipelines=r.common_pipelines or [],
                    first_co_appearance=r.first_co_appearance,
                    last_co_appearance=r.last_co_appearance,
                    best_snapshot_path=r.identity1.best_snapshot_path if r.identity1 else None
                )
                for r in relationships
            ]

        return [
            RelatedIdentityInfo(
                identity_id=str(r.identity_id_2),
                display_name=r.identity2.display_name if r.identity2 else None,
                identity_type=r.identity2.type.value if r.identity2 else "unknown",
                co_appearance_count=r.co_appearance_count,
                co_appearance_percentage=r.co_appearance_percentage or 0,
                relationship_strength=r.relationship_strength.value if r.relationship_strength else "weak",
                common_pipelines=r.common_pipelines or [],
                first_co_appearance=r.first_co_appearance,
                last_co_appearance=r.last_co_appearance,
                best_snapshot_path=r.identity2.best_snapshot_path if r.identity2 else None
            )
            for r in relationships
        ]

    def _calculate_distance_meters(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """
        Calculate distance between two coordinates using Haversine formula.

        Args:
            lat1, lon1: First point coordinates
            lat2, lon2: Second point coordinates

        Returns:
            Distance in meters
        """
        from math import radians, sin, cos, sqrt, atan2

        # Earth's radius in meters
        R = 6371000

        # Convert to radians
        lat1_rad = radians(lat1)
        lat2_rad = radians(lat2)
        delta_lat = radians(lat2 - lat1)
        delta_lon = radians(lon2 - lon1)

        # Haversine formula
        a = sin(delta_lat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(delta_lon / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        distance = R * c

        return distance

    async def _get_pipeline_coordinates_cache(self, db: AsyncSession, pipeline_ids: List[str]) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
        """
        Get pipeline coordinates and cache them for performance.

        Returns:
            Dict mapping pipeline_id to (latitude, longitude) tuple
        """
        from db_models import Pipeline

        if not pipeline_ids:
            return {}

        try:
            query = select(Pipeline).where(Pipeline.pipeline_id.in_(pipeline_ids))
            result = await db.execute(query)
            pipelines = result.scalars().all()

            coordinates = {}
            for pipeline in pipelines:
                coordinates[pipeline.pipeline_id] = (
                    pipeline.latitude if pipeline.latitude is not None else None,
                    pipeline.longitude if pipeline.longitude is not None else None
                )

            return coordinates
        except Exception as e:
            logger.warning(f"[INTELLIGENCE] Error fetching pipeline coordinates: {e}")
            return {}

    async def _calculate_co_appearances(
        self,
        db: AsyncSession,
        identity_id: uuid.UUID,
        time_window_minutes: int,
        min_co_appearances: int,
        limit: int,
        cutoff_date: Optional[datetime] = None,
    ) -> List[RelatedIdentityInfo]:
        """
        Calculate co-appearances from appearance data.

        Supports both:
        1. Same-camera co-appearances (original behavior)
        2. Cross-camera co-appearances (if enabled and coordinates available)

        `cutoff_date` bounds the analysis window — callers advertising a
        days_back parameter used to have it silently ignored here (the scan
        always covered the identity's entire history).
        """
        from config import settings

        # Appearances of the target identity — bounded and cutoff-aware.
        target_appearances_query = select(IdentityAppearance).where(
            IdentityAppearance.identity_id == identity_id
        )
        if cutoff_date is not None:
            target_appearances_query = target_appearances_query.where(
                IdentityAppearance.start_time >= cutoff_date)
        target_appearances_query = target_appearances_query.order_by(
            IdentityAppearance.start_time.desc()).limit(500)
        result = await db.execute(target_appearances_query)
        target_appearances = result.scalars().all()

        if not target_appearances:
            return []

        # Check if multi-camera detection is enabled
        multi_camera_enabled = settings.MULTI_CAMERA_CO_APPEARANCE_ENABLED

        # Build time windows for each appearance
        time_windows = []
        pipeline_ids_set = set()
        for app in target_appearances:
            window_start = app.start_time - timedelta(minutes=time_window_minutes)
            window_end = (app.end_time or app.start_time) + timedelta(minutes=time_window_minutes)
            time_windows.append((app.pipeline_id, window_start, window_end, app.start_time))
            pipeline_ids_set.add(app.pipeline_id)

        # Cross-camera thresholds through the LEARNED-THRESHOLD store with
        # documented precedence (location -> pipeline -> global -> static
        # config default). Only ACTIVATED learned rows are consumed —
        # candidates from the learning job change nothing until an admin
        # activates them. Static config remains the ultimate fallback.
        from backend.core.threshold_store import (
            threshold_store, SIGNAL_TIME_WINDOW, SIGNAL_DISTANCE)
        pipeline_locations: Dict[str, Optional[str]] = {}
        try:
            from db_models import Pipeline as _Pipeline
            loc_result = await db.execute(
                select(_Pipeline.pipeline_id, _Pipeline.location_name)
                .where(_Pipeline.pipeline_id.in_(list(pipeline_ids_set))))
            pipeline_locations = {row[0]: row[1] for row in loc_result}
        except Exception:
            pipeline_locations = {}

        distance_resolved = await threshold_store.resolve(
            db, SIGNAL_DISTANCE,
            static_default=settings.MULTI_CAMERA_DISTANCE_METERS)
        global_window_resolved = await threshold_store.resolve(
            db, SIGNAL_TIME_WINDOW,
            static_default=settings.MULTI_CAMERA_TIME_WINDOW_MINUTES)
        window_by_pipeline: Dict[str, float] = {}
        threshold_provenance = {distance_resolved.provenance,
                                global_window_resolved.provenance}
        for pid in pipeline_ids_set:
            resolved = await threshold_store.resolve(
                db, SIGNAL_TIME_WINDOW,
                pipeline_id=pid, location_name=pipeline_locations.get(pid),
                static_default=settings.MULTI_CAMERA_TIME_WINDOW_MINUTES)
            window_by_pipeline[pid] = resolved.value
            threshold_provenance.add(resolved.provenance)
        logger.debug("[INTELLIGENCE] co-appearance thresholds: distance=%s window=%s provenance=%s",
                     distance_resolved.value, global_window_resolved.value,
                     sorted(threshold_provenance))

        multi_camera_distance = distance_resolved.value
        multi_camera_time_window = global_window_resolved.value
        multi_camera_min = settings.MULTI_CAMERA_MIN_CO_APPEARANCES

        # Pre-load pipeline coordinates if multi-camera is enabled
        pipeline_coords = {}
        if multi_camera_enabled:
            all_pipeline_ids = list(pipeline_ids_set)
            pipeline_coords = await self._get_pipeline_coordinates_cache(db, all_pipeline_ids)
            logger.debug(f"[INTELLIGENCE] Loaded coordinates for {len(pipeline_coords)} pipelines")

        # Find other identities in these time windows
        co_appearance_counts = defaultdict(lambda: {
            'count': 0,
            'pipelines': set(),
            'cross_camera_count': 0,  # Track cross-camera separately
            'same_camera_count': 0,   # Track same-camera separately
            # Which of the TARGET's appearances this identity co-occurred
            # with. The percentage divides by the target's appearance count,
            # so its numerator must count target appearances — the old code
            # incremented once per (window x co-row) pair and produced
            # percentages above 100 (observed live: 225.0).
            'matched_windows': set(),
            'first': None,
            'last': None
        })

        # Process same-camera co-appearances (original logic)
        for window_index, (pipeline_id, window_start, window_end, app_time) in enumerate(time_windows):
            # Other identities in this window on the same pipeline. Interval
            # overlap with COALESCE: appearance rows carry NULL end_time in
            # practice (34/34 live rows), and the old `end_time >= start OR
            # end_time IS NULL` made the window one-sided — ANY appearance
            # before window_end matched, unbounded into the past. A point
            # event's interval is [start, start].
            query = select(IdentityAppearance).where(
                and_(
                    IdentityAppearance.identity_id != identity_id,
                    IdentityAppearance.pipeline_id == pipeline_id,
                    IdentityAppearance.start_time <= window_end,
                    func.coalesce(
                        IdentityAppearance.end_time,
                        IdentityAppearance.start_time
                    ) >= window_start,
                )
            ).order_by(IdentityAppearance.start_time.desc()).limit(1000)
            result = await db.execute(query)
            co_appearances = result.scalars().all()

            for co_app in co_appearances:
                other_id = str(co_app.identity_id)
                co_appearance_counts[other_id]['count'] += 1
                co_appearance_counts[other_id]['same_camera_count'] += 1
                co_appearance_counts[other_id]['matched_windows'].add(window_index)
                co_appearance_counts[other_id]['pipelines'].add(pipeline_id)

                if co_appearance_counts[other_id]['first'] is None or co_app.start_time < co_appearance_counts[other_id]['first']:
                    co_appearance_counts[other_id]['first'] = co_app.start_time
                if co_appearance_counts[other_id]['last'] is None or co_app.start_time > co_appearance_counts[other_id]['last']:
                    co_appearance_counts[other_id]['last'] = co_app.start_time

        # Process cross-camera co-appearances (if enabled)
        if multi_camera_enabled and pipeline_coords:
            logger.debug(f"[INTELLIGENCE] Processing cross-camera co-appearances (distance threshold: {multi_camera_distance}m, time window: {multi_camera_time_window}min)")

            # Get all unique pipeline IDs that have coordinates
            pipelines_with_coords = {
                pid: coords for pid, coords in pipeline_coords.items()
                if coords[0] is not None and coords[1] is not None
            }

            if not pipelines_with_coords:
                logger.debug(f"[INTELLIGENCE] No pipelines with coordinates available, skipping cross-camera detection")
            else:
                # Pre-calculate nearby pipelines for each target pipeline (optimization - avoid recalculating distances)
                nearby_pipelines_cache = {}
                unique_target_pipelines = set(pipeline_ids_set)
                for target_pid in unique_target_pipelines:
                    target_coords = pipelines_with_coords.get(target_pid)
                    if not target_coords:
                        continue

                    target_lat, target_lon = target_coords
                    nearby = []
                    for other_pid, (other_lat, other_lon) in pipelines_with_coords.items():
                        if other_pid != target_pid:
                            distance = self._calculate_distance_meters(target_lat, target_lon, other_lat, other_lon)
                            if distance <= multi_camera_distance:
                                nearby.append(other_pid)
                    nearby_pipelines_cache[target_pid] = nearby

                logger.debug(f"[INTELLIGENCE] Pre-calculated nearby pipelines for {len(nearby_pipelines_cache)} target pipelines")

                # For each target appearance, find cross-camera co-appearances
                for window_index, (target_pipeline_id, window_start, window_end, app_time) in enumerate(time_windows):
                    nearby_pipelines = nearby_pipelines_cache.get(target_pipeline_id, [])

                    if not nearby_pipelines:
                        continue

                    # Use larger time window for cross-camera (people may travel
                    # between cameras). Per-pipeline learned overrides win over
                    # the global value (precedence resolved above).
                    pipeline_window = window_by_pipeline.get(
                        target_pipeline_id, multi_camera_time_window)
                    cross_camera_window_start = app_time - timedelta(minutes=pipeline_window)
                    cross_camera_window_end = app_time + timedelta(minutes=pipeline_window)

                    # Query for identities appearing at nearby cameras within time window
                    # Limit query to prevent performance issues with large datasets
                    try:
                        query = select(IdentityAppearance).where(
                            and_(
                                IdentityAppearance.identity_id != identity_id,
                                IdentityAppearance.pipeline_id.in_(nearby_pipelines),
                                IdentityAppearance.start_time <= cross_camera_window_end,
                                func.coalesce(
                                    IdentityAppearance.end_time,
                                    IdentityAppearance.start_time
                                ) >= cross_camera_window_start,
                            )
                        ).order_by(IdentityAppearance.start_time.desc()).limit(1000)  # deterministic newest-first under the cap

                        result = await db.execute(query)
                        cross_camera_appearances = result.scalars().all()
                    except Exception as e:
                        logger.warning(f"[INTELLIGENCE] Error querying cross-camera appearances for {target_pipeline_id}: {e}")
                        continue

                    for co_app in cross_camera_appearances:
                        other_id = str(co_app.identity_id)
                        # Only count if not already counted as same-camera
                        if co_app.pipeline_id != target_pipeline_id:
                            co_appearance_counts[other_id]['cross_camera_count'] += 1
                            # Add to total count (but weight cross-camera less if desired)
                            co_appearance_counts[other_id]['count'] += 1
                            co_appearance_counts[other_id]['matched_windows'].add(window_index)
                            co_appearance_counts[other_id]['pipelines'].add(co_app.pipeline_id)
                            co_appearance_counts[other_id]['pipelines'].add(target_pipeline_id)  # Also track target pipeline

                            # Use the earlier time for first, later time for last
                            co_app_time = co_app.start_time
                            if co_appearance_counts[other_id]['first'] is None or co_app_time < co_appearance_counts[other_id]['first']:
                                co_appearance_counts[other_id]['first'] = co_app_time
                            if co_appearance_counts[other_id]['last'] is None or co_app_time > co_appearance_counts[other_id]['last']:
                                co_appearance_counts[other_id]['last'] = co_app_time

        # Filter by minimum co-appearances
        # For same-camera: use min_co_appearances
        # For cross-camera: use multi_camera_min (typically lower)
        filtered = {}
        for other_id, data in co_appearance_counts.items():
            same_camera_meets_threshold = data['same_camera_count'] >= min_co_appearances
            cross_camera_meets_threshold = data['cross_camera_count'] >= multi_camera_min if multi_camera_enabled else False
            total_meets_threshold = data['count'] >= min_co_appearances

            # Include if meets any threshold
            if same_camera_meets_threshold or cross_camera_meets_threshold or total_meets_threshold:
                filtered[other_id] = data

        if not filtered:
            logger.debug(f"[INTELLIGENCE] No co-appearances found meeting thresholds (same-camera min: {min_co_appearances}, cross-camera min: {multi_camera_min})")
            return []

        # Get identity details
        identity_ids = [uuid.UUID(i) for i in filtered.keys()]
        query = select(Identity).where(Identity.id.in_(identity_ids))
        result = await db.execute(query)
        identities = {str(i.id): i for i in result.scalars().all()}

        # Build results
        results = []
        total_target_appearances = len(target_appearances)

        for other_id, data in filtered.items():
            identity = identities.get(other_id)
            if not identity:
                continue

            # Share of the TARGET's appearances that had this identity
            # co-occurring — bounded 0..100 by construction. Dividing the raw
            # event count by the appearance count is what produced 225%.
            co_appearance_pct = (
                (len(data['matched_windows']) / total_target_appearances) * 100
                if total_target_appearances > 0 else 0
            )

            # Determine relationship strength (consider both same-camera and cross-camera)
            # Cross-camera relationships are weighted slightly less (0.8x) to account for lower confidence
            effective_count = data['same_camera_count'] + (data['cross_camera_count'] * 0.8) if multi_camera_enabled else data['count']

            # Check activity correlation if enabled (for higher confidence relationships)
            correlation_boost = 1.0
            if settings.ACTIVITY_CORRELATION_ENABLED:
                try:
                    from backend.core.activity_correlation import activity_correlation_analyzer
                    correlation_score, _, _corr_meta = await activity_correlation_analyzer.calculate_correlation(
                        db, str(identity_id), other_id, days_back=90
                    )
                    # Boost effective count based on correlation (up to 30% boost for strong correlation)
                    if correlation_score > 0.5:
                        correlation_boost = 1.0 + (correlation_score * 0.3)
                        effective_count *= correlation_boost
                except Exception as e:
                    logger.debug(f"[INTELLIGENCE] Error calculating correlation for {other_id}: {e}")

            if co_appearance_pct >= 50 or effective_count >= 20:
                strength = "strong"
            elif co_appearance_pct >= 25 or effective_count >= 10:
                strength = "moderate"
            else:
                strength = "weak"

            # Log cross-camera relationships for debugging
            if multi_camera_enabled and data['cross_camera_count'] > 0:
                logger.debug(
                    f"[INTELLIGENCE] Cross-camera relationship: {identity_id} <-> {other_id} "
                    f"(same-camera: {data['same_camera_count']}, cross-camera: {data['cross_camera_count']}, "
                    f"total: {data['count']}, pipelines: {len(data['pipelines'])}, "
                    f"correlation_boost: {correlation_boost:.2f}x)"
                )

            results.append(RelatedIdentityInfo(
                identity_id=other_id,
                display_name=identity.display_name if identity else None,
                identity_type=identity.type.value if identity else "unknown",
                co_appearance_count=data['count'],
                co_appearance_percentage=round(co_appearance_pct, 1),
                relationship_strength=strength,
                common_pipelines=list(data['pipelines']),
                first_co_appearance=data['first'],
                last_co_appearance=data['last'],
                best_snapshot_path=identity.best_snapshot_path if identity else None
            ))

        # Sort by count and limit
        results.sort(key=lambda x: x.co_appearance_count, reverse=True)

        logger.info(
            f"[INTELLIGENCE] Found {len(results)} related identities for {identity_id} "
            f"(same-camera: {sum(1 for r in results if any(p in [tw[0] for tw in time_windows] for p in r.common_pipelines))}, "
            f"cross-camera enabled: {multi_camera_enabled})"
        )

        return results[:limit]

    async def refresh_relationships(
        self,
        db: AsyncSession,
        identity_id: str,
        time_window_minutes: int = None,
        min_co_appearances: int = None
    ):
        """
        Refresh and cache relationship data for an identity.
        Call this periodically or when relationships need updating.
        """
        if time_window_minutes is None:
            time_window_minutes = settings.RELATED_IDENTITY_TIME_WINDOW_MINUTES
        if min_co_appearances is None:
            min_co_appearances = settings.RELATED_IDENTITY_MIN_CO_APPEARANCES

        target_uuid = uuid.UUID(identity_id)

        # Calculate fresh relationships
        relationships = await self._calculate_co_appearances(
            db, target_uuid, time_window_minutes, min_co_appearances, 100
        )

        # Delete old cached relationships
        from sqlalchemy import delete
        await db.execute(
            delete(IdentityRelationship).where(
                or_(
                    IdentityRelationship.identity_id_1 == target_uuid,
                    IdentityRelationship.identity_id_2 == target_uuid
                )
            )
        )

        # Insert new relationships
        for rel in relationships:
            other_uuid = uuid.UUID(rel.identity_id)

            # Ensure consistent ordering (smaller UUID first)
            if target_uuid < other_uuid:
                id1, id2 = target_uuid, other_uuid
            else:
                id1, id2 = other_uuid, target_uuid

            new_rel = IdentityRelationship(
                identity_id_1=id1,
                identity_id_2=id2,
                co_appearance_count=rel.co_appearance_count,
                co_appearance_percentage=rel.co_appearance_percentage,
                relationship_strength=RelationshipStrength(rel.relationship_strength),
                common_pipelines=rel.common_pipelines,
                first_co_appearance=rel.first_co_appearance,
                last_co_appearance=rel.last_co_appearance,
                calculated_at=datetime.utcnow()
            )
            db.add(new_rel)

        await db.commit()
        logger.info(f"[INTELLIGENCE] Refreshed {len(relationships)} relationships for {identity_id}")

    # ==================== TEMPORAL PATTERNS ====================

    async def get_temporal_patterns(
        self,
        db: AsyncSession,
        identity_id: str,
        days_back: int = 90
    ) -> TemporalPattern:
        """
        Analyze temporal patterns for an identity.

        Args:
            identity_id: Target identity UUID
            days_back: Number of days to analyze

        Returns:
            TemporalPattern with hourly/daily distributions
        """
        target_uuid = uuid.UUID(identity_id)
        cutoff_date = datetime.utcnow() - timedelta(days=days_back)

        # Get all appearances
        query = select(IdentityAppearance).where(
            and_(
                IdentityAppearance.identity_id == target_uuid,
                IdentityAppearance.start_time >= cutoff_date
            )
        )
        result = await db.execute(query)
        appearances = result.scalars().all()

        if not appearances:
            return TemporalPattern(
                identity_id=identity_id,
                hourly_distribution={},
                daily_distribution={},
                peak_hours=[],
                peak_days=[],
                most_common_pipelines=[],
                total_appearances=0,
                first_appearance=None,
                last_appearance=None,
                average_appearances_per_day=0
            )

        # Build distributions
        hourly = defaultdict(int)
        daily = defaultdict(int)
        pipeline_counts = defaultdict(int)

        first_app = None
        last_app = None

        for app in appearances:
            hour = app.start_time.hour
            day = app.start_time.weekday()  # 0=Monday, 6=Sunday

            hourly[hour] += 1
            daily[day] += 1
            pipeline_counts[app.pipeline_id] += 1

            if first_app is None or app.start_time < first_app:
                first_app = app.start_time
            if last_app is None or app.start_time > last_app:
                last_app = app.start_time

        # Calculate peak hours (top 3)
        sorted_hours = sorted(hourly.items(), key=lambda x: x[1], reverse=True)
        peak_hours = [h for h, _ in sorted_hours[:3]]

        # Calculate peak days
        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        sorted_days = sorted(daily.items(), key=lambda x: x[1], reverse=True)
        peak_days = [day_names[d] for d, _ in sorted_days[:3]]

        # Get pipeline names
        pipeline_ids = list(pipeline_counts.keys())
        pipeline_query = select(Pipeline).where(Pipeline.pipeline_id.in_(pipeline_ids))
        result = await db.execute(pipeline_query)
        pipelines = {p.pipeline_id: p for p in result.scalars().all()}

        most_common_pipelines = [
            {
                'pipeline_id': pid,
                'count': count,
                'name': pipelines[pid].pipeline_id if pid in pipelines else pid
            }
            for pid, count in sorted(pipeline_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        ]

        # Calculate average per day
        if first_app and last_app:
            days_span = (last_app - first_app).days + 1
            avg_per_day = len(appearances) / days_span if days_span > 0 else 0
        else:
            avg_per_day = 0

        return TemporalPattern(
            identity_id=identity_id,
            hourly_distribution=dict(hourly),
            daily_distribution=dict(daily),
            peak_hours=peak_hours,
            peak_days=peak_days,
            most_common_pipelines=most_common_pipelines,
            total_appearances=len(appearances),
            first_appearance=first_app,
            last_appearance=last_app,
            average_appearances_per_day=round(avg_per_day, 2)
        )

    # ==================== CROSS-CAMERA TRACKING ====================

    async def get_cross_camera_track(
        self,
        db: AsyncSession,
        identity_id: str,
        date: str = None,
        days_back: int = 1
    ) -> List[CrossCameraTrack]:
        """
        Track an identity's movement across cameras.

        Args:
            identity_id: Target identity UUID
            date: Specific date (YYYY-MM-DD) or None for recent
            days_back: Number of days to look back if no date specified

        Returns:
            List of CrossCameraTrack objects, one per day
        """
        target_uuid = uuid.UUID(identity_id)

        # Determine date range
        if date:
            try:
                target_date = datetime.strptime(date, "%Y-%m-%d")
                start_date = target_date.replace(hour=0, minute=0, second=0)
                end_date = target_date.replace(hour=23, minute=59, second=59)
            except ValueError:
                raise ValueError("Invalid date format. Use YYYY-MM-DD")
        else:
            end_date = datetime.utcnow()
            start_date = end_date - timedelta(days=days_back)

        # Get appearances in date range
        query = select(IdentityAppearance).where(
            and_(
                IdentityAppearance.identity_id == target_uuid,
                IdentityAppearance.start_time >= start_date,
                IdentityAppearance.start_time <= end_date
            )
        ).order_by(IdentityAppearance.start_time)

        result = await db.execute(query)
        appearances = result.scalars().all()

        if not appearances:
            return []

        # Get identity info
        identity_query = select(Identity).where(Identity.id == target_uuid)
        result = await db.execute(identity_query)
        identity = result.scalar_one_or_none()

        # Get pipeline info
        pipeline_ids = list(set(a.pipeline_id for a in appearances))
        pipeline_query = select(Pipeline).where(Pipeline.pipeline_id.in_(pipeline_ids))
        result = await db.execute(pipeline_query)
        pipelines = {p.pipeline_id: p for p in result.scalars().all()}

        # Group appearances by date
        appearances_by_date = defaultdict(list)
        for app in appearances:
            date_key = app.start_time.strftime("%Y-%m-%d")
            appearances_by_date[date_key].append(app)

        # Build tracks
        tracks = []
        for date_key, day_appearances in appearances_by_date.items():
            movements = []
            prev_end = None

            for app in day_appearances:
                # Calculate duration at this location
                if app.end_time:
                    duration = int((app.end_time - app.start_time).total_seconds())
                else:
                    duration = None

                # Get pipeline coordinates if available
                pipeline = pipelines.get(app.pipeline_id)
                coordinates = None
                if pipeline and pipeline.latitude is not None and pipeline.longitude is not None:
                    coordinates = {"lat": float(pipeline.latitude), "lng": float(pipeline.longitude)}

                movements.append(CrossCameraMovement(
                    pipeline_id=app.pipeline_id,
                    pipeline_name=pipeline.location_name if pipeline and pipeline.location_name else (pipeline.pipeline_id if pipeline else app.pipeline_id),
                    timestamp=app.start_time,
                    snapshot_path=app.best_snapshot_path,
                    duration_at_location=duration,
                    coordinates=coordinates
                ))

                prev_end = app.end_time or app.start_time

            # Calculate total duration
            first_seen = day_appearances[0].start_time
            last_seen = day_appearances[-1].end_time or day_appearances[-1].start_time
            total_minutes = int((last_seen - first_seen).total_seconds() / 60)

            tracks.append(CrossCameraTrack(
                identity_id=identity_id,
                display_name=identity.display_name if identity else None,
                date=date_key,
                movements=movements,
                total_cameras=len(set(m.pipeline_id for m in movements)),
                first_seen=first_seen,
                last_seen=last_seen,
                total_duration_minutes=total_minutes
            ))

        return tracks

    async def get_movement_timeline(
        self,
        db: AsyncSession,
        identity_id: str,
        hours_back: int = 24
    ) -> Dict:
        """
        Get a simplified movement timeline for dashboard display.

        Returns:
            Dictionary with timeline entries and summary
        """
        target_uuid = uuid.UUID(identity_id)
        cutoff = datetime.utcnow() - timedelta(hours=hours_back)

        # Get recent appearances
        query = select(IdentityAppearance).where(
            and_(
                IdentityAppearance.identity_id == target_uuid,
                IdentityAppearance.start_time >= cutoff
            )
        ).order_by(IdentityAppearance.start_time.desc())

        result = await db.execute(query)
        appearances = result.scalars().all()

        # Get pipeline names
        pipeline_ids = list(set(a.pipeline_id for a in appearances))
        if pipeline_ids:
            pipeline_query = select(Pipeline).where(Pipeline.pipeline_id.in_(pipeline_ids))
            result = await db.execute(pipeline_query)
            pipelines = {p.pipeline_id: p.pipeline_id for p in result.scalars().all()}
        else:
            pipelines = {}

        timeline = []
        for app in appearances:
            timeline.append({
                'timestamp': app.start_time.isoformat(),
                'pipeline_id': app.pipeline_id,
                'pipeline_name': pipelines.get(app.pipeline_id, app.pipeline_id),
                'snapshot_path': app.best_snapshot_path,
                'duration_seconds': int((app.end_time - app.start_time).total_seconds()) if app.end_time else None
            })

        # Summary
        unique_locations = len(set(a.pipeline_id for a in appearances))

        return {
            'identity_id': identity_id,
            'hours_back': hours_back,
            'timeline': timeline,
            'summary': {
                'total_appearances': len(appearances),
                'unique_locations': unique_locations,
                'first_seen': appearances[-1].start_time.isoformat() if appearances else None,
                'last_seen': appearances[0].start_time.isoformat() if appearances else None
            }
        }

    # ==================== BATCH ANALYSIS ====================

    async def analyze_identity(
        self,
        db: AsyncSession,
        identity_id: str
    ) -> Dict:
        """
        Perform comprehensive analysis of an identity.

        Returns all intelligence data in one call.
        """
        # Get all data in parallel-style (sequentially for now)
        related = await self.get_related_identities(db, identity_id)
        patterns = await self.get_temporal_patterns(db, identity_id)
        tracks = await self.get_cross_camera_track(db, identity_id, days_back=7)
        timeline = await self.get_movement_timeline(db, identity_id)

        return {
            'identity_id': identity_id,
            'analyzed_at': datetime.utcnow().isoformat(),
            'related_identities': [
                {
                    'identity_id': r.identity_id,
                    'display_name': r.display_name,
                    'type': r.identity_type,
                    'co_appearances': r.co_appearance_count,
                    'percentage': r.co_appearance_percentage,
                    'strength': r.relationship_strength,
                    'common_locations': r.common_pipelines,
                    'snapshot_path': r.best_snapshot_path
                }
                for r in related
            ],
            'temporal_patterns': {
                'hourly_distribution': patterns.hourly_distribution,
                'daily_distribution': patterns.daily_distribution,
                'peak_hours': patterns.peak_hours,
                'peak_days': patterns.peak_days,
                'most_common_locations': patterns.most_common_pipelines,
                'total_appearances': patterns.total_appearances,
                'avg_per_day': patterns.average_appearances_per_day
            },
            'cross_camera_tracks': [
                {
                    'date': t.date,
                    'cameras_visited': t.total_cameras,
                    'first_seen': t.first_seen.isoformat() if t.first_seen else None,
                    'last_seen': t.last_seen.isoformat() if t.last_seen else None,
                    'duration_minutes': t.total_duration_minutes,
                    'movements': [
                        {
                            'location': m.pipeline_name,
                            'time': m.timestamp.isoformat(),
                            'duration_seconds': m.duration_at_location
                        }
                        for m in t.movements
                    ]
                }
                for t in tracks
            ],
            'recent_timeline': timeline
        }


# Global instance
intelligence_service = IntelligenceService()

