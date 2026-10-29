"""Admin vehicle investigation workspace. Every decision is explicit and audited."""
import asyncio
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, func, or_, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from config import settings
from db_connection import get_db
from db_models import (KnownVehicle, VehicleSighting, VehiclePipeline, VehiclePersonLink,
                       VehicleAudit, Pipeline, Identity, IdentityType, IdentityStatus)
from backend.auth.auth_service import require_admin
from backend.vehicles.common import (plate_key, image_from_bytes, save_image, image_path,
                                     jpeg_bytes, vision_request, MAX_IMAGE_BYTES, EMBEDDING_VERSION)
from backend.vehicles.export import workbook


def csrf(request: Request):
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and not request.headers.get('authorization'):
        if request.headers.get('x-requested-with', '').lower() != 'xmlhttprequest':
            raise HTTPException(403, 'CSRF header required.')


admin = require_admin()
router = APIRouter(prefix='/api/vehicles', tags=['Vehicles'], dependencies=[Depends(admin), Depends(csrf)])


def audit(db, user, kind, identity, action, details):
    db.add(VehicleAudit(entity_type=kind, entity_id=str(identity), action=action,
                        actor_id=user['id'], details=details))


def utc(value):
    return value.isoformat() + 'Z' if value else None


def known_json(v):
    return {key: getattr(v, key) for key in ('name', 'plate', 'country', 'make', 'model', 'color', 'year', 'notes', 'archived', 'version')} | {
        'id': str(v.id), 'photo_url': f'/api/vehicles/known/{v.id}/photo' if v.photo_path else None,
        'created_at': utc(v.created_at), 'updated_at': utc(v.updated_at)}


def sighting_json(s, name=None, similarity=None):
    result = {key: getattr(s, key) for key in ('pipeline_id', 'vehicle_type', 'track_id', 'country', 'plate', 'plate_score',
              'review_state', 'processing_state', 'error_code', 'attempts', 'version', 'ocr_profile')}
    result.update(id=str(s.id), captured_at=utc(s.captured_at), received_at=utc(s.received_at),
                  known_vehicle_id=str(s.known_vehicle_id) if s.known_vehicle_id else None,
                  known_vehicle_name=name, image_url=f'/api/vehicles/sightings/{s.id}/image/vehicle',
                  scene_url=f'/api/vehicles/sightings/{s.id}/image/scene',
                  plate_url=f'/api/vehicles/sightings/{s.id}/image/plate' if s.plate_path else None)
    if similarity is not None:
        result['visual_similarity'] = round(float(similarity), 4)
        result['match_type'] = 'possible_visual_match'
    return result


class KnownWrite(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    plate: str = Field(min_length=1, max_length=64)
    country: str = Field(pattern=r'^[A-Za-z]{2,8}$')
    make: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=80)
    color: str | None = Field(default=None, max_length=60)
    year: int | None = Field(default=None, ge=1886, le=2100)
    notes: str | None = Field(default=None, max_length=5000)
    archived: bool = False
    version: int | None = Field(default=None, ge=1)

    @field_validator('name', 'plate')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Value cannot be blank.')
        return value.strip()


async def locked_known(db, identity, version=None):
    record = await db.scalar(select(KnownVehicle).where(KnownVehicle.id == identity).with_for_update())
    if not record:
        raise HTTPException(404, 'Known car not found.')
    if version is not None and record.version != version:
        raise HTTPException(409, 'This record changed. Reload it before saving.')
    return record


@router.get('/known')
async def known_list(q: str = Query('', max_length=160), page: int = Query(1, ge=1),
                     page_size: int = Query(24, ge=1, le=100), archived: bool = False, db: AsyncSession = Depends(get_db)):
    filters = [KnownVehicle.archived == archived]
    if q.strip():
        filters.append(or_(KnownVehicle.name.icontains(q.strip(), autoescape=True),
                           KnownVehicle.plate_key.contains(plate_key(q), autoescape=True)))
    total = await db.scalar(select(func.count()).select_from(KnownVehicle).where(*filters))
    rows = (await db.scalars(select(KnownVehicle).where(*filters).order_by(KnownVehicle.updated_at.desc(), KnownVehicle.id)
                            .offset((page-1)*page_size).limit(page_size))).all()
    return {'items': [known_json(v) for v in rows], 'total': total, 'page': page, 'page_size': page_size}


