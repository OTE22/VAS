"""
Wipe Pipelines - Clean Slate
============================
Removes ALL pipelines and their display/detection data so the system starts fresh
(new pipelines auto-register from webhooks with proper payload-provided names).

PRESERVES the recognition gallery:
  - identities of type KNOWN (+ their identity_embeddings)
  - users, settings, saved searches, watchlist definitions

DELETES (in FK-safe order):
  - faces, detections
  - identity_appearances, merge_suggestions, identity_relationships,
    identity_merges, identity_audit_log
  - live_alert_triggers, live_search_alerts, watchlist_alerts
  - UNKNOWN identities + their embeddings
  - user_pipeline_access, pipelines

Run inside the API container:
    docker exec face_recognition_api python /app/scripts/maintenance/wipe_pipelines.py
Then restart the API (clears in-memory trackers) and hard-refresh the dashboard.
"""

import os
import sys

import psycopg2

# The repository root must be importable before `config` resolves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from config import settings  # noqa: E402

# Connection details come from the SAME settings object the application uses.
#
# These four were previously read straight from os.getenv with
# POSTGRES_PASSWORD defaulting to the literal "admin". That skipped
# config.py's secret-file resolution entirely, so on any deployment supplying
# the password through /run/secrets this script authenticated with a different
# credential than the service it was maintaining.
DB_HOST = settings.DB_HOST
DB_NAME = settings.POSTGRES_DB
DB_USER = settings.POSTGRES_USER
DB_PASSWORD = settings.POSTGRES_PASSWORD


def table_exists(cur, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s",
        (name,),
    )
    return cur.fetchone() is not None


def count(cur, name: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {name}")
    return cur.fetchone()[0]


def main():
    conn = psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    conn.autocommit = False
    cur = conn.cursor()

    steps = []  # (label, sql)

    # 1. Detection display data
    steps.append(("faces", "DELETE FROM faces"))

    # 2. Detach kept (KNOWN) embeddings from detections before detections vanish
    steps.append((
        "identity_embeddings.detection_id -> NULL (kept KNOWN embeddings)",
        """
        UPDATE identity_embeddings SET detection_id = NULL
        WHERE detection_id IS NOT NULL
          AND identity_id IN (SELECT id FROM identities WHERE type = 'KNOWN')
        """,
    ))

    # 3. Embeddings of UNKNOWN identities (test junk)
    steps.append((
        "identity_embeddings (UNKNOWN identities)",
        "DELETE FROM identity_embeddings WHERE identity_id IN (SELECT id FROM identities WHERE type <> 'KNOWN')",
    ))

    # 4. Identity-linked display/analysis data
    for t in [
        "identity_appearances",
        "merge_suggestions",
        "identity_relationships",
        "identity_merges",
        "identity_audit_log",
        "live_alert_triggers",
        "live_search_alerts",
        "watchlist_alerts",
        "similarity_training_data",
    ]:
        steps.append((t, f"DELETE FROM {t}"))

    # 5. UNKNOWN identities themselves
    steps.append(("identities (UNKNOWN)", "DELETE FROM identities WHERE type <> 'KNOWN'"))

    # 6. Detections
    steps.append(("detections", "DELETE FROM detections"))

    # 7. Pipeline access + pipelines
    steps.append(("user_pipeline_access", "DELETE FROM user_pipeline_access"))
    steps.append(("pipelines", "DELETE FROM pipelines"))

    print("=" * 60)
    print("WIPE PIPELINES - clean slate (keeps KNOWN identities)")
    print("=" * 60)

    kept_before = None
    cur.execute("SELECT COUNT(*) FROM identities WHERE type='KNOWN'")
    kept_before = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM identity_embeddings e JOIN identities i ON i.id=e.identity_id WHERE i.type='KNOWN'"
    )
    kept_emb_before = cur.fetchone()[0]

    for label, sql in steps:
        table = label.split(" ")[0]
        if "." not in table and not table_exists(cur, table.split("(")[0].strip()):
            print(f"  ⏭️  {label}: table missing, skipped")
            continue
        cur.execute(sql)
        print(f"  🗑️  {label}: {cur.rowcount} rows affected")

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM identities WHERE type='KNOWN'")
    kept_after = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM identity_embeddings e JOIN identities i ON i.id=e.identity_id WHERE i.type='KNOWN'"
    )
    kept_emb_after = cur.fetchone()[0]

    print("-" * 60)
    print(f"KNOWN identities preserved: {kept_before} -> {kept_after}")
    print(f"KNOWN embeddings preserved: {kept_emb_before} -> {kept_emb_after}")
    print(f"pipelines remaining: {count(cur, 'pipelines')}")
    print(f"detections remaining: {count(cur, 'detections')}")
    print("Done. Restart the API and hard-refresh the dashboard.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
