"""The planner decides intent; the dispatcher decides authority.

The agent now recognises six actions instead of two. That is only safe
because nothing the model emits is taken at face value: the candidate set is
built in Python, every field is checked against an allow-list, and an
artifact id that was not offered is discarded even if it is real.

Three failure modes are pinned here, in order of how badly they would hurt:

  1. A planner-supplied artifact_id outside the candidate set being honoured.
     That is cross-user document access with extra steps.
  2. An action-shaped request ("make it Arabic") silently degrading to CHAT
     when planning fails — the exact bug this redesign exists to remove,
     and one that produces a confident, wrong answer rather than an error.
  3. The SQL chain losing a path it used to have.

No LLM is invoked: every function under test is deterministic.

    docker exec face_recognition_api python -m pytest tests/test_agent_planner.py -v
"""

import os

import pytest

from sql_agent.tools import planner

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


ARTIFACT_A = "11111111-1111-4111-8111-111111111111"
ARTIFACT_B = "22222222-2222-4222-8222-222222222222"
FOREIGN = "99999999-9999-4999-8999-999999999999"


def candidates(*, artifacts=None, last_artifact_id=None, last_result=None,
               last_query=None, text="", language="en"):
    return planner.resolve_candidates(
        {"last_artifact_id": last_artifact_id, "last_result": last_result,
         "last_query": last_query, "response_language": language},
        artifacts, text)


def index(*ids, language="en", type="pdf"):
    return [{"artifact_id": i, "type": type, "title": f"Report {n}",
             "language": language, "created_at": None}
            for n, i in enumerate(ids)]


# ------------------------------------------------------------ JSON recovery

def test_a_nested_object_is_recovered_whole():
    """The old classifier used r'\\{[^}]+\\}', which stops at the first '}'.

    On any reply containing a nested object that regex returns a truncated,
    unparseable string — and the code then defaulted to CHAT, which is how a
    document request became small talk.
    """
    raw = '{"action": "generate_document", "meta": {"nested": true}, "format": "pdf"}'
    parsed = planner.extract_json_object(raw)
    assert parsed and parsed["action"] == "generate_document"
    assert parsed["format"] == "pdf"


@pytest.mark.parametrize("raw", [
    '```json\n{"action": "chat", "confidence": 0.9}\n```',
    'Sure! Here is the plan:\n{"action": "chat", "confidence": 0.9}\nHope that helps.',
    '{"action": "chat", "confidence": 0.9}',
    'prose {not json at all} then {"action": "chat", "confidence": 0.9}',
])
def test_json_survives_fences_and_surrounding_prose(raw):
    parsed = planner.extract_json_object(raw)
    assert parsed and parsed["action"] == "chat"


def test_a_brace_inside_a_string_does_not_end_the_object():
    raw = '{"action": "clarify", "clarify_question": "what about {this}?"}'
    parsed = planner.extract_json_object(raw)
    assert parsed["clarify_question"] == "what about {this}?"


@pytest.mark.parametrize("raw", ["", "no json here", "{", "{unclosed: ", None])
def test_garbage_yields_nothing_rather_than_a_guess(raw):
    assert planner.extract_json_object(raw) is None


# ------------------------------------------------------ dispatcher authority

def test_an_artifact_id_outside_the_candidate_set_is_discarded():
    """THE security assertion of this phase.

    A planner that names another user's document must not be able to reach
    it. The id is dropped here; ownership is then re-checked against the
    database before anything is read, so this is defence in depth, not the
    only defence.
    """
    cands = candidates(artifacts=index(ARTIFACT_A), last_artifact_id=ARTIFACT_A)
    plan = planner.validate_plan(
        {"action": "translate_artifact", "artifact_id": FOREIGN, "language": "ar"},
        cands)
    assert plan.artifact_id != FOREIGN, "the planner named a document it was not offered"
    # It falls back to a legitimate candidate rather than executing on nothing.
    assert plan.artifact_id == ARTIFACT_A


def test_a_planner_id_is_ignored_when_the_user_owns_nothing():
    """With no candidates there is nothing to fall back TO, so it clarifies."""
    plan = planner.validate_plan(
        {"action": "translate_artifact", "artifact_id": FOREIGN, "language": "ar"},
        candidates())
    assert plan.action == "clarify"
    assert plan.artifact_id is None


