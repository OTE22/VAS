"""Offline policy: development may go online, production must not.

Pure and in-process, like tests/test_config_guard.py: the rules read a
SimpleNamespace through getattr, take env and the artifact probe as explicit
arguments, and never touch os.environ, the network or the filesystem.

    docker exec face_recognition_api python -m pytest tests/test_offline_policy.py -v
"""
from types import SimpleNamespace

import pytest

from backend.security import offline_policy as op
from backend.security.config_guard import codes_of, collect_violations


def _cfg(**overrides):
    base = dict(
        ENVIRONMENT="production",
        OFFLINE_MODE="",
        LLM_PROVIDER="ollama",
        OLLAMA_BASE_URL="http://ollama:11434",
        LLM_BASE_URL="",
        EMBEDDING_PROVIDER="local",
        EMBEDDING_BASE_URL="",
        MCP_SQL_URL="",
        MILVUS_URI="",
        STT_PROVIDER="none",
        STT_BASE_URL="",
        OTEL_EXPORTER_ENDPOINT="",
        OPIK_URL_OVERRIDE="",
        SQL_AGENT_OPIK_ENABLED=False,
        LLM_DEV_PROVIDER="",
        NVIDIA_NIM_API_KEY="",
        ALLOW_EXTERNAL_APIS=False,
        ALLOW_MODEL_DOWNLOADS=False,
        ALLOW_EXTERNAL_TELEMETRY=False,
        EMBEDDING_MODEL_PATH="/models/minilm/model.onnx",
        DETECTION_MODEL="/app/weights/det_10g.onnx",
        RECOGNITION_MODEL="/app/weights/w600k_r50.onnx",
        STT_MODEL_PATH="",
        OFFLINE_BUNDLE_MANIFEST="",
        OFFLINE_ALLOWED_HOSTS="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


PRESENT = lambda path: True          # every artifact exists
ENV_OK = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}


def _codes(cfg, production=True, env=ENV_OK, probe=PRESENT):
    return [f.code for f in op.collect_offline_violations(
        cfg, production=production, env=env, artifact_probe=probe)]


# ------------------------------------------------------------- host classing

@pytest.mark.parametrize("url", [
    "http://ollama:11434", "http://vllm:8000/v1", "http://localhost:9901/mcp",
    "http://127.0.0.1:4317", "http://10.0.0.5:19530", "http://192.168.1.20:8000/v1",
    "http://172.16.0.9:8000", "https://llm.corp.internal/v1", "http://[::1]:8000",
    "http://milvus.lan:19530", "host.docker.internal:8000",
])
def test_internal_urls_are_internal(url):
    assert op.classify_url(url) == "internal", url


@pytest.mark.parametrize("url", [
    "https://integrate.api.nvidia.com/v1", "https://api.openai.com/v1",
    "https://api.anthropic.com", "https://huggingface.co/models/x",
    "https://generativelanguage.googleapis.com", "https://api.cohere.ai/v1",
    "https://example.com/v1", "https://llm.vendor.io/v1", "https://cdn.jsdelivr.net/npm/x",
])
def test_public_urls_are_external(url):
    assert op.classify_url(url) == "external", url


def test_an_operator_approved_host_inside_the_network_is_internal():
    assert op.classify_url("https://gpu-node.example.com/v1", ["gpu-node.example.com"]) == "internal"
    assert op.classify_url("https://api.openai.com/v1", ["api.openai.com"]) == "internal", \
        "the allow-list is the operator's responsibility; the rule honours it"


# --------------------------------------------------------- development mode

def test_development_allows_cloud_llm_downloads_and_external_endpoints():
    cfg = _cfg(ENVIRONMENT="development", OFFLINE_MODE="false",
               LLM_PROVIDER="nvidia_cloud", LLM_DEV_PROVIDER="nim",
               LLM_BASE_URL="https://integrate.api.nvidia.com/v1",
               EMBEDDING_PROVIDER="openai", EMBEDDING_BASE_URL="https://api.openai.com/v1",
               OTEL_EXPORTER_ENDPOINT="https://otel.vendor.io",
               ALLOW_EXTERNAL_APIS=True, ALLOW_MODEL_DOWNLOADS=True,
               ALLOW_EXTERNAL_TELEMETRY=True, STT_PROVIDER="external")
    assert _codes(cfg, production=False, env={}, probe=lambda p: False) == []


