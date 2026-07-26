"""
Relationship Calculation Background Task
========================================
Background task to calculate and cache identity relationships for all identities.
"""

import os
import sys
import logging
import time
from datetime import datetime
from typing import Dict, Any

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_connection import db_manager
from db_models import Identity, IdentityType, IdentityStatus
from backend.core.intelligence_service import intelligence_service
from backend.core.background_task_notifier import background_task_notifier, TaskType
from backend.core.task_history import task_history_manager, TaskStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def calculate_all_relationships(job_id: str = None):
    """
    Background task to calculate and cache relationships for all identities.
    This populates the identity_relationships cache table.

    When job_id is provided (scheduled via the API), progress is written to
    the Background Tasks job row so the monitor page can show it live.
    """
    task_name = "Calculate Identity Relationships"
    task_type = TaskType.RELATIONSHIP_CALCULATION
    start_time = time.time()

    if job_id:
        await task_history_manager.mark_running(job_id)

    try:
        # Notify that task is starting
        await background_task_notifier.notify_task_starting(
            task_type=task_type,
            task_name=task_name,
            description="Calculating co-appearance relationships for all identities and populating cache",
            estimated_duration="5-30 minutes (depends on number of identities)",
            notify_all_users=False
        )
        
        # Record task started (only for legacy direct invocations — API-scheduled
        # runs already have a job row created by the endpoint)
        if not job_id:
            await task_history_manager.record_task_started(
                task_type=task_type.value,
                task_name=task_name,
                description="Calculating relationships for all identities",
                notify_all_users=False
            )
        
        logger.info(f"[RELATIONSHIP_TASK] Starting relationship calculation for all identities")
        
        async with db_manager.get_session() as db:
            # Get all active identities
            result = await db.execute(
                select(Identity).where(
                    Identity.status == IdentityStatus.ACTIVE
                )
            )
            identities = result.scalars().all()
            
            total_identities = len(identities)
            logger.info(f"[RELATIONSHIP_TASK] Found {total_identities} active identities to process")
            
            if total_identities == 0:
                logger.info(f"[RELATIONSHIP_TASK] No identities found, skipping calculation")
                duration = time.time() - start_time
                await background_task_notifier.notify_task_completed(
                    task_type=task_type,
                    task_name=task_name,
                    success=True,
                    duration_seconds=duration,
                    details={"identities_processed": 0, "relationships_cached": 0},
                    notify_all_users=False
                )
                if job_id:
                    await task_history_manager.finish_job(
                        job_id, success=True,
                        result={"identities_processed": 0, "relationships_cached": 0}
                    )
                return
            
            # Process each identity with progress tracking and timeout protection
            processed = 0
            failed = 0
            total_relationships = 0
            max_duration_seconds = 3600 * 2  # 2 hours max
            
            for idx, identity in enumerate(identities, 1):
                # Check if we've exceeded max duration
                elapsed = time.time() - start_time
                if elapsed > max_duration_seconds:
                    logger.warning(f"[RELATIONSHIP_TASK] ⚠️ Maximum duration ({max_duration_seconds/60:.1f} minutes) exceeded. Stopping task.")
                    break
                
                try:
                    # Log less frequently for large batches
                    if idx % 50 == 0 or idx <= 10:
                        logger.info(f"[RELATIONSHIP_TASK] Processing identity {idx}/{total_identities}: {identity.id} ({identity.display_name or 'Unknown'})")
                    
                    # Refresh relationships for this identity
                    await intelligence_service.refresh_relationships(
                        db=db,
                        identity_id=str(identity.id)
                    )
                    
                    # Count relationships for this identity
                    from db_models import IdentityRelationship
                    from sqlalchemy import func, or_
                    
                    count_result = await db.execute(
                        select(func.count(IdentityRelationship.id)).where(
                            or_(
                                IdentityRelationship.identity_id_1 == identity.id,
                                IdentityRelationship.identity_id_2 == identity.id
                            )
                        )
                    )
                    rel_count = count_result.scalar() or 0
                    total_relationships += rel_count
                    
                    processed += 1
                    
                    # Log progress every 50 identities (less frequent for large batches)
                    if idx % 50 == 0:
                        elapsed_minutes = elapsed / 60
                        logger.info(
                            f"[RELATIONSHIP_TASK] Progress: {idx}/{total_identities} identities processed "
                            f"({processed} successful, {failed} failed), "
                            f"{total_relationships} relationships cached, "
                            f"{elapsed_minutes:.1f} minutes elapsed"
                        )
                        if job_id:
                            await task_history_manager.update_progress(
                                job_id,
                                percent=int(idx / total_identities * 100),
                                details={
                                    "identities_processed": processed,
                                    "identities_failed": failed,
                                    "total_identities": total_identities,
                                    "relationships_cached": total_relationships,
                                }
                            )
                
                except Exception as e:
                    failed += 1
                    logger.error(f"[RELATIONSHIP_TASK] Error processing identity {identity.id}: {e}", exc_info=True)
                    continue
            
            duration = time.time() - start_time
            
            # Calculate success rate
            success_rate = (processed / total_identities * 100) if total_identities > 0 else 0
            
            details = {
                "identities_processed": processed,
                "identities_failed": failed,
                "total_identities": total_identities,
                "relationships_cached": total_relationships,
                "success_rate": f"{success_rate:.1f}%",
                "duration_minutes": f"{duration / 60:.1f}"
            }
            
            logger.info(f"[RELATIONSHIP_TASK] ✅ Completed: {processed}/{total_identities} identities processed, {total_relationships} relationships cached in {duration/60:.1f} minutes")
            
            # Notify completion
            await background_task_notifier.notify_task_completed(
                task_type=task_type,
                task_name=task_name,
                success=True,
                duration_seconds=duration,
                details=details,
                notify_all_users=False
            )
            
            # Update task history
            if job_id:
                await task_history_manager.finish_job(job_id, success=True, result=details)
            else:
                await task_history_manager.record_task_completed(
                    task_type=task_type.value,
                    task_name=task_name,
                    success=True,
                    duration_seconds=duration,
                    details=details,
                    notify_all_users=False
                )

    except Exception as e:
        duration = time.time() - start_time
        logger.error(f"[RELATIONSHIP_TASK] ❌ Task failed: {e}", exc_info=True)
        
        # Notify failure
        await background_task_notifier.notify_task_completed(
            task_type=task_type,
            task_name=task_name,
            success=False,
            duration_seconds=duration,
            details={"error": str(e)},
            notify_all_users=False
        )
        
        # Update task history
        if job_id:
            await task_history_manager.finish_job(
                job_id, success=False,
                error_code="RELATIONSHIP_TASK_FAILED",
                error_message=str(e)[:500]
            )
        else:
            await task_history_manager.record_task_completed(
                task_type=task_type.value,
                task_name=task_name,
                success=False,
                duration_seconds=duration,
                details={"error": str(e)},
                notify_all_users=False
            )

        raise