@router.post('/known', status_code=201)
async def known_create(body: KnownWrite, db: AsyncSession = Depends(get_db), user=Depends(admin)):
    values = body.model_dump(exclude={'version'})
    values['country'] = body.country.upper()
    values['plate_key'] = plate_key(body.plate)
    if not values['plate_key']:
        raise HTTPException(422, 'Plate must contain letters or numbers.')
    record = KnownVehicle(**values)
    db.add(record)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(409, 'That country and plate already exist in Known Cars.') from exc
    audit(db, user, 'known_vehicle', record.id, 'created', known_json(record))
    return known_json(record)


@router.put('/known/{identity}')
async def known_update(identity: UUID, body: KnownWrite, db: AsyncSession = Depends(get_db), user=Depends(admin)):
    if body.version is None:
        raise HTTPException(422, 'Record version is required.')
    record = await locked_known(db, identity, body.version)
    before = known_json(record)
    for key, value in body.model_dump(exclude={'version'}).items():
        setattr(record, key, value)
    record.country = body.country.upper()
    record.plate_key = plate_key(body.plate)
    if not record.plate_key:
        raise HTTPException(422, 'Plate must contain letters or numbers.')
    record.updated_at = datetime.utcnow()
    record.version += 1
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(409, 'That country and plate already exist in Known Cars.') from exc
    audit(db, user, 'known_vehicle', record.id, 'updated', {'before': before, 'after': known_json(record)})
    return known_json(record)


@router.get('/known/{identity}')
async def known_detail(identity: UUID, db: AsyncSession = Depends(get_db)):
    record = await db.get(KnownVehicle, identity)
    if not record:
        raise HTTPException(404, 'Known car not found.')
    links = (await db.execute(select(VehiclePersonLink, func.coalesce(Identity.display_name, VehiclePersonLink.identity_name)).outerjoin(Identity, Identity.id == VehiclePersonLink.identity_id)
                             .where(VehiclePersonLink.vehicle_id == identity).order_by(VehiclePersonLink.created_at.desc()))).all()
    return known_json(record) | {'people': [{'id': str(link.id), 'identity_id': str(link.identity_id) if link.identity_id else None, 'name': name,
            'relationship_type': link.relationship_type, 'evidence': link.evidence, 'active': bool(link.active and link.identity_id),
            'created_at': utc(link.created_at)} for link, name in links]}


@router.post('/known/{identity}/photo')
async def known_photo(identity: UUID, version: int = Form(...), photo: UploadFile = File(...),
                      db: AsyncSession = Depends(get_db), user=Depends(admin)):
    record = await locked_known(db, identity, version)
    try:
        image = await asyncio.to_thread(image_from_bytes, await photo.read(MAX_IMAGE_BYTES+1))
    except (ValueError, OSError) as exc:
        raise HTTPException(422, 'Provide a valid JPEG/PNG image up to 20 MB.') from exc
    old = record.photo_path
    record.photo_path = await asyncio.to_thread(save_image, image, 'known')
    record.version += 1
    record.updated_at = datetime.utcnow()
    audit(db, user, 'known_vehicle', identity, 'photo_updated', {'before': old, 'after': record.photo_path})
    return known_json(record)


@router.get('/known/{identity}/photo')
async def get_photo(identity: UUID, db: AsyncSession = Depends(get_db)):
    record = await db.get(KnownVehicle, identity)
    if not record or not record.photo_path or not image_path(record.photo_path).is_file():
        raise HTTPException(404, 'Reference photo unavailable.')
    return FileResponse(image_path(record.photo_path), media_type='image/jpeg', headers={'Cache-Control': 'private, no-cache'})


