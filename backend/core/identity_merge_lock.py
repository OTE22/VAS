"""Shared detection/exclusive merge coordination, held until transaction end."""
import uuid
from sqlalchemy import select, text
from db_models import Identity, IdentityStatus


async def lock_identity_mutation(db, *, exclusive=False):
    # Shared holders run concurrently; ownership changes wait for them.
    function = 'pg_advisory_xact_lock' if exclusive else 'pg_advisory_xact_lock_shared'
    await db.execute(text(f'SELECT {function}(734921, 1)'))


async def resolve_survivor(db, identity_id):
    seen = set()
    current = uuid.UUID(str(identity_id))
    while current not in seen:
        seen.add(current)
        person = (await db.execute(select(Identity).where(Identity.id == current)
                    .execution_options(populate_existing=True))).scalar_one_or_none()
        if person is None:
            raise ValueError(f'Identity {current} no longer exists')
        if person.status != IdentityStatus.MERGED:
            return person
        if not person.merged_into_id:
            raise ValueError(f'Merged identity {current} has no survivor')
        current = person.merged_into_id
    raise ValueError('Identity merge chain contains a cycle')


async def lock_merge_members(db, identity_ids):
    await lock_identity_mutation(db, exclusive=True)
    ids = sorted({uuid.UUID(str(i)) for i in identity_ids}, key=str)
    members = (await db.execute(select(Identity).where(Identity.id.in_(ids))
               .order_by(Identity.id).with_for_update()
               .execution_options(populate_existing=True))).scalars().all()
    if len(members) != len(ids):
        raise ValueError('One or more merge identities no longer exist')
    if any(p.status == IdentityStatus.MERGED for p in members):
        raise ValueError('An identity was already merged; refresh and select its survivor')
    return members
