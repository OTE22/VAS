"""SQL candidate parsing and structural validation tools.

These tools never execute SQL. Extraction preserves SQL literal bytes; AST
policy enforcement and canonicalization happen in the authorization stage.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

try:
    import sqlglot
except ImportError:  # pragma: no cover - deployment requires sqlglot
    sqlglot = None


def _json_objects(text: str) -> List[dict]:
    """Every top-level decodable JSON object in the text, in order.

    `strict=False`: a model that lays its SQL out on several lines INSIDE
    the JSON string emits raw newlines there, which strict JSON forbids.
    Refusing the whole envelope for that turned a correct query into "Could
    not extract SQL" three times in a row and a turn that never ran.
    """
    decoder = json.JSONDecoder(strict=False)
    # `\'` is how the SQL model escapes the quotes around a filter value -
    # LIKE LOWER(\'%MD5AL%\') - and it is not a JSON escape at all. Seen
    # live: a correct query refused as "Could not extract SQL" three times.
    # A single quote never needs escaping in JSON, so dropping the backslash
    # cannot change the meaning of a valid envelope.
    for candidate in (text, text.replace("\\'", "'")):
        found: List[dict] = []
        index = 0
        while index < len(candidate):
            if candidate[index] != "{":
                index += 1
                continue
            try:
                value, end = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                index += 1
                continue
            if isinstance(value, dict):
                found.append(value)
            # Skip past the object: its nested braces are not new candidates.
            index += max(end, 1)
        if found:
            return found
    return []


def _json_object(text: str) -> Optional[dict]:
    """The envelope to act on: the LAST object that carries SQL, else the first.

    A repair prompt says "produce a DIFFERENT query"; the model sometimes
    answers with the rejected object first and the corrected one after it.
    Taking the first object re-submitted the rejected SQL three times in a
    row and the turn failed with the fix sitting unread in the same message
    (Opik trace 01a074f1-6a3a-7daf-b25f-e1d440a006b0, 2026-09-06). A
    compliant response has exactly one object, for which this is identical.
    """
    objects = _json_objects(text)
    if not objects:
        return None
    for value in reversed(objects):
        for key in ("sql", "fixed_sql"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value
    return objects[0]


def _fenced_body(text: str) -> Optional[str]:
    match = re.search(r"```(?:json|sql)?\s*([\s\S]*?)\s*```", text,
                      re.IGNORECASE)
    return match.group(1) if match else None


def _raw_sql(text: str) -> str:
    """Conservative fallback for models that returned bare SQL."""
    candidate = (_fenced_body(text) or text).strip()
    if re.match(r"^(SELECT|WITH|EXPLAIN)\b", candidate, re.IGNORECASE):
        return candidate
    return ""


@tool
def prepare_sql_from_llm_response(llm_response: str) -> Dict[str, Any]:
    """Extract an SQL candidate from a structured model response.

    Accepts ``sql`` (generation/modification) and ``fixed_sql`` (repair).
    JSON decoding is the only unescaping performed. The SQL itself is not
    whitespace-normalized or rewritten, because doing that with regexes can
    alter quoted names and filter values.
    """
    transformations = []
    if not isinstance(llm_response, str) or not llm_response.strip():
        return {
            "success": False, "sql": "", "purpose": "",
            "transformations": transformations,
            "error": "Model response was empty",
        }

    text = llm_response.strip()
    payload = _json_object(text)
    sql = ""
    purpose = ""
    if payload is not None:
        value = payload.get("sql")
        if value in (None, ""):
            value = payload.get("fixed_sql")
        if isinstance(value, str):
            sql = value
        purpose_value = payload.get("purpose") or payload.get("error") or ""
        if isinstance(purpose_value, str):
            purpose = purpose_value.strip()
        transformations.append("Parsed structured JSON response")
    else:
        sql = _raw_sql(text)
        if sql:
            transformations.append("Accepted bare SQL response")

    if not sql:
        return {
            "success": False, "sql": "", "purpose": purpose,
            "transformations": transformations,
            "error": purpose or "Could not extract SQL from model response",
        }

    # Removing a single statement terminator is semantics-preserving. An
    # internal semicolon remains and is rejected as multiple statements by
    # the AST policy stage.
    sql = sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()
        transformations.append("Removed trailing statement terminator")

    return {
        "success": True,
        "sql": sql,
        "purpose": purpose,
        "transformations": transformations,
        "error": "",
    }


@tool
def validate_sql_query(sql_query: str) -> Dict[str, Any]:
    """Parse one SQL candidate for repair decisions; never authorize it.

    Table/function access, read-only enforcement, complexity bounds, and row
    caps belong to ``sql_guard.validate_sql``. This tool only answers whether
    a candidate is structurally parseable so malformed output can be repaired.
    """
    if not isinstance(sql_query, str) or not sql_query.strip():
        return {"is_valid": False, "errors": ["No SQL was produced"],
                "warnings": []}
    if sqlglot is None:
        return {"is_valid": False,
                "errors": ["SQL parser is unavailable"], "warnings": []}
    try:
        statements = [statement for statement in
                      sqlglot.parse(sql_query, dialect="postgres")
                      if statement is not None]
    except Exception as exc:
        return {"is_valid": False,
                "errors": [f"SQL parse error: {type(exc).__name__}"],
                "warnings": []}
    if len(statements) != 1:
        return {"is_valid": False,
                "errors": ["Exactly one SQL statement is required"],
                "warnings": []}
    return {"is_valid": True, "errors": [], "warnings": []}
