"""
Face Quality Scoring Service
=============================
Evaluates face image quality before search to improve results and provide user feedback.
"""

import cv2
import numpy as np
import logging
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
from config import settings

logger = logging.getLogger(__name__)


@dataclass
class QualityAssessment:
    """Face quality assessment result"""
    overall_score: float
    band: str  # "excellent", "good", "moderate", "poor", "unusable"
    details: Dict[str, Dict]
    warnings: list
    proceed_recommendation: bool
    

class FaceQualityScorer:
    """
    Evaluates face image quality based on multiple metrics:
    - Blur detection (Laplacian variance)
    - Lighting analysis (histogram distribution)
    - Face size relative to image
    - Face angle estimation (from landmarks)
    """
    
    # Quality weights
    WEIGHTS = {
        'blur': 0.30,
        'lighting': 0.25,
        'face_size': 0.20,
        'angle': 0.25
    }
    
    # Quality bands
    BANDS = {
        'excellent': (0.85, 1.00),
        'good': (0.70, 0.84),
        'moderate': (0.50, 0.69),
        'poor': (0.30, 0.49),
        'unusable': (0.00, 0.29)
    }
    
    def __init__(self):
        self.min_threshold = settings.SEARCH_MIN_QUALITY_THRESHOLD
        self.warning_threshold = settings.SEARCH_QUALITY_WARNING_THRESHOLD
    
    def assess_quality(
        self,
        face_image: np.ndarray,
        bbox: Optional[Tuple[int, int, int, int]] = None,
        landmarks: Optional[np.ndarray] = None,
        full_image: Optional[np.ndarray] = None
    ) -> QualityAssessment:
        """
        Assess face image quality.
        
        Args:
            face_image: Cropped face image (BGR)
            bbox: Bounding box (x1, y1, x2, y2) if available
            landmarks: 5 facial landmarks if available
            full_image: Full original image if available (for size calculation)
            
        Returns:
            QualityAssessment with scores and recommendations
        """
        details = {}
        warnings = []
        
        # 1. Blur Detection
        blur_score, blur_info = self._assess_blur(face_image)
        details['blur'] = blur_info
        if blur_score < 0.5:
            warnings.append(f"⚠️ {blur_info.get('issue', 'Image appears blurry')}")
        
        # 2. Lighting Analysis
        lighting_score, lighting_info = self._assess_lighting(face_image)
        details['lighting'] = lighting_info
        if lighting_score < 0.5:
            warnings.append(f"⚠️ {lighting_info.get('issue', 'Poor lighting detected')}")
        
        # 3. Face Size
        if full_image is not None and bbox is not None:
            size_score, size_info = self._assess_face_size(bbox, full_image.shape)
        else:
            size_score, size_info = self._assess_face_size_from_crop(face_image)
        details['face_size'] = size_info
        if size_score < 0.5:
            warnings.append(f"⚠️ {size_info.get('issue', 'Face size not optimal')}")
        
        # 4. Face Angle (if landmarks available)
        if landmarks is not None:
            angle_score, angle_info = self._assess_angle(landmarks)
        else:
            angle_score, angle_info = self._estimate_angle_from_image(face_image)
        details['angle'] = angle_info
        if angle_score < 0.5:
            warnings.append(f"⚠️ {angle_info.get('issue', 'Face angle not optimal')}")
        
        # Calculate overall score
        overall_score = (
            self.WEIGHTS['blur'] * blur_score +
            self.WEIGHTS['lighting'] * lighting_score +
            self.WEIGHTS['face_size'] * size_score +
            self.WEIGHTS['angle'] * angle_score
        )
        
        # Determine band
        band = self._get_band(overall_score)
        
        # Add general warnings based on overall score
        if overall_score < self.warning_threshold:
            warnings.append(f"⚠️ Overall quality {overall_score:.0%} - search results may be less accurate")
        
        if overall_score < self.min_threshold:
            warnings.append("❌ Quality too low for reliable search")
        
        # Determine if search should proceed
        proceed = overall_score >= self.min_threshold
        
        # Convert numpy types to Python native types in details
        def convert_numpy_types(obj):
            """Recursively convert numpy types to Python native types."""
            if isinstance(obj, np.integer):
                return int(obj)
            elif isinstance(obj, np.floating):
                return float(obj)
            elif isinstance(obj, np.ndarray):
                return obj.tolist()
            elif isinstance(obj, dict):
                return {key: convert_numpy_types(value) for key, value in obj.items()}
            elif isinstance(obj, (list, tuple)):
                return [convert_numpy_types(item) for item in obj]
            return obj
        
        # Convert details to ensure all values are Python native types
        converted_details = convert_numpy_types(details)
        
        return QualityAssessment(
            overall_score=float(round(overall_score, 3)),
            band=band,
            details=converted_details,
            warnings=warnings,
            proceed_recommendation=proceed
        )
    
    def _assess_blur(self, image: np.ndarray) -> Tuple[float, Dict]:
        """
        Assess image blur using Laplacian variance.
        Higher variance = sharper image.
        """
        try:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
            laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
            
            # Normalize: typical range 0-1000, with 100+ being sharp
            # Score mapping: <50 = poor, 50-100 = moderate, 100-200 = good, >200 = excellent
            if laplacian_var > 200:
                score = 1.0
            elif laplacian_var > 100:
                score = 0.7 + 0.3 * ((laplacian_var - 100) / 100)
            elif laplacian_var > 50:
                score = 0.4 + 0.3 * ((laplacian_var - 50) / 50)
            else:
                score = 0.4 * (laplacian_var / 50)
            
            info = {
                'score': round(score, 3),
                'laplacian_variance': round(laplacian_var, 2)
            }
            
            if score < 0.5:
                info['issue'] = 'Motion blur or out of focus detected'
                info['recommendation'] = 'Use a clearer frame from the video'
            
            return score, info
            
        except Exception as e:
            logger.warning(f"Error assessing blur: {e}")
            return 0.5, {'score': 0.5, 'error': str(e)}
    
    def _assess_lighting(self, image: np.ndarray) -> Tuple[float, Dict]:
        """
        Assess lighting quality using histogram analysis.
        Good lighting = well-distributed histogram without extremes.
        """
        try:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
            hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
            hist = hist.flatten() / hist.sum()
            
            # Check for over/under exposure
            dark_ratio = hist[:50].sum()
            bright_ratio = hist[205:].sum()
            mid_ratio = hist[50:205].sum()
            
            # Calculate spread (entropy-like measure)
            non_zero = hist[hist > 0]
            entropy = -np.sum(non_zero * np.log2(non_zero + 1e-10))
            normalized_entropy = entropy / 8.0  # Max entropy for 256 bins is 8
            
            # Penalize extremes
            score = normalized_entropy
            if dark_ratio > 0.5:
                score *= (1 - dark_ratio + 0.5)
            if bright_ratio > 0.3:
                score *= (1 - bright_ratio + 0.7)
            
            score = min(1.0, max(0.0, score))
            
            info = {
                'score': round(score, 3),
                'dark_ratio': round(dark_ratio, 3),
                'bright_ratio': round(bright_ratio, 3),
                'mid_ratio': round(mid_ratio, 3)
            }
            
            if dark_ratio > 0.5:
                info['issue'] = 'Image is too dark'
                info['recommendation'] = 'Use an image with better lighting'
            elif bright_ratio > 0.3:
                info['issue'] = 'Image is overexposed'
                info['recommendation'] = 'Use an image with less direct light'
            
            return score, info
            
        except Exception as e:
            logger.warning(f"Error assessing lighting: {e}")
            return 0.5, {'score': 0.5, 'error': str(e)}
    
    def _assess_face_size(
        self,
        bbox: Tuple[int, int, int, int],
        image_shape: Tuple[int, ...]
    ) -> Tuple[float, Dict]:
        """
        Assess face size relative to full image.
        Optimal: face is 20-70% of image area.
        """
        try:
            x1, y1, x2, y2 = bbox
            face_area = (x2 - x1) * (y2 - y1)
            image_area = image_shape[0] * image_shape[1]
            face_ratio = face_area / image_area
            
            # Optimal range: 0.15-0.70
            if 0.20 <= face_ratio <= 0.60:
                score = 1.0
            elif 0.15 <= face_ratio < 0.20 or 0.60 < face_ratio <= 0.70:
                score = 0.8
            elif 0.10 <= face_ratio < 0.15 or 0.70 < face_ratio <= 0.80:
                score = 0.6
            elif 0.05 <= face_ratio < 0.10:
                score = 0.4
            else:
                score = 0.2
            
            info = {
                'score': round(score, 3),
                'face_ratio': round(face_ratio, 3),
                'face_pixels': face_area,
                'image_pixels': image_area
            }
            
            if face_ratio < 0.10:
                info['issue'] = 'Face is too small in the image'
                info['recommendation'] = 'Crop the image closer to the face'
            elif face_ratio > 0.80:
                info['issue'] = 'Face is too close/cropped'
                info['recommendation'] = 'Use a wider shot showing full face'
            
            return score, info
            
        except Exception as e:
            logger.warning(f"Error assessing face size: {e}")
            return 0.5, {'score': 0.5, 'error': str(e)}
    
    def _assess_face_size_from_crop(self, face_crop: np.ndarray) -> Tuple[float, Dict]:
        """
        Assess quality from cropped face image dimensions.
        """
        try:
            h, w = face_crop.shape[:2]
            pixels = h * w
            
            # Optimal: 100x100 to 300x300
            if 112 <= min(h, w) <= 400:
                score = 1.0
            elif 80 <= min(h, w) < 112:
                score = 0.7
            elif 50 <= min(h, w) < 80:
                score = 0.5
            else:
                score = 0.3
            
            info = {
                'score': round(score, 3),
                'width': w,
                'height': h,
                'pixels': pixels
            }
            
            if min(h, w) < 80:
                info['issue'] = 'Face resolution too low'
                info['recommendation'] = 'Use a higher resolution image'
            
            return score, info
            
        except Exception as e:
            logger.warning(f"Error assessing face size from crop: {e}")
            return 0.5, {'score': 0.5, 'error': str(e)}
    
    def _assess_angle(self, landmarks: np.ndarray) -> Tuple[float, Dict]:
        """
        Assess face angle from 5-point landmarks.
        Landmarks: [left_eye, right_eye, nose, left_mouth, right_mouth]
        """
        try:
            if len(landmarks) < 5:
                return 0.5, {'score': 0.5, 'error': 'Insufficient landmarks'}
            
            left_eye = landmarks[0]
            right_eye = landmarks[1]
            nose = landmarks[2]
            
            # Calculate eye center
            eye_center = (left_eye + right_eye) / 2
            
            # Calculate yaw (left-right rotation)
            eye_distance = np.linalg.norm(right_eye - left_eye)
            nose_offset = nose[0] - eye_center[0]
            yaw_ratio = abs(nose_offset) / (eye_distance + 1e-6)
            
            # Calculate roll (head tilt)
            eye_angle = np.arctan2(right_eye[1] - left_eye[1], right_eye[0] - left_eye[0])
            roll_degrees = abs(np.degrees(eye_angle))
            
            # Score based on deviation from frontal
            yaw_score = max(0, 1 - yaw_ratio * 2)
            roll_score = max(0, 1 - roll_degrees / 30)
            
            score = (yaw_score + roll_score) / 2
            
            info = {
                'score': round(score, 3),
                'yaw_ratio': round(yaw_ratio, 3),
                'roll_degrees': round(roll_degrees, 2)
            }
            
            if score < 0.5:
                if yaw_ratio > 0.3:
                    info['issue'] = f'Face is turned sideways (profile view)'
                else:
                    info['issue'] = f'Head is tilted {roll_degrees:.0f}°'
                info['recommendation'] = 'Use a more frontal face image'
            
            return score, info
            
        except Exception as e:
            logger.warning(f"Error assessing angle: {e}")
            return 0.5, {'score': 0.5, 'error': str(e)}
    
    def _estimate_angle_from_image(self, face_image: np.ndarray) -> Tuple[float, Dict]:
        """
        Estimate face angle without landmarks using symmetry analysis.
        """
        try:
            gray = cv2.cvtColor(face_image, cv2.COLOR_BGR2GRAY) if len(face_image.shape) == 3 else face_image
            h, w = gray.shape
            
            # Compare left and right halves for symmetry
            left_half = gray[:, :w//2]
            right_half = cv2.flip(gray[:, w//2:], 1)
            
            # Resize to same dimensions if needed
            min_w = min(left_half.shape[1], right_half.shape[1])
            left_half = left_half[:, :min_w]
            right_half = right_half[:, :min_w]
            
            # Calculate similarity
            diff = cv2.absdiff(left_half, right_half)
            symmetry_score = 1 - (diff.mean() / 255)
            
            # Map to quality score
            score = min(1.0, symmetry_score * 1.2)  # Boost slightly
            
            info = {
                'score': round(score, 3),
                'symmetry': round(symmetry_score, 3),
                'method': 'symmetry_estimation'
            }
            
            if score < 0.5:
                info['issue'] = 'Face appears to be at an angle'
                info['recommendation'] = 'Use a more frontal face image'
            
            return score, info
            
        except Exception as e:
            logger.warning(f"Error estimating angle: {e}")
            return 0.5, {'score': 0.5, 'error': str(e)}
    
    def _get_band(self, score: float) -> str:
        """Get quality band name from score."""
        for band, (low, high) in self.BANDS.items():
            if low <= score <= high:
                return band
        return 'unusable'


# Global instance
face_quality_scorer = FaceQualityScorer()


def assess_face_quality(
    face_image: np.ndarray,
    bbox: Optional[Tuple[int, int, int, int]] = None,
    landmarks: Optional[np.ndarray] = None,
    full_image: Optional[np.ndarray] = None
) -> QualityAssessment:
    """
    Convenience function to assess face quality.
    
    Args:
        face_image: Cropped face image (BGR)
        bbox: Bounding box (x1, y1, x2, y2) if available
        landmarks: 5 facial landmarks if available
        full_image: Full original image if available
        
    Returns:
        QualityAssessment with scores and recommendations
    """
    return face_quality_scorer.assess_quality(face_image, bbox, landmarks, full_image)


