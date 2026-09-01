"""A query the database rejected, and whether the agent may fix it.

Reported: "track joey and give me the report in arabic" answered with

    SQL Error: operator does not exist: character varying = integer
    LINE 1: ...LOWER(f.name) LIKE LOWER('%Joey%') AND p.pipeline_id = 3 ...

Two separate faults in one message.

1. The raw driver text - including the generated SQL and a schema hint -
   reached the user. `_failure_narration` is category-driven and leaks
   nothing, but the STREAMING path yields its own error event built by
   interpolating `result["error"]`, so it bypassed all of that. The bug the
   user reported was visible only because they were on the streaming
   endpoint.

2. The agent gave up. `classify_execution_error` sorts errors into TRANSIENT
   (retry the same SQL) and PERMANENT (stop), and everything unrecognised
   falls to PERMANENT. A type mismatch is indeed not worth retrying
   UNCHANGED - but it is exactly the kind of mistake a regeneration fixes,
   given the error text as a hint. It was being treated as a dead end while
   the self-correction machinery sat unused.

So there are three kinds, not two: retry the same SQL, rewrite the SQL, or
stop.

    docker exec face_recognition_api python -m pytest tests/test_correctable_sql_errors.py -v
"""

import pytest

from sql_agent import reasoning
from sql_agent.reasoning import ErrorType


# ------------------------------------------------------ the classification

@pytest.mark.parametrize("error", [
    "operator does not exist: character varying = integer",
    'column "nope" does not exist',
    'function lower(integer) does not exist',
    "syntax error at or near \"FROM\"",
    "invalid input syntax for type integer",
    "column reference \"id\" is ambiguous",
])
def test_a_query_the_database_refuses_can_be_rewritten(error):
    """THE fix. These are mistakes in the QUERY, and a rewrite can fix them."""
    assert reasoning.classify_execution_error(error) == (
        ErrorType.SQL_EXECUTION_ERROR_CORRECTABLE), error


@pytest.mark.parametrize("error", [
    "connection reset by peer",
    "server closed the connection unexpectedly",
    "pool timeout",
    "deadlock detected",
])
def test_a_transport_failure_is_still_retried_unchanged(error):
    """THE control.

    A dropped connection is not a bad query. Rewriting the SQL would spend a
    re-plan on something that was never wrong.
    """
    assert reasoning.classify_execution_error(error) == (
        ErrorType.SQL_EXECUTION_ERROR_TRANSIENT), error


@pytest.mark.parametrize("error", [
    "permission denied for table users",
    "disk full",
    "",
])
def test_anything_unrecognised_still_fails_closed(error):
    """The default must stay PERMANENT.

    Guessing that an unknown failure is correctable spends the budget on a
    rewrite that cannot help.
    """
    assert reasoning.classify_execution_error(error) == (
        ErrorType.SQL_EXECUTION_ERROR_PERMANENT), error


# ------------------------------------------------------------ the decision

def _failed(error, **extra):
    state = {"planned_action": {"action": "query_database"},
             "sql_validation_status": "VALID",
             "generated_sql": "SELECT 1 FROM faces WHERE p.pipeline_id = 3",
             "query_result": {"success": False, "error": error, "rows": [],
                              "row_count": 0},
             "working_context": {}, "normalized_input": "track joey"}
    state.update(extra)
    return state


def test_a_correctable_error_earns_one_rewrite():
    observation = reasoning.build_observation(
        _failed("operator does not exist: character varying = integer"))
    verdict = reasoning.decide_next(observation, replan_count=0, max_replans=1)

    assert observation["error_type"] == ErrorType.SQL_EXECUTION_ERROR_CORRECTABLE
    assert observation["retryable"]
    assert verdict["decision"] == reasoning.REPLAN


def test_the_rewrite_budget_is_still_bounded():
    """The control on the fix: one correction, not an argument with Postgres."""
    observation = reasoning.build_observation(
        _failed("operator does not exist: character varying = integer"))
    verdict = reasoning.decide_next(observation, replan_count=1, max_replans=1)

    assert verdict["decision"] == reasoning.ANSWER


def test_a_correctable_error_does_not_rerun_the_same_sql():
    """Re-running byte-identical SQL against a type error cannot work."""
    observation = reasoning.build_observation(
        _failed("operator does not exist: character varying = integer"))
    verdict = reasoning.decide_next(observation, execution_retries=0,
                                    max_execution_retries=1)

    assert verdict["decision"] != reasoning.RETRY_EXECUTION


# -------------------------------------------------------------- the leak

def test_the_driver_text_never_reaches_the_user():
    """No SQL, no schema, no column names in what is shown."""
    from sql_agent.tools.agent_tools import SQLAgentTools

    raw = ("operator does not exist: character varying = integer\n"
           "LINE 1: ...LOWER(f.name) LIKE LOWER('%Joey%') AND p.pipeline_id = 3")
    said = SQLAgentTools._failure_narration(
        _failed(raw, response_language="en"))

    for leak in ("pipeline_id", "LOWER", "SELECT", "character varying",
                 "LINE 1"):
        assert leak not in said, f"{leak!r} leaked into: {said!r}"


def test_the_streaming_path_says_the_same_thing():
    """The reported bug was visible ONLY on the streaming endpoint.

    A sanitized REST reply and a raw SSE event is not a sanitized system.
    """
    import inspect

    from sql_agent import agent as agent_module

    source = inspect.getsource(agent_module)
    assert 'f"SQL Error: {result.get(' not in source, (
        "the streaming path still interpolates the driver's error text")
