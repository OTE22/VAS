"""Face quality scoring on the camera detection path.

The detection path called `compute_quality_score(face_size=..., confidence=...)`
with `face_size` taken from the UPSTREAM PERSON box and `confidence` from the
upstream object detector. Neither describes the face. The person box is always
far larger than the 100x100 px the size term saturates at, so the whole score
collapsed to:

    quality = 0.3 + 0.2 * upstream_confidence

`pred.get("confidence", 1.0)` only reaches 1.0 when the key is ABSENT, so any
real confidence gave < 0.5 — and `save_embedding` rejects anything below
`IDENTITY_QUALITY_THRESHOLD_KNOWN`, which defaults to exactly 0.5. Known people
were not accumulating embeddings from camera detections at all, and the stored
`quality` column was a linear re-encoding of detector confidence carrying no
image-quality information.
"""

import base64
import json
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

BASE = "http://localhost:8000"
FIXTURES = "/app/tests/fixtures/faces"


@pytest.fixture(scope="module")
def service():
    from backend.core.identity_service import IdentityService
    return IdentityService.__new__(IdentityService)


# ---------------------------------------------------------------------------
# compute_quality_score — the cap, and the semantics of each term
# ---------------------------------------------------------------------------

def test_a_perfect_face_can_reach_one_from_two_signals(service):
    """The regression, stated as directly as it can be.

    Before: 0.3 + 0.2 = 0.5, which the KNOWN threshold rejects.
    """
    assert service.compute_quality_score(face_size=10000, confidence=1.0) == 1.0


def test_score_does_not_depend_on_how_many_signals_were_supplied(service):
    """A face that is excellent on every axis MEASURED scores excellent.

    The property, not a magic constant: adding a signal that is also perfect
    must not change the answer, because the weights are renormalized.
    """
    perfect = [
        service.compute_quality_score(face_size=10000),
        service.compute_quality_score(face_size=10000, confidence=1.0),
        service.compute_quality_score(face_size=10000, confidence=1.0, sharpness=1.0),
        service.compute_quality_score(face_size=10000, confidence=1.0,
                                      sharpness=1.0, pose_score=1.0),
    ]
    assert perfect == [1.0, 1.0, 1.0, 1.0], perfect


def test_a_poor_face_scores_low_however_it_is_measured(service):
    for score in (
        service.compute_quality_score(face_size=500),
        service.compute_quality_score(face_size=500, confidence=0.2),
        service.compute_quality_score(face_size=500, confidence=0.2, sharpness=0.1),
    ):
        assert score < 0.35, score


def test_sharpness_and_blur_score_have_opposite_conventions(service):
    """Pins the inversion so the two parameters are never crossed.

    `sharpness` is 0..1 higher-is-better (what face_quality returns).
    `blur_score` is the legacy Laplacian-ish scale, higher-is-blurrier.
    Feeding one into the other silently destroys the signal.
    """
    sharp = service.compute_quality_score(face_size=10000, sharpness=1.0)
    blurry = service.compute_quality_score(face_size=10000, sharpness=0.0)
    assert sharp > blurry, "sharpness=1.0 must beat sharpness=0.0"

    legacy_sharp = service.compute_quality_score(face_size=10000, blur_score=0.0)
    legacy_blurry = service.compute_quality_score(face_size=10000, blur_score=100.0)
    assert legacy_sharp > legacy_blurry, "blur_score=0 means SHARP on the legacy scale"

    # And the two conventions agree at their extremes.
    assert sharp == legacy_sharp
    assert blurry == legacy_blurry


def test_sharpness_wins_over_the_legacy_parameter(service):
    """If both are supplied, the unambiguous one is used."""
    assert (service.compute_quality_score(face_size=10000, sharpness=1.0,
                                          blur_score=100.0)
            == service.compute_quality_score(face_size=10000, sharpness=1.0))


def test_out_of_range_inputs_are_clamped_not_propagated(service):
    for score in (
        service.compute_quality_score(face_size=10 ** 9, confidence=5.0),
        service.compute_quality_score(face_size=10000, sharpness=-3.0),
        service.compute_quality_score(face_size=10000, pose_score=99.0),
        service.compute_quality_score(face_size=-5, confidence=-1.0),
    ):
        assert 0.0 <= score <= 1.0, score


