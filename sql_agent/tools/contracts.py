"""Versioned internal tool contracts; no remote MCP service is required."""
from dataclasses import dataclass
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "error"]
    tool: str
    version: Literal["1.0"] = "1.0"
    data: dict = Field(default_factory=dict)
    error_code: Literal["INVALID_ARGUMENTS", "INVALID_RESULT", "TIMEOUT",
                        "DEPENDENCY_UNAVAILABLE", "PERMISSION_DENIED"] | None = None


@dataclass(frozen=True)
class ToolPolicy:
    version: str = "1.0"
    permission: str = "chatbot_access"
    scope: str = "caller"
    side_effect: str = "read_only"
    timeout_seconds: float = 30.0
    max_retries: int = 0
    idempotent: bool = True
    audit: bool = True
    confirmation_required: bool = False


def policy(name):
    from .tool_registry import ALL_TOOLS
    if name not in ALL_TOOLS:
        raise ValueError("UNKNOWN_TOOL")
    if name in ("generate_document", "translate_document"):
        return ToolPolicy(side_effect="artifact", idempotent=False,
                          timeout_seconds=60)
    if name == "update_task_state":
        return ToolPolicy(side_effect="conversation_state", idempotent=False)
    return ToolPolicy()


def sanitize_data(value, depth=0):
    """Keep JSON data bounded and remove credential-bearing fields recursively.

    Text remains untrusted data, never promoted to system instructions. This
    is containment, not a claim that injection can be detected by word lists.
    """
    if depth > 5:
        return None
    if isinstance(value, dict):
        secret_fields = {"password", "secret", "token", "api_key", "authorization",
                         "cookie", "embedding", "embeddings", "credentials"}
        return {str(k)[:80]: sanitize_data(v, depth + 1)
                for k, v in list(value.items())[:30]
                if not any(word in str(k).lower() for word in secret_fields)}
    if isinstance(value, (list, tuple)):
        return [sanitize_data(v, depth + 1) for v in value[:25]]
    if isinstance(value, str):
        from utils.logging import SensitiveDataFilter
        for pattern, replacement in SensitiveDataFilter.PATTERNS:
            value = pattern.sub(replacement, value)
        return value[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


_RESULT_FIELDS = {
    "list_cameras": ("cameras", list),
    "resolve_person": ("status", str),
    "get_task_state": ("task_state", dict),
    "list_my_documents": ("documents", list),
}


def validate_result(name, result):
    """Validate the executor envelope before any observation reaches a model."""
    key, kind = _RESULT_FIELDS.get(name, (None, None))
    if not isinstance(result, dict):
        return {"error": "Tool returned an invalid result", "error_code": "INVALID_RESULT"}
    if result.get("error"):
        code = result.get("error_code", "DEPENDENCY_UNAVAILABLE")
        if code not in ("TIMEOUT", "INVALID_ARGUMENTS", "PERMISSION_DENIED",
                        "DEPENDENCY_UNAVAILABLE", "INVALID_RESULT"):
            code = "DEPENDENCY_UNAVAILABLE"
        return {"error": "Tool could not complete the lookup", "error_code": code}
    if key is None or not isinstance(result.get(key), kind):
        return {"error": "Tool returned an invalid result", "error_code": "INVALID_RESULT"}
    if name == "resolve_person" and result["status"] not in (
            "resolved", "ambiguous", "not_found"):
        return {"error": "Tool returned an invalid result", "error_code": "INVALID_RESULT"}
    clean = sanitize_data(result)
    ToolResult(status="ok", tool=name, data=clean)
    return clean


def manifest():
    from dataclasses import asdict
    from .tool_registry import tool_specs
    return [{**spec["function"], "policy": asdict(policy(spec["function"]["name"])),
             "output_schema": ToolResult.model_json_schema()}
            for spec in tool_specs()]
