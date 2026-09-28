import os

# Do not replace application modules during ordinary repository test collection.
if os.environ.get("ISOLATED_INTELLIGENCE_TESTS") != "1":
    collect_ignore_glob = ["test_*.py"]
else:
    """Isolated harness: never imports app startup or reads production configuration.

    Run with --confcutdir=tests/isolated_intelligence in a disposable container.
    Only INTELLIGENCE_TEST_DATABASE_URL is used; no default production DSN.
    """
    import os
    import sys
    import types
    from pathlib import Path
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import pytest
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy.pool import NullPool

    ROOT = Path(__file__).resolve().parents[2]
    for name, relative in [('backend', 'backend'), ('backend.core', 'backend/core'), ('backend.ml', 'backend/ml')]:
        module = types.ModuleType(name)
        module.__path__ = [str(ROOT / relative)]
        sys.modules[name] = module
    settings = SimpleNamespace(AUTO_THRESHOLD_LEARNING_ENABLED=True, TRAJECTORY_PREDICTION_ENABLED=True,
        ACTIVITY_CORRELATION_ENABLED=True, THRESHOLD_MIN_SAMPLES_FOR_ACTIVATION=10,
        MULTI_CAMERA_DISTANCE_METERS=500, MULTI_CAMERA_TIME_WINDOW_MINUTES=10,
        ML_JOB_LEASE_SECONDS=60, ML_WORKER_ID='isolated-test', INTEL_QUERY_TIMEOUT_SECONDS=5)
    config = types.ModuleType('config'); config.settings = settings; sys.modules['config'] = config
    url = os.environ.get('INTELLIGENCE_TEST_DATABASE_URL', '')
    if url and '@intel-test-db/' not in url:
        raise RuntimeError('This test suite only accepts the disposable intel-test-db host')
    engine = create_async_engine(url, poolclass=NullPool) if url else None
    sessions = async_sessionmaker(engine, expire_on_commit=False) if engine else None
    @asynccontextmanager
    async def session():
        if not sessions:
            raise RuntimeError('Disposable database required')
        async with sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
    manager = SimpleNamespace(get_session=session, _initialized=True)
    connection = types.ModuleType('db_connection'); connection.db_manager = manager
    connection.get_db = session
    sys.modules['db_connection'] = connection

    @pytest.fixture(autouse=True)
    def reset_flags():
        original = vars(settings).copy()
        yield
        settings.__dict__.clear(); settings.__dict__.update(original)
