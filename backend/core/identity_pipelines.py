"""Which cameras has this identity been seen on — defined once, for everyone.

The rule is a PRIORITY, not a union:

    1. identity_appearances
    2. otherwise identity_embeddings
    3. otherwise faces -> detections

It decides three things that must agree: the `pipeline_ids` drawn on a card,
whether filtering by a camera returns that identity, and whether a non-admin
may see it at all. When those disagree the failure is invisible and
security-relevant — a listing that shows an identity the detail view then
refuses, or the reverse.

It lived in three places: the unknown-faces listing (twice, once for the page
and once for its statistics) and `auth_service.check_identity_access`. All
three were the same logic hand-written from scratch, which is a drift waiting
to happen; the listing copies had already diverged in how they handled an
identity with no pipelines at all.

This module owns it now. It imports only `db_models` and SQLAlchemy, so both
the route layer and the auth layer can use it without an import cycle —
`backend.routes.identities` already imports `backend.auth.auth_service`, so
the dependency could not run the other way.
"""

from sqlalchemy import exists, false, select, union

from db_models import (
    Detection,
    Face,
    Identity,
    IdentityAppearance,
    IdentityEmbedding,
)


def has_appearance_pipelines(identity_col):
    """Correlated EXISTS: does this identity have any appearance with a camera?"""
    return exists(
        select(1).select_from(IdentityAppearance).where(
            IdentityAppearance.identity_id == identity_col,
            IdentityAppearance.pipeline_id.isnot(None),
        )
    )


def has_embedding_pipelines(identity_col):
    """Correlated EXISTS: does this identity have any embedding with a camera?"""
    return exists(
        select(1).select_from(IdentityEmbedding).where(
            IdentityEmbedding.identity_id == identity_col,
            IdentityEmbedding.pipeline_id.isnot(None),
        )
    )


def effective_pipelines(identity_ids=None):
    """Distinct ``(identity_id, pipeline_id)`` pairs under the priority rule.

    ``identity_ids`` may be a list of ids or a scalar SELECT of them; it is
    pushed into every branch so each can use its own ``(identity_id, ...)``
    index — `idx_appearance_identity_pipeline`,
    `idx_embedding_identity_created`, `ix_faces_identity_id` — rather than
    building the whole relation and filtering afterwards.
    """
    appearances = select(
        IdentityAppearance.identity_id.label("identity_id"),
        IdentityAppearance.pipeline_id.label("pipeline_id"),
    ).where(IdentityAppearance.pipeline_id.isnot(None))

    # Tier 2 only speaks for identities tier 1 said nothing about.
    embeddings = select(
        IdentityEmbedding.identity_id.label("identity_id"),
        IdentityEmbedding.pipeline_id.label("pipeline_id"),
    ).where(
        IdentityEmbedding.pipeline_id.isnot(None),
        ~has_appearance_pipelines(IdentityEmbedding.identity_id),
    )

    # Tier 3 only when neither of the first two has anything.
    faces = select(
        Face.identity_id.label("identity_id"),
        Detection.pipeline_id.label("pipeline_id"),
    ).select_from(Face).join(
        Detection, Face.detection_id == Detection.id
    ).where(
        Face.identity_id.isnot(None),
        Detection.pipeline_id.isnot(None),
        ~has_appearance_pipelines(Face.identity_id),
        ~has_embedding_pipelines(Face.identity_id),
    )

    if identity_ids is not None:
        appearances = appearances.where(IdentityAppearance.identity_id.in_(identity_ids))
        embeddings = embeddings.where(IdentityEmbedding.identity_id.in_(identity_ids))
        faces = faces.where(Face.identity_id.in_(identity_ids))

    # UNION (not UNION ALL): the pairs must be distinct.
    return union(appearances, embeddings, faces)


def pipeline_scope_predicate(pipeline_id, allowed_pipelines):
    """Membership + authorization as ONE predicate, resolved in SQL.

    ``allowed_pipelines`` is ``None`` for an admin (no restriction) or the
    caller's accessible set otherwise. An empty set fails CLOSED: an
    authorization rule that widens when it has nothing to match on is the
    failure mode worth designing against, so it returns ``false()`` rather
    than "no restriction". The same applies to a caller naming a camera
    outside their scope.

    With neither a scope nor an explicit camera this still says something: an
    identity with no effective pipeline at all is not listed.
    """
    wanted = None
    if allowed_pipelines is not None:
        allowed = sorted(set(allowed_pipelines))
        if not allowed:
            return false()
        if pipeline_id:
            if pipeline_id not in allowed:
                return false()
            wanted = [pipeline_id]
        else:
            wanted = allowed
    elif pipeline_id:
        wanted = [pipeline_id]

    scope = effective_pipelines().subquery()
    member = select(scope.c.identity_id)
    if wanted is not None:
        member = member.where(scope.c.pipeline_id.in_(wanted))
    return Identity.id.in_(member)


async def pipelines_for(db, identity_ids):
    """Resolve ``{identity_id: {pipeline_id, ...}}`` in ONE query.

    The callers that need the actual camera list — the card renderer, the
    access check — go through here rather than issuing a query per identity
    per tier, which is what the listing used to do.
    """
    if not identity_ids:
        return {}
    resolved = {}
    rows = await db.execute(effective_pipelines(list(identity_ids)))
    for identity_id, pipeline_id in rows:
        resolved.setdefault(identity_id, set()).add(pipeline_id)
    return resolved
