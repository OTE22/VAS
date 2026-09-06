"""The reader has its own model task.

The interpreter's one reading per turn decides everything downstream, and a
small reader flips on short follow-ups, so a deployment binds
OLLAMA_INTERPRETER_MODEL (or NVIDIA_NIM_INTERPRETER_MODEL in development)
to a stronger model than the chat one. Unset, the reader is the chat order.

    docker exec face_recognition_api python -m pytest tests/test_interpreter_model_task.py -v
"""

from types import SimpleNamespace

from sql_agent.llm.base import TaskType
from sql_agent.llm.registry import build_default_registry


def _cfg(**overrides):
    base = dict(ollama_model="qwen2.5:1.5b", ollama_sql_model="", ollama_timeout=120,
                ollama_interpreter_model="", is_production=True,
                llm_dev_provider="", nim_api_key="", nim_model="", nim_sql_model="",
                nim_interpreter_model="", nim_timeout=60)
    base.update(overrides)
    return SimpleNamespace(**base)


def _order(registry, task):
    return [spec.model_id for spec in registry._ordered(task)]


def test_unset_the_reader_is_the_chat_model():
    registry = build_default_registry(_cfg())
    assert _order(registry, TaskType.INTERPRETATION) == _order(registry, TaskType.CHAT)


def test_a_configured_reader_is_registered_and_preferred_with_chat_as_fallback():
    registry = build_default_registry(_cfg(ollama_interpreter_model="qwen2.5:7b"))
    order = _order(registry, TaskType.INTERPRETATION)
    assert order[0] == "qwen2.5:7b" and "qwen2.5:1.5b" in order
    assert registry.get("qwen2.5:7b").display_name.endswith("(reader)")
    # Chat is untouched: the reader is not preferred for other tasks.
    assert _order(registry, TaskType.CHAT)[0] == "qwen2.5:1.5b"


def test_the_development_reader_comes_first_in_development_only():
    dev = _cfg(is_production=False, llm_dev_provider="nim", nim_api_key="k",
               nim_model="meta/llama-3.2-11b-vision-instruct",
               nim_interpreter_model="meta/llama-3.3-70b-instruct")
    order = _order(build_default_registry(dev), TaskType.INTERPRETATION)
    assert order[0] == "meta/llama-3.3-70b-instruct"
    assert "qwen2.5:1.5b" in order

    prod = _cfg(is_production=True, llm_dev_provider="nim", nim_api_key="k",
                nim_model="meta/llama-3.2-11b-vision-instruct",
                nim_interpreter_model="meta/llama-3.3-70b-instruct")
    order = _order(build_default_registry(prod), TaskType.INTERPRETATION)
    assert not any(m.startswith("meta/") for m in order), "hosted models never in production"
