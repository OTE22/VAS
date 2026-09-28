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



def _source_function(module, function):
    """Export a named repository function as text without importing application code."""
    import ast
    import textwrap
    source = Path(__file__).with_name(module + '.py').read_text()
    tree = ast.parse(source)
    parts = function.split('.')
    nodes = tree.body
    selected = None
    for part in parts:
        selected = next(node for node in nodes
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name == part)
        nodes = selected.body
    value = textwrap.dedent(ast.get_source_segment(source, selected)) + '\n'
    return {'module': 'backend/ml/' + module + '.py', 'function': function,
            'line': selected.lineno, 'sha256': hashlib.sha256(value.encode()).hexdigest(),
            'source': value}


def pipeline_source_references():
    """Read-only source browser. Database/fit/deployment functions stay text, never exec."""
    references = {
        'collection': ('collector', 'run_collection'),
        'person_context': ('feature_builders', 'load_person_context'),
        'person_features': ('feature_store', 'FeatureStore.compute_person_snapshot'),
        'pair_features': ('relational_feature_service', 'compute_pair_snapshot'),
        'graph_features': ('relational_feature_service', '_graph_metrics'),
        'dataset': ('dataset_builder', 'build_dataset'),
        'training_matrix': ('trainer', '_assemble_matrix'),
        'target_selection': ('tabular', 'prepare_rows'),
        'fit_unsupervised': ('trainer', '_fit_unsupervised'),
        'fit_supervised': ('trainer', '_fit_supervised'),
        'preprocessing': ('scoring', 'preprocess_feature_vector'),
        'scoring': ('scoring', 'score_with_payload'),
    }
    out = {name: _source_function(module, function)
           for name, (module, function) in references.items()}
    # Resolve the literal production builder dispatch map without importing it.
    import ast
    tree = ast.parse(Path(__file__).with_name('feature_builders.py').read_text())
    mapping = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign)
                   and isinstance(node.target, ast.Name) and node.target.id == 'BUILDERS')
    for key, value in zip(mapping.keys, mapping.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and isinstance(value, ast.Name):
            out['feature:' + key.value] = _source_function('feature_builders', value.id)
    return out


def build_pipeline_debug_notebook(evidence):
    """End-to-end evidence notebook; exports never run application actions.

    The route supplies an allowlisted JSON-safe envelope. The original dataset
    exporter remains the authority for file/hash/validation/split rechecks.
    Training, feature extraction and deployment are recorded observations;
    only portable pure preprocessing is rerun against the immutable snapshot.
    """
    cells = []

    def markdown(text):
        cells.append({'cell_type': 'markdown', 'metadata': {}, 'source': text.splitlines(True)})

    def code(text, *, hidden=False):
        cells.append({'cell_type': 'code', 'execution_count': None,
                      'metadata': {'jupyter': {'source_hidden': True}} if hidden else {},
                      'outputs': [], 'source': text.splitlines(True)})

    pipeline = evidence.get('pipeline') or {}
    markdown('# ML workflow · inspect every stage\n'
             'Start here, then run the numbered cells in order. Each stage explains **input → processing → output → next use**. '
             'Saved evidence and offline rechecks are explicitly separated. Empty evidence means it was not recorded or the stage has not run.\n\n'
             '**Read-only:** this notebook never queries production, extracts new live data, trains a model, loads pickled models, '
             'approves deployment or changes service settings. It can inspect exported feature samples and a checksum-verified '
             'dataset through the read-only `/artifacts` mount. No credentials are required. '
             'Use ML Ops to run or retry a managed job; download a fresh notebook to see new evidence.\n\n'
             '**Requirements:** Python 3.11+, `pyarrow`, `numpy`. For step debugging, run a cell once, enable JupyterLab’s debugger, '
             'and set a breakpoint in the pure helper cells before rerunning their caller. '
             'Outputs can contain sensitive feature data; clear outputs before sharing.')
    code('import json\nfrom pathlib import Path\nfrom pprint import pprint\n'
         'from datetime import datetime, timezone\n'
         'evidence = json.loads(' + repr(json.dumps(evidence, ensure_ascii=True)) + ')\n'
         'pipeline = evidence.get("pipeline") or {}\n'
         'training = pipeline.get("training") or {}\n'
         'model = pipeline.get("model") or {}\n'
         'dataset = evidence.get("dataset") or {}\n'
         'quality = dataset.get("quality_report") or {}\n'
         'diagnostics = evidence.get("diagnostics") or {}\n'
         'training_config = training.get("configuration") or model.get("training_config") or {}\n'
         'ARTIFACT_ROOT = Path("/artifacts")\n'
         'def show(value):\n    pprint(value if value not in (None, {}, []) else "Not recorded / not yet run")\n'
         'def compact(value, limit=54):\n'
         '    text = "—" if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list, tuple)) else str(value)\n'
         '    text = " ".join(text.split())\n'
         '    return text if len(text) <= limit else text[:limit-1] + "…"\n'
         'def table(items, columns):\n'
         '    items = list(items)\n'
         '    if not items:\n        print("Not recorded / not yet run")\n        return\n'
         '    headers = [label for key, label in columns]\n'
         '    values = [[compact(item.get(key)) for key, label in columns] for item in items]\n'
         '    widths = [max(len(headers[index]), *(len(row[index]) for row in values)) for index in range(len(headers))]\n'
         '    print(" | ".join(label.ljust(width) for label, width in zip(headers, widths)))\n'
         '    print("-+-".join("-" * width for width in widths))\n'
         '    for row in values:\n        print(" | ".join(value.ljust(width) for value, width in zip(row, widths)))\n'

         'show({"job": evidence.get("job"), "model_type": pipeline.get("model_type"),\n'
         '      "dataset_id": dataset.get("id") or dataset.get("dataset_id"), "model_id": model.get("id")})\n')
    markdown('## 1 · Pipeline map and worker timeline\n'
             '| Stage | Input | Output and next use |\n|---|---|---|\n'
             '| Collect | Appearance events and relationship observations | Versioned feature snapshots |\n'
             '| Extract | Compatible event snapshots, declared time range and cap | Candidate dataset rows with recorded exclusions |\n'
             '| Label and validate | Reviewed labels where required, feature definitions | Checked learning rows and quality report |\n'
             '| Split | Timestamps and declared grouping policy | Training, validation, test, and excluded rows |\n'
             '| Preprocess | Training rows | Selected columns, training medians, fixed-order vectors |\n'
             '| Train | Training vectors, algorithm, seed, parameters | Candidate artifact |\n'
             '| Evaluate | Held-out vectors and declared metrics | Measured results and readiness gates |\n'
             '| Connect | Reviewed candidate and explicit approval | Service binding or shadow observation |\n'
             '| Monitor | Requests handled by the bound version | Usage evidence and errors |\n\n'
             'A duration records a stage boundary; it does not itself prove success. '
             'A failed or interrupted job may end at an unfinished stage.')
    code('job = evidence.get("job") or {}\n'
         'table([job] if job else [], [("job_id", "Run"), ("status", "Run status"), ("error_code", "Error code")])\n'
         'timeline_details = {"Preparation": pipeline.get("preparation") or {}, "Dataset": diagnostics, "Training": training}\n'
         'timeline_rows = {}\n'
         'for group, detail in timeline_details.items():\n'
         '    print("\\n" + group)\n'
         '    events = detail.get("stage_history") or ([{"stage": "preparing_features", "status": detail.get("status"), "duration_seconds": detail.get("duration_seconds")}] if group == "Preparation" and detail else [])\n'
         '    timeline_rows[group] = [{"stage": event.get("stage"), "status": event.get("status") or "boundary recorded", "duration_seconds": event.get("duration_seconds")} for event in events]\n'
         '    table(timeline_rows[group], [("stage", "Stage"), ("status", "Recorded status"), ("duration_seconds", "Duration (s)")])\n'
         'failure_details = training.get("failure") or diagnostics.get("failure") or {}\n'
         'print("\\nFailure / worker log reference:")\n'
         'table([failure_details] if failure_details else [], [("code", "Failure code"), ("log_reference", "Worker log reference"), ("failed_checks", "Failed checks")])\n'
         'print("Full timestamps, failure frames and resource samples: timeline_details, failure_details")\n')
    markdown('## 2 · Collection: where the data originates\n'
             '**Person features:** `IdentityAppearance` events are scanned incrementally by processing-time `(created_at, id)`. '
             'The collector applies a late-arrival grace window and deduplicates versioned snapshots. Each person’s feature context '
             'reads appearances before the snapshot cutoff, within the declared lookback. A source cap is detected; unavailable '
             'windowed features carry a reason instead of a false zero. Event snapshots support training; current-state snapshots support inspection and drift.\n\n'
             '**Pair / graph features:** relationship observations are collected as separate schemas at collection time. '
             'They describe the observed cache, not a reconstructed historical graph. Readiness floors and unavailable reasons remain visible.\n\n'
             '**Next use:** the dataset builder selects a declared feature schema and event population from `ml_feature_snapshots`. '
             'The following counts are saved evidence; raw source events are not replayed by this notebook.')
    code('preparation = pipeline.get("preparation") or {}\n'
         'print("Preparation / collector counts:")\n'
         'table([{"measure": key, "value": preparation[key]} for key in ("status", "rows_scanned", "candidate_rows", "snapshots_written", "snapshots_reused", "snapshots_deduplicated", "identities_affected", "current_state_pending") if key in preparation], [("measure", "Measure"), ("value", "Saved value")])\n'
         'lineage = pipeline.get("feature_lineage") or {}\n'
         'feature_samples = (lineage.get("samples") or [])[:3]\n'
         'print("\\nSample scope:", lineage.get("scope") or "Not recorded")\n'
         'print("Bounded saved feature snapshots (at most 3):")\n'
         'table(feature_samples, [("id", "Snapshot"), ("as_of", "Cutoff"), ("feature_set_version", "Feature schema"), ("source_row_counts", "Source row counts")])\n'
         'print("Full preparation and sample metadata: preparation, feature_samples, lineage")\n')
    markdown('## 3 · Feature meaning: value, source, window and missingness\n'
             'Definitions identify the computation and its parameters. Compare each saved value with its snapshot cutoff and '
             'source-row counts. Missing values and explicit unavailable reasons are distinct from numerical zero. '
             'Definitions supplied at export time are reference material unless explicitly frozen with the dataset.')
    code('definitions = lineage.get("definitions") or []\n'
         'print("Feature definitions:")\n'
         'table(definitions, [("name", "Feature"), ("computation", "Production computation"), ("version", "Version"), ("window", "Window"), ("leakage_class", "Leakage class")])\n'
         'feature_value_rows = {}\n'
         'for index, sample in enumerate(feature_samples):\n'
         '    features = sample.get("features") or {}\n'
         '    missingness = sample.get("missingness") or sample.get("unavailable_features") or {}\n'
         '    names = sorted(set(features) | set(missingness))\n'
         '    feature_value_rows[index] = [{"feature": name, "value": features.get(name), "availability": "available" if name in features and features[name] is not None else "unavailable", "reason": missingness.get(name)} for name in names]\n'
         '    print("\\nSnapshot", sample.get("id", index), "·", sample.get("as_of", "cutoff not recorded"))\n'
         '    table(feature_value_rows[index], [("feature", "Feature"), ("value", "Saved value"), ("availability", "Availability"), ("reason", "Unavailable reason")])\n'
         'limitations = quality.get("feature_set_limitations") or pipeline.get("limitations") or []\n'
         'print("\\nKnown limitations:")\nshow(limitations)\n'
         'print("Full definition parameters and values: definitions, feature_value_rows, feature_samples")\n')
    markdown('## 4 · Extraction, label matching and validation\n'
             '**Input:** event-anchored snapshots matching the dataset family/schema and declared time interval. '
             'The explicit row cap and sampling policy control the extracted population; excluded counts must stay visible.\n\n'
             '**Labels:** supervised datasets use active, manual, reviewed positive/negative outcomes and the latest snapshot '
             'at or before each label event. Training should not see future outcomes. The recorded sparse-feature exclusions '
             'show which columns were removed before validation.\n\n'
             '**Output:** an immutable dataset plus its quality report. Failed checks stop managed training. '
             'The offline section later verifies the saved file and repeats validation using the frozen definition.')
    code('show(diagnostics.get("configuration"))\n'
         'show(dataset.get("extraction") or quality.get("extraction"))\n'
         'show({k: quality.get(k) for k in ("passed", "checks", "failed_checks", "warnings", "excluded_sparse_features", "population")})\n')
    markdown('## 5 · Split: what the model may learn from\n'
             'Training fits parameters. Validation supports comparison/tuning. Test rows measure held-out behavior. '
             '`temporal_group` excludes later rows from identities assigned to an earlier period; `temporal` permits the same '
             'identity in later periods and therefore measures later behavior of known identities. Check recorded boundaries, '
             'dropped counts, and overlaps before interpreting any metric.')
    code('show(dataset.get("split_config"))\n'
         'show((quality.get("debug_contract") or {}).get("split"))\n'
         'show(training_config.get("rows"))\n')
    markdown('## 6 · Training recipe and preprocessing contract\n'
             'Covered features are selected from the training split. Missing values are imputed with training medians; '
             'validation/test rows never supply those medians. Feature order is part of the serving contract. '
             'The algorithm, seed, parameters, target/predictor choices, tuning options and dependency versions below '
             'describe the saved run. A model fitting step is inspected here; it is not rerun or registered from this notebook.')
    code('show(training_config)\n'
         'show({k: model.get(k) for k in ("algorithm", "seed", "hyperparameters", "feature_names", "feature_set_version", "score_type", "is_probability")})\n')
    markdown('## 7 · Evaluation and readiness: what the result proves\n'
             'Inspect validation/test sample sizes before their metrics. Unsupervised anomaly scores and classifier review-rank '
             'scores are not calibrated probabilities of threat. An engineering pass establishes technical checks; scientific '
             'readiness and reviewed real-world evidence are separate requirements. Training success alone does not enable a service.')
    code('evaluation = training.get("evaluation") or model.get("evaluation_report") or {}\n'
         'show(evaluation)\n'
         'show(training.get("quality_gates") or model.get("quality_gates"))\n'
         'show({"engineering_gate": training.get("engineering_gate") or evaluation.get("engineering_gate"),\n'
         '      "scientific_gate": training.get("scientific_gate") or evaluation.get("scientific_gate")})\n')
    markdown('## 8 · Reproducibility and version identity\n'
             'Match the recorded commit/build identity, dependency versions, dataset logical checksum, Parquet checksum and '
             'artifact hash. Older runs can lack evidence. Source displayed in this notebook is explicitly **export-time code**; '
             'it is not automatically the historical code that produced an older run.')
    code('show(training.get("reproducibility") or training_config.get("reproducibility"))\n'
         'show({"model_code_version": model.get("code_version"), "artifact_hash": model.get("artifact_hash"),\n'
         '      "dataset_checksum": dataset.get("checksum"), "parquet_sha256": dataset.get("parquet_sha256"),\n'
         '      "export_code_version": pipeline.get("export_code_version")})\n')
    markdown('## 9 · Connection and actual service usage\n'
             'This is a point-in-time status captured when the notebook was downloaded. Confirm which model version the service '
             'selected and whether real service calls used it. A candidate test is different from a consuming-service request. '
             'Anomaly families remain observations in shadow mode; reviewed ranking models support analyst ordering; numeric '
             'experiments have no live security consumer. Use ML Ops to review/activate/stop a model, then download a fresh export.')
    code('show(pipeline.get("service"))\n')
    markdown('## 10 · Browse the production implementation\n'
             'The source index names each exported function, repository location and SHA-256. '
             'Use `show_source("person_context")` to inspect cutoff filters, `show_source("dataset")` for extraction/labels/splits, '
             '`show_source("feature:appearance_count")` for a feature’s formula, or any other key below. These strings are reference code and never execute database, fitting or deployment operations.')
    references = pipeline_source_references()
    code('sources = json.loads(' + repr(json.dumps(references, ensure_ascii=True)) + ')\n'
         'def show_source(name):\n'
         '    item = sources[name]\n'
         '    print(item["module"] + ":" + str(item["line"]), item["function"], "sha256=" + item["sha256"])\n'
         '    print(item["source"])\n'
         'for name, item in sources.items():\n'
         '    print(name, "→", item["module"] + ":" + str(item["line"]), item["function"])\n'
         '# Example: show_source("person_context")\n', hidden=True)
    markdown('## 11 · Offline snapshot rechecks\n'
             'The following cells use the existing dataset debugger: source-hash check, artifact containment, file and logical '
             'checksums, bounded loading, feature coverage, frozen validation and exact declared split. '
             'The first failing assertion identifies the boundary; do not skip a failed integrity check. '
             'All earlier recorded evidence remains readable when no replayable artifact exists.')
    snapshot_notebook = build_debug_notebook(evidence)
    # Keep the snapshot data-loading/validation/split cells; its setup/evidence
    # cells are redundant and its final next-action is replaced below.
    start = next(index for index, cell in enumerate(snapshot_notebook['cells'])
                 if cell['cell_type'] == 'markdown' and ''.join(cell['source']).startswith(
                     '## 3 · Shared production' if evidence.get('artifact_available') else '## Replay unavailable'))
    import re
    for snapshot_cell in snapshot_notebook['cells'][start:-1]:
        snapshot_cell = dict(snapshot_cell)
        if snapshot_cell['cell_type'] == 'markdown':
            value = ''.join(snapshot_cell['source'])
            value = re.sub(r'^## ([3-7]) ·', lambda match: '### 11.' + str(int(match[1]) - 2) + ' ·', value)
            value = value.replace('## Replay unavailable', '### Snapshot recheck unavailable')
            snapshot_cell['source'] = value.splitlines(True)
        cells.append(snapshot_cell)
    if evidence.get('artifact_available'):
        markdown('## 12 · Recheck the matrix used by training and serving\n'
                 'These are the exact portable production preprocessing functions at export time. '
                 'They are pure calculations on the verified snapshot; no model is deserialized or fitted. '
                 'The recheck compares selected feature names and training medians to the recorded model contract, '
                 'then shows vectors and imputed columns. It stops if the contract changed. '
                 'Without a saved training contract this step is unavailable; dataset checks above still apply.')
        # These source fragments are selected by fixed names from our own code.
        # Never execute a function supplied by data or generated by the user.
        import ast
        trainer_tree = ast.parse(Path(__file__).with_name('trainer.py').read_text())
        coverage_floor = next(ast.literal_eval(node.value) for node in trainer_tree.body
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                and target.id == 'FEATURE_COVERAGE_FLOOR' for target in node.targets))
        pure = ('import math\nfrom typing import Any, Dict, List\n'
                'class RegistryError(Exception):\n'
                '    def __init__(self, code, message):\n        self.code = code\n        super().__init__(message)\n'
                'FEATURE_COVERAGE_FLOOR = ' + repr(coverage_floor) + '\n' +
                references['preprocessing']['source'] + '\n' +
                references['target_selection']['source'] + '\n' +
                references['training_matrix']['source'])
        code(pure)
        code('recorded_names = training_config.get("feature_names") or model.get("feature_names")\n'
             'recorded_medians = training_config.get("imputation_medians")\n'
             'matrix_recheck = {"status": "unavailable", "reason": "Training preprocessing contract not recorded"}\n'
             'if recorded_names and recorded_medians is not None:\n'
             '    import numpy as np\n'
             '    floor = training_config.get("feature_coverage_floor")\n'
             '    assert floor in (None, FEATURE_COVERAGE_FLOOR), "Coverage policy changed since training"\n'
             '    recipe = training_config.get("pipeline") or {}\n'
             '    kind = pipeline.get("model_type") or model.get("model_type")\n'
             '    prepared_rows = rows\n'
             '    if kind in ("threat_ranking_model", "tabular_regression_model"):\n'
             '        prepared_rows = prepare_rows(rows, recipe, regression=kind == "tabular_regression_model")\n'
             '    elif recipe.get("features"):\n'
             '        selected = set(recipe["features"])\n'
             '        known = set().union(*(row["features"] for row in rows)) if rows else set()\n'
             '        assert selected <= known, "Selected predictors are absent from this dataset"\n'
             '        prepared_rows = [{**row, "features": {k: v for k, v in row["features"].items() if k in selected}} for row in rows]\n'
             '    training_rows = [row for row in prepared_rows if row.get("split") == "train"]\n'
             '    assert training_rows, "No saved training rows"\n'
             '    names, medians, matrix_of = _assemble_matrix(training_rows)\n'
             '    assert names == recorded_names, "Selected feature order differs from the trained model"\n'
             '    assert set(medians) == set(recorded_medians), "Imputation feature set differs from the trained model"\n'
             '    assert all(math.isclose(medians[name], recorded_medians[name], rel_tol=1e-12, abs_tol=1e-12) for name in names), "Training medians differ from the recorded model"\n'
             '    matrices = {name: matrix_of([row for row in prepared_rows if row.get("split") == name]) for name in ("train", "val", "test")}\n'
             '    assert all(np.isfinite(matrix).all() for matrix in matrices.values()), "Non-finite matrix values"\n'
             '    matrix_recheck = {"status": "matched", "scope": "export-time pure preprocessing against recorded contract",\n'
             '        "feature_names": names, "imputation_medians": medians, "shapes": {name: matrix.shape for name, matrix in matrices.items()}}\n'
             '    print("Example fixed-order vectors and imputed columns:")\n'
             '    for row in prepared_rows[:3]:\n'
             '        vector, imputed = preprocess_feature_vector({"feature_names": names, "imputation_medians": medians}, row["features"])\n'
             '        show({"snapshot_id": row.get("snapshot_id"), "split": row.get("split"), "vector": vector, "imputed_features": imputed})\n'
             'show(matrix_recheck)\n')
    else:
        markdown('## 12 · Training matrix recheck unavailable\n'
                 'No verified immutable snapshot is available for this export. The saved training contract, if any, '
                 'is visible in stage 6. Complete the managed dataset build or restore the registered snapshot, then '
                 'download a fresh notebook. No synthetic training rows or imputation values are supplied.')
    markdown('## Next action\n'
             'Find the first failed check or missing stage. Inspect its saved input, output counts and source reference. '
             'Apply any change in the application, run the managed step in ML Ops, and export a new notebook. '
             'This export is an inspection record, not an approval or a claim that the entire historical pipeline was reproduced.')
    return {'nbformat': 4, 'nbformat_minor': 5,
            'metadata': {'kernelspec': {'display_name': 'Python 3 (ipykernel)', 'language': 'python', 'name': 'python3'},
                         'language_info': {'name': 'python'}, 'vas_debug_export': 2,
                         'vas_debug_scope': 'workflow_evidence_and_offline_rechecks'},
            'cells': [dict(cell, id=f'workflow-{index:02d}') for index, cell in enumerate(cells)]}
