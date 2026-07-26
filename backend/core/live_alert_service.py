"""
Live Alert Service
===================
Manages live search alerts - get notified when a searched face appears again.
"""

import logging
import uuid
from datetime import datetime, time as dt_time
from typing import List, Dict, Optional, Any
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func, update
from sqlalchemy.orm import selectinload

from db_models import (
    LiveSearchAlert, LiveAlertTrigger, LiveAlertStatus, LiveAlertExpirationType,
    Identity
)
from config import settings

logger = logging.getLogger(__name__)


@dataclass
class AlertTriggerInfo:
    """Information about a triggered alert"""
    alert_id: str
    alert_name: str
    identity_id: str
    identity_name: Optional[str]
    similarity: float
    pipeline_id: str
    triggered_at: datetime
    should_notify_dashboard: bool
    should_notify_email: bool
    should_notify_sms: bool
    should_notify_webhook: bool
    email_recipients: List[str]
    sms_recipients: List[str]
    webhook_url: Optional[str]
    sound_alert: bool
    trigger_id: Optional[str] = None  # id of the created LiveAlertTrigger row


class LiveAlertService:
    """
    Service for managing live search alerts.
    
    Live alerts notify users when a specific identity is detected again.
    This is useful for:
    - Tracking suspects/persons of interest
    - VIP arrival notifications
    - Security monitoring
    """
    
    async def create_alert(
        self,
        db: AsyncSession,
        name: str,
        identity_id: str,
        created_by: int,
        min_similarity: float = 0.75,
        pipeline_ids: List[str] = None,
        time_window_enabled: bool = False,
        time_window_start: str = None,
        time_window_end: str = None,
        active_days: List[int] = None,
        cooldown_minutes: int = None,
        notify_dashboard: bool = True,
        notify_email: bool = False,
        notify_sms: bool = False,
        notify_webhook: bool = False,
        email_recipients: List[str] = None,
        sms_recipients: List[str] = None,
        webhook_url: str = None,
        sound_alert: bool = True,
        auto_capture_snapshot: bool = True,
        auto_record_clip: bool = False,
        clip_duration_seconds: int = 60,
        expiration_type: str = "never",
        expiration_date: datetime = None,
        expiration_detections: int = None
    ) -> LiveSearchAlert:
        """Create a new live search alert."""
        try:
            # Check user's alert limit
            user_alert_count = await self._get_user_alert_count(db, created_by)
            max_alerts = settings.LIVE_ALERT_MAX_PER_USER
            if user_alert_count >= max_alerts:
                raise ValueError(f"Maximum alerts per user ({max_alerts}) reached")
            
            # Check identity's alert limit (max alerts per identity)
            max_per_identity = min(settings.LIVE_ALERT_MAX_PER_IDENTITY, 10)  # Enforce max of 10
            identity_alert_count = await self._get_identity_alert_count(db, identity_id, created_by)
            if identity_alert_count >= max_per_identity:
                raise ValueError(f"Maximum alerts per identity ({max_per_identity}) reached. This identity already has {identity_alert_count} active alert(s).")
            
            if cooldown_minutes is None:
                cooldown_minutes = settings.LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES
            
            alert = LiveSearchAlert(
                name=name,
                identity_id=uuid.UUID(identity_id),
                created_by=created_by,
                min_similarity=min_similarity,
                pipeline_ids=pipeline_ids,
                time_window_enabled=time_window_enabled,
                time_window_start=time_window_start,
                time_window_end=time_window_end,
                active_days=active_days,
                cooldown_minutes=cooldown_minutes,
                notify_dashboard=notify_dashboard,
                notify_email=notify_email,
                notify_sms=notify_sms,
                notify_webhook=notify_webhook,
                email_recipients=email_recipients,
                sms_recipients=sms_recipients,
                webhook_url=webhook_url,
                sound_alert=sound_alert,
                auto_capture_snapshot=auto_capture_snapshot,
                auto_record_clip=auto_record_clip,
                clip_duration_seconds=clip_duration_seconds,
                expiration_type=LiveAlertExpirationType(expiration_type),
                expiration_date=expiration_date,
                expiration_detections=expiration_detections,
                status=LiveAlertStatus.ACTIVE
            )
            
            db.add(alert)
            await db.commit()
            await db.refresh(alert)
            
            logger.info(f"[LIVE_ALERT] Created alert '{name}' for identity {identity_id}")
            return alert
            
        except Exception as e:
            logger.error(f"[LIVE_ALERT] Error creating alert: {e}")
            await db.rollback()
            raise
    
    async def get_alert(
        self,
        db: AsyncSession,
        alert_id: str
    ) -> Optional[LiveSearchAlert]:
        """Get a live alert by ID."""
        query = select(LiveSearchAlert).options(
            selectinload(LiveSearchAlert.identity)
        ).where(
            LiveSearchAlert.id == uuid.UUID(alert_id)
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()
    
    async def get_default_alert_settings(
        self,
        db: AsyncSession,
        identity_id: str,
        created_by: int
    ) -> Dict[str, Any]:
        """
        Get default settings for creating a live alert for an identity.
        Backend logic: generates default name, checks limits, returns defaults.
        """
        try:
            # Get identity information
            identity_query = select(Identity).where(Identity.id == uuid.UUID(identity_id))
            identity_result = await db.execute(identity_query)
            identity = identity_result.scalar_one_or_none()
            
            if not identity:
                raise ValueError(f"Identity {identity_id} not found")
            
            # Get user's current alert count
            user_alert_count = await self._get_user_alert_count(db, created_by)
            max_alerts = settings.LIVE_ALERT_MAX_PER_USER
            can_create = user_alert_count < max_alerts
            
            # Generate default alert name (backend logic)
            # Identity model only has display_name field, not person_code
            identity_name = identity.display_name if identity.display_name else "Unknown Person"
            current_date = datetime.now().strftime("%Y-%m-%d")
            default_name = f"Track {identity_name} - {current_date}"
            
            # Check if identity already has active alerts
            existing_alerts_query = select(func.count(LiveSearchAlert.id)).where(
                and_(
                    LiveSearchAlert.identity_id == uuid.UUID(identity_id),
                    LiveSearchAlert.created_by == created_by,
                    LiveSearchAlert.status == LiveAlertStatus.ACTIVE
                )
            )
            existing_result = await db.execute(existing_alerts_query)
            existing_count = existing_result.scalar_one() or 0
            
            warnings = []
            if existing_count > 0:
                warnings.append(f"ℹ️ This identity already has {existing_count} active alert(s). You can create multiple alerts with different settings (e.g., different time windows, similarity thresholds, or notification channels).")
            if not can_create:
                warnings.append(f"⚠️ Maximum alerts per user ({max_alerts}) reached")
            
            return {
                "identity_id": identity_id,
                "identity_name": identity_name,
                "identity_type": identity.type.value if identity.type else "unknown",
                "default_name": default_name,
                "default_min_similarity": 0.75,
                "default_notify_dashboard": True,
                "default_sound_alert": True,
                "default_auto_capture": True,
                "default_cooldown_minutes": settings.LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES,
                "can_create": can_create,
                "user_alert_count": user_alert_count,
                "max_alerts": max_alerts,
                "existing_alerts_count": existing_count,
                "warnings": warnings
            }
            
        except Exception as e:
            logger.error(f"[LIVE_ALERT] Error getting default settings: {e}")
            raise
    
    async def get_default_alert_settings(
        self,
        db: AsyncSession,
        identity_id: str,
        created_by: int
    ) -> Dict[str, Any]:
        """
        Get default settings for creating a live alert for an identity.
        Backend logic: generates default name, checks limits, returns defaults.
        """
        try:
            # Get identity information
            identity_query = select(Identity).where(Identity.id == uuid.UUID(identity_id))
            identity_result = await db.execute(identity_query)
            identity = identity_result.scalar_one_or_none()
            
            if not identity:
                raise ValueError(f"Identity {identity_id} not found")
            
            # Get user's current alert count
            user_alert_count = await self._get_user_alert_count(db, created_by)
            max_alerts = settings.LIVE_ALERT_MAX_PER_USER
            can_create = user_alert_count < max_alerts
            
            # Generate default alert name (backend logic)
            # Identity model only has display_name field, not person_code
            identity_name = identity.display_name if identity.display_name else "Unknown Person"
            current_date = datetime.now().strftime("%Y-%m-%d")
            default_name = f"Track {identity_name} - {current_date}"
            
            # Check if identity already has active alerts
            existing_count = await self._get_identity_alert_count(db, identity_id, created_by)
            max_per_identity = min(settings.LIVE_ALERT_MAX_PER_IDENTITY, 10)  # Enforce max of 10
            can_create_for_identity = existing_count < max_per_identity
            
            warnings = []
            if existing_count > 0:
                remaining = max_per_identity - existing_count
                warnings.append(f"ℹ️ This identity already has {existing_count} active alert(s). You can create up to {max_per_identity} alerts per identity ({remaining} remaining). Multiple alerts allow different settings (e.g., different time windows, similarity thresholds, or notification channels).")
            if not can_create_for_identity:
                warnings.append(f"⚠️ Maximum alerts per identity ({max_per_identity}) reached for this identity")
            if not can_create:
                warnings.append(f"⚠️ Maximum alerts per user ({max_alerts}) reached")
            
            return {
                "identity_id": identity_id,
                "identity_name": identity_name,
                "identity_type": identity.type.value if identity.type else "unknown",
                "default_name": default_name,
                "default_min_similarity": 0.75,
                "default_notify_dashboard": True,
                "default_sound_alert": True,
                "default_auto_capture": True,
                "default_cooldown_minutes": settings.LIVE_ALERT_DEFAULT_COOLDOWN_MINUTES,
                "can_create": can_create and can_create_for_identity,
                "user_alert_count": user_alert_count,
                "max_alerts": max_alerts,
                "existing_alerts_count": existing_count,
                "max_alerts_per_identity": max_per_identity,
                "warnings": warnings
            }
            
        except Exception as e:
            logger.error(f"[LIVE_ALERT] Error getting default settings: {e}")
            raise
    
    async def get_user_alerts(
        self,
        db: AsyncSession,
        user_id: int,
        include_inactive: bool = False,
        user_pipelines: Optional[List[str]] = None
    ) -> List[LiveSearchAlert]:
        """
        Get all alerts for a user.
        If user_pipelines is provided (for regular users), filter alerts to only include
        identities from those pipelines. Admins (user_pipelines=None) see all their alerts.
        """
        from db_models import IdentityAppearance, IdentityEmbedding, Face, Detection
        from sqlalchemy import exists
        
        query = select(LiveSearchAlert).options(
            selectinload(LiveSearchAlert.identity)
        ).where(
            LiveSearchAlert.created_by == user_id
        )
        
        # Filter by user's accessible pipelines if provided (regular users)
        if user_pipelines is not None:
            # Subquery to check if identity has any appearances/embeddings/faces in user's pipelines
            # We need to use proper subqueries with joins, not exists() with join()
            from sqlalchemy.orm import aliased
            
            # Check IdentityAppearance
            appearance_exists = exists().where(
                and_(
                    IdentityAppearance.identity_id == LiveSearchAlert.identity_id,
                    IdentityAppearance.pipeline_id.in_(user_pipelines)
                )
            )
            
            # Check IdentityEmbedding
            embedding_exists = exists().where(
                and_(
                    IdentityEmbedding.identity_id == LiveSearchAlert.identity_id,
                    IdentityEmbedding.pipeline_id.in_(user_pipelines)
                )
            )
            
            # Check Face->Detection (use table objects for join)
            face_detection_subquery = (
                select(1)
                .select_from(
                    Face.__table__.join(
                        Detection.__table__,
                        Face.__table__.c.detection_id == Detection.__table__.c.id
                    )
                )
                .where(
                    and_(
                        Face.__table__.c.identity_id == LiveSearchAlert.identity_id,
                        Detection.__table__.c.pipeline_id.in_(user_pipelines)
                    )
                )
            )
            face_detection_exists = exists(face_detection_subquery)
            
            # Combine all three conditions with OR
            identity_has_access = appearance_exists | embedding_exists | face_detection_exists
            
            query = query.where(identity_has_access)
        
        if not include_inactive:
            query = query.where(
                LiveSearchAlert.status.in_([
                    LiveAlertStatus.ACTIVE,
                    LiveAlertStatus.PAUSED
                ])
            )
        
        query = query.order_by(LiveSearchAlert.created_at.desc())
        
        result = await db.execute(query)
        return result.scalars().all()
    
    async def get_active_alerts_for_identity(
        self,
        db: AsyncSession,
        identity_id: str
    ) -> List[LiveSearchAlert]:
        """Get all active alerts for a specific identity."""
        query = select(LiveSearchAlert).where(
            and_(
                LiveSearchAlert.identity_id == uuid.UUID(identity_id),
                LiveSearchAlert.status == LiveAlertStatus.ACTIVE
            )
        )
        
        result = await db.execute(query)
        return result.scalars().all()
    
    async def update_alert(
        self,
        db: AsyncSession,
        alert_id: str,
        **updates
    ) -> Optional[LiveSearchAlert]:
        """Update a live alert."""
        try:
            alert = await self.get_alert(db, alert_id)
            if not alert:
                return None
            
            # Update fields
            for key, value in updates.items():
                if hasattr(alert, key):
                    if key == 'expiration_type':
                        value = LiveAlertExpirationType(value)
                    setattr(alert, key, value)
            
            alert.updated_at = datetime.utcnow()
            await db.commit()
            await db.refresh(alert)
            
            logger.info(f"[LIVE_ALERT] Updated alert: {alert_id}")
            return alert
            
        except Exception as e:
            logger.error(f"[LIVE_ALERT] Error updating alert: {e}")
            await db.rollback()
            raise
    
    async def delete_alert(
        self,
        db: AsyncSession,
        alert_id: str
    ) -> bool:
        """Delete a live alert."""
        try:
            alert = await self.get_alert(db, alert_id)
            if not alert:
                return False
            
            await db.delete(alert)
            await db.commit()
            
            logger.info(f"[LIVE_ALERT] Deleted alert: {alert_id}")
            return True
            
        except Exception as e:
            logger.error(f"[LIVE_ALERT] Error deleting alert: {e}")
            await db.rollback()
            raise
    
    async def pause_alert(
        self,
        db: AsyncSession,
        alert_id: str
    ) -> bool:
        """Pause a live alert."""
        return await self._set_alert_status(db, alert_id, LiveAlertStatus.PAUSED)
    
    async def resume_alert(
        self,
        db: AsyncSession,
        alert_id: str
    ) -> bool:
        """Resume a paused alert."""
        return await self._set_alert_status(db, alert_id, LiveAlertStatus.ACTIVE)
    
    async def _set_alert_status(
        self,
        db: AsyncSession,
        alert_id: str,
        status: LiveAlertStatus
    ) -> bool:
        """Set alert status."""
        try:
            alert = await self.get_alert(db, alert_id)
            if not alert:
                return False
            
            alert.status = status
            alert.updated_at = datetime.utcnow()
            await db.commit()
            
            logger.info(f"[LIVE_ALERT] Set alert {alert_id} status to {status.value}")
            return True
            
        except Exception as e:
            logger.error(f"[LIVE_ALERT] Error setting alert status: {e}")
            await db.rollback()
            raise
    
    async def check_detection_against_alerts(
        self,
        db: AsyncSession,
        identity_id: str,
        similarity: float,
        pipeline_id: str,
        detection_id: int = None,
        snapshot_path: str = None
    ) -> List[AlertTriggerInfo]:
        """
        Check if a detection should trigger any alerts.
        
        This method should be called whenever a face is detected and matched
        to an identity.
        
        Returns a list of triggered alerts that need notifications sent.
        """
        triggers = []
        
        # Get all active alerts for this identity
        alerts = await self.get_active_alerts_for_identity(db, identity_id)
        
        if not alerts:
            return triggers
        
        now = datetime.utcnow()
        current_time = now.time()
        current_day = now.weekday()  # 0 = Monday, 6 = Sunday
        
        for alert in alerts:
            # Check if alert should trigger
            should_trigger = await self._should_alert_trigger(
                alert=alert,
                similarity=similarity,
                pipeline_id=pipeline_id,
                current_time=current_time,
                current_day=current_day,
                now=now
            )
            
            if should_trigger:
                # Create trigger record
                trigger = await self._create_trigger(
                    db=db,
                    alert=alert,
                    detection_id=detection_id,
                    pipeline_id=pipeline_id,
                    similarity=similarity,
                    snapshot_path=snapshot_path
                )
                
                if trigger:
                    # Get identity name
                    identity_name = None
                    if alert.identity:
                        identity_name = alert.identity.display_name

                    triggers.append(AlertTriggerInfo(
                        trigger_id=str(trigger.id),
                        alert_id=str(alert.id),
                        alert_name=alert.name,
                        identity_id=identity_id,
                        identity_name=identity_name,
                        similarity=similarity,
                        pipeline_id=pipeline_id,
                        triggered_at=now,
                        should_notify_dashboard=alert.notify_dashboard,
                        should_notify_email=alert.notify_email,
                        should_notify_sms=alert.notify_sms,
                        should_notify_webhook=alert.notify_webhook,
                        email_recipients=alert.email_recipients or [],
                        sms_recipients=alert.sms_recipients or [],
                        webhook_url=alert.webhook_url,
                        sound_alert=alert.sound_alert
                    ))
                    
                    # Check if alert should expire
                    await self._check_alert_expiration(db, alert)
        
        return triggers
    
    async def _should_alert_trigger(
        self,
        alert: LiveSearchAlert,
        similarity: float,
        pipeline_id: str,
        current_time: dt_time,
        current_day: int,
        now: datetime
    ) -> bool:
        """Check if alert conditions are met."""
        
        # Check similarity threshold
        if similarity < alert.min_similarity:
            return False
        
        # Check pipeline filter
        if alert.pipeline_ids:
            if pipeline_id not in alert.pipeline_ids:
                return False
        
        # Check time window
        if alert.time_window_enabled:
            if alert.time_window_start and alert.time_window_end:
                try:
                    start = dt_time.fromisoformat(alert.time_window_start)
                    end = dt_time.fromisoformat(alert.time_window_end)
                    
                    if start <= end:
                        # Normal case: start < end (e.g., 09:00-17:00)
                        if not (start <= current_time <= end):
                            return False
                    else:
                        # Overnight case: end < start (e.g., 22:00-06:00)
                        if not (current_time >= start or current_time <= end):
                            return False
                except ValueError:
                    pass  # Invalid time format, ignore filter
        
        # Check active days
        if alert.active_days:
            # Convert weekday to match our format (0=Sun in our system)
            day_index = (current_day + 1) % 7  # Convert Mon=0 to Sun=0
            if day_index not in alert.active_days:
                return False
        
        # Check cooldown
        if alert.last_triggered_at and alert.cooldown_minutes:
            cooldown_delta = (now - alert.last_triggered_at).total_seconds() / 60
            if cooldown_delta < alert.cooldown_minutes:
                return False
        
        # Check expiration
        if alert.expiration_type == LiveAlertExpirationType.DATE:
            if alert.expiration_date and now >= alert.expiration_date:
                return False
        
        return True
    
    async def _create_trigger(
        self,
        db: AsyncSession,
        alert: LiveSearchAlert,
        detection_id: int,
        pipeline_id: str,
        similarity: float,
        snapshot_path: str
    ) -> Optional[LiveAlertTrigger]:
        """Create a trigger record and update alert (idempotent per detection).

        Redis retries, webhook redeliveries and multiple publishers can call
        this more than once for the same detection — the (alert_id,
        detection_id) partial unique index plus this pre-check guarantee at
        most ONE trigger row per detection.
        """
        try:
            if detection_id is not None:
                existing = (await db.execute(
                    select(LiveAlertTrigger.id).where(
                        and_(LiveAlertTrigger.alert_id == alert.id,
                             LiveAlertTrigger.detection_id == detection_id)
                    ).limit(1)
                )).scalar_one_or_none()
                if existing:
                    logger.info(
                        "[ALERT] duplicate suppressed alert_id=%s detection_id=%s trigger_id=%s",
                        alert.id, detection_id, existing)
                    return None

            trigger = LiveAlertTrigger(
                alert_id=alert.id,
                detection_id=detection_id,
                pipeline_id=pipeline_id,
                similarity_score=similarity,
                snapshot_path=snapshot_path
            )
            db.add(trigger)

            # Update alert
            alert.triggers_count += 1
            alert.last_triggered_at = datetime.utcnow()

            await db.commit()
            await db.refresh(trigger)

            logger.info(
                "[ALERT] triggered alert_id=%s trigger_id=%s detection_id=%s pipeline_id=%s "
                "similarity=%.3f total_triggers=%d",
                alert.id, trigger.id, detection_id, pipeline_id,
                similarity, alert.triggers_count)
            return trigger

        except Exception as e:
            # IntegrityError on the unique index = concurrent duplicate — benign
            await db.rollback()
            if "uq_alert_trigger_alert_detection" in str(e):
                logger.info(f"[ALERT] duplicate suppressed by unique index alert_id={alert.id} detection_id={detection_id}")
            else:
                logger.error(f"[ALERT] error creating trigger alert_id={alert.id}: {e}")
            return None
    
    async def _check_alert_expiration(
        self,
        db: AsyncSession,
        alert: LiveSearchAlert
    ):
        """Check if alert should expire based on triggers count."""
        try:
            if alert.expiration_type == LiveAlertExpirationType.DETECTIONS:
                if alert.expiration_detections and alert.triggers_count >= alert.expiration_detections:
                    alert.status = LiveAlertStatus.EXPIRED
                    await db.commit()
                    logger.info(f"[LIVE_ALERT] Alert '{alert.name}' expired after {alert.triggers_count} triggers")
        except Exception as e:
            logger.error(f"[LIVE_ALERT] Error checking expiration: {e}")
    
    async def _get_user_alert_count(
        self,
        db: AsyncSession,
        user_id: int
    ) -> int:
        """Get count of active alerts for a user."""
        query = select(func.count()).select_from(LiveSearchAlert).where(
            and_(
                LiveSearchAlert.created_by == user_id,
                LiveSearchAlert.status.in_([
                    LiveAlertStatus.ACTIVE,
                    LiveAlertStatus.PAUSED
                ])
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0
    
    async def _get_identity_alert_count(
        self,
        db: AsyncSession,
        identity_id: str,
        user_id: int
    ) -> int:
        """Get count of active alerts for a specific identity created by a user."""
        query = select(func.count()).select_from(LiveSearchAlert).where(
            and_(
                LiveSearchAlert.identity_id == uuid.UUID(identity_id),
                LiveSearchAlert.created_by == user_id,
                LiveSearchAlert.status == LiveAlertStatus.ACTIVE
            )
        )
        result = await db.execute(query)
        return result.scalar() or 0
    
    async def get_alert_triggers(
        self,
        db: AsyncSession,
        alert_id: str,
        limit: int = 100,
        offset: int = 0
    ) -> List[LiveAlertTrigger]:
        """Get trigger history for an alert (legacy list form)."""
        query = select(LiveAlertTrigger).where(
            LiveAlertTrigger.alert_id == uuid.UUID(alert_id)
        ).order_by(
            LiveAlertTrigger.created_at.desc()
        ).limit(limit).offset(offset)

        result = await db.execute(query)
        return result.scalars().all()

    async def get_alert_triggers_page(
        self,
        db: AsyncSession,
        alert_id: str,
        page: int = 1,
        page_size: int = 20,
        acknowledged: Optional[bool] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        pipeline_id: Optional[str] = None,
        min_similarity: Optional[float] = None,
        sort_order: str = "desc",
    ) -> Dict[str, Any]:
        """Server-side paginated + filtered trigger history."""
        conditions = [LiveAlertTrigger.alert_id == uuid.UUID(alert_id)]
        if acknowledged is not None:
            conditions.append(LiveAlertTrigger.acknowledged == acknowledged)
        if date_from is not None:
            conditions.append(LiveAlertTrigger.created_at >= date_from)
        if date_to is not None:
            conditions.append(LiveAlertTrigger.created_at <= date_to)
        if pipeline_id:
            conditions.append(LiveAlertTrigger.pipeline_id == pipeline_id)
        if min_similarity is not None:
            conditions.append(LiveAlertTrigger.similarity_score >= min_similarity)

        where = and_(*conditions)
        total = (await db.execute(
            select(func.count(LiveAlertTrigger.id)).where(where)
        )).scalar() or 0

        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))

        order = (LiveAlertTrigger.created_at.asc() if sort_order == "asc"
                 else LiveAlertTrigger.created_at.desc())
        rows = (await db.execute(
            select(LiveAlertTrigger)
            .options(selectinload(LiveAlertTrigger.acknowledged_by_user))
            .where(where).order_by(order)
            .limit(page_size).offset((page - 1) * page_size)
        )).scalars().all()

        unacknowledged_total = (await db.execute(
            select(func.count(LiveAlertTrigger.id)).where(
                and_(LiveAlertTrigger.alert_id == uuid.UUID(alert_id),
                     LiveAlertTrigger.acknowledged == False)  # noqa: E712
            )
        )).scalar() or 0

        return {
            "items": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "unacknowledged_total": unacknowledged_total,
        }

    async def get_trigger_with_alert(
        self,
        db: AsyncSession,
        trigger_id: str,
    ) -> Optional[LiveAlertTrigger]:
        """Get a trigger with its parent alert loaded (for ownership checks)."""
        try:
            trig_uuid = uuid.UUID(trigger_id)
        except ValueError:
            return None
        return (await db.execute(
            select(LiveAlertTrigger)
            .options(selectinload(LiveAlertTrigger.alert))
            .where(LiveAlertTrigger.id == trig_uuid)
        )).scalar_one_or_none()

    async def acknowledge_trigger(
        self,
        db: AsyncSession,
        trigger_id: str,
        acknowledged_by: int
    ) -> bool:
        """Acknowledge a trigger (atomic — only flips if still unacknowledged)."""
        try:
            result = await db.execute(
                update(LiveAlertTrigger)
                .where(LiveAlertTrigger.id == uuid.UUID(trigger_id))
                .values(acknowledged=True,
                        acknowledged_by=acknowledged_by,
                        acknowledged_at=datetime.utcnow())
            )
            await db.commit()
            ok = (result.rowcount or 0) > 0
            if ok:
                logger.info(f"[ALERT] acknowledged trigger_id={trigger_id} user_id={acknowledged_by}")
            return ok

        except Exception as e:
            logger.error(f"[ALERT] Error acknowledging trigger {trigger_id}: {e}")
            await db.rollback()
            raise

    async def bulk_acknowledge(
        self,
        db: AsyncSession,
        alert_id: str,
        acknowledged_by: int,
        trigger_ids: Optional[List[str]] = None,
    ) -> Dict[str, int]:
        """Acknowledge many triggers in ONE statement (never N+1 requests).

        With trigger_ids=None every unacknowledged trigger of the alert is
        acknowledged; otherwise only the listed ids (still scoped to the
        alert). Returns {"acknowledged": n, "failed": m}.
        """
        try:
            conditions = [
                LiveAlertTrigger.alert_id == uuid.UUID(alert_id),
                LiveAlertTrigger.acknowledged == False,  # noqa: E712
            ]
            requested = None
            if trigger_ids is not None:
                parsed = []
                for tid in trigger_ids[:500]:  # bounded batch
                    try:
                        parsed.append(uuid.UUID(str(tid)))
                    except ValueError:
                        continue
                conditions.append(LiveAlertTrigger.id.in_(parsed))
                requested = len(parsed)

            result = await db.execute(
                update(LiveAlertTrigger)
                .where(and_(*conditions))
                .values(acknowledged=True,
                        acknowledged_by=acknowledged_by,
                        acknowledged_at=datetime.utcnow())
            )
            await db.commit()
            acknowledged = result.rowcount or 0
            failed = max(0, (requested or acknowledged) - acknowledged)
            logger.info(
                "[ALERT] bulk_acknowledge alert_id=%s user_id=%s acknowledged=%d failed=%d",
                alert_id, acknowledged_by, acknowledged, failed)
            return {"acknowledged": acknowledged, "failed": failed}

        except Exception as e:
            logger.error(f"[ALERT] Error bulk-acknowledging alert {alert_id}: {e}")
            await db.rollback()
            raise
    
    async def cleanup_expired_alerts(
        self,
        db: AsyncSession
    ) -> int:
        """Mark expired alerts as expired."""
        try:
            now = datetime.utcnow()
            
            # Find date-based expired alerts
            query = select(LiveSearchAlert).where(
                and_(
                    LiveSearchAlert.status == LiveAlertStatus.ACTIVE,
                    LiveSearchAlert.expiration_type == LiveAlertExpirationType.DATE,
                    LiveSearchAlert.expiration_date != None,
                    LiveSearchAlert.expiration_date <= now
                )
            )
            result = await db.execute(query)
            alerts = result.scalars().all()
            
            count = 0
            for alert in alerts:
                alert.status = LiveAlertStatus.EXPIRED
                count += 1
            
            await db.commit()
            
            if count > 0:
                logger.info(f"[LIVE_ALERT] Expired {count} alerts")
            
            return count
            
        except Exception as e:
            logger.error(f"[LIVE_ALERT] Error cleaning up expired alerts: {e}")
            await db.rollback()
            raise


# Global instance
live_alert_service = LiveAlertService()


