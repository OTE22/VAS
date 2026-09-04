"""Volumes: what the app WRITES must land where the deployment PERSISTS.

The compose file documents a rationale for every volume, and the rationales are
right. The gap was between them and the settings: a path can be configured to
somewhere no volume covers, and nothing fails. The container starts, the
directory is created in the writable layer, the app writes to it happily, and
the data disappears on the next `up --build` or image update.

That is what CHROMADB_PATH did. It pointed at /app/sql_agent/chromadb_data --
image filesystem -- while a volume named `chromadb_cache` sat one line below in
the same service, close enough to read as "the chroma data is handled".  It is
not: that volume mounts ~/.cache/chroma, the ONNX embedding-model download
(~167MB of cache), and never touched the vector store. The SQL agent's RAG
index was being silently rebuilt on every container recreate.

Measured before the fix: the directory did not exist in the image, held a
565KB chroma.sqlite3 created at runtime, and sat on no mount.

Run:  python -m pytest tests/test_volume_contract.py -v
"""

import io
import os
import re

import pytest
import yaml

from tests._repo_scan import find_repo_root

REPO = find_repo_root()
STACKS = ["docker/docker-compose.prod.yml", "docker/docker-compose.cpu.yml"]


def _load(rel):
    with io.open(os.path.join(REPO, rel), encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _services(doc):
    return (doc or {}).get("services", {}) or {}


INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _split_mount(entry):
    """Split source:target[:mode], ignoring colons inside ${VAR:-default}.

    `${OLLAMA_MODELS_PATH:-ollama_models}:/root/.ollama` is one source and one
    target; a plain str.split(":") reads it as four fields and reports both a
    volume that is mounted and a mount that is undeclared.
    """
    masked = INTERPOLATION.sub(lambda m: "\x00" * len(m.group(0)), entry)
    parts, start = [], 0
    for index, char in enumerate(masked):
        if char == ":":
            parts.append(entry[start:index])
            start = index + 1
    parts.append(entry[start:])
    return parts


def _resolve_source(source):
    """`${VAR:-ollama_models}` names ollama_models unless the operator
    substitutes a host path, which is the documented purpose of that knob."""
    match = INTERPOLATION.fullmatch(source)
    return match.group(2) if match and match.group(2) is not None else source


def _mount_targets(service):
    """Destination paths this service mounts, whatever the source type."""
    targets = []
    for entry in service.get("volumes", []) or []:
        if isinstance(entry, str):
            parts = _split_mount(entry)
            if len(parts) >= 2:
                targets.append(parts[1])
        elif isinstance(entry, dict) and entry.get("target"):
            targets.append(entry["target"])
    return targets


def _is_covered(path, targets):
    return any(path == t or path.startswith(t.rstrip("/") + "/") for t in targets)


# Settings naming a filesystem location the application WRITES. A read-only
# reference path (weights, map data) is deliberately not here: those must be
# mounted, but they must not be persisted state.
WRITE_PATH_SETTINGS = ("STORAGE_DIR", "LOG_DIR", "ML_ARTIFACT_DIR",
                       "CHROMADB_PATH", "BACKUP_DIR")


@pytest.mark.parametrize("stack", STACKS)
def test_every_configured_write_path_is_on_a_mount(stack):
    """The general form of the CHROMADB_PATH defect.

    Any setting naming a directory the app writes must resolve underneath a
    mount destination in the SAME service, or its contents live in the
    container's writable layer and are lost on recreate.
    """
    orphaned = []
    for name, service in _services(_load(stack)).items():
        environment = service.get("environment", {}) or {}
        if not isinstance(environment, dict):
            continue
        targets = _mount_targets(service)
        for setting in WRITE_PATH_SETTINGS:
            value = environment.get(setting)
            if not value or not str(value).startswith("/"):
                continue
            if not _is_covered(str(value), targets):
                orphaned.append(f"{name}.{setting}={value}")
    assert not orphaned, (
        f"{stack}: these settings name a write path that no mount covers, so "
        f"their contents are discarded on container recreate: {orphaned}")


@pytest.mark.parametrize("stack", STACKS)
def test_every_declared_volume_is_actually_mounted(stack):
    """A declared-but-unmounted volume is dead weight and usually a rename.

    It also reads as coverage that does not exist - which is precisely how
    chromadb_cache disguised the missing persistence above.
    """
    doc = _load(stack)
    declared = set((doc.get("volumes") or {}).keys())
    used = set()
    for service in _services(doc).values():
        for entry in service.get("volumes", []) or []:
            if isinstance(entry, str) and not entry.startswith((".", "/")):
                used.add(_resolve_source(_split_mount(entry)[0]))
            elif isinstance(entry, dict) and entry.get("type") == "volume":
                used.add(entry.get("source"))
    unmounted = sorted(declared - used)
    assert not unmounted, (
        f"{stack} declares volumes nothing mounts: {unmounted}")


@pytest.mark.parametrize("stack", STACKS)
def test_every_mounted_volume_is_declared(stack):
    """Compose errors on this, but the error names one volume at a time."""
    doc = _load(stack)
    declared = set((doc.get("volumes") or {}).keys())
    missing = {}
    for name, service in _services(doc).items():
        for entry in service.get("volumes", []) or []:
            source = None
            if isinstance(entry, str) and not entry.startswith((".", "/")):
                source = _resolve_source(_split_mount(entry)[0])
            elif isinstance(entry, dict) and entry.get("type") == "volume":
                source = entry.get("source")
            if source and source not in declared:
                missing.setdefault(source, []).append(name)
    assert not missing, f"{stack} mounts undeclared volumes: {sorted(missing)}"


# Host paths that are reference data or credentials. A container that can WRITE
# these can rewrite the model it is verified against, the map archives the
# offline gate checks, or its own secret.
READ_ONLY_HOST_PATHS = ("../weights", "../map-data", "../secrets", "../certs",
                        "../nginx.prod.conf", "../init-db.sql",
                        "../docker/redis/users.acl", "./redis/users.acl")


def test_reference_data_and_credentials_are_mounted_read_only():
    """Complements test_certificates_are_mounted_read_only, which covers certs
    alone; the same argument applies to weights, map data and secrets."""
    writable = []
    for stack in STACKS:
        for name, service in _services(_load(stack)).items():
            for entry in service.get("volumes", []) or []:
                if not isinstance(entry, str):
                    continue
                parts = _split_mount(entry)
                source = parts[0]
                if not any(source.startswith(p) for p in READ_ONLY_HOST_PATHS):
                    continue
                if len(parts) < 3 or "ro" not in parts[2].split(","):
                    writable.append(f"{os.path.basename(stack)}:{name}:{entry}")
    assert not writable, (
        "reference data or credentials mounted writable: " + str(writable))


def test_the_two_stacks_never_share_a_volume_namespace():
    """The documented incident this guards: both files derived their project
    name from the parent directory ("docker"), so production and development
    resolved to the same docker_postgres_data and STARTING PRODUCTION MOUNTED
    THE DEVELOPMENT DATABASE.
    """
    names = {}
    for stack in STACKS:
        doc = _load(stack)
        names[stack] = doc.get("name")
    assert all(names.values()), (
        f"a stack declares no project name, so Compose derives it from the "
        f"parent directory and both stacks collide: {names}")
    assert len(set(names.values())) == len(names), (
        f"stacks share a project name and therefore share volumes: {names}")
