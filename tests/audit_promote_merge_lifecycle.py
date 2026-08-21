"""Promote and merge: what happens to a person's photos and vectors — each case.

Run explicitly:

    docker exec -w /app <api> python -m pytest tests/audit_promote_merge_lifecycle.py -v

THE CLAIMS UNDER TEST (from reading the code; here they are executed):

Promote (identity_service.promote_unknown_to_known):
  "promotion is a database fact" — identities.type flips, faiss_index_type is
  relabelled, and NO vector is moved, rebuilt or re-keyed.

Merge (identity_service.merge_identities):
  embeddings are repointed by one UPDATE (never copied), provenance columns
  survive, unique photos are consolidated into the winner's folder, byte-
  identical photos dedupe by checksum, the loser becomes a restorable
  tombstone, and unmerge puts every row back.

Every test builds its own people (prefix qa_pm_) and the module removes them.
"""
import hashlib
import json
import os
import uuid as uuid_module

import pytest

from conftest import run_on_shared_loop as run_async

from audit_add_person_matrix import (
    FACE_A, FACE_B, FACE_C, _http, _read, _sql, _upload)
from audit_embedding_retention_scope import (
    _search_keys, _stored_vectors)

TEST_PREFIX = "qa_pm_"
QA_PIPELINE = "qa-pm-cam"
STORAGE_DIR = "/app/storage"
FACES_DIR = f"{STORAGE_DIR}/faces"

EVIDENCE = {}


def _record(name, payload):
    EVIDENCE[name] = payload
    return payload


