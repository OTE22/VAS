"""Development-only hosted LLM provider (NVIDIA NIM) — the production lock.

    docker exec face_recognition_api python -m pytest tests/test_llm_dev_provider.py -v

The SQL agent may use build.nvidia.com's free OpenAI-compatible endpoint in
DEVELOPMENT to judge query quality against a stronger model. In production the
prompts carry the database schema and user questions about biometric data, and
the system is documented as running fully offline — so production must be
structurally unable to use it, not merely configured away from it.

Three independent layers are proven here, none trusting the others:

  1. the model registry registers no NIM model when the config says
     production, so routing cannot select one, whatever else is set;
  2. the config guard fails a production boot with LLM_DEV_PROVIDER set,
     with no acknowledgement escape;
  3. LLM_DEV_PROVIDER is SECURITY_CRITICAL, so the admin settings API cannot
     persist it for a later boot, and the API key is redacted everywhere
     settings are rendered.

Pure and in-process: no Ollama, no NIM, no network. The registry reads its cfg
through plain attributes and collect_violations() reads through getattr(), so
SimpleNamespace stands in for Settings.
"""

from types import SimpleNamespace

import pytest

from backend.security.config_guard import (
    SECURITY_CRITICAL_KEYS,
    collect_violations,
    fatal_only,
)
from backend.security.redaction import SECRET_SETTINGS
from sql_agent.llm.base import DataSensitivity, ProviderUnavailable, TaskType
from sql_agent.llm.registry import build_default_registry


