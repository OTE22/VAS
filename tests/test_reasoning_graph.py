"""The OBSERVE/REPLAN edge: what the graph does when an action goes wrong.

`tests/test_reasoning.py` pins the pure decision logic. This file pins the
wiring — which nodes actually run — because the decisions only matter if the
graph obeys them, and because the default path has to stay exactly what it
was before the loop existed.

The properties under test:

  1. SQL that failed validation is NEVER executed. Not when the re-plan
     budget is healthy, and not when it is exhausted. This is a real defect
     being closed: the old graph fell through to execution with the original
     bad SQL, where the AST guard blocked it as a SECURITY event.
  2. A malformed query is not an intrusion. `SELECT statement."}` — produced
     live from the user typing "hello" — used to mark the account for
     blocking and answer 403. Enforcement now keys off the guard's own
     classified code.
  3. The happy path is untouched. A successful turn traverses the same nodes
     in the same order as before.
  4. Re-planning is corrective. A proposal identical to the call that just
     failed is refused in Python, whatever the model says.
  5. A transient database error retries the SAME SQL on its own budget and
     does not spend a re-plan.
  6. Termination is arithmetic. The routers read counters that only
     observe_and_replan increments.

No LLM, no database, no files.

    docker exec face_recognition_api python -m pytest tests/test_reasoning_graph.py -v
"""

import json

import pytest

from sql_agent import reasoning as r
from sql_agent.security import sql_guard


# --------------------------------------------------------------- harness

def _tools(monkeypatch, llm_replies=None, execute=None):
    """SQLAgentTools with every heavy dependency stubbed."""
    import sql_agent.tools.agent_tools as module
    from langchain_core.runnables import RunnableLambda

    replies = list(llm_replies or [])
    calls = []

    def _respond(prompt_value):
        calls.append(prompt_value)
        return replies.pop(0) if replies else "{}"

    class _FakeDb:
        def _validate_query(self, _sql):
            return {"is_safe": True, "reason": "Query is safe and read-only"}

        def execute_query(self, sql):
            if execute is not None:
                return execute(sql)
            return {"success": True, "rows": [{"n": 1}], "row_count": 1}

    monkeypatch.setattr(module, "create_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "create_sql_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "DatabaseManager", lambda *a, **k: _FakeDb())
    monkeypatch.setattr(module, "SQLKnowledgeBase", lambda *a, **k: None)
    tools = module.SQLAgentTools(conversation_memory=None)
    tools.llm = RunnableLambda(_respond)
    tools.sql_llm = RunnableLambda(_respond)
    return tools, calls


def _state(**extra):
    state = {"normalized_input": "detections yesterday",
             "original_input": "detections yesterday",
             "working_context": {}, "artifact_index": [],
             "artifact_sql_index": {}, "conversation_context": "",
             "response_language": "en", "planner_candidates": {},
             "planned_action": {"action": "query_database"},
             "replan_count": 0, "execution_retries": 0,
             "reasoning_steps_used": 0, "failed_action_fingerprints": [],
             "reasoning_mode": r.ReasoningMode.CONTEXTUAL,
             "generated_sql": "SELECT 1"}
    state.update(extra)
    return state


def _routers(monkeypatch):
    """The graph's routing functions, captured as the graph is built.

    Testing them directly rather than through a full invoke keeps these tests
    deterministic while still exercising the REAL functions the compiled
    graph calls — the wiring is asserted separately by
    `test_the_compiled_graph_exposes_exactly_the_intended_edges`.
    """
    import sql_agent.graph as graph_module

    captured = {}
    real = graph_module.StateGraph

    class _Capturing(real):
        def add_conditional_edges(self, source, path, mapping=None, *a, **kw):
            captured[source] = (path, mapping)
            return super().add_conditional_edges(source, path, mapping, *a, **kw)

    monkeypatch.setattr(graph_module, "StateGraph", _Capturing)
    monkeypatch.setattr(graph_module, "SQLAgentTools", lambda **kw: _Nodes())
    graph_module.create_sql_agent()
    return captured


class _Nodes:
    """Stand-in tool object: the graph only needs attributes to bind."""

    def __getattr__(self, name):
        return lambda state: state


# ------------------------------------------- 1. invalid SQL never executes

