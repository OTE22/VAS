"""Remove learned knowledge-base examples that carry a camera-scope IN-list.

Until 2026-09-03 the agent kept the guard's SCOPED SQL (one user's pipeline
ids wrapped around every table) as the canonical query, so a scoped user's
successful turns were learned with that IN-list and shown to every later
generation. The guard now hands the agent the unscoped canonical text and
learning refuses scope literals; this removes what was already stored.

    docker exec -w /app face_recognition_api python scripts/dev/purge_scoped_examples.py [--apply]
"""

import re
import sys

sys.path.insert(0, "/app")

from sql_agent.config import Config  # noqa: E402
from sql_agent.knowledge_base import SQLKnowledgeBase  # noqa: E402
from sql_agent.tools.agent_tools import SQLAgentTools  # noqa: E402

apply = "--apply" in sys.argv
kb = SQLKnowledgeBase(Config())
data = kb.collection.get(include=["metadatas", "documents"])

doomed = []
for doc_id, meta, doc in zip(data["ids"], data["metadatas"], data["documents"]):
    meta = meta or {}
    if meta.get("source") != "learned":
        continue
    if SQLAgentTools._carries_scope_literals(meta.get("sql") or "") or \
            SQLAgentTools._carries_scope_literals(doc or ""):
        doomed.append(doc_id)
        print(f"{'DELETE' if apply else 'would delete'} {doc_id[:8]} "
              f"purpose={str(meta.get('purpose'))[:60]!r} added={meta.get('added_at')}")

if doomed and apply:
    kb.collection.delete(ids=doomed)
print(f"{len(doomed)} scoped example(s) {'deleted' if apply else 'found (dry run; pass --apply)'}; "
      f"pattern: {re.escape('pipeline_id IN (<uuid literals>)')}")
