"""
Trajectory Prediction
====================
Predicts where a person will appear next based on historical movement patterns.
"""

import logging
from typing import List, Dict, Tuple, Optional
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from db_models import IdentityAppearance, Pipeline
from config import settings

logger = logging.getLogger(__name__)


class TrajectoryPredictor:
    """
    Predicts next camera location based on historical trajectories.
    """
    
    def __init__(self):
        self.trajectory_cache: Dict[str, List[Dict]] = {}
        self.min_trajectories_for_prediction = 3
    
    async def predict_next_cameras(
        self,
        db: AsyncSession,
        identity_id: str,
        current_camera: str,
        current_time: datetime,
        top_k: int = 3
    ) -> List[Tuple[str, float, datetime]]:
        """
        Predict which cameras the person will appear at next.
        
        Args:
            identity_id: Identity UUID
            current_camera: Current camera/pipeline ID
            current_time: Current timestamp
            top_k: Number of predictions to return
            
        Returns:
            List of (camera_id, probability, estimated_time) tuples, sorted by probability
        """
        try:
            # Get historical trajectories for this identity
            trajectories = await self._get_historical_trajectories(db, identity_id)
            
            if len(trajectories) < self.min_trajectories_for_prediction:
                logger.debug(
                    f"[TRAJECTORY] Insufficient trajectories for {identity_id}: "
                    f"{len(trajectories)} (need {self.min_trajectories_for_prediction})"
                )
                return []
            
            # Find trajectories that start at current camera
            matching_trajectories = [
                t for t in trajectories
                if len(t['cameras']) > 0 and t['cameras'][0] == current_camera
            ]
            
            if not matching_trajectories:
                logger.debug(f"[TRAJECTORY] No trajectories starting at {current_camera} for {identity_id}")
                return []
            
            # Count next cameras
            next_camera_counts = defaultdict(lambda: {'count': 0, 'times': []})
            for traj in matching_trajectories:
                if len(traj['cameras']) > 1:
                    next_camera = traj['cameras'][1]
                    time_diff = traj['time_diffs'][0] if traj['time_diffs'] else None
                    next_camera_counts[next_camera]['count'] += 1
                    if time_diff:
                        next_camera_counts[next_camera]['times'].append(time_diff)
            
            if not next_camera_counts:
                return []
            
            # Calculate probabilities and estimated times
            total = sum(count['count'] for count in next_camera_counts.values())
            predictions = []
            
            for camera, data in next_camera_counts.items():
                probability = data['count'] / total
                
                # Calculate average travel time
                if data['times']:
                    avg_time_minutes = sum(data['times']) / len(data['times'])
                else:
                    # Fallback: estimate based on distance
                    avg_time_minutes = await self._estimate_travel_time(db, current_camera, camera)
                
                estimated_time = current_time + timedelta(minutes=avg_time_minutes)
                
                predictions.append((camera, probability, estimated_time))
            
            # Sort by probability (highest first)
            predictions.sort(key=lambda x: x[1], reverse=True)
            
            logger.debug(
                f"[TRAJECTORY] Predicted {len(predictions)} next cameras for {identity_id} "
                f"from {current_camera}: {[(c, f'{p:.2f}') for c, p, _ in predictions[:top_k]]}"
            )
            
            return predictions[:top_k]
            
        except Exception as e:
            logger.error(f"[TRAJECTORY] Error predicting next cameras: {e}", exc_info=True)
            return []
    
    async def _get_historical_trajectories(
        self,
        db: AsyncSession,
        identity_id: str,
        days_back: int = 90
    ) -> List[Dict]:
        """
        Get historical trajectories for an identity.
        A trajectory is a sequence of cameras visited in order.
        """
        cutoff_date = datetime.utcnow() - timedelta(days=days_back)
        
        # Get all appearances ordered by time
        query = select(IdentityAppearance).where(
            and_(
                IdentityAppearance.identity_id == identity_id,
                IdentityAppearance.start_time >= cutoff_date
            )
        ).order_by(IdentityAppearance.start_time)
        
        result = await db.execute(query)
        appearances = result.scalars().all()
        
        if len(appearances) < 2:
            return []
        
        # Build trajectories (sequences of cameras)
        trajectories = []
        current_trajectory = {
            'cameras': [],
            'times': [],
            'time_diffs': []
        }
        
        prev_appearance = None
        for app in appearances:
            # If gap is too large (> 2 hours), start new trajectory
            if prev_appearance:
                gap = (app.start_time - prev_appearance.start_time).total_seconds() / 3600.0
                if gap > 2.0:  # 2 hours gap
                    if len(current_trajectory['cameras']) > 1:
                        trajectories.append(current_trajectory)
                    current_trajectory = {
                        'cameras': [],
                        'times': [],
                        'time_diffs': []
                    }
            
            current_trajectory['cameras'].append(app.pipeline_id)
            current_trajectory['times'].append(app.start_time)
            
            if prev_appearance:
                time_diff = (app.start_time - prev_appearance.start_time).total_seconds() / 60.0  # minutes
                current_trajectory['time_diffs'].append(time_diff)
            
            prev_appearance = app
        
        # Add last trajectory
        if len(current_trajectory['cameras']) > 1:
            trajectories.append(current_trajectory)
        
        return trajectories
    
    async def _estimate_travel_time(
        self,
        db: AsyncSession,
        camera_1: str,
        camera_2: str
    ) -> float:
        """
        Estimate travel time between two cameras based on distance.
        Assumes average walking speed of 5 km/h (83 m/min).
        """
        try:
            # Get pipeline coordinates
            query = select(Pipeline).where(
                Pipeline.pipeline_id.in_([camera_1, camera_2])
            )
            result = await db.execute(query)
            pipelines = {p.pipeline_id: p for p in result.scalars().all()}
            
            p1 = pipelines.get(camera_1)
            p2 = pipelines.get(camera_2)
            
            if not p1 or not p2 or not p1.latitude or not p1.longitude or not p2.latitude or not p2.longitude:
                return 10.0  # Default: 10 minutes
            
            # Calculate distance
            distance = self._calculate_distance_meters(
                p1.latitude, p1.longitude,
                p2.latitude, p2.longitude
            )
            
            # Estimate time (walking speed: 83 m/min = 5 km/h)
            estimated_minutes = distance / 83.0
            
            # Cap at reasonable maximum (30 minutes)
            return min(estimated_minutes, 30.0)
            
        except Exception as e:
            logger.warning(f"[TRAJECTORY] Error estimating travel time: {e}")
            return 10.0  # Default fallback
    
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
trajectory_predictor = TrajectoryPredictor()

