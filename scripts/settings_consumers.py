#!/usr/bin/env python3
"""Regenerate Docs/98_SETTINGS_CONSUMERS.md: every setting on the admin settings
page, its value in use, when a change applies, and who consumes it.

    python scripts/settings_consumers.py --token <admin JWT> [--base http://localhost]

The page data comes from GET /api/settings (administrator token; read-only).
Consumers come from a scan of the repository:
  * Python attribute reads   settings.KEY / cfg.KEY / config_settings.KEY ...
  * by-name reads            getattr(settings, "KEY"), lookup tables, _seconds("KEY", ...)
  * the sql_agent.config mirror (settings copied into snake_case fields)
  * compose files, env templates, Dockerfiles, shell scripts, monitoring configs
Local uncommitted env files (.env, docker/.env) are reported by presence only.
"""
from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASES = ("settings", "cfg", "config", "config_settings", "app_settings", "_settings", "_app_settings", "s")
SKIP_BY_NAME = ("config.py", "routes/settings.py", "runtime_settings.py", "config_guard.py")


def fetch(base: str, token: str) -> dict:
    req = urllib.request.Request(base.rstrip("/") + "/api/settings")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    return json.load(urllib.request.urlopen(req, timeout=60))


def python_consumers(keys: set) -> dict:
    readers = {k: set() for k in keys}
    files = []
    for r in ("backend", "sql_agent", "models", "utils", "db_connection.py", "db_models.py", "gunicorn.conf.py"):
        p = ROOT / r
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files += [f for f in p.rglob("*.py") if "__pycache__" not in f.parts]
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        rel = f.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in keys and isinstance(node.value, ast.Name) and node.value.id in BASES:
                readers[node.attr].add(rel)
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in keys
                  and not any(rel.endswith(s) for s in SKIP_BY_NAME)):
                readers[node.value].add(rel + " (by name)")
    mirror = (ROOT / "sql_agent/config.py").read_text(encoding="utf-8", errors="replace")
    for k in keys:
        if re.search(r"settings\." + re.escape(k) + r"\b|\"" + re.escape(k) + r"\"", mirror):
            readers[k].add("sql_agent/config.py (mirrored into sql_agent.config.Config)")
    return readers


def other_consumers(keys: set) -> dict:
    other = {k: set() for k in keys}
    targets = [ROOT / p for p in ("docker/docker-compose.cpu.yml", "docker/docker-compose.gpu.yml", "docker/docker-compose.prod.yml",
                                   "docker/docker-compose.prod.gpu.yml", "docker/docker-compose.regression.yml", ".env.example",
                                   "docker/env.production.example", "docker/Dockerfile.cpu", "docker/Dockerfile.gpu",
                                   "docker-entrypoint.sh", "deploy.sh", "gunicorn.conf.py")]
    targets += list((ROOT / "scripts").rglob("*.sh")) + list((ROOT / "monitoring").rglob("*.yml"))
    for f in targets:
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for k in keys:
            if re.search(r"(?<![A-Z_])" + re.escape(k) + r"(?![A-Z_])", text):
                other[k].add(f.relative_to(ROOT).as_posix())
    for f in (ROOT / ".env", ROOT / "docker/.env"):
        if f.exists():
            text = f.read_text(encoding="utf-8", errors="replace")
            for k in keys:
                if re.search(r"^\s*" + re.escape(k) + r"=", text, flags=re.M):
                    other[k].add(f.relative_to(ROOT).as_posix() + " (local, uncommitted)")
    return other


def render(rows, readers, other, generated_on: str) -> str:
    def short(paths, limit=4):
        paths = sorted(paths)
        return "<br>".join(paths[:limit]) + (f"<br>… +{len(paths) - limit} more" if len(paths) > limit else "")
    lines = ["# 98 — Settings page: who consumes each setting", "",
             f"Generated {generated_on} from the live settings page ({len(rows)} settings) and a scan of the",
             "repository: Python attribute reads (`settings.KEY`), by-name reads (`getattr(settings, \"KEY\")`, lookup tables,",
             "`_seconds(\"KEY\", …)`), the `sql_agent.config` mirror, Docker compose files, env templates, Dockerfiles and shell scripts.",
             "**In use** is the value the running API reports. **Applied** is when a change takes effect",
             "(`immediate` / `next_request` / `next_job_run` live; `api_restart`, `worker_restart`, `container_recreate` need a",
             "restart; `index_rebuild` needs the vector index rebuilt).", "",
             "Regenerate: `python scripts/settings_consumers.py --token <admin JWT>`.", ""]
    current = None
    for s in rows:
        if s["category"] != current:
            current = s["category"]
            lines += ["", f"## {current}", "", "| Setting | In use | Applied | Python consumers | Docker / env / shell |", "|---|---|---|---|---|"]
        val = "(hidden)" if s["is_sensitive"] else str(s["effective_value"])
        lines.append(f"| `{s['key']}` | {val} | {s['apply_mode']} | {short(readers[s['key']]) or '—'} | {short(other[s['key']]) or '—'} |")
    unconsumed = [s["key"] for s in rows if not readers[s["key"]] and not other[s["key"]]]
    lines += ["", "## Settings with no consumer found", "",
              "None: every setting on the page is read by application code, a compose file or a script." if not unconsumed
              else "\n".join(f"- `{k}`" for k in unconsumed), ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--token", required=True, help="administrator JWT")
    parser.add_argument("--base", default="http://localhost")
    parser.add_argument("--out", default=str(ROOT / "Docs/98_SETTINGS_CONSUMERS.md"))
    args = parser.parse_args(argv)
    data = fetch(args.base, args.token)
    rows = sorted([s for cat in data["settings_by_category"].values() for s in cat], key=lambda s: (s["category"], s["key"]))
    keys = {s["key"] for s in rows}
    import datetime
    text = render(rows, python_consumers(keys), other_consumers(keys), datetime.date.today().isoformat())
    pathlib.Path(args.out).write_text(text, encoding="utf-8")
    print(f"wrote {args.out}: {len(rows)} settings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
