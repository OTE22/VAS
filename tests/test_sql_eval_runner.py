from scripts.sql_agent_eval.run_eval import judge


def _entry(column):
    return {"check": [column]}


def test_judge_accepts_more_precise_decimal_answer():
    verdict, _ = judge(
        _entry("average_similarity"),
        [{"average_similarity": 0.753}],
        "The average similarity is 0.7529280731735506.",
    )
    assert verdict == "PASS"


def test_judge_normalizes_human_facing_whitespace():
    verdict, _ = judge(
        _entry("camera_name"),
        [{"camera_name": "MAD5AL AMEN  (1)"}],
        "The camera is MAD5AL AMEN (1).",
    )
    assert verdict == "PASS"


def test_judge_does_not_accept_a_different_integer():
    verdict, detail = judge(
        _entry("people"), [{"people": 2}], "There is 1 identified person."
    )
    assert verdict == "FAIL"
    assert "people=2" in detail
