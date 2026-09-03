"""PLAN → ACT → OBSERVE → REPLAN → ANSWER, bounded and deterministic.

The agent could already decide and act. What it could not do was look at the
RESULT of acting. A rejected query, an empty answer, an unresolved name — all
were narrated to the user as if they were the answer. This module is the
missing half of the turn: it turns an executed action into a bounded, factual
`Observation`, and decides — in Python — whether to answer, correct course,
or ask.

Three rules hold the whole design together:

  1. **Python classifies, the model never does.** Whether a failure is
     retryable, transient or terminal comes from a fixed taxonomy below, not
     from a model's opinion about its own output.
  2. **Bounded.** Separate budgets for reasoning re-plans and infrastructure
     retries, both small, both configurable, both checked here. There is no
     open-ended loop: the graph's routing function enforces the cap.
  3. **No chain-of-thought.** An `Observation` carries enums, counts and ids.
     Never rows, never SQL text, never narrative, never model prose.

Nothing here calls an LLM, touches the database, or writes a file. Every
function is pure and directly testable.
"""

import logging
import re
from typing import Any, Dict, List, Optional

#: Denial codes are classified in sql_guard, beside the `_deny` calls
#: that emit them, so a new code cannot drift away from its meaning.
from .security import sql_guard

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- modes

class ReasoningMode:
    """How much thinking this turn is allowed to buy.

    Chosen deterministically from the conversation's SHAPE — never by the
    model, and never from a keyword phrasebook.
    """

    FAST = "FAST"              # no state to reason about; answer directly
    CONTEXTUAL = "CONTEXTUAL"  # a follow-up: needs dialogue state
    MULTI_STEP = "MULTI_STEP"  # a compound request: needs a step plan

    ALL = (FAST, CONTEXTUAL, MULTI_STEP)


# ------------------------------------------------------------- taxonomy

class ErrorType:
    """Why an action did not produce a usable answer.

    Deliberately separate from the SECURITY path: malformed SQL is a
    correctable mistake, not an attack. Only `sql_forbidden` belongs to the
    AST guard, and it is never retryable.
    """

    SQL_INVALID = "sql_invalid"
    SQL_GENERATION_ERROR = "sql_generation_error"
    SQL_FORBIDDEN = "sql_forbidden"
    SQL_EXECUTION_ERROR_TRANSIENT = "sql_execution_error_transient"
    SQL_EXECUTION_ERROR_PERMANENT = "sql_execution_error_permanent"
    #: The database refused the QUERY - a type mismatch, an unknown column, a
    #: syntax error. Retrying the same SQL cannot help, but REWRITING it can,
    #: which is precisely what the re-plan path does.
    SQL_EXECUTION_ERROR_CORRECTABLE = "sql_execution_error_correctable"
    #: The caller has no cameras assigned, so the guard refused to run
    #: anything. Terminal for this turn; only an administrator changes it.
    SQL_OUT_OF_SCOPE = "sql_out_of_scope"
    EMPTY_RESULT = "empty_result"
    ENTITY_UNRESOLVED = "entity_unresolved"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_FORBIDDEN = "artifact_forbidden"
    INVARIANT_VIOLATION = "invariant_violation"


# What a re-plan may correct. Everything absent is terminal by default —
# failing closed means a new error class cannot silently become a retry loop.
_RETRYABLE_VIA_REPLAN = frozenset({
    ErrorType.SQL_EXECUTION_ERROR_CORRECTABLE,
    ErrorType.SQL_INVALID,
    ErrorType.SQL_GENERATION_ERROR,
    ErrorType.ENTITY_UNRESOLVED,
    ErrorType.ARTIFACT_MISSING,
})

# Retried by re-running the SAME SQL, on the INFRASTRUCTURE budget. A dropped
# connection is not a reasoning failure, and must not spend the model's
# re-plan budget: a brief database hiccup should not lobotomize the turn.
_RETRYABLE_VIA_EXECUTION = frozenset({
    ErrorType.SQL_EXECUTION_ERROR_TRANSIENT,
})

