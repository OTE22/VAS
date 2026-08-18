"""
Timezone-aware time context for behavioral analytics
====================================================
Every timestamp in the database is naive UTC (unchanged). BUSINESS-HOUR
questions ("is 03:00 unusual?", "is this off-hours?") are answered in the
camera's LOCAL time: each pipeline can carry an IANA timezone
(``pipelines.timezone``, e.g. ``Asia/Beirut``); when unset, the documented
fallback is ``DEFAULT_SITE_TIMEZONE`` (config, default UTC).

zoneinfo (stdlib) does the conversions, so daylight-saving transitions are
handled by the timezone database, never by fixed numeric offsets.

Also home to the bucketed anomaly baseline (hour-of-day statistics per
day-context bucket: workday / weekend / holiday), kept modular so a
statistical or ML model can replace the heuristic without touching callers.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo
from config import settings

logger = logging.getLogger(__name__)

_ZONE_CACHE: Dict[str, ZoneInfo] = {}

BUCKET_WORKDAY = "workday"
BUCKET_WEEKEND = "weekend"
BUCKET_HOLIDAY = "holiday"



def get_zone(tz_name: Optional[str]) -> ZoneInfo:
    """ZoneInfo for an IANA name; invalid/missing names fall back to the
    configured DEFAULT_SITE_TIMEZONE, then UTC (each fallback logged once)."""
    for candidate in (tz_name, str(settings.DEFAULT_SITE_TIMEZONE), "UTC"):
        if not candidate:
            continue
        zone = _ZONE_CACHE.get(candidate)
        if zone is not None:
            return zone
        try:
            zone = ZoneInfo(candidate)
            _ZONE_CACHE[candidate] = zone
            return zone
        except Exception:
            if candidate == tz_name:
                logger.warning("[TIME_CONTEXT] invalid timezone %r — using fallback", candidate)
    return ZoneInfo("UTC")


def to_local(utc_naive: datetime, tz_name: Optional[str]) -> datetime:
    """Naive-UTC DB timestamp -> aware local time (DST-correct)."""
    aware_utc = utc_naive.replace(tzinfo=dt_timezone.utc)
    return aware_utc.astimezone(get_zone(tz_name))


def local_hour(utc_naive: datetime, tz_name: Optional[str]) -> float:
    local = to_local(utc_naive, tz_name)
    return local.hour + local.minute / 60.0


def parse_weekend_days(raw: Optional[str] = None) -> Set[int]:
    """WEEKEND_DAYS config: comma list of weekday numbers, Monday=0.
    Default '5,6' (Sat/Sun)."""
    if raw is None:
        raw = str(settings.WEEKEND_DAYS)
    days: Set[int] = set()
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6:
            days.add(int(part))
    return days or {5, 6}


def parse_holidays(raw: Optional[str] = None) -> Set[str]:
    """ANOMALY_HOLIDAYS config: comma list of ISO dates (site-local dates)."""
    if raw is None:
        raw = str(settings.ANOMALY_HOLIDAYS or "")
    out: Set[str] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            datetime.strptime(part, "%Y-%m-%d")
            out.add(part)
        except ValueError:
            logger.warning("[TIME_CONTEXT] ignoring malformed holiday date %r", part)
    return out


def day_bucket(local_dt: datetime, weekend_days: Set[int], holidays: Set[str]) -> str:
    """Which behavioral bucket a LOCAL datetime belongs to. Holidays rank
    above weekends (a holiday behaves like neither a workday nor a routine
    weekend)."""
    if local_dt.strftime("%Y-%m-%d") in holidays:
        return BUCKET_HOLIDAY
    if local_dt.weekday() in weekend_days:
        return BUCKET_WEEKEND
    return BUCKET_WORKDAY


@dataclass
class BucketStats:
    samples: int
    mean_hour: float
    std_hours: float


@dataclass
class BaselineEvaluation:
    is_anomalous: bool
    bucket_used: str
    bucket_requested: str
    deviation_ratio: float          # distance / threshold (inf when threshold 0)
    distance_hours: float
    mean_hour: float
    effective_std: float
    confidence: float               # 0-1 reliability of THIS judgement
    insufficient_data: bool = False


@dataclass
class BucketedHourBaseline:
    """Hour-of-day baseline split by day-context bucket.

    Modular by design: callers hand in (local_hour, bucket) samples and ask
    ``evaluate``; swapping this class for a statistical/ML model changes
    nothing upstream. Falls back bucket -> overall when a bucket is thin,
    with reduced confidence; declares insufficient data instead of guessing.
    """
    min_bucket_samples: int
    std_floor: float
    k_sigma: float
    buckets: Dict[str, List[float]] = field(default_factory=dict)

    def add(self, hour: float, bucket: str) -> None:
        self.buckets.setdefault(bucket, []).append(hour)

    @property
    def total_samples(self) -> int:
        return sum(len(v) for v in self.buckets.values())

    def _stats(self, hours: List[float]) -> BucketStats:
        from backend.core.security_intelligence_service import circular_hour_stats
        mean, std = circular_hour_stats(hours)
        return BucketStats(len(hours), mean, std)

    def stats_summary(self) -> Dict[str, Dict[str, float]]:
        out = {}
        for bucket, hours in self.buckets.items():
            s = self._stats(hours)
            out[bucket] = {
                "samples": s.samples,
                "mean_hour": round(s.mean_hour, 2),
                "std_hours": round(s.std_hours, 2) if math.isfinite(s.std_hours) else None,
            }
        return out

    def evaluate(self, hour: float, bucket: str) -> BaselineEvaluation:
        from backend.core.security_intelligence_service import circular_hour_distance

        pool = self.buckets.get(bucket, [])
        bucket_used = bucket
        confidence_scale = 1.0
        if len(pool) < self.min_bucket_samples:
            # Thin bucket: judge against ALL history, at reduced confidence —
            # a weekend visit judged by a workday-dominated baseline is a
            # weaker claim, and the output says so.
            all_hours = [h for hours in self.buckets.values() for h in hours]
            if len(all_hours) < self.min_bucket_samples:
                return BaselineEvaluation(
                    is_anomalous=False, bucket_used="none", bucket_requested=bucket,
                    deviation_ratio=0.0, distance_hours=0.0, mean_hour=12.0,
                    effective_std=float("inf"), confidence=0.0,
                    insufficient_data=True)
            pool = all_hours
            bucket_used = "overall"
            confidence_scale = 0.5

        stats = self._stats(pool)
        effective_std = max(stats.std_hours, self.std_floor)
        distance = circular_hour_distance(hour, stats.mean_hour)
        threshold = self.k_sigma * effective_std
        flagged = math.isfinite(threshold) and distance > threshold
        if flagged:
            ratio = (distance / threshold) if threshold > 0 else float("inf")
        else:
            ratio = (distance / threshold) if threshold and math.isfinite(threshold) else 0.0
        # Reliability grows with samples and saturates (n / (n + k)).
        confidence = confidence_scale * (stats.samples / (stats.samples + 10.0))
        return BaselineEvaluation(
            is_anomalous=flagged, bucket_used=bucket_used, bucket_requested=bucket,
            deviation_ratio=ratio, distance_hours=distance,
            mean_hour=stats.mean_hour, effective_std=effective_std,
            confidence=round(confidence, 3))
