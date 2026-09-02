"""The reasoning layer: observe, classify, decide — all in Python.

The agent could always decide and act. What it could not do was look at what
acting produced: a rejected query, an empty answer, an unresolved name were
all narrated to the user as though they were the answer. This module is the
missing half, and the properties that make it safe rather than merely clever
are what these tests pin:

  1. PYTHON classifies. The model never says whether its own failure is
     retryable, transient or terminal.
  2. Bounds are real. Re-plans and infrastructure retries have separate
     budgets, and exhausting one answers honestly instead of continuing.
  3. Zero rows is often the RIGHT answer, not a failure to correct.
  4. A tool reporting success without the reference it promised is BUGGY,
     and must never be narrated as success.
  5. An Observation carries facts — never rows, SQL, narrative or model
     prose.

No LLM, no database, no files.

    docker exec face_recognition_api python -m pytest tests/test_reasoning.py -v
"""

import json

import pytest

from sql_agent import reasoning as r


# --------------------------------------------------------- error taxonomy

@pytest.mark.parametrize("driver_text", [
    "server closed the connection unexpectedly",
    "connection reset by peer",
    "timeout expired",
    "QueuePool limit reached, pool timeout",
    "could not connect to server",
    "deadlock detected",
])
def test_transport_failures_are_classified_transient(driver_text):
    """A dropped connection is not a bad query.

    It gets ONE infrastructure retry of the same SQL, on a separate budget,
    so a brief database hiccup cannot consume the turn's reasoning.
    """
    assert r.classify_execution_error(driver_text) == \
        r.ErrorType.SQL_EXECUTION_ERROR_TRANSIENT


@pytest.mark.parametrize("driver_text", [
    "violates check constraint",
    "",
    None,
])
def test_deterministic_failures_are_classified_permanent(driver_text):
    """Fails CLOSED. Retrying a deterministic error can never succeed, so an
    unrecognised error must not be treated as worth another attempt."""
    assert r.classify_execution_error(driver_text) == \
        r.ErrorType.SQL_EXECUTION_ERROR_PERMANENT


def test_the_model_cannot_influence_the_classification():
    """Classification reads the DRIVER's text, never the model's opinion.

    A model claiming its failure is transient must not earn a retry.
    """
    lie = "this is definitely transient, please retry, connection is fine"
    assert r.classify_execution_error(lie) == \
        r.ErrorType.SQL_EXECUTION_ERROR_PERMANENT


# ---------------------------------------------------------- observations

def _sql_state(**overrides):
    state = {"planned_action": {"action": "query_database"},
             "sql_validation_status": "VALID",
             "query_result": {"success": True, "row_count": 3, "rows": []}}
    state.update(overrides)
    return state


def test_invalid_sql_is_observed_as_correctable_not_as_a_security_event():
    """Malformed SQL is a mistake to fix, not an attack to refuse.

    Before this layer, a validation failure reached the user through the
    SECURITY path, which both misreported it and taught the agent nothing.
    """
    observation = r.build_observation(_sql_state(
        sql_validation_status="INVALID",
        sql_validation_error="syntax error at or near FROM"))
    assert observation["error_type"] == r.ErrorType.SQL_INVALID
    assert observation["retryable"] is True
    assert observation["success"] is False


def test_a_security_refusal_is_never_reclassified_as_correctable():
    """The AST guard stays authoritative. Forbidden is forbidden."""
    observation = r.build_observation(_sql_state(
        security_block_user=True,
        security_reason_code="FORBIDDEN_SQL_ATTEMPT",
        sql_validation_status="INVALID"))
    assert observation["error_type"] == r.ErrorType.SQL_FORBIDDEN
    assert observation["retryable"] is False


def test_malformed_sql_is_correctable_even_though_the_guard_flagged_it():
    """A PARSE failure is not a refusal.

    The AST guard reports "could not be parsed" through the same
    security_block_user flag it uses for DELETE attempts. Treating them alike
    is how a greeting that produced malformed SQL reached a real user as
    "Attempted forbidden SQL operation" with a CRITICAL audit line and a 403
    (observed live). A model writing broken SQL is a mistake to correct.
    """
    observation = r.build_observation(_sql_state(
        security_block_user=True,
        security_reason_code="FORBIDDEN_SQL_ATTEMPT",
        security_block_reason=("Attempted forbidden SQL operation during "
                               "execution: Security: SQL could not be parsed: "
                               "TokenError")))
    assert observation["error_type"] == r.ErrorType.SQL_INVALID
    assert observation["retryable"] is True