# Substrings that identify a TRANSPORT failure rather than a bad query.
# Matched against the driver's error text, which is machine output, not model
# output. Anything unmatched is treated as PERMANENT — the safe default,
# since retrying a deterministic error just burns the budget twice.
_TRANSIENT_SIGNS = (
    "connection reset", "connection refused", "connection closed",
    "server closed the connection", "terminating connection",
    "timeout expired", "pool timeout", "could not connect",
    "temporarily unavailable", "too many clients", "deadlock detected",
    "operationalerror", "interfaceerror", "cannot acquire connection",
)

#: The database complaining about the QUERY. Postgres is specific about
#: these, and every one names a mistake a regeneration can fix - given the
#: error text as a hint, which is what the correction hint carries.
#:
#: Matched against the DRIVER's text, which is machine output. Anything
#: unmatched still falls through to PERMANENT: guessing that an unknown
#: failure is correctable spends a re-plan on a rewrite that cannot help.
_CORRECTABLE_SIGNS = (
    "operator does not exist", "does not exist", "syntax error",
    "invalid input syntax", "is ambiguous", "cannot be matched",
    "must appear in the group by", "type mismatch", "cannot cast",
    # "function date_trunc(unknown) is not unique" - Postgres cannot
    # pick an overload, which an explicit cast resolves.
    "is not unique", "could not choose", "no function matches",
    "undefinedcolumn", "undefinedfunction", "undefinedtable",
)

_FORBIDDEN_SIGNS = ("security:", "forbidden", "read-only", "not allowed")

_SANITIZED_DETAIL_CHARS = 200

# Actions whose success is only real if a reference came back with it. A tool
# that reports success without one is BUGGY, and the reasoning layer must
# catch that rather than narrate a document that does not exist.
_REQUIRES_ARTIFACT = frozenset({"generate_document", "translate_artifact"})
_REQUIRES_RESULT = frozenset({"query_database", "modify_previous_query"})


# ----------------------------------------------------------- observation

def _sanitize(text: Any) -> Optional[str]:
    """A BOUNDED reason string: one line, at most 200 characters.

    That is all it does, and the name oversells it. It flattens newlines and
    clips — it does NOT remove table names, column names or SQL fragments,
    because a driver error is exactly where an operator needs those to
    diagnose anything.

    So this is safe for a LOG and for the model's own context. It is NOT safe
    for a user-visible reply: `column "cam" does not exist LINE 1: SELECT cam
    FROM detections` survives it nearly whole. Anything shown to a user is
    built from the error CATEGORY instead — see `_FAILURE_PHRASES` in
    agent_tools — because an enum cannot leak.

    An earlier docstring here claimed "never SQL, never rows". It was wrong,
    and a user-facing message was written on the strength of it.
    """
    if not text:
        return None
    flat = " ".join(str(text).split())
    return flat[:_SANITIZED_DETAIL_CHARS] or None


def classify_execution_error(error_text: Any) -> str:
    """Transient vs permanent, decided from the DRIVER's words, not a model.

    Fails closed to PERMANENT: retrying a deterministic error (a bad column,
    an unsupported function) can never succeed, and pretending otherwise
    spends the budget for nothing.
    """
    lowered = str(error_text or "").lower()
    # TRANSIENT first: a dropped connection can carry text that also looks
    # like a complaint about the query, and re-running it unchanged is both
    # cheaper and more likely to work than a rewrite.
    if any(sign in lowered for sign in _TRANSIENT_SIGNS):
        return ErrorType.SQL_EXECUTION_ERROR_TRANSIENT
    if any(sign in lowered for sign in _CORRECTABLE_SIGNS):
        return ErrorType.SQL_EXECUTION_ERROR_CORRECTABLE
    return ErrorType.SQL_EXECUTION_ERROR_PERMANENT


#: Columns in THIS schema that hold a person's name. Ours, closed, and
#: nothing to do with how a user phrases anything.
_NAME_COLUMNS = frozenset({"name", "display_name", "person_name"})

