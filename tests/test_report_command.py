"""A request for a report about a named person is the same command as "track X".

    user: report for tracking joey
    bot:  Joey was last seen on WEZARET DEFA3 at 2026-08-23 11:11:54.

The deterministic seam recognised only the VERB ("track joey"), so this
went to the model, which paraphrased it as the previous turn's question and
answered with that turn's one-line sentence instead of a report.

    docker exec face_recognition_api python -m pytest tests/test_report_command.py -v
"""

import pytest

from sql_agent.tools.planner import deterministic_request_plan


@pytest.mark.parametrize("text", [
    "report for tracking joey",
    "tracking report for joey",
    "give me a report about JOEY",
    "can you please prepare a report on Ali Abbass",
    "i want a detection report for iron man",
    "تقرير عن جوي",
    "تقرير تتبع جوي",
    "أعطني تقرير عن iron man",
])
def test_these_are_the_same_command_as_track(text):
    plan = deterministic_request_plan(text)
    assert plan is not None, text
    assert plan.action == "query_database" and plan.source == "deterministic"


@pytest.mark.parametrize("text", [
    "report",                      # no subject
    "the report about that",       # points back at a report we made
    "report for tracking him",     # needs resolution
    "can you make the report in arabic",   # a translation
    "make the report in english",
    "report about joey and make a PDF",    # compound work
])
def test_these_still_need_the_loop(text):
    assert deterministic_request_plan(text) is None, text


def test_the_verb_form_is_unchanged():
    assert deterministic_request_plan("track joey") is not None
    assert deterministic_request_plan("can you track joey") is not None
    assert deterministic_request_plan("تتبع joey") is not None
    assert deterministic_request_plan("track him") is None
