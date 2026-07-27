"""LLM gateway: construction, resilience and accounting in one place.

Previously each graph node called `chain.invoke()` on a directly-constructed
ChatOllama. That meant no retries, no circuit breaking, no record of which
model answered, and no token accounting — a single SQL question issues five to
six sequential model calls, none of which were measured.

The gateway wraps model construction rather than each call, so the LCEL chains
in the graph nodes are unchanged.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .base import (
    Capability,
    DataSensitivity,
    LLMCallRecord,
    LLMProvider,
    ModelSpec,
    ProviderUnavailable,
    TaskType,
    TokenUsage,
)
from .registry import ModelRegistry

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Stops hammering a provider that is already failing.

    Without this, a provider outage turns every request into `max_retries`
    doomed attempts, each waiting out its own timeout — the queue backs up
    far faster than it would if requests simply failed.
    """

    def __init__(self, failure_threshold: int = 5, reset_after_seconds: float = 60.0):
        self.failure_threshold = failure_threshold
        self.reset_after_seconds = reset_after_seconds
        self._failures: Dict[str, int] = {}
        self._opened_at: Dict[str, float] = {}
        self._lock = threading.Lock()

    def is_open(self, key: str) -> bool:
        with self._lock:
            opened = self._opened_at.get(key)
            if opened is None:
                return False
            if time.monotonic() - opened >= self.reset_after_seconds:
                # Half-open: allow one probe through.
                self._opened_at.pop(key, None)
                self._failures[key] = 0
                logger.info("[LLM] Circuit for %s half-open, probing", key)
                return False
            return True

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures[key] = 0
            self._opened_at.pop(key, None)

    def record_failure(self, key: str) -> None:
        with self._lock:
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            if count >= self.failure_threshold and key not in self._opened_at:
                self._opened_at[key] = time.monotonic()
                logger.error(
                    "[LLM] Circuit OPEN for %s after %d consecutive failures",
                    key, count,
                )


class UsageLedger:
    """In-process token and cost totals.

    Deliberately not a database write: this records per-call totals cheaply so
    they can be exported. Durable per-request attribution belongs with the
    conversation persistence work, which is still open.
    """

    def __init__(self, max_records: int = 1000):
        self._records: List[LLMCallRecord] = []
        self._max_records = max_records
        self._lock = threading.Lock()

    def record(self, entry: LLMCallRecord) -> None:
        with self._lock:
            self._records.append(entry)
            if len(self._records) > self._max_records:
                del self._records[: len(self._records) - self._max_records]

    def totals(self) -> Dict[str, Any]:
        with self._lock:
            records = list(self._records)
        return {
            "calls": len(records),
            "failures": sum(1 for r in records if not r.succeeded),
            "fallbacks": sum(1 for r in records if r.fell_back_from),
            "prompt_tokens": sum(r.usage.prompt_tokens for r in records),
            "completion_tokens": sum(r.usage.completion_tokens for r in records),
            "total_seconds": round(sum(r.duration_seconds for r in records), 3),
        }

    def recent(self, limit: int = 20) -> List[LLMCallRecord]:
        with self._lock:
            return list(self._records[-limit:])


def _backoff_delay(attempt: int, base: float = 0.5, cap: float = 8.0) -> float:
    """Exponential backoff with full jitter.

    Jitter matters here: without it, several queued requests retry in lockstep
    and re-synchronise their load on a provider that is already struggling.
    """
    ceiling = min(cap, base * (2 ** attempt))
    return random.uniform(0, ceiling)


