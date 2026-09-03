"""Canonical dialogue state: what the user is currently trying to accomplish.

The agent's memory used to be operational leftovers — `last_result`,
`last_artifact_id`, a window of raw turns. Useful, but not conversational
memory: "only camera 3" after "show detections yesterday" needs the system to
know the ACTIVE TASK is *yesterday's detections* and that the new turn
REPLACES one facet of it, not that some SQL string ran most recently.

Two objects, deliberately separate:

  * `CanonicalDialogueState` (this module) — authoritative, durable, tiny.
    Active task, filters, references, language — each field carrying
    provenance (which turn set it, and why). Persisted inside
    working_context; versioned and migrated like everything else there.
  * The model-facing context envelope (assembled per call elsewhere) — canonical
    state + recent turns + summary + memories. Transient, budgeted, NEVER
    persisted. Keeping them separate is what stops prompt material from
    quietly becoming permanent memory.

THE AUTHORITY RULE, same as the planner's: the model PROPOSES meaning; the
application COMMITS state. A model may emit a `StateDelta`; only
`apply_delta` — deterministic, validating against current state and the
allowed vocabulary — mutates anything. A model that returns a whole
replacement state replaces nothing.

Nothing here calls an LLM, reads a file, or touches the database. Everything
is deterministic and directly testable.
"""

import copy
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DIALOGUE_STATE_VERSION = 1

# The only operations a delta may name. REPLACE and ADD are distinct on
# purpose: "camera 4 instead" and "also camera 4" differ exactly there, and a
# system that models every follow-up as ADD answers "cameras 3 and 4" to a
# user who corrected themselves.
DELTA_OPERATIONS = ("ADD", "REPLACE", "REMOVE", "PRESERVE", "REFERENCE", "ROLLBACK")

# Fields a delta may touch, with their value shape. A delta naming any other
# field is rejected — the model cannot invent state.
_FIELD_KINDS = {
    "active_camera": "list",        # list of camera identifiers
    "active_time_range": "scalar",  # e.g. "yesterday", "2026-08-01..2026-08-07"
    "referenced_entity": "list",    # person/subject names under discussion
    "referenced_artifact": "scalar",
    "referenced_result": "scalar",
    "output_language": "scalar",    # 'en' | 'ar'
    "requested_format": "scalar",   # 'pdf' | 'word' | 'report'
    "active_task": "scalar",        # short NL description of the current goal
    # A question the agent asked and is still waiting on. Stored as an object
    # ({type, original_intent, original_query, field, candidates}) so the next
    # turn can tell an ANSWER from a new request. Held as a scalar because
    # apply_delta writes a non-list value through untouched.
    "pending_clarification": "scalar",
    "current_topic": "scalar",
}

# Precedence INSIDE structured state, highest first. An explicit correction
# in the current turn outranks everything the system inferred earlier —
# "no, I meant camera 4" wins over any older value regardless of source.
SOURCE_PRECEDENCE = (
    "user_correction",       # the user explicitly corrected a value this turn
    "tool_result",           # a validated tool/action result set it
    "user_statement",        # the user stated it plainly
    "inherited",             # carried forward from the previous task state
    "inferred",              # the model's interpretation, application-committed
)

_MAX_TASK_HISTORY = 8      # bounded snapshots; references only, never content
_MAX_LIST_VALUES = 12      # a filter list longer than this is not a filter


def empty_state() -> dict:
    """A fresh canonical state. Plain dict: it lives inside working_context
    and must serialize/migrate exactly like everything else there."""
    return {
        "version": DIALOGUE_STATE_VERSION,
        "context_version": 0,     # bumped by every committed delta; the turn
                                  # trace records before/after for replay
        "fields": {},             # name -> {value, source, source_turn_id}
        "task_history": [],       # snapshots of `fields`, newest last
        "unresolved_references": [],
    }