@router.get('/people')
async def people(q: str = Query(..., min_length=2, max_length=100), db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(Identity).where(Identity.type == IdentityType.KNOWN,
            Identity.status == IdentityStatus.ACTIVE, Identity.display_name.icontains(q, autoescape=True))
            .order_by(Identity.display_name).limit(20))).all()
    return {'items': [{'id': str(v.id), 'name': v.display_name, 'person_code': v.person_code} for v in rows]}


class PersonLinkWrite(BaseModel):
    identity_id: UUID
    relationship_type: Literal['registered_owner', 'driver', 'associated']
    evidence: str = Field(min_length=3, max_length=2000)


@router.post('/known/{identity}/people', status_code=201)
async def link_person(identity: UUID, body: PersonLinkWrite, db: AsyncSession = Depends(get_db), user=Depends(admin)):
    record = await locked_known(db, identity)
    person = await db.get(Identity, body.identity_id)
    if record.archived or not person or person.type != IdentityType.KNOWN or person.status != IdentityStatus.ACTIVE:
        raise HTTPException(422, 'Select an active Known Car and a known, active person.')
    if not body.evidence.strip():
        raise HTTPException(422, 'Explain the evidence for this association.')
    exists = await db.scalar(select(VehiclePersonLink.id).where(VehiclePersonLink.vehicle_id == identity,
                            VehiclePersonLink.identity_id == body.identity_id, VehiclePersonLink.active.is_(True)))
    if exists:
        raise HTTPException(409, 'This person is already linked. Revoke the existing link first to change it.')
    link = VehiclePersonLink(vehicle_id=identity, **body.model_dump(), reviewed_by=user['id'],
                             identity_name=person.display_name or str(person.id), historical_identity_id=person.id)
    db.add(link)
    await db.flush()
    audit(db, user, 'known_vehicle', identity, 'person_linked', {'link_id': str(link.id), **body.model_dump(mode='json')})
    return {'id': str(link.id)}


@router.post('/known/{identity}/people/{link_id}/revoke')
async def revoke_person(identity: UUID, link_id: UUID, db: AsyncSession = Depends(get_db), user=Depends(admin)):
    await locked_known(db, identity)
    link = await db.get(VehiclePersonLink, link_id)
    if not link or link.vehicle_id != identity:
        raise HTTPException(404, 'Association not found.')
    link.active = False
    audit(db, user, 'known_vehicle', identity, 'person_link_revoked', {'link_id': str(link.id)})
    return {'status': 'revoked'}


def query_filters(start: datetime | None = None, end: datetime | None = None,
                  pipeline_id: str | None = Query(None, max_length=255), plate: str = Query('', max_length=64),
                  known_vehicle_id: UUID | None = None,
                  review: Literal['pending','confirmed','rejected','unreadable'] | None = None,
                  processing: Literal['pending','processing','done','failed'] | None = None,
                  vehicle_type: Literal['car','truck','bus','motorcycle'] | None = None,
                  ids: list[UUID] | None = Query(None, max_length=200)):
    filters = []
    if ids is not None:
        filters.append(VehicleSighting.id.in_(ids))
    for value in (start, end):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise HTTPException(422, 'Date/time filters must include a timezone.')
    if start and end and start > end:
        raise HTTPException(422, 'From must precede To.')
    if start:
        filters.append(VehicleSighting.captured_at >= start.astimezone(timezone.utc).replace(tzinfo=None))
    if end:
        filters.append(VehicleSighting.captured_at <= end.astimezone(timezone.utc).replace(tzinfo=None))
    for field, value in ((VehicleSighting.pipeline_id,pipeline_id),(VehicleSighting.known_vehicle_id,known_vehicle_id),
                         (VehicleSighting.review_state,review),(VehicleSighting.processing_state,processing),
                         (VehicleSighting.vehicle_type,vehicle_type)):
        if value is not None:
            filters.append(field == value)
    if plate_key(plate):
        filters.append(VehicleSighting.plate_key.contains(plate_key(plate), autoescape=True))
    return filters


