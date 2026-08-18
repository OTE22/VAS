"""An uploaded face cannot be reported as the wrong person at 100%.

    docker exec face_recognition_api python -m pytest tests/test_upload_match_integrity.py -v

THE REPORT was "an unrelated image shows as a 100% match". The similarity maths
turned out to be correct — measured live, an identical file scores 1.000000 and
unrelated faces score 0.04-0.13 — so these tests pin the maths in place AND the
four real defects that let a correct score describe the wrong person:

  * `best_snapshot_path` was replaced whenever `similarity > 0.0`, i.e. always,
    so an identity's displayed face became whatever arrived last;
  * auto-enrichment wrote runtime observations into a matched identity at an
    effective attach floor of 0.30;
  * a zero-magnitude vector could be STORED, and `1 - (zero <=> q)` is NaN —
    which PostgreSQL orders ABOVE every real number, so it passed every
    threshold in every search;
  * `/api/search/advanced` had no display floor, only a 0.2 retrieval floor.

The NaN behaviour is the subtle one and is asserted directly against the
deployed database rather than assumed: PostgreSQL has no `isnan()`, and
`x <> x` is FALSE there, so the guard is an upper bound.
"""

import json
import logging
import math
import os
import re
import urllib.error
import urllib.request
import uuid as uuid_module

import pytest

from conftest import run_on_shared_loop as run_async

BASE = "http://localhost:8000"
FIXTURES = "/app/tests/fixtures/faces"
FACE_A = f"{FIXTURES}/face_a.jpg"
FACE_B = f"{FIXTURES}/face_b.jpg"
FACE_C = f"{FIXTURES}/face_c.png"

SERVICE_SRC = "/app/backend/core/advanced_search.py"
IDENTITY_SRC = "/app/backend/core/identity_service.py"
PGVECTOR_SRC = "/app/backend/core/identity_index_pgvector.py"
SEARCH_JS = "/app/frontend/js/admin-search.js"


