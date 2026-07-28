"""service_supervisor: the one loop shape every background service now uses.

Fake clock + fake sleep are injected, so backoff sequences are asserted
EXACTLY, with no real time passing.
"""

import asyncio

import pytest

from conftest import run_on_shared_loop
from backend.core import service_supervisor as sup


@pytest.fixture(autouse=True)
def clean_registry():
    sup.reset_registry_for_tests()
    yield
    sup.reset_registry_for_tests()


class Recorder:
    """Injectable sleep/clock that advances virtual time instantly."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds

    def now(self):
        return self.t


def test_backoff_sequence_is_exponential_and_capped():
    rec = Recorder()
    failures_before_success = 5
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] <= failures_before_success:
            raise RuntimeError("boom")
        raise asyncio.CancelledError  # end the test loop after first success

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await sup.supervised_loop(
                "backoff_probe", 100, flaky,
                error_backoff_base=60, error_backoff_max=600,
                jitter=0, sleep=rec.sleep, now=rec.now,
            )

    run_on_shared_loop(scenario())
    # 5 failures: 60, 120, 240, 480, capped at 600.
    assert rec.sleeps == [60, 120, 240, 480, 600], rec.sleeps


def test_success_resets_failure_count_and_stamps_last_success():
    rec = Recorder()
    calls = {"n": 0}

    async def sometimes():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first cycle fails")
        if calls["n"] == 3:
            raise asyncio.CancelledError
        # second cycle succeeds

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await sup.supervised_loop("reset_probe", 30, sometimes,
                                      error_backoff_base=10, jitter=0,
                                      sleep=rec.sleep, now=rec.now)

    run_on_shared_loop(scenario())
    health = sup.get_service_health()["reset_probe"]
    assert health["consecutive_failures"] == 0, "success must reset the counter"
    assert health["last_success"] is not None
    assert health["last_error"] is None
    assert health["status"] == "stopped"  # ended via cancellation
    # Sleeps: 10 (backoff), 30 (interval after success) — then cancelled.
    assert rec.sleeps == [10, 30], rec.sleeps


def test_cancellation_exits_immediately_without_another_cycle():
    rec = Recorder()
    calls = {"n": 0}

    async def work():
        calls["n"] += 1
        raise asyncio.CancelledError

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await sup.supervised_loop("cancel_probe", 10, work,
                                      jitter=0, sleep=rec.sleep, now=rec.now)

    run_on_shared_loop(scenario())
    assert calls["n"] == 1, "cancellation must not run another cycle"
    assert rec.sleeps == [], "cancellation must not enter any sleep"
    assert sup.get_service_health()["cancel_probe"]["status"] == "stopped"


def test_registry_snapshots_are_copies_and_restarts_count():
    rec = Recorder()

    async def one_shot():
        raise asyncio.CancelledError

    async def scenario():
        for _ in range(2):
            with pytest.raises(asyncio.CancelledError):
                await sup.supervised_loop("restart_probe", 10, one_shot,
                                          jitter=0, sleep=rec.sleep, now=rec.now)

    run_on_shared_loop(scenario())
    snapshot = sup.get_service_health()
    assert snapshot["restart_probe"]["restarts"] == 1, "second start must count as a restart"

    # Mutating the snapshot must not touch the registry.
    snapshot["restart_probe"]["status"] = "vandalized"
    assert sup.get_service_health()["restart_probe"]["status"] != "vandalized"


def test_staleness_uses_per_service_interval_and_exempts_starting():
    rec = Recorder()

    async def succeed_once():
        if rec.t > 0:
            raise asyncio.CancelledError
        # first cycle succeeds (t advances via interval sleep afterwards)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await sup.supervised_loop("stale_probe", 60, succeed_once,
                                      jitter=0, sleep=rec.sleep, now=rec.now)

    run_on_shared_loop(scenario())
    # Force the status back to running (it ended stopped via cancellation).
    with sup._registry_lock:
        sup._registry["stale_probe"].status = sup.STATUS_RUNNING

    # last_success_mono is 0.0 (fake clock at first success).
    # allowance = 60*3 + 120 = 300.
    assert sup.stale_services(_now=lambda: 250) == []
    assert sup.stale_services(_now=lambda: 301) == ["stale_probe"]

    # A service still starting (no success yet) is never stale.
    with sup._registry_lock:
        sup._registry["fresh"] = sup.ServiceHealth(name="fresh", status=sup.STATUS_STARTING,
                                                   interval=1)
    assert "fresh" not in sup.stale_services(_now=lambda: 10_000)


def test_callable_interval_is_reread_each_cycle():
    rec = Recorder()
    intervals = [10, 20]
    calls = {"n": 0}

    def live_interval():
        # calls["n"] is incremented by work() BEFORE the interval sleep, so
        # cycle k's sleep reads index k-1.
        return intervals[min(calls["n"] - 1, len(intervals) - 1)]

    async def work():
        calls["n"] += 1
        if calls["n"] > 2:
            raise asyncio.CancelledError

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await sup.supervised_loop("live_interval_probe", live_interval, work,
                                      jitter=0, sleep=rec.sleep, now=rec.now)

    run_on_shared_loop(scenario())
    assert rec.sleeps == [10, 20], (
        f"interval must be re-read each cycle (admin-tunable cadences): {rec.sleeps}"
    )


def test_jitter_bounds():
    rec = Recorder()
    calls = {"n": 0}

    async def work():
        calls["n"] += 1
        if calls["n"] > 20:
            raise asyncio.CancelledError

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await sup.supervised_loop("jitter_probe", 100, work, jitter=0.1,
                                      sleep=rec.sleep, now=rec.now)

    run_on_shared_loop(scenario())
    assert all(90 <= s <= 110 for s in rec.sleeps), (
        f"jitter must stay within ±10% of the interval: {rec.sleeps}"
    )
