"""Dev must never be more constrained than production.

`config.py` defaults ARE production: the prod compose sets no SQL-agent
tuning, so whatever is written there is what a real deployment runs — on
Ollama/CPU, where every extra step costs seconds. The dev compose exists to
raise those numbers on the faster model.

Raising the default from 3 to 8 without touching the dev compose inverted
that: production got 8 on the slow model while dev sat at 5 on the fast one.
Nothing failed, which is exactly why it needs a test — a silent divergence
between what you develop against and what you ship.

    docker exec face_recognition_api python -m pytest tests/test_reasoning_budget_coherence.py -v
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEV_COMPOSE = REPO / "docker" / "docker-compose.cpu.yml"
PROD_COMPOSE = REPO / "docker" / "docker-compose.prod.yml"

BUDGETS = ["SQL_AGENT_MAX_REASONING_STEPS", "SQL_AGENT_MAX_REPLANS",
           "SQL_AGENT_MAX_EXECUTION_RETRIES"]


def _compose_value(path, name):
    match = re.search(rf"^\s*{name}:\s*(\d+)\s*$", path.read_text(
        encoding="utf-8"), re.M)
    return int(match.group(1)) if match else None


def _default(name):
    from config import settings
    return int(getattr(settings, name))


@pytest.mark.parametrize("name", BUDGETS)
def test_dev_is_never_more_constrained_than_production(name):
    """Dev runs the FASTER model, so it must not think LESS."""
    dev = _compose_value(DEV_COMPOSE, name)
    if dev is None:
        pytest.skip(f"{name} is not pinned in the dev compose")

    # settings reflects the running container, which IS the dev compose, so
    # compare against the declared default rather than the live value.
    import ast
    source = (REPO / "config.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    declared = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "")
                == name and isinstance(node.value, ast.Call)):
            for kw in node.value.keywords:
                if kw.arg == "default":
                    declared = ast.literal_eval(kw.value)
    assert declared is not None, f"{name} has no declared default"

    assert dev >= declared, (
        f"dev gives {name}={dev} but production defaults to {declared}; "
        f"dev runs the faster model and must not be the tighter of the two")


def test_production_takes_the_declared_defaults():
    """If the prod compose ever pins these, this file must be revisited."""
    if not PROD_COMPOSE.exists():
        pytest.skip("no production compose file")

    text = PROD_COMPOSE.read_text(encoding="utf-8")
    pinned = [name for name in BUDGETS if re.search(rf"^\s*{name}:", text, re.M)]
    assert not pinned, (
        f"the production compose now pins {pinned}; config.py is no longer "
        f"the single source of truth for what production runs")
