"""Opik tracing for the SQL agent is a development tool that production
cannot switch on, and that never changes a turn when it is off.

Three layers, mirroring tests/test_llm_dev_provider.py:

  1. sql_agent/tracing.py attaches no tracer unless enabled, non-production,
     self-hosted and installed — and never raises;
  2. the config guard fails a production boot with the flag set;
  3. the keys cannot arrive through the settings API and the credential is
     redacted everywhere.

Plus the wiring: the graph call sites pass the run config through, and a
None config (tracing off) is the pre-tracing call exactly.

No network, no Opik server, no real SDK: the SDK is a fake module injected
into sys.modules, so these tests pass in the production image (no `opik`)
and in the development image alike.
"""

import os
import sys
import types
from types import SimpleNamespace

import pytest

from backend.security.config_guard import (
    SECURITY_CRITICAL_KEYS,
    collect_violations,
    fatal_only,
)
from backend.security.redaction import SECRET_SETTINGS
from sql_agent import tracing
from sql_agent.agent import SQLIntelligenceAgent, _invoke_cancellable

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOCAL_URL = "http://host.docker.internal:5173/api/"


def agent_cfg(**overrides):
    """A sql_agent.config.Config stand-in with the tracer's inputs."""
    base = dict(
        opik_enabled=False,
        opik_url=LOCAL_URL,
        opik_api_key="",
        opik_workspace="default",
        opik_project_name="face-detector-sql-agent",
        is_production=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeTracer:
    """Records what the agent asked for; the real class needs a server."""
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeTracer.instances.append(self)


class ExplodingTracer:
    def __init__(self, **kwargs):
        raise RuntimeError("no server")


@pytest.fixture(autouse=True)
def _clean_state():
    """Fresh once-only logging, fresh recorder, and the SDK env restored."""
    tracing._logged_reasons.clear()
    FakeTracer.instances.clear()
    saved = {k: os.environ.get(k) for k in list(os.environ) if k.startswith("OPIK_")}
    saved_keys = set(saved)
    yield
    for k in [k for k in os.environ if k.startswith("OPIK_")]:
        if k not in saved_keys:
            del os.environ[k]
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def fake_sdk(monkeypatch):
    """Install a stand-in `opik` package exposing OpikTracer."""
    def install(tracer_cls=FakeTracer):
        opik = types.ModuleType("opik")
        integrations = types.ModuleType("opik.integrations")
        langchain = types.ModuleType("opik.integrations.langchain")
        langchain.OpikTracer = tracer_cls
        opik.integrations = integrations
        integrations.langchain = langchain
        monkeypatch.setitem(sys.modules, "opik", opik)
        monkeypatch.setitem(sys.modules, "opik.integrations", integrations)
        monkeypatch.setitem(sys.modules, "opik.integrations.langchain", langchain)
        return langchain
    return install


@pytest.fixture
def no_sdk(monkeypatch):
    """`import opik` raises ImportError even when the real package exists."""
    monkeypatch.setitem(sys.modules, "opik", None)


# ---------------------------------------------------------------------------
# Layer 1: sql_agent/tracing.py
# ---------------------------------------------------------------------------

def test_off_by_default(fake_sdk):
    fake_sdk()
    assert tracing.tracing_status(agent_cfg()) == tracing.DISABLED
    assert tracing.build_tracer(agent_cfg()) is None
    assert FakeTracer.instances == []


def test_production_never_traces_even_when_enabled_and_installed(fake_sdk):
    fake_sdk()
    cfg = agent_cfg(opik_enabled=True, is_production=True)
    assert tracing.tracing_status(cfg) == tracing.PRODUCTION
    assert tracing.build_tracer(cfg, thread_id="s1") is None
    assert FakeTracer.instances == [], "no SDK object may be built in production"


@pytest.mark.parametrize("url", [
    "https://www.comet.com/opik/api/",
    "https://comet.com/opik/api",
    "http://eu.comet.com/opik/api/",
    "https://WWW.COMET.COM/opik/api/",
])
def test_the_hosted_service_is_refused_everywhere(fake_sdk, url):
    fake_sdk()
    cfg = agent_cfg(opik_enabled=True, opik_url=url)
    assert tracing.tracing_status(cfg) == tracing.CLOUD_REFUSED
    assert tracing.build_tracer(cfg) is None
    assert FakeTracer.instances == []


@pytest.mark.parametrize("url", [
    LOCAL_URL,
    "http://localhost:5173/api/",
    "http://127.0.0.1:5173/api",
    "https://opik.lab.internal/api/",
    "http://comet.com.lab.internal/api/",   # a suffix, not the hosted domain
])
def test_self_hosted_urls_are_accepted(fake_sdk, url):
    fake_sdk()
    assert tracing.tracing_status(agent_cfg(opik_enabled=True, opik_url=url)) \
        == tracing.READY


def test_missing_sdk_degrades_to_no_tracing(no_sdk):
    cfg = agent_cfg(opik_enabled=True)
    assert tracing.tracing_status(cfg) == tracing.SDK_MISSING
    assert tracing.build_tracer(cfg, thread_id="s1") is None


def test_tracer_is_built_from_the_agent_settings(fake_sdk):
    fake_sdk()
    cfg = agent_cfg(opik_enabled=True, opik_project_name="proj-x")
    graph = object()
    tracer = tracing.build_tracer(cfg, thread_id="session-7", user_id=42,
                                  graph=graph, tags=["query"])
    assert isinstance(tracer, FakeTracer)
    kw = tracer.kwargs
    assert kw["thread_id"] == "session-7"
    assert kw["project_name"] == "proj-x"
    assert kw["graph"] is graph
    assert kw["tags"] == ["sql_agent", "query"]
    assert kw["metadata"] == {"component": "sql_agent", "user_id": 42}


def test_the_sdk_is_configured_from_settings_with_its_outbound_channels_closed(fake_sdk):
    """The SDK reads OPIK_* from the environment; we write it there, and we
    switch off the two channels it opens on its own (Sentry, analytics)."""
    fake_sdk()
    os.environ["OPIK_URL_OVERRIDE"] = "https://www.comet.com/opik/api/"  # stale
    os.environ["OPIK_API_KEY"] = "stale-key"
    cfg = agent_cfg(opik_enabled=True, opik_url="http://localhost:5173/api/",
                    opik_workspace="default", opik_project_name="p")
    assert tracing.build_tracer(cfg) is not None
    assert os.environ["OPIK_URL_OVERRIDE"] == "http://localhost:5173/api/"
    assert os.environ["OPIK_WORKSPACE"] == "default"
    assert os.environ["OPIK_PROJECT_NAME"] == "p"
    assert "OPIK_API_KEY" not in os.environ, "an empty key clears a stale one"
    assert os.environ["OPIK_SENTRY_ENABLE"] == "false"
    assert os.environ["OPIK_ANALYTICS_ENABLE"] == "false"
    assert os.environ["OPIK_ANALYTICS_URL"] == ""


def test_a_configured_key_reaches_the_sdk_but_not_the_log(fake_sdk, caplog):
    fake_sdk()
    cfg = agent_cfg(opik_enabled=True, opik_api_key="opik-secret-123")
    with caplog.at_level("DEBUG"):
        assert tracing.build_tracer(cfg) is not None
    assert os.environ["OPIK_API_KEY"] == "opik-secret-123"
    assert "opik-secret-123" not in caplog.text


def test_a_failing_sdk_never_fails_the_turn(fake_sdk, caplog):
    fake_sdk(ExplodingTracer)
    cfg = agent_cfg(opik_enabled=True)
    with caplog.at_level("WARNING"):
        assert tracing.build_tracer(cfg) is None
    assert "not attached" in caplog.text


def test_the_reason_is_logged_once_not_per_turn(fake_sdk, caplog):
    fake_sdk()
    cfg = agent_cfg(opik_enabled=True, is_production=True)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            tracing.build_tracer(cfg)
    assert caplog.text.count("refused") == 1


def test_graph_config_is_none_or_the_callbacks():
    assert tracing.graph_config(None) is None
    t = object()
    assert tracing.graph_config(t) == {"callbacks": [t]}


# ---------------------------------------------------------------------------
# The wiring: call sites hand the config to LangGraph
# ---------------------------------------------------------------------------

class RecordingGraph:
    def __init__(self):
        self.calls = []

    def invoke(self, state, config=None):
        self.calls.append(("invoke", config))
        return {**state, "final_response": "ok"}

    def stream(self, state, config=None):
        self.calls.append(("stream", config))
        yield {"node": {"final_response": "ok"}}


class NeverSet:
    def is_set(self):
        return False


def test_invoke_path_passes_the_config_through():
    graph = RecordingGraph()
    cfg = {"callbacks": ["tracer"]}
    out = _invoke_cancellable(graph, {"q": 1}, config=cfg)
    assert out["final_response"] == "ok"
    assert graph.calls == [("invoke", cfg)]


def test_stream_path_passes_the_config_through():
    graph = RecordingGraph()
    cfg = {"callbacks": ["tracer"]}
    out = _invoke_cancellable(graph, {"q": 1}, cancel_event=NeverSet(), config=cfg)
    assert out["final_response"] == "ok"
    assert graph.calls == [("stream", cfg)]


def test_tracing_off_is_the_pre_tracing_call():
    graph = RecordingGraph()
    _invoke_cancellable(graph, {"q": 1})
    _invoke_cancellable(graph, {"q": 1}, cancel_event=NeverSet())
    assert graph.calls == [("invoke", None), ("stream", None)]


def _agent_stub(session="sess-1", user_id=9, graph_definition=None):
    return SimpleNamespace(
        conversation_memory=SimpleNamespace(current_session_id=session,
                                            user_id=user_id),
        _graph_definition=graph_definition,
    )


def test_agent_turn_config_is_none_when_tracing_is_off(monkeypatch):
    monkeypatch.setattr("sql_agent.agent.config", agent_cfg())
    assert SQLIntelligenceAgent._graph_config(_agent_stub(), "query") is None


def test_agent_turn_config_carries_the_conversation_as_the_thread(monkeypatch, fake_sdk):
    fake_sdk()
    monkeypatch.setattr("sql_agent.agent.config", agent_cfg(opik_enabled=True))
    graph_def = object()
    run_config = SQLIntelligenceAgent._graph_config(
        _agent_stub(session="sess-42", user_id=7, graph_definition=graph_def),
        "query_stream")
    (tracer,) = run_config["callbacks"]
    assert tracer.kwargs["thread_id"] == "sess-42"
    assert tracer.kwargs["metadata"]["user_id"] == 7
    assert tracer.kwargs["graph"] is graph_def
    assert tracer.kwargs["tags"] == ["sql_agent", "query_stream"]


def test_agent_turn_config_survives_a_broken_helper(monkeypatch):
    monkeypatch.setattr("sql_agent.agent.config", agent_cfg(opik_enabled=True))
    monkeypatch.setattr(tracing, "turn_config",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert SQLIntelligenceAgent._graph_config(_agent_stub(), "query") is None


# ---------------------------------------------------------------------------
# Layer 2: the production config guard
# ---------------------------------------------------------------------------

def guard_codes(**fields):
    return {v.code for v in collect_violations(SimpleNamespace(**fields),
                                               environment="production")}


@pytest.mark.parametrize("value", [True, "true", "1", "yes", "on"])
def test_production_boot_fails_with_tracing_enabled(value):
    violations = collect_violations(SimpleNamespace(SQL_AGENT_OPIK_ENABLED=value),
                                    environment="production")
    assert "SQL_AGENT_TRACING_IN_PRODUCTION" in {v.code for v in fatal_only(violations)}, \
        "must block the boot, not merely warn"


def test_production_with_tracing_off_is_clean():
    for value in (False, "false", "0", ""):
        assert "SQL_AGENT_TRACING_IN_PRODUCTION" not in guard_codes(
            SQL_AGENT_OPIK_ENABLED=value)


def test_a_stray_tracing_key_in_production_warns_without_blocking():
    violations = collect_violations(SimpleNamespace(OPIK_API_KEY="opik-forgotten"),
                                    environment="production")
    codes = {v.code for v in violations}
    assert "SQL_AGENT_TRACING_API_KEY_PRESENT" in codes
    assert "SQL_AGENT_TRACING_API_KEY_PRESENT" not in {
        v.code for v in fatal_only(violations)}


def test_development_is_unaffected():
    cfg = SimpleNamespace(SQL_AGENT_OPIK_ENABLED=True, OPIK_API_KEY="k")
    codes = {v.code for v in collect_violations(cfg, environment="development")}
    assert "SQL_AGENT_TRACING_IN_PRODUCTION" not in codes
    assert "SQL_AGENT_TRACING_API_KEY_PRESENT" not in codes


# ---------------------------------------------------------------------------
# Layer 3: runtime-change and rendering surfaces, and the image contents
# ---------------------------------------------------------------------------

def test_the_switch_cannot_arrive_through_the_settings_api():
    for key in ("SQL_AGENT_OPIK_ENABLED", "OPIK_URL_OVERRIDE", "OPIK_API_KEY"):
        assert key in SECURITY_CRITICAL_KEYS, key


def test_the_key_is_redacted_wherever_settings_render():
    assert "OPIK_API_KEY" in SECRET_SETTINGS


def test_the_keys_never_reach_the_admin_settings_page():
    source = open(os.path.join(REPO, "backend", "routes", "settings.py"),
                  encoding="utf-8").read()
    for key in ("SQL_AGENT_OPIK_ENABLED", "OPIK_URL_OVERRIDE", "OPIK_API_KEY"):
        assert key not in source, f"{key} must not be exposed on the settings page"


def test_the_sdk_is_a_development_extra_only():
    """Production images must not carry opik: beyond size (litellm, openai),
    the SDK reports errors to a hard-coded Sentry DSN and sends usage
    analytics to Comet by default — outbound traffic from an offline box."""
    def names(path):
        out = set()
        for line in open(os.path.join(REPO, path), encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                out.add(line.split("==")[0].split(">=")[0].split("[")[0].strip().lower())
        return out
    for runtime in ("requirements-base.txt", "requirements-cpu.txt", "requirements-gpu.txt"):
        if os.path.exists(os.path.join(REPO, runtime)):
            assert "opik" not in names(runtime), f"opik must not be in {runtime}"
    assert "opik" in names("requirements-dev.txt")
