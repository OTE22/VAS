"""
Agent State Module
==================
Defines the state structure for the SQL Intelligence Agent.
"""

from typing import TypedDict, Literal, Annotated, Optional, List, Dict
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """State for the SQL Intelligence Agent."""
    # Input
    original_input: str

    # Step 1: Language normalization
    normalized_input: str

    # Step 2: Intent classification
    intent: Literal["CHAT", "SQL_QUERY", "HYBRID"]
    intent_confidence: float

    # Step 3: Schema
    schema_description: str

    # Step 3.5: RAG - Retrieved examples
    retrieved_examples: List[Dict]
    rag_context: str

    # Step 4: SQL generation
    generated_sql: str
    sql_purpose: str
    # True when generation hit the model's time budget rather than producing
    # unusable output. Declared here because LangGraph merges node results
    # against this schema and silently DROPS undeclared keys — without this line
    # the flag never reaches execute_sql, and a timeout keeps surfacing as the
    # misleading "No SQL query to execute".
    sql_generation_timed_out: bool

    # Step 4.5: SQL Validation and Fixing
    sql_validation_status: Literal["VALID", "FIXED", "PARTIAL", "INVALID", "ERROR"]
    sql_fixes_applied: List[str]
    sql_validation_warnings: List[str]
    sql_validation_error: Optional[str]

    # Step 5: SQL execution
    query_result: dict

    # Step 6: Final response
    final_response: str

    # Learning: Whether to save this query for future reference
    should_learn: bool

    # Error handling
    error: Optional[str]

    # Messages for conversation context
    messages: Annotated[list, add_messages]
    
    # Conversation memory context
    conversation_context: Optional[str]
    
    # Name corrections applied
    name_corrections: Optional[Dict[str, str]]
    
    # Security: Flag to indicate user should be blocked
    # A DETECTION, not a verdict: sql_agent/security_policy.py decides whether
    # the account is actually blocked. The name predates that split.
    security_block_user: Optional[bool]
    # Deterministic server-side code (e.g. FORBIDDEN_SQL_ATTEMPT). This, never
    # model prose, is what reaches blocked_reason and the audit trail.
    security_reason_code: Optional[str]
    security_block_reason: Optional[str]
    security_block_user_id: Optional[int]  # User ID to block (if available)
    # Owner of this turn. Scopes knowledge-base retrieval and learning so
    # one user's stored questions cannot reach another user's prompt.
    user_id: Optional[int]