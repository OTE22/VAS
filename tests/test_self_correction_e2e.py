"""The reasoning layer, end to end, on the real graph.

Everything else pins a piece: `test_reasoning.py` the decisions,
`test_reasoning_graph.py` the routing, `test_tool_loop_observability.py` the
tool calls. This drives the COMPILED graph through a whole turn with a
scripted model and a fake database, and asserts the trajectory:

    PLAN -> generate SQL -> validation REJECTS it -> Observation(sql_invalid)
    -> ONE corrective re-plan -> corrected SQL -> AST guard -> execution
    -> Observation(success) -> ANSWER

and the properties that make it safe rather than merely clever:

  * the rejected SQL is executed ZERO times — not "caught later", never sent;
  * exactly one re-plan is spent;
  * the regeneration is TOLD what was wrong, so the retry is corrective
    rather than the same dice roll;
  * with the re-plan budget at zero, invalid SQL still never executes and the
    user gets an honest failure instead of a query nobody validated.

The model is scripted by PROMPT CONTENT, not by call order. Positional
scripting silently rots the moment a node is added or reordered, and then
asserts something other than what it claims to.

No network, no real database, no HTTP.

    docker exec face_recognition_api python -m pytest tests/test_self_correction_e2e.py -v
"""

import json

import pytest

from sql_agent import reasoning as r


# --------------------------------------------------------------- the fakes

class _Script:
    """Answers by what it was ASKED, and records every prompt it saw.

    Wrapped in a RunnableLambda below, because the nodes build real LangChain
    chains (`prompt | llm | StrOutputParser()`) and a hand-rolled pipe is not
    a Runnable.

    Dispatch is on prompt CONTENT, never on call order: positional scripting
    rots the moment a node is added or reordered, and then asserts something
    other than what it claims to.
    """

    def __init__(self, sql_sequence):
        self.sql_sequence = list(sql_sequence)
        self.prompts = []
        self.sql_calls = 0

    # The markers were READ OFF a real run of the graph, not guessed. A
    # fresh question with no conversation context is FAST mode, so the tool
    # loop is skipped by design and the single-shot planner decides — which
    # is why "route requests" appears here and no tool-call prompt does.
    def __call__(self, prompt_value):
        text = self._text(prompt_value)
        self.prompts.append(text)

        if "Fix this SQL query" in text:
            # The repair attempt fails to parse, so validation lands on
            # INVALID rather than FIXED — the branch under test.
            return "I am not able to repair that."

        if "Generate SQL for" in text:
            answer = (self.sql_sequence[self.sql_calls]
                      if self.sql_calls < len(self.sql_sequence)
                      else self.sql_sequence[-1])
            self.sql_calls += 1
            return json.dumps({"sql": answer, "purpose": "counts detections"})

        if "You route requests" in text:          # the single-shot planner
            return json.dumps({"action": "query_database", "confidence": 0.95,
                               "target": None, "artifact_id": None,
                               "language": None, "format": None,
                               "modification": None, "clarify_question": None})

        if "JSON tool call" in text or "ONE action tool" in text:
            return json.dumps({"name": "query_database",
                               "arguments": {"question": "detections yesterday"}})

        if "language correction" in text:         # fix_language passthrough
            return "how many detections were there yesterday?"

        return "Here is what I found."

    @staticmethod
    def _text(prompt_value):
        messages = getattr(prompt_value, "messages", None)
        if messages is not None:
            return "\n".join(str(getattr(m, "content", "")) for m in messages)
        return str(prompt_value)


class _FakeDb:
    """Records every SQL actually handed to the database."""

    def __init__(self, rows=None):
        self.executed = []
        self.rows = rows if rows is not None else [{"detections": 42}]

    def _validate_query(self, sql):
        return {"is_safe": True, "reason": "Query is safe and read-only"}

    def execute_query(self, sql):
        self.executed.append(sql)
        return {"success": True, "rows": self.rows,
                "row_count": len(self.rows), "columns": ["detections"]}

    def get_schema_description(self, *a, **k):
        return "detections(id, camera_id, timestamp)"

    def get_table_names(self, *a, **k):
        return ["detections"]


BAD_SQL = "SELECT count(*) FROM detections WHERE ("      # unbalanced paren
GOOD_SQL = "SELECT count(*) AS detections FROM detections"


@pytest.fixture
def scenario(monkeypatch):
    """The compiled graph, with only the model and database replaced."""
    import sql_agent.tools.agent_tools as tools_module
    import sql_agent.graph as graph_module

    from langchain_core.runnables import RunnableLambda

    script = _Script([BAD_SQL, GOOD_SQL])
    llm = RunnableLambda(script)
    db = _FakeDb()

    monkeypatch.setattr(tools_module, "create_llm", lambda *a, **k: llm)
    monkeypatch.setattr(tools_module, "create_sql_llm", lambda *a, **k: llm)
    monkeypatch.setattr(tools_module, "DatabaseManager", lambda *a, **k: db)
    monkeypatch.setattr(tools_module, "SQLKnowledgeBase", lambda *a, **k: None)

    graph = graph_module.create_sql_agent(conversation_memory=None)
    return graph, script, db


def _initial_state(**extra):
    state = {
        "user_id": 1,
        "original_input": "how many detections were there yesterday?",
        "normalized_input": "how many detections were there yesterday?",
        "intent": "CHAT", "intent_confidence": 0.0,
        "conversation_context": "", "response_language": "en",
        "working_context": {}, "artifact_index": [], "artifact_sql_index": {},
        "schema_description": "detections(id, camera_id, timestamp)",
        "should_learn": False,
        "replan_count": 0, "execution_retries": 0, "reasoning_steps_used": 0,
        "failed_action_fingerprints": [],
    }
    state.update(extra)
    return state


