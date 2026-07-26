"""
Database Migration Utilities
============================
Automatic Alembic migration runner for startup
"""

import argparse
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# sysexits.h
EX_CONFIG = 78
EX_TEMPFAIL = 75


def _get_sync_database_url() -> str:
    """Return the configured database URL with a sync SQLAlchemy driver."""
    from config import settings
    from sqlalchemy.engine.url import make_url

    database_url = settings.DATABASE_URL
    if "+asyncpg" in database_url:
        database_url = database_url.replace("+asyncpg", "+psycopg2")
    elif "asyncpg" in database_url:
        database_url = database_url.replace("asyncpg", "psycopg2")

    url = make_url(database_url)
    if url.host == "postgres" and not Path("/.dockerenv").exists():
        url = url.set(host=os.getenv("LOCAL_DB_HOST", "localhost"))

    return url.render_as_string(hide_password=False)


def _redact_database_url(database_url: str) -> str:
    """Render a database URL without exposing the password in logs."""
    try:
        from sqlalchemy.engine.url import make_url

        return make_url(database_url).render_as_string(hide_password=True)
    except Exception:
        if "@" in database_url:
            return f"***@{database_url.rsplit('@', 1)[1]}"
        return "<database-url-hidden>"


def _summarize_stderr(stderr: str) -> str:
    """Extract the useful final error line from a subprocess stderr blob."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "<no stderr>"

    for line in reversed(lines):
        if line.startswith("(Background on this error at:"):
            continue
        if line.startswith("File ") or line.startswith("Traceback "):
            continue
        return line

    return lines[-1]


def _wait_for_database(max_wait_seconds: float = 60.0, retry_interval: float = 2.0) -> bool:
    """Wait until Postgres accepts a simple sync connection for Alembic."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool

    database_url = _get_sync_database_url()
    safe_url = _redact_database_url(database_url)
    deadline = time.monotonic() + max_wait_seconds
    attempt = 0

    logger.info(f"🔌 Verifying database connectivity for migrations: {safe_url}")

    while True:
        attempt += 1
        engine = None

        try:
            engine = create_engine(
                database_url,
                poolclass=NullPool,
                pool_pre_ping=True,
                connect_args={"connect_timeout": 5},
            )
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))

            logger.info(f"   ✅ Database reachable for migrations (attempt {attempt})")
            return True

        except Exception as exc:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                logger.error("   ❌ Database was not reachable before migration timeout")
                logger.error(f"      Last error: {type(exc).__name__}: {exc}")
                return False

            sleep_for = min(retry_interval, remaining)
            logger.warning(
                "   ⏳ Database not reachable yet for migrations "
                f"(attempt {attempt}, retrying in {sleep_for:.1f}s): "
                f"{type(exc).__name__}: {exc}"
            )
            time.sleep(sleep_for)

        finally:
            if engine is not None:
                engine.dispose()


class MigrationOutcome(str, Enum):
    """What actually happened, as opposed to a bare True/False.

    The old boolean contract reported a missing alembic.ini and a missing
    alembic binary as success, so the application would serve traffic against
    a schema it had never verified.
    """

    UP_TO_DATE = "up_to_date"
    APPLIED = "applied"
    CONFIG_MISSING = "config_missing"
    TOOLING_MISSING = "tooling_missing"
    DB_UNREACHABLE = "db_unreachable"
    REVISION_MISMATCH = "revision_mismatch"
    MULTIPLE_HEADS = "multiple_heads"
    FAILED = "failed"


@dataclass(frozen=True)
class MigrationResult:
    outcome: MigrationOutcome
    current_revision: Optional[str] = None
    head_revision: Optional[str] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in (MigrationOutcome.UP_TO_DATE, MigrationOutcome.APPLIED)


_REVISION_RE = re.compile(r"\b([0-9a-f]{8,32})\b")


