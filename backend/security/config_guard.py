"""Fail-closed production configuration validation.

Production must refuse to start on an unsafe configuration rather than warn and
continue. This module collects *every* violation in one pass and reports them
without ever rendering a secret value.

Run as a preflight:

    python -m backend.security.config_guard

Exit codes follow sysexits.h: 0 = ok, 78 (EX_CONFIG) = configuration is unsafe.

Design constraint: `collect_violations` reads its input exclusively through
`getattr(cfg, NAME, default)` and never touches os.environ or a module-level
settings import. That is what allows tests to pass a plain SimpleNamespace and
exercise every rule without mutating the environment or needing a container.
"""

from __future__ import annotations

import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from typing import (Any, Callable, Iterable, List, Mapping, Optional,
                    Sequence, Set, Tuple)

from backend.security.origins import parse_origins
from backend.security.redaction import (
    SECRET_SETTINGS,
    redact_database_url,
    url_password,
    url_username,
)

EX_CONFIG = 78

# Minimum JWT secret length. `openssl rand -base64 48` yields 64 characters;
# 48 keeps headroom over HS256's 256-bit useful key size.
MIN_JWT_SECRET_LENGTH = 48

_KNOWN_BAD_SECRETS = frozenset({
    "your-secret-key-change-in-production",
    "your-secret-key",
    "my-secret-key",
    "change-me", "changeme", "please-change-me",
    "secret", "secret-key", "supersecret", "topsecret",
    "insecure", "django-insecure",
    "dev", "development", "test", "testing", "local",
    "password", "admin", "jwt-secret", "jwtsecret", "keyboardcat",
})

_DEFAULT_DB_PASSWORDS = frozenset({
    "", "admin", "postgres", "password", "passwd", "changeme", "change-me",
    "root", "secret", "123456", "12345678", "pgpassword", "docker", "test",
})

_DEFAULT_ADMIN_PASSWORDS = frozenset({
    "", "admin", "admin123", "administrator", "password", "password1",
    "passw0rd", "changeme", "change-me", "letmein", "root", "12345678",
    "qwerty", "secret", "test", "test123",
})

_SAFE_JWT_ALGORITHMS = frozenset({
    "HS256", "HS384", "HS512",
    "RS256", "RS384", "RS512",
    "ES256", "ES384", "ES512",
})

_SUPERUSER_DB_ROLES = frozenset({"postgres", "root"})

_SIMPLE_PATTERNS = (
    re.compile(r"^[0-9]+$"),
    re.compile(r"^[a-z]+$"),
    re.compile(r"^[A-Z]+$"),
    re.compile(r"^[a-z0-9]{1,8}$"),
)

_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9._~%-]+(?::\d{1,5})?$")

# Paths that are computed, not configured. config.Settings owns the derivation;
# this table mirrors it as data so the guard can state where a path SHOULD
# resolve even when handed a partially built namespace. tests/test_config_guard
# asserts the two agree, so a change in config.py that is not reflected here is
# caught rather than shipped.
#
#   name -> (root setting, path components appended to it)
DERIVED_PATHS = {
    "FACES_DIR":           ("STORAGE_DIR",     ("faces",)),
    "UPLOAD_TEMP_DIR":     ("STORAGE_DIR",     ("faces", ".incoming")),
    "ARTIFACTS_DIR":       ("STORAGE_DIR",     ("artifacts",)),
    "CONVERSATION_CACHE_DIR": ("STORAGE_DIR",   ("conversation_cache",)),
    "ARTIFACTS_TEMP_DIR":  ("STORAGE_DIR",     ("artifacts", ".incoming")),
    "PENDING_UPLOAD_DIR":  ("STORAGE_DIR",     ("pending",)),
    "WEBHOOK_IMAGES_DIR":  ("STORAGE_DIR",     ("debug", "webhook_images")),
    "CROPPED_IMAGES_DIR":  ("STORAGE_DIR",     ("debug", "cropped")),
    "MODEL_CANDIDATE_DIR": ("ML_ARTIFACT_DIR", ("candidates",)),
    "MAP_PRODUCTION_DIR":  ("MAP_DATA_DIR",     ("production",)),
    "MAP_METADATA_DIR":    ("MAP_DATA_DIR",     ("metadata",)),
}

# Settings that must never be mutable at runtime through the admin settings API.
# backend/core/runtime_settings.apply_to_runtime does a literal setattr on the
# live settings object, so without this an admin token could flip ENVIRONMENT
# to "development" and neutralize every check in this module.
SECURITY_CRITICAL_KEYS = frozenset({
    "ENVIRONMENT", "DEBUG", "WORKERS", "USE_GPU",
    "ALLOW_MULTI_WORKER", "ALLOW_CPU_FALLBACK",
    "JWT_SECRET_KEY", "JWT_SECRET_KEY_FILE", "JWT_ALGORITHM",
    "JWT_ISSUER", "JWT_AUDIENCE", "ACCESS_TOKEN_EXPIRE_MINUTES",
    "AUTH_COOKIE_SECURE", "AUTH_COOKIE_SAMESITE", "AUTH_COOKIE_HOST_PREFIX",
    "AUTH_ALLOWED_ORIGINS", "AUTH_SAME_HOST_ORIGIN_TRUSTED",
    "AUTH_TRUST_PROXY_HEADERS", "AUTH_RATE_LIMIT_ENABLED",
    "CORS_ORIGINS", "ENABLE_API_DOCS",
    "DATABASE_URL", "DATABASE_URL_FILE",
    "POSTGRES_PASSWORD", "POSTGRES_PASSWORD_FILE", "POSTGRES_USER",
    "REDIS_URL", "REDIS_URL_FILE",
    "BOOTSTRAP_ADMIN_ENABLED", "BOOTSTRAP_ADMIN_PASSWORD",
    "BOOTSTRAP_ADMIN_PASSWORD_FILE", "BOOTSTRAP_ADMIN_REQUIRE_ROTATION",
    "MIGRATIONS_MODE", "MIGRATIONS_EXPECTED_HEAD",
    # Storage locations. Repointing face storage at runtime would strand every
    # persisted relative path and let an admin token write images outside the
    # audited tree; these are fixed at boot and validated by the preflight.
    "FACES_DIR", "STORAGE_DIR", "IDENTITY_INDEX_DB_PATH",
    # Ingest authentication. Without these here, an admin token could set
    # WEBHOOK_AUTH_MODE=off at runtime and reopen the unauthenticated webhook
    # that this module refuses to start with.
    "WEBHOOK_API_KEYS", "WEBHOOK_API_KEYS_FILE", "WEBHOOK_AUTH_MODE",
    "WEBHOOK_AUTH_HEADER", "WEBHOOK_AUTH_INSECURE_ACK",
    "WEBHOOK_AUTH_TOKEN", "WEBHOOK_AUTH_TOKEN_FILE",
    # The revocation latency for issued credentials. An admin token that can set
    # this to 86400 can make a revoked credential keep working for a day, which
    # is the same class of hole as flipping WEBHOOK_AUTH_MODE to off.
    "WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS",
    # The development-only hosted LLM switch. Not listed here, an admin token
    # could persist LLM_DEV_PROVIDER=nim to the settings table and the next
    # boot would apply it (at_boot lifts the apply-mode gate) — routing SQL
    # agent prompts to an external endpoint without touching the environment.
    "LLM_DEV_PROVIDER", "NVIDIA_NIM_API_KEY", "NVIDIA_NIM_BASE_URL",
})


