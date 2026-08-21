"""The per-identity embedding cap applies to camera vectors, not the gallery.

Run explicitly:

    docker exec -w /app <api> python -m pytest tests/audit_embedding_retention_scope.py -v

WHY THIS EXISTS
An identity accumulates vectors from two sources that behave nothing alike:

  * enrollment — one per identity_images row, chosen by an administrator,
    already bounded by MAX_IMAGES_PER_IDENTITY (1000). Distinguished by
    image_id IS NOT NULL.
  * camera     — created automatically whenever a pipeline recognises someone.
    image_id IS NULL, pipeline_id set.

_cleanup_excess_embeddings used to trim both against one limit of 10, ordered
by `quality DESC NULLS LAST`. Enrollment rows carried quality = NULL, and NULLS
LAST puts them at exactly the end of the keep-order that deletions are taken
from — so a busy camera pruned the curated gallery first, while leaving the
identity_images rows in place. The photo stayed visible and stopped
contributing to recognition, silently.

The same module explicitly refuses to delete enrollment FILES
(`_is_enrollment_photo`), so deleting their vectors contradicted its own rule.

These tests hold both halves: gallery vectors survive any amount of camera
traffic, and camera vectors are still capped.
"""
import os
import uuid as uuid_module

import pytest

from conftest import run_on_shared_loop as run_async

from audit_add_person_matrix import (
    FACE_A, FACE_B, FACE_C, _http, _read, _sql, _upload, _identity_ids_for,
    _record,
)

TEST_PREFIX = "qa_embscope_"


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         body={"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


@pytest.fixture(scope="module", autouse=True)
def _clean():
    _purge()
    yield
    _purge()


def _purge():
    import shutil
    for identity_id in _identity_ids_for(TEST_PREFIX):
        _sql("DELETE FROM identity_audit_log WHERE identity_id = CAST(:i AS uuid)"
             "   OR related_identity_id = CAST(:i AS uuid)",
             {"i": identity_id}, fetch="none")
        _sql("DELETE FROM identity_embeddings WHERE identity_id = CAST(:i AS uuid)",
             {"i": identity_id}, fetch="none")
        _sql("DELETE FROM identity_images WHERE identity_id = CAST(:i AS uuid)",
             {"i": identity_id}, fetch="none")
        _sql("DELETE FROM identities WHERE id = CAST(:i AS uuid)",
             {"i": identity_id}, fetch="none")
        folder = os.path.join("/app/storage/faces", identity_id)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)


def _enrol_three_photos(token):
    """A person with a real, multi-photo gallery."""
    name = f"{TEST_PREFIX}{uuid_module.uuid4().hex[:8]}"
    status, first = _upload(token, name, FACE_A, on_decision="create_new")
    assert status in (200, 201), first
    identity_id = first["identity_id"]

    for fixture, filename in ((FACE_B, "b.jpg"), (FACE_C, "c.jpg")):
        status, body = _http(
            "POST", f"/api/identities/{identity_id}/images", token=token,
            fields={"is_face_image": "false"},
            files={"photo": (filename, _read(fixture), "image/jpeg")})
        assert status in (200, 201), body
    return identity_id


def _pipeline_id():
    """A real pipelines row — identity_embeddings.pipeline_id is a FK."""
    existing = _sql("SELECT pipeline_id FROM pipelines LIMIT 1", fetch="scalar")
    if existing:
        return existing
    pipeline_id = f"{TEST_PREFIX}cam"
    _sql("""INSERT INTO pipelines (pipeline_id, name, source_type, created_at)
            VALUES (:p, 'audit camera', 'rtsp', now())
            ON CONFLICT (pipeline_id) DO NOTHING""",
         {"p": pipeline_id}, fetch="none")
    return pipeline_id


def _add_camera_embeddings(identity_id, count, quality=0.99):
    """Rows shaped exactly like the recognition path writes them.

    Provenance is what this test is about: pipeline_id set, image_id NULL —
    the combination db_models documents as camera-derived. The vector itself is
    copied from a real enrollment row so it is a valid 512-dimension unit
    vector rather than something the index would reject.
    """
    pipeline_id = _pipeline_id()
    for index in range(count):
        _sql("""
            INSERT INTO identity_embeddings
                (identity_id, detection_id, pipeline_id, image_id, embedding,
                 quality, faiss_index_type, embedding_model_version,
                 vector_index_sync_state, created_at)
            SELECT identity_id, NULL, :pipeline, NULL, embedding,
                   :quality, 'known', embedding_model_version,
                   'synced', now() - make_interval(mins => :age)
              FROM identity_embeddings
             WHERE identity_id = CAST(:i AS uuid) AND image_id IS NOT NULL
             LIMIT 1
            """,
            {"i": identity_id, "pipeline": pipeline_id,
             "quality": quality, "age": index},
            fetch="none")


