"""Counts are counted, never read from a cached counter.

Live: "What are the most active pipelines?" produced "no pipeline has a valid
total number of detections ... pytest-cam", because the SQL model read
pipelines.total_detections - a counter that is incremented as detections
arrive and drifts (KSA: 17 cached, 25 real) - instead of counting the
detections table. The schema the model is shown now says so, on the column
and in the relationships, and this pins that the text reaches the prompt.

    docker exec face_recognition_api python -m pytest tests/test_counts_come_from_detections.py -v
"""

from sql_agent.database import DatabaseManager


def _schema_text():
    from sql_agent.config import Config

    db = DatabaseManager(Config())
    db._use_known_schema = True
    return db.get_schema_description()


def test_the_cached_counter_is_marked_as_such():
    column = next(c for c in DatabaseManager.KNOWN_SCHEMA["tables"]["pipelines"]["columns"]
                  if c["column_name"] == "total_detections")
    assert "CACHED" in column["description"]
    assert "NEVER use it to count" in column["description"]


def test_no_reference_example_teaches_the_cached_counter():
    """The RAG seeds outrank the schema text in the model's eyes: the seed
    for "which pipeline has the most detections" read the cache with
    LIMIT 1, and that is the query the model reproduced."""
    import re

    from sql_agent import knowledge_base as kb

    source = open(kb.__file__, encoding="utf-8").read()
    # Any SELECT that takes total_detections FROM pipelines without counting.
    offenders = [m.group(0) for m in re.finditer(
        r"SELECT[^;\"]*?\btotal_detections\b[^;\"]*?FROM pipelines\b[^;\"]*", source)
        if "COUNT(" not in m.group(0)]
    assert not offenders, offenders


def test_a_query_that_reads_the_cache_is_never_learned():
    """The third door. A learned example outranks the schema text for its
    question, so one wrong-but-executable query pins the wrong answer."""
    from sql_agent.tools.agent_tools import SQLAgentTools as T

    assert T._reads_cached_counter(
        "SELECT pipeline_id, total_detections FROM pipelines ORDER BY total_detections DESC LIMIT 1")
    assert not T._reads_cached_counter(
        "SELECT p.location_name, COUNT(d.id) AS total_detections FROM pipelines p "
        "JOIN detections d ON d.pipeline_id = p.pipeline_id GROUP BY p.location_name")
    assert not T._reads_cached_counter("SELECT COUNT(*) FROM detections")


def test_the_counting_rule_reaches_the_prompt():
    text = _schema_text()
    assert "never from pipelines.total_detections" in text
    assert "COUNT(d.id)" in text


def test_the_schema_says_unknown_is_not_an_identified_person():
    """"How many identified people has each camera seen" came back one too
    high on every camera: the SQL model counted the 'Unknown' placeholder as
    a person, because nothing in the schema text said it was not one."""
    import inspect

    import sql_agent.database as database

    source = inspect.getsource(database)
    assert "PLACEHOLDERS for a face that was NOT identified" in source
    assert "NOT LIKE 'unknown%'" in source


def test_row_facts_sum_grouped_rows_instead_of_counting_them():
    """A grouped result's rows are figures; counting them per day is wrong.

    "IRON MAN per day, split by camera" returned two rows for 2026-08-18
    (1 and 2 detections) and the narrator was told "2026-08-18: 2" - the
    number of rows - against 3 detections (pass 6, 2026-09-06).
    """
    from sql_agent.tools.agent_tools import SQLAgentTools

    rows = [
        {"date": "2026-08-17", "detections": 4, "camera_name": "A"},
        {"date": "2026-08-17", "detections": 1, "camera_name": "B"},
        {"date": "2026-08-18", "detections": 1, "camera_name": "A"},
        {"date": "2026-08-18", "detections": 2, "camera_name": "C"},
    ]
    grouped = ("SELECT DATE(d.timestamp) AS date, COUNT(*) AS detections, "
               "p.location_name AS camera_name FROM faces f JOIN detections d "
               "ON f.detection_id = d.id JOIN pipelines p ON d.pipeline_id = "
               "p.pipeline_id GROUP BY DATE(d.timestamp), p.location_name")

    facts = SQLAgentTools._row_facts(rows, grouped)

    assert "2026-08-18: 3" in facts and "2026-08-17: 5" in facts, facts
    assert "rows per date" not in facts
    assert "computed group" in facts

    # A plain row dump is still counted per value.
    events = [{"camera_name": "A", "timestamp": i} for i in range(3)] + [
        {"camera_name": "B", "timestamp": 9}]
    plain = SQLAgentTools._row_facts(
        events, "SELECT p.location_name AS camera_name, d.timestamp FROM "
                "detections d JOIN pipelines p ON d.pipeline_id = p.pipeline_id")
    assert "rows per camera_name: A: 3, B: 1" in plain, plain
