"""
Helper Functions
================
Validation and utility functions.
"""

import re
import cv2
import numpy as np
from fastapi import HTTPException


def validate_pipeline_id(pipeline_id: str) -> str:
    """Validate pipeline ID format"""
    if not re.match(r'^[a-zA-Z0-9_-]{3,100}$', pipeline_id):
        raise HTTPException(status_code=400, detail="Invalid pipeline ID format. Must be 3-100 characters, alphanumeric, dashes, or underscores.")
    return pipeline_id


def validate_image_content(image_bytes: bytes) -> bool:
    """Validate that bytes contain a valid image"""
    try:
        img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        return img is not None
    except:
        return False