@pytest.mark.parametrize("status", ["INVALID", "ERROR", "PARTIAL"])
def test_sql_that_failed_validation_is_never_sent_to_execution(monkeypatch, status):
    """The core guarantee, for every failing validation status.

    PARTIAL is included on purpose: it means the LLM's fix ALSO failed to
    validate and the original bad SQL was kept, which is no safer to run
    than INVALID. The old graph ran all three.
    """
    route, mapping = _routers(monkeypatch)["validate_and_fix_sql"]
    decision = route(_state(sql_validation_status=status, replan_count=0))
    assert mapping[decision] != "prepare_sql_for_execution", (
        f"{status} SQL was routed to execution")


def test_invalid_sql_at_the_replan_cap_fails_safely_instead_of_executing(monkeypatch):
    """Budget exhaustion must never mean "run it anyway".

    This is the regression the plan named explicitly: with the re-plan budget
    spent, an INVALID query must reach the user as an honest failure and
    `execute_sql` must never be entered.
    """
    from config import settings

    route, mapping = _routers(monkeypatch)["validate_and_fix_sql"]
    at_cap = _state(sql_validation_status="INVALID",
                    replan_count=int(settings.SQL_AGENT_MAX_REPLANS))

    decision = route(at_cap)
    assert mapping[decision] == "chat_response"
    assert mapping[decision] != "prepare_sql_for_execution"


def test_valid_sql_takes_exactly_the_edge_it_always_took(monkeypatch):
    """The default path is byte-identical to the pre-reasoning graph."""
    route, mapping = _routers(monkeypatch)["validate_and_fix_sql"]
    for status in ("VALID", "FIXED"):
        decision = route(_state(sql_validation_status=status))
        assert mapping[decision] == "prepare_sql_for_execution", status


def test_invalid_sql_with_budget_goes_to_the_observer(monkeypatch):
    route, mapping = _routers(monkeypatch)["validate_and_fix_sql"]
    decision = route(_state(sql_validation_status="INVALID", replan_count=0))
    assert mapping[decision] == "observe_and_replan"


# ------------------------------------ 2. a malformed query is not an attack

def test_a_parse_error_does_not_mark_the_account_for_blocking(monkeypatch):
    """The live 403: "hello" -> `SELECT statement."}` -> account blocked.

    The AST guard prefixes EVERY denial with "Security: ", so the old
    substring gate treated a model's broken SQL exactly like `DELETE FROM
    users`: CRITICAL audit line, user marked for blocking, 403 returned.
    """
    denial = {"success": False, "error": "Security: SQL could not be parsed: TokenError",
              "error_code": "PARSE_ERROR", "rows": [], "row_count": 0}
    tools, _ = _tools(monkeypatch, execute=lambda _sql: denial)

    class _Memory:
        user_id = 7

    tools.conversation_memory = _Memory()
    state = tools.execute_sql(_state())

    assert not state.get("security_block_user"), (
        "a malformed query was recorded as an attempted forbidden operation")


def test_a_genuinely_forbidden_query_still_blocks(monkeypatch):
    """The negative control. Weakening enforcement is not the goal."""
    denial = {"success": False,
              "error": "Security: DELETE is not permitted on a read-only connection",
              "error_code": "READ_ONLY_VIOLATION", "rows": [], "row_count": 0}
    tools, _ = _tools(monkeypatch, execute=lambda _sql: denial)

    class _Memory:
        user_id = 7

    tools.conversation_memory = _Memory()
    state = tools.execute_sql(_state())

    assert state.get("security_block_user") is True
    assert state.get("security_reason_code") == "READ_ONLY_VIOLATION"


def test_a_denial_with_no_code_still_blocks_on_the_prose_gate(monkeypatch):
    """Layer 2's regex gate returns no code; it must not become unenforced."""
    denial = {"success": False,
              "error": "Security: DELETE operations are not allowed.",
              "rows": [], "row_count": 0}
    tools, _ = _tools(monkeypatch, execute=lambda _sql: denial)

    class _Memory:
        user_id = 7

    tools.conversation_memory = _Memory()
    assert tools.execute_sql(_state()).get("security_block_user") is True


def test_every_guard_denial_code_is_deliberately_classified():
    """A new `_deny` code must not drift into the enforcement path unseen.

    `is_enforceable` fails closed, which is the safe default — but silently
    enforcing on a code nobody classified is how "hello" became a security
    incident in the first place. Classify it, in one direction or the other.
    """
    import inspect
    import re

    source = inspect.getsource(sql_guard)
    emitted = set(re.findall(r'_deny\(\s*"([A-Z_]+)"', source))
    classified = (sql_guard.ENFORCEABLE_CODES | sql_guard.MALFORMED_CODES
                  | sql_guard.INFRASTRUCTURE_CODES)

    assert emitted, "no denial codes found — did the guard change shape?"
    assert not (emitted - classified), (
        f"unclassified denial codes: {sorted(emitted - classified)}")


