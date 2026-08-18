"""Merge compatibility: is this set of identities plausibly one person?

THE one gate every merge passes before it mutates anything. Both service
entry points (`merge_identities`, `merge_multiple_identities`) call
`assess_merge_compatibility` first, so the pair route, multi-merge, preview
execution, suggestion approval and the promote merge-into-known flow are all
covered by construction — a new merge caller cannot bypass the gate without
bypassing the merge itself.

Method
------
Only stored, validated embeddings participate: a vector must parse, be finite,
non-zero and unit-norm (the storage contract) to count. Comparisons happen
only between embeddings that share an `embedding_model_version` — cosine
similarity across embedding spaces is meaningless, and NULL provenance is its
own bucket (two unknowns may be compared with each other, never with a
stamped vector).

For each identity PAIR the score is the MEDIAN of all cross-identity cosine
similarities, never the maximum: one contaminated embedding — a frame of
person B mis-attributed to identity A — produces exactly one near-1.0 pair,
and a max would let that single bad row vouch for merging two strangers.
The group's `robust_similarity` is the MINIMUM of those per-pair medians, so
in a multi-merge one unrelated member drags the whole group to high risk
instead of averaging away.

`risk_level`:
    compatible   every pair comparable, robust_similarity >= threshold
    high_risk    every pair comparable, robust_similarity <  threshold
    unavailable  at least one pair could not be compared at all — no valid
                 embeddings on a side, or no shared model version. No score
                 is fabricated for it.
"""

import logging
import statistics
import uuid as uuid_module
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings

logger = logging.getLogger(__name__)

# How many stored embeddings per identity participate, newest first. Enough
# for a stable median; bounded so a 200-embedding identity cannot turn one
# gate check into 10k comparisons.
_MAX_EMBEDDINGS_PER_IDENTITY = 8

# NULL embedding_model_version bucket key. NULL-with-NULL comparisons are
# allowed (both unknown, in practice the one deployed model); NULL-with-known
# is not provable and is skipped.
_UNKNOWN_MODEL = "(unknown-model)"


@dataclass
class MergeCompatibility:
    """Structured result of one assessment. Serializable via to_dict()."""

    comparable: bool
    risk_level: str                       # compatible | high_risk | unavailable
    threshold: float
    robust_similarity: Optional[float]    # min over pairs of median cross-sim
    minimum_similarity: Optional[float]   # min single cross-comparison seen
    compared_embedding_count: int         # total cross comparisons performed
    reason_codes: List[str] = field(default_factory=list)
    # (identity_a, identity_b, median_similarity_or_None) for every pair that
    # is below threshold or could not be compared.
    incompatible_pairs: List[Tuple[str, str, Optional[float]]] = field(default_factory=list)

    @property
    def requires_confirmation(self) -> bool:
        return self.risk_level in ("high_risk", "unavailable")

    def to_dict(self) -> dict:
        return {
            "comparable": self.comparable,
            "risk_level": self.risk_level,
            "threshold": round(self.threshold, 4),
            "robust_similarity": (round(self.robust_similarity, 4)
                                  if self.robust_similarity is not None else None),
            "minimum_similarity": (round(self.minimum_similarity, 4)
                                   if self.minimum_similarity is not None else None),
            "compared_embedding_count": self.compared_embedding_count,
            "reason_codes": list(self.reason_codes),
            "incompatible_pairs": [
                {"identity_a": a, "identity_b": b,
                 "similarity": round(s, 4) if s is not None else None}
                for a, b, s in self.incompatible_pairs
            ],
        }


class MergeCompatibilityBlocked(Exception):
    """Raised by the merge services BEFORE any mutation when the assessment
    demands explicit confirmation. Routes map this to the structured 409."""

    def __init__(self, assessment: MergeCompatibility):
        self.assessment = assessment
        super().__init__(
            f"merge blocked: {assessment.risk_level} "
            f"(robust={assessment.robust_similarity}, "
            f"threshold={assessment.threshold})")


def _parse_vector(raw) -> Optional[np.ndarray]:
    """A stored embedding, or None if it is not a usable unit vector.

    Zero, NaN/inf and badly-scaled vectors are refused here, so they simply
    do not participate — they can neither vouch for a merge nor block one
    with a fabricated score.
    """
    try:
        if raw is None:
            return None
        vector = np.asarray(raw, dtype=np.float32).reshape(-1)
        if vector.size == 0 or not np.all(np.isfinite(vector)):
            return None
        norm = float(np.linalg.norm(vector))
        # Stored embeddings are L2-normalized by contract; anything far off
        # is corrupt data, not a face.
        if norm < 1e-6 or abs(norm - 1.0) > 0.01:
            return None
        return vector
    except Exception:                                          # noqa: BLE001
        return None


