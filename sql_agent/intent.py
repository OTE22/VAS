"""Intent labels for a turn, derived from the reading - observable, testable.

The model reads each message into a typed structure (``interpreter.py``:
``wants``, ``shape``, ``format`` ...) and the loop commits a validated action.
Those are the facts; this module only NAMES them with the five labels the
platform reports on, so a dashboard, an audit line and a test can all say
"this turn was routed as ANALYTICS_QUERY" without re-deriving anything from
the user's words.

Labels: CHAT, DATABASE_QUERY, ANALYTICS_QUERY, REPORT_REQUEST,
SYSTEM_TOOL_REQUEST.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

CHAT = "CHAT"
DATABASE_QUERY = "DATABASE_QUERY"
ANALYTICS_QUERY = "ANALYTICS_QUERY"
REPORT_REQUEST = "REPORT_REQUEST"
SYSTEM_TOOL_REQUEST = "SYSTEM_TOOL_REQUEST"
INTENTS = (CHAT, DATABASE_QUERY, ANALYTICS_QUERY, REPORT_REQUEST, SYSTEM_TOOL_REQUEST)

_DOCUMENT_ACTIONS = {"generate_document", "translate_artifact", "translate_document"}
_DATA_ACTIONS = {"query_database", "modify_previous_query", "modify_active_query"}
_SYSTEM_TOOLS = {"list_cameras", "get_task_state", "list_my_documents", "update_task_state"}


def intent_of(interpretation: Optional[Dict[str, Any]],
              planned_action: Optional[Dict[str, Any]],
              *, tools_called: Optional[List[str]] = None) -> str:
    """The turn's label from its reading and its committed action."""
    reading = interpretation or {}
    plan = planned_action or {}
    action = str(plan.get("action") or "")
    wants = str(reading.get("wants") or "")
    shape = str(reading.get("shape") or "")
    if action in _DOCUMENT_ACTIONS or wants in ("document", "translation") or reading.get("format"):
        return REPORT_REQUEST
    if action in _DATA_ACTIONS or wants == "data":
        # A computed figure, ranking, comparison or share is analytics; every
        # row about a subject, or one stored fact, is a database query.
        return ANALYTICS_QUERY if shape == "summary" else DATABASE_QUERY
    called = set(tools_called or [])
    if called and called <= _SYSTEM_TOOLS:
        return SYSTEM_TOOL_REQUEST
    return CHAT


def question_hash(text: str) -> str:
    """An approved representation of the question for audit lines: a stable
    hash, never the words (they may name people under surveillance)."""
    return hashlib.sha256(" ".join(str(text or "").split()).lower().encode("utf-8")).hexdigest()[:16]


def tables_in(sql: str) -> List[str]:
    """Tables a statement reads, from the AST; [] when it cannot be parsed."""
    if not sql:
        return []
    try:
        import sqlglot
        from sqlglot import expressions as exp
        tree = sqlglot.parse_one(sql, read="postgres")
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        names = sorted({t.name.lower() for t in tree.find_all(exp.Table)
                        if t.name and t.name.lower() not in ctes})
        return names
    except Exception:
        return []


def turn_facts(state: Dict[str, Any]) -> Dict[str, Any]:
    """Everything the audit line needs, taken from the final graph state.

    Carries SQL and its validated form (machine output), never the user's
    words, never rows, never credentials.
    """
    state = state or {}
    observations = state.get("observations") or []
    tools = []
    for o in observations:
        tool = (o or {}).get("tool")
        if tool and tool not in tools:
            tools.append(tool)
    plan = state.get("planned_action") or {}
    if plan.get("action") and plan["action"] not in tools:
        tools.append(plan["action"])
    validated = state.get("validated_sql") or ""
    generated = state.get("generated_sql") or ""
    result = state.get("query_result") or {}
    status = state.get("terminal_state") or (
        "ok" if result.get("success") else ("failed" if generated else "answered"))
    authorization = state.get("sql_validation_status") or ("not_applicable" if not generated else "unknown")
    return {
        "intent": intent_of(state.get("interpretation"), plan, tools_called=tools),
        "tools": tools,
        "tables": tables_in(validated or generated),
        "generated_sql": generated,
        "validated_sql": validated,
        "authorization": str(authorization),
        "row_count": result.get("row_count") if isinstance(result, dict) else None,
        "model": state.get("model_used") or state.get("sql_model") or "",
        "status": str(status),
        "retrieved": len(state.get("retrieved_examples") or []),
    }