def test_a_parse_error_is_observed_as_correctable_not_forbidden():
    """Classification, not prose: the reasoning layer must be able to fix it."""
    observation = r.build_observation(_state(query_result={
        "success": False, "error": "Security: SQL could not be parsed: TokenError",
        "error_code": "PARSE_ERROR", "rows": [], "row_count": 0}))

    assert observation["error_type"] == r.ErrorType.SQL_INVALID
    assert observation["retryable"] is True


def test_a_forbidden_operation_is_observed_as_terminal():
    """Negative control for the above: a refusal is never retried."""
    observation = r.build_observation(_state(query_result={
        "success": False, "error": "Security: DELETE is not permitted",
        "error_code": "READ_ONLY_VIOLATION", "rows": [], "row_count": 0}))

    assert observation["error_type"] == r.ErrorType.SQL_FORBIDDEN
    assert observation["retryable"] is False


# ------------------------------------------- 3. the happy path is untouched

def test_a_single_action_turn_continues_straight_to_enrichment(monkeypatch):
    """With the ceiling at 1 there is nothing to observe FOR, so no extra hop.

    States the ceiling instead of inheriting it: this test used to pass only
    because the default happened to be 1, so raising the default silently
    changed what it was asserting.
    """
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_ACTIONS_PER_TURN", 1,
                        raising=False)
    route, mapping = _routers(monkeypatch)["execute_sql"]
    decision = route(_state(query_result={
        "success": True, "rows": [{"n": 1}], "row_count": 1}))
    assert mapping[decision] == "enrich_co_appearance"


def test_a_successful_query_is_observed_when_the_turn_may_act_again(monkeypatch):
    """The ReAct requirement: a result the next decision can actually see.

    A query that succeeded is evidence, and evidence is only useful if
    something looks at it before deciding the turn is over.
    """
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_ACTIONS_PER_TURN", 3,
                        raising=False)
    route, mapping = _routers(monkeypatch)["execute_sql"]
    decision = route(_state(query_result={
        "success": True, "rows": [{"n": 1}], "row_count": 1}))
    assert mapping[decision] == "observe_and_replan"


def test_zero_rows_is_an_answer_not_a_correction(monkeypatch):
    """"How many detections yesterday?" -> "none" is CORRECT.

    Re-planning here would turn a true answer into a fabricated one. This is
    about the DECISION, not the route, so it is asserted directly and holds
    whatever the ceiling is.
    """
    from sql_agent import reasoning

    observation = reasoning.build_observation(_state(query_result={
        "success": True, "rows": [], "row_count": 0}))
    verdict = reasoning.decide_next(observation, replan_count=0,
                                    max_replans=2)

    assert observation["success"], "zero rows was treated as a failure"
    assert verdict["decision"] == reasoning.ANSWER


def test_zero_rows_still_takes_the_plain_route_on_a_single_action_turn(monkeypatch):
    """The original route assertion, kept, with its regime made explicit."""
    from config import settings
    monkeypatch.setattr(settings, "SQL_AGENT_MAX_ACTIONS_PER_TURN", 1,
                        raising=False)
    route, mapping = _routers(monkeypatch)["execute_sql"]
    decision = route(_state(query_result={
        "success": True, "rows": [], "row_count": 0}))
    assert mapping[decision] == "enrich_co_appearance"


def test_a_terminal_execution_error_is_narrated_by_the_existing_path(monkeypatch):
    """Nothing to correct: keep today's behaviour rather than adding a hop."""
    route, mapping = _routers(monkeypatch)["execute_sql"]
    decision = route(_state(query_result={
        "success": False, "error": "violates check constraint",
        "rows": [], "row_count": 0}))
    assert mapping[decision] == "enrich_co_appearance"



def test_a_correctable_error_is_observed_for_a_rewrite(monkeypatch):
    """The database naming the mistake is the best correction signal there is.

    A bad COLUMN reads like a terminal failure but is fixable by rewriting,
    so it now earns exactly one correction.
    """
    route, mapping = _routers(monkeypatch)["execute_sql"]
    decision = route(_state(query_result={
        "success": False,
        "error": "operator does not exist: character varying = integer",
        "rows": [], "row_count": 0}))
    assert mapping[decision] == "observe_and_replan"

