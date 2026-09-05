"""No live database, training job or user file is used by these tests.

Run with: python -m unittest tests.test_ml_workflow_isolated -v
"""
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.ml.dataset_explorer import summarize_records, explore_dataset
from backend.ml.training_telemetry import TrainingTelemetry


def record(entity="fixture", **changes):
    return dict(entity_id=entity, as_of="2026-01-01T00:00:00", split="train",
                label="positive", features_json='{"a": 1, "missing": null}', **changes)


class ExplorerStatisticsTests(unittest.TestCase):
    def test_filters_do_not_change_quality_statistics_and_pagination(self):
        rows = [record(), record(), {**record("second"), "label": "negative", "split": "test"}]
        result = summarize_records(rows, total_rows=3, schema=[], label="positive", page=2, page_size=1)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["class_distribution"], {"positive": 2, "negative": 1})
        self.assertEqual(result["missing_values"]["missing"], 3)
        self.assertEqual(result["filtered_rows"], 2)
        self.assertEqual(len(result["items"]), 1)
        self.assertFalse(result["truncated"])

    def test_invalid_features_remain_json_safe_and_partial_counts_are_explicit(self):
        rows = [{**record(), "features_json": '{"a": NaN, "b": true}'},
                {**record("other"), "features_json": '[]'}]
        result = summarize_records(rows, total_rows=10, schema=[], query="other")
        self.assertEqual(result["invalid_rows"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["filtered_rows"], 1)
        json.dumps(result, allow_nan=False)

    def test_empty_and_no_matches(self):
        empty = summarize_records([], total_rows=0, schema=[])
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["invalid_rows"], 0)
        self.assertFalse(empty["truncated"])
        self.assertEqual(summarize_records([record()], total_rows=1, schema=[], query="absent")["items"], [])

    def test_telemetry_records_completed_stage_duration(self):
        with patch('backend.ml.training_telemetry.time.monotonic', side_effect=[0, 1, 5]):
            telemetry = TrainingTelemetry("fixture-dataset")
            first = telemetry.stage("training")
            second = telemetry.stage("evaluating")
        self.assertNotIn("duration_seconds", first["stage_history"][0])
        self.assertEqual(second["stage_history"][0]["duration_seconds"], 4)
        self.assertEqual(second["dataset_id"], "fixture-dataset")


class ExplorerArtifactTests(unittest.TestCase):
    def setUp(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        self.temp = tempfile.TemporaryDirectory(prefix="ml-workflow-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "fixture.parquet"
        pq.write_table(pa.Table.from_pylist([record(), record("second")]), self.path)
        self.row = SimpleNamespace(id=uuid.uuid4(), storage_path=str(self.path), version=1,
            definition_name="fixture", checksum="fixture-checksum", quality_report={"passed": True}, missing_value_report={})

    def test_artifact_inspection_is_read_only_and_path_free(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        result = explore_dataset(self.row, self.root)
        self.assertEqual(result["total_rows"], 2)
        self.assertEqual(result["source"], "fixture")
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_artifact_path_escape_is_refused(self):
        with self.assertRaises(ValueError):
            explore_dataset(self.row, self.root / "different-root")

    def test_api_permissions_bounds_download_and_recovery(self):
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        from backend.routes.ml_ops import router, ML_MANAGE, get_db
        from config import settings

        class FakeDB:
            async def execute(inner, _query):
                return SimpleNamespace(scalar_one_or_none=lambda: self.row)

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: FakeDB()
        app.dependency_overrides[ML_MANAGE] = lambda: SimpleNamespace(username="fixture-admin")
        with patch.object(settings, "ML_ARTIFACT_DIR", str(self.root)), TestClient(app) as client:
            url = f"/api/ml/datasets/{self.row.id}"
            self.assertEqual(client.get(url + "/explorer?page_size=101").status_code, 422)
            self.assertEqual(client.get(url + "/explorer?split=invalid").status_code, 422)
            self.assertEqual(client.get('/api/ml/datasets/not-a-uuid/explorer').status_code, 422)
            preview = client.get(url + "/explorer?q=second")
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["filtered_rows"], 1)
            self.assertEqual(preview.headers["cache-control"], "no-store")
            download = client.get(url + "/validation-report")
            self.assertEqual(download.json()["validation_report"], {"passed": True})
            self.assertIn("attachment", download.headers["content-disposition"])
            self.row.storage_path = str(self.root / "missing.parquet")
            self.assertEqual(client.get(url + "/explorer").status_code, 409)
            self.assertEqual(client.get(url + "/validation-report").status_code, 200)
            def denied():
                raise HTTPException(403, "Permission denied")
            app.dependency_overrides[ML_MANAGE] = denied
            self.assertEqual(client.get(url + "/explorer").status_code, 403)
            self.assertEqual(client.get(url + "/validation-report").status_code, 403)


class TrainingDiagnosticsTests(unittest.TestCase):
    def test_real_fit_evaluation_and_artifact_round_trip_use_only_temporary_data(self):
        import numpy as np
        from backend.ml.trainer import _assemble_matrix, _fit_supervised, _binary_ranking_metrics
        from backend.ml.evaluation_visuals import supervised_visuals
        from backend.ml.registry_service import save_artifact, validate_artifact, current_dependency_versions, score_with_payload
        from config import settings

        rows = [{"features": {"x": float(i), "y": float(i % 3)}} for i in range(40)]
        names, medians, matrix = _assemble_matrix(rows)
        labels = np.asarray([int(i >= 20) for i in range(40)])
        fitted, _ = _fit_supervised("gradient_boosting", matrix(rows), labels, 42, {"n_estimators": 10})
        diagnostics = supervised_visuals(fitted, names, matrix(rows), labels)
        self.assertEqual(sum(map(sum, diagnostics["confusion_matrix"])), 40)
        self.assertEqual(len(diagnostics["training_curves"]), 10)
        self.assertEqual(set(diagnostics["feature_importance"]), set(names))
        measured = _binary_ranking_metrics(labels, fitted.predict_proba(matrix(rows))[:, 1])
        self.assertGreater(measured["roc_auc"], 0.9)
        payload = {"algorithm": "gradient_boosting", "model": fitted, "feature_names": names,
            "feature_set_version": "fixture", "imputation_medians": medians,
            "normalization": {"min": 0.0, "max": 1.0}, "band_cutpoints": {},
            "dependency_versions": current_dependency_versions(), "metadata": {"dataset_id": "fixture"},
            "saved_at": "2026-01-01T00:00:00Z"}
        with tempfile.TemporaryDirectory(prefix="ml-fit-fixture-") as directory, patch.object(settings, "ML_ARTIFACT_DIR", directory):
            location = str(Path(directory) / "fixture.pkl")
            digest = save_artifact(payload, location)
            loaded = validate_artifact(location, expected_hash=digest, expected_feature_names=names,
                                       expected_dependencies=payload["dependency_versions"])
            np.testing.assert_allclose(score_with_payload(payload, matrix(rows)), score_with_payload(loaded, matrix(rows)))

    def test_unrecorded_diagnostics_are_not_invented(self):
        from backend.ml.evaluation_visuals import supervised_visuals
        self.assertEqual(supervised_visuals(object(), [], [], []), {})


if __name__ == "__main__":
    unittest.main()
