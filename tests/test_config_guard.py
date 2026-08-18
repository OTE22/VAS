"""
Fail-closed production configuration guard.

    docker exec face_recognition_api python -m pytest tests/test_config_guard.py -v

These tests are pure and in-process: collect_violations() reads its input only
through getattr(), so a SimpleNamespace stands in for Settings. Nothing here
mutates the environment, touches the database, or needs a GPU — which is what
makes it safe to assert on production rules while the container itself runs in
development mode.
"""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from backend.security.config_guard import (
    EX_CONFIG,
    ConfigGuardError,
    SECURITY_CRITICAL_KEYS,
    assess_admin_password,
    assess_secret_strength,
    codes_of,
    collect_violations,
    enforce,
    fatal_only,
    format_report,
    shannon_entropy,
)

# A configuration with nothing wrong with it.
_GOOD = dict(
    ENVIRONMENT="production",
    DEBUG=False,
    WORKERS=1,
    ALLOW_MULTI_WORKER=False,
    USE_GPU=False,
    ALLOW_CPU_FALLBACK=False,
    ENABLE_API_DOCS=False,
    JWT_SECRET_KEY="Kq7Zx2Vb9Nm4Pw6Ry8Tj1Ls3Hd5Gf0Ca2Xe4Bn6Mv8Qz1Ur3Yi7Wo9Ap",
    JWT_ALGORITHM="HS256",
    ACCESS_TOKEN_EXPIRE_MINUTES=1440,
    AUTH_COOKIE_SECURE=True,
    AUTH_COOKIE_SAMESITE="strict",
    AUTH_ALLOWED_ORIGINS="https://face-detector.internal",
    AUTH_SAME_HOST_ORIGIN_TRUSTED=False,
    CORS_ORIGINS="https://face-detector.internal",
    POSTGRES_PASSWORD="Wc4Kd9Jf2Lp7Rt5Ym8Bx3Nq6Vz1Hg0S",
    DATABASE_URL=(
        "postgresql+asyncpg://fr_app:Wc4Kd9Jf2Lp7Rt5Ym8Bx3Nq6Vz1Hg0S"
        "@postgres:5432/face_recognition"
    ),
    REDIS_URL="redis://fr_app:Td8Hn3Qw6Zx1Ke9Bv4Ms7Cy2Lp5Rj0@redis:6379/0",
    BOOTSTRAP_ADMIN_PASSWORD="",
    BOOTSTRAP_ADMIN_PASSWORD_FILE="",
    # LLM-generated SQL executes as a SELECT-only role, distinct from the
    # application's own POSTGRES_USER. A production config without this is not
    # clean: a validator bug would become a data-loss bug.
    POSTGRES_USER="fr_app",
    SQL_AGENT_DB_USER="fr_readonly",
    SQL_AGENT_DB_PASSWORD="Qm7Rt2Bx9Kd4Lp6Zn1Hs8Vw3Jc5Yf0",
    # The face gallery has exactly ONE supported location: <STORAGE_DIR>/faces.
    STORAGE_DIR="/app/storage",
    # The pipeline ingest webhook accepts frames that become identities,
    # embeddings and stored face images. A production config that leaves it
    # unauthenticated is not clean.
    WEBHOOK_API_KEYS="Zr6Xk1Nd8Bq3Ly7Vt5Wc0Mj2Hf9Ps4Ga",
    WEBHOOK_AUTH_MODE="enforce",
    WEBHOOK_AUTH_INSECURE_ACK=False,
    SAVE_WEBHOOK_IMAGES=False,
)


def cfg(**overrides):
    return SimpleNamespace(**{**_GOOD, **overrides})


def codes(**overrides):
    return codes_of(collect_violations(cfg(**overrides)))


# ---------------------------------------------------------------- baseline

def test_clean_production_config_has_no_fatal_violations():
    assert fatal_only(collect_violations(cfg())) == []


def test_development_ignores_every_production_rule():
    """The permissive local stack must stay usable."""
    reckless = cfg(
        ENVIRONMENT="development",
        JWT_SECRET_KEY="your-secret-key-change-in-production",
        POSTGRES_PASSWORD="admin",
        DATABASE_URL="postgresql+asyncpg://postgres:admin@postgres:5432/face_recognition",
        REDIS_URL="redis://redis:6379/0",
        AUTH_COOKIE_SECURE=False,
        AUTH_ALLOWED_ORIGINS="",
        CORS_ORIGINS="*",
        DEBUG=True,
        ENABLE_API_DOCS=True,
        WORKERS=16,
    )
    assert fatal_only(collect_violations(reckless)) == []


