"""
Activity Correlation Analysis (xCCA)
=====================================
Detects temporally-linked movement sequences between cameras — association
evidence only; correlation does not prove causation, and neither this module
nor its API ever claims otherwise.
"""

import logging
import math
import uuid as uuid_module
from typing import List, Dict, Tuple, Optional
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from db_models import IdentityAppearance, Pipeline
from config import settings

logger = logging.getLogger(__name__)

# Per-side appearance cap. Two busy identities used to feed an O(A x B)
# nested Python loop — 5,000 appearances each meant 25M datetime comparisons
# in one request, and this function runs once per candidate inside the
# co-appearance calculation.
CORRELATION_MAX_APPEARANCES_PER_SIDE = 500


class ActivityCorrelationAnalyzer:
    """
    Analyzes temporal correlations between two identities' movements.

    Scoring (documented so the number is explainable):
      participation = (distinct A-appearances in a sequence
                       + distinct B-appearances in a sequence)
                      / (len(A) + len(B))            -> symmetric, 0..1
      consistency   = mean over camera pairs (weighted by pair frequency) of
                        0.5 * modal-pair share
                      + 0.5 * 1/(1 + coefficient of variation of travel time)
      score         = participation * (0.7 + 0.3 * consistency)

    The previous score divided the raw sequence count by max(len(A), len(B)),
    which was asymmetric in effect (the busier identity was structurally
    penalised), and its time-consistency term used raw variance in minutes²,
    making the number scale-dependent (a 2-minute spread already crushed it).
    """

    def __init__(self):
        self.min_sequences_for_correlation = 3

    async def calculate_correlation(
        self,
        db: AsyncSession,
        identity_a: str,
        identity_b: str,
        days_back: int = 90
    ) -> Tuple[float, List[Dict], Dict]:
        """
        Calculate correlation between two identities' activities.

        Returns:
            (correlation_score, sequence_patterns, meta)
            - correlation_score: 0.0 to 1.0
            - sequence_patterns: detected A→B sequences
            - meta: {"truncated": bool, "appearances_a": int, "appearances_b": int}

        Raises on infrastructure failure — a DB error must surface as an
        error, not masquerade as "no correlation" (the old blanket
        `except → return 0.0` made the two indistinguishable).
        """
        cutoff_date = datetime.utcnow() - timedelta(days=days_back)
        uuid_a = uuid_module.UUID(identity_a)
        uuid_b = uuid_module.UUID(identity_b)

        # Bounded, newest-first (then re-sorted ascending for the merge scan).
        async def _appearances(identity_uuid):
            result = await db.execute(
                select(IdentityAppearance).where(
                    and_(
                        IdentityAppearance.identity_id == identity_uuid,
                        IdentityAppearance.start_time >= cutoff_date
                    )
                ).order_by(IdentityAppearance.start_time.desc())
                .limit(CORRELATION_MAX_APPEARANCES_PER_SIDE)
            )
            rows = list(result.scalars().all())
            rows.sort(key=lambda r: r.start_time)
            return rows

        appearances_a = await _appearances(uuid_a)
        appearances_b = await _appearances(uuid_b)

        meta = {
            "truncated": (
                len(appearances_a) >= CORRELATION_MAX_APPEARANCES_PER_SIDE
                or len(appearances_b) >= CORRELATION_MAX_APPEARANCES_PER_SIDE
            ),
            "appearances_a": len(appearances_a),
            "appearances_b": len(appearances_b),
        }

        if not appearances_a or not appearances_b:
            return 0.0, [], meta

        # Pipeline coordinates → nearby-camera map, computed ONCE. The old
        # code recomputed Haversine over every pipeline inside the outer loop.
        all_pipeline_ids = {a.pipeline_id for a in appearances_a} | \
                           {b.pipeline_id for b in appearances_b}
        pipeline_coords = await self._get_pipeline_coordinates(db, list(all_pipeline_ids))
        max_distance = settings.MULTI_CAMERA_DISTANCE_METERS
        nearby_map: Dict[str, set] = {
            cam: set(self._get_nearby_cameras(cam, pipeline_coords, max_distance))
            for cam in all_pipeline_ids
        }

        # Two-pointer sweep over the time-sorted lists. For each A-appearance,
        # B-candidates lie strictly after it and within the window; the lower
        # pointer only ever advances, so the scan is O(A + B + matches)
        # instead of O(A x B).
        max_time_window = settings.MULTI_CAMERA_TIME_WINDOW_MINUTES
        window = timedelta(minutes=max_time_window)

        sequences: List[Dict] = []
        matched_a: set = set()
        matched_b: set = set()
        lo = 0
        for a_idx, app_a in enumerate(appearances_a):
            while lo < len(appearances_b) and appearances_b[lo].start_time <= app_a.start_time:
                lo += 1
            nearby = nearby_map.get(app_a.pipeline_id, set())
            j = lo
            while j < len(appearances_b) and appearances_b[j].start_time < app_a.start_time + window:
                app_b = appearances_b[j]
                if app_b.pipeline_id in nearby:
                    time_diff = (app_b.start_time - app_a.start_time).total_seconds() / 60.0
                    sequences.append({
                        'from_camera': app_a.pipeline_id,
                        'to_camera': app_b.pipeline_id,
                        'time_diff_minutes': time_diff,
                        'from_time': app_a.start_time,
                        'to_time': app_b.start_time
                    })
                    matched_a.add(a_idx)
                    matched_b.add(j)
                j += 1

        if len(sequences) < self.min_sequences_for_correlation:
            logger.debug(
                f"[ACTIVITY_CORR] Insufficient sequences for correlation: "
                f"{identity_a} <-> {identity_b}: {len(sequences)} sequences"
            )
            return 0.0, sequences, meta

        # Symmetric participation rate, 0..1 by construction.
        participation = (len(matched_a) + len(matched_b)) / (
            len(appearances_a) + len(appearances_b))

        pattern_consistency = self._calculate_pattern_consistency(sequences)
        correlation_score = min(1.0, participation * (0.7 + 0.3 * pattern_consistency))

        logger.debug(
            f"[ACTIVITY_CORR] Correlation {identity_a} <-> {identity_b}: "
            f"score={correlation_score:.3f}, sequences={len(sequences)}"
        )

        return correlation_score, sequences, meta

    def _calculate_pattern_consistency(self, sequences: List[Dict]) -> float:
        """
        How consistent the sequences are: 0..1.

        Two components, each scale-free:
        - modal-pair share: fraction of sequences on the most common camera
          pair (route regularity);
        - travel-time regularity: 1/(1 + CV) per pair, weighted by pair
          frequency. CV (std/mean) is dimensionless, unlike the raw variance
          the previous version used, which punished any spread over ~1 minute
          regardless of the route's actual travel time.
        """
        if len(sequences) < 2:
            return 1.0

        camera_pair_times = defaultdict(list)
        for seq in sequences:
            camera_pair_times[(seq['from_camera'], seq['to_camera'])].append(
                seq['time_diff_minutes'])

        total = len(sequences)
        modal_share = max(len(times) for times in camera_pair_times.values()) / total

        weighted_time_consistency = 0.0
        for times in camera_pair_times.values():
            weight = len(times) / total
            if len(times) < 2:
                pair_consistency = 1.0
            else:
                mean = sum(times) / len(times)
                if mean <= 0:
                    pair_consistency = 0.0
                else:
                    std = math.sqrt(sum((t - mean) ** 2 for t in times) / len(times))
                    cv = std / mean
                    pair_consistency = 1.0 / (1.0 + cv)
            weighted_time_consistency += weight * pair_consistency

        return 0.5 * modal_share + 0.5 * weighted_time_consistency

    def _get_nearby_cameras(
        self,
        camera_id: str,
        pipeline_coords: Dict[str, Tuple[float, float]],
        max_distance: float
    ) -> List[str]:
        """Get cameras within max_distance of the given camera."""
        if camera_id not in pipeline_coords:
            return []

        camera_lat, camera_lon = pipeline_coords[camera_id]
        nearby = []

        for other_camera, (other_lat, other_lon) in pipeline_coords.items():
            if other_camera != camera_id:
                distance = self._calculate_distance_meters(
                    camera_lat, camera_lon,
                    other_lat, other_lon
                )
                if distance <= max_distance:
                    nearby.append(other_camera)

        return nearby

    async def _get_pipeline_coordinates(
        self,
        db: AsyncSession,
        pipeline_ids: List[str]
    ) -> Dict[str, Tuple[float, float]]:
        """Get pipeline coordinates."""
        if not pipeline_ids:
            return {}

        query = select(Pipeline).where(Pipeline.pipeline_id.in_(pipeline_ids))
        result = await db.execute(query)
        pipelines = result.scalars().all()

        coords = {}
        for pipeline in pipelines:
            if pipeline.latitude and pipeline.longitude:
                coords[pipeline.pipeline_id] = (pipeline.latitude, pipeline.longitude)

        return coords

    def _calculate_distance_meters(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance using Haversine formula."""
        from math import radians, sin, cos, sqrt, atan2

        R = 6371000  # Earth's radius in meters
        lat1_rad = radians(lat1)
        lat2_rad = radians(lat2)
        delta_lat = radians(lat2 - lat1)
        delta_lon = radians(lon2 - lon1)

        a = sin(delta_lat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(delta_lon / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        distance = R * c

        return distance


# Global instance
activity_correlation_analyzer = ActivityCorrelationAnalyzer()