@router.get('/sightings')
async def sightings(filters=Depends(query_filters), page: int = Query(1, ge=1), page_size: int = Query(24, ge=1, le=100),
                    db: AsyncSession = Depends(get_db)):
    stats = (await db.execute(select(func.count(),
        func.count().filter(VehicleSighting.review_state == 'confirmed'),
        func.count().filter(VehicleSighting.review_state == 'pending'),
        func.count().filter(VehicleSighting.review_state == 'unreadable'),
        func.count().filter(VehicleSighting.processing_state == 'failed')).select_from(VehicleSighting).where(*filters))).one()
    rows = (await db.execute(select(VehicleSighting, KnownVehicle.name).outerjoin(KnownVehicle)
        .where(*filters).order_by(VehicleSighting.captured_at.desc(), VehicleSighting.id)
        .offset((page-1)*page_size).limit(page_size))).all()
    return {'items': [sighting_json(s,n) for s,n in rows], 'total': stats[0], 'page': page, 'page_size': page_size,
            'stats': dict(zip(('total','confirmed','pending','unreadable','failed'),stats))}


@router.get('/sightings/{identity}')
async def sighting_detail(identity: UUID, db: AsyncSession = Depends(get_db)):
    row = await db.get(VehicleSighting, identity)
    if not row:
        raise HTTPException(404, 'Sighting not found.')
    matches = []
    if row.plate_key:
        matches = (await db.scalars(select(KnownVehicle).where(KnownVehicle.country == row.country,
            KnownVehicle.plate_key == row.plate_key, KnownVehicle.archived.is_(False)))).all()
    candidates = [known_json(v) for v in matches if str(v.id) not in (row.rejected_vehicle_ids or [])]
    history = (await db.scalars(select(VehicleAudit).where(VehicleAudit.entity_type == 'sighting', VehicleAudit.entity_id == str(identity))
                               .order_by(VehicleAudit.created_at.desc()).limit(50))).all()
    raw = row.ocr_result or {}
    return sighting_json(row) | {'plate_candidates': [{'lines': p.get('ocr',{}).get('lines',[]),
            'detection_score': p.get('detection_score'), 'image_url': f'/api/vehicles/sightings/{identity}/image/plate?candidate={i}'}
            for i,p in enumerate(raw.get('plates',[]))], 'reference_candidates': candidates,
            'history': [{'action': h.action, 'created_at': utc(h.created_at), 'details': h.details} for h in history]}


@router.get('/sightings/{identity}/image/{kind}')
async def sighting_image(identity: UUID, kind: Literal['vehicle','scene','plate'], candidate: int = Query(0, ge=0, le=2),
                         db: AsyncSession = Depends(get_db)):
    row = await db.get(VehicleSighting, identity)
    if not row:
        raise HTTPException(404, 'Sighting not found.')
    path = getattr(row, kind+'_path')
    if kind == 'plate':
        items = (row.ocr_result or {}).get('plates',[])
        if candidate < len(items):
            path = items[candidate].get('image_path')
    if not path or not image_path(path).is_file():
        raise HTTPException(404, 'Image unavailable.')
    return FileResponse(image_path(path), media_type='image/jpeg', headers={'Cache-Control':'private, no-cache'})


class ReviewWrite(BaseModel):
    version: int = Field(ge=1)
    decision: Literal['confirm','reject','unreadable']
    plate: str | None = Field(default=None, max_length=64)
    country: str = Field(default='LB', pattern=r'^[A-Za-z]{2,8}$')
    known_vehicle_id: UUID | None = None
    note: str = Field(default='', max_length=2000)


