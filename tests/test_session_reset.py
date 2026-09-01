"""Starting a new conversation has to actually start one.

For a signed-in user `start_session()` reloads the persistent
`user_{id}_main` session, so POST /session/new answered

    {"success": true, "message": "New session created"}

while handing back the same accumulated conversation. There was no way, from
inside the product, to begin a clean one — the button did not do what it said.

It also made a whole day of measurement worthless. The agent and its
ConversationMemory are cached per user in the API process (`_user_agents`), so
deleting the session file from outside does nothing: the cached object writes
it straight back. A captured prompt showed the FIRST question of a supposedly
fresh run arriving with

    active_task = 'Filter the query to only include camera 3.'

left over from the run before — which makes `modify_active_query` a reasonable
answer to "how many cameras are registered?" and the measurement meaningless.

The subtle part is that the clearing must be EXPLICIT. `save_session` is a
deliberate READ-MERGE-WRITE: it preserves unknown top-level keys and merges
the working context so a concurrent turn cannot lose a field. Emptying the
fields in RAM and saving therefore writes the old context back.

    docker exec face_recognition_api python -m pytest tests/test_session_reset.py -v
"""

import json

import pytest

from sql_agent.conversation_memory import ConversationMemory


@pytest.fixture
def memory(tmp_path, monkeypatch):
    """A ConversationMemory writing into a temporary directory."""
    import sql_agent.conversation_memory as module
    monkeypatch.setattr(module.ConversationMemory, "_resolve_storage_dir",
                        lambda self, *a, **k: tmp_path, raising=False)
    instance = ConversationMemory(user_id=4242)
    instance.storage_dir = tmp_path
    instance.start_session()
    return instance


def _with_state(memory):
    memory.add_user_message("how many cameras are registered?")
    memory.add_ai_message("There are 16 cameras.")
    memory.update_working_context(
        last_artifact_id="a7a857d6-4060-4084-ae55-c90cc8f0d2dd",
        last_action="generate_document",
        dialogue_state={"fields": {
            "active_camera": {"value": [3], "source": "user_correction"},
            "active_task": {"value": "Filter to camera 3", "source": "tool_result"},
        }})
    return memory


def _on_disk(memory):
    path = memory.storage_dir / f"{memory.current_session_id}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_fixture_really_accumulates_state(memory):
    """Guard the premise: a test that clears nothing proves nothing."""
    _with_state(memory)
    stored = _on_disk(memory)

    assert stored["messages"], "no transcript was stored"
    assert stored["working_context"].get("last_artifact_id")
    assert stored["working_context"]["dialogue_state"]["fields"]["active_camera"]


def test_reset_clears_the_transcript_and_the_working_context(memory):
    """Both halves. Either one left behind still resolves "it" and "the same"."""
    _with_state(memory)
    assert memory.reset_session() is True

    assert memory.messages == []
    assert memory.working_context == {}

    stored = _on_disk(memory)
    assert stored["messages"] == []
    assert stored["message_count"] == 0
    assert stored["working_context"] == {}, (
        "reset must REPLACE the document; a merge would keep the old context")


def test_a_later_save_does_not_resurrect_the_old_context(memory):
    """THE subtle one. `save_session` merges by design.

    Clearing the fields in RAM and calling save would write the old working
    context straight back, because the merge reads what is on disk first. The
    reset has to replace the document, and the next ordinary save must not
    undo it.
    """
    _with_state(memory)
    memory.reset_session()

    memory.add_user_message("how many cameras are registered?")
    memory.save_session()

    context = _on_disk(memory)["working_context"]
    # `migrate_working_context` re-adds the schema keys set to None, which is
    # fine — an empty slot is not stale state. What must not come back is a
    # VALUE, so assert on values rather than on key presence.
    stale = {key: value for key, value in context.items()
             if key != "version" and value not in (None, {}, [], "")}
    assert not stale, f"the merge resurrected: {stale}"


def test_reset_is_safe_with_no_session(tmp_path, monkeypatch):
    """It must never raise; a reset that fails loudly is worse than a no-op."""
    instance = ConversationMemory(user_id=4243)
    instance.storage_dir = tmp_path
    instance.current_session_id = None

    assert instance.reset_session() is True
    assert instance.messages == []


def test_reset_leaves_other_sessions_alone(memory, tmp_path):
    """Clearing one conversation must not touch anybody else's."""
    other = tmp_path / "user_9999_main.json"
    other.write_text(json.dumps({
        "session_id": "user_9999_main",
        "messages": [{"type": "human", "content": "not mine"}],
        "working_context": {"last_artifact_id": "keep-me"},
    }), encoding="utf-8")

    _with_state(memory)
    memory.reset_session()

    survived = json.loads(other.read_text(encoding="utf-8"))
    assert survived["working_context"]["last_artifact_id"] == "keep-me"
    assert survived["messages"]


def test_the_endpoint_resets_rather_than_reloading():
    """`/session/new` must call the reset, not just start_session.

    Read from the source: the behavioural version of this needs the running
    stack, and the property worth pinning is that the endpoint cannot quietly
    go back to reporting a new session it did not create.
    """
    import ast
    import inspect
    import textwrap

    import sql_agent.api.routes as routes

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(routes.sql_agent_new_session)))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)}

    assert "reset_session" in called, (
        "/session/new reloads the persistent session and calls it new")