@pytest.mark.parametrize("field,value", [
    ("action", "DROP TABLE users"),
    ("action", "execute_sql"),
    ("action", ""),
    ("action", None),
])
def test_an_action_outside_the_vocabulary_is_refused(field, value):
    assert planner.validate_plan({field: value}, candidates()) is None


def test_format_and_language_are_allow_listed():
    """A format the renderer does not have would fail deep in the stack."""
    plan = planner.validate_plan(
        {"action": "generate_document", "format": "exe", "language": "klingon"},
        candidates(last_result={"row_count": 3, "sql": "SELECT 1"}))
    assert plan.format == "pdf", "an unknown format must not survive"
    assert plan.language in planner.LANGUAGES


def test_confidence_is_clamped_not_trusted():
    for raw, expected in ((5.0, 1.0), (-2.0, 0.0), ("high", 0.5), (None, 0.5)):
        plan = planner.validate_plan({"action": "chat", "confidence": raw}, candidates())
        assert plan.confidence == expected


def test_the_planner_cannot_supply_sql_or_a_path():
    """Those are not fields. Anything extra is ignored, never carried."""
    plan = planner.validate_plan(
        {"action": "query_database", "sql": "DROP TABLE users",
         "storage_path": "../../etc/passwd", "user_id": 999},
        candidates())
    carried = plan.as_dict()
    assert "sql" not in carried and "storage_path" not in carried
    assert "user_id" not in carried
    assert "DROP" not in repr(carried)


# ------------------------------------------------- deterministic resolution

def test_an_id_the_user_typed_wins_but_only_if_it_is_theirs():
    cands = candidates(artifacts=index(ARTIFACT_A, ARTIFACT_B),
                       last_artifact_id=ARTIFACT_B,
                       text=f"translate {ARTIFACT_A} to Arabic")
    assert cands["explicit_artifact_id"] == ARTIFACT_A
    assert planner.default_artifact_id(cands) == ARTIFACT_A

    foreign = candidates(artifacts=index(ARTIFACT_A),
                         text=f"translate {FOREIGN} to Arabic")
    assert foreign["explicit_artifact_id"] is None, "an unowned id was accepted"


def test_an_unqualified_reference_prefers_what_this_session_produced():
    cands = candidates(artifacts=index(ARTIFACT_A, ARTIFACT_B),
                       last_artifact_id=ARTIFACT_B)
    assert planner.default_artifact_id(cands) == ARTIFACT_B


def test_two_candidates_and_no_signal_is_ambiguous_not_a_guess():
    cands = candidates(artifacts=index(ARTIFACT_A, ARTIFACT_B))
    assert planner.default_artifact_id(cands) is None


# ------------------------------------------------------------ preconditions

def test_a_new_document_is_not_given_an_unrelated_parent():
    """Lineage must record relationships that actually happened.

    Binding an unqualified reference is right for translate and modify, which
    act ON a document. It is wrong for generate_document, which renders the
    CURRENT result — doing it there made every new report claim the newest
    unrelated artifact as its parent, which is falsified provenance and would
    send a later "same report but camera 3" to the wrong source SQL.
    """
    cands = candidates(artifacts=index(ARTIFACT_A), last_artifact_id=ARTIFACT_A,
                       last_result={"row_count": 4, "sql": "SELECT 1"})
    plan = planner.validate_plan({"action": "generate_document", "format": "pdf"}, cands)
    assert plan.action == "generate_document"
    assert plan.artifact_id is None, (
        "a fresh document was bound to an unrelated artifact as its parent")

    # The actions that DO act on a document still resolve one.
    translate = planner.validate_plan({"action": "translate_artifact"}, cands)
    assert translate.artifact_id == ARTIFACT_A


def test_translating_with_nothing_rendered_yet_generates_instead():
    """There IS something to say — refusing on a technicality is unhelpful."""
    plan = planner.validate_plan(
        {"action": "translate_artifact", "language": "ar"},
        candidates(last_result={"row_count": 5, "sql": "SELECT 1"}))
    assert plan.action == "generate_document"
    assert plan.language == "ar" and plan.format == "pdf"


def test_translating_with_nothing_at_all_asks_a_question():
    plan = planner.validate_plan({"action": "translate_artifact"}, candidates())
    assert plan.action == "clarify" and plan.clarify_question


