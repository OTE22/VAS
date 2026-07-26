#!/usr/bin/env python3
"""
Check Dashboard Data Endpoints
==============================
Verify that endpoints are returning data correctly for the dashboard.
"""

import os
import sys
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timedelta

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_

from db_connection import db_manager
from db_models import Detection, Face, Identity, IdentityType, LabelState
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


async def check_dashboard_data():
    """Check if there's data available for the dashboard."""
    logger.info("=" * 80)
    logger.info("🔍 Checking Dashboard Data Availability")
    logger.info("=" * 80)
    
    # Initialize database
    logger.info("🔄 Initializing database...")
    try:
        await db_manager.init_db()
        logger.info("✅ Database initialized")
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        return
    
    async with db_manager.get_session() as db:
        # Get display hours from config
        display_hours = getattr(settings, 'DASHBOARD_FACE_DISPLAY_HOURS', 3)
        cutoff_time = datetime.utcnow() - timedelta(hours=display_hours)
        
        logger.info("")
        logger.info("📊 Detection Statistics:")
        logger.info("=" * 80)
        
        # Total detections
        total_detections_query = select(func.count(Detection.id))
        total_detections = (await db.execute(total_detections_query)).scalar_one()
        logger.info(f"   Total Detections (all time):     {total_detections}")
        
        # Recent detections (within display window)
        recent_detections_query = select(func.count(Detection.id)).where(
            Detection.timestamp >= cutoff_time
        )
        recent_detections = (await db.execute(recent_detections_query)).scalar_one()
        logger.info(f"   Recent Detections (last {display_hours}h): {recent_detections}")
        
        # Total faces
        total_faces_query = select(func.count(Face.id))
        total_faces = (await db.execute(total_faces_query)).scalar_one()
        logger.info(f"   Total Faces (all time):         {total_faces}")
        
        # Recent faces
        recent_faces_query = select(func.count(Face.id)).join(
            Detection, Face.detection_id == Detection.id
        ).where(Detection.timestamp >= cutoff_time)
        recent_faces = (await db.execute(recent_faces_query)).scalar_one()
        logger.info(f"   Recent Faces (last {display_hours}h):   {recent_faces}")
        
        logger.info("=" * 80)
        
        # Check face types
        logger.info("")
        logger.info("👤 Face Type Breakdown (Recent):")
        logger.info("=" * 80)
        
        # Known faces (by identity type)
        known_faces_query = select(func.count(Face.id)).join(
            Detection, Face.detection_id == Detection.id
        ).outerjoin(
            Identity, Face.identity_id == Identity.id
        ).where(
            Detection.timestamp >= cutoff_time,
            Identity.type == IdentityType.KNOWN
        )
        known_faces = (await db.execute(known_faces_query)).scalar_one()
        logger.info(f"   KNOWN faces (by identity.type):  {known_faces}")
        
        # Unknown faces (by identity type)
        unknown_faces_query = select(func.count(Face.id)).join(
            Detection, Face.detection_id == Detection.id
        ).outerjoin(
            Identity, Face.identity_id == Identity.id
        ).where(
            Detection.timestamp >= cutoff_time,
            Identity.type == IdentityType.UNKNOWN
        )
        unknown_faces = (await db.execute(unknown_faces_query)).scalar_one()
        logger.info(f"   UNKNOWN faces (by identity.type): {unknown_faces}")
        
        # Faces with label_state = AUTO_UNKNOWN
        auto_unknown_query = select(func.count(Face.id)).join(
            Detection, Face.detection_id == Detection.id
        ).where(
            Detection.timestamp >= cutoff_time,
            Face.label_state == LabelState.AUTO_UNKNOWN
        )
        auto_unknown_faces = (await db.execute(auto_unknown_query)).scalar_one()
        logger.info(f"   AUTO_UNKNOWN (by label_state):   {auto_unknown_faces}")
        
        # Faces with name = "Unknown"
        name_unknown_query = select(func.count(Face.id)).join(
            Detection, Face.detection_id == Detection.id
        ).where(
            Detection.timestamp >= cutoff_time,
            Face.name.ilike("unknown")
        )
        name_unknown_faces = (await db.execute(name_unknown_query)).scalar_one()
        logger.info(f"   Name='Unknown' (legacy):         {name_unknown_faces}")
        
        logger.info("=" * 80)
        
        # Check SHOW_UNKNOWN_FACES_ON_DASHBOARD setting
        show_unknown = getattr(settings, 'SHOW_UNKNOWN_FACES_ON_DASHBOARD', False)
        logger.info("")
        logger.info("⚙️  Dashboard Configuration:")
        logger.info("=" * 80)
        logger.info(f"   SHOW_UNKNOWN_FACES_ON_DASHBOARD: {show_unknown}")
        logger.info(f"   DASHBOARD_FACE_DISPLAY_HOURS:     {display_hours}")
        logger.info("=" * 80)
        
        # Calculate faces that will be shown on dashboard
        if show_unknown:
            dashboard_faces = recent_faces
            logger.info("")
            logger.info("✅ Dashboard will show ALL faces (known + unknown)")
        else:
            dashboard_faces = known_faces
            logger.info("")
            logger.info("✅ Dashboard will show ONLY KNOWN faces")
            logger.info(f"   (Filtering out {unknown_faces + auto_unknown_faces} unknown faces)")
        
        logger.info(f"   Expected faces on dashboard: {dashboard_faces}")
        
        # Sample recent detections
        if recent_detections > 0:
            logger.info("")
            logger.info("📋 Sample Recent Detections:")
            logger.info("=" * 80)
            sample_query = select(Detection).where(
                Detection.timestamp >= cutoff_time
            ).order_by(Detection.timestamp.desc()).limit(5)
            sample_detections = (await db.execute(sample_query)).scalars().all()
            
            for det in sample_detections:
                # Count faces for this detection
                faces_query = select(func.count(Face.id)).where(Face.detection_id == det.id)
                face_count = (await db.execute(faces_query)).scalar_one()
                
                logger.info(f"   - Pipeline: {det.pipeline_id}, Time: {det.timestamp}, Faces: {face_count}")
        
        logger.info("=" * 80)
        
        # Summary
        logger.info("")
        logger.info("📊 Summary:")
        logger.info("=" * 80)
        if recent_detections == 0:
            logger.warning("   ⚠️  NO RECENT DETECTIONS found!")
            logger.warning("   This means:")
            logger.warning("     1. No video streams are active, OR")
            logger.warning("     2. No faces are being detected, OR")
            logger.warning("     3. Detections are older than the display window")
        elif dashboard_faces == 0:
            logger.warning("   ⚠️  NO FACES will be shown on dashboard!")
            logger.warning("   This means:")
            logger.warning("     1. All recent faces are UNKNOWN (and filtering is enabled), OR")
            logger.warning("     2. Faces are not linked to KNOWN identities")
        else:
            logger.info(f"   ✅ Dashboard should show {dashboard_faces} faces from {recent_detections} detections")
        
        logger.info("=" * 80)
    
    await db_manager.close_db()
    
    logger.info("")
    logger.info("=" * 80)
    logger.info("✅ Dashboard Data Check Complete")
    logger.info("=" * 80)


if __name__ == "__main__":
    asyncio.run(check_dashboard_data())