# ---------------------------------------------------------------------------
# Face storage: one canonical location, enforced in EVERY environment
# ---------------------------------------------------------------------------

def test_a_derived_path_cannot_be_pointed_elsewhere():
    """The exact misconfiguration that shipped in .env for months.

    FACES_DIR used to be a settable field validated against <STORAGE_DIR>/faces.
    It is now DERIVED, so it cannot diverge at all — and an environment that
    still tries to set it is reported rather than silently ignored, because an
    operator reading their compose file would otherwise believe the wrong thing.
    """
    for divergent in ("/app/assets/faces", "./assets/faces",
                      "/app/storage/known_faces", "/tmp/faces",
                      "/app/other/faces"):
        found = [v for v in collect_violations(cfg(), env={"FACES_DIR": divergent})
                 if v.code == "DERIVED_PATH_OVERRIDE"]
        assert found, f"{divergent} was accepted silently"
        assert found[0].severity == "fatal", divergent


def test_the_derived_path_rule_is_not_production_only():
    """A divergent gallery path is a data-integrity fault in any environment."""
    violations = collect_violations(cfg(ENVIRONMENT="development"),
                                    env={"FACES_DIR": "/app/assets/faces"})
    assert "DERIVED_PATH_OVERRIDE" in {v.code for v in fatal_only(violations)}


def test_a_leftover_override_that_matches_is_only_advisory():
    """An upgrade must not fail on a now-inert but correct value."""
    for equivalent in ("/app/storage/faces", "/app/storage/faces/",
                       "/app/storage//faces"):
        found = [v for v in collect_violations(cfg(), env={"FACES_DIR": equivalent})
                 if v.code == "DERIVED_PATH_OVERRIDE"]
        assert found and found[0].severity == "warn", equivalent


def test_missing_storage_settings_are_reported():
    # FACES_DIR can no longer be "missing": it is derived from STORAGE_DIR,
    # which is therefore the only storage root that can be absent.
    assert "STORAGE_DIR_MISSING" in codes(STORAGE_DIR="")


def test_a_relative_storage_root_is_rejected():
    """Every stored path would otherwise depend on the working directory,
    which differs between gunicorn, alembic and a maintenance script."""
    assert "STORAGE_DIR_RELATIVE" in codes(STORAGE_DIR="./storage")


def test_the_guards_derivation_table_matches_config():
    """The guard mirrors config.Settings' layout as data. If config.py changes
    where a path resolves and this table does not, the guard would report the
    wrong "should be" value — and, worse, call a correct environment divergent.
    """
    from config import settings
    from backend.security.config_guard import DERIVED_PATHS

    for name, (root_attr, parts) in DERIVED_PATHS.items():
        mirrored = os.path.join(getattr(settings, root_attr), *parts)
        assert getattr(settings, name) == mirrored, (
            f"config.Settings.{name} no longer matches config_guard's table")


def test_every_derived_path_is_guarded():
    """A new derived property that nobody guards can be silently overridden."""
    from config import Settings
    from backend.security.config_guard import DERIVED_PATHS

    declared = {n for n in dir(Settings)
                if n.endswith("_DIR") and isinstance(getattr(Settings, n, None), property)}
    assert declared <= set(DERIVED_PATHS), (
        f"derived but unguarded: {sorted(declared - set(DERIVED_PATHS))}")


def test_storage_paths_cannot_be_changed_at_runtime():
    """An admin token must not be able to repoint face storage on a live
    process — every persisted path is relative to it."""
    from backend.security.config_guard import SECURITY_CRITICAL_KEYS
    for key in ("FACES_DIR", "STORAGE_DIR", "IDENTITY_INDEX_DB_PATH"):
        assert key in SECURITY_CRITICAL_KEYS, f"{key} is runtime-mutable"


# ------------------------------------------------------------- JWT secret

@pytest.mark.parametrize("secret", [
    "your-secret-key-change-in-production",
    "changeme",
    "secret",
    "CHANGE-ME",
    "password",
])
def test_placeholder_jwt_secret_rejected(secret):
    assert "JWT_SECRET_PLACEHOLDER" in codes(JWT_SECRET_KEY=secret)


def test_missing_jwt_secret_rejected():
    assert "JWT_SECRET_MISSING" in codes(JWT_SECRET_KEY="   ")