@router.post('/sightings/{identity}/review')
async def review_sighting(identity: UUID, body: ReviewWrite, db: AsyncSession = Depends(get_db), user=Depends(admin)):
    row = await db.scalar(select(VehicleSighting).where(VehicleSighting.id == identity).with_for_update())
    if not row:
        raise HTTPException(404, 'Sighting not found.')
    if row.version != body.version:
        raise HTTPException(409, 'Sighting changed. Reload before reviewing.')
    before = sighting_json(row)
    if body.decision == 'confirm':
        key = plate_key(body.plate)
        if not key:
            raise HTTPException(422, 'Enter the plate you verified in the image.')
        known = await db.get(KnownVehicle, body.known_vehicle_id) if body.known_vehicle_id else None
        if body.known_vehicle_id and (not known or known.archived):
            raise HTTPException(422, 'Known Car is unavailable.')
        if known and (known.plate_key != key or known.country != body.country.upper()):
            raise HTTPException(422, 'Plate and country must match the selected Known Car.')
        row.plate, row.plate_key, row.country = body.plate.strip(), key, body.country.upper()
        row.known_vehicle_id = known.id if known else None
        row.review_state = 'confirmed'
    elif body.decision == 'reject':
        rejected = body.known_vehicle_id or row.known_vehicle_id
        if rejected:
            if not await db.get(KnownVehicle, rejected):
                raise HTTPException(422, 'Known Car is unavailable.')
            row.rejected_vehicle_ids = sorted(set(row.rejected_vehicle_ids or []) | {str(rejected)})
        row.known_vehicle_id = None
        row.review_state = 'rejected'
    else:
        row.review_state = 'unreadable'
        row.known_vehicle_id = None
        row.plate = row.plate_key = None
    row.version += 1
    audit(db, user, 'sighting', identity, body.decision, {'before': before, 'after': sighting_json(row), 'note': body.note})
    return sighting_json(row)


@router.post('/sightings/{identity}/retry')
async def retry_sighting(identity: UUID, db: AsyncSession = Depends(get_db), user=Depends(admin)):
    row = await db.scalar(select(VehicleSighting).where(VehicleSighting.id == identity).with_for_update())
    if not row:
        raise HTTPException(404, 'Sighting not found.')
    if row.processing_state != 'failed':
        raise HTTPException(409, 'Only failed jobs can be retried here.')
    row.processing_state, row.attempts, row.error_code = 'pending', 0, None
    row.next_attempt_at = datetime.utcnow()
    audit(db, user, 'sighting', identity, 'retry', {})
    return {'status': 'queued'}


@router.post('/search/image')
async def search_image(photo: UploadFile = File(...), profile: Literal['ar','en','latin'] = 'en',
                       country: str = Query('LB', pattern=r'^[A-Za-z]{2,8}$'), filters=Depends(query_filters),
                       db: AsyncSession = Depends(get_db)):
    try:
        image = await asyncio.to_thread(image_from_bytes, await photo.read(MAX_IMAGE_BYTES+1))
        result = await vision_request('analyze', await asyncio.to_thread(jpeg_bytes, image), profile)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, 'Provide a valid car image up to 20 MB.') from exc
    except Exception as exc:
        raise HTTPException(503, 'Image search is busy or unavailable. Try again shortly.') from exc
    distance = VehicleSighting.embedding.cosine_distance(result['embedding'])
    rows = (await db.execute(select(VehicleSighting, KnownVehicle.name, distance.label('distance')).outerjoin(KnownVehicle)
        .where(*filters, VehicleSighting.embedding.is_not(None), VehicleSighting.embedding_version == EMBEDDING_VERSION)
        .order_by(distance, VehicleSighting.id).limit(60))).all()
    keys = {plate_key(' '.join(l.get('text','') for l in p.get('ocr',{}).get('lines',[]))) for p in result.get('plates',[])} - {''}
    exact = []
    if keys:
        exact = (await db.execute(select(VehicleSighting, KnownVehicle.name).outerjoin(KnownVehicle)
           .where(*filters, VehicleSighting.plate_key.in_(keys), VehicleSighting.country == country.upper())
           .order_by(VehicleSighting.captured_at.desc()).limit(100))).all()
    ids = {s.id for s,n in exact}
    return {'plate_matches': [sighting_json(s,n) for s,n in exact],
            'possible_matches': [sighting_json(s,n,max(-1,min(1,1-d))) for s,n,d in rows if s.id not in ids],
            'query_plate_texts': sorted(keys), 'note': 'Visual similarity is a retrieval score, not proof of vehicle identity. OCR plate matches also require review.'}


