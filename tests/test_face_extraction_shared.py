"""One face extractor, shared by enrollment and search.

    docker exec face_recognition_api python -m pytest tests/test_face_extraction_shared.py -v

The defect these pin: a tightly cropped face enrolled perfectly and then could
not be searched. `POST /api/search/by-image` answered 400 "No face detected in
image" for the exact image that `is_face_image=true` had just accepted.

There were three causes, all of them consequences of five separate
implementations of "get a face out of an image":

1. Only enrollment retried detection on a padded canvas. SCRFD letterboxes to
   640x640, and a face that fills the frame is larger than the stride-32 anchors
   can regress — it needs margin, not a lower threshold.

2. Four sites invented five keypoints from image geometry when detection failed
   on a small squarish image. That never errors: ArcFace warps the face using
   the made-up geometry and returns a well-formed 512-vector pointing somewhere
   else. Measured on the fixture crop, cosine(fabricated, real) = 0.665 for the
   SAME face, against a 0.4 match threshold. Two of those sites wrote the result
   into the gallery.

3. `/api/search/by-image` aligned the face itself, converted BGR->RGB, then
   passed the aligned crop plus the reference landmarks to `get_embedding`,
   which aligned again and applied swapRB again. It was the only site feeding
   the model BGR while every stored vector came from RGB — same pixels, same
   face, cosine 0.9428 instead of 1.0000.

Fixtures are committed under tests/fixtures/faces/ precisely so these stay
reproducible; synthetic images do not survive real face detection.
"""

import io
import json
import os
import urllib.error
import urllib.request
import uuid as uuid_module

import numpy as np
import pytest

BASE = "http://localhost:8000"
FIXTURES = "/app/tests/fixtures/faces"

PORTRAIT = f"{FIXTURES}/face_a.jpg"
PORTRAIT_LARGE = f"{FIXTURES}/face_c.png"
# A genuinely pre-cropped face: 112x112, and the plain detector finds NOTHING in
# it. That is the whole point — see test_the_fixture_still_defeats_plain_detection.
CROPPED = f"{FIXTURES}/cropped_face.jpg"
TWO_FACES = f"{FIXTURES}/two_faces.jpg"

EXTRACTION_SRC = "/app/backend/core/face_extraction.py"
SCANNED_FOR_FABRICATION = [
    "/app/backend/core/advanced_search.py",
    "/app/backend/routes/advanced_search.py",
    "/app/backend/core/identity_loader.py",
    "/app/backend/core/enrollment_service.py",
    "/app/backend/routes/identities.py",
    EXTRACTION_SRC,
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _code_only(path):
    """Source with comments and docstrings stripped.

    These files explain the removed behaviour in their own prose; a raw text
    scan cannot tell an explanation from a call.
    """
    import ast

    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    return ast.unparse(tree)


def _multipart(fields, files):
    boundary = "----qafaceext" + uuid_module.uuid4().hex
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f"--{boundary}\r\n".encode())
        out.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.write(f"{value}\r\n".encode())
    for name, (filename, payload, content_type) in files.items():
        out.write(f"--{boundary}\r\n".encode())
        out.write((f'Content-Disposition: form-data; name="{name}"; '
                   f'filename="{filename}"\r\n').encode())
        out.write(f"Content-Type: {content_type}\r\n\r\n".encode())
        out.write(payload)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


class _Headers(dict):
    """Case-insensitive lookup.

    uvicorn emits response header names lowercased, which HTTP/1.1 permits —
    field names are case-insensitive. A plain dict lookup for "X-Faces-Detected"
    therefore misses a header that was sent correctly.
    """

    def get(self, key, default=None):
        lowered = key.lower()
        for name, value in self.items():
            if name.lower() == lowered:
                return value
        return default


def _http(method, path, *, body=None, token=None, headers=None,
          fields=None, files=None, timeout=180):
    """Returns (status, parsed_body, response_headers)."""
    data = None
    hdrs = dict(headers or {})
    if files is not None:
        data, content_type = _multipart(fields or {}, files)
        hdrs["Content-Type"] = content_type
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in hdrs.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw or b"{}"), _Headers(response.headers)
            except Exception:
                return response.status, {"_raw": raw.decode(errors="replace")}, _Headers(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}"), _Headers(exc.headers)
        except Exception:
            return exc.code, {"_raw": raw.decode(errors="replace")}, _Headers(exc.headers)


