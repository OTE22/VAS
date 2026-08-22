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
from backend.ml.data_validator import MAX_NULL_RATE, validate_rows
from backend.ml.dataset_definitions import (
    DatasetDefinition, EXTRACTION_POLICY_VERSION, LEGACY_EXTRACTION_POLICY_VERSION,
    SAMPLING_POLICIES, default_definition_for_kind, feature_set_limitations,
    resolve_time_range, SPLIT_STRATEGIES)
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


def _repo_root() -> str:
    # backend/ml/dataset_builder.py -> repository root, wherever it is checked out
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _code_version() -> Optional[str]:
    """Code revision the dataset/model was produced by — the application's
    ONE resolver (backend.core.runtime_fingerprint._git_commit: injected
    GIT_COMMIT, then .git on disk, then the git binary). None when it is
    genuinely unknown; never guessed."""
    try:
        from backend.core.runtime_fingerprint import _git_commit
        value = (_git_commit() or "").strip()
        return None if not value or value == "unknown" else value[:64]
    except Exception:
        return None


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json_write(path: str, payload: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def manifest_path_for(parquet_path: str) -> str:
    return parquet_path[:-len(".parquet")] + ".manifest.json" if parquet_path.endswith(".parquet") \
        else parquet_path + ".manifest.json"


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
                        build_job_id: Optional[str] = None,
                        definition: Optional[DatasetDefinition] = None,
                        time_range_start: Optional[datetime] = None,
                        time_range_end: Optional[datetime] = None,
                        sampling_policy: Optional[str] = None,
                        split_strategy: Optional[str] = None) -> Dict[str, Any]:
    """Build + validate + persist one dataset version. Returns the metadata
    dict (status='failed' with the quality report when validation fails —
    fail-safe, nothing partial is registered as usable).

    Extraction is driven by a typed DatasetDefinition (legacy callers that
    pass only name + kind are routed to the default definition of that
    kind). The population is COUNTED first; if it exceeds the definition's
    cap the build either refuses (default) or applies the declared sampling
    policy — and `extraction` records candidate/selected/excluded counts
    either way. Nothing is discarded silently."""
    from db_models import MLDataset, MLFeatureSnapshot, MLLabel
    from backend.ml.feature_store import feature_store

    definition = definition or default_definition_for_kind(kind)
    if definition.kind != kind:
        raise ValueError(f"definition {definition.key} is {definition.kind}, build asked for {kind}")
    policy = sampling_policy or definition.sampling_policy
    if policy not in SAMPLING_POLICIES:
        raise ValueError(f"sampling_policy must be one of {SAMPLING_POLICIES}")
    time_range = resolve_time_range(definition, start=time_range_start, end=time_range_end)

    definitions = await feature_store.get_active_definitions(db, definition.entity_type)

    population = [
        MLFeatureSnapshot.entity_type == definition.entity_type,
        MLFeatureSnapshot.feature_set_version == definition.feature_set_version,
    ]
    if definition.require_event_timestamp:
        population.append(MLFeatureSnapshot.event_timestamp.isnot(None))
    if time_range["start"] is not None:
        population.append(MLFeatureSnapshot.as_of_timestamp >= time_range["start"])
    if time_range["end"] is not None:
        population.append(MLFeatureSnapshot.as_of_timestamp < time_range["end"])

    candidate_rows = int((await db.execute(
        select(sa_func.count(MLFeatureSnapshot.id)).where(*population))).scalar() or 0)
    cap = int(definition.row_cap)
    extraction: Dict[str, Any] = {
        "policy_version": EXTRACTION_POLICY_VERSION,
        "definition": definition.key,
        "candidate_rows": candidate_rows,
        "cap": cap,
        "sampling_policy": policy,
        "time_range": {
            "start": time_range["start"].isoformat() + "Z" if time_range["start"] else None,
            "end": time_range["end"].isoformat() + "Z" if time_range["end"] else None,
            "source": time_range["source"],
        },
        "filters": list(definition.exclusions),
    }

    if candidate_rows > cap and policy == "refuse":
        # Explicit refusal: the caller must pick a sampling policy knowingly.
        extraction.update({"selected_rows": 0, "excluded_rows": candidate_rows,
                           "ordering": None, "refused": "EXTRACTION_EXCEEDS_CAP"})
        quality = {
            "passed": False, "kind": kind, "row_count": 0,
            "checks": {"extraction_cap": {
                "passed": False, "current": candidate_rows, "required": cap,
                "detail": "population exceeds the definition cap and the sampling "
                          "policy is 'refuse'; rebuild with an explicit time range "
                          "or sampling_policy=newest_first|oldest_first"}},
            "warnings": [], "failed_checks": ["extraction_cap"],
            "validated_at": datetime.utcnow().isoformat() + "Z",
            "feature_set_limitations": feature_set_limitations(definition.feature_set_version),
        }
        version = ((await db.execute(
            select(sa_func.max(MLDataset.version)).where(MLDataset.name == name)
        )).scalar() or 0) + 1
        failed = MLDataset(
            id=uuid_mod.uuid4(), name=name, version=version, kind=kind,
            feature_set_version=definition.feature_set_version,
            label_definition_version=definition.label_definition_version,
            source_cutoff=time_range["end"] or datetime.utcnow(),
            row_count=0, quality_report=quality, checksum="empty",
            code_version=_code_version(), status="failed",
            build_job_id=build_job_id, created_at=datetime.utcnow(),
            created_by=created_by, definition_name=definition.name,
            definition_version=definition.version, extraction=extraction)
        db.add(failed)
        await db.commit()
        logger.warning("[ML_OPS] dataset build refused name=%s: %s candidate rows exceed cap %s",
                       name, candidate_rows, cap)
        return {"status": "failed", "refusal": "EXTRACTION_EXCEEDS_CAP",
                "dataset_id": str(failed.id), "version": version,
                "quality_report": quality, "extraction": extraction}

    if candidate_rows > cap and policy == "newest_first":
        ordering = "as_of DESC LIMIT cap (newest kept), re-sorted ascending"
        query = (select(MLFeatureSnapshot).where(*population)
                 .order_by(MLFeatureSnapshot.as_of_timestamp.desc(), MLFeatureSnapshot.id.desc())
                 .limit(cap))
    else:
        ordering = ("as_of ASC LIMIT cap (oldest kept)" if candidate_rows > cap
                    else "as_of ASC (no cap applied)")
        query = (select(MLFeatureSnapshot).where(*population)
                 .order_by(MLFeatureSnapshot.as_of_timestamp, MLFeatureSnapshot.id)
                 .limit(cap))
    snapshots = list((await db.execute(query)).scalars().all())
    snapshots.sort(key=lambda snap: (snap.as_of_timestamp, snap.id))
    extraction.update({"selected_rows": len(snapshots),
                       "excluded_rows": max(0, candidate_rows - len(snapshots)),
                       "ordering": ordering})

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

        # Feature selection by coverage, BEFORE validation. The validator's
        # hard rule — a supervised feature missing in >MAX_NULL_RATE of rows
        # fails the build — is right: a model trained on a mostly-absent
        # column is fiction. But applied to the raw inventory it let ONE
        # sparse feature veto the whole dataset: baseline_hour_deviation_last
        # needs six prior sightings and is therefore unavailable for almost
        # everyone on a young or quiet deployment, so no supervised dataset
        # could ever be built. Columns that cannot be learned from are
        # dropped here, by name, and the drop is recorded in the quality
        # report so the lineage says exactly which features the model never
        # saw. The validator then judges what remains.
        excluded = {}
        if rows:
            safe_names = [d["name"] for d in definitions
                          if d.get("leakage_class", "safe") == "safe"]
            for name in safe_names:
                missing = sum(1 for r in rows if name not in (r.get("features") or {}))
                rate = missing / len(rows)
                if rate > MAX_NULL_RATE:
                    excluded[name] = round(rate, 3)
            if excluded:
                for r in rows:
                    for name in excluded:
                        (r.get("features") or {}).pop(name, None)
                definitions = [d for d in definitions if d["name"] not in excluded]
                logger.warning("[ML_OPS] supervised dataset: excluded %d sparse feature(s) "
                               "above %.0f%% missing: %s", len(excluded),
                               MAX_NULL_RATE * 100, excluded)

    quality = validate_rows(rows, kind=kind, definitions=definitions)
    quality["feature_set_limitations"] = feature_set_limitations(definition.feature_set_version)
    quality["extraction"] = dict(extraction)
    if kind == "supervised":
        quality["excluded_sparse_features"] = excluded if rows else {}
        if excluded:
            quality.setdefault("warnings", []).append(
                "excluded sparse features (missing > %.0f%%): %s"
                % (MAX_NULL_RATE * 100, ", ".join(sorted(excluded))))

    version = ((await db.execute(
        select(sa_func.max(MLDataset.version)).where(MLDataset.name == name)
    )).scalar() or 0) + 1

    dataset = MLDataset(
        id=uuid_mod.uuid4(),
        name=name, version=version, kind=kind,
        feature_set_version=definition.feature_set_version,
        label_definition_version=definition.label_definition_version,
        source_cutoff=time_range["end"] or datetime.utcnow(),
        definition_name=definition.name,
        definition_version=definition.version,
        extraction=extraction,
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

    effective_split = split_strategy or definition.split_strategy
    if effective_split not in SPLIT_STRATEGIES:
        raise ValueError(f"split_strategy must be one of {SPLIT_STRATEGIES}")
    train, val, test, split_meta = split_rows(
        rows, effective_split, val_fraction=definition.val_fraction,
        holdout_fraction=definition.holdout_fraction)
    split_meta["declared_by"] = "build_request" if split_strategy else "definition"
    extraction["split_strategy"] = effective_split
    dataset.extraction = dict(extraction)  # explicit reassignment: JSONB in-place edits are not tracked
    for split_name, part in (("train", train), ("val", val), ("test", test)):
        for row in part:
            row["split"] = split_name

    # Population maturity + feature availability BY SPLIT: the difference
    # between "the pipeline works" and "the population is behaviourally
    # mature" is stated on the quality report, never inferred from row count.
    from backend.ml.readiness import (
        cold_start_conclusion, entity_history_statistics, feature_availability_by_split)
    feature_names = sorted({d["name"] for d in definitions})
    retained = train + val + test
    availability = feature_availability_by_split(retained, feature_names)
    population = await entity_history_statistics(
        db, [r["entity_id"] for r in retained], feature_names, rows=retained)
    quality["feature_availability_by_split"] = availability
    quality["population"] = population
    quality["maturity"] = cold_start_conclusion(population, availability)

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
    dataset.parquet_sha256 = _sha256_file(path)

    # Sidecar manifest: everything needed to answer "exactly which data
    # produced this model" next to the bytes themselves. No paths, no
    # credentials — identifiers, versions, counts and hashes only.
    manifest = {
        "manifest_version": 1,
        "dataset_id": str(dataset.id), "name": name, "version": version, "kind": kind,
        "definition": definition.to_manifest(),
        "extraction": extraction,
        "feature_set_version": definition.feature_set_version,
        "feature_set_limitations": feature_set_limitations(definition.feature_set_version),
        "label_definition_version": definition.label_definition_version,
        "time_range": {"start": iso_utc(dataset.time_range_start) if dataset.time_range_start else None,
                       "end": iso_utc(dataset.time_range_end) if dataset.time_range_end else None},
        "row_count": len(rows), "column_count": len(PARQUET_COLUMNS),
        "columns": list(PARQUET_COLUMNS),
        "split": split_meta,
        "checksum": dataset.checksum,            # canonical-row fingerprint
        "parquet_sha256": dataset.parquet_sha256,  # file bytes
        "parquet_bytes": size,
        "lineage_summary": dataset.lineage_summary,
        "quality": {"passed": True, "warnings": quality.get("warnings", []),
                    "excluded_sparse_features": quality.get("excluded_sparse_features", {})},
        "population": quality.get("population"),
        "feature_availability_by_split": quality.get("feature_availability_by_split"),
        "maturity": quality.get("maturity"),
        "code_version": dataset.code_version,
        "build_job_id": build_job_id,
        "created_at": iso_utc(dataset.created_at),
        "comparability": (
            "same purpose/source semantics as earlier versions of this name; "
            "physical extraction follows extraction.policy_version — versions "
            "built under a different policy are not extraction-identical"),
    }
    m_path = manifest_path_for(path)
    _atomic_json_write(m_path, manifest)
    dataset.manifest_path = m_path
    dataset.status = "built"
    db.add(dataset)
    await db.commit()

    logger.info("[ML_OPS] dataset built name=%s v%s kind=%s rows=%s checksum=%s",
                name, version, kind, len(rows), dataset.checksum[:12])
    return {
        "status": "built", "dataset_id": str(dataset.id), "name": name,
        "version": version, "kind": kind, "row_count": len(rows),
        "checksum": dataset.checksum, "parquet_sha256": dataset.parquet_sha256,
        "split": split_meta, "quality_report": quality, "storage_bytes": size,
        "definition": definition.key, "extraction": extraction,
    }


def extraction_for(row) -> Dict[str, Any]:
    """The extraction record of a dataset row. Rows built before revision
    a9c4e2d7f1b3 carry none; they are reported — at read time, never
    rewritten — as products of the legacy silent oldest-first cap."""
    stored = getattr(row, "extraction", None)
    if stored:
        return dict(stored)
    return {"policy_version": LEGACY_EXTRACTION_POLICY_VERSION,
            "sampling_policy": "oldest_first", "cap": 100000,
            "candidate_rows": None, "selected_rows": getattr(row, "row_count", None),
            "excluded_rows": None,
            "note": "built before extraction auditing; rows were the oldest "
                    "<=100000 event-anchored snapshots, excluded count unknown"}


def read_manifest(row) -> Optional[Dict[str, Any]]:
    path = getattr(row, "manifest_path", None)
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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
        "definition_name": getattr(row, "definition_name", None),
        "definition_version": getattr(row, "definition_version", None),
        "extraction": extraction_for(row),
        "parquet_sha256": getattr(row, "parquet_sha256", None),
        "has_manifest": bool(getattr(row, "manifest_path", None)),
        "file_present": (os.path.exists(row.storage_path)
                         if getattr(row, "storage_path", None) else None),
    }