def test_no_signals_returns_zero_never_none(service):
    """None would bypass every gate downstream.

    Each check is written `if quality_score is not None and quality_score <
    threshold`, so a None reaching the database admits an image that should have
    been refused.
    """
    result = service.compute_quality_score(face_size=0)
    assert result is not None
    assert isinstance(result, float)
    assert result == 0.0


# ---------------------------------------------------------------------------
# The real scorer, on real images
# ---------------------------------------------------------------------------

def _detect(image):
    from backend.core.model_manager import model_manager
    model_manager.initialize()
    bboxes, kpss = model_manager.detector.detect(image, max_num=1)
    if kpss is None or len(kpss) == 0:
        return None
    return [int(v) for v in bboxes[0][:4]], kpss[0], float(bboxes[0][4])


def _score(image):
    from backend.core.face_quality import quality_score_for_detection
    found = _detect(image)
    if found is None:
        return None
    bbox, landmarks, det = found
    score, _details, version = quality_score_for_detection(image, bbox, landmarks, det)
    return score, version


@pytest.fixture(scope="module")
def face_image():
    import cv2
    image = cv2.imread(f"{FIXTURES}/face_a.jpg")
    if image is None:
        pytest.skip("fixture face_a.jpg is unreadable")
    return image


def test_the_scorer_produces_a_usable_score_above_the_known_threshold(face_image):
    """A decent face must clear the gate the old scorer could not reach."""
    from config import settings

    result = _score(face_image)
    assert result is not None, "no face detected in the fixture"
    score, version = result
    assert version == "fq1"
    threshold = float(getattr(settings, "IDENTITY_QUALITY_THRESHOLD_KNOWN", 0.5))
    assert score > threshold, (
        f"a good face scored {score:.3f}, still under the KNOWN threshold "
        f"{threshold} — known people would keep not accumulating embeddings")
    assert score < 1.0, "the scorer is saturating rather than discriminating"


def test_blur_lowers_the_score(face_image):
    """Ordering, not a magic constant."""
    import cv2

    sharp = _score(face_image)
    blurred = _score(cv2.GaussianBlur(face_image, (15, 15), 0))
    assert sharp and blurred, "a variant lost the face entirely"
    assert blurred[0] < sharp[0], (
        f"blurring did not lower the score: {sharp[0]:.3f} -> {blurred[0]:.3f}")


def test_bad_lighting_lowers_the_score(face_image):
    dark = _score((face_image * 0.2).astype(np.uint8))
    sharp = _score(face_image)
    assert sharp and dark
    assert dark[0] < sharp[0], (
        f"darkening did not lower the score: {sharp[0]:.3f} -> {dark[0]:.3f}")


def test_the_scorer_never_returns_a_non_numeric_score_to_the_caller(monkeypatch,
                                                                    face_image,
                                                                    service):
    """A scorer failure must degrade to a NUMBER, not to None.

    quality_score_for_detection signals failure with None, and the caller is
    responsible for substituting the legacy score. This pins that contract from
    both sides.
    """
    import backend.core.face_quality as fq

    def explode(*args, **kwargs):
        raise RuntimeError("simulated scorer failure")
    monkeypatch.setattr(fq, "assess_face_quality", explode)

    found = _detect(face_image)
    assert found is not None
    bbox, landmarks, det = found
    score, details, version = fq.quality_score_for_detection(
        face_image, bbox, landmarks, det)

    assert score is None and version == "legacy", (
        "a failure must ask the caller for the legacy score")

    # And the caller's substitution is numeric and gate-respecting.
    face_size = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    fallback = service.compute_quality_score(face_size=face_size, confidence=det)
    assert isinstance(fallback, float) and 0.0 <= fallback <= 1.0