def migrate_state(raw: Optional[dict]) -> dict:
    """Bring a stored state up to the current version. Unknown keys survive."""
    if not isinstance(raw, dict):
        return empty_state()
    state = dict(raw)
    base = empty_state()
    for key, default in base.items():
        state.setdefault(key, copy.deepcopy(default))
    if not isinstance(state.get("fields"), dict):
        state["fields"] = {}
    if not isinstance(state.get("task_history"), list):
        state["task_history"] = []
    return state


def get_value(state: dict, field: str):
    """The committed value of a field, or None."""
    entry = (state.get("fields") or {}).get(field)
    return entry.get("value") if isinstance(entry, dict) else None


def get_provenance(state: dict, field: str) -> Optional[dict]:
    entry = (state.get("fields") or {}).get(field)
    if not isinstance(entry, dict):
        return None
    return {k: entry.get(k) for k in ("source", "source_turn_id")}


# ---------------------------------------------------------------------------
# Deltas
# ---------------------------------------------------------------------------

class DeltaRejected(Exception):
    """The proposed change failed validation; state is untouched."""


def validate_delta(raw: Optional[dict]) -> dict:
    """Normalize and allow-list a proposed delta. Raises DeltaRejected.

    Checks SHAPE and VOCABULARY only — apply_delta re-checks against the
    actual current state (e.g. REMOVE of a value that is not set).
    """
    if not isinstance(raw, dict):
        raise DeltaRejected("delta is not an object")

    operation = str(raw.get("operation") or "").upper()
    if operation not in DELTA_OPERATIONS:
        raise DeltaRejected(f"unknown operation {operation[:24]!r}")

    field = raw.get("field")
    if operation in ("ADD", "REPLACE", "REMOVE") :
        if field not in _FIELD_KINDS:
            raise DeltaRejected(f"unknown field {str(field)[:40]!r}")
    elif field is not None and field not in _FIELD_KINDS:
        raise DeltaRejected(f"unknown field {str(field)[:40]!r}")

    value = raw.get("proposed_value")
    if operation in ("ADD", "REPLACE") and value in (None, "", []):
        raise DeltaRejected(f"{operation} requires a proposed_value")
    if isinstance(value, str) and len(value) > 500:
        raise DeltaRejected("proposed_value too long")
    if isinstance(value, list):
        if len(value) > _MAX_LIST_VALUES:
            raise DeltaRejected("proposed_value list too long")
        if not all(isinstance(item, (str, int)) for item in value):
            raise DeltaRejected("proposed_value list holds a non-scalar")

    source = raw.get("source") or "inferred"
    if source not in SOURCE_PRECEDENCE:
        source = "inferred"

    try:
        confidence = min(max(float(raw.get("confidence", 0.5)), 0.0), 1.0)
    except (TypeError, ValueError):
        confidence = 0.5

    return {
        "operation": operation,
        "field": field,
        "proposed_value": value,
        "referenced_object": raw.get("referenced_object"),
        "source": source,
        "confidence": confidence,
        "evidence_turn_ids": [str(t) for t in (raw.get("evidence_turn_ids") or [])][:8],
    }


def _outranks(new_source: str, old_source: Optional[str]) -> bool:
    """Does a value from new_source override one from old_source?

    Equal rank overrides too — the NEWER statement of equal authority wins
    ("camera 4 instead" after "camera 3" are both user statements).
    """
    if old_source is None:
        return True
    order = {name: index for index, name in enumerate(SOURCE_PRECEDENCE)}
    return order.get(new_source, len(order)) <= order.get(old_source, len(order))


#: Words that carry no selection meaning of their own. Removing them leaves
#: the reference itself, so "the second one" and "number 2" reduce to the same
#: token. This parses a POINTER INTO A LIST the user was shown — it is not a
#: phrasebook for intent, and it decides nothing on its own: whatever survives
#: must still be a position that exists.
_SELECTION_FILLER = frozenset({
    "the", "one", "ones", "number", "num", "no", "option", "candidate",
    "choice", "please", "that", "this", "is", "it", "i", "mean", "want",
})

