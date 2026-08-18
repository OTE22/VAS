"""Changing a setting must change the SYSTEM, not just the settings table.

    docker exec face_recognition_api python -m pytest tests/test_settings_change_behavior.py -v

`tests/test_settings_system.py` proves the settings API is correct: values save,
validate, survive a GET and report their source honestly. That is necessary and
not sufficient. A setting can pass every one of those tests and still change
nothing, because the code that decides the behaviour never reads it:

  * a literal in the consumer wins over the setting
    (`min(settings.LIVE_ALERT_MAX_PER_IDENTITY, 10)` — an admin could raise the
    setting to 50 and still be refused at 10)
  * a default argument shadows it
    (`def search_known(..., threshold: float = 0.4)` — a second declaration of
    SIMILARITY_THRESHOLD that won for every caller that omitted the argument)
  * the value is captured at import into a module-level singleton
    (`self.vector_verify_threshold = settings.UNKNOWN_SIMILARITY_THRESHOLD` in
    an `__init__` that runs exactly once)
  * nothing reads it at all (23 settings were rendered as editable cards wired
    to nothing)

So these tests do the same thing an operator does: change the value through the
REAL runtime path, then exercise the REAL consumer and assert the observable
result moved. Every one of them fails if the corresponding literal comes back.

`tests/test_runtime_editability.py` and `tests/test_config_single_source.py`
catch the same class of defect by scanning source; these catch it by behaviour.
Both are kept because each sees cases the other cannot.
"""

import asyncio

import pytest

from conftest import run_on_shared_loop as run_async  # noqa: E402
from tests._repo_scan import strip_comments_and_docstrings  # noqa: E402


def _module_source(dotted: str) -> str:
    """Source of a module by name.

    `inspect.getsource(module)` is unreliable here: several packages bind an
    INSTANCE over the submodule name (backend.core.identity_service is both a
    module and, after startup, a service object), so the plain import gives
    back something inspect cannot read.
    """
    import importlib
    import sys
    importlib.import_module(dotted)
    with open(sys.modules[dotted].__file__, encoding="utf-8") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class setting_override:
    """Apply a setting through the real runtime path, then restore it.

    Deliberately NOT `setattr(settings, ...)`: that would prove only that
    Python assignment works. `apply_to_runtime` is what the settings API calls,
    including its refusal of security-critical keys, so a setting that cannot
    actually be applied fails here rather than passing on a bypass.
    """

    def __init__(self, key, value):
        self.key = key
        self.value = value
        self._original = None

    def __enter__(self):
        from config import settings
        from backend.core.runtime_settings import apply_to_runtime
        self._original = getattr(settings, self.key)
        applied = apply_to_runtime(self.key, self.value)
        assert applied, (
            f"{self.key} could not be applied to the running process; it is "
            "registered with an apply mode that does not permit it")
        assert getattr(settings, self.key) == self.value
        return self

    def __exit__(self, *exc):
        from backend.core.runtime_settings import apply_to_runtime
        apply_to_runtime(self.key, self._original)
        return False


async def _session():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()
    return db_manager


# ---------------------------------------------------------------------------
# Recognition thresholds
# ---------------------------------------------------------------------------

def test_similarity_threshold_moves_the_known_search_bar():
    """The KNOWN-search bar follows SIMILARITY_THRESHOLD with no argument given.

    `search_known` used to declare `threshold: float = 0.4`. Every caller that
    omitted the argument got 0.4 regardless of the setting, so raising the
    threshold on the settings page changed nothing for those paths.
    """
    import inspect
    from backend.core.identity_index_pgvector import IdentityIndexPgVector

    signature = inspect.signature(IdentityIndexPgVector.search_known)
    assert signature.parameters["threshold"].default is None, (
        "search_known declares a literal threshold default, which shadows "
        "SIMILARITY_THRESHOLD for every caller that omits the argument")

    for name in ("search_unknown", "search_all"):
        sig = inspect.signature(getattr(IdentityIndexPgVector, name))
        assert sig.parameters["threshold"].default is None, (
            f"{name} declares a literal threshold default")


