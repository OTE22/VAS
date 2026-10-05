"""Storage for new camera crops; existing database paths remain authoritative.

The capture UUID identifies an incoming frame (the integer detection row is
allocated later). Each face filename uses its existing event UUID. Pipeline
IDs that are not UUIDs map to a deterministic UUID without trusting path text.
"""
import asyncio
import os
from datetime import datetime
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid5

import cv2
from sqlalchemy import select, tuple_

from config import settings
from db_models import Face, Detection, IdentityAppearance


def portrait_from_frame(frame, face_bbox):
    """Natural, padded portrait for viewing; never an input to recognition.

    Coordinates are in the original frame. Preserve source pixels up to a
    640px long edge, never upscale, rotate, synthesize detail or pad with black.
    The bound keeps camera storage and live payloads predictable.
    """
    import math
    if frame is None or frame.ndim != 3 or len(face_bbox) != 4:
        raise ValueError('Invalid portrait source')
    x1, y1, x2, y2 = map(float, face_bbox)
    if not all(math.isfinite(v) for v in (x1, y1, x2, y2)) or x2 <= x1 or y2 <= y1:
        raise ValueError('Invalid face box')
    height, width = frame.shape[:2]
    if x2 <= 0 or y2 <= 0 or x1 >= width or y1 >= height:
        raise ValueError('Face outside source frame')
    pad_x, pad_y = (x2 - x1) * .2, (y2 - y1) * .2
    left, top = max(0, math.floor(x1 - pad_x)), max(0, math.floor(y1 - pad_y))
    right, bottom = min(width, math.ceil(x2 + pad_x)), min(height, math.ceil(y2 + pad_y))
    portrait = frame[top:bottom, left:right]
    if not portrait.size:
        raise ValueError('Empty portrait')
    longest = max(portrait.shape[:2])
    if longest > 640:
        scale = 640 / longest
        return cv2.resize(portrait, (max(1, round(portrait.shape[1] * scale)),
                                    max(1, round(portrait.shape[0] * scale))),
                          interpolation=cv2.INTER_AREA)
    return portrait.copy()


def camera_key(pipeline_id):
    try:
        return str(UUID(str(pipeline_id)))
    except ValueError:
        return str(uuid5(NAMESPACE_URL, 'face-detector:camera:' + str(pipeline_id)))


def local_path(value):
    """Accept historical absolute paths and storage-relative URLs, within root."""
    root = Path(settings.STORAGE_DIR).resolve()
    value = str(value).replace('\\', '/')
    if value.startswith(('/storage/', 'storage/')):
        value = value.split('storage/', 1)[1]
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root):
        raise ValueError('Storage path contains a symlink')
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError('Storage path is outside the storage directory')
    return resolved


def write_crop(image, pipeline_id, capture_id, face_id, captured_at: datetime):
    relative = Path('detections') / captured_at.strftime('%Y/%m/%d') / camera_key(pipeline_id)
    relative /= str(UUID(str(capture_id)))
    destination = local_path(relative / (str(UUID(str(face_id))) + '.jpg'))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.part.jpg')
    try:
        ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise OSError('Face JPEG encoding failed')
        with temporary.open('xb') as output:
            output.write(encoded.tobytes())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return str(destination)


def existing_paths(values):
    found = set()
    for value in values:
        try:
            path = local_path(value)
            if path.is_file():
                found.add(str(path))
        except (ValueError, OSError):
            continue
    return found


async def stored_camera_photos(db, identity_id, pipeline_id, name):
    """Count live files across both layouts, preserving the camera photo cap.

    Legacy untracked files also counted in the old implementation. Include
    that folder for compatibility, but never use the name for new placement.
    """
    values = []
    if identity_id:
        values = list((await db.execute(select(Face.face_image_path)
            .join(Detection, Face.detection_id == Detection.id)
            .where(Face.identity_id == identity_id, Detection.pipeline_id == pipeline_id,
                   Face.face_image_path.isnot(None)))).scalars().all())
        values.extend((await db.execute(select(IdentityAppearance.best_snapshot_path).where(
            IdentityAppearance.identity_id == identity_id, IdentityAppearance.pipeline_id == pipeline_id,
            IdentityAppearance.best_snapshot_path.isnot(None)))).scalars().all())
    def collect():
        safe_name = ''.join(c for c in name if c.isalnum() or c in ('-', '_')).lower()
        try:
            legacy = local_path(Path(str(pipeline_id)) / safe_name)
            values.extend(str(p) for p in legacy.glob('*') if p.suffix.lower() in ('.jpg', '.jpeg', '.png'))
        except (ValueError, OSError):
            pass
        gallery = Path(settings.FACES_DIR).resolve()
        return {p for p in existing_paths(values) if not Path(p).is_relative_to(gallery)}
    return await asyncio.to_thread(collect)


async def camera_photo_fallbacks(db, pairs):
    """Batch-load saved camera portraits for capped events without their own file."""
    if not pairs:
        return {}
    rows = (await db.execute(select(Face.identity_id, Detection.pipeline_id, Face.face_image_path)
        .join(Detection, Face.detection_id == Detection.id)
        .where(tuple_(Face.identity_id, Detection.pipeline_id).in_(list(pairs)),
               Face.face_image_path.isnot(None))
        .order_by(Detection.timestamp.desc(), Face.id.desc()))).all()
    # Appearance thumbnails may outlive the originating detection record.
    rows.extend((await db.execute(select(IdentityAppearance.identity_id,
        IdentityAppearance.pipeline_id, IdentityAppearance.best_snapshot_path)
        .where(tuple_(IdentityAppearance.identity_id, IdentityAppearance.pipeline_id).in_(list(pairs)),
               IdentityAppearance.best_snapshot_path.isnot(None))
        .order_by(IdentityAppearance.start_time.desc()))).all())
    def collect():
        found = {}
        for identity_id, pipeline_id, value in rows:
            key = (identity_id, pipeline_id)
            if key not in found:
                paths = existing_paths([value])
                if paths:
                    found[key] = next(iter(paths))
        return found
    return await asyncio.to_thread(collect)


async def save_camera_crop(image, *, pipeline_id, capture_id, face_id, captured_at,
                           identity_id, name, session_factory, already_saved=0, executor=None,
                           similarity=None):
    """Apply existing save settings before writing a crop in the new layout."""
    is_unknown = name.lower() == 'unknown'
    if not settings.SAVE_IMAGES or (is_unknown and not settings.SAVE_UNKNOWN_FACES):
        return None
    if not is_unknown:
        maximum = settings.MAX_PHOTOS_PER_PERSON
        async with session_factory() as db:
            count = (len(await stored_camera_photos(db, identity_id, pipeline_id, name))
                     if maximum > already_saved else 0)
            if count + already_saved >= maximum:
                # A gallery photo limit must not discard a new alert's evidence.
                # Reuse alert eligibility (camera, score, cooldown and schedule)
                # so ordinary sightings still obey the existing photo cap.
                capture_alert = False
                if settings.LIVE_ALERTS_ENABLED and identity_id and similarity is not None:
                    from backend.core.live_alert_service import live_alert_service
                    alerts = await live_alert_service.get_active_alerts_for_identity(db, str(identity_id))
                    now = datetime.utcnow()
                    for alert in alerts:
                        if alert.auto_capture_snapshot and await live_alert_service._should_alert_trigger(
                            alert, similarity, pipeline_id, now.time(), now.weekday(), now
                        ):
                            capture_alert = True
                            break
                if not capture_alert:
                    return None
    return await asyncio.get_running_loop().run_in_executor(
        executor, write_crop, image, pipeline_id, capture_id, face_id, captured_at)
