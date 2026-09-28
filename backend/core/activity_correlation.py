"""
Activity Correlation Analysis (xCCA)
=====================================
Detects temporally-linked movement sequences between cameras — association
evidence only; correlation does not prove causation, and neither this module
nor its API ever claims otherwise.
"""

import asyncio
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
                .limit(CORRELATION_MAX_APPEARANCES_PER_SIDE + 1)
            )
            rows = list(result.scalars().all())
            return rows

        appearances_a = await _appearances(uuid_a)
        appearances_b = await _appearances(uuid_b)

        meta = {
            "truncated": (
                len(appearances_a) > CORRELATION_MAX_APPEARANCES_PER_SIDE
                or len(appearances_b) > CORRELATION_MAX_APPEARANCES_PER_SIDE
            ),
            "appearances_a": len(appearances_a),
            "appearances_b": len(appearances_b),
        }

        appearances_a = sorted(appearances_a[:CORRELATION_MAX_APPEARANCES_PER_SIDE], key=lambda row: row.start_time)
        appearances_b = sorted(appearances_b[:CORRELATION_MAX_APPEARANCES_PER_SIDE], key=lambda row: row.start_time)
        meta.update(appearances_a=len(appearances_a), appearances_b=len(appearances_b), sequence_count=0)
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

        return await asyncio.to_thread(self._match_sequences, appearances_a, appearances_b,
            nearby_map, settings.MULTI_CAMERA_TIME_WINDOW_MINUTES, meta)

    def _match_sequences(self, appearances_a, appearances_b, nearby_map, minutes, meta):
        """Exact aggregate score with at most 20 retained examples (A then B)."""
        window = timedelta(minutes=minutes)
        sequences, matched_a, matched_b = [], set(), set()
        statistics = {}  # Welford count/mean/M2, one entry per camera pair
        lo = total = 0
        for a_idx, app_a in enumerate(appearances_a):
            while lo < len(appearances_b) and appearances_b[lo].start_time <= app_a.start_time:
                lo += 1
            j = lo
            while j < len(appearances_b) and appearances_b[j].start_time < app_a.start_time + window:
                app_b = appearances_b[j]
                if app_b.pipeline_id in nearby_map.get(app_a.pipeline_id, set()):
                    difference = (app_b.start_time - app_a.start_time).total_seconds() / 60
                    pair = (app_a.pipeline_id, app_b.pipeline_id)
                    count, mean, m2 = statistics.get(pair, (0, 0.0, 0.0))
                    count += 1
                    delta = difference - mean
                    mean += delta / count
                    statistics[pair] = (count, mean, m2 + delta * (difference - mean))
                    total += 1
                    matched_a.add(a_idx)
                    matched_b.add(j)
                    if len(sequences) < 20:
                        sequences.append({"from_camera": pair[0], "to_camera": pair[1],
                            "time_diff_minutes": difference, "from_time": app_a.start_time,
                            "to_time": app_b.start_time})
                j += 1
        meta = dict(meta, sequence_count=total, examples_truncated=total > len(sequences))
        if total < self.min_sequences_for_correlation:
            return 0.0, sequences, meta
        modal_share = max(item[0] for item in statistics.values()) / total
        regularity = sum(count / total / (1 + math.sqrt(max(0.0, m2 / count)) / mean)
                         for count, mean, m2 in statistics.values())
        consistency = 0.5 * modal_share + 0.5 * regularity
        participation = (len(matched_a) + len(matched_b)) / (len(appearances_a) + len(appearances_b))
        return min(1.0, participation * (0.7 + 0.3 * consistency)), sequences, meta

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
