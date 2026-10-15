"""Persistent, direct-pair human decisions in the existing identity audit journal.

These events are domain evidence, exempt from routine audit expiry. They never
modify embeddings, enrollment, ownership or automatic similarity thresholds.
An identity already owns both sightings; approval attests only this exact pair.
"""
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import HTTPException
from sqlalchemy import select

from config import settings
from db_models import Identity, IdentityAppearance, IdentityAuditLog, IdentityStatus, Face, Detection, Pipeline
from backend.core.identity_merge_lock import lock_identity_mutation

ACTION = 'sighting_review'
AUDIENCE = 'vas:sighting-review'
JOURNEY_STATES = ('selected', 'consistent', 'operator_confirmed')


def require_reviewer(user):
    if user.role != 'admin':
        raise HTTPException(403, 'Only administrators can confirm sighting associations.')


def proof(app):
    from backend.utils.path_utils import normalize_storage_path
    return {'id': app.id, 'identity_id': str(app.identity_id), 'detection_id': app.detection_id,
            'pipeline_id': app.pipeline_id, 'event_id': app.event_id,
            'start_time': app.start_time.isoformat(),
            'snapshot_path': normalize_storage_path(app.best_snapshot_path) if app.best_snapshot_path else None}


def proof_matches(saved, app):
    """Image expiry may clear its path; reassignment/replacement invalidates review."""
    current = proof(app)
    return all(saved.get(k) == v for k, v in current.items()
               if k != 'snapshot_path' or v is not None)


async def latest_decisions(db, identity_id):
    rows = (await db.scalars(select(IdentityAuditLog).where(
        IdentityAuditLog.identity_id == identity_id, IdentityAuditLog.action_type == ACTION,
        IdentityAuditLog.success.is_(True)).order_by(IdentityAuditLog.id.desc()))).all()
    result = {}
    for row in rows:
        data = row.action_details or {}
        pair = data.get('pair')
        if data.get('version') == 1 and isinstance(pair, list) and len(pair) == 2:
            result.setdefault(tuple(pair), row)
    return result


async def apply_decisions(db, identity_id, appearances, reference_id, associations):
    if reference_id is None:
        return
    by_id = {app.id: app for app in appearances}
    reference = by_id.get(reference_id)
    if reference is None:
        return
    for pair, record in (await latest_decisions(db, identity_id)).items():
        if reference_id not in pair:
            continue
        other_id = pair[1] if pair[0] == reference_id else pair[0]
        app = by_id.get(other_id)
        if app is None or other_id not in associations:
            continue
        data = record.action_details
        if data.get('decision') == 'clear':
            continue
        saved = {p['id']: p for p in data.get('sightings', [])}
        valid = all(a.id in saved and proof_matches(saved[a.id], a) for a in (reference, app))
        decision = data.get('decision')
        if decision not in ('confirmed', 'rejected'):
            continue
        state = ('operator_confirmed' if decision == 'confirmed' else 'operator_rejected') if valid else 'unverified'
        associations[other_id].update(
            association_status=state,
            association_reason=('Operator confirmed this sighting against the selected reference' if decision == 'confirmed'
                else 'Operator marked this sighting as a different person') if valid
                else 'The reviewed sighting changed; review it again.',
            manual_review={'decision': decision, 'reviewer': record.username,
                'reviewed_at': record.created_at.isoformat() + 'Z', 'revision': record.id,
                'note': data.get('note', ''), 'stale': not valid})


async def resolve_pair(db, identity_id, reference_id, appearance_id):
    if reference_id == appearance_id:
        raise HTTPException(422, 'Choose a different sighting to compare with the reference.')
    person = await db.get(Identity, identity_id, populate_existing=True)
    if not person or person.merged_into_id or person.status == IdentityStatus.MERGED:
        raise HTTPException(409, 'This identity changed. Reopen its current profile.')
    rows = (await db.scalars(select(IdentityAppearance).where(
        IdentityAppearance.identity_id == identity_id,
        IdentityAppearance.id.in_([reference_id, appearance_id])).execution_options(populate_existing=True))).all()
    by_id = {a.id: a for a in rows}
    if len(by_id) != 2:
        raise HTTPException(409, 'A sighting moved or is unavailable. Reopen the identity before reviewing.')
    return person, by_id[reference_id], by_id[appearance_id]


async def review_photo(db, app):
    """Use one exact event photo; reject ambiguous ownership and missing media."""
    from backend.core.enrollment_service import pending_absolute_path, EnrollmentError
    from backend.utils.path_utils import normalize_storage_path, path_to_url
    if not app.detection_id or not app.best_snapshot_path:
        return None, 'This sighting has no saved photo linked to a detection.'
    faces = (await db.scalars(select(Face).join(Detection, Detection.id == Face.detection_id).where(
        Face.identity_id == app.identity_id, Face.detection_id == app.detection_id,
        Detection.pipeline_id == app.pipeline_id))).all()
    if len(faces) != 1 or normalize_storage_path(faces[0].face_image_path or '') != normalize_storage_path(app.best_snapshot_path):
        return None, 'This photo cannot be linked unambiguously to one face.'
    try:
        path = Path(pending_absolute_path(normalize_storage_path(app.best_snapshot_path))).resolve()
        if not path.is_relative_to(Path(settings.STORAGE_DIR).resolve()):
            raise ValueError('Photo outside storage')
        def checksum():
            if path.stat().st_size > 20 * 1024 * 1024:
                raise ValueError('Photo too large')
            return hashlib.sha256(path.read_bytes()).hexdigest()
        digest = await asyncio.to_thread(checksum)
    except (OSError, ValueError, EnrollmentError):
        return None, 'The saved photo is unavailable. A photo is required to confirm or reject.'
    url = path_to_url(app.best_snapshot_path, settings.STORAGE_DIR)
    if not url:
        return None, 'The saved photo is unavailable.'
    return {'sha256': digest, 'url': url + ('&' if '?' in url else '?') + 'review_version=' + digest}, None


