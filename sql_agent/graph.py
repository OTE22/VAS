"""
Graph Module
============
LangGraph workflow definition for the SQL Intelligence Agent.
"""

import logging
from langgraph.graph import StateGraph, END, START

from config import settings
from .state import AgentState
from .tools import SQLAgentTools
from .run_control import traced_node

logger = logging.getLogger(__name__)


# The only nodes a correction may enter. Listing them here rather than
# deriving them means a new action cannot silently become reachable from the
# reasoning loop without someone adding it on purpose.
_OBSERVATION_TARGETS = (
    "check_schema",              # re-planned query, from the top of the chain
    "prepare_sql_for_execution",  # transient DB failure: the SAME SQL again
    "render_artifact",
    "translate_artifact",
    "chat_response",             # honest failure, or a clarifying question
    "plan_action",               # the request needs another action (multi-step)
    "enrich_co_appearance",      # the action WORKED: narrate it with its data
)


def create_sql_agent(conversation_memory=None, db=None) -> StateGraph:
    """Create the SQL Intelligence Agent workflow with RAG.

    `db` is the owning agent's DatabaseManager. Passing it is what makes a
    policy set on the agent (the per-turn camera scope) govern the SQL the
    graph runs; without it the tools hold a separate, unscoped instance.
    """
    tools = SQLAgentTools(conversation_memory=conversation_memory, db=db)

    # Create the graph
    workflow = StateGraph(AgentState)

    # Add nodes
    workflow.add_node("ingest_query", traced_node("ingest_query", tools.ingest_query))
    workflow.add_node("detect_malicious_intent", traced_node("detect_malicious_intent", tools.detect_malicious_intent))
    workflow.add_node("plan_action", traced_node("plan_action", tools.plan_action))
    workflow.add_node("check_schema", traced_node("check_schema", tools.check_schema))
    workflow.add_node("retrieve_examples", traced_node("retrieve_examples", tools.retrieve_examples))  # RAG retrieval
    workflow.add_node("generate_sql", traced_node("generate_sql", tools.generate_sql))
    workflow.add_node("modify_sql", traced_node("modify_sql", tools.modify_sql))
    workflow.add_node("validate_and_fix_sql", traced_node("validate_and_fix_sql", tools.validate_and_fix_sql))
    workflow.add_node("prepare_sql_for_execution", traced_node("prepare_sql_for_execution", tools.prepare_sql_for_execution))
    workflow.add_node("execute_sql", traced_node("execute_sql", tools.execute_sql))
    workflow.add_node("observe_and_replan", traced_node("observe_and_replan", tools.observe_and_replan))
    workflow.add_node("enrich_co_appearance", traced_node("enrich_co_appearance", tools.enrich_co_appearance))
    workflow.add_node("story_response", traced_node("story_response", tools.generate_story_response))
    workflow.add_node("learn_from_query", traced_node("learn_from_query", tools.learn_from_query))  # Learning step
    workflow.add_node("chat_response", traced_node("chat_response", tools.handle_chat))
    workflow.add_node("render_artifact", traced_node("render_artifact", tools.render_artifact))
    workflow.add_node("translate_artifact", traced_node("translate_artifact", tools.translate_artifact))

    # Define routing function
    def route_by_action(state: AgentState) -> str:
        """Route on the planned action, falling back to the classic intent.

        Falling back on `intent` keeps every path the SQL chain already had,
        byte for byte: when no plan exists — the planner was unavailable and
        the legacy classifier ran — this behaves exactly as it did before the
        planner existed. The document branches only ADD destinations.
        """
        planned = state.get("planned_action") or {}
        action = planned.get("action")
        if action in ("query_database", "modify_previous_query"):
            return "check_schema"
        if action == "generate_document":
            return "render_artifact"
        if action == "translate_artifact":
            return "translate_artifact"
        if action in ("chat", "clarify"):
            return "chat_response"
        # No plan (planner unavailable, legacy fallback): classic behaviour.
        return "chat_response" if state.get("intent", "CHAT") == "CHAT" else "check_schema"

    # A deterministic, non-semantic boundary is first. It preserves the raw
    # request and creates the canonical form consumed by every later stage.
    workflow.add_edge(START, "ingest_query")

    def route_after_ingestion(state: AgentState) -> str:
        if state.get("input_normalization_error"):
            return "reject"
        return "continue"

    workflow.add_conditional_edges(
        "ingest_query",
        route_after_ingestion,
        {"reject": "chat_response", "acknowledge": "chat_response",
         "continue": "detect_malicious_intent"},
    )
    
    # Define routing function to check if user should be blocked
    def check_security_block(state: AgentState) -> str:
        """Check if user should be blocked - if so, skip to end"""
        if state.get("security_block_user"):
            logger.error("[SECURITY] Routing to END - User blocked in STEP 0")
            return "end_security_block"
        return "continue"
    
    # Add conditional edge after the security scan.
    workflow.add_conditional_edges(
        "detect_malicious_intent",
        check_security_block,
        {
            "end_security_block": END,  # Block user and end immediately
            "continue": "plan_action"
        }
    )
    
    # Conditional routing after planning
    workflow.add_conditional_edges(
        "plan_action",
        route_by_action,
        {
            "chat_response": "chat_response",
            "check_schema": "check_schema",
            "render_artifact": "render_artifact",
            "translate_artifact": "translate_artifact"
        }
    )

    # SQL path with RAG
    workflow.add_edge("check_schema", "retrieve_examples")  # Add RAG step
    # A modification rewrites an EXISTING query; a fresh question generates
    # one. Both then continue down the identical chain, so the AST guard in
    # validate_and_fix_sql sees a modified query exactly as it sees any other.
    def route_sql_source(state: AgentState) -> str:
        planned = state.get("planned_action") or {}
        if planned.get("action") == "modify_previous_query":
            return "modify_sql"
        return "generate_sql"

    workflow.add_conditional_edges(
        "retrieve_examples",
        route_sql_source,
        {
            "generate_sql": "generate_sql",
            "modify_sql": "modify_sql"
        }
    )
    workflow.add_edge("generate_sql", "validate_and_fix_sql")
    workflow.add_edge("modify_sql", "validate_and_fix_sql")
    # SQL that failed validation must NOT be executed.
    #
    # Before this, `validate_and_fix_sql` fell through unconditionally: on
    # INVALID/PARTIAL/ERROR the ORIGINAL, known-bad SQL continued to
    # `prepare_sql_for_execution`, where the AST guard blocked it by setting
    # `security_block_user` — so a model writing broken SQL was reported to
    # the user as an attempted forbidden operation. Correct the query, or
    # fail honestly; never execute it and never dress a mistake up as an
    # intrusion.
    def route_after_validation(state: AgentState) -> str:
        status = state.get("sql_validation_status")
        if status in ("VALID", "FIXED"):
            logger.info("[REASONING] validation=%s -> executing", status)
            return "execute"

        # INVALID / PARTIAL / ERROR. PARTIAL is included deliberately: it
        # means the fix ALSO failed to validate and the original bad SQL was
        # kept, which is no safer to run than INVALID.
        replans = int(state.get("replan_count") or 0)
        if replans < int(settings.SQL_AGENT_MAX_REPLANS):
            return "observe"

        # Budget spent. Answer honestly — never "run it anyway because the
        # re-plan limit was reached".
        logger.info(
            "[REASONING] SQL did not validate (%s) and the re-plan budget is "
            "exhausted; failing safely without executing", status)
        return "fail_safely"

    workflow.add_conditional_edges(
        "validate_and_fix_sql",
        route_after_validation,
        {
            "execute": "prepare_sql_for_execution",
            "observe": "observe_and_replan",
            "fail_safely": "chat_response",
        }
    )
    workflow.add_edge("prepare_sql_for_execution", "execute_sql")

    # After execution: observe only when something actually went wrong. A
    # successful query takes the edge it has always taken.
    def route_after_execution(state: AgentState) -> str:
        from . import reasoning

        try:
            observation = reasoning.check_invariants(
                reasoning.build_observation(state))
        except Exception:
            # A router that raises would strand the turn. Reasoning is an
            # improvement on top of the old path, never a new way to fail.
            logger.exception("[REASONING] observation failed; continuing")
            return "continue"

        if observation.get("success"):
            # A successful action still reaches the observer when the turn may
            # take another one: that is where "is the request carried out?" is
            # asked. With the ceiling at 1 (the default) this is skipped and
            # the turn behaves exactly as it always has.
            if (int(state.get("actions_taken") or 0) + 1
                    < int(settings.SQL_AGENT_MAX_ACTIONS_PER_TURN)):
                return "observe"

            # Say so. Deciding NOT to intervene is still a decision, and
            # without this line the reasoning layer is invisible on every
            # turn that works — which is every turn anyone watches.
            logger.info(reasoning.reasoning_trace(
                conversation_id=state.get("conversation_id"),
                turn_id=state.get("query_history_id"),
                mode=state.get("reasoning_mode"),
                observation=observation,
                decision={"decision": reasoning.ANSWER,
                          "reason": "the action succeeded"},
                next_action="enrich_co_appearance",
                replan_count=int(state.get("replan_count") or 0)))
            return "continue"

        decision = reasoning.decide_next(
            observation,
            mode=state.get("reasoning_mode") or reasoning.ReasoningMode.CONTEXTUAL,
            replan_count=int(state.get("replan_count") or 0),
            execution_retries=int(state.get("execution_retries") or 0),
            max_replans=int(settings.SQL_AGENT_MAX_REPLANS),
            max_execution_retries=int(settings.SQL_AGENT_MAX_EXECUTION_RETRIES))

        logger.info(reasoning.reasoning_trace(
            conversation_id=state.get("conversation_id"),
            turn_id=state.get("query_history_id"),
            mode=state.get("reasoning_mode"),
            observation=observation, decision=decision,
            next_action=("enrich_co_appearance"
                         if decision["decision"] == reasoning.ANSWER
                         else "observe_and_replan"),
            replan_count=int(state.get("replan_count") or 0)))

        if decision["decision"] != reasoning.ANSWER:
            return "observe"

        if observation.get("error_type") == reasoning.ErrorType.INVARIANT_VIOLATION:
            # A tool reported a result that contradicts its own contract —
            # success with nothing to show for it, or a query action with no
            # result object at all. The story path would narrate whatever is
            # left in state, which is how a system ends up telling somebody
            # their report is ready when it does not exist. Say so honestly
            # instead. This defends against OUR executors, not the model.
            logger.error("[REASONING] invariant violation on %s; refusing to "
                         "narrate it as a result", observation.get("action"))
            return "observe"

        # Nothing to correct: narrate the failure through the existing story
        # path, exactly as before.
        return "continue"

    workflow.add_conditional_edges(
        "execute_sql",
        route_after_execution,
        {
            "continue": "enrich_co_appearance",
            "observe": "observe_and_replan",
        }
    )

    # Where a correction goes. Every destination is an EXISTING node, so a
    # re-planned action runs through the same validators, the same AST guard
    # and the same ownership checks as a first attempt.
    def route_after_observation(state: AgentState) -> str:
        target = state.get("reasoning_next") or "chat_response"
        if target not in _OBSERVATION_TARGETS:
            logger.warning("[REASONING] unknown next node %r; answering", target)
            return "chat_response"
        return target

    workflow.add_conditional_edges(
        "observe_and_replan",
        route_after_observation,
        {name: name for name in _OBSERVATION_TARGETS}
    )
    workflow.add_edge("enrich_co_appearance", "story_response")
    workflow.add_edge("story_response", "learn_from_query")  # Add learning step

    # End nodes
    workflow.add_edge("learn_from_query", END)
    workflow.add_edge("chat_response", END)
    # Document actions are OBSERVED like every other action. They used to end
    # the turn unconditionally, which made them the one branch that could
    # fail quietly: `check_invariants` knows that a document action reporting
    # success without a registered artifact is BUGGY, and nothing on this
    # path ever asked it. A PDF whose body was "I couldn't reach that report
    # to translate it" reached a user as a finished report that way.
    #
    # The default is unchanged: a document that WAS produced goes straight to
    # END. This adds a branch for failure, not a step for success.
    def route_after_document(state: AgentState) -> str:
        from . import reasoning

        try:
            observation = reasoning.check_invariants(
                reasoning.build_observation(state))
        except Exception:
            logger.exception("[REASONING] document observation failed; ending")
            return "done"

        if observation.get("success"):
            return "done"

        decision = reasoning.decide_next(
            observation,
            mode=state.get("reasoning_mode") or reasoning.ReasoningMode.CONTEXTUAL,
            replan_count=int(state.get("replan_count") or 0),
            execution_retries=int(state.get("execution_retries") or 0),
            max_replans=int(settings.SQL_AGENT_MAX_REPLANS),
            max_execution_retries=int(settings.SQL_AGENT_MAX_EXECUTION_RETRIES))

        if decision["decision"] == reasoning.ANSWER:
            # Nothing to correct. The node has already written an honest
            # message; ending here is what it did before.
            return "done"

        logger.info("[REASONING] document action did not deliver (%s); observing",
                    observation.get("error_type"))
        return "observe"

    for _document_node in ("render_artifact", "translate_artifact"):
        workflow.add_conditional_edges(
            _document_node,
            route_after_document,
            {"done": END, "observe": "observe_and_replan"},
        )

    return workflow.compile()