def _add_legacy_gallery_embedding(identity_id, quality=0.10):
    """A preloaded/legacy gallery vector: NO image_id and NO pipeline_id.

    db_models documents this state ("preloaded gallery") and
    scripts/backfill_identity_images.py exists to repair it. Trimming on
    image_id alone would classify it as camera traffic and delete it — this
    fixture is what makes that mistake fail the test. Its quality is set low so
    that any ordering-based pruning would reach it first.
    """
    _sql("""
        INSERT INTO identity_embeddings
            (identity_id, detection_id, pipeline_id, image_id, embedding,
             quality, faiss_index_type, embedding_model_version,
             vector_index_sync_state, created_at)
        SELECT identity_id, NULL, NULL, NULL, embedding,
               :quality, 'known', embedding_model_version,
               'synced', now() - interval '400 days'
          FROM identity_embeddings
         WHERE identity_id = CAST(:i AS uuid) AND image_id IS NOT NULL
         LIMIT 1
        """, {"i": identity_id, "quality": quality}, fetch="none")


def _counts(identity_id):
    """Counted by PROVENANCE, the same way the trim classifies rows."""
    row = _sql("""SELECT
                    count(*) FILTER (WHERE image_id IS NOT NULL)              AS gallery,
                    count(*) FILTER (WHERE image_id IS NULL
                                       AND pipeline_id IS NULL)               AS legacy,
                    count(*) FILTER (WHERE image_id IS NULL
                                       AND pipeline_id IS NOT NULL)           AS camera,
                    (SELECT count(*) FROM identity_images
                      WHERE identity_id = CAST(:i AS uuid))                   AS images
                  FROM identity_embeddings
                 WHERE identity_id = CAST(:i AS uuid)""",
               {"i": identity_id}, fetch="one")
    return row


def _run_trim():
    """Invoke the real work function, not a copy of its logic."""
    async def _go():
        from backend.core.identity_retention import identity_retention_manager
        return await identity_retention_manager._cleanup_excess_embeddings()
    return run_async(_go())


def _cap():
    from config import settings
    return int(settings.MAX_EMBEDDINGS_PER_IDENTITY)


def test_mixed_population_above_the_cap_prunes_only_camera_vectors(token):
    """The whole contract in one run: 3 gallery + 1 legacy + 25 camera.

    Every camera vector is scored ABOVE every gallery one and the legacy vector
    is scored lowest, so a trim that ranks the populations together would keep
    camera traffic and delete the curated photos — which is what it used to do.
    """
    identity_id = _enrol_three_photos(token)
    _add_legacy_gallery_embedding(identity_id, quality=0.10)
    _add_camera_embeddings(identity_id, 25, quality=0.99)

    seeded = _counts(identity_id)
    assert (seeded["gallery"], seeded["legacy"], seeded["camera"]) == (3, 1, 25), seeded
    assert seeded["camera"] > _cap(), "the fixture must exceed the cap to prove anything"

    removed = _run_trim()
    after = _counts(identity_id)
    _record("embedding_scope_mixed",
            {"seeded": dict(seeded), "after": dict(after),
             "removed": removed, "cap": _cap()})

    assert after["gallery"] == 3, (
        f"retention pruned enrolled photos' vectors: 3 -> {after['gallery']}. "
        f"Those are the curated gallery, and this module refuses to delete "
        f"their files — deleting their vectors contradicts that.")
    assert after["legacy"] == 1, (
        f"a preloaded/legacy gallery vector (image_id NULL, pipeline_id NULL) "
        f"was pruned as if it were camera traffic: 1 -> {after['legacy']}. "
        f"That is the row scripts/backfill_identity_images.py exists to rescue.")
    assert after["camera"] == _cap(), (
        f"camera vectors were not capped at {_cap()}: {after['camera']} remain. "
        f"Scoping the trim must not disable it.")
    assert after["images"] == 3, "the gallery rows themselves must be untouched"
    assert removed == 25 - _cap(), (seeded, after, removed)


def test_the_surviving_camera_vectors_are_the_best_ones(token):
    """Scoping must not disturb the ranking the cap is supposed to apply."""
    identity_id = _enrol_three_photos(token)
    _add_camera_embeddings(identity_id, 6, quality=0.90)
    _add_camera_embeddings(identity_id, 8, quality=0.20)

    _run_trim()
    surviving = _sql("""SELECT quality FROM identity_embeddings
                         WHERE identity_id = CAST(:i AS uuid)
                           AND image_id IS NULL AND pipeline_id IS NOT NULL
                         ORDER BY quality DESC""", {"i": identity_id})
    qualities = [float(r["quality"]) for r in surviving]
    _record("embedding_scope_ranking", qualities)

    assert len(qualities) == _cap(), qualities
    assert qualities.count(0.90) == 6, (
        f"a higher-quality camera vector was pruned before a lower one: {qualities}")


