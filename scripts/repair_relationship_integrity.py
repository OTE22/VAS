"""
Repair the demo/development database so the relationship-integrity migrations
(c2d3e4f5a6b7, d4e5f6a7b8c9) can apply. Operator-run, never automatic.

    # from the host, with the API stopped:
    docker stop face_recognition_api
    docker exec -w /app face_recognition_api \
        python scripts/repair_relationship_integrity.py            # dry-run (default)
    docker exec -w /app face_recognition_api \
        python scripts/repair_relationship_integrity.py --apply --yes-i-understand
    docker exec -w /app/alembic face_recognition_api python -m alembic upgrade head
    docker start face_recognition_api

Why this exists
---------------
The migrations add real foreign keys and NOT NULL rules. Alembic never deletes
data: each migration checks its preconditions and REFUSES (RuntimeError naming
the count and this command) when rows would violate a constraint it is about to
add. Every destructive or uncertain-provenance repair lives here instead, where
it is explicit, dry-run by default and refuses to touch production.

What it repairs (and why deletion, not guessing)
------------------------------------------------
1. identity_embeddings back-links written by the retired heuristic ("the newest
   NULL-linked embedding of this identity at flush time"): the old writer kept
   nothing that ties an embedding to its frame, so no exact origin can be proven
   → the untrusted CAMERA-ORIGIN embeddings are removed. Camera-origin rows that
   never received a link are removed too. Enrollment ("uploaded") and preload
   ("preloaded") rows are NOT camera evidence: they are kept — the migration
   turns their sentinel pipeline_id into NULL. Removal goes through the canonical
   path (vector-index key removal + row delete), never a bare DELETE.
2. identity_appearances / identity_embeddings whose pipeline_id has no
   pipelines row (orphans by definition under the new FK).
3. watchlist_alerts.search_id values with no search_history row → NULL.
4. ml_drift_reports with NULL model_id (unusable under the new contract),
   ml_shadow_comparisons + successful shadow ml_predictions without model/threshold lineage,
   ml_model_thresholds objective rows (regrained by the migration).
5. Demo unknown identities left with zero embeddings, images, appearances,
   faces and no history/operational references are removed with the same
   guarded deletion the maintenance scripts use (never cascading through
   valid history — a referenced identity is left and reported).

Guards
------
* refuses when settings.ENVIRONMENT is production;
* refuses unless the connected database is the development database
  (name `face_recognition`) — a positive check, not a naming convention;
* dry-run by default; `--apply` additionally requires `--yes-i-understand`.
"""
import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import List

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEV_DATABASE_NAME = "face_recognition"
CAMERA_SENTINELS = ("uploaded", "preloaded")


@dataclass
class Step:
    name: str
    count_sql: str
    apply_sql: List[str] = field(default_factory=list)
    before: int = 0
    after: int = 0


