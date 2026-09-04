"""The two .env.example files are a contract, and an unread contract lies.

Existing coverage runs from compose OUTWARD: `test_deployment_surface.py`
proves every `${VAR:?}` compose requires is named in a template, and
`test_compose_sets_no_unknown_application_setting` proves compose sets no name
config.py does not declare. Both directions start at compose.

Nothing checked the templates themselves. The failures that leaves open are
quiet ones - the file looks authoritative and is simply wrong:

  * `REDIS_PASSWORD_FILE=/run/secrets/redis_password` sat in .env.example for
    a field that does not exist. The real field is REDIS_URL_FILE. An operator
    following that line mounts a secret, the app ignores it without a warning,
    and the inline password in REDIS_URL is used instead.

  * Settings that production overrides were absent from the template,
    including AUTH_SAME_HOST_ORIGIN_TRUSTED (declared default True, production
    false) and SAVE_CROPPED_IMAGES / SAVE_WEBHOOK_IMAGES (default True, so the
    default writes every face crop and webhook image to disk forever).

  * The template holds DEVELOPMENT values on purpose - ENVIRONMENT=development,
    AUTH_COOKIE_SECURE=false, ENABLE_API_DOCS=true. That is correct for the
    file and catastrophic if production ever stops overriding it. Nothing
    asserted that production still does.

Run:  python -m pytest tests/test_env_example_contract.py -v
"""

import io
import os
import re
import shutil

import pytest

from tests._repo_scan import find_repo_root

REPO = find_repo_root()

# `#?` on purpose: a commented-out line still documents that a setting exists,
# which is how MIGRATIONS_EXPECTED_HEAD is (correctly) carried in the compose
# template. This mirrors the pattern test_deployment_surface.py already uses.
KEY = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=(.*)$", re.M)


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8") as handle:
        return handle.read()


def _entries(*parts):
    return {m.group(1): m.group(2).strip() for m in KEY.finditer(_read(*parts))}


def _app_template():
    return _entries(".env.example")


def _compose_template():
    return _entries("docker", ".env.example")


def _prod_environment():
    """Names assigned under an `environment:` block in production compose.

    Six-space indent is the environment mapping; deeper or shallower is
    something else in the file.
    """
    return dict(re.findall(r"^\s{6}([A-Z][A-Z0-9_]*):\s*(.+)$",
                           _read("docker", "docker-compose.prod.yml"), re.M))


# Names that legitimately appear in the application template without being
# application settings: they are deployment credentials, documented here as a
# cross-reference so an operator reading one file learns the other exists.
# Every one of them must also appear in docker/.env.example, which is the file
# compose actually interpolates - asserted below.
DEPLOYMENT_CREDENTIALS = {
    "FR_APP_PASSWORD", "FR_MIGRATOR_PASSWORD", "FR_READONLY_PASSWORD",
    "FR_BACKUP_PASSWORD", "POSTGRES_SUPERUSER_PASSWORD",
    "REDIS_PASSWORD", "GRAFANA_ADMIN_PASSWORD", "GRAFANA_ROOT_URL",
    "PUBLIC_ORIGIN",
}


def test_every_key_in_the_application_template_is_real():
    """A name here that config.py does not declare is documentation that lies.

    Pydantic is configured `extra="ignore"`, so an operator who sets such a
    name gets no error and no effect - the exact shape of the
    REDIS_PASSWORD_FILE defect this test was written for.
    """
    from config import Settings

    declared = set(Settings.model_fields)
    unknown = sorted(name for name in _app_template()
                     if name not in declared
                     and name not in DEPLOYMENT_CREDENTIALS)
    assert not unknown, (
        ".env.example documents names that are neither declared settings nor "
        "listed deployment credentials, so setting them does nothing: "
        f"{unknown}")


def test_every_deployment_credential_is_documented_where_compose_reads_it():
    """Compose interpolates docker/.env only.

    A credential documented solely in the root template produces a deployment
    that fails on a variable the operator is looking straight at.
    """
    compose_documented = set(_compose_template())
    missing = sorted(name for name in DEPLOYMENT_CREDENTIALS
                     if name not in compose_documented)
    assert not missing, (
        "these are documented as deployment credentials but docker/.env.example "
        f"never names them: {missing}")


def test_the_application_template_loads_into_settings(tmp_path, monkeypatch):
    """Copy it to .env, construct Settings, and require it to parse.

    The process environment is cleared first: inside the running container
    every one of these names is already set, so an unisolated check reads the
    live values and passes no matter what the file contains. Measured before
    this was fixed - the file says ENVIRONMENT=development and an unisolated
    load reported 'production'.
    """
    from config import Settings

    template = _app_template()
    for name in list(os.environ):
        if name in template or name in set(Settings.model_fields):
            monkeypatch.delenv(name, raising=False)

    env_file = tmp_path / ".env"
    shutil.copy(os.path.join(REPO, ".env.example"), str(env_file))

    settings = Settings(_env_file=str(env_file))

    # Not merely "it constructed": prove the file's own values arrived, so a
    # future template value that cannot parse fails here rather than in a
    # deployment.
    assert settings.ENVIRONMENT == "development", (
        "the application template is the DEVELOPMENT template; if this now "
        "says production, the file's purpose changed and the override checks "
        "below no longer protect anything")


# Settings whose template value is unsafe for production. Each entry is
# (name, required production value). The template is allowed - expected - to
# hold the unsafe value; production compose must then override it explicitly.
PRODUCTION_INVARIANTS = [
    ("ENVIRONMENT", "production"),
    ("DEBUG", "false"),
    ("AUTH_COOKIE_SECURE", "true"),
    ("ENABLE_API_DOCS", "false"),
    ("AUTH_SAME_HOST_ORIGIN_TRUSTED", "false"),
    ("SAVE_CROPPED_IMAGES", "false"),
    ("SAVE_WEBHOOK_IMAGES", "false"),
]


@pytest.mark.parametrize("name,required", PRODUCTION_INVARIANTS)
def test_production_overrides_every_unsafe_template_value(name, required):
    """The safety net: production must not inherit a development value.

    Three of these are unsafe as the DECLARED DEFAULT too, not just in the
    template - AUTH_SAME_HOST_ORIGIN_TRUSTED, SAVE_CROPPED_IMAGES and
    SAVE_WEBHOOK_IMAGES all default True. For those, deleting the line from
    compose is enough to regress production, with nothing else to notice.
    """
    actual = _prod_environment().get(name)
    assert actual is not None, (
        f"docker-compose.prod.yml does not set {name}, so production inherits "
        f"a value that must be {required!r}")
    assert actual.strip().strip('"').lower() == required, (
        f"production sets {name}={actual!r}, required {required!r}")


def test_every_setting_production_overrides_is_documented():
    """If production must change a setting, operators must know it exists.

    Fifteen were undocumented when this was written, among them PGVECTOR_HNSW_M
    - production builds the index at M=32 while the declared default is 16,
    which is the same class of mismatch test_deployment_surface.py already
    guards from the compose side.
    """
    from config import Settings

    declared = set(Settings.model_fields)
    documented = set(_app_template())
    undocumented = sorted(name for name in _prod_environment()
                          if name in declared and name not in documented)
    assert not undocumented, (
        "production overrides these settings but .env.example never mentions "
        f"them, so the template understates the real surface: {undocumented}")
