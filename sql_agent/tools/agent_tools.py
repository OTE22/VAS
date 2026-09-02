"""
Agent Tools Module
==================
Tools/nodes for the SQL Intelligence Agent workflow.
"""

import re
import json
import logging
from typing import List, Dict, Optional
from difflib import SequenceMatcher

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser

from config import settings
from ..config import config
from ..state import AgentState
from ..llm import TaskType, create_llm, create_sql_llm
from ..database import DatabaseManager
from ..knowledge_base import SQLKnowledgeBase
from .sql_tools import prepare_sql_from_llm_response, validate_sql_query
from . import planner
from .agent_loop import _MAX_TURN_OBSERVATIONS
from .. import observability
from ..security import sql_guard as sql_security
from ..skills import resolver as skill_resolver
from ..input_pipeline import QueryInputError, ingest_query as build_query_envelope

# Setup logger for SQL Agent Tools
logger = logging.getLogger(__name__)


# Stage 0 is an attribution control, not the database security boundary: three
# attributed matches can restrict an account. Only explicit write requests may
# reach that policy. Broad words such as "update" or "drop" are ordinary nouns
# in analytical questions and must not be treated as attacks.
_EXPLICIT_WRITE_PATTERNS = (
    (r"\bdelete\s+from\b|\bdelete\s+(?:all|everything)\b|"
     r"\bdelete\s+(?:all|every|the|these|those)\s+"
     r"(?:rows?|records?|data|detections?|users?)\b", "DELETE operation"),
    (r"\b(?:remove|clear|wipe|erase|purge)\s+(?:all|every|the|these|those)\s+"
     r"(?:rows?|records?|data|detections?|users?|table|database)\b",
     "DELETE operation"),
    (r"\bupdate\s+[a-z_][\w.]*\s+set\b|\bupdate\s+(?:the\s+)?"
     r"(?:row|record|table|database|user|detection)s?\b", "UPDATE operation"),
    (r"\binsert\s+into\b|\badd\s+(?:a\s+)?(?:new\s+)?"
     r"(?:row|record|user|detection)\s+(?:to|into)\b", "INSERT operation"),
    (r"\bcreate\s+(?:a\s+)?(?:new\s+)?(?:row|record|table|database)\b",
     "INSERT operation"),
    (r"\balter\s+table\b|\bmodify\s+(?:the\s+)?table\b",
     "ALTER TABLE operation"),
    (r"\bdrop\s+(?:table|database|schema|view)\b|\btruncate(?:\s+table)?\s+"
     r"[a-z_]", "DROP operation"),
    (r"\bgrant\s+\w+\s+on\b|\brevoke\s+\w+\s+on\b",
     "Permission modification"),
    (r"\b(?:commit|rollback)(?:\s+transaction)?\s*;?\s*$|"
     r"\bbegin\s+transaction\b", "Transaction control"),
    (r"\bexec(?:ute)?\s*\(", "SQL execution attempt"),
    # Arabic imperative/write forms paired with a data target. These are
    # intentionally narrower than a language classifier: uncertain text is
    # still protected by the generated-SQL AST guard without blaming a user.
    (r"\b(?:احذف|إحذف|امسح|أمسح)\s+(?:كل|جميع|ال)?\s*"
     r"(?:السجلات|البيانات|الصفوف|المستخدمين|الجدول|قاعدة\s+البيانات)\b",
     "DELETE operation"),
    (r"\b(?:حدّث|حدث|عدّل|عدل|غيّر|غير)\s+(?:ال)?"
     r"(?:سجل|بيانات|صف|جدول|مستخدم)\b", "UPDATE operation"),
    (r"\b(?:أضف|اضف|ادخل|أدخل)\s+(?:سجل|صف|مستخدم|بيانات)\b",
     "INSERT operation"),
)


def _detect_explicit_write_intent(text: str) -> List[str]:
    """Return deterministic operation labels for explicit write requests."""
    normalized = " ".join(str(text or "").split()).lower()
    return sorted({operation for pattern, operation in _EXPLICIT_WRITE_PATTERNS
                   if re.search(pattern, normalized, re.IGNORECASE)})


def _is_timeout_error(exc: BaseException) -> bool:
    """Whether an exception represents the model running out of time.

    Matched structurally where possible (httpx raises ReadTimeout / ConnectTimeout,
    both subclasses of TimeoutException) and by name otherwise, because the
    exception can arrive wrapped by langchain or the ollama client rather than as
    the original httpx type.
    """
    try:
        import httpx
        if isinstance(exc, httpx.TimeoutException):
            return True
    except ImportError:  # pragma: no cover - httpx ships with the ollama client
        pass
    if isinstance(exc, TimeoutError):
        return True
    name = type(exc).__name__.lower()
    if "timeout" in name or "timedout" in name:
        return True
    # Last resort: the wrapped cause chain.
    cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    if cause is not None and cause is not exc:
        return _is_timeout_error(cause)
    return False


def _has_new_information(state: dict, planned: dict) -> bool:
    """Would re-running this action actually do something different?

    Repeating an action is usually futile — translating the same missing
    document, or re-rendering the same failed report, just spends the budget
    reproducing one error. SQL generation is the exception: the rejected
    query and the validator\'s reason are fed back into the prompt
    (`_correction_hint`), so the second attempt has strictly more to work
    with than the first.

    Judged from what the graph will DO, never from how the model describes
    its intentions.
    """
    if planned.get("action") not in ("query_database", "modify_previous_query"):
        return False
    return bool(state.get("sql_correction_hint"))


def _plan_arguments(plan: dict) -> dict:
    """The argument-shaped fields of a plan, for fingerprinting a retry.

    Only what the user actually asked for: two plans differing solely in
    confidence, provenance or derived routing metadata are the SAME attempt.

    `target` is deliberately excluded. It is derived ("artifact" /
    "last_result") and `artifact_id` already says which document is meant, so
    including it only risks making a literal repeat look novel when one side
    was built without it. Erring toward calling something a repeat is the
    safe direction: the cost is one honest answer instead of one more try.
    """
    return {key: plan.get(key) for key in
            ("modification", "format", "language", "artifact_id",
             "clarify_question") if plan.get(key)}




def tool_registry_max_steps() -> int:
    """The CONTEXTUAL look-up budget (the tool loop's own default)."""
    from . import tool_registry
    return tool_registry.MAX_TOOL_STEPS


_TRANSLATION_DIRECTIVE = {
    "ar": ("Rewrite the report below in Modern Standard Arabic (الفصحى). Keep the "
           "SAME markdown structure and section order, with Arabic headings. "
           "Person names, camera names, timestamps, identifiers and numbers must "
           "remain EXACTLY as written — do not translate or transliterate them, "
           "because an operator uses those strings to find the camera or person. "
           "Output only the rewritten report."),
    "en": ("Rewrite the report below in English. Keep the SAME markdown structure "
           "and section order. Person names, camera names, timestamps, identifiers "
           "and numbers must remain EXACTLY as written. Output only the rewritten "
           "report."),
}

# Bounded so a very long report cannot blow the model's context or the budget.
_TRANSLATION_MAX_CHARS = 40_000


