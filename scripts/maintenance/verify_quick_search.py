"""Exercise Quick Search without enrolling/merging identities. Normal search audits are retained."""
import asyncio
import json
import sys
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from config import settings
from db_models import User, SearchHistory
from backend.auth.auth_service import AuthService


async def main():
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with AsyncSession(engine) as db:
            admin = (await db.execute(select(User).where(User.role == 'admin', User.is_active.is_(True)).limit(1))).scalar_one()
            headers = {'Authorization': 'Bearer ' + AuthService.create_access_token({'sub':str(admin.id)})}
            async with httpx.AsyncClient(base_url='http://localhost:8000', timeout=45) as client:
                denied = await client.post('/api/search/by-image', files={'image':('invalid.jpg',b'bad','image/jpeg')})
                assert denied.status_code in (401,403)
                for fields in [{'scope':'invalid'}, {'top_k':'100000'}, {'date_from':'bad'}]:
                    result = await client.post('/api/search/by-image', headers=headers, data=fields,
                        files={'image':('invalid.jpg',b'bad','image/jpeg')})
                    assert result.status_code == 422, result.status_code
                ordinary = (await db.execute(select(User).where(User.role.in_(['viewer','observer','user']),User.is_active.is_(True)).limit(1))).scalar_one_or_none()
                if ordinary:
                    result = await client.post('/api/search/by-image',
                        headers={'Authorization':'Bearer '+AuthService.create_access_token({'sub':str(ordinary.id)})},
                        files={'image':('invalid.jpg',b'bad','image/jpeg')})
                    assert result.status_code == 403, result.status_code
                searches=[]
                for filename in ['face_a.jpg','two_faces.jpg']:
                    path=Path('/app/tests/fixtures/faces') / filename
                    result=await client.post('/api/search/by-image',headers=headers,data={'scope':'both','top_k':'10'},
                        files={'image':(filename,path.read_bytes(),'image/jpeg')})
                    result.raise_for_status()
                    rows=result.json()
                    assert isinstance(rows,list) and len(rows)<=10
                    assert len({row['identity_id'] for row in rows})==len(rows)
                    count=int(result.headers['X-Faces-Detected'])
                    assert count >= (2 if filename=='two_faces.jpg' else 1)
                    history=await db.get(SearchHistory,uuid.UUID(result.headers['X-Search-Id']))
                    assert history is not None and history.results_count==len(rows)
                    searches.append({'faces_detected':count,'distinct_results':len(rows),'audit_verified':True})
                print(json.dumps({'anonymous_blocked':True,'non_admin_checked':ordinary is not None,
                                  'invalid_parameters_blocked':True,'searches':searches}))
    finally:
        await engine.dispose()


asyncio.run(main())
