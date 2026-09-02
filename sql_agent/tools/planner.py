"""What the user is asking for — decided, then re-checked.

The agent used to route on a binary CHAT/SQL_QUERY classification, so
"make that a PDF", "the last report in Arabic" and "same report but camera 3"
all fell into CHAT and were answered with small talk. This module widens that
decision to six actions without widening what the model is trusted to decide.

The division is the point:

  * The PLANNER states an INTENT. It picks an action name and, at most, points
    at one of the candidates it was handed.
  * The DISPATCHER holds the AUTHORITY. Every field the planner returns is
    re-validated here against allow-lists and against the candidate set that
    Python built, and an artifact id is then re-checked against the DATABASE
    by the caller before anything is read.

So a planner that hallucinates an artifact id, invents a file path, asks for
a format that does not exist, or names another user's document does not get
any of those things: it gets its action rejected. Nothing the model emits is
ever a path, SQL to execute, or an authorization decision.

Nothing here calls an LLM. Everything is deterministic and directly testable.
"""

import json
import logging
import re
import uuid as uuid_mod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- vocabulary

ACTIONS = (
    "chat",                    # conversation; no data, no document
    "query_database",          # a new question for the SQL chain
    "modify_previous_query",   # re-run a previous question under a new filter
    "generate_document",       # render what we already have as a file
    "translate_artifact",      # restate an existing document in another language
    "clarify",                 # we cannot act safely; ask one short question
)
TARGETS = ("last_result", "artifact")
LANGUAGES = ("en", "ar")
FORMATS = ("pdf", "word", "report")

# Actions that DO something to prior state. A failure while planning one of
# these must never be answered with small talk — see decide_on_failure.
ACTION_SHAPED = ("generate_document", "translate_artifact", "modify_previous_query")

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


class PlannedAction:
    """A validated plan. Constructing one is the only way to get an action.

    Fields are already allow-listed and the artifact id is already known to
    belong to the candidate set — but ownership is still the database's
    answer, re-checked by the caller before the document is touched.
    """

    __slots__ = ("action", "confidence", "target", "artifact_id", "language",
                 "format", "modification", "clarify_question", "source",
                 "state_delta")

    def __init__(self, action: str, confidence: float = 0.5, target=None,
                 artifact_id=None, language=None, format=None,
                 modification=None, clarify_question=None, source="planner",
                 state_delta=None):
        self.action = action
        self.confidence = confidence
        self.target = target
        self.artifact_id = artifact_id
        self.language = language
        self.format = format
        self.modification = modification
        self.clarify_question = clarify_question
        self.source = source            # planner | deterministic | fallback
        # A VALIDATED dialogue-state change the model proposed (or None).
        # Validation happened in validate_plan via dialogue_state.validate_delta;
        # COMMIT happens in the application, only after the action succeeds.
        self.state_delta = state_delta

    def as_dict(self) -> dict:
        return {slot: getattr(self, slot) for slot in self.__slots__}

    def __repr__(self) -> str:
        return f"PlannedAction({self.action}, source={self.source})"


# A small deterministic seam for commands whose domain intent is explicit.
# This is deliberately not a general keyword classifier: only a complete,
# self-contained imperative with a named subject qualifies. Contextual
# references ("track him") and compound work ("track Ali and make a PDF")
# still need the tool loop because they require resolution or several actions.
_TRACK_PERSON_COMMAND = re.compile(
    r"^\s*(?:please\s+)?track\s+(?:person\s+)?(?P<subject>.+?)\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_CONTEXTUAL_TRACK_SUBJECTS = frozenset({
    "a person", "anyone", "her", "him", "it", "someone", "that person",
    "them", "this person",
})
_COMPOUND_TRACK_WORDS = re.compile(r"\b(?:also|and|then)\b", re.IGNORECASE)


def deterministic_request_plan(user_text: str) -> Optional[PlannedAction]:
    """Recognise only unambiguous, self-contained domain commands.

    A small tool-calling model may answer ``track Iron Man`` conversationally
    even though the command is an exact database operation. Deterministic
    routing is appropriate here because the verb, object and application
    domain jointly remove ambiguity. The SQL pipeline still resolves the
    person after an empty result and all generated SQL still crosses the AST
    authorization gate.
    """
    match = _TRACK_PERSON_COMMAND.fullmatch(str(user_text or ""))
    if not match:
        return None

    subject = match.group("subject").strip().strip("\"'\u201c\u201d\u2018\u2019")
    folded = " ".join(subject.casefold().split())
    if (not folded or folded in _CONTEXTUAL_TRACK_SUBJECTS
            or _COMPOUND_TRACK_WORDS.search(subject)):
        return None

    return PlannedAction(
        action="query_database", confidence=1.0, source="deterministic")


