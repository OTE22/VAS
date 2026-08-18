"""
Watchlist Service
==================
Manages watchlists (VIP, Threat, POI, etc.) for identity monitoring.
"""

import logging
import uuid
from datetime import datetime
from typing import List, Dict, Optional, Any
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select, and_, or_, func, delete
from sqlalchemy.orm import selectinload

from db_models import (
    Watchlist, WatchlistEntry, WatchlistAlert, Identity,
    WatchlistAlertLevel, WatchlistEntryPriority
)

logger = logging.getLogger(__name__)


class WatchlistNameConflict(Exception):
    """A live (non-deleted) watchlist with this name already exists."""


class WatchlistVersionConflict(Exception):
    """The record changed since the client read it (optimistic concurrency)."""

    def __init__(self, current_version: int):
        self.current_version = current_version
        super().__init__(f"version conflict, current={current_version}")


def _parse_watchlist_uuid(value: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass
class WatchlistStats:
    """Statistics for a watchlist"""
    total_entries: int
    active_entries: int
    expired_entries: int
    alerts_today: int
    alerts_total: int
    unacknowledged_alerts: int


class WatchlistService:
    """
    Service for managing watchlists and entries.
    """
    
    # ==================== WATCHLIST CRUD ====================
    
    async def create_watchlist(
        self,
        db: AsyncSession,
        name: str,
        description: str = None,
        color: str = "#6366f1",
        icon: str = "list",
        alert_level: str = "info",
        notify_dashboard: bool = True,
        notify_email: bool = False,
        notify_sms: bool = False,
        notify_webhook: bool = False,
        email_recipients: List[str] = None,
        sms_recipients: List[str] = None,
        webhook_url: str = None,
        created_by: int = None
    ) -> Watchlist:
        """Create a new watchlist (case-insensitive name uniqueness among live rows)."""
        if await self.live_name_exists(db, name):
            raise WatchlistNameConflict(name)
        try:
            watchlist = Watchlist(
                name=name,
                description=description,
                color=color,
                icon=icon,
                alert_level=WatchlistAlertLevel(alert_level),
                notify_dashboard=notify_dashboard,
                notify_email=notify_email,
                notify_sms=notify_sms,
                notify_webhook=notify_webhook,
                email_recipients=email_recipients,
                sms_recipients=sms_recipients,
                webhook_url=webhook_url,
                created_by=created_by,
                is_active=True
            )
            db.add(watchlist)
            await db.commit()
            await db.refresh(watchlist)
            
            logger.info(f"[WATCHLIST] Created watchlist: {name} (id={watchlist.id})")
            return watchlist
            
        except Exception as e:
            logger.error(f"[WATCHLIST] Error creating watchlist: {e}")
            await db.rollback()
            raise
    
    async def get_watchlist(
        self,
        db: AsyncSession,
        watchlist_id: str,
        include_deleted: bool = False
    ) -> Optional[Watchlist]:
        """Get a watchlist by ID. Malformed ids resolve to None (-> 404)."""
        wl_uuid = _parse_watchlist_uuid(watchlist_id)
        if wl_uuid is None:
            return None
        query = select(Watchlist).where(Watchlist.id == wl_uuid)
        if not include_deleted:
            query = query.where(Watchlist.deleted_at.is_(None))
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def live_name_exists(
        self,
        db: AsyncSession,
        name: str,
        exclude_id: Optional[uuid.UUID] = None
    ) -> bool:
        """Case-insensitive name collision among non-deleted watchlists."""
        normalized = (name or "").strip().lower()
        query = select(func.count()).select_from(Watchlist).where(
            func.lower(func.btrim(Watchlist.name)) == normalized,
            Watchlist.deleted_at.is_(None)
        )
        if exclude_id is not None:
            query = query.where(Watchlist.id != exclude_id)
        return ((await db.execute(query)).scalar() or 0) > 0

    async def get_all_watchlists(
        self,
        db: AsyncSession,
        include_inactive: bool = False,
        include_deleted: bool = False
    ) -> List[Watchlist]:
        """Get all watchlists (soft-deleted excluded by default)."""
        query = select(Watchlist)
        if not include_deleted:
            query = query.where(Watchlist.deleted_at.is_(None))
        if not include_inactive:
            query = query.where(Watchlist.is_active == True)
        query = query.order_by(Watchlist.name)

        result = await db.execute(query)
        return result.scalars().all()

    async def list_watchlists_page(
        self,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
        search: Optional[str] = None,
        alert_level: Optional[str] = None,
        is_active: Optional[bool] = None,
        include_deleted: bool = False,
        sort_by: str = "name",
        sort_order: str = "asc",
    ):
        """Server-side paginated listing. Returns (watchlists, total)."""
        filters = []
        if not include_deleted:
            filters.append(Watchlist.deleted_at.is_(None))
        if is_active is not None:
            filters.append(Watchlist.is_active == is_active)
        if alert_level:
            filters.append(Watchlist.alert_level == WatchlistAlertLevel(alert_level))
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append(or_(
                Watchlist.name.ilike(f"%{escaped}%", escape="\\"),
                Watchlist.description.ilike(f"%{escaped}%", escape="\\"),
            ))

        sort_columns = {
            "name": Watchlist.name,
            "created_at": Watchlist.created_at,
            "updated_at": Watchlist.updated_at,
            "alert_level": Watchlist.alert_level,
        }
        col = sort_columns.get(sort_by, Watchlist.name)
        order = col.desc().nulls_last() if str(sort_order).lower() == "desc" else col.asc().nulls_last()

        base = select(Watchlist)
        if filters:
            base = base.where(and_(*filters))

        count_q = select(func.count()).select_from(Watchlist)
        if filters:
            count_q = count_q.where(and_(*filters))
        total = (await db.execute(count_q)).scalar() or 0

        result = await db.execute(
            base.order_by(order).offset((page - 1) * page_size).limit(page_size)
        )
        return result.scalars().all(), total

    async def batch_watchlist_stats(
        self,
        db: AsyncSession,
        watchlist_ids: List[uuid.UUID],
        day_start: datetime,
    ) -> Dict[uuid.UUID, Dict[str, Any]]:
        """Batched (no N+1) per-watchlist counters for a page of watchlists."""
        stats = {
            wl_id: {"entries_count": 0, "alerts_today": 0, "total_alerts": 0, "last_alert_at": None}
            for wl_id in watchlist_ids
        }
        if not watchlist_ids:
            return stats

        entry_rows = await db.execute(
            select(WatchlistEntry.watchlist_id, func.count())
            .where(
                WatchlistEntry.watchlist_id.in_(watchlist_ids),
                WatchlistEntry.is_active == True,
                or_(WatchlistEntry.expires_at == None,
                    WatchlistEntry.expires_at > datetime.utcnow())
            )
            .group_by(WatchlistEntry.watchlist_id)
        )
        for wl_id, count in entry_rows:
            stats[wl_id]["entries_count"] = count or 0

        alert_rows = await db.execute(
            select(
                WatchlistEntry.watchlist_id,
                func.count(),
                func.count().filter(WatchlistAlert.created_at >= day_start),
                func.max(WatchlistAlert.created_at),
            )
            .select_from(WatchlistAlert)
            .join(WatchlistEntry, WatchlistAlert.watchlist_entry_id == WatchlistEntry.id)
            .where(WatchlistEntry.watchlist_id.in_(watchlist_ids))
            .group_by(WatchlistEntry.watchlist_id)
        )
        for wl_id, total_alerts, today, last_at in alert_rows:
            stats[wl_id]["total_alerts"] = total_alerts or 0
            stats[wl_id]["alerts_today"] = today or 0
            stats[wl_id]["last_alert_at"] = last_at
        return stats
    
    async def update_watchlist(
        self,
        db: AsyncSession,
        watchlist_id: str,
        expected_version: Optional[int] = None,
        **updates
    ) -> Optional[Watchlist]:
        """Update a watchlist with optimistic concurrency.

        When expected_version is supplied and does not match the stored
        version, WatchlistVersionConflict is raised (-> 409) so concurrent
        admin edits never silently overwrite each other.
        """
        try:
            watchlist = await self.get_watchlist(db, watchlist_id)
            if not watchlist:
                return None

            if expected_version is not None and (watchlist.version or 1) != expected_version:
                raise WatchlistVersionConflict(watchlist.version or 1)

            new_name = updates.get('name')
            if new_name and new_name.strip().lower() != (watchlist.name or "").strip().lower():
                if await self.live_name_exists(db, new_name, exclude_id=watchlist.id):
                    raise WatchlistNameConflict(new_name)

            for key, value in updates.items():
                if hasattr(watchlist, key):
                    if key == 'alert_level':
                        value = WatchlistAlertLevel(value)
                    setattr(watchlist, key, value)

            watchlist.version = (watchlist.version or 1) + 1
            watchlist.updated_at = datetime.utcnow()
            await db.commit()
            await db.refresh(watchlist)

            logger.info(f"[WATCHLIST] Updated watchlist: {watchlist_id} version={watchlist.version}")
            return watchlist

        except (WatchlistVersionConflict, WatchlistNameConflict):
            await db.rollback()
            raise
        except Exception as e:
            logger.error(f"[WATCHLIST] Error updating watchlist: {e}")
            await db.rollback()
            raise

    async def soft_delete_watchlist(
        self,
        db: AsyncSession,
        watchlist_id: str,
        deleted_by_user_id: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> Optional[Watchlist]:
        """Soft delete: matching stops, history and entries are preserved."""
        try:
            watchlist = await self.get_watchlist(db, watchlist_id)
            if not watchlist:
                return None
            watchlist.deleted_at = datetime.utcnow()
            watchlist.deleted_by_user_id = deleted_by_user_id
            watchlist.deletion_reason = reason
            watchlist.is_active = False
            watchlist.version = (watchlist.version or 1) + 1
            watchlist.updated_at = datetime.utcnow()
            await db.commit()
            await db.refresh(watchlist)
            logger.info(f"[WATCHLIST] Soft-deleted watchlist: {watchlist_id}")
            return watchlist
        except Exception as e:
            logger.error(f"[WATCHLIST] Error soft-deleting watchlist: {e}")
            await db.rollback()
            raise

    async def restore_watchlist(
        self,
        db: AsyncSession,
        watchlist_id: str,
    ) -> Optional[Watchlist]:
        """Restore a soft-deleted watchlist (name must still be free)."""
        try:
            watchlist = await self.get_watchlist(db, watchlist_id, include_deleted=True)
            if not watchlist or watchlist.deleted_at is None:
                return None
            if await self.live_name_exists(db, watchlist.name, exclude_id=watchlist.id):
                raise WatchlistNameConflict(watchlist.name)
            watchlist.deleted_at = None
            watchlist.deleted_by_user_id = None
            watchlist.deletion_reason = None
            watchlist.is_active = True
            watchlist.version = (watchlist.version or 1) + 1
            watchlist.updated_at = datetime.utcnow()
            await db.commit()
            await db.refresh(watchlist)
            logger.info(f"[WATCHLIST] Restored watchlist: {watchlist_id}")
            return watchlist
        except WatchlistNameConflict:
            await db.rollback()
            raise
        except Exception as e:
            logger.error(f"[WATCHLIST] Error restoring watchlist: {e}")
            await db.rollback()
            raise

    async def deletion_impact(
        self,
        db: AsyncSession,
        watchlist_id: str,
    ) -> Optional[Dict[str, int]]:
        """Impact summary shown to the admin BEFORE deletion is confirmed."""
        watchlist = await self.get_watchlist(db, watchlist_id, include_deleted=True)
        if not watchlist:
            return None
        wl_id = watchlist.id
        entries = (await db.execute(
            select(func.count()).select_from(WatchlistEntry)
            .where(WatchlistEntry.watchlist_id == wl_id)
        )).scalar() or 0
        active_entries = (await db.execute(
            select(func.count()).select_from(WatchlistEntry)
            .where(WatchlistEntry.watchlist_id == wl_id,
                   WatchlistEntry.is_active == True)
        )).scalar() or 0
        alerts = (await db.execute(
            select(func.count()).select_from(WatchlistAlert)
            .join(WatchlistEntry, WatchlistAlert.watchlist_entry_id == WatchlistEntry.id)
            .where(WatchlistEntry.watchlist_id == wl_id)
        )).scalar() or 0
        return {"entries": entries, "active_entries": active_entries, "alerts": alerts}

    async def delete_watchlist(
        self,
        db: AsyncSession,
        watchlist_id: str,
        hard_delete: bool = False
    ) -> bool:
        """Legacy entry point. hard_delete=True permanently cascades — the
        API layer requires an explicit confirmation for that path."""
        try:
            watchlist = await self.get_watchlist(db, watchlist_id, include_deleted=True)
            if not watchlist:
                return False

            if hard_delete:
                await db.delete(watchlist)
                await db.commit()
                logger.info(f"[WATCHLIST] HARD-deleted watchlist: {watchlist_id}")
            else:
                await db.rollback()
                return (await self.soft_delete_watchlist(db, watchlist_id)) is not None
            return True

        except Exception as e:
            logger.error(f"[WATCHLIST] Error deleting watchlist: {e}")
            await db.rollback()
            raise
    
    # ==================== WATCHLIST ENTRIES ====================
    
    async def add_to_watchlist(
        self,
        db: AsyncSession,
        watchlist_id: str,
        identity_id: str,
        priority: str = "normal",
        notes: str = None,
        action_instructions: str = None,
        expires_at: datetime = None,
        added_by: int = None
    ) -> WatchlistEntry:
        """Add an identity to a watchlist."""
        try:
            # Check if already exists
            existing = await self.get_entry(db, watchlist_id, identity_id)
            if existing:
                # Update existing entry
                existing.priority = WatchlistEntryPriority(priority)
                existing.notes = notes
                existing.action_instructions = action_instructions
                existing.expires_at = expires_at
                existing.is_active = True
                await db.commit()
                await db.refresh(existing)
                logger.info(f"[WATCHLIST] Updated existing entry: {identity_id} in {watchlist_id}")
                return existing
            
            entry = WatchlistEntry(
                watchlist_id=uuid.UUID(watchlist_id),
                identity_id=uuid.UUID(identity_id),
                priority=WatchlistEntryPriority(priority),
                notes=notes,
                action_instructions=action_instructions,
                expires_at=expires_at,
                added_by=added_by,
                is_active=True
            )
            db.add(entry)
            await db.commit()
            await db.refresh(entry)
            
            logger.info(f"[WATCHLIST] Added identity {identity_id} to watchlist {watchlist_id}")
            return entry
            
        except Exception as e:
            logger.error(f"[WATCHLIST] Error adding to watchlist: {e}")
            await db.rollback()
            raise
    
    async def get_entry(
        self,
        db: AsyncSession,
        watchlist_id: str,
        identity_id: str
    ) -> Optional[WatchlistEntry]:
        """Get a specific watchlist entry. Malformed ids resolve to None."""
        wl_uuid = _parse_watchlist_uuid(watchlist_id)
        id_uuid = _parse_watchlist_uuid(identity_id)
        if wl_uuid is None or id_uuid is None:
            return None
        query = select(WatchlistEntry).where(
            and_(
                WatchlistEntry.watchlist_id == wl_uuid,
                WatchlistEntry.identity_id == id_uuid
            )
        )
        result = await db.execute(query)
        return result.scalar_one_or_none()
    
    async def get_entries(
        self,
        db: AsyncSession,
        watchlist_id: str,
        include_inactive: bool = False,
        include_expired: bool = False
    ) -> List[WatchlistEntry]:
        """Get all entries for a watchlist."""
        query = select(WatchlistEntry).options(
            selectinload(WatchlistEntry.identity)
        ).where(
            WatchlistEntry.watchlist_id == uuid.UUID(watchlist_id)
        )
        
        if not include_inactive:
            query = query.where(WatchlistEntry.is_active == True)
        
        if not include_expired:
            query = query.where(
                or_(
                    WatchlistEntry.expires_at == None,
                    WatchlistEntry.expires_at > datetime.utcnow()
                )
            )
        
        query = query.order_by(WatchlistEntry.added_at.desc())
        
        result = await db.execute(query)
        return result.scalars().all()
    
    async def get_identity_watchlists(
        self,
        db: AsyncSession,
        identity_id: str
    ) -> List[Dict]:
        """Get all watchlists an identity is on."""
        query = select(WatchlistEntry).options(
            selectinload(WatchlistEntry.watchlist)
        ).where(
            and_(
                WatchlistEntry.identity_id == uuid.UUID(identity_id),
                WatchlistEntry.is_active == True,
                or_(
                    WatchlistEntry.expires_at == None,
                    WatchlistEntry.expires_at > datetime.utcnow()
                )
            )
        )
        
        result = await db.execute(query)
        entries = result.scalars().all()
        
        watchlists = []
        for entry in entries:
            # Deleted or inactive watchlists never match
            if entry.watchlist and entry.watchlist.is_active and entry.watchlist.deleted_at is None:
                watchlists.append({
                    'watchlist_id': str(entry.watchlist_id),
                    'watchlist_name': entry.watchlist.name,
                    'alert_level': entry.watchlist.alert_level.value,
                    'color': entry.watchlist.color,
                    'icon': entry.watchlist.icon,
                    'priority': entry.priority.value,
                    'notes': entry.notes,
                    'action_instructions': entry.action_instructions,
                    'added_at': entry.added_at.isoformat() if entry.added_at else None,
                    'expires_at': entry.expires_at.isoformat() if entry.expires_at else None
                })
        
        return watchlists
    
    async def remove_from_watchlist(
        self,
        db: AsyncSession,
        watchlist_id: str,
        identity_id: str,
        hard_delete: bool = False
    ) -> bool:
        """Remove an identity from a watchlist."""
        try:
            entry = await self.get_entry(db, watchlist_id, identity_id)
            if not entry:
                return False
            
            if hard_delete:
                await db.delete(entry)
            else:
                entry.is_active = False
            
            await db.commit()
            logger.info(f"[WATCHLIST] Removed identity {identity_id} from watchlist {watchlist_id}")
            return True
            
        except Exception as e:
            logger.error(f"[WATCHLIST] Error removing from watchlist: {e}")
            await db.rollback()
            raise
    
    # ==================== ALERTS ====================
    
    async def get_alerts(
        self,
        db: AsyncSession,
        watchlist_id: str = None,
        acknowledged: bool = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[WatchlistAlert]:
        """Get watchlist alerts."""
        query = select(WatchlistAlert).options(
            selectinload(WatchlistAlert.entry).selectinload(WatchlistEntry.watchlist),
            selectinload(WatchlistAlert.entry).selectinload(WatchlistEntry.identity)
        )
        
        if watchlist_id:
            query = query.join(WatchlistEntry).where(
                WatchlistEntry.watchlist_id == uuid.UUID(watchlist_id)
            )
        
        if acknowledged is not None:
            query = query.where(WatchlistAlert.acknowledged == acknowledged)
        
        query = query.order_by(WatchlistAlert.created_at.desc())
        query = query.limit(limit).offset(offset)
        
        result = await db.execute(query)
        return result.scalars().all()
    
    async def acknowledge_alert(
        self,
        db: AsyncSession,
        alert_id: str,
        acknowledged_by: int,
        notes: str = None
    ) -> bool:
        """Acknowledge a watchlist alert."""
        try:
            query = select(WatchlistAlert).where(
                WatchlistAlert.id == uuid.UUID(alert_id)
            )
            result = await db.execute(query)
            alert = result.scalar_one_or_none()
            
            if not alert:
                return False
            
            alert.acknowledged = True
            alert.acknowledged_by = acknowledged_by
            alert.acknowledged_at = datetime.utcnow()
            if notes:
                alert.notes = notes
            
            await db.commit()
            logger.info(f"[WATCHLIST] Acknowledged alert: {alert_id}")
            return True
            
        except Exception as e:
            logger.error(f"[WATCHLIST] Error acknowledging alert: {e}")
            await db.rollback()
            raise
    
    # ==================== STATISTICS ====================
    
    async def get_watchlist_stats(
        self,
        db: AsyncSession,
        watchlist_id: str
    ) -> WatchlistStats:
        """Get statistics for a watchlist."""
        wl_id = uuid.UUID(watchlist_id)
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Count entries
        total_query = select(func.count()).select_from(WatchlistEntry).where(
            WatchlistEntry.watchlist_id == wl_id
        )
        total_result = await db.execute(total_query)
        total_entries = total_result.scalar() or 0
        
        # Active entries
        active_query = select(func.count()).select_from(WatchlistEntry).where(
            and_(
                WatchlistEntry.watchlist_id == wl_id,
                WatchlistEntry.is_active == True,
                or_(
                    WatchlistEntry.expires_at == None,
                    WatchlistEntry.expires_at > datetime.utcnow()
                )
            )
        )
        active_result = await db.execute(active_query)
        active_entries = active_result.scalar() or 0
        
        # Expired entries
        expired_query = select(func.count()).select_from(WatchlistEntry).where(
            and_(
                WatchlistEntry.watchlist_id == wl_id,
                WatchlistEntry.expires_at != None,
                WatchlistEntry.expires_at <= datetime.utcnow()
            )
        )
        expired_result = await db.execute(expired_query)
        expired_entries = expired_result.scalar() or 0
        
        # Alerts today
        alerts_today_query = select(func.count()).select_from(WatchlistAlert).join(
            WatchlistEntry
        ).where(
            and_(
                WatchlistEntry.watchlist_id == wl_id,
                WatchlistAlert.created_at >= today
            )
        )
        alerts_today_result = await db.execute(alerts_today_query)
        alerts_today = alerts_today_result.scalar() or 0
        
        # Total alerts
        alerts_total_query = select(func.count()).select_from(WatchlistAlert).join(
            WatchlistEntry
        ).where(
            WatchlistEntry.watchlist_id == wl_id
        )
        alerts_total_result = await db.execute(alerts_total_query)
        alerts_total = alerts_total_result.scalar() or 0
        
        # Unacknowledged
        unack_query = select(func.count()).select_from(WatchlistAlert).join(
            WatchlistEntry
        ).where(
            and_(
                WatchlistEntry.watchlist_id == wl_id,
                WatchlistAlert.acknowledged == False
            )
        )
        unack_result = await db.execute(unack_query)
        unacknowledged = unack_result.scalar() or 0
        
        return WatchlistStats(
            total_entries=total_entries,
            active_entries=active_entries,
            expired_entries=expired_entries,
            alerts_today=alerts_today,
            alerts_total=alerts_total,
            unacknowledged_alerts=unacknowledged
        )
    
    # ==================== BULK OPERATIONS ====================
    
    async def check_identities_against_watchlists(
        self,
        db: AsyncSession,
        identity_ids: List[str]
    ) -> Dict[str, List[Dict]]:
        """
        Check multiple identities against all active watchlists.
        Returns a dict mapping identity_id to list of watchlist matches.
        """
        if not identity_ids:
            return {}
        
        query = select(WatchlistEntry).options(
            selectinload(WatchlistEntry.watchlist)
        ).where(
            and_(
                WatchlistEntry.identity_id.in_([uuid.UUID(i) for i in identity_ids]),
                WatchlistEntry.is_active == True,
                or_(
                    WatchlistEntry.expires_at == None,
                    WatchlistEntry.expires_at > datetime.utcnow()
                )
            )
        )
        
        result = await db.execute(query)
        entries = result.scalars().all()
        
        matches = {}
        for entry in entries:
            # Deleted or inactive watchlists never generate alerts
            if entry.watchlist and entry.watchlist.is_active and entry.watchlist.deleted_at is None:
                identity_str = str(entry.identity_id)
                if identity_str not in matches:
                    matches[identity_str] = []
                
                matches[identity_str].append({
                    'entry_id': str(entry.id),
                    'watchlist_id': str(entry.watchlist_id),
                    'list_name': entry.watchlist.name,
                    'alert_level': entry.watchlist.alert_level.value,
                    'color': entry.watchlist.color,
                    'icon': entry.watchlist.icon,
                    'priority': entry.priority.value,
                    'notes': entry.notes,
                    'action_instructions': entry.action_instructions,
                    'notify_dashboard': bool(entry.watchlist.notify_dashboard),
                })
        
        return matches

    async def record_detection_alerts(
        self,
        db: AsyncSession,
        *,
        identity_id: str,
        detection_id: int,
        pipeline_id: str,
        similarity: float,
        snapshot_path: Optional[str],
    ) -> List[Dict]:
        """Persist detection-triggered watchlist alerts for ONE recognised identity
        of ONE detection. Flush only — joins the caller's transaction (the
        detection-evidence transaction; see backend/core/detection_evidence.py).

        Idempotent per (entry, detection) through the partial unique index
        `uq_watchlist_alert_entry_detection`: a retry inserts nothing and returns
        [] so nothing is broadcast twice. Returns the match dicts (+ alert_id) of
        the rows inserted in THIS call only.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        matches = (await self.check_identities_against_watchlists(db, [identity_id])).get(identity_id) or []
        if not matches:
            return []
        now = datetime.utcnow()
        rows = [{
            "id": uuid.uuid4(),
            "watchlist_entry_id": uuid.UUID(m["entry_id"]),
            "triggered_by": "detection",
            "detection_id": detection_id,
            "similarity_score": float(similarity),
            "pipeline_id": pipeline_id,
            "snapshot_path": snapshot_path,
            "acknowledged": False,
            "created_at": now,
        } for m in matches]
        stmt = (pg_insert(WatchlistAlert).values(rows)
                .on_conflict_do_nothing(index_elements=["watchlist_entry_id", "detection_id"],
                                        index_where=text("detection_id IS NOT NULL"))
                .returning(WatchlistAlert.id, WatchlistAlert.watchlist_entry_id))
        inserted = {str(r[1]): str(r[0]) for r in (await db.execute(stmt)).all()}
        persisted = []
        for m in matches:
            alert_id = inserted.get(m["entry_id"])
            if alert_id:
                persisted.append({**m, "alert_id": alert_id, "detection_id": detection_id})
        return persisted
    
    async def cleanup_expired_entries(
        self,
        db: AsyncSession
    ) -> int:
        """Deactivate expired watchlist entries."""
        try:
            query = select(WatchlistEntry).where(
                and_(
                    WatchlistEntry.is_active == True,
                    WatchlistEntry.expires_at != None,
                    WatchlistEntry.expires_at <= datetime.utcnow()
                )
            )
            result = await db.execute(query)
            entries = result.scalars().all()
            
            count = 0
            for entry in entries:
                entry.is_active = False
                count += 1
            
            await db.commit()
            
            if count > 0:
                logger.info(f"[WATCHLIST] Deactivated {count} expired entries")
            
            return count
            
        except Exception as e:
            logger.error(f"[WATCHLIST] Error cleaning up expired entries: {e}")
            await db.rollback()
            raise


# Global instance
watchlist_service = WatchlistService()


