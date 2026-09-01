"""
Checks on the deployment orchestrator (deploy.sh and scripts/deploy/*.sh).

    docker exec face_recognition_api python -m pytest tests/test_deploy_script.py -v

Two kinds of assertion live here:

1. The shell suite. `./deploy.sh --self-test` exercises the pure functions --
   Alembic head derivation, docker/.env editing, GPU allocation for 0/1/2/3
   cards, model-manifest verification, the deployment state file, the
   configuration fingerprint and --dry-run inertness -- against stubbed
   inputs. Running it from pytest means a regression in the deployment path
   fails the same suite as a regression in the application.

2. Source-level invariants that no run can be trusted to reveal, because the
   dangerous paths are the ones a test must never actually take: uninstall
   must not delete volumes, the host-specific GPU overlay must not be
   committed, and the manifest must describe exactly the weights the
   application loads.

The shell suite needs bash and coreutils, which the API image has; it needs no
docker socket, so it runs unchanged inside the regression container.
"""

import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy.sh")
STAGE_DIR = os.path.join(ROOT, "scripts", "deploy")

STAGE_MODULES = [
    "lib.sh",
    "stage-install.sh",
    "stage-gpu.sh",
    "stage-models.sh",
    "stage-db.sh",
    "stage-dev.sh",
    "stage-health.sh",
    "stage-upgrade.sh",
    "self-test.sh",
]

SUBCOMMANDS = [
    "install", "validate", "start", "stop", "restart", "status", "health",
    "gpu-test", "model-check", "model-manifest", "backup", "restore",
    "upgrade", "logs", "uninstall", "dev",
]


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def run_deploy(*args, timeout=600):
    return subprocess.run(
        ["bash", DEPLOY, *args],
        cwd=ROOT, capture_output=True, text=True, timeout=timeout,
    )


def _bash_available():
    try:
        return subprocess.run(["bash", "-c", "exit 0"], capture_output=True).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


requires_bash = pytest.mark.skipif(not _bash_available(), reason="bash is not available")


# --------------------------------------------------------------------------
# 1. the shell self-test suite
# --------------------------------------------------------------------------
@requires_bash
def test_self_test_suite_passes():
    result = run_deploy("--self-test")
    tail = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    assert result.returncode == 0, (
        "deploy.sh --self-test failed:\n" + result.stdout + "\n" + result.stderr
    )

    match = re.search(r"(\d+) passed, (\d+) failed", tail)
    assert match, "no result line in the self-test output; last line was " + repr(tail)
    passed, failed = int(match.group(1)), int(match.group(2))
    assert failed == 0, str(failed) + " self-test(s) failed:\n" + result.stdout
    # A suite that silently stopped running its cases would still report
    # "0 failed", so pin the floor too.
    assert passed >= 50, (
        "only " + str(passed) + " self-tests ran -- did a group stop being called?"
    )


@requires_bash
@pytest.mark.parametrize("group", [
    "alembic head derivation",
    "docker/.env editing",
    "GPU detection and allocation",
    "model weight verification (fail-closed)",
    "deployment state",
    "configuration fingerprint",
    "origin parsing",
    "--dry-run inertness",
])
def test_self_test_covers_every_group(group):
    """Each group is the pure core of a stage; dropping one would go unnoticed."""
    assert group in run_deploy("--self-test").stdout


@requires_bash
@pytest.mark.parametrize(
    "script", ["deploy.sh"] + ["scripts/deploy/" + m for m in STAGE_MODULES]
)
def test_shell_sources_parse(script):
    result = subprocess.run(
        ["bash", "-n", os.path.join(ROOT, script)], capture_output=True, text=True
    )
    assert result.returncode == 0, script + " does not parse:\n" + result.stderr


@requires_bash
@pytest.mark.parametrize("subcommand", SUBCOMMANDS)
def test_help_documents_every_subcommand(subcommand):
    result = run_deploy("--help")
    assert result.returncode == 0
    # The synopsis is the block before the prose. A subcommand missing from it
    # is a subcommand nobody can discover.
    synopsis = result.stdout.split("WHAT THIS IS")[0]
    pattern = r"deploy\.sh\b[^\n]*\b" + re.escape(subcommand) + r"\b"
    assert re.search(pattern, synopsis), (
        subcommand + " is not in the --help synopsis"
    )