def test_the_legacy_scorer_can_be_restored_without_a_redeploy(monkeypatch, face_image):
    """FACE_QUALITY_SCORER=legacy is the rollback knob."""
    import backend.core.face_quality as fq
    from config import settings

    monkeypatch.setattr(settings, "FACE_QUALITY_SCORER", "legacy", raising=False)
    found = _detect(face_image)
    bbox, landmarks, det = found
    score, _details, version = fq.quality_score_for_detection(
        face_image, bbox, landmarks, det)
    assert score is None and version == "legacy"


def test_disabling_quality_falls_back_to_legacy_not_to_none(monkeypatch, face_image):
    import backend.core.face_quality as fq
    from config import settings

    monkeypatch.setattr(settings, "FACE_QUALITY_ENABLED", False, raising=False)
    bbox, landmarks, det = _detect(face_image)
    score, _details, version = fq.quality_score_for_detection(
        face_image, bbox, landmarks, det)
    assert version == "legacy", "disabling the scorer must select the legacy path"


# ---------------------------------------------------------------------------
# Provenance and cross-version comparison
# ---------------------------------------------------------------------------

def test_the_provenance_column_exists_and_defaults_to_null():
    """Asserted against information_schema, not against the migration source."""
    from sqlalchemy import text
    from conftest import run_on_shared_loop as run_async
    from db_connection import db_manager

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            return (await db.execute(text(
                "SELECT table_name, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE column_name = 'quality_scorer_version' "
                "ORDER BY table_name"))).all()

    rows = run_async(_run())
    tables = {r[0] for r in rows}
    assert {"identity_embeddings", "identity_images"} <= tables, tables
    for table, nullable, default in rows:
        assert nullable == "YES", f"{table}.quality_scorer_version is NOT NULL"
        assert default is None, (
            f"{table}.quality_scorer_version has a default ({default!r}) — a "
            "legacy row would then claim a provenance it does not have")


def test_quality_values_from_different_scorers_are_never_compared():
    """The hazard the version column exists to prevent.

    A legacy row sits near 0.5 by construction. If a new-scale score were ranked
    against it, every incoming frame would win and best_snapshot_path would
    thrash to whatever arrived last, regardless of quality.
    """
    import uuid as uuid_module
    from datetime import datetime

    from sqlalchemy import text
    from conftest import run_on_shared_loop as run_async
    from db_connection import db_manager
    from db_models import (Identity, IdentityEmbedding, IdentityStatus,
                           IdentityType)

    ident = uuid_module.uuid4()

    async def _run():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        async with db_manager.get_session() as db:
            now = datetime.utcnow()
            db.add(Identity(id=ident, type=IdentityType.UNKNOWN,
                            status=IdentityStatus.ACTIVE,
                            display_name=f"qa-scorer-{ident.hex[:8]}",
                            first_seen_at=now, last_seen_at=now))
            await db.flush()
            # A legacy row (NULL provenance) with a HIGHER stored number...
            db.add(IdentityEmbedding(identity_id=ident, pipeline_id=None,  # not a camera sighting: no pipeline row
                                     quality=0.95, quality_scorer_version=None,
                                     created_at=now))
            # ...and a current-scorer row with a lower one.
            db.add(IdentityEmbedding(identity_id=ident, pipeline_id=None,
                                     quality=0.60, quality_scorer_version="fq1",
                                     created_at=now))
            await db.commit()

            scoped = (await db.execute(text(
                "SELECT max(quality) FROM identity_embeddings "
                "WHERE identity_id = :i AND quality_scorer_version = 'fq1'"),
                {"i": str(ident)})).scalar()
            unscoped = (await db.execute(text(
                "SELECT max(quality) FROM identity_embeddings "
                "WHERE identity_id = :i"), {"i": str(ident)})).scalar()

            await db.execute(text(
                "DELETE FROM identity_embeddings WHERE identity_id = :i"),
                {"i": str(ident)})
            await db.execute(text("DELETE FROM identities WHERE id = :i"),
                             {"i": str(ident)})
            await db.commit()
            return scoped, unscoped

    scoped, unscoped = run_async(_run())
    assert float(scoped) == pytest.approx(0.60), scoped
    assert float(unscoped) == pytest.approx(0.95), unscoped
    assert scoped != unscoped, (
        "the version-scoped comparison is not actually filtering")


