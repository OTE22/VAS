"""Reproducible offline contract evaluation; never invokes a live model.

Run in the disposable test container, not against a production application.
Outputs contain test names/outcomes only, not captured prompts or tracebacks.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/tmp/sql-agent-evaluation.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    dataset = json.loads((root / "tests/fixtures/sql_agent_acceptance_v1.json").read_text(encoding="utf-8"))
    selectors = sorted({case["test"] for case in dataset["cases"]})
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="sql-agent-eval-") as work:
        junit = Path(work) / "results.xml"
        completed = subprocess.run([sys.executable, "-m", "pytest", *selectors, "-q",
            "-p", "no:cacheprovider", f"--junitxml={junit}"], cwd=root)
        records = list(ET.parse(junit).iter("testcase")) if junit.exists() else []
    results = []
    for case in dataset["cases"]:
        function = case["test"].split("::")[-1]
        matches = [r for r in records if r.attrib.get("name", "").split("[")[0] == function]
        passed = bool(matches) and all(not any(r.find(tag) is not None
            for tag in ("failure", "error", "skipped")) for r in matches)
        results.append({"id": case["id"], "name": case["name"], "passed": passed,
                        "executions": len(matches), "criterion": case["criterion"]})
    accepted = completed.returncode == 0 and all(r["passed"] for r in results)
    report = {"dataset_version": dataset["version"], "kind": "offline_contracts",
        "threshold": "100% cases pass; no skipped or missing cases",
        "accepted": accepted, "duration_seconds": round(time.monotonic() - started, 2),
        "cases": results, "model_quality_verified": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