def test_development_allows_local_llm_too():
    cfg = _cfg(ENVIRONMENT="development", OFFLINE_MODE="false",
               LLM_PROVIDER="vllm", LLM_BASE_URL="http://localhost:8000/v1")
    assert _codes(cfg, production=False, env={}) == []


def test_development_can_opt_into_offline_enforcement():
    cfg = _cfg(ENVIRONMENT="development", OFFLINE_MODE="true",
               LLM_BASE_URL="https://api.openai.com/v1")
    assert "OFFLINE_EXTERNAL_LLM_ENDPOINT" in _codes(cfg, production=False)


# ---------------------------------------------------------- production mode

def test_a_clean_production_configuration_passes():
    assert _codes(_cfg()) == []
    assert all(c.passed for c in op.startup_checklist(_cfg(), production=True, env=ENV_OK,
                                                       artifact_probe=PRESENT))


def test_production_defaults_to_offline_and_refuses_offline_false():
    assert op.offline_mode(_cfg(), production=True) is True
    assert "OFFLINE_MODE_DISABLED_IN_PRODUCTION" in _codes(_cfg(OFFLINE_MODE="false"))


@pytest.mark.parametrize("key,url,code", [
    ("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1", "OFFLINE_EXTERNAL_LLM_ENDPOINT"),
    ("OLLAMA_BASE_URL", "https://api.openai.com/v1", "OFFLINE_EXTERNAL_LLM_ENDPOINT"),
    ("LLM_BASE_URL", "https://api.anthropic.com/v1", "OFFLINE_EXTERNAL_LLM_ENDPOINT"),
    ("EMBEDDING_BASE_URL", "https://api.openai.com/v1/embeddings", "OFFLINE_EXTERNAL_EMBEDDING_ENDPOINT"),
    ("OTEL_EXPORTER_ENDPOINT", "https://otel.vendor.io:4317", "OFFLINE_EXTERNAL_TELEMETRY_ENDPOINT"),
    ("OPIK_URL_OVERRIDE", "https://www.comet.com/opik/api", "OFFLINE_EXTERNAL_TELEMETRY_ENDPOINT"),
    ("MCP_SQL_URL", "https://mcp.vendor.io/mcp", "OFFLINE_EXTERNAL_MCP_ENDPOINT"),
    ("MILVUS_URI", "https://in03.api.zillizcloud.com", "OFFLINE_EXTERNAL_VECTOR_STORE_ENDPOINT"),
    ("STT_BASE_URL", "https://api.openai.com/v1/audio", "OFFLINE_EXTERNAL_STT_ENDPOINT"),
])
def test_production_rejects_every_external_endpoint(key, url, code):
    findings = op.collect_offline_violations(_cfg(**{key: url}), production=True, env=ENV_OK,
                                             artifact_probe=PRESENT)
    assert any(f.code == code and f.key == key for f in findings), [f.code for f in findings]
    message = next(f.message for f in findings if f.code == code)
    assert "PRODUCTION OFFLINE POLICY VIOLATION" in message and url in message


def test_production_rejects_cloud_providers_and_the_dev_provider():
    assert "OFFLINE_CLOUD_LLM_PROVIDER" in _codes(_cfg(LLM_PROVIDER="nvidia_cloud"))
    assert "OFFLINE_CLOUD_LLM_PROVIDER" in _codes(_cfg(LLM_PROVIDER="openai"))
    assert "OFFLINE_DEV_PROVIDER_SET" in _codes(_cfg(LLM_DEV_PROVIDER="nim"))
    assert "OFFLINE_EXTERNAL_EMBEDDING_PROVIDER" in _codes(_cfg(EMBEDDING_PROVIDER="openai"))
    assert "OFFLINE_EXTERNAL_STT_PROVIDER" in _codes(_cfg(STT_PROVIDER="external"))
    assert "OFFLINE_OPIK_ENABLED" in _codes(_cfg(SQL_AGENT_OPIK_ENABLED=True))


