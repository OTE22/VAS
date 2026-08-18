"""config.py is the only configuration interface — enforced by scanning source.

    docker exec face_recognition_api python -m pytest tests/test_config_single_source.py -v

Every other test in this suite asserts on behaviour. These assert on SOURCE,
because the failures they prevent are invisible at runtime: a module that reads
`os.getenv("SIMILARITY_THRESHOLD", "0.4")` works perfectly, right up to the
deployment where the operator sets it in `.env` instead of the environment and
gets a different threshold than the admin UI reports.

The audit that produced these found configuration declared in eight places:
config.py, backend/config.py, sql_agent/config.py, utils/performance_config.py,
gunicorn.conf.py, runtime_settings.py, config_guard.py and routes/settings.py —
plus three compose files, two Dockerfiles, the entrypoint, alembic and nginx.

Scope: application code. Tests may read and write os.environ freely — that is
harness behaviour, and it is how the contract tests drive a fresh interpreter.
"""

import ast
import os

import pytest

from tests._repo_scan import find_repo_root, settings_aliases

# Discovered, never hard-coded. `REPO = "/app"` meant that outside the
# container os.walk yielded nothing, the scan read no files, and every
# `assert not offenders` in this file passed on an empty list.
REPO = find_repo_root()

# Directories holding application code. `tests` is deliberately absent.
SCANNED = ("backend", "sql_agent", "utils", "scripts")
ROOT_FILES = ("config.py", "gunicorn.conf.py", "db_connection.py", "db_models.py")

# config.py IS the interface: it is the one module allowed to read the
# environment, and pydantic-settings does that on its behalf.
#
# Every entry needs a reason that is about the CODE, not about convenience.
ENVIRONMENT_READERS_ALLOWED = {
    "config.py",
    # Resolves Docker secret FILES, not settings. Imported from inside
    # Settings.__init__, so it cannot import config without a cycle.
    os.path.join("backend", "security", "secrets.py"),
    # Toolchain/runtime probes, not application settings: CUDA_VISIBLE_DEVICES,
    # CUDA_HOME, PATH. Centralizing driver variables would be the wrong kind of
    # ownership.
    os.path.join("utils", "gpu_detection.py"),
    os.path.join("scripts", "setup", "install_dependencies.py"),
    # Reports ON the environment rather than consuming it: the settings API
    # shows `env_value` and `overridden` so an admin can see that a stored
    # value diverges from what the container was started with.
    os.path.join("backend", "core", "runtime_settings.py"),
    # Passes os.environ INTO collect_violations as an explicit argument, which
    # is what keeps the rules themselves pure and unit-testable. The derived
    # path rule has to see what the operator set, not what config resolved.
    os.path.join("backend", "security", "config_guard.py"),
    # Logging bootstrap. It must produce working logs even when config.py is
    # the thing that is broken — otherwise the configuration error that needs
    # reporting has nowhere to be reported. Settings still win when available.
    os.path.join("utils", "logging.py"),
    # Developer CLI: reads API_USERNAME / API_PASSWORD as an alternative to
    # argv. Neither is an application setting.
    os.path.join("utils", "generate_token.py"),
    # Regression isolation checker: compares what the CONTAINER was started
    # with (REGRESSION_ISOLATION_ID run marker, raw DATABASE_URL/REDIS_URL)
    # against what settings resolved — reporting on the environment is its
    # whole job; every application value it checks still comes from settings.
    os.path.join("scripts", "regression_isolation_check.py"),
    # Runs inside the GDAL / Planetiler PREPARATION containers, which mount the
    # repo but not the application: no pydantic, no config module, so importing
    # settings there is not merely undesirable, it raises. The one value it
    # reads (MAP_BUILD_DISK_RESERVE_GB) is a build-time knob for a process the
    # running application never executes; the equivalent runtime setting,
    # MAP_INSTALL_DISK_RESERVE_GB, IS declared in config.py and is what
    # install_dataset.py uses.
    os.path.join("scripts", "map_data", "build_helpers.py"),
    # Reads exactly one variable, MAP_INSTALL_FAIL_AT: a test-only
    # fault-injection seam that aborts the install transaction at a named step
    # so its crash-safety can be proven. Deliberately NOT a setting — routing
    # it through config.py would expose "corrupt an install on purpose" to the
    # admin settings API. Every real setting it uses comes from settings.
    os.path.join("scripts", "map_data", "install_dataset.py"),
}


# Modules that take a DUCK-TYPED config object rather than importing the
# settings singleton, and must therefore keep `getattr(cfg, NAME, default)`.
#
# This exemption exists because widening the alias resolution above (from a
# fixed three-name list to the real imports) correctly surfaced them for the
# first time. Each is a deliberate design choice with the rationale written at
# the top of the module, not an oversight — but the list is explicit so the
# choice stays reviewable, and everything NOT on it is now caught.
DUCK_TYPED_CONFIG_ALLOWED = {
    # collect_violations() reads its input exclusively through getattr so tests
    # can pass a plain SimpleNamespace and exercise every rule without touching
    # the environment or needing a container. Stated in its module docstring.
    os.path.join("backend", "security", "config_guard.py"),
    # ensure_bootstrap_admin(cfg=...) is called with a substitute config in
    # tests that must not mutate the process-wide settings object.
    os.path.join("backend", "services", "bootstrap_admin.py"),
    # Takes `cfg=None` and falls back to the real settings; the getattr shape
    # lets the GPU probe run against a stub with no ONNX runtime present.
    os.path.join("backend", "core", "gpu_runtime.py"),
}


