"""A rejected query's regeneration is told what its own CTEs expose.

Opik 01a08244-d3fe / 01a08246-b49d (2026-09-08): "which hour has the highest
identified:unidentified ratio" joined two CTEs that both expose `hour_of_day`
and referenced it unqualified; "what share of the top person's days were
multi-camera" read `cameras_per_day` from a CTE that does not emit it. The
hint carried only the database's sentence, and the model shuffled column
order until the budget ran out. These facts come from the query's own AST.
"""
from sql_agent.tools.agent_tools import SQLAgentTools as T

AMBIGUOUS = (
    "WITH identified_faces AS (SELECT EXTRACT(HOUR FROM d.timestamp) AS hour_of_day, "
    "COUNT(*) AS identified_count FROM faces AS f JOIN detections AS d "
    "ON f.detection_id = d.id GROUP BY 1), "
    "unidentified_faces AS (SELECT EXTRACT(HOUR FROM d.timestamp) AS hour_of_day, "
    "COUNT(*) AS unidentified_count FROM faces AS f JOIN detections AS d "
    "ON f.detection_id = d.id GROUP BY 1) "
    "SELECT hour_of_day, ROUND(identified_count / NULLIF(unidentified_count, 0), 3) AS ratio "
    "FROM identified_faces JOIN unidentified_faces ON hour_of_day = hour_of_day "
    "ORDER BY ratio DESC LIMIT 1"
)

OUT_OF_SCOPE = (
    "WITH per_person AS (SELECT f.name, COUNT(*) AS total_detections, "
    "DATE(d.timestamp) AS detection_day, COUNT(DISTINCT d.pipeline_id) AS cameras_per_day "
    "FROM faces AS f JOIN detections AS d ON f.detection_id = d.id "
    "GROUP BY f.name, DATE(d.timestamp)), "
    "ranked AS (SELECT name, total_detections, ROW_NUMBER() OVER "
    "(PARTITION BY name ORDER BY total_detections DESC) AS rank_for_person FROM per_person) "
    "SELECT name, ROUND(100.0 * SUM(CASE WHEN cameras_per_day > 1 THEN 1 ELSE 0 END) "
    "/ COUNT(*), 1) AS pct FROM ranked WHERE rank_for_person = 1 GROUP BY name"
)


def test_an_ambiguous_reference_is_named_with_the_sources_that_expose_it():
    facts = T._sql_scope_facts(AMBIGUOUS)
    assert "identified_faces: hour_of_day, identified_count" in facts
    assert "unidentified_faces: hour_of_day, unidentified_count" in facts
    assert "'hour_of_day' is exposed by 2 sources" in facts
    assert "qualify it" in facts


def test_a_column_the_cte_does_not_emit_is_named_with_what_it_does_emit():
    facts = T._sql_scope_facts(OUT_OF_SCOPE)
    assert "per_person: name, total_detections, detection_day, cameras_per_day" in facts
    assert "ranked: name, total_detections, rank_for_person" in facts
    assert "in the final SELECT: 'cameras_per_day' is not exposed by any source (ranked)" in facts


def test_a_selects_own_alias_used_in_order_by_is_not_reported_missing():
    # `ratio` is this SELECT's own output alias; PostgreSQL allows it in ORDER BY.
    assert "'ratio' is not exposed" not in T._sql_scope_facts(AMBIGUOUS)


def test_queries_without_ctes_and_unparseable_text_yield_nothing():
    assert T._sql_scope_facts("SELECT COUNT(*) FROM faces f WHERE f.name IS NOT NULL") == ""
    assert T._sql_scope_facts("NOT SQL AT ALL (((") == ""
    assert T._sql_scope_facts("") == ""
    assert T._sql_scope_facts(None) == ""


def test_the_correction_hint_carries_the_scope_facts():
    hint = T._correction_hint({"sql_correction_hint": {
        "sql": AMBIGUOUS, "reason": 'column reference "hour_of_day" is ambiguous'}})
    assert "Why it was rejected" in hint
    assert "What each CTE in your query actually exposes" in hint
    assert "'hour_of_day' is exposed by 2 sources" in hint
    # a first attempt still gets no hint at all
    assert T._correction_hint({}) == ""


# ---------------------------------------------------------------------------
# Opik 01a08599-6c8d (2026-09-09). Two ways the facts never reached the model:
# the error lived INSIDE a CTE, and the hint stored a 600-character copy that
# does not parse. Both are fixed; both are pinned here.
# ---------------------------------------------------------------------------
WINDOW_ALIAS = (
    "WITH camera_counts AS (SELECT p.pipeline_id, COUNT(d.id) AS count FROM detections AS d "
    "JOIN pipelines AS p ON d.pipeline_id = p.pipeline_id GROUP BY p.pipeline_id "
    "ORDER BY count DESC LIMIT 2), "
    "unidentified_shares AS (SELECT COALESCE(p.location_name, p.pipeline_id) AS camera_name, "
    "COUNT(*) FILTER(WHERE f.name IS NULL) AS unidentified_faces, COUNT(*) AS total_faces "
    "FROM faces AS f JOIN detections AS d ON f.detection_id = d.id "
    "JOIN pipelines AS p ON p.pipeline_id = d.pipeline_id GROUP BY p.pipeline_id, p.location_name), "
    "share_diff AS (SELECT camera_name, unidentified_faces / total_faces AS unidentified_share, "
    "LAG(unidentified_faces / total_faces) OVER (ORDER BY unidentified_share DESC) AS prev_share "
    "FROM unidentified_shares) "
    "SELECT camera_name, unidentified_share, unidentified_share - prev_share AS share_diff "
    "FROM share_diff WHERE NOT prev_share IS NULL"
)


def test_an_output_alias_inside_a_window_is_named_with_the_cte_it_is_in():
    facts = T._sql_scope_facts(WINDOW_ALIAS)
    assert "in CTE share_diff: 'unidentified_share' is an output alias" in facts
    assert "window's ORDER BY/PARTITION BY cannot see it" in facts
    assert "repeat the expression instead" in facts
    # the outer SELECT is legitimate and must not be reported
    assert "in the final SELECT" not in facts


def test_a_truncated_query_yields_nothing_which_is_why_the_facts_are_stored():
    # The parser cannot read a query cut off mid-identifier; deriving the facts
    # from the hint's stored copy therefore produced silence.
    assert T._sql_scope_facts(WINDOW_ALIAS[:600]) == ""
    assert T._sql_scope_facts(WINDOW_ALIAS) != ""


def test_the_hint_prefers_the_facts_recorded_from_the_untruncated_sql():
    recorded = T._sql_scope_facts(WINDOW_ALIAS)
    hint = T._correction_hint({"sql_correction_hint": {
        "sql": WINDOW_ALIAS[:600],          # what the prompt shows
        "reason": 'column "unidentified_share" does not exist',
        "scope_facts": recorded}})          # what was measured at attach time
    assert "is an output alias" in hint, "the stored facts survive truncation"


def test_attaching_the_hint_records_the_facts_from_the_whole_query():
    import sql_agent.reasoning as reasoning

    state = {"generated_sql": WINDOW_ALIAS}
    T._attach_correction_hint(state, {
        "error_type": reasoning.ErrorType.SQL_EXECUTION_ERROR_CORRECTABLE,
        "sanitized_detail": 'column "unidentified_share" does not exist'})
    hint = state["sql_correction_hint"]
    assert "is an output alias" in hint["scope_facts"]
    assert len(hint["sql"]) > 600, "the rejected query is no longer cut to 600 chars"

