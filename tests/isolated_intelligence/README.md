# Isolated intelligence hardening tests

These tests replace app startup/config imports with a small explicit harness. They are ignored during ordinary repository collection. Run them in a disposable container with:

- `ISOLATED_INTELLIGENCE_TESTS=1`
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`
- `PYTHONDONTWRITEBYTECODE=1`
- `python -m pytest --confcutdir=tests/isolated_intelligence -p no:cacheprovider tests/isolated_intelligence -q`

For database tests, provide a disposable pgvector PostgreSQL instance on an internal Docker network with hostname **intel-test-db**, and set `INTELLIGENCE_TEST_DATABASE_URL` to that test database. The harness refuses a different database host. It creates tables and truncates test job/threshold tables; never point it at a real application database. With no DSN, the database tests skip and pure algorithm/route checks still run. `--network none` can enforce isolation for that variant.

The test PostgreSQL image used in this run was `pgvector/pgvector:pg15`; the Python runner was the freshly built VAS worker image with pytest mounted separately. Only the test directory was overlaid for final runs; application modules came from the built image.
