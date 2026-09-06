"""Operational metrics for the agent's silent-failure classes.

Every counter here exists because a specific failure was invisible until a
person noticed a wrong answer:

  * provenance: "same report but camera 3" silently bound to recency on SSE
    for as long as nobody compared the SQL by hand. fr_agent_provenance_total
    {source="last_result"} climbing while artifacts exist IS that bug.
  * planner fallbacks: a planner that quietly degrades to the legacy
    classifier still answers — worse. Watch source="legacy"/"fallback".
  * document completions: a rendered document that fails to persist used to
    cost only a missing link; count it.
  * memory failures: a failed durable-memory or artifact-index load downgrades
    reference resolution without any error the user sees.
  * evictions: lock_kept_busy > 0 means the 11+-user lock scenario actually
    happened in production, not just in the regression test.

All increments are best-effort: observability must never fail a turn. Labels
are drawn from closed vocabularies only — never user input — so cardinality
is bounded by construction.
"""

import logging

logger = logging.getLogger(__name__)

_COUNTERS = {}


def _counter(name: str, documentation: str, labelnames):
    """Create-or-reuse, tolerating double registration (worker reload, tests)."""
    counter = _COUNTERS.get(name)
    if counter is not None:
        return counter
    try:
        from prometheus_client import Counter, REGISTRY
        try:
            counter = Counter(name, documentation, labelnames=labelnames)
        except ValueError:
            counter = REGISTRY._names_to_collectors.get(name)
            if counter is None:
                for collector in list(REGISTRY._names_to_collectors.values()):
                    if getattr(collector, "_name", None) == name:
                        counter = collector
                        break
    except Exception as e:  # prometheus missing or broken: metrics off, agent on
        logger.debug("[AGENT_METRICS] unavailable: %s", e)
        counter = None
    _COUNTERS[name] = counter
    return counter


def _inc(name: str, documentation: str, labelnames, labelvalues) -> None:
    try:
        counter = _counter(name, documentation, labelnames)
        if counter is not None:
            counter.labels(*labelvalues).inc()
    except Exception:
        pass


_ACTIONS = {"chat", "query_database", "modify_previous_query",
            "generate_document", "translate_artifact", "clarify", "legacy"}
_SOURCES = {"planner", "deterministic", "fallback", "legacy", "tool_loop", "interpreter",
            "planner+replanned", "deterministic+replanned", "fallback+replanned"}


def observe_planner_action(action: str, source: str) -> None:
    """One per turn: what the planner decided and on which path."""
    _inc("fr_agent_planner_actions_total",
         "Planner decisions by action and resolution source",
         ("action", "source"),
         (action if action in _ACTIONS else "other",
          source if source in _SOURCES else "other"))


def observe_run(status, seconds, tokens, cost):
    """Low-cardinality run totals for the existing Prometheus registry."""
    try:
        from prometheus_client import Histogram, REGISTRY
        name = "fr_agent_run_duration_seconds"
        metric = REGISTRY._names_to_collectors.get(name)
        if metric is None:
            metric = Histogram(name, "Agent run duration", ("status",),
                               buckets=(1, 5, 15, 30, 60, 120, 300, 600))
        metric.labels(status).observe(seconds)
        for name, doc, amount in (
            ("fr_agent_run_tokens_total", "Reported agent tokens", tokens),
            ("fr_agent_run_cost_total", "Estimated agent cost in USD", cost),
        ):
            _counter(name, doc, ("status",)).labels(status).inc(amount)
    except Exception:
        pass


def observe_event(kind, status=None, reason=None):
    # No run ids, user ids, names or arbitrary error strings as metric labels.
    kinds = {"run_started", "run_finished", "run_error", "node_started", "node_finished",
             "tool_selected", "tool_finished", "model_finished", "model_retry",
             "model_fallback", "retrieval_finished", "memory_read", "memory_write",
             "guardrail_intervention"}
    statuses = {"running", "ok", "error", "completed", "failed", "cancelled"}
    if kind in kinds:
        _inc("fr_agent_events_total", "Categorical agent execution events",
             ("event", "status"), (kind, status if status in statuses else "none"))


def observe_provenance(source: str) -> None:
    """Where modify_sql took its base query from: artifact | last_result | none.

    THE fallback-to-recency detector. `last_result` rising while users have
    artifacts means references are binding to recency again.
    """
    _inc("fr_agent_modify_provenance_total",
         "Base-query source for query modifications",
         ("source",),
         (source if source in ("artifact", "last_result", "none") else "other",))


