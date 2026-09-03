"""
Typed dataset definitions — the reproducible EXTRACTION contract.

A dataset version is only as traceable as the definition that selected its
rows. Before this module the builder selected rows with a hard-coded query
(`ORDER BY as_of ASC LIMIT 100000`) and recorded neither the cap nor the
rows it silently left behind. A definition names the population, the
feature contract, the time-range policy, the cap and what happens when the
cap is exceeded — and every build records which definition/version it used
and exactly what the extraction did (`ml_datasets.extraction`).

Definitions are plain typed Python (the project has no YAML runtime
dependency); adding a dataset for a future model family is one more entry
in DEFINITIONS — no control-flow change in the builder.

Nothing here is a scientific requirement: `min_rows` is OPTIONAL and None
for every shipped definition. The validator's pre-existing technical floors
(MIN_ROWS_*) are a different thing and are left untouched.
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from backend.ml.constants import (
    FEATURE_SET_VERSION, LABEL_DEFINITION_VERSION, PREVIOUS_FEATURE_SET_VERSION)
from backend.ml.model_specs import COAPPEARANCE_FEATURE_SET, SOCIAL_GRAPH_FEATURE_SET

EXTRACTION_POLICY_VERSION = "explicit-cap-v1"
LEGACY_EXTRACTION_POLICY_VERSION = "legacy-oldest-first-cap-v0"

SAMPLING_POLICIES = ("refuse", "newest_first", "oldest_first")

# How rows are divided in time.
#   temporal_group  an entity belongs WHOLLY to its earliest time bucket and
#                   its later-period rows are dropped: no entity recurs across
#                   splits (group isolation). With a long history of regular
#                   entities this leaves almost nothing for val/test — every
#                   regular was first seen in the train period.
#   temporal        same time boundaries, every row stays in the bucket of its
#                   own time; entities MAY recur across splits. Test scores
#                   then measure the later behaviour of known entities (the
#                   operational case), NOT generalisation to unseen entities.
#                   The overlap is measured and recorded, never assumed away.
SPLIT_STRATEGIES = ("temporal_group", "temporal")
DATASET_KINDS = ("unsupervised", "supervised")

# Known limitations of each frozen feature set. Stated on every manifest and
# quality report so a reproducible dataset is never mistaken for a
# scientifically clean one. Fixing any of them needs a new feature-set
# version — semantics under an existing version are frozen (boot-verified).
FEATURE_SET_LIMITATIONS: Dict[str, Tuple[Dict[str, str], ...]] = {
    "secintel-features-v1": (
        {"feature": "is_unknown_identity",
         "class": "point_in_time_violation",
         "detail": "reflects the identity type CURRENT at compute time, not the "
                   "type as of the snapshot; an identity enrolled, renamed or "
                   "merged after as_of is labelled with hindsight"},
        {"feature": "days_since_last_seen",
         "class": "staleness",
         "detail": "derived from the 90-day appearance window read with "
                   "LIMIT 5000 ascending; for identities with more than 5000 "
                   "appearances in the window the newest rows are cut and the "
                   "value is stale (older than the truth), never from the future"},
        {"feature": "count and ratio features",
         "class": "rebuild_hindsight",
         "detail": "identity merges re-parent appearance rows; snapshots are "
                   "immutable once written, but a full_rebuild recomputes "
                   "history with post-merge ownership"},
    ),
    "secintel-features-v2": (
        {"feature": "is_unknown_identity",
         "class": "audit_trail_dependency",
         "detail": "the type as of the snapshot is reconstructed from promote/merge "
                   "audit rows; an identity whose audit rows were purged is treated "
                   "as known since creation"},
        {"feature": "count and ratio features",
         "class": "rebuild_hindsight",
         "detail": "identity merges re-parent appearance rows; snapshots are "
                   "immutable once written, but a full_rebuild recomputes "
                   "history with post-merge ownership"},
        {"feature": "windowed features",
         "class": "unavailable_beyond_cap",
         "detail": "identities with more than 5000 appearances in the 90-day window "
                   "get every windowed feature as unavailable (appearance_window_truncated) "
                   "rather than an undercount"},
    ),
    "coappearance-features-v1": (
        {"feature": "relationship cache",
         "class": "processing_time_observation",
         "detail": "pair snapshots observe the mutable relationship cache at collection time; "
                   "the system does not reconstruct historical pair state from today's row"},
        {"feature": "pair population",
         "class": "readiness_gated",
         "detail": "pairs below ML_GRAPH_MIN_PAIR_APPEARANCES carry an unavailable reason "
                   "instead of a fabricated zero"},
    ),
    "social-graph-features-v1": (
        {"feature": "relationship graph",
         "class": "processing_time_observation",
         "detail": "graph snapshots represent the cache at collection time, not a historical "
                   "graph reconstructed with future relationship rows"},
        {"feature": "graph population",
         "class": "readiness_gated",
         "detail": "no vectors are emitted until configured node, edge and observation-span "
                   "floors all pass"},
    ),
}


def feature_set_limitations(feature_set_version: str) -> List[Dict[str, str]]:
    return [dict(item) for item in FEATURE_SET_LIMITATIONS.get(feature_set_version, ())]


@dataclass(frozen=True)
class DatasetDefinition:
    name: str
    version: str
    purpose: str
    entity_type: str
    kind: str                                   # unsupervised | supervised
    feature_set_version: str = FEATURE_SET_VERSION
    label_definition_version: Optional[str] = None
    source_tables: Tuple[str, ...] = ("ml_feature_snapshots",)
    # Population filters applied in SQL (documented, not free-form)
    require_event_timestamp: bool = True        # excludes the collector current-state rows
    exclusions: Tuple[str, ...] = (
        "snapshots without event_timestamp (current-state rows, not events)",
        "snapshots of a different feature_set_version",
    )
    # Time range: explicit [start, end) at build time, else trailing_days
    # back from the build instant, else the whole history.
    trailing_days: Optional[int] = None
    # Cap + what to do when the population exceeds it. Default REFUSE: the
    # caller must choose a sampling policy knowingly; nothing is dropped in
    # silence. Every build records candidate/selected/excluded counts.
    row_cap: int = 100000
    sampling_policy: str = "refuse"
    # Split
    group_key: str = "entity_id"
    split_strategy: str = "temporal_group"
    val_fraction: float = 0.2
    holdout_fraction: float = 0.2
    # Scientific minimum — deliberately None: REQUIRES_VALIDATION.
    min_rows: Optional[int] = None
    notes: Tuple[str, ...] = ()

    def __post_init__(self):
        if self.kind not in DATASET_KINDS:
            raise ValueError(f"kind must be one of {DATASET_KINDS}")
        if self.sampling_policy not in SAMPLING_POLICIES:
            raise ValueError(f"sampling_policy must be one of {SAMPLING_POLICIES}")
        if self.row_cap <= 0:
            raise ValueError("row_cap must be positive")
        if self.split_strategy not in SPLIT_STRATEGIES:
            raise ValueError(f"split_strategy must be one of {SPLIT_STRATEGIES}")
        if self.kind == "supervised" and not self.label_definition_version:
            raise ValueError("a supervised definition must name its label_definition_version")

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def to_manifest(self) -> Dict[str, Any]:
        out = asdict(self)
        out["source_tables"] = list(self.source_tables)
        out["exclusions"] = list(self.exclusions)
        out["notes"] = list(self.notes)
        out["scientific_minimum"] = (
            self.min_rows if self.min_rows is not None else "REQUIRES_VALIDATION")
        return out


_PERSON_NOTES = (
    "same source population as the legacy builder (person snapshots of the "
    "active feature set with an event_timestamp); the physical extraction "
    "differs: the legacy builder applied a silent oldest-first 100k cap, this "
    "definition refuses or samples by a declared policy and records the counts",
)

_LABELED_EXCLUSIONS = (
    "snapshots without event_timestamp (current-state rows, not events)",
    "snapshots of a different feature_set_version",
    "labels that are not active, manual, reviewed and positive/negative",
    "labels with no snapshot at or before their event time",
)

# Definition versions pin the FEATURE SET they extract: v1 reads the frozen
# secintel-features-v1 snapshots (kept so v1 datasets stay reproducible),
# v2 reads the current set. Same source population and split policy.
DEFINITIONS: Dict[str, Dict[str, DatasetDefinition]] = {
    "behavior_anomaly_person": {
        "v1": DatasetDefinition(
            name="behavior_anomaly_person", version="v1",
            purpose="unsupervised behavioral-anomaly training rows: one row per "
                    "person feature snapshot (event-anchored), feature set v1",
            entity_type="person", kind="unsupervised",
            feature_set_version=PREVIOUS_FEATURE_SET_VERSION,
            notes=_PERSON_NOTES),
        "v2": DatasetDefinition(
            name="behavior_anomaly_person", version="v2",
            purpose="unsupervised behavioral-anomaly training rows: one row per "
                    "person feature snapshot (event-anchored), feature set v2",
            entity_type="person", kind="unsupervised",
            feature_set_version=FEATURE_SET_VERSION,
            notes=_PERSON_NOTES),
        # Same feature set, same population — only the EXTRACTION window
        # differs: the most recent 90 days, for the stronger candidate that
        # becomes possible once history exceeds ~90 days. Nothing about
        # secintel-features-v2 is redefined.
        "v3": DatasetDefinition(
            name="behavior_anomaly_person", version="v3",
            purpose="unsupervised behavioral-anomaly training rows over the trailing "
                    "90 days (event-anchored), feature set v2",
            entity_type="person", kind="unsupervised",
            feature_set_version=FEATURE_SET_VERSION, trailing_days=90,
            notes=_PERSON_NOTES + (
                "trailing-90-day window: intended once history_span_days >= 90 so that "
                "history-dependent features are available for most rows",)),
    },
    "behavior_anomaly_person_labeled": {
        "v1": DatasetDefinition(
            name="behavior_anomaly_person_labeled", version="v1",
            purpose="supervised rows: reviewed manual positive/negative labels "
                    "joined point-in-time to the latest snapshot at or before "
                    "the label event time, feature set v1",
            entity_type="person", kind="supervised",
            feature_set_version=PREVIOUS_FEATURE_SET_VERSION,
            label_definition_version=LABEL_DEFINITION_VERSION,
            source_tables=("ml_feature_snapshots", "ml_labels"),
            exclusions=_LABELED_EXCLUSIONS, notes=_PERSON_NOTES),
        "v2": DatasetDefinition(
            name="behavior_anomaly_person_labeled", version="v2",
            purpose="supervised rows: reviewed manual positive/negative labels "
                    "joined point-in-time to the latest snapshot at or before "
                    "the label event time, feature set v2",
            entity_type="person", kind="supervised",
            feature_set_version=FEATURE_SET_VERSION,
            label_definition_version=LABEL_DEFINITION_VERSION,
            source_tables=("ml_feature_snapshots", "ml_labels"),
            exclusions=_LABELED_EXCLUSIONS, notes=_PERSON_NOTES),
    },
    "coappearance_pair": {
        "v1": DatasetDefinition(
            name="coappearance_pair", version="v1",
            purpose=("unsupervised pair-level anomaly rows captured when the "
                     "relationship cache is refreshed; one immutable point-in-time "
                     "snapshot per canonical identity pair"),
            entity_type="pair", kind="unsupervised",
            feature_set_version=COAPPEARANCE_FEATURE_SET,
            notes=("Pair ids are canonical sorted UUIDs joined by '|'.",
                   "Rows are readiness-gated; missing graph evidence is never encoded as zero.")),
    },
    "social_graph_person": {
        "v1": DatasetDefinition(
            name="social_graph_person", version="v1",
            purpose=("unsupervised person-level graph-position anomaly rows "
                     "captured from a readiness-qualified relationship graph"),
            entity_type="person", kind="unsupervised",
            feature_set_version=SOCIAL_GRAPH_FEATURE_SET,
            notes=("Graph snapshots are processing-time observations of the relationship cache; "
                   "the builder never reconstructs historical graphs from current edges.",)),
    },
    "threat_ranking_person_labeled": {
        "v1": DatasetDefinition(
            name="threat_ranking_person_labeled", version="v1",
            purpose=("supervised analyst-review ranking from active, reviewed manual "
                     "positive/negative outcomes joined to the latest point-in-time person snapshot"),
            entity_type="person", kind="supervised",
            feature_set_version=FEATURE_SET_VERSION,
            label_definition_version=LABEL_DEFINITION_VERSION,
            source_tables=("ml_feature_snapshots", "ml_labels"),
            exclusions=_LABELED_EXCLUSIONS,
            notes=("The output is a relative review-rank score, not a calibrated probability "
                   "of threat and not a replacement for the rules engine.",)),
    },
}


def list_definitions() -> List[DatasetDefinition]:
    return [d for versions in DEFINITIONS.values() for d in versions.values()]


def get_definition(name: str, version: Optional[str] = None) -> DatasetDefinition:
    versions = DEFINITIONS.get(name)
    if not versions:
        raise KeyError(f"unknown dataset definition {name!r}")
    if version is None:
        version = sorted(versions, key=lambda v: int(v.lstrip("v")))[-1]
    if version not in versions:
        raise KeyError(f"unknown version {version!r} for dataset definition {name!r}")
    return versions[version]


DEFAULT_DEFINITION_VERSION = "v2"   # whole-history extraction; v3 (trailing 90 days) is opt-in


def default_definition_for_kind(kind: str) -> DatasetDefinition:
    """The definition legacy callers (name + kind only) are routed to —
    pinned explicitly so adding a newer definition never changes it silently."""
    return get_definition("behavior_anomaly_person_labeled" if kind == "supervised"
                          else "behavior_anomaly_person", DEFAULT_DEFINITION_VERSION)


def resolve_time_range(definition: DatasetDefinition, *,
                       start: Optional[datetime], end: Optional[datetime],
                       now: Optional[datetime] = None) -> Dict[str, Any]:
    """[start, end) actually applied, plus how it was chosen."""
    now = now or datetime.utcnow()
    if start is not None or end is not None:
        source = "explicit"
    elif definition.trailing_days:
        start, source = now - timedelta(days=definition.trailing_days), "trailing_days"
    else:
        source = "whole_history"
    if start is not None and end is not None and start >= end:
        raise ValueError("time_range_start must be before time_range_end")
    return {"start": start, "end": end, "source": source}
