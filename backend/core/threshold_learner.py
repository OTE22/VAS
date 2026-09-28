"""
Automatic Threshold Learning
============================
Learns optimal distance and time thresholds for each camera pair based on historical data.
"""

import asyncio
import logging
from typing import Dict, Tuple, Optional, List
from collections import defaultdict
from datetime import datetime, timedelta

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    # Fallback percentile calculation
    def percentile(data, p):
        """Simple percentile calculation without numpy."""
        if not data:
            return 0.0
        sorted_data = sorted(data)
        index = int(len(sorted_data) * p / 100.0)
        return sorted_data[min(index, len(sorted_data) - 1)]

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func, text
from db_models import IdentityAppearance, Pipeline
from config import settings

logger = logging.getLogger(__name__)

# Bound the per-camera appearance scan feeding the pairwise movement search.
# learn_all_camera_pairs iterates O(cameras²) pairs; each pair used to pull
# BOTH cameras' full appearance history.
THRESHOLD_MAX_APPEARANCES_PER_CAMERA = 2000


class ThresholdLearner:
    """
    Learns optimal thresholds for cross-camera co-appearance detection.
    """
    
    def __init__(self):
        self.learned_thresholds: Dict[Tuple[str, str], Dict] = {}
        self._pipeline_cache = None
        self._appearance_cache = None
        self.history_truncated = False

    # Module-level singleton: these must be read per call, not captured in
    # __init__, or an admin edit never reaches them.
    @property
    def enabled(self) -> bool:
        return bool(settings.AUTO_THRESHOLD_LEARNING_ENABLED)

    @property
    def min_samples_for_learning(self) -> int:
        """Minimum movements needed to learn. Duplicated the declared
        THRESHOLD_MIN_SAMPLES_FOR_ACTIVATION setting as a literal 10."""
        return int(settings.THRESHOLD_MIN_SAMPLES_FOR_ACTIVATION)

    
    async def learn_thresholds_for_pair(
        self,
        db: AsyncSession,
        camera_1: str,
        camera_2: str
    ) -> Optional[Dict]:
        """
        Learn optimal thresholds for a camera pair based on historical cross-camera movements.
        
        Returns:
            Dict with 'time_window_minutes' and 'distance_meters', or None if insufficient data
        """
        # The declared feature flag now gates the feature; it used to be
        # rendered as an editable switch that nothing read.
        if not self.enabled:
            return None
        try:
            # Get pipeline coordinates
            pipeline_query = select(Pipeline).where(
                Pipeline.pipeline_id.in_([camera_1, camera_2])
            )
            if self._pipeline_cache is None:
                result = await db.execute(pipeline_query)
                pipelines = {p.pipeline_id: p for p in result.scalars().all()}
            else:
                pipelines = self._pipeline_cache
            
            pipeline_1 = pipelines.get(camera_1)
            pipeline_2 = pipelines.get(camera_2)
            
            if not pipeline_1 or not pipeline_2:
                logger.debug(f"[THRESHOLD_LEARNER] Missing pipeline data for {camera_1} or {camera_2}")
                return None
            
            if pipeline_1.latitude is None or pipeline_1.longitude is None:
                logger.debug(f"[THRESHOLD_LEARNER] Missing coordinates for {camera_1}")
                return None
            
            if pipeline_2.latitude is None or pipeline_2.longitude is None:
                logger.debug(f"[THRESHOLD_LEARNER] Missing coordinates for {camera_2}")
                return None
            
            # Calculate actual distance
            distance = self._calculate_distance_meters(
                pipeline_1.latitude, pipeline_1.longitude,
                pipeline_2.latitude, pipeline_2.longitude
            )
            
            # Get cross-camera movements (same identity appearing at both cameras)
            movements = await self._get_cross_camera_movements(
                db, camera_1, camera_2
            )
            
            if len(movements) < self.min_samples_for_learning:
                logger.debug(
                    f"[THRESHOLD_LEARNER] Insufficient data for {camera_1} <-> {camera_2}: "
                    f"{len(movements)} movements (need {self.min_samples_for_learning})"
                )
                return None
            
            # Calculate travel times
            travel_times = [m['time_diff_minutes'] for m in movements]

            # Learn optimal time window (95th percentile + buffer)
            # This covers 95% of actual travel times
            if NUMPY_AVAILABLE:
                p95_minutes = np.percentile(travel_times, 95) if len(travel_times) > 0 else 10.0
            else:
                p95_minutes = percentile(travel_times, 95) if len(travel_times) > 0 else 10.0
            optimal_time_window = max(p95_minutes * 1.2, 5.0)  # 20% buffer, min 5min

            # Learn optimal distance (actual distance + 20% buffer for GPS inaccuracy)
            optimal_distance = distance * 1.2

            # Dispersion of the observed travel times. The old confidence was
            # literally samples/50 — a sample COUNT dressed up as a confidence
            # (50 wildly-scattered transits scored 1.0; 20 metronomic ones
            # scored 0.4). Report the ingredients separately and derive the
            # headline number from both sufficiency AND tightness:
            #   sample_term     = samples / (samples + min_samples)   -> 0..1
            #   dispersion_term = 1 / (1 + CV)   (CV = std/mean, scale-free)
            #   confidence      = sample_term * dispersion_term
            mean_minutes = sum(travel_times) / len(travel_times)
            spread_minutes = (
                sum((t - mean_minutes) ** 2 for t in travel_times) / len(travel_times)
            ) ** 0.5
            cv = (spread_minutes / mean_minutes) if mean_minutes > 0 else float('inf')
            dispersion_term = 1.0 / (1.0 + cv) if cv != float('inf') else 0.0
            sample_term = len(movements) / (len(movements) + self.min_samples_for_learning)
            confidence = sample_term * dispersion_term

            learned = {
                'camera_pair': (camera_1, camera_2),
                'optimal_time_window_minutes': float(optimal_time_window),
                'optimal_distance_meters': float(optimal_distance),
                'actual_distance_meters': float(distance),
                'confidence': float(confidence),
                'sample_count': len(movements),
                'p95_minutes': float(p95_minutes),
                'spread_minutes': float(spread_minutes),
                'mean_minutes': float(mean_minutes),
                'learned_at': datetime.utcnow()
            }
            
            # Cache the learned thresholds
            self.learned_thresholds[(camera_1, camera_2)] = learned
            self.learned_thresholds[(camera_2, camera_1)] = learned  # Symmetric
            
            logger.info(
                f"[THRESHOLD_LEARNER] Learned thresholds for {camera_1} <-> {camera_2}: "
                f"time_window={optimal_time_window:.1f}min, distance={optimal_distance:.0f}m "
                f"(confidence={confidence:.2f}, samples={len(movements)})"
            )
            
            return learned
            
        except Exception as e:
            logger.error(f"[THRESHOLD_LEARNER] Error learning thresholds for {camera_1} <-> {camera_2}: {e}", exc_info=True)
            raise

    async def _camera_appearances(self, db, camera_id, days_back=90):
        if self._appearance_cache is not None and camera_id in self._appearance_cache:
            return self._appearance_cache[camera_id]
        rows = list((await db.execute(select(IdentityAppearance).where(and_(
            IdentityAppearance.pipeline_id == camera_id,
            IdentityAppearance.start_time >= datetime.utcnow() - timedelta(days=days_back)
        )).order_by(IdentityAppearance.start_time.desc(), IdentityAppearance.id.desc())
          .limit(THRESHOLD_MAX_APPEARANCES_PER_CAMERA + 1))).scalars().all())
        self.history_truncated |= len(rows) > THRESHOLD_MAX_APPEARANCES_PER_CAMERA
        rows = rows[:THRESHOLD_MAX_APPEARANCES_PER_CAMERA]
        rows.sort(key=lambda row: row.start_time)
        return rows

    async def _get_cross_camera_movements(
        self,
        db: AsyncSession,
        camera_1: str,
        camera_2: str,
        days_back: int = 90
    ) -> List[Dict]:
        """
        Get historical cross-camera movements between two cameras.
        Returns movements where same identity appeared at both cameras.
        """
        appearances_1 = await self._camera_appearances(db, camera_1, days_back)
        appearances_2 = await self._camera_appearances(db, camera_2, days_back)
        by_identity = defaultdict(list)
        for app in appearances_1 + appearances_2:
            by_identity[app.identity_id].append(app)
        movements = []
        # Adjacent camera visits in either direction; repeated detections at
        # one camera do not multiply the same transition into many samples.
        for identity_id, appearances in by_identity.items():
            appearances.sort(key=lambda app: (app.start_time, app.pipeline_id))
            for previous, current in zip(appearances, appearances[1:]):
                minutes = (current.start_time - previous.start_time).total_seconds() / 60
                if previous.pipeline_id != current.pipeline_id and 0 < minutes < 60:
                    movements.append({"identity_id": identity_id,
                        "from_camera": previous.pipeline_id, "to_camera": current.pipeline_id,
                        "time_diff_minutes": minutes, "from_time": previous.start_time,
                        "to_time": current.start_time})
        return movements
    
    def get_thresholds(
        self,
        camera_1: str,
        camera_2: str
    ) -> Tuple[float, float]:
        """
        Get learned thresholds for a camera pair, or use defaults.
        
        Returns:
            (time_window_minutes, distance_meters)
        """
        # Try both orderings (symmetric)
        learned = self.learned_thresholds.get((camera_1, camera_2)) or \
                  self.learned_thresholds.get((camera_2, camera_1))
        
        if learned and learned.get('confidence', 0) > 0.3:  # Use if confidence > 30%
            return (
                learned['optimal_time_window_minutes'],
                learned['optimal_distance_meters']
            )
        
        # Fallback to defaults
        return (
            settings.MULTI_CAMERA_TIME_WINDOW_MINUTES,
            settings.MULTI_CAMERA_DISTANCE_METERS
        )
    
    async def learn_all_camera_pairs(
        self,
        db: AsyncSession,
        pipeline_ids: List[str],
        progress=None
    ) -> Dict[Tuple[str, str], Dict]:
        """
        Learn thresholds for all camera pairs in the network.
        """
        if not self.enabled:
            raise RuntimeError("Threshold learning is disabled")
        if len(pipeline_ids) > 100:
            raise ValueError("At most 100 cameras per learning job")
        learned = {}
        self.history_truncated = False
        self._appearance_cache = {}
        try:
            rows = await db.execute(select(Pipeline).where(and_(
                Pipeline.pipeline_id.in_(pipeline_ids),
                Pipeline.latitude.isnot(None), Pipeline.longitude.isnot(None))))
            self._pipeline_cache = {p.pipeline_id: p for p in rows.scalars().all()
                if -90 <= p.latitude <= 90 and -180 <= p.longitude <= 180}
            cameras = sorted(self._pipeline_cache)
            for camera in cameras:
                self._appearance_cache[camera] = await self._camera_appearances(db, camera)
            total = len(cameras) * (len(cameras) - 1) // 2
            completed = 0
            for i, camera_1 in enumerate(cameras):
                for camera_2 in cameras[i+1:]:
                    result = await self.learn_thresholds_for_pair(db, camera_1, camera_2)
                    if result:
                        learned[(camera_1, camera_2)] = result
                    completed += 1
                    if progress and (completed % 10 == 0 or completed == total):
                        await progress(min(95, int(completed / total * 95)))
                    await asyncio.sleep(0)  # allow timeout/cancellation between pairs
            return learned
        finally:
            self._pipeline_cache = None
            self._appearance_cache = None

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
threshold_learner = ThresholdLearner()

