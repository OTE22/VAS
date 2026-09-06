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
import re
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


SESSION = {}   # what the fake SDK was told through update_session_config


@pytest.fixture(autouse=True)
def _clean_state():
    """Fresh once-only logging and fresh recorders per test."""
    tracing._logged_reasons.clear()
    FakeTracer.instances.clear()
    SESSION.clear()
    yield


@pytest.fixture
def fake_sdk(monkeypatch):
    """Install a stand-in `opik` package exposing OpikTracer and the
    session-config hook the real SDK offers."""
    def install(tracer_cls=FakeTracer):
        opik = types.ModuleType("opik")
        config = types.ModuleType("opik.config")
        config.update_session_config = lambda key, value: SESSION.__setitem__(key, value)
        integrations = types.ModuleType("opik.integrations")
        langchain = types.ModuleType("opik.integrations.langchain")
        langchain.OpikTracer = tracer_cls
        opik.config = config
        opik.integrations = integrations
        integrations.langchain = langchain
        monkeypatch.setitem(sys.modules, "opik", opik)
        monkeypatch.setitem(sys.modules, "opik.config", config)
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


CLOUD_URLS = [
    "https://www.comet.com/opik/api/",
    "https://comet.com/opik/api",
    "http://eu.comet.com/opik/api/",
    "https://WWW.COMET.COM/opik/api/",
]
LOCAL_URLS = [
    LOCAL_URL,
    "http://localhost:5173/api/",
    "http://127.0.0.1:5173/api",
    "https://opik.lab.internal/api/",
    "http://comet.com.lab.internal/api/",   # a suffix, not the hosted domain
]


@pytest.mark.parametrize("url", CLOUD_URLS + LOCAL_URLS)
def test_production_refuses_every_destination(fake_sdk, url):
    fake_sdk()
    cfg = agent_cfg(opik_enabled=True, is_production=True, opik_url=url)
    assert tracing.tracing_status(cfg) == tracing.PRODUCTION
    assert tracing.build_tracer(cfg) is None
    assert FakeTracer.instances == []


@pytest.mark.parametrize("url", CLOUD_URLS)
def test_the_hosted_service_is_accepted_in_development_and_says_so(fake_sdk, url, caplog):
    """Same footing as the NVIDIA development provider — but the log must
    make the hosted case unmistakable, once."""
    fake_sdk()
    cfg = agent_cfg(opik_enabled=True, opik_url=url, opik_api_key="k", opik_workspace="w")
    assert tracing.is_cloud_url(url)
    with caplog.at_level("WARNING"):
        assert tracing.build_tracer(cfg) is not None
        tracing.build_tracer(cfg)
    assert caplog.text.count("traces LEAVE this machine") == 1
    assert SESSION["api_key"] == "k" and SESSION["workspace"] == "w"


@pytest.mark.parametrize("url", LOCAL_URLS)
def test_self_hosted_urls_are_accepted_quietly(fake_sdk, url, caplog):
    fake_sdk()
    assert not tracing.is_cloud_url(url)
    with caplog.at_level("INFO"):
        assert tracing.build_tracer(agent_cfg(opik_enabled=True, opik_url=url)) is not None
    assert "LEAVE" not in caplog.text


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
    """The application settings are the SDK's only authority (its session
    layer outranks its env and ~/.opik.config), and the two channels it opens
    on its own — Sentry error reports, Comet usage analytics — are closed."""
    fake_sdk()
    cfg = agent_cfg(opik_enabled=True, opik_url="http://localhost:5173/api/",
                    opik_workspace="default", opik_project_name="p")
    assert tracing.build_tracer(cfg) is not None
    assert SESSION == {
        "url_override": "http://localhost:5173/api/",
        "workspace": "default",
        "project_name": "p",
        "api_key": None,           # the open-source instance authenticates nobody
        "sentry_enable": False,
        "analytics_enable": False,
        "analytics_url": "",
        "track_disable": False,
    }


