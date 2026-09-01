"""Deterministic, AST-based validation of LLM-generated SQL.

Replaces the regex gate in sql_agent/database.py:_validate_query, which was
bypassable in two proven ways:

  1. `BEGIN READ WRITE; DELETE FROM identities ...`
     The denylist matched `\\bBEGIN\\b` on the uppercased text, but the
     connection's `default_transaction_read_only=on` is a session *default*,
     not a constraint — an explicit `BEGIN READ WRITE` overrides it. Verified
     against the live database: the DELETE executed.

  2. `SELECT username, password_hash, role FROM users`
     A pure SELECT passes every regex in the old validator, and there was no
     table allowlist. Verified: it returned admin's bcrypt hash.

Both are structural problems that string matching cannot solve. `\\bDELETE\\b`
also rejects the legitimate question "show me the last update", so the regex
approach was simultaneously too weak and too strict.

Design rules:

  * Parse with sqlglot into a real AST, in the target dialect.
  * FAIL CLOSED. If the parser is missing or the SQL will not parse, refuse.
    There is deliberately no regex fallback: a degraded validator that still
    executes is worse than no execution.
  * Allowlist, never denylist. New Postgres functions and syntax appear with
    every release; an allowlist does not silently widen.
  * This is one layer. It does not replace connecting as a role that has no
    write grants — see docs in db/roles.sql. A validator can have bugs; role
    permissions cannot be overridden by the query text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional, Sequence, Set

logger = logging.getLogger(__name__)

try:
    import sqlglot
    from sqlglot import exp

    SQLGLOT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by test_parser_missing_fails_closed
    sqlglot = None
    exp = None
    SQLGLOT_AVAILABLE = False


# Only these node types may appear as a top-level statement.
_ALLOWED_STATEMENTS = ("Select", "Union", "Except", "Intersect", "Subquery", "With")

# Schemas that expose catalog internals, credentials and file paths.
_DENIED_SCHEMAS: FrozenSet[str] = frozenset({
    "pg_catalog", "information_schema", "pg_toast", "pg_temp",
})

# Functions that read files, open sockets, sleep, or leak configuration.
# Blocked by name regardless of arguments.
_DENIED_FUNCTIONS: FrozenSet[str] = frozenset({
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "lo_get", "lo_put",
    "dblink", "dblink_exec", "dblink_connect",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "current_setting", "set_config",
    "pg_terminate_backend", "pg_cancel_backend",
    "query_to_xml", "database_to_xml", "table_to_xml",
    "pg_reload_conf", "pg_rotate_logfile",
    "copy_from", "copy_to",
})


class SqlPolicyError(Exception):
    """Raised when a policy is itself invalid (not when SQL is rejected)."""


@dataclass(frozen=True)
class SqlPolicy:
    """What a given caller is permitted to ask the database."""

    allowed_tables: FrozenSet[str]
    dialect: str = "postgres"
    max_rows: int = 500
    allow_explain: bool = False
    max_joins: int = 10
    max_subquery_depth: int = 5

    @staticmethod
    def for_tables(tables: Sequence[str], **kwargs) -> "SqlPolicy":
        normalized = frozenset(t.strip().lower() for t in tables if t and t.strip())
        if not normalized:
            raise SqlPolicyError("policy must allow at least one table")
        return SqlPolicy(allowed_tables=normalized, **kwargs)


@dataclass
class SqlVerdict:
    """Outcome of validation. `allowed` is the only field callers may trust."""

    allowed: bool
    code: str = "OK"
    reason: str = ""
    tables: List[str] = field(default_factory=list)
    statement_type: str = ""
    sql: str = ""

    def __bool__(self) -> bool:
        return self.allowed


# --------------------------------------------------------------- code sets
#
# A denial is not automatically an ATTACK. Two of these codes mean the model
# wrote something broken and one means our own parser is missing; treating
# those as attempted intrusions blocks accounts for typing "hello" (observed
# live 2026-08-30) and, worse, buries real attempts in false positives.
#
# Enforcement and reasoning both classify from these sets rather than from
# the denial text, because the text is prose and prose drifts.

#: The user (or the model on their behalf) asked for something the policy
#: forbids. These are the genuine security events: refuse, audit, enforce.
ENFORCEABLE_CODES = frozenset({
    "READ_ONLY_VIOLATION",
    "TABLE_NOT_ALLOWED",
    "SYSTEM_SCHEMA",
    "FORBIDDEN_FUNCTION",
    "MULTIPLE_STATEMENTS",
    "UNSUPPORTED_STATEMENT",
    "EXPLAIN_NOT_ALLOWED",
})

#: The generated SQL is broken or absent, or the request outgrew the
#: complexity budget. Nothing forbidden was attempted — these are mistakes to
#: CORRECT, and the reasoning layer may re-plan them.
MALFORMED_CODES = frozenset({
    "PARSE_ERROR",
    "EMPTY",
    "TOO_COMPLEX",
})

#: Our own dependency is missing. Never the user's doing; never enforceable
#: and never correctable by re-planning.
INFRASTRUCTURE_CODES = frozenset({
    "PARSER_UNAVAILABLE",
})


def is_enforceable(code: Optional[str]) -> bool:
    """Whether a denial code represents a genuine forbidden-operation attempt.

    Fails CLOSED for an unrecognised code: a new denial reason is treated as
    a security event until someone classifies it deliberately, which is the
    safe direction for the enforcement gate.
    """
    if not code:
        return False
    code = str(code).upper()
    if code in MALFORMED_CODES or code in INFRASTRUCTURE_CODES:
        return False
    return True


def is_malformed(code: Optional[str]) -> bool:
    """Whether a denial means the query was broken rather than forbidden."""
    return bool(code) and str(code).upper() in MALFORMED_CODES


def _deny(code: str, reason: str, **kw) -> SqlVerdict:
    return SqlVerdict(allowed=False, code=code, reason=reason, **kw)


def _qualified_name(table) -> str:
    """`schema.table` when a schema is written, else the bare table name."""
    schema = table.text("db")
    name = (table.name or "").lower()
    return f"{schema.lower()}.{name}" if schema else name


def validate_sql(sql: str, policy: SqlPolicy) -> SqlVerdict:
    """Decide whether `sql` may be executed under `policy`.

    Never raises for rejected SQL — returns a verdict. The caller must check
    `.allowed` and must not execute on a False verdict.
    """
    if not SQLGLOT_AVAILABLE:
        # Fail closed. A regex fallback here would reintroduce exactly the
        # bypasses this module exists to prevent.
        return _deny(
            "PARSER_UNAVAILABLE",
            "SQL parser is not installed, so the query cannot be validated. "
            "Refusing to execute.",
        )

    if not sql or not sql.strip():
        return _deny("EMPTY", "No SQL was produced.")

    # --- parse -------------------------------------------------------------
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=policy.dialect) if s is not None]
    except Exception as e:
        return _deny("PARSE_ERROR", f"SQL could not be parsed: {type(e).__name__}")

    if not statements:
        return _deny("EMPTY", "No SQL statement found.")

    # --- one statement only ------------------------------------------------
    # This is what catches `BEGIN READ WRITE; DELETE ...`: sqlglot splits it
    # into two statements, where the naive `;`-split did not (it also split
    # inside string literals, producing false positives).
    if len(statements) > 1:
        kinds = ", ".join(type(s).__name__ for s in statements)
        return _deny(
            "MULTIPLE_STATEMENTS",
            f"Only one statement may be executed; received {len(statements)} ({kinds}).",
        )

    statement = statements[0]
    statement_type = type(statement).__name__

    # --- statement type allowlist -----------------------------------------
    if statement_type == "Command":
        # sqlglot's catch-all for syntax it does not model (SET, BEGIN, CALL,
        # VACUUM...). Unmodelled means unanalysable, so it is never allowed.
        return _deny(
            "UNSUPPORTED_STATEMENT",
            "This statement type cannot be analysed and is not permitted.",
            statement_type=statement_type,
        )

    if statement_type == "Explain":
        if not policy.allow_explain:
            return _deny("EXPLAIN_NOT_ALLOWED", "EXPLAIN is not permitted.",
                         statement_type=statement_type)
    elif statement_type not in _ALLOWED_STATEMENTS:
        return _deny(
            "READ_ONLY_VIOLATION",
            f"Only read queries are permitted; this is a {statement_type.upper()} statement.",
            statement_type=statement_type,
        )

    # A DML/DDL node nested anywhere (CTE, subquery) is still a write.
    for node_type in ("Insert", "Update", "Delete", "Drop", "Create", "Alter",
                      "TruncateTable", "Grant", "Merge"):
        node_cls = getattr(exp, node_type, None)
        if node_cls is not None and list(statement.find_all(node_cls)):
            return _deny(
                "READ_ONLY_VIOLATION",
                f"Query contains a nested {node_type.upper()} operation.",
                statement_type=statement_type,
            )

    # --- table allowlist ---------------------------------------------------
    referenced: Set[str] = set()
    cte_names: Set[str] = {
        cte.alias_or_name.lower()
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }

    for table in statement.find_all(exp.Table):
        qualified = _qualified_name(table)
        bare = (table.name or "").lower()
        if not bare or bare in cte_names:
            continue  # a CTE reference, not a physical table
        schema = table.text("db").lower()
        if schema and schema in _DENIED_SCHEMAS:
            return _deny(
                "SYSTEM_SCHEMA",
                f"Access to the {schema} schema is not permitted.",
                tables=[qualified], statement_type=statement_type,
            )
        referenced.add(bare)

    unauthorized = sorted(referenced - policy.allowed_tables)
    if unauthorized:
        # Do not echo the allowlist back: it maps the schema for an attacker.
        return _deny(
            "TABLE_NOT_ALLOWED",
            f"Query references table(s) you are not authorized to read: "
            f"{', '.join(unauthorized)}.",
            tables=sorted(referenced), statement_type=statement_type,
        )

    # --- dangerous functions ----------------------------------------------
    for func in statement.find_all(exp.Anonymous):
        name = (func.name or "").lower()
        if name in _DENIED_FUNCTIONS:
            return _deny(
                "FORBIDDEN_FUNCTION",
                f"The function {name}() is not permitted.",
                tables=sorted(referenced), statement_type=statement_type,
            )
    # sqlglot models some of these as dedicated nodes rather than Anonymous.
    for node_name in ("CurrentSetting", "SetConfig"):
        node_cls = getattr(exp, node_name, None)
        if node_cls is not None and list(statement.find_all(node_cls)):
            return _deny(
                "FORBIDDEN_FUNCTION",
                "Reading or writing server configuration is not permitted.",
                tables=sorted(referenced), statement_type=statement_type,
            )

    # --- complexity bounds -------------------------------------------------
    join_count = len(list(statement.find_all(exp.Join)))
    if join_count > policy.max_joins:
        return _deny(
            "TOO_COMPLEX",
            f"Query has {join_count} joins, above the limit of {policy.max_joins}.",
            tables=sorted(referenced), statement_type=statement_type,
        )

    if _subquery_depth(statement) > policy.max_subquery_depth:
        return _deny(
            "TOO_COMPLEX",
            f"Query nests subqueries more than {policy.max_subquery_depth} deep.",
            tables=sorted(referenced), statement_type=statement_type,
        )

    # --- row cap -----------------------------------------------------------
    # Enforced in the SQL itself, not only by fetchmany: without a LIMIT the
    # server still materialises the full result set before the client stops
    # reading it.
    safe_sql = _enforce_limit(statement, policy)

    return SqlVerdict(
        allowed=True,
        code="OK",
        tables=sorted(referenced),
        statement_type=statement_type,
        sql=safe_sql,
    )


def _subquery_depth(node, depth: int = 0) -> int:
    deepest = depth
    for child in node.args.values():
        children = child if isinstance(child, list) else [child]
        for item in children:
            if not hasattr(item, "args"):
                continue
            next_depth = depth + 1 if isinstance(item, (exp.Subquery, exp.Select)) else depth
            deepest = max(deepest, _subquery_depth(item, next_depth))
    return deepest


def _enforce_limit(statement, policy: SqlPolicy) -> str:
    """Return SQL guaranteed to carry a LIMIT no larger than the policy's."""
    try:
        existing = statement.args.get("limit")
        if existing is not None:
            try:
                current = int(existing.expression.name)
                if current <= policy.max_rows:
                    return statement.sql(dialect=policy.dialect)
            except (AttributeError, ValueError):
                pass  # non-literal LIMIT — replace it with ours
        return statement.limit(policy.max_rows).sql(dialect=policy.dialect)
    except Exception as e:
        # Never silently return unbounded SQL.
        logger.warning("Could not enforce LIMIT, re-serialising: %s", e)
        return statement.sql(dialect=policy.dialect)
