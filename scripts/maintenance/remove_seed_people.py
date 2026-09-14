"""Preview/apply removal of exact seed_person_<digits> records, never other QA data.

This narrow maintenance action refuses filesystem or merged-identity cleanup.
Use the full lifecycle workflow if seed records have acquired real face media.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import select, func, delete, update
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from config import settings
import db_models as m
from backend.core.known_face_lifecycle import related_filters, FILE_COLUMNS


async def run(args):
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            async with db.begin():
                rows = (await db.execute(select(m.Identity).where(
                    m.Identity.display_name.op('~')(r'^seed_person_[0-9]+$'))
                    .order_by(m.Identity.id).with_for_update())).scalars().all()
                ids = [p.id for p in rows]
                if not ids:
                    print(json.dumps({'seed_people_remaining': 0})); return
                if any(p.best_snapshot_path or p.merged_into_id for p in rows):
                    raise RuntimeError('Seed records have media or merge ownership; manual lifecycle review required')
                if (await db.execute(select(m.Identity.id).where(m.Identity.merged_into_id.in_(ids)).limit(1))).first():
                    raise RuntimeError('Seed records have merged aliases')
                if any((Path(settings.FACES_DIR) / str(i)).exists() for i in ids):
                    raise RuntimeError('Seed gallery folders exist; full media cleanup is required')
                filters = related_filters(ids)
                tables = m.Base.metadata.tables
                counts = {}
                for name, predicate in filters.items():
                    counts[name] = (await db.execute(select(func.count()).select_from(tables[name]).where(predicate))).scalar_one()
                    for column in FILE_COLUMNS.get(name, ()):
                        if (await db.execute(select(tables[name].c[column]).where(predicate,
                                tables[name].c[column].isnot(None)).limit(1))).first():
                            raise RuntimeError(f'Media reference in {name}; use full media cleanup')
                if counts.get('identity_embeddings') or counts.get('identity_merges'):
                    raise RuntimeError('Embeddings or merge provenance require full lifecycle cleanup')
                payload = {'ids': [str(i) for i in ids], 'counts': counts}
                token = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                print(json.dumps({'seed_people': len(ids), 'dependent_records': {k:v for k,v in counts.items() if v},
                    'preview_token': token, 'apply': args.apply}, sort_keys=True), flush=True)
                if not args.apply:
                    await db.rollback(); return
                if token != args.expected_token or len(ids) != args.expected_count:
                    raise RuntimeError('Scope changed since preview; nothing deleted')
                survivors = set((await db.execute(select(m.Identity.id).where(m.Identity.id.notin_(ids)))).scalars().all())
                await db.execute(update(m.MLLabel).where(m.MLLabel.supersedes_id.in_(
                    select(m.MLLabel.id).where(filters['ml_labels']))).values(supersedes_id=None))
                first = ['watchlist_alerts', 'live_alert_triggers', 'live_alert_audit_log']
                removed = {}
                for name in first + [n for n in filters if n not in first]:
                    result = await db.execute(delete(tables[name]).where(filters[name]))
                    if result.rowcount:
                        removed[name] = result.rowcount
                await db.execute(delete(m.Identity).where(m.Identity.id.in_(ids)))
                remaining = set((await db.execute(select(m.Identity.id))).scalars().all())
                if remaining != survivors:
                    raise RuntimeError('Non-seed identity set changed; rolling back')
                for name, predicate in filters.items():
                    if (await db.execute(select(func.count()).select_from(tables[name]).where(predicate))).scalar_one():
                        raise RuntimeError(f'Linked seed records remain in {name}; rolling back')
                db.add(m.IdentityAuditLog(username='system', action_type='seed_people_deleted',
                    success=True, action_details={'deleted_people': len(ids),
                    'selection': '^seed_person_[0-9]+$', 'requested_by': 'workspace user'}))
                print(json.dumps({'deleted_people': len(ids), 'deleted_dependents': removed,
                    'other_people_preserved': len(survivors)}, sort_keys=True), flush=True)
            print('COMMITTED', flush=True)
            from backend.core.redis_cache import RedisCacheService
            cache = RedisCacheService()
            if not await cache.initialize():
                raise RuntimeError('Deletion committed, but cache invalidation needs retry')
            try:
                await cache.invalidate_unknown_cache()
                await cache.invalidate_dashboard_cache()
                print('CACHES_INVALIDATED', flush=True)
            finally:
                await cache.redis_client.aclose()
    finally:
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-token')
    parser.add_argument('--expected-count', type=int)
    asyncio.run(run(parser.parse_args()))
