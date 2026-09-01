"""Remove test-suite conversations that leaked into the real chat store.

Why this exists
---------------
`tests/test_sql_agent_streaming_session.py` persists a turn with
`session_id=None`; `record_exchange_for_session` then creates a brand-new
conversation per call, and the test's cleanup deletes only the
`user_query_history` row — so every run leaves one conversation titled
`cancel_survival_probe_query` in the FIRST ADMIN'S sidebar. Other suites
delete their probe users, whose conversations survive as orphans
(`conversations.user_id` is ON DELETE SET NULL) that workspace admins can
still see, badged "Deleted User".

What it deletes, and what it never deletes
------------------------------------------
Rows are selected ONLY by explicit test fingerprints: the literal titles and
legacy session ids the test files write (see FINGERPRINT_* below).
**Orphanhood is never a criterion.** Legitimately deleted users keep their
conversations by design (the tombstone behaviour proven in
tests/test_user_deletion_lifecycle.py), so `user_id IS NULL` may corroborate
a fingerprint match but can never establish one.

Deletion is HARD, and that is deliberate: branches, messages and feedback all
cascade (db_models.py — conversation_branches/messages/message_feedback are
ON DELETE CASCADE), agent_artifacts links are SET NULL, and nothing in the
system ever hard-deletes soft-deleted conversations, so a soft delete would
merely hide the rows forever.

Safety protocol
---------------
    python scripts/cleanup_test_conversations.py            # dry run: prints every row
    python scripts/cleanup_test_conversations.py --apply --expect N

  * The dry run prints, per conversation: id, user_id, title,
    legacy_session_id, created_at, branch count, message count.
  * --apply requires --expect N (the dry run's total). Inside ONE
    transaction it re-runs the exact same predicate; if the count differs
    from N, or any selected row no longer matches a fingerprint, the whole
    transaction is rolled back and nothing is deleted.

Run inside the api container:

    docker exec face_recognition_api python scripts/cleanup_test_conversations.py
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text as sa_text  # noqa: E402

# ---------------------------------------------------------------------------
# The fingerprints. Every entry is a LITERAL a test file writes — never a
# pattern, never "looks like test data". Sources:
#   tests/test_sql_agent_streaming_session.py:653   cancel_survival_probe_query
#   tests/test_conversation_domain.py               titles + session ids below
#   tests/test_user_deletion_lifecycle.py:182-206   qa deletion probe*
# ---------------------------------------------------------------------------
FINGERPRINT_TITLES = (
    "cancel_survival_probe_query",
    "A's private thread",
    "Private to A",
    "Renamed thread",
    "Persistence check",
    "Explicit target",
    "Branching",
    "A's target",
    "Feedback",
    "Uniquely Named Falcon Thread",
    "how many cameras?",
    "targeted question",
    "intrusion attempt",
    "qa deletion probe",
)
FINGERPRINT_SESSION_IDS = (
    "dualwrite_probe_session",
    "some_other_session",
    "b_fallback_session",
)
FINGERPRINT_HISTORY_QUERIES = (
    "cancel_survival_probe_query",
    "qa deletion probe query",
)

_SELECT_CONVERSATIONS = sa_text("""
    SELECT c.id, c.user_id, c.title, c.legacy_session_id, c.created_at,
           (SELECT count(*) FROM conversation_branches b
             WHERE b.conversation_id = c.id) AS branches,
           (SELECT count(*) FROM conversation_branches b
             JOIN messages m ON m.branch_id = b.id
             WHERE b.conversation_id = c.id) AS messages
    FROM conversations c
    WHERE c.title = ANY(:titles)
       OR c.legacy_session_id = ANY(:session_ids)
    ORDER BY c.created_at
""")

_SELECT_HISTORY = sa_text("""
    SELECT id, user_id, left(query_text, 60), query_timestamp
    FROM user_query_history
    WHERE query_text = ANY(:queries)
    ORDER BY query_timestamp
""")

# Stale session rows matter beyond tidiness: session_id is UNIQUE, so an
# orphaned row holding a test's fixed session id blocks every future run of
# that test from creating its session — the dual-write then fails with a
# UniqueViolation and the suite reports phantom regressions.
_SELECT_SESSIONS = sa_text("""
    SELECT session_id, user_id, started_at
    FROM user_conversation_sessions
    WHERE session_id = ANY(:session_ids)
    ORDER BY started_at
