"""What the user is told when a turn fails, and what they are not told.

Two defects lived in the same eight lines of `generate_story_response`:

    error_msg = query_result.get("error", "Unknown error occurred")
    state["final_response"] = f"...retrieve that information: {error_msg}"

The RAW driver error went into the reply. Postgres names the table and column
it objected to; the AST guard's denials carry the rejected query itself. So a
failed turn handed the user a description of our schema and of what the
system had tried to run — to anyone who could make a query fail, which is
anyone who can type.

Both strings were also hardcoded English. An Arabic turn asks in Arabic, gets
an Arabic report when it works, and an English apology when it does not —
which is the moment the wording matters most.

Also pinned: the ownership contract on the history accessor, whose filter
used to be gated on `if user_id:` — so `None`, and `0`, returned any user's
row.

    docker exec face_recognition_api python -m pytest tests/test_failure_narration.py -v
"""

import pytest

from sql_agent import reasoning as r


ARABIC = ("؀", "ۿ")


def _is_arabic(text: str) -> bool:
    return any(ARABIC[0] <= ch <= ARABIC[1] for ch in text)


def _tools(monkeypatch):
    import sql_agent.tools.agent_tools as module
    monkeypatch.setattr(module, "create_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "create_sql_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "DatabaseManager", lambda *a, **k: object())
    monkeypatch.setattr(module, "SQLKnowledgeBase", lambda *a, **k: None)
    return module.SQLAgentTools(conversation_memory=None)


def _state(**extra):
    state = {"normalized_input": "detections yesterday", "response_language": "en",
             "working_context": {}, "planned_action": {"action": "query_database"}}
    state.update(extra)
    return state


# ------------------------------------------------- the raw error must not leak

@pytest.mark.parametrize("raw", [
    'column "cam" does not exist\nLINE 1: SELECT cam FROM detections',
    'relation "user_credentials" does not exist',
    'Security: DELETE is not permitted. Query: SELECT * FROM users WHERE id=1',
])
def test_the_raw_database_error_never_reaches_the_user(monkeypatch, raw):
    """A failure must not double as a schema disclosure.

    Anyone who can make a query fail can read these, and making one fail
    takes no privilege at all.
    """
    tools = _tools(monkeypatch)
    message = tools._failure_narration(_state(
        query_result={"success": False, "error": raw, "rows": [], "row_count": 0}))

    for leaked in ("cam", "detections", "user_credentials", "SELECT", "LINE 1"):
        assert leaked not in message, f"{leaked!r} leaked into: {message!r}"


def test_the_user_still_learns_something_useful(monkeypatch):
    """Safe is not the same as useless — a bare apology helps nobody.

    The CATEGORY is what tells someone whether to rephrase, retry or stop,
    and it carries no database text by construction.
    """
    tools = _tools(monkeypatch)

    transient = tools._failure_narration(_state(
        observation={"error_type": r.ErrorType.SQL_EXECUTION_ERROR_TRANSIENT}))
    assert "try again" in transient.lower()

    invalid = tools._failure_narration(_state(
        observation={"error_type": r.ErrorType.SQL_INVALID}))
    assert "rephrase" in invalid.lower()

    forbidden = tools._failure_narration(_state(
        observation={"error_type": r.ErrorType.SQL_FORBIDDEN}))
    assert "only read" in forbidden.lower()

    assert transient != invalid != forbidden, (
        "every failure reads the same, so the category tells the user nothing")


def test_every_error_type_has_a_phrase_in_both_languages(monkeypatch):
    """A new category must not silently degrade to the generic apology.

    Fails when someone adds an ErrorType and forgets the wording — which is
    the moment the user stops being told anything useful.
    """
    tools = _tools(monkeypatch)
    expected = {value for name, value in vars(r.ErrorType).items()
                if not name.startswith("_") and isinstance(value, str)}
    # These two never reach a failure narration: zero rows is an answer, and
    # an unresolved name is answered with a question.
    expected -= {r.ErrorType.EMPTY_RESULT, r.ErrorType.ENTITY_UNRESOLVED}

    missing = expected - set(tools._FAILURE_PHRASES)
    assert not missing, f"no user-facing wording for: {sorted(missing)}"

    for name, phrases in tools._FAILURE_PHRASES.items():
        assert phrases.get("en") and phrases.get("ar"), name


