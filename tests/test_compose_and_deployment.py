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

import re

import pytest

REPO = "/app"
PROD_COMPOSE = f"{REPO}/docker/docker-compose.prod.yml"
GPU_COMPOSE = f"{REPO}/docker/docker-compose.gpu.yml"
DEV_COMPOSE = f"{REPO}/docker/docker-compose.cpu.yml"
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

@pytest.mark.parametrize("compose", [PROD_COMPOSE, GPU_COMPOSE])
@pytest.mark.parametrize("port", ["5432", "6379", "11434"])
def test_datastores_publish_no_host_port(compose, port):
    """Postgres, Redis and Ollama enforce none of the application's
    authorization rules, so a reachable port bypasses all of them."""
    for mapping in published_ports(read(compose)):
        assert not mapping.endswith(f":{port}") and f":{port}:" not in mapping, \
            f"{compose} publishes port {port}: {mapping}"


@pytest.mark.parametrize("compose", [PROD_COMPOSE, GPU_COMPOSE])
def test_only_web_ports_are_published(compose):
    for mapping in published_ports(read(compose)):
        assert re.search(r":(80|443|3000)\b", mapping), \
            f"{compose} publishes an unexpected port: {mapping}"


def test_grafana_is_bound_to_loopback_only():
    assert '"127.0.0.1:3000:3000"' in read(PROD_COMPOSE)


# --------------------------------------------------------------- secrets

@pytest.mark.parametrize("compose", [PROD_COMPOSE, GPU_COMPOSE])
def test_no_default_passwords_remain(compose):
    source = read(compose)
    assert "POSTGRES_PASSWORD: admin" not in source
    assert ":admin@postgres" not in source
    assert "your-secret-key-change-in-production" not in source


@pytest.mark.parametrize("compose", [PROD_COMPOSE, GPU_COMPOSE])
def test_secrets_come_from_files_not_environment_literals(compose):
    source = read(compose)
    assert "JWT_SECRET_KEY_FILE: /run/secrets/jwt_secret" in source
    assert "secrets:" in source


@pytest.mark.parametrize("compose", [PROD_COMPOSE, GPU_COMPOSE])
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
    for compose in (PROD_COMPOSE, GPU_COMPOSE):
        assert not any("--requirepass" in line for line in directives(compose))
        assert "aclfile" in read(compose)


# ------------------------------------------------------------- TLS posture

@pytest.mark.parametrize("compose", [PROD_COMPOSE, GPU_COMPOSE])
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


@pytest.mark.parametrize("compose", [PROD_COMPOSE, GPU_COMPOSE, DEV_COMPOSE])
def test_single_worker(compose):
    """Runtime settings, the SQL-agent cancel registry, the single-flight job
    guards, webhook dedup and FAISS autosave are all process-local."""
    assert "WORKERS: 1" in read(compose)


@pytest.mark.parametrize("compose", [PROD_COMPOSE, GPU_COMPOSE])
def test_entrypoint_is_not_overridden(compose):
    """The Dockerfile entrypoint runs the config preflight, fixes permissions
    and drops privileges via gosu."""
    assert 'entrypoint: ["/bin/sh", "-c"]' not in read(compose)


# ----------------------------------------------------------- migration job

@pytest.mark.parametrize("compose", [PROD_COMPOSE, GPU_COMPOSE])
def test_migrations_run_in_a_dedicated_job(compose):
    source = read(compose)
    assert "migrate:" in source
    assert "--upgrade-head" in source


@pytest.mark.parametrize("compose", [PROD_COMPOSE, GPU_COMPOSE])
def test_api_waits_for_migrations_to_finish(compose):
    """Without this gate, N replicas race to apply the same revision."""
    assert "service_completed_successfully" in read(compose)


@pytest.mark.parametrize("compose", [PROD_COMPOSE, GPU_COMPOSE])
def test_api_workers_only_verify_the_schema(compose):
    assert "MIGRATIONS_MODE: verify" in read(compose)
    assert 'MIGRATIONS_FAIL_CLOSED: "true"' in read(compose)


# ------------------------------------------------------------ API surface

@pytest.mark.parametrize("compose", [PROD_COMPOSE, GPU_COMPOSE])
def test_api_documentation_is_disabled(compose):
    assert 'ENABLE_API_DOCS: "false"' in read(compose)


@pytest.mark.parametrize("compose", [PROD_COMPOSE, GPU_COMPOSE])
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


def test_every_data_action_has_a_registered_handler():
    """A data-action with no registration is a button that silently does
    nothing — exactly the regression this refactor could introduce."""
    used, registered = set(), set()
    for path in _frontend_files("*.html", "*.js"):
        source = read(path)
        if not path.endswith("/actions.js"):
            used |= set(re.findall(r'data-action(?:-\w+)?="([A-Za-z_$][\w$]*)"', source))
        for block in re.findall(r"Actions\.register\(\{(.*?)\n\}\);", source, re.S):
            registered |= set(re.findall(r"^\s{4}([A-Za-z_$][\w$]*)\s*[,:]", block, re.M))

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