""")


def _matches_fingerprint(row) -> bool:
    """Re-verify one selected row against the fingerprints, in Python.

    The SQL predicate already guarantees this; checking again inside the
    apply transaction is the abort condition the protocol demands — if a row
    stopped matching between dry run and apply (a rename, a concurrent
    writer), the whole transaction is rolled back.
    """
    return (row[2] in FINGERPRINT_TITLES
            or (row[3] is not None and row[3] in FINGERPRINT_SESSION_IDS))


async def run(apply: bool, expect: int | None) -> int:
    from db_connection import db_manager
    await db_manager.init_db()

    params = {"titles": list(FINGERPRINT_TITLES),
              "session_ids": list(FINGERPRINT_SESSION_IDS)}

    async with db_manager.get_session() as db:
        rows = (await db.execute(_SELECT_CONVERSATIONS, params)).fetchall()
        history = (await db.execute(
            _SELECT_HISTORY, {"queries": list(FINGERPRINT_HISTORY_QUERIES)}
        )).fetchall()
        sessions = (await db.execute(
            _SELECT_SESSIONS,
            {"session_ids": list(FINGERPRINT_SESSION_IDS)})).fetchall()

        print(f"{'DRY RUN' if not apply else 'APPLY'} — conversations matching "
              f"test fingerprints: {len(rows)}")
        print(f"{'conversation id':38} {'user':>6} {'created':16} "
              f"{'br':>3} {'msg':>4}  {'session':22} title")
        for r in rows:
            print(f"{str(r[0]):38} {str(r[1]):>6} {str(r[4])[:16]:16} "
                  f"{r[5]:>3} {r[6]:>4}  {str(r[3])[:22]:22} {str(r[2])[:48]}")
        print(f"\nuser_query_history rows matching test fingerprints: {len(history)}")
        for r in history:
            print(f"  id={r[0]:6} user={str(r[1]):>6} {str(r[3])[:16]:16} {r[2]}")

        print(f"\nuser_conversation_sessions rows matching test session ids: "
              f"{len(sessions)}")
        for r in sessions:
            print(f"  session={r[0]:26} user={str(r[1]):>6} {str(r[2])[:16]}")

        total = len(rows) + len(history) + len(sessions)
        print(f"\nTOTAL rows that would be deleted: {total} "
              f"({len(rows)} conversations + {len(history)} history rows + "
              f"{len(sessions)} session rows; branches/messages/feedback "
              f"cascade with their conversation)")

        if not apply:
            print("\nDry run only. To delete: --apply --expect "
                  f"{total}")
            return 0

        # ------------------------------------------------------------------
        # APPLY: same predicate, one transaction, verified before deleting.
        # ------------------------------------------------------------------
        if expect is None:
            print("REFUSED: --apply requires --expect N (the dry run's total).")
            return 2
        if total != expect:
            print(f"ABORTED: selection is now {total} rows but --expect said "
                  f"{expect}. The data changed since the dry run — re-run the "
                  f"dry run and look again. Nothing was deleted.")
            return 3
        mismatched = [r for r in rows if not _matches_fingerprint(r)]
        if mismatched:
            print(f"ABORTED: {len(mismatched)} selected row(s) no longer match "
                  f"a fingerprint: {[str(r[0]) for r in mismatched]}. "
                  f"Nothing was deleted.")
            return 4

        deleted_conversations = (await db.execute(sa_text(
            "DELETE FROM conversations WHERE title = ANY(:titles) "
            "OR legacy_session_id = ANY(:session_ids)"), params)).rowcount
        deleted_history = (await db.execute(sa_text(
            "DELETE FROM user_query_history WHERE query_text = ANY(:queries)"),
            {"queries": list(FINGERPRINT_HISTORY_QUERIES)})).rowcount
        deleted_sessions = (await db.execute(sa_text(
            "DELETE FROM user_conversation_sessions "
            "WHERE session_id = ANY(:session_ids)"),
            {"session_ids": list(FINGERPRINT_SESSION_IDS)})).rowcount

        if (deleted_conversations != len(rows) or deleted_history != len(history)
                or deleted_sessions != len(sessions)):
            # The count moved between the SELECT and the DELETE inside this
            # same transaction — a concurrent writer. Refuse the surprise.
            await db.rollback()
            print(f"ABORTED: delete counts ({deleted_conversations} conv, "
                  f"{deleted_history} history, {deleted_sessions} sessions) "
                  f"differ from the verified selection ({len(rows)}, "
                  f"{len(history)}, {len(sessions)}). Rolled back.")
            return 5

        await db.commit()
        print(f"\nDeleted {deleted_conversations} conversations (cascading "
              f"branches/messages/feedback), {deleted_history} history rows "
              f"and {deleted_sessions} session rows.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default: dry run)")
    parser.add_argument("--expect", type=int, default=None,
                        help="the dry run's TOTAL row count; --apply aborts "
                             "if the live selection differs")
    args = parser.parse_args()
    return asyncio.run(run(apply=args.apply, expect=args.expect))


if __name__ == "__main__":
    sys.exit(main())
