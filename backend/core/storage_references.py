"""Reference checks for scheduled cleanup of camera images in either layout."""
import asyncio
from pathlib import Path
from sqlalchemy import select, String, cast, or_, func

from config import settings
import db_models as m
from backend.core.detection_storage import local_path


async def unreferenced_files(db, values, excluding=None):
    """Return canonical paths with no surviving DB reference.

    Exclusions are predicates for records the caller is removing/clearing.
    Enrollment files are never candidates for scheduled camera cleanup.
    Invalid paths fail closed. JSON provenance protects merge/unmerge and
    search-history links as well as the normal typed path columns.
    """
    excluding = excluding or {}
    candidates = {}
    root = Path(settings.STORAGE_DIR).resolve()
    gallery = Path(settings.FACES_DIR).resolve()
    for value in set(v for v in values if v):
        try:
            path = local_path(value)
        except (ValueError, OSError):
            continue
        if path.is_relative_to(gallery):
            continue
        relative = path.relative_to(root).as_posix()
        for variant in (str(path), path.as_posix(), relative, 'storage/' + relative, '/storage/' + relative):
            candidates[variant] = path
    protected = set()
    if candidates:
        from backend.core.detection_spool import protected_paths
        for value in await asyncio.to_thread(protected_paths):
            try:
                protected.add(local_path(value))
            except (ValueError, OSError):
                continue
    variants = list(candidates)
    tables = m.Base.metadata.tables
    for table in tables.values():
        columns = [c for c in table.c if c.name.endswith('_path') and isinstance(c.type, String)]
        for column in columns:
            for offset in range(0, len(variants), 500):
                statement = select(column).where(column.in_(variants[offset:offset + 500]))
                if table.name in excluding:
                    # NULLs outside an excluded predicate are surviving rows.
                    statement = statement.where(func.coalesce(excluding[table.name], False).is_(False))
                protected.update(candidates[v] for v in (await db.execute(statement)).scalars().all())
    for name, column_name in [('identity_merges', 'provenance'), ('search_history', 'results_summary')]:
        column = tables[name].c[column_name]
        for path in set(candidates.values()) - protected:
            alternatives = [v for v, p in candidates.items() if p == path]
            predicate = or_(*(cast(column, String).contains(v, autoescape=True) for v in alternatives))
            if (await db.execute(select(tables[name].c.id).where(predicate).limit(1))).first():
                protected.add(path)
    return sorted(str(p) for p in set(candidates.values()) - protected)


async def retire_image_references(db, records, field="best_snapshot_path"):
    """Expire one batch of image references, preserving every database row.

    The batch must contain one table/field. Enrollment files and invalid paths
    are left untouched. References from other tables (including evidence) keep
    the physical file; only this batch's expired references are cleared.
    """
    eligible_records = []
    for record in records:
        value = getattr(record, field)
        if not value:
            continue
        try:
            path = local_path(value)
        except (ValueError, OSError):
            continue
        if path.is_relative_to(Path(settings.FACES_DIR).resolve()):
            continue
        eligible_records.append(record)
    if not eligible_records:
        return 0
    table = eligible_records[0].__table__
    paths = await unreferenced_files(db, [getattr(r, field) for r in eligible_records], {
        table.name: table.c.id.in_([r.id for r in eligible_records]),
    })
    deleted = 0
    for value in paths:
        try:
            await asyncio.to_thread(Path(value).unlink)
            deleted += 1
        except FileNotFoundError:
            pass
    for record in eligible_records:
        setattr(record, field, None)
    await db.flush()
    return deleted


async def retire_snapshot(db, record):
    """Compatibility entry point for a single expired snapshot."""
    return await retire_image_references(db, [record])