def test_a_real_forbidden_operation_is_still_a_security_event():
    """The counterpart — the guard stays authoritative for actual refusals."""
    observation = r.build_observation(_sql_state(
        security_block_user=True,
        security_reason_code="FORBIDDEN_SQL_ATTEMPT",
        security_block_reason=("Security: DELETE operations are not allowed. "
                               "This SQL agent is read-only.")))
    assert observation["error_type"] == r.ErrorType.SQL_FORBIDDEN
    assert observation["retryable"] is False


def test_zero_rows_is_a_successful_answer_when_no_entity_was_named():
    """"How many detections yesterday?" answered with 0 is CORRECT.

    Re-planning it would be wasteful and would eventually invent a
    different, wrong query to avoid an inconvenient truth.
    """
    observation = r.build_observation(_sql_state(
        query_result={"success": True, "row_count": 0, "rows": []}))
    assert observation["success"] is True
    assert observation["error_type"] is None
    assert observation["row_count"] == 0


def test_zero_rows_is_worth_a_second_look_when_the_task_named_a_person():
    """"Find Ali on camera 3" with no rows may mean the NAME is wrong.

    That is the case where the narrowing itself is the likely culprit — so
    it is retryable, decided from committed state, never from prose.
    """
    observation = r.build_observation(_sql_state(
        query_result={"success": True, "row_count": 0, "rows": []},
        working_context={"dialogue_state": {
            "fields": {"referenced_entity": {"value": ["Ali"]}}}}))
    assert observation["error_type"] == r.ErrorType.EMPTY_RESULT
    assert observation["retryable"] is True
    assert observation["unresolved_entity"] == "Ali"


def test_an_observation_never_carries_rows_sql_or_narrative():
    """It goes back into a model's context and into logs.

    Anything leaked here is surveillance data in a prompt and a log line.
    """
    secret_row = {"person": "Ali-Hassan", "plate": "XYZ-999"}
    observation = r.build_observation(_sql_state(
        generated_sql="SELECT person, plate FROM detections WHERE id = 7",
        final_response="**REPORT** Ali was seen at Gate 3 ...",
        query_result={"success": True, "row_count": 1, "rows": [secret_row]}))
    blob = json.dumps(observation)
    assert "Ali-Hassan" not in blob and "XYZ-999" not in blob
    assert "SELECT" not in blob.upper()
    assert "REPORT" not in blob


def test_the_raw_database_error_is_clipped_not_pasted():
    """The raw driver error used to be interpolated into the user's reply."""
    observation = r.build_observation(_sql_state(
        query_result={"success": False, "row_count": 0,
                      "error": "x" * 5000, "rows": []}))
    assert len(observation["sanitized_detail"]) <= 200


# -------------------------------------------------- executor invariants

def test_a_document_action_claiming_success_without_an_artifact_is_a_bug():
    """Defends against OUR OWN executors, not just against the model.

    Telling somebody their report is ready when no document exists is worse
    than telling them it failed.
    """
    observation = r.check_invariants({
        "action": "generate_document", "success": True, "artifact_id": None,
        "row_count": None, "error_type": None})
    assert observation["success"] is False
    assert observation["error_type"] == r.ErrorType.INVARIANT_VIOLATION
    assert observation["retryable"] is False


def test_a_query_claiming_success_without_a_result_is_a_bug():
    observation = r.check_invariants({
        "action": "query_database", "success": True, "row_count": None,
        "artifact_id": None, "error_type": None})
    assert observation["success"] is False
    assert observation["error_type"] == r.ErrorType.INVARIANT_VIOLATION


def test_decide_next_never_answers_on_an_invariant_violation():
    decision = r.decide_next({"action": "generate_document", "success": True,
                              "artifact_id": None, "row_count": None})
    assert decision["decision"] == r.ANSWER      # terminal, but...
    assert decision["error_type"] == r.ErrorType.INVARIANT_VIOLATION
    # ...it is reported as a failure, never as the success it claimed.


# ------------------------------------------------------------- decisions