# ------------------------------------------------- the decisive trajectory

def test_invalid_sql_is_corrected_and_never_executed(scenario, monkeypatch):
    """The whole point of the reasoning layer, in one turn.

    The first generated query does not validate. The agent must notice,
    correct itself, and answer — without the rejected query ever reaching the
    database.
    """
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_REPLANS", 1, raising=False)

    graph, llm, db = scenario
    final = graph.invoke(_initial_state())

    assert BAD_SQL not in db.executed, (
        "the rejected query was sent to the database anyway")
    assert db.executed, "the corrected query never ran"
    assert final.get("replan_count") == 1, (
        f"expected exactly one re-plan, got {final.get('replan_count')}")

    # The turn RECOVERED, so the last observation is the successful retry -
    # successful actions reach the observer now. What proves the agent
    # NOTICED the invalid SQL is the correction hint that drove the re-plan:
    # it carries the rejected query and the validator's reason, and is only
    # ever set for a validation or correctable-execution failure.
    hint = final.get("sql_correction_hint") or {}
    assert hint.get("reason"), "nothing recorded why the first attempt failed"
    # Whitespace-normalized: the rejected SQL is stored as the generator
    # emitted it, with newlines. Comparing byte-for-byte asserts formatting,
    # which is not what this test is about.
    normalize = lambda text: " ".join((text or "").split())
    assert normalize(BAD_SQL) in normalize(hint.get("sql")), (
        "the correction hint did not carry the rejected query")

    observation = final.get("observation") or {}
    assert observation.get("success"), "the turn never recovered"


def test_the_regeneration_is_told_what_was_wrong(scenario, monkeypatch):
    """Without the feedback the retry is a coin flip, not a correction.

    This is the difference between self-correction and rolling the dice on
    the same inputs — and it is why repeating `query_database` is allowed
    where repeating any other action is refused.
    """
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_REPLANS", 1, raising=False)

    graph, llm, db = scenario
    graph.invoke(_initial_state())

    regenerations = [p for p in llm.prompts if "Generate SQL for" in p]
    assert len(regenerations) >= 2, "the query was never regenerated"
    assert "PREVIOUS ATTEMPT WAS REJECTED" in regenerations[-1], (
        "the retry prompt carried no correction")

    # Whitespace-insensitive: `prepare_sql_from_llm_response` reformats the
    # query onto several lines before it is stored, so demanding the exact
    # bytes back would be asserting the formatter rather than the feedback.
    def _flat(sql):
        return " ".join(str(sql).split())

    assert _flat(BAD_SQL) in _flat(regenerations[-1]), (
        "the rejected SQL was not fed back")
    assert "Why it was rejected" in regenerations[-1], (
        "the retry was told what failed but not why")


def test_the_first_attempt_carries_no_correction(scenario, monkeypatch):
    """Negative control: an ordinary turn's prompt must be unchanged."""
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_REPLANS", 1, raising=False)

    graph, llm, db = scenario
    graph.invoke(_initial_state())

    regenerations = [p for p in llm.prompts if "Generate SQL for" in p]
    assert "PREVIOUS ATTEMPT WAS REJECTED" not in regenerations[0]


# ------------------------------------------------ budget exhausted is safe

def test_with_no_replan_budget_invalid_sql_still_never_executes(
        scenario, monkeypatch):
    """"Run it anyway because the limit was reached" is the one unacceptable
    outcome.

    Exhausting the budget means answering honestly. It must never mean
    executing a query that failed validation.
    """
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_REPLANS", 0, raising=False)

    graph, llm, db = scenario
    final = graph.invoke(_initial_state())

    assert db.executed == [], (
        f"invalid SQL executed with no re-plan budget: {db.executed}")
    assert final.get("final_response"), "the user was told nothing at all"
    assert final.get("replan_count") in (0, None)


def test_the_honest_failure_names_no_schema(scenario, monkeypatch):
    """The failure the user sees must not describe our tables."""
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_REPLANS", 0, raising=False)

    graph, llm, db = scenario
    response = graph.invoke(_initial_state()).get("final_response") or ""

    for leaked in ("SELECT", "FROM detections", "camera_id"):
        assert leaked not in response, f"{leaked!r} leaked into: {response!r}"


# ------------------------------------------------------ nothing leaks out

def test_no_model_prose_reaches_the_reasoning_trace(scenario, monkeypatch, caplog):
    """The trace has to be debuggable WITHOUT exposing model reasoning."""
    import logging
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_REPLANS", 1, raising=False)

    graph, llm, db = scenario
    with caplog.at_level(logging.INFO):
        graph.invoke(_initial_state())

    traces = [rec.getMessage() for rec in caplog.records
              if "[REASONING]" in rec.getMessage()]
    assert traces, "the turn produced no reasoning trace at all"
    for line in traces:
        assert "not able to repair" not in line, (
            f"the model's own words reached a trace line: {line}")


def test_the_observation_never_carries_sql_or_rows(scenario, monkeypatch):
    """It goes into a prompt and a log; both are places data must not go."""
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_REPLANS", 1, raising=False)

    graph, llm, db = scenario
    observation = graph.invoke(_initial_state()).get("observation") or {}

    blob = json.dumps(observation)
    assert "SELECT" not in blob.upper()
    assert "detections WHERE" not in blob
