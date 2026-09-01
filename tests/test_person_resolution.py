"""Resolving a person the user named.

The reported failure: "track ali" answered "no known person matches that
name" while Ali was enrolled and visible in the dashboard. The log line

    [TOOL_LOOP] step=1 lookup=resolve_person -> ok count matches[0] note query

reads as ZERO matches (`_describe_result` renders a list as `key[len]`), not
as "the first row was picked". So the defect is that the resolver found
NOBODY, and the clarifying question that followed was the correct response to
a failed look-up.

It found nobody because it was looking in the wrong place. The pool was

    SELECT DISTINCT name FROM faces

— the DETECTION rows. Enrolled people live in `identities.display_name`. A
person who has been enrolled but never yet detected did not exist as far as
the agent was concerned.

`identities` is deliberately NOT in the SQL guard's table allowlist
(`faces, detections, pipelines, system_metrics`), so the resolver cannot read
it with a query and must not be given permission to: that allowlist is
exactly what stops the model reading tables it was never shown. The enrolled
names arrive the way documents already do — built by the API layer and handed
in — so no SQL, no guard change, and no widening of what the model may query.

    docker exec face_recognition_api python -m pytest tests/test_person_resolution.py -v
"""

import pytest

from sql_agent.tools import tool_executors as tx


class _FakeDB:
    """The faces-name pool, the only thing the resolver may query."""

    def __init__(self, names=()):
        self.names = list(names)
        self.queries = []

    def execute_query(self, sql):
        self.queries.append(sql)
        if "FROM faces" in sql:
            return {"success": True,
                    "rows": [{"name": n} for n in self.names]}
        return {"success": True, "rows": []}


def _resolve(needle, *, enrolled=(), detected=()):
    """Run the tool the way the loop runs it."""
    return tx.execute_read_only(
        "resolve_person", {"name": needle},
        db=_FakeDB(detected),
        identity_index=[{"identity_id": f"id-{i}", "display_name": n}
                        for i, n in enumerate(enrolled)])


# ------------------------------------------------------- THE reported bug

def test_an_enrolled_person_who_was_never_detected_is_found():
    """The root cause, stated as a test.

    Ali is enrolled; no detection row carries his name yet. Before the fix
    the pool came only from `faces`, so this returned zero matches and the
    agent told the user no such person existed.
    """
    out = _resolve("ali", enrolled=["Ali Abbass"], detected=["Joey"])

    assert out["status"] == "resolved", out
    assert out["identity"]["display_name"] == "Ali Abbass"


def test_names_from_detections_still_resolve():
    """Backward compatibility: the old pool is unioned, not replaced."""
    out = _resolve("joey", enrolled=[], detected=["JOEY"])

    assert out["status"] == "resolved"
    assert out["identity"]["display_name"] == "JOEY"


# --------------------------------------------------------- decided status

def test_one_strong_match_resolves_without_asking():
    """A unique match is what lets the turn continue without a question."""
    out = _resolve("iron man", enrolled=["Iron Man", "Joey"])

    assert out["status"] == "resolved"
    assert out["identity"]["display_name"] == "Iron Man"


def test_several_credible_matches_are_ambiguous():
    out = _resolve("ali", enrolled=["Ali Abbass", "Ali Hassan"])

    assert out["status"] == "ambiguous"
    assert len(out["candidates"]) == 2


def test_an_ambiguous_name_never_picks_a_candidate():
    """THE negative control.

    Returning `resolved` here would silently track the wrong person — a
    failure far worse than asking. This is the assertion that keeps
    "resolve" honest, so it must not be relaxed.
    """
    out = _resolve("ali", enrolled=["Ali Abbass", "Ali Hassan"])

    assert out["status"] != "resolved"
    assert "identity" not in out


def test_no_match_is_not_found():
    out = _resolve("Zoltan Kaszubowski", enrolled=["Ali Abbass"])

    assert out["status"] == "not_found"
    assert out["query"] == "Zoltan Kaszubowski"


def test_an_empty_name_is_not_found():
    out = _resolve("   ", enrolled=["Ali Abbass"])

    assert out["status"] == "not_found"


# ------------------------------------------------------------- normalizing

@pytest.mark.parametrize("needle,enrolled", [
    ("ali abbass", "Ali Abbass"),          # case
    ("  Ali   Abbass  ", "Ali Abbass"),    # whitespace, both sides
    ("ALI ABBASS", "Ali Abbass"),
])
def test_case_and_whitespace_do_not_matter(needle, enrolled):
    out = _resolve(needle, enrolled=[enrolled])
    assert out["status"] == "resolved", (needle, out)


@pytest.mark.parametrize("needle,enrolled", [
    ("أحمد", "احمد"),        # alef with hamza above vs bare alef
    ("إبراهيم", "ابراهيم"),  # alef with hamza below
    ("آدم", "ادم"),          # alef madda
    ("علي", "على"),          # ya vs alef maqsura
    ("فاطمة", "فاطمه"),      # ta marbuta vs ha
    ("عــلي", "علي"),        # tatweel
    ("عَلِي", "علي"),          # harakat
])
def test_arabic_spelling_variants_match(needle, enrolled):
    """`.lower()` is a no-op for Arabic, so none of these matched before.

    This system handles Arabic names throughout; every one of these pairs is
    the same name written two legitimate ways.
    """
    out = _resolve(needle, enrolled=[enrolled])
    assert out["status"] == "resolved", (needle, enrolled, out)


def test_the_persisted_key_function_is_left_alone():
    """`name_lookup_key` backs the stored `display_name_key` column.

    Widening it would silently change which enrollments count as duplicates
    and desync every key already written, so the fuzzy key layers ON it
    rather than replacing it.
    """
    from backend.core.enrollment_service import name_lookup_key

    assert name_lookup_key("  Ali   ABBASS ") == "ali abbass"
    assert name_lookup_key("أحمد") == "أحمد"      # NOT folded: persisted


# ---------------------------------------------------------------- bounded

def test_the_candidate_list_stays_bounded():
    """A look-up result is context, not a report."""
    out = _resolve("ali", enrolled=[f"Ali {n}" for n in range(40)])

    assert out["status"] == "ambiguous"
    assert len(out["candidates"]) <= tx._MAX_NAME_MATCHES


def test_the_resolver_reads_no_table_outside_the_allowlist():
    """`identities` is not allowlisted, and must not become so for this.

    The allowlist is what stops the model reading tables it was never shown;
    enrolled names reach the tool from the caller instead.
    """
    db = _FakeDB(["Joey"])
    tx.execute_read_only("resolve_person", {"name": "joey"}, db=db,
                         identity_index=[{"identity_id": "x",
                                          "display_name": "Ali Abbass"}])

    for sql in db.queries:
        assert "identities" not in sql.lower(), sql
