"""Durably accept vehicle events independently of the face-processing queue."""
import asyncio
import base64
import hashlib
import math
import uuid
from datetime import datetime
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from fastapi import HTTPException
from db_connection import db_manager
from db_models import VehiclePipeline, VehicleSighting
from backend.core.event_time import observation_time
from utils.helpers import extract_images_from_payload
from .common import VEHICLE_CLASSES, image_from_bytes, bounded_bbox, save_image


def predictions(payload):
    return payload.get('predictions') or (payload.get('results') or {}).get('predictions') or []


def vehicle_event(payload):
    return any(isinstance(p, dict) and str(p.get('class_name', '')).lower() in VEHICLE_CLASSES
               for p in predictions(payload))


def only_vehicles(payload):
    values = predictions(payload)
    return bool(values) and all(isinstance(p, dict) and str(p.get('class_name', '')).lower() in VEHICLE_CLASSES for p in values)


def _prepare(payload, profile, country, captured_at):
    images = extract_images_from_payload(payload)
    if not images or len(images) > 16:
        raise ValueError('Vehicle events require between 1 and 16 original images.')
    event = payload.get('event_id')
    if not isinstance(event, str) or not 1 <= len(event) <= 128:
        raise ValueError('Vehicle events require a stable event_id (1–128 characters).')
    candidates = predictions(payload)
    if not isinstance(candidates, list) or len(candidates) > 100 or not all(isinstance(d, dict) for d in candidates):
        raise ValueError('Too many vehicle detections in one event.')
    decoded = {}
    rows = []
    for i, det in enumerate(candidates):
        if str(det.get('class_name', '')).lower() not in VEHICLE_CLASSES:
            continue
        index = det.get('image_index', det.get('frame_index', 0))
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(images):
            raise ValueError('Invalid vehicle image index.')
        if index not in decoded:
            encoded = images[index]
            if ',' in encoded and encoded.startswith('data:'):
                encoded = encoded.split(',', 1)[1]
            image = image_from_bytes(base64.b64decode(encoded, validate=True))
            decoded[index] = (image, save_image(image, 'scenes'))
        image, scene = decoded[index]
        box = det.get('bbox')
        if det.get('bbox_format', 'xyxy') != 'xyxy':
            raise ValueError('Vehicle detections require bbox_format=xyxy.')
        box = bounded_bbox(box, *image.size)
        score = float(det.get('confidence', 0))
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError('Invalid detection confidence.')
        crop = image.crop(box)
        rows.append(dict(id=uuid.uuid4(), event_id=event, detection_index=i,
                         captured_at=captured_at, vehicle_type=str(det['class_name']).lower(),
                         track_id=str(det['track_id'])[:128] if det.get('track_id') is not None else None,
                         bbox=list(box), detection_score=score, scene_path=scene,
                         vehicle_path=save_image(crop, 'sightings'), country=country,
                         ocr_profile=profile, next_attempt_at=datetime.utcnow()))
    return rows


async def accept_vehicle_event(pipeline_id, payload):
    async with db_manager.get_session() as db:
        config = await db.get(VehiclePipeline, pipeline_id)
        if config is None or not config.enabled:
            return {'status': 'vehicle_disabled', 'queued': 0}
        event = payload.get('event_id')
        # The unique constraint remains authoritative under concurrent delivery.
        if isinstance(event, str):
            await db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                             {'key': 'vehicle:' + pipeline_id + ':' + event})
            exists = await db.scalar(select(VehicleSighting.id).where(
                VehicleSighting.pipeline_id == pipeline_id, VehicleSighting.event_id == event).limit(1))
            if exists:
                return {'status': 'duplicate', 'queued': 0}
        try:
            captured, _ = observation_time(payload.get('captured_at'), datetime.utcnow())
            rows = await asyncio.to_thread(_prepare, payload, config.ocr_profile, config.country, captured)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, 'Invalid vehicle event: ' + str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, 'Vehicle evidence storage is temporarily unavailable.', headers={'Retry-After':'5'}) from exc
        accepted = 0
        for row in rows:
            statement = insert(VehicleSighting).values(pipeline_id=pipeline_id, **row).on_conflict_do_nothing(
                constraint='uq_vehicle_event_detection').returning(VehicleSighting.id)
            accepted += (await db.scalar(statement)) is not None
        # get_session commits before this function returns, before HTTP 202.
    return {'status': 'vehicle_queued', 'queued': accepted}
