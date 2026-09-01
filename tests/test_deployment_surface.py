"""The deployment surface must agree with config.py.

    docker exec face_recognition_api python -m pytest tests/test_deployment_surface.py -v

Compose files, nginx and gunicorn all encode configuration decisions. None of
them imports `config.py`, so nothing stops them drifting from it — and every
drift here fails at deploy time or, worse, at 3am under load:

  * a `${VAR:?}` that .env.example never mentions stops `docker compose up`
    with an error naming a variable the operator has never seen;
  * an nginx body limit below the application's accepts the request and then
    truncates it at the proxy, so the client sees 413 and the application log
    shows nothing at all;
  * gunicorn falling back to os.getenv defaults when `config.py` fails to
    import produces a running, misconfigured service instead of a failure.

These are text/AST assertions on purpose: the failures they catch are silent at
runtime.
"""

import ast
import os
import re

import pytest

REPO = "/app"
COMPOSE_FILES = ["docker/docker-compose.cpu.yml",
                 "docker/docker-compose.gpu.yml",
                 "docker/docker-compose.prod.yml"]
NGINX_FILES = ["nginx.conf", "nginx.prod.conf"]

INTERPOLATION = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(:[-?][^}]*)?\}")
ENV_ASSIGNMENT = re.compile(r"^\s{6,}([A-Z][A-Z0-9_]*):\s*(.*?)\s*$")


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as handle:
        return handle.read()


def _documented_names():
    """Names either template mentions, commented-out ones included — a
    commented line still tells the operator the variable exists.

    BOTH templates, because there are two and they have different jobs:

      /.env.example         application settings, read by pydantic INSIDE the
                            container
      /docker/.env.example  deployment credentials, used ONLY for compose
                            ${VAR} interpolation on the host

    docker/.env.example is the one that matters for the assertions below.
    Compose reads `.env` from the PROJECT DIRECTORY — the directory of the
    first `-f` file, i.e. docker/ — so a root .env is never consulted for
    interpolation. Documenting a required ${VAR:?} only in the root template
    produced a deployment that failed with "POSTGRES_SUPERUSER_PASSWORD is
    required" while the operator stared at a file that plainly contained it.
    """
    pattern = r"^#?\s*([A-Z][A-Z0-9_]*)="
    return (set(re.findall(pattern, _read(".env.example"), re.M))
            | set(re.findall(pattern, _read("docker", ".env.example"), re.M)))


def test_the_compose_template_documents_every_required_variable():
    """Stricter than the combined check below: a ${VAR:?} must be in the
    template compose ACTUALLY reads, not merely somewhere in the repository."""
    compose_documented = set(re.findall(
        r"^#?\s*([A-Z][A-Z0-9_]*)=", _read("docker", ".env.example"), re.M))
    missing = {}
    for path in COMPOSE_FILES:
        for match in INTERPOLATION.finditer(_read(path)):
            var, modifier = match.group(1), match.group(2) or ""
            if modifier.startswith(":?") and var not in compose_documented:
                missing.setdefault(var, []).append(os.path.basename(path))
    assert not missing, (
        "compose requires these but docker/.env.example never names them, so "
        "an operator copying the template still cannot start the stack: "
        + "; ".join(f"{k} ({', '.join(v)})" for k, v in sorted(missing.items())))


def _compose_assignments(path):
    values = {}
    for line in _read(path).splitlines():
        match = ENV_ASSIGNMENT.match(line)
        if match and not match.group(2).startswith("#"):
            values[match.group(1)] = match.group(2).strip('"')
    return values


# ---------------------------------------------------------------------------
# .env.example is the operator's contract
# ---------------------------------------------------------------------------