#: The columns a query filters on when the user named a CAMERA. A zero-row
#: result narrowed to one of these deserves the same second look a person's
#: name gets: the camera may be misspelled, or not exist at all.
_CAMERA_COLUMNS = frozenset({"location_name", "pipeline_id"})
_UUID_SHAPE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _filtered_literals(sql: Optional[str], columns: frozenset) -> list:
    """String literals the query compared against one of `columns`.

    Same walk as filtered_names, parameterised by column set. Never raises:
    unparseable SQL names nothing.
    """
    if not sql:
        return []
    try:
        import sqlglot
        from sqlglot import expressions as exp

        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return []

    found: list = []
    try:
        for node in tree.walk():
            if not isinstance(node, (exp.EQ, exp.Like, exp.ILike, exp.In,
                                     exp.NEQ)):
                continue
            matched = [c for c in node.find_all(exp.Column)
                       if c.name.lower() in columns]
            if not matched:
                continue
            literals = [lit for lit in node.find_all(exp.Literal) if lit.is_string]
            # The caller's camera SCOPE is an IN-list of pipeline ids that
            # the guard adds; it is not something the user asked for. An
            # IN-list of several ids, or a UUID-shaped id, is the scope.
            if any(c.name.lower() == "pipeline_id" for c in matched) and (
                    (isinstance(node, exp.In) and len(literals) > 1)
                    or any(_UUID_SHAPE.match(str(lit.this)) for lit in literals)):
                continue
            for literal in literals:
                value = str(literal.this).strip("%").strip()
                if value and value not in found:
                    found.append(value)
    except Exception:
        return found
    return found


def filtered_cameras(sql: Optional[str]) -> list:
    """String literals the query compared against a CAMERA column."""
    return _filtered_literals(sql, _CAMERA_COLUMNS)


def filtered_names(sql: Optional[str]) -> list:
    """String literals the query compared against a NAME column.

    The query records what it looked for, so the entity comes from the parsed
    statement rather than from guessing at the user's words. "How many
    detections yesterday?" filters on no name and yields nothing here, which
    is what keeps a correct answer of zero from being treated as a failure.

    Never raises: unparseable SQL simply names nobody.
    """
    if not sql:
        return []
    try:
        import sqlglot
        from sqlglot import expressions as exp

        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return []

    found = []
    try:
        for node in tree.walk():
            if not isinstance(node, (exp.EQ, exp.Like, exp.ILike, exp.In,
                                     exp.NEQ)):
                continue
            # The column may be WRAPPED - LOWER(name), name::text,
            # TRIM(f.name) - so look for a name column anywhere inside the
            # predicate rather than only as a direct operand. Matching just
            # the bare form missed most of what the generator actually emits.
            columns = [c for c in node.find_all(exp.Column)
                       if c.name.lower() in _NAME_COLUMNS]
            if not columns:
                continue
            for literal in node.find_all(exp.Literal):
                if not literal.is_string:
                    continue
                value = str(literal.this).strip("%").strip()
                # A wildcard-only or empty filter names nobody.
                if value and value not in found:
                    found.append(value)
    except Exception:
        return found
    return found
    return found


def would_rerun_help(asked: str, stored: str) -> bool:
    """Would running the query again with the STORED spelling change anything?

    A filter of `ILIKE '%ali%'` against a stored "ali abbass" would already
    have matched had any row existed, so zero rows is a fact about the data -
    the person has no detections - and re-running it is waste. "Jeoy" against
    "JOEY" would NOT have matched, so it is worth another attempt.
    """
    # Compared the way the DATABASE compares, not the way our fuzzy matcher
    # does. ILIKE is case-insensitive and nothing more - it does no Arabic
    # folding - so a filter of '%على%' genuinely misses a stored
    # 'علي' even though both are the same name to us. Using the folded
    # key here would conclude the query had already matched, and answer "no
    # detections" about somebody who has them.
    a = " ".join((asked or "").split()).casefold()
    b = " ".join((stored or "").split()).casefold()
    if not a or not b:
        return False
    return a not in b


