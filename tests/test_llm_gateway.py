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
    """SQL generation goes to the SQL model, and always keeps a local fallback.

    This used to assert the Ollama specialist was routed FIRST, full stop.
    That stopped being true when the development-only NIM provider was added:
    in development NIM deliberately leads, so the assertion failed in the dev
    container for a configuration that is working as designed.

    The invariant it was reaching for survives, and is checked more strictly
    here than before:

      * whatever leads, the local SQL specialist must still be routed, so
        losing the remote provider degrades to a local model rather than to
        no SQL generation at all;
      * among the LOCAL models the specialist must outrank the generic chat
        model — the ordering the original assertion was really about;
      * a remote model may lead ONLY when a dev provider is configured.
        Production has none (it is refused at boot), so there the specialist
        leads exactly as it always did.
    """
    from sql_agent.config import config
    from sql_agent.llm import build_default_registry

    reg = build_default_registry(config)
    routed = reg.route(TaskType.SQL_GENERATION)
    assert routed, "SQL generation has no route at all"

    if not (config.ollama_sql_model and config.ollama_sql_model != config.ollama_model):
        return          # no distinct specialist configured; nothing to order

    ids = [m.model_id for m in routed]
    assert config.ollama_sql_model in ids, "the local SQL specialist is not routed"
    assert config.ollama_model in ids, "no fallback"
    assert ids.index(config.ollama_sql_model) < ids.index(config.ollama_model), \
        "the generic chat model outranks the SQL specialist"

    local = [m for m in routed if getattr(m, "provider", None) == "ollama"]
    assert local and local[0].model_id == config.ollama_sql_model

    dev_provider = getattr(config, "llm_dev_provider", None)
    leader = routed[0]
    if getattr(leader, "provider", None) != "ollama":
        assert dev_provider, (
            f"a remote model ({leader.model_id}) leads SQL generation with no "
            f"development provider configured — production must never do this")
    else:
        assert leader.model_id == config.ollama_sql_model


def test_real_registry_treats_local_models_as_restricted_capable():
    """Ollama runs on-box, so it may see biometric query text."""
    from sql_agent.config import config
    from sql_agent.llm import build_default_registry

    for model in build_default_registry(config).all():
        assert model.permits(DataSensitivity.RESTRICTED)


# ---------------------------------------------------------------------------
# Timeout enforcement and the retry budget
# ---------------------------------------------------------------------------
#
# A live SQL-agent request timed out after 300s with response_chars=0. The
# stalled stage was SQL generation: a single chain.invoke() ran for 458 seconds
# against a configured OLLAMA_TIMEOUT of 120. The timeout was never enforced
# because ChatOllama (langchain_ollama 1.0.x) has NO `timeout` field and
# silently discards the kwarg. These tests pin both the enforcement route and
# the ordering of the timeout hierarchy.


def test_ollama_provider_sets_a_real_client_timeout():
    """The timeout must go through client_kwargs, where httpx enforces it.

    Passing `timeout=` to ChatOllama is silently dropped, so asserting on the
    supported route is the whole point of this test.
    """
    from sql_agent.llm.ollama_provider import OllamaProvider

    spec = ModelSpec(provider="ollama", model_id="m", display_name="m",
                     capabilities=frozenset(), context_tokens=8192,
                     timeout_seconds=120)
    model = OllamaProvider("http://ollama:11434").build(spec)

    assert "timeout" in model.client_kwargs, (
        "no client timeout — the model call would be unbounded"
    )
    timeout = model.client_kwargs["timeout"]
    assert getattr(timeout, "read", None) == 120.0
    assert getattr(timeout, "connect", None) == 20.0, (
        "connect must fail fast; waiting out the full response budget to "
        "discover the server is down helps nobody"
    )


def test_chat_ollama_still_ignores_a_bare_timeout_kwarg():
    """Guards the assumption this fix rests on.

    If a future langchain_ollama gains a real `timeout` field, this fails and
    the provider can be simplified — rather than silently keeping a workaround
    nobody remembers the reason for.
    """
    from langchain_ollama import ChatOllama

    assert "timeout" not in ChatOllama.model_fields, (
        "ChatOllama now has a timeout field; revisit OllamaProvider.build"
    )


