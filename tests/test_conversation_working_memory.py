"""Working memory: the session FILE is the source of truth.

    docker exec face_recognition_api python -m pytest tests/test_conversation_working_memory.py -v

"the last report", "same query", "make it Arabic" all depend on state that
must outlive the in-process agent object. The agent instance is an LRU-cached
cache; a container restart, an eviction, or a future WORKERS>1 must not lose
what "it" refers to. These tests therefore always assert through a NEW
ConversationMemory instance reading the same directory — never through the
object that did the writing.

They also pin the two failure modes this module has already had:

  * save_session used to rebuild the document from four fixed fields, so any
    other key was destroyed by the next autosave (which fires on EVERY
    message). test_unknown_top_level_keys_survive is the regression net.
  * a plain json.dump left a truncated file if anything interrupted it, and a
    truncated session file loses the entire conversation, not one turn.
"""

import json
import os
import threading

import pytest
from pathlib import Path

from sql_agent.conversation_memory import (
    WORKING_CONTEXT_VERSION,
    ConversationMemory,
    migrate_working_context,
)


@pytest.fixture()
def memory(tmp_path):
    mem = ConversationMemory(storage_dir=str(tmp_path), user_id=7)
    mem.start_session()
    return mem


def reopen(memory) -> ConversationMemory:
    """A fresh instance over the same storage — simulates restart/eviction."""
    fresh = ConversationMemory(storage_dir=str(memory.storage_dir.parent), user_id=7)
    fresh.start_session()
    return fresh


# ---------------------------------------------------------------------------
# Persistence and rehydration
# ---------------------------------------------------------------------------

def test_working_context_survives_a_restart(memory):
    memory.update_working_context(last_artifact_id="a-1", last_action="generate_document")

    restarted = reopen(memory)
    ctx = restarted.get_working_context()
    assert ctx["last_artifact_id"] == "a-1"
    assert ctx["last_action"] == "generate_document"


def test_autosave_after_a_message_preserves_working_context(memory):
    memory.update_working_context(last_artifact_id="a-2")
    memory.add_user_message("hello")      # autosaves
    memory.add_ai_message("hi")           # autosaves again

    assert reopen(memory).get_working_context()["last_artifact_id"] == "a-2", \
        "an autosave rebuilt the file and dropped the working context"


def test_unknown_top_level_keys_survive(memory):
    """The destroy-on-save regression, generalized.

    A key this class knows nothing about — written by a future feature, or by
    another process — must still be present after this class saves.
    """
    session_file = memory.storage_dir / f"{memory.current_session_id}.json"
    memory.save_session()
    data = json.loads(session_file.read_text(encoding="utf-8"))
    data["some_future_field"] = {"written_by": "another feature"}
    session_file.write_text(json.dumps(data), encoding="utf-8")

    memory.add_user_message("triggers a save")

    after = json.loads(session_file.read_text(encoding="utf-8"))
    assert after["some_future_field"] == {"written_by": "another feature"}


def test_unknown_working_context_keys_survive(memory):
    memory.update_working_context(experimental_key="keep me")
    assert reopen(memory).get_working_context()["experimental_key"] == "keep me"


def test_old_session_file_without_working_context_still_loads(tmp_path):
    """Files written before this feature must not break on load."""
    user_dir = tmp_path / "user_7"
    user_dir.mkdir(parents=True)
    (user_dir / "user_7_main.json").write_text(json.dumps({
        "session_id": "user_7_main",
        "created_at": "2026-01-01T00:00:00",
        "message_count": 0,
        "messages": [],
    }), encoding="utf-8")

    mem = ConversationMemory(storage_dir=str(tmp_path), user_id=7)
    mem.start_session()
    ctx = mem.get_working_context()
    assert ctx["version"] == WORKING_CONTEXT_VERSION
    assert ctx["last_artifact_id"] is None


# ---------------------------------------------------------------------------
# Concurrency and atomicity
# ---------------------------------------------------------------------------

