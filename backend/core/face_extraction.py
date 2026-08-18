"""The one way to get a face out of an image.

Every path that turns pixels into an embedding — enrollment, both search
endpoints, the quality check, the startup gallery loader — goes through here.
Before this module there were five separate implementations and only one of
them was right, which produced three distinct defects:

1. **A tight crop could not be searched.** Only enrollment retried detection on
   a padded canvas, so a 112x112 face image that enrolled perfectly came back
   from `/api/search/by-image` as 400 "No face detected in image".

2. **Four sites invented landmarks.** When detection found nothing on a small
   squarish image they synthesized five keypoints from width/height fractions
   (`center_x - w*0.15`, ...) and a bbox of `[0, 0, w, h, 1.0]`. That never
   fails and never looks wrong: ArcFace happily warps the face using made-up
   geometry and returns a perfectly well-formed 512-vector pointing somewhere
   else. Measured on the fixture crop, an embedding built from fabricated
   landmarks scores **0.665** against one built from the real landmarks of the
   same face — against a 0.4 match threshold. Two of those sites wrote the
   result into the gallery.

3. **`/api/search/by-image` embedded in the wrong colour space.** It aligned the
   face itself, converted BGR->RGB, then passed the aligned crop plus the
   reference landmarks to `get_embedding`, which aligns again and applies
   `swapRB=True` — swapping the channels back. It was the only site feeding the
   model BGR while every stored vector came from RGB. Same pixels, same face:
   cosine 0.9428 instead of 1.0000.

The rules this module enforces:

* **Landmarks come from the detector or the request fails.** There is no code
  here that constructs a coordinate. `tests/test_face_extraction_shared.py`
  scans for that and so does the enrollment suite.
* **Detection is retried on a padded canvas, never loosened.** `conf_thres` and
  the 640x640 input size are the detector's own; padding changes framing, not
  the decision rule.
* **Coordinates are always in the ORIGINAL image's frame.** Callers slice crops
  and warp alignments out of the array they passed in, so the retry translates
  its results back before returning them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Padding quadruples the array. Neither search route validates decoded
# dimensions (only MAX_FILE_SIZE bounds the upload), so a 10000x10000 PNG would
# mean a ~1.2 GB transient allocation per concurrent request. Above this budget
# the retry is skipped: an image this large that yielded no detections is not a
# tight face crop, which is the only thing the retry exists to rescue.
PADDED_RETRY_MAX_PIXELS = 12_000_000

# Detection is uncapped by default so that "how many faces are in this image?"
# has a truthful answer. A `max_num` cap silently discards the count, which is
# how a two-person photo could be enrolled as whichever face scored highest.
UNCAPPED = 0

_MULTIPLE_REJECT = "reject"
_MULTIPLE_BEST = "best"


class FaceExtractionError(Exception):
    """No usable face — with a machine-readable reason.

    `code` is one of:
      no_face         nothing detected, padded retry included
      no_landmarks    a face was detected but its keypoints are unusable
      multiple_faces  more than one face where exactly one was required
    """

    def __init__(self, code: str, message: str, *, details: Optional[str] = None,
                 faces_found: int = 0, padded_retry: bool = False,
                 status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.faces_found = faces_found
        self.padded_retry = padded_retry
        self.status_code = status_code


@dataclass(frozen=True)
class DetectedFace:
    """One real detection, in the coordinate frame of the image passed in."""

    bbox: np.ndarray          # (4,) float32 — x1, y1, x2, y2
    landmarks: np.ndarray     # (5,2) float32 — eyes, nose, mouth corners
    score: float
    index: int
    padded_retry: bool        # provenance: found only after padding

    def bbox_int(self, image_shape) -> Tuple[int, int, int, int]:
        """Integer bbox clamped to the image — the only safe way to slice.

        A face recovered by the padded retry can legitimately have a bbox that
        extends past the original frame, because the detector saw it against the
        border. Clamping only the low end (`image[max(0, y1):y2]`) is not
        enough: an x1 beyond the width yields an EMPTY crop, and an empty crop
        scored for quality produces a confident-looking number about nothing.
        """
        height, width = image_shape[:2]
        x1 = int(max(0, min(width - 1, np.floor(self.bbox[0]))))
        y1 = int(max(0, min(height - 1, np.floor(self.bbox[1]))))
        x2 = int(max(x1 + 1, min(width, np.ceil(self.bbox[2]))))
        y2 = int(max(y1 + 1, min(height, np.ceil(self.bbox[3]))))
        return x1, y1, x2, y2


def validate_landmarks(landmarks: Any) -> np.ndarray:
    """Accept the detector's keypoints, or refuse to guess.

    Deliberately minimal. It rejects arrays that are structurally unusable and
    nothing else — no bounds check (a padded-retry face maps slightly outside
    the frame by design), no eye-above-mouth rule (fails on rotated faces), no
    symmetry or inter-ocular ratio (fails on profiles). Anything more would be a
    second, weaker face detector second-guessing the real one, which is exactly
    the reasoning that produced the fabricated-landmark bug.
    """
    if landmarks is None:
        raise FaceExtractionError(
            "no_landmarks", "Facial landmarks could not be detected.",
            details="The detector returned a face without usable keypoints.")

    points = np.asarray(landmarks, dtype=np.float32)
    if points.shape != (5, 2):
        raise FaceExtractionError(
            "no_landmarks", "Facial landmarks could not be detected.",
            details=f"Expected 5 keypoints, got an array of shape {points.shape}.")
    if not np.all(np.isfinite(points)):
        raise FaceExtractionError(
            "no_landmarks", "Facial landmarks could not be detected.",
            details="The detected keypoints contain non-finite values.")
    # All-zero / all-identical / collapsed arrays. A real 5-point face spans
    # far more than a pixel in both axes.
    if float(np.ptp(points, axis=0).min()) <= 1.0:
        raise FaceExtractionError(
            "no_landmarks", "Facial landmarks could not be detected.",
            details="The detected keypoints are degenerate (no spatial extent).")
    return points


def _manager(manager=None):
    """The shared detector/recognizer singleton.

    Imported lazily: `backend/core/__init__.py` eagerly imports fifteen modules,
    so a module-level import here would join that cycle. `advanced_search` and
    `IdentityLoader` are constructed with an injected manager and pass it in.
    """
    if manager is None:
        from backend.core import model_manager as manager  # noqa: PLC0415
    if not getattr(manager, "_initialized", False):
        logger.info("[FACE] model_manager not initialized; initializing")
        manager.initialize()
    return manager


def _faces_from_detection(bboxes, kpss, *, padded_retry: bool,
                          offset: float = 0.0) -> List[DetectedFace]:
    """Turn raw detector output into validated DetectedFace records.

    `offset` is subtracted from every coordinate, which is how a padded-canvas
    detection is mapped back onto the original image.
    """
    if kpss is None or len(kpss) == 0:
        return []

    faces: List[DetectedFace] = []
    for index in range(len(kpss)):
        points = validate_landmarks(kpss[index])
        if offset:
            points = points - np.float32(offset)

        if bboxes is not None and len(bboxes) > index:
            raw = np.asarray(bboxes[index], dtype=np.float32)
            box = raw[:4].copy()
            score = float(raw[4]) if raw.shape[0] > 4 else 0.0
            if offset:
                box -= np.float32(offset)
        else:
            # The detector always returns boxes alongside keypoints; fall back
            # to the keypoint hull rather than inventing a frame-sized box.
            box = np.array([points[:, 0].min(), points[:, 1].min(),
                            points[:, 0].max(), points[:, 1].max()],
                           dtype=np.float32)
            score = 0.0

        faces.append(DetectedFace(bbox=box, landmarks=points, score=score,
                                  index=index, padded_retry=padded_retry))
    return faces


def extract_faces(image, *, max_num: int = UNCAPPED, metric: str = "max",
                  allow_padded_retry: bool = True, manager=None) -> List[DetectedFace]:
    """Every real face in `image`, in the image's own coordinates.

    When the first pass finds nothing and `allow_padded_retry` is set, detection
    is retried once on a black-bordered canvas. That is not a relaxed threshold:
    a face that fills the frame is larger than the stride-32 anchors can regress
    after SCRFD letterboxes to 640x640, and giving it margin is what lets the
    SAME detector at the SAME confidence see it.

    The retry is keyed on "found nothing", not on image size. The size test it
    replaces — `h <= 200 and w <= 200 and abs(h-w) <= 20` — missed a 300x300 or
    480x480 crop, so those enrolled fine and then failed to search. Framing is
    the property that matters and pixel dimensions do not carry it.
    """
    import cv2  # noqa: PLC0415 — heavyweight, and this module is imported at startup

    active = _manager(manager)

    bboxes, kpss = active.detector.detect(image, max_num=max_num, metric=metric)
    faces = _faces_from_detection(bboxes, kpss, padded_retry=False)
    if faces or not allow_padded_retry:
        return faces

    height, width = image.shape[:2]
    if height * width > PADDED_RETRY_MAX_PIXELS:
        logger.info("[FACE] padded retry skipped: %dx%d exceeds the %d-pixel budget",
                    width, height, PADDED_RETRY_MAX_PIXELS)
        return []

    pad = max(height, width) // 2
    if pad <= 0:
        return []

    padded = cv2.copyMakeBorder(image, pad, pad, pad, pad,
                                cv2.BORDER_CONSTANT, value=(0, 0, 0))
    bboxes, kpss = active.detector.detect(padded, max_num=max_num, metric=metric)
    faces = _faces_from_detection(bboxes, kpss, padded_retry=True, offset=float(pad))
    if faces:
        logger.info("[FACE] %d face(s) found on padded retry (pad=%d, source %dx%d)",
                    len(faces), pad, width, height)
    return faces


def select_largest(faces: List[DetectedFace]) -> DetectedFace:
    """The biggest face, which is what `detect(max_num=1)` already returned.

    SCRFD's cap sorts by `area` descending and keeps the first (models/scrfd.py:
    169-185, `metric="max"`). Reproducing that rule here means a call site can
    move from `max_num=1` to uncapped detection — and so learn how many faces
    were really present — while still choosing the identical face.
    """
    if not faces:
        raise FaceExtractionError("no_face", "No face was detected in the image.")
    return max(faces, key=lambda f: float((f.bbox[2] - f.bbox[0]) *
                                          (f.bbox[3] - f.bbox[1])))


def extract_single_face(image, *, on_multiple: str = _MULTIPLE_REJECT,
                        allow_padded_retry: bool = True,
                        manager=None) -> DetectedFace:
    """Exactly one face, or a coded refusal.

    `on_multiple="reject"` is the enrollment contract: a second person must not
    be silently discarded, because the stored signature would then belong to
    whichever face happened to score highest. `on_multiple="best"` preserves the
    existing behaviour of the read-only paths, which have always searched or
    scored the largest face in the frame.
    """
    faces = extract_faces(image, max_num=UNCAPPED,
                          allow_padded_retry=allow_padded_retry, manager=manager)

    if not faces:
        raise FaceExtractionError(
            "no_face", "No face was detected in the image.",
            details=("The image does not contain a detectable face. Please try a "
                     "different image with better lighting and a clear view of "
                     "the person's face."),
            padded_retry=allow_padded_retry)

    if len(faces) > 1 and on_multiple == _MULTIPLE_REJECT:
        raise FaceExtractionError(
            "multiple_faces",
            f"This image contains {len(faces)} faces. Exactly one is required.",
            details=("An image used to build a face signature must show a single "
                     "person, otherwise the signature could belong to the wrong "
                     "one."),
            faces_found=len(faces))

    return select_largest(faces)


def embed_face(image, face: DetectedFace, *, manager=None) -> np.ndarray:
    """The 512-d embedding for `face`, from the ORIGINAL image and real landmarks.

    `ArcFace.get_embedding` aligns internally with the landmarks it is given and
    applies `swapRB=True` to a BGR array. Passing a pre-aligned crop, or an
    RGB-converted one, produces a vector in a different space from every stored
    embedding — which is precisely what `/api/search/by-image` used to do.
    """
    active = _manager(manager)
    return active.recognizer.get_embedding(image, face.landmarks)


def embed_face_normalized(image, face: DetectedFace, *, manager=None) -> np.ndarray:
    """`embed_face` L2-normalized, which is the form every index compares."""
    embedding = embed_face(image, face, manager=manager)
    if embedding is None:
        raise FaceExtractionError(
            "no_landmarks", "Failed to generate a face embedding.",
            details="The recognizer returned nothing for this face.")
    embedding = np.asarray(embedding, dtype=np.float32).flatten()
    norm = float(np.linalg.norm(embedding))
    if not np.isfinite(norm) or norm == 0.0:
        raise FaceExtractionError(
            "no_landmarks", "Failed to generate a face embedding.",
            details="The generated embedding has no magnitude.")
    return embedding / norm


def error_payload(exc: FaceExtractionError) -> dict:
    """The HTTP body for a refusal.

    `detail` stays a plain STRING on purpose. The admin UI renders
    `error.detail || 'Search failed'` (frontend/js/admin-unknown.js), so a dict
    here would reach the user as "[object Object]". The machine-readable fields
    ride alongside it.
    """
    body = {
        "detail": exc.message,
        "error": exc.code,
        "faces_detected": exc.faces_found,
        "padded_retry": exc.padded_retry,
    }
    if exc.details:
        body["details"] = exc.details
    return body
