"""
The VectorIndex contract, enforced against every implementation.
================================================================
Run inside the api container:

    docker exec face_recognition_api python -m pytest tests/test_vector_index_contract.py -v

Keys are `identity_embeddings.id` — the database row id. NOT `identity_id`
(a UUID, and one identity owns many embeddings, which keying by identity would
collapse into one entry), and never a positional ordinal, hash or truncated
UUID. Search returns keys; turning keys into people is the caller's job against
the database. That separation is what lets the index type change without any
schema or API change.
"""

import os
import shutil
import tempfile

import numpy as np
import pytest

from backend.core.vector_index import (EMBEDDING_DIM, FlatFaissIndex,
                                       InvalidVector, PgVectorIndex,
                                       UnsupportedIndexType,
                                       UnsupportedOperation, VectorIndex,
                                       build_index, validate_vector,
                                       vector_checksum)

DIM = EMBEDDING_DIM


def unit(seed: int) -> np.ndarray:
    """A deterministic, distinct unit vector."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def flat_index():
    directory = tempfile.mkdtemp(prefix="qa_vecidx_")
    index = FlatFaissIndex(directory, model_version="test-model-v1")
    yield index
    shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# Shared vector validation (identical on every backend)
# ---------------------------------------------------------------------------

def test_validate_vector_normalizes():
    out = validate_vector(np.full(DIM, 3.0, dtype=np.float32))
    assert pytest.approx(1.0, abs=1e-6) == float(np.linalg.norm(out))
    assert out.dtype == np.float32


@pytest.mark.parametrize("bad,reason", [
    (np.zeros(DIM, dtype=np.float32), "zero magnitude"),
    (np.full(DIM, np.nan, dtype=np.float32), "non-finite"),
    (np.ones(128, dtype=np.float32), "wrong dimension"),
    (np.ones(DIM + 1, dtype=np.float32), "wrong dimension"),
])
def test_validate_vector_rejects_unusable_input(bad, reason):
    """Each of these silently corrupted the old index instead of being refused."""
    with pytest.raises(InvalidVector):
        validate_vector(bad)


@pytest.mark.parametrize("backend", ["flat", "pgvector"])
def test_every_backend_rejects_bad_vectors_identically(backend, flat_index):
    index = flat_index if backend == "flat" else PgVectorIndex()
    for bad in (np.zeros(DIM, dtype=np.float32), np.ones(7, dtype=np.float32)):
        with pytest.raises(InvalidVector):
            index.add(1, bad)


# ---------------------------------------------------------------------------
# Add / search / remove round-trips
# ---------------------------------------------------------------------------

def test_add_and_search_returns_the_embedding_key(flat_index):
    flat_index.add(4242, unit(1))
    hits = flat_index.search(unit(1), top_k=1, threshold=0.5)
    assert hits and hits[0][0] == 4242
    assert hits[0][1] == pytest.approx(1.0, abs=1e-4)


def test_search_returns_keys_not_identities(flat_index):
    """The contract must not leak an identity concept into the index."""
    flat_index.add_many([(10, unit(1)), (11, unit(2))])
    for key, score in flat_index.search(unit(1), top_k=2):
        assert isinstance(key, int)


def test_one_identity_many_embeddings_stay_distinct(flat_index):
    """The reason the key is the embedding row, not the identity: three photos
    of one person are three independent entries."""
    keys = [101, 102, 103]
    flat_index.add_many([(k, unit(k)) for k in keys])
    assert flat_index.keys() == set(keys)
    for k in keys:
        hit = flat_index.search(unit(k), top_k=1)
        assert hit[0][0] == k, "an embedding resolved to the wrong row"


def test_threshold_is_a_similarity_floor(flat_index):
    flat_index.add(1, unit(1))
    assert flat_index.search(unit(1), top_k=1, threshold=0.99)
    assert flat_index.search(unit(2), top_k=1, threshold=0.99) == []


def test_remove_is_real_not_a_tombstone(flat_index):
    """The old index only deleted metadata, so a 'removed' vector still won
    top-k and suppressed the live person behind it."""
    flat_index.add_many([(1, unit(1)), (2, unit(2))])
    assert flat_index.remove([1]) == 1
    assert not flat_index.contains(1)
    assert flat_index.stats()["count"] == 1, "vector was not removed from the index"
    # searching for the removed vector must not consume the only slot
    hits = flat_index.search(unit(1), top_k=1, threshold=0.0)
    assert all(key != 1 for key, _ in hits)


def test_removed_vector_cannot_hide_a_live_one(flat_index):
    """Exactly the production failure: delete A, then A's near-duplicate B must
    still be findable at top_k=1."""
    base = unit(7)
    near = base + 0.01 * unit(8)
    near = near / np.linalg.norm(near)
    flat_index.add_many([(1, base), (2, near)])
    flat_index.remove([1])
    hits = flat_index.search(base, top_k=1, threshold=0.5)
    assert hits and hits[0][0] == 2, "a deleted vector still suppressed a live one"


def test_readding_the_same_key_does_not_duplicate(flat_index):
    flat_index.add(5, unit(1))
    flat_index.add(5, unit(2))
    assert flat_index.stats()["count"] == 1
    assert flat_index.search(unit(2), top_k=1)[0][0] == 5


def test_keys_reports_exactly_what_is_indexed(flat_index):
    flat_index.add_many([(k, unit(k)) for k in (3, 4, 5)])
    flat_index.remove([4])
    assert flat_index.keys() == {3, 5}


def test_search_on_empty_index_is_empty_not_an_error(flat_index):
    assert flat_index.search(unit(1), top_k=5) == []


def test_top_k_larger_than_index_is_clamped(flat_index):
    flat_index.add(1, unit(1))
    assert len(flat_index.search(unit(1), top_k=50)) == 1


# ---------------------------------------------------------------------------
# Checksums (the input reconciliation compares)
# ---------------------------------------------------------------------------

def test_checksum_is_stable_and_distinguishing():
    a, b = unit(1), unit(2)
    assert vector_checksum(a) == vector_checksum(a.copy())
    assert vector_checksum(a) != vector_checksum(b)


def test_entry_meta_carries_model_and_checksum(flat_index):
    v = unit(1)
    flat_index.add(9, v)
    model, checksum = flat_index.entry_meta(9)
    assert model == "test-model-v1"
    assert checksum == vector_checksum(v)


# ---------------------------------------------------------------------------
# Backend selection: unimplemented types fail loudly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_type", ["hnsw", "ivf", "ivfpq", "typo"])
def test_unimplemented_index_types_refuse_to_build(bad_type):
    """They used to be constructed WITHOUT a metric argument, defaulting to L2
    while the caller compared the score as a similarity — the threshold
    inverted and the matcher became an anti-matcher. Refusing is honest."""
    with pytest.raises(UnsupportedIndexType) as exc:
        build_index(bad_type, storage_dir="/tmp/qa_unused")
    assert "not implemented" in str(exc.value)
    assert "flat" in str(exc.value), "the error should name what IS supported"


def test_flat_is_built_with_inner_product():
    directory = tempfile.mkdtemp(prefix="qa_metric_")
    try:
        index = build_index("flat", storage_dir=directory)
        assert index.metric == "inner_product"
        # cosine of a vector with itself is 1.0 under IP on unit vectors
        index.add(1, unit(3))
        assert index.search(unit(3), top_k=1)[0][1] == pytest.approx(1.0, abs=1e-4)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------------
# pgvector: satisfies the contract, and is honest about what it cannot do
# ---------------------------------------------------------------------------

def test_pgvector_snapshot_methods_are_unsupported_not_faked():
    """A silent no-op save() would read as 'a snapshot exists' to every caller
    and every operator — the kind of fake success that turns a recovery drill
    into a data-loss incident."""
    index = PgVectorIndex()
    with pytest.raises(UnsupportedOperation):
        index.save()
    result = index.load()
    assert result.supported is False and result.loaded is False
    assert result.reason and "database" in result.reason


def test_pgvector_sync_search_directs_callers_to_the_async_form():
    index = PgVectorIndex()
    with pytest.raises(UnsupportedOperation) as exc:
        index.search(unit(1))
    assert "search_async" in str(exc.value)


def test_both_implementations_satisfy_the_protocol(flat_index):
    assert isinstance(flat_index, VectorIndex)
    assert isinstance(PgVectorIndex(), VectorIndex)
