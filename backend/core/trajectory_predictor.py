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


class TrajectoryHistory(list):
    """Chronological sessions plus bounded-query provenance."""
    truncated = False


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

    async def predict_next_cameras(self, db, identity_id, current_camera, current_time, top_k=3):
        """Compatibility interface; evidence-aware API uses predict_with_evidence."""
        result = await self.predict_with_evidence(db, identity_id, current_camera, current_time, top_k)
        return [(p["camera_id"], p["probability"], p["estimated_time"]) for p in result["predictions"]]

    async def predict_with_evidence(self, db, identity_id, current_camera, current_time, top_k=3):
        trajectories = await self._get_historical_trajectories(db, identity_id)
        counts = defaultdict(lambda: {"count": 0, "times": []})
        supporting_sessions = 0
        for trajectory in trajectories:
            contributed = False
            for i, (start, end) in enumerate(zip(trajectory["cameras"], trajectory["cameras"][1:])):
                if start != current_camera or start == end:
                    continue
                times = trajectory["time_diffs"]
                if i >= len(times) or times[i] is None or times[i] <= 0:
                    continue  # simultaneous detections do not establish direction
                counts[end]["count"] += 1
                counts[end]["times"].append(times[i])
                contributed = True
            supporting_sessions += int(contributed)
        total = sum(item["count"] for item in counts.values())
        result = {"predictions": [], "total_transitions": total,
                  "supporting_sessions": supporting_sessions,
                  "minimum_supporting_sessions": self.min_trajectories_for_prediction,
                  "calibration_status": "uncalibrated",
                  "history_limit": TRAJECTORY_MAX_APPEARANCES,
                  "history_days": 90, "history_truncated": bool(getattr(trajectories, "truncated", False))}
        if supporting_sessions < self.min_trajectories_for_prediction:
            return result
        for camera, data in counts.items():
            result["predictions"].append({"camera_id": camera,
                "probability": data["count"] / total, "transition_count": data["count"],
                "estimated_time": current_time + timedelta(minutes=sum(data["times"]) / len(data["times"]))})
        result["predictions"].sort(key=lambda item: (-item["probability"], item["camera_id"]))
        result["predictions"] = result["predictions"][:top_k]
        return result

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
        ).order_by(IdentityAppearance.start_time.desc()).limit(TRAJECTORY_MAX_APPEARANCES + 1)

        result = await db.execute(query)
        appearances = list(result.scalars().all())
        history_truncated = len(appearances) > TRAJECTORY_MAX_APPEARANCES
        appearances = appearances[:TRAJECTORY_MAX_APPEARANCES]
        appearances.sort(key=lambda a: a.start_time)

        if len(appearances) < 2:
            return []

        # Build trajectories (sequences of cameras)
        trajectories = TrajectoryHistory()
        trajectories.truncated = history_truncated
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

            if not p1 or not p2 or p1.latitude is None or p1.longitude is None or p2.latitude is None or p2.longitude is None:
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