_ORDINAL_WORDS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5,
}


def _selected_position(text: str, count: int) -> Optional[int]:
    """A 1-based position, when the message is ONLY a position.

    "number 2" and "the second one" select; "only camera 3" does not, because
    once the filler is removed something other than the position remains. That
    distinction is what stops an ordinary request being read as an answer.
    """
    tokens = [t.strip("#().,:;!?-،") for t in (text or "").lower().split()]
    tokens = [t for t in tokens if t and t not in _SELECTION_FILLER]
    if len(tokens) != 1:
        return None

    token = tokens[0]
    position = _ORDINAL_WORDS.get(token)
    if position is None and token.isdigit():
        position = int(token)
    if position is None or not (1 <= position <= count):
        return None
    return position


def match_candidate(state: dict, text: str) -> Optional[dict]:
    """The candidate an answer refers to, or None.

    Matched against the STORED candidate list rather than by searching again,
    so "the second one" can only ever mean the second thing the user was
    actually shown.

    Returns None for anything that is not an answer — a new request, a
    cancellation, an out-of-range position — leaving the turn to handle the
    message normally and retire the question.
    """
    pending = get_value(state, "pending_clarification") or {}
    candidates = (pending or {}).get("candidates") or []
    if not candidates or not text:
        return None

    # By NAME first: a person may legitimately be called "2", and the name
    # they were shown beats reading the same text as an index.
    try:
        from .tools.tool_executors import match_key
    except Exception:
        match_key = lambda v: " ".join((v or "").split()).casefold()

    key = match_key(text)
    if key:
        for candidate in candidates:
            if match_key(candidate.get("display_name")) == key:
                return candidate

    # "Did you mean JOEY?" - "yes" picks the one candidate offered. Only
    # for a single candidate: "yes" to a list of three chooses nothing.
    if len(candidates) == 1 and _is_affirmative(text):
        return candidates[0]

    position = _selected_position(text, len(candidates))
    return candidates[position - 1] if position else None


_AFFIRMATIVES = frozenset("""
yes y yeah yep yup sure correct right exactly ok okay indeed
نعم أجل اجل ايوه أيوه صح صحيح تمام اوك أوك بالضبط
""".split())


def _is_affirmative(text: str) -> bool:
    import re

    words = re.findall(r"[^\W\d_]+", str(text or "").casefold())
    return bool(words) and len(words) <= 3 and all(w in _AFFIRMATIVES for w in words)