def test_every_required_variable_is_in_the_template():
    """`${VAR:?}` means compose REFUSES to start without it.

    Four were missing when this test was written — FR_APP_PASSWORD,
    POSTGRES_SUPERUSER_PASSWORD, PUBLIC_ORIGIN, GRAFANA_ADMIN_PASSWORD — so
    copying .env.example to .env and running the production stack failed on a
    variable the template never named.
    """
    documented = _documented_names()
    missing = {}
    for path in COMPOSE_FILES:
        for match in INTERPOLATION.finditer(_read(path)):
            var, modifier = match.group(1), match.group(2) or ""
            if modifier.startswith(":?") and var not in documented:
                missing.setdefault(var, []).append(os.path.basename(path))

    assert not missing, (
        "compose requires these but .env.example never mentions them: "
        + "; ".join(f"{k} ({', '.join(v)})" for k, v in sorted(missing.items())))


def test_the_template_holds_no_real_secrets():
    """Every credential line must be empty or an obvious placeholder.

    Classification comes from `redaction.SECRET_SETTINGS` — the same list the
    log filter and the settings API use — plus a name-suffix rule for the
    compose-only credentials (FR_*_PASSWORD and friends) that never enter the
    application process and so are not in that list. Matching on the SUFFIX,
    not a substring: ACCESS_TOKEN_EXPIRE_MINUTES contains "TOKEN" and is a
    duration.
    """
    from backend.security.redaction import SECRET_SETTINGS

    suffixes = ("_PASSWORD", "_SECRET", "_KEY", "_KEYS", "_SECRET_KEY")
    offenders = []
    for line in _read(".env.example").splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name not in SECRET_SETTINGS and not name.endswith(suffixes):
            continue
        value = value.strip()
        if value and "CHANGE_ME" not in value:
            offenders.append(name)
    assert not offenders, f"real-looking values in .env.example: {offenders}"


# ---------------------------------------------------------------------------
# nginx must not disagree with the application about body size
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", NGINX_FILES)
def test_the_webhook_body_limit_matches_the_setting(path):
    """A tighter proxy limit rejects a frame the handler would have accepted;
    a looser one lets nginx buffer a body the handler will refuse anyway."""
    from config import settings

    text = _read(path)
    location = text.split("location ~ ^/(api/)?webhook/", 1)[1].split("}", 1)[0]
    match = re.search(r"client_max_body_size\s+(\d+)m", location, re.I)
    assert match, f"{path}: the webhook location no longer bounds its body size"
    assert int(match.group(1)) == settings.WEBHOOK_MAX_BODY_MB, (
        f"{path}: nginx allows {match.group(1)}MB on the ingest endpoint while "
        f"WEBHOOK_MAX_BODY_MB is {settings.WEBHOOK_MAX_BODY_MB}")


@pytest.mark.parametrize("path", NGINX_FILES)
def test_the_global_body_limit_covers_the_largest_legitimate_upload(path):
    """Batch search is the only request that needs the big limit.

    Below the batch worst case, a full batch is truncated at the proxy and the
    application never sees it — the client gets a 413 with no server-side log.
    """
    from config import settings

    text = _read(path)
    match = re.search(r"^\s*client_max_body_size\s+(\d+)M;", text, re.M)
    assert match, f"{path}: no server-level client_max_body_size"

    allowed_mb = int(match.group(1))
    needed_mb = (settings.BATCH_SEARCH_MAX_IMAGES * settings.MAX_FILE_SIZE) // (1024 * 1024)
    assert allowed_mb >= needed_mb, (
        f"{path}: nginx caps bodies at {allowed_mb}MB but a full batch search "
        f"is {needed_mb}MB ({settings.BATCH_SEARCH_MAX_IMAGES} images x "
        f"{settings.MAX_FILE_SIZE // (1024*1024)}MB)")


# ---------------------------------------------------------------------------
# gunicorn is a production entry point, not a best-effort script
# ---------------------------------------------------------------------------

