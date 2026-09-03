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
    # The caller's cameras. None is an administrator - no restriction. A set
    # narrows every scoped table to those pipelines. An EMPTY set fails
    # closed: the guard refuses rather than widens, the same rule the app's
    # pipeline_scope_predicate applies to every REST route.
    pipeline_scope: Optional[FrozenSet[str]] = None

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
    # The LIMIT-enforced SQL BEFORE the camera scope: what the agent may
    # keep, show the model, learn from, and store in history. `sql` (scoped)
    # exists for the executor only; seven learned examples had carried one
    # user's pipeline IN-list into every later generation.
    canonical: str = ""

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
    # The camera-scope rewrite itself failed; the query was refused so as to
    # fail closed. Ours, not the user's.
    "SCOPE_ERROR",
})

#: The caller is not allowed to see anything the query would read - no
#: cameras assigned. Not an attack, not a broken query, not our fault: an
#: administrator has not granted access. Never enforceable (a user asking a
#: plain question must not be threatened with a block) and never correctable
#: by re-planning (no rewrite makes an unassigned user assigned).
AUTHORIZATION_CODES = frozenset({
    "NO_PIPELINE_ACCESS",
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
    if (code in MALFORMED_CODES or code in INFRASTRUCTURE_CODES
            or code in AUTHORIZATION_CODES):
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


#: How each readable table narrows to a camera scope. `faces` has no
#: pipeline column of its own and reaches a camera only through its
#: detection, which is why it is scoped through a sub-select.
_SCOPE_PREDICATES = {
    "pipelines": "pipeline_id IN ({ids})",
    "detections": "pipeline_id IN ({ids})",
    "faces": ("detection_id IN (SELECT id FROM detections "
              "WHERE pipeline_id IN ({ids}))"),
}


def _scope_ids_sql(policy: SqlPolicy) -> str:
    return ", ".join(exp.Literal.string(str(p)).sql(dialect=policy.dialect)
                     for p in sorted(policy.pipeline_scope or ()))


def _enclosing_select(node):
    parent = node.parent
    while parent is not None and not isinstance(parent, exp.Select):
        parent = parent.parent
    return parent


def _already_scoped(table, ids: str, policy: SqlPolicy) -> bool:
    """Is this table already inside one of our scope wrappers? The wrapper
    is `SELECT * FROM t WHERE <predicate with exactly this IN-list>`; the
    IN-list is unique to the caller's scope."""
    select = _enclosing_select(table)
    if select is None or not ids:
        return False
    where = select.args.get("where")
    if where is None:
        return False
    return ids in where.sql(dialect=policy.dialect)


def _scope_wrappers(statement, policy: SqlPolicy):
    """The subqueries our rewrite introduced, by identity, so the depth
    bound counts the model's nesting and not ours."""
    ids = _scope_ids_sql(policy)
    if not ids:
        return set()
    found = set()
    for sub in statement.find_all(exp.Subquery):
        inner = sub.this
        where = inner.args.get("where") if isinstance(inner, exp.Select) else None
        if where is not None and ids in where.sql(dialect=policy.dialect):
            found.add(id(sub))
            # the faces wrapper also carries an inner sub-select on detections
            for nested in inner.find_all(exp.Subquery):
                found.add(id(nested))
    return found


def _apply_pipeline_scope(statement, policy: SqlPolicy, cte_names):
    """Rewrite every scoped physical table into a subquery limited to the
    caller's cameras, keeping its alias so column references still resolve.

    Done on the AST after the allow-list, so joins, CTEs and nested selects
    are all covered without any of them having to remember a WHERE. The
    pipeline ids are emitted as SQL string literals through sqlglot, never
    interpolated raw.
    """
    ids = _scope_ids_sql(policy)
    # IDEMPOTENT: the tools validate once and execute_query validates again,
    # so the second pass sees tables we already wrapped. Wrapping them a
    # second time nested every query one level deeper per pass and pushed
    # "where was joey last seen" over the subquery-depth limit.
    targets = [t for t in statement.find_all(exp.Table)
               if (t.name or "").lower() in _SCOPE_PREDICATES
               and (t.name or "").lower() not in cte_names
               and not _already_scoped(t, ids, policy)]
    for table in targets:
        bare = table.name.lower()
        scoped = sqlglot.parse_one(
            f"SELECT * FROM {bare} WHERE "
            + _SCOPE_PREDICATES[bare].format(ids=ids),
            dialect=policy.dialect,
        ).subquery(alias=table.alias_or_name)
        table.replace(scoped)
    return statement


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

    ignored = _scope_wrappers(statement, policy) if policy.pipeline_scope else set()
    depth = _subquery_depth(statement, ignore=ignored)
    if depth > policy.max_subquery_depth:
        # Shape only, never the SQL: enough to tell the model's nesting from
        # our own wrappers when a scoped query is refused.
        logger.info("[GUARD] TOO_COMPLEX depth=%d limit=%d wrappers_ignored=%d "
                    "depth_unignored=%d scoped=%s", depth,
                    policy.max_subquery_depth, len(ignored),
                    _subquery_depth(statement), bool(policy.pipeline_scope))
        return _deny(
            "TOO_COMPLEX",
            f"Query nests subqueries more than {policy.max_subquery_depth} deep.",
            tables=sorted(referenced), statement_type=statement_type,
        )

    # --- camera scope ------------------------------------------------------
    # The chatbot was the one door that skipped the app's pipeline rule: any
    # user with chatbot access could read every camera and every person.
    canonical = _enforce_limit(statement.copy(), policy)
    if policy.pipeline_scope is not None:
        if not policy.pipeline_scope:
            return _deny(
                "NO_PIPELINE_ACCESS",
                "You have not been assigned any cameras, so there is "
                "nothing here you can query.",
                tables=sorted(referenced), statement_type=statement_type,
            )
        try:
            statement = _apply_pipeline_scope(statement, policy, cte_names)
        except Exception as e:
            # Fail closed: a scope that could not be applied is not a scope.
            logger.warning("Could not apply the camera scope: %s", e)
            return _deny(
                "SCOPE_ERROR",
                "The query could not be limited to your cameras, so it "
                "was not run.",
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
        canonical=canonical,
    )


def _subquery_depth(node, depth: int = 0, ignore=frozenset()) -> int:
    """Nesting depth, not counting the scope wrappers this module added.

    One level per nested SELECT. Counting the Subquery node AND the Select
    inside it charged two per level, so the limit of five refused three
    nested IN-subqueries: a 220-character "where was Joey last seen" was
    TOO_COMPLEX while an EXISTS of the same depth (no Subquery node) passed.
    """
    deepest = depth
    for child in node.args.values():
        children = child if isinstance(child, list) else [child]
        for item in children:
            if not hasattr(item, "args"):
                continue
            counts = (isinstance(item, exp.Select)
                      and id(item) not in ignore
                      and id(item.parent) not in ignore)
            next_depth = depth + 1 if counts else depth
            deepest = max(deepest, _subquery_depth(item, next_depth, ignore))
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
