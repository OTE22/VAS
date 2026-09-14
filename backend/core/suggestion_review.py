"""Validated suggestion grouping and transactional, pre-merge feedback."""
import statistics
from datetime import datetime
from itertools import combinations
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from config import settings
from db_models import Identity, IdentityEmbedding, SimilarityTrainingData
from backend.core.face_quality import QUALITY_SCORER_VERSION
from backend.core.merge_compatibility import _load_valid_embeddings, _pair_similarities
from backend.core.identity_pipelines import pipelines_for


def pair_score(left, right):
    values = _pair_similarities(left, right)
    return statistics.median(values) if values else None


def complete_groups(samples, threshold, minimum=2):
    """Deterministic disjoint groups: every pair must meet the threshold."""
    groups = []
    for identifier in sorted(samples):
        for group in groups:
            scores = [pair_score(samples[identifier], samples[other]) for other in group]
            if all(score is not None and score >= threshold for score in scores):
                group.append(identifier)
                break
        else:
            groups.append([identifier])
    return [(group, min(pair_score(samples[a], samples[b]) for a, b in combinations(group, 2)))
            for group in groups if len(group) >= max(2, minimum)]


async def pipeline_groups(db, identities):
    samples = {str(p.id): (await _load_valid_embeddings(db, p.id))[0] for p in identities}
    return complete_groups(samples, max(float(settings.MERGE_WARNING_MIN_SIMILARITY),
                                       1 - float(settings.CLUSTER_EPS)), settings.CLUSTER_MIN_SAMPLES)


async def record_feedback(db, identity_ids, user_id, label):
    """Stage feedback before ownership changes; it commits with the review."""
    people = (await db.execute(select(Identity).where(Identity.id.in_(identity_ids)).order_by(Identity.id))).scalars().all()
    # Rejecting a group says at least one member differs, not that every pair differs.
    if label == 0 and len(people) != 2:
        return
    cameras = await pipelines_for(db, [p.id for p in people])
    samples = {p.id: (await _load_valid_embeddings(db, p.id))[0] for p in people}
    quality = dict((await db.execute(select(IdentityEmbedding.identity_id, func.avg(IdentityEmbedding.quality))
        .where(IdentityEmbedding.identity_id.in_(identity_ids),
               IdentityEmbedding.quality_scorer_version == QUALITY_SCORER_VERSION,
               IdentityEmbedding.quality.between(0, 1))
        .group_by(IdentityEmbedding.identity_id))).all())
    for left, right in combinations(people, 2):
        score = pair_score(samples[left.id], samples[right.id])
        if score is None or left.id not in quality or right.id not in quality:
            continue
        a, b = cameras.get(left.id, set()), cameras.get(right.id, set())
        values = dict(
            identity_id_1=left.id, identity_id_2=right.id,
            embedding_similarity=score, pipeline_overlap=len(a & b) / len(a | b) if a | b else 0,
            quality_score_1=quality[left.id], quality_score_2=quality[right.id],
            appearances_diff=abs((left.appearances_count or 0) - (right.appearances_count or 0)),
            is_cross_pipeline=not bool(a & b), label=label, created_by_user_id=user_id,
            created_at=datetime.utcnow())
        statement = insert(SimilarityTrainingData).values(**values)
        await db.execute(statement.on_conflict_do_update(
            index_elements=['identity_id_1', 'identity_id_2'],
            index_where=SimilarityTrainingData.identity_id_1.isnot(None) & SimilarityTrainingData.identity_id_2.isnot(None),
            set_={key: value for key, value in values.items() if key not in ('identity_id_1', 'identity_id_2')}))