@requires_bash
def test_unknown_flag_is_rejected():
    """A typo must not be treated as a subcommand and silently deploy."""
    result = run_deploy("--not-a-real-flag")
    assert result.returncode == 2, result.stdout + result.stderr


# --------------------------------------------------------------------------
# 2. invariants a test run must never exercise for real
# --------------------------------------------------------------------------
def test_uninstall_deletes_volumes_only_behind_the_typed_phrase():
    """
    `down -v` destroys the database, the face gallery and the ML artifacts.
    It is allowed exactly once in the tree, inside the --purge-data branch,
    and that branch must demand the typed confirmation phrase.
    """
    source = read(os.path.join(STAGE_DIR, "stage-upgrade.sh"))
    # Comments explain the rule; only real commands can break it.
    code = [line for line in source.splitlines() if not line.strip().startswith("#")]
    destructive = [
        line.strip() for line in code
        if re.search(r"\bdown\b.*(\s-v\b|--volumes)", line)
    ]
    assert len(destructive) == 1, (
        "expected exactly one volume-deleting down, found " + repr(destructive)
    )

    purge_block = source[source.index('if [ "$PURGE_DATA" = "1" ]'):]
    assert "confirm_phrase" in purge_block
    assert "DELETE face_detector_prod DATA" in purge_block

    # The default path must not carry the flag at all.
    assert "compose_mutate down --remove-orphans || die" in source


def test_no_stage_recursively_deletes_a_data_directory():
    """No stage may rm -rf a path holding secrets, certificates or data."""
    protected = ("secrets", "certs", "backups", "weights", "storage", "map-data")
    recursive_force = r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\b"
    for name in ["deploy.sh"] + ["scripts/deploy/" + m for m in STAGE_MODULES]:
        for line in read(os.path.join(ROOT, name)).splitlines():
            if re.search(recursive_force, line):
                hit = [p for p in protected if "/" + p in line or '"' + p in line]
                assert not hit, (
                    name + ": recursive delete touches protected data: " + line.strip()
                )


def test_generated_gpu_overlay_is_not_committed():
    """
    The overlay pins this host's GPU UUIDs. Committed, it would be applied on a
    machine whose cards have different UUIDs and the stack would not start.
    """
    ignore = read(os.path.join(ROOT, ".gitignore"))
    assert "docker/gpu-allocation.generated.yml" in ignore


def test_weights_manifest_describes_exactly_the_loaded_models():
    """
    Two ONNX files are loaded at runtime. The recogniser's FILENAME is the
    embedding-version stamp written into every stored vector, so the manifest
    is what stops a substituted file from reaching a container.
    """
    manifest = read(os.path.join(ROOT, "weights", "WEIGHTS_MANIFEST.json"))
    for filename in ("det_10g.onnx", "w600k_r50.onnx"):
        assert '"' + filename + '"' in manifest, filename + " is not in the manifest"
    shas = re.findall(r'"sha256"\s*:\s*"([0-9a-f]*)"', manifest)
    assert len(shas) >= 2, "the manifest carries fewer than two checksums"
    for sha in shas:
        assert len(sha) == 64, "not a sha256: " + repr(sha)


def test_orchestrator_derives_the_migration_head_instead_of_hardcoding_it():
    """
    deploy.sh derives the head from alembic/versions. A literal fallback would
    go stale exactly like the compose default did, and the migrate job would
    then refuse to run on a fresh install.
    """
    lib = read(os.path.join(STAGE_DIR, "lib.sh"))
    assert "derive_migrations_head" in lib
    body = lib[lib.index("derive_migrations_head()"):]
    body = body[:body.index("\n}\n")]
    assert "alembic/versions" in body
    assert not re.search(r"\b[0-9a-f]{12}\b", body), (
        "derive_migrations_head contains a hardcoded revision id"
    )
