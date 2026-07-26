"""
Activity Correlation Analysis (xCCA)
=====================================
Detects causal relationships between activities at different cameras.
"""

import logging
from typing import List, Dict, Tuple, Optional
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_
from db_models import IdentityAppearance, Pipeline
from config import settings

logger = logging.getLogger(__name__)


class ActivityCorrelationAnalyzer:
    """
    Analyzes correlations between activities at different cameras.
    """
    
    def __init__(self):
        self.correlation_cache: Dict[Tuple[str, str], float] = {}
        self.min_sequences_for_correlation = 3
    
    async def calculate_correlation(
        self,
        db: AsyncSession,
        identity_a: str,
        identity_b: str,
        days_back: int = 90
    ) -> Tuple[float, List[Dict]]:
        """
        Calculate correlation between two identities' activities.
        
        Returns:
            (correlation_score, sequence_patterns)
            - correlation_score: 0.0 to 1.0 (1.0 = perfect correlation)
            - sequence_patterns: List of detected sequences
        """
        try:
            cutoff_date = datetime.utcnow() - timedelta(days=days_back)
            
            # Get all appearances for both identities
            query_a = select(IdentityAppearance).where(
                and_(
                    IdentityAppearance.identity_id == identity_a,
                    IdentityAppearance.start_time >= cutoff_date
                )
            ).order_by(IdentityAppearance.start_time)
            
            query_b = select(IdentityAppearance).where(
                and_(
                    IdentityAppearance.identity_id == identity_b,
                    IdentityAppearance.start_time >= cutoff_date
                )
            ).order_by(IdentityAppearance.start_time)
            
            result_a = await db.execute(query_a)
            appearances_a = result_a.scalars().all()
            
            result_b = await db.execute(query_b)
            appearances_b = result_b.scalars().all()
            
            if not appearances_a or not appearances_b:
                return 0.0, []
            
            # Get pipeline coordinates for distance calculation
            all_pipeline_ids = set()
            for app in appearances_a + appearances_b:
                all_pipeline_ids.add(app.pipeline_id)
            
            pipeline_coords = await self._get_pipeline_coordinates(db, list(all_pipeline_ids))
            
            # Find sequences: A appears at Camera X → B appears at Camera Y
            sequences = []
            
            for app_a in appearances_a:
                nearby_cameras = self._get_nearby_cameras(
                    app_a.pipeline_id, pipeline_coords,
                    max_distance=getattr(settings, 'MULTI_CAMERA_DISTANCE_METERS', 500.0)
                )
                
                for app_b in appearances_b:
                    if app_b.pipeline_id in nearby_cameras:
                        time_diff = (app_b.start_time - app_a.start_time).total_seconds() / 60.0  # minutes
                        
                        # B appears after A within time window
                        max_time_window = getattr(settings, 'MULTI_CAMERA_TIME_WINDOW_MINUTES', 10)
                        if 0 < time_diff < max_time_window:
                            sequences.append({
                                'from_camera': app_a.pipeline_id,
                                'to_camera': app_b.pipeline_id,
                                'time_diff_minutes': time_diff,
                                'from_time': app_a.start_time,
                                'to_time': app_b.start_time
                            })
            
            if len(sequences) < self.min_sequences_for_correlation:
                logger.debug(
                    f"[ACTIVITY_CORR] Insufficient sequences for correlation: "
                    f"{identity_a} <-> {identity_b}: {len(sequences)} sequences"
                )
                return 0.0, sequences
            
            # Calculate correlation score
            # Correlation = (number of sequences) / (max appearances of either identity)
            max_appearances = max(len(appearances_a), len(appearances_b))
            correlation_score = min(len(sequences) / max_appearances, 1.0)
            
            # Boost score if sequences show consistent pattern
            if len(sequences) > 0:
                # Check if sequences follow similar patterns (same camera pairs, similar times)
                pattern_consistency = self._calculate_pattern_consistency(sequences)
                correlation_score = correlation_score * (0.7 + 0.3 * pattern_consistency)  # Weighted
            
            logger.debug(
                f"[ACTIVITY_CORR] Correlation {identity_a} <-> {identity_b}: "
                f"score={correlation_score:.3f}, sequences={len(sequences)}"
            )
            
            return correlation_score, sequences
            
        except Exception as e:
            logger.error(f"[ACTIVITY_CORR] Error calculating correlation: {e}", exc_info=True)
            return 0.0, []
    
    def _calculate_pattern_consistency(self, sequences: List[Dict]) -> float:
        """
        Calculate how consistent the sequences are (same camera pairs, similar times).
        Returns 0.0 to 1.0 (1.0 = perfectly consistent).
        """
        if len(sequences) < 2:
            return 1.0
        
        # Group by camera pair
        camera_pair_counts = defaultdict(list)
        for seq in sequences:
            pair = (seq['from_camera'], seq['to_camera'])
            camera_pair_counts[pair].append(seq['time_diff_minutes'])
        
        # Calculate consistency: how often same camera pair appears
        max_count = max(len(times) for times in camera_pair_counts.values())
        consistency = max_count / len(sequences)
        
        # Also consider time consistency (similar travel times)
        time_consistency = 1.0
        for times in camera_pair_counts.values():
            if len(times) > 1:
                avg_time = sum(times) / len(times)
                variance = sum((t - avg_time) ** 2 for t in times) / len(times)
                # Lower variance = higher consistency
                time_consistency = min(time_consistency, 1.0 / (1.0 + variance))
        
        return (consistency + time_consistency) / 2.0
    
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

