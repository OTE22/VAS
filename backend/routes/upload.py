"""
Upload Routes
=============
Routes for uploading person photos.

Now integrates with Identity system and pgvector for persistent database storage.
"""

import os
import sys
import logging
import uuid
import numpy as np
import cv2
from datetime import datetime
from fastapi import APIRouter, File, UploadFile, Form, HTTPException, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from backend.config import MAX_FILE_SIZE, ALLOWED_IMAGE_EXTENSIONS
from backend.core import model_manager
from backend.routes.utils import validate_image_content
from db_models import User, Identity, IdentityType, IdentityStatus
from backend.auth.auth_service import require_role
from db_connection import get_db
from backend.utils.path_utils import normalize_storage_path

# Import identity_service (may be None if not initialized)
def get_identity_service():
    """Get identity_service instance (may be None if not initialized)"""
    try:
        # Try from backend.core first (set during startup)
        import backend.core
        import sys
        
        # Check if it's set in the module
        if hasattr(backend.core, 'identity_service') and backend.core.identity_service is not None:
            logger.debug(f"[UPLOAD] Found identity_service in backend.core")
            return backend.core.identity_service
        
        # Try to get from sys.modules (more reliable)
        core_module = sys.modules.get('backend.core')
        if core_module and hasattr(core_module, 'identity_service') and core_module.identity_service is not None:
            logger.debug(f"[UPLOAD] Found identity_service in sys.modules['backend.core']")
            return core_module.identity_service
    except (ImportError, AttributeError) as e:
        logger.debug(f"[UPLOAD] Could not get identity_service from backend.core: {e}")
    
    try:
        # Fallback to direct import
        from backend.core.identity_service import identity_service
        if identity_service is not None:
            logger.debug(f"[UPLOAD] Found identity_service from direct import")
            return identity_service
    except (ImportError, AttributeError) as e:
        logger.debug(f"[UPLOAD] Could not import identity_service directly: {e}")
    
    logger.warning("[UPLOAD] ⚠️ identity_service is None - embeddings will not be saved to database")
    return None

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/upload-person")
async def upload_person(
    person_name: str = Form(...),
    photo: UploadFile = File(...),
    is_face_image: bool = Form(False),  # Toggle: True = this is already a face image (skip detection), False = full image (detect face)
    current_user: User = Depends(require_role(["admin"])),
    db: AsyncSession = Depends(get_db)
):
    """
    Upload a new person's photo to the face recognition database.
    
    Now integrates with Identity system and pgvector:
    - Creates Identity record in database (type=KNOWN)
    - Stores embedding in PostgreSQL using pgvector (if enabled)
    - Also updates FAISS for backward compatibility
    - Sets best_snapshot_path to uploaded image
    """
    logger.info(f"📤 Upload request from user '{current_user.username}' (ID: {current_user.id}) for person: {person_name}")
    
    try:
        # Validate person name first (before processing file)
        if not person_name or not person_name.strip():
            logger.warning(f"❌ Empty person name provided by user {current_user.username}")
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Person name is required."}
            )

        # Sanitize person name for filename
        safe_name = "".join(c for c in person_name if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_name = safe_name.replace(' ', '_').lower()

        if not safe_name or len(safe_name) < 2:
            logger.warning(f"❌ Invalid person name '{person_name}' provided by user {current_user.username}")
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Invalid person name. Name must be at least 2 characters and contain only letters, numbers, spaces, dashes, or underscores."}
            )

        # Validate file was provided
        if not photo or not photo.filename:
            logger.warning(f"❌ No file provided by user {current_user.username}")
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Please select a photo to upload."}
            )

        # Validate file type
        if not photo.content_type or not photo.content_type.startswith('image/'):
            logger.warning(f"❌ Invalid file type '{photo.content_type}' provided by user {current_user.username}")
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Invalid file type. Please upload an image (JPG, PNG, WEBP)."}
            )

        # Validate file size
        contents = await photo.read()
        file_size = len(contents)
        
        if file_size == 0:
            logger.warning(f"❌ Empty file provided by user {current_user.username}")
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "The uploaded file is empty. Please select a valid image file."}
            )
        
        if file_size > MAX_FILE_SIZE:
            logger.warning(f"❌ File too large ({file_size / 1024 / 1024:.2f}MB) from user {current_user.username}")
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": f"File too large. Maximum size is {MAX_FILE_SIZE/1024/1024:.1f}MB."}
            )

        # Validate image content
        if not validate_image_content(contents):
            logger.warning(f"❌ Invalid image content from user {current_user.username} (file: {photo.filename})")
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Invalid image file. Please upload a valid image (JPG, PNG, WEBP)."}
            )

        # Determine file extension
        file_extension = ".jpg"
        if photo.filename:
            ext = os.path.splitext(photo.filename)[1].lower()
            if ext in ALLOWED_IMAGE_EXTENSIONS:
                file_extension = ext

        # Create faces directory if it doesn't exist
        faces_dir = settings.FACES_DIR
        logger.info(f"📁 Using FACES_DIR: {faces_dir} (from settings)")
        os.makedirs(faces_dir, exist_ok=True)

        # Save file with clean name (no timestamp) - use counter only if file exists
        # This keeps filenames clean: "john_doe.jpg", "john_doe_2.jpg", etc.
        # Create folder for this person: faces/John_Doe/
        person_folder = os.path.join(faces_dir, safe_name)
        os.makedirs(person_folder, exist_ok=True)
        logger.info(f"📁 Created/verified person folder: {person_folder}")

        # Save file with clean name inside person's folder
        # Structure: faces/John_Doe/image1.jpg, faces/John_Doe/image2.jpg, etc.
        base_file_path = os.path.join(person_folder, f"image1{file_extension}")
        file_path = base_file_path
        
        # If file already exists, increment counter (for multiple images per person)
        counter = 1
        while os.path.exists(file_path):
            counter += 1
            file_path = os.path.join(person_folder, f"image{counter}{file_extension}")
            if counter > 1000:  # Safety limit (allow many images per person)
                logger.error(f"❌ Too many images for {safe_name} (limit: 1000)")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "message": "Too many images for this person. Maximum is 1000 images."}
                )
        
        if counter > 1:
            logger.info(f"📝 Image #{counter} for {person_name}: {os.path.basename(file_path)}")
        else:
            logger.info(f"📝 First image for {person_name}: {os.path.basename(file_path)}")

        # Save the uploaded file
        try:
            with open(file_path, "wb") as f:
                f.write(contents)
            logger.info(f"✅ Saved new person photo: {file_path} (size: {file_size / 1024:.2f}KB)")
        except IOError as e:
            logger.error(f"❌ Failed to save file to {file_path}: {e}")
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": f"Failed to save file: {str(e)}"}
            )

        # Process face and add to both Identity system (pgvector) and FAISS
        try:
            # Ensure model_manager is initialized
            if not model_manager._initialized:
                logger.info("🔄 Model manager not initialized, initializing now...")
                model_manager.initialize()
            
            logger.info(f"🔄 Processing face and adding to database: {person_name}")
            
            # Step 1: Detect face and generate embedding
            image = cv2.imread(file_path)
            if image is None:
                logger.warning(f"⚠️ Could not read image: {file_path}")
                return JSONResponse(
                    status_code=200,
                    content={
                        "success": True,
                        "message": f"Photo saved, but could not read image. Please ensure the image is valid.",
                        "filename": os.path.basename(file_path),
                        "warning": "Image read failed"
                    }
                )
            
            # Handle two modes: face image (skip detection) or full image (detect face)
            bboxes, kpss = None, None
            face_detected = False
            
            if is_face_image:
                # This is already a face image - skip detection, use entire image for embedding
                logger.info(f"🔍 User uploaded a face image - skipping face detection, using entire image for embedding")
                # For face images, we use the entire image as the face region
                # Create dummy keypoints for the center of the image (face alignment will handle it)
                h, w = image.shape[:2]
                # Create keypoints at image center (approximate face center)
                # Format: [[x1, y1], [x2, y2], [x3, y3], [x4, y4], [x5, y5]]
                center_x, center_y = w // 2, h // 2
                # Approximate face keypoints (eyes, nose, mouth corners)
                kpss = np.array([[
                    [center_x - w * 0.15, center_y - h * 0.1],  # Left eye
                    [center_x + w * 0.15, center_y - h * 0.1],  # Right eye
                    [center_x, center_y],                         # Nose
                    [center_x - w * 0.1, center_y + h * 0.15],  # Left mouth corner
                    [center_x + w * 0.1, center_y + h * 0.15]   # Right mouth corner
                ]], dtype=np.float32)
                bboxes = np.array([[0, 0, w, h, 1.0]], dtype=np.float32)  # Full image as bounding box
                face_detected = True
                logger.info(f"✅ Using entire image as face region for embedding generation")
            else:
                # This is a full image - detect face first
                logger.info(f"🔍 User uploaded a full image - detecting face...")
                bboxes, kpss = model_manager.detector.detect(image, max_num=1)
                if kpss is None or len(kpss) == 0:
                    # Delete the saved file since we can't process it
                    try:
                        os.remove(file_path)
                        logger.info(f"🗑️  Deleted file {file_path} (no face detected)")
                    except Exception as e:
                        logger.warning(f"⚠️ Could not delete file {file_path}: {e}")
                    
                    logger.warning(f"⚠️ No face detected in image: {os.path.basename(file_path)}")
                    return JSONResponse(
                        status_code=400,
                        content={
                            "success": False,
                            "error": "no_face",
                            "message": "No face detected in the image. Please upload an image with a clear, visible face, or check 'This is a face image' if you're uploading an already cropped face image.",
                            "details": "The image does not contain a detectable face. Please try a different image with better lighting and a clear view of the person's face, or check 'This is a face image' if you're uploading a pre-cropped face image."
                        }
                    )
                face_detected = True
                logger.info(f"✅ Face detected in full image")
            
            # Generate embedding (always generate if we have keypoints)
            embedding = None
            if face_detected and kpss is not None and len(kpss) > 0:
                try:
                    embedding = model_manager.recognizer.get_embedding(image, kpss[0])
                except Exception as e:
                    logger.error(f"❌ Failed to generate embedding: {e}", exc_info=True)
                    embedding = None
            
            # Validate embedding was generated successfully
            if embedding is None or len(embedding) == 0:
                # Delete the saved file since we can't process it
                try:
                    os.remove(file_path)
                    logger.info(f"🗑️  Deleted file {file_path} (embedding generation failed)")
                except Exception as e:
                    logger.warning(f"⚠️ Could not delete file {file_path}: {e}")
                
                logger.error(f"❌ Failed to generate embedding for {person_name}")
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": "embedding_failed",
                        "message": "Failed to generate face embedding. The image quality may be too low.",
                        "details": "Unable to extract face features from the image. Please try a higher quality image with better lighting and a clearer view of the face."
                    }
                )
            
            # Validate embedding quality (check for zero or invalid values)
            embedding_norm = np.linalg.norm(embedding)
            if embedding_norm == 0 or not np.isfinite(embedding_norm):
                # Delete the saved file since we can't process it
                try:
                    os.remove(file_path)
                    logger.info(f"🗑️  Deleted file {file_path} (invalid embedding)")
                except Exception as e:
                    logger.warning(f"⚠️ Could not delete file {file_path}: {e}")
                
                logger.error(f"❌ Invalid embedding generated for {person_name} (norm: {embedding_norm})")
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": "invalid_embedding",
                        "message": "Invalid face embedding generated. The image quality may be insufficient.",
                        "details": "The face detection succeeded but the embedding is invalid. Please try a higher quality image with better lighting and a clearer, front-facing view of the person."
                    }
                )
            
            logger.info(f"✅ Generated embedding for {person_name} (shape: {embedding.shape}, norm: {embedding_norm:.4f})")
            
            # Step 2: Create or update Identity record in database
            identity = None
            identity_created = False
            
            # Check if identity already exists with this display_name
            result = await db.execute(
                select(Identity).where(
                    Identity.display_name == person_name,
                    Identity.type == IdentityType.KNOWN,
                    Identity.status == IdentityStatus.ACTIVE
                )
            )
            existing_identity = result.scalar_one_or_none()
            
            if existing_identity:
                logger.info(f"🔄 Identity already exists for '{person_name}' (ID: {existing_identity.id})")
                logger.info(f"   📸 This is an additional image for the same person")
                logger.info(f"   ✅ Will add new embedding to existing identity (not create duplicate)")
                identity = existing_identity
                identity.last_seen_at = datetime.utcnow()
                # Update best_snapshot_path if not set
                # Note: We keep the existing best_snapshot_path to preserve the original image
                # If you want to always update to the newest image, uncomment the next line:
                # identity.best_snapshot_path = normalize_storage_path(file_path)
                if not identity.best_snapshot_path:
                    identity.best_snapshot_path = normalize_storage_path(file_path)
                    logger.info(f"   ✅ Updated best_snapshot_path (was None)")
                else:
                    logger.info(f"   📷 Keeping existing best_snapshot_path: {identity.best_snapshot_path}")
                    logger.info(f"   📷 New image saved as: {os.path.basename(file_path)}")
                identity.updated_at = datetime.utcnow()
                logger.info(f"   ✅ Updated last_seen_at and updated_at timestamps")
            else:
                # Create new Identity record
                identity_id = uuid.uuid4()
                now = datetime.utcnow()
                
                # Normalize file path for best_snapshot_path
                normalized_path = normalize_storage_path(file_path)
                
                identity = Identity(
                    id=identity_id,
                    type=IdentityType.KNOWN,
                    status=IdentityStatus.ACTIVE,
                    display_name=person_name,
                    first_seen_at=now,
                    last_seen_at=now,
                    appearances_count=0,
                    best_snapshot_path=normalized_path,
                    created_at=now,
                    updated_at=now
                )
                db.add(identity)
                await db.flush()  # Flush to get the ID
                identity_created = True
                logger.info(f"✅ Created new Identity record for '{person_name}' (ID: {identity.id})")
            
            # Step 3: Save embedding to database (pgvector or FAISS)
            embedding_record = None
            if embedding is not None:
                identity_service = get_identity_service()
                if identity_service:
                    try:
                        logger.info(f"🔄 Saving embedding to database using identity_service (backend: {'pgvector' if identity_service.use_pgvector else 'faiss'})")
                        
                        # Normalize embedding (required for pgvector)
                        embedding_normalized = embedding / np.linalg.norm(embedding) if np.linalg.norm(embedding) > 0 else embedding
                        
                        # Save embedding using identity service (handles pgvector/FAISS automatically)
                        embedding_record = await identity_service.save_embedding(
                            identity=identity,
                            embedding=embedding_normalized,
                            detection_id=None,  # No detection for manually uploaded faces
                            pipeline_id="uploaded",  # Special pipeline ID for uploaded faces
                            quality_score=None,  # Quality not calculated for uploads (will use default threshold check)
                            db=db
                        )
                        
                        if embedding_record:
                            # Verify embedding was actually saved (not NULL)
                            if hasattr(embedding_record, 'embedding') and embedding_record.embedding is None:
                                logger.error(f"❌ Embedding was saved but is NULL! This should not happen.")
                                # Rollback identity creation if it was new
                                if identity_created:
                                    await db.rollback()
                                    try:
                                        os.remove(file_path)
                                        logger.info(f"🗑️  Deleted file {file_path} (embedding save failed)")
                                    except Exception as e:
                                        logger.warning(f"⚠️ Could not delete file {file_path}: {e}")
                                    
                                    return JSONResponse(
                                        status_code=500,
                                        content={
                                            "success": False,
                                            "error": "embedding_save_failed",
                                            "message": "Failed to save embedding to database. Please try again.",
                                            "details": "The embedding was generated but could not be saved to the database. This may be a system error."
                                        }
                                    )
                            logger.info(f"✅ Saved embedding to database (backend: {'pgvector' if identity_service.use_pgvector else 'faiss'}, embedding_id: {embedding_record.id})")
                        else:
                            # Embedding save failed - KEEP the uploaded photo. Deleting
                            # it on transient DB errors silently emptied enrollment
                            # galleries (storage/faces/<name>/), leaving identities with
                            # frozen single-view embeddings that could never be refreshed.
                            logger.error(f"❌ save_embedding returned None - embedding save failed (keeping photo {file_path})")
                            await db.rollback()

                            return JSONResponse(
                                status_code=500,
                                content={
                                    "success": False,
                                    "error": "embedding_save_failed",
                                    "message": "Failed to save embedding to database. The image quality may be below the required threshold.",
                                    "details": "The embedding was generated but could not be saved. This may be due to low image quality or system error. Please try a higher quality image."
                                }
                            )
                    except Exception as e:
                        # KEEP the uploaded photo (see note above) - only the DB write failed
                        logger.error(f"❌ Error saving embedding to Identity system: {e} (keeping photo {file_path})", exc_info=True)
                        await db.rollback()

                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "embedding_save_error",
                                "message": "Error saving embedding to database. Please try again.",
                                "details": f"An error occurred while saving the embedding: {str(e)}"
                            }
                        )
            else:
                    logger.warning("⚠️ Identity service not available, skipping database embedding storage")
                    logger.warning("⚠️ This means embeddings will NOT be saved to PostgreSQL - only FAISS will be updated")
                    # If identity_service is not available, we should still fail if using pgvector
                    # But allow FAISS-only mode to continue
                    if settings.VECTOR_BACKEND == 'pgvector':
                        await db.rollback()
                        try:
                            os.remove(file_path)
                            logger.info(f"🗑️  Deleted file {file_path} (pgvector required but service unavailable)")
                        except Exception as e:
                            logger.warning(f"⚠️ Could not delete file {file_path}: {e}")
                        
                        return JSONResponse(
                            status_code=503,
                            content={
                                "success": False,
                                "error": "service_unavailable",
                                "message": "Identity service is not available. Please try again later.",
                                "details": "The identity management service is not initialized. Please contact the administrator."
                            }
                        )
            
            # Step 4: Also update FAISS for backward compatibility
            if embedding is not None:
                try:
                    model_manager.face_db.add_face(embedding, person_name)
                    model_manager.face_db.save()
                    logger.info(f"✅ Updated FAISS database for backward compatibility")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to update FAISS: {e}")
            
            # Commit database transaction
            await db.commit()
            
            # Get updated counts
            total_faces_faiss = 0
            if model_manager.face_db and model_manager.face_db.index:
                total_faces_faiss = model_manager.face_db.index.ntotal
            
            # Count identities in database
            result = await db.execute(select(Identity).where(Identity.type == IdentityType.KNOWN, Identity.status == IdentityStatus.ACTIVE))
            total_identities = len(result.scalars().all())
            
            logger.info(f"✅ Person '{person_name}' successfully added:")
            logger.info(f"   • Identity ID: {identity.id}")
            logger.info(f"   • Identity created: {identity_created}")
            logger.info(f"   • Embedding saved: {embedding_record is not None}")
            logger.info(f"   • Total identities (DB): {total_identities}")
            logger.info(f"   • Total faces (FAISS): {total_faces_faiss}")
            logger.info(f"   • Added by user: {current_user.username} (ID: {current_user.id})")

            return JSONResponse(
                content={
                    "success": True,
                    "message": f"Successfully added {person_name} to tracking database.",
                    "filename": os.path.basename(file_path),
                    "identity_id": str(identity.id),
                    "identity_created": identity_created,
                    "total_identities": total_identities,
                    "total_faces": total_faces_faiss,
                    "backend": "pgvector" if (identity_service and identity_service.use_pgvector) else "faiss"
                }
            )
            
        except Exception as e:
            await db.rollback()
            logger.error(f"❌ Error adding face to database: {e}", exc_info=True)
            # File was saved successfully, but database update failed
            return JSONResponse(
                status_code=200,  # Still return 200 since file was saved
                content={
                    "success": True,
                    "message": f"Photo saved successfully, but face database update failed: {str(e)}. The person will be available after service restart or manual database rebuild.",
                    "filename": os.path.basename(file_path),
                    "warning": f"Database update failed: {str(e)}"
                }
            )

    except HTTPException as e:
        logger.warning(f"❌ HTTPException during upload: {e.detail} (user: {current_user.username})")
        raise
    except Exception as e:
        logger.error(f"❌ Unexpected error uploading person '{person_name}' by user '{current_user.username}': {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"Upload failed: {str(e)}"}
        )