def _expected_data(state: dict) -> bool:
    """Did the active task imply rows SHOULD have come back?

    Zero rows is frequently the correct business answer — "how many
    detections yesterday?" answered with 0 is right, and re-planning it would
    be both wasteful and wrong. It only warrants a second look when the task
    was narrowed to a specific entity, because that is the case where the
    narrowing itself (a misspelled name, an unresolved reference) is the
    likelier culprit.
    """
    dialogue = (state.get("working_context") or {}).get("dialogue_state") or {}
    fields = dialogue.get("fields") or {}
    if any(fields.get(name, {}).get("value") for name in
           ("referenced_entity", "selected_entity")):
        return True
    candidates = state.get("planner_candidates") or {}
    if candidates.get("unresolved_references"):
        return True
    # The SQL itself filtered on a person's name, or on a camera's.
    if filtered_names(state.get("generated_sql")):
        return True
    if filtered_cameras(state.get("generated_sql")):
        return True
    # A name correction happened this turn: the query was about a person.
    return bool(state.get("name_corrections"))


def build_observation(state: dict) -> Dict[str, Any]:
    """The bounded, factual account of what the executed action produced.

    Everything here is derived in Python from AgentState. The model does not
    contribute a single field, which is what makes `decide_next` trustworthy.
    """
    plan = state.get("planned_action") or {}
    action = plan.get("action") or state.get("intent") or "unknown"
    result = state.get("query_result") or {}
    validation = state.get("sql_validation_status")

    observation: Dict[str, Any] = {
        "action": action,
        "success": False,
        "error_type": None,
        "validation_status": validation,
        "row_count": None,
        "result_id": None,
        "artifact_id": None,
        "unresolved_entity": None,
        "retryable": False,
        "prerequisite": None,
        "sanitized_detail": None,
    }

    # --- security first: never reclassify a genuine REFUSAL as correctable.
    #
    # But a PARSE failure is not a refusal. The AST guard reports both through
    # the same flag, and treating them alike is how a greeting that produced
    # malformed SQL reached a user as "Attempted forbidden SQL operation" with
    # a CRITICAL security audit line and a 403 (observed live 2026-08-30).
    # A model writing broken SQL is a mistake to correct; a model asking for
    # DELETE is an attempt to refuse. Only the latter is a security event.
    if state.get("security_block_user"):
        code = str(state.get("security_reason_code") or "")
        reason = str(state.get("security_block_reason") or "")
        # Lead with the code; the prose test stays only as a net for any path
        # that blocks without recording one.
        if sql_guard.is_malformed(code) or "could not be parsed" in reason:
            observation["error_type"] = ErrorType.SQL_INVALID
            observation["retryable"] = True
            observation["sanitized_detail"] = "the generated query was malformed"
        else:
            observation["error_type"] = ErrorType.SQL_FORBIDDEN
            observation["sanitized_detail"] = _sanitize(
                code or "forbidden operation")
        return observation

    # --- SQL validation refused the query: it must not have been executed
    if validation in ("INVALID", "ERROR", "PARTIAL"):
        timed_out = bool(state.get("sql_generation_timed_out"))
        observation["error_type"] = (ErrorType.SQL_GENERATION_ERROR if timed_out
                                     else ErrorType.SQL_INVALID)
        observation["retryable"] = True
        observation["sanitized_detail"] = _sanitize(
            state.get("sql_validation_error")
            or (state.get("sql_validation_warnings") or [None])[0]
            or "the generated query did not validate")
        return observation

    # --- document actions
    if action in _REQUIRES_ARTIFACT:
        payload = state.get("artifact_payload") or {}
        request = state.get("translation_request") or {}
        artifact_id = (state.get("committed_artifact_id")
                       or request.get("artifact_id"))
        produced = bool(payload.get("bytes")) or bool(artifact_id)
        if not produced:
            missing = bool(plan.get("artifact_id")) or bool(request)
            observation["error_type"] = (ErrorType.ARTIFACT_MISSING if missing
                                         else ErrorType.INVARIANT_VIOLATION)
            observation["retryable"] = missing
            observation["prerequisite"] = (None if missing
                                           else "a document to work from")
            observation["sanitized_detail"] = _sanitize(
                state.get("error") or "no document was produced")
            return observation
        observation["success"] = True
        observation["artifact_id"] = artifact_id
        return observation

    # --- conversational actions have no result contract
    if action in ("chat", "clarify"):
        observation["success"] = True
        return observation

    # --- SQL actions
    if not result:
        observation["error_type"] = ErrorType.INVARIANT_VIOLATION
        observation["sanitized_detail"] = "the query produced no result object"
        return observation

    if not result.get("success"):
        error_text = result.get("error") or ""
        code = result.get("error_code")
        if code:
            # The AST guard classified this itself. Trust the code: its reason
            # text always starts "Security: ", so matching prose here reported
            # a model's malformed SQL as a forbidden operation and made it
            # permanently un-correctable.
            if sql_guard.is_malformed(code):
                observation["error_type"] = ErrorType.SQL_INVALID
            elif code in sql_guard.INFRASTRUCTURE_CODES:
                observation["error_type"] = (
                    ErrorType.SQL_EXECUTION_ERROR_PERMANENT)
            elif code in sql_guard.AUTHORIZATION_CODES:
                # No cameras assigned. Not forbidden, not broken: nothing to
                # read. Saying "not permitted - I can only read data" here
                # would be untrue and would carry the block warning.
                observation["error_type"] = ErrorType.SQL_OUT_OF_SCOPE
            else:
                observation["error_type"] = ErrorType.SQL_FORBIDDEN
        elif any(sign in str(error_text).lower() for sign in _FORBIDDEN_SIGNS):
            observation["error_type"] = ErrorType.SQL_FORBIDDEN
        else:
            observation["error_type"] = classify_execution_error(error_text)
        observation["retryable"] = (
            observation["error_type"] in _RETRYABLE_VIA_REPLAN)
        observation["sanitized_detail"] = _sanitize(error_text)
        return observation

    row_count = int(result.get("row_count") or 0)
    observation["row_count"] = row_count
    observation["resolution_attempted"] = bool(
        state.get("entity_resolution_attempted"))
    observation["result_id"] = state.get("query_history_id")

    if row_count == 0:
        # Zero rows is a legitimate answer unless the task was narrowed to an
        # entity — see _expected_data. The MODEL never gets to decide this.
        if _expected_data(state):
            observation["error_type"] = ErrorType.EMPTY_RESULT
            observation["retryable"] = True
            # The name the QUERY used, preferred over the remembered
            # subject: on a fresh turn there is no remembered subject, which
            # is exactly the case that used to fall through unhandled.
            names = filtered_names(state.get("generated_sql"))
            cameras = filtered_cameras(state.get("generated_sql"))
            if names or not cameras:
                observation["unresolved_entity"] = (
                    names[0] if names else _active_entity(state))
                observation["unresolved_kind"] = "person"
            else:
                # The query narrowed to a CAMERA and found nothing. "No
                # matching records" for a camera that does not exist is
                # true and useless; the look-up says which it is.
                observation["unresolved_entity"] = cameras[0]
                observation["unresolved_kind"] = "camera"
            observation["sanitized_detail"] = (
                "no rows for the entity the task was narrowed to")
            return observation
        observation["success"] = True     # 0 is the answer
        return observation

    observation["success"] = True
    return observation