def observe_document_completion(outcome: str) -> None:
    """completed | failed — for every turn that had pending document work."""
    _inc("fr_agent_document_completions_total",
         "Pending-document completion outcomes",
         ("outcome",),
         (outcome if outcome in ("completed", "failed") else "other",))


def observe_memory_failure(stage: str) -> None:
    """A memory load/store failed and the turn continued degraded."""
    _inc("fr_agent_memory_failures_total",
         "Working/durable memory operations that failed non-fatally",
         ("stage",),
         (stage if stage in ("durable_memory_load", "artifact_index_refresh",
                             "working_context_write") else "other",))


def observe_eviction(kind: str) -> None:
    """agent | lock_reclaimed | lock_kept_busy.

    lock_kept_busy means an in-flight user was LRU-evicted — the exact
    scenario that used to drop a held lock.
    """
    _inc("fr_agent_evictions_total",
         "Agent-cache and user-lock eviction outcomes",
         ("kind",),
         (kind if kind in ("agent", "lock_reclaimed", "lock_kept_busy") else "other",))


# ---------------------------------------------------------------------------
# Latency and intent metrics for the data-agent pipeline (2026-09). Names are
# the ones the platform dashboards expect; labels are closed vocabularies.
# ---------------------------------------------------------------------------
_HISTOGRAMS = {}
_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300)


def _histogram(name: str, documentation: str, labelnames, buckets=_LATENCY_BUCKETS):
    metric = _HISTOGRAMS.get(name)
    if metric is not None:
        return metric
    try:
        from prometheus_client import Histogram, REGISTRY
        try:
            metric = Histogram(name, documentation, labelnames=labelnames, buckets=buckets)
        except ValueError:
            metric = REGISTRY._names_to_collectors.get(name)
    except Exception as e:
        logger.debug("[AGENT_METRICS] unavailable: %s", e)
        metric = None
    _HISTOGRAMS[name] = metric
    return metric


def _observe(name: str, documentation: str, labelnames, labelvalues, value, buckets=_LATENCY_BUCKETS) -> None:
    try:
        metric = _histogram(name, documentation, labelnames, buckets)
        if metric is not None:
            metric.labels(*labelvalues).observe(float(value))
    except Exception:
        pass


_INTENTS = {"CHAT", "DATABASE_QUERY", "ANALYTICS_QUERY", "REPORT_REQUEST", "SYSTEM_TOOL_REQUEST"}
_STAGES = {"llm", "sql_generation", "vanna_retrieval", "sql_validation", "db_query", "mcp_call",
           "voice_stt", "response_generation", "request"}
_OUTCOMES = {"ok", "error", "timeout", "rejected"}


def observe_intent(intent: str) -> None:
    """One per turn: the routed intent (see sql_agent/intent.py)."""
    _inc("fr_agent_intent_total", "Turns by routed intent", ("intent",),
         (intent if intent in _INTENTS else "other",))


def observe_stage_latency(stage: str, seconds: float, outcome: str = "ok",
                          component: str = "-") -> None:
    """request_latency / llm_latency / vanna_retrieval_latency /
    sql_generation_latency / db_query_latency / mcp_call_latency /
    voice_stt_latency, one histogram keyed by stage."""
    _observe("fr_agent_stage_duration_seconds", "Data-agent stage latency",
             ("stage", "outcome", "component"),
             (stage if stage in _STAGES else "other",
              outcome if outcome in _OUTCOMES else "other",
              str(component or "-")[:40]),
             seconds)
    if outcome != "ok":
        _inc("fr_agent_stage_errors_total", "Data-agent stage failures",
             ("stage", "outcome"),
             (stage if stage in _STAGES else "other",
              outcome if outcome in _OUTCOMES else "other"))


def observe_sql_validation_failure(code: str) -> None:
    _inc("fr_agent_sql_validation_failures_total", "Generated SQL refused by the guard",
         ("code",), (str(code or "unknown")[:40],))


def observe_rows_returned(rows: int) -> None:
    _observe("fr_agent_rows_returned", "Rows returned by executed queries", (), (),
             rows, buckets=(0, 1, 5, 10, 25, 50, 100, 250, 500, 1000))


def observe_db_timeout() -> None:
    _inc("fr_agent_db_timeouts_total", "Read-only executions that hit the statement timeout", (), ())
