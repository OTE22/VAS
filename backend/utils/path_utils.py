"""
Path Utilities
==============
Centralized path normalization and URL generation for file serving.
This ensures consistent path handling across all endpoints.
"""

import os
import logging
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)


def normalize_storage_path(file_path: Optional[str], storage_dir: Optional[str] = None, check_exists: bool = False) -> Optional[str]:
    """
    Normalize a file path to a relative storage path.
    
    Converts various path formats to a consistent relative path:
    - /app/storage/pipeline/name/file.jpg -> storage/pipeline/name/file.jpg
    - /app/storage/faces/person.jpg -> storage/faces/person.jpg
    - storage/pipeline/name/file.jpg -> storage/pipeline/name/file.jpg
    - storage/faces/person.jpg -> storage/faces/person.jpg
    - pipeline/name/file.jpg -> storage/pipeline/name/file.jpg
    
    Storage structure:
    - storage/faces/ - Known faces (uploaded persons)
    - storage/{pipeline_id}/ - Detected faces from pipelines
    
    Args:
        file_path: The file path to normalize (can be absolute or relative)
        storage_dir: Storage directory path (defaults to settings.STORAGE_DIR)
        check_exists: If True, verify file exists (default: False - let static server handle 404s)
    
    Returns:
        Normalized relative path starting with 'storage/' or None if invalid
    """
    if not file_path:
        return None
    
    # Get storage directory
    if storage_dir is None:
        from config import settings
        storage_dir = settings.STORAGE_DIR
    
    storage_dir_abs = os.path.abspath(storage_dir)
    
    try:
        # Handle absolute paths
        if os.path.isabs(file_path):
            file_path_abs = os.path.abspath(file_path)
            
            # Check if path is within storage directory
            if file_path_abs.startswith(storage_dir_abs):
                # Convert to relative: /app/storage/pipeline/name/file.jpg -> storage/pipeline/name/file.jpg
                relative_path = os.path.relpath(file_path_abs, storage_dir_abs)
                normalized = 'storage/' + relative_path.replace('\\', '/')
                
                # Optionally verify file exists (but don't require it - let static server handle 404s)
                if check_exists and not os.path.exists(file_path_abs):
                    logger.debug(f"[PATH_UTILS] File does not exist: {file_path_abs}")
                    return None
                
                return normalized
            else:
                # Path is absolute but outside storage directory - try to extract relative part
                # Handle Docker paths like /app/storage/... or any path containing /storage/
                if '/storage/' in file_path:
                    # Extract everything after /storage/
                    parts = file_path.split('/storage/')
                    if len(parts) > 1:
                        normalized = 'storage/' + parts[-1].replace('\\', '/')
                        logger.debug(f"[PATH_UTILS] Extracted path from Docker format: {file_path} -> {normalized}")
                        return normalized
                
                # Try to find storage in the path in other ways
                # Handle /app/storage/... format
                if file_path.startswith('/app/'):
                    # Remove /app/ prefix and see if it's a storage path
                    without_app = file_path[5:]  # Remove '/app/'
                    if without_app.startswith('storage/'):
                        normalized = without_app.replace('\\', '/')
                        logger.debug(f"[PATH_UTILS] Extracted path from /app/ format: {file_path} -> {normalized}")
                        return normalized
                
                logger.debug(f"[PATH_UTILS] Path outside storage directory, attempting extraction: {file_path}")
                # Last resort: if path contains 'storage', try to use it
                if 'storage' in file_path.lower():
                    # Find storage in path and use everything after it
                    storage_idx = file_path.lower().find('storage')
                    if storage_idx >= 0:
                        normalized = file_path[storage_idx:].replace('\\', '/')
                        if not normalized.startswith('storage/'):
                            normalized = 'storage/' + normalized.split('/', 1)[-1] if '/' in normalized else normalized
                        logger.debug(f"[PATH_UTILS] Extracted path as fallback: {file_path} -> {normalized}")
                        return normalized
                
                logger.warning(f"[PATH_UTILS] Could not normalize path: {file_path}")
                return None
        
        # Handle relative paths
        else:
            # Remove leading slashes
            file_path = file_path.lstrip('/')
            
            # Handle known_faces special case
            if file_path.startswith('known_faces/'):
                return 'storage/' + file_path
            
            # Add storage/ prefix if not present
            if not file_path.startswith('storage/'):
                normalized = 'storage/' + file_path
            else:
                normalized = file_path
            
            # Optionally verify file exists (but don't require it)
            if check_exists:
                full_path = os.path.join(storage_dir_abs, normalized.replace('storage/', ''))
                if not os.path.exists(full_path):
                    logger.debug(f"[PATH_UTILS] File does not exist: {full_path}")
                    return None
            
            return normalized
                
    except Exception as e:
        logger.error(f"[PATH_UTILS] Error normalizing path {file_path}: {e}")
        return None


def path_to_url(file_path: Optional[str], storage_dir: Optional[str] = None, check_exists: bool = False) -> Optional[str]:
    """
    Convert a file path to a URL for serving.
    
    This is the main function to use in API responses.
    It normalizes the path and returns a ready-to-use URL.
    
    Args:
        file_path: The file path to convert
        storage_dir: Storage directory path (optional)
        check_exists: If True, verify file exists (default: False - let static server handle 404s)
    
    Returns:
        URL path like '/storage/pipeline/name/file.jpg' or None if invalid
    """
    normalized = normalize_storage_path(file_path, storage_dir, check_exists=check_exists)
    
    if normalized:
        # Ensure leading slash for URL
        return '/' + normalized if not normalized.startswith('/') else normalized
    else:
        return None

