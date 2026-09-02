"""
AST-based SQL validation for LLM-generated queries.

    docker exec face_recognition_api python -m pytest tests/test_sql_guard.py -v

Two regressions are pinned here, both verified as working exploits against the
live database before the guard existed:

  1. `BEGIN READ WRITE; DELETE FROM identities ...`
     The connection sets default_transaction_read_only=on, but that is a
     session *default*, not a constraint — an explicit BEGIN overrides it. The
     old regex denylist matched \\bBEGIN\\b on uppercased text yet the naive
     `;` split did not treat this as two statements.

  2. `SELECT username, password_hash, role FROM users`
     A pure SELECT passes every regex in the old validator, and there was no
     table allowlist. This returned admin's bcrypt hash.

Pure unit tests — no database, no LLM.
"""

import pytest

from sql_agent.security import SqlPolicy, SqlPolicyError, validate_sql

SCHEMA_TABLES = ["faces", "detections", "pipelines", "system_metrics"]


@pytest.fixture
def policy():
    return SqlPolicy.for_tables(SCHEMA_TABLES)


def allowed(sql, policy):
    return validate_sql(sql, policy).allowed


# ------------------------------------------------------- proven exploits

def test_read_only_bypass_via_explicit_begin_is_blocked(policy):
    verdict = validate_sql(
        "BEGIN READ WRITE; DELETE FROM identities WHERE 1=0", policy
    )
    assert verdict.allowed is False
    assert verdict.code == "MULTIPLE_STATEMENTS"


def test_password_hash_exfiltration_is_blocked(policy):
    verdict = validate_sql(
        "SELECT username, password_hash, role FROM users", policy
    )
    assert verdict.allowed is False
    assert verdict.code == "TABLE_NOT_ALLOWED"
    assert "users" in verdict.reason


def test_rejection_does_not_disclose_the_allowlist(policy):
    """Echoing permitted tables back would map the schema for an attacker."""
    verdict = validate_sql("SELECT * FROM users", policy)
    for table in SCHEMA_TABLES:
        assert table not in verdict.reason


# --------------------------------------------------------- write blocking

@pytest.mark.parametrize("sql", [
    "DELETE FROM faces",
    "UPDATE faces SET name = 'x'",
    "INSERT INTO faces (name) VALUES ('x')",
    "DROP TABLE faces",
    "ALTER TABLE faces ADD COLUMN x int",
    "TRUNCATE faces",
    "CREATE TABLE evil (id int)",
])
def test_writes_are_blocked(sql, policy):
    assert allowed(sql, policy) is False


def test_write_nested_in_a_cte_is_blocked(policy):
    """Postgres allows DML inside a CTE; a top-level type check alone misses it."""
    verdict = validate_sql(
        "WITH gone AS (DELETE FROM faces RETURNING *) SELECT * FROM gone", policy
    )
    assert verdict.allowed is False
    assert verdict.code == "READ_ONLY_VIOLATION"


@pytest.mark.parametrize("sql", [
    "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE",
    "COMMIT",
    "CALL some_procedure()",
    "VACUUM FULL",
    "COPY faces TO '/tmp/out.csv'",
])
def test_unanalysable_statements_are_refused(sql, policy):
    """Anything the parser cannot model is unanalysable, so it is never run."""
    assert allowed(sql, policy) is False


# ------------------------------------------------------- table allowlist

@pytest.mark.parametrize("table", ["users", "chatbot_audit_log", "identities", "api_keys"])
def test_tables_outside_the_schema_are_blocked(table, policy):
    assert allowed(f"SELECT * FROM {table}", policy) is False


def test_allowed_tables_pass(policy):
    assert allowed("SELECT name FROM faces", policy) is True


def test_join_across_allowed_tables_passes(policy):
    assert allowed(
        "SELECT f.name, d.timestamp FROM faces f "
        "JOIN detections d ON d.id = f.detection_id", policy
    ) is True


def test_one_disallowed_table_in_a_join_blocks_the_whole_query(policy):
    assert allowed(
        "SELECT f.name, u.password_hash FROM faces f JOIN users u ON u.id = f.id",
        policy,
    ) is False


def test_disallowed_table_in_a_subquery_is_blocked(policy):
    assert allowed(
        "SELECT name FROM faces WHERE id IN (SELECT id FROM users)", policy
    ) is False