def test_gunicorn_has_no_environment_fallback():
    """It used to catch ImportError around `from config import settings` and
    continue on os.getenv defaults — so a broken config.py produced a running
    service bound to 0.0.0.0:8000 instead of a startup failure."""
    tree = ast.parse(_read("gunicorn.conf.py"))

    reads = [node for node in ast.walk(tree)
             if isinstance(node, ast.Attribute) and node.attr in ("getenv", "environ")]
    assert not reads, (
        "gunicorn.conf.py reads the environment directly; central "
        "configuration must be the only source")

    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            imports = [n for n in ast.walk(node)
                       if isinstance(n, ast.ImportFrom) and n.module == "config"]
            assert not imports, (
                "the settings import is wrapped in try/except again; a failed "
                "import must abort startup, not fall through to defaults")


def test_gunicorn_takes_the_worker_count_literally():
    """WORKERS=1 is load-bearing: admin-settings propagation, SQL-agent
    cancellation and single-flight job guards are all process-local. The old
    `if WORKERS > 0 else <auto-derive>` would silently start 8-26 processes."""
    source = _read("gunicorn.conf.py")
    assert "workers = settings.WORKERS\n" in source
    assert "cpu_count * 1.5" not in source and "cpu_count * 2" not in source, (
        "the auto-derived worker count is back")


