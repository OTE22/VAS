"""
Metrics Routes
==============
Prometheus metrics endpoint.
"""

import os
import sys
import time
import logging
from fastapi import APIRouter, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Metrics"])

# The auxiliary refreshes below are NOT free: refresh_all stats the backup
# directory and parses the TLS cert, and under VECTOR_BACKEND=pgvector
# publish_metrics runs two COUNT(*) joins over identity_embeddings×identities,
# each borrowing a pool connection. At a 15s scrape interval that work
# competed with recognition four times a minute. The gauges it feeds change on
# the order of minutes, so a short TTL keeps them honest while capping the
# cost regardless of how aggressively Prometheus scrapes.
_REFRESH_TTL_SECONDS = 30.0
_last_refresh = 0.0


@router.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint.

    Access is restricted at the reverse proxy: this exposes request volumes,
    authentication failure counts and internal timings.
    """
    global _last_refresh
    try:
        if time.monotonic() - _last_refresh >= _REFRESH_TTL_SECONDS:
            _last_refresh = time.monotonic()

            # Refresh operational gauges (CUDA availability, model load state,
            # disk, backup age, certificate expiry) on scrape rather than from
            # a separate timer, so their freshness tracks scraping. Each probe
            # is bounded and swallows its own failures.
            try:
                from backend.core.operational_metrics import refresh_all
                from fastapi.concurrency import run_in_threadpool

                # nvidia-smi and disk stats are blocking; keep them off the loop.
                await run_in_threadpool(refresh_all)
            except Exception as e:
                logger.debug(f"Operational metrics refresh skipped: {e}")

            # Vector index gauges, for the same reason and one more: under
            # VECTOR_BACKEND=pgvector no index loop runs at all, so without
            # this they would hold whatever startup published and never move —
            # a size gauge frozen at boot is indistinguishable from a stalled
            # index.
            try:
                from backend.core.vector_index.access import get_vector_index_manager
                manager = get_vector_index_manager()
                if manager is not None:
                    await manager.publish_metrics()
            except Exception as e:
                logger.debug(f"Vector index metrics refresh skipped: {e}")

        return Response(
            content=generate_latest().decode('utf-8'),
            media_type=CONTENT_TYPE_LATEST
        )
    except Exception as e:
        # A failed exposition must LOOK failed. This used to answer 200 with
        # an empty body, so Prometheus kept `up == 1` while storing nothing —
        # a broken exporter was indistinguishable from a healthy quiet system
        # and every alert stayed silent.
        logger.error(f"Metrics generation error: {e}")
        return Response(content="metrics generation failed\n",
                        status_code=500, media_type="text/plain")
