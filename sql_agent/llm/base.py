"""Provider-independent LLM interfaces.

The agent previously constructed `ChatOllama` directly in two factory
functions, so the provider was welded into the call path: swapping it meant
editing every site, there was no place to put timeouts, retries or token
accounting, and nothing recorded which model answered a given request.

The seam is deliberately narrow. Adapters return a LangChain-compatible
Runnable because the graph nodes build LCEL chains (`prompt | llm | parser`)
and call `.invoke()` / `.stream()` on them — rewriting all seven call sites to
a bespoke interface would be churn with no safety benefit. What changes is
that construction, capability declaration, routing and instrumentation now
live behind this interface instead of being implicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, FrozenSet, Optional, Protocol, runtime_checkable


class Capability(str, Enum):
    """What a model can do. Routing selects on these rather than on names."""

    STREAMING = "streaming"
    TOOL_CALLING = "tool_calling"
    STRUCTURED_OUTPUT = "structured_output"
    JSON_MODE = "json_mode"
    VISION = "vision"
    REASONING = "reasoning"


class TaskType(str, Enum):
    """Why a model is being asked for. Drives model selection."""

    CHAT = "chat"
    INTENT = "intent"
    NORMALIZE = "normalize"
    SQL_GENERATION = "sql_generation"
    # Rewriting VALID SQL under a new constraint ("the same, but only camera
    # 3"). Deliberately not SQL_REPAIR: repair's prompt exists to fix broken
    # SQL, and overloading it would tell the model the input is wrong when it
    # is not — corrupting both prompts' purpose. Same model preference as
    # generation, different intent.
    SQL_MODIFICATION = "sql_modification"
    SQL_REPAIR = "sql_repair"
    EXPLANATION = "explanation"


class DataSensitivity(str, Enum):
    """How sensitive the prompt content is.

    This deployment queries biometric data, so a model that would send prompt
    text off-box must never be selected for RESTRICTED work. Enforced in the
    registry, not left to a comment.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


@dataclass(frozen=True)
class ModelSpec:
    """A model the deployment is allowed to use."""

    provider: str
    model_id: str
    display_name: str
    capabilities: FrozenSet[Capability]
    context_tokens: int
    # Local models cost nothing per token; the fields exist so a hosted
    # provider can be added without changing the accounting code.
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0
    max_sensitivity: DataSensitivity = DataSensitivity.RESTRICTED
    timeout_seconds: float = 120.0
    max_retries: int = 2
    available: bool = True

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def permits(self, sensitivity: DataSensitivity) -> bool:
        """Whether this model may see content of the given sensitivity."""
        order = {
            DataSensitivity.PUBLIC: 0,
            DataSensitivity.INTERNAL: 1,
            DataSensitivity.RESTRICTED: 2,
        }
        return order[sensitivity] <= order[self.max_sensitivity]


@dataclass
class TokenUsage:
    """Token counts and derived cost for one request."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def cost(self, spec: ModelSpec) -> float:
        return (
            self.prompt_tokens / 1000.0 * spec.input_cost_per_1k
            + self.completion_tokens / 1000.0 * spec.output_cost_per_1k
        )


@dataclass
class LLMCallRecord:
    """What actually happened on one model call.

    Recorded per call so a request can be attributed to a specific model and
    so a fallback is never silent — which model answered is data, not an
    inference from logs.
    """

    provider: str
    model_id: str
    task: str
    duration_seconds: float = 0.0
    usage: TokenUsage = field(default_factory=TokenUsage)
    succeeded: bool = True
    error_type: Optional[str] = None
    attempts: int = 1
    fell_back_from: Optional[str] = None
    run_id: Optional[str] = None
    estimated_cost: float = 0.0


class ProviderUnavailable(RuntimeError):
    """The provider could not serve the request (down, refused, timed out)."""


@runtime_checkable
class LLMProvider(Protocol):
    """What every provider adapter must offer.

    `build` returns a LangChain-compatible Runnable so existing LCEL chains
    keep working unchanged.
    """

    name: str

    def build(self, spec: ModelSpec, **overrides: Any) -> Any:
        """Return a Runnable chat model for `spec`."""

    def health_check(self) -> bool:
        """Cheap liveness probe. Must not raise."""
