"""
Identity Audit Logging Utility
==============================
Comprehensive audit logging for all identity management operations.
All actions are logged to the database for forensic and accountability purposes.
"""

import os
import sys
import logging
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
from uuid import UUID

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_models import IdentityAuditLog, Identity, User
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

logger = logging.getLogger(__name__)


class IdentityAuditLogger:
    """
    Centralized audit logging for identity management operations.
    All operations are logged with full context for forensic analysis.
    """
    
    @staticmethod
    async def log_action(
        db: AsyncSession,
        user_id: int,
        username: str,
        action_type: str,
        identity_id: Optional[UUID] = None,
        related_identity_id: Optional[UUID] = None,
        action_details: Optional[Dict[str, Any]] = None,
        before_state: Optional[Dict[str, Any]] = None,
        after_state: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        success: bool = True,
        error_message: Optional[str] = None,
        notes: Optional[str] = None
    ) -> IdentityAuditLog:
        """
        Log an identity management action to the database.
        
        PRIMARY IDENTIFIERS (Required):
            - user_id: ID of user performing the action (REQUIRED)
            - username: Username performing the action (REQUIRED) - MAIN IDENTIFIER FOR ACCOUNTABILITY
        
        SUPPLEMENTARY INFORMATION (Optional):
            - ip_address: IP address of the requester (optional, for additional context only)
            - user_agent: User agent string (optional, for additional context only)
            NOTE: IP address should NEVER be used as the primary identifier. Username is the source of truth.
        
        Args:
            db: Database session
            user_id: ID of user performing the action (REQUIRED - PRIMARY IDENTIFIER)
            username: Username (REQUIRED - PRIMARY IDENTIFIER, denormalized for easier querying)
            action_type: Type of action (promote, merge, search, view, approve, reject, etc.)
            identity_id: Target identity ID (if applicable)
            related_identity_id: Related identity ID (for merges, etc.)
            action_details: Additional action-specific details (JSON)
            before_state: State before action (for tracking changes)
            after_state: State after action (for tracking changes)
            ip_address: IP address of the requester (OPTIONAL - supplementary only, not for identification)
            user_agent: User agent string (OPTIONAL - supplementary only)
            success: Whether action succeeded
            error_message: Error message if failed
            notes: Additional notes/justification
            
        Returns:
            Created audit log entry
        """
        try:
            audit_log = IdentityAuditLog(
                user_id=user_id,
                username=username,
                action_type=action_type,
                identity_id=identity_id,
                related_identity_id=related_identity_id,
                action_details=action_details,
                before_state=before_state,
                after_state=after_state,
                ip_address=ip_address,
                user_agent=user_agent,
                success=success,
                error_message=error_message,
                notes=notes,
                created_at=datetime.utcnow()
            )
            
            db.add(audit_log)
            try:
                await db.flush()
            except Exception as flush_error:
                # If flush fails (e.g., transaction aborted), try to rollback and continue
                try:
                    await db.rollback()
                except Exception:
                    pass  # Ignore rollback errors
                # Re-raise to let caller handle
                raise flush_error
            
            logger.debug(f"[AUDIT] Logged {action_type} action by USERNAME={username} (user_id={user_id}, identity_id={identity_id})")
            
            return audit_log
            
        except Exception as e:
            # Never fail the main operation due to audit logging errors
            logger.error(f"[AUDIT] Failed to log action: {e}", exc_info=True)
            # Try to rollback if transaction is in bad state
            try:
                await db.rollback()
            except Exception:
                pass  # Ignore rollback errors
            # Return None instead of raising to avoid breaking the main operation
            return None
    
    @staticmethod
    async def log_promote(
        db: AsyncSession,
        user_id: int,
        username: str,
        identity_id: UUID,
        display_name: str,
        before_state: Optional[Dict[str, Any]] = None,
        after_state: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        notes: Optional[str] = None
    ) -> IdentityAuditLog:
        """Log identity promotion action"""
        return await IdentityAuditLogger.log_action(
            db=db,
            user_id=user_id,
            username=username,
            action_type="promote",
            identity_id=identity_id,
            action_details={
                "display_name": display_name,
                "promotion_type": "unknown_to_known"
            },
            before_state=before_state,
            after_state=after_state,
            ip_address=ip_address,
            user_agent=user_agent,
            notes=notes,
            success=True
        )
    
    @staticmethod
    async def log_merge(
        db: AsyncSession,
        user_id: int,
        username: str,
        from_identity_id: UUID,
        to_identity_id: UUID,
        before_state: Optional[Dict[str, Any]] = None,
        after_state: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        notes: Optional[str] = None
    ) -> IdentityAuditLog:
        """Log identity merge action"""
        return await IdentityAuditLogger.log_action(
            db=db,
            user_id=user_id,
            username=username,
            action_type="merge",
            identity_id=to_identity_id,
            related_identity_id=from_identity_id,
            action_details={
                "merge_type": "manual",
                "from_identity_id": str(from_identity_id),
                "to_identity_id": str(to_identity_id)
            },
            before_state=before_state,
            after_state=after_state,
            ip_address=ip_address,
            user_agent=user_agent,
            notes=notes,
            success=True
        )
    
    @staticmethod
    async def log_search_by_image(
        db: AsyncSession,
        user_id: int,
        username: str,
        scope: str,
        results_count: int,
        search_results: Optional[List[Dict[str, Any]]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        processing_time_ms: Optional[float] = None
    ) -> IdentityAuditLog:
        """Log search by image action"""
        return await IdentityAuditLogger.log_action(
            db=db,
            user_id=user_id,
            username=username,
            action_type="search_by_image",
            action_details={
                "scope": scope,
                "results_count": results_count,
                "search_results": search_results[:5] if search_results else None,  # Store first 5 results
                "processing_time_ms": processing_time_ms
            },
            ip_address=ip_address,
            user_agent=user_agent,
            success=True
        )
    
    @staticmethod
    async def log_view_details(
        db: AsyncSession,
        user_id: int,
        username: str,
        identity_id: UUID,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> IdentityAuditLog:
        """Log identity details view action"""
        return await IdentityAuditLogger.log_action(
            db=db,
            user_id=user_id,
            username=username,
            action_type="view_details",
            identity_id=identity_id,
            ip_address=ip_address,
            user_agent=user_agent,
            success=True
        )
    
    @staticmethod
    async def log_approve_merge_suggestion(
        db: AsyncSession,
        user_id: int,
        username: str,
        suggestion_id: int,
        identity_ids: List[UUID],
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        notes: Optional[str] = None
    ) -> IdentityAuditLog:
        """Log merge suggestion approval"""
        return await IdentityAuditLogger.log_action(
            db=db,
            user_id=user_id,
            username=username,
            action_type="approve_merge_suggestion",
            action_details={
                "suggestion_id": suggestion_id,
                "identity_ids": [str(id) for id in identity_ids]
            },
            ip_address=ip_address,
            user_agent=user_agent,
            notes=notes,
            success=True
        )
    
    @staticmethod
    async def log_reject_merge_suggestion(
        db: AsyncSession,
        user_id: int,
        username: str,
        suggestion_id: int,
        identity_ids: List[UUID],
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        notes: Optional[str] = None
    ) -> IdentityAuditLog:
        """Log merge suggestion rejection"""
        return await IdentityAuditLogger.log_action(
            db=db,
            user_id=user_id,
            username=username,
            action_type="reject_merge_suggestion",
            action_details={
                "suggestion_id": suggestion_id,
                "identity_ids": [str(id) for id in identity_ids]
            },
            ip_address=ip_address,
            user_agent=user_agent,
            notes=notes,
            success=True
        )
    
    @staticmethod
    async def log_list_unknown(
        db: AsyncSession,
        user_id: int,
        username: str,
        filters: Optional[Dict[str, Any]] = None,
        results_count: int = 0,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> IdentityAuditLog:
        """Log unknown identities list view"""
        return await IdentityAuditLogger.log_action(
            db=db,
            user_id=user_id,
            username=username,
            action_type="list_unknown",
            action_details={
                "filters": filters,
                "results_count": results_count
            },
            ip_address=ip_address,
            user_agent=user_agent,
            success=True
        )
    
    @staticmethod
    async def log_error(
        db: AsyncSession,
        user_id: int,
        username: str,
        action_type: str,
        error_message: str,
        identity_id: Optional[UUID] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> IdentityAuditLog:
        """Log a failed action"""
        return await IdentityAuditLogger.log_action(
            db=db,
            user_id=user_id,
            username=username,
            action_type=action_type,
            identity_id=identity_id,
            ip_address=ip_address,
            user_agent=user_agent,
            success=False,
            error_message=error_message
        )


# Helper function to get client IP and user agent from FastAPI request
# NOTE: These are SUPPLEMENTARY only - username is the PRIMARY identifier
def get_client_info(request) -> tuple[Optional[str], Optional[str]]:
    """
    Extract IP address and user agent from FastAPI request.
    
    IMPORTANT: IP address is SUPPLEMENTARY information only.
    Username (from authentication) is the PRIMARY identifier for accountability.
    IP address should NEVER be used as the primary identifier.
    
    Args:
        request: FastAPI Request object
        
    Returns:
        Tuple of (ip_address, user_agent) - Both are optional/supplementary
    """
    try:
        # Get IP address (check for proxy headers)
        # NOTE: This is for supplementary context only, not identification
        ip_address = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or
            request.headers.get("X-Real-IP", "") or
            request.client.host if request.client else None
        )
        
        # Get user agent (supplementary context)
        user_agent = request.headers.get("User-Agent", "")
        
        return ip_address, user_agent
    except Exception:
        return None, None