def _search_by_image(path, token, *, filename=None):
    payload = _read(path)
    name = filename or os.path.basename(path)
    kind = "image/png" if name.lower().endswith(".png") else "image/jpeg"
    return _http("POST", "/api/search/by-image", token=token,
                 fields={"scope": "both", "top_k": "10"},
                 files={"image": (name, payload, kind)})


def _quality_check(path, token, *, payload=None, filename=None):
    data = payload if payload is not None else _read(path)
    name = filename or (os.path.basename(path) if path else "probe.png")
    kind = "image/png" if name.lower().endswith(".png") else "image/jpeg"
    return _http("POST", "/api/search/quality-check", token=token,
                 files={"image": (name, data, kind)})


def _png_bytes(image):
    import cv2

    ok, buffer = cv2.imencode(".png", image)
    assert ok, "failed to encode the synthetic probe"
    return buffer.tobytes()


@pytest.fixture(scope="module")
def token():
    status, body, _ = _http("POST", "/api/auth/login",
                            body={"username": "admin", "password": "admin123"})
    assert status == 200, body
    return body["access_token"]


# ---------------------------------------------------------------------------
# The fixture has to keep being hard, or nothing below proves anything
# ---------------------------------------------------------------------------

def test_the_fixture_still_defeats_plain_detection():
    """Guard on the guard.

    Every padded-retry assertion in this file is vacuous if the detector can
    simply see this crop. If a model change ever makes it detectable, this fails
    first and says so, rather than letting the real tests quietly stop testing.
    """
    import cv2

    from backend.core import model_manager

    model_manager.initialize()
    image = cv2.imread(CROPPED)
    assert image is not None, f"missing fixture {CROPPED}"
    assert image.shape[:2] == (112, 112), image.shape

    _bboxes, kpss = model_manager.detector.detect(image, max_num=0)
    found = 0 if kpss is None else len(kpss)
    assert found == 0, (
        "cropped_face.jpg is now detectable without padding — this fixture no "
        "longer reproduces the bug; find a tighter crop")


# ---------------------------------------------------------------------------
# 1. Normal portrait — the path that always worked must keep working
# ---------------------------------------------------------------------------

def test_a_normal_portrait_needs_no_retry():
    import cv2

    from backend.core.face_extraction import extract_faces

    faces = extract_faces(cv2.imread(PORTRAIT))
    assert len(faces) == 1
    assert faces[0].padded_retry is False, "a normal portrait must not need padding"
    assert faces[0].landmarks.shape == (5, 2)
    assert np.all(np.isfinite(faces[0].landmarks))
    assert faces[0].score > 0.0


def test_a_normal_portrait_searches_and_scores(token):
    status, body, headers = _search_by_image(PORTRAIT, token)
    assert status == 200, body
    assert headers.get("X-Faces-Detected") == "1"
    assert headers.get("X-Padded-Retry") == "false"

    status, body, _ = _quality_check(PORTRAIT, token)
    assert status == 200, body
    assert 0.0 <= body["overall_score"] <= 1.0


# ---------------------------------------------------------------------------
# 2. Tight crop — THE regression
# ---------------------------------------------------------------------------

def test_a_tight_crop_is_found_by_the_padded_retry():
    import cv2

    from backend.core.face_extraction import extract_faces

    faces = extract_faces(cv2.imread(CROPPED))
    assert len(faces) == 1, "the padded retry did not recover the cropped face"
    assert faces[0].padded_retry is True, "it was found without padding?"


def test_a_tight_crop_can_be_searched(token):
    """The reported bug, end to end: this used to be 400."""
    status, body, headers = _search_by_image(CROPPED, token)
    assert status == 200, (
        f"a tightly cropped face still cannot be searched: {status} {body}")
    assert headers.get("X-Padded-Retry") == "true"


def test_a_tight_crop_can_be_quality_checked(token):
    status, body, _ = _quality_check(CROPPED, token)
    assert status == 200, body
    assert 0.0 <= body["overall_score"] <= 1.0


