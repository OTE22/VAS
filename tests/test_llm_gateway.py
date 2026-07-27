"""
LLM provider abstraction: routing, resilience and accounting.

    docker exec face_recognition_api python -m pytest tests/test_llm_gateway.py -v

Before this, sql_agent/llm.py was 31 lines constructing ChatOllama directly in
two factories. The provider was welded into the call path: swapping it meant
editing every site, and there was nowhere to put timeouts, retries, fallback
or token accounting. A single SQL question issues five to six sequential model
calls, none of which were measured.

Pure unit tests — no Ollama, no network. Providers are doubles.
"""

import pytest

from sql_agent.llm import (
    Capability,
    CircuitBreaker,
    DataSensitivity,
    LLMGateway,
    ModelRegistry,
    ModelSpec,
    ProviderUnavailable,
    TaskType,
    UsageLedger,
)

LOCAL = "local-model"
HOSTED = "hosted-model"


def spec(model_id, *, provider="fake", sensitivity=DataSensitivity.RESTRICTED,
         available=True, retries=2, caps=None):
    return ModelSpec(
        provider=provider,
        model_id=model_id,
        display_name=model_id,
        capabilities=frozenset(caps or {Capability.STREAMING}),
        context_tokens=8192,
        max_sensitivity=sensitivity,
        max_retries=retries,
        available=available,
    )


