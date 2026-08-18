"""
Versioned, immutable, checksummed datasets.

Postgres holds the metadata/lineage (ml_datasets row); Parquet holds the
rows (models/ml/datasets/, atomic tmp→fsync→verify→replace write, path
never serialized to clients). Versions are never overwritten — a rebuild
is always version N+1.

Splitting is temporal + group-aware: rows ordered by as_of, boundaries at
time quantiles, and an ENTITY never straddles a boundary (it moves wholly
to the earlier side). Rows at/after the holdout boundary form the untouched
final test period.
"""

import hashlib
import json
import logging
import os
import uuid as uuid_mod
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ml.constants import FEATURE_SET_VERSION, LABEL_DEFINITION_VERSION
from backend.ml.data_validator import validate_rows
from config import settings
from backend.utils.time_utils import iso_utc

logger = logging.getLogger(__name__)

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


def _code_version() -> Optional[str]:
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5, cwd="/app")
        return out.stdout.strip() or None
    except Exception:
        return None


PARQUET_COLUMNS = ("entity_id", "as_of", "label", "label_event_time", "split",
                   "features_json", "snapshot_id", "label_id")


def _atomic_parquet_write(rows: List[Dict[str, Any]], path: str,
                          *, expected_checksum: Optional[str] = None) -> Dict[str, Any]:
    """tmp → fsync → reload-verify → os.replace (house artifact discipline).

    Every row carries its LINEAGE: `snapshot_id` (the ml_feature_snapshots row
    it was built from) and, for supervised rows, `label_id`. After the write
    the file is READ BACK and verified — row count, the full column set,
    non-null snapshot_id on every row, and (when given) the dataset checksum
    recomputed from the reloaded rows — because the persisted artifact, not
    the in-memory frame, is what training reproduces from."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    os.makedirs(os.path.dirname(path), exist_ok=True)
    flat = [
        {
            "entity_id": row["entity_id"],
            "as_of": row["as_of"].isoformat(),
            "label": row.get("label"),
            "label_event_time": (row["label_event_time"].isoformat()
                                 if row.get("label_event_time") else None),
            "split": row.get("split"),
            "features_json": json.dumps(row["features"], sort_keys=True),
            "snapshot_id": int(row["snapshot_id"]),
            "label_id": (str(row["label_id"]) if row.get("label_id") else None),
        }
        for row in rows
    ]
    schema = pa.schema([
        ("entity_id", pa.string()), ("as_of", pa.string()), ("label", pa.string()),
        ("label_event_time", pa.string()), ("split", pa.string()),
        ("features_json", pa.string()), ("snapshot_id", pa.int64()), ("label_id", pa.string()),
    ])
    table = pa.Table.from_pylist(flat, schema=schema)
    tmp = path + ".tmp"
    pq.write_table(table, tmp)
    with open(tmp, "rb") as f:
        os.fsync(f.fileno())

    # ---- read-back verification (the artifact is the source of truth)
    reloaded = pq.read_table(tmp)
    if reloaded.num_rows != len(flat):
        raise RuntimeError(f"parquet reload-verify row mismatch {reloaded.num_rows} != {len(flat)}")
    missing_cols = [c for c in PARQUET_COLUMNS if c not in reloaded.column_names]
    if missing_cols:
        raise RuntimeError(f"parquet reload-verify missing columns {missing_cols}")
    back = reloaded.to_pylist()
    if any(r.get("snapshot_id") is None for r in back):
        raise RuntimeError("parquet reload-verify: a row lost its snapshot_id lineage")
    if expected_checksum is not None and back:
        recomputed = dataset_fingerprint([
            {"entity_id": r["entity_id"], "as_of": datetime.fromisoformat(r["as_of"]),
             "features": json.loads(r["features_json"]), "label": r.get("label")}
            for r in back])
        if recomputed != expected_checksum:
            raise RuntimeError("parquet reload-verify: checksum of the reloaded rows differs "
                               "from the dataset checksum")
    os.replace(tmp, path)
    snapshot_ids = [r["snapshot_id"] for r in back]
    return {
        "bytes": os.path.getsize(path),
        "lineage_summary": {
            "snapshot_count": len(snapshot_ids),
            "snapshot_id_min": min(snapshot_ids) if snapshot_ids else None,
            "snapshot_id_max": max(snapshot_ids) if snapshot_ids else None,
            "label_count": sum(1 for r in back if r.get("label_id")),
            "columns": list(PARQUET_COLUMNS),
        },
    }


async def build_dataset(db: AsyncSession, *, name: str, kind: str,
                        created_by: Optional[int] = None,
                        build_job_id: Optional[str] = None) -> Dict[str, Any]:
    """Build + validate + persist one dataset version. Returns the metadata
    dict (status='failed' with the quality report when validation fails —
    fail-safe, nothing partial is registered as usable)."""
    from db_models import MLDataset, MLFeatureSnapshot, MLLabel
    from backend.ml.feature_store import feature_store

    definitions = await feature_store.get_active_definitions(db, "person")

    snapshots = (await db.execute(
        select(MLFeatureSnapshot)
        .where(MLFeatureSnapshot.entity_type == "person",
               MLFeatureSnapshot.feature_set_version == FEATURE_SET_VERSION,
               MLFeatureSnapshot.event_timestamp.isnot(None))
        .order_by(MLFeatureSnapshot.as_of_timestamp)
        .limit(100000)
    )).scalars().all()

    rows: List[Dict[str, Any]] = [
        {
            "entity_id": snapshot.entity_id,
            "as_of": snapshot.as_of_timestamp,
            "features": dict(snapshot.features or {}),
            "unavailable": dict(snapshot.unavailable_features or {}),
            "snapshot_id": snapshot.id,
        }
        for snapshot in snapshots
    ]

    if kind == "supervised":
        # Reviewed manual labels only; join on subject + closest snapshot at
        # or before the label's event time (point-in-time anchor).
        labels = (await db.execute(
            select(MLLabel).where(
                MLLabel.status == "active",
                MLLabel.label_kind == "manual",
                MLLabel.review_status == "reviewed",
                MLLabel.label.in_(("positive", "negative")),
            ))).scalars().all()
        by_entity: Dict[str, List] = {}
        for row in rows:
            by_entity.setdefault(row["entity_id"], []).append(row)
        labeled_rows = []
        for label in labels:
            candidates = [
                r for r in by_entity.get(label.subject_id, [])
                if r["as_of"] <= label.event_time]
            if not candidates:
                continue
            example = dict(max(candidates, key=lambda r: r["as_of"]))
            example["label"] = label.label
            example["label_event_time"] = label.event_time
            example["label_id"] = label.id
            labeled_rows.append(example)
        rows = labeled_rows

    quality = validate_rows(rows, kind=kind, definitions=definitions)

    version = ((await db.execute(
        select(sa_func.max(MLDataset.version)).where(MLDataset.name == name)
    )).scalar() or 0) + 1

    dataset = MLDataset(
        id=uuid_mod.uuid4(),
        name=name, version=version, kind=kind,
        feature_set_version=FEATURE_SET_VERSION,
        label_definition_version=(LABEL_DEFINITION_VERSION if kind == "supervised" else None),
        source_cutoff=datetime.utcnow(),
        row_count=len(rows),
        quality_report=quality,
        missing_value_report={
            "features_with_warnings": quality.get("warnings", [])},
        checksum=dataset_fingerprint(rows) if rows else "empty",
        code_version=_code_version(),
        status="failed" if not quality["passed"] else "building",
        build_job_id=build_job_id,
        created_at=datetime.utcnow(),
        created_by=created_by,
    )

    if not quality["passed"]:
        db.add(dataset)
        await db.commit()
        return {"status": "failed", "dataset_id": str(dataset.id),
                "version": version, "quality_report": quality}

    train, val, test, split_meta = temporal_group_split(rows)
    for split_name, part in (("train", train), ("val", val), ("test", test)):
        for row in part:
            row["split"] = split_name

    artifact_dir = str(settings.ML_ARTIFACT_DIR)
    path = os.path.join(artifact_dir, "datasets", f"{name}-v{version}.parquet")
    written = _atomic_parquet_write(rows, path, expected_checksum=dataset.checksum)
    size = written["bytes"]
    dataset.lineage_summary = written["lineage_summary"]

    dataset.split_config = split_meta
    dataset.time_range_start = rows[0]["as_of"] if rows else None
    dataset.time_range_end = rows[-1]["as_of"] if rows else None
    dataset.holdout_boundary = (
        datetime.fromisoformat(split_meta["holdout_boundary"].rstrip("Z"))
        if split_meta.get("holdout_boundary") else None)
    if kind == "supervised":
        dataset.positive_count = sum(1 for r in rows if r.get("label") == "positive")
        dataset.negative_count = sum(1 for r in rows if r.get("label") == "negative")
    dataset.storage_path = path
    dataset.storage_bytes = size
    dataset.status = "built"
    db.add(dataset)
    await db.commit()

    logger.info("[ML_OPS] dataset built name=%s v%s kind=%s rows=%s checksum=%s",
                name, version, kind, len(rows), dataset.checksum[:12])
    return {
        "status": "built", "dataset_id": str(dataset.id), "name": name,
        "version": version, "kind": kind, "row_count": len(rows),
        "checksum": dataset.checksum, "split": split_meta,
        "quality_report": quality, "storage_bytes": size,
    }


def serialize_dataset(row) -> Dict[str, Any]:
    """Client payload — storage_path deliberately omitted."""
    def iso(dt):
        return iso_utc(dt) if dt else None
    return {
        "id": str(row.id), "name": row.name, "version": row.version,
        "kind": row.kind, "feature_set_version": row.feature_set_version,
        "label_definition_version": row.label_definition_version,
        "row_count": row.row_count, "positive_count": row.positive_count,
        "negative_count": row.negative_count,
        "time_range_start": iso(row.time_range_start),
        "time_range_end": iso(row.time_range_end),
        "holdout_boundary": iso(row.holdout_boundary),
        "split_config": row.split_config, "checksum": row.checksum,
        "quality_report": row.quality_report, "status": row.status,
        "code_version": row.code_version, "created_at": iso(row.created_at),
        "lineage_summary": getattr(row, "lineage_summary", None),
    }
