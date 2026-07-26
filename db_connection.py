import asyncio
import logging
from typing import AsyncGenerator, Optional, Callable, Awaitable, TypeVar
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    async_sessionmaker,
    AsyncEngine,
)
from sqlalchemy.exc import InterfaceError, DisconnectionError
from fastapi import HTTPException

from db_models import Base
from config import settings

logger = logging.getLogger(__name__)
T = TypeVar("T")


class DatabaseManager:
    """Manages async PostgreSQL connections safely"""

    def __init__(self):
        self.engine: Optional[AsyncEngine] = None
        self.session_maker: Optional[async_sessionmaker[AsyncSession]] = None
        self._initialized = False

        self._stats = {
            "total_sessions": 0,
            "active_sessions": 0,
            "failed_sessions": 0,
        }

    # =====================================================
    # Initialization
    # =====================================================
    async def init_db(self) -> None:
        if self._initialized:
            logger.warning("Database already initialized")
            return

        try:
            self.engine = create_async_engine(
                settings.DATABASE_URL,
                echo=settings.DEBUG,

                # --- Pool configuration (OPTIMIZED FOR 50+ CAMERAS AND 20 USERS)
                pool_size=settings.DB_POOL_SIZE,  # Default: 50 for GPU, 30 for CPU
                max_overflow=settings.DB_MAX_OVERFLOW,  # Default: 100 for GPU, 60 for CPU
                pool_timeout=60,  # Wait up to 60 seconds for connection
                pool_recycle=getattr(settings, 'DB_POOL_RECYCLE', 3600),  # Recycle every hour
                pool_pre_ping=getattr(settings, 'DB_POOL_PRE_PING', True),  # Verify connections
                pool_reset_on_return='rollback',  # Reset connections on return

                # --- asyncpg tuning (INCREASED TIMEOUTS)
                connect_args={
                    "timeout": 30,  # INCREASED from 10 to 30 seconds
                    "command_timeout": 120,  # INCREASED from 60 to 120 seconds
                    "server_settings": {
                        "application_name": settings.APP_NAME,
                        "jit": "off",
                        "statement_timeout": "120000",  # 120 seconds in milliseconds
                        "idle_in_transaction_session_timeout": "300000",  # 5 minutes
                    },
                },

                execution_options={
                    "isolation_level": "READ COMMITTED",
                },
            )

            self.session_maker = async_sessionmaker(
                self.engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
                autocommit=False,
            )

            # Create tables (checkfirst=True skips existing tables)
            try:
                async with self.engine.begin() as conn:
                    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=True))
                logger.info("✅ Database tables verified/created")
            except Exception as table_error:
                # Check if it's a type conflict error (table/type already exists)
                error_str = str(table_error)
                if "duplicate key value violates unique constraint" in error_str or "already exists" in error_str:
                    logger.warning(f"⚠️  Table creation conflict (tables may already exist): {error_str}")
                    logger.warning("   This is usually safe to ignore if tables already exist.")
                    # Try to verify tables exist by querying one
                    try:
                        async with self.engine.begin() as conn:
                            result = await conn.execute(text("""
                                SELECT EXISTS (
                                    SELECT FROM information_schema.tables 
                                    WHERE table_schema = 'public' 
                                    AND table_name = 'pipelines'
                                )
                            """))
                            table_exists = result.scalar()
                            if table_exists:
                                logger.info("✅ Tables already exist, skipping creation")
                            else:
                                logger.error("❌ Tables don't exist but creation failed - manual intervention needed")
                                raise table_error
                    except Exception as verify_error:
                        logger.error(f"❌ Failed to verify table existence: {verify_error}")
                        raise table_error
                else:
                    # Re-raise if it's a different error
                    raise table_error

            self._initialized = True
            logger.info("✅ Database initialized successfully")
            await self._log_pool_status()

        except Exception as e:
            logger.exception("❌ Database initialization failed")
            raise e

    # =====================================================
    # Shutdown
    # =====================================================
    async def close_db(self) -> None:
        if not self.engine:
            return

        await asyncio.sleep(0.2)  # allow in-flight requests to finish
        await self.engine.dispose()
        self._initialized = False
        logger.info("✅ Database connections closed")

    # =====================================================
    # Session Context Manager (CRITICAL FIX)
    # =====================================================
    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        if not self.session_maker:
            logger.error("[DB] ❌ Session maker not initialized - database not ready")
            raise RuntimeError("Database not initialized")

        self._stats["total_sessions"] += 1
        self._stats["active_sessions"] += 1
        logger.debug(f"[DB] 🔄 Creating database session (total: {self._stats['total_sessions']}, active: {self._stats['active_sessions']})")

        try:
            async with self.session_maker() as session:
                logger.debug(f"[DB] ✅ Database session created successfully")
                try:
                    yield session
                    # Attempt to commit - handle closed connections gracefully
                    try:
                        await session.commit()
                    except (InterfaceError, DisconnectionError) as conn_error:
                        # Connection was closed (timeout, network issue, or server restart)
                        # This can happen if the connection pool recycled the connection
                        # or if the database server closed it. Log warning but don't fail.
                        error_str = str(conn_error)
                        if "connection is closed" in error_str.lower() or \
                           "underlying connection is closed" in error_str.lower():
                            logger.warning(
                                f"⚠️  Database connection closed during commit - "
                                f"this may occur due to connection pool recycling or database timeout. "
                                f"Error: {type(conn_error).__name__}"
                            )
                            # Don't raise - connection closure during commit is often recoverable
                            # The transaction may have already completed on the server side
                        else:
                            # Re-raise other connection errors
                            raise
                    except Exception as commit_error:
                        # Re-raise other exceptions (not connection-related)
                        raise

                except asyncio.CancelledError:
                    # 🚨 MUST propagate cancellation
                    try:
                        await session.rollback()
                    except Exception:
                        pass  # Ignore rollback errors if connection is closed
                    raise

                except HTTPException:
                    # 🚨 HTTPException should propagate normally (not a DB error)
                    # Don't rollback or log as DB error - let FastAPI handle it
                    try:
                        await session.rollback()
                    except Exception:
                        pass  # Ignore rollback errors
                    raise

                except Exception as e:
                    self._stats["failed_sessions"] += 1
                    # Attempt rollback, but handle closed connections gracefully
                    try:
                        await session.rollback()
                    except (InterfaceError, DisconnectionError) as rollback_error:
                        # Connection closed during rollback - log but don't fail
                        error_str = str(rollback_error)
                        if "connection is closed" in error_str.lower():
                            logger.warning(
                                f"⚠️  Connection closed during rollback - "
                                f"skipping rollback. Original error: {type(e).__name__}: {e}"
                            )
                        else:
                            logger.error(f"Rollback failed with connection error: {rollback_error}")
                    except Exception as rollback_error:
                        # Other rollback errors
                        logger.error(f"Rollback failed: {rollback_error}")
                    
                    logger.error(f"DB session error: {type(e).__name__}: {e}")
                    raise

                finally:
                    self._stats["active_sessions"] -= 1
                    try:
                        await session.close()
                    except Exception as close_error:
                        # Ignore errors when closing if connection is already closed
                        if "connection is closed" not in str(close_error).lower():
                            logger.warning(f"Error closing session: {close_error}")

        except HTTPException:
            # 🚨 HTTPException should propagate normally (not a DB error)
            # This can happen if HTTPException is raised during session creation
            self._stats["active_sessions"] -= 1
            raise  # Re-raise to let FastAPI handle it
        except TimeoutError as e:
            self._stats["failed_sessions"] += 1
            self._stats["active_sessions"] -= 1
            logger.error(f"DB connection timeout - database may be overloaded. Active sessions: {self._stats['active_sessions']}")
            raise
        except asyncio.TimeoutError as e:
            self._stats["failed_sessions"] += 1
            self._stats["active_sessions"] -= 1
            logger.error(f"DB connection timeout - database may be overloaded. Active sessions: {self._stats['active_sessions']}")
            raise
        except Exception as e:
            self._stats["failed_sessions"] += 1
            self._stats["active_sessions"] -= 1
            logger.error(f"DB session creation failed: {e}")
            raise

    # =====================================================
    # Health Check (NO ORM, NO TX)
    # =====================================================
    async def health_check(self) -> bool:
        if not self.engine:
            return False

        try:
            async with self.engine.connect() as conn:
                result = await conn.execute(text("SELECT 1"))
                return result.scalar_one() == 1

        except asyncio.CancelledError:
            raise

        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    # =====================================================
    # Optional retry helper (USE OUTSIDE SESSION)
    # =====================================================
    async def execute_with_retry(
        self,
        fn: Callable[[], Awaitable[T]],
        retries: int = 3,
        delay: float = 0.5,
    ) -> T:
        last_exc: Optional[Exception] = None

        for attempt in range(retries):
            try:
                return await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_exc = e
                if attempt == retries - 1:
                    break
                await asyncio.sleep(delay * (attempt + 1))

        raise last_exc  # type: ignore

    # =====================================================
    # Monitoring
    # =====================================================
    async def _log_pool_status(self) -> None:
        if not self.engine:
            return

        try:
            pool = self.engine.pool
            size = getattr(pool, "size", lambda: "n/a")()
            checked_in = getattr(pool, "checkedin", lambda: "n/a")()
            checked_out = getattr(pool, "checkedout", lambda: "n/a")()
            overflow = getattr(pool, "overflow", lambda: "n/a")()

            logger.info(
                "📊 DB Pool | size=%s checked_in=%s checked_out=%s overflow=%s",
                size, checked_in, checked_out, overflow
            )

            # Warn if pool is near capacity
            if checked_out != "n/a" and size != "n/a":
                if checked_out >= size * 0.8:
                    logger.warning(
                        f"⚠️  Connection pool is {checked_out}/{size} ({checked_out/size*100:.0f}%) - consider increasing pool_size"
                    )
        except Exception:
            pass

    def get_pool_status(self) -> dict:
        """Get current pool status"""
        if not self.engine:
            return {"status": "not_initialized"}

        try:
            pool = self.engine.pool
            return {
                "status": "initialized",
                "size": getattr(pool, "size", lambda: None)(),
                "checked_in": getattr(pool, "checkedin", lambda: None)(),
                "checked_out": getattr(pool, "checkedout", lambda: None)(),
                "overflow": getattr(pool, "overflow", lambda: None)(),
                "active_sessions": self._stats["active_sessions"],
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def get_stats(self) -> dict:
        return {
            **self._stats,
            "pool_size": settings.DB_POOL_SIZE,
            "max_overflow": settings.DB_MAX_OVERFLOW,
            "max_connections": settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW,
        }


# =====================================================
# Global instance
# =====================================================
db_manager = DatabaseManager()


# =====================================================
# FastAPI dependency
# =====================================================
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with db_manager.get_session() as session:
        yield session
