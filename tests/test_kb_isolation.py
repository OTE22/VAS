"""
Knowledge-base tenant isolation.

    docker exec face_recognition_api python -m pytest tests/test_kb_isolation.py -v

The regression: after any query where the database did not raise, the agent
wrote the user's raw question and the model's SQL into a single shared
ChromaDB collection, and retrieval was unfiltered. Retrieved examples are
interpolated into the SQL-generation *system prompt*, so:

  * one user's question text — which in this deployment routinely names
    people — became another user's prompt content, and
  * a crafted question became a persistent prompt-injection vector, replayed
    into every later user's prompt.

"Success" also only meant psycopg2 did not raise, so wrong-but-valid SQL was
learned as an exemplar.

Curated seed examples remain shared by design; learned entries are visible
only to the user who produced them.
"""

import uuid

import pytest

from sql_agent.config import config
from sql_agent.knowledge_base import SQLKnowledgeBase

OWNER = 4242
STRANGER = 9999


@pytest.fixture(scope="module")
def kb():
    return SQLKnowledgeBase(config)


@pytest.fixture
def learned_entry(kb):
    """A learned entry owned by OWNER, removed afterwards."""
    question = f"where was Jane Doe seen on Tuesday {uuid.uuid4().hex[:8]}"
    entry_id = kb.learn_from_success(
        question=question, sql="SELECT 1", purpose="isolation probe", user_id=OWNER
    )
    yield question, entry_id
    try:
        kb.collection.delete(ids=[entry_id])
    except Exception:
        pass


def questions(rows):
    return [row["question"] for row in rows]


# ------------------------------------------------------------- isolation

def test_owner_can_retrieve_their_own_learned_entry(kb, learned_entry):
    question, _ = learned_entry
    assert question in questions(kb.search_similar(question, top_k=5, user_id=OWNER))


def test_another_user_cannot_retrieve_it(kb, learned_entry):
    """The core leak: this text would otherwise enter a stranger's prompt."""
    question, _ = learned_entry
    assert question not in questions(kb.search_similar(question, top_k=5, user_id=STRANGER))


def test_absent_user_id_means_seeds_only_not_everything(kb, learned_entry):
    """The shared global agent instance carries no user id, so an unfiltered
    search there would expose every user's learned entries."""
    question, _ = learned_entry
    rows = kb.search_similar(question, top_k=5)
    assert question not in questions(rows)
    assert {row["source"] for row in rows} <= {"seed"}


def test_curated_seed_examples_stay_shared(kb):
    for user_id in (OWNER, STRANGER, None):
        rows = kb.search_similar("how many faces were detected today", top_k=5, user_id=user_id)
        assert rows, f"seed examples must remain available (user_id={user_id})"


# ------------------------------------------------------------- provenance

def test_learning_without_an_owner_is_refused(kb):
    """An untagged entry cannot be scoped, so it would be readable by all."""
    assert kb.learn_from_success(question="orphan", sql="SELECT 1") == ""


def test_learned_entries_record_their_owner(kb, learned_entry):
    _, entry_id = learned_entry
    stored = kb.collection.get(ids=[entry_id], include=["metadatas"])
    metadata = stored["metadatas"][0]
    assert metadata["user_id"] == str(OWNER)
    assert metadata["source"] == "learned"


# --------------------------------------------------------- fail closed

def test_a_failed_filter_returns_nothing_rather_than_everything(kb, monkeypatch):
    """A broken filter must not silently widen the search to all users."""
    def explode(**kwargs):
        raise RuntimeError("malformed where clause")

    monkeypatch.setattr(kb.collection, "query", explode)
    assert kb.search_similar("anything", top_k=5, user_id=OWNER) == []


# ------------------------------------------------------------- wiring

def _tools_source():
    with open("/app/sql_agent/tools/agent_tools.py", encoding="utf-8") as handle:
        return handle.read()


def test_retrieval_node_passes_the_caller():
    source = _tools_source()
    start = source.index("examples = self.kb.search_similar(")
    assert 'user_id=state.get("user_id")' in source[start:start + 400]


def test_learning_node_passes_the_caller():
    source = _tools_source()
    start = source.index("self.kb.learn_from_success(")
    assert 'user_id=state.get("user_id")' in source[start:start + 400]


def test_agent_populates_user_id_from_conversation_memory():
    with open("/app/sql_agent/agent.py", encoding="utf-8") as handle:
        source = handle.read()
    start = source.index("def _create_initial_state")
    assert 'getattr(self.conversation_memory, "user_id", None)' in source[start:start + 800]