async def preview_review(db, identity_id, reference_id, appearance_id, user):
    require_reviewer(user)
    person, reference, app = await resolve_pair(db, identity_id, reference_id, appearance_id)
    pair = sorted([reference.id, app.id])
    record = (await latest_decisions(db, identity_id)).get(tuple(pair))
    photos, blocks, displays = [], [], []
    for sighting in (reference, app):
        photo, error = await review_photo(db, sighting)
        photos.append(photo['sha256'] if photo else None)
        if error:
            blocks.append(error)
        camera = await db.scalar(select(Pipeline.location_name).where(Pipeline.pipeline_id == sighting.pipeline_id))
        displays.append({'id': sighting.id, 'snapshot_url': photo['url'] if photo else None,
            'camera_name': camera or sighting.location_name or sighting.pipeline_id,
            'seen_at': sighting.start_time.isoformat() + 'Z'})
    now = datetime.now(timezone.utc)
    token = jwt.encode({'aud': AUDIENCE, 'iat': now, 'exp': now + timedelta(minutes=10),
        'actor_id': user.id, 'identity_id': str(identity_id), 'reference_id': reference.id,
        'appearance_id': app.id, 'revision': record.id if record else 0,
        'sightings': [proof(reference), proof(app)], 'photos': photos},
        settings.JWT_SECRET_KEY, algorithm='HS256')
    return {'preview_token': token, 'identity_id': str(identity_id),
        'reference': displays[0], 'sighting': displays[1],
        'decision': (record.action_details or {}).get('decision') if record else None,
        'reviewer': record.username if record else None,
        'reviewed_at': record.created_at.isoformat() + 'Z' if record else None,
        'can_decide': not blocks, 'blocked_reason': ' '.join(dict.fromkeys(blocks)),
        'can_undo': bool(record and record.action_details.get('decision') != 'clear')}


async def save_review(db, identity_id, appearance_id, user, *, preview_token, decision, note=''):
    require_reviewer(user)
    if decision not in ('confirmed', 'rejected', 'clear'):
        raise HTTPException(422, 'Choose Confirm same person, Different person, or Undo.')
    try:
        claims = jwt.decode(preview_token, settings.JWT_SECRET_KEY, algorithms=['HS256'], audience=AUDIENCE,
                            options={'require': ['exp', 'iat', 'aud']})
    except jwt.InvalidTokenError as exc:
        raise HTTPException(409, 'This review preview expired. Reopen it before saving.') from exc
    if (claims.get('actor_id') != user.id or claims.get('identity_id') != str(identity_id)
            or claims.get('appearance_id') != appearance_id):
        raise HTTPException(403, 'This preview belongs to a different review or administrator.')
    await lock_identity_mutation(db, exclusive=True)
    person, reference, app = await resolve_pair(db, identity_id, claims['reference_id'], appearance_id)
    sightings = [reference, app]
    if [proof(a) for a in sightings] != claims.get('sightings'):
        raise HTTPException(409, 'The selected sightings changed. Reopen the review.')
    pair = tuple(sorted([reference.id, app.id]))
    latest = (await latest_decisions(db, identity_id)).get(pair)
    if (latest.id if latest else 0) != claims.get('revision'):
        raise HTTPException(409, 'Another review was saved. Reopen to see the latest decision.')
    if decision == 'clear':
        if not latest or latest.action_details.get('decision') == 'clear':
            raise HTTPException(409, 'There is no saved decision to undo.')
    else:
        for index, sighting in enumerate(sightings):
            photo, error = await review_photo(db, sighting)
            if error or not claims['photos'][index] or photo['sha256'] != claims['photos'][index]:
                raise HTTPException(409, 'A reviewed photo changed or is unavailable. Reopen the review.')
    record = IdentityAuditLog(user_id=user.id, username=user.username, action_type=ACTION,
        identity_id=person.id, success=True,
        action_details={'version': 1, 'pair': list(pair), 'decision': decision,
            'sightings': claims['sightings'], 'photo_sha256': claims['photos'],
            'note': note.strip(), 'previous_revision': latest.id if latest else None},
        before_state={'decision': latest.action_details.get('decision') if latest else None},
        after_state={'decision': decision})
    db.add(record)
    await db.flush()
    return {'success': True, 'identity_id': str(person.id), 'reference_appearance_id': reference.id,
        'appearance_id': app.id, 'decision': decision, 'reviewed_at': record.created_at.isoformat() + 'Z',
        'reviewer': user.username, 'revision': record.id}