# ---------------------------------------------------------------------------
# Vector-level verification.
#
# Identity-level recognition is NOT the same claim. `/api/search/by-image`
# returning the right person proves the feature works end to end, but any one
# of that person's vectors could have satisfied the query — it says nothing
# about whether a PARTICULAR surviving embedding is still reachable. These
# tests assert on embedding keys, using the interface that already exposes
# them (`search_similar_embeddings` returns `embedding_id`). No endpoint is
# added for the audit.
# ---------------------------------------------------------------------------

def _search_keys(vector, top_k=50, threshold=0.0):
    """Embedding keys the real search interface returns for a vector."""
    async def _go():
        from db_connection import db_manager
        from backend.core.vector_index.access import search_similar_embeddings
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            hits = await search_similar_embeddings(
                db, vector, top_k=top_k, threshold=threshold)
            return [int(h["embedding_id"]) for h in hits]
    return run_async(_go())


def _stored_vectors(embedding_ids):
    async def _go():
        from db_connection import db_manager
        from backend.core.vector_index.access import load_vectors
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            return await load_vectors(db, list(embedding_ids))
    return run_async(_go())


def test_every_surviving_enrollment_vector_is_individually_retrievable(token):
    """Each surviving gallery vector must be reachable AS ITSELF.

    Queried with its own stored vector, an embedding must come back under its
    own key. Asserting only that the identity is found would pass even if this
    particular vector had been dropped, because a sibling photo of the same
    person would answer the query.
    """
    identity_id = _enrol_three_photos(token)
    _add_camera_embeddings(identity_id, 25, quality=0.99)
    _run_trim()

    gallery = [r["id"] for r in _sql(
        """SELECT id FROM identity_embeddings
            WHERE identity_id = CAST(:i AS uuid) AND image_id IS NOT NULL
            ORDER BY id""", {"i": identity_id})]
    assert len(gallery) == 3, gallery

    vectors = _stored_vectors(gallery)
    unreachable = []
    for embedding_id in gallery:
        vector = vectors.get(embedding_id)
        assert vector is not None, f"embedding {embedding_id} has no stored vector"
        if embedding_id not in _search_keys(vector):
            unreachable.append(embedding_id)

    _record("vector_level_gallery_retrievable",
            {"gallery": gallery, "unreachable": unreachable})
    assert not unreachable, (
        f"{len(unreachable)} surviving enrollment vector(s) are not returned by "
        f"a search for their own vector: {unreachable}. The row exists but does "
        f"not participate in recognition.")


def test_deleted_camera_vectors_no_longer_participate_in_search(token):
    """A trimmed key must be unreachable, not merely absent from the table."""
    identity_id = _enrol_three_photos(token)
    _add_camera_embeddings(identity_id, 25, quality=0.99)

    before = {r["id"] for r in _sql(
        "SELECT id FROM identity_embeddings WHERE identity_id = CAST(:i AS uuid)",
        {"i": identity_id})}
    # Capture the vectors BEFORE the trim, so a deleted key can be searched for
    # with the very vector it used to hold.
    vectors = _stored_vectors(before)
    _run_trim()
    after = {r["id"] for r in _sql(
        "SELECT id FROM identity_embeddings WHERE identity_id = CAST(:i AS uuid)",
        {"i": identity_id})}

    deleted = sorted(before - after)
    assert deleted, "nothing was trimmed; the fixture must exceed the cap"

    still_reachable = []
    for embedding_id in deleted[:5]:            # a sample is enough and is fast
        vector = vectors.get(embedding_id)
        if vector is None:
            continue
        if embedding_id in _search_keys(vector):
            still_reachable.append(embedding_id)

    _record("vector_level_deleted_unreachable",
            {"deleted": deleted, "still_reachable": still_reachable})
    assert not still_reachable, (
        f"deleted embedding(s) still returned by search: {still_reachable}. "
        f"The database and the search path disagree.")