def _parse_revision_ids(output: str) -> List[str]:
    """Extract revision ids from `alembic current` / `alembic heads` output.

    Alembic interleaves INFO lines and annotates heads, e.g.
    'a5c6d7e8f9b0 (head)', so a naive split produces junk.
    """
    revisions: List[str] = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line or line.startswith("INFO") or line.startswith("WARN"):
            continue
        match = _REVISION_RE.match(line)
        if match and match.group(1) not in revisions:
            revisions.append(match.group(1))
    return revisions


def _compare_revisions(
    current: List[str],
    heads: List[str],
    expected: str = "",
) -> MigrationResult:
    """Decide whether the live schema is the one this code expects."""
    current_repr = ",".join(current) or None
    head_repr = ",".join(heads) or None

    if len(heads) > 1:
        return MigrationResult(
            MigrationOutcome.MULTIPLE_HEADS, current_repr, head_repr,
            f"branched migration history: {heads}",
        )

    if expected and heads and heads[0] != expected:
        return MigrationResult(
            MigrationOutcome.REVISION_MISMATCH, current_repr, head_repr,
            f"head {heads[0]} does not match the pinned MIGRATIONS_EXPECTED_HEAD {expected}",
        )

    if set(current) != set(heads):
        return MigrationResult(
            MigrationOutcome.REVISION_MISMATCH, current_repr, head_repr,
            f"database is at {current_repr or 'no revision'} but code expects {head_repr}",
        )

    return MigrationResult(MigrationOutcome.UP_TO_DATE, current_repr, head_repr)


def should_fail_startup(
    result: MigrationResult,
    *,
    environment: str,
    fail_closed: bool = False,
) -> bool:
    """Whether an unsatisfied migration state must abort startup.

    Pure, so the policy is testable without a database or a container.
    """
    if result.ok:
        return False
    is_production = str(environment or "").strip().lower() in ("production", "prod")
    return bool(is_production or fail_closed)


