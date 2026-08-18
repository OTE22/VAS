"""
First-administrator bootstrap: no hardcoded credential, never logged.

    docker exec face_recognition_api python -m pytest tests/test_bootstrap_admin.py -v

The regression these cover: backend/lifespan.py used to create the first admin
with a hardcoded password and then log it in cleartext at INFO level, which put
a live administrator credential into stdout and into the bind-mounted log file.

The database-touching path (advisory lock, insert) is exercised by a
fresh-volume run, not here; these use a fake session so they run anywhere.
"""

import inspect
import logging
from types import SimpleNamespace

import pytest

from conftest import run_on_shared_loop as run_async

from backend.services.bootstrap_admin import (
    BootstrapAdminError,
    BootstrapOutcome,
    _assess_password,
    _audit_line,
    ensure_bootstrap_admin,
)


def cfg(**overrides):
    base = dict(
        ENVIRONMENT="development",
        BOOTSTRAP_ADMIN_ENABLED=True,
        BOOTSTRAP_ADMIN_USERNAME="admin",
        BOOTSTRAP_ADMIN_EMAIL="admin@example.com",
        BOOTSTRAP_ADMIN_PASSWORD="",
        BOOTSTRAP_ADMIN_PASSWORD_FILE="",
        BOOTSTRAP_ADMIN_REQUIRE_ROTATION=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value


class _FakeSession:
    """Answers the three queries ensure_bootstrap_admin issues, in order."""

    def __init__(self, admin_id, user_count):
        self._answers = [admin_id, user_count, admin_id]
        self._index = 0
        self.added = []
        self.committed = False

    async def execute(self, statement, params=None):
        text = str(statement).lower()
        if "pg_advisory_xact_lock" in text:
            return _FakeResult(True)
        answer = self._answers[min(self._index, len(self._answers) - 1)]
        self._index += 1
        return _FakeResult(answer)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


class _FakeSessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


class _FakeDbManager:
    def __init__(self, admin_id=None, user_count=0):
        self.session = _FakeSession(admin_id, user_count)

    def get_session(self):
        return _FakeSessionContext(self.session)


# ------------------------------------------------- no hardcoded credential

def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_lifespan_no_longer_hardcodes_a_seed_password():
    source = _read("/app/backend/lifespan.py")
    assert "admin123" not in source
    assert "password: admin" not in source
    assert 'hash_password("' not in source


def test_bootstrap_module_contains_no_hardcoded_password():
    source = _read("/app/backend/services/bootstrap_admin.py")
    assert "admin123" not in source


def test_audit_signature_cannot_accept_a_password():
    """Structural, so a future edit cannot casually add one."""
    assert "password" not in inspect.signature(_audit_line).parameters


def test_audit_line_records_provenance_not_the_value():
    line = _audit_line(username="admin", source="file", rotation_required=True)
    assert "credential_source=file" in line
    assert "rotation_required=True" in line


# ------------------------------------------------------- password strength

@pytest.mark.parametrize("password", ["admin123", "admin", "password", "short"])
def test_weak_bootstrap_passwords_are_rejected(password):
    assert _assess_password(password)


def test_strong_bootstrap_password_accepted():
    assert _assess_password("Rk8-Vt3-Jm6-Qz1-Hd9") == []


def test_assessment_never_echoes_the_password():
    secret = "Zx9-unique-bootstrap-sentinel-7Kq2-weak"
    for reason in _assess_password(secret):
        assert secret not in reason


# --------------------------------------------------------------- behavior

def test_existing_admin_is_left_alone():
    """The live deployment takes this branch, which is why the suite keeps
    its seeded credential."""
    manager = _FakeDbManager(admin_id=1)
    outcome = run_async(ensure_bootstrap_admin(manager, cfg=cfg()))
    assert outcome is BootstrapOutcome.EXISTS
    assert manager.session.added == []


def test_production_without_a_bootstrap_secret_refuses_to_start():
    manager = _FakeDbManager(admin_id=None, user_count=0)
    with pytest.raises(BootstrapAdminError) as exc:
        run_async(ensure_bootstrap_admin(manager, cfg=cfg(ENVIRONMENT="production")))
    assert "bootstrap credential" in str(exc.value)


def test_development_without_a_bootstrap_secret_skips():
    manager = _FakeDbManager(admin_id=None, user_count=0)
    outcome = run_async(ensure_bootstrap_admin(manager, cfg=cfg()))
    assert outcome is BootstrapOutcome.SKIPPED


def test_production_rejects_a_weak_bootstrap_password():
    manager = _FakeDbManager(admin_id=None, user_count=0)
    with pytest.raises(BootstrapAdminError) as exc:
        run_async(ensure_bootstrap_admin(
            manager, cfg=cfg(ENVIRONMENT="production", BOOTSTRAP_ADMIN_PASSWORD="admin123")
        ))
    assert "unacceptable" in str(exc.value)
    assert "admin123" not in str(exc.value)


def test_users_without_an_admin_is_not_treated_as_a_fresh_install():
    """Auto-creating an admin into a populated userbase is a privilege
    escalation path, not a bootstrap."""
    manager = _FakeDbManager(admin_id=None, user_count=5)
    with pytest.raises(BootstrapAdminError):
        run_async(ensure_bootstrap_admin(
            manager,
            cfg=cfg(ENVIRONMENT="production", BOOTSTRAP_ADMIN_PASSWORD="Rk8-Vt3-Jm6-Qz1-Hd9"),
        ))


def test_created_admin_is_forced_to_rotate():
    manager = _FakeDbManager(admin_id=None, user_count=0)
    outcome = run_async(ensure_bootstrap_admin(
        manager, cfg=cfg(BOOTSTRAP_ADMIN_PASSWORD="Rk8-Vt3-Jm6-Qz1-Hd9")
    ))
    assert outcome is BootstrapOutcome.CREATED
    assert manager.session.added[0].must_change_password is True


def test_rotation_can_be_disabled_for_the_test_stack():
    manager = _FakeDbManager(admin_id=None, user_count=0)
    run_async(ensure_bootstrap_admin(manager, cfg=cfg(
        BOOTSTRAP_ADMIN_PASSWORD="Rk8-Vt3-Jm6-Qz1-Hd9",
        BOOTSTRAP_ADMIN_REQUIRE_ROTATION=False,
    )))
    assert manager.session.added[0].must_change_password is False


def test_password_is_hashed_not_stored():
    manager = _FakeDbManager(admin_id=None, user_count=0)
    secret = "Rk8-Vt3-Jm6-Qz1-Hd9"
    run_async(ensure_bootstrap_admin(manager, cfg=cfg(BOOTSTRAP_ADMIN_PASSWORD=secret)))
    stored = manager.session.added[0].password_hash
    assert secret not in stored
    assert stored.startswith("$")


# ------------------------------------------------- the never-logged proof

def test_bootstrap_password_never_reaches_any_log_record(caplog):
    """Behavioral, not just source-string: inspect every emitted record's
    message and its lazy-format args."""
    secret = "Zx9-unique-bootstrap-sentinel-7Kq2"
    manager = _FakeDbManager(admin_id=None, user_count=0)

    with caplog.at_level(logging.DEBUG):
        run_async(ensure_bootstrap_admin(manager, cfg=cfg(BOOTSTRAP_ADMIN_PASSWORD=secret)))

    assert caplog.records, "must emit an audit record or this test is vacuous"
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert secret not in repr(record.args)


def test_creation_is_audited():
    secret = "Rk8-Vt3-Jm6-Qz1-Hd9"
    manager = _FakeDbManager(admin_id=None, user_count=0)
    import backend.services.bootstrap_admin as module

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Capture()
    module.logger.addHandler(handler)
    try:
        run_async(ensure_bootstrap_admin(manager, cfg=cfg(BOOTSTRAP_ADMIN_PASSWORD=secret)))
    finally:
        module.logger.removeHandler(handler)

    assert any("[AUDIT]" in message for message in records)
    assert all(secret not in message for message in records)


def test_the_system_principal_alone_does_not_count_as_an_existing_user():
    """A fresh migrated database already holds the `system` audit principal
    (migration a3b4c5d6e7f8). It is a machine actor: its presence must not turn
    a fresh database into a 'damaged deployment' and block the first admin —
    the count in ensure_bootstrap_admin excludes it explicitly."""
    import inspect as _inspect
    from backend.services import bootstrap_admin
    src = _inspect.getsource(bootstrap_admin.ensure_bootstrap_admin)
    assert "SYSTEM_USERNAME" in src and "SYSTEM_ROLE" in src, (
        "ensure_bootstrap_admin must exclude the system principal from the human-user count")
