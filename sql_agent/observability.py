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