def test_short_jwt_secret_rejected():
    assert "JWT_SECRET_TOO_SHORT" in codes(JWT_SECRET_KEY="Ab3$xK9pQ2mZ")


@pytest.mark.parametrize("secret", [
    "a" * 64,                 # one distinct character
    "1234567890" * 6,         # digits only, repeated
    "abcabcabc" * 8,          # short string repeated to fake length
])
def test_low_entropy_jwt_secret_rejected(secret):
    assert "JWT_SECRET_LOW_ENTROPY" in codes(JWT_SECRET_KEY=secret)


def test_entropy_scoring_separates_random_from_repetitive():
    assert shannon_entropy("a" * 50) == 0.0
    assert shannon_entropy(_GOOD["JWT_SECRET_KEY"]) > 4.0


def test_unsafe_jwt_algorithm_rejected_in_every_environment():
    assert "JWT_ALGORITHM_UNSAFE" in codes(ENVIRONMENT="development", JWT_ALGORITHM="none")


# ---------------------------------------------------------------- database

@pytest.mark.parametrize("password", ["admin", "postgres", "password", "changeme", "", "ADMIN"])
def test_default_database_password_rejected(password):
    assert "DB_PASSWORD_DEFAULT" in codes(POSTGRES_PASSWORD=password)


def test_default_password_embedded_in_database_url_rejected():
    url = "postgresql+asyncpg://fr_app:admin@postgres:5432/face_recognition"
    assert "DATABASE_URL_DEFAULT_PASSWORD" in codes(DATABASE_URL=url)


def test_connecting_as_superuser_rejected():
    url = "postgresql+asyncpg://postgres:Wc4Kd9Jf2Lp7Rt5Ym8Bx3Nq6Vz1Hg0S@postgres:5432/face_recognition"
    assert "DATABASE_URL_SUPERUSER" in codes(DATABASE_URL=url)


def test_unauthenticated_redis_rejected():
    assert "REDIS_NO_AUTH" in codes(REDIS_URL="redis://redis:6379/0")


def test_unauthenticated_loopback_redis_allowed():
    """A loopback Redis is not reachable from the network."""
    assert "REDIS_NO_AUTH" not in codes(REDIS_URL="redis://localhost:6379/0")


def test_production_requires_a_dedicated_role_for_generated_sql():
    """Without it, LLM-generated SQL inherits the application's write grants
    and the AST guard becomes the only thing preventing a write."""
    assert "SQL_AGENT_DB_ROLE_MISSING" in codes(SQL_AGENT_DB_USER="")


def test_production_rejects_sharing_the_application_role():
    assert "SQL_AGENT_DB_ROLE_SHARED" in codes(
        POSTGRES_USER="fr_app", SQL_AGENT_DB_USER="fr_app"
    )


def test_dedicated_read_only_role_is_accepted():
    assert "SQL_AGENT_DB_ROLE_MISSING" not in codes()
    assert "SQL_AGENT_DB_ROLE_SHARED" not in codes()


def test_base64_password_in_a_url_is_detected_as_broken():
    """`openssl rand -base64` emits '/', and a '/' in a URL password silently
    changes what the URL means: the host becomes the username and the password
    is lost, so the service connects nowhere. The guard sees this as missing
    credentials, which is the correct outcome — it must not be waved through.
    """
    broken = "redis://fr_app:aB3/xY9+zQ==@redis:6379/0"
    assert "REDIS_NO_AUTH" in codes(REDIS_URL=broken)


def test_url_safe_password_is_accepted():
    safe = "redis://fr_app:" + ("a1b2c3d4" * 6) + "@redis:6379/0"
    assert "REDIS_NO_AUTH" not in codes(REDIS_URL=safe)


def test_secret_generator_uses_url_safe_passwords_for_connection_strings():
    """Guards the fix: URL-embedded passwords must be hex, not base64."""
    with open("/app/scripts/setup/generate-secrets.sh", encoding="utf-8") as handle:
        source = handle.read()
    assert "openssl rand -hex" in source
    for variable in ("FR_APP_PASSWORD", "REDIS_PASSWORD", "POSTGRES_SUPERUSER_PASSWORD"):
        line = next(l for l in source.splitlines() if l.startswith(f"{variable}="))
        assert "gen_url" in line, f"{variable} must be URL-safe: {line}"


# ----------------------------------------------------------------- cookies

