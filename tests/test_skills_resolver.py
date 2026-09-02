"""Loading only the recipes a turn can actually use.

Everything used to arrive in one prompt on every turn, including instructions
for handling follow-ups and producing documents. That is not merely wasteful:
a model told how to handle a follow-up will find a follow-up to handle, which
is how "hi" came back as a PDF of somebody's surveillance report.

The signal is STRUCTURAL - is there a result, is there a document - read off
the conversation state. The resolver never looks at what the user wrote,
because deciding what a message MEANS is the model's job.

    docker exec face_recognition_api python -m pytest tests/test_skills_resolver.py -v
"""

import re
from pathlib import Path

import pytest

from sql_agent.skills import resolver


BASE = "BASE CONTRACT"


# ------------------------------------------------------- what loads when

def test_a_bare_turn_loads_only_the_always_on_skills():
    """THE case that produced a PDF from "hi".

    Nothing has been produced, so nothing can be referred to or exported.
    """
    loaded = resolver.resolve(has_result=False, has_documents=False)

    assert loaded == ["conversation", "database"]
    assert "follow_up" not in loaded
    assert "artifacts" not in loaded


def test_a_turn_holding_a_result_can_refer_to_it_and_export_it():
    loaded = resolver.resolve(has_result=True, has_documents=False)

    assert "follow_up" in loaded
    assert "artifacts" in loaded


def test_a_turn_holding_a_document_can_refer_to_it():
    loaded = resolver.resolve(has_result=False, has_documents=True)

    assert "follow_up" in loaded
    assert "artifacts" in loaded


def test_the_core_skills_are_always_present():
    """Answering and asking the data are what the assistant IS."""
    for state in ({}, {"has_result": True}, {"has_documents": True}):
        loaded = resolver.resolve(**state)
        assert "conversation" in loaded and "database" in loaded, state


# ------------------------------------------------------------ composition

def test_composition_keeps_the_base_contract_first():
    composed = resolver.compose(BASE, has_result=True, has_documents=True)
    assert composed.startswith(BASE)


def test_an_empty_turn_carries_no_document_instructions():
    """The measurable point: less context, not merely differently arranged."""
    bare = resolver.compose(BASE)
    rich = resolver.compose(BASE, has_result=True, has_documents=True)

    assert len(bare) < len(rich)
    assert "translate_document" not in bare
    assert "translate_document" in rich


def test_a_missing_skill_degrades_instead_of_failing(monkeypatch):
    """Losing a recipe must cost quality, never the turn."""
    monkeypatch.setattr(resolver, "_SKILLS_DIR", Path("/nonexistent"))
    resolver._read.cache_clear()
    try:
        assert resolver.compose(BASE) == BASE
    finally:
        resolver._read.cache_clear()


# ------------------------------------------------ what the resolver is NOT

def test_the_resolver_never_reads_the_user_message():
    """It takes no message argument, so it cannot route on one.

    The moment a resolver starts matching words it becomes the rigid
    classifier this architecture exists to remove.
    """
    import inspect

    signature = inspect.signature(resolver.resolve)
    assert "message" not in signature.parameters
    assert "user_text" not in signature.parameters
    assert set(signature.parameters) == {"has_result", "has_documents"}


def test_the_resolver_holds_no_business_reasoning():
    """No keyword lists, no intent names, no domain vocabulary in the CODE.

    Prose explaining WHY a rule exists is not reasoning the code performs, so
    docstrings are stripped before the check. What must stay clean is the
    executable part: the moment a resolver branches on domain words it becomes
    the rigid classifier this architecture exists to remove.
    """
    import ast

    tree = ast.parse(Path(resolver.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    code = ast.unparse(tree).lower()

    for smell in ("select", "intent", "camera", "pdf", "arabic", "report"):
        assert smell not in code, f"{smell!r} leaked into executable code"


# ------------------------------------------------------- the skills files

@pytest.mark.parametrize("name", ["conversation", "database", "follow_up",
                                  "artifacts"])
def test_every_skill_follows_the_agreed_shape(name):
    text = (Path(resolver.__file__).parent / name / "SKILL.md").read_text(
        encoding="utf-8")

    for heading in ("## Purpose", "## When to Use", "## Available Tools",
                    "## Process", "## Constraints"):
        assert heading in text, f"{name} is missing {heading}"


@pytest.mark.parametrize("name", ["conversation", "database", "follow_up",
                                  "artifacts"])
def test_a_skill_describes_process_rather_than_code(name):
    """Skills are recipes. Python belongs in Python."""
    text = (Path(resolver.__file__).parent / name / "SKILL.md").read_text(
        encoding="utf-8")

    assert not re.search(r"^\s*(if|elif|for|def|import)\s", text, re.M), (
        f"{name} contains code-shaped instructions")


def test_every_tool_a_skill_names_actually_exists():
    """A recipe referring to a tool that is gone is a recipe that misleads."""
    from sql_agent.tools import tool_registry as tr

    real = {s["function"]["name"] if "function" in s else s["name"]
            for s in tr.tool_specs()}

    for name in ("conversation", "database", "follow_up", "artifacts"):
        text = (Path(resolver.__file__).parent / name / "SKILL.md").read_text(
            encoding="utf-8")
        for mentioned in re.findall(r"`([a-z_]+)`", text):
            if mentioned.endswith("_document") or mentioned.startswith(
                    ("query_", "list_", "resolve_", "answer_", "modify_",
                     "get_", "ask_", "update_")):
                assert mentioned in real, (
                    f"{name} names a tool that does not exist: {mentioned}")
