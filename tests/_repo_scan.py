"""Shared helpers for the source-scanning guard tests.

Two of those suites (`test_config_single_source`, `test_runtime_editability`)
read the repository's SOURCE rather than its behaviour, because the failures
they catch are invisible at runtime — nothing errors, the wrong number is
simply used. That makes the scan itself load-bearing, and it had three holes:

1. **`REPO = "/app"` with no guard.** Outside the container that path does not
   exist, `os.walk` yields nothing, the scan finds zero read sites, and every
   assertion of the form `assert not offenders` passes on an empty set. The
   suites reported green while checking nothing.

2. **A fixed alias list.** Only `settings`, `config_settings` and
   `main_settings` were recognised as the settings object, so
   `from config import settings as app_settings` was invisible — which is
   exactly how `getattr(app_settings, "MAP_OFFLINE_TILES_ENABLED", False)`
   shipped a fallback contradicting the declared default of True.

3. **`__init__` counted as a live read.** A read inside a function was assumed
   to re-run. For a class instantiated once at module scope, `__init__` runs
   exactly once at import, so those reads are frozen in the same way a
   module-level read is — the case that kept `UNKNOWN_SIMILARITY_THRESHOLD`
   frozen inside `identity_clustering` while the test called it live.
"""

import ast
import os

REPO_MARKERS = ("config.py", "backend", "db_connection.py")


def find_repo_root() -> str:
    """Locate the repository root, or fail loudly.

    Never returns a path that does not exist: a scan rooted at a missing
    directory silently examines nothing, which is worse than an error because
    it looks like a pass.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = here
    for _ in range(6):
        if all(os.path.exists(os.path.join(candidate, m)) for m in REPO_MARKERS):
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    raise RuntimeError(
        "Could not locate the repository root from "
        f"{here!r} (looked for {', '.join(REPO_MARKERS)}). The source-scanning "
        "guard tests cannot run without it, and must not pass vacuously.")


def iter_source_files(repo: str, roots, extra_files=()):
    """Every .py file under `roots`, plus `extra_files` at the repo root."""
    seen = 0
    for root in roots:
        base = os.path.join(repo, root)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, names in os.walk(base):
            if "__pycache__" in dirpath or f"{os.sep}legacy{os.sep}" in dirpath + os.sep:
                continue
            for name in names:
                if name.endswith(".py"):
                    seen += 1
                    yield os.path.join(dirpath, name)
    for name in extra_files:
        path = os.path.join(repo, name)
        if os.path.exists(path):
            seen += 1
            yield path
    if seen == 0:
        raise RuntimeError(
            f"No source files found under {repo!r} — the scan would pass "
            "vacuously. Check the repo root and SCAN_ROOTS.")


# Modules that export the settings singleton. `backend.config` re-exports it.
_SETTINGS_MODULES = {"config", "backend.config"}


def settings_aliases(tree: ast.AST) -> set:
    """Every local name bound to the settings singleton in this module.

    Resolved from the AST rather than a hard-coded list, so a new alias is
    covered the day it is written instead of the day someone remembers to add
    it here. Covers:

        from config import settings                  -> {"settings"}
        from config import settings as app_settings  -> {"app_settings"}
        import config            + config.settings   -> {"config.settings"}
        cfg = settings                               -> {"cfg"}
    """
    aliases = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _SETTINGS_MODULES:
            for alias in node.names:
                if alias.name == "settings":
                    aliases.add(alias.asname or alias.name)

    # Second pass: local rebindings of an alias we already know about
    # (`cfg = settings`, `_settings = app_settings`). Repeated until stable so
    # a chain of rebindings resolves.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Name):
                continue
            if node.value.id not in aliases:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True

    return aliases


def module_level_singleton_classes(tree: ast.AST) -> set:
    """Class names instantiated at module scope in this file.

    `x = SomeClass()` at module level means `SomeClass.__init__` runs once, at
    import. Anything it reads off `settings` is captured then and never re-read,
    so those reads are frozen even though they sit inside a function body.
    """
    names = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            names.add(value.func.id)
    return names


def frozen_init_functions(tree: ast.AST) -> set:
    """ids of FunctionDef nodes whose body runs exactly once, at import.

    Currently: `__init__` (and `__new__`) of a class instantiated at module
    scope in the same file.
    """
    singletons = module_level_singleton_classes(tree)
    frozen = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name not in singletons:
            continue
        for item in node.body:
            if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name in ("__init__", "__new__")):
                frozen.add(id(item))
    return frozen


def strip_comments_and_docstrings(source: str) -> str:
    """Source with comments and string literals blanked out.

    Prose repeatedly produced false positives in these scans — a docstring
    explaining why a literal was removed reads identically to the literal.

    Handles indented input: `inspect.getsource` on a method returns it at its
    class indentation, which `ast.parse` rejects. Without the dedent the parse
    raised SyntaxError, this returned the source untouched, and every comment
    in it was scanned as if it were code.
    """
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        # Genuinely unparseable: strip comments line-wise rather than give up,
        # so the caller never silently scans prose.
        return "\n".join(
            line[:line.find("#")] if "#" in line else line
            for line in source.splitlines())

    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.end_lineno is not None:
                spans.append((node.lineno, node.end_lineno))

    lines = source.splitlines()
    blanked = set()
    for start, end in spans:
        for lineno in range(start, end + 1):
            blanked.add(lineno)

    out = []
    for index, line in enumerate(lines, start=1):
        if index in blanked:
            out.append("")
            continue
        hash_at = line.find("#")
        out.append(line[:hash_at] if hash_at != -1 else line)
    return "\n".join(out)