def _steps() -> List[Step]:
    return [
        Step(
            "watchlist_alerts.search_id pointing at no search_history row → NULL",
            "SELECT count(*) FROM watchlist_alerts a WHERE a.search_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM search_history s WHERE s.id = a.search_id)",
            ["UPDATE watchlist_alerts a SET search_id = NULL WHERE a.search_id IS NOT NULL "
             "AND NOT EXISTS (SELECT 1 FROM search_history s WHERE s.id = a.search_id)"],
        ),
        Step(
            "identity_appearances with a pipeline_id that has no pipelines row",
            "SELECT count(*) FROM identity_appearances a WHERE NOT EXISTS "
            "(SELECT 1 FROM pipelines p WHERE p.pipeline_id = a.pipeline_id)",
            ["DELETE FROM identity_appearances a WHERE NOT EXISTS "
             "(SELECT 1 FROM pipelines p WHERE p.pipeline_id = a.pipeline_id)"],
        ),
        Step(
            "ml_drift_reports with NULL model_id",
            "SELECT count(*) FROM ml_drift_reports WHERE model_id IS NULL",
            ["DELETE FROM ml_drift_reports WHERE model_id IS NULL"],
        ),
        Step(
            "ml_shadow_comparisons of successful shadow predictions without model/threshold lineage",
            "SELECT count(*) FROM ml_shadow_comparisons c WHERE c.prediction_id IN "
            "(SELECT id FROM ml_predictions WHERE actual_mode_used = 'shadow' AND fallback_reason IS NULL AND (threshold_id IS NULL OR threshold_version IS NULL OR model_id IS NULL))",
            ["DELETE FROM ml_shadow_comparisons c WHERE c.prediction_id IN "
             "(SELECT id FROM ml_predictions WHERE actual_mode_used = 'shadow' AND fallback_reason IS NULL AND (threshold_id IS NULL OR threshold_version IS NULL OR model_id IS NULL))"],
        ),
        Step(
            "successful shadow ml_predictions without model/threshold lineage (explicit failure rows are kept)",
            "SELECT count(*) FROM ml_predictions WHERE actual_mode_used = 'shadow' AND fallback_reason IS NULL AND (threshold_id IS NULL OR threshold_version IS NULL OR model_id IS NULL)",
            ["UPDATE threat_assessments SET ml_prediction_id = NULL WHERE ml_prediction_id IN "
             "(SELECT id FROM ml_predictions WHERE actual_mode_used = 'shadow' AND fallback_reason IS NULL AND (threshold_id IS NULL OR threshold_version IS NULL OR model_id IS NULL))",
             "DELETE FROM ml_predictions WHERE actual_mode_used = 'shadow' AND fallback_reason IS NULL AND (threshold_id IS NULL OR threshold_version IS NULL OR model_id IS NULL)"],
        ),
        Step(
            # PRE-REGRAIN shape only (legacy `objective` column present): after
            # d4e5f6a7b8c9 the rows are threshold SETS and are never touched.
            "ml_model_thresholds objective rows (pre-regrain shape only; regrained sets are never touched)",
            "SELECT CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns "
            " WHERE table_name = 'ml_model_thresholds' AND column_name = 'objective') "
            " THEN (SELECT count(*) FROM ml_model_thresholds) ELSE 0 END",
            ["DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns "
             " WHERE table_name = 'ml_model_thresholds' AND column_name = 'objective') THEN "
             " UPDATE ml_predictions SET threshold_id = NULL WHERE threshold_id IS NOT NULL; "
             " DELETE FROM ml_model_thresholds; END IF; END $$"],
        ),
    ]


async def _camera_embedding_ids(db) -> List[int]:
    """Camera-origin embeddings whose provenance is untrusted: every row with a
    real pipeline id (not a sentinel) whose detection link is heuristic or
    absent. Under the retired writer that is ALL of them."""
    rows = await db.execute(text(
        "SELECT id FROM identity_embeddings "
        "WHERE pipeline_id IS NOT NULL AND pipeline_id NOT IN ('uploaded', 'preloaded') "
        "ORDER BY id"))
    return [int(r[0]) for r in rows.all()]


async def _remove_embeddings(db, ids: List[int]) -> int:
    """Canonical removal: vector-index keys first, then rows (identity_retention
    does exactly this for surplus embeddings)."""
    if not ids:
        return 0
    from backend.core.vector_index.access import remove_embedding_keys
    await remove_embedding_keys(db, ids)
    res = await db.execute(text("DELETE FROM identity_embeddings WHERE id = ANY(CAST(:ids AS int[]))"),
                           {"ids": ids})
    return res.rowcount or 0


async def _evidence_free_unknown_identity_ids(db) -> List[int]:
    rows = await db.execute(text("""
        SELECT i.id::text FROM identities i
        WHERE i.type::text = 'UNKNOWN'
          AND NOT EXISTS (SELECT 1 FROM identity_embeddings e WHERE e.identity_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM identity_images g WHERE g.identity_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM identity_appearances a WHERE a.identity_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM faces f WHERE f.identity_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM identity_merges m WHERE m.from_identity_id = i.id OR m.to_identity_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM identity_audit_log l WHERE l.identity_id = i.id OR l.related_identity_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM identities c WHERE c.merged_into_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM watchlist_entries w WHERE w.identity_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM live_search_alerts s WHERE s.identity_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM identity_relationships r WHERE r.identity_id_1 = i.id OR r.identity_id_2 = i.id)
          AND NOT EXISTS (SELECT 1 FROM similarity_training_data t WHERE t.identity_id_1 = i.id OR t.identity_id_2 = i.id)
          AND NOT EXISTS (SELECT 1 FROM threat_assessments ta WHERE ta.person_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM ml_predictions mp WHERE mp.person_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM ml_labels ml WHERE ml.person_id = i.id)
    """))
    return [r[0] for r in rows.all()]