# ---------------------------------------------------------------------------
# Legacy datasets: prove the file, then record its hash (never rewrite lineage)
# ---------------------------------------------------------------------------

async def backfill_dataset_file_hashes(db: AsyncSession) -> Dict[str, Any]:
    """For every built dataset that predates Parquet file hashing: reload the
    Parquet, recompute the canonical-row fingerprint and, ONLY if it equals
    the registered `checksum` (so the file on disk is provably the registered
    dataset), record `parquet_sha256` and write a manifest that says the row
    was produced by the legacy extraction policy. Nothing else on the row is
    touched; a row whose file is missing or whose rows no longer match stays
    unverifiable and is reported as such."""
    from db_models import MLDataset
    rows = (await db.execute(
        select(MLDataset).where(MLDataset.status == "built",
                                MLDataset.parquet_sha256.is_(None))
        .order_by(MLDataset.created_at))).scalars().all()
    report: Dict[str, Any] = {"verified": [], "unverifiable": [], "checked": len(rows)}
    for row in rows:
        ident = {"dataset_id": str(row.id), "name": row.name, "version": row.version}
        if not row.storage_path or not os.path.exists(row.storage_path):
            report["unverifiable"].append({**ident, "reason": "DATASET_FILE_MISSING"})
            continue
        try:
            import pyarrow.parquet as pq
            back = pq.read_table(row.storage_path).to_pylist()
            recomputed = dataset_fingerprint([
                {"entity_id": r["entity_id"], "as_of": datetime.fromisoformat(r["as_of"]),
                 "features": json.loads(r["features_json"]), "label": r.get("label")}
                for r in back]) if back else "empty"
        except Exception as exc:  # unreadable file is a finding, not a crash
            report["unverifiable"].append({**ident, "reason": "DATASET_FILE_UNREADABLE",
                                           "detail": str(exc)[:200]})
            continue
        if recomputed != row.checksum:
            report["unverifiable"].append({**ident, "reason": "DATASET_CHECKSUM_MISMATCH"})
            continue
        row.parquet_sha256 = _sha256_file(row.storage_path)
        if not row.manifest_path:
            manifest = {
                "manifest_version": 1,
                "dataset_id": str(row.id), "name": row.name, "version": row.version,
                "kind": row.kind,
                "definition": None,
                "extraction": extraction_for(row),
                "feature_set_version": row.feature_set_version,
                "feature_set_limitations": feature_set_limitations(row.feature_set_version),
                "label_definition_version": row.label_definition_version,
                "time_range": {"start": iso_utc(row.time_range_start) if row.time_range_start else None,
                               "end": iso_utc(row.time_range_end) if row.time_range_end else None},
                "row_count": row.row_count, "column_count": len(PARQUET_COLUMNS),
                "columns": list(PARQUET_COLUMNS),
                "split": row.split_config,
                "checksum": row.checksum,
                "parquet_sha256": row.parquet_sha256,
                "parquet_bytes": os.path.getsize(row.storage_path),
                "lineage_summary": row.lineage_summary,
                "code_version": row.code_version,
                "build_job_id": row.build_job_id,
                "created_at": iso_utc(row.created_at) if row.created_at else None,
                "backfilled_at": datetime.utcnow().isoformat() + "Z",
                "comparability": (
                    "built under the legacy silent oldest-first cap; the file hash was "
                    "recorded later after the reloaded rows reproduced the registered "
                    "checksum. Not extraction-identical to explicit-cap-v1 datasets."),
            }
            m_path = manifest_path_for(row.storage_path)
            _atomic_json_write(m_path, manifest)
            row.manifest_path = m_path
        report["verified"].append({**ident, "parquet_sha256": row.parquet_sha256})
    await db.commit()
    return report


