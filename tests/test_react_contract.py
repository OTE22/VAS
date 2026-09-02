"""The ReAct contract, asserted as a property of the system.

Every other suite pins a behaviour. This one pins the ARCHITECTURE, so a
future change cannot quietly leave the agent looking agentic while it has
stopped being so. Each test states a property a production agent framework
has to hold, and checks it against the compiled graph and the live code
rather than against a recollection of the design.

  1. REASON — the loop is the entry point. Nothing decides the turn before
     it, and nothing decides again afterwards.
  2. ACT — every action the model can name maps to a real node, and the
     model can only ever propose: Python validates before anything runs.
  3. OBSERVE — every terminal action is observed. A branch that can fail
     and still end the turn quietly is not part of a ReAct cycle.
  4. LOOP — the cycle is bounded by counters Python owns, so termination
     does not depend on the model behaving.
  5. SAFETY — the authority layers are reachable from every path that
     touches data.

No LLM, no database, no network.

    docker exec face_recognition_api python -m pytest tests/test_react_contract.py -v
"""

import ast
import inspect
import textwrap
from unittest.mock import patch

import pytest


@pytest.fixture(scope="module")
def graph():
    """The real compiled graph, with only the tool object stubbed."""
    import sql_agent.graph as graph_module

    class _Nodes:
        def __getattr__(self, name):
            return lambda state: state

    with patch.object(graph_module, "SQLAgentTools", lambda **kw: _Nodes()):
        return graph_module.create_sql_agent().get_graph()


def _edges(graph):
    return {(e.source, e.target) for e in graph.edges}


# ------------------------------------------------------- 1. REASON first

def test_the_loop_is_the_entry_point(graph):
    """Nothing may decide the turn before the agent reasons about it.

    Two stages used to sit in front of it: an LLM that rewrote the user's
    message, and a regex name-corrector that substituted its guess into the
    text. Both decided things outside the cycle, and both could only lose
    fidelity.
    """
    edges = _edges(graph)
    assert ("detect_malicious_intent", "plan_action") in edges, (
        "something sits between the security gate and the loop again")

    # And whatever remains before it must not call a model.
    import sql_agent.tools.agent_tools as module
    source = inspect.getsource(module.SQLAgentTools.ingest_query)
    tree = ast.parse(textwrap.dedent(source))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "llm" not in attrs and "sql_llm" not in attrs, (
        "a model call crept back in front of the loop")


def test_no_second_decider_runs_after_the_loop():
    """A weaker stage running LAST overrides a better-informed one.

    `classify_intent` — binary CHAT/SQL, no tools, no candidates — used to
    run after the planner and win. Deleting it is what made the cycle single.
    """
    import sql_agent.tools.agent_tools as module

    assert not hasattr(module.SQLAgentTools, "classify_intent"), (
        "the legacy classifier is back and it runs after the loop")


# ------------------------------------------------------------- 2. ACT

def test_every_action_the_model_may_name_reaches_a_real_node(graph):
    """A vocabulary entry with no node routes the turn nowhere, silently."""
    from sql_agent.tools import planner
    from sql_agent.tools.agent_tools import SQLAgentTools

    nodes = SQLAgentTools._ACTION_TO_NODE
    assert set(nodes) == set(planner.ACTIONS), (
        f"actions with no node: {set(planner.ACTIONS) - set(nodes)}")

    graph_nodes = set(graph.nodes)
    for action, node in nodes.items():
        assert node in graph_nodes, f"{action!r} routes to a missing {node!r}"


def test_the_model_proposes_and_python_disposes():
    """The property the whole design rests on.

    Every committed tool call is re-validated before it can run. If this
    stops being true, the model's judgement becomes the security boundary.
    """
    from sql_agent.tools import agent_loop

    tree = ast.parse(textwrap.dedent(inspect.getsource(agent_loop.run_tool_loop)))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}

    assert "validate_call" in called, (
        "the loop commits tool calls without validating them")


