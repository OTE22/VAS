"""A compose file must not set a setting that nothing reads.

`test_compose_sets_no_unknown_application_setting` already proves compose sets
no name config.py fails to DECLARE. This is the other half: a name can be
declared, look like a real tuning knob, be set in production, and be read by
nothing at all.

That was true of REDIS_POOL_SIZE. It is declared in config.py, was set to 25 in
docker-compose.prod.yml, and reached the running container as an environment
variable - while nothing in the codebase ever read it. The pool size the redis
client actually uses is REDIS_MAX_CONNECTIONS, set on the line above it. An
operator tuning REDIS_POOL_SIZE would have changed nothing and had no way to
tell.

The codebase already knew. backend/routes/settings.py hides these from the
admin UI, saying "nothing in the codebase reads them, so rendering them here
offered editable knobs wired to nothing", and Docs/63_REDIS_CACHING_GUIDE.md
states it outright. Only compose disagreed - which is exactly the kind of
disagreement no one notices, because the value is applied without complaint.

Run:  python -m pytest tests/test_no_dead_knobs.py -v
"""

import ast
import io
import os
import re

import pytest

from tests._repo_scan import find_repo_root

REPO = find_repo_root()
COMPOSE_FILES = [
    "docker/docker-compose.prod.yml",
    "docker/docker-compose.prod.gpu.yml",
    "docker/docker-compose.cpu.yml",
    "docker/docker-compose.gpu.yml",
]

# Declared in config.py, read by NOTHING. Verified by scanning every .py for an
# attribute access or a literal getattr, and every .sh for a shell expansion.
#
# They are not deleted from config.py here - that is a separate decision, and a
# declared-but-unread field is harmless as long as no deployment file pretends
# to configure it. This test enforces exactly that boundary.
DEAD_SETTINGS = {
    "REDIS_POOL_SIZE": "the client uses REDIS_MAX_CONNECTIONS",
    "CACHE_VERSION": "nothing reads it",
    "CACHE_WARNING_ENABLED": "nothing reads it",
    "CACHE_WARNING_INTERVAL": "nothing reads it",
    "BATCH_WRITE_MAX_WAIT": "batch_writer.py reads BATCH_WRITE_INTERVAL and BATCH_WRITE_SIZE",
    "GPU_BATCH_SIZE": "superseded by PIPELINE_BATCH_SIZE",
    "CPU_BATCH_SIZE": "superseded by PIPELINE_BATCH_SIZE",
    "MAP_DEFAULT_LAT": "nothing reads it",
    "MAP_DEFAULT_LON": "nothing reads it",
    "MAP_DEFAULT_ZOOM": "nothing reads it",
}

ASSIGNMENT = re.compile(r"^\s{4,}([A-Z][A-Z0-9_]*):\s*(.+?)\s*$")


def _read(rel):
    path = os.path.join(REPO, rel)
    if not os.path.exists(path):
        return ""
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


def _assignments(rel):
    """Names assigned under an environment: block, ignoring comment lines."""
    found = {}
    for line in _read(rel).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = ASSIGNMENT.match(line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


@pytest.mark.parametrize("stack", COMPOSE_FILES)
def test_compose_sets_no_setting_that_nothing_reads(stack):
    """Setting one of these is worse than leaving it out: it looks like it
    works, applies silently, and changes nothing."""
    offenders = []
    for name, why in DEAD_SETTINGS.items():
        if name in _assignments(stack):
            offenders.append(f"{name} ({why})")
    assert not offenders, (
        f"{stack} sets settings that nothing in the codebase reads, so they are "
        f"tuning knobs wired to nothing: {offenders}")


def test_the_dead_list_is_still_accurate():
    """Guards the guard.

    If someone later WIRES one of these up, this list becomes wrong and would
    keep forbidding a setting that had become real. Fail loudly so the list is
    corrected rather than silently misleading the next reader.
    """
    sources = []
    for base, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs
                   if d not in {".git", "__pycache__", "node_modules", "tests",
                                "logs", ".venv", "venv"}]
        sources.extend(os.path.join(base, f) for f in files if f.endswith(".py"))

    now_read = set()
    for path in sources:
        if os.path.basename(path) == "config.py":
            continue                      # the declaration itself is not a read
        try:
            tree = ast.parse(io.open(path, encoding="utf-8", errors="ignore").read())
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in DEAD_SETTINGS:
                now_read.add(node.attr)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "getattr" and len(node.args) >= 2
                  and isinstance(node.args[1], ast.Constant)
                  and node.args[1].value in DEAD_SETTINGS):
                now_read.add(node.args[1].value)

    assert not now_read, (
        "these are listed as dead but the code now reads them — remove them "
        f"from DEAD_SETTINGS and wire them up in compose: {sorted(now_read)}")
