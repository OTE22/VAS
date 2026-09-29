"""Service workflow contract; isolated fixtures only, no production DB or jobs."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest
from backend.ml import service_deployment as service
from backend.ml import model_scoring_service as scoring
from backend.ml.registry_service import RegistryError

BEHAVIOR = "behavior_anomaly_model"
RANKING = "threat_ranking_model"


def model(model_type=BEHAVIOR, **changes):
    values = dict(id=uuid.uuid4(), model_type=model_type, stage="validated", version=2,
        artifact_hash="a" * 64, artifact_path="/tmp/test-only-model", dependency_versions={},
        feature_names=["first"], feature_set_version="secintel-features-v2",
        quality_gates={"passed": True}, evaluation_report={"engineering_gate": {"status": "PASS"}, "splits": {name: {"rows": 40, "positive": 20, "negative": 20} for name in ("train", "val", "test")}},
        training_config={}, code_version="a"*40, dataset_id=uuid.uuid4(), created_at=datetime(2026, 1, 1))
    values.update(changes)
    return SimpleNamespace(**values)


def run(coro):
    return asyncio.run(coro)


def test_no_model_does_not_claim_ready():
    assert service.deployment_blockers(None, BEHAVIOR)[0]["code"] == "NO_MODEL"


def test_offline_regression_never_becomes_live_service():
    assert service.deployment_blockers(model(), "tabular_regression_model")[0]["code"] == "OFFLINE_ONLY"


@pytest.mark.parametrize("changes,expected", [
    ({"feature_set_version": "wrong"}, "FEATURE_SCHEMA_MISMATCH"),
    ({"quality_gates": {"passed": False}}, "ENGINEERING_REVIEW_REQUIRED"),
    ({"evaluation_report": {}, "training_config": {}}, "ENGINEERING_REVIEW_REQUIRED"),
])
def test_review_never_infers_unknown_readiness(changes, expected):
    assert expected in [x["code"] for x in service.deployment_blockers(model(**changes), BEHAVIOR)]


def test_observation_does_not_require_or_claim_scientific_evidence():
    row = model()
    assert service.deployment_blockers(row, BEHAVIOR) == []
    assert service.model_summary(row)["scientific_gate"] == "NOT_RECORDED"


def test_summary_does_not_expose_server_artifact_path():
    assert "artifact_path" not in service.model_summary(model())


def deploy(row, **kwargs):
    return service.deploy_service(SimpleNamespace(), row.model_type, model_id=str(row.id),
        artifact_hash=kwargs.get("artifact_hash", row.artifact_hash), reason="reviewed fixture",
        actor="test admin", actor_id=99)


def test_checksum_mismatch_refuses_before_registry_mutation():
    row = model()
    with patch.object(service.registry_service, "get_model", AsyncMock(return_value=row)), \
         patch.object(service.registry_service, "transition", AsyncMock()) as transition:
        with pytest.raises(RegistryError, match="reviewed model changed"):
            run(deploy(row, artifact_hash="b" * 64))
        transition.assert_not_awaited()


def test_wrong_family_refused():
    row = model()
    with patch.object(service.registry_service, "get_model", AsyncMock(return_value=model(RANKING))):
        with pytest.raises(RegistryError, match="trained for this service"):
            run(deploy(row))


def test_shadow_deployment_uses_registry_and_records_previous_selection():
    row, prior = model(), model(stage="shadow")
    with patch.object(service.registry_service, "get_model", AsyncMock(return_value=row)), \
         patch.object(service.registry_service, "get_stage_model", AsyncMock(return_value=prior)), \
         patch.object(service.registry_service, "transition", AsyncMock(return_value={"id": str(row.id)})) as transition:
        result = run(deploy(row))
    args = transition.await_args.kwargs
    assert args["to_stage"] == "shadow"
    assert args["shadow_approval"]["previous_model_id"] == str(prior.id)
    assert args["shadow_approval"]["intended_scope"] == "all_pipelines"
    assert not result["decision_mode_changed"]


def test_ranking_service_uses_reviewed_approval_not_shadow():
    row = model(RANKING)
    with patch.object(service.registry_service, "get_model", AsyncMock(return_value=row)), \
         patch.object(service.registry_service, "get_stage_model", AsyncMock(return_value=None)), \
         patch.object(service.registry_service, "transition", AsyncMock(return_value={})) as transition:
        run(deploy(row))
    assert transition.await_args.kwargs["to_stage"] == "approved"


def test_stop_requires_current_selected_model():
    row = model(stage="shadow")
    with patch.object(service.registry_service, "get_stage_model", AsyncMock(return_value=row)), \
         patch.object(service.registry_service, "transition", AsyncMock()) as transition:
        with pytest.raises(RegistryError, match="selected model changed"):
            run(service.stop_service(SimpleNamespace(), BEHAVIOR, model_id=str(uuid.uuid4()),
                reason="fixture stop", actor="tester", actor_id=99))
        transition.assert_not_awaited()


def test_ranking_default_resolves_approved_not_newest_candidate():
    row = model(RANKING, stage="approved")
    with patch.object(scoring.registry_service, "get_stage_model", AsyncMock(return_value=row)) as lookup, \
         patch.object(scoring, "validate_artifact", return_value={"feature_set_version": row.feature_set_version}):
        result, _, _ = run(scoring._load_model(None, RANKING, None))
    assert result is row
    assert lookup.await_args.args[2] == "approved"


def test_explicit_candidate_remains_testable():
    row = model(RANKING)
    with patch.object(scoring.registry_service, "get_model", AsyncMock(return_value=row)), \
         patch.object(scoring, "validate_artifact", return_value={"feature_set_version": row.feature_set_version}):
        result, _, _ = run(scoring._load_model(None, RANKING, str(row.id)))
    assert result is row


def test_missing_approved_ranker_gives_actionable_failure_without_fallback_candidate():
    with patch.object(scoring.registry_service, "get_stage_model", AsyncMock(return_value=None)):
        result = run(scoring.rank_identities(None, [str(uuid.uuid4())]))
    assert result["scored"] == 0
    assert result["items"][0]["error_code"] == "MODEL_NOT_AVAILABLE"
    assert result["model_id"] is None


def test_candidate_test_is_not_recorded_as_service_consumption():
    from backend.ml import audit
    result = dict(model_id=str(uuid.uuid4()), model_type=RANKING, model_version="v1", feature_set_version="test")
    with patch.object(audit, "ml_audit", AsyncMock()) as record:
        run(scoring._record_consumption(None, result, candidate_test=True))
    assert record.await_args.kwargs["action"] == "ml_candidate_tested"


def test_service_observation_records_exact_version_and_schema():
    from backend.ml import audit
    result = dict(model_id=str(uuid.uuid4()), model_type=RANKING, model_version="v9", feature_set_version="test", latency_ms=10)
    with patch.object(audit, "ml_audit", AsyncMock()) as record:
        run(scoring._record_consumption(None, result))
    args = record.await_args.kwargs
    assert args["object_id"] == result["model_id"]
    assert args["after"]["model_version"] == "v9"
    assert args["after"]["applied_to_live_result"] is False


@patch("backend.ml.workflow_policy.training_readiness", new_callable=AsyncMock, return_value={"ready": True, "blockers": []})
def test_status_counts_and_next_action_are_service_scoped(_readiness):
    from backend.ml.decision_service import decision_service
    from backend.ml.labeling_service import labeling_service
    candidate = model(RANKING)
    class DB:
        def __init__(self):
            self.results = iter([
                [("person", "secintel-features-v2", 42), ("pair", "coappearance-features-v1", 3)],
                [("threat_ranking_person_labeled", "secintel-features-v2", 1)],
                [(RANKING, 1)],
            ])
        async def execute(self, _query):
            data = next(self.results)
            return SimpleNamespace(all=lambda: data)
    async def lookup(db, model_type, stage):
        return candidate if model_type == RANKING and stage == "validated" else None
    with patch.object(decision_service, "mode_availability", AsyncMock(return_value={"current_mode": "rules", "modes": {}})), \
         patch.object(labeling_service, "label_stats", AsyncMock(return_value={"supervised_gate_open": False})), \
         patch.object(service.registry_service, "get_stage_model", lookup), \
         patch.object(service, "_consumption", AsyncMock(return_value={})):
        output = run(service.service_status(DB()))
    by_type = {row["model_type"]: row for row in output["items"]}
    assert by_type[BEHAVIOR]["next_action"] == "prepare_train"
    assert by_type[BEHAVIOR]["counts"]["datasets"] == 0
    assert by_type[RANKING]["counts"]["datasets"] == 1
    assert by_type[RANKING]["candidate"]["id"] == str(candidate.id)
    assert by_type["coappearance_anomaly_model"]["counts"]["snapshots"] == 3
    assert by_type["social_graph_anomaly_model"]["counts"]["snapshots"] == 0
    assert by_type["tabular_regression_model"]["next_action"] == "advanced"


@patch("backend.ml.workflow_policy.training_readiness", new_callable=AsyncMock, return_value={"ready": True, "blockers": []})
def test_selected_model_without_artifact_cannot_claim_ready(_readiness):
    from backend.ml.decision_service import decision_service
    from backend.ml.labeling_service import labeling_service
    from backend.ml.threshold_service import threshold_service
    row = model(stage="shadow")
    async def lookup(db, model_type, stage):
        return row if model_type == BEHAVIOR and stage == "shadow" else None
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
    with patch.object(decision_service, "mode_availability", AsyncMock(return_value={"current_mode": "shadow", "modes": {}})), \
         patch.object(labeling_service, "label_stats", AsyncMock(return_value={"supervised_gate_open": True})), \
         patch.object(service.registry_service, "get_stage_model", lookup), \
         patch.object(service, "_consumption", AsyncMock(return_value={})), \
         patch.object(threshold_service, "get_active", AsyncMock(return_value=None)), \
         patch.object(service.os.path, "isfile", return_value=False):
        output = run(service.service_status(db))
    row = output["items"][0]
    assert row["state"] == "falling_back"
    assert {b["code"] for b in row["blockers"]} == {"ARTIFACT_MISSING", "THRESHOLD_UNRESOLVED"}


def test_behavior_history_query_only_counts_real_assessments():
    statements = []
    class DB:
        async def execute(self, query):
            statements.append(str(query))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))
    output = run(service._consumption(DB(), BEHAVIOR, None))
    assert all("assessment_id IS NOT NULL" in q for q in statements)
    assert output["last_success_at"] is None
    assert output["source"] == "ml_predictions"


def test_ml_ops_test_does_not_claim_security_screen_used_model():
    row = model("social_graph_anomaly_model", stage="shadow")
    event = SimpleNamespace(created_at=datetime(2026, 1, 1), object_id=str(row.id),
        after={"consumer": "ml_ops"})
    class DB:
        def __init__(self): self.count = 0
        async def execute(self, query):
            self.count += 1
            value = (event, row.version) if self.count == 1 else None
            return SimpleNamespace(first=lambda: value)
    output = run(service._consumption(DB(), row.model_type, row))
    assert output["last_test_at"]
    assert output["last_success_at"] is None
    assert output["selected_model_used"] is False


# Database tests below use the harness's explicit disposable DSN only.
from conftest import engine as isolated_engine, session as isolated_session


@pytest.fixture
def service_database():
    if isolated_engine is None:
        pytest.skip("Disposable PostgreSQL not supplied")
    from sqlalchemy import text
    from db_models import Base, MLDataset
    from conftest import settings
    settings.ML_GRAPH_MIN_EDGES = 50
    settings.ML_GRAPH_MIN_NODES = 25
    settings.ML_GRAPH_MIN_OBSERVATION_DAYS = 14
    async def setup():
        async with isolated_engine.begin() as db:
            await db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await db.run_sync(Base.metadata.create_all)
            await db.execute(text("TRUNCATE ml_audit_log, ml_models, ml_datasets, ml_feature_snapshots CASCADE"))
        async with isolated_session() as db:
            db.add(MLDataset(id=uuid.UUID('dddddddd-1111-4111-8111-111111111111'), name='service-contract-fixture', version=1, kind='supervised', feature_set_version='secintel-features-v2', checksum='a'*64))
    run(setup())


def db_model(version, *, stage="validated", model_type=RANKING):
    from db_models import MLModel
    return MLModel(id=uuid.uuid4(), model_type=model_type, version=version,
        stage=stage, algorithm="logreg", model_purpose="analyst_review_ranking",
        score_type="risk_rank_score", is_probability=False,
        calibration_status="not_calibrated", artifact_name=f"fixture-v{version}.pkl",
        artifact_path=f"/tmp/nonexistent-fixture-v{version}.pkl", artifact_hash="a" * 64,
        dependency_versions={}, feature_set_version="secintel-features-v2", feature_names=["first"],
        quality_gates={"passed": True}, evaluation_report={"engineering_gate": {"status": "PASS"}, "splits": {name: {"rows": 40, "positive": 20, "negative": 20} for name in ("train", "val", "test")}},
        training_config={}, code_version='a'*40, dataset_id=uuid.UUID('dddddddd-1111-4111-8111-111111111111'), created_at=datetime.utcnow())


def test_database_approved_selection_replaces_atomically_and_stop_does_not_revive_old(service_database):
    from sqlalchemy import select
    from db_models import MLModel, MLAuditLog
    from backend.ml import registry_service as registry_module
    async def check():
        async with isolated_session() as db:
            old, new = db_model(10, stage="approved"), db_model(2)
            db.add_all([old, new])
            await db.commit()
            new_id, old_id = new.id, old.id
        with patch.object(registry_module, "validate_artifact", return_value={}):
            async with isolated_session() as db:
                await service.deploy_service(db, RANKING, model_id=str(new_id), artifact_hash="a" * 64,
                    reason="explicit older reviewed version", actor="isolated", actor_id=99)
        async with isolated_session() as db:
            selected = await service.registry_service.get_stage_model(db, RANKING, "approved")
            assert selected.id == new_id  # max version is deliberately not the selection
            assert (await db.get(MLModel, old_id)).stage == "archived"
            assert len((await db.execute(select(MLAuditLog).where(MLAuditLog.object_id == str(old_id), MLAuditLog.action == "model_archived"))).scalars().all()) == 1
            await service.stop_service(db, RANKING, model_id=str(new_id), reason="fixture stop",
                actor="isolated", actor_id=99)
        async with isolated_session() as db:
            assert await service.registry_service.get_stage_model(db, RANKING, "approved") is None
    run(check())


def test_database_refused_artifact_preserves_existing_binding(service_database):
    from db_models import MLModel
    from backend.ml import registry_service as registry_module
    async def check():
        async with isolated_session() as db:
            old, new = db_model(1, stage="approved"), db_model(2)
            db.add_all([old, new]); await db.commit()
            old_id, new_id = old.id, new.id
        with patch.object(registry_module, "validate_artifact", side_effect=RegistryError("ARTIFACT_HASH_MISMATCH", "fixture hash mismatch")):
            async with isolated_session() as db:
                with pytest.raises(RegistryError):
                    await service.deploy_service(db, RANKING, model_id=str(new_id), artifact_hash="a" * 64,
                        reason="fixture replacement", actor="isolated", actor_id=99)
                await db.rollback()
        async with isolated_session() as db:
            assert (await db.get(MLModel, old_id)).stage == "approved"
            assert (await db.get(MLModel, new_id)).stage == "validated"
    run(check())


def test_database_status_queries_keep_family_and_actual_consumer_separate(service_database):
    from db_models import MLFeatureSnapshot, MLAuditLog
    from backend.ml.decision_service import decision_service
    from backend.ml.labeling_service import labeling_service
    async def check():
        async with isolated_session() as db:
            pair = db_model(1, stage="shadow", model_type="coappearance_anomaly_model")
            pair.feature_set_version = "coappearance-features-v1"
            db.add(pair)
            db.add(MLFeatureSnapshot(entity_type="pair", entity_id="fixture-pair",
                feature_set_version="coappearance-features-v1", as_of_timestamp=datetime.utcnow(), features={"first": 1}))
            await db.flush()
            db.add(MLAuditLog(action="ml_service_consumed", object_type="ml_model", object_id=str(pair.id),
                after={"consumer": "ml_ops", "model_type": pair.model_type}))
            await db.commit()
        with patch.object(decision_service, "mode_availability", AsyncMock(return_value={"current_mode": "rules", "modes": {}})), \
             patch.object(labeling_service, "label_stats", AsyncMock(return_value={"supervised_gate_open": False})):
            async with isolated_session() as db:
                status = await service.service_status(db)
        items = {r["model_type"]: r for r in status["items"]}
        assert items["coappearance_anomaly_model"]["counts"]["snapshots"] == 1
        assert items[BEHAVIOR]["counts"]["snapshots"] == 0
        consumption = items["coappearance_anomaly_model"]["consumption"]
        assert consumption["last_test_at"]
        assert consumption["last_success_at"] is None
        assert not consumption["selected_model_used"]
    run(check())


def test_scoring_refuses_snapshot_from_another_feature_schema():
    row = model(RANKING)
    spec = service.get_model_spec(RANKING)
    with patch.object(scoring, "_load_model", AsyncMock(return_value=(row, {}, spec))), \
         patch.object(scoring, "score_with_payload") as score:
        with pytest.raises(RegistryError, match="snapshot does not match"):
            run(scoring._score_snapshot(None, model_type=RANKING,
                snapshot={"feature_set_version": "other-schema", "features": {}}))
        score.assert_not_called()