def agent_cfg(**overrides):
    """A sql_agent.config.Config stand-in with the registry's inputs."""
    base = dict(
        ollama_model="qwen2.5:1.5b",
        ollama_sql_model="",
        ollama_timeout=120,
        is_production=False,
        llm_dev_provider="",
        nim_base_url="https://integrate.api.nvidia.com/v1",
        nim_api_key="",
        nim_model="meta/llama-3.2-11b-vision-instruct",
        nim_sql_model="openai/gpt-oss-120b",
        nim_timeout=60,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def providers_of(registry):
    return {spec.provider for spec in registry.all()}


# ---------------------------------------------------------------------------
# Layer 1: the registry
# ---------------------------------------------------------------------------

def test_default_configuration_is_ollama_only():
    """Nothing set: local models only, in every environment."""
    for production in (False, True):
        registry = build_default_registry(agent_cfg(is_production=production))
        assert providers_of(registry) == {"ollama"}


def test_dev_with_provider_and_key_registers_nim_and_prefers_it():
    registry = build_default_registry(agent_cfg(
        llm_dev_provider="nim", nim_api_key="nvapi-test"))
    assert providers_of(registry) == {"ollama", "nim"}

    routed = registry.route(TaskType.SQL_GENERATION,
                            sensitivity=DataSensitivity.RESTRICTED)
    assert routed, "SQL routing must yield candidates"
    assert routed[0].provider == "nim", "the point of enabling NIM is to use it"
    assert any(spec.provider == "ollama" for spec in routed), \
        "Ollama must remain the fallback when the endpoint misbehaves"


def test_production_registers_no_nim_model_even_fully_configured():
    """The load-bearing property: a key present in production changes NOTHING.

    Routing cannot select a model that was never registered, so even a caller
    that lowers the sensitivity gets local models only."""
    registry = build_default_registry(agent_cfg(
        is_production=True, llm_dev_provider="nim", nim_api_key="nvapi-test"))
    assert providers_of(registry) == {"ollama"}

    for sensitivity in DataSensitivity:
        for task in (TaskType.SQL_GENERATION, TaskType.CHAT):
            routed = registry.route(task, sensitivity=sensitivity)
            assert all(spec.provider == "ollama" for spec in routed)


def test_dev_without_a_key_registers_nothing():
    """The flag alone is not enough — a keyless provider would register a
    model that can never build, and every SQL task would burn its retry
    budget on it before falling back."""
    registry = build_default_registry(agent_cfg(llm_dev_provider="nim"))
    assert providers_of(registry) == {"ollama"}


def test_sql_specialist_split_mirrors_ollama():
    registry = build_default_registry(agent_cfg(
        llm_dev_provider="nim", nim_api_key="nvapi-test"))
    nim_ids = {s.model_id for s in registry.all() if s.provider == "nim"}
    assert nim_ids == {"meta/llama-3.2-11b-vision-instruct",
                       "openai/gpt-oss-120b"}
    routed = registry.route(TaskType.SQL_GENERATION)
    assert routed[0].model_id == "openai/gpt-oss-120b"
    chat = registry.route(TaskType.CHAT)
    assert chat[0].model_id == "meta/llama-3.2-11b-vision-instruct"


def test_identical_general_and_sql_model_yields_no_duplicate_candidates():
    registry = build_default_registry(agent_cfg(
        llm_dev_provider="nim", nim_api_key="nvapi-test",
        nim_sql_model=""))   # falls back to nim_model
    routed = registry.route(TaskType.SQL_GENERATION)
    ids = [s.model_id for s in routed]
    assert len(ids) == len(set(ids)), f"duplicate candidates: {ids}"


# ---------------------------------------------------------------------------
# Layer 2: the config guard
# ---------------------------------------------------------------------------

def guard_codes(**overrides):
    cfg = SimpleNamespace(**overrides)
    return {v.code for v in collect_violations(cfg, environment="production")}


def test_production_boot_fails_with_the_dev_provider_set():
    codes = guard_codes(LLM_DEV_PROVIDER="nim")
    assert "LLM_EXTERNAL_PROVIDER_IN_PRODUCTION" in codes

    fatal = {v.code for v in fatal_only(
        collect_violations(SimpleNamespace(LLM_DEV_PROVIDER="nim"),
                           environment="production"))}
    assert "LLM_EXTERNAL_PROVIDER_IN_PRODUCTION" in fatal, \
        "must block the boot, not merely warn"


def test_any_nonempty_provider_value_is_refused_not_just_nim():
    """The rule guards the class of misconfiguration, not one spelling."""
    assert "LLM_EXTERNAL_PROVIDER_IN_PRODUCTION" in guard_codes(
        LLM_DEV_PROVIDER="openai")


def test_a_stray_api_key_in_production_warns_without_blocking():
    violations = collect_violations(
        SimpleNamespace(NVIDIA_NIM_API_KEY="nvapi-forgotten"),
        environment="production")
    codes = {v.code for v in violations}
    assert "LLM_EXTERNAL_API_KEY_PRESENT" in codes
    assert "LLM_EXTERNAL_API_KEY_PRESENT" not in {
        v.code for v in fatal_only(violations)}, \
        "an unused credential is a hygiene warning, not a boot blocker"


def test_development_is_unaffected():
    cfg = SimpleNamespace(LLM_DEV_PROVIDER="nim", NVIDIA_NIM_API_KEY="nvapi-x")
    codes = {v.code for v in collect_violations(cfg, environment="development")}
    assert "LLM_EXTERNAL_PROVIDER_IN_PRODUCTION" not in codes
    assert "LLM_EXTERNAL_API_KEY_PRESENT" not in codes


# ---------------------------------------------------------------------------
# Layer 3: runtime-change and rendering surfaces
# ---------------------------------------------------------------------------

def test_the_switch_cannot_arrive_through_the_settings_api():
    """Not SECURITY_CRITICAL, an admin token could persist LLM_DEV_PROVIDER
    to the settings table and the next boot would apply it — flipping the
    provider without ever touching the environment."""
    for key in ("LLM_DEV_PROVIDER", "NVIDIA_NIM_API_KEY", "NVIDIA_NIM_BASE_URL"):
        assert key in SECURITY_CRITICAL_KEYS, key


def test_the_api_key_is_redacted_wherever_settings_render():
    assert "NVIDIA_NIM_API_KEY" in SECRET_SETTINGS


def test_the_key_never_reaches_the_admin_settings_page():
    """Follows the WEBHOOK_API_KEYS precedent: a secret kept out of the
    category map is never listed, never persisted, never rendered."""
    source = open("/app/backend/routes/settings.py", encoding="utf-8").read()
    for key in ("NVIDIA_NIM_API_KEY", "LLM_DEV_PROVIDER"):
        assert key not in source, f"{key} must not be exposed on the settings page"


# ---------------------------------------------------------------------------
# The provider adapter itself (no network)
# ---------------------------------------------------------------------------

def test_build_without_a_key_raises_provider_unavailable():
    from sql_agent.llm.base import Capability, ModelSpec
    from sql_agent.llm.nim_provider import NIMProvider

    provider = NIMProvider(base_url="https://integrate.api.nvidia.com/v1",
                           api_key="")
    spec = ModelSpec(provider="nim", model_id="meta/llama-3.2-11b-vision-instruct",
                     display_name="test",
                     capabilities=frozenset({Capability.STREAMING}),
                     context_tokens=32768)
    with pytest.raises(ProviderUnavailable):
        provider.build(spec)


def test_message_conversion_and_payload_shape():
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    from sql_agent.llm.nim_provider import NIMChatModel, _to_openai_messages

    converted = _to_openai_messages([
        SystemMessage(content="you are a SQL assistant"),
        HumanMessage(content="how many detections today?"),
        AIMessage(content="SELECT count(*) ..."),
    ])
    assert [m["role"] for m in converted] == ["system", "user", "assistant"]

    model = NIMChatModel(base_url="https://example.invalid/v1",
                         model="m", api_key="nvapi-secret-value", temperature=0.2,
                         timeout_seconds=30.0)
    payload = model._payload(
        [HumanMessage(content="q")], stop=["\n\n"], stream=False, max_tokens=64)
    assert payload["model"] == "m"
    assert payload["stream"] is False
    assert payload["stop"] == ["\n\n"]
    assert payload["max_tokens"] == 64
    assert payload["messages"] == [{"role": "user", "content": "q"}]
    # The key travels only in the Authorization header, never the body.
    assert "nvapi-secret-value" not in str(payload)


def test_generate_parses_an_openai_response_and_reports_usage(monkeypatch):
    """The gateway's token accounting reads usage_metadata first; a provider
    that fails to populate it silently zeroes the usage ledger."""
    import httpx
    from langchain_core.messages import HumanMessage

    from sql_agent.llm import nim_provider

    body = {
        "model": "meta/llama-3.2-11b-vision-instruct",
        "choices": [{"message": {"role": "assistant",
                                 "content": "SELECT 1"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 21, "completion_tokens": 4,
                  "total_tokens": 25},
    }

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers=None, json=None):
            assert url.endswith("/chat/completions")
            assert headers["Authorization"] == "Bearer nvapi-test"
            return httpx.Response(200, json=body,
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(nim_provider.httpx, "Client", FakeClient)

    model = nim_provider.NIMChatModel(
        base_url="https://example.invalid/v1", model="meta/llama-3.2-11b-vision-instruct",
        api_key="nvapi-test", timeout_seconds=5.0)
    result = model._generate([HumanMessage(content="one?")])

    message = result.generations[0].message
    assert message.content == "SELECT 1"
    assert message.usage_metadata["input_tokens"] == 21
    assert message.usage_metadata["output_tokens"] == 4


def test_the_production_compose_file_never_mentions_the_dev_provider():
    """The prod stack must not even pass the variables through: strict Ollama.
    (Template: the ENVIRONMENT assertions in test_webhook_auth.py.)"""
    source = open("/app/docker/docker-compose.prod.yml", encoding="utf-8").read()
    for token in ("LLM_DEV_PROVIDER", "NVIDIA_NIM"):
        assert token not in source, \
            f"{token} appears in the production compose file"