def test_the_retry_maps_landmarks_back_to_the_original_image():
    """Padding shifts every coordinate; forgetting to undo it would align the
    face against a point outside the frame and embed mostly black pixels."""
    import cv2

    from backend.core.face_extraction import extract_faces

    image = cv2.imread(CROPPED)
    height, width = image.shape[:2]
    face = extract_faces(image)[0]

    # Landmarks land on the face, which occupies the crop. A tolerance of one
    # face-width outside the frame allows for a detector box that legitimately
    # includes the border it saw.
    assert -width <= face.landmarks[:, 0].min() <= width * 2
    assert -height <= face.landmarks[:, 1].min() <= height * 2
    x1, y1, x2, y2 = face.bbox_int(image.shape)
    assert 0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height, (
        "bbox_int must always yield a non-empty in-bounds slice")
    assert image[y1:y2, x1:x2].size > 0


# ---------------------------------------------------------------------------
# 3. Padded retry produces a CORRECT embedding, not merely an embedding
# ---------------------------------------------------------------------------

def test_the_probe_embedding_matches_the_gallery_convention():
    """Pins defect 3.

    `/api/search/by-image` used to pre-align, convert BGR->RGB and pass the
    reference landmarks, so `get_embedding` aligned a second time and swapped
    the channels back. The probe was computed in a different colour convention
    from every stored vector: the same pixels scored 0.9428 against their own
    gallery embedding instead of 1.0000. That erodes the margin on every query.
    """
    import cv2

    from backend.core.face_extraction import embed_face_normalized, extract_faces

    image = cv2.imread(PORTRAIT_LARGE)
    face = extract_faces(image)[0]

    probe = embed_face_normalized(image, face)

    # The gallery convention: raw BGR array + real landmarks, exactly what
    # enrollment and the live pipeline pass.
    from backend.core import model_manager
    model_manager.initialize()
    gallery = model_manager.recognizer.get_embedding(image, face.landmarks)
    gallery = gallery / np.linalg.norm(gallery)

    assert float(np.dot(probe, gallery)) >= 0.999, (
        "the search probe is not in the same space as stored embeddings")


def test_the_retry_embedding_is_nothing_like_a_fabricated_one():
    """Pins defect 2 numerically.

    Reproduces the exact fabrication that used to run — five keypoints from
    width/height fractions around the image midpoint — and shows it lands
    somewhere else entirely. 0.665 against a 0.4 match threshold is not a small
    error: it is a different person as far as the index is concerned.
    """
    import cv2

    from backend.core.face_extraction import embed_face_normalized, extract_faces

    image = cv2.imread(CROPPED)
    height, width = image.shape[:2]
    real = embed_face_normalized(image, extract_faces(image)[0])

    mid_x, mid_y = width // 2, height // 2
    fabricated = np.array([
        [mid_x - width * 0.15, mid_y - height * 0.1],
        [mid_x + width * 0.15, mid_y - height * 0.1],
        [mid_x, mid_y],
        [mid_x - width * 0.1, mid_y + height * 0.15],
        [mid_x + width * 0.1, mid_y + height * 0.15],
    ], dtype=np.float32)

    from backend.core import model_manager
    model_manager.initialize()
    invented = model_manager.recognizer.get_embedding(image, fabricated)
    invented = invented / np.linalg.norm(invented)

    similarity = float(np.dot(real, invented))
    assert similarity < 0.95, (
        f"fabricated landmarks produce a near-identical embedding ({similarity:.3f}); "
        "if that were true the fabrication would have been harmless — re-check "
        "which landmarks the extractor actually used")


def test_enrollment_and_search_extract_the_same_face():
    """Requirement: one shared extraction function, not two that agree today."""
    import cv2

    from backend.core.enrollment_service import detect_face_landmarks
    from backend.core.face_extraction import extract_single_face

    image = cv2.imread(CROPPED)
    _bbox, enrollment_landmarks = detect_face_landmarks(image, True)
    search_landmarks = extract_single_face(image).landmarks

    assert np.allclose(enrollment_landmarks, search_landmarks, atol=1e-4), (
        "enrollment and search disagree about where the face is")


# ---------------------------------------------------------------------------
# 4. No face — a refusal, never an invented one
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape", [
    (480, 640),      # ordinary frame
    (200, 200),      # THE important one: satisfies the old fabrication gate
    (112, 112),      # the same shape as the real crop fixture
])
def test_an_image_with_no_face_is_refused(shape):
    from backend.core.face_extraction import (FaceExtractionError, extract_faces,
                                              extract_single_face)

    blank = np.zeros((shape[0], shape[1], 3), dtype=np.uint8)
    assert extract_faces(blank) == [], (
        f"a blank {shape[1]}x{shape[0]} image produced a face")

    with pytest.raises(FaceExtractionError) as caught:
        extract_single_face(blank)
    assert caught.value.code == "no_face"