# ------------------------------------------------------------ JSON recovery

def extract_json_object(raw: str) -> Optional[dict]:
    """Pull the first complete JSON object out of a model's reply.

    A regex like r'\\{[^}]+\\}' — which is what intent classification used —
    stops at the first '}', so it truncates any object containing a nested
    one and returns None for the rest. This walks braces instead, honours
    string literals and escapes, and tolerates ```json fences and prose on
    either side.
    """
    if not raw:
        return None
    text = raw.strip()
    if "```" in text:
        fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
        if fenced:
            text = fenced.group(1).strip()

    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:index + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except (ValueError, TypeError):
                        pass
                    break
        start = text.find("{", start + 1)
    return None


# ------------------------------------------------ deterministic resolution

def resolve_candidates(working_context: Optional[dict],
                       artifact_index: Optional[List[dict]],
                       user_text: str) -> dict:
    """Everything this turn could legitimately refer to — built in Python.

    The planner is handed this and may only choose from it. It is never asked
    to remember or rediscover state, because a model that can invent the id it
    is about to act on is a model that decides authorization.
    """
    context = working_context or {}
    artifacts = list(artifact_index or [])
    last_artifact_id = context.get("last_artifact_id")

    allowed_ids = {str(entry.get("artifact_id")) for entry in artifacts
                   if entry.get("artifact_id")}
    if last_artifact_id:
        allowed_ids.add(str(last_artifact_id))

    # An id the user typed themselves outranks any inference — but it still
    # has to be one of theirs, so it is only accepted if it is in the set.
    explicit = None
    for match in _UUID_RE.findall(user_text or ""):
        if match.lower() in {a.lower() for a in allowed_ids}:
            explicit = match
            break

    # "Report" is deliberately not an artifact word. A database answer can
    # itself be a report, and that is exactly what "make the report Arabic"
    # means immediately after a query. Only concrete file vocabulary grants
    # the planner permission to bind an otherwise unqualified request to a
    # stored artifact. Conversely, an explicit reference to the response must
    # keep pointing at the response even when a document was generated later.
    lowered = (user_text or "").casefold()
    explicit_document_reference = bool(re.search(
        r"\b(?:document|file|pdf|word|docx)\b|(?:مستند|وثيقة|ملف|بي\s*دي\s*إف)",
        lowered,
    ))
    explicit_response_reference = bool(re.search(
        r"\b(?:response|answer|result|text|narrative)\b|"
        r"(?:الرد|الإجابة|الاجابة|النتيجة|النص)",
        lowered,
    ))

    return {
        "artifacts": artifacts,
        "allowed_artifact_ids": allowed_ids,
        "explicit_artifact_id": explicit,
        "explicit_document_reference": explicit_document_reference,
        "explicit_response_reference": explicit_response_reference,
        "last_artifact_id": str(last_artifact_id) if last_artifact_id else None,
        "last_result": context.get("last_result"),
        "last_query": context.get("last_query"),
        "last_action": context.get("last_action"),
        "response_language": context.get("response_language") or "en",
        # The canonical dialogue state — active task/filters/references with
        # provenance. Built by application commits, handed to the model as
        # resolved fact; the model never rediscovers or rewrites it.
        "dialogue_state": context.get("dialogue_state"),
    }


def default_artifact_id(candidates: dict) -> Optional[str]:
    """The artifact an unqualified reference means, or None if ambiguous.

    Order: an id the user typed, then the artifact this session last produced,
    then the only one they have. Two or more with no other signal is genuinely
    ambiguous and resolves to nothing, so the caller asks instead of guessing.
    """
    if candidates.get("explicit_artifact_id"):
        return candidates["explicit_artifact_id"]
    if candidates.get("last_artifact_id"):
        return candidates["last_artifact_id"]
    artifacts = candidates.get("artifacts") or []
    if len(artifacts) == 1:
        return str(artifacts[0].get("artifact_id"))
    return None


def has_actionable_context(candidates: dict) -> bool:
    """True when there is something a document action could act ON."""
    return bool(candidates.get("allowed_artifact_ids") or candidates.get("last_result"))


