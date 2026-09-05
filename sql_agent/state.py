"""
Agent State Module
==================
Defines the state structure for the SQL Intelligence Agent.
"""

from typing import Any, TypedDict, Literal, Annotated, Optional, List, Dict
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """State for the SQL Intelligence Agent."""
    # Stage 0: immutable request provenance. ``original_input`` is never used
    # as model-ready text; normalization writes the two derived forms below.
    original_input: str
    normalized_input: str
    security_normalized_input: str
    input_language: Literal["en", "ar"]
    input_normalization_error: Optional[str]
    # Optional bounded paraphrase proposed by the planning loop for the SQL
    # specialist. It supplements, never replaces, normalized_input.
    sql_generation_input: Optional[str]

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
    # Canonical SQL returned by the AST policy. Execution, history, learning,
    # and follow-up modification must all use this exact value.
    validated_sql: str
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
    sql_validation_code: Optional[str]

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
    security_block_actor: Optional[Literal["user", "model"]]
    security_block_user_id: Optional[int]  # User ID to block (if available)
    # Owner of this turn. Scopes knowledge-base retrieval and learning so
    # one user's stored questions cannot reach another user's prompt.
    user_id: Optional[int]

    # Token-streaming callback installed by SQLAgent.query_stream. This key
    # existed as a plain dict entry for a long time WITHOUT being declared
    # here, so LangGraph dropped it at the first node boundary (see the
    # sql_generation_timed_out comment above for the rule) — story_response
    # always saw None, silently fell back to invoke(), and the "stream" the
    # client received was the finished report cut into 50-char slices.
    streaming_callback: Optional[Any]

    # Output language for the FINAL narrative ('en' or 'ar'). Detected
    # deterministically during query ingestion — Arabic
    # script, or an explicit request like 'in Arabic' — never by asking
    # the LLM what language it thinks it saw.
    response_language: Optional[str]

    # --- Planning (STEP 2) ---------------------------------------------
    # EVERY key below must stay declared. LangGraph merges each node's return
    # against this schema and silently DROPS anything it does not find here —
    # the trap that made streaming_callback and sql_generation_timed_out look
    # like deep bugs. A dropped planned_action would route every document
    # request to chat with no error anywhere.
    #
    # What the planner decided, already validated by the dispatcher.
    planned_action: Optional[Dict]
    # The closed set of things this turn may refer to, built in Python from
    # working memory and the caller's own artifacts. The planner chooses from
    # it; it never adds to it.
    planner_candidates: Optional[Dict]
    # Durable working memory, read from the session file at request time.
    working_context: Optional[Dict]
    # The caller's recent artifacts (id/type/title/language only — never
    # content), pre-fetched in the route so graph nodes stay synchronous.
    artifact_index: Optional[List[Dict]]
    # Enrolled people this caller may resolve a name against. Declared
    # here because LangGraph SILENTLY DROPS undeclared keys — the trap
    # this codebase has hit repeatedly.
    identity_index: Optional[List[Dict]]
    # People THIS turn actually resolved, newest last. The structured
    # subject is committed from here, so it does not depend on prose.
    resolved_entities: Optional[List[Dict]]
    # Candidates offered by an ambiguous look-up this turn, and whether this
    # turn ANSWERED a question asked earlier. LangGraph drops undeclared keys.
    clarification_candidates: Optional[List[Dict]]
    clarification_answered: Optional[bool]
    # THE canonical observation record for this turn, in order. It survives
    # graph re-entry, which is the whole point: without it a second action
    # starts blind. Bounded entries only - never rows, SQL or documents.
    observations: Optional[List[Dict]]
    # Which of reasoning.TERMINAL_STATES this turn ended in.
    terminal_state: Optional[str]
    # Set when bounded reasoning ran out on a real request, so the answer can
    # say so instead of changing the subject.
    reasoning_exhausted: Optional[bool]
    # The turn produced a closed-vocabulary failure phrase, not an answer.
    # Reported as a failure and kept out of memory.
    turn_failed: Optional[bool]
    # Entity resolution after an empty result: attempted once per turn, and
    # its two honest outcomes.
    entity_resolution_attempted: Optional[bool]
    entity_without_data: Optional[str]
    entity_not_found: Optional[str]
    # [canonical name, detections on record]: the question matched nothing,
    # but the person is not without data.
    entity_has_data: Optional[List]
    # The camera equivalents: the filter named a camera that does not exist,
    # or one that exists and simply has nothing recorded. `known_cameras`
    # carries the real names so the answer can offer them.
    camera_not_found: Optional[str]
    camera_without_data: Optional[str]
    # [label, detections on record]: the question matched nothing, but the
    # camera is not without data.
    camera_has_data: Optional[List]
    known_cameras: Optional[List[str]]
    # The stored name a misspelled camera was corrected to before re-running
    # the query, so a second empty result can be worded about THAT camera.
    camera_corrected_to: Optional[str]
    # The stored label of the camera a SUCCESSFUL query filtered on, when the
    # filter used the user's own spelling ('wezaret' matched 'WEZARET DEFA3').
    # The narration names the camera as the system knows it.
    camera_matched: Optional[str]
    # "thank you" / "ok" / "شكرا": answered with a fixed phrase, no model.
    acknowledgement: Optional[bool]
    # The model read this turn as questioning the last answer; the
    # chat node re-runs the check instead of answering from prose.
    confirmation_challenge: Optional[bool]
    # The validated reading of this turn (interpreter.Interpretation).
    interpretation: Optional[Dict]
    # True when the model decided the answer is already in this conversation
    # and the chat node should use recent context rather than run a new query.
    recall: Optional[bool]
    # The one routing decision for the turn: "data", "chat" or "undecided".
    turn_kind: Optional[str]
    # The misspelled token a "Did you mean X?" question is about; stored
    # with the pending question so the answer can correct the request.
    typo_of: Optional[str]
    # This turn's request was rebuilt from a suspended one plus the answer.
    resumed_from_typo: Optional[bool]
    # The (tool, args) signature of the action this turn committed to, so a
    # second action can recognise a repeat of it.
    committed_signature: Optional[List]
    # Set when the planner could not act safely; the chat node answers with
    # this question instead of inventing a reply.
    clarify_question: Optional[str]
    # Conversation this turn belongs to, for the audit line.
    conversation_id: Optional[str]
    # A rendered document waiting to be persisted: {bytes, type, title,
    # language, source_content, source_sql, source_result_id}. Graph nodes are
    # synchronous and registration needs the database, so the node renders and
    # the API layer commits — through the same render_and_register the HTTP
    # export uses, so there is only ever one persistence path.
    artifact_payload: Optional[Dict]
    # The canonical dialogue state (sql_agent/dialogue_state.py): what the
    # user is currently trying to accomplish — active task, filters,
    # references, each with provenance. Loaded from working_context; committed
    # back ONLY through application-validated deltas, never by a model.
    dialogue_state: Optional[Dict]
    # --- Bounded reasoning (PLAN -> ACT -> OBSERVE -> REPLAN -> ANSWER) --
    # FAST | CONTEXTUAL | MULTI_STEP, chosen deterministically in Python from
    # the conversation's shape (sql_agent/reasoning.py) — never by the model.
    reasoning_mode: Optional[str]
    # Steps spent this turn: tool look-ups AND re-plans share this budget.
    reasoning_steps_used: Optional[int]
    # Corrective re-plans so far. The graph's routing function reads this to
    # guarantee termination, so the bound does not depend on model behaviour.
    replan_count: Optional[int]
    # Retries of the SAME SQL after a TRANSIENT database error. A separate
    # budget: infrastructure trouble must not consume reasoning.
    execution_retries: Optional[int]
    # The bounded, factual account of what the last action produced. Enums,
    # counts and ids only — never rows, SQL, narrative or model prose.
    observation: Optional[Dict]
    # Fingerprints of actions that already failed this turn, so a re-plan is
    # corrective rather than a repeat of the same failing call.
    failed_action_fingerprints: Optional[List[str]]
    # What the LAST rejected attempt got wrong, fed back into generate_sql
    # so a retry is corrective rather than the same dice roll on the same
    # inputs: {sql, reason}. Machine output only — never model prose.
    sql_correction_hint: Optional[Dict]
    # What observe_and_replan decided: {decision, reason, error_type}. The
    # routing function reads it, so it MUST be declared — an undeclared key
    # is silently dropped by LangGraph and the router would see nothing.
    reasoning_decision: Optional[Dict]
    # The node observe_and_replan chose. Recorded so a test can assert the
    # decision without re-deriving it from the trace text.
    reasoning_next: Optional[str]
    # A short operational summary of the goal, for the audit line. Capped
    # hard: an action summary, deliberately NOT a place for reasoning text.
    reasoning_goal: Optional[str]
    # Whether this turn ASKS for anything: the interpreter's reading is
    # anything but `chat`. The chat node reads it to decide whether the
    # prior-turns block is relevant at all.
    turn_is_a_request: Optional[bool]
    # Actions taken this turn while pursuing the request. The graph's router
    # reads it, so the ceiling is arithmetic rather than a matter of the model
    # choosing to stop. Incremented in exactly one place.
    actions_taken: Optional[int]
    # The id of the persisted user_query_history row for this turn. Read in
    # agent.py but historically UNDECLARED — exactly the LangGraph
    # drops-undeclared-keys trap that has bitten this file three times.
    query_history_id: Optional[int]

    # Which tools the agent used to reach this turn's decision, for
    # the audit line: [{tool, ok|rejected|committed}, ...].
    tool_trace: Optional[List[Dict]]
    # A translation the node decided on but cannot perform: reading the stored
    # source text is an ownership-checked database call, so the async API
    # layer resolves the id, translates, re-renders and registers the result.
    translation_request: Optional[Dict]
    # {artifact_id: source_sql} for this caller's recent documents, so
    # "same report but camera 3" modifies the query that report came FROM
    # rather than whatever ran most recently. Owner-scoped at the query.
    artifact_sql_index: Optional[Dict]
    # Where modify_sql took its base query from, for the audit line.
    sql_base_provenance: Optional[str]
    # True only when the rewritten query actually DIFFERS from the base. A
    # failed rewrite falls back to the original, and without this flag that
    # is indistinguishable from a successful one — which is exactly how a
    # broken modification passed its gate once.
    sql_was_modified: Optional[bool]

    # Co-appearance enrichment for tracking narratives: who else was seen at
    # the same camera within the window around each of the subject's
    # detections. Computed deterministically by enrich_co_appearance (never
    # by the LLM), keyed per passage.
    co_appearances: Optional[List[Dict]]