def test_pgvector_keeps_no_independently_persisted_vector_state(token):
    """The invariant, stated semantically rather than as a return value.

    Under pgvector PostgreSQL is the authoritative vector store: the searchable
    entry IS the `identity_embeddings` row, so there is no separate persisted
    index that can drift from the table. This asserts that property through
    behaviour — delete a row directly, and it must stop being searchable with
    no index maintenance of any kind.

    Deliberately NOT asserted: that `PgVectorIndex.remove()` returns 0 forever.
    That is today's implementation of the invariant, not the invariant, and a
    semantically correct refactor must not fail here merely for returning a
    different count.
    """
    from config import settings
    if str(settings.VECTOR_BACKEND).lower() != "pgvector":
        pytest.skip(f"backend is {settings.VECTOR_BACKEND}, not pgvector")

    identity_id = _enrol_three_photos(token)
    gallery = [r["id"] for r in _sql(
        """SELECT id FROM identity_embeddings
            WHERE identity_id = CAST(:i AS uuid) AND image_id IS NOT NULL
            ORDER BY id""", {"i": identity_id})]
    victim = gallery[-1]
    vector = _stored_vectors([victim])[victim]

    assert victim in _search_keys(vector), "precondition: the vector is searchable"

    # A bare DELETE, with no index call whatsoever.
    _sql("DELETE FROM identity_embeddings WHERE id = :e", {"e": victim}, fetch="none")

    still = victim in _search_keys(vector)
    _record("pgvector_authoritative_store",
            {"deleted_row": victim, "still_searchable": still})
    assert not still, (
        "a row deleted straight from identity_embeddings is still returned by "
        "search, so pgvector is NOT the authoritative store and separate vector "
        "state exists that the retention trim would have to maintain")


def test_unit_context_index_probe_states_what_it_saw():
    """In a process with no lifespan, get_vector_index() is legitimately None.

    Recorded explicitly so that a `None` here is never mistaken for evidence
    about the serving process — and so this can never pass silently.
    """
    async def _probe():
        from backend.core.vector_index.access import get_vector_index
        index = get_vector_index()
        return None if index is None else type(index).__name__

    seen = run_async(_probe())
    _record("index_manager_in_pytest_process", seen)

    if seen is None:
        pytest.skip(
            "no lifespan has published a vector index in the pytest process, so "
            "this context cannot observe the serving process's index — see "
            "test_live_serving_process_agrees_with_the_database")
    assert seen in ("PgVectorIndex", "FlatFaissIndex"), seen


def test_live_serving_process_agrees_with_the_database(token):
    """Integration evidence from the RUNNING API, not this process.

    A skip in the unit-context probe above does not cover any of this.
    """
    status, health = _http("GET", "/health/detailed", token=token)
    assert status == 200, health
    runtime_backend = health.get("runtime", {}).get("vector_backend")

    identity_id = _enrol_three_photos(token)
    _add_camera_embeddings(identity_id, 25, quality=0.99)
    _run_trim()

    # The serving process must still recognise the person from each enrolled
    # photo. This is identity-level, end-to-end evidence.
    recognised = {}
    for fixture, label in ((FACE_A, "face_a"), (FACE_B, "face_b"), (FACE_C, "face_c")):
        status, rows = _http("POST", "/api/search/by-image", token=token,
                             fields={"threshold": "0.3", "limit": "10"},
                             files={"image": (f"{label}.jpg", _read(fixture),
                                              "image/jpeg")})
        assert status == 200, rows
        recognised[label] = any(
            isinstance(r, dict) and r.get("identity_id") == identity_id
            for r in (rows if isinstance(rows, list) else []))

    counts = _counts(identity_id)
    _record("live_serving_process",
            {"runtime_vector_backend": runtime_backend,
             "recognised": recognised, "counts": dict(counts)})

    assert runtime_backend == "pgvector", (
        f"the serving process reports vector_backend={runtime_backend!r}")
    assert all(recognised.values()), (
        f"the running API no longer recognises the person from every enrolled "
        f"photo after trimming: {recognised}")
    assert counts["gallery"] == 3 and counts["camera"] == _cap(), dict(counts)


def test_a_gallery_photo_always_has_a_usable_vector(token):
    """The invariant the old behaviour broke: every gallery photo stays
    searchable for as long as it is in the gallery."""
    identity_id = _enrol_three_photos(token)
    _add_camera_embeddings(identity_id, 30, quality=0.99)
    _run_trim()

    orphaned = _sql("""SELECT m.id, m.storage_path
                         FROM identity_images m
                    LEFT JOIN identity_embeddings e ON e.image_id = m.id
                        WHERE m.identity_id = CAST(:i AS uuid)
                          AND e.id IS NULL""",
                    {"i": identity_id})
    _record("gallery_photos_without_vectors", orphaned)

    assert not orphaned, (
        f"{len(orphaned)} gallery photo(s) are still displayed but have no "
        f"embedding, so they contribute nothing to recognition: {orphaned}")
