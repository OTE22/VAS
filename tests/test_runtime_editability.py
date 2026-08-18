"""Runtime editability must be honest.

    docker exec face_recognition_api python -m pytest tests/test_runtime_editability.py -v

`runtime_settings.apply_to_runtime` does `setattr(settings, key, value)`. That
only changes behaviour if the consumer reads `settings.KEY` when it runs. A
module that did

    DATA_RETENTION_DAYS = settings.DATA_RETENTION_DAYS      # at import

froze the value at import — while the admin UI reported the change as applied
and the settings API kept serving the stored number. /api/stats was reporting
the frozen value from two fields and the live value from a third, so the same
response could disagree with itself about how long data is kept.

These tests read the SOURCE, not the behaviour, because the failure is
invisible at runtime: nothing errors, the wrong number is simply used.
"""

import ast
import os

import pytest

from tests._repo_scan import (find_repo_root, frozen_init_functions,
                              iter_source_files, settings_aliases)

# Discovered, never hard-coded. `REPO = "/app"` made every assertion in this
# file pass vacuously outside the container: os.walk on a missing directory
# yields nothing, so the scan found no offenders because it read no files.
REPO = find_repo_root()
SCAN_ROOTS = ("backend", "sql_agent", "utils", "database")
SCAN_FILES = ("config.py", "db_connection.py")


def _read_sites(name_filter):
    """(live, frozen) read sites for settings attributes.

    live   — the read re-runs on every call, so an admin edit reaches it
    frozen — the read happens once at import, so it cannot

    A read is frozen when it sits at module scope, OR inside the __init__ of a
    class that is instantiated at module scope in the same file. The second
    case matters: `identity_clustering`, `face_quality` and
    `pipeline_aware_clustering` are all module-level singletons, so their
    constructors run exactly once and captured values that the settings page
    then reported as live.
    """
    live, frozen = {}, {}

    def scan(path):
        try:
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
        except (OSError, SyntaxError):
            return

        aliases = settings_aliases(tree)
        if not aliases:
            return
        once_only = frozen_init_functions(tree)

        in_live_function = set()

        class Walker(ast.NodeVisitor):
            depth = 0

            def _nested(self, node):
                # A constructor that runs once at import does not make the
                # reads inside it live, so do not increment the depth for it.
                counts_as_live = id(node) not in once_only
                if counts_as_live:
                    Walker.depth += 1
                self.generic_visit(node)
                if counts_as_live:
                    Walker.depth -= 1

            visit_FunctionDef = _nested
            visit_AsyncFunctionDef = _nested
            visit_Lambda = _nested

            def visit_Attribute(self, node):
                if Walker.depth > 0:
                    in_live_function.add(id(node))
                self.generic_visit(node)

        Walker().visit(tree)

        rel = os.path.relpath(path, REPO)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            base = getattr(node.value, "id", None) or getattr(node.value, "attr", None)
            if base not in aliases or not name_filter(node.attr):
                continue
            bucket = live if id(node) in in_live_function else frozen
            bucket.setdefault(node.attr, []).append(f"{rel}:{node.lineno}")

    for path in iter_source_files(REPO, SCAN_ROOTS, SCAN_FILES):
        scan(path)

    return live, frozen


def test_the_scan_actually_reads_the_repository():
    """Guard the guard.

    Every other test here asserts `not offenders`. If the scan reads nothing,
    they all pass while checking nothing — which is how a hard-coded "/app"
    root went unnoticed. Prove the scan sees a read site it must see.
    """
    live, _frozen = _read_sites(lambda name: name == "SIMILARITY_THRESHOLD")
    assert live.get("SIMILARITY_THRESHOLD"), (
        "the source scan found no live read of SIMILARITY_THRESHOLD; it is not "
        "reading the repository, so every assertion in this file is vacuous")


def _dynamic_keys():
    from backend.core.runtime_settings import DYNAMIC_APPLY_MODES, SETTINGS_REGISTRY

    return {name for name, meta in SETTINGS_REGISTRY.items()
            if meta.apply_mode in DYNAMIC_APPLY_MODES}


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------

