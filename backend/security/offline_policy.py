"""Production offline policy: the rules that make accidental cloud use impossible.

Development may use the internet. Production must ENFORCE offline operation,
not merely prefer local services. This module is the enforcement: a set of
pure functions that read a configuration object through ``getattr`` and
answer two questions -

  * ``collect_offline_violations`` - what is misconfigured, as records the
    config guard turns into fatal ``ConfigViolation``s (boot aborts);
  * ``startup_checklist`` - the ``[PASS]``/``[FAIL]`` list production
    readiness reports, so a box whose gateway is unplugged still comes up
    and a box that would call out never becomes healthy.

Contract (same as ``config_guard``): never read ``os.environ`` here. Every
input arrives as an explicit argument (``cfg``, ``env``, ``artifact_probe``)
so a rule can be exercised from a ``SimpleNamespace`` in a test and never
fires because of the test runner's own environment.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

# Public inference / model / telemetry / asset hosts. A production URL whose
# host ends with one of these is an offline-policy violation whatever the
# setting is called. Suffix match, case-insensitive.
KNOWN_EXTERNAL_HOSTS: Tuple[str, ...] = (
    "api.openai.com", "openai.com",
    "api.anthropic.com", "anthropic.com",
    "integrate.api.nvidia.com", "api.nvidia.com", "build.nvidia.com",
    "huggingface.co", "hf.co",
    "googleapis.com", "generativelanguage.googleapis.com",
    "api.cohere.ai", "api.cohere.com", "cohere.com",
    "api.mistral.ai",
    "api.groq.com", "api.together.xyz", "api.deepseek.com",
    "azure.com", "openai.azure.com", "amazonaws.com", "bedrock.amazonaws.com",
    "comet.com",
    "cdn.jsdelivr.net", "cdnjs.cloudflare.com", "unpkg.com",
    "fonts.googleapis.com", "fonts.gstatic.com",
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
)

# Settings that hold service endpoints, with the component each one selects.
ENDPOINT_SETTINGS: Tuple[Tuple[str, str], ...] = (
    ("LLM_BASE_URL", "LLM"),
    ("OLLAMA_BASE_URL", "LLM"),
    ("EMBEDDING_BASE_URL", "embedding"),
    ("MCP_SQL_URL", "MCP"),
    ("MILVUS_URI", "vector store"),
    ("STT_BASE_URL", "STT"),
    ("OTEL_EXPORTER_ENDPOINT", "telemetry"),
    ("OPIK_URL_OVERRIDE", "telemetry"),
)

# Flags that must be off when offline.
PERMISSION_FLAGS: Tuple[Tuple[str, str], ...] = (
    ("ALLOW_EXTERNAL_APIS", "external APIs"),
    ("ALLOW_MODEL_DOWNLOADS", "model downloads"),
    ("ALLOW_EXTERNAL_TELEMETRY", "external telemetry"),
)

# Providers that are, by construction, remote.
CLOUD_LLM_PROVIDERS = frozenset({"nvidia_cloud", "nim_cloud", "openai", "anthropic",
                                 "gemini", "cohere", "mistral_cloud"})
LOCAL_LLM_PROVIDERS = frozenset({"ollama", "vllm", "nim_local", "nim", "openai_compat"})
LOCAL_EMBEDDING_PROVIDERS = frozenset({"local", "chroma", "ollama", "openai_compat_local"})
LOCAL_STT_PROVIDERS = frozenset({"none", "local", "whisper", "faster_whisper", "riva"})

# Process-environment switches the model libraries honour. When offline they
# must be set so a library never even tries the network.
OFFLINE_ENV_SWITCHES: Tuple[str, ...] = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")


@dataclass(frozen=True)
class OfflineFinding:
    """One rule outcome. ``severity`` is ``fatal`` or ``warn``."""
    code: str
    key: str
    message: str
    fix: str
    severity: str = "fatal"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""

    def line(self) -> str:
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.name}" + (f" - {self.detail}" if self.detail else "")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _text(cfg: Any, name: str, default: str = "") -> str:
    return str(getattr(cfg, name, default) or default).strip()


def offline_mode(cfg: Any, production: bool) -> bool:
    """The effective mode. Production is offline unless it says otherwise -
    and saying otherwise is itself a violation (see the collector)."""
    raw = getattr(cfg, "OFFLINE_MODE", None)
    if raw is None or str(raw).strip() == "":
        return bool(production)
    return _truthy(raw)


def host_of(url: str) -> str:
    """The host part of a URL or ``host:port`` string, lower-cased; '' if none."""
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "//" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    return (parts.hostname or "").strip().lower()


def is_known_external_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in KNOWN_EXTERNAL_HOSTS)


def is_internal_host(host: str, allowed_hosts: Iterable[str] = ()) -> bool:
    """Loopback, private ranges, link-local, Docker service names (single
    label), ``.local``/``.internal``/``.lan``/``.localdomain`` names, or an
    operator-approved host. Anything else is treated as the internet."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in {h.strip().lower() for h in allowed_hosts if h}:
        return True
    if host in ("localhost", "host.docker.internal"):
        return True
    try:
        address = ipaddress.ip_address(host.strip("[]"))
        return bool(address.is_loopback or address.is_private or address.is_link_local
                    or address.is_unspecified)
    except ValueError:
        pass
    if is_known_external_host(host):
        return False
    if "." not in host:
        return True                      # a Docker/Compose service name
    return host.rsplit(".", 1)[-1] in ("local", "internal", "lan", "localdomain", "intranet")