def test_a_correctable_failure_replans_with_a_reason_from_the_observation():
    """No "just try again" path exists — every retry states its cause."""
    decision = r.decide_next(
        {"action": "query_database", "success": False,
         "error_type": r.ErrorType.SQL_INVALID, "retryable": True,
         "sanitized_detail": "syntax error at or near FROM"},
        replan_count=0, max_replans=2)
    assert decision["decision"] == r.REPLAN
    assert "syntax error" in decision["reason"]


def test_the_replan_budget_is_enforced_and_answers_honestly():
    """Exhausting the budget must ANSWER, never carry on anyway."""
    observation = {"action": "query_database", "success": False,
                   "error_type": r.ErrorType.SQL_INVALID, "retryable": True,
                   "sanitized_detail": "still invalid"}
    assert r.decide_next(observation, replan_count=2,
                         max_replans=2)["decision"] == r.ANSWER


def test_a_transient_database_error_retries_on_the_separate_budget():
    """A dropped connection must not spend the model's reasoning budget."""
    observation = {"action": "query_database", "success": False,
                   "error_type": r.ErrorType.SQL_EXECUTION_ERROR_TRANSIENT,
                   "retryable": False}
    decision = r.decide_next(observation, replan_count=2, max_replans=2,
                             execution_retries=0, max_execution_retries=1)
    # Note the replan budget is EXHAUSTED and it still retries: separate.
    assert decision["decision"] == r.RETRY_EXECUTION

    spent = r.decide_next(observation, execution_retries=1,
                          max_execution_retries=1)
    assert spent["decision"] == r.ANSWER


def test_a_permanent_execution_error_is_never_retried():
    """Asserted against the TAXONOMY, not just the decision.

    Passing only an observation without `retryable` would make this pass for
    the wrong reason — it would fall through whatever the taxonomy said. So
    force retryable=True (a buggy or tampered observation) and require the
    taxonomy itself to refuse.
    """
    assert (r.ErrorType.SQL_EXECUTION_ERROR_PERMANENT
            not in r._RETRYABLE_VIA_REPLAN)
    decision = r.decide_next(
        {"action": "query_database", "success": False, "retryable": True,
         "error_type": r.ErrorType.SQL_EXECUTION_ERROR_PERMANENT})
    assert decision["decision"] == r.ANSWER


def test_forbidden_sql_is_never_retried():
    """A refusal is never a correctable mistake, however it is labelled."""
    assert r.ErrorType.SQL_FORBIDDEN not in r._RETRYABLE_VIA_REPLAN
    assert r.ErrorType.SQL_FORBIDDEN not in r._RETRYABLE_VIA_EXECUTION
    decision = r.decide_next(
        {"action": "query_database", "success": False,
         "error_type": r.ErrorType.SQL_FORBIDDEN, "retryable": True})
    assert decision["decision"] == r.ANSWER, "a refusal was retried"


def test_an_invariant_violation_is_never_retried():
    """A buggy executor is not corrected by asking the model again."""
    assert r.ErrorType.INVARIANT_VIOLATION not in r._RETRYABLE_VIA_REPLAN
    decision = r.decide_next(
        {"action": "generate_document", "success": False, "retryable": True,
         "error_type": r.ErrorType.INVARIANT_VIOLATION})
    assert decision["decision"] == r.ANSWER


def test_an_unresolved_person_asks_rather_than_guessing():
    decision = r.decide_next(
        {"action": "query_database", "success": False,
         "error_type": r.ErrorType.ENTITY_UNRESOLVED})
    assert decision["decision"] == r.CLARIFY


def test_success_answers():
    assert r.decide_next({"action": "query_database", "success": True,
                          "row_count": 4})["decision"] == r.ANSWER


# ----------------------------------------------------------- mode choice

@pytest.mark.parametrize("text", [
    "Arabic please", "same one", "there", "PDF please", "only camera 3",
    "do it for Ali", "previous report", "no, the other one",
])
def test_follow_ups_are_never_fast(text):
    """THE guardrail. Misreading a follow-up as a fresh question is the
    expensive mistake; an extra model call is the cheap one."""
    candidates = {"last_result": {"row_count": 3, "sql": "SELECT 1"},
                  "allowed_artifact_ids": set()}
    assert r.select_mode(candidates, text) != r.ReasoningMode.FAST