def test_concurrent_updates_do_not_lose_a_field(memory):
    """Two turns writing DIFFERENT fields must both survive.

    Read-merge-write without a lock loses whichever write lands first.
    """
    errors = []

    def writer(field, value):
        try:
            for _ in range(25):
                memory.update_working_context(**{field: value})
        except Exception as exc:              # pragma: no cover - diagnostic
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=("last_artifact_id", "artifact-A")),
        threading.Thread(target=writer, args=("selected_entity", "IRON MAN")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    ctx = reopen(memory).get_working_context()
    assert ctx["last_artifact_id"] == "artifact-A"
    assert ctx["selected_entity"] == "IRON MAN", "a concurrent write was lost"


def test_session_file_is_always_valid_json_under_concurrent_writes(memory):
    """A reader must never observe a half-written document."""
    session_file = memory.storage_dir / f"{memory.current_session_id}.json"
    memory.add_user_message("seed")
    failures = []

    def writer():
        for i in range(40):
            memory.update_working_context(last_query=f"q{i}")

    def reader():
        for _ in range(80):
            try:
                raw = session_file.read_text(encoding="utf-8")
                if raw:
                    json.loads(raw)
            except json.JSONDecodeError as exc:
                failures.append(exc)
            except FileNotFoundError:
                pass          # the replace window; the old file is simply gone

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not failures, "a partially written session file was observed"


# ---------------------------------------------------------------------------
# Versioning and result hygiene
# ---------------------------------------------------------------------------

def test_migration_adds_version_and_known_keys():
    ctx = migrate_working_context({"last_artifact_id": "x"})
    assert ctx["version"] == WORKING_CONTEXT_VERSION
    assert ctx["last_artifact_id"] == "x"
    assert "selected_entity" in ctx and ctx["selected_entity"] is None


def test_migration_tolerates_garbage():
    assert migrate_working_context(None)["version"] == WORKING_CONTEXT_VERSION
    assert migrate_working_context("not a dict")["version"] == WORKING_CONTEXT_VERSION


def test_result_reference_is_bounded_and_keeps_a_durable_pointer():
    """Session files must not become a copy of surveillance query output."""
    rows = [{"name": f"person {i}", "note": "x" * 500} for i in range(50)]
    ref = ConversationMemory.build_result_reference(
        rows=rows, sql="SELECT 1", purpose="test", history_id=173)

    assert ref["row_count"] == 50, "the true count is still reported"
    assert len(ref["preview"]) <= 3, "only a preview is stored"
    assert all(len(v) <= 81 for row in ref["preview"] for v in row.values()
               if isinstance(v, str)), "cells must be truncated"
    assert ref["history_id"] == 173, "the durable reference to the full result is kept"


def test_result_reference_handles_empty_rows():
    ref = ConversationMemory.build_result_reference(rows=[], sql=None, purpose=None)
    assert ref["row_count"] == 0 and ref["preview"] == [] and ref["columns"] == []


# ---------------------------------------------------------------------------
# Stage 2: the durability contract beyond location
# ---------------------------------------------------------------------------

def test_an_interrupted_write_cannot_corrupt_the_previous_state(memory, monkeypatch):
    """A crash between temp-write and os.replace loses the NEW turn only.

    The atomic-write contract: the previous file must remain intact and
    loadable no matter where the writer dies. Simulated by making os.replace
    raise — everything before it (temp file, flush, fsync) has happened,
    which is the widest window a real crash gets.
    """
    memory.update_working_context(last_action="before-crash")
    session_file = memory.storage_dir / f"{memory.current_session_id}.json"
    good = session_file.read_text(encoding="utf-8")

    real_replace = os.replace

    def _crash(src, dst):
        raise OSError("simulated crash between tmp and replace")

    monkeypatch.setattr(os, "replace", _crash)
    memory.update_working_context(last_action="lost-to-the-crash")
    monkeypatch.setattr(os, "replace", real_replace)

    # The file on disk is byte-identical to the pre-crash version...
    assert session_file.read_text(encoding="utf-8") == good
    # ...and a fresh process reads coherent pre-crash state, not garbage.
    fresh = reopen(memory)
    assert fresh.get_working_context(reload=True)["last_action"] == "before-crash"
    # No temp litter accumulates from the failed attempt.
    assert not list(memory.storage_dir.glob("*.tmp"))


def test_the_default_location_is_under_the_storage_root(tmp_path, monkeypatch):
    """Working memory must live where a volume actually persists it.

    The old default was a RELATIVE 'conversation_cache' resolved against CWD
    — the container's writable layer, erased by every --force-recreate, and
    invisible in dev because the repo bind-mount faked persistence. The
    default now derives from STORAGE_DIR, which the deployment's storage
    volume already covers, and the config guard pins the derivation.
    """
    from config import settings
    mem = ConversationMemory(user_id=424242)
    try:
        resolved = str(mem.storage_dir.resolve())
        expected_root = str(Path(settings.CONVERSATION_CACHE_DIR).resolve())
        assert resolved.startswith(expected_root), (
            f"default memory dir {resolved} is not under "
            f"CONVERSATION_CACHE_DIR {expected_root}")
        assert str(Path(settings.CONVERSATION_CACHE_DIR)).startswith(
            str(Path(settings.STORAGE_DIR))), (
            "CONVERSATION_CACHE_DIR is not derived from STORAGE_DIR — the "
            "storage volume does not cover it")
    finally:
        import shutil
        shutil.rmtree(mem.storage_dir, ignore_errors=True)


def test_legacy_sessions_are_migrated_forward_not_lost(tmp_path, monkeypatch):
    """The location fix must not itself erase everyone's memory on deploy.

    A session file in the old CWD-relative location is copied to the new
    directory on first construction; a file the new location already has is
    NOT overwritten (the new state is newer by definition).
    """
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / "conversation_cache" / "user_31337"
    legacy.mkdir(parents=True)
    (legacy / "user_31337_main.json").write_text(
        json.dumps({"session_id": "user_31337_main", "messages": [],
                    "working_context": {"version": 1, "last_action": "legacy"}}),
        encoding="utf-8")

    target_root = tmp_path / "new_home"
    mem = ConversationMemory(storage_dir=str(target_root), user_id=31337)
    migrated = target_root / "user_31337" / "user_31337_main.json"
    assert migrated.exists(), "the legacy session was not migrated"
    assert mem.load_session("user_31337_main")
    assert mem.get_working_context()["last_action"] == "legacy"

    # Second construction must not clobber newer state with the legacy copy.
    mem.update_working_context(last_action="new-era")
    ConversationMemory(storage_dir=str(target_root), user_id=31337)
    fresh = ConversationMemory(storage_dir=str(target_root), user_id=31337)
    fresh.load_session("user_31337_main")
    assert fresh.get_working_context()["last_action"] == "new-era", (
        "re-migration overwrote newer state with the legacy file")


def test_the_message_cap_prunes_raw_turns_and_nothing_else(memory):
    """Latency must not grow with conversation age forever.

    Only the OLDEST RAW TURNS are pruned. Working context, unknown top-level
    keys and every reference the resolver depends on survive — pruning that
    truncated last_artifact_id or provenance would trade latency for the
    agent's memory of what "it" means.
    """
    from sql_agent.conversation_memory import _MAX_STORED_MESSAGES

    memory.update_working_context(last_artifact_id="keep-me",
                                  active_filters={"camera": 3})
    session_file = memory.storage_dir / f"{memory.current_session_id}.json"
    data = json.loads(session_file.read_text(encoding="utf-8"))
    data["future_field"] = {"survives": True}
    session_file.write_text(json.dumps(data), encoding="utf-8")

    for i in range(_MAX_STORED_MESSAGES + 25):
        memory.add_user_message(f"turn {i}")

    stored = json.loads(session_file.read_text(encoding="utf-8"))
    assert len(stored["messages"]) == _MAX_STORED_MESSAGES, (
        f"cap not applied: {len(stored['messages'])} messages stored")
    texts = [m["data"]["content"] for m in stored["messages"]]
    assert f"turn {_MAX_STORED_MESSAGES + 24}" in texts[-1], "newest turn lost"
    assert "turn 0" not in texts, "oldest turn was not the one pruned"
    # The semantic state is untouched by pruning.
    assert stored["working_context"]["last_artifact_id"] == "keep-me"
    assert stored["working_context"]["active_filters"] == {"camera": 3}
    assert stored["future_field"] == {"survives": True}


# ------------------------------- one bad cell must not destroy a session

def test_a_datetime_in_a_result_preview_does_not_break_the_whole_save(memory):
    """The live failure: "Object of type datetime is not JSON serializable".

    A tracking query returns timestamps, build_result_reference copied cells
    verbatim, and json.dump then failed for the ENTIRE document — transcript
    and working context both silently lost, with one WARNING as the only
    sign. The agent simply stopped remembering.
    """
    import datetime
    import decimal
    import uuid as uuid_mod

    rows = [{"name": "JOEY",
             "seen_at": datetime.datetime(2026, 8, 30, 12, 0),
             "score": decimal.Decimal("0.97"),
             "pipeline": uuid_mod.uuid4()}]
    reference = memory.build_result_reference(rows, "SELECT 1", "track")

    memory.add_user_message("track Joey")
    assert memory.update_working_context(last_result=reference), (
        "the session save failed on a result containing a datetime")

    fresh = reopen(memory)
    stored = fresh.get_working_context(reload=True)["last_result"]
    assert stored["row_count"] == 1
    assert stored["preview"][0]["seen_at"] == "2026-08-30T12:00:00"
    assert stored["preview"][0]["score"] == 0.97
    # And the transcript survived alongside it.
    assert any("track Joey" in str(getattr(m, "content", ""))
               for m in fresh.messages)


def test_an_unanticipated_object_degrades_to_a_string_not_a_lost_session(memory):
    """Insurance, not laziness.

    Some future field will hold something json.dump cannot handle. Losing
    that one value is acceptable; losing every prior turn is not.
    """
    class _Exotic:
        def __str__(self):
            return "exotic-value"

    memory.add_user_message("hello")
    assert memory.update_working_context(selected_entity=_Exotic())

    fresh = reopen(memory)
    context = fresh.get_working_context(reload=True)
    assert "exotic" in str(context.get("selected_entity"))
    assert any("hello" in str(getattr(m, "content", ""))
               for m in fresh.messages), "the transcript was lost"
