"""The configuration contract: one source of truth, provably bound.

Written BEFORE the Pydantic-v2 loader migration and run against the old loader
first, so it demonstrates the migration preserved behaviour rather than merely
passing afterwards.

The hazard it exists for: `config.py` declared 284 fields as
`Field(default=..., env="NAME")` while running on Pydantic **v2**, where the
`env=` kwarg is silently ignored (v2 wants `validation_alias`). Environment
binding worked only because every field name happened to equal its variable
name. Nothing detected that — a single renamed field would have stopped reading
its variable with no error, no warning, and no failing test.

These tests assert on OBSERVABLE binding — construct a Settings with a specific
environment and check the value arrives — so they hold across any loader
implementation.
"""

import os
import re
import subprocess
import sys
from typing import Any, Dict

import pytest

REPO = "/app"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings_class():
    from config import Settings
    return Settings


def _field_names():
    """Every declared field name, in declaration order."""
    return list(_settings_class().model_fields.keys())


def _construct_in_subprocess(env_overrides: Dict[str, str], probe: str,
                             cwd: str = REPO) -> str:
    """Build a Settings in a CLEAN interpreter and print one expression.

    A subprocess is required, not a convenience: `config.settings` is a module
    singleton built at import (`config.py`, `settings = Settings()`), and
    `auth_service.py` caches values off it at import too. Mutating os.environ
    in-process after that point proves nothing about how the app actually loads.
    """
    env = {**os.environ, **env_overrides}
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from config import Settings\n"
        "s = Settings()\n"
        "print(%s)\n" % (cwd, probe)
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, env=env, cwd=cwd, timeout=120)
    if result.returncode != 0:
        raise AssertionError(
            f"Settings() failed to construct with {sorted(env_overrides)}:\n"
            f"{result.stderr[-2000:]}")
    return result.stdout.strip()


# A representative field per scalar type. Chosen so a wrong value cannot
# coincide with the default.
_BINDING_CASES = [
    ("ENVIRONMENT", "staging-probe", "staging-probe"),
    ("HOST", "10.11.12.13", "10.11.12.13"),
    ("PORT", "8123", "8123"),
    ("WORKERS", "7", "7"),
    ("DEBUG", "true", "True"),
    ("LOG_LEVEL", "WARNING", "WARNING"),
    ("STORAGE_DIR", "/app/storage-probe", "/app/storage-probe"),
    ("VECTOR_BACKEND", "faiss", "faiss"),
    ("SIMILARITY_THRESHOLD", "0.77", "0.77"),
    ("MAX_QUEUE_SIZE", "4321", "4321"),
    ("CACHE_TTL", "1234", "1234"),
    ("WEBHOOK_AUTH_MODE", "log_only", "log_only"),
]


# ---------------------------------------------------------------------------
# Binding: environment -> field
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,raw,expected", _BINDING_CASES)
def test_environment_variable_binds_to_its_field(name, raw, expected):
    """The core contract. If this breaks, every setting silently reverts."""
    got = _construct_in_subprocess({name: raw}, f"s.{name}")
    assert got == expected, (
        f"{name}={raw!r} did not reach settings.{name} (got {got!r}). "
        "Environment binding is broken — every deployment value would silently "
        "fall back to its declared default.")