def test_no_face_returns_a_structured_error_the_ui_can_render(token):
    """Requirement 9, and the frontend contract.

    The admin UI renders `error.detail || 'Search failed'`, so `detail` must
    stay a STRING — a dict would reach the user as "[object Object]". The
    machine-readable code rides alongside it.
    """
    blank = _png_bytes(np.zeros((200, 200, 3), dtype=np.uint8))

    status, body, _ = _http("POST", "/api/search/by-image", token=token,
                            fields={"scope": "both", "top_k": "10"},
                            files={"image": ("blank.png", blank, "image/png")})
    assert status == 400, body
    assert isinstance(body.get("detail"), str) and body["detail"], (
        "detail must be a human-readable string for the admin UI")
    assert body.get("error") == "no_face"
    assert body.get("faces_detected") == 0

    status, body, _ = _quality_check(None, token, payload=blank, filename="blank.png")
    assert status == 400, body
    assert isinstance(body.get("detail"), str)
    assert body.get("error") == "no_face"


def test_a_blank_small_square_no_longer_scores_as_a_face(token):
    """The fabrication's worst symptom.

    A 200x200 image of nothing used to satisfy `is_small_image`, get five
    invented keypoints, and come back with a full quality assessment — whose
    angle sub-score was a constant 1.0, because the invented points are
    symmetric about the midpoint by construction. It was scoring a wall.
    """
    blank = _png_bytes(np.full((200, 200, 3), 127, dtype=np.uint8))
    status, body, _ = _quality_check(None, token, payload=blank, filename="grey.png")
    assert status == 400, (
        f"a blank square still receives a quality score: {body}")


# ---------------------------------------------------------------------------
# 5. Multiple faces — per-endpoint semantics, preserved
# ---------------------------------------------------------------------------

def test_multiple_faces_are_rejected_where_one_is_required():
    from backend.core.face_extraction import (FaceExtractionError,
                                              extract_single_face)
    import cv2

    image = cv2.imread(TWO_FACES)
    with pytest.raises(FaceExtractionError) as caught:
        extract_single_face(image, on_multiple="reject")
    assert caught.value.code == "multiple_faces"
    assert caught.value.faces_found == 2


def test_multiple_faces_select_the_largest_where_that_is_the_contract():
    """`on_multiple="best"` must reproduce what `detect(max_num=1)` returned.

    That equivalence is what makes the read-only paths' move to uncapped
    detection a non-change: they learn the true face count while still choosing
    the identical face.
    """
    import cv2

    from backend.core import model_manager
    from backend.core.face_extraction import extract_single_face

    model_manager.initialize()
    image = cv2.imread(TWO_FACES)

    _b, capped = model_manager.detector.detect(image, max_num=1)
    chosen = extract_single_face(image, on_multiple="best")

    assert np.allclose(np.asarray(capped[0], dtype=np.float32),
                       chosen.landmarks, atol=1e-4), (
        "select_largest chose a different face than max_num=1 did")


def test_by_image_searches_the_largest_face_and_reports_the_count(token):
    """Search on a group photo keeps working, and now says what it did."""
    status, body, headers = _search_by_image(TWO_FACES, token)
    assert status == 200, body
    assert headers.get("X-Faces-Detected") == "2", (
        "the face count is not reported, so 'why this face?' is unanswerable")


def test_enrollment_still_refuses_a_two_face_photo(token):
    status, body, _ = _http(
        "POST", "/api/upload-person", token=token,
        fields={"person_name": f"qa_faceext_{uuid_module.uuid4().hex[:8]}"},
        files={"photo": ("two.jpg", _read(TWO_FACES), "image/jpeg")})
    assert status == 400, body
    assert body.get("error") == "multiple_faces", body
    assert "2 faces" in (body.get("message") or ""), body


# ---------------------------------------------------------------------------
# 6. Missing / invalid landmarks — refuse, never guess
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad,reason", [
    (None, "None"),
    (np.zeros((5, 2), dtype=np.float32), "all zeros"),
    (np.full((5, 2), 7.0, dtype=np.float32), "all identical"),
    (np.full((5, 2), np.nan, dtype=np.float32), "not finite"),
    (np.array([[np.inf, 0], [1, 2], [3, 4], [5, 6], [7, 8]], dtype=np.float32), "infinite"),
    (np.zeros((4, 2), dtype=np.float32), "four points"),
    (np.zeros((5, 3), dtype=np.float32), "three dimensions"),
    (np.zeros((5,), dtype=np.float32), "flat"),
])
def test_unusable_landmarks_are_refused(bad, reason):
    from backend.core.face_extraction import FaceExtractionError, validate_landmarks

    with pytest.raises(FaceExtractionError) as caught:
        validate_landmarks(bad)
    assert caught.value.code == "no_landmarks", reason


