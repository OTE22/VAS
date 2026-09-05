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


class Camera(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str | None = None
    camera: str | None = None
    location: str | None = None
    active: bool | None = None


class Cameras(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    cameras: list[Camera] = Field(max_length=25)
    count: int = Field(default=0, ge=0, le=25)
    note: str = ""


class PersonResult(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    status: Literal["resolved", "ambiguous", "not_found"]
    query: str = ""
    matches: list[str] = Field(default_factory=list, max_length=5)
    count: int = Field(default=0, ge=0, le=5)
    identity: dict | None = None
    candidates: list[dict] = Field(default_factory=list, max_length=5)
    note: str = ""


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    task_state: dict
    earlier_tasks: list[dict] = Field(default_factory=list, max_length=3)
    context_version: int = Field(default=0, ge=0)
    note: str = ""


class Document(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    document_id: str | None = None
    title: str | None = None
    language: str | None = None
    type: str | None = None


class Documents(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    documents: list[Document] = Field(max_length=5)
    count: int = Field(default=0, ge=0, le=5)
    note: str = ""


class ActionObservation(BaseModel):
    """Graph-produced output, not a model's promise that an action succeeded."""
    model_config = ConfigDict(extra="allow", strict=True)
    action: str
    success: bool
    error_type: str | None = None
    row_count: int | None = Field(default=None, ge=0)
    artifact_id: str | None = None
    result_id: int | None = None
    retryable: bool = False


OUTPUTS = {"list_cameras": Cameras, "resolve_person": PersonResult,
           "get_task_state": TaskResult, "list_my_documents": Documents}


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
    try:
        return OUTPUTS[name].model_validate(sanitize_data(result)).model_dump(exclude_none=True)
    except (ValidationError, KeyError):
        return {"error": "Tool returned an invalid result", "error_code": "INVALID_RESULT"}


def manifest():
    from dataclasses import asdict
    from .tool_registry import tool_specs
    return [{**spec["function"], "policy": asdict(policy(spec["function"]["name"])),
             "output_schema": OUTPUTS.get(spec["function"]["name"], ActionObservation).model_json_schema(),
             "error_schema": ToolResult.model_json_schema()}
            for spec in tool_specs()]