def test_a_document_request_with_no_source_asks_a_question():
    plan = planner.validate_plan({"action": "generate_document"}, candidates())
    assert plan.action == "clarify"
    assert "report on" in plan.clarify_question or plan.clarify_question


def test_modifying_with_no_previous_query_asks_a_question():
    plan = planner.validate_plan(
        {"action": "modify_previous_query", "modification": "camera 3"}, candidates())
    assert plan.action == "clarify"


def test_translate_without_a_language_flips_the_current_one():
    plan = planner.validate_plan(
        {"action": "translate_artifact"},
        candidates(artifacts=index(ARTIFACT_A), last_artifact_id=ARTIFACT_A,
                   language="en"))
    assert plan.action == "translate_artifact" and plan.language == "ar"


# ------------------------------------------------------- proposed StateDelta

def test_a_valid_proposed_delta_is_carried_validated():
    """The planner may PROPOSE a dialogue-state change; validate_plan runs it
    through dialogue_state.validate_delta and carries the normalized form.
    Commit still happens elsewhere, only after the action succeeds."""
    plan = planner.validate_plan(
        {"action": "modify_previous_query", "modification": "camera 4 instead",
         "state_delta": {"operation": "REPLACE", "field": "active_camera",
                         "proposed_value": [4], "source": "user_correction"}},
        candidates(last_result={"row_count": 1, "sql": "SELECT 1"}))
    assert plan.state_delta is not None
    assert plan.state_delta["operation"] == "REPLACE"
    assert plan.state_delta["field"] == "active_camera"
    assert plan.state_delta["source"] == "user_correction"


def test_an_invalid_proposed_delta_is_dropped_not_fatal():
    """A model inventing a field loses the DELTA, never the ACTION."""
    plan = planner.validate_plan(
        {"action": "modify_previous_query", "modification": "camera 4",
         "state_delta": {"operation": "OVERWRITE_ALL", "field": "sql_to_run",
                         "proposed_value": "DROP TABLE users"}},
        candidates(last_result={"row_count": 1, "sql": "SELECT 1"}))
    assert plan.action == "modify_previous_query", "the action was lost"
    assert plan.state_delta is None, "an invalid delta was carried"


def test_a_proposed_delta_commits_only_after_the_action_succeeds():
    """"No, camera 4" updates state once the camera-4 query RAN — a proposal
    whose action failed taught us nothing and must commit nothing."""
    from sql_agent.agent import SQLIntelligenceAgent
    from sql_agent import dialogue_state as ds

    class _StubMemory:
        def __init__(self):
            self.current_session_id = "stub"
            self.saved = {}

        def get_working_context(self, reload=False):
            return dict(self.saved)

        def update_working_context(self, **fields):
            self.saved.update(fields)
            return True

    agent = SQLIntelligenceAgent.__new__(SQLIntelligenceAgent)  # no heavy init
    agent.conversation_memory = _StubMemory()

    delta = {"operation": "REPLACE", "field": "active_camera",
             "proposed_value": [4], "source": "user_correction"}

    # Failed action: nothing commits.
    agent._commit_tool_result_deltas("no, camera 4", {
        "planned_action": {"action": "modify_previous_query",
                           "state_delta": delta},
        "query_result": {"success": False},
    })
    state = ds.migrate_state(agent.conversation_memory.saved.get("dialogue_state"))
    assert ds.get_value(state, "active_camera") is None, (
        "a delta committed although its action failed")

    # Successful action: the correction lands, with its provenance.
    agent._commit_tool_result_deltas("no, camera 4", {
        "planned_action": {"action": "modify_previous_query",
                           "state_delta": delta},
        "query_result": {"success": True},
        "sql_purpose": "detections on camera 4",
    })
    state = ds.migrate_state(agent.conversation_memory.saved.get("dialogue_state"))
    assert ds.get_value(state, "active_camera") == [4]
    assert ds.get_provenance(state, "active_camera")["source"] == "user_correction"
    assert ds.get_value(state, "active_task") == "detections on camera 4"


# ------------------------------------------------------------- failure path

def test_an_action_shaped_request_never_degrades_to_small_talk():
    """The headline bug: "make it Arabic" answered as conversation.

    When planning fails on a short follow-up in a session that HAS state, the
    honest answer is a question. Answering it as chat produces fluent,
    confident nonsense about a document the chat model cannot see.
    """
    cands = candidates(artifacts=index(ARTIFACT_A), last_artifact_id=ARTIFACT_A)
    plan = planner.decide_on_failure("make it Arabic", cands)
    assert plan is not None and plan.action == "clarify"
    assert plan.source == "fallback"


