"""SQL_AGENT_USE_KNOWLEDGE_BASE=false runs a turn with no retrieved examples.

Asked for on 2026-09-08 to measure what the verified seeds contribute: the
same question, with and without them. RAG_TOP_K cannot express it, because
`SQLKnowledgeBase.search_similar` clamps top_k with max(1, ...) - zero still
returns one example.
"""
from config import Settings
from sql_agent.tools import agent_tools


def test_the_setting_exists_and_defaults_to_on():
    field = Settings.model_fields["SQL_AGENT_USE_KNOWLEDGE_BASE"]
    assert field.default is True, "retrieval stays on unless an operator turns it off"


def test_top_k_alone_cannot_disable_retrieval():
    # Why the switch had to exist: the knowledge base floors top_k at one.
    import inspect

    from sql_agent.knowledge_base import SQLKnowledgeBase

    source = inspect.getsource(SQLKnowledgeBase.search_similar)
    assert "max(1," in source


def _tools(monkeypatch, enabled, searched):
    # retrieve_examples degrades on ANY exception, so a raising stub would be
    # swallowed and prove nothing. Record the call instead.
    from types import SimpleNamespace
    tools = agent_tools.SQLAgentTools.__new__(agent_tools.SQLAgentTools)
    tools.kb = SimpleNamespace(format_examples_for_prompt=lambda examples: "")
    monkeypatch.setattr(agent_tools.settings, "SQL_AGENT_USE_KNOWLEDGE_BASE", enabled)
    monkeypatch.setattr(tools, "_retrieve_through_catalogue",
                        lambda *a, **k: searched.append(1) or [], raising=False)
    return tools


def test_a_disabled_knowledge_base_never_searches_and_yields_no_context(monkeypatch):
    searched = []
    tools = _tools(monkeypatch, False, searched)
    state = tools.retrieve_examples({"normalized_input": "how many detections yesterday"})
    assert searched == [], "the knowledge base must not be searched while disabled"
    assert state["retrieved_examples"] == []
    assert state["rag_context"] == ""


def test_an_enabled_knowledge_base_still_searches(monkeypatch):
    searched = []
    tools = _tools(monkeypatch, True, searched)
    tools.retrieve_examples({"normalized_input": "how many detections yesterday"})
    assert searched, "retrieval must still run when the switch is on"


def test_global_config_observes_boot_hydrated_knowledge_base_setting(monkeypatch):
    from config import settings
    from sql_agent.config import config
    monkeypatch.setattr(settings, "SQL_AGENT_USE_KNOWLEDGE_BASE", False)
    assert config.use_knowledge_base is False
    monkeypatch.setattr(settings, "SQL_AGENT_USE_KNOWLEDGE_BASE", True)
    assert config.use_knowledge_base is True
