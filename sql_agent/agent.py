"""
Agent Module
============
Main SQL Intelligence Agent class.
"""

import logging
from datetime import datetime
from typing import List, Dict, Optional

from .config import config
from .state import AgentState
from .database import DatabaseManager
from .knowledge_base import SQLKnowledgeBase
from .conversation_memory import ConversationMemory
from .graph import create_sql_agent
from .run_control import controlled

# Setup logger for SQL Agent
logger = logging.getLogger(__name__)

#: The only thing an unexpected exception may say to the user. The exception
#: text itself used to be returned as the answer AND committed to memory,
#: where the next turn replayed it to the model as context.
_UNEXPECTED_FAILURE = ("Something went wrong while handling that request. "
                       "Please try again.")

# Neutral placeholder. Every transport replaces this with the policy layer's
# verdict, but if one ever forgets, the fallback the user sees must still be
# true — a refusal, never a claim about their account.
_SECURITY_PLACEHOLDER = ("That operation is not permitted — the assistant can "
                         "only read data.")


class TurnCancelled(RuntimeError):
    """Raised after a cooperative cancellation reaches a graph boundary."""


def _invoke_cancellable(graph, initial_state: dict, cancel_event=None) -> dict:
    """Run the graph while making node boundaries cancellation points.

    LangGraph nodes and the local model are synchronous, so an in-progress
    model call cannot be interrupted safely. The returned state is committed
    only after the whole graph finishes; a cancelled turn is discarded.
    """
    if cancel_event is None:
        return graph.invoke(initial_state)
    if cancel_event.is_set():
        raise TurnCancelled("turn cancelled before execution")

    accumulated = dict(initial_state)
    for chunk in graph.stream(initial_state):
        if cancel_event.is_set():
            raise TurnCancelled("turn cancelled at graph boundary")
        for node_output in chunk.values():
            if isinstance(node_output, dict):
                accumulated.update(node_output)
    if cancel_event.is_set():
        raise TurnCancelled("turn cancelled after execution")
    return accumulated


def _security_event(state: dict, reason: str) -> dict:
    """A DETECTION event: structured, and silent about account state.

    `security_violation` is what the transports key off. They must never key off
    the wording of `message` — that was the original defect: the API matched
    `message.startswith("Security:")` while the agent emitted "SECURITY ALERT:",
    so the policy layer was never invoked on streaming transports.
    """
    return {
        "type": "error",
        "step": "security_block",
        "security_violation": True,
        "security_reason_code": state.get("security_reason_code",
                                          "FORBIDDEN_SQL_ATTEMPT"),
        "security_reason": reason,
        "message": _SECURITY_PLACEHOLDER,
    }


