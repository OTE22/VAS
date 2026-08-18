"""
Trajectory Prediction
====================
Predicts where a person will appear next based on historical movement
patterns: a first-order Markov transition model built from EVERY adjacent
camera pair in every session.

The v1 model matched only sessions whose FIRST hop was the current camera and
read only the second camera of each session — a person routinely walking
A→B→C produced zero predictions when queried at B, and the B→C transition was
never learned at all. v2 learns the full transition matrix.
"""

import logging
import uuid as uuid_module
from typing import List, Dict, Tuple, Optional
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from db_models import IdentityAppearance, Pipeline
from config import settings

logger = logging.getLogger(__name__)

# Bounded history: enough to build a per-person transition model, small
# enough to never dominate a request.
TRAJECTORY_MAX_APPEARANCES = 2000


class TrajectoryPredictor:
    """
    First-order Markov predictor over camera transitions.

    P(next = Y | current = X) = count(X→Y transitions) / count(X→* transitions),
    learned from all adjacent pairs within sessions (a session breaks on a
    gap larger than `session_gap_hours`). Estimated arrival = current time +
    mean observed X→Y transit; when a transition has no observed times the
    walking-speed distance estimate is used as a clearly-labelled fallback.
    """

    def __init__(self):
        self.min_trajectories_for_prediction = 3
        self.session_gap_hours = 2.0

    @property
    def enabled(self) -> bool:
        """TRAJECTORY_PREDICTION_ENABLED was declared and offered on the
        settings page but gated nothing. Read per call (module singleton)."""
        return bool(settings.TRAJECTORY_PREDICTION_ENABLED)

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

        Returns:
            List of (camera_id, probability, estimated_time), sorted by
            probability. Empty ONLY means insufficient evidence — an
            infrastructure failure raises (the old blanket `except → []`
            made a database error render as "Insufficient Evidence").
        """
        trajectories = await self._get_historical_trajectories(db, identity_id)

        if len(trajectories) < self.min_trajectories_for_prediction:
            logger.debug(
                f"[TRAJECTORY] Insufficient trajectories for {identity_id}: "
                f"{len(trajectories)} (need {self.min_trajectories_for_prediction})"
            )
            return []

        # Learn transitions from EVERY adjacent pair in every session, then
        # condition on the current camera — wherever it occurs in a route.
        transition_counts = defaultdict(lambda: {'count': 0, 'times': []})
        for traj in trajectories:
            cameras = traj['cameras']
            diffs = traj['time_diffs']
            for i in range(len(cameras) - 1):
                if cameras[i] != current_camera:
                    continue
                next_camera = cameras[i + 1]
                if next_camera == current_camera:
                    continue  # self-loop (re-detection on the same camera)
                transition_counts[next_camera]['count'] += 1
                if i < len(diffs) and diffs[i] is not None and diffs[i] > 0:
                    transition_counts[next_camera]['times'].append(diffs[i])

        if not transition_counts:
            logger.debug(
                f"[TRAJECTORY] No observed transitions out of {current_camera} "
                f"for {identity_id}")
            return []

        total = sum(data['count'] for data in transition_counts.values())
        predictions = []

        for camera, data in transition_counts.items():
            probability = data['count'] / total

            if data['times']:
                avg_time_minutes = sum(data['times']) / len(data['times'])
            else:
                # Fallback: walking-speed distance estimate.
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

        # UUID bind — the column is UUID(as_uuid=True); binding the raw string
        # is driver-dependent behaviour.
        identity_uuid = uuid_module.UUID(str(identity_id))

        # NEWEST rows under the cap (ascending + LIMIT would keep the oldest
        # slice and learn a stale model), then chronological for the session
        # walk below.
        query = select(IdentityAppearance).where(
            and_(
                IdentityAppearance.identity_id == identity_uuid,
                IdentityAppearance.start_time >= cutoff_date
            )
        ).order_by(IdentityAppearance.start_time.desc()).limit(TRAJECTORY_MAX_APPEARANCES)

        result = await db.execute(query)
        appearances = list(result.scalars().all())
        appearances.sort(key=lambda a: a.start_time)

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
            # If gap is too large, start new trajectory
            if prev_appearance:
                gap = (app.start_time - prev_appearance.start_time).total_seconds() / 3600.0
                if gap > self.session_gap_hours:
                    if len(current_trajectory['cameras']) > 1:
                        trajectories.append(current_trajectory)
                    current_trajectory = {
                        'cameras': [],
                        'times': [],
                        'time_diffs': []
                    }
                    prev_appearance = None

            current_trajectory['cameras'].append(app.pipeline_id)
            current_trajectory['times'].append(app.start_time)

            if prev_appearance:
                time_diff = (app.start_time - prev_appearance.start_time).total_seconds() / 60.0
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
                return 10.0  # Default: 10 minutes (no coordinates to estimate from)

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
