"""Read-only live verification. Tokens stay in memory and are never printed."""
import asyncio
import inspect
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
import websockets
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from config import settings
from db_models import User, IdentityAppearance
from backend.auth.auth_service import AuthService


async def main():
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with AsyncSession(engine) as db:
            user = (await db.execute(select(User).where(User.role == 'admin', User.is_active.is_(True)).limit(1))).scalar_one()
            token = AuthService.create_access_token({'sub': str(user.id)})
            headers = {'Authorization': 'Bearer ' + token}
            async with httpx.AsyncClient(base_url='http://localhost:8000', headers=headers, timeout=45) as client:
                health = await client.get('/health/detailed')
                health.raise_for_status()
                state = health.json()
                services = state['components']['background_services']['services']
                print(json.dumps({'api_healthy': state['healthy'], 'services': {
                    name: {'status': value['status'], 'error': value['last_error']}
                    for name, value in services.items()}}))
                response = await client.get('/api/admin/unknown', params={'show_all':'true', 'page_size':20})
                response.raise_for_status()
                data = response.json()
                checked = 0
                for identity in data['identities']:
                    for pid, event in identity['camera_events'].items():
                        row = await db.get(IdentityAppearance, event['appearance_id'])
                        assert str(row.identity_id) == identity['id'] == event['identity_id']
                        assert row.pipeline_id == pid == event['pipeline_id']
                        assert event['timestamp'] == row.start_time.isoformat() + 'Z'
                        assert event['event_id'] == (row.event_id or f'appearance:{row.id}')
                        checked += 1
                known = await client.get('/api/admin/known-faces')
                known.raise_for_status()
                print(json.dumps({'http_camera_events_verified': checked, 'known_faces_http': known.status_code}))
                suggestions = await client.get('/api/admin/merge-suggestions')
                suggestions.raise_for_status()
                from urllib.parse import quote
                cameras = sorted({pid for person in data['identities'] for pid in person['camera_events']})[:2]
                counts = []
                for camera in cameras:
                    result = await client.get('/api/admin/merge-suggestions/pipeline/' + quote(camera, safe=''))
                    result.raise_for_status()
                    groups = result.json()
                    assert all(len(set(group['identity_ids'])) >= 2 for group in groups)
                    counts.append(len(groups))
                print(json.dumps({'stored_suggestions_http': suggestions.status_code,
                                  'camera_suggestion_endpoints_verified': len(cameras), 'group_counts': counts}))
            connect_options = {'additional_headers' if 'additional_headers' in inspect.signature(websockets.connect).parameters else 'extra_headers':
                               {'Cookie':'access_token=' + token}}
            async with websockets.connect('ws://localhost:8000/ws?view=unknown', **connect_options) as ws:
                for _ in range(12):
                    message = json.loads(await asyncio.wait_for(ws.recv(), 45))
                    if message.get('type') == 'initial_unknown_data':
                        events = [e for group in message['data'] for e in group['faces']]
                        assert all(e.get('event_id') and e.get('timestamp') and e.get('pipeline_id') for e in events)
                        print(json.dumps({'websocket_individual_events_verified':len(events)}))
                        break
                else:
                    raise AssertionError('No initial unknown event payload received')
    finally:
        await engine.dispose()


asyncio.run(main())