@pytest.fixture(scope="module")
def token():
    status, body = _http("POST", "/api/auth/login",
                         body={"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


# ---------------------------------------------------------------------------
# builders / cleanup
# ---------------------------------------------------------------------------

def _delete_identity(identity_id):
    import shutil
    for statement in (
        "UPDATE identities SET merged_into_id = NULL WHERE merged_into_id = CAST(:i AS uuid)",
        "DELETE FROM identity_merges WHERE from_identity_id = CAST(:i AS uuid) OR to_identity_id = CAST(:i AS uuid)",
        "DELETE FROM identity_audit_log WHERE identity_id = CAST(:i AS uuid) OR related_identity_id = CAST(:i AS uuid)",
        "DELETE FROM identity_embeddings WHERE identity_id = CAST(:i AS uuid)",
        "DELETE FROM identity_images WHERE identity_id = CAST(:i AS uuid)",
        "DELETE FROM identity_appearances WHERE identity_id = CAST(:i AS uuid)",
        "DELETE FROM identities WHERE id = CAST(:i AS uuid)",
    ):
        _sql(statement, {"i": identity_id}, fetch="none")
    folder = os.path.join(FACES_DIR, str(identity_id))
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)


def _cleanup():
    rows = _sql("SELECT id::text AS id FROM identities WHERE display_name LIKE :p",
                {"p": TEST_PREFIX + "%"})
    for row in rows:
        _delete_identity(row["id"])
    _sql("DELETE FROM identity_appearances WHERE pipeline_id = :p",
         {"p": QA_PIPELINE}, fetch="none")
    _sql("DELETE FROM pipelines WHERE pipeline_id = :p", {"p": QA_PIPELINE}, fetch="none")


@pytest.fixture(scope="module", autouse=True)
def _clean_module():
    _cleanup()
    yield
    _cleanup()
    os.makedirs("/app/logs/audit/add-person", exist_ok=True)
    with open("/app/logs/audit/add-person/evidence_promote_merge.json", "w") as handle:
        json.dump(EVIDENCE, handle, indent=2, default=str)


def _person(token, label, fixtures):
    """A KNOWN person enrolled through the real endpoint, extra photos by id."""
    name = f"{TEST_PREFIX}{label}_{uuid_module.uuid4().hex[:6]}"
    status, first = _upload(token, name, fixtures[0], on_decision="create_new")
    assert status in (200, 201), first
    identity_id = first["identity_id"]
    for index, fixture in enumerate(fixtures[1:], start=2):
        status, body = _http(
            "POST", f"/api/identities/{identity_id}/images", token=token,
            fields={"is_face_image": "false"},
            files={"photo": (f"p{index}.jpg", _read(fixture), "image/jpeg")})
        assert status in (200, 201), body
    return identity_id, name


def _unknown(label, donor_identity_id, camera_vectors=2):
    """An ACTIVE UNKNOWN with real (donor-copied) camera vectors + a snapshot.

    Camera provenance exactly as the recognition path writes it: pipeline_id
    set, image_id NULL, faiss_index_type='unknown'.
    """
    name = f"{TEST_PREFIX}{label}_{uuid_module.uuid4().hex[:6]}"
    identity_id = _sql(
        "INSERT INTO identities (id, type, status, display_name, first_seen_at, "
        " last_seen_at, created_at, updated_at, appearances_count) "
        "VALUES (gen_random_uuid(), 'UNKNOWN', 'ACTIVE', :n, now(), now(), now(), now(), 1) "
        "RETURNING id::text", {"n": name}, fetch="scalar")
    _sql("INSERT INTO pipelines (pipeline_id, created_at, is_active) "
         "VALUES (:p, now(), 1) ON CONFLICT (pipeline_id) DO NOTHING",
         {"p": QA_PIPELINE}, fetch="none")
    _sql("INSERT INTO identity_appearances (identity_id, pipeline_id, start_time, created_at) "
         "VALUES (CAST(:i AS uuid), :p, now(), now())",
         {"i": identity_id, "p": QA_PIPELINE}, fetch="none")
    for age in range(camera_vectors):
        _sql("""INSERT INTO identity_embeddings
                    (identity_id, detection_id, pipeline_id, image_id, embedding,
                     quality, faiss_index_type, embedding_model_version,
                     vector_index_sync_state, created_at)
                SELECT CAST(:i AS uuid), NULL, :p, NULL, embedding,
                       0.6, 'unknown', embedding_model_version, 'synced',
                       now() - make_interval(mins => :age)
                  FROM identity_embeddings
                 WHERE identity_id = CAST(:d AS uuid) AND image_id IS NOT NULL
                 LIMIT 1""",
             {"i": identity_id, "d": donor_identity_id, "p": QA_PIPELINE,
              "age": age}, fetch="none")
    return identity_id, name


def _embeddings(identity_id):
    return _sql("""SELECT id, image_id, pipeline_id, faiss_index_type, quality
                     FROM identity_embeddings
                    WHERE identity_id = CAST(:i AS uuid) ORDER BY id""",
                {"i": identity_id})


def _merge(token, from_id, to_id):
    return _http("POST", "/api/admin/identities/merge", token=token,
                 body={"from_identity_id": from_id, "to_identity_id": to_id,
                       "notes": "qa_pm lifecycle", "decision": "merge_existing",
                       "confirm_merge_risk": True})


def _merge_id(from_id):
    return _sql("SELECT id FROM identity_merges WHERE from_identity_id = CAST(:i AS uuid) "
                "ORDER BY id DESC LIMIT 1", {"i": from_id}, fetch="scalar")


# ===========================================================================
# PROMOTE
# ===========================================================================

def test_promote_keeps_every_embedding_row_and_vector(token):
    """P1 — 'promotion is a database fact': same rows, same vectors, new label."""
    donor_id, _ = _person(token, "donor", [FACE_A])
    unknown_id, _ = _unknown("prom", donor_id, camera_vectors=3)

    before = _embeddings(unknown_id)
    before_ids = [row["id"] for row in before]
    vector_before = _stored_vectors([before_ids[0]])[before_ids[0]]
    assert all(row["faiss_index_type"] == "unknown" for row in before), before

    status, body = _http(
        "POST", f"/api/admin/unknown/{unknown_id}/promote", token=token,
        body={"display_name": f"{TEST_PREFIX}promoted_{uuid_module.uuid4().hex[:6]}",
              "decision": "create_new"})
    assert status == 200 and body.get("success"), (status, body)

    after = _embeddings(unknown_id)
    identity = _sql("""SELECT type::text AS type, status::text AS status
                         FROM identities WHERE id = CAST(:i AS uuid)""",
                    {"i": unknown_id}, fetch="one")
    vector_after = _stored_vectors([before_ids[0]])[before_ids[0]]
    _record("promote_rows", {"before": before, "after": after, "identity": identity})

    assert identity["type"].upper().endswith("KNOWN"), identity
    assert [row["id"] for row in after] == before_ids, (
        "promotion changed the embedding row set — vectors were moved or "
        "rebuilt, but promotion must be a pure database fact")
    assert all(row["faiss_index_type"] == "known" for row in after), (
        f"labels not flipped to 'known': {after}")
    assert (vector_before == vector_after).all(), (
        "an embedding VECTOR changed during promotion")
    # Camera provenance must survive: these are still camera observations.
    assert all(row["pipeline_id"] == QA_PIPELINE and row["image_id"] is None
               for row in after), after


def test_promoted_person_is_searchable_at_vector_level(token):
    """P2 — the same keys answer searches the moment the promotion commits."""
    donor_id, _ = _person(token, "donor2", [FACE_B])
    unknown_id, _ = _unknown("prom2", donor_id, camera_vectors=2)
    keys = [row["id"] for row in _embeddings(unknown_id)]

    status, body = _http(
        "POST", f"/api/admin/unknown/{unknown_id}/promote", token=token,
        body={"display_name": f"{TEST_PREFIX}promoted2_{uuid_module.uuid4().hex[:6]}",
              "decision": "create_new"})
    assert status == 200 and body.get("success"), (status, body)

    vector = _stored_vectors([keys[0]])[keys[0]]
    found = _search_keys(vector)
    _record("promote_search", {"keys": keys, "found": found[:10]})
    assert keys[0] in found, (
        "a promoted person's embedding key is not returned by search for its "
        "own vector — promotion broke reachability")


def test_promote_refuses_an_already_known_identity(token):
    """P3 — re-promoting is a business refusal, not a silent success."""
    known_id, _ = _person(token, "known", [FACE_C])
    status, body = _http(
        "POST", f"/api/admin/unknown/{known_id}/promote", token=token,
        body={"display_name": f"{TEST_PREFIX}nope", "decision": "create_new"})
    _record("promote_already_known", {"status": status, "body": body})
    assert status == 400, (status, body)


def test_promote_refuses_a_duplicate_person_code(token):
    """P4 — person_code is unique among the living; the second claim fails."""
    donor_id, _ = _person(token, "donor3", [FACE_A])
    first_id, _ = _unknown("code1", donor_id)
    second_id, _ = _unknown("code2", donor_id)
    code = f"QA-PM-{uuid_module.uuid4().hex[:6].upper()}"

    status, body = _http(
        "POST", f"/api/admin/unknown/{first_id}/promote", token=token,
        body={"display_name": f"{TEST_PREFIX}coded1", "person_code": code,
              "decision": "create_new"})
    assert status == 200 and body.get("success"), (status, body)

    status, body = _http(
        "POST", f"/api/admin/unknown/{second_id}/promote", token=token,
        body={"display_name": f"{TEST_PREFIX}coded2", "person_code": code,
              "decision": "create_new"})
    _record("promote_duplicate_code", {"status": status, "body": body})
    assert status in (400, 409), (
        f"the same person_code was accepted twice: {status} {body}")


# ===========================================================================
# MERGE
# ===========================================================================

def test_merge_repoints_embeddings_without_copying(token):
    """M1 — one UPDATE moves ownership; row ids and vectors are untouched."""
    winner_id, _ = _person(token, "win", [FACE_A, FACE_B])
    loser_id, _ = _person(token, "lose", [FACE_C])

    winner_before = [row["id"] for row in _embeddings(winner_id)]
    loser_before = [row["id"] for row in _embeddings(loser_id)]
    loser_vector = _stored_vectors([loser_before[0]])[loser_before[0]]

    status, body = _merge(token, loser_id, winner_id)
    assert status == 200 and body.get("success"), (status, body)

    winner_after = [row["id"] for row in _embeddings(winner_id)]
    loser_after = [row["id"] for row in _embeddings(loser_id)]
    vector_after = _stored_vectors([loser_before[0]])[loser_before[0]]
    _record("merge_repoint", {"winner_before": winner_before,
                              "loser_before": loser_before,
                              "winner_after": winner_after,
                              "loser_after": loser_after})

    assert loser_after == [], "the loser still owns embeddings after the merge"
    assert sorted(winner_after) == sorted(winner_before + loser_before), (
        "the winner's embeddings are not exactly winner+loser: rows were "
        "created or lost — repointing must neither copy nor drop")
    assert (loser_vector == vector_after).all(), "a vector changed during merge"


def test_merge_preserves_provenance_columns(token):
    """M2 — camera stays camera, gallery stays gallery, across the merge.
    The retention trim classifies by these columns, so corrupting them here
    would silently change what retention may delete."""
    winner_id, _ = _person(token, "win2", [FACE_A])
    loser_id, _ = _person(token, "lose2", [FACE_B])
    # give the loser a camera vector too
    _sql("""INSERT INTO pipelines (pipeline_id, created_at, is_active)
            VALUES (:p, now(), 1) ON CONFLICT (pipeline_id) DO NOTHING""",
         {"p": QA_PIPELINE}, fetch="none")
    _sql("""INSERT INTO identity_embeddings
                (identity_id, detection_id, pipeline_id, image_id, embedding,
                 quality, faiss_index_type, embedding_model_version,
                 vector_index_sync_state, created_at)
            SELECT identity_id, NULL, :p, NULL, embedding, 0.5, 'known',
                   embedding_model_version, 'synced', now()
              FROM identity_embeddings
             WHERE identity_id = CAST(:i AS uuid) AND image_id IS NOT NULL
             LIMIT 1""", {"i": loser_id, "p": QA_PIPELINE}, fetch="none")

    before = {row["id"]: row for row in _embeddings(loser_id)}
    status, body = _merge(token, loser_id, winner_id)
    assert status == 200 and body.get("success"), (status, body)

    after = {row["id"]: row for row in _embeddings(winner_id)}
    _record("merge_provenance", {"loser_before": list(before.values()),
                                 "winner_after": list(after.values())})

    for embedding_id, row in before.items():
        moved = after.get(embedding_id)
        assert moved is not None, f"embedding {embedding_id} vanished in the merge"
        assert moved["pipeline_id"] == row["pipeline_id"], (embedding_id, row, moved)
        assert moved["quality"] == row["quality"], (embedding_id, row, moved)
        # gallery embeddings keep an image link; M4 covers the dedup rewrite
        if row["image_id"] is not None:
            assert moved["image_id"] is not None, (
                f"gallery embedding {embedding_id} lost its image link")


def test_merge_moves_unique_photos_and_demotes_their_primary(token):
    """M3 — a photo the winner does not own is consolidated into the winner's
    folder; the winner's own primary survives."""
    winner_id, _ = _person(token, "win3", [FACE_A])
    loser_id, _ = _person(token, "lose3", [FACE_C])

    status, body = _merge(token, loser_id, winner_id)
    assert status == 200 and body.get("success"), (status, body)

    images = _sql("""SELECT id, identity_id::text AS owner, storage_path,
                            is_primary, file_checksum
                       FROM identity_images
                      WHERE identity_id = CAST(:w AS uuid)
                      ORDER BY id""", {"w": winner_id})
    _record("merge_images", images)

    moved = [row for row in images if hashlib.sha256(_read(FACE_C)).hexdigest()
             == row["file_checksum"]]
    assert len(moved) == 1, f"the loser's photo did not arrive: {images}"
    assert moved[0]["is_primary"] is False, "the moved photo stole primary"
    assert f"faces/{winner_id}/" in moved[0]["storage_path"], (
        f"the moved row still points into the loser's folder: {moved[0]}")
    absolute = os.path.join(STORAGE_DIR,
                            moved[0]["storage_path"].split("storage/", 1)[1])
    assert os.path.isfile(absolute), f"consolidated file missing: {absolute}"
    assert hashlib.sha256(_read(absolute)).hexdigest() == moved[0]["file_checksum"]

    primaries = [row for row in images if row["is_primary"]]
    assert len(primaries) == 1, images
    assert primaries[0]["file_checksum"] == hashlib.sha256(_read(FACE_A)).hexdigest(), (
        "the winner's own primary did not survive the merge")


def test_merge_dedupes_byte_identical_photos_by_checksum(token):
    """M4 — both people own the exact same file: the duplicate row stays on
    the loser and its embedding is repointed at the winner's copy."""
    winner_id, _ = _person(token, "win4", [FACE_A])
    loser_id, _ = _person(token, "lose4", [FACE_C])
    # the loser also gets the winner's exact photo
    status, body = _http(
        "POST", f"/api/identities/{loser_id}/images", token=token,
        fields={"is_face_image": "false"},
        files={"photo": ("dup.jpg", _read(FACE_A), "image/jpeg")})
    assert status in (200, 201), body
    dup_image_id = body["image_id"]

    winner_copy = _sql("""SELECT id FROM identity_images
                           WHERE identity_id = CAST(:w AS uuid)
                             AND file_checksum = :c""",
                       {"w": winner_id,
                        "c": hashlib.sha256(_read(FACE_A)).hexdigest()},
                       fetch="scalar")

    status, body = _merge(token, loser_id, winner_id)
    assert status == 200 and body.get("success"), (status, body)

    dup_row = _sql("""SELECT identity_id::text AS owner FROM identity_images
                       WHERE id = :i""", {"i": dup_image_id}, fetch="one")
    repointed = _sql("""SELECT image_id FROM identity_embeddings
                         WHERE image_id IN (:a, :b)
                           AND identity_id = CAST(:w AS uuid)""",
                     {"a": dup_image_id, "b": winner_copy, "w": winner_id})
    _record("merge_dedup", {"dup_row_owner": dup_row, "winner_copy": winner_copy,
                            "repointed": repointed})

    assert dup_row["owner"] == loser_id, (
        "the byte-identical duplicate row was moved onto the winner — that "
        "would violate uq_identity_image_checksum")
    assert all(row["image_id"] == winner_copy for row in repointed), (
        f"an embedding still points at the loser's duplicate copy: {repointed}")


def test_merge_leaves_a_tombstone_excluded_from_search(token):
    """M5 — the loser is MERGED + merged_into_id, and can no longer surface."""
    winner_id, _ = _person(token, "win5", [FACE_A])
    loser_id, _ = _person(token, "lose5", [FACE_C])
    loser_keys = [row["id"] for row in _embeddings(loser_id)]
    loser_vector = _stored_vectors([loser_keys[0]])[loser_keys[0]]

    status, body = _merge(token, loser_id, winner_id)
    assert status == 200 and body.get("success"), (status, body)

    tombstone = _sql("""SELECT status::text AS status,
                               merged_into_id::text AS merged_into
                          FROM identities WHERE id = CAST(:i AS uuid)""",
                     {"i": loser_id}, fetch="one")
    assert tombstone["status"].upper().endswith("MERGED"), tombstone
    assert tombstone["merged_into"] == winner_id, tombstone

    # The vector is still reachable — under the WINNER, never the tombstone.
    owner = _sql("""SELECT identity_id::text AS owner FROM identity_embeddings
                     WHERE id = :e""", {"e": loser_keys[0]}, fetch="one")
    assert owner["owner"] == winner_id, owner
    found = _search_keys(loser_vector)
    _record("merge_tombstone", {"tombstone": tombstone, "found": found[:10]})
    assert loser_keys[0] in found, (
        "the merged person's vector stopped answering searches — merging must "
        "transfer recognition, not delete it")


def test_merging_an_unknown_into_a_known_relabels_its_vectors(token):
    """M6 — the unknown's vectors become 'known' when absorbed by a person."""
    winner_id, _ = _person(token, "win6", [FACE_A])
    unknown_id, _ = _unknown("lose6", winner_id, camera_vectors=2)
    unknown_keys = [row["id"] for row in _embeddings(unknown_id)]

    status, body = _merge(token, unknown_id, winner_id)
    assert status == 200 and body.get("success"), (status, body)

    rows = _sql("""SELECT id, faiss_index_type, pipeline_id, image_id
                     FROM identity_embeddings WHERE id = ANY(:k)""",
                {"k": unknown_keys})
    _record("merge_unknown_relabel", rows)
    assert all(row["faiss_index_type"] == "known" for row in rows), rows
    # provenance still camera
    assert all(row["pipeline_id"] == QA_PIPELINE and row["image_id"] is None
               for row in rows), rows


def test_merge_takes_the_latest_real_sighting(token):
    """M7 — winner.last_seen_at becomes max(both), because both are real."""
    winner_id, _ = _person(token, "win7", [FACE_A])
    loser_id, _ = _person(token, "lose7", [FACE_C])
    _sql("UPDATE identities SET last_seen_at = now() - interval '10 days' "
         "WHERE id = CAST(:i AS uuid)", {"i": winner_id}, fetch="none")
    _sql("UPDATE identities SET last_seen_at = now() - interval '2 days' "
         "WHERE id = CAST(:i AS uuid)", {"i": loser_id}, fetch="none")
    loser_seen = _sql("SELECT last_seen_at FROM identities WHERE id = CAST(:i AS uuid)",
                      {"i": loser_id}, fetch="scalar")

    status, body = _merge(token, loser_id, winner_id)
    assert status == 200 and body.get("success"), (status, body)

    winner_seen = _sql("SELECT last_seen_at FROM identities WHERE id = CAST(:i AS uuid)",
                       {"i": winner_id}, fetch="scalar")
    _record("merge_last_seen", {"loser": loser_seen, "winner_after": winner_seen})
    assert winner_seen == loser_seen, (
        f"winner.last_seen_at should be the max of both ({loser_seen}), "
        f"got {winner_seen}")


def test_unmerge_restores_the_loser_exactly(token):
    """M8 — the provenance snapshot puts every row back where it was."""
    winner_id, _ = _person(token, "win8", [FACE_A])
    loser_id, _ = _person(token, "lose8", [FACE_C])
    loser_before = [row["id"] for row in _embeddings(loser_id)]

    status, body = _merge(token, loser_id, winner_id)
    assert status == 200 and body.get("success"), (status, body)
    merge_id = _merge_id(loser_id)
    assert merge_id, "no identity_merges row was written"

    status, body = _http("POST",
                         f"/api/admin/identities/merges/{merge_id}/unmerge",
                         token=token, body={})
    assert status == 200 and body.get("success"), (status, body)

    restored = _sql("""SELECT type::text AS type, status::text AS status,
                              merged_into_id
                         FROM identities WHERE id = CAST(:i AS uuid)""",
                    {"i": loser_id}, fetch="one")
    loser_after = [row["id"] for row in _embeddings(loser_id)]
    winner_after = [row["id"] for row in _embeddings(winner_id)]
    _record("unmerge", {"restored": restored,
                        "loser_before": loser_before, "loser_after": loser_after,
                        "winner_after": winner_after})

    assert restored["status"].upper().endswith("ACTIVE"), (
        f"unmerge did not restore the loser's prior status: {restored}")
    assert restored["merged_into_id"] is None, restored
    assert sorted(loser_after) == sorted(loser_before), (
        "unmerge did not return exactly the loser's embeddings: "
        f"{loser_before} -> {loser_after}")
    assert not set(winner_after) & set(loser_before), (
        "the winner still owns some of the loser's embeddings after unmerge")


def test_unmerge_reverses_the_consolidation_too(token):
    """M9 — the fix's own symmetry: timestamps and labels return on unmerge.

    Merge widens the winner's sighting window and relabels absorbed 'unknown'
    vectors; unmerge must put BOTH back, or an unmerged pair keeps traces of a
    merge that officially never happened.
    """
    winner_id, _ = _person(token, "win9", [FACE_A])
    unknown_id, _ = _unknown("lose9", winner_id, camera_vectors=2)
    unknown_keys = [row["id"] for row in _embeddings(unknown_id)]

    _sql("UPDATE identities SET last_seen_at = now() - interval '30 days', "
         " first_seen_at = now() - interval '60 days' WHERE id = CAST(:i AS uuid)",
         {"i": winner_id}, fetch="none")
    winner_before = _sql("""SELECT first_seen_at, last_seen_at FROM identities
                             WHERE id = CAST(:i AS uuid)""",
                         {"i": winner_id}, fetch="one")

    status, body = _merge(token, unknown_id, winner_id)
    assert status == 200 and body.get("success"), (status, body)

    merged = _sql("""SELECT first_seen_at, last_seen_at FROM identities
                      WHERE id = CAST(:i AS uuid)""", {"i": winner_id}, fetch="one")
    labels_merged = _sql("SELECT faiss_index_type FROM identity_embeddings "
                         "WHERE id = ANY(:k)", {"k": unknown_keys})
    assert merged["last_seen_at"] > winner_before["last_seen_at"], (
        "precondition: the merge should have widened the window")
    assert all(r["faiss_index_type"] == "known" for r in labels_merged), (
        "precondition: the merge should have relabelled the absorbed vectors")

    merge_id = _merge_id(unknown_id)
    status, body = _http("POST",
                         f"/api/admin/identities/merges/{merge_id}/unmerge",
                         token=token, body={})
    assert status == 200 and body.get("success"), (status, body)

    winner_after = _sql("""SELECT first_seen_at, last_seen_at FROM identities
                            WHERE id = CAST(:i AS uuid)""",
                        {"i": winner_id}, fetch="one")
    labels_after = _sql("SELECT faiss_index_type FROM identity_embeddings "
                        "WHERE id = ANY(:k)", {"k": unknown_keys})
    _record("unmerge_consolidation", {
        "winner_before": winner_before, "merged": merged,
        "winner_after": winner_after,
        "labels_after": [r["faiss_index_type"] for r in labels_after]})

    assert winner_after["last_seen_at"] == winner_before["last_seen_at"], (
        f"unmerge left the winner with the loser's sighting window: "
        f"{winner_before['last_seen_at']} -> {winner_after['last_seen_at']}")
    assert winner_after["first_seen_at"] == winner_before["first_seen_at"], winner_after
    assert all(r["faiss_index_type"] == "unknown" for r in labels_after), (
        f"unmerge left absorbed vectors labelled 'known' on an UNKNOWN "
        f"identity: {labels_after}")
