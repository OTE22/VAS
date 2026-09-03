"""Every SQL-agent module imports.

An import-time error in `agent_loop` (a `list | set` in a word list) did
not fail the API's start-up: the SQL-agent router simply did not mount, the
rest of the API served normally, and every chatbot endpoint answered 404.
The focused suites that would have caught it errored at collection with an
empty summary, which the run script read as "no failures" and restarted on.

This is the cheapest possible tripwire, run first.

    docker exec face_recognition_api python -m pytest tests/test_sql_agent_imports.py -v
"""

import importlib
import pkgutil

import pytest

import sql_agent

MODULES = sorted(
    name for _finder, name, _ispkg in pkgutil.walk_packages(
        sql_agent.__path__, prefix="sql_agent.")
    if not name.endswith(("__main__", ".main")))


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    importlib.import_module(module)


def test_the_router_module_is_among_them():
    assert "sql_agent.api.routes" in MODULES
    assert "sql_agent.tools.agent_loop" in MODULES
