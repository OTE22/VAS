"""Executing the read-only look-ups, in ordinary Python.

These are what let the agent stop guessing. "Who was detected there?" needs
to know which camera "there" is; "track Ali" needs to know whether Ali exists
and how the name is actually spelled. Before tools, the model invented both.

Every executor here:

  * is READ-ONLY and side-effect free, so a confused model calling one twice
    costs a little latency and nothing else;
  * is scoped by the CALLER, never by an argument — a tool argument cannot
    widen what the caller may see;
  * returns a small, bounded, JSON-serializable dict that goes back into the
    model's context, so it must never carry a raw row dump or a full report.

The action tools are NOT here: they route through the existing graph nodes so
they keep the AST guard, the artifact ownership checks and the audit trail.
Re-implementing them here would be a second, weaker path to the same power.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_ROWS = 25          # a look-up result is context, not a report
# The name POOL is matched in Python, so it needs enough coverage that a
# late-sorting name is still reachable; the existing typo corrector reads
# 500 for the same reason. Capping this at _MAX_ROWS silently made fuzzy
# matching depend on alphabetical position.
_MAX_NAME_POOL = 500
_MAX_NAME_MATCHES = 5


def execute_read_only(name: str, arguments: Dict[str, Any], *,
                      db, dialogue_state: Optional[dict] = None,
                      artifact_index: Optional[List[dict]] = None,
                      identity_index: Optional[List[dict]] = None) -> dict:
    """Run one validated read-only tool. Never raises.

    A failed look-up returns {"error": ...} rather than propagating: the model
    should be told the look-up failed and given the chance to ask, not have
    the whole turn collapse. Callers must have already run validate_call.
    """
    try:
        if name == "list_cameras":
            return _list_cameras(db)
        if name == "resolve_person":
            return _resolve_person(db, arguments.get("name", ""),
                                   identity_index=identity_index)
        if name == "get_task_state":
            return _get_task_state(dialogue_state)
        if name == "list_my_documents":
            return _list_documents(artifact_index)
        return {"error": f"{name} is not a read-only tool"}
    except Exception as e:
        logger.warning("[TOOL] %s failed: %s", name, e)
        return {"error": f"the {name} look-up failed"}


def _rows(db, sql: str, cap: Optional[int] = None) -> List[dict]:
    """A bounded read through the agent's OWN validated read path.

    Uses DatabaseManager.execute_query, the same read-only, AST-guarded path
    the SQL chain uses.

    NO PARAMETERS BY DESIGN. execute_query takes a bare string and hands it to
    psycopg2 with no binding, so every query passed here is a FIXED LITERAL
    written in this module. Model or user input is never interpolated into
    SQL; matching against user input happens in Python, below.
    """
    result = db.execute_query(sql)
    if isinstance(result, dict):
        if not result.get("success"):
            raise RuntimeError(result.get("error") or "query failed")
        return (result.get("rows") or [])[:cap or _MAX_ROWS]
    return list(result or [])[:cap or _MAX_ROWS]


def _list_cameras(db) -> dict:
    """The cameras that exist. Stops the model inventing camera ids.

    `pipelines` has no `name` column: a camera is identified by
    `pipeline_id` and described by `location_name`. Selecting `name` here
    raised on every call — which the model would have seen as "the look-up
    failed" and then, predictably, gone back to guessing.
    """
    rows = _rows(db, "SELECT id, pipeline_id, location_name, is_active "
                     "FROM pipelines ORDER BY pipeline_id LIMIT 25")
    cameras = [{"id": str(r.get("id")),
                "camera": r.get("pipeline_id"),
                "location": r.get("location_name"),
                "active": r.get("is_active")}
               for r in rows]
    return {"cameras": cameras, "count": len(cameras),
            "note": "refer to a camera by its `camera` value; never invent one"}


#: Arabic marks that carry no identity: harakat, the dagger alef, and the
#: tatweel used purely to stretch a word. Two spellings that differ only by
#: these are the same name.
_ARABIC_MARKS = dict.fromkeys(
    list(range(0x064B, 0x0653)) + [0x0640, 0x0670], None)

#: Letter forms a writer chooses freely. `.lower()` is a no-op for Arabic, so
#: before this every one of these pairs was a different person: a query for
#: 'علي' could not find a stored 'على'.
_ARABIC_FOLD = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",   # alef with hamza / madda
    "ى": "ي", "ئ": "ي",                        # alef maqsura, hamza on ya
    "ة": "ه",                                   # ta marbuta
    "ؤ": "و",
})


def match_key(name: Optional[str]) -> str:
    """The key two names are compared on when LOOKING ONE UP.

    Deliberately wider than `name_lookup_key`, and deliberately separate from
    it. That function decides whether two enrollments are the same person and
    backs the stored `display_name_key` column, so widening it would change
    which enrollments count as duplicates and desync every key already
    written. This one is computed at read time and persisted nowhere, so it
    can fold as aggressively as searching wants.

    Layers Unicode NFKC and Arabic folding on top of the shared
    casefold-and-collapse-whitespace semantics, so the two agree about
    everything they both handle.
    """
    if not name:
        return ""
    import unicodedata

    try:
        from backend.core.enrollment_service import name_lookup_key
        base = name_lookup_key(unicodedata.normalize("NFKC", str(name)))
    except Exception:
        # The agent must still resolve names if the enrollment module cannot
        # be imported; matching a little less precisely beats not at all.
        base = " ".join(unicodedata.normalize("NFKC", str(name)).split()).casefold()

    return base.translate(_ARABIC_MARKS).translate(_ARABIC_FOLD)


def _known_names(db) -> List[str]:
    """Distinct names the recognition pipeline has actually SEEN, bounded.

    A FIXED query — no interpolation. This is only half the pool: a person
    who is enrolled but has never been detected has no row here, which is
    exactly why "track ali" used to answer that Ali did not exist.
    """
    rows = _rows(db, "SELECT DISTINCT name FROM faces "
                     "WHERE name IS NOT NULL AND name != '' "
                     "ORDER BY name LIMIT 500", cap=_MAX_NAME_POOL)
    return [r.get("name") for r in rows if r.get("name")]


def _name_pool(db, identity_index: Optional[List[dict]]) -> List[dict]:
    """Everyone the agent may resolve: enrolled people, plus detected names.

    The enrolled half is handed in by the caller rather than queried. It lives
    in `identities`, which is NOT in the SQL guard's table allowlist — and
    must not be added to it, because that allowlist is precisely what stops
    the model reading tables it was never shown. Documents already reach the
    tools this way (`artifact_index`), built by an owner-scoped query in the
    API layer; this follows that path, so it is also where any future
    per-user visibility rule would attach.

    Deduplicated on the match key, preferring the entry that carries an
    identity id, so one person enrolled AND detected is one candidate.
    """
    pool: Dict[str, dict] = {}

    for entry in (identity_index or []):
        display = (entry or {}).get("display_name")
        key = match_key(display)
        if key and key not in pool:
            pool[key] = {"key": key, "display_name": display,
                         "identity_id": (entry or {}).get("identity_id")}

    for display in _known_names(db):
        key = match_key(display)
        if key and key not in pool:
            pool[key] = {"key": key, "display_name": display,
                         "identity_id": None}

    return list(pool.values())


def _person(entry: dict) -> dict:
    """The bounded shape a candidate takes in a tool result."""
    return {"identity_id": entry.get("identity_id"),
            "display_name": entry.get("display_name")}


def _resolve_person(db, name: str,
                    identity_index: Optional[List[dict]] = None) -> dict:
    """Resolve a name to ONE person, several candidates, or nobody.

    Returns a decided `status`, not a bare list, because the caller's next
    move differs completely between the three: continue, ask, or say so. A
    unique match is what lets a turn proceed without a needless question,
    and more than one match must NEVER be silently narrowed to the first —
    tracking the wrong person is worse than asking which one.

    Matching is done in PYTHON against a bounded pool: execute_query binds no
    parameters, so putting the user's text in the SQL would mean building a
    query out of untrusted input.

    `matches`/`count`/`note` are kept alongside the new fields; they are what
    the model has been reading, and the older tests pin them.
    """
    needle = (name or "").strip()
    key = match_key(needle)
    if not key:
        return {"status": "not_found", "query": needle, "matches": [],
                "count": 0, "note": "no name was given; ask the user"}

    pool = _name_pool(db, identity_index)

    # Exact key first, so "Ali" prefers a person actually called Ali over
    # every name that merely contains it. Then substring ("ali" ->
    # "Ali Abbass"), then fuzzy for genuine misspellings ("Jeoy" -> "JOEY").
    hits = [e for e in pool if e["key"] == key]
    if not hits:
        hits = [e for e in pool if key in e["key"]]
    if not hits:
        from difflib import get_close_matches
        by_key = {e["key"]: e for e in pool}
        hits = [by_key[k] for k in get_close_matches(
            key, list(by_key), n=_MAX_NAME_MATCHES, cutoff=0.6)]

    hits = hits[:_MAX_NAME_MATCHES]
    names = [e["display_name"] for e in hits]

    if not hits:
        return {"status": "not_found", "query": needle, "matches": [],
                "count": 0,
                "note": ("no known person matches that name; ask the user "
                         "rather than guessing")}

    if len(hits) == 1:
        return {"status": "resolved", "query": needle,
                "identity": _person(hits[0]),
                "matches": names, "count": 1,
                "note": "resolved; use this exact name"}

    return {"status": "ambiguous", "query": needle,
            "candidates": [_person(e) for e in hits],
            "matches": names, "count": len(names),
            "note": ("several people match; ask which one — do not pick for "
                     "them")}


def _get_task_state(dialogue_state: Optional[dict]) -> dict:
    """What the agent currently believes the task is.

    Lets the model resolve "there" / "the same" / "go back" by READING
    committed state rather than re-deriving it from message fragments.
    """
    from .. import dialogue_state as ds
    state = ds.migrate_state(dialogue_state)
    fields = {}
    for name in (state.get("fields") or {}):
        fields[name] = {"value": ds.get_value(state, name),
                        "set_by": (ds.get_provenance(state, name) or {}).get("source")}
    return {
        "task_state": fields,
        "earlier_tasks": ds.list_task_history(state)[-3:],
        "context_version": state.get("context_version", 0),
        "note": ("this is the authoritative task state; prefer it over your "
                 "own recollection of the conversation"),
    }


def _list_documents(artifact_index: Optional[List[dict]]) -> dict:
    """The caller's recent documents — ids, titles, languages. No content.

    The index was built by an owner-scoped query in the API layer, so this
    physically cannot list another user's document.
    """
    documents = [
        {"document_id": entry.get("artifact_id"), "title": entry.get("title"),
         "language": entry.get("language"), "type": entry.get("type")}
        for entry in (artifact_index or [])[:5]
    ]
    return {"documents": documents, "count": len(documents),
            "note": "use these ids exactly; never invent one"}