# ------------------------------------------------------------- the context

# Per-section character budgets for the model-facing envelope, in priority
# order. The envelope can otherwise grow without limit — recent turns, task
# history, artifact candidates, memories, a summary and provenance all
# accumulate — and an over-long prompt degrades a small model badly.
#
# THE RULE: sections are emitted highest-priority first, and a lower-priority
# section is dropped when the budget is spent. The authoritative dialogue
# state and resolved references are NEVER truncated to make room for anything
# below them — losing the active camera to fit an old memory is precisely the
# failure this ordering exists to prevent.
_ENVELOPE_BUDGET_CHARS = 4000
_SECTION_BUDGETS = {
    "dialogue_state": 1200,   # priority 2 — authoritative, always kept
    "references": 900,        # priority 3 — resolved artifacts/results, always
    "recent_turns": 700,      # priority 4
    "summary": 800,           # priority 5
    "memories": 400,          # priority 6
}
#: How to read a field that the model would otherwise take at face value.
#: Only fields where the distinction changes behaviour are annotated.
_FIELD_NOTES = {
    "referenced_entity": ("  <- THE SUBJECT. Authoritative. Use this, never "
                          "the wording of active_task."),
    "active_task": ("  <- a readable summary of an earlier turn; it may be "
                    "stale and never overrides referenced_entity."),
    "pending_clarification": ("  <- you asked this and it is still open. If "
                              "the user just answered it, CONTINUE the "
                              "original_query with that person; do not start "
                              "over and do not ask again."),
}

_ALWAYS_KEPT = ("dialogue_state", "references")


def _fit(lines: List[str], budget: int) -> List[str]:
    """Keep whole lines while they fit; never emit a half-truncated fact."""
    kept, used = [], 0
    for line in lines:
        cost = len(line) + 1
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    return kept


def build_planner_context(candidates: dict, recent_turns: Optional[List[str]] = None,
                          max_turn_chars: int = 120,
                          conversation_summary: Optional[str] = None) -> str:
    """The state block handed to the planner. Assembled here, never by the LLM.

    Bounded on purpose: a result preview is a one-line shape summary, not
    rows, and artifact entries carry no content. Session state is surveillance
    output, and a prompt is the easiest place to leak it from.

    Budgeted BY SECTION in the priority order on _SECTION_BUDGETS: the
    authoritative dialogue state and resolved references are emitted first and
    never dropped; the rolling summary and durable memories yield first when
    space runs short.
    """
    sections = {}

    # Priority 2: authoritative dialogue state — never truncated away.
    state_lines = []
    state = candidates.get("dialogue_state") or {}
    fields = state.get("fields") or {}
    if fields:
        state_lines.append("- current task state (authoritative; the newest explicit "
                           "user correction always wins):")
        # The SUBJECT first, and labelled. `active_task` is a sentence written
        # by the SQL model about an earlier turn; presented as plain fact in an
        # "authoritative" block it reads as the subject, which is how "Track
        # Joey" survived into a request about someone else.
        ordered = sorted(fields, key=lambda n: (n != "referenced_entity", n))
        for name in ordered:
            entry = fields.get(name) or {}
            note = _FIELD_NOTES.get(name, "")
            state_lines.append(f"    {name} = {entry.get('value')!r} "
                               f"(set by {entry.get('source', 'unknown')})"
                               f"{note}")
    history = (state.get("task_history") or [])[-3:]
    if history:
        state_lines.append("- earlier tasks you can return to:")
        for entry in history:
            state_lines.append(f"    {entry.get('label')!r} -> "
                               f"artifact {entry.get('artifact_id')}")
    sections["dialogue_state"] = state_lines

    lines = []
    result = candidates.get("last_result")
    if result:
        columns = ", ".join(result.get("columns") or [])[:120]
        lines.append(
            f"- last_result: {result.get('row_count', 0)} row(s)"
            f"{'; columns: ' + columns if columns else ''}"
            f"{'; about: ' + result['purpose'] if result.get('purpose') else ''}")
    else:
        lines.append("- last_result: none")

    artifacts = candidates.get("artifacts") or []
    if artifacts:
        lines.append("- documents already generated (newest first):")
        for entry in artifacts[:3]:
            lines.append(
                f"    id={entry.get('artifact_id')} type={entry.get('type')} "
                f"language={entry.get('language')} title={str(entry.get('title'))[:60]!r}")
    else:
        lines.append("- documents already generated: none")

    lines.append(f"- reply language: {candidates.get('response_language', 'en')}")
    sections["references"] = lines

    # Priority 4-5: recent turns, then the rolling summary.
    turn_lines = []
    if candidates.get("last_query"):
        turn_lines.append(
            f"- previous question: {str(candidates['last_query'])[:max_turn_chars]}")
    for turn in (recent_turns or [])[-2:]:
        turn_lines.append(f"- earlier turn: {str(turn)[:max_turn_chars]}")
    sections["recent_turns"] = turn_lines
    sections["summary"] = (
        [f"- conversation so far: {conversation_summary.strip()}"]
        if conversation_summary and conversation_summary.strip() else [])

    out, remaining = [], _ENVELOPE_BUDGET_CHARS
    for name in ("dialogue_state", "references", "recent_turns", "summary",
                 "memories"):
        body = sections.get(name) or []
        if not body:
            continue
        kept = (body if name in _ALWAYS_KEPT
                else _fit(body, min(_SECTION_BUDGETS.get(name, 0),
                                    max(remaining, 0))))
        if not kept:
            continue
        out.extend(kept)
        remaining -= sum(len(line) + 1 for line in kept)
    return "\n".join(out)