def test_no_branch_pretends_to_distinguish_cases_it_does_not():
    """`timeout = 600 if USE_GPU else 600` reads as a deliberate GPU/CPU
    distinction and is not one."""
    tree = ast.parse(_read("gunicorn.conf.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.IfExp):
            body, orelse = ast.dump(node.body), ast.dump(node.orelse)
            assert body != orelse, (
                f"line {node.lineno}: both branches of the ternary are identical")


# ---------------------------------------------------------------------------
# Compose must not set application settings that do not exist
# ---------------------------------------------------------------------------

def test_compose_sets_no_unknown_application_setting():
    """A typo'd name in compose is silently ignored — `extra="ignore"`.

    Variables belonging to another image or to a shell script are listed
    explicitly, so the exemption stays reviewable rather than becoming a
    catch-all.
    """
    from config import Settings

    not_application_settings = {
        # postgres image server tuning + initdb
        "POSTGRES_INITDB_ARGS", "POSTGRES_MAX_CONNECTIONS",
        "POSTGRES_SHARED_BUFFERS", "POSTGRES_EFFECTIVE_CACHE_SIZE",
        "POSTGRES_WORK_MEM",
        # grafana image
        "GF_SECURITY_ADMIN_USER", "GF_SECURITY_ADMIN_PASSWORD",
        "GF_USERS_ALLOW_SIGN_UP",
        "GF_AUTH_ANONYMOUS_ENABLED", "GF_SERVER_ROOT_URL",
        # nvidia container runtime
        "NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES",
        # scripts/backup.sh (libpq), redis-cli, python runtime, build arg
        "PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE",
        "REDISCLI_AUTH", "PYTHONUNBUFFERED", "INSTALL_DEV",
        # compose interpolation only; the application never reads it
        "PUBLIC_ORIGIN",
        # huggingface_hub / sentence-transformers read this directly from the
        # environment. Pinned in compose so the model cache location is
        # deployment configuration rather than a library default that varies
        # with $HOME — the image creates and owns exactly this path.
        "HF_HOME",
        # Same class as HF_HOME: read straight from the environment by
        # huggingface_hub. Progress bars write carriage-return animations to
        # stdout, which is the log stream here — one model load turns into
        # thousands of unreadable partial lines in the container log.
        "HF_HUB_DISABLE_PROGRESS_BARS",
    }

    declared = set(Settings.model_fields)
    unknown = {}
    for path in COMPOSE_FILES:
        for name in _compose_assignments(path):
            if name in declared or name in not_application_settings:
                continue
            unknown.setdefault(name, []).append(os.path.basename(path))

    assert not unknown, (
        "compose sets names that are neither declared settings nor known "
        f"foreign variables (typo? removed setting?): {sorted(unknown)}")


def test_production_does_not_silently_inherit_a_value_the_others_override():
    """If cpu and gpu both override a default and agree, production saying
    nothing means production is the odd one out by omission.

    That is how prod ended up building its pgvector index with HNSW M=16 while
    every measurement was taken at M=32.
    """
    from config import Settings

    stacks = {os.path.basename(p).split(".")[1]: _compose_assignments(p)
              for p in COMPOSE_FILES}
    defaults = {name: field.default
                for name, field in Settings.model_fields.items()}

    def same(a, b):
        return a is not None and b is not None and str(a).lower() == str(b).lower()

    odd = []
    for name in set(stacks["cpu"]) & set(stacks["gpu"]):
        if name in stacks["prod"] or name not in defaults:
            continue
        agreed = stacks["cpu"][name]
        if same(agreed, stacks["gpu"][name]) and not same(agreed, defaults[name]):
            odd.append(f"{name} (cpu=gpu={agreed!r}, prod inherits "
                       f"{defaults[name]!r})")

    assert not odd, (
        "production inherits a default that both other stacks deliberately "
        "override: " + "; ".join(sorted(odd)))


def test_the_entrypoint_derives_directories_from_the_storage_root():
    """The shell cannot import config.py, so the layout is spelled out twice.

    It used to create `${FACES_DIR:-/app/storage/faces}`. Once FACES_DIR stopped
    being a setting that expansion ALWAYS produced the literal fallback, so a
    deployment with a non-default STORAGE_DIR had its permissions fixed on a
    directory the application never writes to — and enrollment failed on the
    one it does.
    """
    from config import settings

    # Comment lines stripped: the script explains the old bug in its own
    # comments, and a raw text scan cannot tell an explanation from a command.
    script = "\n".join(line for line in _read("docker-entrypoint.sh").splitlines()
                       if not line.lstrip().startswith("#"))

    assert "${FACES_DIR" not in script, (
        "the entrypoint reads FACES_DIR, which is derived and never set")
    assert 'STORAGE_ROOT="${STORAGE_DIR:-/app/storage}"' in script, (
        "the entrypoint no longer derives from STORAGE_DIR")

    # Each derived directory the application writes to must be created, spelled
    # relative to the root rather than hard-coded.
    for suffix in ("faces", "faces/.incoming", "pending",
                   "debug/webhook_images", "debug/cropped"):
        assert f'mkdir -p "$STORAGE_ROOT/{suffix}"' in script, (
            f"the entrypoint does not create $STORAGE_ROOT/{suffix}")

    # And the shell's idea of the layout must match config.py's.
    root = settings.STORAGE_DIR
    for derived, suffix in ((settings.FACES_DIR, "faces"),
                            (settings.UPLOAD_TEMP_DIR, "faces/.incoming"),
                            (settings.PENDING_UPLOAD_DIR, "pending"),
                            (settings.WEBHOOK_IMAGES_DIR, "debug/webhook_images"),
                            (settings.CROPPED_IMAGES_DIR, "debug/cropped")):
        assert derived == os.path.join(root, *suffix.split("/")), (
            f"config.py resolves {derived} but the entrypoint creates "
            f"{root}/{suffix}")


def test_no_derived_path_is_set_by_any_deployment_file():
    """Derived paths cannot be configured; a value here is inert and the guard
    aborts startup on a divergent one."""
    from backend.security.config_guard import DERIVED_PATHS

    offenders = []
    for path in COMPOSE_FILES + [".env.example"]:
        assignments = _compose_assignments(path) if path.endswith(".yml") else {
            m.group(1): m.group(2)
            for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=(.*)$", _read(path), re.M)}
        for name in assignments:
            if name in DERIVED_PATHS:
                offenders.append(f"{os.path.basename(path)}:{name}")
    assert not offenders, f"derived paths set in deployment files: {offenders}"