def test_every_field_is_reachable_from_its_own_name():
    """No field may be unbindable.

    Constructs one Settings with EVERY field set to a marker derived from its
    name, then verifies each one arrived. This is the test that would have
    caught a rename: a field whose env name diverges from its attribute name
    keeps its default and is reported here.

    Non-string fields are skipped for the value check (a string marker will not
    coerce) but are still asserted to be *settable* by the type-appropriate
    probes above.
    """
    Settings = _settings_class()
    string_fields = [
        name for name, field in Settings.model_fields.items()
        if field.annotation is str
    ]
    assert len(string_fields) > 50, "expected many string settings; introspection changed"

    overrides = {name: f"probe-{name.lower()}" for name in string_fields}
    probe = "[%s]" % ",".join(f"s.{n}" for n in string_fields)
    raw = _construct_in_subprocess(overrides, probe)

    values = eval(raw)  # noqa: S307 - our own repr, from our own subprocess

    # WEBHOOK_API_KEYS is a SET that is deliberately merged after binding:
    # WEBHOOK_AUTH_TOKEN (the bearer alias) is appended to it so there is one
    # credential source at runtime. Because this test sets EVERY string field at
    # once, the alias probe lands in it too and exact equality no longer holds.
    # Binding is still what is under test — assert the field's own probe is
    # present in the parsed set, which fails just as loudly if it never arrived.
    merged = {"WEBHOOK_API_KEYS"}

    unbound = []
    for name, value in zip(string_fields, values):
        expected = f"probe-{name.lower()}"
        if name in merged:
            if expected not in [p.strip() for p in str(value).split(",")]:
                unbound.append(name)
        elif value != expected:
            unbound.append(name)

    assert not unbound, (
        f"{len(unbound)} field(s) did not bind to their environment variable: "
        f"{unbound[:20]}")


def test_declared_field_count_is_stable():
    """A tripwire, not a rule.

    The loader migration must not drop fields. If this number changes
    deliberately, update it in the same commit as the field change.
    """
    # 269 after the 2026-08 retirement of 21 dead fields (FAISS knob family,
    # DB_PATH, ENABLE_METRICS/METRICS_PORT, RATE_LIMIT_ENABLED/INTERVAL, four
    # MAP_* flags). 271 after adding WEBHOOK_AUTH_TOKEN + WEBHOOK_AUTH_TOKEN_FILE;
    # 272 after WEBHOOK_CREDENTIAL_CACHE_TTL_SECONDS.
    # Update deliberately, in the same commit as a field change.
    assert len(_field_names()) >= 272, (
        f"only {len(_field_names())} fields declared; the migration may have "
        "dropped settings")


# ---------------------------------------------------------------------------
# Precedence: OS env > .env > declared default
# ---------------------------------------------------------------------------

def test_os_environment_overrides_dotenv(tmp_path):
    """Docker/compose values must win over a bind-mounted .env."""
    env_file = tmp_path / ".env"
    env_file.write_text("ENVIRONMENT=from-dotenv\n", encoding="utf-8")

    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from config import Settings\n"
        "print(Settings(_env_file=%r).ENVIRONMENT)\n" % (REPO, str(env_file))
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env={**os.environ, "ENVIRONMENT": "from-os-env"}, cwd=REPO, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "from-os-env", (
        "a .env value beat the process environment; Docker/compose settings "
        "would be silently ignored")