class FakeModel:
    def __init__(self, fail_times=0, exc=RuntimeError("provider down")):
        self.fail_times = fail_times
        self.calls = 0
        self._exc = exc

    def invoke(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self._exc
        return "ok"

    def stream(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self._exc
        yield "chunk"


class FakeProvider:
    name = "fake"

    def __init__(self, model=None, build_error=None):
        self.model = model or FakeModel()
        self.build_error = build_error
        self.built = []

    def build(self, model_spec, **_overrides):
        if self.build_error:
            raise self.build_error
        self.built.append(model_spec.model_id)
        return self.model

    def health_check(self):
        return True


def gateway(registry, provider, **kw):
    return LLMGateway(registry, {"fake": provider}, **kw)


@pytest.fixture
def registry():
    reg = ModelRegistry()
    reg.register(spec(LOCAL))
    reg.register(spec(HOSTED, sensitivity=DataSensitivity.PUBLIC))
    reg.prefer(TaskType.SQL_GENERATION, [LOCAL, HOSTED])
    return reg


# ------------------------------------------------------------- routing

def test_preferred_model_is_chosen_first(registry):
    assert registry.route(TaskType.SQL_GENERATION)[0].model_id == LOCAL


def test_unavailable_models_are_skipped():
    reg = ModelRegistry()
    reg.register(spec(LOCAL, available=False))
    reg.register(spec(HOSTED))
    reg.prefer(TaskType.CHAT, [LOCAL, HOSTED])
    assert [m.model_id for m in reg.route(TaskType.CHAT)] == [HOSTED]


def test_capability_requirements_filter_candidates():
    reg = ModelRegistry()
    reg.register(spec(LOCAL, caps={Capability.STREAMING}))
    reg.register(spec(HOSTED, caps={Capability.STREAMING, Capability.VISION}))
    routed = reg.route(TaskType.CHAT, required=[Capability.VISION])
    assert [m.model_id for m in routed] == [HOSTED]


# --------------------------------------------------- data sensitivity

def test_restricted_content_excludes_lower_clearance_models(registry):
    """This deployment queries biometric data; a model that would send prompt
    text off-box must never serve RESTRICTED work."""
    routed = registry.route(TaskType.SQL_GENERATION, sensitivity=DataSensitivity.RESTRICTED)
    assert HOSTED not in [m.model_id for m in routed]
    assert LOCAL in [m.model_id for m in routed]


def test_public_content_may_use_any_model(registry):
    routed = registry.route(TaskType.SQL_GENERATION, sensitivity=DataSensitivity.PUBLIC)
    assert {m.model_id for m in routed} == {LOCAL, HOSTED}


def test_requested_model_cannot_widen_the_data_policy(registry):
    """A user preference must not override the sensitivity rule."""
    routed = registry.route(
        TaskType.SQL_GENERATION,
        sensitivity=DataSensitivity.RESTRICTED,
        requested_model=HOSTED,
    )
    assert HOSTED not in [m.model_id for m in routed]


def test_no_eligible_model_raises(registry):
    reg = ModelRegistry()
    reg.register(spec(HOSTED, sensitivity=DataSensitivity.PUBLIC))
    g = gateway(reg, FakeProvider())
    with pytest.raises(ProviderUnavailable):
        g.build_for(TaskType.CHAT, sensitivity=DataSensitivity.RESTRICTED)


# ------------------------------------------------------------ fallback

def test_falls_back_to_the_next_model_when_the_first_cannot_build():
    reg = ModelRegistry()
    reg.register(spec(LOCAL, provider="broken"))
    reg.register(spec(HOSTED))
    reg.prefer(TaskType.CHAT, [LOCAL, HOSTED])

    working = FakeProvider()
    g = LLMGateway(reg, {"broken": FakeProvider(build_error=RuntimeError("boom")),
                         "fake": working})
    g.build_for(TaskType.CHAT, sensitivity=DataSensitivity.PUBLIC)
    assert working.built == [HOSTED]


def test_fallback_is_logged_not_silent(registry, caplog):
    """Which model answered changes how much the result should be trusted."""
    reg = ModelRegistry()
    reg.register(spec(LOCAL, provider="broken"))
    reg.register(spec(HOSTED))
    reg.prefer(TaskType.CHAT, [LOCAL, HOSTED])
    g = LLMGateway(reg, {"broken": FakeProvider(build_error=RuntimeError("boom")),
                         "fake": FakeProvider()})
    with caplog.at_level("WARNING"):
        g.build_for(TaskType.CHAT, sensitivity=DataSensitivity.PUBLIC)
    assert any("fell back" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------- retries

def test_transient_failures_are_retried(registry):
    model = FakeModel(fail_times=2)
    g = gateway(registry, FakeProvider(model))
    built = g.build_for(TaskType.SQL_GENERATION)
    assert built.invoke("prompt") == "ok"
    assert model.calls == 3


def test_retries_are_bounded(registry):
    reg = ModelRegistry()
    reg.register(spec(LOCAL, retries=1))
    model = FakeModel(fail_times=99)
    g = gateway(reg, FakeProvider(model))
    built = g.build_for(TaskType.CHAT)
    with pytest.raises(RuntimeError):
        built.invoke("prompt")
    assert model.calls == 2  # initial + 1 retry


def test_streaming_is_not_retried(registry):
    """Chunks may already have reached the client; a retry would duplicate
    visible output."""
    model = FakeModel(fail_times=1)
    g = gateway(registry, FakeProvider(model))
    built = g.build_for(TaskType.CHAT)
    with pytest.raises(RuntimeError):
        list(built.stream("prompt"))
    assert model.calls == 1


# ------------------------------------------------------ circuit breaker

def test_circuit_opens_after_repeated_failures():
    breaker = CircuitBreaker(failure_threshold=3, reset_after_seconds=60)
    for _ in range(3):
        breaker.record_failure("fake/model")
    assert breaker.is_open("fake/model") is True


def test_success_closes_the_circuit():
    breaker = CircuitBreaker(failure_threshold=2)
    breaker.record_failure("fake/model")
    breaker.record_success("fake/model")
    breaker.record_failure("fake/model")
    assert breaker.is_open("fake/model") is False


def test_circuit_half_opens_after_the_reset_window():
    breaker = CircuitBreaker(failure_threshold=1, reset_after_seconds=0)
    breaker.record_failure("fake/model")
    assert breaker.is_open("fake/model") is False  # probe allowed through


def test_open_circuit_skips_the_model(registry):
    breaker = CircuitBreaker(failure_threshold=1)
    breaker.record_failure(f"fake/{LOCAL}")
    provider = FakeProvider()
    g = gateway(registry, provider, breaker=breaker)
    g.build_for(TaskType.SQL_GENERATION, sensitivity=DataSensitivity.PUBLIC)
    assert provider.built == [HOSTED]  # the open one was skipped


# ----------------------------------------------------------- accounting

def test_successful_calls_are_recorded(registry):
    ledger = UsageLedger()
    g = gateway(registry, FakeProvider(), ledger=ledger)
    g.build_for(TaskType.SQL_GENERATION).invoke("prompt")
    totals = ledger.totals()
    assert totals["calls"] == 1
    assert totals["failures"] == 0


def test_failed_calls_are_recorded(registry):
    ledger = UsageLedger()
    g = gateway(registry, FakeProvider(FakeModel(fail_times=99)), ledger=ledger)
    with pytest.raises(RuntimeError):
        g.build_for(TaskType.SQL_GENERATION).invoke("prompt")
    assert ledger.totals()["failures"] == 1


def test_ledger_is_bounded():
    ledger = UsageLedger(max_records=5)
    from sql_agent.llm import LLMCallRecord

    for _ in range(20):
        ledger.record(LLMCallRecord(provider="fake", model_id=LOCAL, task="chat"))
    assert ledger.totals()["calls"] == 5


def test_token_usage_costs_are_computed():
    from sql_agent.llm import TokenUsage

    priced = ModelSpec(
        provider="fake", model_id="m", display_name="m",
        capabilities=frozenset(), context_tokens=1000,
        input_cost_per_1k=1.0, output_cost_per_1k=2.0,
    )
    usage = TokenUsage(prompt_tokens=1000, completion_tokens=500)
    assert usage.total_tokens == 1500
    assert usage.cost(priced) == pytest.approx(2.0)


def test_local_models_cost_nothing(registry):
    assert registry.get(LOCAL).input_cost_per_1k == 0.0


# -------------------------------------------------------- composition

def test_wrapper_preserves_lcel_piping(registry):
    """The graph nodes build `prompt | llm | parser`; the wrapper must not
    break that or every call site would need rewriting."""
    g = gateway(registry, FakeProvider())
    built = g.build_for(TaskType.CHAT)
    assert hasattr(built, "__or__")
    assert hasattr(built, "invoke")
    assert hasattr(built, "stream")


def test_public_factories_still_exist():
    """Call sites were not rewritten; the seam is behind these."""
    from sql_agent.llm import create_llm, create_sql_llm

    assert callable(create_llm)
    assert callable(create_sql_llm)


def test_real_registry_prefers_the_sql_specialist():
    from sql_agent.config import config
    from sql_agent.llm import build_default_registry

    reg = build_default_registry(config)
    routed = reg.route(TaskType.SQL_GENERATION)
    if config.ollama_sql_model and config.ollama_sql_model != config.ollama_model:
        assert routed[0].model_id == config.ollama_sql_model
        assert config.ollama_model in [m.model_id for m in routed], "no fallback"


def test_real_registry_treats_local_models_as_restricted_capable():
    """Ollama runs on-box, so it may see biometric query text."""
    from sql_agent.config import config
    from sql_agent.llm import build_default_registry

    for model in build_default_registry(config).all():
        assert model.permits(DataSensitivity.RESTRICTED)
