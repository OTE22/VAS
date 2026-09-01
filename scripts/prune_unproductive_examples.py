"""Remove LEARNED knowledge-base examples whose SQL matches nothing.

Why this exists
---------------
`learn_from_query` used to gate on `query_result["success"]`, which is True
for a query that ran cleanly and returned zero rows. So queries that found
nothing were saved as worked examples.

One of them did real damage. Asked "track Joey and give me report in Arabic",
the generator translated the person's NAME into Arabic:

    WHERE LOWER(f.name) LIKE LOWER('%عوي%')     -- 0 rows

The table stores 'JOEY' in Latin script, so it matched nothing — and the query
was then learned. It became the TOP-ranked example for every similar
question, so the agent reproduced the bug on every attempt and no prompt
instruction could outweigh a worked example that said otherwise. The gate is
fixed; this removes what it already let in.

What it deletes, and what it never deletes
------------------------------------------
ONLY examples with `source` = "learned" (or "user") whose stored SQL, run
read-only through the agent's own validated path, returns zero rows. SEED
examples are curated and are never candidates whatever they return — a seed
that matches nothing on this deployment may be perfectly correct for another.

An example that cannot be run at all (invalid SQL, missing table) is REPORTED
but not deleted: "I could not check it" is not evidence of a bad example, and
deleting on that basis would quietly erase examples during an unrelated
outage.

Safety protocol
---------------
    python scripts/prune_unproductive_examples.py            # dry run
    python scripts/prune_unproductive_examples.py --apply --expect N

The dry run prints every candidate with its question, source and row count.
`--apply` requires `--expect N` from that dry run; if the count has changed
the script refuses and deletes nothing.

Run inside the api container:

    docker exec face_recognition_api python scripts/prune_unproductive_examples.py
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PRUNABLE_SOURCES = {"learned", "user"}


def _load():
    from sql_agent.config import config
    from sql_agent.database import DatabaseManager
    from sql_agent.knowledge_base import SQLKnowledgeBase
    return SQLKnowledgeBase(config), DatabaseManager(config)


def _all_examples(kb):
    """Every stored example, with its id, question, sql and source."""
    raw = kb.collection.get(include=["documents", "metadatas"])
    out = []
    for doc_id, document, meta in zip(raw.get("ids") or [],
                                      raw.get("documents") or [],
                                      raw.get("metadatas") or []):
        meta = meta or {}
        out.append({
            "id": doc_id,
            "question": meta.get("question") or document or "",
            "sql": meta.get("sql") or "",
            "source": (meta.get("source") or "unknown").lower(),
        })
    return out


def _row_count(db, sql):
    """Rows the example's SQL returns now, or None if it cannot be run.

    Goes through the agent's own execute path, so the AST guard and the
    read-only policy apply exactly as they do in a real turn.
    """
    try:
        result = db.execute_query(sql)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:90]
    if not result.get("success"):
        return None, str(result.get("error"))[:90]
    return int(result.get("row_count") or 0), None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect", type=int)
    args = parser.parse_args()

    kb, db = _load()
    examples = _all_examples(kb)
    print(f"stored examples: {len(examples)}")

    prunable, unrunnable, kept = [], [], 0
    for example in examples:
        if example["source"] not in PRUNABLE_SOURCES or not example["sql"]:
            kept += 1
            continue
        rows, error = _row_count(db, example["sql"])
        if rows is None:
            unrunnable.append((example, error))
        elif rows == 0:
            prunable.append(example)
        else:
            kept += 1

    print(f"\nproductive or seed (kept): {kept}")

    if unrunnable:
        print(f"\ncould not be checked — REPORTED, NOT DELETED ({len(unrunnable)}):")
        for example, error in unrunnable:
            print(f"  {example['source']:<8} {example['question'][:60]!r}")
            print(f"           {error}")

    print(f"\nlearned examples that match NOTHING ({len(prunable)}):")
    for example in prunable:
        print(f"  {example['source']:<8} {example['question'][:66]!r}")
        print(f"           {' '.join(example['sql'].split())[:100]}")

    if not args.apply:
        print(f"\nDry run only. To delete: --apply --expect {len(prunable)}")
        return 0

    if args.expect is None:
        print("\nREFUSED: --apply requires --expect N (the dry run's total).")
        return 1
    if args.expect != len(prunable):
        print(f"\nREFUSED: found {len(prunable)}, expected {args.expect}. "
              f"The data changed since the dry run — look again. "
              f"Nothing was deleted.")
        return 1
    if not prunable:
        print("\nNothing to delete.")
        return 0

    kb.collection.delete(ids=[e["id"] for e in prunable])
    print(f"\nDeleted {len(prunable)} unproductive example(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
