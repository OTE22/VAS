"""SQL candidate parsing and structural validation tools.

These tools never execute SQL. Extraction preserves SQL literal bytes; AST
policy enforcement and canonicalization happen in the authorization stage.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from langchain_core.tools import tool

try:
    import sqlglot
except ImportError:  # pragma: no cover - deployment requires sqlglot
    sqlglot = None


def _json_object(text: str) -> Optional[dict]:
    """Return the first decodable JSON object without a greedy regex."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


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
