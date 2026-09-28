"""Selected-node/pair consumer contract; no application startup or live identities."""
import ast
import asyncio
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from conftest import ROOT
from test_hardening import endpoint

run = asyncio.run
LEFT = uuid.UUID('10000000-0000-4000-8000-000000000001')
RIGHT = uuid.UUID('10000000-0000-4000-8000-000000000002')
MODEL = uuid.UUID('20000000-0000-4000-8000-000000000001')


class ConsumerError(Exception):
    def __init__(self, code, message='Unavailable'):
        self.code = code
        self.message = message
        super().__init__(message)


@pytest.fixture
def consumer(monkeypatch):
    registry = SimpleNamespace(get_stage_model=AsyncMock(return_value=SimpleNamespace(id=MODEL)))
    scoring = AsyncMock(return_value={
        'model_id': str(MODEL), 'model_version': 'social_graph_anomaly_model-v3',
        'threshold_version': 'threshold-v2', 'score': 0.62,
        'feature_set_version': 'graph-v1', 'applied_to_live_result': False})
    registry_module = ModuleType('backend.ml.registry_service')
    registry_module.registry_service = registry
    registry_module.RegistryError = ConsumerError
    scoring_module = ModuleType('backend.ml.model_scoring_service')
    scoring_module.score_relational_subject = scoring
    monkeypatch.setitem(sys.modules, registry_module.__name__, registry_module)
    monkeypatch.setitem(sys.modules, scoring_module.__name__, scoring_module)
    identities = AsyncMock()
    bounds = []
    async def bounded(operation, awaitable):
        bounds.append(operation)
        return await awaitable
    db = SimpleNamespace(rollback=AsyncMock())
    handler = endpoint('network_model_insights', _get_identity_or_404=identities,
                       _bounded_intel_call=bounded)
    return SimpleNamespace(handler=handler, db=db, scoring=scoring, registry=registry,
                           identities=identities, bounds=bounds)


@pytest.mark.parametrize('related,family', [
    (None, 'social_graph_anomaly_model'), (RIGHT, 'coappearance_anomaly_model')])
def test_consumer_scores_only_the_selected_node_or_pair(consumer, related, family):
    result = run(consumer.handler(SimpleNamespace(identity_id=LEFT, related_identity_id=related), consumer.db))
    assert result['status'] == 'observed'
    assert result['applied_to_live_result'] is False
    assert result['observation']['model_id'] == str(MODEL)
    consumer.registry.get_stage_model.assert_awaited_once_with(consumer.db, family, 'shadow')
    assert consumer.identities.await_args_list[0].args == (consumer.db, str(LEFT))
    assert consumer.identities.await_count == (2 if related else 1)
    if related:
        assert consumer.identities.await_args_list[1].args == (consumer.db, str(RIGHT))
    kwargs = consumer.scoring.await_args.kwargs
    assert kwargs['identity_id'] == str(LEFT)
    assert kwargs['related_identity_id'] == (str(RIGHT) if related else None)
    assert kwargs['model_type'] == family
    assert kwargs['consumer'] == 'security_intelligence'
    assert kwargs['model_id'] == str(MODEL)  # selected deployment stays pinned through extraction
    assert consumer.scoring.await_count == 1
    assert consumer.bounds == ['model_insights']


@pytest.mark.parametrize('related', [None, RIGHT])
def test_no_deployed_model_does_not_extract_or_score_features(consumer, related):
    consumer.registry.get_stage_model.return_value = None
    result = run(consumer.handler(SimpleNamespace(identity_id=LEFT, related_identity_id=related), consumer.db))
    assert result['status'] == 'not_deployed'
    assert result['applied_to_live_result'] is False
    consumer.scoring.assert_not_awaited()
    assert consumer.bounds == []


def test_same_identity_pair_rejected_before_lookup(consumer):
    with pytest.raises(HTTPException) as error:
        run(consumer.handler(SimpleNamespace(identity_id=LEFT, related_identity_id=LEFT), consumer.db))
    assert error.value.status_code == 422
    consumer.identities.assert_not_awaited()
    consumer.registry.get_stage_model.assert_not_awaited()
    consumer.scoring.assert_not_awaited()


def test_missing_identity_cannot_trigger_extraction(consumer):
    consumer.identities.side_effect = HTTPException(status_code=404)
    with pytest.raises(HTTPException) as error:
        run(consumer.handler(SimpleNamespace(identity_id=LEFT, related_identity_id=None), consumer.db))
    assert error.value.status_code == 404
    consumer.registry.get_stage_model.assert_not_awaited()
    consumer.scoring.assert_not_awaited()


@pytest.mark.parametrize('reason', ['MODEL_NOT_AVAILABLE', 'THRESHOLD_UNRESOLVED',
                                   'FEATURE_SCHEMA_MISMATCH', 'MISSING_REQUIRED_FEATURES'])
def test_expected_model_failures_are_observational_unavailability(consumer, reason):
    consumer.scoring.side_effect = ConsumerError(reason)
    result = run(consumer.handler(SimpleNamespace(identity_id=LEFT, related_identity_id=None), consumer.db))
    assert result['status'] == 'unavailable'
    assert result['reason_code'] == reason
    assert result['applied_to_live_result'] is False
    assert 'observation' not in result
    consumer.db.rollback.assert_awaited_once()


def test_timeout_does_not_become_a_successful_observation(consumer):
    consumer.scoring.side_effect = HTTPException(status_code=503, detail='Timed out')
    with pytest.raises(HTTPException) as error:
        run(consumer.handler(SimpleNamespace(identity_id=LEFT, related_identity_id=None), consumer.db))
    assert error.value.status_code == 503


def test_unexpected_failure_is_sanitized(consumer):
    consumer.scoring.side_effect = RuntimeError('secret database DSN must not leave backend')
    with pytest.raises(HTTPException) as error:
        run(consumer.handler(SimpleNamespace(identity_id=LEFT, related_identity_id=None), consumer.db))
    assert error.value.status_code == 500
    assert 'secret' not in str(error.value.detail)


def test_route_declares_admin_csrf_and_heavy_rate_limit():
    tree = ast.parse((ROOT / 'backend/routes/intelligence.py').read_text())
    node = next(item for item in tree.body if isinstance(item, ast.AsyncFunctionDef)
                and item.name == 'network_model_insights')
    defaults = {arg.arg: ast.unparse(default) for arg, default in
                zip(node.args.args[-len(node.args.defaults):], node.args.defaults)}
    assert defaults['current_user'] == 'Depends(require_admin())'
    assert defaults['_csrf'] == 'Depends(require_intel_csrf)'
    assert defaults['_rl'] == "Depends(rate_limited('model_insights', heavy=True))"
    assert ast.unparse(node.decorator_list[0]).startswith("router.post('/api/security/model-insights'")


def test_request_uses_real_uuid_validation_and_pair_is_optional():
    tree = ast.parse((ROOT / 'backend/routes/intelligence.py').read_text())
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef)
                and item.name == 'ModelInsightRequest')
    from typing import Optional
    env = {'BaseModel': BaseModel, 'Optional': Optional, 'uuid_mod': uuid}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / 'backend/routes/intelligence.py'), 'exec'), env)
    schema = env['ModelInsightRequest']
    assert schema(identity_id=str(LEFT)).related_identity_id is None
    assert schema(identity_id=str(LEFT), related_identity_id=str(RIGHT)).related_identity_id == RIGHT
    with pytest.raises(ValidationError):
        schema(identity_id='arbitrary SQL or list')
    with pytest.raises(ValidationError):
        schema(identity_id=str(LEFT), related_identity_id='invalid')