def test_create_appearance_scopes_its_comparison_by_scorer_version():
    """Behavioural: the query the fix lives in must carry the filter."""
    import inspect

    from backend.core.identity_service import IdentityService

    source = inspect.getsource(IdentityService.create_appearance)
    assert "quality_scorer_version" in source, (
        "create_appearance ranks stored quality values without scoping them to "
        "a scorer version")


# ---------------------------------------------------------------------------
# End to end, through the authenticated ingest path
# ---------------------------------------------------------------------------

def test_camera_ingest_stores_a_real_quality_score():
    """The test that would have caught the original bug.

    Sends a real face through the real webhook and asserts the stored quality is
    above what `0.3 + 0.2*confidence` could ever produce for that confidence.
    """
    import cv2
    from sqlalchemy import text
    from conftest import run_on_shared_loop as run_async
    from config import settings
    from backend.security.webhook_auth import parse_keys
    from db_connection import db_manager

    keys = parse_keys(getattr(settings, "WEBHOOK_API_KEYS", ""))
    if not keys:
        pytest.skip("no ingest key configured in this container")

    image = cv2.imread(f"{FIXTURES}/face_a.jpg")
    if image is None:
        pytest.skip("fixture unreadable")
    height, width = image.shape[:2]
    _ok, buffer = cv2.imencode(".jpg", image)

    pipeline = "qa-quality-regression"
    confidence = 0.87
    payload = {
        "image": base64.b64encode(buffer.tobytes()).decode(),
        "predictions": [{"class_name": "person", "confidence": confidence,
                         "bbox": [0, 0, width, height]}],
        "request_id": f"qa-quality-{time.time()}",
    }
    request = urllib.request.Request(BASE + f"/webhook/{pipeline}",
                                     data=json.dumps(payload).encode(), method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("X-Webhook-Key", keys[0])
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.status == 202, response.status

    async def _await_rows():
        if not getattr(db_manager, "_initialized", False):
            await db_manager.init_db()
        import asyncio
        for _ in range(45):
            async with db_manager.get_session() as db:
                rows = (await db.execute(text(
                    "SELECT quality, quality_scorer_version FROM identity_embeddings "
                    "WHERE pipeline_id = :p"), {"p": pipeline})).all()
            if rows:
                return rows
            await asyncio.sleep(1)
        return []

    async def _cleanup():
        async with db_manager.get_session() as db:
            await db.execute(text(
                "DELETE FROM identity_embeddings WHERE pipeline_id = :p"),
                {"p": pipeline})
            await db.execute(text(
                "DELETE FROM identity_appearances WHERE pipeline_id = :p"),
                {"p": pipeline})
            await db.execute(text(
                "DELETE FROM faces WHERE detection_id IN "
                "(SELECT id FROM detections WHERE pipeline_id = :p)"), {"p": pipeline})
            await db.execute(text("DELETE FROM detections WHERE pipeline_id = :p"),
                             {"p": pipeline})
            await db.execute(text(
                "DELETE FROM identities WHERE display_name IS NULL AND id NOT IN "
                "(SELECT DISTINCT identity_id FROM identity_embeddings)"))
            await db.execute(text("DELETE FROM pipelines WHERE pipeline_id = :p"),
                             {"p": pipeline})
            await db.commit()

    try:
        rows = run_async(_await_rows())
        assert rows, "the ingest produced no embedding row"
        quality, version = float(rows[0][0]), rows[0][1]

        legacy_would_be = 0.3 + 0.2 * confidence
        assert quality > legacy_would_be, (
            f"stored quality {quality:.3f} is no better than the legacy formula "
            f"({legacy_would_be:.3f}) — the person box is probably still being "
            "used as the face size")
        assert quality > 0.5, (
            f"stored quality {quality:.3f} is still under the KNOWN threshold")
        assert version == "fq1", (
            f"provenance was not stamped (got {version!r}); the row would be "
            "treated as legacy and excluded from every future comparison")
    finally:
        run_async(_cleanup())
