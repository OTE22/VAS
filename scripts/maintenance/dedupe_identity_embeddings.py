"""
Dedupe Identity Embeddings
==========================
One-time cleanup: historical bug re-added the same enrollment embedding to each
identity on every application restart (loader missed NULL faiss_index_type rows),
leaving 4-11 identical copies per person. Identical vectors add zero recognition
value and skew nothing except storage/scan cost - but they hide how few real
views each identity has.

Keeps the OLDEST row of each exact-duplicate group per identity, deletes the rest.

Run inside the API container:
    docker exec face_recognition_api python /app/scripts/maintenance/dedupe_identity_embeddings.py
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


def main():
    conn = psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM identity_embeddings WHERE embedding IS NOT NULL")
    before = cur.fetchone()[0]

    # Delete exact duplicates: same identity + identical vector, keep the oldest id
    cur.execute(
        """
        DELETE FROM identity_embeddings e
        USING identity_embeddings keeper
        WHERE e.identity_id = keeper.identity_id
          AND e.embedding IS NOT NULL
          AND keeper.embedding IS NOT NULL
          AND e.embedding::text = keeper.embedding::text
          AND e.id > keeper.id
        """
    )
    deleted = cur.rowcount

    cur.execute("SELECT COUNT(*) FROM identity_embeddings WHERE embedding IS NOT NULL")
    after = cur.fetchone()[0]

    cur.execute(
        """
        SELECT i.display_name, COUNT(e.id)
        FROM identities i
        JOIN identity_embeddings e ON e.identity_id = i.id AND e.embedding IS NOT NULL
        WHERE i.type = 'KNOWN'
        GROUP BY i.display_name ORDER BY i.display_name
        """
    )
    rows = cur.fetchall()

    conn.commit()
    cur.close()
    conn.close()

    print(f"Embeddings before: {before}")
    print(f"Duplicates deleted: {deleted}")
    print(f"Embeddings after:  {after}")
    print("KNOWN identities after cleanup:")
    for name, count in rows:
        print(f"  {name}: {count} embedding(s)")


if __name__ == "__main__":
    sys.exit(main())
