"""Image-search evidence: a score, source photo and sighting from the same vector.

Historical group membership is retained, but is not proof of a Known name.
All reads use authoritative DB vectors, including with a disposable FAISS index.
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import aliased

from config import settings
from db_models import (Identity, IdentityEmbedding, IdentityImage, IdentityAppearance,
                       IdentityStatus, IdentityType, Face, Detection)
from backend.core.merge_compatibility import _parse_vector
from backend.utils.path_utils import path_to_url, normalize_storage_path


class SearchMatchContext(BaseModel):
    """Additive API fields shared by main search and Quick Search."""
    matched_embedding_id: Optional[int] = None
    matched_appearance_id: Optional[int] = None
    matched_face_id: Optional[int] = None
    matched_image_id: Optional[int] = None
    matched_at: Optional[str] = None
    matched_pipeline_id: Optional[str] = None
    match_source: str = 'unavailable'
    identity_verification: str = 'unverified'
    reference_similarity: Optional[float] = None


@dataclass
class SearchEvidence:
    identity: Identity
    embedding: IdentityEmbedding
    similarity: float
    snapshot_path: Optional[str] = None
    appearance_id: Optional[int] = None
    face_id: Optional[int] = None
    image_id: Optional[int] = None
    seen_at: object = None
    pipeline_id: Optional[str] = None
    source: str = 'unavailable'
    verification: str = 'unverified'
    reference_similarity: Optional[float] = None

    def fields(self):
        return {
            'matched_embedding_id': self.embedding.id,
            'matched_appearance_id': self.appearance_id,
            'matched_face_id': self.face_id,
            'matched_image_id': self.image_id,
            'matched_at': self.seen_at.isoformat() + 'Z' if self.seen_at else None,
            'matched_pipeline_id': self.pipeline_id,
            'match_source': self.source,
            'identity_verification': self.verification,
            'reference_similarity': self.reference_similarity,
        }

    def result(self):
        known = self.identity.type == IdentityType.KNOWN
        verified = self.verification == 'verified_reference'
        return {
            'identity_id': str(self.identity.id),
            'type': 'known' if known and verified else 'unverified' if known else 'unknown',
            'display_name': self.identity.display_name if known and verified else None,
            'similarity': self.similarity,
            # Compatibility keys now deliberately describe the MATCHED photo.
            'best_snapshot_path': self.snapshot_path,
            'snapshot_url': path_to_url(self.snapshot_path, settings.STORAGE_DIR) if self.snapshot_path else None,
            'last_seen_at': self.identity.last_seen_at.isoformat() + 'Z' if self.identity.last_seen_at else None,
            'appearances_count': self.identity.appearances_count or 0,
            **self.fields(),
        }


async def hydrate_evidence(db, hits, query_vector, model_version, *, reference_threshold=None):
    """Resolve only unambiguous links. Never borrow another photo from a group."""
    if not hits:
        return []
    threshold = float(settings.SIMILARITY_THRESHOLD if reference_threshold is None else reference_threshold)
    ids = {person.id for person, _, _ in hits}
    detection_ids = {row.detection_id for _, row, _ in hits if row.detection_id is not None}
    image_ids = {row.image_id for _, row, _ in hits if row.image_id is not None}
    images = {im.id: im for im in (await db.execute(select(IdentityImage).where(
        IdentityImage.id.in_(image_ids)))).scalars().all()} if image_ids else {}
    faces, apps, vector_counts = {}, {}, {}
    if detection_ids:
        for face, pipeline, stamp in (await db.execute(select(Face, Detection.pipeline_id, Detection.timestamp)
                .join(Detection, Face.detection_id == Detection.id).where(
                    Face.identity_id.in_(ids), Face.detection_id.in_(detection_ids)))).all():
            faces.setdefault((face.identity_id, face.detection_id), []).append((face, pipeline, stamp))
        for app in (await db.execute(select(IdentityAppearance).where(
                IdentityAppearance.identity_id.in_(ids), IdentityAppearance.detection_id.in_(detection_ids)))).scalars().all():
            apps.setdefault((app.identity_id, app.detection_id), []).append(app)
        # More than one same-model vector or face in a detection has no exact
        # vector-to-face key in the current schema; do not guess by row order.
        for identity_id, detection_id, count in (await db.execute(select(
                IdentityEmbedding.identity_id, IdentityEmbedding.detection_id, func.count())
                .where(IdentityEmbedding.identity_id.in_(ids), IdentityEmbedding.detection_id.in_(detection_ids),
                       IdentityEmbedding.embedding_model_version == model_version)
                .group_by(IdentityEmbedding.identity_id, IdentityEmbedding.detection_id))).all():
            vector_counts[(identity_id, detection_id)] = count

    anchor = aliased(IdentityEmbedding)
    scores = {}
    known_rows = [row.id for person, row, _ in hits if person.type == IdentityType.KNOWN]
    if known_rows:
        # BOTH the query and the historical vector must meet the threshold
        # against the SAME approved image. Old auto-enrichment is not approval.
        reference_score = 1 - anchor.embedding.cosine_distance(IdentityEmbedding.embedding)
        if query_vector is not None:
            reference_score = func.least(reference_score, 1 - anchor.embedding.cosine_distance(query_vector))
        rows = await db.execute(select(IdentityEmbedding.id, func.max(reference_score))
            .join(anchor, anchor.identity_id == IdentityEmbedding.identity_id)
            .join(IdentityImage, (IdentityImage.id == anchor.image_id) &
                  (IdentityImage.identity_id == anchor.identity_id))
            .where(IdentityEmbedding.id.in_(known_rows), anchor.embedding_model_version == model_version,
                   anchor.embedding.isnot(None), IdentityImage.processing_status == 'completed')
            .group_by(IdentityEmbedding.id))
        scores = dict(rows.all())

    # Unknown watchlist matching also needs direct agreement with the stable
    # group anchor. Historical membership alone must not trigger an alert.
    unknown_scores = {}
    unknown_ids = [person.id for person, _, _ in hits if person.type == IdentityType.UNKNOWN]
    if unknown_ids:
        first = select(IdentityEmbedding.identity_id.label('identity_id'),
            func.min(IdentityEmbedding.id).label('id')).where(
                IdentityEmbedding.identity_id.in_(unknown_ids),
                IdentityEmbedding.faiss_index_type.is_distinct_from('unknown_observation'),
                IdentityEmbedding.embedding_model_version == model_version,
                IdentityEmbedding.embedding.isnot(None)).group_by(IdentityEmbedding.identity_id).subquery()
        similarity = 1 - anchor.embedding.cosine_distance(IdentityEmbedding.embedding)
        if query_vector is not None:
            similarity = func.least(similarity, 1 - anchor.embedding.cosine_distance(query_vector))
        unknown_scores = dict((await db.execute(select(IdentityEmbedding.id, similarity)
            .join(first, first.c.identity_id == IdentityEmbedding.identity_id)
            .join(anchor, anchor.id == first.c.id)
            .where(IdentityEmbedding.id.in_([row.id for person, row, _ in hits
                                           if person.type == IdentityType.UNKNOWN])))).all())

    results = []
    for person, row, similarity in hits:
        item = SearchEvidence(person, row, float(np.clip(similarity, -1., 1.)))
        image = images.get(row.image_id)
        if image and image.identity_id == person.id and image.processing_status == 'completed':
            item.snapshot_path, item.image_id, item.source = image.storage_path, image.id, 'enrollment'
        elif row.image_id is None:
            key = (person.id, row.detection_id)
            owners = faces.get(key, [])
            if len(owners) == 1 and vector_counts.get(key) == 1:
                face, pipeline, stamp = owners[0]
                if pipeline == row.pipeline_id:
                    item.face_id, item.snapshot_path = face.id, face.face_image_path
                    item.source, item.pipeline_id, item.seen_at = 'sighting', pipeline, stamp
                    candidates = [app for app in apps.get(key, []) if app.pipeline_id == pipeline and
                        (not app.best_snapshot_path or (face.face_image_path and
                         normalize_storage_path(app.best_snapshot_path) == normalize_storage_path(face.face_image_path)))]
                    if len(candidates) == 1:
                        item.appearance_id, item.seen_at = candidates[0].id, candidates[0].start_time
        score = scores.get(row.id)
        if score is not None and np.isfinite(score) and -1.000001 <= score <= 1.000001:
            item.reference_similarity = float(np.clip(score, -1., 1.))
        if person.type == IdentityType.KNOWN:
            if item.reference_similarity is not None and item.reference_similarity >= threshold:
                item.verification = 'verified_reference'
        else:
            score = unknown_scores.get(row.id)
            item.verification = ('consistent_reference' if score is not None and
                np.isfinite(score) and threshold <= score <= 1.000001 else 'unknown')
        results.append(item)
    return results


async def search_evidence(db, embedding, model_version, scope, top_k, *,
                          start=None, end=None, pipeline_id=None, exclude_identity_ids=None,
                          min_similarity=None, max_similarity=None):
    """Filter exact observation, rank distinct groups, then resolve provenance.

