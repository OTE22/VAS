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
from typing import Any, Callable, Iterable, List, Optional, Sequence, Set, Tuple

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
    "MIGRATIONS_MODE", "MIGRATIONS_FAIL_CLOSED", "MIGRATIONS_EXPECTED_HEAD",
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
            f"WORKERS={workers} but process-local state is not shared across "
            "processes: runtime settings, the SQL-agent cancellation registry, "
            "the relationship/threshold/training single-flight guards, webhook "
            "dedup, FAISS autosave and the in-process revocation fallback all "
            "live in one process. Admin settings would apply to 1 worker in "
            f"{workers}, and 'single-flight' jobs could run {workers} times.",
            "Set WORKERS=1, or move that state to Redis/Postgres and then set "
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

    # ---- Advisory --------------------------------------------------------
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


def enforce(cfg: Any = None, *, gpu_probe: Optional[GpuProbe] = None) -> None:
    """Raise ConfigGuardError when the configuration is unsafe to serve."""
    if cfg is None:
        from config import settings as cfg

    violations = collect_violations(cfg, gpu_probe=gpu_probe)
    if fatal_only(violations):
        raise ConfigGuardError(violations)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from config import settings

    violations = collect_violations(settings)
    if violations:
        sys.stderr.write(format_report(violations))
    return EX_CONFIG if fatal_only(violations) else 0


if __name__ == "__main__":
    raise SystemExit(main())
