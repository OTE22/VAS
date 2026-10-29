"""Shared vehicle validation, immutable image storage and worker client."""
import asyncio
from io import BytesIO
import math
import os
from pathlib import Path
import unicodedata
import uuid
import warnings

import httpx
from PIL import Image, ImageOps
from config import settings

VEHICLE_CLASSES = frozenset({'car', 'truck', 'bus', 'motorcycle'})
EMBEDDING_VERSION = 'yolov8n-layer21-256-v1'
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def plate_key(value):
    value = unicodedata.normalize('NFKC', value or '').upper()
    return ''.join(str(unicodedata.decimal(c)) if c.isdecimal() else c
                   for c in value if c.isalnum())


def image_from_bytes(payload):
    if not payload or len(payload) > MAX_IMAGE_BYTES:
        raise ValueError('Provide a JPEG or PNG image up to 20 MB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as source:
                if source.format not in ('JPEG', 'PNG') or source.width * source.height > 40_000_000:
                    raise ValueError('Unsupported image or image exceeds 40 megapixels.')
                if min(source.size) < 8:
                    raise ValueError('Image is too small.')
                return ImageOps.exif_transpose(source).convert('RGB')
    except (OSError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ValueError('Invalid or oversized image.') from exc


def bounded_bbox(box, width, height):
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError('Expected an xyxy bounding box.')
    values = [float(v) for v in box]
    if not all(math.isfinite(v) for v in values):
        raise ValueError('Bounding box must be finite.')
    x1, y1, x2, y2 = values
    result = (max(0, int(x1)), max(0, int(y1)), min(width, math.ceil(x2)), min(height, math.ceil(y2)))
    if result[2]-result[0] < 8 or result[3]-result[1] < 8:
        raise ValueError('Vehicle bounding box is outside the image or too small.')
    return result


def root():
    return Path(settings.STORAGE_DIR) / 'vehicles'


def image_path(relative):
    base = root().resolve()
    target = (base / relative).resolve()
    if not target.is_relative_to(base):
        raise ValueError('Invalid vehicle image path.')
    return target


def save_image(image, category):
    relative = f'{category}/{uuid.uuid4().hex}.jpg'
    path = image_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Each write gets a new name; edits never overwrite captured evidence.
    with path.open('xb') as output:
        image.save(output, 'JPEG', quality=95)
        output.flush()
        os.fsync(output.fileno())
    return relative


def worker_token():
    path = getattr(settings, 'VEHICLE_WORKER_TOKEN_FILE', '')
    if not path:
        raise RuntimeError('Vehicle processing is not configured.')
    token = Path(path).read_text().strip()
    if len(token) < 32:
        raise RuntimeError('Vehicle worker token is invalid.')
    return token


async def vision_request(operation, payload, profile='en'):
    async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=3), trust_env=False) as client:
        result = await client.post(
            settings.VEHICLE_VISION_URL.rstrip('/') + '/' + operation,
            params={'profile': profile}, content=payload,
            headers={'Authorization': 'Bearer ' + worker_token(), 'Content-Type': 'image/jpeg'})
        result.raise_for_status()
        data = result.json()
    vector = data.get('embedding')
    if not isinstance(vector, list) or len(vector) != 256 or not all(math.isfinite(float(x)) for x in vector):
        raise ValueError('Vehicle worker returned an invalid image descriptor.')
    if data.get('embedding_version') != EMBEDDING_VERSION:
        raise ValueError('Vehicle image descriptor versions do not match.')
    return data


def jpeg_bytes(image, max_side=1280):
    copy = image.copy()
    copy.thumbnail((max_side, max_side))
    output = BytesIO()
    copy.save(output, 'JPEG', quality=95)
    return output.getvalue()
