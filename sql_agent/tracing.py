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
    Comet by default. Sentry is decided when the package is imported, so the
    development compose file switches it off in the container environment
    (OPIK_SENTRY_ENABLE / OPIK_ANALYTICS_ENABLE); this module additionally
    closes both in the SDK's session configuration, which the application
    settings own outright — no ``~/.opik.config`` on the box is consulted.
  * The hosted Opik (comet.com) is accepted in DEVELOPMENT ONLY, on the same
    footing as the NVIDIA development LLM provider — and it receives more
    than that provider does (result rows included), so the development
    database must be synthetic before it is switched on. The log says so,
    once, at start. Production never reaches this branch: the production
    refusal above comes first.

Every function here is best-effort: tracing must never fail, slow or change
a turn. Any error in building the tracer is logged once and the turn runs
untraced.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Hosted Opik. Development only — see the module docstring.
CLOUD_HOSTS = ("comet.com", "www.comet.com")
CLOUD_URL = "https://www.comet.com/opik/api/"

# Reason codes reported by ``tracing_status`` and logged once at agent start.
DISABLED = "disabled"          # SQL_AGENT_OPIK_ENABLED is off (the default)
PRODUCTION = "production"      # never in production, whatever the flag says
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
    try:
        import opik  # noqa: F401  (dev-only dependency)
    except Exception:  # ImportError, or a broken partial install
        return SDK_MISSING
    return READY


def _log_once(reason: str, cfg: Any) -> None:
    if reason in _logged_reasons:
        return
    _logged_reasons.add(reason)
    url = getattr(cfg, "opik_url", "")
    if reason == READY and is_cloud_url(url):
        logger.warning("[SQL_AGENT] Opik tracing ENABLED -> HOSTED %s project=%r "
                       "workspace=%r: traces LEAVE this machine (prompts, answers, "
                       "SQL, result rows). Development only; the database must be "
                       "synthetic.", url, getattr(cfg, "opik_project_name", ""),
                       getattr(cfg, "opik_workspace", ""))
    elif reason == READY:
        logger.info("[SQL_AGENT] Opik tracing ENABLED -> %s project=%r "
                    "(development only; traces hold user text and names)",
                    url, getattr(cfg, "opik_project_name", ""))
    elif reason == DISABLED:
        logger.debug("[SQL_AGENT] Opik tracing off (SQL_AGENT_OPIK_ENABLED unset)")
    elif reason == PRODUCTION:
        logger.warning("[SQL_AGENT] Opik tracing requested but this is production — "
                       "refused. Traces would carry user questions and surveillance "
                       "subjects' names to a store outside the audit rules.")
    elif reason == SDK_MISSING:
        logger.warning("[SQL_AGENT] Opik tracing requested but the `opik` package is "
                       "not installed. It is a development extra: build the image "
                       "with INSTALL_DEV=true (requirements-dev.txt).")


def sdk_session_settings(cfg: Any) -> Dict[str, Any]:
    """What the SDK is told, derived from the application settings only.

    Pure, so the policy is testable without the SDK. ``api_key`` is None for
    the open-source instance (it does not authenticate) and the account key
    for the hosted service; the two outbound channels the SDK opens on its
    own are closed every time.
    """
    api_key = str(getattr(cfg, "opik_api_key", "") or "").strip() or None
    return {
        "url_override": str(cfg.opik_url),
        "workspace": str(getattr(cfg, "opik_workspace", "") or "default"),
        "project_name": str(cfg.opik_project_name),
        "api_key": api_key,
        "sentry_enable": False,
        "analytics_enable": False,
        "analytics_url": "",
        "track_disable": False,
    }


def _configure_sdk(cfg: Any) -> None:
    """Hand the application settings to the SDK's session configuration.

    The session layer outranks the SDK's own environment and ``~/.opik.config``
    sources, so the application settings are the single authority for where
    traces go. (Sentry is decided at import time from the environment, which
    is why the development compose also sets OPIK_SENTRY_ENABLE=false; this
    call makes every later configuration read agree.)
    """
    from opik import config as opik_config
    for key, value in sdk_session_settings(cfg).items():
        opik_config.update_session_config(key, value)


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
        _configure_sdk(cfg)
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


def tool_tracker(cfg: Any, tool_name: str):
    """A decorator that records one MCP tool call as an Opik span named
    ``mcp:<tool>`` under the current turn trace, or None when tracing is
    not active (production, disabled, SDK missing). Development only, like
    every other tracer here; the span carries the call arguments and the
    envelope, which is what a person debugging a turn needs to see."""
    if tracing_status(cfg) != READY:
        return None
    try:
        _configure_sdk(cfg)
        import opik
        return opik.track(name=f"mcp:{tool_name}", type="tool",
                          capture_input=True, capture_output=True,
                          project_name=str(cfg.opik_project_name))
    except Exception as exc:
        logger.debug("[SQL_AGENT] MCP tool span not attached: %s", exc)
        return None