@dataclass(frozen=True)
class ConfigViolation:
    code: str
    setting: str
    message: str
    remedy: str
    severity: str = "fatal"   # "fatal" blocks startup; "warn" is advisory


class ConfigGuardError(RuntimeError):
    def __init__(self, violations: Sequence[ConfigViolation]) -> None:
        self.violations: Tuple[ConfigViolation, ...] = tuple(violations)
        codes = ", ".join(v.code for v in self.violations if v.severity == "fatal")
        super().__init__(f"unsafe production configuration: {codes}")

    def report(self) -> str:
        return format_report(self.violations)


GpuProbe = Callable[[], Tuple[bool, str]]


# --------------------------------------------------------------------------
# Strength assessment
# --------------------------------------------------------------------------

def shannon_entropy(value: str) -> float:
    """Bits of entropy per character of the observed character distribution.

    A base64 secret scores ~5.5; "aaaa..." scores 0.0.
    """
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum(
        (n / length) * math.log2(n / length) for n in counts.values()
    )


def _is_repeated_substring(value: str) -> bool:
    """True for values like 'abcabcabc' that look long but are not."""
    length = len(value)
    for size in range(1, length // 2 + 1):
        if length % size == 0 and value == value[:size] * (length // size):
            return True
    return False


def assess_secret_strength(value: str) -> List[str]:
    """Reasons a secret is too weak. Empty list means acceptable."""
    reasons: List[str] = []
    if len(set(value)) < 16:
        reasons.append(f"only {len(set(value))} distinct characters (need 16+)")
    entropy = shannon_entropy(value)
    if entropy < 3.0:
        reasons.append(f"character entropy {entropy:.2f} bits/char (need 3.0+)")
    if any(pattern.match(value) for pattern in _SIMPLE_PATTERNS):
        reasons.append("single character class")
    if _is_repeated_substring(value):
        reasons.append("a short string repeated to fake length")
    return reasons


def assess_admin_password(value: str) -> List[str]:
    """Reasons a bootstrap admin password is unacceptable."""
    reasons: List[str] = []
    if not value:
        return ["empty"]
    if value.strip().lower() in _DEFAULT_ADMIN_PASSWORDS:
        reasons.append("a well-known default password")
    if len(value) < 12:
        reasons.append(f"{len(value)} characters (need 12+)")
    if len(set(value)) < 6:
        reasons.append(f"only {len(set(value))} distinct characters")
    return reasons


# --------------------------------------------------------------------------
# GPU probe
# --------------------------------------------------------------------------

def default_gpu_probe() -> Tuple[bool, str]:
    """Whether ONNX Runtime can actually use CUDA on this machine."""
    try:
        import onnxruntime as ort

        providers = list(ort.get_available_providers())
    except Exception as exc:
        return False, f"onnxruntime unavailable: {type(exc).__name__}"

    if "CUDAExecutionProvider" not in providers:
        return False, f"CUDAExecutionProvider not registered (available: {providers})"
    return True, "CUDAExecutionProvider available"


# --------------------------------------------------------------------------
# Storage probe
# --------------------------------------------------------------------------

def default_storage_probe(path: str) -> Tuple[bool, str]:
    """Whether the process can actually write face images to `path`.

    A read-only or absent storage root does not fail at startup — it fails at
    the first enrollment, as a 500 from deep inside the image pipeline, hours
    after deploy. `.env` once pointed the gallery at ./assets, which is mounted
    read-only, and that is exactly how it surfaced.

    Injected rather than called directly so the rules stay unit-testable
    without a filesystem, mirroring `gpu_probe`.
    """
    import os

    if not os.path.isdir(path):
        parent = os.path.dirname(path.rstrip(os.sep)) or os.sep
        if not os.path.isdir(parent):
            return False, f"neither {path} nor its parent {parent} exists"
        if not os.access(parent, os.W_OK):
            return False, f"{path} does not exist and {parent} is not writable"
        return True, f"{path} will be created under {parent}"

    if not os.access(path, os.W_OK | os.X_OK):
        return False, f"{path} exists but is not writable by this process"
    return True, f"{path} is writable"


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def _is_production(cfg: Any, override: Optional[str] = None) -> bool:
    value = override if override is not None else getattr(cfg, "ENVIRONMENT", "")
    return str(value).strip().lower() in ("production", "prod")


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def collect_violations(
    cfg: Any,
    *,
    environment: Optional[str] = None,
    gpu_probe: Optional[GpuProbe] = None,
    env: Optional[Mapping[str, str]] = None,
    storage_probe: Optional[Callable[[str], Tuple[bool, str]]] = None,
) -> List[ConfigViolation]:
    """Every configuration problem, collected in a single pass.

    Never short-circuits: an operator fixing one issue at a time across five
    restarts is exactly the failure mode this avoids.
    """
    out: List[ConfigViolation] = []
    add = out.append
    production = _is_production(cfg, environment)

    # ---- JWT signing key -------------------------------------------------
    secret = str(getattr(cfg, "JWT_SECRET_KEY", "") or "")
    if production:
        if not secret.strip():
            add(ConfigViolation(
                "JWT_SECRET_MISSING", "JWT_SECRET_KEY",
                "No JWT signing key is configured.",
                "openssl rand -base64 48  → set JWT_SECRET_KEY_FILE",
            ))
        elif secret.strip().lower() in _KNOWN_BAD_SECRETS:
            add(ConfigViolation(
                "JWT_SECRET_PLACEHOLDER", "JWT_SECRET_KEY",
                "Still the placeholder shipped in config.py. Anyone with the "
                "source can forge an administrator token.",
                "openssl rand -base64 48  → set JWT_SECRET_KEY_FILE",
            ))
        else:
            if len(secret) < MIN_JWT_SECRET_LENGTH:
                add(ConfigViolation(
                    "JWT_SECRET_TOO_SHORT", "JWT_SECRET_KEY",
                    f"Signing key is {len(secret)} characters; "
                    f"{MIN_JWT_SECRET_LENGTH} or more are required.",
                    "openssl rand -base64 48",
                ))
            weak = assess_secret_strength(secret)
            if weak:
                add(ConfigViolation(
                    "JWT_SECRET_LOW_ENTROPY", "JWT_SECRET_KEY",
                    "Signing key is predictable: " + "; ".join(weak) + ".",
                    "openssl rand -base64 48",
                ))

    algorithm = str(getattr(cfg, "JWT_ALGORITHM", "HS256") or "").strip().upper()
    if algorithm not in _SAFE_JWT_ALGORITHMS:
        add(ConfigViolation(
            "JWT_ALGORITHM_UNSAFE", "JWT_ALGORITHM",
            f"Algorithm {algorithm!r} is not an accepted signing algorithm.",
            "Use HS256 (or an RS/ES algorithm with a key pair).",
        ))

    # ---- Database --------------------------------------------------------
    if production:
        db_password = str(getattr(cfg, "POSTGRES_PASSWORD", "") or "")
        if db_password.strip().lower() in _DEFAULT_DB_PASSWORDS:
            add(ConfigViolation(
                "DB_PASSWORD_DEFAULT", "POSTGRES_PASSWORD",
                "Database password is a well-known default "
                f"({len(db_password)} characters checked, value not shown).",
                "openssl rand -base64 32  → set POSTGRES_PASSWORD_FILE",
            ))

        database_url = str(getattr(cfg, "DATABASE_URL", "") or "")
        embedded = url_password(database_url)
        if embedded is not None and embedded.strip().lower() in _DEFAULT_DB_PASSWORDS:
            add(ConfigViolation(
                "DATABASE_URL_DEFAULT_PASSWORD", "DATABASE_URL",
                "DATABASE_URL embeds a well-known default password "
                f"({redact_database_url(database_url)}).",
                "Rotate the role password and update DATABASE_URL.",
            ))
        db_user = (url_username(database_url) or "").lower()
        if db_user in _SUPERUSER_DB_ROLES:
            add(ConfigViolation(
                "DATABASE_URL_SUPERUSER", "DATABASE_URL",
                f"Application connects as the {db_user!r} superuser, so every "
                "in-application authorization check can be bypassed by "
                "connecting directly.",
                "Create a least-privilege role (db/roles.sql) and use fr_app.",
            ))

        redis_url = str(getattr(cfg, "REDIS_URL", "") or "")
        redis_host = ""
        try:
            from urllib.parse import urlsplit

            redis_host = (urlsplit(redis_url).hostname or "").lower()
        except Exception:
            redis_host = ""
        if not url_password(redis_url) and redis_host not in ("localhost", "127.0.0.1", "::1", ""):
            add(ConfigViolation(
                "REDIS_NO_AUTH", "REDIS_URL",
                "Redis has no credentials but holds sessions, login rate-limit "
                "counters and the token revocation denylist.",
                "Enable a Redis ACL user and set REDIS_URL_FILE.",
            ))

        # LLM-generated SQL must not execute with the application's own write
        # privileges. The AST guard is application code and can have bugs;
        # role grants cannot be overridden by the query text.
        agent_user = str(getattr(cfg, "SQL_AGENT_DB_USER", "") or "").strip()
        app_user = str(getattr(cfg, "POSTGRES_USER", "") or "").strip()
        if not agent_user:
            add(ConfigViolation(
                "SQL_AGENT_DB_ROLE_MISSING", "SQL_AGENT_DB_USER",
                "LLM-generated SQL would execute as the application's database "
                "role, which can write. A validator bug would then be a data-loss "
                "bug.",
                "Set SQL_AGENT_DB_USER=fr_readonly and SQL_AGENT_DB_PASSWORD_FILE "
                "(see db/roles.sql).",
            ))
        elif agent_user == app_user:
            add(ConfigViolation(
                "SQL_AGENT_DB_ROLE_SHARED", "SQL_AGENT_DB_USER",
                "The SQL agent shares the application's database role, so "
                "generated SQL inherits its write privileges.",
                "Point SQL_AGENT_DB_USER at a SELECT-only role such as fr_readonly.",
            ))

        # The development-only hosted LLM provider (NVIDIA NIM) sends the
        # database schema and every user question to an external endpoint.
        # This system queries biometric data and is documented as running
        # fully offline, so in production that is data exfiltration, not a
        # configuration preference. No acknowledgement escape — like
        # WEBHOOK_AUTH_DISABLED, "on" is not accepted in production under any
        # justification. (The model registry independently registers no NIM
        # model in production; this rule exists so the misconfiguration stops
        # the boot loudly instead of being silently ignored.)
        dev_provider = str(getattr(cfg, "LLM_DEV_PROVIDER", "") or "").strip()
        if dev_provider:
            add(ConfigViolation(
                "LLM_EXTERNAL_PROVIDER_IN_PRODUCTION", "LLM_DEV_PROVIDER",
                f"LLM_DEV_PROVIDER={dev_provider!r} selects a hosted LLM "
                "endpoint. The SQL agent's prompts carry the schema and user "
                "questions about biometric data; a hosted provider sends them "
                "off-box from a system documented as fully offline.",
                "Unset LLM_DEV_PROVIDER. Production uses the local Ollama "
                "models only (OLLAMA_MODEL / OLLAMA_SQL_MODEL).",
            ))
        if str(getattr(cfg, "NVIDIA_NIM_API_KEY", "") or "").strip():
            add(ConfigViolation(
                "LLM_EXTERNAL_API_KEY_PRESENT", "NVIDIA_NIM_API_KEY",
                "A credential for an external LLM endpoint is configured in "
                "production. Nothing uses it while LLM_DEV_PROVIDER is unset, "
                "but a live credential on a box that must not call out is an "
                "accident waiting for a caller.",
                "Remove NVIDIA_NIM_API_KEY from the production environment.",
                severity="warn",
            ))

    # ---- Cookies ---------------------------------------------------------
    cookie_secure = _truthy(getattr(cfg, "AUTH_COOKIE_SECURE", False))
    samesite = str(getattr(cfg, "AUTH_COOKIE_SAMESITE", "lax") or "").strip().lower()

    if production and not cookie_secure:
        add(ConfigViolation(
            "AUTH_COOKIE_INSECURE", "AUTH_COOKIE_SECURE",
            "Session cookie is sent without the Secure flag, so it travels in "
            "cleartext on any plain-HTTP request.",
            "Terminate TLS, then set AUTH_COOKIE_SECURE=true.",
        ))

    if samesite not in ("lax", "strict", "none"):
        add(ConfigViolation(
            "AUTH_COOKIE_SAMESITE_INVALID", "AUTH_COOKIE_SAMESITE",
            f"SameSite value {samesite!r} is not one of lax, strict, none.",
            "Set AUTH_COOKIE_SAMESITE=lax.",
        ))
    elif samesite == "none" and not cookie_secure:
        add(ConfigViolation(
            "AUTH_COOKIE_SAMESITE_NONE_WITHOUT_SECURE", "AUTH_COOKIE_SAMESITE",
            "SameSite=None without Secure — browsers reject the cookie "
            "outright, so login silently fails.",
            "Set AUTH_COOKIE_SECURE=true or use SameSite=lax.",
        ))

    # ---- Origins ---------------------------------------------------------
    auth_origins = parse_origins(getattr(cfg, "AUTH_ALLOWED_ORIGINS", ""))
    if production:
        if not auth_origins:
            add(ConfigViolation(
                "AUTH_ALLOWED_ORIGINS_EMPTY", "AUTH_ALLOWED_ORIGINS",
                "No credential-submission origins configured, so login-CSRF "
                "validation falls back to trusting the request Host header.",
                "AUTH_ALLOWED_ORIGINS=https://face-detector.internal",
            ))
        if "*" in auth_origins:
            add(ConfigViolation(
                "AUTH_ALLOWED_ORIGINS_WILDCARD", "AUTH_ALLOWED_ORIGINS",
                "Wildcard credential-submission origin accepts logins "
                "initiated by any site.",
                "List each internal hostname explicitly.",
            ))
        for origin in auth_origins:
            if origin == "*":
                continue
            host = origin.split("//")[-1].split(":")[0].lower()
            if origin.startswith("http://") and host not in ("localhost", "127.0.0.1", "::1"):
                add(ConfigViolation(
                    "AUTH_ALLOWED_ORIGINS_INSECURE_SCHEME", "AUTH_ALLOWED_ORIGINS",
                    f"Origin {origin} uses plain HTTP, so credentials would be "
                    "accepted from an unencrypted page.",
                    "Use https:// for every non-loopback origin.",
                ))
        if _truthy(getattr(cfg, "AUTH_SAME_HOST_ORIGIN_TRUSTED", True)):
            add(ConfigViolation(
                "AUTH_SAME_HOST_ORIGIN_TRUSTED", "AUTH_SAME_HOST_ORIGIN_TRUSTED",
                "The request Host header is trusted as a credential-submission "
                "origin, which lets a spoofed Host self-approve.",
                "Set AUTH_SAME_HOST_ORIGIN_TRUSTED=false once "
                "AUTH_ALLOWED_ORIGINS lists every real hostname.",
            ))

    cors_origins = parse_origins(getattr(cfg, "CORS_ORIGINS", ""))
    if production and "*" in cors_origins:
        add(ConfigViolation(
            "CORS_WILDCARD_WITH_CREDENTIALS", "CORS_ORIGINS",
            "Wildcard CORS origin. Combined with credentialed requests the "
            "browser would let any site read authenticated responses.",
            "CORS_ORIGINS=https://face-detector.internal",
        ))
    for origin in cors_origins:
        if origin != "*" and not _ORIGIN_RE.match(origin):
            add(ConfigViolation(
                "CORS_ORIGIN_MALFORMED", "CORS_ORIGINS",
                f"Origin {origin!r} is not a bare scheme://host[:port] value, "
                "so it will never match any browser Origin header.",
                "Remove paths, trailing slashes and quoting.",
            ))

    # ---- Runtime posture -------------------------------------------------
    if production and _truthy(getattr(cfg, "DEBUG", False)):
        add(ConfigViolation(
            "DEBUG_ENABLED", "DEBUG",
            "Debug mode leaks stack traces and internal state to clients.",
            "DEBUG=false",
        ))

    if production and _truthy(getattr(cfg, "ENABLE_API_DOCS", False)):
        add(ConfigViolation(
            "DOCS_ENABLED", "ENABLE_API_DOCS",
            "/docs, /redoc and /openapi.json publish the full API surface "
            "unauthenticated.",
            "ENABLE_API_DOCS=false",
        ))

    try:
        workers = int(getattr(cfg, "WORKERS", 1) or 1)
    except (TypeError, ValueError):
        workers = 1
    if production and workers > 1 and not _truthy(getattr(cfg, "ALLOW_MULTI_WORKER", False)):
        add(ConfigViolation(
            "WORKERS_GT_ONE", "WORKERS",
            f"WORKERS={workers} but some process-local state is still not "
            "shared across processes: runtime settings, the SQL-agent "
            "cancellation registry, the model-training single-flight guard, "
            "webhook dedup, FAISS autosave and the in-process revocation "
            "fallback live in one process. (The intelligence relationship/"
            "threshold single-flight guards and threat assessments ARE "
            "multi-worker safe via Redis locks + DB idempotency when Redis "
            "is available.) Admin settings would apply to 1 worker in "
            f"{workers}, and the remaining 'single-flight' paths could run "
            f"{workers} times.",
            "Set WORKERS=1, or accept the remaining caveats (with Redis "
            "configured for the distributed locks) and set "
            "ALLOW_MULTI_WORKER=true.",
        ))

    if production and _truthy(getattr(cfg, "USE_GPU", False)):
        probe = gpu_probe or default_gpu_probe
        try:
            usable, detail = probe()
        except Exception as exc:
            usable, detail = False, f"probe failed: {type(exc).__name__}"
        if not usable and not _truthy(getattr(cfg, "ALLOW_CPU_FALLBACK", True)):
            add(ConfigViolation(
                "GPU_WITHOUT_CUDA_PROVIDER", "USE_GPU",
                f"GPU mode requested but CUDA is unusable ({detail}). "
                "Inference would silently fall back to CPU while every log "
                "line still reports a healthy service.",
                "Fix the CUDA/onnxruntime-gpu pairing, or set "
                "ALLOW_CPU_FALLBACK=true to accept CPU inference.",
            ))
        elif not usable:
            add(ConfigViolation(
                "GPU_CPU_FALLBACK_ACTIVE", "USE_GPU",
                f"GPU mode requested but CUDA is unusable ({detail}); "
                "inference is running on the CPU.",
                "Set ALLOW_CPU_FALLBACK=false to make this fatal.",
                severity="warn",
            ))

    # ---- Bootstrap admin -------------------------------------------------
    bootstrap_password = str(getattr(cfg, "BOOTSTRAP_ADMIN_PASSWORD", "") or "")
    if production and bootstrap_password:
        reasons = assess_admin_password(bootstrap_password)
        if reasons:
            add(ConfigViolation(
                "BOOTSTRAP_ADMIN_PASSWORD_WEAK", "BOOTSTRAP_ADMIN_PASSWORD",
                "Bootstrap administrator password is unacceptable: "
                + "; ".join(reasons) + ".",
                "openssl rand -base64 24  → set BOOTSTRAP_ADMIN_PASSWORD_FILE",
            ))

    # ---- Derived filesystem layout --------------------------------------
    # FACES_DIR, UPLOAD_TEMP_DIR, WEBHOOK_IMAGES_DIR and CROPPED_IMAGES_DIR are
    # computed from STORAGE_DIR (see config.Settings) and are no longer settable.
    #
    # A value left in the environment is therefore INERT, and silently ignoring
    # it is the worst outcome: the operator reads their compose file, believes
    # face storage lives where it says, and is wrong. (.env once shipped
    # FACES_DIR=./assets/faces while compose set /app/storage/faces — the two
    # halves of the application disagreed about where a person's photos lived,
    # and ./assets is a read-only mount, so enrollment simply failed there.)
    #
    # Enforced in EVERY environment: a stale override is a data-integrity fault
    # anywhere, not only in production.
    import os as _os

    storage_dir = str(getattr(cfg, "STORAGE_DIR", "") or "")
    if not storage_dir:
        add(ConfigViolation(
            "STORAGE_DIR_MISSING", "STORAGE_DIR",
            "No storage root is configured; every derived path depends on it.",
            "STORAGE_DIR=/app/storage",
        ))
    elif not _os.path.isabs(storage_dir):
        add(ConfigViolation(
            "STORAGE_DIR_RELATIVE", "STORAGE_DIR",
            f"STORAGE_DIR={storage_dir!r} is relative, so every stored path "
            "depends on the process working directory — which differs between "
            "gunicorn, alembic and a maintenance script.",
            "STORAGE_DIR=/app/storage",
        ))
    elif storage_probe is not None:
        # Fatal in every environment: a gallery that cannot be written to is a
        # broken deployment, and finding out at the first enrollment instead of
        # at startup costs a debugging session.
        writable, detail = storage_probe(storage_dir)
        if not writable:
            add(ConfigViolation(
                "STORAGE_DIR_NOT_WRITABLE", "STORAGE_DIR",
                f"Face storage is not usable: {detail}. Enrollment and every "
                "saved detection image write under this root.",
                "Mount it read-write, or chown it to the service account "
                "(the container runs as uid 1000 / appuser).",
            ))

    # `env` is passed in rather than read from os.environ, preserving this
    # module's contract that every rule is exercisable with a SimpleNamespace
    # and an explicit mapping. Reading the process environment here would make
    # this one rule fire spuriously in every other test.
    _env = env if env is not None else {}
    for _name, (_root_attr, _parts) in DERIVED_PATHS.items():
        _present = _env.get(_name)
        if _present is None:
            continue
        # Recomputed from the root rather than read off cfg: the guard must be
        # able to say where a path SHOULD resolve even when cfg is a partially
        # built namespace that has no such attribute — otherwise "absent" and
        # "pointed somewhere wrong" become the same answer.
        _root = str(getattr(cfg, _root_attr, "") or "")
        _derived = _os.path.join(_root, *_parts) if _root else ""
        if _derived and _os.path.normpath(_present) == _os.path.normpath(_derived):
            severity = "warn"
            message = (f"{_name} is set in the environment but is now derived "
                       f"from {_root_attr}. The value matches, so nothing breaks "
                       f"— but it is inert and will not track a "
                       f"{_root_attr} change.")
        else:
            severity = "fatal"
            message = (f"{_name}={_present!r} is set in the environment, but "
                       f"{_name} is derived from {_root_attr} and resolves to "
                       f"{_derived!r}. The configured value has NO effect; "
                       f"anything relying on it is writing somewhere else.")
        add(ConfigViolation(
            "DERIVED_PATH_OVERRIDE", _name, message,
            f"Remove {_name} from the environment and set {_root_attr} instead.",
            severity=severity,
        ))

    # ---- Enrollment review bands -----------------------------------------
    # The two thresholds partition similarity into three actions: enroll
    # directly, ask the administrator, recommend an existing person. An
    # inverted or out-of-range pair does not degrade gracefully — it produces a
    # band that can never be reached, so one of the three outcomes silently
    # stops happening. The pool/display pair has the same property: a pool no
    # larger than the display count truncates candidates BEFORE per-identity
    # collapsing, so several embeddings of one person can crowd out every other
    # candidate and the operator reviews a list that is missing the right answer.
    #
    # Fatal in EVERY environment, and never silently corrected: clamping the
    # values at read time would mean the operator who typed them never learns
    # the configuration they wrote does not exist.
    def _enroll_number(name, default, cast):
        raw = getattr(cfg, name, default)
        try:
            return cast(raw), None
        except (TypeError, ValueError):
            return None, raw

    strong, strong_bad = _enroll_number("ENROLL_STRONG_MATCH_MIN", 0.75, float)
    floor, floor_bad = _enroll_number("ENROLL_CANDIDATE_MIN", 0.45, float)
    pool, pool_bad = _enroll_number("ENROLL_CANDIDATE_POOL", 25, int)
    shown, shown_bad = _enroll_number("ENROLL_MAX_CANDIDATES", 5, int)

    for _name, _bad in (("ENROLL_STRONG_MATCH_MIN", strong_bad),
                        ("ENROLL_CANDIDATE_MIN", floor_bad),
                        ("ENROLL_CANDIDATE_POOL", pool_bad),
                        ("ENROLL_MAX_CANDIDATES", shown_bad)):
        if _bad is not None:
            add(ConfigViolation(
                "ENROLL_THRESHOLD_NOT_A_NUMBER", _name,
                f"{_name}={_bad!r} is not a number.",
                "Set a numeric value (similarities are 0-1, counts are whole "
                "numbers).",
            ))

    if strong is not None and not (0.0 <= strong <= 1.0):
        add(ConfigViolation(
            "ENROLL_THRESHOLD_OUT_OF_RANGE", "ENROLL_STRONG_MATCH_MIN",
            f"ENROLL_STRONG_MATCH_MIN={strong} is outside 0-1. Cosine "
            "similarity between unit vectors cannot exceed 1, so this band "
            "would never be entered.",
            "ENROLL_STRONG_MATCH_MIN=0.75",
        ))
    if floor is not None and not (0.0 <= floor <= 1.0):
        add(ConfigViolation(
            "ENROLL_THRESHOLD_OUT_OF_RANGE", "ENROLL_CANDIDATE_MIN",
            f"ENROLL_CANDIDATE_MIN={floor} is outside 0-1.",
            "ENROLL_CANDIDATE_MIN=0.45",
        ))
    if (strong is not None and floor is not None
            and 0.0 <= strong <= 1.0 and 0.0 <= floor <= 1.0
            and floor > strong):
        add(ConfigViolation(
            "ENROLL_THRESHOLDS_INVERTED", "ENROLL_CANDIDATE_MIN",
            f"ENROLL_CANDIDATE_MIN={floor} is above "
            f"ENROLL_STRONG_MATCH_MIN={strong}. The 'uncertain' band between "
            "them is empty, so no upload could ever be sent for review — every "
            "match would be reported as a confident one.",
            "Set ENROLL_CANDIDATE_MIN <= ENROLL_STRONG_MATCH_MIN "
            "(defaults: 0.45 and 0.75).",
        ))

    # Advisory, not fatal: this is a calibration judgment an operator may make
    # deliberately, but it silently reopens the defect the review flow exists to
    # close, so it must never pass unremarked.
    recognition_min, recognition_bad = _enroll_number("SIMILARITY_THRESHOLD", 0.4, float)
    if (floor is not None and recognition_min is not None and recognition_bad is None
            and 0.0 <= floor <= 1.0 and floor > recognition_min):
        add(ConfigViolation(
            "ENROLL_CANDIDATE_MIN_ABOVE_RECOGNITION", "ENROLL_CANDIDATE_MIN",
            f"ENROLL_CANDIDATE_MIN={floor} is above SIMILARITY_THRESHOLD="
            f"{recognition_min}. Two faces scoring between them are the SAME "
            "person as far as recognition is concerned, but enrollment will not "
            "ask about them — so a second identity is created silently and "
            "recognition then reports either name for that face.",
            f"Set ENROLL_CANDIDATE_MIN to {recognition_min} or lower.",
            severity="warn",
        ))

    if shown is not None and shown < 1:
        add(ConfigViolation(
            "ENROLL_CANDIDATE_COUNT_INVALID", "ENROLL_MAX_CANDIDATES",
            f"ENROLL_MAX_CANDIDATES={shown} would show the administrator no "
            "candidates at all, leaving a review prompt with nothing to review.",
            "ENROLL_MAX_CANDIDATES=5",
        ))
    if pool is not None and shown is not None and pool <= shown:
        add(ConfigViolation(
            "ENROLL_CANDIDATE_POOL_TOO_SMALL", "ENROLL_CANDIDATE_POOL",
            f"ENROLL_CANDIDATE_POOL={pool} is not larger than "
            f"ENROLL_MAX_CANDIDATES={shown}. The pool is truncated before "
            "per-identity collapsing, so one person holding several embeddings "
            "can fill it and hide every other candidate.",
            "Set ENROLL_CANDIDATE_POOL well above ENROLL_MAX_CANDIDATES "
            "(defaults: 25 and 5).",
        ))

    # ---- Search retrieval floor vs display threshold ----------------------
    # SEARCH_RETRIEVAL_FLOOR is how deep the vector index is asked to look;
    # SIMILARITY_THRESHOLD is the bar a match must clear to be shown. Retrieval
    # has to sit at or below display, because display filters what retrieval
    # returned. Inverted, the display threshold silently stops meaning anything:
    # nothing between the two bars is ever retrieved, so raising it changes
    # nothing and lowering it cannot recover the matches it excluded.
    #
    # Same contract as the enrollment bands above: refuse, never clamp.
    retrieval, retrieval_bad = _enroll_number("SEARCH_RETRIEVAL_FLOOR", 0.2, float)
    if retrieval_bad is not None:
        add(ConfigViolation(
            "SEARCH_RETRIEVAL_FLOOR_NOT_A_NUMBER", "SEARCH_RETRIEVAL_FLOOR",
            f"SEARCH_RETRIEVAL_FLOOR={retrieval_bad!r} is not a number.",
            "Set a cosine similarity between 0 and 1 (default: 0.2).",
        ))
    elif retrieval is not None and not (0.0 <= retrieval <= 1.0):
        add(ConfigViolation(
            "SEARCH_RETRIEVAL_FLOOR_OUT_OF_RANGE", "SEARCH_RETRIEVAL_FLOOR",
            f"SEARCH_RETRIEVAL_FLOOR={retrieval} is outside 0-1.",
            "SEARCH_RETRIEVAL_FLOOR=0.2",
        ))
    elif (retrieval is not None and recognition_min is not None
            and recognition_bad is None and retrieval > recognition_min):
        add(ConfigViolation(
            "SEARCH_RETRIEVAL_FLOOR_ABOVE_DISPLAY", "SEARCH_RETRIEVAL_FLOOR",
            f"SEARCH_RETRIEVAL_FLOOR={retrieval} is above SIMILARITY_THRESHOLD="
            f"{recognition_min}. The index would never return a candidate the "
            "display threshold could admit, so lowering SIMILARITY_THRESHOLD "
            "would appear to do nothing.",
            f"Set SEARCH_RETRIEVAL_FLOOR to {recognition_min} or lower.",
        ))

    # ---- Search result depth ---------------------------------------------
    default_k, default_k_bad = _enroll_number("SEARCH_DEFAULT_TOP_K", 10, int)
    max_k, max_k_bad = _enroll_number("SEARCH_MAX_TOP_K", 100, int)
    for _name, _bad in (("SEARCH_DEFAULT_TOP_K", default_k_bad),
                        ("SEARCH_MAX_TOP_K", max_k_bad)):
        if _bad is not None:
            add(ConfigViolation(
                "SEARCH_TOP_K_NOT_A_NUMBER", _name,
                f"{_name}={_bad!r} is not a whole number.",
                "Set a positive integer (defaults: 10 and 100).",
            ))
    if default_k is not None and default_k < 1:
        add(ConfigViolation(
            "SEARCH_TOP_K_INVALID", "SEARCH_DEFAULT_TOP_K",
            f"SEARCH_DEFAULT_TOP_K={default_k} would return no matches at all.",
            "SEARCH_DEFAULT_TOP_K=10",
        ))
    if default_k is not None and max_k is not None and default_k > max_k:
        add(ConfigViolation(
            "SEARCH_TOP_K_INVERTED", "SEARCH_DEFAULT_TOP_K",
            f"SEARCH_DEFAULT_TOP_K={default_k} is above SEARCH_MAX_TOP_K={max_k}, "
            "so every search that does not name a depth would be rejected by "
            "the ceiling applied to the one that does.",
            "Set SEARCH_DEFAULT_TOP_K <= SEARCH_MAX_TOP_K (defaults: 10 and 100).",
        ))

    # ---- Webhook ingest authentication -----------------------------------
    # The ingest endpoint accepts frames that become identities, embeddings and
    # stored face images. It shipped with no credential check at all.
    from backend.security import webhook_auth as _webhook_auth

    raw_mode = str(getattr(cfg, "WEBHOOK_AUTH_MODE", _webhook_auth.MODE_ENFORCE)
                   or _webhook_auth.MODE_ENFORCE).strip().lower()
    mode = _webhook_auth.auth_mode(cfg)
    acknowledged = _truthy(getattr(cfg, "WEBHOOK_AUTH_INSECURE_ACK", False))
    has_keys = _webhook_auth.keys_configured(cfg)

    # Fatal in EVERY environment: a mistyped mode must never be interpreted
    # leniently, and silently correcting it to `enforce` would hide the typo
    # until someone edited the value again and got different behaviour.
    if raw_mode not in _webhook_auth.VALID_MODES:
        add(ConfigViolation(
            "WEBHOOK_AUTH_MODE_INVALID", "WEBHOOK_AUTH_MODE",
            f"WEBHOOK_AUTH_MODE={raw_mode!r} is not one of "
            f"{', '.join(_webhook_auth.VALID_MODES)}.",
            "WEBHOOK_AUTH_MODE=enforce",
        ))

    if production:
        # No mode exemption. This used to read `mode != MODE_OFF and not
        # has_keys`, so `off` skipped the keys check entirely — and `off` also
        # suppressed its own violation below when acknowledged. The two
        # exemptions combined into a production deployment that started clean
        # with no credential and no enforcement.
        if not has_keys:
            add(ConfigViolation(
                "WEBHOOK_AUTH_KEYS_MISSING", "WEBHOOK_API_KEYS",
                "The pipeline ingest webhook has no key configured, so it "
                "accepts frames from anyone who can reach the port.",
                "openssl rand -base64 48  → set WEBHOOK_API_KEYS_FILE",
            ))
        # Unconditionally fatal: `WEBHOOK_AUTH_INSECURE_ACK` cannot buy a fully
        # open ingest endpoint in production. `log_only` below remains
        # acknowledgeable because it still REQUIRES a key and still verifies it —
        # it is a migration posture for a fleet mid-rollout. `off` verifies
        # nothing, so there is no rollout it serves that `log_only` does not.
        if mode == _webhook_auth.MODE_OFF:
            add(ConfigViolation(
                "WEBHOOK_AUTH_DISABLED", "WEBHOOK_AUTH_MODE",
                "Ingest authentication is disabled. Anyone who can reach the "
                "port can enqueue frames, create identities and fill storage.",
                "WEBHOOK_AUTH_MODE=enforce. If cameras are still being "
                "credentialed, use WEBHOOK_AUTH_MODE=log_only with "
                "WEBHOOK_AUTH_INSECURE_ACK=true — it keeps verifying keys. "
                "'off' is not accepted in production under any acknowledgement.",
            ))
        if mode == _webhook_auth.MODE_LOG_ONLY and not acknowledged:
            add(ConfigViolation(
                "WEBHOOK_AUTH_LOG_ONLY", "WEBHOOK_AUTH_MODE",
                "Ingest credentials are checked but not enforced; invalid keys "
                "are logged and accepted.",
                "WEBHOOK_AUTH_MODE=enforce once every camera carries the key "
                "(or WEBHOOK_AUTH_INSECURE_ACK=true during the rollout)",
            ))
        for index, reason in _webhook_auth.weak_keys(cfg):
            add(ConfigViolation(
                "WEBHOOK_KEY_WEAK", "WEBHOOK_API_KEYS",
                f"Ingest key #{index + 1} is guessable: {reason}.",
                "openssl rand -base64 48",
            ))
        for index in _webhook_auth.reused_keys(cfg):
            add(ConfigViolation(
                "WEBHOOK_KEY_REUSED", "WEBHOOK_API_KEYS",
                f"Ingest key #{index + 1} is also the JWT signing key or the "
                "database password. It is distributed to every camera, so it is "
                "the least-protected secret in the deployment.",
                "Generate a distinct key for ingest.",
            ))
        for index in _webhook_auth.published_keys(cfg):
            add(ConfigViolation(
                "WEBHOOK_KEY_IS_PUBLISHED", "WEBHOOK_API_KEYS",
                f"Ingest key #{index + 1} is the DEVELOPMENT key committed to "
                "this repository. Anyone with the source can post frames.",
                "openssl rand -base64 48  → set WEBHOOK_API_KEYS_FILE",
            ))

        # The secret FILE itself, when that is how the key is supplied.
        #
        # A missing or unreadable mount is the failure this catches: without it
        # the file silently resolves to empty, WEBHOOK_API_KEYS ends up unset,
        # and the deployment either refuses to start for a confusing reason or —
        # if the mode was relaxed during a rollout — runs wide open.
        for code, setting, message, remedy in _secret_file_violations(cfg):
            add(ConfigViolation(code, setting, message, remedy))

    # ---- Advisory --------------------------------------------------------
    # `log_only` only: `off` is now fatal above regardless of the acknowledgement,
    # so it can never reach this branch. Narrowed from `mode != MODE_ENFORCE` so
    # a fatal misconfiguration is never also reported as a mere warning.
    if production and mode == _webhook_auth.MODE_LOG_ONLY and acknowledged:
        add(ConfigViolation(
            "WEBHOOK_AUTH_UNENFORCED_ACKNOWLEDGED", "WEBHOOK_AUTH_INSECURE_ACK",
            f"Ingest authentication is running in {mode!r} with the risk "
            "explicitly acknowledged. Keys ARE verified, but an invalid or "
            "missing credential is logged and accepted. This is a migration "
            "posture, not a destination.",
            "Finish rolling keys to the cameras, then WEBHOOK_AUTH_MODE=enforce.",
            severity="warn",
        ))
    if production:
        try:
            ttl = int(getattr(cfg, "WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS", 30) or 30)
        except (TypeError, ValueError):
            ttl = 30
        if ttl > 300:
            add(ConfigViolation(
                "WEBHOOK_CREDENTIAL_CACHE_TTL_LONG",
                "WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS",
                f"Issued ingest credentials are cached for {ttl}s, so revoking "
                "one in the admin UI leaves it working for that long on every "
                "worker.",
                "WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS=30",
                severity="warn",
            ))

    if production and _truthy(getattr(cfg, "SAVE_WEBHOOK_IMAGES", False)):
        add(ConfigViolation(
            "WEBHOOK_DEBUG_IMAGES_ENABLED", "SAVE_WEBHOOK_IMAGES",
            "Every ingested frame is written to disk for debugging, which "
            "accumulates unbounded and stores faces outside the audited tree.",
            "SAVE_WEBHOOK_IMAGES=false",
            severity="warn",
        ))

    try:
        ttl = int(getattr(cfg, "ACCESS_TOKEN_EXPIRE_MINUTES", 1440) or 1440)
    except (TypeError, ValueError):
        ttl = 1440
    if production and ttl > 1440:
        add(ConfigViolation(
            "ACCESS_TOKEN_TTL_LONG", "ACCESS_TOKEN_EXPIRE_MINUTES",
            f"Access tokens live {ttl} minutes; a stolen token stays valid "
            "that long unless explicitly revoked.",
            "Consider 1440 (24h) or less.",
            severity="warn",
        ))

    return out


# ---------------------------------------------------------------------------
# Secret files
# ---------------------------------------------------------------------------

# Docker mounts secrets 0444 by default. Anything group- or world-WRITABLE is a
# tampering vector; anything world-readable on a shared host leaks the key to
# every account on the box.
_MAX_SECRET_MODE = 0o444


def _secret_file_violations(cfg) -> List[Tuple[str, str, str, str]]:
    """Check the ingest key file exists, is readable, tight, and non-trivial.

    Returns tuples rather than ConfigViolations so the caller keeps ownership of
    severity, and so this stays importable by the deployment preflight script.
    """
    import os
    import stat

    from backend.security import webhook_auth

    out: List[Tuple[str, str, str, str]] = []
    path = str(getattr(cfg, "WEBHOOK_API_KEYS_FILE", "") or "").strip()
    if not path:
        return out          # the key is supplied inline; other rules cover it

    if not os.path.exists(path):
        out.append((
            "WEBHOOK_KEY_FILE_MISSING", "WEBHOOK_API_KEYS_FILE",
            f"The ingest key file {path} does not exist. The secret is not "
            "mounted, so no camera can authenticate.",
            f"Create ../secrets/webhook_api_keys and mount it at {path}",
        ))
        return out

    if not os.path.isfile(path):
        out.append((
            "WEBHOOK_KEY_FILE_NOT_A_FILE", "WEBHOOK_API_KEYS_FILE",
            f"{path} is not a regular file.",
            "Mount the secret as a file, not a directory.",
        ))
        return out

    try:
        with open(path, "r", encoding="utf-8") as handle:
            contents = handle.read()
    except OSError as exc:
        out.append((
            "WEBHOOK_KEY_FILE_UNREADABLE", "WEBHOOK_API_KEYS_FILE",
            f"The service account cannot read {path} ({exc.__class__.__name__}). "
            "The container runs as a non-root user; a secret owned by root with "
            "0400 is unreadable to it.",
            "chown the secret to the service UID, or mount it 0444.",
        ))
        return out

    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        mode = None
    if mode is not None and mode & ~_MAX_SECRET_MODE:
        out.append((
            "WEBHOOK_KEY_FILE_PERMISSIONS", "WEBHOOK_API_KEYS_FILE",
            f"{path} is mode {mode:04o}; anything beyond {_MAX_SECRET_MODE:04o} "
            "lets another account on the host read or rewrite the ingest key.",
            f"chmod 0400 {path} (or 0444 if the mount requires it)",
        ))

    keys = webhook_auth.parse_keys(contents)
    if not keys:
        out.append((
            "WEBHOOK_KEY_FILE_EMPTY", "WEBHOOK_API_KEYS_FILE",
            f"{path} contains no key. An empty secret file reads exactly like a "
            "missing configuration and leaves ingest unauthenticated.",
            "Write at least one key: openssl rand -base64 48 > the secret file",
        ))
        return out

    for index, key in enumerate(keys):
        if len(key) < webhook_auth.MIN_KEY_LENGTH:
            out.append((
                "WEBHOOK_KEY_FILE_WEAK", "WEBHOOK_API_KEYS_FILE",
                f"Key #{index + 1} in {path} is only {len(key)} characters "
                f"(need {webhook_auth.MIN_KEY_LENGTH}+).",
                "openssl rand -base64 48",
            ))
            continue
        reasons = assess_secret_strength(key)
        if reasons:
            out.append((
                "WEBHOOK_KEY_FILE_WEAK", "WEBHOOK_API_KEYS_FILE",
                f"Key #{index + 1} in {path} is guessable: {'; '.join(reasons)}.",
                "openssl rand -base64 48",
            ))

    return out


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def codes_of(violations: Iterable[ConfigViolation]) -> Set[str]:
    return {v.code for v in violations}


def fatal_only(violations: Iterable[ConfigViolation]) -> List[ConfigViolation]:
    return [v for v in violations if v.severity == "fatal"]


def format_report(violations: Sequence[ConfigViolation]) -> str:
    """Human-readable report. Contains no secret values, by construction."""
    fatal = [v for v in violations if v.severity == "fatal"]
    warn = [v for v in violations if v.severity != "fatal"]

    bar = "=" * 72
    lines = [bar]
    if fatal:
        lines.append(f"PRODUCTION CONFIGURATION PREFLIGHT FAILED — "
                     f"{len(fatal)} blocking problem(s)")
    else:
        lines.append("Configuration preflight: no blocking problems")
    lines.append(bar)

    for index, violation in enumerate(fatal, start=1):
        lines.append(f"{index:>2}. [{violation.code}]  {violation.setting}")
        lines.append(f"    {violation.message}")
        lines.append(f"    fix: {violation.remedy}")
        lines.append("")

    if warn:
        lines.append("-" * 72)
        lines.append(f"{len(warn)} advisory (non-blocking):")
        for violation in warn:
            lines.append(f"  - [{violation.code}] {violation.setting}: {violation.message}")
        lines.append("")

    lines.append("-" * 72)
    lines.append("No secret values appear above, by design.")
    lines.append("Set ENVIRONMENT=development for the permissive local stack.")
    lines.append(bar)
    return "\n".join(lines) + "\n"


def enforce(cfg: Any = None, *, gpu_probe: Optional[GpuProbe] = None,
            storage_probe: Optional[Callable[[str], Tuple[bool, str]]] = None) -> None:
    """Raise ConfigGuardError when the configuration is unsafe to serve."""
    if cfg is None:
        from config import settings as cfg

    import os as _os_env
    violations = collect_violations(cfg, gpu_probe=gpu_probe,
                                    env=_os_env.environ,
                                    storage_probe=storage_probe or default_storage_probe)
    if fatal_only(violations):
        raise ConfigGuardError(violations)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from config import settings

    import os as _os_env
    violations = collect_violations(settings, env=_os_env.environ,
                                    storage_probe=default_storage_probe)
    if violations:
        sys.stderr.write(format_report(violations))
    return EX_CONFIG if fatal_only(violations) else 0


if __name__ == "__main__":
    raise SystemExit(main())
