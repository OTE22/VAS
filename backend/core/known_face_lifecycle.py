"""Known-person lifecycle. No deletion is reported complete before file/index cleanup.

A transient identity audit row is the durable cleanup journal. The identity is
made INACTIVE and the journal committed before any irreversible cleanup. On a
failure the record stays inactive, the journal survives, and the same operation
can be retried after a process restart. Completion replaces the journal with a
minimal audit receipt and removes the old personal audit payloads.
"""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import String, cast, delete, func, or_, select, update

import db_models as m
from config import settings
from backend.core.identity_service import invalidate_merge_suggestions
from backend.core.vector_index.access import get_vector_index, get_vector_index_manager

logger = logging.getLogger(__name__)
PENDING = "person_delete_pending"


async def _person(db, identity_id, lock=False):
    query = select(m.Identity).where(m.Identity.id == identity_id, m.Identity.type == m.IdentityType.KNOWN)
    if lock:
        query = query.with_for_update()
    person = (await db.execute(query)).scalar_one_or_none()
    if person is None:
        raise HTTPException(404, "Known person not found.")
    if person.merged_into_id is not None or person.status == m.IdentityStatus.MERGED:
        raise HTTPException(409, "This record was merged. Manage the surviving person instead.")
    return person


async def _journal(db, identity_id):
    return (await db.execute(select(m.IdentityAuditLog).where(
        m.IdentityAuditLog.identity_id == identity_id,
        m.IdentityAuditLog.action_type == PENDING).order_by(m.IdentityAuditLog.id.desc()))).scalars().first()


async def set_active(db, identity_id, active, actor):
    person = await _person(db, identity_id, lock=True)
    if await _journal(db, identity_id):
        raise HTTPException(409, "Deletion is unfinished. Retry permanent deletion; this record cannot be reactivated.")
    previous = person.status.value
    person.status = m.IdentityStatus.ACTIVE if active else m.IdentityStatus.INACTIVE
    if not active:
        await invalidate_merge_suggestions(db, [identity_id], "Person deactivated by administrator")
    db.add(m.IdentityAuditLog(user_id=actor.id, historical_user_id=actor.id, username=actor.username,
        identity_id=identity_id, action_type="reactivate" if active else "deactivate",
        before_state={"status": previous}, after_state={"status": person.status.value}, success=True))
    await db.commit()
    # Search resolution on BOTH backends checks the committed identity status.
    # The database remains authoritative even before FAISS reconciliation runs.
    return {"success": True, "status": "active" if active else "inactive"}


def storage_file(value):
    """Resolve a file strictly inside storage; never follow a symlink or erase a directory."""
    root = Path(settings.STORAGE_DIR).resolve()
    raw = str(value).replace("\\", "/")
    if raw.startswith("/storage/"):
        candidate = root / raw[len("/storage/"):]
    elif raw.startswith("storage/"):
        candidate = root / raw[len("storage/"):]
    elif Path(raw).is_absolute():
        candidate = Path(raw)
    else:
        candidate = root / raw
    resolved = candidate.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("Photo path is outside managed storage")
    if any(p.is_symlink() for p in (candidate, *candidate.parents) if p != root):
        raise ValueError("Symlink photo paths cannot be deleted")
    if resolved.is_dir():
        raise ValueError("A photo path names a directory")
    return resolved


def _contains_ids(column, ids):
    return or_(*(cast(column, String).contains(str(i), autoescape=True) for i in ids))


async def _family(db, identity_id):
    # Current merged aliases belong to the same person. Do not follow historical
    # merge edges into a different surviving person after an unmerge.
    ids, frontier = {identity_id}, {identity_id}
    while frontier:
        found = set((await db.execute(select(m.Identity.id).where(
            m.Identity.merged_into_id.in_(frontier)))).scalars().all()) - ids
        ids.update(found)
        frontier = found
    return sorted(ids, key=str)


def related_filters(ids):
    """Every modeled direct FK is covered, including SET NULL and non-cascading FKs."""
    tables = m.Base.metadata.tables
    filters = {}
    for name, table in tables.items():
        columns = [fk.parent for fk in table.foreign_keys if fk.target_fullname == "identities.id"]
        if columns and name != "identities":
            filters[name] = or_(*(c.in_(ids) for c in columns))
    filters["merge_suggestions"] = _contains_ids(tables["merge_suggestions"].c.identity_ids, ids)
    filters["pending_enrollments"] = _contains_ids(tables["pending_enrollments"].c.candidates, ids)
    filters["search_history"] = or_(*(_contains_ids(tables["search_history"].c[c], ids)
        for c in ("results_summary", "filters", "exclude_identity_ids")))
    for name in ("threat_assessments", "ml_labels", "ml_predictions", "ml_shadow_comparisons"):
        table = tables[name]
        filters[name] = or_(filters.get(name, False), _contains_ids(table.c.subject_id, ids))
    filters["ml_feature_snapshots"] = _contains_ids(tables["ml_feature_snapshots"].c.entity_id, ids)
    # Indirect records contain copies of face snapshots and must be collected
    # before their parents cascade away.
    filters["watchlist_alerts"] = m.WatchlistAlert.watchlist_entry_id.in_(select(m.WatchlistEntry.id).where(filters["watchlist_entries"]))
    alert_ids = select(m.LiveSearchAlert.id).where(filters["live_search_alerts"])
    filters["live_alert_triggers"] = m.LiveAlertTrigger.alert_id.in_(alert_ids)
    filters["live_alert_audit_log"] = m.LiveAlertAuditLog.alert_id.in_(alert_ids)
    return filters


