"""Live SQL Bot evaluation: read-only ground truth, real HTTP chat, full Opik traces.
Run inside the development API container. Writes only evaluation conversations
and local report files; never modifies surveillance evidence.
"""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "artifacts" / "sql-ground-truth"
OUT.mkdir(parents=True, exist_ok=True)


def write(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def admin_session():
    import getpass
    if ARGS.admin_credentials_file:
        credentials = json.loads(Path(ARGS.admin_credentials_file).read_text(encoding="utf-8"))
    else:
        credentials = {"username": "admin", "password": getpass.getpass("Admin password (account management only): ")}
    client = requests.Session()
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    response = client.post(ARGS.base_url + "/api/auth/login", json=credentials, timeout=30)
    response.raise_for_status()
    return client


def cleanup_replay():
    account = json.loads((OUT / "replay-account.json").read_text(encoding="utf-8"))
    if account.get("username") != ARGS.username or not ARGS.username.startswith("sql-audit-"):
        raise RuntimeError("Cleanup must name the recorded temporary audit account")
    client = admin_session()
    response = client.delete(ARGS.base_url + "/api/users/" + str(account["id"]), timeout=30)
    response.raise_for_status()
    write("replay-cleanup.json", {"id": account["id"], "username": ARGS.username, "deleted": True, "response": response.json()})
    if ARGS.credentials_file:
        path = Path(ARGS.credentials_file)
        if path.exists() and json.loads(path.read_text(encoding="utf-8")).get("username") == ARGS.username:
            path.unlink()
    print("Temporary audit account deleted")


def active_settings():
    client = admin_session()
    result = {}
    for key in ["SQL_AGENT_LEARN_FROM_QUERIES", "SQL_AGENT_USE_KNOWLEDGE_BASE", "SQL_AGENT_OPIK_ENABLED", "LLM_DEV_PROVIDER", "NVIDIA_NIM_MODEL", "NVIDIA_NIM_SQL_MODEL", "SQL_AGENT_DB_USER"]:
        response = client.get(ARGS.base_url + "/api/settings/" + key, timeout=30)
        data = response.json()
        result[key] = {k:data.get(k) for k in ["value", "effective_value", "source", "apply_mode"]}
    write("active-settings.json", result)
    print(json.dumps(result))


def provision_replay():
    import secrets
    client = admin_session()
    scope = json.loads((OUT / "scoped-ground-truth.json").read_text(encoding="utf-8"))["queries"]["shares"]["rows"][:2]
    username = "sql-audit-" + uuid.uuid4().hex[:8]
    password = secrets.token_urlsafe(28) + "aA1!"
    response = client.post(ARGS.base_url + "/api/users", json={"username": username,
        "email": username + "@example.invalid", "password": password, "full_name": "Temporary SQL audit replay",
        "role": "user", "can_use_chatbot": True, "pipeline_ids": [row["pipeline_id"] for row in scope]}, timeout=30)
    response.raise_for_status()
    account = response.json()
    path = Path("/tmp/sql-audit-replay-credentials.json")
    path.write_text(json.dumps({"username": username, "password": password}), encoding="utf-8")
    path.chmod(0o600)
    # A newly admin-provisioned user must take ownership before normal API access.
    if account.get("must_change_password"):
        replay = requests.Session()
        replay.headers.update({"X-Requested-With": "XMLHttpRequest"})
        response = replay.post(ARGS.base_url + "/api/auth/login", json={"username": username, "password": password}, timeout=30)
        response.raise_for_status()
        new_password = secrets.token_urlsafe(28) + "aA1!"
        response = replay.post(ARGS.base_url + "/api/auth/change-password", json={"current_password": password, "new_password": new_password}, timeout=30)
        response.raise_for_status()
        path.write_text(json.dumps({"username": username, "password": new_password}), encoding="utf-8")
    write("replay-account.json", {"id": account["id"], "username": username, "pipeline_ids": account["pipeline_ids"]})
    print(json.dumps({"id": account["id"], "username": username, "pipeline_ids": account["pipeline_ids"]}))


def truth():
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from config import settings
    with psycopg2.connect(host=settings.DB_HOST, port=settings.DB_PORT, dbname=settings.POSTGRES_DB,
                          user=settings.POSTGRES_USER, password=settings.POSTGRES_PASSWORD) as conn:
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, username, role FROM users WHERE username = %s", (ARGS.username,))
            account = cur.fetchone()
            if not account or account["role"] != "user":
                raise RuntimeError("Ground truth requires the existing regular replay account")
            cur.execute("SELECT pipeline_id FROM user_pipeline_access WHERE user_id = %s ORDER BY pipeline_id", (account["id"],))
            scope = [row["pipeline_id"] for row in cur.fetchall()]
            cur.execute("SELECT COUNT(*) AS n FROM pipelines")
            if not scope or len(scope) >= cur.fetchone()["n"]:
                raise RuntimeError("Replay account must have a nonempty restricted camera set")
            queries = {
                "counts": "SELECT COUNT(*) AS detection_count FROM detections WHERE pipeline_id = ANY(%s)",
                "today": "SELECT COUNT(*) AS detection_count FROM detections WHERE pipeline_id = ANY(%s) AND timestamp::date = CURRENT_DATE",
                "cameras": "SELECT p.pipeline_id, p.location_name, COUNT(d.id) AS detection_count FROM pipelines p LEFT JOIN detections d ON d.pipeline_id=p.pipeline_id WHERE p.pipeline_id = ANY(%s) GROUP BY p.pipeline_id,p.location_name ORDER BY detection_count DESC,p.pipeline_id",
                "date_range": "SELECT MIN(timestamp) AS first_detection, MAX(timestamp) AS last_detection FROM detections WHERE pipeline_id = ANY(%s)",
                "people": "SELECT f.name, COUNT(DISTINCT d.id) AS detection_count FROM faces f JOIN detections d ON d.id=f.detection_id WHERE d.pipeline_id = ANY(%s) GROUP BY f.name ORDER BY detection_count DESC",
                "person_cameras": "SELECT f.name, p.pipeline_id, p.location_name, COUNT(DISTINCT d.id) AS detection_count, MIN(d.timestamp) AS earliest_utc, MAX(d.timestamp) AS latest_utc FROM faces f JOIN detections d ON d.id=f.detection_id JOIN pipelines p ON p.pipeline_id=d.pipeline_id WHERE d.pipeline_id = ANY(%s) AND f.name IN ('IRON MAN','JOEY') GROUP BY f.name,p.pipeline_id,p.location_name ORDER BY f.name,detection_count DESC,p.pipeline_id",
                "shares": "SELECT p.pipeline_id, p.location_name, COUNT(DISTINCT d.id) AS detections, COUNT(f.id) AS faces, COUNT(f.id) FILTER (WHERE f.name IS NULL OR f.name = '' OR LOWER(f.name) LIKE 'unknown%%' OR LOWER(f.name) LIKE 'person_%%') AS unidentified FROM pipelines p LEFT JOIN detections d ON d.pipeline_id=p.pipeline_id LEFT JOIN faces f ON f.detection_id=d.id WHERE p.pipeline_id = ANY(%s) GROUP BY p.pipeline_id,p.location_name ORDER BY detections DESC,p.pipeline_id",
            }
            results = {"account": dict(account), "scope": scope, "queries": {}}
            for name, sql in queries.items():
                cur.execute(sql, (scope,))
                rows = [dict(row) for row in cur.fetchall()]
                results["queries"][name] = {"sql": sql, "rows": rows}
            # Compute percentages independently of the generated SQL.
            for row in results["queries"]["shares"]["rows"]:
                row["unidentified_percent"] = 100 * row["unidentified"] / row["faces"] if row["faces"] else None
    write("scoped-ground-truth.json", results)
    print(json.dumps(results, default=str, ensure_ascii=False), flush=True)


def session():
    if not ARGS.credentials_file:
        raise RuntimeError("Provide --credentials-file for the dedicated replay account; admin replay is prohibited")
    credentials = json.loads(Path(ARGS.credentials_file).read_text(encoding="utf-8"))
    if credentials.get("username") != ARGS.username or ARGS.username == "admin":
        raise RuntimeError("Credential username must match the restricted replay account")
    client = requests.Session()
    client.headers.update({"X-Requested-With": "XMLHttpRequest"})
    base = ARGS.base_url
    response = client.post(base + "/api/auth/login", json=credentials, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f"Login failed: HTTP {response.status_code}")
    identity = client.get(base + "/api/auth/me", timeout=30)
    identity.raise_for_status()
    if identity.json().get("role") != "user":
        raise RuntimeError("Replay must use a regular user")
    response = client.post(base + "/api/sql-agent/session/new", timeout=120)
    response.raise_for_status()
    return client, base


def run(label, question, context=None):
    if context is None:
        client, base = session()
        response = client.post(base + "/api/v1/conversations", json={"title": "SQL validation - " + label}, timeout=30)
        response.raise_for_status()
        conversation = response.json()["id"]
    else:
        client, base, conversation = context
    request_id = uuid.uuid4().hex
    started = time.time()
    events = []
    with client.post(base + "/api/sql-agent/query/stream", json={"query": question, "conversation_id": conversation, "request_id": request_id}, stream=True, timeout=(20, 600)) as response:
        response.raise_for_status()
        response.encoding = "utf-8"
        for line in response.iter_lines(decode_unicode=True):
            if line.startswith("data:"):
                event = json.loads(line[5:].strip())
                events.append(event)
                if event.get("type") in ("status", "complete", "error"):
                    print(json.dumps(event, ensure_ascii=False), flush=True)
    result = {"label": label, "question": question, "conversation_id": conversation,
              "request_id": request_id, "started": started, "duration": time.time()-started, "events": events}
    write(label + ".json", result)
    print("SAVED " + label, flush=True)
    return client, base, conversation


def traces(label):
    from sql_agent.config import config
    from sql_agent.tracing import _configure_sdk
    _configure_sdk(config)
    from opik import Opik
    client = Opik()
    traces = client.search_traces(project_name=config.opik_project_name, max_results=15, truncate=False)
    items = []
    for trace in traces:
        item = trace.model_dump()
        spans = client.search_spans(project_name=config.opik_project_name, trace_id=trace.id, max_results=1000, truncate=False)
        item["spans"] = [span.model_dump() for span in spans]
        items.append(item)
    write(label + "-traces.json", items)
    print(json.dumps([{ "id": t["id"], "name": t.get("name"), "start_time": t.get("start_time"), "spans": len(t["spans"]), "error": t.get("error_info")} for t in items], default=str), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["truth", "run", "traces", "provision", "settings", "cleanup"])
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--question")
    parser.add_argument("--followup", help="An intentional dependent/context-isolation turn in the same conversation")
    parser.add_argument("--username", default="claudetest")
    parser.add_argument("--credentials-file")
    parser.add_argument("--admin-credentials-file")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    ARGS = args
    if args.mode == "settings": active_settings()
    elif args.mode == "provision": provision_replay()
    elif args.mode == "cleanup": cleanup_replay()
    elif args.mode == "truth": truth()
    elif args.mode == "traces": traces(args.label)
    else:
        context = run(args.label, args.question)
        if args.followup:
            run(args.label + "-followup", args.followup, context=context)
