"""
Model Manager
=============
Singleton for managing ML models.
"""

import os
import sys
import asyncio
import logging
import cv2
from typing import Optional, Tuple

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from database import FaceDatabase
from models import SCRFD, ArcFace
import faiss

logger = logging.getLogger(__name__)


class ModelManager:
    """Singleton for managing ML models"""

    def __init__(self):
        self.detector: Optional[SCRFD] = None
        self.recognizer: Optional[ArcFace] = None
        self.face_db: Optional[FaceDatabase] = None
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

        # Optimize FAISS workers based on GPU availability
        # GPU can handle more parallel searches efficiently
        try:
            from utils.gpu_detection import get_faiss_backend
            faiss_backend = get_faiss_backend()
            if faiss_backend == 'gpu':
                max_workers = 16  # GPU can handle more parallel operations
                logger.info("Using GPU-optimized FAISS workers: 16")
            else:
                max_workers = 8  # CPU: more workers for parallel processing
                logger.info("Using CPU-optimized FAISS workers: 8")
        except Exception:
            max_workers = 8  # Default fallback
            logger.info("Using default FAISS workers: 8")
        
        self.face_db = FaceDatabase(
            db_path=settings.DB_PATH,
            max_workers=max_workers,
        )

        # Load or build face database
        loaded = self.face_db.load()
        total_faces_in_db = self.face_db.index.ntotal if loaded and hasattr(self.face_db, "index") and self.face_db.index else 0
        logger.info(f"Face database loaded: {loaded}, total faces: {total_faces_in_db}")
        
        # Count faces in storage/faces directory
        faces_in_dir = 0
        if os.path.exists(settings.FACES_DIR):
            faces_in_dir = len([f for f in os.listdir(settings.FACES_DIR) 
                                if f.lower().endswith((".jpg", ".jpeg", ".png"))])
        
        # Rebuild if database is empty, doesn't exist, or if new faces were added
        if not loaded or total_faces_in_db == 0:
            logger.info(f"Building face database (empty or not found)...")
            self._build_face_database()
        elif faces_in_dir > total_faces_in_db:
            logger.info(f"New faces detected in {settings.FACES_DIR} ({faces_in_dir} files vs {total_faces_in_db} in DB). Rebuilding database...")
            self._build_face_database()
        else:
            logger.info(f"Face database up to date ({total_faces_in_db} faces)")

        self._initialized = True
        logger.info("✅ Models initialized successfully")

    def _build_face_database(self):
        """Build face database from images"""
        if not os.path.exists(settings.FACES_DIR):
            logger.warning(f"Faces directory not found: {settings.FACES_DIR}")
            self.face_db.save()
            return

        # Clear existing database before rebuilding
        # Create a new empty index to replace the old one
        self.face_db.index = faiss.IndexFlatIP(self.face_db.embedding_size)
        self.face_db.metadata = []
        logger.info("Cleared existing face database for rebuild")

        added = 0
        skipped = 0
        error_count = 0
        
        # Get all image files
        image_files = [f for f in os.listdir(settings.FACES_DIR) 
                      if f.lower().endswith((".jpg", ".jpeg", ".png"))]
        
        logger.info(f"Found {len(image_files)} image files in {settings.FACES_DIR}")
        
        for filename in image_files:
            name = filename.rsplit(".", 1)[0]
            image_path = os.path.join(settings.FACES_DIR, filename)

            image = cv2.imread(image_path)
            if image is None:
                logger.warning(f"Could not read image: {image_path}")
                skipped += 1
                continue

            bboxes, kpss = self.detector.detect(image, max_num=1)
            if kpss is None or len(kpss) == 0:
                logger.warning(f"No face detected in {filename} - skipping")
                skipped += 1
                continue

            try:
                embedding = self.recognizer.get_embedding(image, kpss[0])
                self.face_db.add_face(embedding, name)
                added += 1
                logger.info(f"✅ Added face: {name}")
            except Exception as e:
                logger.error(f"❌ Error adding {filename}: {e}")
                error_count += 1

        self.face_db.save()
        logger.info(f"✅ Face DB built: {added} faces added, {skipped} skipped (no face detected), {error_count} errors")

    def add_face_from_image(self, image_path: str, person_name: str) -> Tuple[bool, str]:
        """
        Add a single face from an image file incrementally (best practice).
        This is much more efficient than rebuilding the entire database.
        
        Args:
            image_path: Path to the image file
            person_name: Name of the person
            
        Returns:
            Tuple of (success: bool, message: str)
        """
        if not self._initialized:
            logger.warning("Model manager not initialized, initializing now...")
            self.initialize()
        
        if not os.path.exists(image_path):
            return False, f"Image file not found: {image_path}"
        
        try:
            # Read and validate image
            image = cv2.imread(image_path)
            if image is None:
                return False, f"Could not read image: {image_path}"
            
            # Detect face
            bboxes, kpss = self.detector.detect(image, max_num=1)
            if kpss is None or len(kpss) == 0:
                return False, f"No face detected in image: {os.path.basename(image_path)}"
            
            # Generate embedding
            embedding = self.recognizer.get_embedding(image, kpss[0])
            
            # Add face incrementally to database
            self.face_db.add_face(embedding, person_name)
            
            # Save database (incremental update)
            self.face_db.save()
            
            total_faces = self.face_db.index.ntotal if self.face_db and self.face_db.index else 0
            logger.info(f"✅ Incrementally added face '{person_name}' from {os.path.basename(image_path)}. Total faces: {total_faces}")
            
            return True, f"Successfully added {person_name} to face database"
            
        except Exception as e:
            logger.error(f"❌ Error adding face incrementally: {e}", exc_info=True)
            return False, f"Failed to add face: {str(e)}"

    def health_check(self) -> bool:
        """Check if models are healthy"""
        return self._initialized and self.detector is not None and self.recognizer is not None


model_manager = ModelManager()

