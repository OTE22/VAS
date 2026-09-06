"""Retrieval reaches the verified seeds through their placeholders.

The catalogs are written with PERSON_NAME / OTHER_PERSON / CAMERA_NAME, and
a placeholder embeds far from a real name: "Which camera saw JOEY, and how
many minutes passed between JOEY's first and last detection there?" ranked
its exact seed second (0.65) behind a learned "which camera detected Joey"
(0.73), and the SQL model wrote its own broken self-join (Opik
01a075ef-f861, 2026-09-06). A second retrieval with the stored names the
message mentions swapped for the placeholders is merged by similarity.

Also here: a regenerated SQL identical to the rejected one is identical
whether or not the validator had appended its LIMIT.

No database, no model: the name pool is stubbed.
"""
from sql_agent.tools.agent_tools import SQLAgentTools


class _Tools(SQLAgentTools):
    def __init__(self):          # no heavy dependencies
        self._stored_names_cache = None

    def _stored_names(self):
        return (["IRON MAN", "JOEY", "Ali Abbass"],
                ["WEZARET DEFA3", "MAD5AL AMEN  (1)", "KSA", "qa"])


def test_people_and_camera_become_placeholders_in_order():
    tools = _Tools()
    state = {"normalized_input": "Which cameras have seen IRON MAN but never JOEY at WEZARET DEFA3?",
             "interpretation": {}}
    assert (tools._placeholder_query(state)
            == "Which cameras have seen PERSON_NAME but never OTHER_PERSON at CAMERA_NAME?")


def test_possessives_and_case_are_kept():
    tools = _Tools()
    state = {"normalized_input": "how many minutes passed between joey's first and last detection there",
             "interpretation": {}}
    assert (tools._placeholder_query(state)
            == "how many minutes passed between PERSON_NAME's first and last detection there")


def test_a_message_naming_nobody_stored_gets_no_second_query():
    tools = _Tools()
    assert tools._placeholder_query({"normalized_input": "How many detections per weekday?",
                                     "interpretation": {}}) == ""


def test_short_camera_ids_do_not_match_inside_words():
    """'qa' is a stored camera id; 'quality' and 'Qatar' must not become CAMERA_NAME."""
    tools = _Tools()
    out = tools._placeholder_query({"normalized_input": "Show the quality of detections in Qatar for JOEY",
                                    "interpretation": {}})
    assert out == "Show the quality of detections in Qatar for PERSON_NAME"


def test_the_reading_s_people_are_used_even_when_not_in_the_pool():
    tools = _Tools()
    state = {"normalized_input": "Track Monica today",
             "interpretation": {"people": ["Monica"]}}
    assert tools._placeholder_query(state) == "Track PERSON_NAME today"


def test_merge_keeps_the_best_similarity_per_document_and_ranks():
    first = [{"document_id": "a", "similarity": 0.73, "question": "x"},
             {"document_id": "b", "similarity": 0.65, "question": "y"}]
    second = [{"document_id": "b", "similarity": 0.97, "question": "y"},
              {"document_id": "c", "similarity": 0.40, "question": "z"}]
    merged = SQLAgentTools._merge_examples(first, second, top_k=2)
    assert [m["document_id"] for m in merged] == ["b", "a"]
    assert merged[0]["similarity"] == 0.97


def test_sql_digest_ignores_the_validators_limit():
    with_limit = SQLAgentTools._sql_digest("SELECT a FROM t ORDER BY a LIMIT 500")
    without = SQLAgentTools._sql_digest("select a from t order by a")
    assert with_limit == without
    assert SQLAgentTools._sql_digest("SELECT a FROM t LIMIT 5") != SQLAgentTools._sql_digest("SELECT b FROM t")


def test_an_exact_verified_seed_is_recognised_only_from_seeds():
    assert SQLAgentTools._exact_seed_similarity(
        {"retrieved_examples": [{"source": "seed", "similarity": 0.97}]}) == 0.97
    assert SQLAgentTools._exact_seed_similarity(
        {"retrieved_examples": [{"source": "user", "similarity": 0.99}]}) == 0.0
    assert SQLAgentTools._exact_seed_similarity({"retrieved_examples": []}) == 0.0
    assert SQLAgentTools._EXACT_SEED_SIMILARITY == 0.9