def run_alembic_migrations_detailed(mode: str = "run") -> MigrationResult:
    """
    Run or verify Alembic migrations.

    mode:
        run    - wait for the database, `upgrade head`, then verify
        verify - verify the current revision only; never writes
        skip   - do nothing (the entrypoint already verified)
    """
    if str(mode).strip().lower() == "skip":
        logger.info("🔄 Migrations skipped (MIGRATIONS_MODE=skip)")
        return MigrationResult(MigrationOutcome.UP_TO_DATE, detail="skipped by configuration")

    verify_only = str(mode).strip().lower() == "verify"
    logger.info("=" * 70)
    logger.info("🔄 DATABASE MIGRATION CHECK - Starting...")
    logger.info("=" * 70)
    
    try:
        # Get project root directory
        project_root = Path(__file__).parent.parent.parent
        alembic_dir = project_root / "alembic"
        alembic_ini = alembic_dir / "alembic.ini"
        versions_dir = alembic_dir / "versions"
        
        logger.info(f"📁 Project root: {project_root}")
        logger.info(f"📁 Alembic directory: {alembic_dir}")
        logger.info(f"📁 Alembic config: {alembic_ini}")
        logger.info(f"📁 Versions directory: {versions_dir}")
        
        # A missing config is NOT success: reporting it as such let the
        # application serve requests against an unverified schema.
        if not alembic_ini.exists():
            logger.error("❌ alembic.ini not found — cannot verify the database schema")
            logger.error(f"   Expected location: {alembic_ini}")
            return MigrationResult(
                MigrationOutcome.CONFIG_MISSING, detail=f"alembic.ini missing at {alembic_ini}"
            )

        if not alembic_dir.exists():
            logger.error("❌ alembic directory not found — cannot verify the database schema")
            logger.error(f"   Expected location: {alembic_dir}")
            return MigrationResult(
                MigrationOutcome.CONFIG_MISSING, detail=f"alembic dir missing at {alembic_dir}"
            )
        
        # Check for migration files
        if versions_dir.exists():
            migration_files = list(versions_dir.glob("*.py"))
            logger.info(f"📋 Found {len(migration_files)} migration file(s)")
            for mig_file in migration_files:
                logger.info(f"   • {mig_file.name}")
        else:
            logger.info("📋 No versions directory found (will be created on first migration)")
        
        # Build Alembic command base
        alembic_cmd = [
            sys.executable, "-m", "alembic",
            "--config", str(alembic_ini)
        ]
        
        logger.info(f"🔧 Alembic command: {' '.join(alembic_cmd)}")

        wait_seconds = float(os.getenv("MIGRATION_DB_WAIT_SECONDS", "60"))
        retry_interval = float(os.getenv("MIGRATION_DB_RETRY_INTERVAL_SECONDS", "2"))
        if not _wait_for_database(wait_seconds, retry_interval):
            return MigrationResult(
                MigrationOutcome.DB_UNREACHABLE,
                detail=f"database unreachable after {wait_seconds}s",
            )
        
        # Change to alembic directory for Alembic commands
        original_cwd = os.getcwd()
        os.chdir(str(alembic_dir))
        logger.info(f"📂 Changed working directory to: {alembic_dir}")
        
        try:
            # Step 1: Check current revision
            logger.info("")
            logger.info("📍 Step 1: Checking current database revision...")
            result = subprocess.run(
                alembic_cmd + ["current"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(alembic_dir)
            )
            
            if result.returncode == 0:
                current_rev = result.stdout.strip()
                if current_rev:
                    logger.info(f"   ✅ Current revision: {current_rev}")
                    # Parse revision details
                    for line in current_rev.split('\n'):
                        if line.strip() and not line.startswith('INFO'):
                            logger.info(f"      {line.strip()}")
                else:
                    logger.info("   ℹ️  No current revision (fresh database or no migrations applied)")
            else:
                logger.warning(f"   ⚠️  Could not get current revision")
                logger.warning(f"      Error: {_summarize_stderr(result.stderr)}")
                logger.debug(f"      Full alembic current stderr:\n{result.stderr}")
                if "Target database is not up to date" in result.stderr:
                    logger.info("   ℹ️  Database exists but migrations not applied yet")
            
            # Step 2: Check for available heads
            logger.info("")
            logger.info("📍 Step 2: Checking available migration heads...")
            result = subprocess.run(
                alembic_cmd + ["heads"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(alembic_dir)
            )
            
            if result.returncode == 0:
                heads_output = result.stdout.strip()
                if heads_output:
                    heads = [h.strip() for h in heads_output.split('\n') if h.strip() and not h.startswith('INFO')]
                    logger.info(f"   ✅ Found {len(heads)} migration head(s):")
                    for head in heads:
                        logger.info(f"      • {head}")
                else:
                    logger.info("   ℹ️  No migration heads found (no migrations created yet)")
            else:
                logger.warning(f"   ⚠️  Could not check migration heads: {_summarize_stderr(result.stderr)}")
                logger.debug(f"      Full alembic heads stderr:\n{result.stderr}")
                if "Can't locate revision identified by" in result.stderr:
                    logger.warning("   ⚠️  Database may have inconsistent migration state")
            
            # Step 3: Check for pending migrations
            logger.info("")
            logger.info("📍 Step 3: Checking for pending migrations...")
            result = subprocess.run(
                alembic_cmd + ["check"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(alembic_dir)
            )
            
            # Step 4: Show migration history
            logger.info("")
            logger.info("📍 Step 4: Migration history...")
            result = subprocess.run(
                alembic_cmd + ["history", "--verbose"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(alembic_dir)
            )
            
            if result.returncode == 0 and result.stdout.strip():
                history_lines = [l for l in result.stdout.strip().split('\n') if l.strip() and not l.startswith('INFO')]
                if history_lines:
                    logger.info(f"   📜 Migration history ({len(history_lines)} entries):")
                    for line in history_lines[:10]:  # Show first 10
                        logger.info(f"      {line}")
                    if len(history_lines) > 10:
                        logger.info(f"      ... and {len(history_lines) - 10} more")
                else:
                    logger.info("   ℹ️  No migration history (no migrations created yet)")
            
            # Step 5: Run migrations (upgrade to head), unless verifying only
            expected_head = os.getenv("MIGRATIONS_EXPECTED_HEAD", "") or ""

            if verify_only:
                logger.info("")
                logger.info("📍 Step 5: Verifying revision (MIGRATIONS_MODE=verify, no writes)...")
                verdict = _verify_revision(alembic_cmd, alembic_dir, expected_head)
                if verdict.ok:
                    logger.info(f"   ✅ Schema verified at revision {verdict.current_revision}")
                else:
                    logger.error(f"   ❌ Schema verification failed: {verdict.detail}")
                return verdict

            logger.info("")
            logger.info("📍 Step 5: Applying migrations (upgrade to head)...")
            logger.info("   This may take a moment...")

            result = subprocess.run(
                alembic_cmd + ["upgrade", "head"],
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout for migrations (increased for large migrations)
                cwd=str(alembic_dir)
            )
            
            logger.info("")
            if result.returncode == 0:
                output_lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
                error_lines = result.stderr.strip().split('\n') if result.stderr.strip() else []
                
                # Check if any migrations were actually run
                migrations_applied = False
                for line in output_lines:
                    if "Running upgrade" in line:
                        migrations_applied = True
                        logger.info(f"   ✅ {line.strip()}")
                    elif "Running downgrade" in line:
                        migrations_applied = True
                        logger.warning(f"   ⚠️  {line.strip()}")
                    elif line.strip() and not line.startswith('INFO'):
                        logger.debug(f"      {line.strip()}")
                
                # Log any warnings from stderr (Alembic often puts info in stderr)
                for line in error_lines:
                    if line.strip() and not line.startswith('INFO'):
                        if "warning" in line.lower() or "error" in line.lower():
                            logger.warning(f"      ⚠️  {line.strip()}")
                        else:
                            logger.debug(f"      {line.strip()}")
                
                if migrations_applied:
                    logger.info("   ✅ Migrations applied successfully!")
                else:
                    logger.info("   ✅ Database is up to date (no migrations needed)")
                
                # Applying without confirming leaves the same blind spot the
                # boolean contract had: verify the resulting revision.
                verdict = _verify_revision(alembic_cmd, alembic_dir, expected_head)
                if not verdict.ok:
                    logger.error("")
                    logger.error("=" * 70)
                    logger.error(f"❌ POST-MIGRATION VERIFICATION FAILED: {verdict.detail}")
                    logger.error("=" * 70)
                    return verdict

                logger.info("")
                logger.info("=" * 70)
                logger.info("✅ DATABASE MIGRATION CHECK - Completed Successfully")
                logger.info("=" * 70)
                return MigrationResult(
                    MigrationOutcome.APPLIED if migrations_applied else MigrationOutcome.UP_TO_DATE,
                    verdict.current_revision,
                    verdict.head_revision,
                )
            else:
                logger.error("")
                logger.error("❌ MIGRATION FAILED!")
                logger.error("")
                if result.stdout:
                    logger.error("📋 STDOUT:")
                    for line in result.stdout.strip().split('\n'):
                        logger.error(f"   {line}")
                if result.stderr:
                    logger.error("📋 STDERR:")
                    for line in result.stderr.strip().split('\n'):
                        logger.error(f"   {line}")
                logger.error("")
                logger.error("=" * 70)
                logger.error("❌ DATABASE MIGRATION CHECK - Failed")
                logger.error("=" * 70)
                return MigrationResult(
                    MigrationOutcome.FAILED,
                    detail=_summarize_stderr(result.stderr) or "alembic upgrade returned non-zero",
                )

        finally:
            # Restore original working directory
            os.chdir(original_cwd)
            logger.debug(f"📂 Restored working directory to: {original_cwd}")
            
    except subprocess.TimeoutExpired:
        logger.error("")
        logger.error("=" * 70)
        logger.error("❌ Migration timeout (exceeded 5 minutes)")
        logger.error("   This may indicate a database connection issue or very large migration")
        logger.error("=" * 70)
        return MigrationResult(MigrationOutcome.FAILED, detail="alembic timed out")
    except FileNotFoundError as e:
        # Missing tooling is NOT success: without alembic the schema cannot be
        # verified at all, which is exactly when serving is most dangerous.
        logger.error("")
        logger.error("=" * 70)
        logger.error("❌ Alembic not found — the database schema cannot be verified")
        logger.error(f"   Error: {e}")
        logger.error("   Install with: pip install alembic")
        logger.error("=" * 70)
        return MigrationResult(MigrationOutcome.TOOLING_MISSING, detail=str(e))
    except Exception as e:
        logger.error("")
        logger.error("=" * 70)
        logger.error(f"❌ Migration error: {type(e).__name__}: {e}")
        logger.error("")
        logger.exception("Full exception traceback:")
        logger.error("=" * 70)
        return MigrationResult(
            MigrationOutcome.FAILED, detail=f"{type(e).__name__}: {e}"
        )


def _verify_revision(alembic_cmd, alembic_dir, expected_head: str = "") -> MigrationResult:
    """Compare the database's current revision against the code's head."""
    try:
        current_proc = subprocess.run(
            alembic_cmd + ["current"], capture_output=True, text=True,
            timeout=30, cwd=str(alembic_dir),
        )
        heads_proc = subprocess.run(
            alembic_cmd + ["heads"], capture_output=True, text=True,
            timeout=30, cwd=str(alembic_dir),
        )
    except subprocess.TimeoutExpired:
        return MigrationResult(MigrationOutcome.FAILED, detail="alembic current/heads timed out")

    if current_proc.returncode != 0 or heads_proc.returncode != 0:
        return MigrationResult(
            MigrationOutcome.FAILED,
            detail=_summarize_stderr(current_proc.stderr or heads_proc.stderr),
        )

    return _compare_revisions(
        _parse_revision_ids(current_proc.stdout),
        _parse_revision_ids(heads_proc.stdout),
        expected_head,
    )


def run_alembic_migrations() -> bool:
    """Backwards-compatible boolean wrapper for existing callers."""
    return run_alembic_migrations_detailed(
        os.getenv("MIGRATIONS_MODE", "run")
    ).ok


async def run_migrations_async_detailed(mode: str = "run") -> MigrationResult:
    """Async wrapper returning the full outcome."""
    from fastapi.concurrency import run_in_threadpool

    try:
        return await run_in_threadpool(run_alembic_migrations_detailed, mode)
    except Exception as e:
        logger.error(f"Failed to run migrations: {type(e).__name__}: {e}")
        return MigrationResult(MigrationOutcome.FAILED, detail=f"{type(e).__name__}: {e}")


async def run_migrations_async() -> bool:
    """Backwards-compatible boolean wrapper."""
    result = await run_migrations_async_detailed(os.getenv("MIGRATIONS_MODE", "run"))
    return result.ok


def main(argv: Optional[List[str]] = None) -> int:
    """CLI for the dedicated migration job and the startup preflight.

        python -m backend.utils.migrations --upgrade-head
        python -m backend.utils.migrations --verify

    Exit codes: 0 ok, 75 (EX_TEMPFAIL) database unreachable — retryable,
    78 (EX_CONFIG) anything else — do not start the application.
    """
    parser = argparse.ArgumentParser(prog="backend.utils.migrations")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--upgrade-head", action="store_true",
                       help="apply pending migrations, then verify")
    group.add_argument("--verify", action="store_true",
                       help="verify the current revision without writing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    mode = "verify" if args.verify else "run"
    result = run_alembic_migrations_detailed(mode)

    if result.ok:
        print(f"migrations: {result.outcome.value} (revision={result.current_revision})")
        return 0

    sys.stderr.write(
        f"migrations: {result.outcome.value} — {result.detail}\n"
        f"  current={result.current_revision} head={result.head_revision}\n"
    )
    return EX_TEMPFAIL if result.outcome is MigrationOutcome.DB_UNREACHABLE else EX_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())