PLANNER_SYSTEM_PROMPT = """You route requests for a security-camera database assistant.

Choose exactly ONE action:
- query_database: a new question about the data (people, cameras, detections, times).
- modify_previous_query: repeat the PREVIOUS question with a changed filter
  ("same but only camera 3", "same for yesterday").
- generate_document: turn what was just produced into a file (PDF or Word).
- translate_artifact: translate either the latest response or an EXISTING
  document. Use target=last_result with no artifact_id for a response; use
  target=artifact only when the user refers to a stored file/document.
- chat: greetings, thanks, questions about your own abilities.
- clarify: the request refers to something that is not in the state below.

Reply with ONLY a JSON object:
{"action": "...", "confidence": 0.0-1.0, "target": "last_result"|"artifact"|null,
 "artifact_id": "<one of the ids listed below>"|null, "language": "en"|"ar"|null,
 "format": "pdf"|"word"|null, "modification": "<the changed filter, in words>"|null,
 "clarify_question": "<one short question>"|null}

You MAY also include "state_delta" when the request changes the task state:
{"operation": "ADD"|"REPLACE"|"REMOVE"|"PRESERVE"|"ROLLBACK",
 "field": "active_camera"|"active_time_range"|"referenced_entity"|"output_language"|"requested_format"|"active_task",
 "proposed_value": ..., "source": "user_correction"|"user_statement"}
Use REPLACE for "X instead", ADD for "also X", REMOVE for "forget the X
filter", ROLLBACK (with referenced_object) for "go back to ...". Omit
state_delta when nothing about the task state changes.

Rules:
- artifact_id MUST be copied from the state block. Never invent one.
- Never output SQL, a file path, or a user id.
- A fragmentary follow-up inherits the previous turn's subject.
- state_delta changes ONE field. Never restate fields that did not change —
  the application preserves them; a full restate risks losing them.

Examples:
"how many people were detected yesterday" -> {"action": "query_database", "confidence": 0.95, "target": null, "artifact_id": null, "language": null, "format": null, "modification": null, "clarify_question": null}
"track IRON MAN" -> {"action": "query_database", "confidence": 0.95, "target": null, "artifact_id": null, "language": null, "format": null, "modification": null, "clarify_question": null}
"make that a PDF" -> {"action": "generate_document", "confidence": 0.9, "target": "last_result", "artifact_id": null, "language": null, "format": "pdf", "modification": null, "clarify_question": null}
"make the previous response Arabic" -> {"action": "translate_artifact", "confidence": 0.9, "target": "last_result", "artifact_id": null, "language": "ar", "format": null, "modification": null, "clarify_question": null}
"make the last PDF English" -> {"action": "translate_artifact", "confidence": 0.9, "target": "artifact", "artifact_id": "<id from state>", "language": "en", "format": null, "modification": null, "clarify_question": null}
"same report but only for camera 3" -> {"action": "modify_previous_query", "confidence": 0.9, "target": "artifact", "artifact_id": "<id from state>", "language": null, "format": null, "modification": "only camera 3", "clarify_question": null}
"thanks!" -> {"action": "chat", "confidence": 0.95, "target": null, "artifact_id": null, "language": null, "format": null, "modification": null, "clarify_question": null}
"no, camera 4" -> {"action": "modify_previous_query", "confidence": 0.9, "target": "last_result", "artifact_id": null, "language": null, "format": null, "modification": "camera 4 instead of the previous camera", "clarify_question": null, "state_delta": {"operation": "REPLACE", "field": "active_camera", "proposed_value": [4], "source": "user_correction"}}
"forget the camera filter" -> {"action": "modify_previous_query", "confidence": 0.9, "target": "last_result", "artifact_id": null, "language": null, "format": null, "modification": "remove the camera filter", "clarify_question": null, "state_delta": {"operation": "REMOVE", "field": "active_camera", "source": "user_statement"}}"""