def _python_files():
    for directory in SCANNED:
        base = os.path.join(REPO, directory)
        for dirpath, _dirs, names in os.walk(base):
            if "__pycache__" in dirpath:
                continue
            for name in names:
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)
    for name in ROOT_FILES:
        path = os.path.join(REPO, name)
        if os.path.exists(path):
            yield path


def _tree(path):
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _relative(path):
    return os.path.relpath(path, REPO)


# ---------------------------------------------------------------------------
# No module invents its own environment fallback
# ---------------------------------------------------------------------------

def test_no_application_module_reads_the_environment():
    """`os.getenv("X", default)` outside config.py is a second source of truth.

    It bypasses type coercion, `.env` loading, Docker-secret resolution and the
    admin settings API — so the value the module sees can differ from the value
    every other part of the system reports.
    """
    offenders = []
    for path in _python_files():
        rel = _relative(path)
        if rel.replace("\\", "/") in {p.replace("\\", "/")
                                      for p in ENVIRONMENT_READERS_ALLOWED}:
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Attribute) and node.attr in ("getenv", "environ"):
                offenders.append(f"{rel}:{node.lineno} (os.{node.attr})")

    assert not offenders, (
        "these read the environment directly instead of importing settings:\n  "
        + "\n  ".join(sorted(offenders)))


def test_nothing_writes_to_the_environment():
    """A write lands after Settings() was constructed, so it can never reach
    `settings` — it only misleads a later reader. utils/performance_config.py
    did exactly this for 15 values before it was deleted."""
    offenders = []
    for path in _python_files():
        tree = _tree(path)
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Attribute)
                        and target.value.attr == "environ"):
                    offenders.append(f"{_relative(path)}:{node.lineno}")
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("setdefault", "update", "pop")
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "environ"):
                offenders.append(f"{_relative(path)}:{node.lineno} "
                                 f"(environ.{node.func.attr})")

    assert not offenders, (
        "these mutate os.environ; the value cannot reach the already-built "
        "settings object:\n  " + "\n  ".join(sorted(offenders)))


def test_no_module_supplies_a_default_for_a_declared_setting():
    """`getattr(settings, "VECTOR_BACKEND", "faiss")` is the worst shape of all.

    It looks defensive and reads as harmless, but the fallback is a SECOND
    declaration of the default — and the two disagreed: config.py said
    `pgvector` while fourteen call sites defaulted to `faiss`, three of them
    frozen at module import. The codebase disagreed with itself about the most
    consequential setting it has.
    """
    from config import Settings

    declared = set(Settings.model_fields) | {
        name for name in dir(Settings) if isinstance(getattr(Settings, name, None), property)
    }

    offenders = []
    for path in _python_files():
        tree = _tree(path)
        # Aliases resolved from THIS file's imports rather than a fixed list.
        # The old hard-coded {"settings", "config_settings", "main_settings"}
        # could not see `from config import settings as app_settings`, which is
        # how `getattr(app_settings, "MAP_OFFLINE_TILES_ENABLED", False)`
        # shipped a fallback of False against a declared default of True — the
        # exact defect this test exists to catch.
        if _relative(path) in DUCK_TYPED_CONFIG_ALLOWED:
            continue
        aliases = settings_aliases(tree)
        if not aliases:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) == 3):
                continue
            target, name_node = node.args[0], node.args[1]
            base = getattr(target, "id", None) or getattr(target, "attr", None)
            if base not in aliases:
                continue
            if isinstance(name_node, ast.Constant) and name_node.value in declared:
                offenders.append(
                    f"{_relative(path)}:{node.lineno} -> {name_node.value}")

    assert not offenders, (
        "these re-declare a default that config.py already owns:\n  "
        + "\n  ".join(sorted(offenders)))


# ---------------------------------------------------------------------------
# No hard-coded credentials
# ---------------------------------------------------------------------------

def test_no_module_hard_codes_a_database_password():
    """Two maintenance scripts built their DSN with POSTGRES_PASSWORD="admin".

    On any deployment supplying the password through /run/secrets they
    authenticated with a different credential than the service they were
    maintaining — and reported a clean connection failure that looked like a
    network problem.
    """
    forbidden = {"admin", "postgres", "password", "changeme", "root"}
    offenders = []

    for path in _python_files():
        tree = _tree(path)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
                continue
            value = node.value.value
            if not isinstance(value, str) or value.lower() not in forbidden:
                continue
            if value in docstrings:
                continue
            for target in node.targets:
                name = getattr(target, "id", None) or getattr(target, "attr", "")
                if "PASSWORD" in str(name).upper():
                    offenders.append(f"{_relative(path)}:{node.lineno} -> {name}")

    assert not offenders, (
        "hard-coded database credentials:\n  " + "\n  ".join(sorted(offenders)))


# ---------------------------------------------------------------------------
# The retired layers stay retired
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "utils/performance_config.py",
    "scripts/legacy/app_production.py",
])
def test_retired_configuration_layers_are_not_back(path):
    assert not os.path.exists(os.path.join(REPO, path)), (
        f"{path} was deleted because it declared configuration of its own")


def test_config_py_remains_the_only_settings_class():
    """A second BaseSettings subclass is a second schema, with its own
    defaults, its own env binding and no config_guard coverage."""
    offenders = []
    for path in _python_files():
        if _relative(path) == "config.py":
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                if (getattr(base, "id", None) or getattr(base, "attr", None)) == "BaseSettings":
                    offenders.append(f"{_relative(path)}:{node.lineno} -> {node.name}")
    assert not offenders, f"second settings schema declared: {offenders}"
