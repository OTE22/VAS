"""
Model Manager
=============
Singleton for managing ML models.
"""

import os
import sys
import asyncio
import logging
from typing import Optional, Tuple

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from models import SCRFD, ArcFace

logger = logging.getLogger(__name__)


class ModelManager:
    """Singleton for managing ML models.

    Owns exactly two things: the SCRFD detector and the ArcFace recognizer.
    It used to also load a legacy display-name-keyed FaceDatabase (FAISS) on
    every startup; that store was write-never under pgvector, so the degraded
    fallback that read it could only ever answer "Unknown" — while carrying
    stale vectors keyed by a representation (display names) the identity
    system no longer uses. The chain was deleted, not shimmed: enrollment and
    recognition are owned by the identity system (pgvector, or the vector_index
    contract under VECTOR_BACKEND=faiss).
    """

    def __init__(self):
        self.detector: Optional[SCRFD] = None
        self.recognizer: Optional[ArcFace] = None
        self._initialized = False
        self._lock = asyncio.Lock()

    def initialize(self):
        """Initialize models (thread-safe)"""
        if self._initialized:
            return

        logger.info("🔄 Initializing models...")

        self.detector = SCRFD(
            settings.DETECTION_MODEL,
            input_size=(640, 640),
            conf_thres=settings.CONFIDENCE_THRESHOLD,
        )

        self.recognizer = ArcFace(settings.RECOGNITION_MODEL)

        self._initialized = True
        logger.info("✅ Models initialized successfully")

    def health_check(self) -> bool:
        """Check if models are healthy"""
        return self._initialized and self.detector is not None and self.recognizer is not None


model_manager = ModelManager()

