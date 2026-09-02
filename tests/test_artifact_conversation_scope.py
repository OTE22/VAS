"""Which documents and queries a conversation may refer to.

Asked to track Iron Man and then for the report in Arabic, the agent
translated a document from a DIFFERENT conversation - one produced during an
earlier test session - and `modify_active_query` edited that conversation's
SQL, which still filtered on Joey. The answer was about Joey.

The cause is in the data, not the reasoning:

    agent_artifacts.conversation_id -> NULL on all 51 rows

The column exists and `list_recent_artifacts` already accepts a filter for it,
but nothing ever writes the value, so every document ever produced is offered
as a candidate to every new conversation. `session/new` cannot help: there is
nothing to filter on.

`reset_session` keeps the session id and writes a fresh `created_at`, so that
timestamp IS the conversation boundary - and it works on all three transports
without a migration.

    docker exec face_recognition_api python -m pytest tests/test_artifact_conversation_scope.py -v
"""

from datetime import datetime, timedelta

import pytest

from conftest import run_on_shared_loop


NOW = datetime.utcnow()
BEFORE = NOW - timedelta(hours=3)
AFTER = NOW + timedelta(minutes=1)


class _Row:
    def __init__(self, created_at, title):
        self.id = f"id-{title}"
        self.type = "pdf"
        self.title = title
        self.language = "en"
        self.created_at = created_at
        self.source_sql = f"SELECT '{title}'"

    def __getitem__(self, index):
        # get_artifact_source_sql reads rows positionally.
        return (self.id, self.source_sql)[index]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _DB:
    """Captures the statement so the WHERE clause can be inspected."""

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return _Result(self.rows)


def _compiled(db):
    return " ".join(db.statements[-1].split())


# ------------------------------------------------------------- the filter

def test_the_index_can_be_scoped_to_this_conversation():
    """THE fix: a `since` boundary reaches the query at all."""
    from sql_agent.services import artifact_registry

    db = _DB([_Row(AFTER, "mine")])
    run_on_shared_loop(
        artifact_registry.list_recent_artifacts(db, 1, since=NOW))

    assert "created_at" in _compiled(db).lower(), (
        "the conversation boundary never reached the query")


def test_the_source_sql_map_is_scoped_the_same_way():
    """Otherwise modify_active_query still reaches across conversations.

    That map is what `modify_active_query` edits, so leaving it unscoped
    leaves the reported bug in place while appearing to fix it.
    """
    from sql_agent.services import artifact_registry

    db = _DB([_Row(AFTER, "mine")])
    run_on_shared_loop(
        artifact_registry.get_artifact_source_sql(db, 1, since=NOW))

    assert "created_at" in _compiled(db).lower(), (
        "the SQL index is still visible across conversations")


def test_without_a_boundary_nothing_changes():
    """THE control.

    A caller that cannot determine when the conversation began must keep
    today's behaviour rather than silently seeing nothing.
    """
    from sql_agent.services import artifact_registry

    db = _DB([_Row(BEFORE, "old")])
    out = run_on_shared_loop(
        artifact_registry.list_recent_artifacts(db, 1))

    assert out, "an unscoped call stopped returning anything"


def test_a_missing_user_still_returns_nothing():
    """The existing owner check is not weakened by adding a time bound."""
    from sql_agent.services import artifact_registry

    out = run_on_shared_loop(
        artifact_registry.list_recent_artifacts(_DB([]), None, since=NOW))
    assert out == []


# ------------------------------------------------- the conversation boundary

def test_the_session_reports_when_it_started(tmp_path):
    """`reset_session` rewrites created_at, so it marks the boundary."""
    from sql_agent.conversation_memory import ConversationMemory

    memory = ConversationMemory.__new__(ConversationMemory)
    memory.storage_dir = tmp_path
    memory.current_session_id = "user_1_main"
    memory.reset_session()

    started = memory.session_started_at()
    assert started is not None
    assert abs((datetime.utcnow() - started).total_seconds()) < 60


def test_ordinary_message_saves_do_not_move_the_conversation_boundary(tmp_path):
    """A report stays in scope after later turns autosave the transcript."""
    from sql_agent.conversation_memory import ConversationMemory

    memory = ConversationMemory(storage_dir=str(tmp_path), user_id=1)
    memory.start_session()
    assert memory.reset_session() is True
    started = memory.session_started_at()

    memory.add_user_message("make that a PDF")
    memory.add_ai_message("The report is ready.")
    memory.update_working_context(last_artifact_id="report-1")

    assert memory.session_started_at() == started


def test_an_unknown_session_reports_no_boundary(tmp_path):
    """Never fatal: no boundary means today's behaviour, not an empty index."""
    from sql_agent.conversation_memory import ConversationMemory

    memory = ConversationMemory.__new__(ConversationMemory)
    memory.storage_dir = tmp_path
    memory.current_session_id = "does-not-exist"

    assert memory.session_started_at() is None