def test_insecure_cookie_rejected():
    assert "AUTH_COOKIE_INSECURE" in codes(AUTH_COOKIE_SECURE=False)


def test_invalid_samesite_rejected_in_every_environment():
    assert "AUTH_COOKIE_SAMESITE_INVALID" in codes(
        ENVIRONMENT="development", AUTH_COOKIE_SAMESITE="sometimes"
    )


def test_samesite_none_without_secure_rejected_in_every_environment():
    """Browsers drop this cookie outright — a correctness bug, not just policy."""
    assert "AUTH_COOKIE_SAMESITE_NONE_WITHOUT_SECURE" in codes(
        ENVIRONMENT="development",
        AUTH_COOKIE_SAMESITE="none",
        AUTH_COOKIE_SECURE=False,
    )


# ----------------------------------------------------------------- origins

def test_empty_allowed_origins_rejected():
    assert "AUTH_ALLOWED_ORIGINS_EMPTY" in codes(AUTH_ALLOWED_ORIGINS="")


def test_wildcard_allowed_origins_rejected():
    assert "AUTH_ALLOWED_ORIGINS_WILDCARD" in codes(AUTH_ALLOWED_ORIGINS="*")


def test_plain_http_origin_rejected():
    assert "AUTH_ALLOWED_ORIGINS_INSECURE_SCHEME" in codes(
        AUTH_ALLOWED_ORIGINS="http://face-detector.internal"
    )


def test_loopback_http_origin_allowed():
    assert "AUTH_ALLOWED_ORIGINS_INSECURE_SCHEME" not in codes(
        AUTH_ALLOWED_ORIGINS="http://localhost:8000"
    )


def test_trusting_request_host_rejected_in_production():
    assert "AUTH_SAME_HOST_ORIGIN_TRUSTED" in codes(AUTH_SAME_HOST_ORIGIN_TRUSTED=True)


@pytest.mark.parametrize("value", ["*", '["*"]', "https://a.internal, *"])
def test_credentialed_wildcard_cors_rejected(value):
    """'["*"]' is the literal value the compose files historically passed."""
    assert "CORS_WILDCARD_WITH_CREDENTIALS" in codes(CORS_ORIGINS=value)


@pytest.mark.parametrize("value", [
    "https://a.internal/path",
    "face-detector.internal",
    "ftp://a.internal",
])
def test_malformed_cors_origin_rejected(value):
    assert "CORS_ORIGIN_MALFORMED" in codes(CORS_ORIGINS=value)


# --------------------------------------------------------- runtime posture

def test_debug_mode_rejected():
    assert "DEBUG_ENABLED" in codes(DEBUG=True)


def test_api_docs_rejected():
    assert "DOCS_ENABLED" in codes(ENABLE_API_DOCS=True)


def test_multiple_workers_rejected():
    assert "WORKERS_GT_ONE" in codes(WORKERS=16)


def test_multiple_workers_allowed_with_explicit_opt_in():
    assert "WORKERS_GT_ONE" not in codes(WORKERS=16, ALLOW_MULTI_WORKER=True)


def test_worker_violation_names_the_affected_subsystems():
    violation = next(
        v for v in collect_violations(cfg(WORKERS=16)) if v.code == "WORKERS_GT_ONE"
    )
    for subsystem in ("runtime settings", "single-flight", "FAISS"):
        assert subsystem in violation.message


# --------------------------------------------------------------------- GPU

def test_gpu_without_cuda_rejected_when_fallback_disabled():
    found = codes_of(collect_violations(
        cfg(USE_GPU=True, ALLOW_CPU_FALLBACK=False),
        gpu_probe=lambda: (False, "CUDAExecutionProvider not registered"),
    ))
    assert "GPU_WITHOUT_CUDA_PROVIDER" in found


def test_gpu_without_cuda_is_advisory_when_fallback_allowed():
    violations = collect_violations(
        cfg(USE_GPU=True, ALLOW_CPU_FALLBACK=True),
        gpu_probe=lambda: (False, "no CUDA"),
    )
    assert "GPU_CPU_FALLBACK_ACTIVE" in codes_of(violations)
    assert "GPU_CPU_FALLBACK_ACTIVE" not in {v.code for v in fatal_only(violations)}


def test_working_gpu_passes():
    found = codes_of(collect_violations(
        cfg(USE_GPU=True, ALLOW_CPU_FALLBACK=False),
        gpu_probe=lambda: (True, "CUDAExecutionProvider available"),
    ))
    assert "GPU_WITHOUT_CUDA_PROVIDER" not in found


