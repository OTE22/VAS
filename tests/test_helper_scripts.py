"""Helper scripts must stay correct, or they are worse than absent.

    docker exec face_recognition_api python -m pytest tests/test_helper_scripts.py -v

`docker/` accumulated ~1,000 lines of scripts nothing referenced, including two
independent implementations of the same GPU-detecting launcher. They were not
merely dead: after the compose restructure they were *wrong* — invoking
`-f docker-compose.gpu.yml` alone, which no longer declares a database — while
still looking authoritative to anyone who found them.
"""

import pathlib
import re

import pytest

REPO = pathlib.Path("/app")
SCRIPT_SUFFIXES = (".sh", ".bat", ".ps1")
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "site-packages"}


def _scripts():
    for path in REPO.rglob("*"):
        if path.suffix not in SCRIPT_SUFFIXES or not path.is_file():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        yield path


def _text(path):
    return path.read_text(encoding="utf-8", errors="replace")


def test_no_script_invokes_a_gpu_override_on_its_own():
    """docker-compose.gpu.yml and prod.gpu.yml are OVERRIDES. Alone they
    declare no postgres, no redis and no nginx, so a single-`-f` invocation
    starts a broken partial stack.

    The property checked is pairing on the same command: a line that names a
    GPU override must also name its base. Adjacency is deliberately NOT
    required — auto-start.sh composes the arguments from shell variables,
    which is fine and should stay allowed.
    """
    pairs = {"docker-compose.gpu.yml": "docker-compose.cpu.yml",
             "docker-compose.prod.gpu.yml": "docker-compose.prod.yml"}
    offenders = []
    for path in _scripts():
        for number, line in enumerate(_text(path).splitlines(), 1):
            if line.lstrip().startswith(("#", "REM", "::")):
                continue
            if not re.search(r"docker[- ]compose\s", line):
                continue
            for override, base in pairs.items():
                # prod.gpu.yml also contains the substring "gpu.yml"; match the
                # most specific name present so the check is not fooled.
                if override == "docker-compose.gpu.yml" and "prod.gpu.yml" in line:
                    continue
                if override in line and base not in line:
                    offenders.append(
                        f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert not offenders, (
        "these invoke a GPU override without its base stack:\n  "
        + "\n  ".join(offenders))


def test_no_script_uses_the_compose_v1_binary():
    """`docker-compose` (v1) ignores the top-level `name:` key that keeps the
    development and production stacks on separate volumes — the key that stops
    production mounting the dev database."""
    v1 = re.compile(r"(?<![\w-])docker-compose\s+(-f|up|down|ps|exec|logs|build|restart|config)")
    offenders = []
    for path in _scripts():
        for number, line in enumerate(_text(path).splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith(("#", "REM", "::")):
                continue
            # An echoed hint is still instruction to the operator, so it counts.
            if v1.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert not offenders, (
        "these use the legacy docker-compose v1 binary:\n  " + "\n  ".join(offenders))


def test_no_script_hardcodes_a_stale_or_orphaned_volume_name():
    """Volumes are namespaced by the compose project. `face_detector_*` is the
    ORPHANED prefix from an older layout; the live ones are
    `face_detector_dev_*` / `face_detector_prod_*`. A script naming the old
    prefix silently operates on a volume nothing mounts."""
    stale = re.compile(r"face_detector_(?!dev_|prod_)[a-z_]+|docker_(?:postgres|redis|chromadb|face_database)_\w*")
    offenders = []
    for path in _scripts():
        for number, line in enumerate(_text(path).splitlines(), 1):
            if line.lstrip().startswith(("#", "REM", "::")):
                continue
            match = stale.search(line)
            if match:
                offenders.append(
                    f"{path.relative_to(REPO)}:{number}: {match.group(0)}")
    assert not offenders, (
        "these name a volume from a superseded project layout:\n  "
        + "\n  ".join(offenders))


def test_every_script_in_docker_is_referenced_somewhere():
    """A script nothing points at rots into wrong-but-authoritative. Four such
    launchers accumulated here before this test existed."""
    docker_scripts = [p for p in (REPO / "docker").iterdir()
                      if p.suffix in SCRIPT_SUFFIXES]
    assert docker_scripts, "no helper scripts found under docker/"

    # Bounded search. An unbounded REPO.rglob walks storage/, tiles/ (145k
    # files) and every cache directory, which took this test past three
    # minutes. These are the only places that plausibly reference a helper
    # script, plus the top-level files.
    searchable = []
    for directory in ("Docs", "scripts", "docker", "tests", "monitoring", "db"):
        root = REPO / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if (path.is_file() and path.suffix in
                    (".md", ".py", ".sh", ".bat", ".ps1", ".yml", ".yaml")
                    and not SKIP_DIRS & set(path.parts)):
                searchable.append(path)
    searchable += [p for p in REPO.iterdir()
                   if p.is_file() and p.suffix in (".md", ".yml", ".yaml", ".sh")]

    # Read the corpus ONCE. Reading it per script turned this into ~1,200 file
    # reads and made a guard test the slowest thing in the suite.
    script_paths = {p.resolve() for p in docker_scripts}
    corpus = "\n".join(
        _text(path) for path in searchable if path.resolve() not in script_paths)

    orphans = [script.name for script in docker_scripts
               if script.name not in corpus]
    assert not orphans, (
        f"these scripts under docker/ are referenced by nothing — delete them "
        f"or wire them into the docs: {orphans}")


@pytest.mark.parametrize("name", ["start.sh", "start.bat",
                                  "add_settings_tables.py"])
def test_the_removed_duplicates_have_not_come_back(name):
    """start.* duplicated auto-start.*; add_settings_tables.py mutated the
    schema outside Alembic. Both classes of file reappear by copy-paste."""
    assert not (REPO / "docker" / name).exists(), (
        f"docker/{name} is back; it was removed deliberately")


def test_the_launcher_knows_about_both_files_of_the_gpu_pair():
    """The one supported GPU development invocation is the layered pair.

    Pairing on a single command line is enforced by
    test_no_script_invokes_a_gpu_override_on_its_own; this only checks the
    launcher still knows the GPU path exists at all. Kept separate because a
    launcher that quietly lost its GPU branch would still pass that test —
    there would simply be nothing left to pair."""
    for name in ("auto-start.sh", "auto-start.bat"):
        text = _text(REPO / "docker" / name)
        assert "docker-compose.cpu.yml" in text, (
            f"docker/{name} no longer references the CPU stack")
        assert "docker-compose.gpu.yml" in text, (
            f"docker/{name} lost its GPU branch — GPU development would "
            f"silently run on the CPU")
