"""
Graph Module
============
LangGraph workflow definition for the SQL Intelligence Agent.
"""

import logging
from langgraph.graph import StateGraph, END, START

from .state import AgentState
from .tools import SQLAgentTools

logger = logging.getLogger(__name__)


def create_sql_agent(conversation_memory=None) -> StateGraph:
    """Create the SQL Intelligence Agent workflow with RAG."""
    tools = SQLAgentTools(conversation_memory=conversation_memory)

    # Create the graph
    workflow = StateGraph(AgentState)

    # Add nodes
    workflow.add_node("detect_malicious_intent", tools.detect_malicious_intent)  # STEP 0: Security scan FIRST
    workflow.add_node("fix_language", tools.fix_language)
    workflow.add_node("correct_name_typos", tools.correct_name_typos)
    workflow.add_node("classify_intent", tools.classify_intent)
    workflow.add_node("check_schema", tools.check_schema)
    workflow.add_node("retrieve_examples", tools.retrieve_examples)  # RAG retrieval
    workflow.add_node("generate_sql", tools.generate_sql)
    workflow.add_node("validate_and_fix_sql", tools.validate_and_fix_sql)
    workflow.add_node("prepare_sql_for_execution", tools.prepare_sql_for_execution)
    workflow.add_node("execute_sql", tools.execute_sql)
    workflow.add_node("story_response", tools.generate_story_response)
    workflow.add_node("learn_from_query", tools.learn_from_query)  # Learning step
    workflow.add_node("chat_response", tools.handle_chat)

    # Define routing function
    def route_by_intent(state: AgentState) -> str:
        """Route based on classified intent."""
        intent = state.get("intent", "CHAT")
        if intent == "CHAT":
            return "chat_response"
        else:  # SQL_QUERY or HYBRID
            return "check_schema"

    # Add edges - Security scan FIRST
    workflow.add_edge(START, "detect_malicious_intent")
    
    # Define routing function to check if user should be blocked
    def check_security_block(state: AgentState) -> str:
        """Check if user should be blocked - if so, skip to end"""
        if state.get("security_block_user"):
            logger.error("[SECURITY] Routing to END - User blocked in STEP 0")
            return "end_security_block"
        return "continue"
    
    # Add conditional edge after security scan
    workflow.add_conditional_edges(
        "detect_malicious_intent",
        check_security_block,
        {
            "end_security_block": END,  # Block user and end immediately
            "continue": "fix_language"  # Continue normal processing
        }
    )
    
    workflow.add_edge("fix_language", "correct_name_typos")
    workflow.add_edge("correct_name_typos", "classify_intent")

    # Conditional routing after intent classification
    workflow.add_conditional_edges(
        "classify_intent",
        route_by_intent,
        {
            "chat_response": "chat_response",
            "check_schema": "check_schema"
        }
    )

    # SQL path with RAG
    workflow.add_edge("check_schema", "retrieve_examples")  # Add RAG step
    workflow.add_edge("retrieve_examples", "generate_sql")
    workflow.add_edge("generate_sql", "validate_and_fix_sql")
    workflow.add_edge("validate_and_fix_sql", "prepare_sql_for_execution")
    workflow.add_edge("prepare_sql_for_execution", "execute_sql")
    workflow.add_edge("execute_sql", "story_response")
    workflow.add_edge("story_response", "learn_from_query")  # Add learning step

    # End nodes
    workflow.add_edge("learn_from_query", END)
    workflow.add_edge("chat_response", END)

    return workflow.compile()