async def _load_valid_embeddings(
    db: AsyncSession, identity_id
) -> Tuple[Dict[str, List[np.ndarray]], int]:
    """{model_version_bucket: [unit vectors]} plus how many rows were skipped."""
    from db_models import IdentityEmbedding

    rows = (await db.execute(
        select(IdentityEmbedding.embedding,
               IdentityEmbedding.embedding_model_version)
        .where(IdentityEmbedding.identity_id == identity_id,
               IdentityEmbedding.embedding.isnot(None))
        .order_by(IdentityEmbedding.created_at.desc())
        .limit(_MAX_EMBEDDINGS_PER_IDENTITY * 3))).all()

    by_model: Dict[str, List[np.ndarray]] = {}
    skipped = 0
    for raw, model_version in rows:
        vector = _parse_vector(raw)
        if vector is None:
            skipped += 1
            continue
        bucket = model_version or _UNKNOWN_MODEL
        vectors = by_model.setdefault(bucket, [])
        if len(vectors) < _MAX_EMBEDDINGS_PER_IDENTITY:
            vectors.append(vector)
    return by_model, skipped


def _pair_similarities(
    a_by_model: Dict[str, List[np.ndarray]],
    b_by_model: Dict[str, List[np.ndarray]],
) -> List[float]:
    """All cross-identity cosine similarities within shared model buckets."""
    sims: List[float] = []
    for model, a_vectors in a_by_model.items():
        b_vectors = b_by_model.get(model)
        if not b_vectors:
            continue
        a_matrix = np.stack(a_vectors)
        b_matrix = np.stack(b_vectors)
        # unit vectors: the inner product IS the cosine similarity
        sims.extend(float(s) for s in (a_matrix @ b_matrix.T).reshape(-1))
    return sims


async def assess_merge_compatibility(
    db: AsyncSession,
    identity_ids: Sequence,
) -> MergeCompatibility:
    """Assess whether merging these identities is visually defensible.

    Read-only: performs no writes of any kind. Always recomputed at decision
    time — an override request never trusts a score the frontend displayed.
    """
    threshold = float(settings.MERGE_WARNING_MIN_SIMILARITY)
    ids = [uuid_module.UUID(str(i)) for i in identity_ids]
    reason_codes: List[str] = []

    per_identity: Dict[str, Dict[str, List[np.ndarray]]] = {}
    for identity_id in ids:
        by_model, skipped = await _load_valid_embeddings(db, identity_id)
        per_identity[str(identity_id)] = by_model
        if skipped:
            reason_codes.append(f"invalid_embeddings_skipped:{identity_id}:{skipped}")
        if not by_model:
            reason_codes.append(f"no_valid_embeddings:{identity_id}")

    pair_medians: List[float] = []
    incompatible: List[Tuple[str, str, Optional[float]]] = []
    minimum_similarity: Optional[float] = None
    compared = 0
    any_pair_uncomparable = False

    id_strings = [str(i) for i in ids]
    for index_a in range(len(id_strings)):
        for index_b in range(index_a + 1, len(id_strings)):
            a, b = id_strings[index_a], id_strings[index_b]
            sims = _pair_similarities(per_identity[a], per_identity[b])
            if not sims:
                any_pair_uncomparable = True
                incompatible.append((a, b, None))
                if per_identity[a] and per_identity[b]:
                    reason_codes.append(f"no_common_model_version:{a}:{b}")
                continue
            compared += len(sims)
            pair_median = float(statistics.median(sims))
            pair_min = min(sims)
            minimum_similarity = (pair_min if minimum_similarity is None
                                  else min(minimum_similarity, pair_min))
            pair_medians.append(pair_median)
            if pair_median < threshold:
                incompatible.append((a, b, pair_median))

    if any_pair_uncomparable or not pair_medians:
        reason_codes.append("compatibility_unavailable")
        return MergeCompatibility(
            comparable=False, risk_level="unavailable", threshold=threshold,
            robust_similarity=None, minimum_similarity=minimum_similarity,
            compared_embedding_count=compared,
            reason_codes=reason_codes, incompatible_pairs=incompatible)

    robust = min(pair_medians)
    if robust < threshold:
        reason_codes.append("below_threshold")
        # A member whose every pairing is incompatible is the outlier the
        # multi-merge probably swept up by mistake — name it.
        if len(id_strings) > 2:
            bad_counts: Dict[str, int] = {}
            for a, b, _s in incompatible:
                bad_counts[a] = bad_counts.get(a, 0) + 1
                bad_counts[b] = bad_counts.get(b, 0) + 1
            for identity_id, count in bad_counts.items():
                if count == len(id_strings) - 1:
                    reason_codes.append(f"outlier_member:{identity_id}")
        risk = "high_risk"
    else:
        risk = "compatible"

    return MergeCompatibility(
        comparable=True, risk_level=risk, threshold=threshold,
        robust_similarity=robust, minimum_similarity=minimum_similarity,
        compared_embedding_count=compared,
        reason_codes=reason_codes, incompatible_pairs=incompatible)
