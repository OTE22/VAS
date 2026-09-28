"""Worker-owned threshold learning. Candidates never activate automatically."""
import asyncio
from datetime import datetime
from typing import Dict
from sqlalchemy import select
from config import settings
from db_models import Pipeline
from backend.core.threshold_learner import ThresholdLearner

THRESHOLD_ALGORITHM_VERSION = "threshold-v3"
THRESHOLD_JOB_TIMEOUT_SECONDS = 900

async def persist_threshold_candidates(db, learned: dict) -> int:
    """Persist learning output as CANDIDATE learned_thresholds rows —
    global + per-pipeline scopes, activation strictly manual (an admin
    reviews and activates via /api/security/learned-thresholds)."""
    from backend.core.threshold_store import (
        threshold_store, SIGNAL_DISTANCE, SIGNAL_TIME_WINDOW)
    if not learned:
        return 0
    per_pipeline: Dict[str, Dict[str, list]] = {}
    all_windows, all_distances, total_samples = [], [], 0
    for (cam1, cam2), data in learned.items():
        window = float(data.get("optimal_time_window_minutes") or 0)
        distance = float(data.get("optimal_distance_meters") or 0)
        samples = int(data.get("sample_count") or 0)
        all_windows.append(window)
        all_distances.append(distance)
        total_samples += samples
        for cam in (cam1, cam2):
            bucket = per_pipeline.setdefault(cam, {"windows": [], "distances": [], "samples": 0, "pairs": []})
            bucket["windows"].append(window)
            bucket["distances"].append(distance)
            bucket["samples"] += samples
            bucket["pairs"].append({"pair": [cam1, cam2], "window": window,
                                    "distance": distance, "samples": samples})
    written = 0
    # Global candidates: the max over learned routes (covers the slowest one).
    await threshold_store.record_candidate(
        db, scope_type="global", scope_id="", signal_name=SIGNAL_TIME_WINDOW,
        value=max(all_windows), sample_count=total_samples,
        extras={"aggregation": "max_over_pairs", "pairs": len(learned)})
    await threshold_store.record_candidate(
        db, scope_type="global", scope_id="", signal_name=SIGNAL_DISTANCE,
        value=max(all_distances), sample_count=total_samples,
        extras={"aggregation": "max_over_pairs", "pairs": len(learned)})
    written += 2
    for cam, bucket in per_pipeline.items():
        await threshold_store.record_candidate(
            db, scope_type="pipeline", scope_id=cam, signal_name=SIGNAL_TIME_WINDOW,
            value=max(bucket["windows"]), sample_count=bucket["samples"],
            extras={"aggregation": "max_over_pairs", "pairs": bucket["pairs"][:20]})
        written += 1
    await db.commit()
    return written



async def run_threshold_learning(db, pipeline_ids, progress=None):
    if not settings.AUTO_THRESHOLD_LEARNING_ENABLED:
        raise RuntimeError("Threshold learning is disabled")
    learner = ThresholdLearner()
    async with asyncio.timeout(THRESHOLD_JOB_TIMEOUT_SECONDS):
        if not pipeline_ids:
            rows = await db.execute(select(Pipeline.pipeline_id).where(Pipeline.is_active == 1))
            pipeline_ids = list(rows.scalars().all())
        if len(pipeline_ids) > 100:
            raise ValueError("At most 100 cameras can be learned in one job; select a subset")
        learned = await learner.learn_all_camera_pairs(db, pipeline_ids, progress=progress)
        written = await persist_threshold_candidates(db, learned)
    thresholds = [dict(camera_1=pair[0], camera_2=pair[1], **{
        k: data.get(k) for k in ("optimal_time_window_minutes", "optimal_distance_meters",
          "actual_distance_meters", "confidence", "sample_count", "p95_minutes", "spread_minutes")
    }) for pair, data in learned.items()]
    return {
        "outcome": "candidates_saved" if written else "insufficient_evidence",
        "learned_pairs": len(thresholds), "thresholds": thresholds[:200],
        "thresholds_truncated": len(thresholds) > 200,
        "algorithm_version": THRESHOLD_ALGORITHM_VERSION,
        "calculated_at": datetime.utcnow().isoformat() + "Z",
        "pipelines_scoped": len(pipeline_ids), "candidates_written": written,
        "history_truncated": learner.history_truncated,
        "activation_required": bool(written),
        "activation_note": "Saved candidates require administrator review and activation before analysis uses them.",
    }