def test_an_ordinary_question_still_falls_back_to_the_old_classifier():
    """Returning None here is what preserves the pre-planner behaviour.

    Forcing every failed plan into a clarification would make the agent worse
    for the majority of users, who never ask for a document at all.
    """
    cands = candidates()
    assert planner.decide_on_failure("how many people were detected today", cands) is None


def test_a_long_standalone_question_is_not_treated_as_a_follow_up():
    cands = candidates(artifacts=index(ARTIFACT_A), last_artifact_id=ARTIFACT_A)
    long_question = ("please tell me how many distinct people were detected by "
                     "the north gate camera between Monday and Friday last week")
    assert planner.decide_on_failure(long_question, cands) is None


# --------------------------------------------------------------- the context

def test_the_context_block_never_carries_result_rows():
    """It goes into a PROMPT. Rows are surveillance data."""
    secret = "Ali-Hassan-License-XYZ"
    cands = candidates(last_result={
        "row_count": 2, "columns": ["person", "camera"],
        "preview": [{"person": secret, "camera": "Gate 3"}],
        "sql": "SELECT person FROM detections", "purpose": "tracking"})
    block = planner.build_planner_context(cands)
    assert secret not in block, "a result row reached the planner prompt"
    assert "SELECT" not in block, "SQL reached the planner prompt"
    assert "2 row(s)" in block


def test_the_context_block_lists_ids_the_planner_may_choose_from():
    block = planner.build_planner_context(
        candidates(artifacts=index(ARTIFACT_A, ARTIFACT_B)))
    assert ARTIFACT_A in block and ARTIFACT_B in block


# --------------------------------------------------------------- audit line

def test_the_audit_line_records_actions_not_thoughts():
    """Model reasoning is unverifiable and duplicates sensitive text."""
    plan = planner.PlannedAction(action="translate_artifact", confidence=0.9,
                                 artifact_id=ARTIFACT_A)
    line = planner.audit_line(user_id=7, conversation_id="c1", plan=plan,
                              executed="translate_artifact", resolution="planner",
                              artifact_id=ARTIFACT_A, result_id=42)
    assert line.startswith("[AGENT_AUDIT]")
    for expected in ("user_id=7", "action=translate_artifact", f"artifact={ARTIFACT_A}",
                     "result=42", "outcome=ok"):
        assert expected in line
    for forbidden in ("prompt", "reasoning", "thought"):
        assert forbidden not in line.lower()


# ------------------------------------------------------------------ routing

def test_every_action_routes_somewhere_the_graph_defines():
    """A typo in either map would silently strand a whole branch.

    Both maps must cover every action, and every node named by the node map
    must be a node the graph actually registers — a name that does not exist
    routes the turn nowhere, with no error at the point of the mistake.
    """
    import inspect
    from sql_agent.tools.agent_tools import SQLAgentTools
    from sql_agent import graph as graph_module

    intents = SQLAgentTools._ACTION_TO_INTENT
    nodes = SQLAgentTools._ACTION_TO_NODE
    assert set(intents) == set(planner.ACTIONS), (
        f"actions with no intent: {set(planner.ACTIONS) - set(intents)}")
    assert set(nodes) == set(planner.ACTIONS), (
        f"actions with no node: {set(planner.ACTIONS) - set(nodes)}")
    assert set(intents.values()) <= {"CHAT", "SQL_QUERY", "HYBRID"}

    source = inspect.getsource(graph_module.create_sql_agent)
    for action, node in nodes.items():
        assert f'workflow.add_node("{node}"' in source, (
            f"action {action!r} routes to {node!r}, which the graph never adds")


def test_each_document_action_has_its_own_node():
    """They map onto CHAT for compatibility; they must not RUN as chat.

    While generate_document routed to chat_response, "make that a PDF" was
    answered by the chat model — fluent text about a document that did not
    exist. The intent map still says CHAT so downstream readers of `intent`
    keep working, which is exactly why the node map has to be checked too.
    """
    from sql_agent.tools.agent_tools import SQLAgentTools
    nodes = SQLAgentTools._ACTION_TO_NODE
    assert nodes["generate_document"] == "render_artifact"
    assert nodes["translate_artifact"] == "translate_artifact"
    assert nodes["generate_document"] != "chat_response"
    assert nodes["translate_artifact"] != "chat_response"