# ------------------------------------------------------ dispatcher validation

def validate_plan(parsed: Optional[dict], candidates: dict) -> Optional[PlannedAction]:
    """Turn raw planner output into a plan, or into nothing.

    This is the authority boundary. Everything is checked against an
    allow-list or against the candidate set Python built; an id the planner
    supplied that is not in that set is DROPPED rather than trusted, even
    though it may well be a real artifact — belonging to someone else is
    exactly what that looks like.
    """
    if not isinstance(parsed, dict):
        return None

    action = str(parsed.get("action") or "").strip().lower()
    if action not in ACTIONS:
        logger.info("[PLANNER] rejected unknown action %r", action[:40])
        return None

    try:
        confidence = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = min(max(confidence, 0.0), 1.0)

    target = parsed.get("target")
    target = target if target in TARGETS else None

    language = str(parsed.get("language") or "").lower() or None
    language = language if language in LANGUAGES else None

    fmt = str(parsed.get("format") or "").lower() or None
    fmt = fmt if fmt in FORMATS else None

    artifact_id = parsed.get("artifact_id")
    if artifact_id is not None:
        artifact_id = str(artifact_id)
        allowed = {a.lower() for a in candidates.get("allowed_artifact_ids") or set()}
        if artifact_id.lower() not in allowed:
            logger.warning(
                "[PLANNER] discarded artifact_id outside the candidate set "
                "(planner may not name documents it was not offered)")
            artifact_id = None
        else:
            try:
                artifact_id = str(uuid_mod.UUID(artifact_id))
            except (ValueError, AttributeError, TypeError):
                artifact_id = None

    modification = parsed.get("modification")
    modification = str(modification)[:500] if modification else None
    question = parsed.get("clarify_question")
    question = str(question)[:300] if question else None

    # An optional dialogue-state change the model PROPOSES. Validation here
    # (shape + vocabulary via dialogue_state.validate_delta); the COMMIT
    # happens in the application, only after the planned action actually
    # succeeds. An invalid proposal is dropped, never fatal to the plan —
    # the action can still run, it just teaches the state nothing.
    state_delta = None
    raw_delta = parsed.get("state_delta")
    if raw_delta is not None:
        try:
            from .. import dialogue_state as _ds
            state_delta = _ds.validate_delta(raw_delta)
        except Exception as delta_error:
            logger.info("[PLANNER] discarded invalid state_delta: %s", delta_error)

    return apply_preconditions(PlannedAction(
        action=action, confidence=confidence, target=target,
        artifact_id=artifact_id, language=language, format=fmt,
        modification=modification, clarify_question=question,
        state_delta=state_delta), candidates)