def test_no_runtime_editable_setting_is_frozen_at_import():
    """The whole point of the registry.

    A setting registered `immediate`, `next_request` or `next_job_run` promises
    the running process picks the new value up. A module-scope read breaks that
    promise silently — the admin sees "applied", and nothing changes.
    """
    dynamic = _dynamic_keys()
    _live, frozen = _read_sites(dynamic.__contains__)

    assert not frozen, (
        "these settings are advertised as runtime-editable but are read at "
        "import, so an admin change cannot reach them: "
        + "; ".join(f"{k} ({', '.join(v)})" for k, v in sorted(frozen.items())))


def test_every_runtime_editable_setting_has_a_consumer():
    """A registry entry nobody reads is a knob wired to nothing.

    Names resolved dynamically (`getattr(settings, flag_key)`) are exempt and
    listed explicitly, so the exemption itself stays reviewable.
    """
    dynamically_resolved = {"MLFLOW_ENABLED", "OPTUNA_ENABLED",
                            "XGBOOST_ENABLED", "SHAP_ENABLED"}

    dynamic = _dynamic_keys()
    live, _frozen = _read_sites(dynamic.__contains__)

    unread = dynamic - set(live) - dynamically_resolved
    assert not unread, f"registered as editable but never read: {sorted(unread)}"

    # Prove the exemption is real rather than a way to hide a dead knob: the
    # flags ARE read, through a name computed at call time.
    from backend.ml.constants import all_optional_capabilities
    from config import settings

    reported = all_optional_capabilities()
    assert len(reported) == len(dynamically_resolved)
    for name in dynamically_resolved:
        assert hasattr(settings, name), f"{name} is not a declared field"


# ---------------------------------------------------------------------------
# What must NOT be editable
# ---------------------------------------------------------------------------

def test_security_critical_settings_refuse_runtime_mutation():
    from backend.core.runtime_settings import apply_to_runtime
    from backend.security.config_guard import SECURITY_CRITICAL_KEYS

    for key in sorted(SECURITY_CRITICAL_KEYS):
        assert apply_to_runtime(key, "irrelevant") is False, (
            f"{key} was applied to the live settings object")


@pytest.mark.parametrize("key", [
    # Constructed once at import: a ThreadPoolExecutor, asyncio.Semaphores and
    # the global queue. Nothing re-reads these, and pretending otherwise means
    # an operator raises a limit, sees "applied", and gets no more throughput.
    "INFERENCE_WORKERS",
    "MAX_CONCURRENT_INFERENCE",
    "MAX_CONCURRENT_INFERENCE_PER_PIPELINE",
    "QUEUE_WORKERS",
    "MAX_QUEUE_SIZE",
    "MAX_CONCURRENT_REQUESTS",
    "PIPELINE_BATCH_SIZE",
    # Chooses which index implementation is constructed at startup.
    "VECTOR_BACKEND",
    # Process shape.
    "WORKERS",
])
def test_concurrency_primitives_are_not_advertised_as_dynamic(key):
    from backend.core.runtime_settings import DYNAMIC_APPLY_MODES, get_meta

    mode = get_meta(key).apply_mode
    assert mode not in DYNAMIC_APPLY_MODES, (
        f"{key} claims apply_mode={mode!r}, but it is consumed once at import")


def test_a_dynamic_setting_actually_changes_observed_behaviour():
    """End-to-end for one representative `immediate` entry.

    The static scan proves the read is inside a function; this proves the write
    path reaches it. Uses a consumer whose answer is a pure function of the
    setting, so the assertion cannot pass for an unrelated reason.
    """
    from backend.core.runtime_settings import apply_to_runtime
    from backend.ml.constants import optional_capability_status
    from config import settings

    original = settings.MLFLOW_ENABLED
    try:
        assert apply_to_runtime("MLFLOW_ENABLED", not original) is True
        assert optional_capability_status("mlflow")["configured"] is (not original), (
            "the consumer did not observe an applied runtime change")
    finally:
        apply_to_runtime("MLFLOW_ENABLED", original)
        assert settings.MLFLOW_ENABLED == original


def test_an_immutable_setting_reports_that_a_restart_is_needed():
    """The honest half: `apply_to_runtime` returns False rather than pretending.

    False is what makes the API answer "restart required" instead of "applied".
    """
    from backend.core.runtime_settings import apply_to_runtime
    from config import settings

    original = settings.MAX_QUEUE_SIZE
    assert apply_to_runtime("MAX_QUEUE_SIZE", original + 500) is False
    assert settings.MAX_QUEUE_SIZE == original, (
        "a restart-required setting was mutated on the live object anyway")
