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
    """Prometheus metrics endpoint.

    Access is restricted at the reverse proxy: this exposes request volumes,
    authentication failure counts and internal timings.
    """
    try:
        # Refresh operational gauges (CUDA availability, model load state, disk,
        # backup age, certificate expiry) on scrape rather than from a separate
        # timer, so their freshness always matches the scrape interval. Each
        # probe is bounded and swallows its own failures.
        try:
            from backend.core.operational_metrics import refresh_all
            from fastapi.concurrency import run_in_threadpool

            # nvidia-smi and disk stats are blocking; keep them off the loop.
            await run_in_threadpool(refresh_all)
        except Exception as e:
            logger.debug(f"Operational metrics refresh skipped: {e}")

        return Response(
            content=generate_latest().decode('utf-8'),
            media_type=CONTENT_TYPE_LATEST
        )
    except Exception as e:
        logger.error(f"Metrics generation error: {e}")
        return Response(content="", media_type=CONTENT_TYPE_LATEST)

