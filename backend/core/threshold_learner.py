"""
Automatic Threshold Learning
============================
Learns optimal distance and time thresholds for each camera pair based on historical data.
"""

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


class ThresholdLearner:
    """
    Learns optimal thresholds for cross-camera co-appearance detection.
    """
    
    def __init__(self):
        self.learned_thresholds: Dict[Tuple[str, str], Dict] = {}
        self.min_samples_for_learning = 10  # Minimum movements needed to learn
    
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
        try:
            # Get pipeline coordinates
            pipeline_query = select(Pipeline).where(
                Pipeline.pipeline_id.in_([camera_1, camera_2])
            )
            result = await db.execute(pipeline_query)
            pipelines = {p.pipeline_id: p for p in result.scalars().all()}
            
            pipeline_1 = pipelines.get(camera_1)
            pipeline_2 = pipelines.get(camera_2)
            
            if not pipeline_1 or not pipeline_2:
                logger.debug(f"[THRESHOLD_LEARNER] Missing pipeline data for {camera_1} or {camera_2}")
                return None
            
            if not pipeline_1.latitude or not pipeline_1.longitude:
                logger.debug(f"[THRESHOLD_LEARNER] Missing coordinates for {camera_1}")
                return None
            
            if not pipeline_2.latitude or not pipeline_2.longitude:
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
                optimal_time_window = np.percentile(travel_times, 95) if len(travel_times) > 0 else 10.0
            else:
                optimal_time_window = percentile(travel_times, 95) if len(travel_times) > 0 else 10.0
            optimal_time_window = max(optimal_time_window * 1.2, 5.0)  # 20% buffer, min 5min
            
            # Learn optimal distance (actual distance + 20% buffer for GPS inaccuracy)
            optimal_distance = distance * 1.2
            
            confidence = min(len(movements) / 50.0, 1.0)  # More data = higher confidence (max 1.0)
            
            learned = {
                'camera_pair': (camera_1, camera_2),
                'optimal_time_window_minutes': float(optimal_time_window),
                'optimal_distance_meters': float(optimal_distance),
                'actual_distance_meters': float(distance),
                'confidence': float(confidence),
                'sample_count': len(movements),
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
            return None
    
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
        cutoff_date = datetime.utcnow() - timedelta(days=days_back)
        
        # Get all appearances at camera_1
        query_1 = select(IdentityAppearance).where(
            and_(
                IdentityAppearance.pipeline_id == camera_1,
                IdentityAppearance.start_time >= cutoff_date
            )
        ).order_by(IdentityAppearance.identity_id, IdentityAppearance.start_time)
        
        result_1 = await db.execute(query_1)
        appearances_1 = result_1.scalars().all()
        
        # Get all appearances at camera_2
        query_2 = select(IdentityAppearance).where(
            and_(
                IdentityAppearance.pipeline_id == camera_2,
                IdentityAppearance.start_time >= cutoff_date
            )
        ).order_by(IdentityAppearance.identity_id, IdentityAppearance.start_time)
        
        result_2 = await db.execute(query_2)
        appearances_2 = result_2.scalars().all()
        
        # Group by identity
        appearances_by_identity_1 = defaultdict(list)
        for app in appearances_1:
            appearances_by_identity_1[app.identity_id].append(app)
        
        appearances_by_identity_2 = defaultdict(list)
        for app in appearances_2:
            appearances_by_identity_2[app.identity_id].append(app)
        
        # Find cross-camera movements
        movements = []
        for identity_id in set(appearances_by_identity_1.keys()) & set(appearances_by_identity_2.keys()):
            apps_1 = appearances_by_identity_1[identity_id]
            apps_2 = appearances_by_identity_2[identity_id]
            
            # Find movements: camera_1 -> camera_2
            for app_1 in apps_1:
                for app_2 in apps_2:
                    if app_2.start_time > app_1.start_time:
                        time_diff = (app_2.start_time - app_1.start_time).total_seconds() / 60.0  # minutes
                        if 0 < time_diff < 60:  # Within 1 hour (reasonable travel time)
                            movements.append({
                                'identity_id': identity_id,
                                'from_camera': camera_1,
                                'to_camera': camera_2,
                                'time_diff_minutes': time_diff,
                                'from_time': app_1.start_time,
                                'to_time': app_2.start_time
                            })
                            break  # Only count first movement per appearance
        
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
            getattr(settings, 'MULTI_CAMERA_TIME_WINDOW_MINUTES', 10),
            getattr(settings, 'MULTI_CAMERA_DISTANCE_METERS', 500.0)
        )
    
    async def learn_all_camera_pairs(
        self,
        db: AsyncSession,
        pipeline_ids: List[str]
    ) -> Dict[Tuple[str, str], Dict]:
        """
        Learn thresholds for all camera pairs in the network.
        """
        learned = {}
        
        # Get all pipelines with coordinates
        query = select(Pipeline).where(
            and_(
                Pipeline.pipeline_id.in_(pipeline_ids),
                Pipeline.latitude.isnot(None),
                Pipeline.longitude.isnot(None)
            )
        )
        result = await db.execute(query)
        pipelines_with_coords = [p.pipeline_id for p in result.scalars().all()]
        
        logger.info(f"[THRESHOLD_LEARNER] Learning thresholds for {len(pipelines_with_coords)} cameras")
        
        # Learn for each pair
        for i, camera_1 in enumerate(pipelines_with_coords):
            for camera_2 in pipelines_with_coords[i+1:]:
                learned_thresholds = await self.learn_thresholds_for_pair(db, camera_1, camera_2)
                if learned_thresholds:
                    learned[(camera_1, camera_2)] = learned_thresholds
        
        logger.info(f"[THRESHOLD_LEARNER] Learned thresholds for {len(learned)} camera pairs")
        return learned
    
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