def classify_url(url: str, allowed_hosts: Iterable[str] = ()) -> str:
    """``internal`` | ``external`` | ``empty`` | ``invalid``."""
    if not (url or "").strip():
        return "empty"
    host = host_of(url)
    if not host:
        return "invalid"
    return "internal" if is_internal_host(host, allowed_hosts) else "external"


def _allowed_hosts(cfg: Any) -> List[str]:
    raw = _text(cfg, "OFFLINE_ALLOWED_HOSTS")
    return [h.strip() for h in raw.split(",") if h.strip()]


def collect_offline_violations(
    cfg: Any,
    *,
    production: bool,
    env: Optional[Mapping[str, str]] = None,
    artifact_probe: Optional[Callable[[str], bool]] = None,
) -> List[OfflineFinding]:
    """Every offline-policy problem, in one pass. Empty when nothing is wrong.

    Applies when the effective mode is offline (production by default). In
    development with ``OFFLINE_MODE=false`` nothing here fires: cloud
    inference, downloads and telemetry are development conveniences.
    """
    out: List[OfflineFinding] = []
    add = out.append
    offline = offline_mode(cfg, production)
    raw_mode = getattr(cfg, "OFFLINE_MODE", None)

    if production and raw_mode is not None and str(raw_mode).strip() != "" and not _truthy(raw_mode):
        add(OfflineFinding(
            "OFFLINE_MODE_DISABLED_IN_PRODUCTION", "OFFLINE_MODE",
            "Production is documented as air-gapped; OFFLINE_MODE=false would let "
            "components reach the internet.",
            "Set OFFLINE_MODE=true (or leave it unset) in production."))
    if not offline:
        return out

    allowed = _allowed_hosts(cfg)

    for key, component in ENDPOINT_SETTINGS:
        url = _text(cfg, key)
        kind = classify_url(url, allowed)
        if kind == "external":
            add(OfflineFinding(
                f"OFFLINE_EXTERNAL_{component.upper().replace(' ', '_')}_ENDPOINT", key,
                f"PRODUCTION OFFLINE POLICY VIOLATION: external {component} endpoint "
                f"detected: {url}",
                f"Point {key} at an internal service (a Compose service name, a "
                f"private address or localhost), or add the host to OFFLINE_ALLOWED_HOSTS "
                f"only if it is inside the air-gapped network."))
        elif kind == "invalid":
            add(OfflineFinding(
                f"OFFLINE_INVALID_{component.upper().replace(' ', '_')}_ENDPOINT", key,
                f"{key}={url!r} is not a URL that can be checked against the offline policy.",
                f"Give {key} a full URL such as http://service:port/v1."))

    for key, what in PERMISSION_FLAGS:
        if _truthy(getattr(cfg, key, False)):
            add(OfflineFinding(
                f"OFFLINE_{key}", key,
                f"{key}=true permits {what} while the deployment is offline.",
                f"Set {key}=false in production."))

    provider = _text(cfg, "LLM_PROVIDER", "ollama").lower()
    if provider in CLOUD_LLM_PROVIDERS:
        add(OfflineFinding(
            "OFFLINE_CLOUD_LLM_PROVIDER", "LLM_PROVIDER",
            f"PRODUCTION OFFLINE POLICY VIOLATION: LLM_PROVIDER={provider!r} is a hosted "
            f"inference service.",
            "Use LLM_PROVIDER=ollama, vllm or nim_local with a local LLM_BASE_URL."))
    elif provider and provider not in LOCAL_LLM_PROVIDERS:
        add(OfflineFinding(
            "OFFLINE_UNKNOWN_LLM_PROVIDER", "LLM_PROVIDER",
            f"LLM_PROVIDER={provider!r} is not a known local provider.",
            "Use one of: " + ", ".join(sorted(LOCAL_LLM_PROVIDERS)) + "."))
    if _text(cfg, "LLM_DEV_PROVIDER"):
        add(OfflineFinding(
            "OFFLINE_DEV_PROVIDER_SET", "LLM_DEV_PROVIDER",
            "LLM_DEV_PROVIDER selects the hosted development provider.",
            "Unset LLM_DEV_PROVIDER in production."))

    embedding = _text(cfg, "EMBEDDING_PROVIDER", "local").lower()
    if embedding and embedding not in LOCAL_EMBEDDING_PROVIDERS:
        add(OfflineFinding(
            "OFFLINE_EXTERNAL_EMBEDDING_PROVIDER", "EMBEDDING_PROVIDER",
            f"PRODUCTION OFFLINE POLICY VIOLATION: EMBEDDING_PROVIDER={embedding!r} "
            f"is not a local embedding provider.",
            "Set EMBEDDING_PROVIDER=local (the bundled ONNX MiniLM) or a local service."))

    stt = _text(cfg, "STT_PROVIDER", "none").lower()
    if stt and stt not in LOCAL_STT_PROVIDERS:
        add(OfflineFinding(
            "OFFLINE_EXTERNAL_STT_PROVIDER", "STT_PROVIDER",
            f"PRODUCTION OFFLINE POLICY VIOLATION: STT_PROVIDER={stt!r} is not local.",
            "Use STT_PROVIDER=none, local, whisper, faster_whisper or riva."))

    if _truthy(getattr(cfg, "SQL_AGENT_OPIK_ENABLED", False)):
        add(OfflineFinding(
            "OFFLINE_OPIK_ENABLED", "SQL_AGENT_OPIK_ENABLED",
            "The Opik tracer ships prompts, SQL and rows to a tracing service.",
            "Set SQL_AGENT_OPIK_ENABLED=false in production."))

    for key in ("NVIDIA_NIM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HF_TOKEN",
                "HUGGINGFACE_HUB_TOKEN", "OPIK_API_KEY"):
        present = bool(_text(cfg, key)) or bool((env or {}).get(key, "").strip())
        if present:
            add(OfflineFinding(
                "OFFLINE_CLOUD_CREDENTIAL_PRESENT", key,
                f"A credential for an external service ({key}) is present on an "
                f"offline deployment; nothing should need it.",
                f"Remove {key} from the production environment.",
                severity="warn"))

    if env is not None:
        for switch in OFFLINE_ENV_SWITCHES:
            if not _truthy(env.get(switch, "")):
                add(OfflineFinding(
                    "OFFLINE_LIBRARY_SWITCH_UNSET", switch,
                    f"{switch} is not set; a model library may try to reach the hub "
                    f"before it finds the local file.",
                    f"Export {switch}=1 in the production container environment.",
                    severity="warn"))

    probe = artifact_probe or os.path.exists
    for key, label in (("EMBEDDING_MODEL_PATH", "embedding model"),
                       ("DETECTION_MODEL", "detection model"),
                       ("RECOGNITION_MODEL", "recognition model"),
                       ("STT_MODEL_PATH", "STT model")):
        path = _text(cfg, key)
        if key == "STT_MODEL_PATH" and stt in ("none", ""):
            continue
        if path and not probe(path):
            add(OfflineFinding(
                "OFFLINE_ARTIFACT_MISSING", key,
                f"The {label} is not present at {path}; an offline deployment cannot "
                f"download it.",
                "Import the offline bundle (scripts/import_offline_bundle.sh) before "
                "starting, or correct the path."))
    manifest = _text(cfg, "OFFLINE_BUNDLE_MANIFEST")
    if manifest and not probe(manifest):
        add(OfflineFinding(
            "OFFLINE_BUNDLE_MANIFEST_MISSING", "OFFLINE_BUNDLE_MANIFEST",
            f"OFFLINE_BUNDLE_MANIFEST={manifest} does not exist, so the artifacts "
            f"cannot be verified.",
            "Run scripts/verify_offline_bundle.sh, or unset the setting."))
    return out


