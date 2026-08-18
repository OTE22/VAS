"""Documentation cannot drift away from the code without failing a test.

    docker exec face_recognition_api python -m pytest tests/test_documentation_consistency.py -v

Every assertion here encodes a mistake that was actually found in this
repository, not a hypothetical one. Each failure message says what to do.
"""

import pathlib
import re

import pytest

REPO = pathlib.Path("/app")
DOCS = REPO / "Docs"
INDEX = DOCS / "00_DOCUMENTATION_INDEX.md"
README = REPO / "README.md"


def _markdown_files():
    return sorted(DOCS.glob("*.md"))


def _text(path):
    return path.read_text(encoding="utf-8", errors="replace")


def _relative_md_links(text):
    """Markdown links to a local .md file, ignoring URLs and pure anchors."""
    return re.findall(r"\]\((?!https?://|#)([^)#]+\.md)(?:#[^)]*)?\)", text)


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------

def test_the_root_readme_exists():
    """The repository had no README at all for its entire history."""
    assert README.is_file(), "there is no README.md at the repository root"
    assert len(_text(README)) > 1000


def test_no_documentation_link_dangles():
    """A link to a file that does not exist is worse than no link — the reader
    assumes the document is missing rather than that the link is wrong."""
    broken = []
    for path in _markdown_files() + [README]:
        for target in _relative_md_links(_text(path)):
            if not (path.parent / target).is_file():
                broken.append(f"{path.name} -> {target}")
    assert not broken, "dangling documentation links:\n  " + "\n  ".join(sorted(set(broken)))


def test_every_document_is_reachable_from_the_index():
    """52 files were once unreachable from the index, including the real
    deployment runbook and the backup guide."""
    linked = {pathlib.Path(t).name for t in _relative_md_links(_text(INDEX))}
    orphans = [p.name for p in _markdown_files()
               if p.name != INDEX.name and p.name not in linked]
    assert not orphans, (
        f"{len(orphans)} documents are not linked from 00_DOCUMENTATION_INDEX.md: "
        f"{sorted(orphans)}")


# ---------------------------------------------------------------------------
# commands in the docs must be runnable
# ---------------------------------------------------------------------------

def test_no_document_tells_the_reader_to_run_a_root_compose_file():
    """There is no docker-compose.yml at the repository root; the stacks live
    under docker/. A bare `docker-compose up -d` fails immediately."""
    assert not list(REPO.glob("docker-compose*.yml")), (
        "a root compose file now exists — relax this test, it is guarding a "
        "layout that has changed")

    offenders = []
    # a compose invocation whose next token is a subcommand, i.e. no -f
    pattern = re.compile(r"^\s*docker[- ]compose\s+(?!-f|--file|\$)(up|down|restart|build|exec|logs|ps)\b",
                         re.MULTILINE)
    for path in _markdown_files() + [README]:
        for match in pattern.finditer(_text(path)):
            offenders.append(f"{path.name}: {match.group(0).strip()}")
    assert not offenders, (
        "these commands assume a compose file at the repository root, which "
        "does not exist — add `-f docker/docker-compose.<stack>.yml`:\n  "
        + "\n  ".join(offenders))


def test_no_document_uses_a_container_name_that_does_not_exist():
    """`face_recognition_backend` has never been a container in this project;
    the API container is face_recognition_api."""
    offenders = [p.name for p in _markdown_files() + [README]
                 if "face_recognition_backend" in _text(p)]
    assert not offenders, (
        f"{offenders} reference the container 'face_recognition_backend', "
        f"which does not exist — the API container is 'face_recognition_api'")


def test_alembic_commands_specify_the_working_directory():
    """alembic.ini lives in /app/alembic, not /app. Without -w the command
    fails with "No 'script_location' key found in configuration"."""
    offenders = []
    for path in _markdown_files() + [README]:
        for line in _text(path).splitlines():
            if re.search(r"(docker exec|exec)\s.*\balembic\s+(upgrade|downgrade|current|heads|history)", line):
                if "-w /app/alembic" not in line and "--workdir" not in line:
                    offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, (
        "alembic invocations missing `-w /app/alembic` (alembic.ini is not in "
        "/app, so these fail):\n  " + "\n  ".join(offenders))


# ---------------------------------------------------------------------------
# contracts the code actually implements
# ---------------------------------------------------------------------------

