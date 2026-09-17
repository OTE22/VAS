"""Cheap admission checks on the actual 112x112 camera recognition input.

Separate from the weighted quality score: good pose/size cannot compensate
for severe blur or missing source pixels. Dark pixels are never padding tests.
"""
import cv2
import numpy as np

from config import settings


def assess_aligned_face(image, source_shape, inverse_matrix, landmarks, face_bbox):
    result = {'accepted': False, 'reason': 'invalid_alignment', 'version': 'aligned-v1'}
    if not settings.CAMERA_FACE_ACCEPTANCE_ENABLED:
        return {**result, 'accepted': True, 'reason': 'disabled'}
    try:
        if image is None or image.shape != (112, 112, 3) or image.dtype != np.uint8:
            return result
        height, width = source_shape[:2]
        inverse = np.asarray(inverse_matrix, dtype=np.float64)
        points = np.asarray(landmarks, dtype=np.float64)
        box = np.asarray(face_bbox, dtype=np.float64)
        if (inverse.shape != (2, 3) or points.shape != (5, 2) or box.shape != (4,)
                or not all(np.isfinite(x).all() for x in (inverse, points, box))
                or abs(np.linalg.det(inverse[:, :2])) < 1e-8):
            return result
        visible_width = min(width, box[2]) - max(0, box[0])
        visible_height = min(height, box[3]) - max(0, box[1])
        if min(visible_width, visible_height) < 8:
            return {**result, 'reason': 'insufficient_source_face'}
        # Required facial landmarks must be inside the supplied source crop.
        if ((points[:, 0] < 0).any() or (points[:, 0] >= width).any()
                or (points[:, 1] < 0).any() or (points[:, 1] >= height).any()):
            return {**result, 'reason': 'landmarks_outside_source'}

        # Fixed-size coordinate projection avoids allocating a 4K source mask.
        # This region contains the canonical eyes, nose and mouth, with margin.
        yy, xx = np.mgrid[20:100, 20:92]
        sx = inverse[0, 0] * xx + inverse[0, 1] * yy + inverse[0, 2]
        sy = inverse[1, 0] * xx + inverse[1, 1] * yy + inverse[1, 2]
        valid = (sx >= 0) & (sx <= width - 1) & (sy >= 0) & (sy <= height - 1)
        coverage = float(valid.mean())
        result['source_coverage'] = round(coverage, 4)
        if coverage < settings.CAMERA_FACE_MIN_SOURCE_COVERAGE:
            return {**result, 'reason': 'insufficient_source_coverage'}

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)[20:100, 20:92]
        # A little smoothing reduces noise-driven false sharpness. Exclude
        # padding boundaries and the ROI edge, which can otherwise score high.
        interior = cv2.erode(valid.astype(np.uint8), np.ones((5, 5), np.uint8),
                             borderType=cv2.BORDER_CONSTANT, borderValue=0).astype(bool)
        if not interior.any():
            return {**result, 'reason': 'insufficient_source_coverage'}
        lap = cv2.Laplacian(cv2.GaussianBlur(gray, (3, 3), 0), cv2.CV_64F)
        sharpness = float(lap[interior].var())
        result['sharpness'] = round(sharpness, 4)
        if not np.isfinite(sharpness) or sharpness < settings.CAMERA_FACE_MIN_ALIGNED_SHARPNESS:
            return {**result, 'reason': 'severe_blur'}
        return {**result, 'accepted': True, 'reason': 'accepted'}
    except (TypeError, ValueError, IndexError, cv2.error):
        # Failure here must never fall through to the permissive legacy score.
        return result