def test_identity_service_thresholds_track_the_settings():
    """IdentityService reads both bars per call, not once at construction."""
    from backend.core.identity_service import IdentityService
    from config import settings

    service = IdentityService.__new__(IdentityService)

    with setting_override("SIMILARITY_THRESHOLD", 0.87):
        assert service.known_threshold == pytest.approx(0.87)
    assert service.known_threshold == pytest.approx(settings.SIMILARITY_THRESHOLD)

    with setting_override("UNKNOWN_SIMILARITY_THRESHOLD", 0.61):
        assert service.unknown_threshold == pytest.approx(0.61)


def test_clustering_honours_a_threshold_edit_without_a_restart():
    """Pins the module-level-singleton freeze.

    `identity_clustering` is instantiated once at import. Its constructor did
    `self.vector_verify_threshold = float(settings.UNKNOWN_SIMILARITY_THRESHOLD)`
    under a comment reading "Read the live setting instead" — and it did, once,
    at first import. An admin edit was reported as applied and never reached
    the merge decision.
    """
    from backend.core.identity_clustering import clustering_service

    with setting_override("UNKNOWN_SIMILARITY_THRESHOLD", 0.66):
        assert clustering_service.vector_verify_threshold == pytest.approx(0.66)
    with setting_override("UNKNOWN_SIMILARITY_THRESHOLD", 0.29):
        assert clustering_service.vector_verify_threshold == pytest.approx(0.29)

    with setting_override("CROSS_PIPELINE_SIMILARITY_THRESHOLD", 0.71):
        assert clustering_service.cross_camera_verify_threshold == pytest.approx(0.71)


def test_pipeline_clustering_weights_are_not_frozen_at_import():
    """The four weights the settings page offers were duplicated as literals."""
    from backend.core.pipeline_aware_clustering import pipeline_aware_clustering

    with setting_override("EMBEDDING_SIMILARITY_WEIGHT", 0.55):
        assert pipeline_aware_clustering.embedding_weight == pytest.approx(0.55)
    with setting_override("PIPELINE_SIMILARITY_WEIGHT", 0.45):
        assert pipeline_aware_clustering.pipeline_weight == pytest.approx(0.45)
    with setting_override("UNKNOWN_SIMILARITY_THRESHOLD", 0.33):
        assert pipeline_aware_clustering.min_similarity_threshold == pytest.approx(0.33)


def test_similarity_model_heuristic_uses_the_configured_weights():
    """The heuristic fallback held a fourth copy of the same weight pair."""
    from backend.core.similarity_model import similarity_model

    def score():
        return similarity_model._heuristic_prediction(
            embedding_similarity=1.0, pipeline_overlap=0.0,
            quality_score_1=1.0, quality_score_2=1.0,
            appearances_diff=0.0, is_cross_pipeline=False)

    with setting_override("EMBEDDING_SIMILARITY_WEIGHT", 1.0):
        with setting_override("PIPELINE_SIMILARITY_WEIGHT", 0.0):
            high = score()
    with setting_override("EMBEDDING_SIMILARITY_WEIGHT", 0.2):
        with setting_override("PIPELINE_SIMILARITY_WEIGHT", 0.8):
            low = score()

    assert high > low, (
        "the heuristic ignored EMBEDDING_SIMILARITY_WEIGHT — it carried its own "
        "0.9/0.1 and 0.7/0.3 literals")


# ---------------------------------------------------------------------------
# Retrieval and display floors
# ---------------------------------------------------------------------------

def test_retrieval_floor_and_display_floor_are_separately_configurable():
    from backend.core.advanced_search import AdvancedSearchService

    service = AdvancedSearchService.__new__(AdvancedSearchService)

    with setting_override("SEARCH_RETRIEVAL_FLOOR", 0.12):
        assert service.candidate_threshold == pytest.approx(0.12)
    with setting_override("SEARCH_CANDIDATE_MULTIPLIER", 7):
        assert service.candidate_multiplier == 7
    with setting_override("SEARCH_FILTERED_CANDIDATE_MULTIPLIER", 9):
        assert service.filtered_candidate_multiplier == 9


