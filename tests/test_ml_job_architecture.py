"""Production boundary checks for the durable ML control plane."""

from pathlib import Path


REPO = Path("/app") if Path("/app").exists() else Path(__file__).resolve().parents[1]


def source(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


def test_api_only_enqueues_expensive_ml_work():
    routes = source("backend/routes/ml_ops.py")
    assert "BackgroundTasks" not in routes
    assert "background_tasks.add_task" not in routes
    assert "await build_dataset(" not in routes
    assert "await backfill_dataset_file_hashes(" not in routes
    assert "await drift_service.run_all(" not in routes
    for kind in ('kind="training"', 'kind="collection"', 'kind="dataset"',
                 'kind="backfill"', 'kind="drift"'):
        assert kind in routes


def test_worker_uses_database_leases_and_child_process_isolation():
    manager = source("backend/core/task_history.py")
    worker = source("backend/ml/worker.py")
    assert "with_for_update(skip_locked=True)" in manager
    assert "renew_job_lease" in manager
    assert "fail_expired_queue_leases" in manager
    assert "create_subprocess_exec" in worker
    assert "cancel_requested" in worker
    assert 'WORKER_ID = settings.ML_WORKER_ID or' in worker
    assert '"ML_JOB_MAINTENANCE_SECONDS"' in worker


def test_queue_payload_is_not_in_the_public_job_serializer():
    manager = source("backend/core/task_history.py")
    public = manager.split("def _task_to_dict", 1)[1]
    assert '"payload": task.payload' not in public


def test_overview_projection_has_no_database_commit():
    state = source("backend/ml/system_state.py")
    assert "refresh_recorded_scientific_status" not in state
    assert "await db.commit()" not in state


def test_frontend_rehydrates_server_owned_jobs():
    js = source("frontend/js/admin-ml-ops.js")
    assert "activeJobs: new Map()" in js
    assert "api('/api/ml/jobs'" in js
    assert "data-cancel-job-id" in js or "cancelJobId" in js
    assert "JOB_POLL_MAX_BACKOFF_MS" in js
    assert "workerStatus" in js


def test_compose_runs_a_separate_non_http_ml_worker():
    for relative in ("docker/docker-compose.cpu.yml", "docker/docker-compose.prod.yml"):
        compose = source(relative)
        block = compose.split("  ml_worker:", 1)[1].split("\n  ollama:", 1)[0]
        assert 'command: ["python", "-m", "backend.ml.worker"]' in block
        assert "ports:" not in block and "expose:" not in block
