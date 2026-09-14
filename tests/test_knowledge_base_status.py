"""Streaming progress must match the knowledge-base switch."""
from types import SimpleNamespace

import pytest

from sql_agent.agent import SQLIntelligenceAgent
from config import settings


@pytest.mark.parametrize("enabled", [False, True])
def test_retrieval_status_respects_knowledge_base_switch(monkeypatch, enabled):
    monkeypatch.setattr(settings, "SQL_AGENT_USE_KNOWLEDGE_BASE", enabled)
    agent = SQLIntelligenceAgent.__new__(SQLIntelligenceAgent)
    agent.conversation_memory = SimpleNamespace(get_conversation_context=lambda **kwargs: "")
    agent._durable_memory = ""
    agent._create_initial_state = lambda *args, **kwargs: {}
    agent._graph_config = lambda *args: None
    agent.agent = SimpleNamespace(stream=lambda *args, **kwargs: iter([
        {"retrieve_examples": {"retrieved_examples": [], "rag_context": ""}},
        {"generate_sql": {}},
    ]))
    events = []
    stream = agent.query_stream("Count detections")
    try:
        for event in stream:
            events.append(event)
            if event.get("step") == "generate_sql":
                break
    finally:
        stream.close()
    assert events[-1].get("step") == "generate_sql"
    retrieval = [event for event in events if event.get("step") == "rag"]
    assert bool(retrieval) is enabled
    assert any("Retrieving similar examples" in event.get("message", "") for event in events) is enabled


@pytest.mark.parametrize("enabled", [False, True])
def test_learning_status_respects_learning_switch(monkeypatch, enabled):
    monkeypatch.setattr(settings, "SQL_AGENT_LEARN_FROM_QUERIES", enabled)
    agent = SQLIntelligenceAgent.__new__(SQLIntelligenceAgent)
    agent.conversation_memory = SimpleNamespace(get_conversation_context=lambda **kwargs: "")
    agent._durable_memory = ""
    agent._create_initial_state = lambda *args, **kwargs: {}
    agent._graph_config = lambda *args: None
    agent.agent = SimpleNamespace(stream=lambda *args, **kwargs: iter([
        {"learn_from_query": {"should_learn": enabled}},
        {"generate_sql": {}},
    ]))
    events = []
    stream = agent.query_stream("Count detections")
    try:
        for event in stream:
            events.append(event)
            if event.get("step") == "generate_sql":
                break
    finally:
        stream.close()
    assert bool([event for event in events if event.get("step") == "learn"]) is enabled
