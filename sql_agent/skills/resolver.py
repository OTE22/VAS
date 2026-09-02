"""Which recipes this turn needs, decided from what the turn HAS.

The instructions for producing a document are useless on a turn where nothing
exists to make a document from - and worse than useless, because a model told
how to handle follow-ups will find a follow-up to handle. "hi" produced a PDF
that way.

So the signal is STRUCTURAL: is there a result, is there a document. Facts
about the conversation's state, read off the state itself. This module holds
no business reasoning, matches no keywords and never looks at what the user
wrote - deciding what the user MEANS is the model's job, and the model gets
the skill text to do it with.

Adding a capability means adding a skill directory and one line in
`_APPLICABILITY`, not editing a router.
"""

import functools
import logging
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).parent

#: skill -> does this turn have anything for it to act on?
#:
#: `conversation` and `database` are always available: answering and asking
#: the data are what the assistant IS. The other two describe work that can
#: only apply to something already produced, so they load only when it exists.
_APPLICABILITY = {
    "conversation": lambda **state: True,
    "database": lambda **state: True,
    "follow_up": lambda **state: state["has_result"] or state["has_documents"],
    "artifacts": lambda **state: state["has_result"] or state["has_documents"],
}


@functools.lru_cache(maxsize=None)
def _read(name: str) -> str:
    """One skill's text. Cached: these are read on every turn and never change.

    A missing or unreadable skill degrades to nothing rather than failing the
    turn - the model still has its tools and the base prompt.
    """
    path = _SKILLS_DIR / name / "SKILL.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("[SKILLS] could not read %s: %s", name, e)
        return ""


def resolve(*, has_result: bool = False, has_documents: bool = False) -> List[str]:
    """The skills applicable to this turn, in a stable order."""
    state = {"has_result": bool(has_result), "has_documents": bool(has_documents)}
    return [name for name, applies in _APPLICABILITY.items() if applies(**state)]


def compose(base_prompt: str, *, has_result: bool = False,
            has_documents: bool = False) -> str:
    """The base contract plus only the recipes this turn can actually use."""
    names = resolve(has_result=has_result, has_documents=has_documents)
    sections = [text for text in (_read(name) for name in names) if text]
    logger.info("[SKILLS] loaded=%s", ",".join(names) or "none")
    if not sections:
        return base_prompt
    return base_prompt + "\n\n" + "\n\n".join(sections)