def apply_delta(state: dict, raw_delta: dict, *, turn_id: str) -> dict:
    """Commit one validated change. Returns a NEW state; never mutates.

    This is the only door. It validates the delta, checks it against the
    CURRENT state, applies exactly the named change, bumps context_version,
    and leaves every other field byte-identical — a model cannot lose
    `yesterday` while changing the camera, because it never writes the state,
    only this function does.
    """
    delta = validate_delta(raw_delta)
    new_state = copy.deepcopy(migrate_state(state))
    fields: Dict[str, Any] = new_state["fields"]
    operation, field = delta["operation"], delta["field"]
    kind = _FIELD_KINDS.get(field or "", "scalar")

    if operation == "PRESERVE":
        # Explicitly "same thing": no field changes, but the intent is real —
        # record the version bump so the trace shows the turn was understood.
        pass

    elif operation == "ADD":
        current = get_value(new_state, field)
        if kind == "list":
            merged = list(current or [])
            for item in (delta["proposed_value"]
                         if isinstance(delta["proposed_value"], list)
                         else [delta["proposed_value"]]):
                if item not in merged:
                    merged.append(item)
            if len(merged) > _MAX_LIST_VALUES:
                raise DeltaRejected(f"{field} would exceed {_MAX_LIST_VALUES} values")
            value = merged
        else:
            # ADD to a scalar that already holds a DIFFERENT value is a
            # contradiction the model must resolve, not a merge.
            if current not in (None, delta["proposed_value"]):
                raise DeltaRejected(
                    f"ADD to scalar {field} that already holds {current!r}; "
                    f"use REPLACE")
            value = delta["proposed_value"]
        old = fields.get(field) or {}
        if not _outranks(delta["source"], old.get("source")):
            raise DeltaRejected(
                f"{delta['source']} does not outrank {old.get('source')} on {field}")
        fields[field] = {"value": value, "source": delta["source"],
                         "source_turn_id": turn_id}

    elif operation == "REPLACE":
        old = fields.get(field) or {}
        if not _outranks(delta["source"], old.get("source")):
            raise DeltaRejected(
                f"{delta['source']} does not outrank {old.get('source')} on {field}")
        value = delta["proposed_value"]
        if kind == "list" and not isinstance(value, list):
            value = [value]
        fields[field] = {"value": value, "source": delta["source"],
                         "source_turn_id": turn_id}

    elif operation == "REMOVE":
        current = get_value(new_state, field)
        if current is None:
            raise DeltaRejected(f"REMOVE of {field}, which is not set")
        proposed = delta["proposed_value"]
        if kind == "list" and proposed not in (None, "", []):
            # remove ONE value from the list ("remove camera 3")
            remaining = [item for item in current if item != proposed
                         and str(item) != str(proposed)]
            if remaining == list(current):
                raise DeltaRejected(f"{proposed!r} is not in {field}")
            if remaining:
                fields[field] = {"value": remaining, "source": delta["source"],
                                 "source_turn_id": turn_id}
            else:
                fields.pop(field, None)
        else:
            # "forget the camera filter": the whole field goes; everything
            # else — the time range included — stays untouched.
            fields.pop(field, None)

    elif operation == "REFERENCE":
        # Point at a prior object without altering filters: resolution data
        # for THIS turn, recorded so the trace shows what "it" meant.
        if field:
            old = fields.get(field) or {}
            if _outranks(delta["source"], old.get("source")):
                fields[field] = {"value": delta["proposed_value"]
                                 or delta["referenced_object"],
                                 "source": delta["source"],
                                 "source_turn_id": turn_id}

    elif operation == "ROLLBACK":
        # "Go back to the camera 3 report": restore a snapshot from task
        # history, matched by the referenced object (artifact id or an index).
        restored = _find_snapshot(new_state, delta["referenced_object"])
        if restored is None:
            raise DeltaRejected(
                "ROLLBACK target not found in task history — ask, don't guess")
        new_state["fields"] = copy.deepcopy(restored["fields"])

    new_state["context_version"] = int(new_state.get("context_version", 0)) + 1
    return new_state


# ---------------------------------------------------------------------------
# Task history — what "go back to the previous report" resolves against
# ---------------------------------------------------------------------------

def snapshot_task(state: dict, *, turn_id: str, label: Optional[str] = None) -> dict:
    """Record the current task state as a restorable snapshot.

    Called by the APPLICATION when a task boundary is crossed (a document was
    produced, a new task replaced the old one) — never by the model. Holds
    references and filter values only; content lives in the artifact/history
    rows the references point to.
    """
    new_state = copy.deepcopy(migrate_state(state))
    if not new_state["fields"]:
        return new_state
    entry = {
        "turn_id": turn_id,
        "label": (label or "")[:120] or None,
        "artifact_id": get_value(new_state, "referenced_artifact"),
        "fields": copy.deepcopy(new_state["fields"]),
        "context_version": new_state.get("context_version", 0),
    }
    history: List[dict] = new_state["task_history"]
    history.append(entry)
    del history[:-_MAX_TASK_HISTORY]
    return new_state


def _find_snapshot(state: dict, reference) -> Optional[dict]:
    """Resolve a rollback target: artifact id, snapshot label, or 'previous'."""
    history = state.get("task_history") or []
    if not history:
        return None
    if reference in (None, "", "previous", "last"):
        return history[-1]
    needle = str(reference)
    for entry in reversed(history):
        if str(entry.get("artifact_id")) == needle or entry.get("label") == needle:
            return entry
    return None