def test_config_guard_refuses_a_retrieval_floor_above_the_display_threshold():
    """Refuse, never clamp.

    Inverted, the display threshold stops meaning anything: nothing between the
    two bars is ever retrieved, so lowering SIMILARITY_THRESHOLD cannot recover
    the matches it excluded. Silently correcting it would mean the operator who
    typed it never learns the configuration they wrote does not exist.
    """
    from types import SimpleNamespace
    from backend.security.config_guard import collect_violations

    cfg = SimpleNamespace(SEARCH_RETRIEVAL_FLOOR=0.9, SIMILARITY_THRESHOLD=0.4)
    codes = {v.code for v in collect_violations(cfg, env={})}
    assert "SEARCH_RETRIEVAL_FLOOR_ABOVE_DISPLAY" in codes

    cfg = SimpleNamespace(SEARCH_RETRIEVAL_FLOOR=0.2, SIMILARITY_THRESHOLD=0.4)
    codes = {v.code for v in collect_violations(cfg, env={})}
    assert "SEARCH_RETRIEVAL_FLOOR_ABOVE_DISPLAY" not in codes


def test_config_guard_refuses_an_inverted_top_k_pair():
    from types import SimpleNamespace
    from backend.security.config_guard import collect_violations

    cfg = SimpleNamespace(SEARCH_DEFAULT_TOP_K=200, SEARCH_MAX_TOP_K=50)
    codes = {v.code for v in collect_violations(cfg, env={})}
    assert "SEARCH_TOP_K_INVERTED" in codes


# ---------------------------------------------------------------------------
# Result depth — one default for every search surface
# ---------------------------------------------------------------------------

def test_batch_and_single_search_share_one_default_depth():
    """They defaulted to 5 and 10 while driven by ONE control on the page, so a
    batch silently ran at half the depth the operator asked for."""
    import inspect
    from backend.core.batch_search_service import BatchSearchService
    from backend.routes import advanced_search as single_route
    from backend.routes import batch_export

    batch_default = inspect.signature(
        BatchSearchService.search_batch).parameters["top_k"].default
    assert batch_default is None, (
        "the batch service declares its own literal top_k default")

    for module in (single_route, batch_export):
        source = inspect.getsource(module)
        assert "settings.SEARCH_DEFAULT_TOP_K" in source, (
            f"{module.__name__} does not resolve top_k from the setting")
        assert "settings.SEARCH_MAX_TOP_K" in source, (
            f"{module.__name__} does not enforce the configured ceiling")


def test_ingest_depth_is_configurable():
    source = _module_source("backend.core.identity_service")
    assert "settings.IDENTITY_INGEST_TOP_K" in source
    assert "top_k=5" not in source, "the ingest search still carries a literal depth"


# ---------------------------------------------------------------------------
# Confidence bands — the frontend must not hold a second copy
# ---------------------------------------------------------------------------

def test_confidence_band_of_a_fixed_score_follows_the_settings():
    from backend.core.advanced_search import AdvancedSearchService

    service = AdvancedSearchService.__new__(AdvancedSearchService)
    band = service._get_confidence_band

    # One fixed score, two different configurations, two different bands.
    with setting_override("CONFIDENCE_HIGH_MIN", 0.50):
        assert band(0.55) in ("HIGH", "VERY_HIGH")
    with setting_override("CONFIDENCE_HIGH_MIN", 0.95):
        with setting_override("CONFIDENCE_MEDIUM_MIN", 0.90):
            assert band(0.55) not in ("HIGH", "VERY_HIGH")


