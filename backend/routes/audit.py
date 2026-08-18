"""
Chatbot Audit Log Routes
========================
Admin-only endpoints to view chatbot usage audit logs.
"""

import os
import sys
import logging
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, Integer
from pydantic import BaseModel

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_connection import get_db
from db_models import ChatbotAuditLog, User
from backend.auth.auth_service import get_current_user, require_role
from backend.utils.time_utils import iso_utc, utc_now

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Audit"])


def to_naive_utc(dt: datetime) -> datetime:
    """
    Convert a datetime to naive UTC datetime.
    Database uses TIMESTAMP WITHOUT TIME ZONE, so we need naive datetimes.
    
    Args:
        dt: Datetime object (can be timezone-aware or naive)
        
    Returns:
        Naive datetime in UTC
    """
    if dt.tzinfo is not None:
        # Convert timezone-aware datetime to UTC, then make it naive
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        # Already naive, assume it's UTC
        return dt


class AuditLogResponse(BaseModel):
    id: int
    # NULL after the account was deleted (FK SET NULL, migration b0c1d2e3f4a5);
    # the durable numeric attribution is historical_user_id, stamped at deletion
    # time — read straight from the column, never reconstructed here.
    user_id: Optional[int] = None
    historical_user_id: Optional[int] = None
    username: str
    query: str
    response: Optional[str]
    success: bool
    error_message: Optional[str]
    processing_time_ms: Optional[float]
    session_id: Optional[str]
    created_at: str


class AuditLogStats(BaseModel):
    total_queries: int
    successful_queries: int
    failed_queries: int
    unique_users: int
    avg_processing_time_ms: Optional[float]
    total_processing_time_ms: float


@router.get("/api/audit/chatbot", response_model=List[AuditLogResponse])
async def get_chatbot_audit_logs(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user_id: Optional[int] = Query(default=None),
    username: Optional[str] = Query(default=None),
    success: Optional[bool] = Query(default=None),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """
    Get chatbot audit logs (admin only).
    
    Query parameters:
    - limit: Number of records to return (1-1000, default: 100)
    - offset: Number of records to skip (default: 0)
    - user_id: Filter by user ID
    - username: Filter by username
    - success: Filter by success status (true/false)
    - start_date: Filter from date (ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
    - end_date: Filter to date (ISO format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
    """
    try:
        query = select(ChatbotAuditLog)
        
        # Apply filters
        if user_id:
            query = query.where(ChatbotAuditLog.user_id == user_id)
        if username:
            query = query.where(ChatbotAuditLog.username.ilike(f"%{username}%"))
        if success is not None:
            query = query.where(ChatbotAuditLog.success == success)
        if start_date:
            try:
                # Parse datetime and handle timezone
                start_dt_str = start_date.replace('Z', '+00:00')
                start_dt = datetime.fromisoformat(start_dt_str)
                # Convert to naive UTC datetime for database
                start_dt = to_naive_utc(start_dt)
                query = query.where(ChatbotAuditLog.created_at >= start_dt)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid start_date format. Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
        if end_date:
            try:
                # Parse datetime and handle timezone
                end_dt_str = end_date.replace('Z', '+00:00')
                end_dt = datetime.fromisoformat(end_dt_str)
                # Convert to naive UTC datetime for database
                end_dt = to_naive_utc(end_dt)
                query = query.where(ChatbotAuditLog.created_at <= end_dt)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid end_date format. Use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
        
        # Order by created_at descending (newest first)
        query = query.order_by(desc(ChatbotAuditLog.created_at))
        
        # Apply pagination
        query = query.limit(limit).offset(offset)
        
        result = await db.execute(query)
        logs = result.scalars().all()
        
        return [
            AuditLogResponse(
                id=log.id,
                user_id=log.user_id,
                historical_user_id=log.historical_user_id,
                username=log.username,
                query=log.query,
                response=log.response,
                success=log.success,
                error_message=log.error_message,
                processing_time_ms=log.processing_time_ms,
                session_id=log.session_id,
                created_at=iso_utc(log.created_at)
            )
            for log in logs
        ]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching audit logs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/audit/chatbot/stats", response_model=AuditLogStats)
async def get_chatbot_audit_stats(
    user_id: Optional[int] = Query(default=None),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """
    Get chatbot audit log statistics (admin only).
    """
    try:
        query = select(
            func.count(ChatbotAuditLog.id).label("total"),
            func.sum(func.cast(ChatbotAuditLog.success, Integer)).label("successful"),
            func.count(func.distinct(ChatbotAuditLog.user_id)).label("unique_users"),
            func.avg(ChatbotAuditLog.processing_time_ms).label("avg_time"),
            func.sum(ChatbotAuditLog.processing_time_ms).label("total_time")
        )
        
        # Apply filters
        if user_id:
            query = query.where(ChatbotAuditLog.user_id == user_id)
        if start_date:
            try:
                # Parse datetime and handle timezone
                start_dt_str = start_date.replace('Z', '+00:00')
                start_dt = datetime.fromisoformat(start_dt_str)
                # Convert to naive UTC datetime for database
                start_dt = to_naive_utc(start_dt)
                query = query.where(ChatbotAuditLog.created_at >= start_dt)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid start_date format")
        if end_date:
            try:
                # Parse datetime and handle timezone
                end_dt_str = end_date.replace('Z', '+00:00')
                end_dt = datetime.fromisoformat(end_dt_str)
                # Convert to naive UTC datetime for database
                end_dt = to_naive_utc(end_dt)
                query = query.where(ChatbotAuditLog.created_at <= end_dt)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid end_date format")
        
        result = await db.execute(query)
        stats = result.first()
        
        total = stats.total or 0
        successful = stats.successful or 0
        failed = total - successful
        
        return AuditLogStats(
            total_queries=total,
            successful_queries=successful,
            failed_queries=failed,
            unique_users=stats.unique_users or 0,
            avg_processing_time_ms=float(stats.avg_time) if stats.avg_time else None,
            total_processing_time_ms=float(stats.total_time) if stats.total_time else 0.0
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching audit stats: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/audit/chatbot/{log_id}", response_model=AuditLogResponse)
async def get_chatbot_audit_log(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"]))
):
    """Get a specific audit log entry by ID (admin only)"""
    result = await db.execute(
        select(ChatbotAuditLog).where(ChatbotAuditLog.id == log_id)
    )
    log = result.scalar_one_or_none()
    
    if not log:
        raise HTTPException(status_code=404, detail="Audit log not found")
    
    return AuditLogResponse(
        id=log.id,
        user_id=log.user_id,
        historical_user_id=log.historical_user_id,
        username=log.username,
        query=log.query,
        response=log.response,
        success=log.success,
        error_message=log.error_message,
        processing_time_ms=log.processing_time_ms,
        session_id=log.session_id,
        created_at=iso_utc(log.created_at)
    )