def test_the_sql_chain_keeps_the_entry_it_always_had():
    """query_database must still reach check_schema, unchanged."""
    import inspect
    from sql_agent import graph as graph_module
    source = inspect.getsource(graph_module.create_sql_agent)
    assert 'workflow.add_edge("check_schema", "retrieve_examples")' in source
    assert '"chat_response": "chat_response"' in source
    assert '"check_schema": "check_schema"' in source
    # After deterministic ingestion and security, nothing may make a semantic
    # decision before the loop. Name resolution belongs inside that cycle.
    assert '"continue": "plan_action"' in source


def test_a_modified_query_passes_through_the_same_validation_chain():
    """A modification is not a privileged path into the database.

    modify_sql produces SQL from a model, exactly like generate_sql does, so
    it must enter validate_and_fix_sql — where the AST authorization guard
    lives — by the same edge. If it ever went straight to execution, "change
    it to delete those rows" would bypass every check the system has.
    """
    import inspect
    from sql_agent import graph as graph_module
    source = inspect.getsource(graph_module.create_sql_agent)

    assert 'workflow.add_edge("modify_sql", "validate_and_fix_sql")' in source
    assert 'workflow.add_edge("generate_sql", "validate_and_fix_sql")' in source
    # and it must NOT have a shortcut past validation
    for shortcut in ("prepare_sql_for_execution", "execute_sql"):
        assert f'workflow.add_edge("modify_sql", "{shortcut}")' not in source, (
            f"modify_sql reaches {shortcut} without passing the AST guard")


def test_a_users_lock_survives_their_agent_being_evicted():
    """Agent lifetime and lock lifetime are different concerns.

    The agent LRU (cap 10) used to pop the user's asyncio.Lock together with
    their agent. But _get_or_create_user_agent runs BEFORE the lock is
    acquired, so with 11+ active users a turn could be IN FLIGHT holding lock
    object L when its user was evicted; L was dropped from the dict, the same
    user's second tab then got a brand-new lock, and two turns ran
    concurrently for one user — precisely the corruption the lock exists to
    prevent.

    The rule: a synchronization primitive must remain reachable while it is
    held or awaited. Only a lock with no holder and no waiters may be
    reclaimed.

    Reproduction: user 1's lock is acquired (a turn in flight); ten more
    users create agents, forcing user 1's agent out of the LRU; the second
    tab asks for user 1's lock. It must receive the SAME object — a fresh
    lock is exactly the bug.
    """
    import sql_agent.agent as agent_module
    import sql_agent.conversation_memory as memory_module
    from conftest import run_on_shared_loop
    from sql_agent.api import routes

    # Save and clear module state; restore no matter what. The heavy agent
    # and memory constructors are stubbed so eleven "agents" cost nothing and
    # write no session files — the code under test is the LRU/lock
    # bookkeeping, which runs unchanged.
    saved = (dict(routes._user_agents), dict(routes._user_query_locks),
             dict(routes._user_agent_versions),
             agent_module.SQLIntelligenceAgent,
             memory_module.ConversationMemory)

    class _StubMemory:
        def __init__(self, user_id=None, **_kwargs):
            self.user_id = user_id
            self.current_session_id = f"stub_{user_id}"

        def start_session(self, *_a, **_k):
            return self.current_session_id

    class _StubAgent:
        def __init__(self, conversation_memory=None):
            self.conversation_memory = conversation_memory

    routes._user_agents.clear()
    routes._user_query_locks.clear()
    routes._user_agent_versions.clear()
    agent_module.SQLIntelligenceAgent = _StubAgent
    memory_module.ConversationMemory = _StubMemory

    async def _scenario():
        base = 9_000_000  # ids far outside any real user's range
        first = base + 1

        # Turn in flight: the REAL request path takes the lock only after
        # _get_or_create_user_agent has run, so this ordering is faithful.
        routes._get_or_create_user_agent(first)
        held_lock = routes._get_user_lock(first)
        await held_lock.acquire()
        try:
            # Ten more users arrive; the cap is 10, so user `first` is evicted
            # by the PRODUCTION eviction loop inside _get_or_create_user_agent.
            for offset in range(2, 2 + routes._USER_AGENTS_MAX):
                routes._get_or_create_user_agent(base + offset)

            assert first not in routes._user_agents, (
                "setup failed: the first user was not evicted from the LRU")

            second_tab_lock = routes._get_user_lock(first)
            assert second_tab_lock is held_lock, (
                "eviction dropped a HELD lock — the same user's second tab "
                "got a fresh lock and both turns would run concurrently")
            assert second_tab_lock.locked(), "the held lock lost its state"
        finally:
            held_lock.release()

        # Once nothing holds or waits on it, reclamation is allowed again.
        routes._maybe_release_user_lock(first)
        assert first not in routes._user_query_locks, (
            "an idle lock with no holder and no waiters should be reclaimed")

    try:
        run_on_shared_loop(_scenario())
    finally:
        routes._user_agents.clear(); routes._user_agents.update(saved[0])
        routes._user_query_locks.clear(); routes._user_query_locks.update(saved[1])
        routes._user_agent_versions.clear(); routes._user_agent_versions.update(saved[2])
        agent_module.SQLIntelligenceAgent = saved[3]
        memory_module.ConversationMemory = saved[4]


