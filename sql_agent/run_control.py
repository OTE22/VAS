"""One bounded execution context shared by graph nodes, tools and providers.

Only categorical metadata enters telemetry. Prompts, rows and private reasoning
never enter the event envelope. Cancellation is cooperative; provider/SQL socket
timeouts remain responsible for an operation already in flight.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import json
import logging
import threading
import time
import uuid

from config import settings

logger = logging.getLogger(__name__)
current_run = ContextVar("sql_agent_run", default=None)
operation_deadline = ContextVar("sql_agent_operation_deadline", default=None)
PROMPT_VERSION = "sql-agent-2026-09-05.2"


class RunStopped(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass
class RunControl:
    run_id: str
    cancel_event: object = None
    started: float = field(default_factory=time.monotonic)
    model_calls: int = 0
    tokens: int = 0
    tool_calls: int = 0
    cost: float = 0.0
    outcome: str = "completed"
    lock: object = field(default_factory=threading.Lock, repr=False)

    def remaining(self):
        return max(0.0, float(settings.SQL_AGENT_TOTAL_TIMEOUT)
                   - (time.monotonic() - self.started))

    def check(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise RunStopped("CANCELLED")
        if self.remaining() <= 0:
            raise RunStopped("DEADLINE_EXCEEDED")
        if self.tokens >= settings.SQL_AGENT_MAX_RUN_TOKENS:
            raise RunStopped("TOKEN_BUDGET_EXHAUSTED")

    def reserve(self, kind):
        with self.lock:
            self.check()
            attr, limit = (("model_calls", settings.SQL_AGENT_MAX_MODEL_CALLS)
                           if kind == "model" else
                           ("tool_calls", settings.SQL_AGENT_MAX_TOOL_CALLS))
            if getattr(self, attr) >= limit:
                raise RunStopped(kind.upper() + "_BUDGET_EXHAUSTED")
            setattr(self, attr, getattr(self, attr) + 1)

    def usage(self, tokens, cost):
        with self.lock:
            self.tokens += max(0, int(tokens))
            self.cost += max(0.0, float(cost))


def event(kind, **fields):
    """Closed field allowlist: unknown fields (including raw content) are dropped."""
    allowed = {"node", "tool", "model", "provider", "status", "reason_code",
               "duration_ms", "attempt", "tokens", "cost", "count",
               "source_ids", "index_version", "method"}
    run = current_run.get()
    payload = {"event": kind, "run_id": run.run_id if run else None,
               "prompt_version": PROMPT_VERSION}
    payload.update({k: v for k, v in fields.items() if k in allowed})
    logger.info("%s", json.dumps(payload, ensure_ascii=True, default=str))


def remaining_seconds(default):
    run = current_run.get()
    if run:
        run.check()
        default = min(default, run.remaining())
    deadline = operation_deadline.get()
    if deadline is not None:
        default = min(default, deadline - time.monotonic())
    if default <= 0:
        raise RunStopped("DEADLINE_EXCEEDED")
    return max(0.001, default)


@contextmanager
def run_scope(cancel_event=None):
    from utils.logging import request_id_var
    inherited = request_id_var.get()
    run_id = getattr(cancel_event, "agent_request_id", None)
    run = RunControl(run_id or (inherited if inherited != "-" else uuid.uuid4().hex),
                     cancel_event=cancel_event)
    token = current_run.set(run)
    log_token = request_id_var.set(run.run_id)
    status = "completed"
    event("run_started", status="running")
    try:
        yield run
    except BaseException as exc:
        status = "cancelled" if isinstance(exc, GeneratorExit) else "failed"
        event("run_error", status=status,
              reason_code=getattr(exc, "code", type(exc).__name__))
        raise
    finally:
        if status == "completed":
            status = run.outcome
        elapsed = time.monotonic() - run.started
        event("run_finished", status=status, duration_ms=round(elapsed * 1000, 2),
              tokens=run.tokens, cost=run.cost, count=run.model_calls)
        from .observability import observe_run
        observe_run(status, elapsed, run.tokens, run.cost)
        request_id_var.reset(log_token)
        current_run.reset(token)


def controlled(stream=False):
    def decorate(fn):
        if stream:
            @wraps(fn)
            def streaming(self, user_input, learn=True, cancel_event=None):
                with run_scope(cancel_event) as run:
                    for item in fn(self, user_input, learn, cancel_event):
                        if item.get("type") == "error" or item.get("success") is False:
                            run.outcome = "failed"
                        yield item
                    if cancel_event is not None and cancel_event.is_set():
                        run.outcome = "cancelled"
            return streaming
        @wraps(fn)
        def invoke(self, user_input, learn=True, cancel_event=None):
            with run_scope(cancel_event) as run:
                result = fn(self, user_input, learn, cancel_event)
                if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
                    if result[1].get("turn_failed") or result[1].get("security_block_user"):
                        run.outcome = "failed"
                return result
        return invoke
    return decorate


def traced_node(name, fn):
    @wraps(fn)
    def invoke(state):
        run = current_run.get()
        if run:
            run.check()
        started = time.monotonic()
        event("node_started", node=name, status="running")
        try:
            result = fn(state)
            if run:
                run.check()
        except BaseException as exc:
            if run:
                run.outcome = "cancelled" if getattr(exc, "code", None) == "CANCELLED" else "failed"
            event("node_finished", node=name, status="failed",
                  reason_code=getattr(exc, "code", type(exc).__name__),
                  duration_ms=round((time.monotonic() - started) * 1000, 2))
            raise
        if run and (result.get("turn_failed") or result.get("security_block_user")):
            run.outcome = "failed"
        event("node_finished", node=name,
              status="failed" if result.get("turn_failed") else "completed",
              duration_ms=round((time.monotonic() - started) * 1000, 2))
        return result
    return invoke
