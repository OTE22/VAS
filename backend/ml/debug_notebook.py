"""Generate a credential-free notebook over recorded evidence and immutable data.

The two executable helper cells are the same pure sources used by production.
No notebook kernel runs in the API and exports never invoke extraction/training.
"""
import hashlib
import json
from pathlib import Path

HELPERS = ('dataset_steps.py', 'data_validator.py')


def helper_sources():
    return {name: Path(__file__).with_name(name).read_text() for name in HELPERS}


def helper_hashes():
    return {name: hashlib.sha256(source.encode()).hexdigest() for name, source in helper_sources().items()}


def build_debug_notebook(evidence):
    cells = []

    def markdown(text):
        cells.append({'cell_type': 'markdown', 'metadata': {}, 'source': text.splitlines(True)})

    def code(text):
        cells.append({'cell_type': 'code', 'execution_count': None, 'metadata': {},
                      'outputs': [], 'source': text.splitlines(True)})

    markdown('# Dataset pipeline debugger\n'
             'Run from top to bottom. Each stage has its own cell. Use JupyterLab’s debugger to inspect variables and exceptions.\n\n'
             'This notebook inspects saved evidence and rechecks a saved snapshot. It does not rerun database extraction, '
             'reconstruct unsaved inputs, train a model, or update production. '
             'Cells use the exported production validation and split functions. '
             'For builds without recorded helper hashes, rechecks use export-time code; they are not proof of historical equivalence.\n\n'
             '**Requirements:** Python 3.11+ and `pyarrow`. Upload this notebook to the separate debug workspace, '
             'or open it locally and point `ARTIFACT_ROOT` at a read-only copy of the ML artifacts directory. '
             'Do not add production credentials. Notebook outputs may contain dataset records; clear them before sharing.')
    code('import json\nfrom pathlib import Path\nfrom pprint import pprint\n'
         'from datetime import datetime, timezone\n'
         'evidence = json.loads(' + repr(json.dumps(evidence, ensure_ascii=True)) + ')\n'
         'ARTIFACT_ROOT = Path("/artifacts")\n'
         'pprint({k: evidence.get(k) for k in ("job", "dataset", "artifact_available")})\n')
    markdown('## 1 · Recorded stage history\nFailed stages identify the boundary reached by the worker. '
             'Interrupted jobs may end with a running stage; this does not mean the stage completed. '
             'Older jobs have no stage-by-stage record.')
    code('diagnostics = evidence.get("diagnostics") or {}\n'
         'for event in diagnostics.get("stage_history", []):\n    pprint(event)\n'
         'pprint(diagnostics.get("failure") or "No recorded failure details")\n')
    markdown('## 2 · Extraction, labels and validation evidence\n'
             'Counts and exclusions come from the saved build. Upstream source rows and pre-label intermediate rows are not reconstructed.')
    code('dataset = evidence.get("dataset") or {}\n'
         'quality = dataset.get("quality_report") or {}\n'
         'pprint(diagnostics.get("configuration") or "Build configuration not recorded")\n'
         'pprint(dataset.get("extraction") or "Extraction evidence unavailable")\n'
         'pprint(quality)\n')
    if not evidence.get('artifact_available'):
        markdown('## Replay unavailable\nThis job has no available immutable dataset snapshot. '
                 'Use the recorded stage, failed checks and code locations above to investigate the worker log reference. '
                 'After correcting the cause, submit a new build through ML Ops. No data is fabricated for this notebook.')
    else:
        markdown('## 3 · Shared production functions\nThe following cells contain the exact helper source at export time. '
                 'New builds record helper hashes so the next cell can check for code drift.')
        sources = helper_sources()
        for name, source in sources.items():
            markdown('### ' + name)
            code(source)
        code('exported_hashes = ' + repr(helper_hashes()) + '\n'
             'recorded = (quality.get("debug_contract") or {}).get("helper_sha256")\n'
             'if recorded:\n    assert recorded == exported_hashes, "Helper code changed since this build. Use the matching application release."\n'
             'else:\n    print("Historical helper hashes unavailable; this is an export-time recheck.")\n')
        markdown('## 4 · Verify and load the snapshot\nA missing file or hash mismatch stops execution. '
                 'This reads the full saved snapshot, not the dataset explorer’s preview. Adjust the root only for a local read-only copy.')
        code('import hashlib\nimport pyarrow.parquet as pq\n'
             'artifact = (ARTIFACT_ROOT / evidence["artifact_relative"]).resolve()\n'
             'assert artifact.is_relative_to(ARTIFACT_ROOT.resolve()), "Artifact is outside the selected root"\n'
             'assert artifact.is_file(), "Snapshot missing: check the read-only artifacts mount"\n'
             'expected_hash = dataset.get("parquet_sha256")\n'
             'assert expected_hash, "No recorded file hash. Verify legacy hashes through ML Ops first."\n'
             'digest = hashlib.sha256()\n'
             'with artifact.open("rb") as handle:\n    for chunk in iter(lambda: handle.read(1024 * 1024), b""):\n        digest.update(chunk)\n'
             'assert digest.hexdigest() == expected_hash, "Dataset file checksum mismatch"\n'
             'parquet = pq.ParquetFile(artifact)\n'
             'assert parquet.metadata.num_rows <= 100_000, "Snapshot exceeds notebook inspection limit"\n'
             'raw_rows = parquet.read().to_pylist()\n'
             'def parse_time(value):\n    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None) if value else None\n'
             'rows = [{**row, "features": json.loads(row["features_json"]), "as_of": parse_time(row["as_of"]),\n'
             '         "label_event_time": parse_time(row.get("label_event_time"))} for row in raw_rows]\n'
             'assert dataset_fingerprint(rows) == dataset["checksum"], "Logical dataset checksum mismatch"\n'
             'print("Verified rows:", len(rows))\npprint(rows[:3])\n')
        markdown('## 5 · Inspect feature coverage and label counts')
        code('from collections import Counter\n'
             'names = sorted({name for row in rows for name in row["features"]})\n'
             'pprint({name: {"missing": sum(row["features"].get(name) is None for row in rows), "total": len(rows)} for name in names})\n'
             'pprint(Counter(row.get("label") or "unlabelled" for row in rows))\n')
        markdown('## 6 · Re-run validation\nNew builds include the feature definitions and validation time used in production. '
                 'For legacy builds, this cell stops if that evidence is missing; the saved validation report above remains available.')
        code('contract = quality.get("debug_contract") or {}\n'
             'definitions = contract.get("definitions")\n'
             'assert definitions is not None, "Frozen feature definitions were not recorded for this build"\n'
             'assert quality.get("validated_at"), "Validation time not recorded"\n'
             'rechecked = validate_rows(rows, kind=dataset["kind"], definitions=definitions, now=parse_time(quality["validated_at"]))\n'
             'pprint(rechecked)\n'
             'assert rechecked["checks"] == quality["checks"], "Validation checks differ from the recorded report"\n')
        markdown('## 7 · Inspect and re-run the declared split\nThis does not save any changes. '
                 'Group exclusions are reported separately from retained training, validation and test rows.')
        code('contract = quality.get("debug_contract") or {}\n'
             'split_config = contract.get("split")\n'
             'assert split_config, "Exact split fractions were not recorded for this build"\n'
             'train, validation, test, split_report = split_rows(rows, split_config["strategy"],\n'
             '    val_fraction=split_config["val_fraction"], holdout_fraction=split_config["holdout_fraction"])\n'
             'pprint(split_report)\n'
             'pprint({"saved_assignments": dict(Counter(row.get("split") or "excluded" for row in rows))})\n'
             'for split_name, part in (("train", train), ("val", validation), ("test", test)):\n'
             '    assert all(row.get("split") == split_name for row in part), "Split assignments differ from the artifact"\n'
             'assert split_report.get("counts") == (dataset.get("split_config") or {}).get("counts"), "Split counts changed"\n')
    markdown('## Next action\nUse the first failed cell or recorded stage to identify the cause. '
             'Edits here affect only this notebook session. Apply a reviewed fix in the application and build a new dataset version. '
             'A successful recheck does not approve model deployment.')
    return {'nbformat': 4, 'nbformat_minor': 5,
            'metadata': {'kernelspec': {'display_name': 'Python 3 (ipykernel)', 'language': 'python', 'name': 'python3'},
                         'language_info': {'name': 'python'}, 'vas_debug_export': 1},
            'cells': [dict(cell, id=f'cell-{index:02d}') for index, cell in enumerate(cells)]}