class DatasetArchiveRefusal(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def archive_dataset(db: AsyncSession, dataset_id: str, *, actor: Optional[str] = None,
                          reason: str = "") -> Dict[str, Any]:
    """Explicit administrator action — never an automatic sweep: dataset rows
    are provenance. The Parquet bytes of a dataset that NO registered model
    references may be released; the row stays (status=archived), the manifest
    stays, and the row reports file_present=false from then on. A dataset any
    model was trained from is refused: its file is part of that model's lineage."""
    from db_models import MLDataset, MLModel
    try:
        row_uuid = uuid_mod.UUID(str(dataset_id))
    except (ValueError, TypeError):
        raise DatasetArchiveRefusal("DATASET_NOT_FOUND", "not a dataset id")
    row = (await db.execute(select(MLDataset).where(MLDataset.id == row_uuid))).scalar_one_or_none()
    if row is None:
        raise DatasetArchiveRefusal("DATASET_NOT_FOUND", "no such dataset")
    if row.status == "archived":
        raise DatasetArchiveRefusal("DATASET_ALREADY_ARCHIVED", "already archived")
    referencing = (await db.execute(
        select(sa_func.count(MLModel.id)).where(MLModel.dataset_id == row.id))).scalar() or 0
    if referencing:
        raise DatasetArchiveRefusal(
            "DATASET_REFERENCED_BY_MODEL",
            f"{referencing} registered model(s) were trained from this dataset; its file is their lineage")
    released = 0
    if row.storage_path and os.path.exists(row.storage_path):
        released = os.path.getsize(row.storage_path)
        os.remove(row.storage_path)
    row.status = "archived"
    await db.commit()
    logger.info("[ML_OPS] dataset archived name=%s v%s by=%s reason=%s bytes=%s",
                row.name, row.version, actor, reason, released)
    return {"status": "archived", "dataset_id": str(row.id), "name": row.name,
            "version": row.version, "bytes_released": released, "manifest_kept": bool(row.manifest_path)}
