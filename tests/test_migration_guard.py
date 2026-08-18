"""
Migrations must fail closed rather than report success they cannot justify.

    docker exec face_recognition_api python -m pytest tests/test_migration_guard.py -v

The regression these cover: run_alembic_migrations() used to `return True` when
alembic.ini was missing and when the alembic binary was missing, and
backend/lifespan.py continued startup after a failure. Together that meant the
application would happily serve requests against a schema it had never checked.

All pure — no database, no subprocess, no container state.
"""

import os
import pytest

from backend.utils.migrations import (
    EX_CONFIG,
    EX_TEMPFAIL,
    MigrationOutcome as MO,
    MigrationResult,
    _compare_revisions,
    _parse_revision_ids,
    should_fail_startup,
)

HEAD = "a5c6d7e8f9b0"
NEXT_HEAD = "b1c2d3e4f5a6"


# --------------------------------------------- outcomes that are not success

@pytest.mark.parametrize("outcome", [
    MO.CONFIG_MISSING,
    MO.TOOLING_MISSING,
    MO.DB_UNREACHABLE,
    MO.REVISION_MISMATCH,
    MO.MULTIPLE_HEADS,
    MO.FAILED,
])
def test_unsatisfied_outcomes_are_not_ok(outcome):
    assert MigrationResult(outcome).ok is False


def test_missing_alembic_config_is_not_success():
    """Previously `return True` — the application then served an unknown schema."""
    assert MigrationResult(MO.CONFIG_MISSING).ok is False


def test_missing_alembic_binary_is_not_success():
    """Previously `return True  # Non-critical, continue startup`."""
    assert MigrationResult(MO.TOOLING_MISSING).ok is False


@pytest.mark.parametrize("outcome", [MO.UP_TO_DATE, MO.APPLIED])
def test_satisfied_outcomes_are_ok(outcome):
    assert MigrationResult(outcome).ok is True


# --------------------------------------------------------- revision parsing

def test_parse_revision_ids_ignores_info_lines_and_head_marker():
    output = (
        "INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.\n"
        "INFO  [alembic.runtime.migration] Will assume transactional DDL.\n"
        f"{HEAD} (head)\n"
    )
    assert _parse_revision_ids(output) == [HEAD]


def test_parse_revision_ids_handles_empty_output():
    assert _parse_revision_ids("") == []
    assert _parse_revision_ids("INFO  [alembic] nothing here\n") == []


def test_parse_revision_ids_handles_multiple_heads():
    assert _parse_revision_ids(f"{HEAD} (head)\n{NEXT_HEAD} (head)\n") == [HEAD, NEXT_HEAD]


def test_parse_revision_ids_deduplicates():
    assert _parse_revision_ids(f"{HEAD}\n{HEAD}\n") == [HEAD]


# ------------------------------------------------------ revision comparison

def test_matching_revision_is_up_to_date():
    assert _compare_revisions([HEAD], [HEAD]).outcome is MO.UP_TO_DATE


def test_behind_database_is_a_mismatch():
    result = _compare_revisions([HEAD], [NEXT_HEAD])
    assert result.outcome is MO.REVISION_MISMATCH
    assert NEXT_HEAD in result.detail


def test_fresh_database_with_pending_migrations_is_a_mismatch():
    assert _compare_revisions([], [HEAD]).outcome is MO.REVISION_MISMATCH


def test_branched_history_is_rejected():
    """Multiple heads are otherwise completely silent."""
    result = _compare_revisions([HEAD], [HEAD, NEXT_HEAD])
    assert result.outcome is MO.MULTIPLE_HEADS


def test_expected_head_pin_is_enforced():
    """A drifted schema must not serve traffic even if it is self-consistent."""
    result = _compare_revisions([HEAD], [HEAD], expected=NEXT_HEAD)
    assert result.outcome is MO.REVISION_MISMATCH
    assert NEXT_HEAD in result.detail


def test_expected_head_pin_passes_when_it_matches():
    assert _compare_revisions([HEAD], [HEAD], expected=HEAD).outcome is MO.UP_TO_DATE


def test_empty_pin_disables_the_check():
    assert _compare_revisions([HEAD], [HEAD], expected="").outcome is MO.UP_TO_DATE


# ------------------------------------------------------------ startup policy

def test_migration_failure_is_fatal_everywhere():
    """No permissive mode for schema state: an unsatisfied outcome aborts in
    every environment (MIGRATIONS_FAIL_CLOSED was removed — it only ever
    permitted a boot after a failed step)."""
    assert should_fail_startup(MigrationResult(MO.FAILED)) is True


@pytest.mark.parametrize("outcome", [
    MO.CONFIG_MISSING, MO.TOOLING_MISSING, MO.REVISION_MISMATCH, MO.MULTIPLE_HEADS,
])
def test_every_unsatisfied_outcome_is_fatal(outcome):
    assert should_fail_startup(MigrationResult(outcome)) is True


def test_success_never_fails_startup():
    assert should_fail_startup(MigrationResult(MO.UP_TO_DATE)) is False


def test_the_flag_is_gone_from_config_and_compose():
    import re
    from config import Settings
    assert "MIGRATIONS_FAIL_CLOSED" not in Settings.model_fields
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("docker/docker-compose.cpu.yml", "docker/docker-compose.prod.yml",
                ".env.example", "backend/lifespan.py", "backend/security/config_guard.py"):
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            body = fh.read()
        for line in body.splitlines():
            if "MIGRATIONS_FAIL_CLOSED" in line:
                assert line.strip().startswith("#"), f"{rel}: {line.strip()!r} still consumes the removed flag"


def test_expected_head_from_scripts_is_a_single_head():
    from backend.utils.migrations import expected_head_from_scripts
    head = expected_head_from_scripts()
    assert isinstance(head, str) and len(head) >= 12


# ------------------------------------------------------------- exit codes

def test_database_unreachable_uses_a_retryable_exit_code():
    """An orchestrator should retry a cold database, not treat it as misconfig."""
    assert EX_TEMPFAIL == 75
    assert EX_CONFIG == 78


# ------------------------------------------------------- lifespan wiring

def test_lifespan_aborts_startup_on_unsatisfied_migrations():
    with open("/app/backend/lifespan.py", encoding="utf-8") as handle:
        source = handle.read()
    block = source[source.index("Running database migrations"):]
    block = block[:block.index("# 1.1 Database")]
    assert "should_fail_startup" in block
    assert "raise RuntimeError" in block
    # no permissive branch survives: an unsatisfied migration step never continues
    assert "Continuing startup" not in block
    assert "Continuing startup, but database schema may be incomplete" not in source


def test_skip_mode_is_available_for_workers_behind_a_migration_job():
    from backend.utils.migrations import run_alembic_migrations_detailed

    result = run_alembic_migrations_detailed("skip")
    assert result.ok
    assert "skipped" in result.detail
