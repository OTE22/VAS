"""Remove LEARNED knowledge-base examples that read pipelines.total_detections.

A wrong-but-executable query is learned the first time it returns a row, and
from then on it is the closest example for that exact question - which is how
"What are the most active pipelines?" kept producing "pytest-cam ... null"
after both the schema text and the seeds were corrected. Seeds re-load by
hash; learned entries do not. This removes the poisoned ones.

    docker exec -w /app face_recognition_api python scripts/dev/purge_cached_counter_examples.py [--apply]
"""
import re
import sys

sys.path.insert(0, "/app")

from sql_agent.config import Config  # noqa: E402
from sql_agent.knowledge_base import SQLKnowledgeBase  # noqa: E402

READS_CACHE = re.compile(
    r"\btotal_detections\b(?![^,]*\bCOUNT\()", re.I)


def reads_cached_counter(sql: str) -> bool:
    text = " ".join(str(sql or "").split())
    if "COUNT(" in text.upper():
        return False
    return ("total_detections" in text) and ("FROM pipelines" in text
                                             or "pipelines" in text)


kb = SQLKnowledgeBase(Config())
data = kb.collection.get(include=["metadatas", "documents"])
victims = []
for doc_id, meta, question in zip(data["ids"], data["metadatas"], data["documents"]):
    if (meta or {}).get("source") != "learned":
        continue
    if reads_cached_counter((meta or {}).get("sql", "")):
        victims.append((doc_id, question, (meta or {}).get("sql", "")[:140]))

print(f"learned examples reading the cached counter: {len(victims)}")
for doc_id, question, sql in victims:
    print(f"  - {question!r}\n      {sql}")

if "--apply" in sys.argv and victims:
    kb.collection.delete(ids=[v[0] for v in victims])
    print(f"deleted {len(victims)}")
elif victims:
    print("dry run; pass --apply to delete")