FILE_COLUMNS = {
    "identity_images": ("storage_path",), "faces": ("face_image_path",),
    "identity_appearances": ("best_snapshot_path",),
    "watchlist_alerts": ("snapshot_path",), "live_alert_triggers": ("snapshot_path", "clip_path"),
    "pending_enrollments": ("storage_path",),
}


def _provenance_paths(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith('_path') and isinstance(child, str) and child:
                yield child
            elif isinstance(child, (dict, list)):
                yield from _provenance_paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from _provenance_paths(child)


async def deletion_plan(db, identity_id):
    person = await _person(db, identity_id)
    ids = await _family(db, identity_id)
    filters, paths, counts = related_filters(ids), set(), {}
    tables = m.Base.metadata.tables
    for name, predicate in filters.items():
        counts[name] = (await db.execute(select(func.count()).select_from(tables[name]).where(predicate))).scalar() or 0
        for column in FILE_COLUMNS.get(name, ()):
            paths.update(p for p in (await db.execute(select(tables[name].c[column]).where(predicate))).scalars().all() if p)
    paths.update(p for p in (await db.execute(select(m.Identity.best_snapshot_path).where(m.Identity.id.in_(ids)))).scalars().all() if p)
    for provenance in (await db.execute(select(m.IdentityMerge.provenance).where(filters['identity_merges']))).scalars().all():
        paths.update(_provenance_paths(provenance))
    keys = list((await db.execute(select(m.IdentityEmbedding.id).where(m.IdentityEmbedding.identity_id.in_(ids)))).scalars().all())
    # Include unreferenced enrollment files in the owned UUID folders, never a
    # display-name folder that another person might share.
    for i in ids:
        folder = Path(settings.FACES_DIR) / str(i)
        if folder.is_symlink():
            raise ValueError("An enrollment folder is a symlink")
        if folder.exists():
            paths.update(str(p) for p in folder.rglob('*') if p.is_file() or p.is_symlink())
    canonical = sorted({str(storage_file(p)) for p in paths})
    pending = await _journal(db, identity_id)
    if pending:
        canonical = sorted(set(canonical) | set(pending.action_details.get("files", [])))
        keys = sorted(set(keys) | set(pending.action_details.get("embedding_ids", [])))
    payload = {"ids": [str(i) for i in ids], "files": canonical, "embedding_ids": sorted(keys),
               "counts": counts, "name": person.display_name}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return payload, {"identity_id": str(identity_id), "display_name": person.display_name,
        "photos": counts.get("identity_images", 0), "embeddings": len(keys),
        "sightings": counts.get("faces", 0), "files": len(canonical), "merged_aliases": len(ids) - 1,
        "watchlist_memberships": counts.get("watchlist_entries", 0),
        "live_alerts": counts.get("live_search_alerts", 0), "preview_token": digest,
        "deletion_pending": pending is not None}


async def _shared_files(db, paths, ids, filters):
    root = Path(settings.STORAGE_DIR).resolve()
    variants = set()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        variants.update([str(path), path.as_posix(), relative, 'storage/' + relative, '/storage/' + relative])
    variants = sorted(variants)
    tables = m.Base.metadata.tables
    shared = set()
    for name, columns in {**FILE_COLUMNS, "identities": ("best_snapshot_path",)}.items():
        table = tables[name]
        excluded = table.c.id.in_(ids) if name == "identities" else filters[name]
        for column in columns:
            for offset in range(0, len(variants), 500):
                found = (await db.execute(select(table.c[column]).where(
                    table.c[column].in_(variants[offset:offset + 500]),
                    func.coalesce(excluded, False).is_(False)))).scalars().all()
                shared.update(storage_file(p) for p in found)
    return shared


async def _purge_index(keys):
    if settings.VECTOR_BACKEND == "pgvector":
        return  # deleting identity_embeddings is the authoritative index deletion
    index, manager = get_vector_index(), get_vector_index_manager()
    if index is None or manager is None or not hasattr(index, 'purge'):
        raise RuntimeError("Recognition index is unavailable for verified deletion")
    if manager._saving or manager._reconciling:
        raise RuntimeError("Recognition index maintenance is busy; retry deletion")
    from backend.core.distributed_lock import DistributedLock
    lock = DistributedLock("vector-index-save", ttl_seconds=600)
    if not await lock.acquire():
        raise RuntimeError("Recognition index maintenance is busy; retry deletion")
    manager._saving = True
    try:
        await asyncio.to_thread(index.purge, keys)
    finally:
        manager._saving = False
        await lock.release()


async def permanently_delete(db, identity_id, confirmation_name, preview_token, actor):
    person = await _person(db, identity_id, lock=True)
    if confirmation_name != person.display_name:
        raise HTTPException(400, "Type the person's full name exactly to confirm permanent deletion.")
    plan, impact = await deletion_plan(db, identity_id)
    if preview_token != impact["preview_token"]:
        raise HTTPException(409, "The record changed. Refresh the deletion preview and confirm again.")
    job = await _journal(db, identity_id)
    if job is None:
        person.status = m.IdentityStatus.INACTIVE
        await invalidate_merge_suggestions(db, [UUID(i) for i in plan['ids']], "Permanent deletion in progress")
        job = m.IdentityAuditLog(user_id=actor.id, historical_user_id=actor.id,
            username=actor.username, identity_id=identity_id, action_type=PENDING,
            action_details={"files": plan['files'], "embedding_ids": plan['embedding_ids']}, success=False)
        db.add(job)
        await db.flush()
    else:
        job.action_details = {"files": plan['files'], "embedding_ids": plan['embedding_ids']}
    job_id = job.id
    # Persist the journal and stop recognition BEFORE destructive work. An
    # interruption leaves a resumable inactive record, never a false success.
    await db.commit()
    try:
        await _person(db, identity_id, lock=True)
        # Re-read after the journal commit: an already-running camera write
        # may have finished in that gap. Include it before locking/deleting.
        plan, _ = await deletion_plan(db, identity_id)
        ids = [UUID(i) for i in plan['ids']]
        await db.execute(select(m.Identity.id).where(m.Identity.id.in_(ids)).with_for_update())
        filters = related_filters(ids)
        await _purge_index(plan['embedding_ids'])
        import backend.core as core
        tracker = getattr(core, 'face_tracker', None)
        if tracker is not None:
            names = (await db.execute(select(m.Identity.display_name).where(m.Identity.id.in_(ids)))).scalars().all()
            await tracker.forget_names(names)
        shared = 0
        paths = [storage_file(raw) for raw in plan['files']]
        shared_paths = await _shared_files(db, paths, ids, filters)
        for path in paths:
            if path in shared_paths:
                shared += 1
                continue
            path.unlink(missing_ok=True)
        # Explicit deletion handles historical SET NULL references and merge
        # records. Cascades handle their children. Bulk SQL avoids ORM nulling
        # nonnullable FKs on relationships loaded elsewhere in the session.
        tables = m.Base.metadata.tables
        # Self-referencing label revisions can otherwise block the removal.
        await db.execute(update(m.MLLabel).where(m.MLLabel.supersedes_id.in_(
            select(m.MLLabel.id).where(filters['ml_labels']))).values(supersedes_id=None))
        order = list(FILE_COLUMNS) + ['live_alert_audit_log']
        order += [n for n in filters if n not in order]
        # Child audit rows must be removed while alert membership subqueries
        # still exist; all indirect predicates are evaluated before parents.
        order = ['watchlist_alerts', 'live_alert_triggers', 'live_alert_audit_log'] + [
            n for n in order if n not in ('watchlist_alerts', 'live_alert_triggers', 'live_alert_audit_log')]
        for name in order:
            predicate = filters[name]
            if name == 'identity_audit_log':
                predicate = predicate & (tables[name].c.id != job_id)
            await db.execute(delete(tables[name]).where(predicate))
        await db.execute(update(m.IdentityAuditLog).where(m.IdentityAuditLog.id == job_id).values(
            identity_id=None, related_identity_id=None, action_type="person_deleted", success=True,
            action_details={"deleted_record_id": str(identity_id), "shared_files_retained": shared},
            before_state=None, after_state=None, notes=None))
        await db.execute(delete(m.Identity).where(m.Identity.id.in_(ids)))
        await db.commit()
        return {"success": True, "deleted": True, "shared_files_retained": shared,
                "message": "Person and linked face data permanently deleted."}
    except Exception as exc:
        await db.rollback()
        logger.exception("[KNOWN_DELETE] Cleanup incomplete for record %s", identity_id)
        raise HTTPException(503, {"code": "deletion_incomplete", "deletion_pending": True,
            "message": "Deletion is unfinished. The person is inactive. Refresh the preview and retry to finish cleanup."}) from exc