def test_blocking_session_file_writes_never_run_on_the_event_loop():
    """Session-file writes must be threaded, not awaited inline.

    `update_working_context` -> `save_session` does read + json.dump + fsync +
    os.replace under a **threading.Lock** that the agent's own stream THREAD
    also takes. Called directly from an async function, the event loop waits
    on a lock held by a worker thread, and the whole process stalls — every
    request, including /health/live.

    That happened live on 2026-08-30: six minutes of total log silence, not
    even the loop-lag watchdog (which could not run either), 504s on health,
    while a user's query sat wedged. The fix is `run_in_threadpool`; this
    test is the net.

    AST-based: it walks async function bodies in routes.py and flags a direct
    call to a known-blocking memory writer that is not wrapped.
    """
    import ast

    blocking_calls = {"update_working_context", "save_session",
                      "_remember_result_row_id"}
    source = _route_source_planner()
    tree = ast.parse(source)
    offenders = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        # Calls made directly in the async body — excluding anything inside a
        # nested plain `def`, which is what gets handed to run_in_threadpool.
        nested_sync = {n for sub in ast.walk(node)
                       if isinstance(sub, ast.FunctionDef)
                       for n in ast.walk(sub)}
        for call in ast.walk(node):
            if call in nested_sync or not isinstance(call, ast.Call):
                continue
            name = None
            if isinstance(call.func, ast.Attribute):
                name = call.func.attr
            elif isinstance(call.func, ast.Name):
                name = call.func.id
            if name in blocking_calls:
                offenders.append(f"{node.name}:{call.lineno} -> {name}")

    assert not offenders, (
        "blocking session-file writes called directly from async code — wrap "
        f"them in run_in_threadpool: {offenders}")


def _route_source_planner():
    import io as _io
    import os as _os
    path = _os.path.join(REPO, "sql_agent", "api", "routes.py")
    with _io.open(path, encoding="utf-8") as handle:
        return handle.read()


def test_no_test_residue_is_visible_in_any_real_users_sidebar():
    """The chat store must not contain conversations a TEST wrote.

    The dev stack has no row-level test isolation, so a suite that persists a
    turn writes into the real conversation store — and one that cleans up
    wrong leaves its marker in a real user's sidebar. That happened: ten
    `cancel_survival_probe_query` threads, one per run of the streaming
    suite, plus 31 orphaned threads from deleted probe users.

    This converts "the user noticed junk in their sidebar" into a red test.
    The fingerprints are the literal strings the test files write; keep this
    list in sync when adding a suite that persists turns — or better, use the
    `chat_sandbox` fixture and never appear here at all.
    """
    from sqlalchemy import text as sa_text
    from conftest import run_on_shared_loop

    test_titles = [
        "cancel_survival_probe_query",
        "A's private thread", "Private to A", "Renamed thread",
        "Persistence check", "Explicit target", "Branching", "A's target",
        "Feedback", "Uniquely Named Falcon Thread",
        "how many cameras?", "targeted question", "intrusion attempt",
        "qa deletion probe",
    ]
    test_sessions = ["dualwrite_probe_session", "some_other_session",
                     "b_fallback_session"]

    async def _check():
        from db_connection import db_manager
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            rows = (await db.execute(sa_text("""
                SELECT c.id, c.title, c.user_id
                FROM conversations c
                WHERE c.deleted_at IS NULL
                  AND (c.title = ANY(:titles)
                       OR c.legacy_session_id = ANY(:sessions))
            """), {"titles": test_titles, "sessions": test_sessions})).fetchall()
        return rows

    leaked = run_on_shared_loop(_check())
    assert not leaked, (
        f"{len(leaked)} test conversation(s) are live in the chat store — a "
        f"suite leaked again. Offenders: "
        f"{[(str(r[0])[:8], r[1], r[2]) for r in leaked[:5]]}. "
        f"Clean with scripts/cleanup_test_conversations.py and migrate the "
        f"leaking suite to the chat_sandbox fixture.")


