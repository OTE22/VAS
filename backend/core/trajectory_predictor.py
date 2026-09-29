"""First-order next-camera frequencies from saved, as-of-bounded sightings.

Sessions split on long gaps and ambiguous simultaneous camera observations.
Sighting gaps estimate arrival timing; no pixel motion or zones are inferred.
"""

import uuid as uuid_module
from typing import List, Dict
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import groupby

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from db_models import IdentityAppearance
from config import settings

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
    mean observed X→Y sighting gap. This is not measured travel time or a
    coordinate trajectory; no distance-based arrival times are fabricated.
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
        current_time = utc_naive(current_time)
        trajectories = await self._get_historical_trajectories(db, identity_id, as_of=current_time)
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
                  "history_as_of": current_time, "history_days": 90, "history_truncated": bool(getattr(trajectories, "truncated", False))}
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
        self, db: AsyncSession, identity_id: str, days_back: int = 90,
        *, as_of: datetime = None,
    ) -> List[Dict]:
        """Bounded history known strictly before the prediction anchor.

        created_at excludes late arrivals unavailable at that historical anchor.
        Identity ownership is current; this does not reconstruct pre-merge history.
        """
        as_of = utc_naive(as_of or datetime.utcnow())
        cutoff_date = as_of - timedelta(days=days_back)
        query = select(IdentityAppearance).where(and_(
            IdentityAppearance.identity_id == uuid_module.UUID(str(identity_id)),
            IdentityAppearance.start_time >= cutoff_date,
            IdentityAppearance.start_time < as_of,
            IdentityAppearance.created_at < as_of,
        )).order_by(IdentityAppearance.start_time.desc(), IdentityAppearance.id.desc()).limit(
            TRAJECTORY_MAX_APPEARANCES + 1)
        result = await db.execute(query)
        appearances = list(result.scalars().all())
        truncated = len(appearances) > TRAJECTORY_MAX_APPEARANCES
        appearances = appearances[:TRAJECTORY_MAX_APPEARANCES]
        # A tied group split by the cap cannot establish direction either.
        if truncated and appearances:
            boundary = appearances[-1].start_time
            appearances = [a for a in appearances if a.start_time > boundary]
        history = camera_sessions(appearances, self.session_gap_hours)
        history.truncated = truncated
        return history


def utc_naive(value):
    """Stored timestamps use naive UTC; accept aware API anchors safely."""
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def camera_sessions(appearances, session_gap_hours=2.0):
    """Deterministic observed sightings, with source IDs and honest tie handling.

    One identity per call. Exact camera/time duplicates select the lowest source
    ID. Simultaneous different-camera observations break both sides of a session;
    sorting camera IDs must never invent a direction through an ambiguous group.
    """
    history = TrajectoryHistory()
    current = None
    def finish():
        if current and len(current['cameras']) > 1:
            history.append(current)
    ordered = sorted(appearances, key=lambda a: (a.start_time, a.id))
    for timestamp, group in groupby(ordered, key=lambda a: a.start_time):
        tied = list(group)
        if len({a.pipeline_id for a in tied}) != 1:
            finish()
            current = None
            continue
        app = tied[0]
        if current and (timestamp - current['times'][-1]).total_seconds() > session_gap_hours * 3600:
            finish()
            current = None
        if current is None:
            current = {'cameras': [], 'times': [], 'time_diffs': [], 'appearance_ids': [], 'available_at': []}
        if current['times']:
            current['time_diffs'].append((timestamp - current['times'][-1]).total_seconds() / 60)
        current['cameras'].append(app.pipeline_id)
        current['times'].append(timestamp)
        current['appearance_ids'].append(app.id)
        current['available_at'].append(max(timestamp, app.created_at))
    finish()
    return history


def observed_camera_examples(appearances, session_gap_hours=2.0):
    """Inspection examples only: inputs known at the anchor, future target separate.

    No imputation, model fitting, label persistence or synthetic next zone. Split
    examples by time/entity before fitting anything; adjacent examples overlap.
    """
    examples = []
    for session in camera_sessions(appearances, session_gap_hours):
        for i in range(len(session['cameras']) - 1):
            if session['cameras'][i] == session['cameras'][i + 1]:
                continue
            anchor = session['available_at'][i]
            target_time = session['times'][i + 1]
            if target_time <= anchor:
                continue  # target already occurred before this input became available
            examples.append({
                'input_appearance_ids': [session['appearance_ids'][j] for j in range(i + 1)
                                         if session['available_at'][j] <= anchor],
                'anchor_appearance_id': session['appearance_ids'][i],
                'input_camera': session['cameras'][i], 'anchor_time': anchor,
                'target_appearance_id': session['appearance_ids'][i + 1],
                'target_camera': session['cameras'][i + 1], 'target_time': target_time,
                'horizon_seconds': (target_time - anchor).total_seconds(),
            })
    return examples


# Global instance
trajectory_predictor = TrajectoryPredictor()
