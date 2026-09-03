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
