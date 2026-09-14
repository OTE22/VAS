"""Atomic, local durable queue for processed detection evidence (no image copies)."""
import json
import os
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path

from config import settings


def root():
    return Path(settings.STORAGE_DIR) / '.detection-queue'


def _encode(value):
    if isinstance(value, datetime):
        return {'__datetime__': value.isoformat()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, 'item'):
        return value.item()
    raise TypeError(f'Unsupported detection value: {type(value).__name__}')


def _decode(value):
    if set(value) == {'__datetime__'}:
        return datetime.fromisoformat(value['__datetime__'])
    return value


def enqueue(data):
    key = str(uuid.UUID(data['detection']['uuid']))
    directory = root()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f'{key}.json'
    if target.exists():
        return
    temp = directory / f'{key}.{uuid.uuid4()}.part'
    try:
        with temp.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, default=_encode, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def pending(limit=25):
    # Bound each flush; sorting prevents a repeatedly failing item starving others.
    paths = sorted(root().glob('*.json'), key=lambda p: p.stat().st_mtime)
    result = []
    for path in paths:
        if path.name.endswith('.failed.json') or path.is_symlink():
            continue
        with path.open(encoding='utf-8') as stream:
            result.append((path, json.load(stream, object_hook=_decode)))
        if len(result) >= limit:
            break
    return result


def protected_embedding_ids():
    # Fail closed: a corrupt queue must not make retention delete its evidence.
    ids = set()
    for _, data in pending(limit=2**63):
        for face in data.get('faces', []):
            ids.update(int(v) for v in [face.get('_embedding_id'),
                       *(face.get('_secondary_embedding_ids') or [])] if v is not None)
    return ids


def protected_paths():
    return {face['face_image_path'] for _, data in pending(limit=2**63)
            for face in data.get('faces', []) if face.get('face_image_path')}


def stats():
    files = list(root().glob('*.json'))
    failed = sum(p.name.endswith('.failed.json') for p in files)
    return {'pending_frames': len(files) - failed, 'quarantined_frames': failed}


def acknowledge(path):
    path.unlink(missing_ok=True)


def quarantine(path, error):
    # Preserve invalid work for diagnosis, without endlessly replaying it.
    os.replace(path, path.with_suffix('.failed.json'))
    path.with_suffix('.error.txt').write_text(str(error)[:1000], encoding='utf-8')