def apply_preconditions(plan: PlannedAction, candidates: dict) -> PlannedAction:
    """Downgrade an action we cannot carry out, deterministically.

    A plan whose subject does not exist is not executed hopefully and allowed
    to fail somewhere deeper; it becomes a question. This is also where an
    unqualified document reference is bound to a specific id, in Python.
    """
    # The newest successful result is the authoritative source until a
    # document-producing action supersedes it. A small model may still copy a
    # stale artifact id from the candidate list for "make the report Arabic";
    # remove that inferred id here. An id typed by the user, or concrete file
    # language such as "PDF"/"Word document", remains an explicit artifact
    # reference. "Previous response" always selects the response.
    last_action = candidates.get("last_action")
    latest_result_is_source = bool(candidates.get("last_result")) and (
        candidates.get("explicit_response_reference")
        or (
            last_action not in ("generate_document", "translate_artifact")
            and not candidates.get("explicit_document_reference")
        )
    )
    if plan.action == "translate_artifact" and latest_result_is_source:
        if not candidates.get("explicit_artifact_id"):
            plan.artifact_id = None
            plan.target = "last_result"

    # Bind an unqualified reference to a concrete document — but ONLY for the
    # actions that act on one. generate_document renders the current result or
    # narrative; giving it a default artifact would make every new document
    # claim the newest unrelated one as its parent, and lineage that records a
    # relationship which never existed is worse than no lineage.
    if (plan.action in ("translate_artifact", "modify_previous_query")
            and plan.artifact_id is None
            and not (plan.action == "translate_artifact"
                     and latest_result_is_source)):
        plan.artifact_id = default_artifact_id(candidates)

    if plan.action == "translate_artifact":
        if not plan.artifact_id:
            if candidates.get("last_result") or candidates.get("last_query"):
                # The response itself is the source. Without an explicitly
                # requested file format this stays an inline translation; a
                # translation request must not manufacture a PDF/Word file.
                # When a format WAS requested, render that translated source.
                if plan.format:
                    plan.action = "generate_document"
                plan.language = plan.language or "ar"
                plan.target = "last_result"
                plan.source += "+latest-response"
            else:
                plan.action = "clarify"
                plan.clarify_question = plan.clarify_question or (
                    "I don't have a previous report to translate. "
                    "Which report do you mean?")
        elif not plan.language:
            plan.language = "ar" if candidates.get("response_language") == "en" else "en"

    elif plan.action == "generate_document":
        if not has_actionable_context(candidates):
            plan.action = "clarify"
            plan.clarify_question = plan.clarify_question or (
                "I don't have anything to put in a document yet. "
                "What would you like me to report on?")
        else:
            plan.format = plan.format or "pdf"
            plan.language = plan.language or candidates.get("response_language") or "en"

    elif plan.action == "modify_previous_query":
        last_result = candidates.get("last_result") or {}
        if not (plan.artifact_id or last_result.get("sql")):
            plan.action = "clarify"
            plan.clarify_question = plan.clarify_question or (
                "I don't have a previous query to adjust. "
                "Could you ask the full question?")

    if plan.action == "clarify" and not plan.clarify_question:
        plan.clarify_question = "Could you say a little more about what you need?"
    return plan


# -------------------------------------------------------------- failure path

def looks_action_shaped(user_text: str, candidates: dict) -> bool:
    """Is this a command about state we hold, rather than a fresh question?

    Used ONLY on the failure path, and it reasons from the resolver's own
    evidence rather than from keywords: a short, fragmentary utterance in a
    session that already has a result or a document is a follow-up. It cannot
    change a successful plan — it only decides whether a FAILED one becomes a
    question or falls back to the old classifier.
    """
    if not has_actionable_context(candidates):
        return False
    words = (user_text or "").split()
    if len(words) > 12:
        return False        # long enough to stand on its own as a question
    return bool(words)


def decide_on_failure(user_text: str, candidates: dict) -> Optional[PlannedAction]:
    """What to do when the planner produced nothing usable.

    Returns a `clarify` plan when the request was clearly ABOUT something we
    hold, and None when the caller should fall back to the legacy CHAT/SQL
    classifier. The distinction matters: silently answering "make it Arabic"
    with small talk is the exact failure this redesign exists to remove, while
    forcing an ordinary question through a clarification would be a
    regression for every user who never asks for a document.
    """
    if looks_action_shaped(user_text, candidates):
        return PlannedAction(
            action="clarify", confidence=0.3, source="fallback",
            clarify_question=(
                "I'm not sure what you'd like me to do with the previous "
                "result. Do you want it as a document, translated, or "
                "filtered differently?"))
    return None


# ------------------------------------------------------------------- audit

def audit_line(*, user_id, conversation_id, plan: Optional[PlannedAction],
               executed: str, resolution: str, artifact_id=None,
               result_id=None, success: bool = True) -> str:
    """One line per turn, recording what was DONE.

    Deliberately records the tool that ran and the rows it touched — never the
    prompt, the planner's reasoning, or the user's text. An audit trail of
    model thoughts is unverifiable and stores surveillance questions in a
    second place; an audit trail of executed actions is evidence.
    """
    return ("[AGENT_AUDIT] user_id=%s conversation=%s action=%s source=%s "
            "confidence=%.2f resolution=%s executed=%s artifact=%s result=%s "
            "outcome=%s") % (
        user_id, conversation_id,
        plan.action if plan else "none",
        plan.source if plan else "none",
        plan.confidence if plan else 0.0,
        resolution, executed, artifact_id, result_id,
        "ok" if success else "failed")