def test_failing_gpu_probe_does_not_crash_the_guard():
    def explode():
        raise RuntimeError("driver missing")

    found = codes_of(collect_violations(
        cfg(USE_GPU=True, ALLOW_CPU_FALLBACK=False), gpu_probe=explode
    ))
    assert "GPU_WITHOUT_CUDA_PROVIDER" in found


# --------------------------------------------------------- bootstrap admin

@pytest.mark.parametrize("password", ["admin123", "admin", "password", "short"])
def test_weak_bootstrap_password_rejected(password):
    assert "BOOTSTRAP_ADMIN_PASSWORD_WEAK" in codes(BOOTSTRAP_ADMIN_PASSWORD=password)


def test_strong_bootstrap_password_accepted():
    assert "BOOTSTRAP_ADMIN_PASSWORD_WEAK" not in codes(
        BOOTSTRAP_ADMIN_PASSWORD="Rk8-Vt3-Jm6-Qz1-Hd9"
    )


def test_admin_password_assessment_reports_reasons_not_values():
    reasons = assess_admin_password("admin123")
    assert reasons
    assert all("admin123" not in reason for reason in reasons)


# ------------------------------------------------------------- aggregation

def test_all_violations_reported_together_not_one_at_a_time():
    """An operator must not have to restart five times to see five problems."""
    broken = cfg(
        JWT_SECRET_KEY="your-secret-key-change-in-production",
        POSTGRES_PASSWORD="admin",
        DATABASE_URL="postgresql+asyncpg://postgres:admin@postgres:5432/face_recognition",
        REDIS_URL="redis://redis:6379/0",
        AUTH_COOKIE_SECURE=False,
        AUTH_ALLOWED_ORIGINS="",
        CORS_ORIGINS="*",
        DEBUG=True,
        ENABLE_API_DOCS=True,
        WORKERS=16,
    )
    assert len(fatal_only(collect_violations(broken))) >= 10


def test_enforce_raises_on_unsafe_config():
    with pytest.raises(ConfigGuardError):
        enforce(cfg(JWT_SECRET_KEY="your-secret-key-change-in-production"))


def test_enforce_silent_on_safe_config():
    enforce(cfg())


def test_enforce_ignores_advisory_only_violations():
    enforce(cfg(ACCESS_TOKEN_EXPIRE_MINUTES=100000))


# ------------------------------------------------- secret-leak prevention

_SENTINEL = "changeme-S3NT1NEL-do-not-print-9f8e7d"


@pytest.mark.parametrize("field", [
    "JWT_SECRET_KEY",
    "POSTGRES_PASSWORD",
    "BOOTSTRAP_ADMIN_PASSWORD",
    "DATABASE_URL",
    "REDIS_URL",
])
def test_report_never_echoes_configured_secrets(field):
    """A report generated while a secret is present must not contain it.

    AUTH_COOKIE_SECURE=False guarantees the report is non-empty regardless of
    which field carries the sentinel, so this can never pass vacuously.
    """
    value = _SENTINEL
    if field == "DATABASE_URL":
        value = f"postgresql+asyncpg://fr_app:{_SENTINEL}@postgres:5432/face_recognition"
    elif field == "REDIS_URL":
        value = f"redis://fr_app:{_SENTINEL}@redis:6379/0"

    violations = collect_violations(cfg(AUTH_COOKIE_SECURE=False, **{field: value}))
    assert violations, "report must be non-empty or this test is vacuous"
    assert _SENTINEL not in format_report(violations)


def test_database_url_violation_redacts_the_password():
    """The one violation that quotes the URL back must mask its password."""
    url = "postgresql+asyncpg://fr_app:admin@postgres:5432/face_recognition"
    violations = collect_violations(cfg(DATABASE_URL=url))
    assert "DATABASE_URL_DEFAULT_PASSWORD" in codes_of(violations)
    report = format_report(violations)
    assert "postgres:5432/face_recognition" in report, "URL should be shown"
    assert ":admin@" not in report, "password must be masked"
    assert "***" in report


def test_weak_secret_violation_reports_metrics_not_the_value():
    weak = "sentinelweakvalue" * 4
    violations = collect_violations(cfg(JWT_SECRET_KEY=weak))
    assert "JWT_SECRET_LOW_ENTROPY" in codes_of(violations)
    assert weak not in format_report(violations)