def test_a_failure_with_no_safe_reason_still_says_something(monkeypatch):
    """No reason available must not produce a dangling '()' or an empty reply."""
    tools = _tools(monkeypatch)
    message = tools._failure_narration(_state(observation={}))

    assert message.strip()
    assert "()" not in message


# --------------------------------------------------------------- language

def test_an_arabic_turn_gets_an_arabic_failure(monkeypatch):
    """The apology is the moment the language matters most."""
    tools = _tools(monkeypatch)
    message = tools._failure_narration(_state(
        response_language="ar",
        query_result={"success": False, "error": "boom", "rows": [], "row_count": 0}))

    assert _is_arabic(message), f"Arabic turn got: {message!r}"


def test_an_arabic_turn_gets_an_arabic_empty_result(monkeypatch):
    tools = _tools(monkeypatch)
    message = tools._empty_narration(_state(response_language="ar"))
    assert _is_arabic(message), f"Arabic turn got: {message!r}"


def test_an_english_turn_stays_english(monkeypatch):
    """The negative control: localizing must not localize everything."""
    tools = _tools(monkeypatch)
    for message in (tools._failure_narration(_state()),
                    tools._empty_narration(_state())):
        assert not _is_arabic(message), message


def test_zero_rows_is_worded_as_an_answer_not_an_apology(monkeypatch):
    """"How many detections yesterday?" answered with none IS the answer.

    Wording it as a failure teaches the user to distrust a correct result.
    """
    tools = _tools(monkeypatch)
    message = tools._empty_narration(_state())

    assert "found no matching records" in message
    for apologetic in ("sorry", "apologize", "error", "problem", "issue"):
        assert apologetic not in message.lower()


# ------------------------------------------------ the ownership contract

def test_a_history_row_is_never_returned_unscoped():
    """`if user_id:` meant None — and 0 — dropped the ownership filter.

    Today's only caller passes a real id, so nothing was exposed; the hazard
    was that the SAFE behaviour was the caller's job and the DANGEROUS one
    was the default.
    """
    # `conftest`, never `tests.conftest`: importing the latter creates a
    # SECOND module object with its own event loop, which has bitten this
    # suite before.
    from conftest import run_on_shared_loop
    from sql_agent.services.user_query_history_service import (
        user_query_history_service as service)

    class _Db:
        executed = False

        async def execute(self, *a, **k):
            _Db.executed = True
            raise AssertionError("a falsy user_id reached the database")

    for falsy in (None, 0):
        row = run_on_shared_loop(service.get_query_by_id_for_user(
            db=_Db(), query_id=1, user_id=falsy))
        assert row is None, f"user_id={falsy!r} returned a row"

    assert not _Db.executed, "the query ran despite an unusable user_id"


def test_the_deprecated_name_can_no_longer_omit_the_owner():
    """A caller that forgets is now a TypeError, not a silent cross-user read."""
    import inspect
    from sql_agent.services.user_query_history_service import (
        user_query_history_service as service)

    for name in ("get_query_by_id", "get_query_by_id_for_user"):
        signature = inspect.signature(getattr(service, name))
        user_id = signature.parameters["user_id"]
        assert user_id.default is inspect.Parameter.empty, (
            f"{name} still has an optional user_id")


def test_the_scoped_accessor_filters_on_owner_unconditionally():
    """Pins the WHERE clause: an id-only lookup would defeat the contract.

    Read through the AST, not with substring matching. The first version of
    this test searched the source text for "if user_id:" and matched the
    DOCSTRING — which quotes that line to explain why it was wrong — so the
    test failed on correct code. A source scan that cannot tell code from
    prose is measuring the wrong thing.
    """
    import ast
    import inspect
    import textwrap
    from sql_agent.services.user_query_history_service import (
        user_query_history_service as service)

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(type(service).get_query_by_id_for_user)))
    function = tree.body[0]

    # The ownership comparison must be present as CODE.
    comparisons = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Attribute)
        and node.left.attr == "user_id"
    ]
    assert comparisons, "no ownership comparison in the query"

    # ...and it must not sit inside a branch that can skip it. The early
    # `if not user_id: return None` guard is fine — that REFUSES, it does not
    # widen — so only a comparison nested under an `if` is a problem.
    for branch in ast.walk(function):
        if not isinstance(branch, ast.If):
            continue
        for comparison in comparisons:
            if comparison in ast.walk(branch):
                raise AssertionError(
                    "the ownership filter is inside a conditional again")
