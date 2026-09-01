"""
Deployment-configuration checks that unit tests structurally cannot catch.

    docker exec face_recognition_api python -m pytest tests/test_compose_and_deployment.py -v

A published database port, a missing certificate mount, an overridden
entrypoint or a wildcard CORS value is not a code defect — every unit test can
pass while the deployment is wide open. These assert on the compose, nginx and
Dockerfile sources directly.

Parsed as text rather than YAML so the checks run without PyYAML and so
comments (which carry the reasoning) are visible to the assertions.
"""

import os
import re

import pytest

REPO = "/app"
PROD_COMPOSE = f"{REPO}/docker/docker-compose.prod.yml"
DEV_COMPOSE = f"{REPO}/docker/docker-compose.cpu.yml"
# Hardware overrides. Neither is a stack: each is layered on a base with a
# second -f. They add GPU reservations and cannot weaken the base's
# security posture, so the posture assertions below target the BASES.
DEV_GPU_OVERRIDE = f"{REPO}/docker/docker-compose.gpu.yml"
PROD_GPU_OVERRIDE = f"{REPO}/docker/docker-compose.prod.gpu.yml"
NGINX_DEV = f"{REPO}/nginx.conf"
NGINX_PROD = f"{REPO}/nginx.prod.conf"


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def published_ports(source):
    """Host:container mappings under a `ports:` key, ignoring `expose:`."""
    found = []
    in_ports = False
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("ports:"):
            in_ports = True
            continue
        if in_ports:
            match = re.match(r'-\s*"?([\d.:]+):(\d+)"?', stripped)
            if match:
                found.append(match.group(0))
                continue
            if stripped and not stripped.startswith("#"):
                in_ports = False
    return found


# ------------------------------------------------- datastores are not exposed

@pytest.mark.parametrize("compose", [PROD_COMPOSE])
@pytest.mark.parametrize("port", ["5432", "6379", "11434"])
def test_datastores_publish_no_host_port(compose, port):
    """Postgres, Redis and Ollama enforce none of the application's
    authorization rules, so a reachable port bypasses all of them."""
    for mapping in published_ports(read(compose)):
        assert not mapping.endswith(f":{port}") and f":{port}:" not in mapping, \
            f"{compose} publishes port {port}: {mapping}"


@pytest.mark.parametrize("compose", [PROD_COMPOSE])
def test_only_web_ports_are_published(compose):
    for mapping in published_ports(read(compose)):
        assert re.search(r":(80|443|3000)\b", mapping), \
            f"{compose} publishes an unexpected port: {mapping}"


def test_grafana_is_bound_to_loopback_only():
    assert '"127.0.0.1:3000:3000"' in read(PROD_COMPOSE)


# --------------------------------------------------------------- secrets

@pytest.mark.parametrize("compose", [PROD_COMPOSE])
def test_no_default_passwords_remain(compose):
    source = read(compose)
    assert "POSTGRES_PASSWORD: admin" not in source
    assert ":admin@postgres" not in source
    assert "your-secret-key-change-in-production" not in source


@pytest.mark.parametrize("compose", [PROD_COMPOSE])
def test_secrets_come_from_files_not_environment_literals(compose):
    source = read(compose)
    assert "JWT_SECRET_KEY_FILE: /run/secrets/jwt_secret" in source
    assert "secrets:" in source


@pytest.mark.parametrize("compose", [PROD_COMPOSE])
def test_required_secrets_fail_the_config_when_missing(compose):
    """`${VAR:?message}` makes `docker compose config` fail rather than
    silently interpolating an empty password."""
    source = read(compose)
    assert ":?" in source, "required variables must use the ${VAR:?} form"