def test_default_db_password_violation_states_length_not_value():
    violations = collect_violations(cfg(POSTGRES_PASSWORD="admin"))
    message = next(
        v.message for v in violations if v.code == "DB_PASSWORD_DEFAULT"
    )
    assert "value not shown" in message


def test_report_names_the_setting_so_it_is_actionable():
    report = format_report(collect_violations(cfg(AUTH_COOKIE_SECURE=False)))
    assert "AUTH_COOKIE_SECURE" in report
    assert "AUTH_COOKIE_INSECURE" in report


# --------------------------------------------- runtime-mutation protection

def test_security_critical_keys_cover_the_settings_the_guard_checks():
    """A setting the guard validates must not be editable at runtime.

    backend/core/runtime_settings.apply_to_runtime does a literal setattr on the
    live settings object, so anything omitted here could be flipped by an admin
    token to neutralize the corresponding check.
    """
    for key in (
        "ENVIRONMENT", "DEBUG", "WORKERS", "JWT_SECRET_KEY", "JWT_ALGORITHM",
        "AUTH_COOKIE_SECURE", "AUTH_ALLOWED_ORIGINS", "CORS_ORIGINS",
        "ENABLE_API_DOCS", "DATABASE_URL", "POSTGRES_PASSWORD", "REDIS_URL",
        "BOOTSTRAP_ADMIN_PASSWORD", "ALLOW_MULTI_WORKER",
    ):
        assert key in SECURITY_CRITICAL_KEYS, f"{key} must not be runtime-mutable"


# ------------------------------------------------------ preflight CLI

def _run_guard(**overrides):
    env = {**os.environ, **overrides, "MIGRATIONS_MODE": "skip"}
    return subprocess.run(
        [sys.executable, "-m", "backend.security.config_guard"],
        capture_output=True, text=True, env=env, cwd="/app", timeout=120,
    )


def test_preflight_exits_78_on_production_placeholder_secret():
    result = _run_guard(
        ENVIRONMENT="production",
        JWT_SECRET_KEY="your-secret-key-change-in-production",
    )
    assert result.returncode == EX_CONFIG, result.stderr
    assert "JWT_SECRET_PLACEHOLDER" in result.stderr


def test_preflight_output_never_contains_the_placeholder_value():
    result = _run_guard(
        ENVIRONMENT="production",
        JWT_SECRET_KEY="your-secret-key-change-in-production",
    )
    assert "your-secret-key-change-in-production" not in (result.stdout + result.stderr)


def test_preflight_exits_zero_in_development():
    assert _run_guard(ENVIRONMENT="development").returncode == 0


# ------------------------------------------------------- startup wiring

def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_entrypoint_runs_the_preflight_before_starting_the_server():
    script = _read("/app/docker-entrypoint.sh")
    assert "backend.security.config_guard" in script
    guard_at = script.index("backend.security.config_guard")
    exec_at = script.index('exec gosu appuser "$@"')
    assert guard_at < exec_at, "preflight must run before the server is exec'd"


def test_entrypoint_preflight_failure_is_fatal():
    """A non-zero preflight must exit, not warn and continue."""
    script = _read("/app/docker-entrypoint.sh")
    block = script[script.index("Configuration preflight"):]
    assert 'exit "$status"' in block


def test_gunicorn_master_also_enforces():
    """docker-compose.gpu.yml overrides the entrypoint, so the master must
    enforce independently or the GPU deployment would skip the gate."""
    conf = _read("/app/gunicorn.conf.py")
    block = conf[conf.index("def on_starting"):]
    assert "config_guard" in block
    assert "sys.exit(78)" in block


def test_gunicorn_on_starting_exits_78_under_unsafe_production_config():
    result = subprocess.run(
        [sys.executable, "-c",
         "import runpy;ns=runpy.run_path('/app/gunicorn.conf.py');ns['on_starting'](None)"],
        capture_output=True, text=True, cwd="/app", timeout=120,
        env={**os.environ,
             "ENVIRONMENT": "production",
             "MIGRATIONS_MODE": "skip",
             "JWT_SECRET_KEY": "your-secret-key-change-in-production"},
    )
    assert result.returncode == 78, result.stderr


# --------------------------------------------- runtime mutation is refused