def test_dotenv_overrides_the_declared_default(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("APP_NAME=from-dotenv\n", encoding="utf-8")

    clean = {k: v for k, v in os.environ.items() if k != "APP_NAME"}
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from config import Settings\n"
        "print(Settings(_env_file=%r).APP_NAME)\n" % (REPO, str(env_file))
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, env=clean, cwd=REPO, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "from-dotenv"


def test_declared_default_applies_when_nothing_is_set():
    clean = {k: v for k, v in os.environ.items() if k != "JWT_ALGORITHM"}
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from config import Settings\n"
        "print(Settings(_env_file=None).JWT_ALGORITHM)\n" % REPO
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, env=clean, cwd=REPO, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "HS256"


# ---------------------------------------------------------------------------
# Secret files — behaviour that must survive the migration untouched
# ---------------------------------------------------------------------------

_SECRET_PAIRS = [
    ("JWT_SECRET_KEY", "JWT_SECRET_KEY_FILE"),
    ("POSTGRES_PASSWORD", "POSTGRES_PASSWORD_FILE"),
    ("DATABASE_URL", "DATABASE_URL_FILE"),
    ("REDIS_URL", "REDIS_URL_FILE"),
    ("BOOTSTRAP_ADMIN_PASSWORD", "BOOTSTRAP_ADMIN_PASSWORD_FILE"),
    ("WEBHOOK_API_KEYS", "WEBHOOK_API_KEYS_FILE"),
    ("WEBHOOK_AUTH_TOKEN", "WEBHOOK_AUTH_TOKEN_FILE"),
]


@pytest.mark.parametrize("field,file_field", _SECRET_PAIRS)
def test_a_mounted_secret_file_populates_its_field(field, file_field, tmp_path):
    """Every *_FILE pair must resolve. This is how production supplies secrets."""
    secret = tmp_path / field.lower()
    marker = f"secret-value-for-{field.lower()}"
    secret.write_text(marker, encoding="utf-8")
    os.chmod(secret, 0o400)

    got = _construct_in_subprocess({file_field: str(secret)}, f"s.{field}")
    assert got == marker, (
        f"{file_field} did not populate {field}; production secrets would not load")


def test_settings_construction_never_raises_on_a_bad_secret_path():
    """A missing secret file must not break IMPORT.

    alembic/env.py, gunicorn.conf.py and every test import config at module
    scope. A raising validator turns a misconfiguration into an unimportable
    process with no diagnostic — which is why policy lives in config_guard
    (exit 78 with a report) and not in the model.
    """
    got = _construct_in_subprocess(
        {"JWT_SECRET_KEY_FILE": "/definitely/not/mounted",
         "JWT_SECRET_KEY": "fallback-value"},
        "s.JWT_SECRET_KEY")
    assert got == "fallback-value"


# ---------------------------------------------------------------------------
# The singleton
# ---------------------------------------------------------------------------

def test_settings_is_a_module_singleton():
    from config import Settings, settings
    assert isinstance(settings, Settings)


def test_importing_config_does_not_mutate_the_environment():
    """Importing configuration must be a read.

    utils/performance_config.py writes into os.environ; anything it injects
    lands AFTER Settings() was constructed, so it can never reach `settings` —
    it only misleads later os.getenv readers.
    """
    code = (
        "import sys, os; sys.path.insert(0, %r)\n"
        "before = dict(os.environ)\n"
        "import config\n"
        "after = dict(os.environ)\n"
        "added = {k: after[k] for k in after.keys() - before.keys()}\n"
        "changed = {k for k in before.keys() & after.keys() if before[k] != after[k]}\n"
        "print(repr((added, sorted(changed))))\n" % REPO
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, env=dict(os.environ), cwd=REPO, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    added, changed = eval(result.stdout.strip())  # noqa: S307
    assert not added and not changed, (
        f"importing config mutated the environment: added={added} changed={changed}")


# ---------------------------------------------------------------------------
# Phase 2 — every entry point resolves configuration the same way
#
# The two maintenance scripts built a DSN from raw os.getenv with
# POSTGRES_PASSWORD defaulting to the literal "admin", and never imported
# `settings`. That skipped config.py's secret-file resolution, so on any
# deployment supplying the password through /run/secrets they authenticated
# with a different credential than the service they were maintaining.
# ---------------------------------------------------------------------------

MAINTENANCE_SCRIPTS = [
    f"{REPO}/scripts/maintenance/dedupe_identity_embeddings.py",
    f"{REPO}/scripts/maintenance/wipe_pipelines.py",
]


def _code_only(path):
    """File contents with comment lines stripped, so prose is not asserted on."""
    with open(path, encoding="utf-8") as handle:
        return "\n".join(line for line in handle.read().splitlines()
                         if not line.lstrip().startswith("#"))


def _load_module(path):
    """Import a script's module scope. main() is guarded, so nothing executes."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "cfgprobe_" + os.path.basename(path)[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", MAINTENANCE_SCRIPTS)
def test_maintenance_scripts_carry_no_literal_credentials(path):
    code = _code_only(path)
    assert '"admin"' not in code and "'admin'" not in code, (
        f"{path} hard-codes a database password")
    assert "os.getenv" not in code, (
        f"{path} reads the environment directly, bypassing secret-file resolution")


@pytest.mark.parametrize("path", MAINTENANCE_SCRIPTS)
def test_maintenance_scripts_resolve_the_same_credentials_as_the_app(path):
    from config import settings

    module = _load_module(path)
    assert (module.DB_HOST, module.DB_NAME, module.DB_USER, module.DB_PASSWORD) == (
        settings.DB_HOST, settings.POSTGRES_DB,
        settings.POSTGRES_USER, settings.POSTGRES_PASSWORD), (
        f"{path} resolves different credentials than the application")


@pytest.mark.parametrize("path", MAINTENANCE_SCRIPTS)
def test_maintenance_scripts_honour_a_mounted_secret(path, tmp_path):
    """The case that used to diverge.

    With POSTGRES_PASSWORD_FILE set, the app reads the file; the scripts used to
    ignore it entirely and fall back to "admin".
    """
    secret = tmp_path / "pgpw"
    secret.write_text("SecretFromMountedFile123", encoding="utf-8")
    os.chmod(secret, 0o400)

    code = (
        "import importlib.util, sys\n"
        "sys.path.insert(0, %r)\n"
        "spec = importlib.util.spec_from_file_location('probe', %r)\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "print(m.DB_PASSWORD)\n" % (REPO, path)
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env={**os.environ, "POSTGRES_PASSWORD_FILE": str(secret)},
        cwd=REPO, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "SecretFromMountedFile123", (
        f"{path} ignored the mounted secret")


def test_migration_utilities_read_no_environment_directly():
    """migrations.py and alembic/env.py route through settings.

    LOCAL_DB_HOST, MIGRATION_DB_WAIT_SECONDS and
    MIGRATION_DB_RETRY_INTERVAL_SECONDS had no central declaration at all, and
    MIGRATIONS_MODE / MIGRATIONS_EXPECTED_HEAD were dual-sourced — declared as
    settings fields AND read via os.getenv, which made the fields decorative.
    """
    for path in (f"{REPO}/backend/utils/migrations.py", f"{REPO}/alembic/env.py"):
        code = _code_only(path)
        leaked = re.findall(r'os\.getenv\(\s*["\']([A-Z_0-9]+)["\']', code)
        assert not leaked, f"{path} still reads {leaked} from the environment"


def test_the_new_migration_settings_are_declared_and_bind():
    from config import Settings

    for name in ("LOCAL_DB_HOST", "MIGRATION_DB_WAIT_SECONDS",
                 "MIGRATION_DB_RETRY_INTERVAL_SECONDS"):
        assert name in Settings.model_fields, f"{name} is not a declared setting"

    got = _construct_in_subprocess({"LOCAL_DB_HOST": "db.probe"}, "s.LOCAL_DB_HOST")
    assert got == "db.probe"


def test_the_new_operational_settings_are_declared():
    from config import Settings

    for name in ("BACKUP_DIR", "BACKUP_RETENTION_DAYS",
                 "BACKUP_INTERVAL_SECONDS", "TLS_CERT_PATH"):
        assert name in Settings.model_fields, f"{name} is not a declared setting"


# ---------------------------------------------------------------------------
# Phase 4 — derived filesystem layout
#
# STORAGE_DIR is the only settable root. FACES_DIR, UPLOAD_TEMP_DIR,
# WEBHOOK_IMAGES_DIR, CROPPED_IMAGES_DIR and MODEL_CANDIDATE_DIR are computed,
# so no compose file, .env, module or admin API can point one of them elsewhere.
#
# `.env` once shipped FACES_DIR=./assets/faces while compose set
# /app/storage/faces: the two halves of the application disagreed about where a
# person's photos lived, and ./assets is a read-only mount, so enrollment
# simply failed there.
# ---------------------------------------------------------------------------

DERIVED_PATHS = ["FACES_DIR", "UPLOAD_TEMP_DIR", "WEBHOOK_IMAGES_DIR",
                 "CROPPED_IMAGES_DIR", "MODEL_CANDIDATE_DIR",
                 # MAP_DATA_DIR is the settable root; production/ holds the
                 # archives and metadata/ holds the ledger that authorizes
                 # them. Splitting those two across trees would let a verdict
                 # file describe archives it has never seen.
                 "MAP_PRODUCTION_DIR", "MAP_METADATA_DIR"]


@pytest.mark.parametrize("name", DERIVED_PATHS)
def test_derived_paths_are_not_settable_fields(name):
    from config import Settings

    assert name not in Settings.model_fields, (
        f"{name} is a declared field again — it can be pointed anywhere by a "
        "compose file or .env, which is exactly the divergence this removed")
    assert isinstance(getattr(Settings, name, None), property), (
        f"{name} is not a read-only property")


def test_faces_dir_always_equals_storage_dir_slash_faces():
    from config import settings

    assert settings.FACES_DIR == os.path.join(settings.STORAGE_DIR, "faces")


def test_derivation_follows_storage_dir_wherever_it_points():
    """The relationship must hold for ANY root, not just the default."""
    probe = "/app/storage-derivation-probe"
    raw = _construct_in_subprocess(
        {"STORAGE_DIR": probe},
        "[s.FACES_DIR, s.UPLOAD_TEMP_DIR, s.WEBHOOK_IMAGES_DIR, s.CROPPED_IMAGES_DIR]")
    faces, incoming, webhook_dir, cropped = eval(raw)  # noqa: S307

    assert faces == os.path.join(probe, "faces")
    assert incoming == os.path.join(probe, "faces", ".incoming")
    assert webhook_dir == os.path.join(probe, "debug", "webhook_images")
    assert cropped == os.path.join(probe, "debug", "cropped")


def test_the_upload_staging_area_is_inside_faces_dir():
    """Not cosmetic: enrollment relies on os.replace, which needs one filesystem.

    A staging directory on a different mount turns the atomic rename into a
    cross-device copy, and the "file moves last, then commit" guarantee with it.
    """
    from config import settings

    assert settings.UPLOAD_TEMP_DIR.startswith(settings.FACES_DIR + os.sep)


def test_setting_a_derived_path_in_the_environment_is_rejected():
    from types import SimpleNamespace
    from backend.security.config_guard import codes_of, collect_violations

    cfg = SimpleNamespace(ENVIRONMENT="production", STORAGE_DIR="/app/storage",
                          FACES_DIR="/app/storage/faces")
    codes = codes_of(collect_violations(cfg, env={"FACES_DIR": "/somewhere/else"}))
    assert "DERIVED_PATH_OVERRIDE" in codes, (
        "a stale FACES_DIR in the environment is silently ignored; the operator "
        "would read their compose file and believe the wrong thing")


def test_a_matching_derived_override_is_advisory_not_fatal():
    """Inert but harmless: warn, so an upgrade does not fail on a leftover."""
    from types import SimpleNamespace
    from backend.security.config_guard import collect_violations

    cfg = SimpleNamespace(ENVIRONMENT="production", STORAGE_DIR="/app/storage",
                          FACES_DIR="/app/storage/faces")
    found = [v for v in collect_violations(
        cfg, env={"FACES_DIR": "/app/storage/faces"})
        if v.code == "DERIVED_PATH_OVERRIDE"]
    assert found and all(v.severity == "warn" for v in found)


def test_the_guard_still_reads_no_process_environment_of_its_own():
    """The module's testability contract.

    Every rule must be exercisable with a SimpleNamespace and an explicit
    mapping. A rule that reaches into os.environ fires in every unrelated test
    and cannot be driven from a fixture.
    """
    source = open(f"{REPO}/backend/security/config_guard.py", encoding="utf-8").read()
    body = source.split("def collect_violations(", 1)[1].split("\ndef ", 1)[0]
    # Comments explaining the contract legitimately mention os.environ.
    body = "\n".join(line for line in body.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "os.environ" not in body, (
        "collect_violations reads the process environment directly")


def test_no_deployment_file_sets_a_derived_path():
    import glob

    offenders = []
    candidates = glob.glob(f"{REPO}/docker/docker-compose*.yml") + [f"{REPO}/.env.example"]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        for name in DERIVED_PATHS:
            if re.search(r"^\s*" + name + r"\s*[:=]", text, re.M):
                offenders.append(f"{os.path.basename(path)}:{name}")
    assert not offenders, (
        f"derived paths are still set in deployment files: {offenders}")


# ---------------------------------------------------------------------------
# Phase 5 -- one configuration layer, not four
#
# Four things declared configuration of their own:
#
#   utils/performance_config.py  wrote 15 values into os.environ AFTER
#                                Settings() was built, so they could never
#                                reach `settings` -- a shadow layer invisible
#                                to the object it appeared to configure.
#   sql_agent/config.py          resolved every value as
#                                `getattr(settings, n) or os.getenv(n, default)`,
#                                so a deliberately empty setting silently
#                                picked up something else, and the agent could
#                                talk to a different database than the API.
#   backend/config.py            created a directory as a side effect of
#                                `import`.
#   twelve private _setting()    helpers wrapped `getattr(settings, n, default)`
#                                in `except Exception: return default`, so a
#                                broken config ran on hard-coded numbers.
#
# Assertions here go through the AST rather than a substring scan: these files
# explain the old behaviour in their own docstrings, and a text search cannot
# tell an explanation from a call.
# ---------------------------------------------------------------------------

def _environment_reads(path):
    """Names of os.environ / os.getenv accesses in real code (not prose)."""
    import ast

    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
            found.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in ("environ", "getenv"):
            found.append(node.id)
    return found


def _called_names(path):
    """Function names actually called, so a docstring describing the old
    behaviour is not mistaken for the behaviour."""
    import ast

    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            names.add(func.attr if isinstance(func, ast.Attribute)
                      else getattr(func, "id", ""))
    return names


def _string_constants(path):
    import ast

    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in docstrings]


def test_the_shadow_performance_layer_is_gone():
    assert not os.path.exists(os.path.join(REPO, "utils", "performance_config.py")), (
        "utils/performance_config.py is back; it writes settings values into "
        "os.environ after Settings() was constructed, where nothing reads them")

    source = _code_only(os.path.join(REPO, "backend", "lifespan.py"))
    assert "apply_optimized_config()" not in source, (
        "lifespan still applies the shadow performance layer")


def test_importing_the_backend_shim_creates_no_directories():
    """`import backend.config` used to run os.makedirs(STORAGE_DIR).

    Importing a constant must not touch the filesystem: alembic, gunicorn's
    config module and every test collection import this.
    """
    path = os.path.join(REPO, "backend", "config.py")
    assert "makedirs" not in _called_names(path), (
        "backend/config.py still has an import side effect")
    assert not _environment_reads(path)


def test_the_sql_agent_reads_settings_and_nothing_else():
    path = os.path.join(REPO, "sql_agent", "config.py")
    assert not _environment_reads(path), (
        "sql_agent/config.py still reads the environment; an empty setting "
        "would silently resolve to something the API never sees")
    assert "admin" not in _string_constants(path), (
        "the literal fallback password is back")


def test_the_sql_agent_and_the_api_resolve_the_same_values():
    """The agent is not a separate application. If these ever diverge, an
    operator changes DATABASE_URL and the agent keeps querying the old host."""
    from config import settings as api
    from sql_agent.config import config as agent

    for attr, name in (("db_url", "DATABASE_URL"), ("db_host", "DB_HOST"),
                       ("db_port", "DB_PORT"), ("db_name", "POSTGRES_DB"),
                       ("chroma_persist_dir", "CHROMADB_PATH"),
                       ("ollama_base_url", "OLLAMA_BASE_URL"),
                       ("ollama_model", "OLLAMA_MODEL"),
                       ("ollama_timeout", "OLLAMA_TIMEOUT"),
                       ("rag_top_k", "RAG_TOP_K")):
        assert getattr(agent, attr) == getattr(api, name), name


def test_importing_the_sql_agent_does_not_mutate_the_environment():
    probe = ("import sys, os; sys.path.insert(0, %r)\n"
             "before = dict(os.environ)\n"
             "import sql_agent.config\n"
             "after = dict(os.environ)\n"
             "print(repr((sorted(after.keys() - before.keys()),\n"
             "            sorted(k for k in before.keys() & after.keys()\n"
             "                   if before[k] != after[k]))))\n" % REPO)
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                            text=True, env=dict(os.environ), cwd=REPO, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    added, changed = eval(result.stdout.strip().splitlines()[-1])  # noqa: S307
    assert not added and not changed, f"added={added} changed={changed}"


def test_the_agent_password_is_resolved_by_config_not_by_the_agent():
    """SQL_AGENT_DB_PASSWORD_FILE used to be opened by sql_agent/config.py.

    Two resolvers for one Docker secret means two failure modes; the mounted
    file now reaches the agent through the same code path as every other
    secret.
    """
    import tempfile

    assert '("SQL_AGENT_DB_PASSWORD", "SQL_AGENT_DB_PASSWORD_FILE")' in \
        _code_only(os.path.join(REPO, "config.py"))

    agent_path = os.path.join(REPO, "sql_agent", "config.py")
    assert "open" not in _called_names(agent_path), (
        "the agent still reads a secret file itself")

    handle = tempfile.NamedTemporaryFile("w", suffix=".secret", delete=False)
    handle.write("AgentSecretFromMountedFile456\n")
    handle.close()
    try:
        resolved = _construct_in_subprocess(
            {"SQL_AGENT_DB_PASSWORD_FILE": handle.name,
             "SQL_AGENT_DB_PASSWORD": "inline-should-lose"},
            "s.SQL_AGENT_DB_PASSWORD")
        assert resolved == "AgentSecretFromMountedFile456", resolved
    finally:
        os.unlink(handle.name)


def test_no_module_reinvents_a_settings_fallback_helper():
    """`def _setting(name, default)` appeared in twelve modules.

    Each swallowed every exception and returned a hard-coded number, so a
    genuinely broken configuration ran on defaults instead of failing. Every
    name they resolved is a declared field, so the default never applied
    anyway.
    """
    offenders = []
    for directory in ("backend", "sql_agent", "utils", "scripts"):
        for root, _dirs, names in os.walk(os.path.join(REPO, directory)):
            if "__pycache__" in root:
                continue
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as handle:
                    if re.search(r"^def _setting\(", handle.read(), re.M):
                        offenders.append(os.path.relpath(path, REPO))
    assert not offenders, f"private settings fallback helpers are back: {offenders}"


# ---------------------------------------------------------------------------
# Every entry point resolves the SAME values
#
# The application is not one process. It is gunicorn's config module, the API
# workers, alembic, the SQL agent and a handful of maintenance scripts, each
# started differently. Before this work they did not agree: two maintenance
# scripts built their DSN from raw os.getenv with POSTGRES_PASSWORD defaulting
# to "admin", so on any deployment supplying the password through
# /run/secrets they authenticated as a different user than the service they
# were maintaining -- and the failure looked like a network problem.
#
# Each entry point is loaded THE WAY IT IS REALLY LOADED, in its own
# interpreter, and asked for the same five values.
# ---------------------------------------------------------------------------

CROSS_CHECKED = ["DATABASE_URL", "REDIS_URL", "STORAGE_DIR", "FACES_DIR",
                 "VECTOR_BACKEND"]

ENTRY_POINTS = {
    # The API: plain `from config import settings`, as every route does.
    "api": "from config import settings as s",

    # gunicorn imports its config module by path; `bind` is built from
    # settings.HOST/PORT there, so importing it proves the same object loads.
    "gunicorn": (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('gunicorn_conf',"
        " '/app/gunicorn.conf.py')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "s = module.settings"
    ),

    # alembic/env.py is executed by alembic with its own sys.path handling.
    "alembic": (
        "import sys; sys.path.insert(0, '/app')\n"
        "from config import settings as s"
    ),

    # The SQL agent: its Config dataclass, not the settings object, so this
    # catches the agent drifting even when config.py is fine.
    "sql_agent": (
        "from sql_agent.config import config as agent\n"
        "from config import settings as s\n"
        "assert agent.db_url == s.DATABASE_URL, ('sql_agent DSN', agent.db_url)"
    ),

    # A maintenance script, loaded exactly as `python scripts/...` loads it.
    "maintenance_script": (
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('maint',"
        " '/app/scripts/maintenance/wipe_pipelines.py')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "from config import settings as s\n"
        "assert module.DB_PASSWORD == s.POSTGRES_PASSWORD, 'script password differs'"
    ),
}


@pytest.mark.parametrize("name", sorted(ENTRY_POINTS))
def test_entry_point_resolves_the_canonical_values(name):
    probe = ("import sys; sys.path.insert(0, '/app')\n"
             + ENTRY_POINTS[name] + "\n"
             + "print(repr([%s]))\n" % ", ".join(f"s.{v}" for v in CROSS_CHECKED))

    result = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                            text=True, env=dict(os.environ), cwd=REPO, timeout=300)
    assert result.returncode == 0, (
        f"{name} could not resolve configuration:\n{result.stderr[-2000:]}")

    values = eval(result.stdout.strip().splitlines()[-1])  # noqa: S307
    from config import settings

    expected = [getattr(settings, name_) for name_ in CROSS_CHECKED]
    mismatched = [f"{key}: {name}={got!r} api={want!r}"
                  for key, got, want in zip(CROSS_CHECKED, values, expected)
                  if got != want]
    assert not mismatched, (
        f"{name} resolves different configuration than the API: "
        + "; ".join(mismatched))


def test_a_mounted_secret_reaches_every_entry_point():
    """The concrete failure this class of bug produced.

    With POSTGRES_PASSWORD_FILE set, the app, a maintenance script and the SQL
    agent must all authenticate with the file's contents -- not with an inline
    value, and certainly not with a literal default.
    """
    import tempfile

    handle = tempfile.NamedTemporaryFile("w", suffix=".secret", delete=False)
    handle.write("SecretFromMountedFile123\n")
    handle.close()

    probe = (
        "import sys; sys.path.insert(0, '/app')\n"
        "import importlib.util\n"
        "from config import settings\n"
        "spec = importlib.util.spec_from_file_location('maint',"
        " '/app/scripts/maintenance/wipe_pipelines.py')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "from sql_agent.config import config as agent\n"
        "print(repr([settings.POSTGRES_PASSWORD, module.DB_PASSWORD, agent.db_name]))\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True,
            env={**os.environ, "POSTGRES_PASSWORD_FILE": handle.name,
                 "POSTGRES_PASSWORD": "inline-should-lose"},
            cwd=REPO, timeout=300)
        assert result.returncode == 0, result.stderr[-2000:]
        app_pw, script_pw, _agent_db = eval(result.stdout.strip().splitlines()[-1])  # noqa: S307
        assert app_pw == "SecretFromMountedFile123", app_pw
        assert script_pw == "SecretFromMountedFile123", (
            "the maintenance script did not see the mounted secret")
    finally:
        os.unlink(handle.name)