class LLMGateway:
    """Selects a model, builds it, and records what happened."""

    def __init__(
        self,
        registry: ModelRegistry,
        providers: Dict[str, LLMProvider],
        *,
        breaker: Optional[CircuitBreaker] = None,
        ledger: Optional[UsageLedger] = None,
    ):
        self.registry = registry
        self.providers = providers
        self.breaker = breaker or CircuitBreaker()
        self.ledger = ledger or UsageLedger()

    def build_for(
        self,
        task: TaskType,
        *,
        sensitivity: DataSensitivity = DataSensitivity.RESTRICTED,
        required: Optional[List[Capability]] = None,
        requested_model: Optional[str] = None,
        **overrides: Any,
    ) -> Any:
        """A runnable chat model for `task`.

        Walks the routing order, skipping providers whose circuit is open, and
        logs a warning whenever it lands on anything but the first choice.
        """
        candidates = self.registry.route(
            task, sensitivity=sensitivity, required=required,
            requested_model=requested_model,
        )
        if not candidates:
            raise ProviderUnavailable(
                f"No model is eligible for task={task.value} at "
                f"sensitivity={sensitivity.value}."
            )

        preferred = candidates[0]
        errors = []

        for spec in candidates:
            provider = self.providers.get(spec.provider)
            if provider is None:
                errors.append(f"{spec.provider}: no adapter registered")
                continue

            key = f"{spec.provider}/{spec.model_id}"
            if self.breaker.is_open(key):
                errors.append(f"{key}: circuit open")
                continue

            try:
                model = provider.build(spec, **overrides)
            except Exception as e:
                self.breaker.record_failure(key)
                errors.append(f"{key}: {type(e).__name__}")
                continue

            if spec.model_id != preferred.model_id:
                # Never silent: a fallback can change answer quality, and for
                # a hosted provider it would change where data goes.
                logger.warning(
                    "[LLM] Task %s fell back from %s to %s",
                    task.value, preferred.model_id, spec.model_id,
                )
            return _InstrumentedModel(model, spec, task, self)

        raise ProviderUnavailable(
            f"Every candidate for task={task.value} failed: {'; '.join(errors)}"
        )

    def call_with_retries(self, spec: ModelSpec, task: TaskType, fn: Callable[[], Any]) -> Any:
        """Run `fn`, retrying transient failures with jittered backoff."""
        key = f"{spec.provider}/{spec.model_id}"
        started = time.monotonic()
        last_error: Optional[Exception] = None

        for attempt in range(spec.max_retries + 1):
            try:
                result = fn()
            except Exception as e:
                last_error = e
                self.breaker.record_failure(key)
                if attempt < spec.max_retries:
                    delay = _backoff_delay(attempt)
                    logger.warning(
                        "[LLM] %s attempt %d/%d failed (%s); retrying in %.2fs",
                        key, attempt + 1, spec.max_retries + 1, type(e).__name__, delay,
                    )
                    time.sleep(delay)
                    continue
                self.ledger.record(LLMCallRecord(
                    provider=spec.provider, model_id=spec.model_id, task=task.value,
                    duration_seconds=time.monotonic() - started,
                    succeeded=False, error_type=type(e).__name__, attempts=attempt + 1,
                ))
                raise

            self.breaker.record_success(key)
            self.ledger.record(LLMCallRecord(
                provider=spec.provider, model_id=spec.model_id, task=task.value,
                duration_seconds=time.monotonic() - started,
                usage=_usage_from_response(result), attempts=attempt + 1,
            ))
            return result

        raise ProviderUnavailable(str(last_error))  # pragma: no cover


def _usage_from_response(response: Any) -> TokenUsage:
    """Best-effort token counts from a LangChain response.

    Providers report usage inconsistently, so this reads the common shapes and
    returns zeros rather than raising when none match — accounting must never
    break a working request.
    """
    try:
        metadata = getattr(response, "usage_metadata", None)
        if isinstance(metadata, dict):
            return TokenUsage(
                prompt_tokens=int(metadata.get("input_tokens", 0) or 0),
                completion_tokens=int(metadata.get("output_tokens", 0) or 0),
            )
        response_metadata = getattr(response, "response_metadata", None) or {}
        if isinstance(response_metadata, dict):
            return TokenUsage(
                prompt_tokens=int(response_metadata.get("prompt_eval_count", 0) or 0),
                completion_tokens=int(response_metadata.get("eval_count", 0) or 0),
            )
    except Exception:
        pass
    return TokenUsage()


try:
    from langchain_core.runnables import Runnable as _RunnableBase
except ImportError:  # pragma: no cover - langchain_core is a pinned dependency
    _RunnableBase = object


class _InstrumentedModel(_RunnableBase):
    """Wraps a chat model so invoke/stream are retried and recorded.

    MUST subclass Runnable. The graph nodes build LCEL chains
    (`prompt | llm | parser`), and LangChain's `coerce_to_runnable` does an
    isinstance check — a duck-typed wrapper with invoke/stream is rejected at
    pipe-construction time with "Expected a Runnable, callable or dict".
    Delegating __or__ is not enough, because the *prompt* on the left builds
    the sequence and inspects the right-hand operand.

    Everything not overridden falls through to the wrapped model, so the graph
    nodes never learn this exists.
    """

    def __init__(self, inner: Any, spec: ModelSpec, task: TaskType, gateway: LLMGateway):
        self._inner = inner
        self._spec = spec
        self._task = task
        self._gateway = gateway

    # -- the two methods the agent actually calls -------------------------

    def invoke(self, input=None, config=None, **kwargs):
        return self._gateway.call_with_retries(
            self._spec, self._task,
            lambda: self._inner.invoke(input, config=config, **kwargs),
        )

    def stream(self, input=None, config=None, **kwargs):
        yield from self._stream_instrumented(input, config, **kwargs)

    def _stream_instrumented(self, *args, **kwargs):
        # Not retried: chunks may already have reached the client, so a retry
        # would duplicate visible output. Failures are recorded instead.
        started = time.monotonic()
        key = f"{self._spec.provider}/{self._spec.model_id}"
        try:
            for chunk in self._inner.stream(*args, **kwargs):
                yield chunk
        except Exception as e:
            self._gateway.breaker.record_failure(key)
            self._gateway.ledger.record(LLMCallRecord(
                provider=self._spec.provider, model_id=self._spec.model_id,
                task=self._task.value, duration_seconds=time.monotonic() - started,
                succeeded=False, error_type=type(e).__name__,
            ))
            raise
        self._gateway.breaker.record_success(key)
        self._gateway.ledger.record(LLMCallRecord(
            provider=self._spec.provider, model_id=self._spec.model_id,
            task=self._task.value, duration_seconds=time.monotonic() - started,
        ))

    # -- everything else passes through -----------------------------------

    def __getattr__(self, item):
        # Only reached for attributes this class does not define, so Runnable's
        # own machinery keeps working while model-specific attributes fall
        # through to the wrapped instance.
        return getattr(self._inner, item)
