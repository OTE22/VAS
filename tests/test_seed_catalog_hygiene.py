"""The seed catalogs stay verified-by-construction.

The knowledge base holds only verified examples (user rule, 2026-09-06), and
the seeds outrank the schema text for a similar question - so a seed that
counts the 'Unknown' placeholder as a person teaches the model to do the
same (it did: every per-camera people count was one too high). These checks
are the static half of that guarantee; the dynamic half is executing every
seed against the schema (scratch catalog_check.py) before release.

No database, no model.
"""
import re

import pytest

from sql_agent.knowledge_base import SQLKnowledgeBase
from sql_agent import seed_catalog, seed_catalog_generated


def _seeds():
    return SQLKnowledgeBase.SEED_EXAMPLES


def test_every_seed_is_a_complete_triple():
    for seed in _seeds():
        assert seed["question"].strip(), seed
        assert seed["sql"].strip().upper().startswith(("SELECT", "WITH")), seed["question"]
        assert seed["purpose"].strip(), seed["question"]


def test_no_two_seeds_ask_the_same_question():
    questions = [s["question"].strip().lower() for s in _seeds()]
    duplicates = {q for q in questions if questions.count(q) > 1}
    assert not duplicates, duplicates


def test_the_catalogs_are_large_and_loaded():
    assert len(seed_catalog.CATALOG) >= 200
    assert len(seed_catalog_generated.GENERATED) >= 500
    assert len(_seeds()) >= len(seed_catalog.CATALOG) + len(seed_catalog_generated.GENERATED)


def test_seeds_never_count_placeholder_names_as_people():
    """A distinct-name count must exclude NULL/''/Unknown/person_<n>."""
    for seed in _seeds():
        sql = seed["sql"]
        filtered = "NOT LIKE 'unknown%'" in sql
        for match in re.finditer(r"COUNT\(DISTINCT\s+(\w+\.)?name\)", sql, flags=re.I):
            if filtered:
                continue        # the WHERE already keeps only identified names
            pytest.fail(f"{seed['question']!r} counts distinct names without excluding "
                        f"placeholders: {match.group(0)}")
        if re.search(r"GROUP BY\s+(\w+\.)?name\b", sql, flags=re.I) and "LIKE" not in sql.upper():
            pytest.fail(f"{seed['question']!r} groups by name with no placeholder filter")


def test_seeds_never_read_the_lagging_detection_cache():
    for seed in _seeds():
        assert "total_detections FROM pipelines" not in seed["sql"], seed["question"]
        assert not re.search(r"\bp\.total_detections\b", seed["sql"]), seed["question"]


def test_generated_seeds_skip_nonsense_period_combinations():
    questions = [s["question"] for s in seed_catalog_generated.GENERATED]
    for q in questions:
        lowered = q.lower()
        if "per day" in lowered or "daily" in lowered or "day-by-day" in lowered or "each day" in lowered:
            assert not any(w in lowered for w in ("today", "yesterday", "in the last hour", "hours")), q
        if re.search(r"\bper week\b", lowered) or "weekly" in lowered:
            assert not any(w in lowered for w in ("today", "yesterday", "hour", "3 days", "7 days", "14 days", "this week", "last week")), q