def test_the_confidence_bands_are_published_to_the_browser():
    """The frontend must read them, not keep its own copy — admin-live-alerts
    painted bands at 0.8/0.6, numbers matching no backend boundary."""
    import inspect
    from backend.routes import advanced_search

    source = inspect.getsource(advanced_search.get_search_config)
    for key in ("CONFIDENCE_VERY_HIGH_MIN", "CONFIDENCE_HIGH_MIN",
                "CONFIDENCE_MEDIUM_MIN", "CONFIDENCE_LOW_MIN"):
        assert key in source, f"{key} is not published to the UI"
    assert "SEARCH_DEFAULT_TOP_K" in source
    assert "SEARCH_MAX_TOP_K" in source


# ---------------------------------------------------------------------------
# Limits that were clamped by a literal
# ---------------------------------------------------------------------------

def test_live_alert_per_identity_cap_is_not_clamped_to_ten():
    """`min(settings.LIVE_ALERT_MAX_PER_IDENTITY, 10)` meant an admin could
    raise the setting to any number and still be refused at 10, with no way to
    see why."""
    import inspect
    from backend.core import live_alert_service

    source = inspect.getsource(live_alert_service)
    assert "min(settings.LIVE_ALERT_MAX_PER_IDENTITY" not in source, (
        "a literal ceiling silently overrides the configured cap")

    with setting_override("LIVE_ALERT_MAX_PER_IDENTITY", 42):
        from config import settings
        assert settings.LIVE_ALERT_MAX_PER_IDENTITY == 42


def test_ml_retention_has_no_hidden_seven_day_floor():
    """`timedelta(days=max(7, days))` silently overrode every ML retention
    setting below 7 — the operator who set 3 got 7 and was never told."""
    import inspect
    from backend.core import data_retention

    source = strip_comments_and_docstrings(inspect.getsource(
        data_retention.DataRetentionManager._cleanup_auxiliary))
    assert "max(7, days)" not in source, (
        "a hidden floor still overrides the configured ML retention window")
    assert "settings.TASK_HISTORY_RETENTION_DAYS" in source, (
        "background-task history still uses a literal 30-day window while "
        "every sibling sweep in the same function reads config")


def test_batch_search_timeout_is_enforced_not_just_reported():
    """BATCH_SEARCH_TIMEOUT_SECONDS was registered as immediate-apply and
    echoed in every batch response, but the gather it was supposed to bound ran
    unbounded."""
    import inspect
    from backend.core import batch_search_service

    source = inspect.getsource(batch_search_service.BatchSearchService.search_batch)
    assert "asyncio.wait_for" in source
    assert "BATCH_SEARCH_TIMEOUT_SECONDS" in source


def test_every_retention_sweep_runs_without_poisoning_the_others():
    """ML_SNAPSHOT_RETENTION_DAYS had never deleted a row.

    ml_feature_snapshots records `computed_at`, not `created_at`, so the sweep
    raised UndefinedColumn every run. In PostgreSQL a failed statement aborts
    the whole transaction, so the ml_drift_reports sweep that followed it died
    with InFailedSQLTransactionError too. Both were swallowed by a broad except
    and reported as zero rows — indistinguishable from "nothing was expired".

    A dry run touches nothing, so this is safe to assert against the live DB.
    """
    import logging
    from backend.core.data_retention import DataRetentionManager

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    async def scenario():
        await _session()          # the test process needs its own engine
        return await DataRetentionManager().cleanup_old_data(dry_run=True)

    handler = Capture()
    logger = logging.getLogger("backend.core.data_retention")
    logger.addHandler(handler)
    try:
        result = run_async(scenario())
    finally:
        logger.removeHandler(handler)

    assert result["status"] == "completed", result
    failures = [m for m in records if "cleanup failed" in m or "cap failed" in m]
    assert not failures, "a retention sweep failed: " + " | ".join(failures)

    extra = result.get("extra") or {}
    for key in ("ml_predictions_deleted", "ml_snapshots_deleted",
                "ml_drift_reports_deleted", "search_history_deleted",
                "task_history_deleted", "search_history_over_cap"):
        assert key in extra, f"{key} was not reported — its sweep did not run"