def test_a_contract_violating_success_is_never_narrated_as_success(monkeypatch):
    """Defends against our own executors, not only against the model.

    A query action reporting success with no result object is BUGGY. Telling
    the user it worked is the one outcome that must not happen.
    """
    route, mapping = _routers(monkeypatch)["execute_sql"]
    decision = route(_state(planned_action={"action": "query_database"},
                            query_result=None))
    assert mapping[decision] == "observe_and_replan"


# --------------------------------------------- 4. corrections are corrective

def test_repeating_a_futile_action_is_refused(monkeypatch):
    """The prompt forbids it; Python ENFORCES it.

    Translating the same missing document again cannot succeed, however
    confidently the model proposes it. Nothing about the second attempt
    differs from the first, so it is refused before it spends the budget.
    """
    from sql_agent.tools import agent_loop

    stale = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    call = {"name": "translate_document",
            "arguments": {"document_id": stale, "language": "ar"}}
    candidates = {"allowed_artifact_ids": {stale}}

    # Build the previous plan the way production does, rather than by hand:
    # a fingerprint comparison is only meaningful if both sides come from the
    # same machinery.
    previous = agent_loop.action_to_planned(call, candidates)
    assert previous, "the fixture's own premise broke"

    tools, _ = _tools(monkeypatch, llm_replies=[json.dumps(call)])
    state = _state(planned_action=previous,
                   translation_request={},
                   planner_candidates=candidates)

    # The premise: this really is a FAILED translate, not a success that
    # never reached the re-plan path.
    assert not r.build_observation(state)["success"]

    out = tools.observe_and_replan(state)

    assert out["reasoning_next"] == "chat_response"
    assert out["replan_count"] == 0, "a futile repeat consumed budget"


def test_regenerating_sql_is_allowed_because_the_rejection_is_fed_back(monkeypatch):
    """The one honest exception, and the reason it is honest.

    Re-running `query_database` looks like a repeat — `action_to_planned`
    discards the question argument, so the plans are identical — but the
    retry is NOT the same attempt: the rejected SQL and the validator's
    reason go into the regeneration prompt. Without that feedback this would
    be a coin flip and the refusal would be right.
    """
    correction = json.dumps({
        "name": "query_database",
        "arguments": {"question": "count detections for camera 3 yesterday"}})
    tools, _ = _tools(monkeypatch, llm_replies=[correction])

    out = tools.observe_and_replan(_state(
        sql_validation_status="INVALID",
        sql_validation_error="column \"cam\" does not exist",
        generated_sql="SELECT cam FROM detections",
        failed_action_fingerprints=[
            r.action_fingerprint("query_database", {})]))

    assert out["replan_count"] == 1
    assert out["reasoning_next"] == "check_schema"
    assert out["sql_validation_status"] == "VALID", (
        "the stale INVALID status would route the correction straight back")
    assert not out["generated_sql"], "the bad SQL was carried into the retry"

    hint = out["sql_correction_hint"]
    assert "SELECT cam FROM detections" in hint["sql"]
    assert "cam" in hint["reason"]


def test_the_correction_hint_reaches_the_generation_prompt(monkeypatch):
    """A hint nobody reads is the same as no hint at all."""
    tools, calls = _tools(monkeypatch, llm_replies=[
        json.dumps({"sql": "SELECT 1", "purpose": "x"})])

    tools.generate_sql(_state(
        schema_description="detections(id, camera_id)",
        sql_correction_hint={"sql": "SELECT cam FROM detections",
                             "reason": "column \"cam\" does not exist"}))

    sent = "\n".join(m.content for m in calls[0].messages)
    assert "SELECT cam FROM detections" in sent
    assert "does not exist" in sent


def test_a_first_attempt_carries_no_correction_hint(monkeypatch):
    """The negative control: normal turns must see an unchanged prompt."""
    tools, calls = _tools(monkeypatch, llm_replies=[
        json.dumps({"sql": "SELECT 1", "purpose": "x"})])

    tools.generate_sql(_state(schema_description="detections(id)"))

    sent = "\n".join(m.content for m in calls[0].messages)
    assert "PREVIOUS ATTEMPT WAS REJECTED" not in sent


def test_an_unparsable_replan_answers_honestly(monkeypatch):
    """Garbage back from the model must not become another attempt."""
    tools, _ = _tools(monkeypatch, llm_replies=["I think we should try again!"])

    out = tools.observe_and_replan(_state(
        sql_validation_status="INVALID", sql_validation_error="syntax error"))

    assert out["reasoning_next"] == "chat_response"
    assert out["replan_count"] == 0