def _active_entity(state: dict) -> Optional[str]:
    dialogue = (state.get("working_context") or {}).get("dialogue_state") or {}
    entry = (dialogue.get("fields") or {}).get("referenced_entity") or {}
    value = entry.get("value")
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value) if value else None


def check_invariants(observation: Dict[str, Any]) -> Dict[str, Any]:
    """Refuse a 'success' that contradicts the action's contract.

    A tool claiming it generated a document while returning no artifact id is
    BUGGY, not successful. Narrating that to the user is how a system tells
    somebody their report is ready when it does not exist. This defends
    against our own executors, not just against the model.
    """
    if not observation.get("success"):
        return observation

    action = observation.get("action")
    if action in _REQUIRES_ARTIFACT and not observation.get("artifact_id"):
        violated = "reported success without a registered artifact"
    elif (action in _REQUIRES_RESULT
          and observation.get("row_count") is None):
        violated = "reported success without a result"
    else:
        return observation

    logger.error("[REASONING] invariant violation: %s %s", action, violated)
    return {**observation,
            "success": False,
            "error_type": ErrorType.INVARIANT_VIOLATION,
            "retryable": False,
            "sanitized_detail": violated}


# -------------------------------------------------------------- decision

#: How a turn ended. Recorded on the state and logged, so "what happened?"
#: has an answer that does not depend on reading the whole trace.
FINAL = "FINAL"                    # answered from real results
CLARIFY = "CLARIFY"                # asked the user something
NOT_FOUND = "NOT_FOUND"            # looked, and there is nothing
BLOCKED = "BLOCKED"                # refused by a guard
MAX_ITERATIONS = "MAX_ITERATIONS"  # bounded reasoning ran out
ERROR = "ERROR"                    # something broke

