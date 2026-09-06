#!/usr/bin/env python3
"""Offline artifact bundle: prepare, verify, import.

Development may download models; production may not. This tool moves the
artifacts across the air gap with a SHA-256 manifest so production can
verify every file before it starts.

    python scripts/offline_bundle.py prepare  --spec bundle.spec.json --out /media/bundle
    python scripts/offline_bundle.py verify   --bundle /media/bundle
    python scripts/offline_bundle.py import   --bundle /media/bundle --dest /

A bundle is a directory:

    manifest.json           {"created", "items": [{"path", "sha256", "size", "kind", "dest"}]}
    <kind>/...              the files (models, wheels, images, frontend, drivers, certs, config)
    sbom.json               optional: `pip freeze`-style inventory of the wheels

The spec lists what to collect; each entry is {"kind", "src", "dest"} where
``src`` is a file or directory on the preparing machine and ``dest`` the
absolute path production expects (for example the Chroma ONNX cache). Docker
images are listed by name and saved with ``docker save``.

No network access is made by any subcommand. ``verify`` exits non-zero on
the first mismatch and prints every mismatch; ``import`` refuses to copy a
bundle that does not verify.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List

KINDS = ("llm", "tokenizer", "embedding", "stt", "tts", "image", "wheel", "frontend",
         "driver", "cert", "config", "other")
CHUNK = 1024 * 1024


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files_under(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def prepare(spec_path: Path, out: Path, *, docker: str = "docker") -> Dict:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    items: List[Dict] = []
    for entry in spec.get("items", []):
        kind = entry.get("kind", "other")
        if kind not in KINDS:
            raise SystemExit(f"unknown kind {kind!r} in spec (one of {', '.join(KINDS)})")
        if kind == "image":
            name = entry["name"]
            target = out / "image" / (name.replace("/", "_").replace(":", "_") + ".tar")
            target.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([docker, "save", "-o", str(target), name], check=True)
            items.append({"path": str(target.relative_to(out)).replace(os.sep, "/"), "sha256": sha256_of(target),
                          "size": target.stat().st_size, "kind": kind, "image": name})
            continue
        src = Path(entry["src"]).expanduser()
        if not src.exists():
            raise SystemExit(f"spec item does not exist: {src}")
        dest = entry.get("dest", "")
        for file in _files_under(src):
            rel = file.relative_to(src) if src.is_dir() else Path(file.name)
            target = out / kind / (src.name if src.is_dir() else "") / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
            items.append({"path": str(target.relative_to(out)).replace(os.sep, "/"), "sha256": sha256_of(target),
                          "size": target.stat().st_size, "kind": kind,
                          "dest": (str(Path(dest) / rel) if src.is_dir() else dest) if dest else ""})
    manifest = {"created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "items": items,
                "count": len(items), "bytes": sum(i["size"] for i in items)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    wheels = [i["path"] for i in items if i["kind"] == "wheel"]
    if wheels:
        (out / "sbom.json").write_text(json.dumps({"wheels": wheels}, indent=2), encoding="utf-8")
    return manifest


def verify(bundle: Path) -> List[str]:
    """Every mismatch, as messages. Empty means the bundle is intact."""
    manifest_path = bundle / "manifest.json"
    if not manifest_path.exists():
        return [f"manifest missing: {manifest_path}"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = []
    for item in manifest.get("items", []):
        path = bundle / item["path"]
        if not path.exists():
            problems.append(f"missing: {item['path']}")
            continue
        if path.stat().st_size != item.get("size", path.stat().st_size):
            problems.append(f"size mismatch: {item['path']}")
            continue
        if sha256_of(path) != item["sha256"]:
            problems.append(f"sha256 mismatch: {item['path']}")
    return problems


def import_bundle(bundle: Path, dest_root: Path, *, docker: str = "docker", load_images: bool = True) -> List[str]:
    problems = verify(bundle)
    if problems:
        raise SystemExit("bundle does not verify:\n  " + "\n  ".join(problems))
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    placed = []
    for item in manifest["items"]:
        src = bundle / item["path"]
        if item["kind"] == "image":
            if load_images:
                subprocess.run([docker, "load", "-i", str(src)], check=True)
                placed.append(f"image {item.get('image')}")
            continue
        if not item.get("dest"):
            continue
        dest = Path(item["dest"])
        if not dest.is_absolute():
            dest = dest_root / dest
        elif dest_root != Path("/"):
            dest = dest_root / dest.relative_to(dest.anchor)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        placed.append(str(dest))
    return placed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare"); p.add_argument("--spec", required=True); p.add_argument("--out", required=True)
    v = sub.add_parser("verify"); v.add_argument("--bundle", required=True)
    i = sub.add_parser("import"); i.add_argument("--bundle", required=True); i.add_argument("--dest", default="/")
    i.add_argument("--no-images", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        manifest = prepare(Path(args.spec), Path(args.out))
        print(f"bundle written: {manifest['count']} file(s), {manifest['bytes']} bytes, manifest.json")
        return 0
    if args.command == "verify":
        problems = verify(Path(args.bundle))
        for problem in problems:
            print(f"[FAIL] {problem}")
        print("[PASS] bundle verified" if not problems else f"{len(problems)} problem(s)")
        return 0 if not problems else 1
    placed = import_bundle(Path(args.bundle), Path(args.dest), load_images=not args.no_images)
    for item in placed:
        print(f"placed {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