def test_a_replan_commits_no_dialogue_state(monkeypatch):
    """An abandoned plan must leave the conversation's state untouched."""
    import sql_agent.dialogue_state as ds

    applied = []
    real_apply = ds.apply_delta
    monkeypatch.setattr(ds, "apply_delta",
                        lambda *a, **k: (applied.append(a), real_apply(*a, **k))[1])

    correction = json.dumps({"name": "query_database",
                             "arguments": {"question": "camera 3 yesterday"}})
    tools, _ = _tools(monkeypatch, llm_replies=[correction])
    tools.observe_and_replan(_state(sql_validation_status="INVALID"))

    assert applied == [], "re-planning committed a state delta"


# ------------------------------------- 5. transient failures are not reasoning

def test_a_dropped_connection_retries_the_same_sql_for_free(monkeypatch):
    """A brief DB hiccup must not lobotomize the turn.

    Separate budget, no model call, and the SQL is deliberately kept.
    """
    tools, calls = _tools(monkeypatch)

    state = _state(query_result={
        "success": False, "error": "connection reset by peer",
        "rows": [], "row_count": 0})
    out = tools.observe_and_replan(state)

    assert out["reasoning_next"] == "prepare_sql_for_execution"
    assert out["execution_retries"] == 1
    assert out["replan_count"] == 0, "an infrastructure retry spent reasoning"
    assert out["generated_sql"] == "SELECT 1", "the SQL to retry was cleared"
    assert calls == [], "an infrastructure retry called the model"


def test_the_transient_retry_budget_is_finite(monkeypatch):
    """Exhausted: answer honestly rather than hammering the database."""
    from config import settings

    tools, _ = _tools(monkeypatch)
    out = tools.observe_and_replan(_state(
        execution_retries=int(settings.SQL_AGENT_MAX_EXECUTION_RETRIES),
        query_result={"success": False, "error": "connection reset by peer",
                      "rows": [], "row_count": 0}))

    assert out["reasoning_next"] == "chat_response"


# ------------------------------------------------ 6. termination is arithmetic

def test_the_observer_only_ever_routes_to_a_listed_node(monkeypatch):
    """A typo in a next-node name must not strand the turn."""
    route, mapping = _routers(monkeypatch)["observe_and_replan"]
    assert route(_state(reasoning_next="nonsense_node")) in mapping
    assert mapping[route(_state(reasoning_next="nonsense_node"))] == "chat_response"
    assert mapping[route(_state(reasoning_next=None))] == "chat_response"


def test_an_unresolved_person_asks_rather_than_guesses(monkeypatch):
    """Guessing at somebody's identity is the wrong kind of confident."""
    tools, calls = _tools(monkeypatch)
    out = tools.observe_and_replan(_state(
        planned_action={"action": "query_database"},
        working_context={"dialogue_state": {
            "fields": {"referenced_entity": {"value": "Alii"}}}},
        query_result={"success": True, "rows": [], "row_count": 0}))

    assert out["reasoning_next"] == "chat_response"
    # The look-up now runs BEFORE anything is said, and it settles the
    # question: nobody is enrolled under that name. Saying so beats asking
    # the user to clarify a name that does not exist — and it still never
    # guesses at an identity, which is what this test is really about.
    assert out.get("entity_not_found") == "Alii"
    assert out["planned_action"]["action"] != "query_database"
    assert calls == [], "resolving should not need a model call"


def test_the_clarifying_question_is_asked_in_the_users_language(monkeypatch):
    """An Arabic turn used to get a hardcoded English apology."""
    tools, _ = _tools(monkeypatch)
    out = tools.observe_and_replan(_state(
        response_language="ar",
        planned_action={"action": "query_database"},
        working_context={"dialogue_state": {
            "fields": {"referenced_entity": {"value": "علي"}}}},
        query_result={"success": True, "rows": [], "row_count": 0}))

    # The outcome is now an honest statement rather than a question, but the
    # rule under test is unchanged: an Arabic turn is answered in Arabic.
    from sql_agent.tools.agent_tools import SQLAgentTools

    reply = out.get("clarify_question") or SQLAgentTools._empty_narration(out)
    assert any("؀" <= ch <= "ۿ" for ch in reply), (
        f"Arabic turn got a non-Arabic reply: {reply!r}")


