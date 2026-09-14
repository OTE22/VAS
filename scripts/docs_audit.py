#!/usr/bin/env python3
"""Audit Docs/*.md against the code that ships in this repository.

For every document it reports, as evidence for keeping / updating / deleting it:

  paths      repository paths the document names that do not exist
  api        /api/... endpoints the document names that no route declares
  settings   UPPER_CASE settings the document names that config.py does not declare
  tables     SQL table names the document queries that no model declares
  services   compose services / container names that do not exist
  overlap    how much of this document's prose already appears in ANOTHER document
             (containment of 8-word shingles, reported when >= 30%)

Nothing is imported or executed from the application; routes, settings, models
and compose services are read with the AST / a YAML-free line scan.

    python3 scripts/docs_audit.py            # full report
    python3 scripts/docs_audit.py 07 12      # only documents whose name starts with these
"""
import ast
import glob
import io
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "Docs")


def read(path):
    return io.open(path, encoding="utf-8", errors="replace").read()


# ---------------------------------------------------------------- code facts
def declared_routes():
    out = set()
    for path in glob.glob(f"{ROOT}/backend/routes/*.py") + [f"{ROOT}/sql_agent/api/routes.py"]:
        try:
            tree = ast.parse(read(path))
        except SyntaxError:
            continue
        prefix = ""
        m = re.search(r'APIRouter\([^)]*prefix\s*=\s*["\']([^"\']+)', read(path))
        if m:
            prefix = m.group(1)
        for node in ast.walk(tree):
            for dec in getattr(node, "decorator_list", []):
                s = ast.unparse(dec)
                m2 = re.match(r"(?:router|app)\.(get|post|put|patch|delete|websocket)\((['\"])([^'\"]+)", s)
                if m2:
                    out.add(re.sub(r"\{[^}]+\}", "{}", (prefix + m2.group(3)) or "/"))
    return out


def declared_settings():
    tree = ast.parse(read(f"{ROOT}/config.py"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    out.add(stmt.target.id)
    return out


def declared_tables():
    out = set()
    for path in glob.glob(f"{ROOT}/db_models.py") + glob.glob(f"{ROOT}/backend/**/*.py", recursive=True):
        out |= set(re.findall(r'__tablename__\s*=\s*["\']([a-z_0-9]+)["\']', read(path)))
    return out


def declared_services():
    out = set()
    for path in glob.glob(f"{ROOT}/docker/docker-compose*.yml"):
        body = read(path)
        block = body.split("\nservices:", 1)[-1]
        out |= set(re.findall(r"^  ([a-z][a-z0-9_-]*):", block, re.M))
    return out


ROUTES, SETTINGS, TABLES, SERVICES = declared_routes(), declared_settings(), declared_tables(), declared_services()
TOP = {d for d in os.listdir(ROOT) if os.path.isdir(os.path.join(ROOT, d)) and not d.startswith(".")}

# ------------------------------------------------------------ per-doc checks
PATH_RE = re.compile(r"(?<![\w/.])((?:" + "|".join(sorted(TOP)) + r")/[A-Za-z0-9_./{}-]+)")
API_RE = re.compile(r"(/api/[A-Za-z0-9/_.{}-]*[A-Za-z0-9}])")
SET_RE = re.compile(r"`([A-Z][A-Z0-9_]{3,})`")
TBL_RE = re.compile(r"(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_][a-z_0-9]{3,})", re.I)
SVC_RE = re.compile(r"(?:face_detector_prod[-_]([a-z_]+?)(?:-\d+)?\b|compose (?:exec|logs|restart|up|stop) (?:-[a-zA-Z]+ )*([a-z_]+))")
WORD_RE = re.compile(r"[a-z0-9]+")
FENCE_RE = re.compile(r"```.*?```", re.S)
KNOWN_MISSING_OK = {"scripts/generate_api_index.py"}


def prose_shingles(text, n=8):
    text = FENCE_RE.sub(" ", text)
    text = re.sub(r"\|[^\n]*\|", " ", text)          # tables are often legitimately similar
    words = WORD_RE.findall(text.lower())
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def audit(path):
    body = read(path)
    rel = os.path.relpath(path, ROOT)
    res = {"paths": [], "api": [], "settings": [], "tables": [], "services": []}
    for p in sorted(set(PATH_RE.findall(body))):
        clean = p.rstrip(".,);:`").split("{")[0].rstrip("/")
        if not clean or clean in KNOWN_MISSING_OK or "*" in clean:
            continue
        if not os.path.exists(os.path.join(ROOT, clean)) and not glob.glob(os.path.join(ROOT, clean + "*")):
            res["paths"].append(clean)
    for a in sorted(set(API_RE.findall(body))):
        norm = re.sub(r"\{[^}]*\}", "{}", a.rstrip("/."))
        norm = re.sub(r"/(\d+|<[^>]+>|:[a-z_]+|abc123|[0-9a-f]{8,})(?=/|$)", "/{}", norm)
        if norm in ROUTES or any(r.startswith(norm) for r in ROUTES):
            continue
        if any(norm.startswith(r.rstrip("{}")) for r in ROUTES):
            continue
        res["api"].append(a)
    for s in sorted(set(SET_RE.findall(body))):
        if s not in SETTINGS and re.search(r"[A-Z]_[A-Z]", s) and s not in {
            "NOT_NULL", "ON_ERROR_STOP", "HTTP_200_OK", "TODO_LATER"}:
            res["settings"].append(s)
    for t in sorted({t.lower() for t in TBL_RE.findall(body)}):
        if t not in TABLES and t not in {"select", "where", "table", "values", "dual", "information_schema"}:
            res["tables"].append(t)
    for m in SVC_RE.findall(body):
        svc = (m[0] or m[1]).strip("_-")
        if svc and svc not in SERVICES and svc not in {"logs", "exec", "restart"}:
            res["services"].append(svc)
    res["services"] = sorted(set(res["services"]))
    return rel, res


def main(argv):
    files = sorted(glob.glob(f"{DOCS}/*.md"), key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", os.path.basename(p))])
    if argv:
        files = [f for f in files if any(os.path.basename(f).startswith(a) for a in argv)]
    shingles = {os.path.basename(f): prose_shingles(read(f)) for f in files}
    print(f"# Docs audit — {len(files)} documents, against {len(ROUTES)} routes, "
          f"{len(SETTINGS)} settings, {len(TABLES)} tables, {len(SERVICES)} compose services\n")
    for f in files:
        rel, res = audit(f)
        name = os.path.basename(f)
        mine = shingles[name]
        best = []
        for other, sh in shingles.items():
            if other == name or not mine or len(mine) < 40:
                continue
            contained = len(mine & sh) / len(mine)
            if contained >= 0.30:
                best.append((contained, other))
        best.sort(reverse=True)
        problems = {k: v for k, v in res.items() if v}
        if not problems and not best:
            print(f"{name}: OK")
            continue
        print(f"{name}:")
        for k, v in problems.items():
            print(f"    {k:9}({len(v)}) {', '.join(v[:12])}{' …' if len(v) > 12 else ''}")
        for c, other in best[:3]:
            print(f"    overlap  {c:.0%} of its prose also in {other}")
    print("\n(paths/api/settings/tables/services = named in the document, absent from the code)")


if __name__ == "__main__":
    main(sys.argv[1:])
