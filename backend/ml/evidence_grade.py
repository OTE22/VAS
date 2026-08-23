"""
THE definition of evidence-grade data — one place, used by every consumer.

A reviewed outcome may feed scientific evidence (shadow evidence report,
scientific gate / readiness, supervised label gate) only when ALL hold:

    label_kind    = manual          (never weak / rule-derived)
    review_status = reviewed        (a second, authorized review confirmed it)
    status        = active          (not retracted / superseded)
    label         in {positive, negative}   (a legitimate outcome, not unknown)
    source        not synthetic / demo-seed (prefixes below)

Everything else is a distinct population and is REPORTED, never mixed in:
unreviewed, weak, synthetic/seed, disputed, retracted, unknown.

Why prefixes: the two data generators the repository ships mark their rows
(`scripts/seed_ml_ops_demo.py` -> source `seed-*`; the synthetic-year corpus
-> `synthetic-*`). They refuse production, but a guard is not a filter —
evidence code must not depend on nobody ever having run them.
"""

from typing import Any, Dict, Iterable, Optional

from sqlalchemy import and_, not_, or_

EVIDENCE_LABEL_KIND = "manual"
EVIDENCE_REVIEW_STATUS = "reviewed"
EVIDENCE_STATUS = "active"
EVIDENCE_OUTCOMES = ("positive", "negative")
NON_EVIDENCE_SOURCE_PREFIXES = ("seed-", "synthetic-", "synth-")

# Population names used by every report (fixed vocabulary).
POP_BLIND_REVIEWED = "blind_reviewed"
POP_REVEALED_REVIEWED = "revealed_reviewed"
POP_SELF_REVIEWED = "self_reviewed"
POP_WEAK = "weak"
POP_SYNTHETIC_OR_SEED = "synthetic_or_seed"
POP_UNREVIEWED = "unreviewed"
POP_DISPUTED = "disputed"
POP_RETRACTED = "retracted"
POP_UNKNOWN_OUTCOME = "unknown_outcome"
POPULATIONS = (POP_BLIND_REVIEWED, POP_REVEALED_REVIEWED, POP_SELF_REVIEWED, POP_WEAK,
               POP_SYNTHETIC_OR_SEED, POP_UNREVIEWED, POP_DISPUTED, POP_RETRACTED,
               POP_UNKNOWN_OUTCOME)


def is_non_evidence_source(source: Optional[str]) -> bool:
    s = str(source or "").lower()
    return any(s.startswith(prefix) for prefix in NON_EVIDENCE_SOURCE_PREFIXES)


def is_evidence_grade(label) -> bool:
    """Python-side check on an MLLabel row (or any object with the fields).
    Mirrors evidence_filter() exactly."""
    if label is None:
        return False
    return (getattr(label, "label_kind", None) == EVIDENCE_LABEL_KIND
            and getattr(label, "review_status", None) == EVIDENCE_REVIEW_STATUS
            and getattr(label, "status", None) == EVIDENCE_STATUS
            and getattr(label, "label", None) in EVIDENCE_OUTCOMES
            and not is_non_evidence_source(getattr(label, "source", None)))


def evidence_filter(label_model, *, outcomes_only: bool = True):
    """SQLAlchemy predicate for `label_model` (MLLabel or an aliased/subquery
    column set with the same names) — the SQL twin of is_evidence_grade().
    outcomes_only=False keeps reviewed manual `unknown` labels in (for
    breakdowns that report them separately); everything else is identical."""
    conditions = [
        label_model.label_kind == EVIDENCE_LABEL_KIND,
        label_model.review_status == EVIDENCE_REVIEW_STATUS,
        label_model.status == EVIDENCE_STATUS,
        not_(or_(*[label_model.source.ilike(prefix + "%") for prefix in NON_EVIDENCE_SOURCE_PREFIXES])),
    ]
    if outcomes_only:
        conditions.append(label_model.label.in_(EVIDENCE_OUTCOMES))
    return and_(*conditions)


def population_of(label) -> str:
    """Which population a label belongs to — every label lands in exactly
    one. Evidence-grade labels split by how they were recorded."""
    if label is None:
        return POP_UNREVIEWED
    if is_non_evidence_source(getattr(label, "source", None)):
        return POP_SYNTHETIC_OR_SEED
    if getattr(label, "status", None) == "retracted":
        return POP_RETRACTED
    if getattr(label, "label_kind", None) != EVIDENCE_LABEL_KIND:
        return POP_WEAK
    review = getattr(label, "review_status", None)
    if review == "disputed":
        return POP_DISPUTED
    if review != EVIDENCE_REVIEW_STATUS:
        return POP_UNREVIEWED
    if getattr(label, "label", None) not in EVIDENCE_OUTCOMES:
        return POP_UNKNOWN_OUTCOME
    created_by = getattr(label, "created_by", None)
    reviewed_by = getattr(label, "reviewed_by", None)
    created_id = getattr(label, "created_by_user_id", None)
    reviewed_id = getattr(label, "reviewed_by_user_id", None)
    if created_id is not None and reviewed_id is not None:
        if int(created_id) == int(reviewed_id):
            return POP_SELF_REVIEWED
    elif created_by and reviewed_by and str(created_by) == str(reviewed_by):
        return POP_SELF_REVIEWED
    selection = getattr(label, "selection", None) or {}
    revealed = selection.get("ml_observation_revealed") if isinstance(selection, dict) else None
    return POP_REVEALED_REVIEWED if revealed is True else POP_BLIND_REVIEWED


def empty_population_counts() -> Dict[str, int]:
    return {name: 0 for name in POPULATIONS}


def count_populations(labels: Iterable[Any]) -> Dict[str, int]:
    counts = empty_population_counts()
    for label in labels:
        counts[population_of(label)] += 1
    return counts


DEFINITION_TEXT = ("manual AND reviewed AND active AND outcome in {positive, negative} "
                   "AND source not seed-/synthetic-")
