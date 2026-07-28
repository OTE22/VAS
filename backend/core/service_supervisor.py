"""Background-service supervision: one loop shape, one health registry.

Why this module exists
----------------------
Seven periodic background loops (system metrics, log cleanup, identity
clustering, identity retention, data retention, FAISS repair, FAISS rebuild —
plus the batch writer/flusher and the loop-lag monitor) were copy-pasted
`while True` bodies with flat constant retry sleeps. None tracked failures,
none recorded a last-success time, and NO health endpoint observed any of
them: every loop could be dead for a week and /health/ready stayed green.

`supervised_loop` replaces the copy-pasted shape. Each service keeps its
class, its `start()`/`stop()` signatures and its singleton — only the inner
while-loop delegates here. The registry gives health endpoints and Prometheus
one place to read.

Deliberate properties
---------------------
* **The supervisor IS the task.** There is no outer respawn loop, so a
  "restart during shutdown" path structurally cannot exist. Cancellation
  re-raises immediately — never another cycle, never a backoff sleep.
* **It never gives up.** A permanently failing service shows as `degraded`
  with a climbing failure counter and exponential (capped, jittered) backoff,
  visible in /health/ready and metrics — instead of hammering a broken
  dependency every cycle or dying silently.
* **stdlib only.** No imports from lifespan, routes or service modules, so
  health.py can import this with zero circular risk. Metrics are looked up
  lazily and every update is null-checked (the repo's lazy-gauge pattern).
* **Injectable clock/sleep** so tests drive exact backoff sequences without
  real time passing.
"""

import asyncio
import copy
import logging
import random
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Status vocabulary — kept to four values on purpose. "failed" is deliberately
# absent: the supervisor never gives up, so the truthful terminal-ish state
# for a broken service is `degraded` (with consecutive_failures saying how
# broken); `stopped` means cancellation, i.e. intentional shutdown.
STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_DEGRADED = "degraded"
STATUS_STOPPED = "stopped"


@dataclass
class ServiceHealth:
    name: str
    status: str = STATUS_STARTING
    started_at: Optional[float] = None        # wall clock, for humans
    last_success: Optional[float] = None      # wall clock, for humans
    last_success_mono: Optional[float] = None  # monotonic, for staleness math
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    restarts: int = 0                         # re-registrations of this name
    interval: float = 0.0                     # recorded for staleness math


_registry: Dict[str, ServiceHealth] = {}
# Mutations happen on the event-loop thread, but /metrics scrapes and
# to_thread contexts read concurrently; a plain lock + copied snapshots keeps
# every reader race-free without handing out live references.
_registry_lock = threading.Lock()


def _register(name: str, interval: float) -> ServiceHealth:
    with _registry_lock:
        existing = _registry.get(name)
        if existing is not None:
            existing.restarts += 1
            existing.status = STATUS_STARTING
            existing.started_at = time.time()
            existing.interval = interval
            return existing
        health = ServiceHealth(name=name, started_at=time.time(), interval=interval)
        _registry[name] = health
        return health


def get_service_health() -> Dict[str, dict]:
    """Deep-copied snapshot of every registered service's health."""
    with _registry_lock:
        return {name: asdict(health) for name, health in _registry.items()}


def stale_services(factor: float = 3.0, grace: float = 120.0,
                   _now: Callable[[], float] = time.monotonic) -> List[str]:
    """Services that claim to run but have not succeeded in ~factor intervals.

    Staleness is per-service (`interval * factor + grace`) because these
    cadences span 60 seconds to 24 hours — one global constant would either
    never fire for metrics or always fire for clustering. Monotonic time so
    container clock jumps cannot create false alarms. Services still in their
    initial delay (status `starting`, no success yet) are exempt, otherwise
    every boot would report the hour-delayed services as stale.
    """
    now = _now()
    stale: List[str] = []
    with _registry_lock:
        for name, health in _registry.items():
            if health.status not in (STATUS_RUNNING, STATUS_DEGRADED):
                continue
            if health.last_success_mono is None:
                continue  # has not completed a first cycle; covered by status
            allowance = health.interval * factor + grace
            if now - health.last_success_mono > allowance:
                stale.append(name)
    return stale


def reset_registry_for_tests() -> None:
    """Test hook: the registry is process-global state."""
    with _registry_lock:
        _registry.clear()


def _set_gauges(name: str, up: Optional[bool] = None,
                last_success: Optional[float] = None,
                failed: bool = False) -> None:
    """Best-effort metric updates in the repo's lazy-gauge style."""
    try:
        from backend.core import metrics as m
        if up is not None and getattr(m, "metrics_bg_service_up", None):
            m.metrics_bg_service_up.labels(service=name).set(1 if up else 0)
        if last_success is not None and getattr(m, "metrics_bg_last_success", None):
            m.metrics_bg_last_success.labels(service=name).set(last_success)
        if failed and getattr(m, "metrics_bg_failures", None):
            m.metrics_bg_failures.labels(service=name).inc()
    except Exception:  # pragma: no cover - metrics must never break a service
        pass


async def supervised_loop(
    name: str,
    interval,  # float, or a zero-arg callable re-read each cycle (live-tunable cadences)
    work: Callable[[], Awaitable[None]],
    *,
    initial_delay: float = 0.0,
    error_backoff_base: float = 60.0,
    error_backoff_max: float = 1800.0,
    jitter: float = 0.1,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Run `work()` every `interval` seconds under supervision, forever.

    Success: reset failure count, stamp last-success, sleep `interval`
    (± `jitter` fraction, so many same-interval loops de-synchronize).
    Failure: log with traceback, count it, mark `degraded`, back off
    exponentially from `error_backoff_base` capped at `error_backoff_max`.
    Cancellation: mark `stopped` and RE-RAISE IMMEDIATELY — the awaiting
    `stop()` gets its CancelledError; no further cycles, no backoff sleep.
    """
    def _interval_now() -> float:
        return float(interval() if callable(interval) else interval)

    health = _register(name, _interval_now())
    _set_gauges(name, up=True)

    try:
        if initial_delay > 0:
            await sleep(initial_delay)

        while True:
            try:
                await work()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                with _registry_lock:
                    health.consecutive_failures += 1
                    health.last_error = f"{type(e).__name__}: {e}"[:500]
                    health.status = STATUS_DEGRADED
                    failures = health.consecutive_failures
                _set_gauges(name, failed=True)
                backoff = min(error_backoff_max,
                              error_backoff_base * (2 ** (failures - 1)))
                backoff *= 1 + random.uniform(0, jitter)
                logger.error(
                    "service=%s cycle_failed consecutive_failures=%d backoff_s=%.0f",
                    name, failures, backoff, exc_info=True,
                )
                await sleep(backoff)
                continue

            interval_s = _interval_now()
            with _registry_lock:
                health.consecutive_failures = 0
                health.last_error = None
                health.status = STATUS_RUNNING
                health.last_success = time.time()
                health.last_success_mono = now()
                health.interval = interval_s  # keep staleness math honest for live-tuned cadences
            _set_gauges(name, up=True, last_success=health.last_success)
            await sleep(interval_s * (1 + random.uniform(-jitter, jitter)))

    except asyncio.CancelledError:
        with _registry_lock:
            health.status = STATUS_STOPPED
        _set_gauges(name, up=False)
        raise