def test_apply_to_runtime_refuses_security_critical_settings():
    """runtime_settings is the only writer to the live settings object."""
    from backend.core.runtime_settings import apply_to_runtime

    for key in ("ENVIRONMENT", "JWT_SECRET_KEY", "AUTH_COOKIE_SECURE", "WORKERS"):
        assert apply_to_runtime(key, "attacker-value") is False


def test_apply_to_runtime_does_not_change_environment():
    from config import settings as live
    from backend.core.runtime_settings import apply_to_runtime

    before = live.ENVIRONMENT
    apply_to_runtime("ENVIRONMENT", "development")
    assert live.ENVIRONMENT == before


def test_settings_api_marks_security_critical_keys_readonly():
    source = _read("/app/backend/routes/settings.py")
    assert "SECURITY_CRITICAL_KEYS" in source
    assert "readonly_keys" in source


def test_runtime_settings_does_not_log_secret_values():
    source = _read("/app/backend/core/runtime_settings.py")
    block = source[source.index("def apply_to_runtime"):]
    assert "SECRET_SETTINGS" in block, "secret values must be masked before logging"


# ---------------------------------------------------------------------------
# The admin settings surface must not advertise a derived path
#
# The settings table is seeded from routes/settings.py's `categories` map and
# rendered as an editable form. A derived path listed there is a field the
# operator can type into, whose value is stored, displayed back as the
# configuration -- and never read by anything. Worse than useless: actively
# misleading about where face images live.
# ---------------------------------------------------------------------------

def test_no_derived_path_is_offered_as_an_editable_setting():
    from backend.routes.settings import sync_settings_from_config
    from backend.security.config_guard import DERIVED_PATHS
    import inspect

    source = inspect.getsource(sync_settings_from_config)
    categories = source.split("categories = {", 1)[1].split("\n    }", 1)[0]
    offenders = [name for name in DERIVED_PATHS if f'"{name}"' in categories]
    assert not offenders, (
        f"{offenders} are seeded into the settings table but have no setter; "
        "an admin edit would be stored, displayed, and silently ignored")


def test_derived_paths_are_not_runtime_mutable():
    """Defence in depth: even if one were seeded, it must not be applied."""
    from backend.core import runtime_settings
    from backend.security.config_guard import DERIVED_PATHS

    for name in DERIVED_PATHS:
        assert not runtime_settings.has_key(name), (
            f"{name} is in SETTINGS_REGISTRY; hydrate_from_db would setattr it "
            "onto a read-only property")


def test_a_derived_path_has_no_setter():
    """The property is the enforcement point -- if it grows a setter, every
    other protection here becomes advisory."""
    from config import settings
    from backend.security.config_guard import DERIVED_PATHS

    for name in DERIVED_PATHS:
        with pytest.raises(AttributeError):
            setattr(settings, name, "/tmp/somewhere-else")


# ---------------------------------------------------------------------------
# Storage writability
#
# A read-only gallery does not fail at startup -- it fails at the first
# enrollment, as a 500 from inside the image pipeline, hours after deploy.
# The probe is injected so these stay pure: no filesystem, no real mount.
# ---------------------------------------------------------------------------

def test_an_unwritable_storage_root_is_fatal_in_every_environment():
    for environment in ("production", "development"):
        violations = collect_violations(
            cfg(ENVIRONMENT=environment),
            storage_probe=lambda p: (False, f"{p} is mounted read-only"))
        assert "STORAGE_DIR_NOT_WRITABLE" in codes_of(fatal_only(violations)), environment


def test_a_writable_storage_root_is_silent():
    violations = collect_violations(cfg(), storage_probe=lambda p: (True, "ok"))
    assert "STORAGE_DIR_NOT_WRITABLE" not in codes_of(violations)


def test_the_probe_is_skipped_when_not_supplied():
    """Rules stay unit-testable without a filesystem; only real entry points
    pay for the syscalls."""
    assert "STORAGE_DIR_NOT_WRITABLE" not in codes()


def test_the_probe_is_not_run_on_an_already_invalid_root():
    """No point reporting 'not writable' about a path that is itself wrong --
    two violations for one fault sends the operator down the wrong trail."""
    def explode(path):
        raise AssertionError(f"probed {path!r} despite an invalid root")

    for bad in ({"STORAGE_DIR": ""}, {"STORAGE_DIR": "./storage"}):
        found = codes_of(collect_violations(cfg(**bad), storage_probe=explode))
        assert "STORAGE_DIR_NOT_WRITABLE" not in found


