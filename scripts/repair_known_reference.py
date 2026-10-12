#!/usr/bin/env python3
"""Preview or explicitly approve one existing gallery image as a Known reference.

Run inside the application runtime. Default mode only reports counts/IDs.
--apply requires --confirm-photo-correct and an active administrator's ID.
No identity, sighting, image, or historical vector is deleted or relabeled.
"""
import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def repair(args):
    from sqlalchemy import select, func
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from config import settings
    from db_models import Identity, IdentityEmbedding, IdentityImage, IdentityType, User
    from backend.core.identity_service import IdentityService
    from backend.core.enrollment_service import ensure_image_reference
    from backend.utils.identity_audit import IdentityAuditLogger
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            query = select(Identity).where(Identity.id == args.identity_id)
            if args.apply:
                query = query.with_for_update()
            person = (await db.execute(query)).scalar_one()
            if person.type != IdentityType.KNOWN or person.merged_into_id is not None:
                raise ValueError('Select a surviving Known identity')
            im = (await db.execute(select(IdentityImage).where(
                IdentityImage.id == args.image_id, IdentityImage.identity_id == person.id))).scalar_one()
            if im.processing_status != 'completed':
                raise ValueError('The selected image is not ready')
            service = IdentityService()
            before = await db.scalar(select(func.count(IdentityEmbedding.id)).where(
                IdentityEmbedding.identity_id == person.id))
            trusted = await db.scalar(select(func.count(IdentityEmbedding.id)).where(
                IdentityEmbedding.identity_id == person.id, IdentityEmbedding.image_id == im.id,
                IdentityEmbedding.embedding_model_version == service.embedding_model_version))
            result = {'mode': 'apply' if args.apply else 'preview', 'identity_id': str(person.id),
                      'image_id': im.id, 'is_primary': im.is_primary,
                      'stored_vectors_before': before, 'selected_image_references_before': trusted,
                      'historical_records_deleted': 0}
            if args.apply:
                actor = (await db.execute(select(User).where(User.id == args.actor_user_id,
                    User.role == 'admin', User.is_active.is_(True)))).scalar_one()
                from backend.core.model_manager import model_manager
                await asyncio.to_thread(model_manager.initialize)
                reference = await ensure_image_reference(db, person, im, service)
                after = await db.scalar(select(func.count(IdentityEmbedding.id)).where(
                    IdentityEmbedding.identity_id == person.id))
                if after not in (before, before + 1):
                    raise RuntimeError('Unexpected reference count change; rolling back')
                await IdentityAuditLogger.log_action(db, actor.id, actor.username,
                    'recognition_reference_repair', identity_id=person.id,
                    action_details={'image_id': im.id, 'embedding_id': reference.id,
                                    'operator_confirmed_photo': True,
                                    'reference_model_version': service.embedding_model_version,
                                    'history_preserved': True},
                    before_state={'embedding_count': before},
                    after_state={'embedding_count': after}, strict=True)
                await db.commit()
                await service.sync_pending_embedding(reference.id, reference.embedding, db)
                result.update(stored_vectors_after=after, reference_embedding_id=reference.id)
            print(json.dumps(result, sort_keys=True))
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--identity-id', required=True, type=uuid.UUID)
    parser.add_argument('--image-id', required=True, type=int)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-photo-correct', action='store_true')
    parser.add_argument('--actor-user-id', type=int)
    args = parser.parse_args()
    if args.apply and (not args.confirm_photo_correct or args.actor_user_id is None):
        parser.error('--apply requires --confirm-photo-correct and --actor-user-id')
    asyncio.run(repair(args))


if __name__ == '__main__':
    main()