def test_documentation_does_not_invent_an_api_health_endpoint():
    """Project contract: the detailed health endpoint is /health/detailed.
    A negative mention ("there is no /api/health") is allowed."""
    offenders = []
    for path in _markdown_files() + [README]:
        for number, line in enumerate(_text(path).splitlines(), 1):
            if "/api/health" not in line:
                continue
            if re.search(r"\b(no|not|never|instead of|rather than)\b", line, re.I):
                continue  # explicitly telling the reader it does NOT exist
            offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, (
        "these lines document /api/health, which the application does not "
        "serve — the detailed health endpoint is /health/detailed:\n  "
        + "\n  ".join(offenders))


def test_the_version_is_not_restated_as_a_conflicting_number():
    """config.py is the single source of truth for VERSION. The index says so;
    this checks the index does not itself hard-code a different one."""
    source = _text(REPO / "config.py")
    match = re.search(r'^\s*VERSION:\s*str\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    assert match, "VERSION is no longer declared in config.py as expected"
    version = match.group(1)

    index = _text(INDEX)
    assert "config.py" in index, (
        "the index must point at config.py as the source of truth for VERSION")

    # Only a *declaration* counts — `**Version:** 3.0.0` on its own line.
    # Prose that merely mentions numbers found in other documents is fine, and
    # is in fact how the index records the known drift.
    declarations = re.findall(r"^\**Version:?\**[:\s]+v?(\d+\.\d+\.\d+)",
                              index, re.MULTILINE | re.I)
    for claim in declarations:
        assert claim == version, (
            f"the index declares version {claim} but config.py says {version}")


# ---------------------------------------------------------------------------
# the vector backend
# ---------------------------------------------------------------------------

FAISS_HEAVY = 8  # more than this many mentions = the doc is *about* FAISS


def test_faiss_heavy_documents_carry_a_pointer_to_the_current_contract():
    """The live vector backend is pgvector. ~46 documents still describe FAISS
    as the active index. They are kept, but each must say so at the top so a
    reader does not act on them."""
    missing = []
    for path in _markdown_files():
        # The contract document cannot be asked to point at itself. It discusses
        # FAISS precisely because it is the document that settles the question —
        # "why pgvector and not FAISS" belongs there and nowhere else.
        if path.name == "70_VECTOR_INDEX_CONTRACT.md":
            continue
        text = _text(path)
        if text.count("FAISS") <= FAISS_HEAVY:
            continue
        head = text[:2500]  # wide enough to cover a title + intro + the banner
        if ("70_VECTOR_INDEX_CONTRACT" in head
                or "SUPERSEDED" in head
                or "pgvector" in head):
            continue
        missing.append(f"{path.name} ({text.count('FAISS')} mentions)")
    assert not missing, (
        "these documents present FAISS as the live index with no note that the "
        "system runs pgvector — add the vector-backend banner pointing at "
        "70_VECTOR_INDEX_CONTRACT.md:\n  " + "\n  ".join(missing))


def test_the_vector_index_contract_document_exists_and_names_postgres_as_authoritative():
    contract = DOCS / "70_VECTOR_INDEX_CONTRACT.md"
    assert contract.is_file()
    text = _text(contract).lower()
    assert "postgres" in text and "authoritative" in text


# ---------------------------------------------------------------------------
# the API docs claims in the README
# ---------------------------------------------------------------------------

def test_the_readme_does_not_promise_api_docs_in_production():
    """/docs is gated off in production because it publishes every admin
    route. The README must not claim otherwise."""
    text = _text(README)
    assert "/docs" in text, "the README no longer documents the API docs URLs"
    lowered = text.lower()
    assert "disabled in production" in lowered or "not production" in lowered, (
        "the README documents /docs without stating that it is disabled in "
        "production — that is how an operator ends up exposing it")


@pytest.mark.parametrize("path", ["Docs/61_DEPLOYMENT_RUNBOOK.md",
                                  "Docs/60_BACKUP_AND_RESTORE.md",
                                  "Docs/72_ADMIN_CHEAT_SHEET.md",
                                  "Docs/73_TROUBLESHOOTING.md",
                                  "Docs/74_SECURITY_CHECKLIST.md"])
def test_the_operational_documents_exist(path):
    """These five are what a solo administrator actually needs. The README and
    the index both link to all of them."""
    assert (REPO / path).is_file(), f"{path} is linked but missing"


def test_the_generated_api_reference_matches_the_live_spec():
    """75_API_REFERENCE.md is generated from /openapi.json. If a route is added
    or removed without regenerating, the reference silently lies — which is
    worse than not having one, because people trust a reference."""
    import json
    import urllib.request

    reference = DOCS / "75_API_REFERENCE.md"
    assert reference.is_file(), (
        "75_API_REFERENCE.md is missing — regenerate it with "
        "`python /app/scripts/generate_api_reference.py`")
    text = _text(reference)

    with urllib.request.urlopen("http://localhost:8000/openapi.json", timeout=120) as response:
        spec = json.loads(response.read().decode())

    verbs = ("get", "post", "put", "patch", "delete")
    live = {path for path, methods in spec["paths"].items()
            if any(v in verbs for v in methods)}
    documented = set(re.findall(r"\| `(/[^`]*)` \|", text))

    missing = live - documented
    stale = documented - live
    assert not missing and not stale, (
        "75_API_REFERENCE.md is out of date — regenerate it with "
        "`python /app/scripts/generate_api_reference.py`.\n"
        f"  routes missing from the doc: {sorted(missing)[:10]}\n"
        f"  routes in the doc that no longer exist: {sorted(stale)[:10]}")


def test_the_api_reference_generator_is_committed():
    """The doc is only trustworthy if the thing that makes it ships with it."""
    generator = REPO / "scripts" / "generate_api_reference.py"
    assert generator.is_file(), (
        "75_API_REFERENCE.md claims to be generated by "
        "scripts/generate_api_reference.py, which does not exist")


def test_no_document_tells_the_reader_to_add_a_package_to_a_hardware_file():
    """Shared dependencies go in requirements-base.txt.

    Three guides said "Or add to `requirements-cpu.txt`" for folium and
    offline-folium. That file is included only by the CPU build, so following
    the advice installs the package on CPU and leaves every GPU deployment
    without it — the exact asymmetry that left sentence-transformers missing
    from GPU and silently disabled semantic query-history search there.

    Only the ONNX Runtime build, the FAISS build, torch and
    sentence-transformers legitimately live in a hardware file.
    """
    offenders = []
    for path in _markdown_files():
        for number, line in enumerate(_text(path).splitlines(), 1):
            if re.search(r"add\b.{0,40}requirements-(cpu|gpu)\.txt", line, re.I):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, (
        "documentation directs the reader to add packages to a hardware-specific "
        "requirements file instead of requirements-base.txt: " + "; ".join(offenders))


def test_no_document_claims_a_root_requirements_txt():
    """`pip install -r requirements.txt` appeared in the quick start and in a
    README Dockerfile example. No such file has ever existed in this
    repository — Dockerfile.gpu merely COPYied requirements-gpu.txt *as*
    requirements.txt, which made the claim look true inside the container."""
    assert not (REPO / "requirements.txt").exists(), (
        "a root requirements.txt now exists; this test and the comments in "
        "requirements-base.txt need revisiting")
    offenders = []
    for path in [*_markdown_files(), README]:
        for number, line in enumerate(_text(path).splitlines(), 1):
            if re.search(r"-r\s+(\./)?requirements\.txt", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, (
        "documentation installs from requirements.txt, which does not exist: "
        + "; ".join(offenders))


def test_the_admin_cheat_sheet_warns_about_down_v():
    """`docker compose down -v` deletes the database, the stored faces and the
    logs. Every document that shows `down` must say so."""
    for name in ("72_ADMIN_CHEAT_SHEET.md",):
        text = _text(DOCS / name)
        assert "down -v" in text
        window = text[text.index("down -v"):text.index("down -v") + 700]
        assert re.search(r"delete|destroy|data loss|no undo", window, re.I), (
            f"{name} shows `down -v` without an adjacent data-loss warning")


def test_no_document_recommends_down_v_as_routine_administration():
    """It is only ever a deliberate destructive act."""
    offenders = []
    for path in _markdown_files() + [README]:
        for number, line in enumerate(_text(path).splitlines(), 1):
            if not re.search(r"docker[- ]compose[^\n]*\bdown\b[^\n]*\s-v\b", line):
                continue
            if line.lstrip().startswith((">", "#", "*", "-")) or "**" in line:
                continue  # prose warning, not a prescribed command
            offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, (
        "`down -v` appears as a command to run; it destroys all data and must "
        "only appear inside a warning:\n  " + "\n  ".join(offenders))
