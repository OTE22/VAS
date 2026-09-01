"""The single place that decides what a chatbot security violation costs.

WHY THIS MODULE EXISTS
----------------------
It used to be three places, and they disagreed.

The detector (`sql_agent/tools/agent_tools.py`) wrote the sentence "Your account
has been blocked" into the user-facing response the moment a pattern matched. The
policy (a threshold of 3 violations in an hour) lived in the API layer. The two
were joined only by a string test on the *prose* the agent produced:

    if error_message.startswith("Security:")        # SSE and WebSocket

Nothing the agent emitted ever matched that: its security event begins
"SECURITY ALERT:" (different case) and its SQL errors are prefixed "SQL Error: ".
So on both streaming transports the policy was never invoked — the counter never
incremented and no account was ever blocked — while the user was told, on every
single attempt, that their account had been. Reproduced end to end before this
module existed: four consecutive attempts, "blocked" reported each time,
`is_active` still true, `blocked_at` still NULL, and the original JWT still
returning 200.

THE RULE THIS ENFORCES
----------------------
    "ACCOUNT_BLOCKED" shown to a user
            <=>  a committed blocked account in the database
            <=>  their existing credentials can no longer authorize work

A detector may say "this was a violation". Only this module may say what happened
to the account, and it says so only after the transaction commits.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Policy constants
# --------------------------------------------------------------------------
VIOLATION_THRESHOLD = 3
VIOLATION_WINDOW_SECONDS = 3600

# Deterministic, server-side reason codes. These — never model prose — are what
# reach `blocked_reason` and the audit trail, so the LLM cannot author security
# or audit content.
REASON_FORBIDDEN_SQL = "FORBIDDEN_SQL_ATTEMPT"
REASON_THRESHOLD_EXCEEDED = "SECURITY_POLICY_THRESHOLD_EXCEEDED"

# Outcomes. Exactly three, and they mean exactly what they say.
OUTCOME_DENIED = "QUERY_DENIED"
OUTCOME_BLOCKED = "ACCOUNT_BLOCKED"
OUTCOME_ENFORCEMENT_FAILED = "ENFORCEMENT_FAILED"

TRANSPORT_REST = "rest"
TRANSPORT_SSE = "sse"
TRANSPORT_WEBSOCKET = "websocket"

_DENIED_MESSAGE = ("That operation is not permitted — the assistant can only "
                   "read data. Repeated attempts will restrict your access.")
_BLOCKED_MESSAGE = ("Your access to the SQL assistant is restricted. "
                    "Please contact an administrator.")
# ENFORCEMENT_FAILED must not hint at internals, and must NOT imply a block:
# telling a user they are blocked when they are not is the bug this module was
# written to remove.
_FAILED_MESSAGE = ("That operation is not permitted — the assistant can only "
                   "read data.")


@dataclass
class SecurityDecision:
    """What the DETECTOR observed. Never what happened to the account.

    Produced by the agent/detector layer, consumed by `apply_security_policy`.
    It deliberately carries no account state and no user-facing verdict: those
    are the policy layer's to decide, and keeping them out of this object is
    what stops a detector from announcing a block again.
    """

    violation: bool = False
    action: str = "DENY"
    reason_code: str = REASON_FORBIDDEN_SQL
    reason: str = ""
    detail: Optional[str] = None       # sanitized, for the audit log only


@dataclass
class PolicyOutcome:
    """What the POLICY did. This is what a transport is allowed to serialize."""

    outcome: str
    message: str
    reference_id: str
    reason_code: str
    violations: int = 0
    blocked: bool = False
    close_connection: bool = False
    exempt: bool = False
    metadata: dict = field(default_factory=dict)

    @property
    def error_code(self) -> str:
        # ENFORCEMENT_FAILED is an OPERATIONAL state, not a user-facing one. The
        # user sees a plain denial; the alert goes to logs, audit and metrics.
        # Serializing it as ACCOUNT_BLOCKED would recreate the original bug.
        if self.outcome == OUTCOME_ENFORCEMENT_FAILED:
            return OUTCOME_DENIED
        return self.outcome


# --------------------------------------------------------------------------
# Metrics — reason_code and transport only.
#
# Never user id or username: unbounded label cardinality, and it would publish
# who tripped a security rule to anyone who can read /metrics.
#
# The point of `blocks_total` is precisely the failure this module fixes: when
# denials climb while blocks stay at zero, enforcement is disconnected again.
# --------------------------------------------------------------------------
try:
    from prometheus_client import Counter

    metrics_security_denials = Counter(
        "chatbot_security_denials_total",
        "Chatbot queries denied by security policy",
        ["reason_code", "transport"],
    )
    metrics_security_blocks = Counter(
        "chatbot_security_blocks_total",
        "Accounts blocked by chatbot security policy (committed to the database)",
        ["reason_code", "transport"],
    )
    metrics_security_block_failures = Counter(
        "chatbot_security_block_failures_total",
        "Blocks required by policy that could not be persisted",
        ["reason_code", "transport"],
    )
except Exception:                                              # pragma: no cover
    metrics_security_denials = None
    metrics_security_blocks = None
    metrics_security_block_failures = None


def _count(metric, reason_code: str, transport: str) -> None:
    if metric is None:
        return
    try:
        metric.labels(reason_code=reason_code, transport=transport).inc()
    except Exception:                                          # pragma: no cover
        pass


def _violation_key(user_id: int) -> str:
    return f"sec:violations:{user_id}"


async def record_violation(user_id: int) -> int:
    """Count one violation in the rolling window; return the running total.

    Backed by Redis so the threshold is shared across workers. The previous
    counter was a module-level dict, which made the effective threshold
    3 x WORKERS and the behaviour depend on which process answered.

    Reuses `_incr_with_window` from backend.auth.auth_security: a single INCR
    plus expire-on-first-increment. Never read-modify-write, which would let two
    concurrent requests both read 2 and both write 3.
    """
    from backend.auth.auth_security import _incr_with_window

    count, _ttl = await _incr_with_window(_violation_key(user_id),
                                          VIOLATION_WINDOW_SECONDS)
    return int(count)


async def reset_violations(user_id: int) -> None:
    """Clear the counter — used when an administrator restores access."""
    from backend.auth.auth_security import _reset_counter

    await _reset_counter(_violation_key(user_id))


def _is_exempt(user) -> tuple:
    """(exempt, why). Administrators and the system principal are never
    auto-blocked by a chatbot rule.

    An automated rule that can deactivate the last administrator locks everyone
    out of user administration — the exact condition `_guard_last_administrator`
    exists to prevent on the manual path.

    Exemption from AUTO-BLOCKING is not exemption from query policy: the query
    is still denied, and the event is still audited, at higher severity.
    """
    from backend.auth.capabilities import Role, canonical_role
    from backend.services.system_principal import is_system_principal

    if is_system_principal(user):
        return True, "system_principal"
    # The canonical helper, not `role == "admin"`: stored values have included
    # "Administrator" and " ADMIN ", which a bare comparison misses.
    if canonical_role(getattr(user, "role", None)) is Role.ADMIN:
        return True, "administrator"
    return False, ""


async def apply_security_policy(*, user, decision: SecurityDecision,
                                transport: str, query: str = "",
                                execution_time_ms: float = 0.0,
                                session_id=None,
                                attributable: bool = True) -> PolicyOutcome:
    """The one entry point. Every transport calls this and serializes the result.

    Returns a PolicyOutcome; raises nothing that a caller must interpret.
    """
    reference_id = f"SEC-{uuid.uuid4().hex[:8]}"
    reason_code = decision.reason_code or REASON_FORBIDDEN_SQL

    _count(metrics_security_denials, reason_code, transport)

    if user is None:
        return PolicyOutcome(
            outcome=OUTCOME_DENIED, message=_DENIED_MESSAGE,
            reference_id=reference_id, reason_code=reason_code,
        )

    user_id = getattr(user, "id", None)
    username = getattr(user, "username", "unknown")

    # Audit every denial, with the offending query — to the audit log, never to
    # the client.
    await _audit_denial(user_id, username, query, decision, reference_id,
                        execution_time_ms, session_id)

    exempt, why = _is_exempt(user)
    if exempt:
        logger.critical(
            "[SECURITY] severity=HIGH outcome=QUERY_DENIED exempt=%s user_id=%s "
            "username=%s reason_code=%s transport=%s reference_id=%s "
            "note=privileged account not auto-blocked; query denied and audited",
            why, user_id, username, reason_code, transport, reference_id,
        )
        return PolicyOutcome(
            outcome=OUTCOME_DENIED, message=_DENIED_MESSAGE,
            reference_id=reference_id, reason_code=reason_code,
            exempt=True, metadata={"exempt_reason": why},
        )

    if not attributable:
        # The guard refused SQL the MODEL wrote. Denied and audited exactly as
        # before, but no violation is recorded: blocking an account is a
        # statement about a PERSON, and a user who asked an ordinary question
        # did nothing. Same shape as the exempt branch above.
        logger.warning(
            "[SECURITY] outcome=QUERY_DENIED attributable=no user_id=%s "
            "reason_code=%s transport=%s reference_id=%s "
            "note=model-generated SQL; denied and audited, account not marked",
            user_id, reason_code, transport, reference_id)
        return PolicyOutcome(
            outcome=OUTCOME_DENIED, message=_DENIED_MESSAGE,
            reference_id=reference_id, reason_code=reason_code,
        )

    violations = await record_violation(user_id)
    logger.warning(
        "[SECURITY] outcome=QUERY_DENIED user_id=%s violations_in_window=%s "
        "threshold=%s transport=%s reference_id=%s reason_code=%s",
        user_id, violations, VIOLATION_THRESHOLD, transport, reference_id,
        reason_code,
    )

    if violations < VIOLATION_THRESHOLD:
        return PolicyOutcome(
            outcome=OUTCOME_DENIED, message=_DENIED_MESSAGE,
            reference_id=reference_id, reason_code=reason_code,
            violations=violations,
        )

    # Threshold reached. The account is blocked only if the write commits.
    blocked, already = await _persist_block(
        user_id=user_id, username=username, reference_id=reference_id,
        transport=transport,
    )

    if not blocked:
        _count(metrics_security_block_failures, REASON_THRESHOLD_EXCEEDED, transport)
        logger.critical(
            "[SECURITY] severity=CRITICAL outcome=ENFORCEMENT_FAILED user_id=%s "
            "username=%s reason_code=%s transport=%s reference_id=%s "
            "revocation_succeeded=false "
            "note=policy required a block but persistence failed; account REMAINS ACTIVE",
            user_id, username, REASON_THRESHOLD_EXCEEDED, transport, reference_id,
        )
        return PolicyOutcome(
            outcome=OUTCOME_ENFORCEMENT_FAILED, message=_FAILED_MESSAGE,
            reference_id=reference_id, reason_code=REASON_THRESHOLD_EXCEEDED,
            violations=violations,
        )

    if not already:
        _count(metrics_security_blocks, REASON_THRESHOLD_EXCEEDED, transport)
    logger.critical(
        "[SECURITY] severity=CRITICAL outcome=ACCOUNT_BLOCKED user_id=%s "
        "username=%s reason_code=%s trigger=%s threshold=%s window_seconds=%s "
        "transport=%s reference_id=%s actor=SYSTEM timestamp=%s "
        "revocation_succeeded=true already_blocked=%s",
        user_id, username, REASON_THRESHOLD_EXCEEDED, reason_code,
        VIOLATION_THRESHOLD, VIOLATION_WINDOW_SECONDS, transport, reference_id,
        datetime.utcnow().isoformat(), already,
    )
    return PolicyOutcome(
        outcome=OUTCOME_BLOCKED, message=_BLOCKED_MESSAGE,
        reference_id=reference_id, reason_code=REASON_THRESHOLD_EXCEEDED,
        violations=violations, blocked=True, close_connection=True,
        metadata={"already_blocked": already},
    )


async def _persist_block(*, user_id: int, username: str, reference_id: str,
                         transport: str) -> tuple:
    """Commit the block. Returns (succeeded, was_already_blocked).

    Idempotent: an account that is already inactive is left exactly as it was —
    the original `blocked_at` and reason stand, and `permissions_version` is not
    bumped again. Re-stamping would rewrite the moment of the first offence and
    manufacture a second "new block" event for what is one incident.

    `succeeded` is the honest result of the transaction. It is the only thing
    that may produce ACCOUNT_BLOCKED for the user.
    """
    from sqlalchemy import select

    from db_connection import db_manager
    from db_models import User
    from backend.services.user_service import UserService

    blocked_reason = (f"{REASON_THRESHOLD_EXCEEDED}: {VIOLATION_THRESHOLD} denied "
                      f"operations within "
                      f"{int(VIOLATION_WINDOW_SECONDS / 60)} minutes "
                      f"[{reference_id}]")
    try:
        async with db_manager.get_session() as db:
            user = (await db.execute(
                select(User).where(User.id == user_id))).scalar_one_or_none()
            if user is None:
                logger.error("[SECURITY] block target vanished user_id=%s", user_id)
                return False, False

            if not user.is_active:
                return True, True          # already blocked; nothing to rewrite

            old_authz = {
                "role": user.role,
                "can_use_chatbot": user.can_use_chatbot,
                "is_active": user.is_active,
                "pipeline_ids": await UserService._current_pipeline_ids(user_id, db),
            }

            # One transaction: the flag and its metadata are written together.
            # Split across transactions, a crash between them leaves an account
            # that is inactive with no recorded reason, or a reason with an
            # active account.
            user.is_active = False
            user.can_use_chatbot = False
            user.blocked_reason = blocked_reason
            user.blocked_at = datetime.utcnow()
            user.permissions_version = int(user.permissions_version or 1) + 1

            UserService._record_authorization_audit(
                db=db, user=user, old=old_authz,
                new={"role": user.role, "can_use_chatbot": False,
                     "is_active": False,
                     "pipeline_ids": old_authz["pipeline_ids"]},
                action="user_blocked", actor=None,
                context={"reason": blocked_reason, "request_id": reference_id},
            )
            await db.commit()
    except Exception as exc:                                   # noqa: BLE001
        # Deliberately NOT swallowed into a success. The old code logged and
        # then told the user they were blocked anyway.
        logger.critical(
            "[SECURITY] block persistence FAILED user_id=%s reference_id=%s "
            "error=%s", user_id, reference_id, type(exc).__name__, exc_info=True)
        return False, False

    # Best-effort only, and deliberately after the commit. Security does not
    # depend on this: enforcement is the is_active check on every request, which
    # reads the database. A cache that fails to evict cannot un-block anyone.
    try:
        UserService._invalidate_sql_agent_cache(user_id, "user_blocked")
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("[SECURITY] agent-cache eviction failed (block stands): %s", exc)

    return True, False


async def _audit_denial(user_id, username, query, decision: SecurityDecision,
                        reference_id: str, execution_time_ms: float,
                        session_id) -> None:
    """Record the denial. The rejected SQL goes here, never to the client."""
    try:
        from sql_agent.api.routes import log_chatbot_query

        await log_chatbot_query(
            user_id=user_id,
            username=username,
            query=query,
            response=None,
            success=False,
            error_message=(f"SECURITY [{reference_id}] {decision.reason_code}: "
                           f"{(decision.reason or '')[:300]}"),
            processing_time_ms=execution_time_ms,
            session_id=session_id,
        )
    except Exception as exc:                                   # noqa: BLE001
        logger.error("[SECURITY] audit write failed reference_id=%s: %s",
                     reference_id, exc)
