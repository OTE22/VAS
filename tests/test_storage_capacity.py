"""Storage capacity is measured, not configured.

    docker exec face_recognition_api python -m pytest tests/test_storage_capacity.py -v

The home page reported "0.4 MB of 5,000 GB — Healthy" while the volume backing
STORAGE_DIR was 97% full with 11.5 GB free. Both halves of that sentence came
from the wrong place:

  * the "5,000 GB" was `settings.MAX_STORAGE_GB`, a compose env var that
    enforces nothing — no code deletes, blocks or rejects on it;
  * `usage_percent` was our own os.walk footprint divided by that constant, so
    it could not see anything else on the disk. `backend/routes/health.py`
    keyed its storage verdict off the same number and agreed everything was
    fine.

`usage_percent` now means real volume utilisation. The app footprint survives
as `total_size_*` / `app_usage_percent`, and MAX_STORAGE_GB survives as a
labelled soft budget.
"""

import json
import os
import re
import urllib.error
import urllib.request

import pytest

from conftest import run_on_shared_loop as run_async

BASE = "http://localhost:8000"
HOME_JS = "/app/frontend/js/home.js"
HOME_HTML = "/app/frontend/home.html"


def _http(method, path, body=None, token=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except Exception:
            return exc.code, {"_raw": raw.decode(errors="replace")}


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         {"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


def _storage_stats():
    """The producer directly — no HTTP cache, no route shape in the way."""
    from backend.core.data_retention import retention_manager

    async def _run():
        return await retention_manager.get_storage_stats()
    return run_async(_run())


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------

def test_disk_capacity_reads_a_real_filesystem():
    from backend.core.operational_metrics import disk_capacity
    from config import settings

    capacity = disk_capacity(settings.STORAGE_DIR)
    assert capacity is not None, "the storage volume could not be measured"
    total, used, free = capacity
    assert total > 0 and used >= 0 and free >= 0
    # used + free need not equal total (reserved blocks), but both must fit.
    assert used <= total and free <= total


def test_disk_capacity_returns_none_for_an_unreadable_path():
    """A bad mount must degrade a report, never raise into a request."""
    from backend.core.operational_metrics import disk_capacity

    assert disk_capacity("/nonexistent-path-for-tests") is None


def test_prometheus_gauges_and_the_api_share_one_probe():
    """probe_storage and the storage stats must not measure different things."""
    import inspect

    from backend.core import operational_metrics

    source = inspect.getsource(operational_metrics.probe_storage)
    assert "disk_capacity(" in source, "probe_storage bypasses the shared probe"
    assert "shutil.disk_usage" not in source, (
        "probe_storage reads the disk directly again — the gauges and the API "
        "would be free to disagree")


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------

def test_storage_stats_keep_every_legacy_key():
    """Additive change: the home page and existing clients still work."""
    stats = _storage_stats()
    for key in ("total_size_mb", "total_size_gb", "file_count",
                "max_storage_gb", "usage_percent"):
        assert key in stats, f"{key} disappeared from the storage block"


def test_storage_stats_report_real_disk_figures():
    stats = _storage_stats()
    assert stats["capacity_source"] == "disk", stats
    assert stats["disk_total_gb"] > 0
    assert stats["disk_free_gb"] >= 0
    assert stats["disk_used_gb"] >= 0
    assert stats["disk_used_gb"] <= stats["disk_total_gb"]


def test_usage_percent_tracks_the_disk_not_the_budget():
    """THE regression this fix exists for.

    Before, usage_percent was footprint / MAX_STORAGE_GB — which on this
    machine read 0.000008% while the disk was 97% full. It must now equal
    disk_used / disk_total, and must NOT equal the old formula.
    """
    stats = _storage_stats()

    expected = stats["disk_used_gb"] / stats["disk_total_gb"] * 100
    assert abs(stats["usage_percent"] - expected) < 0.5, (
        f"usage_percent {stats['usage_percent']} does not track the disk "
        f"({expected})")

    old_formula = stats["total_size_gb"] / stats["max_storage_gb"] * 100
    assert abs(stats["usage_percent"] - old_formula) > 1e-6, (
        "usage_percent is still footprint-over-budget — the number that "
        "reported Healthy on a nearly-full disk")


def test_usage_percent_is_a_percentage_not_a_fraction():
    stats = _storage_stats()
    assert 0 <= stats["usage_percent"] <= 100


def test_app_footprint_is_still_reported_separately():
    """"How much are MY files using" is a real question; it just is not the
    same question as "is the disk full"."""
    stats = _storage_stats()
    assert stats["total_size_gb"] >= 0
    assert stats["file_count"] >= 0
    expected = min(100, stats["total_size_gb"] / stats["max_storage_gb"] * 100)
    assert abs(stats["app_usage_percent"] - expected) < 1e-6


def test_the_soft_budget_no_longer_drives_the_meter(monkeypatch):
    """Changing MAX_STORAGE_GB must move app_usage_percent and leave
    usage_percent alone — proof the denominator really is the disk."""
    from backend.core import data_retention
    from config import settings

    original = settings.MAX_STORAGE_GB
    before = _storage_stats()
    try:
        settings.MAX_STORAGE_GB = max(1, int(original) // 2)
        # The 60 s TTL would otherwise serve the pre-change dict.
        data_retention.DataRetentionManager._storage_stats_cache = None
        after = _storage_stats()

        assert abs(after["usage_percent"] - before["usage_percent"]) < 0.5, (
            "the soft budget still moves the disk meter")
        assert after["app_usage_percent"] > before["app_usage_percent"], (
            "app_usage_percent ignored the budget change")
    finally:
        settings.MAX_STORAGE_GB = original
        data_retention.DataRetentionManager._storage_stats_cache = None


def test_probe_failure_degrades_to_the_configured_budget(monkeypatch):
    """An unreadable mount must not blank the panel."""
    from backend.core import data_retention

    monkeypatch.setattr(
        "backend.core.operational_metrics.disk_capacity",
        lambda path: None)
    data_retention.DataRetentionManager._storage_stats_cache = None
    try:
        stats = _storage_stats()
        assert stats["capacity_source"] == "configured"
        assert stats["disk_total_gb"] is None
        # Every key still present, and usage_percent falls back to the old
        # meaning rather than vanishing.
        for key in ("total_size_mb", "total_size_gb", "file_count",
                    "max_storage_gb", "usage_percent", "app_usage_percent"):
            assert key in stats
        assert stats["usage_percent"] == stats["app_usage_percent"]
    finally:
        data_retention.DataRetentionManager._storage_stats_cache = None


def test_api_stats_exposes_the_new_capacity_fields(token):
    status, body = _http("GET", "/api/stats", token=token)
    assert status == 200, body
    storage = body["storage"]
    for key in ("total_size_mb", "total_size_gb", "file_count",
                "max_storage_gb", "usage_percent", "app_usage_percent",
                "disk_total_gb", "disk_used_gb", "disk_free_gb",
                "capacity_source"):
        assert key in storage, f"/api/stats storage is missing {key}"
    assert storage["disk_total_gb"] > 0


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def test_home_page_shows_real_capacity_not_the_budget():
    source = _read(HOME_JS)
    assert "storage.disk_total_gb" in source
    assert "storage.disk_free_gb" in source
    assert "GB free of " in source, "the capacity line must report free space"
    # The old line rendered the budget as though it were capacity.
    assert "setText('st-max', 'of ' + limitText)" not in source


def test_home_page_still_shows_the_soft_budget_as_a_budget():
    assert 'id="st-budget"' in _read(HOME_HTML), "the budget row is missing"
    source = _read(HOME_JS)
    assert "st-budget" in source
    assert "budget" in source.lower()


def test_footprint_callout_reads_the_app_percentage():
    """It used to say "well within the 5,000 GB limit" based on the disk
    percentage — it would have said that while the volume was full."""
    source = _read(HOME_JS)
    assert "storage.app_usage_percent" in source
    assert re.search(r"appUsage\s*!==\s*null\s*&&\s*appUsage\s*<\s*0\.01", source), (
        "the callout must gate on the app footprint, not the disk")


def test_percent_is_not_rescaled_in_the_frontend():
    """usage_percent arrives already scaled 0-100.

    Scoped to formatPercent's own body, like the assertion in
    tests/test_home_dashboard.py — elsewhere `* 100` is legitimate rounding
    (formatGB does Math.round(n * 100) / 100).
    """
    match = re.search(r"function formatPercent\(n\) \{.*?\n\}", _read(HOME_JS), re.S)
    assert match, "formatPercent is gone"
    assert "* 100" not in match.group(0), (
        "formatPercent must not re-scale an already-scaled percentage")
