"""
Synthetic one-year generator — proves the pipeline on data with known answers.

Runs the generator at reduced scale on the isolated stack (it refuses
without the REGRESSION_ISOLATION_ID marker), then checks what it promised:
deterministic output, personas/planted anomalies in the ground truth, a year
of appearances in the source table, the REAL collector and builder producing
a system dataset whose population statistics reflect the generated history,
and a clean removal. Runs only where the marker exists.
"""

import json
import os
import re
import subprocess
import sys

import pytest

SCRIPT = "/app/scripts/generate_synthetic_year.py"
pytestmark = pytest.mark.skipif(not os.environ.get("REGRESSION_ISOLATION_ID"),
                                reason="synthetic generator only runs on the isolated regression stack")


def _run(*args):
    proc = subprocess.run([sys.executable, SCRIPT, *args], cwd="/app", capture_output=True, text=True, timeout=1800)
    return proc.returncode, proc.stdout + proc.stderr


@pytest.fixture(scope="module", autouse=True)
def clean():
    """Never delete a corpus somebody else is using: a dry run reports the
    synthetic rows present; if any exist the module SKIPS instead of
    removing them. Only what this module wrote is removed afterwards."""
    code, out = _run()
    if code != 0:
        pytest.skip(f"generator dry run failed on this stack: {out[-400:]}")
    m = re.search(r"current synthetic rows: (\{.*\})", out)
    existing = json.loads(m.group(1).replace("'", '"')) if m else {}
    if any(existing.get(k) for k in ("identities", "appearances", "datasets", "models")):
        pytest.skip(f"a synthetic corpus is already present on this stack ({existing}); "
                    "refusing to delete it — remove it explicitly with --remove first")
    yield
    _run("--remove", "--yes-i-understand")


def test_generation_is_deterministic_and_plants_anomalies():
    sys.path.insert(0, "/app")
    import importlib.util
    spec = importlib.util.spec_from_file_location("synth", SCRIPT)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    from datetime import datetime
    anchor = datetime(2026, 6, 1)
    a = mod.generate(120, 40, 7, end=anchor)
    b = mod.generate(120, 40, 7, end=anchor)
    assert [r["start_time"] for r in a[1]] == [r["start_time"] for r in b[1]], "same seed + anchor -> same corpus"
    assert a[2] == b[2], "same planted ground truth"
    assert a[1][-1]["start_time"] <= anchor, "the corpus ends at the declared anchor"
    c = mod.generate(120, 40, 7, end=datetime(2026, 6, 1, 6))
    assert [r["start_time"] for r in c[1]] != [r["start_time"] for r in a[1]], "a different anchor is a different corpus"
    assert mod.default_anchor().hour == 0 and mod.default_anchor().minute == 0, "default anchor is a day boundary"
    people, rows, truth = a
    assert len(people) == 40 and truth and all(t["kind"] in mod.ANOMALY_KINDS for t in truth)
    span = (rows[-1]["start_time"] - rows[0]["start_time"]).days
    assert 90 <= span <= 120
    personas = {p["persona"] for p in people}
    assert {"regular_daytime", "night_shift"} <= personas
    assert any(r["planted"] for r in rows) and all(r["end_time"] > r["start_time"] for r in rows)


def test_apply_build_train_and_remove():
    code, out = _run("--apply", "--yes-i-understand", "--days", "120", "--identities", "40",
                     "--build-dataset", "--train", "--export-parquet", "/tmp/synthetic-pytest")
    assert code == 0, out[-3000:]
    assert "[5] dataset: built" in out and "[6] model v" in out and "[7] synthetic sanity" in out, out[-3000:]
    assert "engineering=PASS" in out and "scientific=INSUFFICIENT_EVIDENCE" in out
    assert os.path.exists("/tmp/synthetic-pytest/synthetic_year_observations.parquet")
    truth = json.load(open("/tmp/synthetic-pytest/synthetic_year_ground_truth.json"))
    assert truth["planted"] and "synthetic" in truth["note"]
    # the system's own dataset carries the population statistics of the generated history
    line = next(l for l in out.splitlines() if l.startswith("[5] dataset"))
    span = float(line.split("history_span_days=")[1].split()[0])
    assert 90 <= span <= 121
    code, out = _run("--remove", "--yes-i-understand")
    assert code == 0 and "'identities': 0" in out and "'models': 0" in out and "'datasets': 0" in out