async def _guards(db) -> List[str]:
    problems = []
    try:
        from config import settings
        env = str(settings.ENVIRONMENT).strip().lower()
        if env in ("production", "prod"):
            problems.append("ENVIRONMENT is production")
    except Exception as exc:  # pragma: no cover
        problems.append(f"cannot read settings: {exc}")
    dbname = (await db.execute(text("SELECT current_database()"))).scalar()
    if dbname != DEV_DATABASE_NAME:
        problems.append(f"connected database is {dbname!r}, not the development database {DEV_DATABASE_NAME!r}")
    return problems


async def main(args) -> int:
    from db_connection import db_manager
    await db_manager.init_db()
    async with db_manager.get_session() as db:
        problems = await _guards(db)
        if problems:
            print("REFUSING:\n  " + "\n  ".join(problems))
            return 2

        steps = _steps()
        for s in steps:
            s.before = int((await db.execute(text(s.count_sql))).scalar() or 0)
        camera_ids = await _camera_embedding_ids(db)
        sentinel_sql = "SELECT count(*) FROM identity_embeddings WHERE pipeline_id IN ('uploaded', 'preloaded')"
        sentinel_n = int((await db.execute(text(sentinel_sql))).scalar() or 0)

        print("Repair plan (dry-run)" if not args.apply else "Repair plan (APPLY)")
        print(f"  camera-origin embeddings with heuristic/absent provenance → REMOVE (canonical path): {len(camera_ids)}")
        print(f"  enrollment/preload sentinel embeddings → KEEP (migration sets pipeline_id NULL): {sentinel_n}")
        for s in steps:
            print(f"  {s.name}: {s.before}")

        if not args.apply:
            print("\nDry-run only. Re-run with --apply --yes-i-understand to apply.")
            return 0
        if not args.yes_i_understand:
            print("\nREFUSING: --apply also requires --yes-i-understand.")
            return 2

        removed = await _remove_embeddings(db, camera_ids)
        print(f"\nremoved camera-origin embeddings: {removed}")
        for s in steps:
            if s.before:
                for sql in s.apply_sql:
                    await db.execute(text(sql))
            s.after = int((await db.execute(text(s.count_sql))).scalar() or 0)
            print(f"  {s.name}: {s.before} → {s.after}")
        orphan_ids = await _evidence_free_unknown_identity_ids(db)
        if orphan_ids:
            res = await db.execute(text("DELETE FROM identities WHERE id::text = ANY(CAST(:ids AS text[]))"),
                                   {"ids": orphan_ids})
            print(f"  evidence-free demo unknown identities removed: {res.rowcount}")
        else:
            print("  evidence-free demo unknown identities removed: 0")
        await db.commit()

        # verification: nothing the migrations will refuse on remains
        remaining = {s.name: int((await db.execute(text(s.count_sql))).scalar() or 0) for s in steps}
        remaining["camera-origin embeddings without proven provenance"] = len(await _camera_embedding_ids(db))
        bad = {k: v for k, v in remaining.items() if v}
        if bad:
            print("\nFAILED — rows remain:", bad)
            return 3
        print("\nOK — repaired. Next: alembic upgrade head (docker exec -w /app/alembic face_recognition_api python -m alembic upgrade head)")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="apply the repairs (default: dry-run)")
    parser.add_argument("--yes-i-understand", action="store_true",
                        help="required alongside --apply; deleted demo rows are not recoverable")
    sys.exit(asyncio.run(main(parser.parse_args())))
