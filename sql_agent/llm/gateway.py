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
        from ..run_control import current_run, event
        run = current_run.get()
        entry.run_id = run.run_id if run else None
        event("model_finished", provider=entry.provider, model=entry.model_id,
              status="ok" if entry.succeeded else "error",
              reason_code=entry.error_type, attempt=entry.attempts,
              duration_ms=round(entry.duration_seconds * 1000, 2),
              tokens=entry.usage.total_tokens, cost=entry.estimated_cost)
        if run:
            run.usage(entry.usage.total_tokens, entry.estimated_cost)
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

    # Fraction of the outer streaming deadline that all LLM attempts for a single
    # node may consume. Below 1.0 so the agent still has room to validate SQL,
    # run it and compose an answer after the model returns — a model allowed to
    # eat the entire deadline produces a timeout with nothing to show for it,
    # which is exactly the reported `response_chars=0`.
    LLM_BUDGET_FRACTION = 0.6

    def total_budget_seconds(self, spec: ModelSpec) -> float:
        """Total wall-clock all attempts for one call may take.

        Derived from the SQL-agent streaming deadline so the two cannot drift
        apart: raising SQL_AGENT_TOTAL_TIMEOUT widens this automatically, and
        lowering it tightens this rather than silently inverting the hierarchy.
        """
        # Read the declared field directly. The old shape read it through a
        # getattr default of 0, then substituted a literal 300.0 "matching the
        # SQL_AGENT_TOTAL_TIMEOUT default" — a second declaration of the same
        # setting that would silently diverge the moment the default moved.
        from config import settings as _settings
        outer = float(_settings.SQL_AGENT_TOTAL_TIMEOUT)
        # Never below a single attempt: a budget that cannot fit one call would
        # fail every request before the model was even given a chance.
        return max(spec.timeout_seconds, outer * self.LLM_BUDGET_FRACTION)

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
            remaining = candidates[candidates.index(spec) + 1:]
            return _InstrumentedModel(model, spec, task, self,
                                      fallbacks=remaining, overrides=overrides)

        raise ProviderUnavailable(
            f"Every candidate for task={task.value} failed: {'; '.join(errors)}"
        )

    def call_with_retries(self, spec: ModelSpec, task: TaskType, fn: Callable[[], Any],
                          *, deadline=None, fell_back_from=None) -> Any:
        """Run `fn`, retrying transient failures with jittered backoff.

        Retries are bounded by a TOTAL budget, not just a count. With
        max_retries=2 and a 120s per-call timeout, a naive count-only policy
        permits 3 x 120s = 360s — longer than the 300s streaming deadline that
        wraps it, so the caller would be cancelled mid-retry and the work
        discarded. A retry is therefore skipped when there is not enough budget
        left for it to plausibly finish.
        """
        key = f"{spec.provider}/{spec.model_id}"
        started = time.monotonic()
        last_error: Optional[Exception] = None
        budget = self.total_budget_seconds(spec)
        from ..run_control import current_run, RunStopped, event
        run = current_run.get()

        for attempt in range(spec.max_retries + 1):
            if run:
                run.reserve("model")
            if deadline is not None and time.monotonic() >= deadline:
                raise ProviderUnavailable("MODEL_DEADLINE_EXCEEDED")
            try:
                result = fn()
            except RunStopped:
                raise
            except Exception as e:
                last_error = e
                self.breaker.record_failure(key)
                elapsed = time.monotonic() - started
                if attempt < spec.max_retries:
                    delay = _backoff_delay(attempt)
                    # Only retry if another full attempt could still fit.
                    remaining = budget - elapsed
                    if deadline is not None:
                        remaining = min(remaining, deadline - time.monotonic())
                    if run:
                        remaining = min(remaining, run.remaining())
                    # Invalid/authentication requests cannot be repaired by
                    # repeating them against the same provider.
                    status_code = getattr(getattr(e, "response", None), "status_code", None)
                    permanent = status_code in (400, 401, 403, 404, 422)
                    if permanent or delay + spec.timeout_seconds > remaining:
                        logger.warning(
                            "[LLM] %s attempt %d/%d failed (%s); no retry — "
                            "%.0fs elapsed of %.0fs budget",
                            key, attempt + 1, spec.max_retries + 1,
                            type(e).__name__, elapsed, budget,
                        )
                    else:
                        event("model_retry", provider=spec.provider, model=spec.model_id,
                              attempt=attempt + 1, reason_code="TRANSIENT_PROVIDER_ERROR")
                        logger.warning(
                            "[LLM] %s attempt %d/%d failed (%s); retrying in %.2fs",
                            key, attempt + 1, spec.max_retries + 1, type(e).__name__, delay,
                        )
                        if run and run.cancel_event is not None:
                            run.cancel_event.wait(delay)
                            run.check()
                        else:
                            time.sleep(delay)
                        continue
                self.ledger.record(LLMCallRecord(
                    provider=spec.provider, model_id=spec.model_id, task=task.value,
                    duration_seconds=time.monotonic() - started,
                    succeeded=False, error_type=type(e).__name__, attempts=attempt + 1,
                    fell_back_from=fell_back_from,
                ))
                raise

            self.breaker.record_success(key)
            self.ledger.record(LLMCallRecord(
                provider=spec.provider, model_id=spec.model_id, task=task.value,
                duration_seconds=time.monotonic() - started,
                usage=_usage_from_response(result), attempts=attempt + 1,
                estimated_cost=_usage_from_response(result).cost(spec),
                fell_back_from=fell_back_from,
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

    def __init__(self, inner: Any, spec: ModelSpec, task: TaskType, gateway: LLMGateway,
                 *, fallbacks=None, overrides=None):
        self._inner = inner
        self._spec = spec
        self._task = task
        self._gateway = gateway
        self._fallbacks = tuple(fallbacks or ())
        self._overrides = dict(overrides or {})

    # -- the two methods the agent actually calls -------------------------

    def invoke(self, input=None, config=None, **kwargs):
        from ..run_control import current_run, RunStopped, event
        run = current_run.get()
        budget = self._gateway.total_budget_seconds(self._spec)
        if run:
            run.check()
            budget = min(budget, run.remaining())
        deadline = time.monotonic() + budget
        last_error = None
        for index, spec in enumerate((self._spec,) + self._fallbacks):
            if time.monotonic() >= deadline:
                break
            if self._gateway.breaker.is_open(f"{spec.provider}/{spec.model_id}"):
                continue
            try:
                inner = (self._inner if index == 0 else
                         self._gateway.providers[spec.provider].build(spec, **self._overrides))
                if index:
                    event("model_fallback", provider=spec.provider, model=spec.model_id,
                          reason_code="PROVIDER_UNAVAILABLE")
                return self._gateway.call_with_retries(
                    spec, self._task,
                    lambda: self._invoke_attempt(inner, spec, input, config, kwargs, deadline),
                    deadline=deadline,
                    fell_back_from=self._spec.model_id if index else None)
            except RunStopped:
                raise
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise ProviderUnavailable("MODEL_DEADLINE_EXCEEDED")

    def _invoke_attempt(self, inner, spec, input, config, kwargs, deadline):
        from ..run_control import operation_deadline
        previous = operation_deadline.get()
        token = operation_deadline.set(min(previous, deadline) if previous else deadline)
        try:
            return self._bounded_inner(inner, spec).invoke(input, config=config, **kwargs)
        finally:
            operation_deadline.reset(token)

    def _bounded_inner(self, inner, spec):
        from ..run_control import current_run, remaining_seconds
        if current_run.get() and spec.provider == "ollama":
            # New per-call client: never mutate another user's shared Runnable.
            overrides = dict(self._overrides)
            overrides["timeout"] = remaining_seconds(float(overrides.get("timeout", spec.timeout_seconds)))
            return self._gateway.providers[spec.provider].build(spec, **overrides)
        return inner

    def stream(self, input=None, config=None, **kwargs):
        yield from self._stream_instrumented(input, config, **kwargs)

    def _stream_instrumented(self, *args, **kwargs):
        # Not retried: chunks may already have reached the client, so a retry
        # would duplicate visible output. Failures are recorded instead.
        started = time.monotonic()
        key = f"{self._spec.provider}/{self._spec.model_id}"
        from ..run_control import current_run, RunStopped
        run = current_run.get()
        if run:
            run.reserve("model")
        usage = TokenUsage()
        output_bytes = 0
        succeeded = False
        error_type = None
        try:
            for chunk in self._bounded_inner(self._inner, self._spec).stream(*args, **kwargs):
                measured = _usage_from_response(chunk)
                output_bytes += len(str(getattr(chunk, "content", "")).encode("utf-8"))
                usage.prompt_tokens = max(usage.prompt_tokens, measured.prompt_tokens)
                usage.completion_tokens = max(usage.completion_tokens, measured.completion_tokens)
                if run:
                    run.check()
                    from config import settings
                    # A provider without stream usage still cannot emit forever.
                    if run.tokens + max(usage.total_tokens, (output_bytes + 2) // 3) >= settings.SQL_AGENT_MAX_RUN_TOKENS:
                        raise RunStopped("TOKEN_BUDGET_EXHAUSTED")
                yield chunk
            succeeded = True
            self._gateway.breaker.record_success(key)
        except RunStopped as e:
            error_type = e.code
            raise
        except Exception as e:
            self._gateway.breaker.record_failure(key)
            error_type = type(e).__name__
            raise
        finally:
            if not usage.completion_tokens:
                usage.completion_tokens = (output_bytes + 2) // 3
            self._gateway.ledger.record(LLMCallRecord(
                provider=self._spec.provider, model_id=self._spec.model_id,
                task=self._task.value, duration_seconds=time.monotonic() - started,
                succeeded=succeeded, error_type=error_type, usage=usage,
                estimated_cost=usage.cost(self._spec),
            ))

    # -- everything else passes through -----------------------------------

    def __getattr__(self, item):
        # Only reached for attributes this class does not define, so Runnable's
        # own machinery keeps working while model-specific attributes fall
        # through to the wrapped instance.
        return getattr(self._inner, item)