The window retains the winning vector ID (min(distance) alone loses it).
Exact DB ranking avoids approximate-index omissions and works for both index
configurations. No identity, sighting, gallery or embedding writes occur here.
"""
    vector = _parse_vector(embedding)
    if vector is None or vector.size != 512 or not model_version:
        raise HTTPException(503, 'The current recognition model is not ready for search.')
    if scope not in ('known', 'unknown', 'both'):
        raise HTTPException(422, 'Choose a valid search scope.')
    floor = float(settings.SIMILARITY_THRESHOLD if min_similarity is None else min_similarity)
    distance = IdentityEmbedding.embedding.cosine_distance(vector)
    filters = [Identity.merged_into_id.is_(None),
               Identity.status.in_([IdentityStatus.ACTIVE, IdentityStatus.PROMOTED, IdentityStatus.INACTIVE]),
               IdentityEmbedding.embedding_model_version == model_version,
               IdentityEmbedding.embedding.isnot(None),
               distance >= -0.000001, distance <= 1 - floor]
    if scope != 'both':
        filters.append(Identity.type == (IdentityType.KNOWN if scope == 'known' else IdentityType.UNKNOWN))
    if exclude_identity_ids:
        import uuid
        filters.append(Identity.id.notin_([uuid.UUID(str(value)) for value in exclude_identity_ids]))
    if start or end or pipeline_id:
        sighting = select(IdentityAppearance.id).where(
            IdentityAppearance.identity_id == IdentityEmbedding.identity_id,
            IdentityAppearance.detection_id == IdentityEmbedding.detection_id,
            IdentityAppearance.pipeline_id == IdentityEmbedding.pipeline_id)
        if start is not None: sighting = sighting.where(IdentityAppearance.start_time >= start)
        if end is not None: sighting = sighting.where(IdentityAppearance.start_time <= end)
        if pipeline_id: sighting = sighting.where(IdentityAppearance.pipeline_id == pipeline_id)
        filters.append(sighting.exists())
    ranked = select(IdentityEmbedding.id.label('embedding_id'), distance.label('distance'),
        func.row_number().over(partition_by=IdentityEmbedding.identity_id,
            order_by=(distance, IdentityEmbedding.id)).label('rank'))\
        .join(Identity, Identity.id == IdentityEmbedding.identity_id).where(*filters).subquery()
    best = [ranked.c.rank == 1]
    # Apply the upper boundary AFTER choosing the group's best observation,
    # so a strong group cannot reappear here via a weaker historical vector.
    if max_similarity is not None:
        best.append(ranked.c.distance > 1 - float(max_similarity))
    rows = (await db.execute(select(Identity, IdentityEmbedding, (1 - ranked.c.distance).label('similarity'))
        .join(IdentityEmbedding, IdentityEmbedding.identity_id == Identity.id)
        .join(ranked, ranked.c.embedding_id == IdentityEmbedding.id).where(*best)
        .order_by(ranked.c.distance, Identity.id).limit(top_k))).all()
    return await hydrate_evidence(db, rows, vector, model_version)
