"""Provider selection is configuration, never a code change.

    LLM_PROVIDER=ollama      (default)  -> today's Ollama models, unchanged
    LLM_PROVIDER=vllm|nim_local + LLM_BASE_URL + LLM_MODEL
                                        -> a local OpenAI-compatible server,
                                           preferred for every task, RESTRICTED
                                           data allowed (it runs on the host)
    LLM_PROVIDER=nvidia_cloud           -> refused by the offline policy

No network, no model: the registry is built from a SimpleNamespace and the
provider object is constructed but never called.

    docker exec face_recognition_api python -m pytest tests/test_llm_provider_selection.py -v
"""
from types import SimpleNamespace

import pytest

from sql_agent.llm.base import DataSensitivity, ProviderUnavailable, TaskType
from sql_agent.llm.registry import build_default_registry
from sql_agent.llm.openai_compat_provider import OpenAICompatProvider


def _cfg(**overrides):
    base = dict(
        ollama_model="qwen2.5:7b", ollama_sql_model="sqlcoder:7b", ollama_timeout=120,
        ollama_interpreter_model="", is_production=True,
        llm_dev_provider="", nim_api_key="", nim_model="", nim_sql_model="", nim_timeout=60,
        nim_interpreter_model="",
        llm_provider="ollama", llm_base_url="", llm_model="", llm_sql_model="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_default_is_ollama_and_nothing_else_is_registered():
    registry = build_default_registry(_cfg())
    assert {s.provider for s in registry.all()} == {"ollama"}
    assert registry.route(TaskType.SQL_GENERATION)[0].model_id == "sqlcoder:7b"


@pytest.mark.parametrize("provider", ["vllm", "nim_local"])
def test_a_local_openai_compatible_server_is_preferred_for_every_task(provider):
    registry = build_default_registry(_cfg(
        llm_provider=provider, llm_base_url="http://vllm:8000/v1",
        llm_model="Qwen/Qwen2.5-14B-Instruct", llm_sql_model="defog/sqlcoder-7b-2"))
    providers = {s.provider for s in registry.all()}
    assert providers == {"ollama", "openai_compat"}
    assert registry.route(TaskType.SQL_GENERATION)[0].model_id == "defog/sqlcoder-7b-2"
    assert registry.route(TaskType.CHAT)[0].model_id == "Qwen/Qwen2.5-14B-Instruct"
    assert registry.route(TaskType.INTERPRETATION)[0].model_id == "Qwen/Qwen2.5-14B-Instruct"
    spec = registry.get("Qwen/Qwen2.5-14B-Instruct")
    assert spec.max_sensitivity is DataSensitivity.RESTRICTED, "it runs on the host"


def test_a_local_server_without_a_model_or_url_changes_nothing():
    for partial in (dict(llm_provider="vllm"), dict(llm_provider="vllm", llm_base_url="http://vllm:8000/v1"),
                    dict(llm_provider="vllm", llm_model="x")):
        registry = build_default_registry(_cfg(**partial))
        assert {s.provider for s in registry.all()} == {"ollama"}, partial


def test_ollama_remains_the_fallback_behind_the_local_server():
    registry = build_default_registry(_cfg(llm_provider="vllm", llm_base_url="http://vllm:8000/v1",
                                           llm_model="m"))
    order = [spec.model_id for spec in registry.route(TaskType.SQL_GENERATION)]
    assert order[0] == "m" and "sqlcoder:7b" in order


def test_the_cloud_provider_name_is_not_a_local_registration():
    registry = build_default_registry(_cfg(llm_provider="nvidia_cloud", llm_base_url="https://integrate.api.nvidia.com/v1",
                                           llm_model="meta/llama"))
    assert {s.provider for s in registry.all()} == {"ollama"}, "the hosted path is LLM_DEV_PROVIDER, refused offline"


def test_the_provider_needs_a_base_url_but_no_key():
    with pytest.raises(ProviderUnavailable):
        OpenAICompatProvider(base_url="")
    provider = OpenAICompatProvider(base_url="http://vllm:8000/v1/")
    assert provider.base_url == "http://vllm:8000/v1"
    assert provider.api_key == "local"
    model = provider.build(SimpleNamespace(model_id="m", timeout_seconds=30.0))
    assert model.model == "m" and model.base_url == "http://vllm:8000/v1"


def test_production_offline_policy_refuses_a_cloud_provider_but_accepts_vllm():
    from backend.security.offline_policy import collect_offline_violations
    cloud = SimpleNamespace(LLM_PROVIDER="nvidia_cloud", LLM_BASE_URL="https://integrate.api.nvidia.com/v1")
    codes = {f.code for f in collect_offline_violations(cloud, production=True, env={}, artifact_probe=lambda p: True)}
    assert {"OFFLINE_CLOUD_LLM_PROVIDER", "OFFLINE_EXTERNAL_LLM_ENDPOINT"} <= codes
    local = SimpleNamespace(LLM_PROVIDER="vllm", LLM_BASE_URL="http://vllm:8000/v1")
    assert not [f for f in collect_offline_violations(local, production=True, env={}, artifact_probe=lambda p: True)
                if f.severity == "fatal"]


def test_the_vector_store_factory_defaults_to_chroma_and_refuses_unknown_stores(tmp_path):
    from sql_agent import vector_store
    cfg = SimpleNamespace(vector_store="chroma", chroma_persist_dir=str(tmp_path / "chroma"),
                          chroma_collection_name="test_examples", milvus_uri="")
    collection = vector_store.open_collection(cfg, collection_metadata={"index_version": "x"})
    assert collection.count() == 0 and collection.metadata.get("index_version") == "x"
    with pytest.raises(ValueError):
        vector_store.open_collection(SimpleNamespace(vector_store="pinecone"))
    with pytest.raises((RuntimeError, ValueError)):
        vector_store.open_collection(SimpleNamespace(vector_store="milvus", milvus_uri="",
                                                     chroma_collection_name="test_examples"))