@pytest.mark.parametrize("flag", ["ALLOW_EXTERNAL_APIS", "ALLOW_MODEL_DOWNLOADS", "ALLOW_EXTERNAL_TELEMETRY"])
def test_production_rejects_the_permission_flags(flag):
    assert f"OFFLINE_{flag}" in _codes(_cfg(**{flag: True}))


def test_production_local_providers_are_accepted():
    for provider in ("ollama", "vllm", "nim_local"):
        cfg = _cfg(LLM_PROVIDER=provider, LLM_BASE_URL="http://vllm:8000/v1")
        assert _codes(cfg) == [], provider


def test_production_requires_the_local_model_artifacts():
    missing = lambda path: not path.endswith("model.onnx")
    findings = op.collect_offline_violations(_cfg(), production=True, env=ENV_OK, artifact_probe=missing)
    assert [f.key for f in findings if f.code == "OFFLINE_ARTIFACT_MISSING"] == ["EMBEDDING_MODEL_PATH"]
    checks = {c.name: c for c in op.startup_checklist(_cfg(), production=True, env=ENV_OK,
                                                       artifact_probe=missing)}
    assert checks["required model artifacts found"].passed is False
    assert checks["offline policy validated"].passed is False


def test_a_missing_bundle_manifest_is_fatal_when_configured():
    cfg = _cfg(OFFLINE_BUNDLE_MANIFEST="/bundle/manifest.json")
    absent = lambda path: not path.endswith("manifest.json")
    assert "OFFLINE_BUNDLE_MANIFEST_MISSING" in _codes(cfg, probe=absent)


def test_credentials_and_library_switches_are_warnings_not_fatal():
    findings = op.collect_offline_violations(_cfg(NVIDIA_NIM_API_KEY="nvapi-x"),
                                             production=True, env={"HF_TOKEN": "hf_x"},
                                             artifact_probe=PRESENT)
    codes = {(f.code, f.severity) for f in findings}
    assert ("OFFLINE_CLOUD_CREDENTIAL_PRESENT", "warn") in codes
    assert ("OFFLINE_LIBRARY_SWITCH_UNSET", "warn") in codes
    assert not [f for f in findings if f.severity == "fatal"]


def test_the_checklist_reads_like_the_runbook():
    text = op.format_checklist(op.startup_checklist(
        _cfg(LLM_BASE_URL="https://api.openai.com/v1"), production=True, env=ENV_OK,
        artifact_probe=PRESENT, reachability={"PostgreSQL": (True, ""), "local LLM": (False, "refused")}))
    assert "[FAIL] no external inference endpoint configured" in text
    assert "[PASS] PostgreSQL reachable" in text
    assert "[FAIL] local LLM reachable - refused" in text
    assert "[FAIL] offline policy validated" in text


# ------------------------------------------------- wired into the config guard

def test_the_config_guard_carries_offline_findings_as_fatal_violations():
    from tests.test_config_guard import _GOOD  # the guard's own clean production config
    cfg = SimpleNamespace(**dict(_GOOD, LLM_BASE_URL="https://integrate.api.nvidia.com/v1",
                                 ALLOW_MODEL_DOWNLOADS=True))
    codes = codes_of(collect_violations(cfg, env=ENV_OK))
    assert "OFFLINE_EXTERNAL_LLM_ENDPOINT" in codes
    assert "OFFLINE_ALLOW_MODEL_DOWNLOADS" in codes


def test_the_guards_clean_production_config_stays_clean():
    from tests.test_config_guard import _GOOD
    cfg = SimpleNamespace(**_GOOD)
    offline_codes = [c for c in codes_of(collect_violations(cfg, env=ENV_OK)) if c.startswith("OFFLINE_")]
    assert offline_codes == [], offline_codes