def test_provider_honours_an_explicit_client_kwargs_timeout():
    """A caller-supplied timeout is not overwritten by the default."""
    import httpx
    from sql_agent.llm.ollama_provider import OllamaProvider

    spec = ModelSpec(provider="ollama", model_id="m", display_name="m",
                     capabilities=frozenset(), context_tokens=8192,
                     timeout_seconds=120)
    model = OllamaProvider("http://ollama:11434").build(
        spec, client_kwargs={"timeout": httpx.Timeout(5.0)}
    )
    assert model.client_kwargs["timeout"].read == 5.0


def test_llm_retry_budget_fits_inside_the_streaming_deadline():
    """All attempts together must finish before the stream gives up.

    max_retries=2 with a 120s per-call timeout permits 3 x 120s = 360s on a
    count-only policy — longer than the 300s streaming deadline that wraps it,
    so the caller would be cancelled mid-retry and the work thrown away.
    """
    from config import settings
    from sql_agent.llm.gateway import LLMGateway

    gateway = LLMGateway.__new__(LLMGateway)
    spec = ModelSpec(provider="ollama", model_id="m", display_name="m",
                     capabilities=frozenset(), context_tokens=8192,
                     timeout_seconds=120, max_retries=2)

    budget = gateway.total_budget_seconds(spec)
    outer = float(settings.SQL_AGENT_TOTAL_TIMEOUT)

    assert budget < outer, f"LLM budget {budget}s must be under the {outer}s stream deadline"
    assert budget >= spec.timeout_seconds, "budget must fit at least one attempt"
    assert budget < spec.timeout_seconds * (spec.max_retries + 1), (
        "budget must actually constrain the count-only worst case"
    )


def test_retries_stop_when_the_budget_is_spent():
    """A slow failing call is not retried into the outer deadline."""
    from sql_agent.llm.gateway import LLMGateway

    gateway = LLMGateway.__new__(LLMGateway)
    gateway.breaker = CircuitBreaker()
    gateway.ledger = UsageLedger()

    spec = ModelSpec(provider="ollama", model_id="m", display_name="m",
                     capabilities=frozenset(), context_tokens=8192,
                     timeout_seconds=120, max_retries=2)

    calls = {"n": 0}
    clock = {"t": 0.0}

    def slow_failure():
        calls["n"] += 1
        clock["t"] += 120.0        # each attempt burns its whole timeout
        raise RuntimeError("model timed out")

    import time as _time
    real_monotonic = _time.monotonic
    real_sleep = _time.sleep
    _time.monotonic = lambda: clock["t"]
    _time.sleep = lambda s: None
    try:
        with pytest.raises(RuntimeError):
            gateway.call_with_retries(spec, TaskType.SQL_GENERATION, slow_failure)
    finally:
        _time.monotonic = real_monotonic
        _time.sleep = real_sleep

    # 180s budget, 120s per attempt: the second attempt cannot fit, so exactly
    # one is made rather than the three a count-only policy would allow.
    assert calls["n"] == 1, f"expected 1 attempt within budget, made {calls['n']}"


def test_fast_failures_still_retry():
    """The budget must not disable retries for genuinely transient errors."""
    from sql_agent.llm.gateway import LLMGateway

    gateway = LLMGateway.__new__(LLMGateway)
    gateway.breaker = CircuitBreaker()
    gateway.ledger = UsageLedger()

    spec = ModelSpec(provider="ollama", model_id="m", display_name="m",
                     capabilities=frozenset(), context_tokens=8192,
                     timeout_seconds=120, max_retries=2)

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("connection reset")
        return "ok"

    import time as _time
    real_sleep = _time.sleep
    _time.sleep = lambda s: None
    try:
        assert gateway.call_with_retries(spec, TaskType.SQL_GENERATION, flaky) == "ok"
    finally:
        _time.sleep = real_sleep

    assert calls["n"] == 3, "a fast transient failure must still exhaust its retries"