@router.get('/export.xlsx')
async def export_report(request: Request, filters=Depends(query_filters), db: AsyncSession = Depends(get_db), user=Depends(admin)):
    rows = (await db.execute(select(VehicleSighting, KnownVehicle.name).outerjoin(KnownVehicle).where(*filters)
                            .order_by(VehicleSighting.captured_at,VehicleSighting.id).limit(10001))).all()
    if len(rows) > 10000:
        raise HTTPException(422, 'More than 10,000 results. Narrow the date/time or camera filters before exporting.')
    headers = ['Sighting ID','Captured at (UTC)','Pipeline','Vehicle type','Plate','Country','Review','Known Car',
               'OCR score (not probability)','Processing','Vehicle image','Scene image']
    data = [headers] + [[str(s.id),utc(s.captured_at),s.pipeline_id,s.vehicle_type,s.plate,s.country,s.review_state,n,
             s.plate_score,s.processing_state,f'/api/vehicles/sightings/{s.id}/image/vehicle',f'/api/vehicles/sightings/{s.id}/image/scene'] for s,n in rows]
    criteria = [['Field','Value'],['Generated at (UTC)',utc(datetime.utcnow())],['Exported rows',len(rows)],
                ['Review meaning','Confirmed means an operator reviewed this sighting. Similarity and OCR alone are not identity proof.']]
    criteria += [[k,v] for k,v in request.query_params.items()]
    audit(db,user,'report','vehicle_sightings','exported',{'rows':len(rows),'filters':dict(request.query_params)})
    contents = await asyncio.to_thread(workbook,[('Vehicle sightings',data),('Report criteria',criteria)])
    return Response(contents,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition':'attachment; filename="vehicle-sightings.xlsx"','Cache-Control':'no-store'})


@router.get('/pipelines')
async def pipelines(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Pipeline,VehiclePipeline).outerjoin(VehiclePipeline,VehiclePipeline.pipeline_id == Pipeline.pipeline_id)
                            .where(Pipeline.is_active == 1).order_by(Pipeline.pipeline_id))).all()
    worker_status = 'disabled'
    if settings.VEHICLE_PROCESSING_ENABLED:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                response = await client.get(settings.VEHICLE_VISION_URL.rstrip('/') + '/health/ready')
                worker_status = 'ready' if response.status_code == 200 else 'unavailable'
        except Exception:
            worker_status = 'unavailable'
    return {'worker_status':worker_status,'items':[{'pipeline_id':p.pipeline_id,'name':p.location_name or p.pipeline_id,
                    'pilot':p.pipeline_id == settings.VEHICLE_PILOT_PIPELINE_ID,
                    'enabled':bool(v and v.enabled),'ocr_profile':v.ocr_profile if v else 'en','country':v.country if v else 'LB'} for p,v in rows],
            'processing_enabled':settings.VEHICLE_PROCESSING_ENABLED}


class PipelineWrite(BaseModel):
    enabled: bool
    ocr_profile: Literal['ar','en','latin'] = 'en'
    country: str = Field(default='LB', pattern=r'^[A-Za-z]{2,8}$')


@router.put('/pipelines/{pipeline_id}')
async def configure_pipeline(pipeline_id: str, body: PipelineWrite, db: AsyncSession = Depends(get_db), user=Depends(admin)):
    if pipeline_id != settings.VEHICLE_PILOT_PIPELINE_ID:
        raise HTTPException(422, 'This deployment is limited to the IT-DIRECT-CAM-1 pilot.')
    if not await db.scalar(select(Pipeline.id).where(Pipeline.pipeline_id == pipeline_id)):
        raise HTTPException(404, 'Pipeline not found.')
    row = await db.get(VehiclePipeline,pipeline_id)
    if row is None:
        row = VehiclePipeline(pipeline_id=pipeline_id)
        db.add(row)
    row.enabled,row.ocr_profile,row.country = body.enabled,body.ocr_profile,body.country.upper()
    row.updated_at = datetime.utcnow()
    audit(db,user,'pipeline',pipeline_id,'vehicle_settings',body.model_dump())
    return {'status':'saved'}