def test_sentry_is_also_closed_where_the_sdk_decides_it_at_import():
    """The SDK arms Sentry when `opik` is imported, before any session config
    can speak, so the development compose must say it in the environment."""
    compose = open(os.path.join(REPO, "docker", "docker-compose.cpu.yml"),
                   encoding="utf-8").read()
    api = compose.split("  face_recognition:", 1)[1].split("\n  ml_worker:", 1)[0]
    assert re.search(r'OPIK_SENTRY_ENABLE:\s*"false"', api), \
        "face_recognition must set OPIK_SENTRY_ENABLE=false"
    assert re.search(r'OPIK_ANALYTICS_ENABLE:\s*"false"', api), \
        "face_recognition must set OPIK_ANALYTICS_ENABLE=false"


def test_a_configured_key_reaches_the_sdk_but_not_the_log(fake_sdk, caplog):
    fake_sdk()
    cfg = agent_cfg(opik_enabled=True, opik_api_key="opik-secret-123")
    with caplog.at_level("DEBUG"):
        assert tracing.build_tracer(cfg) is not None
    assert SESSION["api_key"] == "opik-secret-123"
    assert "opik-secret-123" not in caplog.text


# ---------------------------------------------------------------------------
# The MCP launcher reads the same docker/.env and aims opik-mcp accordingly
# ---------------------------------------------------------------------------

def _launcher():
    import importlib.util
    path = os.path.join(REPO, "scripts", "opik_mcp.py")
    spec = importlib.util.spec_from_file_location("opik_mcp_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_defaults_to_the_workstation_instance_on_the_oss_api_path():
    env = _launcher().mcp_environment({})
    assert env["COMET_URL_OVERRIDE"] == "http://localhost:5173"
    assert env["OPIK_URL"] == "http://localhost:5173/api", \
        "open-source Opik serves /api, not the hosted /opik/api"
    assert "OPIK_API_KEY" not in env and "OPIK_WORKSPACE" not in env
    assert env["OPIK_MCP_ANALYTICS_ENABLED"] == "false"
    assert env["OPIK_DEFAULT_PROJECT_NAME"] == "face-detector-sql-agent"


def test_launcher_turns_the_container_alias_into_localhost():
    env = _launcher().mcp_environment(
        {"OPIK_URL_OVERRIDE": "http://host.docker.internal:5173/api/"})
    assert env["OPIK_URL"] == "http://localhost:5173/api"


def test_launcher_aims_at_the_hosted_service_with_the_operator_credentials():
    env = _launcher().mcp_environment({
        "OPIK_URL_OVERRIDE": "https://www.comet.com/opik/api/",
        "OPIK_API_KEY": "opik-key-1",
        "OPIK_WORKSPACE": "my-team",
        "OPIK_PROJECT_NAME": "proj",
    })
    assert env["COMET_URL_OVERRIDE"] == "https://www.comet.com"
    assert env["OPIK_URL"] == "https://www.comet.com/opik/api"
    assert env["OPIK_API_KEY"] == "opik-key-1"
    assert env["OPIK_WORKSPACE"] == "my-team"
    assert env["OPIK_DEFAULT_PROJECT_NAME"] == "proj"


def test_launcher_reads_dotenv_like_compose_does(tmp_path):
    p = tmp_path / ".env"
    p.write_text('\ufeff# comment\nOPIK_API_KEY="quoted"\nOPIK_WORKSPACE=ws \n\nBAD LINE\n',
                 encoding="utf-8")
    values = _launcher().read_dotenv(str(p))
    assert values == {"OPIK_API_KEY": "quoted", "OPIK_WORKSPACE": "ws"}
    assert _launcher().read_dotenv(str(tmp_path / "missing")) == {}


def test_the_mcp_config_has_no_credentials_and_uses_the_launcher():
    import json
    cfg = json.load(open(os.path.join(REPO, ".mcp.json"), encoding="utf-8"))
    server = cfg["mcpServers"]["opik-mcp"]
    assert server["args"] == ["scripts/opik_mcp.py"]
    assert "env" not in server, "credentials belong in docker/.env, not the repository"


def test_the_tracing_module_never_touches_the_process_environment():
    """The single-source rule (tests/test_config_single_source.py) — spelled
    out here too because the obvious way to configure this SDK is exactly
    the way that rule forbids."""
    source = open(os.path.join(REPO, "sql_agent", "tracing.py"), encoding="utf-8").read()
    assert "os.environ" not in source and "getenv" not in source


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
