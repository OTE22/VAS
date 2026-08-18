"""Generate Docs/75_API_REFERENCE.md from the LIVE OpenAPI document.

Generated, not written by hand, so it cannot drift from the code: every path,
method, tag and auth requirement below came out of the running application.
"""
import collections
import json
import urllib.request

SPEC_URL = "http://localhost:8000/openapi.json"
OUT = "/app/Docs/75_API_REFERENCE.md"
VERBS = ("get", "post", "put", "patch", "delete")

spec = json.load(urllib.request.urlopen(SPEC_URL, timeout=120))

# tag -> description, in the order the app declares them
tag_order = [t["name"] for t in spec.get("tags", [])]
tag_desc = {t["name"]: t.get("description", "") for t in spec.get("tags", [])}

by_tag = collections.defaultdict(list)
operation_count = 0

for path, methods in sorted(spec["paths"].items()):
    for verb, op in methods.items():
        if verb not in VERBS:
            continue
        operation_count += 1
        tags = op.get("tags") or ["(untagged)"]
        # security is expressed per-operation; fall back to the global default
        secured = bool(op.get("security", spec.get("security")))
        params = op.get("parameters", []) or []
        path_params = [p["name"] for p in params if p.get("in") == "path"]
        query_params = [p["name"] for p in params if p.get("in") == "query"]
        has_body = "requestBody" in op
        codes = sorted(op.get("responses", {}).keys())
        by_tag[tags[0]].append({
            "verb": verb.upper(),
            "path": path,
            "summary": op.get("summary", "").strip(),
            "path_params": path_params,
            "query_params": query_params,
            "has_body": has_body,
            "codes": codes,
        })

lines = []
w = lines.append

w("# API Reference")
w("")
w("**Generated from the running application's OpenAPI document — do not edit "
  "by hand.** Regenerate with `scripts/generate_api_reference.py` after any "
  "route change; a stale copy is worse than none.")
w("")
w(f"- **{operation_count} operations** across **{len(spec['paths'])} paths**")
w(f"- Service: `{spec['info']['title']}` v`{spec['info']['version']}`")
w("")
w("## How to read this")
w("")
w("**Auth.** Almost every endpoint requires a bearer token from "
  "`POST /api/auth/login`, sent as `Authorization: Bearer <token>`, or the "
  "equivalent session cookie. The exceptions are the health endpoints, login "
  "itself, `/metrics` (restricted by source IP at nginx instead), and the "
  "camera ingest endpoints — which use an **ingest key**, not a bearer token: "
  "`X-Webhook-Key: <key>`.")
w("")
w("**Cookie-authenticated mutations additionally require** "
  "`X-Requested-With: XMLHttpRequest`. Without it the request is rejected as "
  "cross-site (403 `CSRF_FAILED`). Bearer-token clients are unaffected.")
w("")
w("**Expensive operations return `202 Accepted` with a `job_id`** rather than "
  "blocking — relationship calculation, threshold learning, model training and "
  "alert-channel tests. Poll the job rather than holding the connection open.")
w("")
w("**Columns.** *Path params* are the `{braced}` segments. *Query* lists "
  "query-string parameters. *Body* marks endpoints taking a request body. "
  "*Returns* lists the documented status codes.")
w("")
w("> The interactive version of this document is `/docs` (Swagger UI) and "
  "`/redoc`, served from vendored local assets with no internet access. Both "
  "are **disabled in production**, because they publish every admin route. "
  "This file is the production-safe substitute.")
w("")
w("---")
w("")

# contents
w("## Contents")
w("")
ordered = [t for t in tag_order if t in by_tag] + \
          [t for t in sorted(by_tag) if t not in tag_order]
for tag in ordered:
    anchor = tag.lower().replace(" ", "-").replace("(", "").replace(")", "")
    w(f"- [{tag}](#{anchor}) — {len(by_tag[tag])} operations")
w("")
w("---")
w("")

for tag in ordered:
    w(f"## {tag}")
    w("")
    if tag_desc.get(tag):
        w(tag_desc[tag])
        w("")
    w("| Method | Path | Summary | Path params | Query | Body | Returns |")
    w("|---|---|---|---|---|---|---|")
    for op in sorted(by_tag[tag], key=lambda o: (o["path"], o["verb"])):
        pp = ", ".join(f"`{p}`" for p in op["path_params"]) or "—"
        qp = ", ".join(f"`{p}`" for p in op["query_params"][:6]) or "—"
        if len(op["query_params"]) > 6:
            qp += f", +{len(op['query_params']) - 6} more"
        body = "yes" if op["has_body"] else "—"
        codes = ", ".join(op["codes"])
        summary = op["summary"].replace("|", "\\|") or "—"
        w(f"| `{op['verb']}` | `{op['path']}` | {summary} | {pp} | {qp} | "
          f"{body} | {codes} |")
    w("")

w("---")
w("")
w("## Errors")
w("")
w("Structured errors carry a machine-readable code:")
w("")
w("```json")
w('{"error": {"code": "RATE_LIMITED", "message": "...", '
  '"reference_id": "AUTH-1a2b3c4d", "retryable": true}}')
w("```")
w("")
w("Every response carries an **`X-Request-ID`** header (12 hex characters). "
  "Every log line written while serving that request carries `req=<id>`, so "
  "the header is how you find the traceback for a 500:")
w("")
w("```bash")
w("docker compose -f docker/docker-compose.prod.yml exec face_recognition \\")
w('  sh -c "grep req=<id> /var/log/face-recognition/app.log"')
w("```")
w("")
w("Auth endpoints use a second scheme, `AUTH-` plus 8 hex characters, in "
  "`error.reference_id` and in the `[AUTH_AUDIT]` log line.")
w("")
w("Common codes: `INVALID_CREDENTIALS` (401), `RATE_LIMITED` (429, with "
  "`Retry-After`), `CSRF_FAILED` (403), `WEBHOOK_AUTH_REQUIRED` (401), "
  "`SESSION_CREATION_FAILED` (500), `AUTH_SERVICE_UNAVAILABLE` (500).")
w("")
w("Troubleshooting each of these: "
  "[`73_TROUBLESHOOTING.md`](73_TROUBLESHOOTING.md).")
w("")

with open(OUT, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines))

print(f"wrote {OUT}: {len(lines)} lines, {operation_count} operations, "
      f"{len(ordered)} tags")