def directives(path):
    """Non-comment, non-blank lines. Comments explain the reasoning and often
    name the very anti-pattern being asserted against."""
    return [
        line.strip()
        for line in read(path).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_redis_password_is_not_a_command_line_argument():
    """A --requirepass value is visible in `docker inspect` and in the host
    process list; an ACL file is not."""
    for compose in (PROD_COMPOSE,):
        assert not any("--requirepass" in line for line in directives(compose))
        assert "aclfile" in read(compose)


# ------------------------------------------------------------- TLS posture

@pytest.mark.parametrize("compose", [PROD_COMPOSE])
def test_certificates_are_mounted_read_only(compose):
    assert "/etc/nginx/certs:ro" in read(compose)


def test_production_nginx_terminates_tls():
    source = read(NGINX_PROD)
    assert "listen 443 ssl" in source
    assert "ssl_certificate" in source
    assert "return 308 https://" in source, "HTTP must redirect to HTTPS"


def test_production_nginx_offers_no_obsolete_tls():
    source = read(NGINX_PROD)
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in source
    assert "TLSv1.1" not in source
    assert "SSLv3" not in source


def test_hsts_is_not_enabled_before_clients_trust_the_ca():
    """HSTS makes browsers refuse the HTTP fallback with no override, so it
    must stay off until the internal CA is distributed."""
    for line in read(NGINX_PROD).splitlines():
        if "Strict-Transport-Security" in line:
            assert line.strip().startswith("#"), \
                "HSTS must remain commented out until CA trust is established"


def test_production_sets_secure_cookies():
    assert 'AUTH_COOKIE_SECURE: "true"' in read(PROD_COMPOSE)


# --------------------------------------------------- immutability and workers

def test_production_has_no_whole_repo_bind_mount():
    """`../:/app` shadows the image with the working tree and injects .env."""
    assert "- ../:/app" not in read(PROD_COMPOSE)


def test_development_still_has_its_bind_mount():
    """Hot reload is the point of the dev stack; this guards against the
    production hardening being applied to it by accident."""
    assert "- ../:/app" in read(DEV_COMPOSE)


@pytest.mark.parametrize("compose", [PROD_COMPOSE, DEV_COMPOSE])
def test_single_worker(compose):
    """Runtime settings, the SQL-agent cancel registry, the single-flight job
    guards, webhook dedup and FAISS autosave are all process-local."""
    assert "WORKERS: 1" in read(compose)


@pytest.mark.parametrize("compose", [PROD_COMPOSE])
def test_entrypoint_is_not_overridden(compose):
    """The Dockerfile entrypoint runs the config preflight, fixes permissions
    and drops privileges via gosu."""
    assert 'entrypoint: ["/bin/sh", "-c"]' not in read(compose)


# ----------------------------------------------------------- migration job

@pytest.mark.parametrize("compose", [PROD_COMPOSE])
def test_migrations_run_in_a_dedicated_job(compose):
    source = read(compose)
    assert "migrate:" in source
    assert "--upgrade-head" in source


@pytest.mark.parametrize("compose", [PROD_COMPOSE])
def test_api_waits_for_migrations_to_finish(compose):
    """Without this gate, N replicas race to apply the same revision."""
    assert "service_completed_successfully" in read(compose)


@pytest.mark.parametrize("compose", [PROD_COMPOSE])
def test_api_workers_only_verify_the_schema(compose):
    assert "MIGRATIONS_MODE: verify" in read(compose)
    # the permissive flag was removed: no compose file may set it
    assert "MIGRATIONS_FAIL_CLOSED" not in read(compose)


# ------------------------------------------------------------ API surface

@pytest.mark.parametrize("compose", [PROD_COMPOSE])
def test_api_documentation_is_disabled(compose):
    assert 'ENABLE_API_DOCS: "false"' in read(compose)


@pytest.mark.parametrize("compose", [PROD_COMPOSE])
def test_cors_is_not_a_wildcard(compose):
    source = read(compose)
    assert "CORS_ORIGINS: '[\"*\"]'" not in source
    assert 'CORS_ORIGINS: "*"' not in source


@pytest.mark.parametrize("path", [NGINX_DEV, NGINX_PROD])
def test_metrics_is_not_publicly_reachable(path):
    source = read(path)
    block = source[source.index("location /metrics"):]
    block = block[:block.index("}")]
    assert "deny all;" in block
    assert "allow 192.168." not in block, "LAN ranges are what this must exclude"
    assert "allow 10.0.0.0/8" not in block


@pytest.mark.parametrize("path", [NGINX_DEV, NGINX_PROD])
def test_forwarded_for_is_not_trusted_from_arbitrary_clients(path):
    """nginx is the edge proxy; trusting a client-supplied X-Forwarded-For lets
    it choose the IP used for rate limiting and IP allowlists."""
    for line in read(path).splitlines():
        stripped = line.strip()
        if stripped.startswith("set_real_ip_from") or stripped.startswith("real_ip_header"):
            pytest.fail(f"{path} trusts client-supplied forwarding headers: {stripped}")


@pytest.mark.parametrize("path", [NGINX_DEV, NGINX_PROD])
def test_security_headers_present(path):
    source = read(path)
    for header in (
        "X-Content-Type-Options",
        "Referrer-Policy",
        "Permissions-Policy",
        "Content-Security-Policy",
    ):
        assert header in source, f"{path} missing {header}"


@pytest.mark.parametrize("path", [NGINX_DEV, NGINX_PROD])
def test_csp_does_not_allow_inline_script(path):
    source = read(path)
    csp = next(line for line in source.splitlines() if "Content-Security-Policy" in line)
    script_src = csp.split("script-src")[1].split(";")[0]
    assert "unsafe-inline" not in script_src
    assert "unsafe-eval" not in script_src


def _frontend_files(*patterns):
    """Authored frontend files, excluding third-party vendor bundles."""
    import glob

    found = []
    for pattern in patterns:
        for path in glob.glob(f"{REPO}/frontend/**/{pattern}", recursive=True):
            normalized = path.replace("\\", "/")
            if "/vendor/" in normalized:
                continue
            found.append(normalized)
    return found


def test_frontend_has_no_inline_script_blocks():
    """CSP script-src 'self' only holds if nothing relies on inline script.

    Recursive: the first version of this test globbed frontend/*.html only and
    so never looked in frontend/admin/, where five inline blocks lived.
    """
    offenders = []
    for path in _frontend_files("*.html"):
        source = read(path)
        for match in re.finditer(r"<script([^>]*)>", source):
            if "src=" not in match.group(1):
                offenders.append(path)
                break
    assert not offenders, f"inline <script> blocks are blocked by CSP: {offenders}"


INLINE_HANDLER = re.compile(
    r"""\son(click|submit|change|input|load|error|keydown|keyup|focus|blur"""
    r"""|mouseover|mouseout|dblclick)\s*=\s*["']""",
    re.I,
)


def test_frontend_has_no_inline_event_handlers():
    """Inline handlers are inline script, whether authored in HTML or built
    from a template literal in JavaScript. Both are blocked by CSP."""
    offenders = {}
    for path in _frontend_files("*.html", "*.js"):
        if path.endswith("/actions.js"):
            continue  # its documentation quotes the patterns it replaces
        hits = INLINE_HANDLER.findall(read(path))
        if hits:
            offenders[path] = len(hits)
    assert not offenders, f"inline event handlers are blocked by CSP: {offenders}"


def _registered_action_names(source):
    """Names passed to Actions.register, wherever the call sits.

    Brace-counted rather than regexed on indentation: registration is legal
    both at top level (closing `});` at column 0) and inside a page's IIFE
    (indented) — admin-search.js now does both, because handlers scoped inside
    the IIFE must register from inside it. The old `\\n\\}\\);` anchor only saw
    the first shape and reported every in-IIFE registration as a dead button.
    """
    names = set()
    for m in re.finditer(r"Actions\.register\(\{", source):
        depth, i = 1, m.end()
        while i < len(source) and depth:
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
            i += 1
        block = source[m.end():i]
        names |= set(re.findall(r"^\s*([A-Za-z_$][\w$]*)\s*[,:]", block, re.M))
    return names


def test_every_data_action_has_a_registered_handler():
    """A data-action with no registration is a button that silently does
    nothing — exactly the regression this refactor could introduce."""
    used, registered = set(), set()
    for path in _frontend_files("*.html", "*.js"):
        source = read(path)
        if not path.endswith("/actions.js"):
            used |= set(re.findall(r'data-action(?:-\w+)?="([A-Za-z_$][\w$]*)"', source))
        registered |= _registered_action_names(source)

    # admin-background-tasks.js owns this one with its own delegated listener.
    unhandled = used - registered - {"details"}
    assert not unhandled, f"data-action names with no handler: {sorted(unhandled)}"


def test_actions_dispatcher_is_loaded_before_page_scripts():
    """Both are deferred, so execution follows document order: actions.js must
    appear first or Actions.register would be undefined."""
    for path in _frontend_files("*.html"):
        source = read(path)
        if "actions.js" not in source:
            continue
        scripts = [m.group(1) for m in re.finditer(r'<script[^>]+src="([^"]+)"', source)]
        assert scripts and "actions.js" in scripts[0], (
            f"{path} loads actions.js at position "
            f"{next(i for i, s in enumerate(scripts) if 'actions.js' in s)}, not first"
        )


# ------------------------------------------------------- build hygiene

def test_dockerignore_excludes_secrets_and_runtime_data():
    source = read(f"{REPO}/.dockerignore")
    for pattern in (".env", "logs/", "certs/", ".git", "*.key"):
        assert pattern in source, f".dockerignore missing {pattern}"


def test_dockerignore_keeps_what_the_runtime_needs():
    source = read(f"{REPO}/.dockerignore")
    for required in ("alembic", "docker-entrypoint.sh", "gunicorn.conf.py"):
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            assert stripped != required, f".dockerignore excludes required {required}"


def test_gitignore_covers_secrets():
    source = read(f"{REPO}/.gitignore")
    for pattern in (".env", "certs/", "*.key", "secrets/"):
        assert pattern in source, f".gitignore missing {pattern}"


def test_env_example_contains_no_real_secrets():
    source = read(f"{REPO}/.env.example")
    assert "your-secret-key-change-in-production" not in source
    assert "POSTGRES_PASSWORD=admin" not in source
    assert "openssl rand" in source, "must explain how to generate secrets"


def test_entrypoint_runs_the_config_preflight():
    source = read(f"{REPO}/docker-entrypoint.sh")
    assert "backend.security.config_guard" in source
    assert source.index("config_guard") < source.index('exec gosu appuser "$@"')


# ---------------------------------------------------------------------------
# Dependency sources and the test runner
#
# Recreating the container off an image built before a dependency was added is
# indistinguishable, at the test level, from a code regression: 18 sql_guard
# tests failed simply because the running image predated sqlglot being pinned.
# ---------------------------------------------------------------------------

DOCKERFILE_CPU = f"{REPO}/docker/Dockerfile.cpu"
REQ_BASE = f"{REPO}/requirements-base.txt"
REQ_CPU = f"{REPO}/requirements-cpu.txt"
REQ_GPU = f"{REPO}/requirements-gpu.txt"
REQ_DEV = f"{REPO}/requirements-dev.txt"
LOCK_CPU = f"{REPO}/requirements-cpu.lock.txt"


def resolved_requirements(path):
    """A requirements file WITH its `-r` includes expanded.

    requirements-cpu.txt and requirements-gpu.txt are thin: they `-r
    requirements-base.txt` and add only the packages that genuinely differ by
    hardware. Reading either file literally therefore no longer shows what the
    image installs, and a test that did so would report a missing dependency
    that is in fact present. Resolve the include instead of relaxing the check.
    """
    import os

    text = read(path)
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("-r "):
            included = os.path.join(os.path.dirname(path), stripped[3:].strip())
            out.append(resolved_requirements(included))
        else:
            out.append(line)
    return "\n".join(out)


def test_sql_parser_is_pinned_in_both_runtime_dependency_sources():
    """sql_guard imports sqlglot; without it every guard test fails closed."""
    for path in (REQ_CPU, REQ_GPU):
        assert re.search(r"^sqlglot==", resolved_requirements(path), re.M), (
            f"{path} does not pin sqlglot, which backend SQL validation imports")


def test_every_runtime_import_of_sqlglot_is_satisfied():
    """Behavioural counterpart: the parser is actually importable here."""
    import sqlglot  # noqa: F401


def test_the_dev_image_ships_its_own_test_runner():
    dockerfile = read(DOCKERFILE_CPU)
    assert "ARG INSTALL_DEV=false" in dockerfile, (
        "no INSTALL_DEV build arg — recreating the container drops pytest")
    assert "requirements-dev.txt" in dockerfile, (
        "the Dockerfile never installs the development requirements")
    assert re.search(r"pytest==", read(REQ_DEV)), (
        "requirements-dev.txt does not pin pytest")


def test_only_non_production_images_install_the_test_runner():
    """A production image must not ship a test runner."""
    assert 'INSTALL_DEV: "true"' in read(DEV_COMPOSE), (
        "the development stack does not enable INSTALL_DEV, so the suite cannot "
        "run after a container recreate without a manual pip install")
    for compose in (PROD_COMPOSE,):
        assert "INSTALL_DEV" not in read(compose), (
            f"{compose} enables development dependencies in a production image")


def test_no_stale_lock_artifact():
    """A lock file must be wired into the build or absent.

    A lock that nothing installs is worse than none: it looks authoritative,
    drifts silently, and sends anyone reading it to reproduce an environment
    that was never built.
    """
    import os

    if not os.path.exists(LOCK_CPU):
        return          # deleted, which is a valid resolution

    dockerfile = read(DOCKERFILE_CPU)
    assert "requirements-cpu.lock.txt" in dockerfile, (
        "requirements-cpu.lock.txt exists but no build installs it — either "
        "wire it into Dockerfile.cpu or delete it")

    lock = read(LOCK_CPU)
    locked = {m.group(1).lower().replace("_", "-")
              for m in re.finditer(r"^([A-Za-z0-9_.-]+)==", lock, re.M)}

    # Everything the source list pins explicitly must be in the lock. The lock
    # silently lost sqlglot this way: it was added to requirements-cpu.txt and
    # the lock was never regenerated, so a container rebuilt from the image had
    # no SQL parser and 18 guard tests failed for a reason unrelated to code.
    for match in re.finditer(r"^([A-Za-z0-9_.-]+)==", read(REQ_CPU), re.M):
        name = match.group(1).lower().replace("_", "-")
        assert name in locked, (
            f"requirements-cpu.txt pins {name} but the lock does not contain it "
            "— regenerate the lock from a clean build")

    # And the lock must not drag development dependencies into production.
    # The previous lock was frozen from a container with pytest pip-installed by
    # hand, so wiring it would have shipped a test runner in the production
    # image — the opposite of what INSTALL_DEV exists to control.
    dev_only = {
        m.group(1).lower().replace("_", "-")
        for m in re.finditer(r"^([A-Za-z0-9_.-]+)==", read(REQ_DEV), re.M)
    } - {"requests"}          # requests is also a genuine runtime dependency
    leaked = sorted(dev_only & locked)
    assert not leaked, (
        f"the lock carries development-only packages {leaked}; regenerate it "
        "from a build with INSTALL_DEV unset")


# ---------------------------------------------------------------------------
# Ingest credentials in deployment configuration
# ---------------------------------------------------------------------------

def test_no_production_compose_carries_an_inline_ingest_key():
    """Production takes the key from a mounted secret, never from the file."""
    for compose in (PROD_COMPOSE,):
        source = read(compose)
        # WEBHOOK_AUTH_TOKEN is the bearer alias. It folds into WEBHOOK_API_KEYS
        # at startup, so pasting it inline is exactly as bad as pasting the key
        # inline — and it would sail past a regex that only knew the old name.
        inline = re.search(r"^\s*WEBHOOK_(API_KEYS|AUTH_TOKEN):\s*\S", source, re.M)
        assert not inline, (
            f"{compose} sets {inline.group(0).strip() if inline else ''} inline; "
            "the ingest credential belongs in a mounted secret")
        assert "WEBHOOK_API_KEYS_FILE:" in source, (
            f"{compose} does not point at a webhook key secret file")


def test_production_mounts_the_ingest_key_secret(compose=PROD_COMPOSE):
    source = read(compose)
    assert "webhook_api_keys:" in source, (
        "the webhook_api_keys secret is not declared")
    assert "- webhook_api_keys" in source, (
        "the webhook_api_keys secret is declared but not mounted into the service")


def test_the_dev_gpu_override_is_development_and_declares_no_stack():
    """docker-compose.gpu.yml was a THIRD production stack — ENVIRONMENT=production
    and file secrets — while every document called it the development GPU stack.
    It was also unrunnable (it required POSTGRES_SUPERUSER_PASSWORD and
    secrets/jwt_secret), so there was no working GPU development path at all.

    It is now a small override layered on cpu.yml. If it ever regains a
    production posture or its own datastores, it has become a fourth stack
    again and this fails."""
    source = read(DEV_GPU_OVERRIDE)
    assert "ENVIRONMENT: production" not in source, (
        "the dev GPU override sets ENVIRONMENT=production — it is a "
        "development override layered on docker-compose.cpu.yml")
    assert "/run/secrets/" not in source, (
        "the dev GPU override mounts production secrets")
    for service in ("postgres:", "redis:", "nginx:", "migrate:"):
        assert f"\n  {service}" not in source, (
            f"the dev GPU override declares its own {service} — it must only "
            f"override face_recognition and ollama, everything else is inherited")


def test_neither_gpu_override_declares_a_project_name():
    """Compose merges the top-level `name` last-wins. An override that declared
    its own name would fork the project — and therefore the volumes — the
    moment someone ran the GPU pair."""
    for override in (DEV_GPU_OVERRIDE, PROD_GPU_OVERRIDE):
        assert not re.search(r"^name:", read(override), re.M), (
            f"{override} declares a project name; it must inherit the base's")


def test_the_two_stacks_use_disjoint_project_names():
    """Without an explicit `name:` both stacks defaulted to the parent
    directory, "docker". Both declare volumes called postgres_data,
    redis_data, face_database_data and chromadb_cache, so both resolved to
    docker_postgres_data &c. — STARTING PRODUCTION MOUNTED THE DEVELOPMENT
    DATABASE. The names are the fix; this test is why they cannot be removed."""
    dev = re.search(r"^name:\s*(\S+)", read(DEV_COMPOSE), re.M)
    prod = re.search(r"^name:\s*(\S+)", read(PROD_COMPOSE), re.M)
    assert dev, "docker-compose.cpu.yml declares no project name"
    assert prod, "docker-compose.prod.yml declares no project name"
    assert dev.group(1) != prod.group(1), (
        f"dev and production share the project name {dev.group(1)!r}, so they "
        f"share every named volume")
    # Plain "face_detector" is not safe: volumes under that prefix already exist
    # on some hosts from an older layout, and adopting them resurrects stale data.
    for match in (dev, prod):
        assert match.group(1) != "face_detector", (
            "the bare 'face_detector' project would adopt pre-existing orphaned "
            "volumes from an earlier layout")


def test_no_compose_file_hardcodes_a_developer_home_directory():
    """cpu.yml mounted "C:/Users/Raven/.ollama" — one developer's home
    directory — which made the repository unusable anywhere else and silently
    produced an Ollama with no models on Linux."""
    for compose in (PROD_COMPOSE, DEV_COMPOSE, DEV_GPU_OVERRIDE, PROD_GPU_OVERRIDE):
        source = read(compose)
        offenders = re.findall(r"^\s*-\s*[\"']?[A-Za-z]:[/\\][^\n]*", source, re.M)
        assert not offenders, (
            f"{compose} mounts an absolute host path from a developer machine: "
            f"{offenders}")


def test_ingest_enforcement_is_on_in_every_stack():
    for compose in (PROD_COMPOSE, DEV_COMPOSE):
        assert "WEBHOOK_AUTH_MODE: enforce" in read(compose), (
            f"{compose} does not enforce ingest authentication")
    for compose in (PROD_COMPOSE,):
        assert "WEBHOOK_AUTH_INSECURE_ACK" not in read(compose), (
            f"{compose} ships the acknowledgement that disables the startup refusal")


def test_the_secret_generator_writes_every_secret_file_compose_requires():
    """A clean deploy must not fail at `docker compose up` on a missing file.

    generate-secrets.sh wrote jwt_secret and bootstrap_admin_password but not
    webhook_api_keys, while both production compose files declare it as a
    required secret FILE — so following the runbook in order on a clean machine
    failed at startup. Generalised over the compose `secrets:` blocks rather
    than hardcoding that one name, so the next secret added to compose without a
    generator line fails here instead of in someone's deployment.
    """
    generator = read(f"{REPO}/scripts/setup/generate-secrets.sh")
    generated = set(re.findall(r"^\s*write_secret\s+([A-Za-z0-9_]+)", generator, re.M))

    missing = {}
    for label, compose_path in (("prod", PROD_COMPOSE),):
        text = read(compose_path)
        # `file: ../secrets/<name>` is what docker resolves at `up` time; a
        # missing file is a hard startup failure, not a warning.
        required = set(re.findall(r"file:\s*\.\./secrets/([A-Za-z0-9_.\-]+)", text))
        required = {n for n in required if not n.endswith(".example")}
        absent = sorted(required - generated)
        if absent:
            missing[label] = absent

    assert not missing, (
        f"compose requires secret files the generator never writes: {missing}. "
        "Add a `write_secret <name>` line to scripts/setup/generate-secrets.sh.")


# ---------------------------------------------------------------------------
# Structural parseability: undeclared references
# ---------------------------------------------------------------------------
# `docker compose config -q` cannot run inside the API container (no docker
# CLI), and no test asserted the equivalent - which is exactly how prod
# shipped with martin on an undeclared network and the documented runbook
# gate (`config -q`, runbook section 5) failing on every attempt. These
# checks are the in-container equivalent for the reference classes that made
# a stack unparseable; deploy.sh --self-test runs the real `config -q` on the
# host as well.


def _top_level_block_keys(source, block):
    """Top-level `networks:`/`volumes:` definition keys of a compose file."""
    lines = source.splitlines()
    keys = []
    inside = False
    for line in lines:
        if re.match(rf"^{block}:\s*$", line):
            inside = True
            continue
        if inside:
            if line.strip() and not line.startswith(" ") and not line.startswith("#"):
                inside = False
                continue
            match = re.match(r"^  ([A-Za-z0-9_-]+):", line)
            if match:
                keys.append(match.group(1))
    return set(keys)


def _service_network_references(source):
    """Every `networks:` list entry under any service, with aliases-style
    mapping keys included (e.g. `webhook_integration:` with an aliases map)."""
    refs = set()
    in_networks = False
    networks_indent = 0
    for line in source.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if stripped == "networks:" and indent >= 4:
            in_networks = True
            networks_indent = indent
            continue
        if in_networks:
            if not stripped or stripped.startswith("#"):
                continue
            if indent <= networks_indent:
                in_networks = False
                continue
            # Only entries at exactly one level in are network names. Deeper
            # lines belong to a per-network mapping (`aliases:` items are NOT
            # networks - counting them reported `face-webhook` as undeclared).
            if indent != networks_indent + 2:
                continue
            match = re.match(r"-\s*([A-Za-z0-9_-]+)\s*$", stripped)
            if match:
                refs.add(match.group(1))
                continue
            match = re.match(r"([A-Za-z0-9_-]+):\s*$", stripped)
            if match:
                refs.add(match.group(1))
    return refs


@pytest.mark.parametrize("label, path", [
    ("prod", PROD_COMPOSE),
    ("dev", DEV_COMPOSE),
    ("regression", f"{REPO}/docker/docker-compose.regression.yml"),
])
def test_every_service_network_is_declared(label, path):
    source = read(path)
    declared = _top_level_block_keys(source, "networks")
    referenced = _service_network_references(source)
    undeclared = sorted(referenced - declared)
    assert not undeclared, (
        f"{label}: services reference networks the file never declares: {undeclared}. "
        "This makes the whole stack unparseable (`docker compose config -q` fails) - "
        "it is how prod shipped with martin on face_recognition_network.")


def test_prod_martin_is_on_edge_without_container_name():
    """Martin must share nginx's network (nginx proxies /maps/ to it and
    depends_on its health) and must not pin a container_name - the only one
    in prod collided with the dev stack's martin and defeated the project
    namespacing this file documents at length."""
    source = read(PROD_COMPOSE)
    martin = source.split("\n  martin:", 1)[1].split("\n  nginx:", 1)[0]
    assert "container_name" not in martin, "prod martin must not pin a container name"
    assert re.search(r"networks:\s*(?:\n\s*#[^\n]*)*\n\s*- edge", martin), (
        "prod martin must join the edge network (nginx's only network)")


def test_prod_api_mounts_map_data_and_hf_cache():
    """MAP_DATA_DIR (/app/map-data) feeds the map verify gate the runbook
    itself prescribes; HF_HOME on a named volume stops the
    sentence-transformers model re-downloading on every recreate (and never
    arriving on an offline host)."""
    source = read(PROD_COMPOSE)
    api = source.split("\n  face_recognition:", 1)[1].split("\n  ollama:", 1)[0]
    assert "../map-data:/app/map-data:ro" in api
    assert "hf_cache_data:/home/appuser/.cache/huggingface" in api
    volumes = _top_level_block_keys(source, "volumes")
    assert "hf_cache_data" in volumes


def test_backup_service_covers_ml_artifacts():
    """The DB registry references ML artifacts by sha256; a backup that
    restores the database but not the files leaves every registered
    model/dataset row dangling."""
    source = read(PROD_COMPOSE)
    backup = source.split("\n  backup:", 1)[1].split("\nvolumes:", 1)[0]
    assert "ml_artifacts_data:/data/ml:ro" in backup
    script = read(f"{REPO}/scripts/backup/backup.sh")
    assert "/data/ml" in script and "ml_artifacts.tar.gz" in script


def _repo_migration_head():
    """The single alembic head, derived exactly the way deploy.sh derives it
    (no alembic import: revisions minus every referenced down_revision)."""
    import glob
    revisions, downs = set(), set()
    for path in glob.glob(f"{REPO}/alembic/versions/*.py"):
        text = read(path)
        rev = re.search(r"^revision(?::\s*[^=]+)?\s*=\s*['\"]([A-Za-z0-9_]+)['\"]", text, re.M)
        if rev:
            revisions.add(rev.group(1))
        for line in text.splitlines():
            if line.startswith("down_revision"):
                downs.update(re.findall(r"['\"]([A-Za-z0-9_]{6,})['\"]", line))
    heads = sorted(revisions - downs)
    assert len(heads) == 1, f"expected exactly one alembic head, got {heads}"
    return heads[0]


def test_prod_migrations_head_default_matches_the_repo_head():
    """A stale MIGRATIONS_EXPECTED_HEAD default makes a fresh deploy of
    current code fail the migrate job (REVISION_MISMATCH, fail-closed). The
    default shipped 14 revisions behind once; deploy.sh derives the pin into
    docker/.env, and this locks the fallback to the code it ships with."""
    head = _repo_migration_head()
    source = read(PROD_COMPOSE)
    defaults = set(re.findall(r"MIGRATIONS_EXPECTED_HEAD:-([A-Za-z0-9_]+)", source))
    assert defaults == {head}, (
        f"prod compose pins MIGRATIONS_EXPECTED_HEAD default(s) {sorted(defaults)} "
        f"but the repository's migration head is {head}")


# ---------------------------------------------------------------------------
# Host paths the stack mounts must actually be in the repository
# ---------------------------------------------------------------------------
def _prod_bind_sources(source):
    """Host-side paths of every `- ../x:/y` bind mount in the prod stack."""
    found = set()
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ../"):
            continue
        spec = stripped[2:]
        host = spec.split(":", 1)[0]
        found.add(host)
    return found


def test_every_host_path_the_prod_stack_mounts_exists():
    """
    A bind mount whose source is missing does not fail loudly: Docker creates
    an empty directory and the container starts with nothing where its code or
    configuration should be. The backup service runs /scripts/backup-loop.sh
    from such a mount, so an absent source means no backups and no restore.
    """
    missing = []
    for host in sorted(_prod_bind_sources(read(PROD_COMPOSE))):
        # Sources are relative to the compose project directory, docker/.
        resolved = os.path.normpath(os.path.join(REPO, "docker", host))
        if not os.path.exists(resolved):
            missing.append(host)
    assert not missing, (
        "the prod stack mounts host paths that do not exist here: " + repr(missing)
    )


def _bare_directory_patterns(gitignore):
    """Unanchored `name/` patterns — the ones that match at ANY depth."""
    patterns = []
    for raw in gitignore.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if not line.endswith("/"):
            continue
        body = line[:-1]
        if body.startswith("/") or "/" in body or "*" in body or "?" in body:
            continue
        patterns.append(body)
    return patterns


def test_no_unanchored_gitignore_pattern_hides_source():
    """
    `backup/` in .gitignore matches a directory of that name at ANY depth, and
    it silently excluded the whole of scripts/backup/ -- backup.sh, restore.sh
    and backup-loop.sh -- from version control. Nothing showed in `git status`,
    and a fresh clone had no disaster-recovery path at all. Anchor such a
    pattern (`/backup/`) so it only means the directory at the repository root.
    """
    # Caches are supposed to be matched wherever they appear; anchoring those
    # would be wrong. Everything else that shadows a source directory is not.
    tool_caches = {
        "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache",
        ".ruff_cache", ".ipynb_checkpoints", "htmlcov", ".eggs",
    }
    source_suffixes = (".py", ".sh", ".sql", ".js", ".yml", ".yaml", ".conf", ".json")
    source_roots = ["scripts", "backend", "frontend", "alembic", "db", "config", "tests"]

    offenders = []
    for pattern in _bare_directory_patterns(read(f"{REPO}/.gitignore")):
        if pattern in tool_caches:
            continue
        for root in source_roots:
            root_path = os.path.join(REPO, root)
            if not os.path.isdir(root_path):
                continue
            for dirpath, dirnames, _ in os.walk(root_path):
                if any(cache in dirpath for cache in tool_caches):
                    continue
                if pattern not in dirnames:
                    continue
                hidden = os.path.join(dirpath, pattern)
                # Only a directory that actually holds source is a problem.
                carries_source = any(
                    name.endswith(source_suffixes)
                    for _, _, names in os.walk(hidden)
                    for name in names
                )
                if carries_source:
                    offenders.append(
                        f"{pattern}/ hides {os.path.relpath(hidden, REPO)}"
                    )
    assert not offenders, (
        "unanchored .gitignore directory patterns are hiding source: "
        + repr(sorted(set(offenders)))
        + " -- anchor them with a leading slash"
    )
