"""Read-only, bounded inspection of the exact dataset artifact used by training.

The saved build report remains the authority for validation. Explorer statistics
describe the inspected prefix, independently of display filters, and never repair
or rewrite a dataset. No artifact deserialization (pickle/joblib) is involved.
"""
import json
import math
from collections import Counter
from pathlib import Path

SCAN_LIMIT = 100_000


def summarize_records(records, *, total_rows, schema, split=None, label=None,
                      query="", page=1, page_size=25):
    classes, present = Counter(), Counter()
    seen, feature_names, matches = set(), set(), []
    duplicates = invalid = scanned = 0
    for raw in records:
        scanned += 1
        row = dict(raw)
        try:
            features = json.loads(row.pop("features_json", "{}") or "{}")
            if not isinstance(features, dict):
                raise ValueError("features must be an object")
        except (ValueError, TypeError):
            features = {}
        bad = not features or not row.get("entity_id") or not row.get("as_of")
        clean = {}
        for name, value in features.items():
            feature_names.add(name)
            if value is not None:
                present[name] += 1
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    bad = True
                    value = None
                elif (name.endswith("_ratio") or name.endswith("_ratio_30d")) and not 0 <= value <= 1:
                    bad = True
            clean[name] = value
        row["features"] = clean
        key = (row.get("entity_id"), row.get("as_of"))
        duplicates += key in seen
        seen.add(key)
        invalid += bool(bad)
        classes[row.get("label") or "unlabelled"] += 1
        if split and row.get("split") != split:
            continue
        if label and (row.get("label") or "unlabelled") != label:
            continue
        if query and query.casefold() not in json.dumps(row, ensure_ascii=False).casefold():
            continue
        matches.append(row)
    start = (page - 1) * page_size
    return {
        "total_rows": total_rows, "scanned_rows": scanned,
        "truncated": scanned < total_rows, "scan_limit": SCAN_LIMIT,
        "schema": schema, "column_count": len(schema),
        "feature_count": len(feature_names),
        "class_distribution": dict(classes),
        "missing_values": {name: scanned - present[name] for name in sorted(feature_names)},
        "duplicates": duplicates, "invalid_rows": invalid,
        "invalid_rows_definition": "Missing identity/time, empty or malformed features, nonnumeric/nonfinite values, or ratios outside [0,1]. Build validation below also checks leakage and timestamps.",
        "items": matches[start:start + page_size], "filtered_rows": len(matches),
        "page": page, "page_size": page_size,
    }


def explore_dataset(row, artifact_root, **filters):
    import pyarrow.parquet as pq

    if not row.storage_path:
        raise FileNotFoundError("Dataset has no artifact")
    root = Path(artifact_root).resolve()
    path = Path(row.storage_path).resolve()
    if not path.is_relative_to(root) or path.suffix != ".parquet":
        raise ValueError("Dataset artifact is outside the configured root")
    with path.open("rb") as handle:
        parquet = pq.ParquetFile(handle)
        schema = [{"name": f.name, "type": str(f.type), "nullable": f.nullable}
                  for f in parquet.schema_arrow]

        def records():
            remaining = SCAN_LIMIT
            for batch in parquet.iter_batches(batch_size=1024):
                for item in batch.to_pylist()[:remaining]:
                    yield item
                remaining -= batch.num_rows
                if remaining <= 0:
                    break

        result = summarize_records(records(), total_rows=parquet.metadata.num_rows,
                                   schema=schema, **filters)
    result.update(dataset_id=str(row.id), version=row.version,
                  source=row.definition_name or "Feature snapshots",
                  checksum=row.checksum, validation_report=row.quality_report,
                  stored_missing_value_report=row.missing_value_report)
    return result
