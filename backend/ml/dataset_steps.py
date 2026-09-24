"""Pure dataset fingerprint and split functions shared by builds and notebooks.

This module deliberately has no database, settings or filesystem imports.
"""
import hashlib
import json
from typing import Any, Dict, List, Tuple

DATASET_SEED = 42
VAL_FRACTION = 0.2
HOLDOUT_FRACTION = 0.2


def dataset_fingerprint(rows: List[Dict[str, Any]]) -> str:
    """Deterministic sha256 over canonically ordered rows."""
    canonical = json.dumps(
        [
            {
                "entity_id": row["entity_id"],
                "as_of": row["as_of"].isoformat(),
                "features": {k: row["features"][k] for k in sorted(row["features"])},
                "label": row.get("label"),
            }
            for row in sorted(rows, key=lambda r: (r["entity_id"], r["as_of"].isoformat()))
        ],
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def temporal_group_split(rows: List[Dict[str, Any]], *,
                         seed: int = DATASET_SEED,
                         val_fraction: float = VAL_FRACTION,
                         holdout_fraction: float = HOLDOUT_FRACTION
                         ) -> Tuple[List, List, List, Dict[str, Any]]:
    """Time-ordered split with group integrity.

    1. Sort by as_of; the last `holdout_fraction` of TIME (not rows) is the
       untouched test period; before it, the last `val_fraction` is
       validation.
    2. Group-awareness: an entity with rows on both sides of a boundary
       moves ENTIRELY to the earlier side (no person leaks across).
    Deterministic — no randomness is actually consumed, but the seed is
    recorded so the metadata is complete and future samplers stay seeded.
    """
    if not rows:
        return [], [], [], {"method": "temporal_group", "seed": seed,
                            "boundaries": None, "group_key": "entity_id"}
    ordered = sorted(rows, key=lambda r: r["as_of"])
    t0, t1 = ordered[0]["as_of"], ordered[-1]["as_of"]
    span = (t1 - t0).total_seconds() or 1.0
    holdout_boundary = t0 + (t1 - t0) * (1.0 - holdout_fraction)
    val_boundary = t0 + (t1 - t0) * (1.0 - holdout_fraction - val_fraction)

    def initial_bucket(row):
        if row["as_of"] >= holdout_boundary:
            return "test"
        if row["as_of"] >= val_boundary:
            return "val"
        return "train"

    # Entity -> earliest bucket in temporal order (train < val < test). An
    # entity belongs WHOLLY to its earliest bucket; any of its rows falling
    # in a LATER time window are DROPPED, not moved — both properties hold:
    # no entity straddles a boundary AND no future-period row leaks into an
    # earlier split.
    rank = {"train": 0, "val": 1, "test": 2}
    entity_bucket: Dict[str, str] = {}
    for row in ordered:
        bucket = initial_bucket(row)
        current = entity_bucket.get(row["entity_id"])
        if current is None or rank[bucket] < rank[current]:
            entity_bucket[row["entity_id"]] = bucket

    train, val, test = [], [], []
    dropped = 0
    for row in ordered:
        assigned = entity_bucket[row["entity_id"]]
        own = initial_bucket(row)
        if own != assigned:
            dropped += 1  # row's time window disagrees with its entity's split
            continue
        (train if assigned == "train" else val if assigned == "val" else test).append(row)
    meta = {
        "method": "temporal_group",
        "seed": seed,
        "group_key": "entity_id",
        "val_boundary": val_boundary.isoformat() + "Z",
        "holdout_boundary": holdout_boundary.isoformat() + "Z",
        "span_seconds": span,
        "counts": {"train": len(train), "val": len(val), "test": len(test)},
        "dropped_for_group_integrity": dropped,
        "group_counts": {
            "train": len({r["entity_id"] for r in train}),
            "val": len({r["entity_id"] for r in val}),
            "test": len({r["entity_id"] for r in test}),
        },
    }
    return train, val, test, meta


def temporal_split(rows: List[Dict[str, Any]], *,
                   seed: int = DATASET_SEED,
                   val_fraction: float = VAL_FRACTION,
                   holdout_fraction: float = HOLDOUT_FRACTION
                   ) -> Tuple[List, List, List, Dict[str, Any]]:
    """Time-ordered split WITHOUT group isolation: the same time boundaries
    as temporal_group_split, every row in the bucket of its own as_of, no
    row dropped. Entities may recur across splits — that overlap is measured
    and recorded so nobody reads test scores as unseen-entity generalisation.
    No future-period row ever lands in an earlier split."""
    if not rows:
        return [], [], [], {"method": "temporal", "seed": seed,
                            "boundaries": None, "group_key": "entity_id"}
    ordered = sorted(rows, key=lambda r: r["as_of"])
    t0, t1 = ordered[0]["as_of"], ordered[-1]["as_of"]
    span = (t1 - t0).total_seconds() or 1.0
    holdout_boundary = t0 + (t1 - t0) * (1.0 - holdout_fraction)
    val_boundary = t0 + (t1 - t0) * (1.0 - holdout_fraction - val_fraction)
    train, val, test = [], [], []
    for row in ordered:
        if row["as_of"] >= holdout_boundary:
            test.append(row)
        elif row["as_of"] >= val_boundary:
            val.append(row)
        else:
            train.append(row)
    train_entities = {r["entity_id"] for r in train}
    val_entities = {r["entity_id"] for r in val}
    test_entities = {r["entity_id"] for r in test}

    def _overlap(part, part_entities):
        shared = part_entities & train_entities
        rows_from_train = sum(1 for r in part if r["entity_id"] in train_entities)
        return {"entities_shared_with_train": len(shared),
                "rows_of_train_entities": rows_from_train,
                "row_fraction_of_train_entities": (round(rows_from_train / len(part), 4) if part else None)}

    meta = {
        "method": "temporal",
        "seed": seed,
        "group_key": "entity_id",
        "val_boundary": val_boundary.isoformat() + "Z",
        "holdout_boundary": holdout_boundary.isoformat() + "Z",
        "span_seconds": span,
        "counts": {"train": len(train), "val": len(val), "test": len(test)},
        "dropped_for_group_integrity": 0,
        "group_counts": {"train": len(train_entities), "val": len(val_entities), "test": len(test_entities)},
        "entity_overlap": {"val": _overlap(val, val_entities), "test": _overlap(test, test_entities)},
        "caveat": "entities recur across splits by design: val/test scores describe the later "
                  "behaviour of known entities, not generalisation to unseen entities",
    }
    return train, val, test, meta


def split_rows(rows: List[Dict[str, Any]], strategy: str, *,
               val_fraction: float = VAL_FRACTION,
               holdout_fraction: float = HOLDOUT_FRACTION
               ) -> Tuple[List, List, List, Dict[str, Any]]:
    """Dispatch on a DECLARED strategy; unknown strategies are refused."""
    if strategy == "temporal_group":
        return temporal_group_split(rows, val_fraction=val_fraction, holdout_fraction=holdout_fraction)
    if strategy == "temporal":
        return temporal_split(rows, val_fraction=val_fraction, holdout_fraction=holdout_fraction)
    raise ValueError(f"unknown split strategy {strategy!r}")