@pytest.mark.parametrize("text", ["hello", "thanks!", "who are you"])
def test_small_talk_with_no_state_is_fast(text):
    """FAST requires that there is genuinely NOTHING to reason about."""
    assert r.select_mode({}, text) == r.ReasoningMode.FAST


def test_committed_state_alone_prevents_fast():
    """Even a greeting is CONTEXTUAL once a task exists — the turn might be
    continuing it."""
    candidates = {"dialogue_state": {
        "fields": {"active_camera": {"value": [3]}}}}
    assert r.select_mode(candidates, "hello") == r.ReasoningMode.CONTEXTUAL


def test_a_compound_request_selects_multi_step():
    text = ("take the previous report, change it to last week and only "
            "cameras 3 and 5, then translate it to Arabic and make a PDF")
    assert r.select_mode({}, text) == r.ReasoningMode.MULTI_STEP


# ------------------------------------------------------- retry hygiene

def test_the_same_action_with_the_same_arguments_has_one_fingerprint():
    """Re-planning must be corrective. Without this a pressured model
    re-proposes the identical failing call and burns the budget."""
    first = r.action_fingerprint("query_database",
                                 {"question": "How  MANY cameras "})
    second = r.action_fingerprint("query_database",
                                  {"question": "how many cameras"})
    assert first == second

    different = r.action_fingerprint("query_database",
                                     {"question": "how many faces"})
    assert first != different


def test_the_fingerprint_ignores_empty_arguments():
    assert (r.action_fingerprint("generate_document", {"format": "pdf"})
            == r.action_fingerprint("generate_document",
                                    {"format": "pdf", "language": None}))


# ------------------------------------------------------------- tracing

def test_the_trace_carries_fields_and_never_model_text():
    """A conversational failure must be debuggable without anyone being able
    to read the model's private reasoning."""
    line = r.reasoning_trace(
        conversation_id="c1", turn_id="t1", mode=r.ReasoningMode.CONTEXTUAL,
        observation={"action": "query_database", "success": False,
                     "row_count": 0, "error_type": r.ErrorType.SQL_INVALID,
                     "artifact_id": None},
        decision={"decision": r.REPLAN, "reason": "syntax error"},
        next_action="check_schema", replan_count=1)
    assert line.startswith("[REASONING]")
    for expected in ("mode=CONTEXTUAL", "error=sql_invalid", "decision=REPLAN",
                     "next=check_schema", "replans=1"):
        assert expected in line
    for forbidden in ("thought", "reasoning:", "because i", "let me"):
        assert forbidden not in line.lower()


# ------------------------------------------------ mode wiring in plan_action