def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _source(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _sql(statement, params=None, fetch="scalar"):
    from sqlalchemy import text
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            result = await db.execute(text(statement), params or {})
            if fetch == "scalar":
                return result.scalar()
            if fetch == "none":
                await db.commit()
                return None
            return result.all()
    return run_async(_run())


def _http(method, path, *, body=None, token=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except Exception:
            return exc.code, {"_raw": raw.decode(errors="replace")}


def _embedding(fixture):
    from backend.core import model_manager
    from backend.core.enrollment_service import prepare_upload

    model_manager.initialize()
    return prepare_upload(_read(fixture),
                          original_filename=os.path.basename(fixture)
                          ).embedding_normalized


# ---------------------------------------------------------------------------
# The maths
# ---------------------------------------------------------------------------

def test_identical_embeddings_score_one():
    import numpy as np

    vector = _embedding(FACE_A)
    assert abs(float(np.dot(vector, vector)) - 1.0) < 1e-4
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-4, (
        "query embeddings must be L2-normalized or cosine is not cosine")


def test_unrelated_faces_are_nowhere_near_one():
    """THE headline assertion. Two different people must not look identical."""
    import numpy as np

    from config import settings

    a, c = _embedding(FACE_A), _embedding(FACE_C)
    similarity = float(np.dot(a, c))

    assert similarity < 0.5, (
        f"two different people score {similarity:.4f} — at that level the "
        "matcher cannot tell them apart")
    assert similarity < float(settings.SIMILARITY_THRESHOLD), (
        f"two different people score {similarity:.4f}, at or above the "
        f"configured match threshold {settings.SIMILARITY_THRESHOLD}")


def test_cosine_distance_is_converted_exactly_once():
    """`1 - distance` applied zero or twice would invert or flatten the score.

    Compares what the database returns against numpy for the same pair, so a
    second conversion sneaking into any query is caught by arithmetic rather
    than by reading SQL.
    """
    import numpy as np

    a, c = _embedding(FACE_A), _embedding(FACE_C)
    expected = float(np.dot(a, c))

    literal_a = "[" + ",".join(f"{float(x):.8f}" for x in a) + "]"
    literal_c = "[" + ",".join(f"{float(x):.8f}" for x in c) + "]"
    from_sql = float(_sql(
        "SELECT 1 - (CAST(:a AS vector) <=> CAST(:c AS vector))",
        {"a": literal_a, "c": literal_c}))

    assert abs(from_sql - expected) < 1e-5, (
        f"SQL says {from_sql:.6f}, numpy says {expected:.6f} — the "
        "distance-to-similarity conversion does not match")


def test_no_face_search_query_uses_an_l2_or_inner_product_operator():
    """`<->` is L2 and `<#>` is negative inner product; neither is cosine."""
    for path in (PGVECTOR_SRC, "/app/backend/core/vector_index/access.py",
                 "/app/backend/core/vector_index/pgvector_index.py"):
        source = _source(path)
        assert "<->" not in source, f"{path} uses the L2 operator for a similarity"
        assert "<#>" not in source, f"{path} uses the inner-product operator"


# ---------------------------------------------------------------------------
# NaN — asserted against the deployed database, not assumed
# ---------------------------------------------------------------------------

def test_postgres_orders_nan_above_every_real_number():
    """The premise the SQL guards rest on. If this ever changes, they change."""
    assert _sql("SELECT ('NaN'::float8 >= 0.4)") is True, (
        "NaN no longer passes a >= threshold; the guards can be simplified")
    assert _sql("SELECT ('NaN'::float8 <> 'NaN'::float8)") is False, (
        "PostgreSQL now follows IEEE for <>; `score <> score` would work")
    assert _sql("SELECT ('NaN'::float8 <= 1.0)") is False, (
        "the upper bound no longer excludes NaN — the guards are ineffective")


def test_a_zero_vector_really_does_produce_nan():
    assert _sql("SELECT (1 - ('[0,0,0]'::vector <=> '[1,0,0]'::vector)) >= 0.4") is True
    assert _sql("SELECT (1 - ('[0,0,0]'::vector <=> '[1,0,0]'::vector)) "
                "BETWEEN 0.4 AND 1.0") is False, (
        "the BETWEEN guard no longer excludes a zero-vector comparison")


def test_every_search_query_bounds_the_score_from_above():
    """`>= :threshold` alone admits NaN. The upper bound is what excludes it."""
    # Comments stripped: this module explains the NaN bug in its own prose, and
    # a raw text scan cannot tell an explanation from a WHERE clause.
    source = "\n".join(line.split("#", 1)[0]
                       for line in _source(PGVECTOR_SRC).splitlines())
    # No similarity may be filtered with a bare lower bound: `NaN >= x` is TRUE
    # in PostgreSQL, so an upper bound is what actually excludes it.
    assert ">= :threshold" not in source, (
        "a similarity is still filtered with a bare lower bound")
    assert source.count("BETWEEN :threshold AND 1.0") >= 4, (
        "not every search query carries the NaN-excluding upper bound")


def test_invalid_scores_are_rejected_in_python_too():
    from backend.core.vector_index.base import usable_score

    assert usable_score(1.0, 0.4) is True
    assert usable_score(0.5, 0.4) is True
    assert usable_score(0.3, 0.4) is False
    assert usable_score(float("nan"), 0.4) is False
    assert usable_score(float("inf"), 0.4) is False
    assert usable_score(float("-inf"), 0.4) is False
    assert usable_score(None, 0.4) is False
    assert usable_score("not a number", 0.4) is False


def test_a_degenerate_embedding_cannot_be_stored():
    """The write path refuses it, so the read path never has to cope."""
    import inspect

    import numpy as np

    from backend.core.identity_index_pgvector import IdentityIndexPgVector

    source = inspect.getsource(IdentityIndexPgVector.add_embedding)
    assert "REFUSED" in source, "add_embedding no longer refuses a bad vector"
    assert "return None" in source
    # The old behaviour, in one line, is what must not come back.
    assert "normalized = embedding.astype(np.float32)" not in source, (
        "a zero-norm embedding is being stored as-is again")


def test_no_stored_embedding_is_degenerate():
    """A completion gate: the live table holds nothing unusable."""
    import numpy as np

    rows = _sql("SELECT id, embedding FROM identity_embeddings "
                "WHERE embedding IS NOT NULL", fetch="all")
    bad = []
    for embedding_id, raw in rows:
        vector = np.fromstring(str(raw).strip("[]"), sep=",", dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if vector.size == 0 or not np.all(np.isfinite(vector)) or abs(norm - 1.0) > 0.01:
            bad.append((embedding_id, norm))
    assert not bad, f"degenerate stored embeddings: {bad}"


# ---------------------------------------------------------------------------
# The four defects that made a correct score describe the wrong person
# ---------------------------------------------------------------------------

def test_a_snapshot_is_not_replaced_by_whatever_arrived_last():
    source = _source(IDENTITY_SRC)
    assert "elif similarity > 0.0:" not in source, (
        "best_snapshot_path is replaced on any positive similarity again — an "
        "identity's displayed face becomes the most recent match, so a correct "
        "100% match can show a different person")
    assert "snapshot_replace_min_similarity" in source


def test_auto_enrichment_is_off_by_default():
    from config import settings

    assert settings.IDENTITY_AUTO_ENRICH_ENABLED is False, (
        "auto-enrichment writes runtime observations permanently into an "
        "enrolled identity; one wrong attribution is then self-reinforcing")
    # The shipped DEFAULT, not the live value: this setting is runtime-editable
    # and a deliberate admin override is not a regression. What must not
    # regress is the default the code ships with.
    from config import Settings
    shipped = float(Settings.model_fields["IDENTITY_ENRICH_MIN_SIMILARITY"].default)
    assert shipped >= 0.75, (
        f"the shipped enrich floor is {shipped}; at 0.55 it sat only 0.15 above "
        "the match threshold itself")


def test_a_face_below_the_threshold_is_not_attached_anyway():
    """The lowered-threshold re-attachment made SIMILARITY_THRESHOLD a lie."""
    source = _source(IDENTITY_SRC)
    assert "self.known_threshold - 0.1" not in source, (
        "recognition re-searches KNOWN at a lower floor and returns that "
        "identity anyway, so the configured threshold does not hold")


def test_the_display_floor_is_the_configured_threshold():
    source = _source(SERVICE_SRC)
    assert "display_floor" in source, "results are shown below the match threshold"
    assert "settings.SIMILARITY_THRESHOLD" in source


def test_by_image_search_uses_configured_thresholds_and_collapses_per_identity():
    source = _source("/app/backend/routes/identities.py")
    assert "threshold=0.4" not in source, "by-image search hardcodes its threshold"
    assert "best_by_identity" in source, (
        "by-image search returns one row per EMBEDDING, so one person can "
        "occupy several of the top_k slots")


# ---------------------------------------------------------------------------
# The [UPLOAD_MATCH] trace
# ---------------------------------------------------------------------------

def test_every_stage_is_emitted_with_one_correlation_id():
    """Drive a REAL request and read the trace back out of logs/app.log.

    Over HTTP rather than in-process: the trace only means anything if it comes
    out of the running server, where the search singleton is initialized and the
    logging pipeline is the configured one. An in-process call skips both.
    """
    import io as _io
    import time

    log_path = "/var/log/face-recognition/app.log"
    if not os.path.isfile(log_path):
        pytest.skip("app.log is not mounted in this container")

    status, body = _http("POST", "/api/auth/login",
                         body={"username": "admin", "password": "admin123"})
    assert status == 200, body
    token = body["access_token"]

    request_id = "itg" + uuid_module.uuid4().hex[:9]
    boundary = "----itg" + uuid_module.uuid4().hex
    crlf = chr(13) + chr(10)
    out = _io.BytesIO()
    out.write(f"--{boundary}{crlf}".encode())
    out.write(('Content-Disposition: form-data; name="image"; '
               f'filename="face_a.jpg"{crlf}'
               f"Content-Type: image/jpeg{crlf}{crlf}").encode())
    out.write(_read(FACE_A))
    out.write(f"{crlf}--{boundary}--{crlf}".encode())

    request = urllib.request.Request(BASE + "/api/search/advanced",
                                     data=out.getvalue(), method="POST")
    request.add_header("Content-Type",
                       f"multipart/form-data; boundary={boundary}")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-Request-ID", request_id)
    with urllib.request.urlopen(request, timeout=180) as response:
        assert response.status == 200

    # The handler is a QueueListener on another thread; give it a moment.
    lines = []
    for _ in range(20):
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            lines = [line for line in handle
                     if "[UPLOAD_MATCH]" in line and request_id in line]
        if any("stage=request_completed" in line for line in lines):
            break
        time.sleep(0.5)

    assert lines, f"no trace was written for request_id={request_id}"
    seen = {re.search(r"stage=(\w+)", line).group(1) for line in lines}

    expected = {"request_started", "file_validated", "image_decoded",
                "face_detected", "embedding_created", "vector_search_started",
                "final_candidates", "request_completed"}
    missing = expected - seen
    assert not missing, f"stages never emitted: {sorted(missing)}"

    from backend.core.advanced_search import UPLOAD_MATCH_STAGES
    assert seen <= set(UPLOAD_MATCH_STAGES), (
        f"undeclared stage emitted: {sorted(seen - set(UPLOAD_MATCH_STAGES))}")

    for line in lines:
        assert f"request_id={request_id}" in line
    for needle in ("/app/", "Bearer ", "password"):
        assert not any(needle in line for line in lines), (
            f"the trace leaked {needle!r}")
    assert not any(re.search(r"[0-9a-f]{64}", line) for line in lines), (
        "a full checksum reached the log")


def test_the_trace_leaks_nothing_sensitive(caplog):
    from backend.core.advanced_search import _match_log

    with caplog.at_level(logging.DEBUG, logger="backend.core.advanced_search"):
        _match_log("raw_candidate", request_id="abc123",
                   identity_id="11111111-2222-3333-4444-555555555555",
                   raw_similarity="0.912345", raw_distance="0.087655")
        _match_log("file_validated", request_id="abc123",
                   filename="photo.jpg", checksum_prefix="0123456789ab")

    blob = "\n".join(record.getMessage() for record in caplog.records)
    for forbidden in ("/app/", "C:\\", "Bearer ", "password", "token="):
        assert forbidden not in blob, f"the trace leaked {forbidden!r}"
    # A full SHA-256 is 64 hex chars; only a short prefix may be logged.
    assert not re.search(r"\b[0-9a-f]{64}\b", blob), "a full checksum was logged"
    # An embedding would appear as a long bracketed float list.
    assert not re.search(r"\[[-0-9.]+,[-0-9.]+,[-0-9.]+,", blob), (
        "a vector was logged")


def test_stage_levels_match_the_agreed_routing():
    from backend.core.advanced_search import UPLOAD_MATCH_LEVELS

    assert UPLOAD_MATCH_LEVELS["raw_candidate"] == logging.DEBUG
    assert UPLOAD_MATCH_LEVELS["candidate_filtered"] == logging.DEBUG
    assert UPLOAD_MATCH_LEVELS["request_failed"] == logging.ERROR
    for stage in ("request_started", "file_validated", "image_decoded",
                  "face_detected", "embedding_created", "vector_search_started",
                  "final_candidates", "request_completed"):
        assert UPLOAD_MATCH_LEVELS[stage] == logging.INFO, stage


def test_only_summary_stages_reach_stdout():
    """Docker logs must not carry one line per candidate."""
    from utils.logging import (UPLOAD_MATCH_STDOUT_STAGES,
                               UploadMatchConsoleFilter)

    assert UPLOAD_MATCH_STDOUT_STAGES == {"request_completed", "request_failed"}

    console = UploadMatchConsoleFilter()

    def record(stage):
        item = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
        if stage is not None:
            item.upload_match_stage = stage
        return item

    assert console.filter(record("request_completed")) is True
    assert console.filter(record("request_failed")) is True
    assert console.filter(record("raw_candidate")) is False
    assert console.filter(record("final_candidates")) is False
    # A record that is not part of the trace must pass untouched.
    assert console.filter(record(None)) is True


# ---------------------------------------------------------------------------
# The frontend shows the backend's number, and nothing else
# ---------------------------------------------------------------------------

def test_the_frontend_never_fabricates_a_similarity():
    source = _source(SEARCH_JS)
    for forbidden in ("similarity || 1", "similarity ?? 1", "|| 100", "?? 100"):
        assert forbidden not in source, (
            f"the frontend defaults a missing similarity via {forbidden!r}")
    assert "1 - distance" not in source and "1-distance" not in source, (
        "the frontend converts a distance to a similarity; that belongs in SQL")


def test_the_frontend_bands_come_from_the_backend():
    """Hard-coded band thresholds silently diverge from the editable settings."""
    source = _source(SEARCH_JS)
    match = re.search(r"function getSimilarityClass\(\w+\)\s*\{[\s\S]*?\n    \}",
                      source, re.S)
    assert match, "getSimilarityClass is gone"
    body = match.group(0)
    for literal in ("0.90", "0.75", "0.60"):
        assert literal not in body, (
            f"getSimilarityClass hard-codes {literal}; CONFIDENCE_*_MIN are "
            "runtime-editable, so the colour and the backend's confidence_band "
            "drift apart the moment an admin changes one")


# ---------------------------------------------------------------------------
# Completion gates on the live data
# ---------------------------------------------------------------------------

def test_no_exact_embedding_is_shared_across_active_identities():
    import hashlib

    import numpy as np

    rows = _sql(
        "SELECT e.identity_id::text, e.embedding FROM identity_embeddings e "
        "JOIN identities i ON i.id = e.identity_id "
        "WHERE e.embedding IS NOT NULL AND i.status::text IN ('ACTIVE','PROMOTED')",
        fetch="all")
    owners = {}
    for identity_id, raw in rows:
        vector = np.fromstring(str(raw).strip("[]"), sep=",", dtype=np.float32)
        owners.setdefault(hashlib.sha256(vector.tobytes()).hexdigest(),
                          set()).add(identity_id)
    shared = {k: v for k, v in owners.items() if len(v) > 1}
    assert not shared, (
        f"{len(shared)} vector(s) belong to several identities — a search for "
        "that face returns each of them at 1.0")


def test_no_exact_file_is_shared_across_active_identities():
    shared = _sql(
        "SELECT count(*) FROM (SELECT m.file_checksum FROM identity_images m "
        "JOIN identities i ON i.id = m.identity_id "
        "WHERE i.status::text IN ('ACTIVE','PROMOTED') "
        "GROUP BY m.file_checksum HAVING count(DISTINCT m.identity_id) > 1) t")
    assert shared == 0, f"{shared} file(s) are attached to several identities"


def test_every_identity_has_at_most_one_primary_and_owns_its_snapshot():
    assert _sql(
        "SELECT count(*) FROM (SELECT identity_id FROM identity_images "
        "WHERE is_primary GROUP BY identity_id HAVING count(*) > 1) t") == 0

    # KNOWN identities only. An unpromoted ACTIVE unknown ALWAYS displays its
    # camera snapshot and never owns identity_images rows — galleries are
    # created at promotion — so the unscoped form of this assertion was
    # unsatisfiable on any system with live ingest: every ordinary unknown
    # tripped it (observed three separate times with freshly ingested faces).
    # The property this test exists to protect — "the face shown for a person
    # belongs to that person's own gallery" — is a promise the system makes for
    # KNOWN people, and it keeps full strength here.
    assert _sql(
        "SELECT count(*) FROM identities i WHERE i.best_snapshot_path IS NOT NULL "
        "AND i.type::text = 'KNOWN' "
        "AND NOT EXISTS (SELECT 1 FROM identity_images m "
        "                WHERE m.identity_id = i.id "
        "                  AND m.storage_path = i.best_snapshot_path)") == 0, (
        "a KNOWN identity displays a snapshot it does not own — the face shown "
        "in search results belongs to somebody else")


# ---------------------------------------------------------------------------
# P1 — camera ingest cannot re-point a KNOWN person's representative face
#
# The test above states the invariant. These state the MECHANISM that keeps it
# true, because the invariant was being broken by the ingest write path itself:
# "asasa" was promoted at 15:31:04 and a routine re-recognition ten seconds
# later re-pointed best_snapshot_path at a pipeline file the gallery does not
# own. best_snapshot_path feeds the snapshot_url of every search result, list
# card and alert, so the enrolled photo silently stopped being that person.
#
# Three faults compounded, and each is pinned below:
#   1. create_appearance made no KNOWN/UNKNOWN distinction;
#   2. quality_score was ALWAYS None on the ingest call — batch_writer read it
#      back by detection_id, which is linked to the embedding only AFTER
#      create_appearance has run, so the lookup could never match;
#   3. which left the similarity-only fallback, and a correct re-recognition
#      scores 0.9-1.0 — comfortably over the 0.75 floor. The branch meant as a
#      last resort was the branch that always fired.
# ---------------------------------------------------------------------------

P1_PREFIX = "qa_p1snap_"
P1_PIPELINE = "qa-p1snap-cam"
ENROLLED = "storage/faces/qa-p1snap/enrolled.jpg"
CAMERA_CROP = f"/app/storage/{P1_PIPELINE}/somebody/somebody_20260808_120000.jpg"


def _p1_sql(statement, params=None, fetch="none"):
    return _sql(statement, params, fetch)


def _p1_known(name, *, gallery_primary, kind="KNOWN", status="PROMOTED"):
    """An identity whose representative image is already set."""
    identity_id = str(_p1_sql(
        "INSERT INTO identities (id, type, status, display_name, best_snapshot_path, "
        " first_seen_at, last_seen_at, created_at, updated_at, appearances_count) "
        f"VALUES (gen_random_uuid(), '{kind}', '{status}', :n, :p, now(), now(), "
        "         now(), now(), 1) RETURNING id",
        {"n": name, "p": ENROLLED}, fetch="scalar"))
    if gallery_primary:
        _p1_sql(
            "INSERT INTO identity_images (identity_id, storage_path, file_checksum, "
            " is_primary, source_type, processing_status, created_at, updated_at) "
            "VALUES (:i, :p, :c, true, 'promotion', 'completed', now(), now())",
            {"i": identity_id, "p": ENROLLED, "c": f"p1chk{identity_id[:26]}"})
    _p1_sql("INSERT INTO pipelines (pipeline_id, total_detections, is_active, "
            " created_at, updated_at) VALUES (:p, 0, 1, now(), now()) "
            "ON CONFLICT (pipeline_id) DO NOTHING", {"p": P1_PIPELINE})
    return identity_id


def _p1_stored_quality(identity_id, quality):
    """A stored embedding quality for the ladder to compare against."""
    _p1_sql(
        "INSERT INTO identity_embeddings (identity_id, pipeline_id, quality, "
        " quality_scorer_version, embedding_model_version, vector_index_sync_state, "
        " created_at) "
        "VALUES (:i, :p, :q, 'fq1', 'w600k_r50', 'pending', now())",
        {"i": identity_id, "p": P1_PIPELINE, "q": quality})


def _p1_ingest(identity_id, *, candidate, quality, similarity):
    """Drive create_appearance exactly as the batch writer's TX 3 does."""
    from datetime import datetime as _dt

    async def _run():
        from db_connection import db_manager
        from sqlalchemy import select as sa_select
        from db_models import Identity
        from backend.core.identity_service import IdentityService

        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        service = IdentityService()
        async with db_manager.get_session() as db:
            identity = (await db.execute(
                sa_select(Identity).where(Identity.id == identity_id))).scalar_one()
            await service.create_appearance(
                identity=identity,
                pipeline_id=P1_PIPELINE,
                track_id=None,
                start_time=_dt.utcnow(),
                best_snapshot_path=candidate,
                db=db,
                quality_score=quality,
                similarity=similarity,
                quality_scorer_version="fq1",
            )
    run_async(_run())
    return _p1_sql("SELECT best_snapshot_path FROM identities WHERE id = :i",
                   {"i": identity_id}, fetch="scalar")


@pytest.fixture
def p1_clean():
    def _purge():
        for (identity_id,) in _p1_sql(
                "SELECT id FROM identities WHERE display_name LIKE :p",
                {"p": P1_PREFIX + "%"}, fetch="all"):
            for statement in (
                "DELETE FROM identity_embeddings WHERE identity_id = :i",
                "DELETE FROM identity_images WHERE identity_id = :i",
                "DELETE FROM identity_appearances WHERE identity_id = :i",
                "DELETE FROM identities WHERE id = :i",
            ):
                _p1_sql(statement, {"i": str(identity_id)})
        _p1_sql("DELETE FROM identity_appearances WHERE pipeline_id = :p",
                {"p": P1_PIPELINE})
        _p1_sql("DELETE FROM pipelines WHERE pipeline_id = :p", {"p": P1_PIPELINE})
    _purge()
    yield
    _purge()


def _known_bar():
    from config import settings
    return float(settings.IDENTITY_QUALITY_THRESHOLD_KNOWN)


def test_a_promoted_person_survives_a_correct_re_recognition(p1_clean):
    """THE regression, reproduced end to end: a 0.98 match must not move it."""
    identity_id = _p1_known(P1_PREFIX + "promoted", gallery_primary=True)
    after = _p1_ingest(identity_id, candidate=CAMERA_CROP, quality=0.95, similarity=0.98)
    assert after == ENROLLED, (
        f"camera ingest replaced a promoted person's enrolled face with {after}")


def test_a_gallery_primary_is_not_displaced_by_a_better_crop(p1_clean):
    """Enrolled provenance is a decision, not an entry in a quality contest."""
    identity_id = _p1_known(P1_PREFIX + "beaten", gallery_primary=True)
    _p1_stored_quality(identity_id, 0.20)      # the enrolled face scores badly
    after = _p1_ingest(identity_id, candidate=CAMERA_CROP, quality=0.99, similarity=0.99)
    assert after == ENROLLED, "a gallery primary was displaced by a better crop"


def test_a_known_without_a_gallery_accepts_a_crop_above_the_known_bar(p1_clean):
    """No enrolled image to defend, so the best observed crop may represent
    them — but only through IDENTITY_QUALITY_THRESHOLD_KNOWN."""
    identity_id = _p1_known(P1_PREFIX + "nogallery", gallery_primary=False)
    _p1_stored_quality(identity_id, 0.10)
    after = _p1_ingest(identity_id, candidate=CAMERA_CROP,
                       quality=_known_bar() + 0.2, similarity=0.99)
    assert after == CAMERA_CROP, "a clearly better crop should win when nothing is enrolled"


def test_a_known_without_a_gallery_refuses_a_crop_below_the_known_bar(p1_clean):
    identity_id = _p1_known(P1_PREFIX + "belowbar", gallery_primary=False)
    _p1_stored_quality(identity_id, 0.10)
    weak = max(0.0, _known_bar() - 0.2)
    after = _p1_ingest(identity_id, candidate=CAMERA_CROP, quality=weak, similarity=0.99)
    assert after == ENROLLED, (
        f"a crop scoring {weak} cleared the KNOWN bar of {_known_bar()}")


def test_newer_but_worse_never_wins_for_a_known_identity(p1_clean):
    """Recency is not a reason: the candidate clears the bar but is the weaker
    face, and the stored one stays."""
    identity_id = _p1_known(P1_PREFIX + "worse", gallery_primary=False)
    _p1_stored_quality(identity_id, _known_bar() + 0.3)
    after = _p1_ingest(identity_id, candidate=CAMERA_CROP,
                       quality=_known_bar() + 0.1, similarity=0.99)
    assert after == ENROLLED, "a later, worse face replaced a better one"


def test_similarity_alone_can_never_move_a_known_identitys_face(p1_clean):
    """The unscored path — no quality at all and a perfect match. This is the
    exact branch that fired in production. UNKNOWN accepts it; KNOWN must not."""
    identity_id = _p1_known(P1_PREFIX + "simonly", gallery_primary=False)
    after = _p1_ingest(identity_id, candidate=CAMERA_CROP, quality=None, similarity=1.0)
    assert after == ENROLLED, "similarity alone moved a KNOWN identity's face"


def test_the_per_sighting_evidence_is_still_recorded(p1_clean):
    """Freezing the avatar must not discard the sighting. The appearance row
    keeps the crop it actually saw — only the identity-level pointer is held."""
    identity_id = _p1_known(P1_PREFIX + "evidence", gallery_primary=True)
    _p1_ingest(identity_id, candidate=CAMERA_CROP, quality=0.95, similarity=0.98)
    assert _p1_sql(
        "SELECT count(*) FROM identity_appearances "
        "WHERE identity_id = :i AND best_snapshot_path = :p",
        {"i": identity_id, "p": CAMERA_CROP}, fetch="scalar") == 1, (
        "the sighting's own snapshot was lost along with the avatar freeze")


def test_unknown_identities_keep_the_similarity_fallback(p1_clean):
    """The guard is scoped to KNOWN; unknown-face tracking is unchanged."""
    identity_id = _p1_known(P1_PREFIX + "unknown", gallery_primary=False,
                            kind="UNKNOWN", status="ACTIVE")
    after = _p1_ingest(identity_id, candidate=CAMERA_CROP, quality=None, similarity=0.99)
    assert after == CAMERA_CROP, "UNKNOWN snapshot tracking should be unchanged"


def test_ingest_carries_the_crops_own_quality_to_create_appearance():
    """The plumbing fault, pinned at both ends.

    batch_writer's fallback keys on detection_id, and the embedding is linked
    to detection_id only AFTER create_appearance has already run — so that read
    always missed and every ingest call arrived with quality_score=None. The
    value must therefore travel on the face payload itself."""
    producer = _source("/app/backend/services/image_processing.py")
    assert '"quality": quality_score,' in producer, (
        "image_processing no longer puts the scored quality on the face dict")
    assert '"quality": f.get("quality"),' in producer, (
        "the batch payload drops quality before it reaches batch_writer")

    # The ONE detection write path (batch writer and direct path alike) is
    # backend/core/detection_evidence.py: FaceEvidence carries the crop's own
    # ingest-time quality into create_appearance, and the Face row is built
    # from face_row_columns(), which strips quality/quality_scorer (`faces`
    # has no such column) together with every `_`-prefixed internal key.
    consumer = _source("/app/backend/core/detection_evidence.py")
    assert 'quality=f.get("quality")' in consumer, (
        "detection_evidence no longer reads the crop's own ingest-time quality")
    assert 'quality_score=face.quality' in consumer, (
        "detection_evidence does not hand the crop's own quality to create_appearance")
    assert '_FACE_INTERNAL_KEYS = ("quality", "quality_scorer")' in consumer, (
        "quality must be stripped before the Face row insert — `faces` has no such column")
    writer = _source("/app/backend/core/batch_writer.py")
    assert "persist_detection(" in writer and "insert(Face)" not in writer, (
        "batch_writer must delegate to persist_detection, never build Face rows itself")


def test_the_known_guard_uses_the_centralized_threshold_and_a_real_gallery_check():
    """Scoped to create_appearance via the AST — a substring scan of the whole
    module would be satisfied by any other function, or by a docstring."""
    import ast

    tree = ast.parse(_source(IDENTITY_SRC))
    body = next(
        (node for node in ast.walk(tree)
         if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_appearance"),
        None)
    assert body is not None, "create_appearance not found"
    names = {n.attr for n in ast.walk(body) if isinstance(n, ast.Attribute)}

    assert "IDENTITY_QUALITY_THRESHOLD_KNOWN" in names, (
        "the KNOWN bar must come from settings, not from a literal")
    assert "IDENTITY_QUALITY_THRESHOLD_UNKNOWN" not in names, (
        "create_appearance gates a KNOWN identity on the UNKNOWN bar (0.1), "
        "which admits almost any crop")
    assert "is_primary" in names, (
        "the freeze must key on an actual gallery primary, not on type alone")
    # the similarity fallback still exists — for UNKNOWN
    assert "snapshot_replace_min_similarity" in names
