"""A failed SQL execution is not the end of a streamed turn.

Opik trace 01a07537-4185 (2026-09-06): `execute_sql` failed with a
correctable error, `observe_and_replan` decided REPLAN, and in the same
second the stream generator yielded a terminal `error` event - the route
closed the stream with the closed failure phrase and cancelled the graph
while the repair's model call was in flight. Four of ten capability
questions died this way. The execute_sql branch may only report progress;
the graph's own ending narrates a real failure when the repairs are spent.

    docker exec face_recognition_api python -m pytest tests/test_stream_repairs_before_failing.py -v
"""

import inspect
import re

from sql_agent.agent import SQLIntelligenceAgent


def test_the_execute_branch_of_the_stream_never_ends_the_turn():
    source = inspect.getsource(SQLIntelligenceAgent.query_stream)
    start = source.index('elif node_name == "execute_sql":')
    end = source.index('elif node_name == "story_response":', start)
    branch = source[start:end]
    assert '"type": "error"' not in branch, "an execution failure must not be terminal"
    assert re.search(r'"type":\s*"status"', branch), "the client is told a repair is under way"
    assert "repairing" in branch
