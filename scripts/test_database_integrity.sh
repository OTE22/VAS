#!/bin/bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
qa_work="$(mktemp -d /tmp/vas-normalization.XXXXXX)"
printf "Test evidence directory: %s\n" "$qa_work"
qa_db="vas-normalization-qa-$$"
trap 'if [ $? -ne 0 ]; then tail -45 "$qa_work/migration.log" 2>/dev/null || true; fi; docker rm -f "$qa_db" >/dev/null 2>&1 || true' EXIT
# Schema only: no production rows or secrets are copied.
docker exec face_detector_prod-postgres-1 pg_dump -U postgres --schema-only --no-owner --no-privileges face_recognition > "$qa_work/schema.sql"
docker run -d --name "$qa_db" --network none --tmpfs /var/lib/postgresql/data -e POSTGRES_PASSWORD=test -e POSTGRES_DB=normalization_qa pgvector/pgvector:pg15 >/dev/null
for attempt in {1..30}; do
 if docker exec "$qa_db" pg_isready -U postgres -d normalization_qa >/dev/null; then break; fi
 sleep 1
done
docker exec -i "$qa_db" psql -X -v ON_ERROR_STOP=1 -U postgres -d normalization_qa < "$qa_work/schema.sql" > "$qa_work/schema-restore.log"
qa_args=(--rm --network "container:$qa_db" --tmpfs /tmp -v "$repo_root:/app:ro" -w /app/alembic --entrypoint python -e DATABASE_URL=postgresql+asyncpg://postgres:test@127.0.0.1:5432/normalization_qa -e ENVIRONMENT=development -e PYTHONPATH=/app)
# pg_dump excludes the migration version row. Stamp only the isolated schema copy.
qa_revision=$(docker exec face_detector_prod-postgres-1 psql -X -At -U postgres -d face_recognition -c 'SELECT version_num FROM alembic_version')
docker run "${qa_args[@]}" face-detector/api:dev -m alembic -c alembic.ini stamp "$qa_revision" > "$qa_work/migration.log" 2>&1
docker run "${qa_args[@]}" face-detector/api:dev -m alembic -c alembic.ini upgrade head >> "$qa_work/migration.log" 2>&1
set +e
docker run --rm --network "container:$qa_db" --tmpfs /tmp -v "$repo_root:/app:ro" -w /app --entrypoint python -e DATABASE_URL=postgresql+asyncpg://postgres:test@127.0.0.1:5432/normalization_qa -e NORMALIZATION_TEST_DATABASE=1 -e ENVIRONMENT=development -e PYTHONPATH=/app face-detector/api:dev -m pytest -q --tb=short -rx -p no:cacheprovider tests/test_normalization_integrity.py tests/test_background_job_fixes.py tests/test_expiry_maintenance.py tests/test_related_identity_filters.py tests/test_migration_schema_parity.py::test_fresh_database_is_at_the_scripts_head_and_the_app_check_passes tests/test_migration_schema_parity.py::test_orm_tables_are_a_subset_of_the_fresh_database > "$qa_work/tests.log" 2>&1
result=$?
tail -70 "$qa_work/tests.log"
if [ "$result" -eq 0 ]; then
 docker run "${qa_args[@]}" face-detector/api:dev -m alembic -c alembic.ini downgrade "$qa_revision" >> "$qa_work/migration.log" 2>&1 || result=$?
 if [ "$result" -eq 0 ]; then
  docker run "${qa_args[@]}" face-detector/api:dev -m alembic -c alembic.ini upgrade head >> "$qa_work/migration.log" 2>&1 || result=$?
 fi
fi
exit "$result"