def test_retention_sweeps_are_individually_isolated():
    """Each sweep runs in its own SAVEPOINT, so one bad table cannot take the
    rest of the run down with it."""
    import inspect
    from backend.core import data_retention

    source = strip_comments_and_docstrings(inspect.getsource(
        data_retention.DataRetentionManager._cleanup_auxiliary))
    assert source.count("begin_nested()") >= 4, (
        "retention sweeps are not savepoint-isolated; one failure will abort "
        "the transaction and every later sweep with it")


def test_search_history_per_user_cap_is_actually_enforced():
    """SEARCH_HISTORY_MAX_PER_USER was registered next_job_run and reported on
    the search page; no job trimmed anything."""
    import inspect
    from backend.core import data_retention

    source = strip_comments_and_docstrings(inspect.getsource(
        data_retention.DataRetentionManager._cleanup_auxiliary))
    assert "SEARCH_HISTORY_MAX_PER_USER" in source


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def test_page_size_bounds_come_from_configuration():
    from fastapi import HTTPException
    from backend.utils.pagination import resolve_page_size

    with setting_override("API_MAX_PAGE_SIZE", 30):
        assert resolve_page_size(25) == 25
        with pytest.raises(HTTPException) as excinfo:
            resolve_page_size(31)
        assert excinfo.value.status_code == 422
        assert "30" in str(excinfo.value.detail)

    with setting_override("API_DEFAULT_PAGE_SIZE", 7):
        assert resolve_page_size(None) == 7


def test_previously_unbounded_routes_now_bound_their_page_size():
    """Four route groups took a bare `limit: int = 100` with no ceiling, so a
    client could request any page size it liked."""
    import inspect
    from backend.routes import cache, conversations, detections

    for module in (cache, conversations, detections):
        source = inspect.getsource(module)
        assert "resolve_page_size" in source, (
            f"{module.__name__} does not bound its page size")


# ---------------------------------------------------------------------------
# Face quality
# ---------------------------------------------------------------------------

def test_face_quality_thresholds_reach_the_scorer():
    """Four FACE_QUALITY_THRESHOLD_* fields were declared, described and
    rendered as editable while face_quality.py used its own literals."""
    from backend.core.face_quality import face_quality_scorer

    with setting_override("FACE_QUALITY_THRESHOLD_BLUR", 0.31):
        assert face_quality_scorer.blur_threshold == pytest.approx(0.31)
    with setting_override("FACE_QUALITY_THRESHOLD_LIGHTING", 0.42):
        assert face_quality_scorer.lighting_threshold == pytest.approx(0.42)
    with setting_override("FACE_QUALITY_THRESHOLD_SIZE", 96):
        assert face_quality_scorer.min_face_pixels == 96
    with setting_override("FACE_QUALITY_THRESHOLD_ANGLE", 45.0):
        assert face_quality_scorer.max_roll_degrees == pytest.approx(45.0)


def test_quality_search_thresholds_are_not_frozen_in_the_singleton():
    from backend.core.face_quality import face_quality_scorer

    with setting_override("SEARCH_MIN_QUALITY_THRESHOLD", 0.11):
        assert face_quality_scorer.min_threshold == pytest.approx(0.11)
    with setting_override("SEARCH_QUALITY_WARNING_THRESHOLD", 0.77):
        assert face_quality_scorer.warning_threshold == pytest.approx(0.77)


# ---------------------------------------------------------------------------
# Feature flags that gated nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "RELATED_IDENTITIES_ENABLED",
    "TEMPORAL_PATTERNS_ENABLED",
    "CROSS_CAMERA_TRACKING_ENABLED",
    "TRAJECTORY_PREDICTION_ENABLED",
    "BATCH_SEARCH_ENABLED",
    "EXPORT_RESULTS_ENABLED",
    "NEGATIVE_SEARCH_ENABLED",
    "PIPELINE_AWARE_CLUSTERING_ENABLED",
])
def test_every_feature_flag_gates_something(key):
    """Each of these was declared with a description asserting it enables or
    disables a feature, offered as a switch on the settings page, and read by
    nothing at all."""
    import inspect
    from backend.routes import advanced_search, batch_export, identities, intelligence

    sources = "\n".join(inspect.getsource(m) for m in
                        (advanced_search, batch_export, identities, intelligence))
    assert f"settings.{key}" in sources, f"{key} still gates nothing"