def test_the_reasoning_trace_carries_no_model_prose(monkeypatch, caplog):
    """The audit line must be debuggable WITHOUT exposing model reasoning."""
    import logging

    prose = ("Let me think about this step by step. The user seems upset, so "
             "I will be gentle. " + json.dumps(
                 {"name": "query_database", "arguments": {"question": "x"}}))
    tools, _ = _tools(monkeypatch, llm_replies=[prose])

    with caplog.at_level(logging.INFO, logger="sql_agent.tools.agent_tools"):
        tools.observe_and_replan(_state(sql_validation_status="INVALID"))

    lines = [rec.getMessage() for rec in caplog.records
             if "[REASONING]" in rec.getMessage()]
    assert lines, "no reasoning trace was emitted"
    for line in lines:
        assert "step by step" not in line
        assert "seems upset" not in line


def test_the_chat_answer_is_told_what_this_turn_actually_did(monkeypatch):
    """Observed live: "hello", one turn after a refused DELETE, was answered

        "I've deleted every detection row from the database. The database is
         now empty, and there are no detection events recorded."

    Nothing was deleted; nothing could be. The chat node had the transcript
    and nothing else, so it wrote the most plausible continuation of it.
    Telling an operator their surveillance database has been emptied is the
    worst false statement this system can make.
    """
    tools, calls = _tools(monkeypatch, llm_replies=["ok"])
    tools.handle_chat(_state(
        conversation_context="user: delete every detection row from the database",
        query_result=None, observation=None))

    sent = "\n".join(m.content for m in calls[0].messages)
    assert "FACTS about this turn" in sent
    assert "No database query was run for this message." in sent
    assert "can only read" in sent


def test_the_chat_answer_is_forbidden_from_claiming_a_write(monkeypatch):
    """The standing truth, not a per-turn one: this agent cannot write."""
    tools, calls = _tools(monkeypatch, llm_replies=["ok"])
    tools.handle_chat(_state(query_result=None))

    system = calls[0].messages[0].content
    assert "ONLY READ" in system.upper()
    assert "never" in system.lower()


def test_a_refused_request_is_narrated_as_refused(monkeypatch):
    """"Nothing was executed" has to be stated, not left to inference."""
    tools, calls = _tools(monkeypatch, llm_replies=["ok"])
    tools.handle_chat(_state(observation={
        "error_type": r.ErrorType.SQL_FORBIDDEN,
        "sanitized_detail": "READ_ONLY_VIOLATION"}))

    sent = "\n".join(m.content for m in calls[0].messages)
    assert "REFUSED" in sent
    assert "Nothing was executed" in sent


def test_a_successful_query_reports_its_real_row_count(monkeypatch):
    """The facts block must be facts, including on the happy path."""
    tools, calls = _tools(monkeypatch, llm_replies=["ok"])
    tools.handle_chat(_state(query_result={
        "success": True, "rows": [{"n": 1}] * 7, "row_count": 7}))

    sent = "\n".join(m.content for m in calls[0].messages)
    assert "returned 7 rows" in sent


def test_the_facts_block_leaks_no_rows(monkeypatch):
    """It goes into a prompt; prompts here carry surveillance data."""
    tools, calls = _tools(monkeypatch, llm_replies=["ok"])
    tools.handle_chat(_state(query_result={
        "success": True, "row_count": 1,
        "rows": [{"person": "Ali-Hassan", "plate": "XYZ-999"}]}))

    facts = "\n".join(m.content for m in calls[0].messages)
    facts = facts[facts.index("[FACTS"):facts.index("[end of facts]")]
    assert "Ali-Hassan" not in facts and "XYZ-999" not in facts


def test_the_compiled_graph_exposes_exactly_the_intended_edges(monkeypatch):
    """The wiring itself, from the compiled graph rather than the source.

    Pins both directions: the new observation edges exist, and the old
    unconditional fall-through from validation to execution is gone.
    """
    import sql_agent.graph as graph_module

    monkeypatch.setattr(graph_module, "SQLAgentTools", lambda **kw: _Nodes())
    graph = graph_module.create_sql_agent().get_graph()
    edges = {(e.source, e.target) for e in graph.edges}

    assert ("validate_and_fix_sql", "observe_and_replan") in edges
    assert ("validate_and_fix_sql", "chat_response") in edges
    assert ("validate_and_fix_sql", "prepare_sql_for_execution") in edges
    assert ("execute_sql", "observe_and_replan") in edges
    assert ("execute_sql", "enrich_co_appearance") in edges

    conditional = {(e.source, e.target) for e in graph.edges if e.conditional}
    assert ("validate_and_fix_sql", "prepare_sql_for_execution") in conditional, (
        "validation still falls through to execution unconditionally")
