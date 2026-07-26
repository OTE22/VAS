"""
Database Migration Utilities
============================
Automatic Alembic migration runner for startup
"""

import os
import sys
import logging
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)


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


def run_alembic_migrations():
    """
    Run Alembic migrations automatically at startup.
    Detects and applies any pending database schema changes.
    Enhanced with detailed logging.
    
    Returns:
        bool: True if migrations succeeded or were not needed, False on error
    """
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
        
        if not alembic_ini.exists():
            logger.warning("⚠️  alembic.ini not found, skipping migrations")
            logger.warning(f"   Expected location: {alembic_ini}")
            return True
        
        if not alembic_dir.exists():
            logger.warning("⚠️  alembic directory not found, skipping migrations")
            logger.warning(f"   Expected location: {alembic_dir}")
            return True
        
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
            return False
        
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
            
            # Step 5: Run migrations (upgrade to head)
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
                
                logger.info("")
                logger.info("=" * 70)
                logger.info("✅ DATABASE MIGRATION CHECK - Completed Successfully")
                logger.info("=" * 70)
                return True
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
                return False
                
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
        return False
    except FileNotFoundError as e:
        logger.warning("")
        logger.warning("=" * 70)
        logger.warning("⚠️  Alembic not found, skipping migrations")
        logger.warning(f"   Error: {e}")
        logger.warning("   Install with: pip install alembic")
        logger.warning("=" * 70)
        return True  # Non-critical, continue startup
    except Exception as e:
        logger.error("")
        logger.error("=" * 70)
        logger.error(f"❌ Migration error: {type(e).__name__}: {e}")
        logger.error("")
        logger.exception("Full exception traceback:")
        logger.error("=" * 70)
        return False


async def run_migrations_async():
    """
    Async wrapper for running migrations.
    Runs migrations in a thread pool to avoid blocking.
    """
    import asyncio
    from fastapi.concurrency import run_in_threadpool
    
    try:
        result = await run_in_threadpool(run_alembic_migrations)
        return result
    except Exception as e:
        logger.error(f"Failed to run migrations: {e}")
        return False