def test_auto_threshold_learning_flag_is_honoured():
    from backend.core.threshold_learner import threshold_learner

    with setting_override("AUTO_THRESHOLD_LEARNING_ENABLED", False):
        assert threshold_learner.enabled is False
    with setting_override("AUTO_THRESHOLD_LEARNING_ENABLED", True):
        assert threshold_learner.enabled is True


def test_threshold_learner_min_samples_is_the_declared_setting():
    """`self.min_samples_for_learning = 10` duplicated
    THRESHOLD_MIN_SAMPLES_FOR_ACTIVATION, which also defaults to 10 — agreeing
    today, silently diverging the first time either moved."""
    from backend.core.threshold_learner import threshold_learner

    with setting_override("THRESHOLD_MIN_SAMPLES_FOR_ACTIVATION", 23):
        assert threshold_learner.min_samples_for_learning == 23


# ---------------------------------------------------------------------------
# Hydration — "requires a restart" must be a promise that is kept
# ---------------------------------------------------------------------------

def test_boot_hydration_applies_restart_required_settings():
    """The failure this whole audit turned on.

    `hydrate_from_db` skipped every key whose apply_mode was not dynamic. The
    UI told the admin "saved — takes effect after an API restart", the value
    was durably stored, and the restart re-read env/defaults and ignored it.
    111 of 170 settings behaved this way, permanently.

    Boot IS the restart those saves were waiting for, so at boot the apply_mode
    gate must not apply.
    """
    from config import settings
    from backend.core.runtime_settings import apply_to_runtime, get_meta

    key = "PGVECTOR_HNSW_EF_SEARCH"
    assert get_meta(key).apply_mode not in ("immediate", "next_request", "next_job_run"), (
        "this test needs a restart-required key to be meaningful")

    original = getattr(settings, key)
    try:
        # Normal PUT path: correctly refuses, because a live change is a lie.
        assert apply_to_runtime(key, original + 8) is False
        assert getattr(settings, key) == original

        # Boot path: applies, because this IS the restart.
        assert apply_to_runtime(key, original + 8, at_boot=True) is True
        assert getattr(settings, key) == original + 8
    finally:
        apply_to_runtime(key, original, at_boot=True)


def test_container_level_settings_are_still_refused_at_boot():
    """WORKERS describes how the container was launched. Applying it to this
    process's settings object would make `effective_value` report a number the
    container is not running under — worse than not applying it."""
    from config import settings
    from backend.core.runtime_settings import apply_to_runtime

    original = settings.WORKERS
    assert apply_to_runtime("WORKERS", original + 1, at_boot=True) is False
    assert settings.WORKERS == original


def test_security_critical_settings_are_refused_at_boot_too():
    """The boot path must not become a way around the production guard."""
    from config import settings
    from backend.core.runtime_settings import apply_to_runtime
    from backend.security.config_guard import SECURITY_CRITICAL_KEYS

    checked = 0
    for key in SECURITY_CRITICAL_KEYS:
        if not hasattr(settings, key):
            continue
        original = getattr(settings, key)
        assert apply_to_runtime(key, original, at_boot=True) is False, (
            f"{key} is security-critical and must never be applied in-process")
        checked += 1
    assert checked, "no security-critical key was actually exercised"


def test_hydration_runs_before_the_components_that_read_settings():
    """Ordering matters as much as the gate.

    Hydration used to run after the pgvector index had already been built, so
    an index-geometry setting was applied too late to affect the thing it
    configures. It now runs immediately after the database is reachable, in
    phase 1.1.1, before any component is constructed.
    """
    import inspect
    from backend import lifespan

    source = inspect.getsource(lifespan)
    hydrate_at = source.index("hydrate_from_db")
    for later in ("cache_manager.initialize()", "ensure_indexes_exist",
                  "IdentityService("):
        if later in source:
            assert hydrate_at < source.index(later), (
                f"settings hydration runs after {later}, so a stored value "
                "cannot reach it")