def test_real_landmarks_pass_validation():
    import cv2

    from backend.core.face_extraction import extract_faces, validate_landmarks

    for path in (PORTRAIT, PORTRAIT_LARGE, CROPPED):
        face = extract_faces(cv2.imread(path))[0]
        assert validate_landmarks(face.landmarks).shape == (5, 2), path


def test_a_face_with_degenerate_landmarks_is_never_embedded(monkeypatch):
    """A detector that returns a plausible box with unusable keypoints must not
    reach the recognizer — that is the shape the fabrication used to produce."""
    from backend.core import face_extraction

    class _StubDetector:
        def detect(self, image, max_num=0, metric="max"):
            boxes = np.array([[10, 10, 90, 90, 0.99]], dtype=np.float32)
            collapsed = np.zeros((1, 5, 2), dtype=np.float32)
            return boxes, collapsed

    class _StubRecognizer:
        def get_embedding(self, image, landmarks):
            raise AssertionError("the recognizer was reached with bad landmarks")

    class _StubManager:
        _initialized = True
        detector = _StubDetector()
        recognizer = _StubRecognizer()

    blank = np.zeros((120, 120, 3), dtype=np.uint8)
    with pytest.raises(face_extraction.FaceExtractionError) as caught:
        face_extraction.extract_faces(blank, manager=_StubManager())
    assert caught.value.code == "no_landmarks"


# ---------------------------------------------------------------------------
# 7. One extractor — structurally, not by convention
# ---------------------------------------------------------------------------

def test_no_module_fabricates_landmarks():
    """The invariant, repo-wide.

    The enrollment suite has pinned this for enrollment since the padded retry
    was written; the search and loader paths were never covered, which is how
    four copies of the same fabrication survived there.
    """
    offenders = []
    for path in SCANNED_FOR_FABRICATION:
        code = _code_only(path)
        for token_name in ("center_x", "center_y", "is_small_image"):
            if token_name in code:
                offenders.append(f"{os.path.basename(path)}:{token_name}")
    assert not offenders, f"fabricated-landmark code is back: {offenders}"


def test_the_detector_is_called_from_one_place():
    """Every face-extraction path shares the retry, the validation and the
    refusal — the alternative is five implementations and one of them right."""
    import ast

    allowed = {
        "backend/core/face_extraction.py",       # the shared extractor
        "backend/services/image_processing.py",  # live pipeline: detects inside
                                                 # an already-cropped person box
        "backend/routes/identities.py",          # promotion probe: "does any
                                                 # stored image contain a face?"
                                                 # — existence only, no embedding
    }
    offenders = []
    for root, _dirs, names in os.walk("/app/backend"):
        if "__pycache__" in root:
            continue
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            relative = os.path.relpath(path, "/app").replace(os.sep, "/")
            if relative in allowed:
                continue
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "detect"
                        and isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr == "detector"):
                    offenders.append(f"{relative}:{node.lineno}")
    assert not offenders, (
        "these call the detector directly instead of using face_extraction: "
        f"{offenders}")


def test_the_extractor_never_loosens_the_detector():
    """Requirement 8. The retry adds margin; it must not touch the thresholds.

    If a future change reaches for conf_thres or input_size to make tight crops
    work, it degrades detection for every normal image at the same time.
    """
    code = _code_only(EXTRACTION_SRC)
    for forbidden in ("conf_thres", "iou_thres", "input_size", "det_size",
                      "CONFIDENCE_THRESHOLD"):
        assert forbidden not in code, (
            f"the shared extractor manipulates {forbidden}; the padded retry "
            "must change framing, not the decision rule")


def test_enrollment_delegates_rather_than_duplicating():
    code = _code_only("/app/backend/core/enrollment_service.py")
    assert "extract_single_face" in code, "enrollment no longer shares the extractor"
    assert "copyMakeBorder" not in code, "enrollment kept its own padded retry"
    # The distinct refusals the operator depends on are still enrollment's.
    assert "no_landmarks" in code and "no_face" in code
