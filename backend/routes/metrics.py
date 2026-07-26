"""
Metrics Routes
==============
Prometheus metrics endpoint.
"""

import os
import sys
import logging
from fastapi import APIRouter, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint"""
    try:
        return Response(
            content=generate_latest().decode('utf-8'),
            media_type=CONTENT_TYPE_LATEST
        )
    except Exception as e:
        logger.error(f"Metrics generation error: {e}")
        return Response(content="", media_type=CONTENT_TYPE_LATEST)

