"""
ML Milestone 5: drift monitoring — report-only, honest about insufficiency.
===========================================================================
Run INSIDE the api container against the live app:

    docker exec face_recognition_api python -m pytest tests/test_ml_drift.py -v

Pins: PSI/KS/JS respond to synthetic shifts and stay quiet on identical
distributions; degenerate inputs return None (never a fabricated zero);
severity bands follow the configured PSI thresholds; below the sample floor
a report says insufficient_data=true instead of pretending NORMAL; every
report embeds its own baseline; and the drift module contains NO call path
into deployment, training, or mode changes — it observes, only.
"""

import io
import uuid as uuid_mod
from datetime import datetime

import pytest

from conftest import run_on_shared_loop as run_async

DRIFT_PATH = "/app/backend/ml/drift_service.py"


async def _ensure_db():
    from db_connection import db_manager
    if not getattr(db_manager, "_initialized", False):
        await db_manager.init_db()


# ---------------------------------------------------------------------------
# Metric functions on synthetic distributions
# ---------------------------------------------------------------------------

def _baseline():
    # 300 evenly spread points in [0, 3)
    return [i / 100.0 for i in range(300)]


def test_psi_is_quiet_on_identical_distributions():
    from backend.ml.drift_service import psi
    value = psi(_baseline(), list(_baseline()))
    assert value is not None
    assert value < 0.01, f"identical distributions should give ~0 PSI, got {value}"


def test_psi_fires_on_a_hard_shift():
    from backend.ml.drift_service import psi
    shifted = [v + 5.0 for v in _baseline()]
    value = psi(_baseline(), shifted)
    assert value is not None
    assert value > 0.25, f"a disjoint shift must exceed the critical band, got {value}"


def test_psi_refuses_degenerate_inputs():
    """One data point is not a distribution — None, never a fake number."""
    from backend.ml.drift_service import psi
    assert psi([1.0], _baseline()) is None
    assert psi(_baseline(), []) is None
    assert psi([], []) is None


def test_ks_statistic_separates_shifted_from_identical():
    from backend.ml.drift_service import ks_statistic
    same = ks_statistic(_baseline(), list(_baseline()))
    shifted = ks_statistic(_baseline(), [v + 5.0 for v in _baseline()])
    assert same is not None and shifted is not None
    assert same["statistic"] < 0.05
    assert shifted["statistic"] > 0.9
    assert shifted["p_value"] < 0.001
    assert ks_statistic([1.0], _baseline()) is None


def test_js_divergence_is_bounded_and_ordered():
    from backend.ml.drift_service import js_divergence
    import math
    same = js_divergence(_baseline(), list(_baseline()))
    disjoint = js_divergence(_baseline(), [v + 100.0 for v in _baseline()])
    assert same is not None and disjoint is not None
    assert same < 0.01
    assert disjoint > same
    assert disjoint <= math.log(2) + 1e-6, "JS divergence (nats) is bounded by ln 2"
    assert js_divergence([], []) is None


def test_severity_bands_follow_configured_thresholds():
    from backend.ml.drift_service import _severity_for_psi
    from config import settings
    warning = float(getattr(settings, "ML_DRIFT_PSI_WARNING", 0.1))
    critical = float(getattr(settings, "ML_DRIFT_PSI_CRITICAL", 0.25))
    assert _severity_for_psi(None) == "normal", "no PSI is not evidence of drift"
    assert _severity_for_psi(warning / 2) == "normal"
    assert _severity_for_psi((warning + critical) / 2) == "warning"
    assert _severity_for_psi(critical * 2) == "critical"


# ---------------------------------------------------------------------------
# Live reports: embedded baseline + insufficiency honesty
# ---------------------------------------------------------------------------

def test_run_all_persists_self_describing_reports():
    """Drift reports are ALWAYS about one model (model_id NOT NULL, scope
    global). With no shadow model nothing is written and run_all says so;
    with one, each report names it and embeds its baseline."""
    async def _run():
        from db_connection import db_manager
        from backend.ml.drift_service import drift_service
        from backend.ml.registry_service import registry_service
        from backend.ml.constants import ANOMALY_MODEL_TYPES
        from db_models import MLDriftReport
        from sqlalchemy import select
        from config import settings

        await _ensure_db()
        job_id = f"pytest-drift-{uuid_mod.uuid4().hex[:8]}"
        async with db_manager.get_session() as db:
            shadow = {}
            for mt in ANOMALY_MODEL_TYPES:
                row = await registry_service.get_stage_model(db, mt, "shadow")
                if row is not None:
                    shadow[mt] = row
            result = await drift_service.run_all(db, job_id=job_id)
            assert "never" in result["note"], "the report-only note is part of the contract"

            rows = (await db.execute(
                select(MLDriftReport).where(MLDriftReport.job_id == job_id))).scalars().all()
            if not shadow:
                assert result.get("skipped") == "NO_SHADOW_MODEL", result
                assert result["reports"] == []
                assert rows == [], "no shadow model → nothing may be written"
                return

            assert "data_drift" in result and "prediction_drift" in result
            assert len(result["reports"]) == len(shadow)
            min_samples = int(getattr(settings, "ML_DRIFT_MIN_SAMPLES", 200))
            for entry in result["reports"]:
                assert entry["model_id"] in {str(r.id) for r in shadow.values()}
                for kind in ("data_drift", "prediction_drift"):
                    report = entry[kind]
                    assert report["model_id"] == entry["model_id"]
                    assert report["scope_type"] == "global" and report["scope_id"] == ""
                    # Baseline is EMBEDDED: period + sample count travel with the report.
                    assert report["baseline_start"] and report["baseline_end"]
                    assert report["baseline_sample_count"] is not None
                    assert report["window_start"] and report["window_end"]
                    below_floor = (report["sample_count"] < min_samples
                                   or report["baseline_sample_count"] < min_samples)
                    assert report["insufficient_data"] == below_floor
                    if report["insufficient_data"]:
                        assert report["severity"] == "normal"
            assert all(r.model_id is not None for r in rows)
            kinds = sorted(r.report_kind for r in rows)
            assert kinds == sorted(["data_drift", "prediction_drift"] * len(shadow)), kinds

            # cleanup: drift reports are retention-managed, but do not leave
            # pytest noise for the admin page
            for r in rows:
                await db.delete(r)
            await db.commit()
    run_async(_run())


# ---------------------------------------------------------------------------
# Report-only: no action path exists in the module
# ---------------------------------------------------------------------------

def test_drift_module_has_no_action_path():
    """Drift must never deploy, train, relabel, or change modes. These exact
    call tokens do not exist anywhere in the module — code or comment."""
    with io.open(DRIFT_PATH, encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("registry_service.transition", "run_training_job",
                      "launch_collection_job", "apply_to_runtime",
                      "stop_shadow", "shadow_approval", "create_label"):
        assert forbidden not in source, (
            f"drift_service references {forbidden} — drift is report-only")


def test_scheduled_drift_is_delegated_to_the_durable_worker():
    with io.open("/app/backend/lifespan.py", encoding="utf-8") as handle:
        lifespan_src = handle.read()
    with io.open("/app/backend/ml/worker.py", encoding="utf-8") as handle:
        worker_src = handle.read()
    assert "scheduled_check" not in lifespan_src
    assert "delegated to the durable ML worker" in lifespan_src
    assert "_enqueue_scheduled_drift_if_due" in worker_src
    assert 'kind="drift"' in worker_src