TERMINAL_STATES = (FINAL, CLARIFY, NOT_FOUND, BLOCKED, MAX_ITERATIONS, ERROR)

ANSWER = "ANSWER"
REPLAN = "REPLAN"
CLARIFY = "CLARIFY"
RETRY_EXECUTION = "RETRY_EXECUTION"
#: Look the person up, now that an empty result says the filter was wrong or
#: the person has nothing recorded. Carried out in Python - no model call.
RESOLVE_ENTITY = "RESOLVE_ENTITY"


def decide_next(observation: Dict[str, Any], *, mode: str = ReasoningMode.CONTEXTUAL,
                replan_count: int = 0, execution_retries: int = 0,
                max_replans: int = 2, max_execution_retries: int = 1
                ) -> Dict[str, Any]:
    """What happens after an action. Pure, and the only thing that decides.

    Returns {"decision", "reason", "error_type"}. A REPLAN always carries a
    reason drawn from the Observation — there is no "just try again" path,
    which is what keeps a bounded corrector from becoming a retry loop.
    """
    observation = check_invariants(observation)
    error_type = observation.get("error_type")

    if observation.get("success"):
        return {"decision": ANSWER, "reason": None, "error_type": None}

    # Infrastructure retry: same SQL, separate budget. Not reasoning.
    if error_type in _RETRYABLE_VIA_EXECUTION:
        if execution_retries < max_execution_retries:
            return {"decision": RETRY_EXECUTION,
                    "reason": "the database connection failed transiently",
                    "error_type": error_type}
        return {"decision": ANSWER,
                "reason": "the database was unavailable",
                "error_type": error_type}

    if error_type == ErrorType.ENTITY_UNRESOLVED:
        # Asking beats guessing at a person's identity.
        return {"decision": CLARIFY,
                "reason": "the person referred to could not be identified",
                "error_type": error_type}

    if error_type == ErrorType.EMPTY_RESULT and observation.get("retryable"):
        # Resolve the name BEFORE asking anything. Zero rows for a person
        # means three different things - the wrong spelling, no such
        # person, or a real person with nothing recorded - and only a
        # look-up separates them. Once per turn: the flag is what stops an
        # empty re-query resolving the same name forever.
        if (observation.get("unresolved_entity")
                and not observation.get("resolution_attempted")):
            return {"decision": RESOLVE_ENTITY,
                    "reason": "the query returned nothing for a named person",
                    "error_type": error_type}
        # A CAMERA was already resolved against the real list and the
        # query re-run with its stored name. Nothing matched: that is an
        # answer about the data, not a doubt about which camera is meant.
        if observation.get("unresolved_kind") == "camera":
            return {"decision": ANSWER,
                    "reason": "no records matched the camera named",
                    "error_type": error_type}
        # The query ran and the database answered honestly: nothing matched
        # the person this task was narrowed to. The likeliest cause is the
        # NAME, not the data — so ask which person is meant rather than
        # re-planning around an inconvenient answer, and rather than
        # reporting "not seen" about somebody who may not be enrolled under
        # that spelling. `retryable` here already required a committed
        # entity (see _expected_data), never model prose.
        return {"decision": CLARIFY,
                "reason": "no records matched the person named",
                "error_type": error_type}

    if error_type in _RETRYABLE_VIA_REPLAN and observation.get("retryable"):
        if replan_count < max_replans:
            return {"decision": REPLAN,
                    "reason": observation.get("sanitized_detail")
                              or f"the previous action failed ({error_type})",
                    "error_type": error_type}
        # Budget spent: answer honestly. Never "carry on anyway".
        return {"decision": ANSWER,
                "reason": "the request could not be completed",
                "error_type": error_type}

    return {"decision": ANSWER,
            "reason": observation.get("sanitized_detail"),
            "error_type": error_type}


