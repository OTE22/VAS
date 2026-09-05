"""Development-only LLM tracing for the SQL agent (Opik).

The Prometheus counters in ``observability.py`` say *how often* the agent
did something. They cannot show *what it did on one turn*: the exact prompt
each node sent, what the model answered, which tool it proposed, how long
each step took, and where a wrong answer first went wrong. Opik records that
as one trace per turn, with a span per graph node and per model call, and
the Opik MCP server lets Claude Code read those traces back.

POLICY — this is a DEVELOPMENT tool, never a production one:

  * A trace holds the user's own words, the names of people under
    surveillance, the generated SQL and its result rows. The audit rules keep
    all of that out of log files in production; a tracing store outside the
    application's retention and audit is the same class of leak, so
    production refuses it (config guard, exit 78) and ``build_tracer`` returns
    None there regardless of any flag.
  * The ``opik`` SDK lives in requirements-dev.txt only. Production images do
    not ship it; this module imports it lazily and degrades to "no tracing"
    when it is absent. Beyond size (it drags in litellm and openai), the SDK
    reports errors to a hard-coded Sentry DSN and sends usage analytics to
    Comet by default — both are switched off here before the SDK loads, and
    neither belongs on a box documented as fully offline.
  * Cloud Opik (comet.com) is refused even in development. Development runs
    against synthetic data, but the habit of pointing traces at a hosted
    endpoint is exactly what must not survive into production; a self-hosted
    Opik on the workstation costs nothing and keeps the rule simple.

Every function here is best-effort: tracing must never fail, slow or change
a turn. Any error in building the tracer is logged once and the turn runs
untraced.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Sequence
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Hosted Opik. Refused unconditionally — see the module docstring.
CLOUD_HOSTS = ("comet.com", "www.comet.com")

# Reason codes reported by ``tracing_status`` and logged once at agent start.
DISABLED = "disabled"          # SQL_AGENT_OPIK_ENABLED is off (the default)
PRODUCTION = "production"      # never in production, whatever the flag says
CLOUD_REFUSED = "cloud_refused"  # OPIK_URL_OVERRIDE points at comet.com
SDK_MISSING = "sdk_missing"    # opik is not installed (production image)
READY = "ready"

_logged_reasons: set = set()


def is_cloud_url(url: str) -> bool:
    host = (urlsplit(str(url or "").strip()).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in CLOUD_HOSTS)


def tracing_status(cfg: Any) -> str:
    """Why tracing is or is not active for this configuration.

    ``cfg`` is the agent's ``sql_agent.config.Config`` (or any object with the
    same attribute names). Pure — no import of the SDK, no side effects — so
    it is safe to call from the config guard and from tests.
    """
    if not bool(getattr(cfg, "opik_enabled", False)):
        return DISABLED
    if bool(getattr(cfg, "is_production", True)):
        return PRODUCTION
    if is_cloud_url(getattr(cfg, "opik_url", "")):
        return CLOUD_REFUSED
    try:
        import opik  # noqa: F401  (dev-only dependency)
    except Exception:  # ImportError, or a broken partial install
        return SDK_MISSING
    return READY


def _log_once(reason: str, cfg: Any) -> None:
    if reason in _logged_reasons:
        return
    _logged_reasons.add(reason)
    if reason == READY:
        logger.info("[SQL_AGENT] Opik tracing ENABLED -> %s project=%r "
                    "(development only; traces hold user text and names)",
                    getattr(cfg, "opik_url", ""), getattr(cfg, "opik_project_name", ""))
    elif reason == DISABLED:
        logger.debug("[SQL_AGENT] Opik tracing off (SQL_AGENT_OPIK_ENABLED unset)")
    elif reason == PRODUCTION:
        logger.warning("[SQL_AGENT] Opik tracing requested but this is production — "
                       "refused. Traces would carry user questions and surveillance "
                       "subjects' names to a store outside the audit rules.")
    elif reason == CLOUD_REFUSED:
        logger.warning("[SQL_AGENT] Opik tracing refused: OPIK_URL_OVERRIDE=%r is the "
                       "hosted service. Run a self-hosted Opik and point at it.",
                       getattr(cfg, "opik_url", ""))
    elif reason == SDK_MISSING:
        logger.warning("[SQL_AGENT] Opik tracing requested but the `opik` package is "
                       "not installed. It is a development extra: build the image "
                       "with INSTALL_DEV=true (requirements-dev.txt).")


def _export_sdk_environment(cfg: Any) -> None:
    """Hand our settings to the SDK, which configures itself from OPIK_* env.

    Done explicitly rather than trusting whatever ~/.opik.config or a stray
    variable says: the application's settings are the single authority, and
    the two outbound channels the SDK opens on its own (Sentry error reports,
    Comet usage analytics) are closed here every time, not left to a default.
    """
    env = os.environ
    env["OPIK_URL_OVERRIDE"] = str(cfg.opik_url)
    env["OPIK_WORKSPACE"] = str(cfg.opik_workspace or "default")
    env["OPIK_PROJECT_NAME"] = str(cfg.opik_project_name)
    api_key = str(getattr(cfg, "opik_api_key", "") or "").strip()
    if api_key:
        env["OPIK_API_KEY"] = api_key
    else:
        env.pop("OPIK_API_KEY", None)
    env["OPIK_SENTRY_ENABLE"] = "false"
    env["OPIK_ANALYTICS_ENABLE"] = "false"
    env["OPIK_ANALYTICS_URL"] = ""
    env["OPIK_TRACK_DISABLE"] = "false"


def build_tracer(cfg: Any, *, thread_id: Optional[str] = None,
                 user_id: Optional[int] = None, graph: Any = None,
                 tags: Sequence[str] = ()) -> Optional[Any]:
    """An ``OpikTracer`` for one turn, or None when tracing is not active.

    One tracer per turn, not per agent: the tracer keeps per-run state, and a
    turn is the unit a person wants to inspect. ``thread_id`` groups the
    turns of one conversation in the Opik UI; ``graph`` (the compiled graph's
    ``get_graph()``) lets Opik draw the node topology next to the trace.
    """
    reason = tracing_status(cfg)
    _log_once(reason, cfg)
    if reason != READY:
        return None
    try:
        _export_sdk_environment(cfg)
        from opik.integrations.langchain import OpikTracer
        metadata: Dict[str, Any] = {"component": "sql_agent"}
        if user_id is not None:
            metadata["user_id"] = int(user_id)
        return OpikTracer(
            tags=["sql_agent", *tags],
            metadata=metadata,
            graph=graph,
            project_name=str(cfg.opik_project_name),
            thread_id=str(thread_id) if thread_id else None,
        )
    except Exception as exc:  # never let observability fail a turn
        logger.warning("[SQL_AGENT] Opik tracer not attached: %s: %s",
                       type(exc).__name__, exc)
        return None


def graph_config(tracer: Optional[Any]) -> Optional[Dict[str, Any]]:
    """The LangGraph ``config`` that attaches the tracer, or None.

    A None config is exactly what the call sites passed before tracing
    existed, so the untraced path is byte-for-byte the old behaviour.
    """
    if tracer is None:
        return None
    return {"callbacks": [tracer]}


def turn_config(cfg: Any, **tracer_kwargs: Any) -> Optional[Dict[str, Any]]:
    """``graph_config(build_tracer(...))`` in one call, for the agent."""
    return graph_config(build_tracer(cfg, **tracer_kwargs))