def test_cte_names_are_not_mistaken_for_tables(policy):
    """A CTE alias is not a physical table and must not trip the allowlist."""
    assert allowed(
        "WITH recent AS (SELECT * FROM faces LIMIT 10) SELECT * FROM recent", policy
    ) is True


# ------------------------------------------------------- system access

@pytest.mark.parametrize("sql", [
    "SELECT * FROM pg_catalog.pg_shadow",
    "SELECT * FROM information_schema.tables",
])
def test_system_schemas_are_blocked(sql, policy):
    verdict = validate_sql(sql, policy)
    assert verdict.allowed is False
    assert verdict.code == "SYSTEM_SCHEMA"


@pytest.mark.parametrize("sql", [
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_sleep(60)",
    "SELECT lo_import('/etc/shadow')",
    "SELECT dblink('host=evil', 'SELECT 1')",
])
def test_dangerous_functions_are_blocked(sql, policy):
    verdict = validate_sql(sql, policy)
    assert verdict.allowed is False
    assert verdict.code in ("FORBIDDEN_FUNCTION", "TABLE_NOT_ALLOWED")


# ------------------------------------------------- no false positives

@pytest.mark.parametrize("sql", [
    "SELECT name FROM faces WHERE name = 'Update Smith'",
    "SELECT name FROM faces WHERE name LIKE '%delete%'",
    "SELECT name AS drop_off FROM faces",
])
def test_write_keywords_inside_string_literals_and_aliases_are_fine(sql, policy):
    """The old regex matched \\bDELETE\\b anywhere in the uppercased text, so
    ordinary questions were rejected. Structure, not substrings."""
    assert allowed(sql, policy) is True


# ----------------------------------------------------------- row limit

def test_limit_is_injected_when_absent(policy):
    verdict = validate_sql("SELECT name FROM faces", policy)
    assert verdict.allowed
    assert "LIMIT" in verdict.sql.upper()


def test_oversized_limit_is_reduced(policy):
    verdict = validate_sql("SELECT name FROM faces LIMIT 100000", policy)
    assert verdict.allowed
    assert "100000" not in verdict.sql


def test_small_limit_is_preserved(policy):
    verdict = validate_sql("SELECT name FROM faces LIMIT 5", policy)
    assert verdict.allowed
    assert "5" in verdict.sql


# ----------------------------------------------------------- fail closed

def test_unparseable_sql_is_refused(policy):
    verdict = validate_sql("SELECT FROM WHERE ((((", policy)
    assert verdict.allowed is False


@pytest.mark.parametrize("sql", ["", "   ", None])
def test_empty_sql_is_refused(sql, policy):
    assert validate_sql(sql, policy).allowed is False


def test_parser_missing_fails_closed(monkeypatch, policy):
    """Without a parser the guard must refuse, never fall back to regex."""
    import sql_agent.security.sql_guard as guard

    monkeypatch.setattr(guard, "SQLGLOT_AVAILABLE", False)
    verdict = guard.validate_sql("SELECT name FROM faces", policy)
    assert verdict.allowed is False
    assert verdict.code == "PARSER_UNAVAILABLE"


def test_policy_requires_at_least_one_table():
    with pytest.raises(SqlPolicyError):
        SqlPolicy.for_tables([])


# ----------------------------------------------------------- complexity

def test_excessive_joins_are_blocked():
    policy = SqlPolicy.for_tables(SCHEMA_TABLES, max_joins=2)
    sql = ("SELECT 1 FROM faces a "
           "JOIN detections b ON b.id=a.id "
           "JOIN pipelines c ON c.id=b.id "
           "JOIN system_metrics d ON d.id=c.id")
    assert allowed(sql, policy) is False


# ------------------------------------------------- wired into execution

def test_database_manager_builds_its_policy_from_the_known_schema():
    """Allowlist and advertised schema must not drift apart."""
    from sql_agent.config import config
    from sql_agent.database import DatabaseManager

    db = DatabaseManager(config)
    assert db.sql_policy.allowed_tables == frozenset(DatabaseManager.KNOWN_SCHEMA["tables"])
    assert "users" not in db.sql_policy.allowed_tables


def test_execute_query_uses_the_ast_guard_without_a_regex_gate():
    import inspect

    from sql_agent.database import DatabaseManager

    source = inspect.getsource(DatabaseManager.execute_query)
    assert "validate_sql" in source
    assert "_validate_query" not in source