def test_an_acting_tool_requires_that_the_user_asked_for_something():
    """"hi" produced a PDF, then a query. Acting needs a request."""
    from sql_agent.tools import agent_loop, tool_registry as tr

    tree = ast.parse(textwrap.dedent(inspect.getsource(agent_loop.run_tool_loop)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "asked_for_an_action" in called, "the intent-fit gate is gone"

    # Answering and asking must stay ungated: they are what a greeting needs.
    assert set(tr.ALWAYS_SAFE_TOOLS) == {"answer_directly",
                                         "ask_clarifying_question"}


# --------------------------------------------------------- 3. OBSERVE

@pytest.mark.parametrize("terminal", [
    "execute_sql", "render_artifact", "translate_artifact",
])
def test_every_terminal_action_is_observed(graph, terminal):
    """A branch that can fail and still end the turn is not in the cycle.

    The document nodes went straight to END, which is how a PDF containing
    "I couldn't reach that report to translate it" was delivered as a
    finished report: the artifact existed, so nothing questioned it.
    """
    targets = {t for s, t in _edges(graph) if s == terminal}
    assert "observe_and_replan" in targets, (
        f"{terminal} can finish a turn without being observed: {targets}")


def test_invalid_sql_is_observed_rather_than_executed(graph):
    """The one outcome that must never happen, whatever the budget says."""
    edges = _edges(graph)
    assert ("validate_and_fix_sql", "observe_and_replan") in edges
    assert ("validate_and_fix_sql", "chat_response") in edges, (
        "with the budget spent there is no honest-failure exit")


def test_the_observation_is_built_in_python_not_by_the_model():
    """If the model wrote the observation it could grade its own homework."""
    from sql_agent import reasoning

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(reasoning.build_observation)))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}

    for forbidden in ("invoke", "llm", "sql_llm", "predict", "generate"):
        assert forbidden not in attrs, (
            f"build_observation calls {forbidden!r}; the observation must be "
            f"derived from state alone")


# ------------------------------------------------------------ 4. LOOP

def test_termination_is_arithmetic_not_behavioural():
    """The routers must read counters, not trust the model to stop."""
    import sql_agent.graph as graph_module

    # EVERY bound must come from settings, checked through the AST. An
    # earlier version of this test looked for the setting's NAME anywhere in
    # the source, so replacing one of the three call sites with a literal
    # still passed — the word survived elsewhere. Presence of a string is
    # not evidence that a bound is honoured.
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(graph_module.create_sql_agent)))
    bounds = [kw for node in ast.walk(tree) if isinstance(node, ast.Call)
              for kw in node.keywords
              if kw.arg in ("max_replans", "max_execution_retries")]
    assert bounds, "the graph no longer bounds re-planning at all"
    for kw in bounds:
        assert not isinstance(kw.value, ast.Constant), (
            f"{kw.arg} is hard-coded to {getattr(kw.value, 'value', '?')!r} "
            f"instead of coming from settings")

    from sql_agent.tools.agent_tools import SQLAgentTools
    act = inspect.getsource(SQLAgentTools._act_on_decision)
    assert "replan_count" in act and "+ 1" in act, (
        "nothing increments the counter the routers read")


def test_only_the_observer_increments_the_budget():
    """Two writers and the bound stops being a bound."""
    import sql_agent.tools.agent_tools as module

    source = inspect.getsource(module)
    writes = [line.strip() for line in source.splitlines()
              if 'state["replan_count"] =' in line]
    assert len(writes) == 1, f"replan_count is written in {len(writes)} places"


def test_a_repeat_of_a_failed_action_is_refused_in_python():
    """The prompt asks; Python enforces. A model under pressure repeats."""
    from sql_agent.tools import agent_tools

    source = inspect.getsource(agent_tools.SQLAgentTools._replan)
    assert "action_fingerprint" in source
    assert "_has_new_information" in source, (
        "the repeat guard no longer allows the one honest exception")


# ---------------------------------------------------------- 5. SAFETY

def test_security_still_gates_the_entry(graph):
    """Reasoning was never allowed to be the first thing that runs."""
    edges = _edges(graph)
    assert ("__start__", "ingest_query") in edges
    assert ("ingest_query", "detect_malicious_intent") in edges
    assert ("detect_malicious_intent", "__end__") in edges, (
        "the security node can no longer end a turn")


def test_the_ast_guard_sits_between_the_agent_and_the_database(graph):
    """Nothing may reach execute_sql except through it."""
    sources = {s for s, t in _edges(graph) if t == "execute_sql"}
    assert sources == {"prepare_sql_for_execution"}, (
        f"execute_sql is reachable from {sources}, bypassing the AST guard")


def test_a_correction_re_enters_the_validated_chain(graph):
    """A re-planned query is not a privileged path into the database."""
    targets = {t for s, t in _edges(graph) if s == "observe_and_replan"}
    assert "check_schema" in targets, "a corrected query skips the SQL chain"
    assert "execute_sql" not in targets, (
        "a correction can reach execution without validation")
