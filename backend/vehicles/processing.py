"""Durable, leased vehicle jobs. Face capture never waits for this worker."""
import asyncio
import base64
from datetime import datetime, timedelta
import logging
from sqlalchemy import and_, or_, select, update
from config import settings
from db_connection import db_manager
from db_models import VehicleSighting
from backend.core.service_supervisor import supervised_loop
from .common import image_path, image_from_bytes, save_image, jpeg_bytes, vision_request, plate_key

log = logging.getLogger(__name__)


async def process_one():
    now = datetime.utcnow()
    async with db_manager.get_session() as db:
        await db.execute(update(VehicleSighting).where(
            VehicleSighting.processing_state == 'processing', VehicleSighting.attempts >= 5,
            VehicleSighting.lease_until < now).values(processing_state='failed', lease_until=None,
                                                     error_code='WorkerLeaseExpired'))
        sighting = await db.scalar(select(VehicleSighting).where(
            VehicleSighting.attempts < 5,
            or_(and_(VehicleSighting.processing_state == 'pending', VehicleSighting.next_attempt_at <= now),
                and_(VehicleSighting.processing_state == 'processing', VehicleSighting.lease_until < now)))
            .order_by(VehicleSighting.received_at).with_for_update(skip_locked=True).limit(1))
        if not sighting:
            return
        sighting.processing_state = 'processing'
        sighting.attempts += 1
        sighting.lease_until = now + timedelta(seconds=120)
        job = dict(id=sighting.id, path=sighting.vehicle_path, profile=sighting.ocr_profile,
                   lease=sighting.lease_until, attempts=sighting.attempts)
    try:
        def load():
            image = image_from_bytes(image_path(job['path']).read_bytes())
            return jpeg_bytes(image)
        payload = await asyncio.to_thread(load)
        result = await vision_request('analyze', payload, job['profile'])
        stored_plates = []
        for candidate in result.get('plates', [])[:3]:
            data = base64.b64decode(candidate.pop('crop_b64'), validate=True)
            image = await asyncio.to_thread(image_from_bytes, data)
            candidate['image_path'] = await asyncio.to_thread(save_image, image, 'plates')
            stored_plates.append(candidate)
        async with db_manager.get_session() as db:
            sighting = await db.scalar(select(VehicleSighting).where(VehicleSighting.id == job['id']).with_for_update())
            if not sighting or sighting.lease_until != job['lease']:
                return
            sighting.embedding = result['embedding']
            sighting.embedding_version = result['embedding_version']
            sighting.ocr_result = {'plates': stored_plates, 'inference_ms': result.get('inference_ms')}
            if stored_plates:
                sighting.plate_path = stored_plates[0]['image_path']
                lines = stored_plates[0].get('ocr', {}).get('lines', [])
                if sighting.review_state == 'pending':
                    raw = ' '.join(str(line.get('text', '')) for line in lines).strip()[:64]
                    sighting.plate = raw or None
                    sighting.plate_key = plate_key(raw) or None
                    sighting.plate_score = sum(float(line.get('score', 0)) for line in lines)/len(lines) if lines else None
                    if not raw:
                        sighting.review_state = 'unreadable'
            elif sighting.review_state == 'pending':
                sighting.review_state = 'unreadable'
            sighting.processing_state = 'done'
            sighting.error_code = None
            sighting.lease_until = None
            sighting.version += 1
    except asyncio.CancelledError:
        # The durable lease expires; another process can safely retry.
        raise
    except Exception as exc:
        log.warning('vehicle_processing_retry id=%s type=%s attempt=%s', job['id'], type(exc).__name__, job['attempts'])
        async with db_manager.get_session() as db:
            sighting = await db.scalar(select(VehicleSighting).where(VehicleSighting.id == job['id']).with_for_update())
            if sighting and sighting.lease_until == job['lease']:
                sighting.processing_state = 'failed' if job['attempts'] >= 5 else 'pending'
                sighting.error_code = type(exc).__name__[:80]
                sighting.next_attempt_at = datetime.utcnow() + timedelta(seconds=min(60, 2**job['attempts']))
                sighting.lease_until = None


async def run():
    await supervised_loop('vehicle_processing', 1.0, process_one)