def startup_checklist(
    cfg: Any,
    *,
    production: bool,
    env: Optional[Mapping[str, str]] = None,
    artifact_probe: Optional[Callable[[str], bool]] = None,
    reachability: Optional[Mapping[str, Tuple[bool, str]]] = None,
) -> List[Check]:
    """The ``[PASS]``/``[FAIL]`` list. Pure: reachability results are handed
    in by the caller (the readiness endpoint probes services; this function
    never opens a socket)."""
    findings = collect_offline_violations(cfg, production=production, env=env,
                                          artifact_probe=artifact_probe)
    fatal = [f for f in findings if f.severity == "fatal"]
    by_key = {}
    for f in fatal:
        by_key.setdefault(f.key, []).append(f)
    checks: List[Check] = []
    offline = offline_mode(cfg, production)
    checks.append(Check("offline mode", offline or not production,
                        "OFFLINE_MODE=true" if offline else "development (online allowed)"))

    external_endpoints = [f for f in fatal if f.code.startswith("OFFLINE_EXTERNAL_") and f.code.endswith("_ENDPOINT")]
    checks.append(Check("no external inference endpoint configured",
                        not any(f.key in ("LLM_BASE_URL", "OLLAMA_BASE_URL", "EMBEDDING_BASE_URL")
                                for f in external_endpoints)
                        and not any(f.code in ("OFFLINE_CLOUD_LLM_PROVIDER", "OFFLINE_DEV_PROVIDER_SET",
                                               "OFFLINE_EXTERNAL_EMBEDDING_PROVIDER") for f in fatal),
                        "; ".join(f.message for f in fatal if f.key in ("LLM_BASE_URL", "OLLAMA_BASE_URL",
                                                                        "EMBEDDING_BASE_URL", "LLM_PROVIDER",
                                                                        "LLM_DEV_PROVIDER", "EMBEDDING_PROVIDER"))))
    checks.append(Check("no external telemetry endpoint configured",
                        not any(f.key in ("OTEL_EXPORTER_ENDPOINT", "OPIK_URL_OVERRIDE",
                                          "SQL_AGENT_OPIK_ENABLED", "ALLOW_EXTERNAL_TELEMETRY")
                                for f in fatal)))
    checks.append(Check("MCP and vector store internal",
                        not any(f.key in ("MCP_SQL_URL", "MILVUS_URI") for f in fatal)))
    checks.append(Check("STT local",
                        not any(f.key in ("STT_BASE_URL", "STT_PROVIDER") for f in fatal)))
    checks.append(Check("no model download permitted",
                        not any(f.key in ("ALLOW_MODEL_DOWNLOADS", "ALLOW_EXTERNAL_APIS") for f in fatal)))
    artifact_failures = [f for f in fatal if f.code in ("OFFLINE_ARTIFACT_MISSING",
                                                          "OFFLINE_BUNDLE_MANIFEST_MISSING")]
    checks.append(Check("required model artifacts found", not artifact_failures,
                        "; ".join(f"{f.key} missing" for f in artifact_failures)))
    checks.append(Check("offline policy validated", not fatal,
                        f"{len(fatal)} violation(s)" if fatal else ""))
    for name, (ok, detail) in sorted((reachability or {}).items()):
        checks.append(Check(f"{name} reachable", bool(ok), detail))
    return checks


def format_checklist(checks: Sequence[Check]) -> str:
    return "\n".join(c.line() for c in checks)
