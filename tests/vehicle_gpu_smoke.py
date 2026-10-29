"""Opt-in GPU integration against vehicle_tests only; never production data."""
import asyncio
import base64
from datetime import datetime, timezone
from io import BytesIO
import os
import uuid

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from config import settings
from db_connection import db_manager
from db_models import VehicleSighting
from backend.vehicles.ingest import accept_vehicle_event
from backend.vehicles.processing import process_one


async def main():
    host = os.environ.get('VEHICLE_TEST_DB_HOST', '127.0.0.1')
    assert host in ('127.0.0.1', 'vas-vehicles-test-db')
    db_manager.engine = create_async_engine(
        f'postgresql+asyncpg://postgres:vehicle-tests-only@{host}:5432/vehicle_tests')
    db_manager.session_maker = async_sessionmaker(db_manager.engine, expire_on_commit=False)
    settings.STORAGE_DIR = '/tmp/vehicle-gpu-smoke'
    settings.VEHICLE_WORKER_TOKEN_FILE = '/run/secrets/vehicle_worker_token'
    settings.VEHICLE_VISION_URL = 'http://vehicle-vision:8092'
    async with db_manager.get_session() as db:
        assert await db.scalar(text('SELECT current_database()')) == 'vehicle_tests'
    image = Image.new('RGB', (800, 400), '#253140')
    draw = ImageDraw.Draw(image)
    draw.rectangle((170, 220, 630, 335), fill='white', outline='black', width=4)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 70)
    draw.text((205, 230), 'B 123456', fill='black', font=font)
    stream = BytesIO(); image.save(stream, 'JPEG', quality=95)
    event = uuid.uuid4().hex
    payload = {'event_id': event, 'captured_at': datetime.now(timezone.utc).isoformat(),
               'image': base64.b64encode(stream.getvalue()).decode(),
               'results': {'predictions': [{'class_name': 'car', 'confidence': .99,
                                            'bbox': [0, 0, 800, 400]}]}}
    result = await accept_vehicle_event('4c79dc55-8ef0-4cbe-bbd6-dcd01d4d755b', payload)
    assert result['queued'] == 1, result
    await process_one()
    from sqlalchemy import select
    async with db_manager.get_session() as db:
        sighting = await db.scalar(select(VehicleSighting).where(VehicleSighting.event_id == event))
        assert sighting.processing_state == 'done', (sighting.processing_state, sighting.error_code)
        assert len(sighting.embedding) == 256
        assert sighting.plate_key == 'B123456', sighting.plate
        assert sighting.review_state == 'pending' and sighting.known_vehicle_id is None
        print('PASS: durable event → LPR.pt GPU → PaddleOCR GPU → retained plate crop, B123456, 256D descriptor, operator review required.')
        print('Synthetic workflow test; not a real-camera accuracy benchmark.')
    await db_manager.engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