def test_no_test_module_imports_the_conftest_under_a_package_path():
    """`from tests.conftest import ...` creates a SECOND shared event loop.

    Pytest imports the root conftest as the top-level module `conftest`.
    Importing the same file again as `tests.conftest` produces a distinct
    module object with its own `SHARED_LOOP = asyncio.new_event_loop()`. The
    database engine is then bound to whichever loop ran first, and any suite
    using the other one fails with "attached to a different loop" — in a
    DIFFERENT test file, only when the two are run together. This cost real
    time to diagnose once; the rule is cheap to keep.

    Scanned with the AST, not with substring matching: the comment explaining
    this very rule contains the offending text, so a substring scan flags the
    explanation — the same mistake the config guard made before it.
    """
    import ast
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path(REPO, "tests").glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "tests.conftest":
                offenders.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "tests.conftest":
                        offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"these import the conftest under a package path: {offenders}. "
        f"Use `from conftest import ...` so there is ONE shared event loop.")


def test_a_modification_that_returns_forbidden_sql_reaches_the_shared_ast_gate():
    """Modification has no private validator and no private authority.

    The model is replaced with one that returns forbidden SQL directly. The
    modification stage may extract that candidate, but only the shared AST
    stage may authorize it and that stage must reject it.
    """
    from langchain_core.runnables import RunnableLambda
    from sql_agent.tools import agent_tools as module

    tools = module.SQLAgentTools(conversation_memory=None)

    forbidden = ('{"sql": "DELETE FROM detections WHERE camera_id = 3", '
                 '"purpose": "remove them"}')
    original = module.create_sql_llm
    module.create_sql_llm = lambda *_a, **_k: RunnableLambda(lambda _x: forbidden)
    try:
        state = {
            "planned_action": {"action": "modify_previous_query",
                               "modification": "only camera 3"},
            "normalized_input": "same report but only for camera 3",
            "working_context": {"last_result": {
                "sql": "SELECT count(*) FROM detections", "purpose": "count"}},
            "schema_description": "detections(id, camera_id)",
        }
        result = tools.modify_sql(state)
    finally:
        module.create_sql_llm = original

    assert result.get("generated_sql", "").startswith("DELETE FROM"), (
        "the modification stage did more than extract an untrusted candidate")
    assert not result.get("validated_sql"), (
        "the modification stage incorrectly authorized its own output")

    result = tools.validate_and_fix_sql(result)
    assert result.get("sql_validation_status") == "INVALID"
    assert not result.get("validated_sql"), (
        "forbidden modified SQL survived the shared AST policy")


def test_query_modification_has_its_own_task_type():
    """Rewriting valid SQL is not repairing broken SQL.

    Overloading SQL_REPAIR would tell the model its input is wrong when it is
    not, corrupting both prompts' purpose. The new type must still route to
    the SQL specialist, not to the chat model.
    """
    from sql_agent.llm import TaskType
    from sql_agent.llm.registry import _SQL_TASKS

    assert hasattr(TaskType, "SQL_MODIFICATION")
    assert TaskType.SQL_MODIFICATION != TaskType.SQL_REPAIR
    assert TaskType.SQL_MODIFICATION in _SQL_TASKS, (
        "query modification would be routed to the chat model")


def test_routing_falls_back_to_intent_when_there_is_no_plan():
    """The legacy path must still work when the planner is unavailable."""
    import inspect
    from sql_agent import graph as graph_module
    source = inspect.getsource(graph_module.create_sql_agent)
    assert 'state.get("intent", "CHAT")' in source, (
        "no legacy fallback left in the router")