class SQLIntelligenceAgent:
    """
    SQL Intelligence Agent

    An advanced agent that transforms natural language into meaningful
    database insights using a multi-step reasoning pipeline with RAG.
    """

    def __init__(self, conversation_memory: Optional[ConversationMemory] = None):
        logger.info("Initializing SQL Intelligence Agent")
        self.conversation_memory = conversation_memory or ConversationMemory()
        logger.info(f"Conversation memory initialized (session: {self.conversation_memory.current_session_id})")
        # The database manager FIRST, then the graph built around it, so the
        # tools execute through the same instance `set_pipeline_scope` binds.
        self.db = DatabaseManager(config)
        self.agent = create_sql_agent(conversation_memory=self.conversation_memory,
                                      db=self.db)
        self.kb = SQLKnowledgeBase(config)
        # The caller's recent documents, refreshed by the API layer before
        # each turn. Ids, titles and languages only — never content, and never
        # another user's, because the query that fills it is owner-scoped.
        self._artifact_index = []
        self._identity_index = []
        # {artifact_id: source_sql}, for provenance-first query modification.
        self._artifact_sql_index = {}
        # A short block of the user's stored memories, supplied by the API
        # layer (reading them needs the database) and appended to the
        # conversation context every prompt already consumes.
        self._durable_memory = ""
        # Work the graph decided on but could not perform: rendering is
        # synchronous, persisting is not. Both transports leave it here and
        # the API layer takes it, so neither has its own persistence path.
        self._pending_document = {}
        logger.info("SQL Intelligence Agent initialized successfully")

    def set_artifact_index(self, artifacts) -> None:
        """Give the next turn the documents it may refer to.

        Called from the async API layer, which can query the database; graph
        nodes are synchronous and cannot. The agent instance is per-user and
        each turn holds that user's lock, so this cannot leak across users.
        """
        self._artifact_index = list(artifacts or [])

    def set_pipeline_scope(self, allowed) -> None:
        """Bind this agent's SQL to the caller's cameras for the coming turn.

        `None` is an administrator - no restriction. Anything else becomes
        the exact set the guard rewrites every table against, so a user
        assigned cameras 1 and 2 cannot read camera 3 through the chatbot
        any more than through the REST API. An empty set stays empty: the
        guard refuses, it does not widen.
        """
        import dataclasses

        scope = None if allowed is None else frozenset(
            str(p) for p in allowed)
        self.db.sql_policy = dataclasses.replace(
            self.db.sql_policy, pipeline_scope=scope)

    def keep_stored_names(self, source: str, translated: str, language: str) -> str:
        """The name-fidelity footer for a translation made outside the graph
        (the artifact route): stored person and camera names the source
        mentions that the translation transliterated are appended as stored."""
        from .tools.agent_tools import SQLAgentTools

        if not translated:
            return translated
        tools = SQLAgentTools.__new__(SQLAgentTools)
        tools.db = self.db
        literals = tools._names_in_text(source, {"identity_index": self._identity_index})
        missing = tools._missing_literals(translated, literals)
        if missing and len(literals) <= tools._LITERAL_ENFORCE_MAX:
            return translated + tools._names_as_stored_footer(missing, language)
        return translated

    def set_identity_index(self, identities) -> None:
        """Give the next turn the enrolled people it may resolve names to.

        `identities` is deliberately outside the SQL guard's allowlist, so
        this cannot be queried from inside the graph — and should not be:
        keeping it out of the allowlist is what stops the model reading the
        table directly. Same path, and same per-user safety, as the artifact
        index above.
        """
        self._identity_index = list(identities or [])

    def set_artifact_sql_index(self, mapping) -> None:
        """Base queries for the caller's documents, keyed by artifact id."""
        self._artifact_sql_index = dict(mapping or {})

    def set_durable_memory(self, text: str) -> None:
        """Stored memories to carry into this turn's prompts."""
        self._durable_memory = text or ""

    def _finish_turn(self, user_input: str, final_response: str,
                     state: dict) -> bool:
        """Everything a completed streaming turn owes, in one place.

        `stream_query` can finish in two ways — the streamed response, or the
        invoke fallback when the stream carried none — and each used to do its
        own bookkeeping. They drifted: the fallback recorded the reply and
        nothing else, so a document it rendered was silently dropped (the SSE
        route persists an artifact only when `has_document` is set), working
        memory never learned what the turn produced, and no dialogue-state
        delta was committed.

        That is why the SSE acceptance test failed about half the time while
        the model chose `generate_document` correctly every single time.

        Returns whether there is document work for the API layer to finish.
        The payload itself travels on the INSTANCE, never through an event:
        it is raw PDF bytes, which cannot be serialised into SSE and must not
        be sent to the client anyway.
        """
        state = state or {}
        self.conversation_memory.add_user_message(user_input)
        self.conversation_memory.add_ai_message(final_response)
        self._record_working_context(user_input, state)
        self._commit_tool_result_deltas(user_input, state)
        return self._stash_pending_document(state)

    def _stash_pending_document(self, state: dict) -> bool:
        """Hold document work for the API layer. True if there is any."""
        state = state or {}
        pending = {}
        if state.get("artifact_payload"):
            pending["artifact_payload"] = state["artifact_payload"]
        if state.get("translation_request"):
            pending["translation_request"] = state["translation_request"]
        self._pending_document = pending
        return bool(pending)

    def take_pending_document(self) -> dict:
        """Return the pending document work and clear it.

        Cleared on read so a turn that produces no document cannot inherit the
        previous turn's payload and register it a second time.
        """
        pending, self._pending_document = self._pending_document, {}
        return pending

    def _create_initial_state(self, user_input: str, should_learn: bool = True) -> AgentState:
        """Create the initial state for the agent."""
        return {
            # Owner of this turn, taken from the per-user conversation memory.
            # Scopes knowledge-base retrieval and learning; when it is None the
            # knowledge base falls back to curated seed examples only.
            "user_id": getattr(self.conversation_memory, "user_id", None),
            "original_input": user_input,
            "normalized_input": "",
            "security_normalized_input": "",
            "input_language": "en",
            "input_normalization_error": None,
            "sql_generation_input": None,
            "intent": "CHAT",
            "intent_confidence": 0.0,
            "schema_description": "",
            "retrieved_examples": [],
            "rag_context": "",
            "generated_sql": "",
            "validated_sql": "",
            "sql_purpose": "",
            "sql_validation_status": "VALID",
            "sql_fixes_applied": [],
            "sql_validation_warnings": [],
            "sql_validation_error": None,
            "sql_validation_code": None,
            "query_result": {},
            "final_response": "",
            "should_learn": should_learn,
            "error": None,
            "messages": [],
            "conversation_context": "",
            "name_corrections": None,
            "security_block_user": None,
            "security_block_reason": None,
            "security_block_actor": None,
            # Working memory is read from the SESSION FILE, not from this
            # instance's attributes: after a restart or an LRU eviction this
            # object is new but the file still knows what the last report was.
            "working_context": self._load_working_context(),
            "dialogue_state": self._load_dialogue_state(),
            # Set by the API layer through set_artifact_index() before the
            # turn runs: graph nodes are synchronous and this needs the
            # database, so it cannot be fetched from inside the graph.
            "artifact_index": list(self._artifact_index or []),
            "artifact_sql_index": dict(self._artifact_sql_index or {}),
            # getattr, not attribute access: this index is optional
            # CONTEXT, and an agent built without it must lose name
            # resolution quality rather than the whole turn. Accessing
            # it directly crashed every streaming turn for any agent
            # constructed without __init__.
            "identity_index": list(getattr(self, "_identity_index", None) or []),
            "planned_action": None,
            "planner_candidates": None,
            "clarify_question": None,
            "conversation_id": None,
        }

    def _load_working_context(self) -> dict:
        """The durable working context, reloaded from disk. Never fatal."""
        try:
            if self.conversation_memory:
                return self.conversation_memory.get_working_context(reload=True)
        except Exception as e:
            logger.warning("[SQL_AGENT] could not load working context: %s", e)
        return {}

    def _load_dialogue_state(self) -> dict:
        """The canonical dialogue state, migrated forward. Never fatal."""
        from . import dialogue_state as ds
        try:
            context = self._load_working_context()
            return ds.migrate_state(context.get("dialogue_state"))
        except Exception as e:
            logger.warning("[SQL_AGENT] could not load dialogue state: %s", e)
            return ds.empty_state()

    def _commit_tool_result_deltas(self, user_input: str, state: dict) -> None:
        """Application-committed state transitions from VALIDATED results.

        THE authority rule, applied to memory: the model proposes meaning,
        the application commits state — and these commits happen only after
        a tool actually did what the turn claimed. A successful query updates
        the active task; each commit goes through apply_delta, so it can
        change exactly one field and bump context_version, and the per-turn
        trace records before → delta → after. Never fatal: losing a state
        transition must not fail the turn that earned it.
        """
        from . import dialogue_state as ds
        try:
            previous = ds.migrate_state(
                (self.conversation_memory.get_working_context() or {})
                .get("dialogue_state"))
            current = previous
            turn_id = f"h{state.get('query_history_id') or ''}-" \
                      f"{datetime.utcnow().strftime('%H%M%S')}"
            committed_delta = None

            result = state.get("query_result") or {}

            # The MODEL'S proposed delta first — but only when the action it
            # was proposed alongside actually SUCCEEDED. "No, camera 4" only
            # updates state once the camera-4 query ran; a proposal whose
            # action failed taught us nothing and commits nothing.
            plan = state.get("planned_action") or {}
            proposed = plan.get("state_delta")
            if proposed and result.get("success"):
                try:
                    current = ds.apply_delta(current, proposed, turn_id=turn_id)
                    committed_delta = proposed
                except ds.DeltaRejected as rejection:
                    logger.info("[DIALOGUE_STATE] proposed delta rejected: %s",
                                rejection)

            # The question we just asked, so next turn can recognise its
            # ANSWER. Written on the clarify path, where no query runs — which
            # is exactly why the success-gated commits wrote nothing and an
            # open question left no trace at all.
            action = (state.get("planned_action") or {}).get("action")
            offered = state.get("clarification_candidates") or []
            # ANY question, not only a person-candidate one. Scoping this
            # to candidates meant a question the model asked for any other
            # reason vanished the moment it was asked, so the reply had
            # nothing to attach to and arrived as an unanchored fragment.
            if action == "clarify":
                try:
                    current = ds.apply_delta(current, {
                        "operation": "REPLACE",
                        "field": "pending_clarification",
                        "proposed_value": {
                            "type": ("typo" if state.get("typo_of")
                                     else "person_resolution" if offered
                                     else "open_question"),
                            "original_intent": state.get("intent") or "SQL_QUERY",
                            "original_query": str(user_input)[:200],
                            # The question itself, so the next turn's reading
                            # sees what the answer is answering.
                            "question": str(state.get("clarify_question") or "")[:300],
                            "field": "person",
                            # The misspelled token, so the answer can be
                            # substituted into the original words.
                            "wrong": str(state.get("typo_of") or "")[:120],
                            "candidates": offered},
                        "source": "tool_result"}, turn_id=turn_id)
                except ds.DeltaRejected as rejection:
                    logger.info("[DIALOGUE_STATE] clarification not stored: %s",
                                rejection)
            elif ds.get_value(current, "pending_clarification"):
                # Any turn that did something ELSE retires the question: an
                # answer resolves it, and a new subject, a new intent or an
                # explicit cancellation all make it obsolete. Structural, so
                # no phrase has to be recognised for a request to move on.
                try:
                    current = ds.apply_delta(current, {
                        "operation": "REMOVE",
                        "field": "pending_clarification",
                        "source": "user_correction"}, turn_id=turn_id)
                    logger.info("[REACT] pending clarification cleared "
                                "action=%s", action)
                except ds.DeltaRejected:
                    pass

            # THE SUBJECT of this turn, committed whether or not a query
            # ran. The old code wrote the subject only inside `active_task`
            # prose, and only on a successful query, so a turn that ended in
            # a question left the PREVIOUS person in place — "track ali"
            # answered about Joey.
            subject = None
            for entry in (state.get("resolved_entities") or []):
                if (entry or {}).get("canonical_name"):
                    subject = entry
            if subject:
                canonical = subject["canonical_name"]
                held = ds.get_value(current, "referenced_entity") or []
                if canonical not in held:
                    try:
                        current = ds.apply_delta(current, {
                            "operation": "REPLACE",
                            "field": "referenced_entity",
                            "proposed_value": [canonical],
                            # Same rank as any earlier explicit statement, and
                            # equal rank wins, so a subject committed once can
                            # never pin the conversation to one person.
                            "source": "user_correction"}, turn_id=turn_id)
                        committed_delta = committed_delta or {
                            "operation": "REPLACE",
                            "field": "referenced_entity",
                            "proposed_value": [canonical]}
                        logger.info(
                            "[STATE] subject replaced old=%s new=%s "
                            "source=current_turn", held or None, canonical)
                        # The task sentence describes the OLD job. Left alone
                        # it is re-injected next turn under a header reading
                        # "authoritative".
                        if ds.get_value(current, "active_task"):
                            current = ds.apply_delta(current, {
                                "operation": "REMOVE", "field": "active_task",
                                "source": "user_correction"}, turn_id=turn_id)
                        # The camera and time range were qualifiers of that
                        # old job. They stayed "authoritative" until the
                        # model happened to propose removing them, so a new
                        # subject inherited yesterday's window. A field the
                        # model's delta set THIS turn is the new job's own.
                        touched = {(proposed or {}).get("field")}
                        for stale in ("active_camera", "active_time_range"):
                            if stale in touched:
                                continue
                            if ds.get_value(current, stale):
                                current = ds.apply_delta(current, {
                                    "operation": "REMOVE", "field": stale,
                                    "source": "user_correction"},
                                    turn_id=turn_id)
                                logger.info("[STATE] %s retired with the old "
                                            "subject", stale)
                    except ds.DeltaRejected as rejection:
                        logger.info("[DIALOGUE_STATE] subject delta rejected: "
                                    "%s", rejection)

            if result.get("success") and state.get("sql_purpose"):
                delta = {"operation": "REPLACE", "field": "active_task",
                         "proposed_value": str(state["sql_purpose"])[:200],
                         "source": "tool_result"}
                try:
                    current = ds.apply_delta(current, delta, turn_id=turn_id)
                    committed_delta = committed_delta or delta
                except ds.DeltaRejected as rejection:
                    logger.info("[DIALOGUE_STATE] delta rejected: %s", rejection)

            if state.get("response_language"):
                delta = {"operation": "REPLACE", "field": "output_language",
                         "proposed_value": state["response_language"],
                         "source": "tool_result"}
                try:
                    current = ds.apply_delta(current, delta, turn_id=turn_id)
                except ds.DeltaRejected:
                    pass

            if current is not previous:
                self.conversation_memory.update_working_context(
                    dialogue_state=current)
            logger.info(
                "[DIALOGUE_STATE] conversation=%s turn=%s context_version=%s->%s "
                "action=%s changed_field=%s artifact_reference=%s",
                self.conversation_memory.current_session_id,
                turn_id,
                previous.get("context_version"), current.get("context_version"),
                (state.get("planned_action") or {}).get("action"),
                # None on a turn that committed nothing (a translation, a
                # clarification): calling .get on it raised inside this very
                # log call and the handler then reported the commit as
                # "skipped" - after it had already been written.
                (committed_delta or {}).get("field"),
                bool((state.get("planned_action") or {}).get("artifact_id")),
            )
        except Exception as e:
            logger.warning("[SQL_AGENT] dialogue-state commit skipped: %s", e)

    @controlled()
    def query(self, user_input: str, learn: bool = True, cancel_event=None):
        """
        Process a user query and return a human-friendly response.

        Args:
            user_input: The natural language query from the user
            learn: Whether to save successful queries for future reference

        Returns:
            A human-readable narrative response, or tuple (response, result_dict) if security flags are set
        """
        logger.info("[SQL_AGENT] Processing query (chars=%d)",
                    len(user_input) if isinstance(user_input, str) else 0)
        
        # The current turn is committed as a user/assistant pair only after the
        # graph succeeds. A timeout or cancellation therefore cannot leave an
        # orphaned user message that contaminates the next turn's context.
        conversation_context = self.conversation_memory.get_conversation_context(limit=6)
        if conversation_context:
            logger.debug(f"[SQL_AGENT] Conversation context retrieved ({len(conversation_context)} chars)")
        
        initial_state = self._create_initial_state(user_input, should_learn=learn)
        initial_state["conversation_context"] = (
            conversation_context + self._durable_memory)

        # Run the agent
        try:
            logger.info("[SQL_AGENT] 🔄 Invoking agent workflow...")
            logger.debug(f"[SQL_AGENT] Initial state keys: {list(initial_state.keys())}")
            logger.debug("[SQL_AGENT] User input present=%s chars=%d",
                         bool(initial_state.get("original_input")),
                         len(initial_state.get("original_input") or ""))
            
            result = _invoke_cancellable(
                self.agent, initial_state, cancel_event=cancel_event)
            
            logger.info(f"[SQL_AGENT] ✅ Agent workflow completed")
            logger.debug(f"[SQL_AGENT] Result keys: {list(result.keys())}")
            
            response = result.get("final_response", "I apologize, but I couldn't process your request.")
            
            logger.info("[SQL_AGENT] Final response produced (chars=%d)",
                        len(response))
            
            # A violation was DETECTED. The API route runs it through the policy
            # layer, which decides — and states — what happens to the account.
            # This response is a placeholder the route replaces; it must not
            # claim an account action that may not occur.
            if result.get("security_block_user"):
                block_reason = result.get("security_block_reason", "Attempted forbidden SQL operation")
                user_id_from_state = result.get("security_block_user_id")  # Get user_id from state if available
                response = _SECURITY_PLACEHOLDER
                logger.error(f"[SECURITY] 🚨 Agent flagged a violation for policy review: {block_reason}")
                # Include user_id in result_dict if available
                if user_id_from_state:
                    result["security_block_user_id"] = user_id_from_state
                # Return both response and result for API route to check
                return response, result

            if result.get("input_normalization_error"):
                # Rejected boundary input is not a conversation turn and must
                # not contaminate memory for the next valid request.
                return response
            
            if result.get("turn_failed"):
                # A closed failure phrase: reported as a failure, and kept
                # OUT of memory where it would be replayed as an answer.
                logger.info("[SQL_AGENT] turn failed; nothing committed")
                return response, result

            # Commit the completed turn together.
            self.conversation_memory.add_user_message(user_input)
            self.conversation_memory.add_ai_message(response)
            self._record_working_context(user_input, result)
            self._commit_tool_result_deltas(user_input, result)
            logger.info(f"[SQL_AGENT] ✅ Query processed successfully (response length: {len(response)} chars)")

            # Work the API layer has to finish has to REACH it. A rendered
            # document needs persisting and a translation request needs an
            # ownership-checked read — both need the database, which only the
            # async layer can await. The route already accepts a
            # (response, state) tuple, so this reuses that shape rather than
            # inventing a second return contract.
            if self._stash_pending_document(result):
                return response, result

            return response
        except TurnCancelled:
            logger.info("[SQL_AGENT] Query cancelled before commit")
            raise
        except Exception as e:
            logger.error(f"[SQL_AGENT] Error processing query: {str(e)}", exc_info=True)
            # Closed phrase, and NOT committed to memory.
            return _UNEXPECTED_FAILURE, {"turn_failed": True}

    @controlled(stream=True)
    def query_stream(self, user_input: str, learn: bool = True, cancel_event=None):
        """
        Process a user query and stream progress updates.

        Args:
            user_input: The natural language query from the user
            learn: Whether to save successful queries for future reference
            cancel_event: optional threading.Event — when set (client disconnect,
                timeout), the stream stops at the next graph-node boundary
                instead of running all remaining LLM calls

        Yields:
            Progress updates as the agent processes the query
        """
        logger.info("[SQL_AGENT] Processing streaming query (chars=%d)",
                    len(user_input) if isinstance(user_input, str) else 0)
        
        try:
            # Commit only at successful completion, as one user/assistant pair.
            conversation_context = self.conversation_memory.get_conversation_context(limit=6)
            yield {"type": "status", "message": "Processing query...", "step": "start"}

            initial_state = self._create_initial_state(user_input, should_learn=learn)
            # Durable memory travels on BOTH transports. This line lacking
            # `+ self._durable_memory` while query() had it meant stored
            # memories reached only the non-streaming path — the browser,
            # which streams, never saw them.
            initial_state["conversation_context"] = (
                conversation_context + self._durable_memory)

            # Set up streaming callback for word-by-word generation
            # Use a queue-like structure to collect words that need to be yielded
            word_queue = []
            
            def streaming_callback(data):
                """Callback to queue words for streaming"""
                if data.get("type") == "content":
                    word_queue.append(data)
            
            # Store callback in state (will be used by generate_story_response)
            initial_state["streaming_callback"] = streaming_callback
            
            # Stream the agent execution
            yield {"type": "status", "message": "Initializing agent workflow...", "step": "init"}
            
            # Use stream instead of invoke for streaming
            # Accumulate state as we go through the stream
            accumulated_state = initial_state.copy()
            final_response = None
            
            # Process all chunks from the stream
            stream_ended_early = False
            try:
                for chunk in self.agent.stream(initial_state):
                    # Cancellation (client disconnect / timeout): stop at the node
                    # boundary instead of running the remaining LLM calls.
                    if cancel_event is not None and cancel_event.is_set():
                        logger.info("[SQL_AGENT] 🛑 Stream cancelled - stopping at node boundary")
                        yield {"type": "complete", "message": "Query cancelled", "step": "done", "success": False}
                        return

                    # First, yield any queued words from streaming callback
                    while word_queue:
                        yield word_queue.pop(0)
                    
                    # Extract step information from chunk
                    for node_name, node_output in chunk.items():
                        # Update accumulated state with node output
                        accumulated_state.update(node_output)
                        
                        # A violation was DETECTED. What happens to the account is
                        # decided by sql_agent/security_policy.py, not here — so
                        # this event carries a structured marker and says nothing
                        # about account state. The transport replaces `message`
                        # with the policy's verdict before the client sees it.
                        #
                        # This used to yield "…Your account has been blocked."
                        # directly, while the policy layer was never reached at
                        # all on this transport: the user was told they were
                        # blocked on every attempt and never actually was.
                        if accumulated_state.get("security_block_user"):
                            block_reason = accumulated_state.get("security_block_reason", "Security violation detected")
                            logger.error(f"[SECURITY] Stream ended early - violation detected: {block_reason}")
                            yield _security_event(accumulated_state, block_reason)
                            stream_ended_early = True
                            break

                        if node_name == "detect_malicious_intent":
                            # Security scan completed
                            if accumulated_state.get("security_block_user"):
                                block_reason = accumulated_state.get("security_block_reason", "Malicious intent detected")
                                yield _security_event(accumulated_state, block_reason)
                                stream_ended_early = True
                                break
                            yield {"type": "status", "message": "Safety checks complete.", "step": "security"}
                        elif node_name == "ingest_query":
                            yield {"type": "status", "message": "Validating request...", "step": "ingest"}
                        elif node_name == "plan_action":
                            yield {"type": "status", "message": "Planning the request...", "step": "plan"}
                        elif node_name == "check_schema":
                            yield {"type": "status", "message": "Loading database schema...", "step": "schema"}
                        elif node_name == "retrieve_examples":
                            yield {"type": "status", "message": "Retrieving similar examples...", "step": "rag"}
                        elif node_name == "generate_sql":
                            yield {"type": "status", "message": "Building a read-only query...", "step": "generate_sql"}
                        elif node_name == "validate_and_fix_sql":
                            yield {"type": "status", "message": "Validating and authorizing SQL...", "step": "validate"}
                            sql = node_output.get("validated_sql", "")
                            if sql:
                                yield {"type": "sql", "sql": sql[:200] + "..." if len(sql) > 200 else sql, "step": "validate"}
                        elif node_name == "prepare_sql_for_execution":
                            yield {"type": "status", "message": "Preparing the authorized query...", "step": "authorize"}
                        elif node_name == "execute_sql":
                            result = node_output.get("query_result", {})
                            if result.get("success"):
                                row_count = result.get("row_count", 0)
                                yield {"type": "status", "message": f"Query executed: {row_count} rows returned", "step": "execute"}
                            else:
                                # The DRIVER's text names columns, types and
                                # the query itself. The REST path has always
                                # narrated failures from the error CATEGORY;
                                # this event interpolated the raw string, so
                                # the same failure was sanitized over one
                                # transport and verbatim over the other.
                                from .tools.agent_tools import SQLAgentTools

                                detail = str(result.get("error") or "")
                                logger.warning("[STREAM] query failed "
                                               "(detail_chars=%d)", len(detail))
                                yield {
                                    "type": "error",
                                    "message": SQLAgentTools._failure_narration({
                                        "query_result": result,
                                        "generated_sql": node_output.get(
                                            "generated_sql"),
                                        "planned_action": {
                                            "action": "query_database"},
                                        "response_language": (
                                            node_output.get("response_language")
                                            or "en"),
                                    }),
                                    "step": "execute"}
                        elif node_name == "story_response":
                            # When story_response node runs, it streams word-by-word via callback
                            # Yield any remaining queued words
                            while word_queue:
                                yield word_queue.pop(0)
                            
                            # Get final response from state
                            final_response = node_output.get("final_response", "") or accumulated_state.get("final_response", "")
                            if final_response:
                                logger.debug(f"[SQL_AGENT] Final response captured in stream ({len(final_response)} chars)")
                        elif node_name == "chat_response":
                            yield {"type": "status", "message": "Response ready.", "step": "response"}
                        elif node_name == "render_artifact":
                            yield {"type": "status", "message": "Preparing document...", "step": "document"}
                        elif node_name == "translate_artifact":
                            yield {"type": "status", "message": "Translating report...", "step": "document"}
                        elif node_name == "learn_from_query":
                            yield {"type": "status", "message": "Learning from query...", "step": "learn"}
                    
                    if stream_ended_early:
                        break
            except StopIteration:
                # Stream ended normally
                pass
            except Exception as stream_error:
                logger.error(f"[SQL_AGENT] Stream error: {str(stream_error)}", exc_info=True)
                yield {"type": "error", "message": f"Stream error: {str(stream_error)}", "step": "error"}
                stream_ended_early = True
            
            # Check if stream ended early due to security block
            if stream_ended_early or accumulated_state.get("security_block_user"):
                # Stream ended early - ensure we send completion
                if not final_response:
                    final_response = accumulated_state.get("final_response", _SECURITY_PLACEHOLDER)
                yield {"type": "complete", "message": "Stream ended", "step": "done", "success": False}
                return
            
            # Yield any remaining queued words
            while word_queue:
                yield word_queue.pop(0)
            
            # After stream completes, check accumulated_state for final_response
            document_response_pending = bool(
                accumulated_state.get("artifact_payload")
                or accumulated_state.get("translation_request"))
            if not final_response:
                final_response = accumulated_state.get("final_response", "")
                if final_response:
                    logger.debug(f"[SQL_AGENT] Final response found in accumulated_state ({len(final_response)} chars)")
                    # A document response is provisional until the async API
                    # layer finishes persistence/translation. Its authoritative
                    # text travels on the completion event. Streaming this value
                    # early exposed a failure placeholder while translation was
                    # still running and exposed success before a save could fail.
                    if not document_response_pending:
                        chunk_size = 50
                        for i in range(0, len(final_response), chunk_size):
                            chunk_text = final_response[i:i+chunk_size]
                            yield {"type": "content", "content": chunk_text, "step": "response"}
            
            # Add AI response to conversation memory
            if final_response:
                if accumulated_state.get("input_normalization_error"):
                    yield {"type": "complete", "message": "Query rejected",
                           "step": "done", "response": final_response,
                           "response_length": len(final_response),
                           "success": False, "has_document": False}
                    return
                if accumulated_state.get("turn_failed"):
                    # The phrase is streamed, the failure is reported as one,
                    # and nothing is committed to memory.
                    yield {"type": "complete", "message": "Query failed",
                           "step": "done", "response": final_response,
                           "response_length": len(final_response),
                           "success": False, "has_document": False}
                    return
                logger.info(f"[SQL_AGENT] Streaming query completed successfully (response length: {len(final_response)} chars)")
                has_document = self._finish_turn(
                    user_input, final_response, accumulated_state)
                # Include the response in completion message for API route to capture
                yield {"type": "complete", "message": "Query completed successfully", "step": "done", "response": final_response, "response_length": len(final_response), "success": True, "has_document": has_document}
            else:
                # Last resort: use invoke to get the final response
                logger.warning("[SQL_AGENT] Final response not found in stream, trying invoke as fallback")
                try:
                    result = self.agent.invoke(initial_state)
                    final_response = result.get("final_response", "")
                    if final_response:
                        logger.info(f"[SQL_AGENT] Final response retrieved via invoke ({len(final_response)} chars)")
                        # The SAME completion work as the primary path. This
                        # branch used to do only add_ai_message, so a turn
                        # that fell back rendered its document and then threw
                        # it away: `has_document` was absent, and that flag is
                        # the only thing the SSE route looks at before
                        # persisting and emitting an artifact. Working context
                        # and the dialogue-state deltas were dropped too, so
                        # the next turn could not say "it".
                        has_document = self._finish_turn(
                            user_input, final_response, result)
                        # Stream the complete response
                        chunk_size = 50
                        for i in range(0, len(final_response), chunk_size):
                            chunk_text = final_response[i:i+chunk_size]
                            yield {"type": "content", "content": chunk_text, "step": "response"}
                        yield {"type": "complete", "message": "Query completed successfully", "step": "done", "response": final_response, "response_length": len(final_response), "success": True, "has_document": has_document}
                    else:
                        logger.error("[SQL_AGENT] No final_response found even after invoke fallback")
                        yield {"type": "error", "message": "No response generated", "step": "error"}
                        # Always yield completion even on error
                        yield {"type": "complete", "message": "Stream ended with error - no response generated", "step": "done", "success": False}
                except Exception as e:
                    logger.error(f"[SQL_AGENT] Error getting final response via invoke: {str(e)}", exc_info=True)
                    yield {"type": "error", "message": f"No response generated: {str(e)}", "step": "error"}
                
        except Exception as e:
            logger.error(f"[SQL_AGENT] Error in streaming query: {str(e)}", exc_info=True)
            # Closed phrase, and NOT committed to memory.
            error_msg = _UNEXPECTED_FAILURE
            yield {"type": "error", "message": error_msg, "step": "error"}
            # Always yield completion to close stream properly
            yield {"type": "complete", "message": "Stream ended with error", "step": "done", "success": False}

    def _record_working_context(self, user_input: str, state: dict) -> None:
        """Persist what this turn produced, so the NEXT turn can say "it".

        Written through to the session file immediately (the file, not this
        instance, is what survives a restart). Never fatal: losing the memory
        of a turn must not fail the turn itself.
        """
        try:
            state = state or {}
            fields = {"last_query": (user_input or "")[:500]}

            result = state.get("query_result") or {}
            rows = result.get("rows") or []
            if result.get("success") and rows:
                fields["last_result"] = self.conversation_memory.build_result_reference(
                    rows=rows,
                    sql=state.get("generated_sql"),
                    purpose=state.get("sql_purpose"),
                    history_id=state.get("query_history_id"),
                    question=user_input,
                )
                fields["last_action"] = "query_database"

            # Is the narrative this turn produced worth putting IN a document?
            #
            # Only if the turn actually produced data or a document. Without
            # this, "generate a PDF" after a failed turn rendered the failure
            # NOTICE as the report — observed live, a PDF whose entire body
            # was "I couldn't reach that report to translate it." presented as
            # a finished intelligence report.
            #
            # A boolean, deliberately, not the text: the narrative is already
            # stored once in conversation memory, and copying surveillance
            # prose somewhere else to answer this question is a bad trade.
            observation = state.get("observation") or {}
            produced_data = bool(result.get("success") and rows)
            produced_document = bool(state.get("artifact_payload")
                                     or state.get("committed_artifact_id"))
            failed = observation.get("success") is False
            fields["last_narrative_reportable"] = bool(
                (produced_data or produced_document) and not failed)
            if state.get("response_language"):
                fields["response_language"] = state["response_language"]

            self.conversation_memory.update_working_context(**fields)
        except Exception as e:
            logger.warning("[SQL_AGENT] Could not record working context: %s", e)
            try:
                from . import observability
                observability.observe_memory_failure("working_context_write")
            except Exception:
                pass

    def query_with_details(self, user_input: str, learn: bool = False) -> dict:
        """
        Process a query and return full details (for debugging).

        Args:
            user_input: The natural language query from the user
            learn: Whether to save successful queries for future reference

        Returns:
            Complete state dictionary with all intermediate results
        """
        initial_state = self._create_initial_state(user_input, should_learn=learn)
        # This entry point silently ran memoryless while query()/query_stream()
        # carried context — same agent, different answers depending on which
        # method a caller happened to use.
        initial_state["conversation_context"] = \
            self.conversation_memory.get_conversation_context(limit=6)

        try:
            return self.agent.invoke(initial_state)
        except Exception as e:
            initial_state["error"] = str(e)
            return initial_state

    def test_connection(self) -> bool:
        """Test the database connection."""
        try:
            logger.debug("[SQL_AGENT] Testing database connection")
            conn = self.db.get_connection()
            conn.close()
            logger.info("[SQL_AGENT] Database connection test successful")
            return True
        except Exception as e:
            logger.error(f"[SQL_AGENT] Database connection test failed: {str(e)}")
            return False

    def get_schema(self) -> str:
        """Get the database schema description."""
        return self.db.get_schema_description()

    def get_knowledge_base_stats(self) -> Dict:
        """Get statistics about the knowledge base."""
        return self.kb.get_stats()