def list_task_history(state: dict) -> List[dict]:
    """Compact view for the context envelope: labels and references only."""
    return [
        {"turn_id": entry.get("turn_id"), "label": entry.get("label"),
         "artifact_id": entry.get("artifact_id"),
         "context_version": entry.get("context_version")}
        for entry in (state.get("task_history") or [])
    ]


# ---------------------------------------------------------------------------
# Rolling summary — a DERIVED CACHE, never a source of truth
# ---------------------------------------------------------------------------

SUMMARY_VERSION = 1
_MAX_SUMMARY_CHARS = 700


def build_summary(recent_turns: List[str], state: Optional[dict] = None) -> dict:
    """Summarize older conversation, deterministically and rebuildably.

    Deliberately EXTRACTIVE, not generative. A model-written summary would be
    lossy in exactly the places that matter (an id dropped, a negation
    inverted) and unrebuildable after an upgrade. This keeps the first clause
    of each retained turn plus the structured facts, so it can always be
    regenerated from retained history — and if it is ever corrupt or
    version-incompatible, `needs_rebuild` says so and the caller rebuilds it
    rather than trusting it.

    It is NEVER consulted for exact values. UUIDs, camera numbers, dates,
    person names, SQL provenance and artifact ids come from the canonical
    state and its provenance; this only helps the model follow the thread.
    """
    turns = [str(t).strip() for t in (recent_turns or []) if str(t).strip()]
    fragments = []
    for turn in turns[-6:]:
        first = turn.split("\n", 1)[0]
        fragments.append(first[:90])
    text = " | ".join(fragments)[:_MAX_SUMMARY_CHARS]
    return {
        "version": SUMMARY_VERSION,
        "text": text,
        # What the summary was derived FROM, so staleness is detectable
        # without re-reading the transcript.
        "source_turns": len(turns),
        "context_version": (state or {}).get("context_version", 0),
    }


def needs_rebuild(summary: Optional[dict], *, turn_count: int,
                  context_version: int) -> bool:
    """True when the cached summary must be regenerated before use.

    Rebuild triggers: missing, wrong/absent version (an upgrade changed the
    shape), corrupt, or derived from strictly fewer turns / an older state
    than we now have. Conversation correctness never depends on the cached
    copy surviving any of those.
    """
    if not isinstance(summary, dict):
        return True
    if summary.get("version") != SUMMARY_VERSION:
        return True
    if not isinstance(summary.get("text"), str):
        return True
    if int(summary.get("source_turns") or 0) < turn_count:
        return True
    if int(summary.get("context_version") or 0) < context_version:
        return True
    return False


# ---------------------------------------------------------------------------
# The turn trace — reproducibility
# ---------------------------------------------------------------------------

def transition_trace(*, conversation_id, turn_id, previous_state: dict,
                     resulting_state: dict, resolved_references: dict,
                     planned_action: Optional[str], delta: Optional[dict]) -> str:
    """One sanitized line per turn: state before → delta → state after.

    Answers "exactly what state did the agent believe when it generated this
    SQL?" without reconstructing it from scattered logs. Values are field
    names and versions, never report content.
    """
    def _fields(state):
        return {name: get_value(state, name)
                for name in sorted((state.get("fields") or {}).keys())}

    return ("[DIALOGUE_STATE] conversation=%s turn=%s context_version=%s->%s "
            "action=%s delta=%s resolved=%s fields=%s") % (
        conversation_id, turn_id,
        (previous_state or {}).get("context_version", 0),
        (resulting_state or {}).get("context_version", 0),
        planned_action,
        {k: delta.get(k) for k in ("operation", "field", "proposed_value", "source")}
        if delta else None,
        resolved_references, _fields(resulting_state or {}))
