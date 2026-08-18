"""
Point-in-time feature builders.

Every builder sees ONLY source rows with ``start_time < as_of`` — the
context loader enforces the cutoff once, so no builder can leak the future
by construction. Timezone-sensitive features convert through each
pipeline's IANA timezone (fallback DEFAULT_SITE_TIMEZONE) via
backend.core.time_context; storage stays UTC.

Unavailability is honest: a builder that cannot answer raises
FeatureUnavailable(reason) and the snapshot records the reason — never a
misleading zero. Graph/pair features additionally pass a readiness gate
(minimum nodes/edges/observation span/pair appearances) before computing.

PRIVACY: builders never read identity_embeddings, face embeddings, or any
biometric payload — behavioral aggregates only (test-pinned).
"""

import logging
import math
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select, and_, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession
from config import settings

logger = logging.getLogger(__name__)

PERSON_LOOKBACK_DAYS = 90


class FeatureUnavailable(Exception):
    """Raised by a builder when the feature cannot be computed honestly."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Context loading (ONE bounded query set per entity per as_of)
# ---------------------------------------------------------------------------

async def load_person_context(db: AsyncSession, identity_id, as_of: datetime) -> Dict[str, Any]:
    """Everything person-level builders need, cutoff-enforced ONCE here."""
    from db_models import Identity, IdentityAppearance, Pipeline
    import uuid as uuid_mod

    identity_uuid = identity_id if not isinstance(identity_id, str) else uuid_mod.UUID(identity_id)
    floor = as_of - timedelta(days=PERSON_LOOKBACK_DAYS)

    rows = list((await db.execute(
        select(IdentityAppearance.pipeline_id, IdentityAppearance.start_time,
               IdentityAppearance.end_time)
        .where(and_(
            IdentityAppearance.identity_id == identity_uuid,
            IdentityAppearance.start_time < as_of,
            IdentityAppearance.start_time >= floor,
        ))
        .order_by(IdentityAppearance.start_time)
        .limit(5000)
    )).all())

    # first-seen needs ALL history, not the 90d window — one aggregate.
    first_seen = (await db.execute(
        select(sa_func.min(IdentityAppearance.start_time))
        .where(and_(IdentityAppearance.identity_id == identity_uuid,
                    IdentityAppearance.start_time < as_of))
    )).scalar()

    identity = (await db.execute(
        select(Identity).where(Identity.id == identity_uuid))).scalar_one_or_none()

    tz_rows = await db.execute(select(Pipeline.pipeline_id, Pipeline.timezone))
    pipe_timezones = {r[0]: r[1] for r in tz_rows}

    from backend.core.time_context import parse_holidays, parse_weekend_days
    return {
        "entity_type": "person",
        "identity": identity,
        "as_of": as_of,
        "rows": rows,                      # (pipeline_id, start_time, end_time), all < as_of
        "first_seen": first_seen,
        "pipe_timezones": pipe_timezones,
        "weekend_days": parse_weekend_days(),
        "holidays": parse_holidays(),
        "source_row_counts": {"identity_appearances": len(rows)},
    }


def _window_rows(ctx: Dict[str, Any], days: int) -> List[Tuple]:
    floor = ctx["as_of"] - timedelta(days=days)
    return [r for r in ctx["rows"] if r[1] >= floor]


def _local(ctx: Dict[str, Any], pipeline_id: str, ts: datetime):
    from backend.core.time_context import to_local
    return to_local(ts, ctx["pipe_timezones"].get(pipeline_id))


# ---------------------------------------------------------------------------
# Person-level builders (pure over ctx; params from the feature definition)
# ---------------------------------------------------------------------------

def _b_appearance_count(ctx, params):
    return float(len(_window_rows(ctx, int(params.get("days", 30)))))


def _b_distinct_pipelines(ctx, params):
    return float(len({r[0] for r in _window_rows(ctx, int(params.get("days", 30)))}))


def _b_days_since_first_seen(ctx, params):
    if ctx["first_seen"] is None:
        raise FeatureUnavailable("no_history_before_as_of")
    return max(0.0, (ctx["as_of"] - ctx["first_seen"]).total_seconds() / 86400.0)


def _b_days_since_last_seen(ctx, params):
    if not ctx["rows"]:
        raise FeatureUnavailable("no_history_before_as_of")
    return max(0.0, (ctx["as_of"] - ctx["rows"][-1][1]).total_seconds() / 86400.0)


def _b_active_days_ratio(ctx, params):
    days = int(params.get("days", 30))
    rows = _window_rows(ctx, days)
    if not rows:
        raise FeatureUnavailable("no_rows_in_window")
    return len({r[1].date() for r in rows}) / float(days)


def _off_hours_window():
    return (int(settings.PATTERN_OFF_HOURS_START),
            int(settings.PATTERN_OFF_HOURS_END))


def _b_off_hours_ratio(ctx, params):
    rows = _window_rows(ctx, int(params.get("days", 30)))
    if not rows:
        raise FeatureUnavailable("no_rows_in_window")
    start, end = _off_hours_window()

    def in_window(hour: int) -> bool:
        if start <= end:
            return start <= hour <= end
        return hour >= start or hour <= end

    off = sum(1 for r in rows if in_window(_local(ctx, r[0], r[1]).hour))
    return off / len(rows)


def _b_night_count(ctx, params):
    rows = _window_rows(ctx, int(params.get("days", 30)))
    return float(sum(1 for r in rows if 0 <= _local(ctx, r[0], r[1]).hour < 5))


def _b_weekend_holiday_ratio(ctx, params):
    from backend.core.time_context import day_bucket
    rows = _window_rows(ctx, int(params.get("days", 30)))
    if not rows:
        raise FeatureUnavailable("no_rows_in_window")
    non_workday = sum(
        1 for r in rows
        if day_bucket(_local(ctx, r[0], r[1]), ctx["weekend_days"], ctx["holidays"]) != "workday")
    return non_workday / len(rows)


def _local_hours(ctx, days):
    rows = _window_rows(ctx, days)
    return [
        _local(ctx, r[0], r[1]).hour + _local(ctx, r[0], r[1]).minute / 60.0
        for r in rows
    ]


def _b_hour_sin(ctx, params):
    from backend.core.security_intelligence_service import circular_hour_stats
    hours = _local_hours(ctx, int(params.get("days", 30)))
    if len(hours) < 3:
        raise FeatureUnavailable("fewer_than_3_samples")
    mean, _std = circular_hour_stats(hours)
    return math.sin(2 * math.pi * mean / 24.0)


def _b_hour_cos(ctx, params):
    from backend.core.security_intelligence_service import circular_hour_stats
    hours = _local_hours(ctx, int(params.get("days", 30)))
    if len(hours) < 3:
        raise FeatureUnavailable("fewer_than_3_samples")
    mean, _std = circular_hour_stats(hours)
    return math.cos(2 * math.pi * mean / 24.0)


def _b_hour_std(ctx, params):
    from backend.core.security_intelligence_service import circular_hour_stats
    hours = _local_hours(ctx, int(params.get("days", 30)))
    if len(hours) < 3:
        raise FeatureUnavailable("fewer_than_3_samples")
    _mean, std = circular_hour_stats(hours)
    if not math.isfinite(std):
        raise FeatureUnavailable("degenerate_distribution")
    return std


def _b_baseline_hour_deviation_last(ctx, params):
    """Deviation ratio of the LATEST pre-as_of appearance against the
    bucketed baseline built from everything before it."""
    from backend.core.time_context import BucketedHourBaseline, day_bucket

    rows = ctx["rows"]
    if len(rows) < 4:
        raise FeatureUnavailable("insufficient_history")
    *history, last = rows
    baseline = BucketedHourBaseline(
        min_bucket_samples=int(settings.ANOMALY_MIN_BASELINE_SAMPLES),
        std_floor=float(settings.ANOMALY_MIN_STD_HOURS),
        k_sigma=float(settings.ANOMALY_DEVIATION_SIGMA))
    for r in history:
        local_dt = _local(ctx, r[0], r[1])
        baseline.add(local_dt.hour + local_dt.minute / 60.0,
                     day_bucket(local_dt, ctx["weekend_days"], ctx["holidays"]))
    last_local = _local(ctx, last[0], last[1])
    evaluation = baseline.evaluate(
        last_local.hour + last_local.minute / 60.0,
        day_bucket(last_local, ctx["weekend_days"], ctx["holidays"]))
    if evaluation.insufficient_data:
        raise FeatureUnavailable("insufficient_baseline")
    ratio = evaluation.deviation_ratio
    return min(10.0, ratio) if math.isfinite(ratio) else 10.0


def _b_max_hourly_burst(ctx, params):
    rows = _window_rows(ctx, int(params.get("days", 30)))
    if not rows:
        raise FeatureUnavailable("no_rows_in_window")
    buckets = Counter(r[1].replace(minute=0, second=0, microsecond=0) for r in rows)
    return float(max(buckets.values()))


def _b_new_pipeline_flag(ctx, params):
    recent_days = int(params.get("recent_days", 7))
    rows = ctx["rows"]
    recent_floor = ctx["as_of"] - timedelta(days=recent_days)
    recent_pipes = {r[0] for r in rows if r[1] >= recent_floor}
    if not recent_pipes:
        raise FeatureUnavailable("no_recent_rows")
    earlier_pipes = {r[0] for r in rows if r[1] < recent_floor}
    if not earlier_pipes:
        raise FeatureUnavailable("no_earlier_history_to_compare")
    return 1.0 if (recent_pipes - earlier_pipes) else 0.0


def _b_is_unknown_identity(ctx, params):
    identity = ctx.get("identity")
    if identity is None:
        raise FeatureUnavailable("identity_row_missing")
    # NOTE: identity type is mutable — this reflects compute time, a known
    # limitation recorded in the feature description.
    return 1.0 if str(getattr(identity.type, "value", identity.type)).lower() == "unknown" else 0.0


BUILDERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], float]] = {
    "appearance_count": _b_appearance_count,
    "distinct_pipelines": _b_distinct_pipelines,
    "days_since_first_seen": _b_days_since_first_seen,
    "days_since_last_seen": _b_days_since_last_seen,
    "active_days_ratio": _b_active_days_ratio,
    "off_hours_ratio": _b_off_hours_ratio,
    "night_count": _b_night_count,
    "weekend_holiday_ratio": _b_weekend_holiday_ratio,
    "hour_sin": _b_hour_sin,
    "hour_cos": _b_hour_cos,
    "hour_std": _b_hour_std,
    "baseline_hour_deviation_last": _b_baseline_hour_deviation_last,
    "max_hourly_burst": _b_max_hourly_burst,
    "new_pipeline_flag": _b_new_pipeline_flag,
    "is_unknown_identity": _b_is_unknown_identity,
}


# ---------------------------------------------------------------------------
# Graph / pair readiness gate (approval-binding: unmet gates => unavailable
# with a recorded reason, never zeros; the rules engine is never affected)
# ---------------------------------------------------------------------------

async def graph_readiness(db: AsyncSession, as_of: datetime) -> Tuple[bool, List[str], Dict[str, Any]]:
    """Check the relationship graph against the configured floors."""
    from db_models import IdentityRelationship

    stats_row = (await db.execute(
        select(
            sa_func.count(IdentityRelationship.id),
            sa_func.count(sa_func.distinct(IdentityRelationship.identity_id_1)),
            sa_func.min(IdentityRelationship.first_co_appearance),
            sa_func.max(IdentityRelationship.last_co_appearance),
        ).where(IdentityRelationship.calculated_at < as_of)
    )).one()
    edge_count = int(stats_row[0] or 0)
    # distinct endpoint estimate (id1 side; cheap lower bound)
    node_lower_bound = int(stats_row[1] or 0)
    span_days = 0.0
    if stats_row[2] and stats_row[3]:
        span_days = (stats_row[3] - stats_row[2]).total_seconds() / 86400.0

    reasons = []
    min_nodes = int(settings.ML_GRAPH_MIN_NODES)
    min_edges = int(settings.ML_GRAPH_MIN_EDGES)
    min_days = int(settings.ML_GRAPH_MIN_OBSERVATION_DAYS)
    if edge_count < min_edges:
        reasons.append(f"graph_below_min_edges({edge_count}<{min_edges})")
    if node_lower_bound < min_nodes:
        reasons.append(f"graph_below_min_nodes({node_lower_bound}<{min_nodes})")
    if span_days < min_days:
        reasons.append(f"graph_observation_span_too_short({span_days:.1f}d<{min_days}d)")
    ready = not reasons
    return ready, reasons, {"edges": edge_count, "node_lower_bound": node_lower_bound,
                            "span_days": round(span_days, 1)}


async def build_pair_co_appearance_count(db: AsyncSession, id1: str, id2: str,
                                         as_of: datetime, params: Dict) -> float:
    """Pair feature with its own readiness floor on pair appearances."""
    from db_models import IdentityAppearance
    import uuid as uuid_mod

    days = int(params.get("days", 30))
    window_min = int(settings.RELATED_IDENTITY_TIME_WINDOW_MINUTES)
    floor = as_of - timedelta(days=days)
    a = IdentityAppearance.__table__.alias("a")
    b = IdentityAppearance.__table__.alias("b")
    count = (await db.execute(
        select(sa_func.count())
        .select_from(a.join(b, and_(
            a.c.pipeline_id == b.c.pipeline_id,
            b.c.identity_id == uuid_mod.UUID(str(id2)),
            b.c.start_time < as_of,
            b.c.start_time.between(
                a.c.start_time - timedelta(minutes=window_min),
                a.c.start_time + timedelta(minutes=window_min)),
        )))
        .where(and_(
            a.c.identity_id == uuid_mod.UUID(str(id1)),
            a.c.start_time < as_of,
            a.c.start_time >= floor,
        ))
    )).scalar() or 0
    min_pair = int(settings.ML_GRAPH_MIN_PAIR_APPEARANCES)
    if count < min_pair:
        raise FeatureUnavailable(f"pair_below_min_appearances({count}<{min_pair})")
    return float(count)
