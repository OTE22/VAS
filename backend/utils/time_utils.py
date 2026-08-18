"""Canonical wall-clock helpers for the naive-UTC storage convention.

STORAGE CONVENTION — READ THIS BEFORE "FIXING" ANYTHING HERE
============================================================
Every DateTime column in db_models.py is timezone-NAIVE and stores UTC.
That is a deliberate, documented contract (.env.example's DEFAULT_SITE_TIMEZONE
note, backend/core/time_context.py), not an accident:

  * asyncpg binds naive datetimes against TIMESTAMP WITHOUT TIME ZONE;
  * ~45 call sites compare naive datetime.utcnow() against column values;
  * 15 columns carry onupdate=datetime.utcnow;
  * backend.core.time_context.to_local() contracts on naive-UTC input
    (it applies tzinfo itself and would double-shift an aware value).

Migrating 126 columns to DateTime(timezone=True) would touch every binding
and comparison for zero behavioural gain. Columns intentionally STAY naive —
which is precisely why iso_utc() below may treat a naive datetime as UTC:
in THIS repository a naive datetime IS UTC by definition. Do not copy that
assumption into codebases without the same contract.

WIRE CONVENTION
===============
Anything serialized for a browser / REST / WebSocket consumer must be
timezone-aware ISO-8601 ending in "Z". Route every outgoing datetime through
iso_utc(). Never call bare .isoformat() at a wire producer, and never
hand-append "Z" to a possibly-aware datetime — that yields the invalid
"...+00:00Z".

THE RULE
========
  * DB-bound / comparison values: keep datetime.utcnow() (naive).
  * Wire-bound values: iso_utc(db_value); iso_utc(utc_now()) for "now".
"""

from datetime import datetime, timezone
from typing import Optional

__all__ = ["utc_now", "iso_utc"]


def utc_now() -> datetime:
    """Aware UTC now — ONLY for values that go to the wire via iso_utc().

    Never store or compare this against database values: columns are naive,
    and asyncpg/psycopg refuse (or worse, silently mangle) aware bindings.
    """
    return datetime.now(timezone.utc)


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """A datetime as a UTC ISO-8601 string ending in "Z"; None passes through.

    Accepts ONLY datetime or None. Anything else — including datetime.date —
    raises TypeError rather than silently stringifying: a date-only value must
    stay a date string ("2026-08-03"), never become a fabricated midnight-UTC
    timestamp, and a str is either already serialized or not a timestamp at
    all. The caller decides; this helper refuses to guess.

    A NAIVE input is treated as UTC **solely because this repository defines
    database timestamps as naive UTC** (see module docstring). An AWARE input
    is converted with astimezone() — never .replace(tzinfo=...), which would
    relabel the wall-clock reading and shift the instant. The instant is
    preserved either way; microseconds are preserved; the output never
    contains "+00:00".
    """
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        raise TypeError(
            f"iso_utc() accepts datetime or None, got {type(dt).__name__}; "
            "date-only values must remain date strings, and pre-serialized "
            "values must not be re-wrapped")
    if dt.tzinfo is None:
        # Naive == UTC by this repository's storage contract.
        return dt.isoformat() + "Z"
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