def translate_document_text(source: str, language: str) -> str:
    """Restate a report in `language`, preserving its structure.

    Module-level and stateless because both the graph node and the API layer
    need it — the node for an inline narrative, the route for a stored
    document. Returns the ORIGINAL text if translation fails: an untranslated
    report is a disappointment, an empty one is a data loss.
    """
    text = (source or "").strip()
    if not text:
        return ""
    directive = _TRANSLATION_DIRECTIVE.get(language, _TRANSLATION_DIRECTIVE["en"])
    truncated = text[:_TRANSLATION_MAX_CHARS]
    try:
        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content="You are a precise technical translator. You do "
                                  "not summarise, add, or omit anything."),
            HumanMessage(content=directive + "\n\n---\n" + truncated),
        ])
        llm = create_llm(TaskType.EXPLANATION)
        translated = (prompt | llm | StrOutputParser()).invoke({})
        translated = (translated or "").strip()
        if len(translated) < max(40, len(truncated) // 10):
            logger.warning("[TRANSLATE] output implausibly short (%d chars from %d); "
                           "keeping the original", len(translated), len(truncated))
            return text
        return translated
    except Exception as e:
        logger.error("[TRANSLATE] translation failed, returning the original: %s", e)
        return text


class SQLAgentTools:
    """Tools for the SQL Intelligence Agent."""

    def __init__(self, conversation_memory=None):
        # Routed by task, not by hardcoded model name: the registry decides
        # which model serves each, applies the data-sensitivity policy, and
        # records any fallback.
        self.llm = create_llm(TaskType.CHAT)  # chat, intent, normalization, analysis
        self.sql_llm = create_sql_llm(TaskType.SQL_GENERATION)  # generation and repair
        self.db = DatabaseManager(config)
        self.kb = SQLKnowledgeBase(config)  # Knowledge Base for RAG
        self.conversation_memory = conversation_memory  # Conversation memory for context

    def _validate_sql_policy(self, sql: str) -> dict:
        """Use the public DB policy contract, with legacy test-double support."""
        validator = getattr(self.db, "validate_query", None)
        if validator is None:
            validator = self.db._validate_query
        return validator(sql)

    def ingest_query(self, state: AgentState) -> AgentState:
        """STAGE 0: create the canonical request envelope.

        This is deterministic input handling, not semantic correction. The
        raw request remains immutable for audit/history while every planner,
        retriever, and generator consumes the same normalized value.
        """
        raw = state.get("original_input")
        logger.info("[STAGE_0] Ingesting query (raw_chars=%d)",
                    len(raw) if isinstance(raw, str) else 0)
        try:
            envelope = build_query_envelope(
                raw, max_chars=int(settings.SQL_AGENT_MAX_QUERY_CHARS))
        except QueryInputError as exc:
            state["normalized_input"] = ""
            state["security_normalized_input"] = ""
            state["input_normalization_error"] = str(exc)
            state["error"] = "INVALID_QUERY_INPUT"
            state["final_response"] = str(exc)
            state["query_result"] = {
                "success": False,
                "error": "INVALID_QUERY_INPUT",
                "error_code": "INVALID_QUERY_INPUT",
                "rows": [],
                "row_count": 0,
            }
            logger.info("[STAGE_0] Query rejected by the input contract")
            return state

        state["normalized_input"] = envelope.normalized_text
        state["security_normalized_input"] = envelope.security_text
        state["input_language"] = envelope.input_language
        state["response_language"] = envelope.response_language
        state["input_normalization_error"] = None
        logger.info(
            "[STAGE_0] Query accepted (normalized_chars=%d input_language=%s "
            "response_language=%s)",
            len(envelope.normalized_text), envelope.input_language,
            envelope.response_language)
        return state

    # Compatibility entry point for callers that invoked the old node
    # directly. The graph intentionally uses the accurately named stage.
    def fix_language(self, state: AgentState) -> AgentState:
        return self.ingest_query(state)

    def detect_malicious_intent(self, state: AgentState) -> AgentState:
        """
        STAGE 1: scan the canonical security view for explicit write intent.

        Ingestion runs first so compatibility characters and zero-width
        controls cannot make different stages see different verbs.
        """
        logger.info("[STEP_0] Security scan - input_chars=%d",
                    len(state.get("original_input") or ""))
        logger.info("\n" + "="*60)
        logger.info("🛡️ STEP 0: SECURITY SCAN - MALICIOUS INTENT DETECTION")
        logger.debug("="*60)
        detected_operations = _detect_explicit_write_intent(
            state.get("security_normalized_input")
            or state.get("normalized_input")
            or state.get("original_input") or "")
        for operation in detected_operations:
            logger.warning("[SECURITY] STEP 0 explicit write intent: %s",
                           operation)
        
        if detected_operations:
            # The operation is denied immediately. Account consequences are
            # decided later by the centralized threshold policy.
            operations_str = ", ".join(set(detected_operations))
            block_reason = (
                "Explicit database alteration request detected in the user's "
                f"natural-language query: {operations_str}.")
            
            logger.warning("[SECURITY] STEP 0 denying explicit write request: %s",
                           operations_str)
            logger.error(f"[SECURITY] Block reason: {block_reason}")
            
            # Mark the turn for centralized policy review.
            # Try multiple ways to get user_id
            user_id = getattr(self.conversation_memory, 'user_id', None)
            if not user_id and hasattr(self.conversation_memory, 'user_id'):
                user_id = self.conversation_memory.user_id
            
            # Always set security flags even if user_id is not available
            # The API route will handle the actual blocking when it receives these flags
            logger.warning("[SECURITY] STEP 0 marking turn for policy review")
            if user_id:
                logger.error(f"[SECURITY] User ID available: {user_id}")
            else:
                logger.warning(f"[SECURITY] User ID not available in conversation_memory - will be handled by API route")
            
            state["security_block_user"] = True
            state["security_block_actor"] = "user"
            state["security_block_reason"] = block_reason
            # Store user_id in state if available for API route to use
            if user_id:
                state["security_block_user_id"] = user_id
            
            # Set error state to stop processing
            # The detector says WHAT was refused. It must not say what happens to
            # the account — sql_agent/security_policy.py decides that, and only
            # after the database write commits. Claiming a block here is what
            # produced the "you are blocked" message for users who were not.
            state["security_reason_code"] = "FORBIDDEN_SQL_ATTEMPT"
            state["error"] = f"SECURITY VIOLATION: {operations_str} detected. This system is read-only and cannot execute data modification operations."
            state["final_response"] = f"That operation is not permitted: the assistant is read-only and cannot modify the database ({operations_str})."
            state["query_result"] = {
                "success": False,
                "error": f"Security: {operations_str} operations are not allowed.",
                "rows": [],
                "row_count": 0
            }
            
            logger.error(f"🚨 SECURITY ALERT: Malicious intent detected!")
            logger.info(f"   Operations: {operations_str}")
            logger.info("   Account action deferred to security policy")
            logger.info(f"   Processing stopped")
            
            return state
        
        logger.info(f"✅ Security scan passed - No malicious intent detected")
        logger.debug(f"[STEP_0] Security scan passed")
        return state

    # `intent` is the LEGACY vocabulary, kept populated so downstream readers
    # and older tests keep working. Document actions map to CHAT here for
    # that compatibility — which is exactly why routing must NOT use this map.
    _ACTION_TO_INTENT = {
        "chat": "CHAT",
        "clarify": "CHAT",
        "query_database": "SQL_QUERY",
        "modify_previous_query": "SQL_QUERY",
        "generate_document": "CHAT",
        "translate_artifact": "CHAT",
    }

    # The graph node each action actually runs on. Kept separate from the
    # intent map above: while generate_document routed to chat_response,
    # "make that a PDF" was answered by the chat model — fluent text about a
    # document that did not exist. Routing and the audit line use THIS.
    _ACTION_TO_NODE = {
        "chat": "chat_response",
        "clarify": "chat_response",
        "query_database": "check_schema",
        "modify_previous_query": "check_schema",
        "generate_document": "render_artifact",
        "translate_artifact": "translate_artifact",
    }

    def plan_action(self, state: AgentState) -> AgentState:
        """STEP 2: Decide what the user is asking for.

        Replaces the binary CHAT/SQL_QUERY classifier at the same graph seam,
        in three stages that keep authority in Python:

          1. Python resolves what this turn could refer to (working memory +
             the caller's own artifacts) into a closed candidate set.
          2. The LLM chooses an action from that set — intent only.
          3. Python re-validates the choice and downgrades anything whose
             precondition is missing to `clarify`.

        `intent` and `intent_confidence` are still written, so every
        downstream node and every existing test sees the state it expects.
        On any planner failure this falls back to the verbatim previous
        classifier — except for requests that are clearly ABOUT state we
        hold, which become a question rather than small talk.
        """
        user_text = state.get("normalized_input") or state.get("original_input") or ""
        # Did this message ANSWER the question we asked last turn? Decided
        # in Python against the STORED candidate list, so a selection resumes
        # the original task instead of arriving as an unanchored fragment
        # ("Ali Abbass" after "Which Ali?").
        try:
            from .. import dialogue_state as _ds

            chosen = _ds.match_candidate(
                (state.get("working_context") or {}).get("dialogue_state") or {},
                user_text)
        except Exception as e:
            logger.warning("[REACT] clarification match failed: %s", e)
            chosen = None
        if chosen:
            state["resolved_entities"] = [{
                "tool": "pending_clarification",
                "raw_text": user_text,
                "identity_id": chosen.get("identity_id"),
                "canonical_name": chosen.get("display_name")}]
            state["clarification_answered"] = True
            logger.info("[REACT] clarification answered subject_present=%s",
                        bool(chosen.get("display_name")))

        # What THIS TURN has already established. Re-entering the loop for a
        # second action used to start from nothing, so the agent could resolve
        # a person and then immediately not know who they were.
        prior_observations = state.get("observations") or []

        candidates = planner.resolve_candidates(
            state.get("working_context"), state.get("artifact_index"), user_text)
        state["planner_candidates"] = candidates

        # MODE FIRST, chosen deterministically from the conversation's shape.
        # FAST is deliberately hard to reach: misreading a follow-up as a
        # fresh question is the expensive mistake, an extra model call is the
        # cheap one. The model never picks its own budget.
        from .. import reasoning
        mode = reasoning.select_mode(candidates, user_text)
        state["reasoning_mode"] = mode
        state.setdefault("replan_count", 0)
        state.setdefault("execution_retries", 0)
        state.setdefault("reasoning_steps_used", 0)
        state.setdefault("failed_action_fingerprints", [])

        plan = None
        resolution = "planner"
        try:
            # The rolling summary is a DERIVED CACHE: rebuilt whenever it is
            # stale, corrupt or version-incompatible, never trusted blind and
            # never consulted for exact values. It is the lowest-priority
            # section in the envelope, so it yields before the authoritative
            # state does.
            summary_text = None
            try:
                from .. import dialogue_state as ds
                context = state.get("working_context") or {}
                cached = context.get("conversation_summary")
                recent = self._recent_turn_texts(limit=8)
                dialogue = ds.migrate_state(context.get("dialogue_state"))
                if ds.needs_rebuild(cached, turn_count=len(recent),
                                    context_version=int(
                                        dialogue.get("context_version") or 0)):
                    cached = ds.build_summary(recent, dialogue)
                    if self.conversation_memory:
                        self.conversation_memory.update_working_context(
                            conversation_summary=cached)
                summary_text = (cached or {}).get("text")
            except Exception as summary_error:
                logger.info("[SUMMARY] skipped: %s", summary_error)

            context_block = planner.build_planner_context(
                candidates, conversation_summary=summary_text)

            # TOOL LOOP FIRST. It may perform read-only look-ups before
            # committing, which is what stops the agent guessing at camera
            # ids and misspelled names. It returns None whenever it does not
            # commit to an action, and the single-shot planner below then
            # runs exactly as it did before tools existed.
            try:
                from . import agent_loop

                # The loop runs on EVERY turn. It used to be skipped in FAST
                # mode, which meant the cheapest turns never reasoned at all
                # and fell straight through to the single-shot planner — the
                # opposite of deciding from the moment the prompt arrives.
                #
                # The mode now sets the BUDGET, not whether to think:
                #   FAST        nothing to refer to, so one step is enough
                #   CONTEXTUAL  the default room for a look-up then an action
                #   MULTI_STEP  a compound request may need several look-ups
                if mode == reasoning.ReasoningMode.FAST:
                    step_budget = 1
                elif mode == reasoning.ReasoningMode.MULTI_STEP:
                    step_budget = int(settings.SQL_AGENT_MAX_REASONING_STEPS)
                else:
                    step_budget = tool_registry_max_steps()
                tool_call, tool_trace, turn_is_a_request = agent_loop.run_tool_loop(
                    self.llm,
                    user_text=user_text,
                    context_block=context_block,
                    db=self.db,
                    dialogue_state=(state.get("working_context") or {}).get(
                        "dialogue_state"),
                    artifact_index=state.get("artifact_index") or [],
                    identity_index=state.get("identity_index") or [],
                    max_steps=max(1, step_budget),
                    prior_observations=prior_observations,
                    # Deterministic: this message ANSWERS the question we
                    # asked, so it continues that request by construction.
                    known_request=True if state.get(
                        "clarification_answered") else None,
                    has_result=bool(candidates.get("last_result")))
                state["reasoning_steps_used"] = (
                    int(state.get("reasoning_steps_used") or 0) + len(tool_trace))
                state["tool_trace"] = tool_trace
                # APPEND, never replace: a later action in the same turn
                # must still see what the earlier ones found.
                observations = list(state.get("observations") or [])
                for e in tool_trace:
                    if e.get("observation"):
                        observations.append({
                            "sequence": len(observations) + 1,
                            **e["observation"]})
                    elif e.get("tool") and "ok" in e:
                        observations.append({
                            "sequence": len(observations) + 1,
                            "tool": e["tool"],
                            "status": "ok" if e["ok"] else "error",
                            "signature": e.get("signature")})
                state["observations"] = observations[-_MAX_TURN_OBSERVATIONS:]

                state["resolved_entities"] = list(
                    state.get("resolved_entities") or []) + [
                    e["resolved_entity"] for e in tool_trace
                    if e.get("resolved_entity")]
                for e in tool_trace:
                    if e.get("committed") and e.get("signature"):
                        # Kept so a later action in the same turn can tell
                        # what has already been done, not merely that
                        # something was.
                        state["committed_signature"] = e["signature"]
                    if e.get("clarification_candidates"):
                        state["clarification_candidates"] =                             e["clarification_candidates"]
                # Judged once, by a model call already paid for. Discarding it
                # and letting the narrative re-guess from the transcript is
                # how "hi" got answered with a surveillance summary.
                state["turn_is_a_request"] = turn_is_a_request
                if tool_call:
                    planned = agent_loop.action_to_planned(tool_call, candidates)
                    if planned:
                        if (planned.get("action") == "query_database"
                                and tool_call.get("name") == "query_database"):
                            paraphrase = str(
                                (tool_call.get("arguments") or {}).get(
                                    "question") or "").strip()
                            if paraphrase:
                                # A planning aid, not a replacement for the
                                # authoritative normalized request.
                                state["sql_generation_input"] = paraphrase[:500]
                        state["planned_action"] = planned
                        state["intent"] = self._ACTION_TO_INTENT.get(
                            planned.get("action"), "CHAT")
                        state["intent_confidence"] = planned.get("confidence", 0.9)
                        if planned.get("action") == "clarify":
                            state["clarify_question"] = planned.get(
                                "clarify_question")
                        if planned.get("language"):
                            state["response_language"] = planned["language"]
                        logger.info(planner.audit_line(
                            user_id=state.get("user_id"),
                            conversation_id=state.get("conversation_id"),
                            plan=planner.PlannedAction(**{
                                k: v for k, v in planned.items()
                                if k in planner.PlannedAction.__slots__}),
                            executed=self._ACTION_TO_NODE.get(
                                planned.get("action"), state["intent"]),
                            resolution=f"tools:{len(tool_trace)}/{mode}",
                            artifact_id=planned.get("artifact_id"),
                            result_id=(candidates.get("last_result") or {}).get(
                                "history_id")))
                        return state
            except (TypeError, AttributeError, NameError, KeyError) as bug:
                # A CODE fault, not a model one. This catch-all previously
                # reported every failure as "unavailable, using the planner",
                # which reads like an expected condition — so a tuple-unpack
                # error silently discarded a correct loop decision and let the
                # planner override it for a whole round of testing. Programming
                # errors get a traceback and ERROR level; the turn still
                # degrades to the planner rather than failing the user.
                logger.error("[TOOL_LOOP] BUG in the loop, falling back to the "
                             "planner: %s", bug, exc_info=True)
            except Exception as tool_error:
                logger.warning("[TOOL_LOOP] unavailable, using the planner: %s",
                               tool_error)
            planner_prompt = skill_resolver.compose(
                planner.PLANNER_SYSTEM_PROMPT,
                has_result=bool(candidates.get("last_result")),
                has_documents=bool(state.get("artifact_index")),
            )
            prompt = ChatPromptTemplate.from_messages([
                SystemMessage(content=planner_prompt),
                HumanMessage(content=(
                    self._context_section(state)
                    + "Conversation state:\n" + context_block
                    + f"\n\nRequest: {user_text}\n\nJSON:"))
            ])
            self._trace_envelope("plan_action", prompt)
            raw = (prompt | self.llm | StrOutputParser()).invoke({})
            # DEBUG ONLY. If a model prepends prose to its JSON, that prose
            # is its reasoning, and a production log is not a place for it.
            if settings.DEBUG:
                logger.debug("[STEP_2] planner response received (chars=%d)",
                             len(str(raw)))
            else:
                logger.info("[STEP_2] planner replied (%d chars)", len(str(raw)))
            plan = planner.validate_plan(planner.extract_json_object(raw), candidates)
        except Exception as e:
            logger.error("[STEP_2] planner failed: %s", e, exc_info=True)

        if plan is None:
            plan = planner.decide_on_failure(user_text, candidates)
            resolution = ("failed->clarify" if plan else "failed->legacy") + f"/{mode}"

        if plan is None:
            # Both the loop and the planner declined. There used to be a
            # third decision here — `classify_intent`, a binary CHAT vs
            # SQL_QUERY call with no tools, no candidate set and no dialogue
            # state. It was the weakest of the three and ran LAST, so it
            # overrode better-informed decisions: one captured turn shows the
            # planner deciding and then the classifier deciding again.
            #
            # Answering is the safe residual. A request that was clearly
            # ABOUT held state has already become a clarification above
            # (`decide_on_failure`), so what reaches here is an ordinary
            # message that two better-informed stages could not turn into an
            # action.
            # ...but only for a message that ASKED for nothing. A data
            # request that reaches here has been abandoned, and answering it
            # conversationally reports success for work never done.
            if state.get("turn_is_a_request"):
                from .. import reasoning

                logger.info("[REACT] terminal=%s reason=no_action_chosen",
                            reasoning.MAX_ITERATIONS)
                state["terminal_state"] = reasoning.MAX_ITERATIONS
                state["planned_action"] = {"action": "chat",
                                           "source": "exhausted"}
                state["intent"] = "CHAT"
                state["reasoning_exhausted"] = True
                observability.observe_planner_action("chat", "exhausted")
                return state

            logger.info("[STEP_2] no action chosen; answering directly")
            state["planned_action"] = {"action": "chat", "source": "fallback"}
            observability.observe_planner_action("chat", "fallback")
            state["intent"] = "CHAT"
            state["terminal_state"] = None
            return state

        state["planned_action"] = plan.as_dict()
        observability.observe_planner_action(plan.action, plan.source)
        state["intent"] = self._ACTION_TO_INTENT.get(plan.action, "CHAT")
        state["intent_confidence"] = plan.confidence
        if plan.action == "clarify":
            state["clarify_question"] = plan.clarify_question
        if plan.language:
            state["response_language"] = plan.language

        # `executed` names the NODE this turn will run, not the legacy intent:
        # document actions map onto CHAT for compatibility, so logging the
        # intent would record every translation as a chat turn.
        executed = self._ACTION_TO_NODE.get(plan.action, state["intent"])
        logger.info(planner.audit_line(
            user_id=state.get("user_id"), conversation_id=state.get("conversation_id"),
            plan=plan, executed=executed,
            resolution=(resolution if "/" in resolution
                        else f"{resolution}/{mode}"),
            artifact_id=plan.artifact_id,
            result_id=(candidates.get("last_result") or {}).get("history_id")))
        return state

    def observe_and_replan(self, state: AgentState) -> AgentState:
        """OBSERVE what the action produced; decide whether to correct course.

        Bounded and deterministic:
          * the Observation is built in Python from state — the model
            contributes nothing to it;
          * `decide_next` applies a fixed taxonomy and the budgets;
          * a re-plan makes ONE model call, and its proposal is re-validated
            through the same dispatcher path as any other action;
          * a proposal identical to one that already failed this turn is
            refused, so re-planning is corrective rather than repetitive.

        Never raises: a failure here degrades to answering, which is exactly
        what the graph did before this node existed.
        """
        from .. import reasoning

        observation = reasoning.check_invariants(reasoning.build_observation(state))
        state["observation"] = observation

        decision = reasoning.decide_next(
            observation,
            mode=state.get("reasoning_mode") or reasoning.ReasoningMode.CONTEXTUAL,
            replan_count=int(state.get("replan_count") or 0),
            execution_retries=int(state.get("execution_retries") or 0),
            max_replans=int(settings.SQL_AGENT_MAX_REPLANS),
            max_execution_retries=int(settings.SQL_AGENT_MAX_EXECUTION_RETRIES))
        state["reasoning_decision"] = decision

        try:
            next_action = self._act_on_decision(state, observation, decision)
        except Exception as e:
            logger.error("[REASONING] observe/replan failed: %s", e, exc_info=True)
            next_action = "chat_response"
            state["reasoning_decision"] = {**decision,
                                           "decision": reasoning.ANSWER}

        state["reasoning_next"] = next_action
        logger.info(reasoning.reasoning_trace(
            conversation_id=state.get("conversation_id"),
            turn_id=state.get("query_history_id"),
            mode=state.get("reasoning_mode"),
            observation=observation, decision=state["reasoning_decision"],
            next_action=next_action,
            replan_count=int(state.get("replan_count") or 0)))
        return state

    def _act_on_decision(self, state: AgentState, observation: dict,
                         decision: dict) -> str:
        """Carry out one decision. Returns the node the router should enter."""
        from .. import reasoning

        verdict = decision["decision"]

        if verdict == reasoning.RETRY_EXECUTION:
            # Infrastructure only: the SAME SQL, on its own budget. No model
            # call, so a dropped connection cannot spend reasoning.
            state["execution_retries"] = int(state.get("execution_retries") or 0) + 1
            state["query_result"] = None
            return "prepare_sql_for_execution"

        if verdict == reasoning.RESOLVE_ENTITY:
            return self._resolve_entity_and_route(state, observation)

        if verdict == reasoning.CLARIFY:
            state["clarify_question"] = self._clarify_question_for(
                observation, state)
            state["planned_action"] = {"action": "clarify", "source": "reasoning",
                                       "confidence": 1.0}
            return "chat_response"

        if verdict == reasoning.REPLAN:
            replanned = self._replan(state, observation, decision)
            if not replanned:
                # Nothing usable came back. Answer honestly rather than
                # spending another step reproducing the same failure.
                state["reasoning_decision"] = {**decision,
                                               "decision": reasoning.ANSWER}
                return "chat_response"

            state["planned_action"] = replanned
            state["replan_count"] = int(state.get("replan_count") or 0) + 1
            state["reasoning_steps_used"] = int(
                state.get("reasoning_steps_used") or 0) + 1
            # A corrected action starts from a clean slate. Without this the
            # stale INVALID status (or a stale failed result) would route the
            # correction straight back here without it ever being tried.
            state["sql_validation_status"] = "VALID"
            state["sql_validation_error"] = None
            state["sql_validation_warnings"] = []
            state["query_result"] = None
            if replanned.get("action") in ("query_database", "modify_previous_query"):
                state["generated_sql"] = ""
            if replanned.get("language"):
                state["response_language"] = replanned["language"]
            return self._ACTION_TO_NODE.get(replanned.get("action"),
                                            "chat_response")

        if verdict == reasoning.ANSWER and observation.get("success"):
            # The action worked. Is the user's WHOLE request carried out, or
            # was this one step of it? Only asked while budget remains, and it
            # fails safe toward finishing.
            from . import agent_loop

            taken = int(state.get("actions_taken") or 0) + 1
            state["actions_taken"] = taken
            ceiling = int(settings.SQL_AGENT_MAX_ACTIONS_PER_TURN)

            if taken < ceiling:
                done_summary = (f"{observation.get('action')} "
                                f"(rows={observation.get('row_count')}, "
                                f"artifact={bool(observation.get('artifact_id'))})")
                if not agent_loop.request_is_satisfied(
                        self.llm, state.get("normalized_input") or "",
                        done_summary):
                    logger.info("[REASONING] request not yet complete after %s; "
                                "acting again (%d/%d)",
                                observation.get("action"), taken, ceiling)
                    # A fresh action starts from a clean slate, exactly as a
                    # correction does — otherwise the previous result routes
                    # the next step straight back here.
                    # Record the completed action as done, WITH its
                    # signature, so the next one recognises a repeat rather
                    # than re-running it.
                    if state.get("committed_signature"):
                        done = list(state.get("observations") or [])
                        done.append({
                            "sequence": len(done) + 1,
                            "tool": observation.get("action"),
                            "status": "ok",
                            "summary": f"rows={observation.get('row_count')}",
                            "signature": state["committed_signature"]})
                        state["observations"] = done[-_MAX_TURN_OBSERVATIONS:]

                    # HAND THE RESULT ON before wiping the per-action state.
                    # The next action re-enters a fresh loop, and the context
                    # it builds reads `last_result` from the working context -
                    # which was only ever written at END of turn. So action two
                    # opened with `last_result=n`, could not see the rows action
                    # one had just fetched, and simply ran the query again:
                    # "track joey and give me the report in arabic" queried
                    # twice and reported never.
                    #
                    # A bounded REFERENCE, the same one the end of the turn
                    # records - row count and question, never the rows.
                    try:
                        finished = state.get("query_result") or {}
                        rows = finished.get("rows") or []
                        if finished.get("success") and rows and self.conversation_memory:
                            carried = dict(state.get("working_context") or {})
                            carried["last_result"] = (
                                self.conversation_memory.build_result_reference(
                                    rows=rows,
                                    sql=state.get("generated_sql"),
                                    purpose=state.get("sql_purpose"),
                                    history_id=state.get("query_history_id"),
                                    question=state.get("normalized_input")))
                            carried["last_action"] = "query_database"
                            state["working_context"] = carried
                    except Exception as e:
                        logger.warning("[REASONING] could not carry the result "
                                       "to the next action: %s", e)

                    state["planned_action"] = None
                    state["query_result"] = None
                    state["generated_sql"] = ""
                    state["sql_validation_status"] = "VALID"
                    return "plan_action"

            # The action WORKED and the request is finished, so hand the turn
            # back to its normal narration path.
            #
            # This used to fall through to chat_response, which was correct
            # while only FAILURES reached the observer. Raising the action
            # ceiling above 1 started routing SUCCESSES here too, and the chat
            # node is never given the result rows - so `track joey` retrieved
            # 3 real detections and answered "Joey has 1 query pattern
            # tracked", and on another run "there are no tracking records for
            # him". Both were invented, because the narrator could not see the
            # data it was describing.
            if observation.get("action") in ("query_database",
                                             "modify_previous_query"):
                return "enrich_co_appearance"

        return "chat_response"


    @staticmethod
    def _clarify_question_for(observation: dict, state: AgentState) -> str:
        """One short question, in the language the user is speaking.

        Asking beats guessing at somebody's identity — and the question has
        to reach them in their own language, which the hardcoded English
        failure strings this node replaces never did.

        Keyed off whether a name is actually in play, not off the error code.
        A name that resolved to nobody and a query that found nothing for
        that name are different facts, and saying the accurate one is what
        makes the question answerable.
        """
        from .. import reasoning

        arabic = (state.get("response_language") or "en") == "ar"
        entity = observation.get("unresolved_entity")
        unmatched = (observation.get("error_type")
                     == reasoning.ErrorType.EMPTY_RESULT)

        if entity:
            if arabic:
                if unmatched:
                    return ("لم أجد أي "
                            "سجلات باسم "
                            "“" + str(entity) + "”. "
                            "هل هذا هو "
                            "الاسم المسجل "
                            "في النظام؟")
                return ("لم أتمكن من "
                        "تحديد “" + str(entity) + "”. "
                        "هل يمكنك "
                        "كتابة الاسم "
                        "كما هو مسجل؟")
            if unmatched:
                return (f"I found no records for {entity!r}. Is that the name "
                        f"as it is enrolled?")
            return (f"I could not identify {entity!r}. Could you give the "
                    f"name as it is enrolled?")

        if observation.get("error_type") == reasoning.ErrorType.ENTITY_UNRESOLVED:
            if arabic:
                return ("لم أتعرف على "
                        "هذا الشخص. "
                        "ما الاسم "
                        "المسجل؟")
            return "I could not identify that person. What name is enrolled?"

        if arabic:
            return ("هل يمكنك "
                    "توضيح ما "
                    "تريده بالضبط؟")
        return "Could you tell me a little more about what you need?"

    @staticmethod
    def _correction_hint(state) -> str:
        """What the LAST attempt got wrong, for a corrective regeneration.

        Empty on a first attempt, so the prompt is unchanged for every normal
        turn. On a re-plan it is the difference between self-correction and
        rolling the dice again on identical inputs: without it, regenerating
        from the same `normalized_input` and the same schema most often
        reproduces the same broken query.

        Carries the rejected SQL and the validator's reason — machine output,
        both of them. No model prose, and nothing from the result set.
        """
        hint = state.get("sql_correction_hint") or {}
        if not hint:
            return ""
        parts = ["\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED. Correct it."]
        if hint.get("sql"):
            parts.append(f"Rejected SQL:\n{hint['sql']}")
        if hint.get("reason"):
            parts.append(f"Why it was rejected: {hint['reason']}")
        parts.append("Produce a DIFFERENT query that fixes that problem.")
        return "\n".join(parts)

    @staticmethod
    def _attach_correction_hint(state: AgentState, observation: dict) -> None:
        """Record what the rejected attempt got wrong, for the regeneration.

        Only for query failures, and only from machine output: the SQL the
        validator refused and the reason it gave. Nothing from the result
        set, nothing the model wrote about itself.
        """
        from .. import reasoning

        if observation.get("error_type") not in (
                reasoning.ErrorType.SQL_INVALID,
                reasoning.ErrorType.SQL_GENERATION_ERROR,
                # The DATABASE's complaint is the most precise correction
                # signal available - it names the column and the types.
                reasoning.ErrorType.SQL_EXECUTION_ERROR_CORRECTABLE):
            return
        reason = (observation.get("sanitized_detail")
                  or state.get("sql_validation_error") or "")
        state["sql_correction_hint"] = {
            "sql": (state.get("generated_sql") or "")[:600],
            "reason": str(reason)[:200],
        }

    def _resolve_entity_and_route(self, state: AgentState,
                                  observation: dict) -> str:
        """Look up the person the empty query filtered on, and act on it.

        Python, not a model call: which of the three things an empty result
        means is a matter of fact, and one look-up settles it. Attempted at
        most once per turn.
        """
        from .. import reasoning
        from . import tool_executors as tx

        needle = observation.get("unresolved_entity") or ""
        state["entity_resolution_attempted"] = True

        result = tx.execute_read_only(
            "resolve_person", {"name": needle}, db=self.db,
            identity_index=state.get("identity_index") or [])
        status = result.get("status")
        logger.info("[REACT] entity_resolution name_chars=%d status=%s",
                    len(str(needle or "")), status)

        if status == "ambiguous":
            candidates = result.get("candidates") or []
            state["clarification_candidates"] = candidates
            names = ", ".join(c.get("display_name", "") for c in candidates)
            state["clarify_question"] = f"Which one did you mean: {names}?"
            state["planned_action"] = {"action": "clarify",
                                       "source": "entity_resolution",
                                       "confidence": 1.0}
            state["terminal_state"] = reasoning.CLARIFY
            return "chat_response"

        if status != "resolved":
            state["entity_not_found"] = needle
            state["terminal_state"] = reasoning.NOT_FOUND
            state["planned_action"] = {"action": "chat",
                                       "source": "entity_resolution",
                                       "confidence": 1.0}
            return "chat_response"

        identity = result.get("identity") or {}
        canonical = identity.get("display_name") or ""
        state["resolved_entities"] = list(
            state.get("resolved_entities") or []) + [{
                "tool": "resolve_person", "raw_text": needle,
                "identity_id": identity.get("identity_id"),
                "canonical_name": canonical}]

        if reasoning.would_rerun_help(needle, canonical):
            # A genuine misspelling: the filter never could have matched, so
            # the query is worth running again with the stored spelling.
            state["sql_correction_hint"] = {
                "sql": (state.get("generated_sql") or "")[:600],
                "reason": (f"the filter used {needle!r}, but this person is "
                           f"stored as {canonical!r} - use that exactly")}
            state["generated_sql"] = ""
            state["query_result"] = None
            state["sql_validation_status"] = "VALID"
            # Deliberately NOT charged to replan_count: that counter has
            # exactly one writer (a contract test pins it) and it bounds
            # MODEL re-plans. This is a deterministic correction with no
            # model call, already bounded to once per turn by
            # entity_resolution_attempted - the same reasoning that keeps
            # execution retries off the reasoning budget.

            logger.info("[REACT] re-querying with stored spelling "
                        "(name_chars=%d)", len(str(canonical or "")))
            return "check_schema"

        # The filter WOULD have matched this person, so zero rows is a fact
        # about the DATA, not about the query: they exist and have nothing
        # recorded. "No matching records" hides that difference.
        state["entity_without_data"] = canonical
        state["terminal_state"] = reasoning.FINAL
        state["planned_action"] = {"action": "chat",
                                   "source": "entity_resolution",
                                   "confidence": 1.0}
        return "chat_response"

    def _replan(self, state: AgentState, observation: dict,
                decision: dict) -> Optional[dict]:
        """ONE corrective reasoning call. Returns a validated action, or None.

        The prompt carries the previous action, the factual Observation and
        the failure reason, and forbids re-proposing what already failed.
        That prohibition is then ENFORCED in Python by fingerprint: a model
        under pressure will otherwise cheerfully repeat itself and spend the
        whole budget reproducing one error.
        """
        from .. import reasoning
        from . import agent_loop, tool_registry

        candidates = state.get("planner_candidates") or {}
        previous = state.get("planned_action") or {}
        fingerprints = list(state.get("failed_action_fingerprints") or [])
        failed = reasoning.action_fingerprint(previous.get("action"),
                                              _plan_arguments(previous))
        if failed not in fingerprints:
            fingerprints.append(failed)
        state["failed_action_fingerprints"] = fingerprints

        # Attach what went wrong BEFORE asking for a correction, so a repeat
        # of the same action is a genuinely different attempt rather than the
        # same dice roll — see `_correction_hint`.
        self._attach_correction_hint(state, observation)

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=agent_loop.TOOL_SYSTEM_PROMPT),
            HumanMessage(content=(
                self._context_section(state)
                + planner.build_planner_context(candidates)
                + "\nThe previous attempt FAILED and must be corrected.\n"
                + f"- what was tried: {previous.get('action')}\n"
                + f"- why it failed: {decision.get('reason')}\n"
                + f"- rows returned: {observation.get('row_count')}\n\n"
                + "Choose a DIFFERENT tool, or the same tool with different "
                + "arguments, that avoids that failure. Do not repeat the "
                + "call that just failed.\n\n"
                + tool_registry.render_tools_for_prompt() + "\n\n"
                + f"User's request: {state.get('normalized_input') or ''}\n\n"
                + "Respond with ONLY the JSON tool call:"))
        ])
        self._trace_envelope("replan", prompt)

        try:
            raw = (prompt | self.llm | StrOutputParser()).invoke({})
        except Exception as e:
            logger.warning("[REASONING] replan model call failed: %s", e)
            return None

        call = tool_registry.parse_tool_response(raw)
        if not call or not call.get("name"):
            logger.info("[REASONING] replan produced no parsable tool call")
            return None
        try:
            arguments = tool_registry.validate_call(call["name"],
                                                    call.get("arguments"))
        except tool_registry.ToolCallRejected as rejection:
            logger.info("[REASONING] replan proposal rejected: %s", rejection)
            return None

        planned = agent_loop.action_to_planned(
            {"name": call["name"], "arguments": arguments}, candidates)
        if not planned:
            return None

        # Compare like with like. Fingerprinting the tool CALL against a
        # stored PLAN never matched: `action_to_planned` discards
        # `query_database`\'s question argument (the graph regenerates from
        # `normalized_input` regardless), so the two shapes differed even for
        # a literal repeat. Both sides are now taken from the validated plan.
        repeat = reasoning.action_fingerprint(planned.get("action"),
                                              _plan_arguments(planned))
        if repeat in fingerprints and not _has_new_information(state, planned):
            logger.info("[REASONING] replan repeated a failed action (%s) with "
                        "nothing new to go on; refusing", planned.get("action"))
            return None

        state["failed_action_fingerprints"] = fingerprints + [repeat]
        return planned

    def check_schema(self, state: AgentState) -> AgentState:
        """
        STEP 3: Schema Awareness
        Load and provide schema information.
        """
        logger.info("[STEP_3] Loading database schema")
        logger.info("\n" + "="*60)
        logger.info("📋 STEP 3: SCHEMA AWARENESS")
        logger.debug("="*60)

        try:
            state["schema_description"] = self.db.get_schema_description()
            logger.info(f"[STEP_3] Schema loaded successfully ({len(state['schema_description'])} chars)")
            logger.info(f"✅ Schema loaded ({len(state['schema_description'])} chars)")
            logger.info(f"📄 Tables: faces, detections, pipelines, system_metrics")
        except Exception as e:
            state["schema_description"] = ""
            state["error"] = f"Schema loading error: {str(e)}"
            logger.error(f"[STEP_3] Schema loading failed: {str(e)}", exc_info=True)
            logger.error(f"❌ Error: {state['error']}")

        return state

    def retrieve_examples(self, state: AgentState) -> AgentState:
        """
        STEP 3.5: RAG Retrieval
        Search knowledge base for similar questions and their SQL queries.
        """
        logger.info("[STEP_3.5] RAG retrieval (query_chars=%d)",
                    len(state.get("normalized_input") or ""))
        logger.info("\n" + "="*60)
        logger.info("📚 STEP 3.5: RAG RETRIEVAL")
        logger.debug("="*60)

        try:
            # Search for similar questions
            # Scoped to the caller: learned examples carry the raw text of
            # the question that produced them, and retrieved examples are
            # interpolated into the SQL-generation system prompt. Unscoped,
            # one user's questions became another user's prompt content.
            examples = self.kb.search_similar(
                query=state["normalized_input"],
                top_k=config.rag_top_k,
                user_id=state.get("user_id"),
            )

            state["retrieved_examples"] = examples
            state["rag_context"] = self.kb.format_examples_for_prompt(examples)

            if examples:
                logger.info(f"[STEP_3.5] Found {len(examples)} similar examples")
                logger.info("[STEP_3.5] Similar examples ready "
                            "(top_similarities=%s)",
                            [ex.get("similarity") for ex in examples[:3]])
            else:
                logger.warning("[STEP_3.5] No similar examples found")
                logger.warning("⚠️ No similar examples found")

        except Exception as e:
            state["retrieved_examples"] = []
            state["rag_context"] = ""
            state["error"] = f"RAG retrieval error: {str(e)}"
            logger.error(f"[STEP_3.5] RAG retrieval failed: {str(e)}", exc_info=True)
            logger.error(f"❌ Error: {state['error']}")

        return state

    def generate_sql(self, state: AgentState) -> AgentState:
        """
        STAGE 4: produce an untrusted SQL candidate using retrieved examples.

        Extraction decodes the structured envelope but never rewrites SQL.
        """
        logger.info("[STEP_4] Generating SQL (query_chars=%d)",
                    len(state.get("normalized_input") or ""))
        logger.info("\n" + "="*60)
        logger.info("⚙️ STEP 4: SQL GENERATION")
        logger.debug("="*60)

        # Build RAG context
        rag_section = ""
        if state.get("rag_context"):
            rag_section = f"""
REFERENCE EXAMPLES:
Use these similar examples from the knowledge base as guidance.
Adapt them to match the user's specific question.

{state['rag_context']}
"""
            logger.info(f"📚 RAG context included: {len(state['rag_context'])} chars")
        else:
            logger.warning("⚠️ No RAG context available")

        # Get conversation context if available
        conversation_context = state.get("conversation_context", "")
        context_section = ""
        if conversation_context:
            context_section = f"""
CONVERSATION CONTEXT:
{conversation_context}

Use this context to understand references to previous queries (e.g., "that person", "the same camera", etc.).
"""
        
        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=f"""You are an expert SQL query generator for PostgreSQL.

{state.get('schema_description', 'No schema available')}
{rag_section}
{context_section}

CRITICAL SECURITY RULES - READ-ONLY MODE:
=========================================
⚠️⚠️⚠️ ABSOLUTE PROHIBITION - NO EXCEPTIONS ⚠️⚠️⚠️

This SQL agent operates in STRICT READ-ONLY mode. You MUST follow these rules ABSOLUTELY:

ALLOWED OPERATIONS (ONLY - NO EXCEPTIONS):
- SELECT queries ONLY
- WITH clauses (Common Table Expressions) that contain ONLY SELECT statements
- EXPLAIN queries (for query analysis)

FORBIDDEN OPERATIONS (NEVER USE - EVEN IN CTEs, COMMENTS, OR SIMULATIONS):
- DELETE (NEVER, even in CTEs, comments, or "simulation" queries)
- UPDATE (NEVER, even in CTEs or comments)
- INSERT (NEVER, even in CTEs or comments)
- MERGE, UPSERT, REPLACE (NEVER)
- DROP, ALTER, CREATE, RENAME, TRUNCATE (NEVER)
- GRANT, REVOKE (NEVER)
- EXEC, EXECUTE, CALL (NEVER)
- COMMIT, ROLLBACK, BEGIN, START TRANSACTION (NEVER)
- COPY (NEVER)

CRITICAL INSTRUCTIONS:
1. If a user requests DELETE, UPDATE, INSERT, or any data modification, you MUST respond with:
   {{"sql": "", "purpose": "This system is read-only. Data modification operations (DELETE, UPDATE, INSERT) are not permitted. Please contact an administrator if you need to modify data."}}

2. DO NOT generate queries that simulate deletion, even if the user asks for it
3. DO NOT use DELETE, UPDATE, or INSERT keywords anywhere in your response, even in comments
4. DO NOT create CTEs that contain DELETE logic
5. If you see "delete", "update", "insert" in the user's request, immediately return empty SQL with an explanation

VIOLATION OF THESE RULES WILL RESULT IN IMMEDIATE USER BLOCKING AND ADMIN NOTIFICATION.

Query Generation Rules:
1. Generate READ-ONLY queries ONLY (SELECT, WITH, EXPLAIN)
2. NEVER use any DML (Data Manipulation Language) or DDL (Data Definition Language) operations
3. Use explicit column names (avoid SELECT *)
4. Add appropriate filtering, aggregation, and LIMIT clauses
5. Optimize for clarity and performance
6. Learn from the reference examples but adapt to the specific question
7. If the request cannot be fulfilled with the schema, explain why
8. LITERALS ARE COPIED, NEVER TRANSLATED. A person's name, a camera name or
   any other value you search for must appear in the SQL EXACTLY as the user
   wrote it — same script, same spelling. The data is stored as it was
   enrolled, so a translated or transliterated value matches nothing and the
   user is told their query has no records when it has many.
9. A request for the ANSWER in another language ("in Arabic", "بالعربية")
   changes only how the report is written afterwards. It never changes the
   query, the column names, or the values you filter on. Ignore it here.

Respond with ONLY a JSON object:
{{"sql": "YOUR SQL QUERY HERE", "purpose": "Brief explanation of what the query does"}}

If no query is possible:
{{"sql": "", "purpose": "Explanation of why no query can be generated"}}"""),
            HumanMessage(content=(
                "Generate SQL for the AUTHORITATIVE USER REQUEST:\n"
                f"{state['normalized_input']}\n"
                + (("\nPLANNER PARAPHRASE (interpretation aid only; the "
                    "authoritative request wins on any conflict):\n"
                    f"{state['sql_generation_input']}\n")
                   if state.get("sql_generation_input") else "")
                + "\nGenerate SQL that satisfies the authoritative request."
                + self._correction_hint(state)))
        ])

        self._trace_envelope("generate_sql", prompt)
        chain = prompt | self.sql_llm | StrOutputParser()

        try:
            logger.info("[STEP_4] 🤖 Calling LLM for SQL generation...")
            logger.info("🤖 Calling LLM for SQL generation...")
            result = chain.invoke({})
            logger.info(f"[STEP_4] ✅ LLM raw response received ({len(result)} chars)")
            logger.info("[STEP_4] LLM response received (chars=%d)",
                        len(str(result)))
            logger.debug("-"*40)

            # Use the prepare_sql_from_llm_response tool to parse and clean the response
            logger.info("🔧 Using prepare_sql_from_llm_response tool...")
            prepared = prepare_sql_from_llm_response.invoke(result)

            if prepared["success"]:
                # Generation produces an UNTRUSTED candidate. It neither
                # authorizes nor reformats it; every generation/modification
                # path converges on validate_and_fix_sql next.
                state["generated_sql"] = prepared["sql"]
                state["validated_sql"] = ""
                state["sql_purpose"] = prepared["purpose"]
                logger.info("[STEP_4] SQL candidate extracted (length=%d)",
                            len(state["generated_sql"]))
                logger.debug(f"[STEP_4] Transformations: {prepared['transformations']}")
                logger.info("[STEP_4] SQL candidate ready (sql_chars=%d purpose_chars=%d)",
                            len(state.get("generated_sql") or ""),
                            len(state.get("sql_purpose") or ""))
            else:
                state["generated_sql"] = ""
                state["sql_purpose"] = prepared["error"]
                logger.error(f"[STEP_4] SQL preparation failed: {prepared['error']}")
                logger.error(f"❌ Could not prepare SQL: {prepared['error']}")

        except Exception as e:
            state["generated_sql"] = ""
            if _is_timeout_error(e):
                # Distinguish "the model ran out of time" from "the model
                # produced unusable SQL". Both used to arrive downstream as the
                # bare "No SQL query to execute", which tells the user nothing
                # and reads like a bug rather than a capacity limit.
                state["sql_purpose"] = (
                    "The assistant took too long to build a query for this question. "
                    "Please try a simpler or more specific question."
                )
                state["sql_generation_timed_out"] = True
                logger.warning(
                    "[STEP_4] SQL generation timed out after the model budget (%s)",
                    type(e).__name__,
                )
            else:
                state["sql_purpose"] = f"SQL generation error: {str(e)}"
                logger.error(f"[STEP_4] SQL generation exception: {str(e)}", exc_info=True)
            logger.error("[STEP_4] SQL generation failed (reason_chars=%d)",
                         len(state.get("sql_purpose") or ""))

        return state

    def validate_and_fix_sql(self, state: AgentState) -> AgentState:
        """
        STAGE 5: parse, optionally repair, authorize, and canonicalize SQL.

        Only malformed SQL is offered to the repair model. Every successful
        path finishes at the same AST policy and stores its exact output.
        """
        logger.info("[STEP_4.5] Starting SQL validation")
        logger.info("\n" + "="*60)
        logger.info("🔍 STEP 4.5: SQL VALIDATION & FIXING")
        logger.debug("="*60)

        generated_sql = state.get("generated_sql", "")
        state["validated_sql"] = ""

        if not generated_sql:
            logger.warning("[STEP_4.5] No SQL to validate")
            logger.error("❌ No SQL to validate")
            state["sql_validation_status"] = "ERROR"
            state["sql_validation_error"] = "No SQL to validate"
            return state

        logger.debug(f"[STEP_4.5] Validating SQL ({len(generated_sql)} chars)")
        logger.info("[STEP_5] Validating SQL (chars=%d)", len(generated_sql))

        # Use the validate_sql_query tool
        validation_result = validate_sql_query.invoke(generated_sql)

        if validation_result["is_valid"]:
            # This is the ONE authorization/canonicalization seam. The AST
            # policy checks statement type, table/function allowlists and
            # complexity, then returns SQL with the enforced LIMIT.
            policy_result = self._validate_sql_policy(generated_sql)
            state["sql_validation_code"] = policy_result.get("code")
            if not policy_result["is_safe"]:
                state["sql_validation_status"] = "INVALID"
                state["sql_validation_error"] = policy_result["reason"]
                logger.warning("[STEP_4.5] SQL candidate denied (code=%s)",
                               policy_result.get("code", "POLICY_REJECTED"))
                return state

            canonical_sql = policy_result.get("sql") or generated_sql
            state["generated_sql"] = canonical_sql
            state["validated_sql"] = canonical_sql
            state["sql_validation_status"] = "VALID"
            state["sql_fixes_applied"] = []
            state["sql_validation_warnings"] = validation_result.get("warnings", [])
            state["sql_validation_error"] = None
            logger.info("[STEP_4.5] SQL authorized and canonicalized "
                        "(chars=%d)", len(canonical_sql))
            return state

        logger.warning(f"[STEP_4.5] Validation errors found: {validation_result['errors']}")
        logger.warning(f"⚠️ Validation errors found: {validation_result['errors']}")

        # Try to fix the SQL using LLM
        logger.info("\n🔧 Attempting to fix SQL with LLM...")

        fix_prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=f"""You are an expert PostgreSQL SQL debugger and fixer.

DATABASE SCHEMA:
{state.get('schema_description', 'No schema available')}

You received a SQL query that has potential issues. Your task is to:
1. Analyze the SQL query for errors
2. Fix ALL issues while maintaining the original intent
3. Ensure the query is syntactically correct for PostgreSQL
4. Ensure all column and table names match the schema
5. Add proper quotes/escaping where needed

VALIDATION ERRORS DETECTED:
{chr(10).join(f"- {e}" for e in validation_result['errors'])}

RULES:
- Return ONLY valid PostgreSQL SELECT/WITH/EXPLAIN queries
- Preserve the original query intent
- Fix syntax errors, missing keywords, incorrect escaping
- Ensure proper JOIN conditions
- CRITICAL: detections.pipeline_id (varchar) must join to pipelines.pipeline_id (varchar), NOT pipelines.id (integer)
- The correct join is: JOIN pipelines p ON d.pipeline_id = p.pipeline_id
- Ensure column names exist in the referenced tables
- Add missing aliases where needed
- Fix string concatenation issues (use proper escaping)

Respond with ONLY a JSON object:
{{"fixed_sql": "YOUR CORRECTED SQL QUERY", "fixes_applied": ["list", "of", "fixes"], "is_valid": true/false}}

If the query cannot be fixed:
{{"fixed_sql": "", "fixes_applied": [], "is_valid": false, "error": "explanation"}}"""),
            HumanMessage(content=f"""Fix this SQL query:

ORIGINAL SQL:
{generated_sql}

USER'S ORIGINAL REQUEST: {state.get('normalized_input', '')}

Provide the corrected SQL:""")
        ])

        chain = fix_prompt | self.sql_llm | StrOutputParser()

        try:
            logger.info("[STEP_5] 🤖 Calling LLM for SQL fix...")
            result = chain.invoke({})
            logger.info(f"[STEP_5] ✅ LLM fix response received ({len(result)} chars)")
            logger.info("[STEP_5] SQL-fix response received (chars=%d)",
                        len(str(result)))

            # Use prepare_sql_from_llm_response to parse the fix response
            prepared = prepare_sql_from_llm_response.invoke(result)

            if prepared["success"] and prepared["sql"]:
                fixed_sql = prepared["sql"]

                # Parse, then send the repaired candidate through the same
                # AST authorization seam as a first-attempt candidate.
                revalidation = validate_sql_query.invoke(fixed_sql)

                if revalidation["is_valid"]:
                    policy_result = self._validate_sql_policy(fixed_sql)
                    state["sql_validation_code"] = policy_result.get("code")
                    if policy_result["is_safe"]:
                        canonical_sql = policy_result.get("sql") or fixed_sql
                        state["generated_sql"] = canonical_sql
                        state["validated_sql"] = canonical_sql
                        state["sql_validation_status"] = "FIXED"
                        state["sql_fixes_applied"] = prepared["transformations"]
                        state["sql_validation_error"] = None
                        logger.info("[STEP_4.5] Repaired SQL authorized "
                                    "(chars=%d)", len(canonical_sql))
                    else:
                        state["sql_validation_status"] = "INVALID"
                        state["sql_validation_error"] = policy_result["reason"]
                        logger.warning("[STEP_4.5] Repaired SQL denied "
                                       "(code=%s)", policy_result.get("code"))
                else:
                    logger.warning("[STEP_4.5] Repaired SQL is still malformed")
                    state["sql_validation_status"] = "PARTIAL"
                    state["sql_validation_warnings"] = revalidation["errors"]
            else:
                logger.error(f"❌ Could not fix SQL: {prepared['error']}")
                state["sql_validation_status"] = "INVALID"
                state["sql_validation_error"] = prepared["error"]

        except Exception as e:
            logger.error(f"❌ Error during SQL fixing: {str(e)}")
            state["sql_validation_status"] = "ERROR"
            state["sql_validation_error"] = str(e)

        return state

    def prepare_sql_for_execution(self, state: AgentState) -> AgentState:
        """STAGE 5: assert that execution receives the authorized snapshot.

        No cleanup is permitted after authorization. A transformation here
        would make the checked SQL and the executed SQL different objects.
        """
        canonical = state.get("validated_sql") or ""
        if not canonical or canonical != (state.get("generated_sql") or ""):
            logger.error("[STAGE_5] Refusing SQL that is absent or changed "
                         "after authorization")
            state["generated_sql"] = ""
            state["query_result"] = {
                "success": False,
                "error": "SQL authorization state is missing or stale",
                "error_code": "STALE_SQL_AUTHORIZATION",
                "rows": [],
                "row_count": 0,
            }
            return state
        logger.info("[STAGE_5] Authorized SQL snapshot ready (chars=%d)",
                    len(canonical))
        return state

    def execute_sql(self, state: AgentState) -> AgentState:
        """
        STAGE 6: execute the exact canonical SQL authorized in stage 5.
        """
        logger.info("[STEP_5] Executing SQL query")
        logger.info("\n" + "="*60)
        logger.info("🚀 STEP 5: SQL EXECUTION")
        logger.debug("="*60)

        if not state.get("generated_sql"):
            # Carry forward WHY there is no SQL. A generation timeout surfaced
            # here as the bare "No SQL query to execute", which reads like an
            # internal fault instead of telling the user the assistant ran out
            # of time and that a simpler question may work.
            if state.get("sql_generation_timed_out"):
                reason = state.get("sql_purpose") or (
                    "The assistant took too long to build a query for this question."
                )
                logger.warning("[STEP_5] No SQL to execute — generation timed out")
            else:
                reason = "No SQL query to execute"
                logger.error("[STEP_5] No SQL query to execute")
            logger.error(f"❌ {reason}")
            state["query_result"] = {
                "success": False,
                "error": reason,
                "rows": [],
                "row_count": 0
            }
            return state

        logger.info("[STEP_5] Executing validated SQL (chars=%d)",
                    len(state.get("generated_sql") or ""))

        try:
            result = self.db.execute_query(state["generated_sql"])
            state["query_result"] = result
            if result.get("success"):
                row_count = result.get('row_count', 0)
                logger.info(f"[STEP_5] Query executed successfully - {row_count} rows returned")
                logger.info(f"✅ Query successful! Rows returned: {row_count}")
                if result.get("rows"):
                    logger.debug("[STEP_5] Result rows available (count=%d)",
                                 len(result["rows"]))
            else:
                error = result.get('error', 'Unknown error')
                logger.error(f"[STEP_5] Query execution failed: {error}")
                logger.error(f"❌ Query failed: {error}")
                
                # SECURITY LAYER 4: Mark user for blocking (handled in the API route).
                #
                # Enforce on the guard's CODE, never on its prose. Every AST
                # denial reason begins "Security: ", so the old substring test
                # treated a model that emitted `SELECT statement."}` — from the
                # user typing "hello" — exactly like `DELETE FROM users`:
                # CRITICAL audit entry, account marked for blocking, 403
                # (observed live 2026-08-30). A malformed query is a mistake to
                # correct; a forbidden operation is an attempt to refuse. Only
                # the second is a security event, and drowning it in false
                # positives is how real ones get missed.
                #
                # `is_enforceable` fails closed on an unknown code, and the
                # prose test below is retained for the regex gate (Layer 2),
                # which returns no code.
                error_code = result.get("error_code")
                if error_code:
                    enforce = sql_security.is_enforceable(error_code)
                    if not enforce:
                        logger.info(
                            "[SECURITY] Layer 4: denial code %s is not an "
                            "enforceable violation; treated as a correctable "
                            "query failure", error_code)
                else:
                    enforce = ("Security:" in error or "forbidden" in error.lower()
                               or "read-only" in error.lower())

                if enforce:
                    user_id = getattr(self.conversation_memory, 'user_id', None)
                    if user_id:
                        logger.error(f"[SECURITY] ⚠️ LAYER 4: Marking user ID {user_id} for blocking - Execution validation failed: {error}")
                        state["security_block_user"] = True
                        state["security_block_actor"] = "model"
                        state["security_reason_code"] = error_code or "FORBIDDEN_SQL_ATTEMPT"
                        state["security_block_reason"] = (
                            "SQL execution was rejected by the read-only policy.")
                    else:
                        logger.warning(f"[SECURITY] Execution validation failed but no user_id available: {error}")
        except Exception as e:
            logger.error(f"[STEP_5] SQL execution exception: {str(e)}", exc_info=True)
            state["query_result"] = {
                "success": False,
                "error": str(e),
                "rows": [],
                "row_count": 0
            }
            logger.error(f"❌ Exception: {str(e)}")

        return state


    def _recent_turn_texts(self, limit: int = 8):
        """Recent raw turn texts, for summary rebuilds. Never fatal."""
        try:
            messages = self.conversation_memory.get_recent_messages(limit=limit) or []
            return [str(getattr(m, "content", "") or "")[:300] for m in messages]
        except Exception:
            return []

    @staticmethod
    def _trace_envelope(node: str, prompt) -> None:
        """Sanitized per-call context-envelope trace, for development only.

        Proves what each model call actually RECEIVED — a memory object that
        is populated but never consumed is equivalent to no memory, and only
        the final prompt can settle that. Logs section PRESENCE and sizes,
        never the content: prompts hold surveillance data.

        Enabled by SQL_AGENT_TRACE_CONTEXT=1 (development flag; off is free).
        """
        if not settings.SQL_AGENT_TRACE_CONTEXT:
            return
        try:
            texts = [getattr(m, "content", "") or "" for m in prompt.messages]
            joined = "\n".join(str(t) for t in texts)
            sections = {
                "prior_turns": "[prior turns" in joined,
                "durable_memory": "[durable memory" in joined,
                "conversation_state": "Conversation state:" in joined,
                "schema": "Database schema" in joined or "DATABASE SCHEMA" in joined,
                "language_directive": "OUTPUT LANGUAGE" in joined,
            }
            logger.info("[CONTEXT_ENVELOPE] node=%s messages=%d chars=%d %s",
                        node, len(texts), len(joined),
                        " ".join(f"{k}={'Y' if v else 'n'}"
                                 for k, v in sections.items()))
        except Exception:
            pass

    @staticmethod
    def _context_section(state) -> str:
        """The prior-turns block for prompts, or "" when there is none.

        Memory was loaded and injected into state on every query, but only
        the SQL-generation prompt ever read it — so intent classification
        routed follow-ups like "and yesterday?" to the contextless CHAT
        branch, and every answer was written as if the conversation had just
        begun. One shared builder keeps the five prompts from drifting apart
        again.
        """
        context = (state.get("conversation_context") or "").strip()
        if not context:
            return ""
        return (
            "\n[prior turns - internal context for resolving references like "
            "\"that person\", \"the same camera\", \"and yesterday?\". Never "
            "quote, echo, or use this bracketed label in the answer]\n"
            + context + "\n[end of prior turns]\n")

    #: What each failure CATEGORY tells the user, per language. The key is a
    #: closed enum this codebase owns, so nothing from the database driver can
    #: reach a reply through here — there is no filter to get wrong.
    #:
    #: The first attempt at this used the Observation's `sanitized_detail`,
    #: which only flattens and clips: `column "cam" does not exist LINE 1:
    #: SELECT cam FROM detections` came through nearly whole. The detail stays
    #: in the log, where the operator who needs it can see it and the person
    #: the query is ABOUT cannot.
    _FAILURE_PHRASES = {
        "sql_execution_error_correctable": {
            "en": ("I could not build a query that the database would accept "
                   "for that. Could you rephrase it, or ask for one thing at "
                   "a time?"),
            "ar": ("\u0644\u0645 \u0623\u062a\u0645\u0643\u0646 \u0645\u0646 "
                   "\u0628\u0646\u0627\u0621 \u0627\u0633\u062a\u0639\u0644\u0627\u0645 "
                   "\u0635\u0627\u0644\u062d \u0644\u0647\u0630\u0627 \u0627\u0644\u0637\u0644\u0628. "
                   "\u0647\u0644 \u064a\u0645\u0643\u0646\u0643 \u0625\u0639\u0627\u062f\u0629 "
                   "\u0635\u064a\u0627\u063a\u062a\u0647\u061f"),
        },
        "sql_invalid": {
            "en": "I could not build a valid query for that. Could you rephrase it?",
            "ar": "\u0644\u0645 \u0623\u062a\u0645\u0643\u0646 \u0645\u0646 "
                  "\u0628\u0646\u0627\u0621 \u0627\u0633\u062a\u0639\u0644\u0627\u0645 "
                  "\u0635\u0627\u0644\u062d. \u0647\u0644 \u064a\u0645\u0643\u0646\u0643 "
                  "\u0625\u0639\u0627\u062f\u0629 \u0635\u064a\u0627\u063a\u0629 "
                  "\u0627\u0644\u0633\u0624\u0627\u0644\u061f",
        },
        "sql_generation_error": {
            "en": "I ran out of time building a query for that. A simpler "
                  "question may work.",
            "ar": "\u0627\u0633\u062a\u063a\u0631\u0642 \u0625\u0639\u062f\u0627\u062f "
                  "\u0627\u0644\u0627\u0633\u062a\u0639\u0644\u0627\u0645 "
                  "\u0648\u0642\u062a\u064b\u0627 \u0637\u0648\u064a\u0644\u064b\u0627. "
                  "\u062c\u0631\u0651\u0628 \u0633\u0624\u0627\u0644\u064b\u0627 "
                  "\u0623\u0628\u0633\u0637.",
        },
        "sql_forbidden": {
            "en": "That operation is not permitted — I can only read data.",
            "ar": "\u0647\u0630\u0627 \u0627\u0644\u0625\u062c\u0631\u0627\u0621 "
                  "\u063a\u064a\u0631 \u0645\u0633\u0645\u0648\u062d \u0628\u0647 "
                  "\u2014 \u064a\u0645\u0643\u0646\u0646\u064a "
                  "\u0642\u0631\u0627\u0621\u0629 \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a "
                  "\u0641\u0642\u0637.",
        },
        "sql_execution_error_transient": {
            "en": "The database was briefly unavailable. Please try again.",
            "ar": "\u0642\u0627\u0639\u062f\u0629 \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a "
                  "\u063a\u064a\u0631 \u0645\u062a\u0627\u062d\u0629 "
                  "\u0645\u0624\u0642\u062a\u064b\u0627. "
                  "\u064a\u0631\u062c\u0649 \u0627\u0644\u0645\u062d\u0627\u0648\u0644\u0629 "
                  "\u0645\u0631\u0629 \u0623\u062e\u0631\u0649.",
        },
        "sql_execution_error_permanent": {
            "en": "That question could not be answered from the data as it is "
                  "stored. Could you ask it differently?",
            "ar": "\u062a\u0639\u0630\u0631\u062a \u0627\u0644\u0625\u062c\u0627\u0628\u0629 "
                  "\u0639\u0646 \u0647\u0630\u0627 \u0627\u0644\u0633\u0624\u0627\u0644 "
                  "\u0645\u0646 \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a "
                  "\u0627\u0644\u0645\u062a\u0627\u062d\u0629. \u0647\u0644 "
                  "\u064a\u0645\u0643\u0646\u0643 \u0637\u0631\u062d\u0647 "
                  "\u0628\u0637\u0631\u064a\u0642\u0629 \u0623\u062e\u0631\u0649\u061f",
        },
        "artifact_missing": {
            "en": "I could not find that document any more.",
            "ar": "\u0644\u0645 \u0623\u0639\u062f \u0623\u062c\u062f "
                  "\u0630\u0644\u0643 \u0627\u0644\u0645\u0633\u062a\u0646\u062f.",
        },
        "artifact_forbidden": {
            "en": "That document is not available to you.",
            "ar": "\u0647\u0630\u0627 \u0627\u0644\u0645\u0633\u062a\u0646\u062f "
                  "\u063a\u064a\u0631 \u0645\u062a\u0627\u062d \u0644\u0643.",
        },
        "invariant_violation": {
            "en": "Something went wrong on my side, so I do not have a "
                  "reliable answer for you.",
            "ar": "\u062d\u062f\u062b \u062e\u0637\u0623 \u0644\u062f\u064a\u0651\u060c "
                  "\u0648\u0644\u0627 \u0623\u0645\u0644\u0643 "
                  "\u0625\u062c\u0627\u0628\u0629 \u0645\u0648\u062b\u0648\u0642\u0629.",
        },
    }

    _FAILURE_DEFAULT = {
        "en": "I could not complete that request.",
        "ar": "\u0644\u0645 \u0623\u062a\u0645\u0643\u0646 \u0645\u0646 "
              "\u0625\u062a\u0645\u0627\u0645 \u0647\u0630\u0627 "
              "\u0627\u0644\u0637\u0644\u0628.",
    }

    @classmethod
    def _failure_narration(cls, state: AgentState) -> str:
        """A failure the user can act on, with no copy of our schema in it.

        Driven by the Observation's error CATEGORY, which is a closed enum —
        so no database text can reach the reply through this path, whatever
        the driver said. The category is what tells the user whether to
        rephrase, retry, or stop, which is the actionable part; the detail
        goes to the log for the operator.
        """
        from .. import reasoning

        observation = state.get("observation") or {}
        error_type = observation.get("error_type")
        if not error_type:
            try:
                error_type = reasoning.build_observation(state).get("error_type")
            except Exception:
                error_type = None

        language = "ar" if (state.get("response_language") or "en") == "ar" else "en"
        phrases = cls._FAILURE_PHRASES.get(error_type) or cls._FAILURE_DEFAULT
        # A category with no phrase yet falls back rather than saying nothing.
        return phrases.get(language) or cls._FAILURE_DEFAULT[language]

    @staticmethod
    def _empty_narration(state: AgentState) -> str:
        """No rows is an ANSWER, and it is given in the language asked.

        Worded as a result rather than a failure: "how many detections
        yesterday" answered with none is correct. The case where zero rows is
        suspicious — a task narrowed to a named person — is caught earlier by
        the reasoning layer, which asks which person is meant.
        """
        # Zero rows for a NAMED person means one of two quite different
        # things, and the look-up has already settled which. Reporting both
        # as "no matching records" hides exactly the distinction the user
        # needs: whether the person is unknown to the system, or known and
        # simply never seen.
        arabic = (state.get("response_language") or "en") == "ar"

        known = state.get("entity_without_data")
        if known:
            if arabic:
                return (f"{known} مسجل في "
                        f"النظام، "
                        f"لكن لا توجد "
                        f"عمليات رصد "
                        f"مسجلة له.")
            return (f"{known} is enrolled, but has no detections recorded "
                    f"yet — no camera has seen them.")

        missing = state.get("entity_not_found")
        if missing:
            if arabic:
                return (f"لا يوجد "
                        f"شخص مسجل "
                        f"باسم «{missing}».")
            return (f"No person named “{missing}” is enrolled, so "
                    f"there is nothing to track.")

        if (state.get("response_language") or "en") == "ar":
            return ("\u0644\u0627 \u062a\u0648\u062c\u062f "
                    "\u0633\u062c\u0644\u0627\u062a "
                    "\u0645\u0637\u0627\u0628\u0642\u0629 "
                    "\u0644\u0647\u0630\u0627 \u0627\u0644\u0628\u062d\u062b.")
        return "I searched the database and found no matching records."

    @staticmethod
    def _grounding_section(state) -> str:
        """What THIS turn actually did, as facts the narrative may not invent.

        Built from the Observation — derived in Python from state — so the
        narration cannot be argued into claiming an action that never ran.
        The prior-turns block alone was enough to produce "I've deleted every
        detection row from the database" after a request that was refused
        (observed live 2026-08-30); this is the block that makes that
        impossible to say honestly.
        """
        from .. import reasoning

        facts = []
        observation = state.get("observation") or {}
        result = state.get("query_result") or {}

        if observation.get("error_type") == reasoning.ErrorType.SQL_FORBIDDEN:
            facts.append("The request was REFUSED by the security policy. "
                         "Nothing was executed and nothing was changed.")
        elif result.get("success"):
            facts.append(f"One read-only query ran and returned "
                         f"{result.get('row_count', 0)} rows.")
        elif observation.get("error_type"):
            facts.append("No query was completed for this message. "
                         f"Reason: {observation.get('sanitized_detail') or 'it failed'}.")
        else:
            facts.append("No database query was run for this message.")

        # Documents, explicitly. The block used to speak only about DATA, so
        # a turn that produced no document left the question open and the
        # model answered it from the transcript: "thanks" was told "I've
        # prepared the security intelligence report about Joey as a PDF" when
        # no artifact existed.
        if state.get("artifact_payload") or state.get("committed_artifact_id"):
            facts.append("A document WAS produced for this message.")
        else:
            facts.append("NO document was produced for this message. Do not "
                         "say you have prepared, generated or attached one, "
                         "and do not offer a download.")

        facts.append("No data was created, modified or deleted — this "
                     "assistant can only read.")

        return ("\n[FACTS about this turn - the ONLY actions you may describe "
                "as having happened]\n"
                + "\n".join(f"- {f}" for f in facts)
                + "\n[end of facts]\n\n")

    @staticmethod
    def _language_directive(state) -> str:
        """The output-language instruction for response prompts.

        Arabic reports keep the structure: Arabic headings and prose in Modern
        Standard Arabic, but person names, camera names, timestamps and numbers
        stay EXACTLY as they appear in the data — transliterating a camera name
        would break the operator's ability to find that camera.
        """
        if (state.get("response_language") or "en") != "ar":
            # Explicit for English too. With prior Arabic turns in the
            # conversation context and no directive, the model mimicked the
            # dominant context language — an English question after an Arabic
            # report came back in Arabic. The language follows THIS question,
            # not the transcript.
            return ("OUTPUT LANGUAGE: English (the user asked this question in "
                    "English; earlier turns in other languages do not change that).")
        return (
            "OUTPUT LANGUAGE: Write the ENTIRE report in Modern Standard Arabic "
            "(\u0627\u0644\u0641\u0635\u062d\u0649). Keep the same markdown section structure with Arabic "
            "headings. Person names, camera names, timestamps and numbers must "
            "remain EXACTLY as they appear in the data (do not translate or "
            "transliterate them). Confidence stays as percentages.")

    def enrich_co_appearance(self, state: AgentState) -> dict:
        """Who else was seen at the same camera around the tracked person.

        Deterministic, not LLM-driven: one bounded self-join per report,
        through the same AST-guarded read-only executor as every other query.
        The narrative node then RECEIVES facts; asking the model to find
        co-appearances would invite it to invent them.

        Never fails the run - a tracking report without co-appearances is
        degraded, not broken.
        """
        try:
            result = state.get("query_result") or {}
            rows = result.get("rows") or []
            if not rows:
                return {"co_appearances": []}
            first = rows[0]
            looks_tracking = (
                any(k in first for k in ("camera_name", "pipeline_id", "camera"))
                and any(k in first for k in ("timestamp", "time"))
                and any(k in first for k in ("name", "person_name"))
            )
            if not looks_tracking:
                return {"co_appearances": []}

            subject = str(first.get("name") or first.get("person_name") or "").strip()
            if not subject:
                return {"co_appearances": []}
            # The name came out of the database, but it still becomes a SQL
            # literal here - escape it, and refuse anything degenerate.
            if len(subject) > 120:
                return {"co_appearances": []}
            escaped = subject.replace("'", "''")

            sql = f"""SELECT COALESCE(p.location_name, p.pipeline_id) AS camera_name,
       f2.name AS person,
       d2.timestamp AS seen_at,
       d1.timestamp AS subject_seen_at
FROM faces f1
JOIN detections d1 ON f1.detection_id = d1.id
JOIN detections d2 ON d2.pipeline_id = d1.pipeline_id
JOIN faces f2 ON f2.detection_id = d2.id
JOIN pipelines p ON d1.pipeline_id = p.pipeline_id
WHERE LOWER(f1.name) = LOWER('{escaped}')
  AND f2.name IS NOT NULL
  AND LOWER(f2.name) != LOWER('{escaped}')
  AND LOWER(f2.name) NOT LIKE 'unknown%'
  AND LOWER(f2.name) NOT LIKE 'person_%'
  AND d2.timestamp BETWEEN d1.timestamp - INTERVAL '5 minutes'
                       AND d1.timestamp + INTERVAL '5 minutes'
ORDER BY d1.timestamp ASC
LIMIT 200"""
            outcome = self.db.execute_query(sql)
            if not outcome.get("success"):
                logger.warning("[CO_APPEAR] enrichment query refused/failed: %s",
                               outcome.get("error", "?"))
                return {"co_appearances": []}

            # Collapse bursts: one entry per (camera, person, subject-minute),
            # so ten frames of the same encounter read as one observation.
            seen = set()
            entries = []
            for row in outcome.get("rows") or []:
                subject_at = str(row.get("subject_seen_at") or "")[:16]  # to the minute
                key = (row.get("camera_name"), str(row.get("person") or "").lower(), subject_at)
                if key in seen:
                    continue
                seen.add(key)
                entries.append({
                    "camera_name": row.get("camera_name"),
                    "person": row.get("person"),
                    "seen_at": str(row.get("seen_at") or "")[:19],
                    "subject_seen_at": str(row.get("subject_seen_at") or "")[:19],
                })
                if len(entries) >= 25:
                    break
            logger.info("[CO_APPEAR] %d co-appearance observation(s) "
                        "(subject_chars=%d)", len(entries),
                        len(str(subject or "")))
            return {"co_appearances": entries}
        except (TypeError, AttributeError, NameError, KeyError, IndexError) as bug:
            # A CODE fault. This used to log only `type(e).__name__`, so a
            # broken enrichment silently returned nothing and the report was
            # quietly poorer with no way to find out why.
            logger.error("[CO_APPEAR] BUG in enrichment, returning none: %s",
                         bug, exc_info=True)
            return {"co_appearances": []}
        except Exception as e:
            logger.warning("[CO_APPEAR] enrichment skipped: %s", e)
            return {"co_appearances": []}

    def generate_story_response(self, state: AgentState) -> AgentState:
        """
        STEP 6: Humanized Story Output
        Transform results into a clear narrative.
        """
        logger.info("[STEP_6] Generating story response")
        logger.info("\n" + "="*60)
        logger.info("📖 STEP 6: STORY RESPONSE GENERATION")
        logger.debug("="*60)

        intent = state.get("intent", "CHAT")
        logger.debug(f"[STEP_6] Intent: {intent}")
        logger.info(f"📥 Intent: {intent}")

        if intent == "CHAT":
            # Pure chat response
            prompt = ChatPromptTemplate.from_messages([
                SystemMessage(content="""You are a helpful database assistant.
Answer the user's question in a friendly, professional manner.
Do not expose internal workings or SQL queries."""),
                HumanMessage(content=(
                    self._context_section(state)
                    + state["normalized_input"]
                    + (("\n\n" + self._language_directive(state))
                       if self._language_directive(state) else "")
                ))
            ])
        else:
            # SQL-based response
            query_result = state.get("query_result", {})

            if not query_result.get("success"):
                state["final_response"] = self._failure_narration(state)
                return state

            rows = query_result.get("rows", [])
            row_count = query_result.get("row_count", 0)

            if row_count == 0:
                state["final_response"] = self._empty_narration(state)
                return state

            # Check if this is a person tracking query (has camera_name/pipeline_id and timestamp)
            is_tracking_query = False
            if rows:
                first_row = rows[0]
                has_camera = any(key in first_row for key in ['camera_name', 'pipeline_id', 'camera'])
                has_timestamp = any(key in first_row for key in ['timestamp', 'time'])
                has_name = any(key in first_row for key in ['name', 'person_name'])
                
                if has_camera and has_timestamp and has_name:
                    is_tracking_query = True

            # Process and format results for LLM with actual data extraction
            # Extract key information from actual data
            processed_data = self._process_tracking_data(rows) if is_tracking_query else rows
            results_preview = json.dumps(processed_data[:100], indent=2, default=str)  # Increased limit

            if is_tracking_query:
                # Extract actual statistics from data
                actual_stats = self._extract_tracking_stats(rows)

                # Deterministic enrichment computed by enrich_co_appearance —
                # the model is told about co-appearances, never asked to find them.
                co_rows = state.get('co_appearances') or []
                language_directive = self._language_directive(state)
                co_appearance_block = (
                    json.dumps(co_rows, indent=2, default=str)
                    if co_rows else '(none found in the window)'
)
                
                # Natural Narrative Surveillance Intelligence Report prompt
                prompt = ChatPromptTemplate.from_messages([
                    SystemMessage(content="""You are a Security Intelligence Analyst in the SECURITY INTELLIGENCE SECTION, producing organized tracking reports from surveillance data.

CRITICAL DATA RULES (violating any of these makes the report worthless):
- Use ONLY the data provided in the Detection Results and Co-Appearance sections
- camera_name values are real, human-readable camera names (e.g. "Main Entrance", "Parking Lot") - use them EXACTLY as given, never invent one
- If a camera_name looks like a raw identifier (letters-and-digits with dashes), still use it verbatim and refer to it as an unnamed camera
- Never print a pipeline_id when a camera_name is present
- Use the person's actual name throughout - NEVER "subject" or "the subject"
- Timestamps: trim to seconds (13:04:26, not 13:04:26.625798)
- Confidence: always as a percentage with one decimal (62.6%)
- Never use placeholders like [X] or [location]; never invent statistics

REPORT STRUCTURE - follow it exactly, with these markdown headings:

# SECURITY INTELLIGENCE REPORT - <PERSON'S NAME IN CAPS>
*Prepared by: Security Intelligence Section*

## 1. Executive Summary
Three to four sentences: who was tracked, over what time window, across how
many cameras, and the single most significant finding.

## 2. Timeline of Movements
A chronological entry per detection, formatted as:
**HH:MM:SS - <camera name>** - one sentence: what was observed and the
confidence (e.g. "detected with a 62.6% confidence match").
When the Co-Appearance Observations list someone at the same camera within
the window of that detection, add on the next line:
> Also present within the same window: NAME (HH:MM:SS)
Group entries under a date subheading when the data spans multiple days.

## 3. Movement Analysis
The route as a short narrative: which camera to which camera, time between
them, dwell periods, and any gap worth noting.

## 4. Co-Appearance Analysis
Who else appeared alongside the tracked person, at which cameras, and how
often, drawn ONLY from the Co-Appearance Observations. Repeated
co-appearance with the same person deserves explicit mention as a possible
association - in careful language ("appeared alongside", "was present at
the same camera within N minutes"), never as an accusation. If the
observations list is empty, state in one sentence that no other identified
person was detected within the window and omit speculation.

## 5. Statistical Summary
A short bullet list: total detections, unique cameras, most frequent
camera (by name), first and last detection with dates, average confidence,
confidence range.

## 6. Assessment & Recommendations
Two short paragraphs: what the pattern indicates, and concrete next
surveillance steps.

STYLE: professional, precise, readable. Organized sections - not one wall
of prose, not repetitive sentence templates. Each timeline entry should
read differently; do not repeat the same sentence pattern for every
detection."""),
                    HumanMessage(content=f"""User asked: {state['normalized_input']}
{self._context_section(state)}
Query purpose: {state.get('sql_purpose', 'Track person movement')}

ACTUAL DETECTION RESULTS ({row_count} total detections):
{results_preview}

EXTRACTED STATISTICS FROM ACTUAL DATA:
{actual_stats}

CO-APPEARANCE OBSERVATIONS (other identified people at the same camera within +/-5 minutes of the tracked person; computed from the database, not to be invented or extended):
{co_appearance_block}

{language_directive}

Generate the SECURITY INTELLIGENCE REPORT following the exact structure from your instructions, using ONLY the data above.""")
                ])
            else:
                # Professional Surveillance Intelligence Report for general queries
                prompt = ChatPromptTemplate.from_messages([
                    SystemMessage(content="""You are a Security Intelligence Analyst generating professional surveillance reports from a Security Intelligence System. Your reports must be formal, precise, and intelligence-focused.

CRITICAL DATA USAGE RULES:
- You MUST use ONLY the actual data provided in the "Results" section
- You MUST extract all values EXACTLY as they appear in the data
- NEVER invent or create fake values, names, or statistics
- NEVER use placeholder values like [X], [Y], [number], [time range], [percentage]
- ALWAYS calculate real statistics from the actual data provided
- ALWAYS use exact field names and values from the data
- If a field is missing in the data, state "Data not available" rather than inventing values

REPORT FORMAT - Advanced Intelligence Briefing:
- Write as a formal intelligence report from a security system
- Use professional, authoritative language appropriate for security operations
- Structure as an advanced intelligence briefing with deep analysis
- Include all critical operational details with precise data
- Maintain objectivity and factual accuracy
- Present data in a clear, actionable format with quantitative analysis

REPORT STRUCTURE:
1. HEADER: "SURVEILLANCE INTELLIGENCE REPORT" or "INTELLIGENCE BRIEFING"
2. EXECUTIVE SUMMARY: Brief overview with actual statistics from data
3. KEY FINDINGS: Main data points with exact values from the data
4. DETAILED ANALYSIS: Intelligence analysis with quantitative metrics
5. STATISTICAL INSIGHTS: Calculated statistics and patterns from actual data
6. ASSESSMENT: Operational assessment with data-driven recommendations

LANGUAGE STYLE:
- Professional, formal, and authoritative
- Use advanced security/intelligence terminology
- Active voice, direct statements
- Precise references with exact data values
- Objective observations with quantitative analysis
- Data-driven conclusions with statistical backing
- Clear, concise, and actionable

TERMINOLOGY:
- "Detection events" for face detections
- "Observation points" or "Detection points" for cameras (use actual names from data)
- "Confidence levels" for recognition quality (convert similarity to percentage)
- "Operational metrics" for system statistics
- "Activity patterns" for behavioral trends
- Use formal time references with actual timestamps
- Present statistics clearly with exact numbers

CRITICAL RULES:
- ALWAYS use formal report structure
- ALWAYS extract and use EXACT values from the data provided
- ALWAYS calculate real statistics from the actual data (count rows, sum values, calculate averages, etc.)
- NEVER use placeholder values or invented data
- NEVER use generic examples - use the actual data provided
- ALWAYS use professional security terminology
- NEVER mention technical database terms (use descriptive names instead)
- ALWAYS maintain objectivity and factual reporting
- ALWAYS structure as an advanced intelligence briefing
- Include quantitative analysis and statistical insights from the actual data"""),
                    HumanMessage(content=f"""User asked: {state['normalized_input']}
{self._context_section(state)}
Query purpose: {state.get('sql_purpose', 'Data retrieval')}

ACTUAL RESULTS ({row_count} total rows):
{results_preview}

{self._language_directive(state)}

Generate a professional SURVEILLANCE INTELLIGENCE REPORT using ONLY the actual data provided above.
- Extract all values EXACTLY as they appear in the data
- Calculate real statistics from the actual data (count, sum, average, etc.)
- Do NOT invent or create fake values, names, or statistics
- Use exact field names and values from the data
- Present all findings with precise data points from the actual results
- Provide advanced analysis based on the actual patterns in the data""")
                ])

        self._trace_envelope("generate_story_response", prompt)
        chain = prompt | self.llm | StrOutputParser()

        try:
            logger.debug("[STEP_6] Calling LLM for story generation")
            
            # Check if we're in streaming mode (has streaming_callback)
            streaming_callback = state.get("streaming_callback")
            
            if streaming_callback:
                # Stream word-by-word
                logger.debug("[STEP_6] Using streaming mode for word-by-word generation")
                response_text = ""
                buffer = ""
                
                # Stream tokens from LLM
                for chunk in chain.stream({}):
                    if chunk:
                        buffer += chunk
                        # Split by spaces to get words, but keep punctuation with words
                        words = buffer.split(' ')
                        # If we have complete words (more than one word or buffer ends with space)
                        if len(words) > 1 or buffer.endswith(' '):
                            # Send all complete words except the last one (which might be incomplete)
                            for word in words[:-1]:
                                if word.strip():
                                    response_text += word + ' '
                                    # Call the callback with each word
                                    streaming_callback({"type": "content", "content": word + ' ', "step": "response"})
                            # Keep the last word in buffer (might be incomplete)
                            buffer = words[-1] if not buffer.endswith(' ') else ""
                
                # Send any remaining buffer
                if buffer.strip():
                    response_text += buffer
                    streaming_callback({"type": "content", "content": buffer, "step": "response"})
                
                response_text = response_text.strip()
            else:
                # Non-streaming mode (original behavior)
                logger.info("[STEP_6] 🤖 Calling LLM for story generation (non-streaming)...")
                response = chain.invoke({})
                logger.info(f"[STEP_6] ✅ LLM story response received ({len(response)} chars)")
                logger.info("[STEP_6] Story response received (chars=%d)",
                            len(str(response)))
                response_text = response.strip()
                
                # Even in non-streaming mode, send the response via callback if available
                # This ensures the frontend receives the content
                if streaming_callback:
                    logger.debug("[STEP_6] Sending non-streaming response via callback")
                    # Send response in chunks for better UX
                    chunk_size = 100
                    for i in range(0, len(response_text), chunk_size):
                        chunk = response_text[i:i+chunk_size]
                        streaming_callback({"type": "content", "content": chunk, "step": "response"})
            
            # Add name correction notice if applicable
            if state.get("name_correction_notice"):
                notice = state['name_correction_notice'] + '\n\n'
                if streaming_callback:
                    # Stream the notice word-by-word too
                    for word in notice.split(' '):
                        if word.strip():
                            streaming_callback({"type": "content", "content": word + ' ', "step": "response"})
                response_text = notice + response_text
            
            state["final_response"] = response_text
            logger.info(f"[STEP_6] Story response generated successfully ({len(state['final_response'])} chars)")
            logger.info(f"✅ Final response generated ({len(state['final_response'])} chars)")
        except Exception as e:
            logger.error(f"[STEP_6] Story generation failed: {str(e)}", exc_info=True)
            error_msg = f"I apologize, but I encountered an error while preparing your response: {str(e)}"
            state["final_response"] = error_msg
            if streaming_callback:
                streaming_callback({"type": "error", "message": error_msg, "step": "error"})
            logger.error(f"❌ Error generating response: {str(e)}")

        logger.info("[STEP_6] Final response ready (chars=%d)",
                    len(state.get("final_response") or ""))

        return state

    def _process_tracking_data(self, rows: list) -> list:
        """Process tracking data to ensure all fields are properly formatted."""
        processed = []
        for row in rows:
            processed_row = {}
            # Map common field names
            for key, value in row.items():
                # Normalize field names
                if key in ['pipeline_id', 'camera']:
                    processed_row['camera_name'] = value
                elif key in ['detection_time', 'time']:
                    processed_row['timestamp'] = value
                elif key in ['person_name', 'subject']:
                    processed_row['name'] = value
                else:
                    processed_row[key] = value
            processed.append(processed_row)
        return processed

    def _extract_tracking_stats(self, rows: list) -> dict:
        """Extract actual statistics from tracking data."""
        if not rows:
            return {}
        
        from datetime import datetime
        
        stats = {
            "total_detections": len(rows),
            "unique_cameras": set(),
            "unique_names": set(),
            "timestamps": [],
            "first_detection": None,
            "last_detection": None,
            "total_duration_seconds": None,
            "confidence_scores": []
        }
        
        for row in rows:
            # Extract camera name
            camera = row.get('camera_name') or row.get('pipeline_id') or row.get('camera')
            if camera:
                stats["unique_cameras"].add(str(camera))
            
            # Extract name
            name = row.get('name') or row.get('person_name')
            if name:
                stats["unique_names"].add(str(name))
            
            # Extract timestamp
            timestamp = row.get('timestamp') or row.get('detection_time') or row.get('time')
            if timestamp:
                try:
                    if isinstance(timestamp, str):
                        # Try parsing different timestamp formats
                        for fmt in ['%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S']:
                            try:
                                ts = datetime.strptime(timestamp, fmt)
                                stats["timestamps"].append(ts)
                                break
                            except:
                                continue
                    else:
                        stats["timestamps"].append(timestamp)
                except:
                    pass
            
            # Extract confidence/similarity
            confidence = row.get('similarity') or row.get('confidence')
            if confidence is not None:
                try:
                    stats["confidence_scores"].append(float(confidence))
                except:
                    pass
        
        # Calculate statistics
        stats["unique_cameras"] = list(stats["unique_cameras"])
        stats["unique_names"] = list(stats["unique_names"])
        stats["camera_count"] = len(stats["unique_cameras"])
        stats["name_count"] = len(stats["unique_names"])
        
        if stats["timestamps"]:
            stats["timestamps"].sort()
            stats["first_detection"] = stats["timestamps"][0]
            stats["last_detection"] = stats["timestamps"][-1]
            if len(stats["timestamps"]) > 1:
                duration = (stats["last_detection"] - stats["first_detection"]).total_seconds()
                stats["total_duration_seconds"] = duration
                stats["total_duration_minutes"] = duration / 60
                stats["total_duration_hours"] = duration / 3600
        
        if stats["confidence_scores"]:
            stats["avg_confidence"] = sum(stats["confidence_scores"]) / len(stats["confidence_scores"])
            stats["min_confidence"] = min(stats["confidence_scores"])
            stats["max_confidence"] = max(stats["confidence_scores"])
        
        # Format for display
        formatted_stats = {
            "Total Detections": stats["total_detections"],
            "Unique Detection Points": stats["camera_count"],
            "Detection Points": ", ".join(stats["unique_cameras"]) if stats["unique_cameras"] else "N/A",
            "Subjects Tracked": ", ".join(stats["unique_names"]) if stats["unique_names"] else "N/A",
        }
        
        if stats["first_detection"]:
            formatted_stats["First Detection"] = stats["first_detection"].strftime("%Y-%m-%d %H:%M:%S")
        if stats["last_detection"]:
            formatted_stats["Last Detection"] = stats["last_detection"].strftime("%Y-%m-%d %H:%M:%S")
        if stats["total_duration_seconds"]:
            hours = int(stats["total_duration_seconds"] // 3600)
            minutes = int((stats["total_duration_seconds"] % 3600) // 60)
            seconds = int(stats["total_duration_seconds"] % 60)
            formatted_stats["Total Tracking Duration"] = f"{hours}h {minutes}m {seconds}s"
        
        if stats.get("avg_confidence"):
            formatted_stats["Average Confidence"] = f"{stats['avg_confidence']*100:.1f}%"
            formatted_stats["Confidence Range"] = f"{stats['min_confidence']*100:.1f}% - {stats['max_confidence']*100:.1f}%"
        
        return formatted_stats

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    _CONFIRMATION = {
        "en": "I've prepared **{title}** as a {fmt}. You can download it below.",
        "ar": "لقد أعددت **{title}** بصيغة {fmt}. يمكنك تنزيله أدناه.",
    }

    def _document_source_text(self, state: AgentState) -> str:
        """What to put in the document.

        Preference order: the narrative just written for this user, then the
        previous answer in this conversation, then a plain summary of the last
        result. The narrative is read back from conversation memory rather
        than copied into working memory — it is already stored there once, and
        storing report text a second time would widen the surveillance data's
        footprint for no gain.
        """
        current = (state.get("final_response") or "").strip()
        if current:
            return current

        # Only reuse the previous narrative if the turn that wrote it actually
        # produced something. Length was the old test — over 40 characters —
        # which cannot tell a report from an apology, so an apology is what it
        # rendered: a PDF whose entire body was "I couldn't reach that report
        # to translate it", delivered as a finished report (observed live).
        working = state.get("working_context") or {}
        reportable = working.get("last_narrative_reportable")
        if reportable is False:
            logger.info("[RENDER] the last turn produced no result; not "
                        "rendering its narrative as a document")
        else:
            try:
                for message in reversed(
                        self.conversation_memory.get_recent_messages(limit=8) or []):
                    if message.__class__.__name__ == "AIMessage":
                        text = (getattr(message, "content", "") or "").strip()
                        if len(text) > 40:
                            return text
            except (TypeError, AttributeError, NameError, KeyError) as bug:
                # A CODE fault choosing what goes INTO a document. Loud.
                logger.error("[RENDER] BUG reading the previous narrative: %s",
                             bug, exc_info=True)
            except Exception as e:
                logger.warning("[RENDER] could not read the previous narrative: %s", e)

        result = (state.get("working_context") or {}).get("last_result") or {}
        if result.get("row_count"):
            columns = ", ".join(result.get("columns") or [])
            return (f"Query summary\n\nQuestion: {result.get('purpose') or 'n/a'}\n"
                    f"Rows returned: {result.get('row_count')}\n"
                    f"Columns: {columns or 'n/a'}")
        return ""

    #: The example text in the SQL-generation prompt. A model that echoes the
    #: template instead of describing its query put this on a real document:
    #: "I have prepared **Brief explanation of what the query does** as a PDF".
    #: Matched exactly against OUR OWN template string — not a guess about
    #: what a bad title looks like.
    _PROMPT_PLACEHOLDERS = frozenset({
        "brief explanation of what the query does",
        "your sql query here",
        "explanation of why no query can be generated",
    })

    @classmethod
    def _usable_title(cls, candidate) -> str:
        """A title, or "" when the candidate is prompt scaffolding.

        Truncation stops at a WORD boundary. A hard slice produced
        "...ordered chronologically for story gene" on a real document, which
        reads as a bug to whoever opens the file.
        """
        text = " ".join(str(candidate or "").split())
        if not text or text.lower() in cls._PROMPT_PLACEHOLDERS:
            return ""
        if len(text) <= 120:
            return text
        clipped = text[:120].rsplit(" ", 1)[0].rstrip(",;:-")
        return (clipped or text[:120]) + "..."

    def render_artifact(self, state: AgentState) -> AgentState:
        """STEP: render what we already have as a document.

        Renders BYTES only. Persisting them needs the database, and graph
        nodes are synchronous, so the API layer commits the artifact through
        the same `render_and_register` the HTTP export uses — one persistence
        path, not two.
        """
        plan = state.get("planned_action") or {}
        fmt = plan.get("format") or "pdf"
        language = plan.get("language") or state.get("response_language") or "en"
        content = self._document_source_text(state)

        if not content:
            state["final_response"] = (
                "I don't have anything to put in a document yet. "
                "What would you like me to report on?")
            logger.info("[RENDER] nothing to render; asked for a subject instead")
            return state

        # Name the document after what is IN it. `last_query` alone titled a
        # report with whatever the user last typed, which after a failed turn
        # is unrelated to the contents — hence a camera report delivered as
        # "i am just saying hi".
        working_context = state.get("working_context") or {}
        last_result = working_context.get("last_result") or {}
        # Title the document after the QUESTION THAT PRODUCED ITS CONTENTS,
        # which travels with the result itself.
        #
        # The two obvious candidates are both wrong. `last_query` is "the last
        # thing typed": after track Joey -> hi -> make that a PDF it titled a
        # surveillance report "hi". `sql_purpose` is written FOR the SQL
        # generator and reads like it ("Track all detections of a person named
        # Joey including which camera detected them, ordered chronologically
        # for story gene"). They stay as fallbacks for results recorded before
        # the question was stored.
        title = (self._usable_title(last_result.get("question"))
                 or self._usable_title(last_result.get("purpose"))
                 or self._usable_title(working_context.get("last_query"))
                 or "Intelligence Report")

        try:
            from ..services import export_builders

            class _Request:
                pass

            request = _Request()
            request.content = content
            request.title = title
            request.timestamp = ""
            safe_title, safe_content, safe_date = export_builders.sanitize_export(request)

            if fmt == "word":
                payload = export_builders.build_word_bytes(
                    safe_title, content, safe_date, "Agent")
            else:
                fmt = "pdf"
                payload = export_builders.build_pdf_bytes(
                    safe_title, safe_content, safe_date, "Agent")

            state["artifact_payload"] = {
                "bytes": payload,
                "type": fmt,
                "title": safe_title,
                "language": language,
                # Lineage for a later translation: the text the document was
                # rendered FROM, so translating never means parsing a PDF.
                "source_content": content,
                "source_sql": (state.get("generated_sql")
                               or ((state.get("working_context") or {})
                                   .get("last_result") or {}).get("sql")),
                "source_result_id": ((state.get("working_context") or {})
                                     .get("last_result") or {}).get("history_id"),
                # Only when this document is genuinely DERIVED from another.
                # Recording a parent that was merely the newest document
                # would put a relationship in the lineage that never existed.
                "parent_artifact_id": (plan.get("artifact_id")
                                       if plan.get("target") == "artifact" else None),
            }
            template = self._CONFIRMATION.get(language, self._CONFIRMATION["en"])
            state["final_response"] = template.format(
                title=safe_title, fmt=("Word document" if fmt == "word" else "PDF"))
            logger.info("[RENDER] rendered %s (%d bytes, lang=%s)",
                        fmt, len(payload), language)
        except Exception as e:
            logger.error("[RENDER] document rendering failed: %s", e, exc_info=True)
            state["final_response"] = (
                "I couldn't build that document. The report text is above — "
                "please try again, or ask for a different format.")
        return state

    def modify_sql(self, state: AgentState) -> AgentState:
        """STEP 4 (alternate): re-run the previous question under a new filter.

        PROVENANCE FIRST. When the user's reference resolved to a document
        ("same report but only camera 3"), the base query is THAT document's
        originating SQL — not whatever ran most recently. Those differ exactly
        when it matters: generate a report, run an unrelated query, then say
        "same report but camera 3". Binding to recency would silently modify
        the unrelated query and return a confident answer to a question nobody
        asked.

        The rewritten SQL then enters `validate_and_fix_sql` like any other,
        so it passes the same AST authorization guard. A "modification" that
        tries to smuggle a write is refused there, not here.
        """
        plan = state.get("planned_action") or {}
        modification = plan.get("modification") or state.get("normalized_input") or ""
        artifact_id = plan.get("artifact_id")
        last_result = (state.get("working_context") or {}).get("last_result") or {}

        base_sql, provenance = None, "none"
        if artifact_id:
            base_sql = (state.get("artifact_sql_index") or {}).get(str(artifact_id))
            if base_sql:
                provenance = f"artifact:{artifact_id}"
        if not base_sql:
            base_sql = last_result.get("sql")
            provenance = "last_result" if base_sql else "none"

        if not base_sql:
            state["error"] = "No previous query to modify"
            state["final_response"] = (
                "I don't have a previous query to adjust. "
                "Could you ask the full question?")
            state["generated_sql"] = ""
            logger.info("[MODIFY_SQL] nothing to modify")
            observability.observe_provenance("none")
            return state

        logger.info("[MODIFY_SQL] base query from %s; change_chars=%d",
                    provenance, len(str(modification)))
        state["sql_base_provenance"] = provenance
        observability.observe_provenance(provenance.split(":", 1)[0])

        # The SAME JSON envelope generate_sql asks for, because the SAME
        # parser consumes it. Asking for bare SQL here made every response
        # unparseable, and the fallback below then quietly re-ran the
        # unmodified query.
        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=(
                "You adjust an existing PostgreSQL SELECT query.\n\n"
                "Apply ONLY the change the user asks for. Keep every other "
                "part of the query — the same tables, joins, columns and "
                "ordering — so the result stays comparable to the original.\n"
                "It must remain a single read-only SELECT.\n\n"
                "Respond with ONLY a JSON object:\n"
                '{{"sql": "<the adjusted query>", "purpose": "<one line>"}}')),
            HumanMessage(content=(
                # The conversation context is included so a reference INSIDE
                # the delta ("only the person we discussed") can resolve.
                # Every other LLM node already received it; this one did not,
                # and a modification was interpreted with no memory at all.
                self._context_section(state)
                + f"Database schema:\n{state.get('schema_description', '')}\n\n"
                f"Existing query:\n{base_sql}\n\n"
                f"Requested change: {modification}\n\nJSON:"))
        ])

        try:
            self._trace_envelope("modify_sql", prompt)
            raw = (prompt | create_sql_llm(TaskType.SQL_MODIFICATION)
                   | StrOutputParser()).invoke({})
            # .invoke(), not a direct call: this is a StructuredTool.
            prepared = prepare_sql_from_llm_response.invoke(raw)
            if not prepared.get("success") or not prepared.get("sql"):
                raise ValueError(prepared.get("error") or "no SQL in the response")

            adjusted = prepared["sql"]
            # Still untrusted. The graph sends modifications through the same
            # validator as fresh generation; no branch gets private authority.
            state["generated_sql"] = adjusted
            state["validated_sql"] = ""
            state["sql_purpose"] = (prepared.get("purpose")
                                    or f"{last_result.get('purpose') or 'previous query'} "
                                       f"({str(modification)[:80]})")
            state["sql_was_modified"] = (adjusted.strip() != (base_sql or "").strip())
            logger.info("[MODIFY_SQL] adjusted SQL changed=%s chars=%d",
                        state["sql_was_modified"], len(adjusted))
        except Exception as e:
            logger.error("[MODIFY_SQL] failed: %s", e, exc_info=True)
            # Never execute the old query and present it as the requested
            # modification. A visible failure is more truthful than valid but
            # semantically stale data.
            state["generated_sql"] = ""
            state["validated_sql"] = ""
            state["sql_was_modified"] = False
            state["error"] = f"Could not apply the change: {e}"
        return state

    def translate_artifact(self, state: AgentState) -> AgentState:
        """STEP: restate an existing document in another language.

        This node DECIDES; the API layer EXECUTES. Translating needs the
        artifact's stored `source_content`, and reading that means an
        ownership-checked database call, which a synchronous graph node
        cannot make. Rather than let a node open its own event loop against a
        pool bound to another one, it publishes a request and the async layer
        resolves the id against the database, translates, re-renders and
        registers the result with its parent recorded.

        Translation works from `source_content` — the narrative the document
        was rendered from — so it is text in, text out. A PDF is never parsed
        back, which is both unreliable and how Arabic shaping gets destroyed.
        """
        plan = state.get("planned_action") or {}
        language = plan.get("language") or "en"
        artifact_id = plan.get("artifact_id")

        if not artifact_id:
            # Nothing rendered, but there IS a narrative: translate the text
            # itself and answer inline. No document, no artifact.
            source = self._document_source_text(state)
            if not source:
                state["final_response"] = (
                    "I don't have a previous report to translate. "
                    "What would you like me to report on?")
                return state
            state["final_response"] = translate_document_text(source, language)
            state["response_language"] = language
            logger.info("[TRANSLATE] translated the last narrative inline (lang=%s)",
                        language)
            return state

        state["translation_request"] = {"artifact_id": artifact_id, "language": language,
                                        "format": plan.get("format") or "pdf"}
        # Replaced by the API layer on success. If that fails, this is what the
        # user sees — it must not claim a document exists.
        state["final_response"] = (
            "I couldn't reach that report to translate it. "
            "Please try asking for it again.")
        logger.info("[TRANSLATE] requested translation of %s to %s",
                    artifact_id, language)
        return state

    #: Said when a real request ran out of bounded reasoning. Both languages
    #: because the failure must be as readable as the success - the older
    #: failure paths were hardcoded English regardless of the turn.
    _EXHAUSTED_NARRATION = {
        "ar": ("لم أتمكن من إكمال هذا الطلب ضمن خطوات المعالجة المسموح بها. "
               "يرجى إعادة صياغة السؤال أو تبسيطه."),
        "en": ("I could not complete this request within the allowed "
               "reasoning steps. Please try rephrasing it, or asking for "
               "one thing at a time."),
    }

    def handle_chat(self, state: AgentState) -> AgentState:
        """Handle pure chat responses without SQL."""
        if state.get("input_normalization_error"):
            # Boundary errors are deterministic and complete. Do not send an
            # invalid request to a model merely to paraphrase the validator.
            state["final_response"] = state["input_normalization_error"]
            return state

        # A look-up already SETTLED this turn: the person is enrolled with
        # nothing recorded, or is not enrolled at all. Both are facts, and
        # stating them is the whole point of having resolved the name -
        # handing them to the narration model instead produced answers that
        # contradicted the look-up. Deterministic, and one fewer model call.
        if state.get("entity_without_data") or state.get("entity_not_found"):
            state["final_response"] = self._empty_narration(state)
            logger.info("[REACT] terminal=%s answered from the look-up",
                        state.get("terminal_state"))
            return state

        logger.info("\n" + "="*60)
        logger.info("💬 CHAT RESPONSE (No SQL needed)")
        logger.debug("="*60)
        logger.info("[STEP_7] Chat input received (chars=%d)",
                    len(state.get("normalized_input") or ""))

        # A planned clarification is the answer, not a prompt for one. Passing
        # "make it Arabic" to the chat model here is precisely the failure
        # this redesign removes: it produces confident small talk about a
        # document the model cannot see.
        clarification = state.get("clarify_question")
        if clarification:
            state["final_response"] = clarification
            logger.info("[STEP_7] answering with the planned clarification")
            return state

        # A turn that asks for nothing gets no transcript. The prior-turns
        # block exists to resolve "it", "that one", "the same camera"; a
        # greeting has nothing to resolve, and handing it a surveillance
        # transcript is what produced "hi" -> "seems like you're referring to
        # the security intelligence report about Joey...".
        asks_for_something = state.get("turn_is_a_request")
        context_block = ("" if asks_for_something is False
                         else self._context_section(state))
        topic_rule = ("" if asks_for_something is not False else (
            "\n\nThe user is not asking about the data. Respond to what they "
            "actually said — briefly, and without summarising or referring to "
            "anything from earlier in the conversation."))

        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content="""You are a helpful database assistant.
Answer questions about databases, SQL, and data in a friendly manner.
If you don't know something, say so politely.

YOU CAN ONLY READ DATA. You have never created, changed or deleted a
record, and you never will — the system physically cannot. Never state or
imply that you performed, completed or will perform any such action, no
matter what an earlier message in the conversation asked for. If a previous
request was for a change or a deletion, say plainly that it was not carried
out because the assistant is read-only.

Never claim an action succeeded unless the FACTS block below says it did."""),
            HumanMessage(content=(
                context_block
                + self._grounding_section(state)
                + state["normalized_input"]
                + topic_rule
                + (("\n\n" + self._language_directive(state))
                   if self._language_directive(state) else "")
            ))
        ])

        self._trace_envelope("handle_chat", prompt)
        chain = prompt | self.llm | StrOutputParser()

        try:
            logger.info("[STEP_7] 🤖 Calling LLM for final response generation (chat mode)...")
            response = chain.invoke({})
            logger.info(f"[STEP_7] ✅ LLM final response received ({len(response)} chars)")
            logger.info("[STEP_7] Chat response received (chars=%d)",
                        len(str(response)))
            state["final_response"] = response.strip()
            logger.info(f"[STEP_7] ✅ Final response set in state ({len(state['final_response'])} chars)")
            logger.info(f"✅ Chat response generated")
        except Exception as e:
            state["final_response"] = f"I apologize, but I encountered an error: {str(e)}"
            logger.error(f"❌ Error: {str(e)}")

        logger.info("[STEP_7] Final response ready (chars=%d)",
                    len(state.get("final_response") or ""))

        return state

    def learn_from_query(self, state: AgentState) -> AgentState:
        """
        STEP 7: Learning
        If the query was successful, save it to the knowledge base for future reference.
        """
        logger.info("[STEP_7] Starting learning phase")
        logger.info("\n" + "="*60)
        logger.info("🧠 STEP 7: LEARNING")
        logger.debug("="*60)

        # Only learn from successful SQL queries
        query_result = state.get("query_result", {})
        generated_sql = state.get("generated_sql", "")

        query_success = query_result.get('success', False)
        has_sql = bool(generated_sql)
        should_learn = state.get('should_learn', True)
        
        logger.debug(f"[STEP_7] Query successful: {query_success}, Has SQL: {has_sql}, Should learn: {should_learn}")
        logger.info(f"📊 Query successful: {query_success}")
        logger.info(f"📊 Has SQL: {has_sql}")
        logger.info(f"📊 Should learn: {should_learn}")

        # Learn only from a query that actually FOUND something.
        #
        # `success` is True for a query that ran cleanly and matched nothing,
        # so this used to save empty-result queries as worked examples. One
        # of them — a person's name translated into Arabic, matching a table
        # that stores it in Latin script — became the top-ranked example for
        # every similar question and taught the agent to reproduce the bug.
        #
        # Zero rows remains a legitimate ANSWER (see reasoning.build_observation);
        # it is simply not a demonstration of a query that works, which is the
        # only thing this knowledge base is for.
        rows_found = int(query_result.get("row_count") or 0)
        if query_result.get("success") and generated_sql and not rows_found:
            logger.info("[STEP_7] not learning a query that matched nothing")

        if (
            query_result.get("success") and
            rows_found > 0 and
            generated_sql and
            state.get("should_learn", True)
        ):
            try:
                # Check if this is a novel query worth learning
                existing = self.kb.search_similar(
                    state["normalized_input"],
                    top_k=1,
                    user_id=state.get("user_id"),
                )

                # Only learn if no very similar example exists (similarity < 0.9)
                if not existing or existing[0].get("similarity", 0) < 0.9:
                    self.kb.learn_from_success(
                        question=state["normalized_input"],
                        sql=generated_sql,
                        purpose=state.get("sql_purpose", ""),
                        user_id=state.get("user_id"),
                    )
                    logger.info("[STEP_7] Learned new query pattern "
                                "(query_chars=%d)",
                                len(state.get("normalized_input") or ""))
                    logger.info("✅ Learned new query pattern!")
                else:
                    similarity = existing[0].get('similarity', 0)
                    logger.debug(f"[STEP_7] Skipped learning - similar example exists (similarity: {similarity})")
                    logger.info(f"⏭️ Skipped learning - similar example exists (similarity: {similarity})")
            except Exception as e:
                # Don't fail the response if learning fails
                logger.warning(f"[STEP_7] Learning failed: {str(e)}", exc_info=True)
                logger.warning(f"⚠️ Learning failed: {str(e)}")
        else:
            logger.info("⏭️ Skipped learning - conditions not met")

        return state
