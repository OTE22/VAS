"""The offline bundle: prepared with checksums, verified before use, refused when tampered.

Pure filesystem; no docker (image entries are not used here), no network.

    docker exec face_recognition_api python -m pytest tests/test_offline_bundle.py -v
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import offline_bundle as ob  # noqa: E402


def _make_source(tmp_path):
    model_dir = tmp_path / "src" / "minilm"
    model_dir.mkdir(parents=True)
    (model_dir / "model.onnx").write_bytes(b"onnx" * 1000)
    (model_dir / "tokenizer.json").write_text('{"vocab": 1}', encoding="utf-8")
    wheel = tmp_path / "src" / "pymilvus-2.4.0-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04wheel")
    spec = tmp_path / "bundle.spec.json"
    spec.write_text(json.dumps({"items": [
        {"kind": "embedding", "src": str(model_dir), "dest": "/home/appuser/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx"},
        {"kind": "wheel", "src": str(wheel), "dest": ""},
    ]}), encoding="utf-8")
    return spec


def test_prepare_writes_a_manifest_with_sha256_for_every_file(tmp_path):
    spec = _make_source(tmp_path)
    out = tmp_path / "bundle"
    manifest = ob.prepare(spec, out)
    assert manifest["count"] == 3 and (out / "manifest.json").exists() and (out / "sbom.json").exists()
    by_path = {i["path"]: i for i in manifest["items"]}
    assert "embedding/minilm/model.onnx" in by_path
    assert by_path["embedding/minilm/model.onnx"]["sha256"] == ob.sha256_of(out / "embedding/minilm/model.onnx")
    assert by_path["embedding/minilm/model.onnx"]["dest"].endswith("onnx/model.onnx")
    assert ob.verify(out) == []


def test_verify_reports_every_tampered_or_missing_file(tmp_path):
    out = tmp_path / "bundle"
    ob.prepare(_make_source(tmp_path), out)
    (out / "embedding/minilm/model.onnx").write_bytes(b"tampered" * 500)
    (out / "embedding/minilm/tokenizer.json").unlink()
    problems = ob.verify(out)
    assert any("sha256 mismatch" in p or "size mismatch" in p for p in problems)
    assert any("missing: embedding/minilm/tokenizer.json" in p for p in problems)
    assert ob.main(["verify", "--bundle", str(out)]) == 1


def test_import_refuses_a_bundle_that_does_not_verify_and_places_a_good_one(tmp_path):
    out = tmp_path / "bundle"
    ob.prepare(_make_source(tmp_path), out)
    dest_root = tmp_path / "root"
    placed = ob.import_bundle(out, dest_root, load_images=False)
    assert any(str(p).endswith("model.onnx") for p in placed)
    assert (dest_root / "home/appuser/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/model.onnx").read_bytes() == b"onnx" * 1000
    (out / "embedding/minilm/model.onnx").write_bytes(b"x")
    with pytest.raises(SystemExit):
        ob.import_bundle(out, dest_root, load_images=False)


def test_unknown_kinds_are_refused(tmp_path):
    spec = tmp_path / "s.json"
    spec.write_text(json.dumps({"items": [{"kind": "malware", "src": str(spec)}]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        ob.prepare(spec, tmp_path / "b")