def test_the_real_probe_reads_the_filesystem():
    """The default probe is what production actually runs; a stub-only test
    would not catch it inverting its own return value."""
    import tempfile
    from backend.security.config_guard import default_storage_probe

    with tempfile.TemporaryDirectory() as tmp:
        ok, detail = default_storage_probe(tmp)
        assert ok, detail

        missing = os.path.join(tmp, "not-created-yet")
        ok, detail = default_storage_probe(missing)
        assert ok and "will be created" in detail, detail

        nested = os.path.join(tmp, "no", "such", "parent")
        ok, detail = default_storage_probe(nested)
        assert not ok, detail

    if os.geteuid() != 0:  # root ignores the permission bits
        with tempfile.TemporaryDirectory() as tmp:
            locked = os.path.join(tmp, "locked")
            os.makedirs(locked)
            os.chmod(locked, 0o555)
            try:
                ok, detail = default_storage_probe(locked)
                assert not ok and "not writable" in detail, detail
            finally:
                os.chmod(locked, 0o755)


# ------------------------------------------------- webhook ingest postures
#
# `off` used to be acknowledgeable. Combined with the keys-missing check being
# skipped whenever mode was `off`, a production stack could start with no
# credential AND no enforcement, reporting only a warning. These pin the
# postures so that combination cannot come back.

def test_webhook_off_is_fatal_in_production_even_when_acknowledged():
    assert "WEBHOOK_AUTH_DISABLED" in codes(
        WEBHOOK_AUTH_MODE="off", WEBHOOK_AUTH_INSECURE_ACK=True)
    assert "WEBHOOK_AUTH_DISABLED" in codes_of(fatal_only(collect_violations(
        cfg(WEBHOOK_AUTH_MODE="off", WEBHOOK_AUTH_INSECURE_ACK=True)))), (
        "the acknowledgement downgraded a fully-open ingest webhook to a warning")


def test_webhook_keys_missing_is_reported_even_when_mode_is_off():
    """The check used to carry a `mode != off` exemption.

    That is exactly backwards: `off` is the mode where a missing key matters
    least for enforcement and most for what happens if someone later flips the
    mode back to `enforce` without ever setting one.
    """
    assert "WEBHOOK_AUTH_KEYS_MISSING" in codes(
        WEBHOOK_API_KEYS="", WEBHOOK_AUTH_MODE="off",
        WEBHOOK_AUTH_INSECURE_ACK=True)


def test_webhook_log_only_stays_acknowledgeable():
    """The one surviving migration posture: keys are still required and verified.

    If this ever becomes fatal, a fleet mid-rollout has no supported path and
    operators will reach for `off` — which no longer exists in production.
    """
    violations = collect_violations(
        cfg(WEBHOOK_AUTH_MODE="log_only", WEBHOOK_AUTH_INSECURE_ACK=True))
    assert fatal_only(violations) == [], (
        "log_only + acknowledgement must not be fatal")
    assert "WEBHOOK_AUTH_UNENFORCED_ACKNOWLEDGED" in codes_of(violations)

    # Unacknowledged it is still fatal.
    assert "WEBHOOK_AUTH_LOG_ONLY" in codes(
        WEBHOOK_AUTH_MODE="log_only", WEBHOOK_AUTH_INSECURE_ACK=False)


def test_the_advisory_never_fires_for_a_mode_that_is_already_fatal():
    """`off` must be reported once, as fatal — not also as an advisory warn."""
    assert "WEBHOOK_AUTH_UNENFORCED_ACKNOWLEDGED" not in codes(
        WEBHOOK_AUTH_MODE="off", WEBHOOK_AUTH_INSECURE_ACK=True)


def test_a_token_supplied_only_as_the_alias_satisfies_the_keys_check():
    """WEBHOOK_AUTH_TOKEN folds into WEBHOOK_API_KEYS during Settings construction.

    The guard reads the folded field, so this asserts the fold happens through a
    real Settings object rather than the SimpleNamespace the other cases use.
    """
    from config import Settings

    resolved = Settings(ENVIRONMENT="production", WEBHOOK_API_KEYS="",
                        WEBHOOK_AUTH_TOKEN="Zr6Xk1Nd8Bq3Ly7Vt5Wc0Mj2Hf9Ps4Gb",
                        WEBHOOK_AUTH_MODE="enforce")
    assert "WEBHOOK_AUTH_KEYS_MISSING" not in codes_of(collect_violations(resolved))