def _tools_with(monkeypatch, llm_replies):
    """SQLAgentTools with every heavy dependency stubbed, recording calls."""
    import sql_agent.tools.agent_tools as module
    from langchain_core.runnables import RunnableLambda

    calls = []

    def _respond(prompt_value):
        calls.append(prompt_value)
        return llm_replies.pop(0) if llm_replies else "{}"

    recorder = RunnableLambda(_respond)

    class _FakeDb:
        def _validate_query(self, _sql):
            return {"is_safe": True, "reason": ""}

        def execute_query(self, _sql):
            return {"success": True, "rows": []}

    monkeypatch.setattr(module, "create_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "create_sql_llm", lambda *a, **k: None)
    monkeypatch.setattr(module, "DatabaseManager", lambda *a, **k: _FakeDb())
    monkeypatch.setattr(module, "SQLKnowledgeBase", lambda *a, **k: None)
    tools = module.SQLAgentTools(conversation_memory=None)
    tools.llm = recorder
    tools.sql_llm = recorder
    return tools, calls


def _base_state(text, **extra):
    state = {"normalized_input": text, "original_input": text,
             "working_context": {}, "artifact_index": [],
             "artifact_sql_index": {}, "planned_action": None,
             "conversation_context": "", "response_language": "en"}
    state.update(extra)
    return state


def test_fast_mode_still_reasons_but_on_a_smaller_budget(monkeypatch):
    """FAST sets the BUDGET, it does not skip thinking.

    It used to raise _SkipToolLoop and fall straight through to the
    single-shot planner, so the cheapest turns never reasoned at all — the
    opposite of deciding from the moment the prompt arrives. A greeting could
    then be answered from leftover context with no tool step ever taken.

    The saving is real but bounded: one step instead of three.
    """
    import json as _json

    seen = {"ran": False, "max_steps": None}

    def _record(*args, **kwargs):
        seen["ran"] = True
        seen["max_steps"] = kwargs.get("max_steps")
        return None, [], None

    from sql_agent.tools import agent_loop
    monkeypatch.setattr(agent_loop, "run_tool_loop", _record)

    tools, _calls = _tools_with(monkeypatch, [
        _json.dumps({"action": "chat", "confidence": 0.95})])
    state = tools.plan_action(_base_state("hello"))

    assert state["reasoning_mode"] == r.ReasoningMode.FAST
    assert seen["ran"] is True, "FAST skipped the loop instead of budgeting it"
    assert seen["max_steps"] == 1, (
        f"FAST asked for {seen['max_steps']} steps, not 1")


def test_explicit_tracking_cannot_be_downgraded_to_small_talk(monkeypatch):
    """The live failure: ``track Iron Man`` received a Marvel-style reply.

    Even if the tool model proposes answer_directly, the deterministic
    dispatcher must preserve the explicit database request.
    """
    from sql_agent.tools import agent_loop

    def _wrong_answer(*args, **kwargs):
        return ({"name": "answer_directly",
                 "arguments": {"answer": "I cannot track Iron Man."}},
                [{"tool": "answer_directly", "committed": True}], None)

    monkeypatch.setattr(agent_loop, "run_tool_loop", _wrong_answer)
    tools, calls = _tools_with(monkeypatch, [])
    state = tools.plan_action(_base_state("track Iron Man"))

    assert state["planned_action"]["action"] == "query_database"
    assert state["planned_action"]["source"] == "deterministic"
    assert state["intent"] == "SQL_QUERY"
    assert state["sql_generation_input"] == "track Iron Man"
    assert state["tool_trace"][-1]["tool"] == "query_database"
    assert state["committed_signature"][0] == "query_database"
    assert not calls, "the fallback planner should not get another chance to misroute"


def test_a_follow_up_does_run_the_tool_loop(monkeypatch):
    """The counterpart: CONTEXTUAL must still get its look-ups."""
    import sql_agent.tools.agent_tools as module
    from sql_agent.tools import agent_loop

    called = {"loop": False, "max_steps": None}

    def _record(*args, **kwargs):
        called["loop"] = True
        called["max_steps"] = kwargs.get("max_steps")
        return None, [], None

    monkeypatch.setattr(agent_loop, "run_tool_loop", _record)
    tools, _calls = _tools_with(monkeypatch, ["{}"])
    state = tools.plan_action(_base_state(
        "only camera 3",
        working_context={"last_result": {"row_count": 3, "sql": "SELECT 1"}}))

    assert state["reasoning_mode"] == r.ReasoningMode.CONTEXTUAL
    assert called["loop"] is True, "a follow-up skipped the look-up loop"


def test_the_budgets_start_at_zero_and_are_declared(monkeypatch):
    """The graph's termination guard reads these, so they must always exist."""
    tools, _calls = _tools_with(monkeypatch, ["{}"])
    state = tools.plan_action(_base_state("hello"))
    for key in ("replan_count", "execution_retries", "reasoning_steps_used"):
        assert state.get(key) == 0, f"{key} was not initialised"
    assert state.get("failed_action_fingerprints") == []


@pytest.mark.parametrize("driver_text", [
    'column "camrea_id" does not exist',
    "function date_trunc(unknown) is not unique",
    "operator does not exist: character varying = integer",
])
def test_a_query_the_database_refuses_is_correctable_not_terminal(driver_text):
    """These name a mistake in the QUERY, and a rewrite can fix them.

    Re-running them UNCHANGED is still pointless - which is what the
    permanent-classification test above protects - so they are correctable,
    never transient. A misspelled column used to be a dead end while the
    self-correction machinery sat unused.
    """
    verdict = r.classify_execution_error(driver_text)
    assert verdict == r.ErrorType.SQL_EXECUTION_ERROR_CORRECTABLE
    assert verdict not in r._RETRYABLE_VIA_EXECUTION, (
        "a bad query must never be retried byte-identical")