# ------------------------------------------------------------ mode choice

# A follow-up is short and refers to something. These are SIGNALS, not a
# phrasebook: none of them decides anything on its own, and none maps a
# phrase to an action. They only push a turn AWAY from FAST, which is the
# conservative direction — a slightly more expensive correct turn beats a
# cheap wrong one.
_REFERENTIAL = re.compile(
    r"\b(it|that|this|those|these|there|same|previous|last|again|"
    r"other|another|instead|back)\b", re.I)
_CONJUNCTIONS = re.compile(r"\b(and|then|also|after that|plus)\b", re.I)
_FAST_MAX_WORDS = 8


def select_mode(candidates: Optional[dict], user_text: str,
                dialogue_state: Optional[dict] = None) -> str:
    """FAST / CONTEXTUAL / MULTI_STEP, decided from shape alone.

    FAST is deliberately hard to reach: it requires that there is NOTHING to
    reason about — no artifacts, no last result, no committed dialogue state,
    no referential word, and a short request. Anything else is at least
    CONTEXTUAL, because misreading a follow-up as a fresh question is the
    expensive mistake, not spending an extra model call.
    """
    from .tools import planner

    text = (user_text or "").strip()
    words = text.split()
    candidates = candidates or {}

    # A compound request names several things to do. Counted from clause
    # structure, never from the model's own account of its intentions.
    conjunctions = len(_CONJUNCTIONS.findall(text))
    if conjunctions >= 2 or (conjunctions >= 1 and len(words) > 18):
        return ReasoningMode.MULTI_STEP

    state = dialogue_state or candidates.get("dialogue_state") or {}
    has_state = bool((state.get("fields") or {}))
    has_context = planner.has_actionable_context(candidates)
    referential = bool(_REFERENTIAL.search(text))
    action_shaped = planner.looks_action_shaped(text, candidates)

    if has_state or has_context or referential or action_shaped:
        return ReasoningMode.CONTEXTUAL
    if len(words) <= _FAST_MAX_WORDS:
        return ReasoningMode.FAST
    return ReasoningMode.CONTEXTUAL


# --------------------------------------------------------------- tracing

def reasoning_trace(*, conversation_id, turn_id, mode: str,
                    observation: Optional[dict], decision: Optional[dict],
                    next_action: Optional[str],
                    replan_count: int = 0) -> str:
    """One sanitized line per decision. Fields only — never model text.

    Deliberately structured so a conversational failure is debuggable without
    anyone ever needing (or being able) to read the model's private
    reasoning: mode, the factual observation, the decision, and what runs
    next.
    """
    observation = observation or {}
    decision = decision or {}
    return ("[REASONING] conversation=%s turn=%s mode=%s replans=%s "
            "observation={action=%s success=%s rows=%s error=%s artifact=%s} "
            "decision=%s reason=%s next=%s") % (
        conversation_id, turn_id, mode, replan_count,
        observation.get("action"), observation.get("success"),
        observation.get("row_count"), observation.get("error_type"),
        observation.get("artifact_id"),
        decision.get("decision"), _sanitize(decision.get("reason")),
        next_action)


# ------------------------------------------------------- retry fingerprint

def action_fingerprint(action: Optional[str],
                       arguments: Optional[dict]) -> str:
    """A stable identity for "this exact action, with these exact arguments".

    Re-planning must be CORRECTIVE. Without this, a model under pressure
    happily proposes the identical failing call again and burns the budget
    reproducing the same error.
    """
    import json
    normalized = {}
    for key, value in sorted((arguments or {}).items()):
        if value is None or value == "":
            continue
        normalized[key] = (" ".join(str(value).split()).lower()[:200])
    return f"{action or 'none'}::{json.dumps(normalized, sort_keys=True)}"
