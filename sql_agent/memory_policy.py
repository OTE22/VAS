"""Explicit user memory validation; no preferences inferred from query wording."""
from datetime import datetime, timedelta, timezone
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from config import settings


class MemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_type: Literal["fact", "preference", "context", "pattern"] = "fact"
    memory_key: str = Field(min_length=1, max_length=255)
    memory_value: dict = Field(default_factory=dict)
    importance_score: int = Field(default=50, ge=0, le=100)
    expires_at: datetime | None = None

    @field_validator("memory_key")
    @classmethod
    def key(cls, value):
        if not value.strip():
            raise ValueError("Memory key must not be blank")
        return value.strip()

    @field_validator("memory_value")
    @classmethod
    def value(cls, value):
        from .tools.contracts import sanitize_data
        if len(json.dumps(value, ensure_ascii=False)) > 4096:
            raise ValueError("Memory value is too large")
        if "_provenance" in value or sanitize_data(value) != value:
            raise ValueError("Memory contains reserved, sensitive, or unsupported data")
        return value


def expiry(requested=None, now=None):
    now = now or datetime.utcnow()
    ceiling = now + timedelta(days=settings.SQL_AGENT_MEMORY_RETENTION_DAYS)
    if requested is None:
        return ceiling
    if requested.tzinfo:
        requested = requested.astimezone(timezone.utc).replace(tzinfo=None)
    if requested <= now:
        raise ValueError("Memory expiry must be in the future")
    return min(requested, ceiling)


def expired_session(data, now=None):
    stamp = data.get("updated_at") or data.get("created_at")
    if not stamp:
        return False  # pre-versioned files are upgraded on their next save
    try:
        timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if timestamp.tzinfo:
            timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
        return (now or datetime.utcnow()) - timestamp > timedelta(
            days=settings.SQL_AGENT_MEMORY_RETENTION_DAYS)
    except (TypeError, ValueError):
        return True