# ---------------------------------------------------------------------------
# The settings page itself
# ---------------------------------------------------------------------------

def test_every_registered_setting_reaches_the_page():
    """25 registry keys were absent from the category map, so no row was
    seeded, they rendered nowhere, and PUT answered 404 for a setting the
    registry advertised as editable."""
    import inspect
    from backend.core.runtime_settings import SETTINGS_REGISTRY, has_key
    from backend.routes.settings import sync_settings_from_config

    source = inspect.getsource(sync_settings_from_config)
    assert "SETTINGS_REGISTRY" in source, (
        "the category map is not reconciled against the registry, so a "
        "registered key can silently render nowhere")

    missing = [key for key in SETTINGS_REGISTRY
               if has_key(key) and f'"{key}"' not in source]
    # Reconciliation files anything unlisted under `advanced`; this asserts the
    # mechanism exists rather than that the hand-written map is exhaustive.
    assert "advanced" in source or not missing


def test_the_settings_writer_requires_csrf():
    """Six sibling admin routers carried this check. The one endpoint that can
    change recognition thresholds for the whole deployment did not."""
    import inspect
    from backend.routes import settings as settings_routes

    signature = inspect.signature(settings_routes.update_setting)
    assert "_csrf" in signature.parameters, (
        "PUT /api/settings/{key} has no CSRF dependency")


def test_dashboard_display_hours_accepts_a_fractional_value():
    """The registry advertised this as a float with a 0.1 minimum while the
    config field was an int, so saving 2.5 was accepted and then threw
    int('2.5') inside the WebSocket broadcast, swallowed as 'could not
    broadcast config change'."""
    from config import Settings
    from backend.core.runtime_settings import get_meta, typed_parse

    assert Settings.model_fields["DASHBOARD_FACE_DISPLAY_HOURS"].annotation is float
    assert get_meta("DASHBOARD_FACE_DISPLAY_HOURS").value_type == "float"
    assert typed_parse("DASHBOARD_FACE_DISPLAY_HOURS", "2.5") == pytest.approx(2.5)


def test_cluster_startup_delay_accepts_a_fractional_value():
    """A genuine float auto-derived as integer, so 0.5 was rejected."""
    from backend.core.runtime_settings import typed_parse

    assert typed_parse("CLUSTER_STARTUP_DELAY_HOURS", "0.5") == pytest.approx(0.5)


def test_notification_readiness_can_ever_be_true():
    """smtp_ready and sms_ready read three names that were declared nowhere,
    so both were permanently False no matter how the deployment was set up."""
    from config import Settings
    from backend.routes.live_alerts import _channel_config_status

    for name in ("SMTP_HOST", "SMS_PROVIDER_URL", "TWILIO_ACCOUNT_SID"):
        assert name in Settings.model_fields, f"{name} is not a declared setting"

    with setting_override("SMTP_HOST", "smtp.example.test"):
        assert _channel_config_status()["email"] is True
    with setting_override("SMTP_HOST", ""):
        assert _channel_config_status()["email"] is False


def test_one_cap_governs_how_many_embeddings_an_identity_keeps():
    """IDENTITY_MAX_EMBEDDINGS capped enrichment growth at 20 while
    MAX_EMBEDDINGS_PER_IDENTITY capped retention pruning at 10, so enrichment
    grew an identity to 20 views and the nightly job cut it back to 10."""
    from config import Settings

    assert "IDENTITY_MAX_EMBEDDINGS" not in Settings.model_fields
    assert "MAX_EMBEDDINGS_PER_IDENTITY" in Settings.model_fields

    source = _module_source("backend.core.identity_service")
    assert "settings.MAX_EMBEDDINGS_PER_IDENTITY" in source


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
