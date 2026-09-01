"""
User Management Service
=======================
Admin-only user management operations.
"""

import os
import sys
import asyncio
import logging
from datetime import datetime
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from db_models import User, UserPipelineAccess, Pipeline, ChatbotAuditLog
from backend.auth.password import hash_password

logger = logging.getLogger(__name__)


async def _hash_password_async(password: str) -> str:
    """bcrypt hashing off the event loop (~200-300ms of CPU)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, hash_password, password)


class UserService:
    """User management service"""

    @staticmethod
    async def create_user(
        username: str,
        email: str,
        password: str,
        full_name: Optional[str] = None,
        role: str = "user",
        can_use_chatbot: bool = False,
        pipeline_ids: Optional[List[str]] = None,
        db: AsyncSession = None
    ) -> User:
        """Create a new user"""
        logger.info(f"[CREATE_USER] 👤 Starting user creation for username: '{username}'")
        logger.debug(f"[CREATE_USER]   Email: {email}, Role: {role}, Full name: {full_name}")
        logger.debug(f"[CREATE_USER]   Password provided: {bool(password)}, Length: {len(password) if password else 0}")
        
        # Step 1: Check if username or email already exists
        logger.info(f"[CREATE_USER] Step 1: Checking if user already exists...")
        result = await db.execute(
            select(User).where(
                (User.username == username) | (User.email == email)
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            logger.error(f"[CREATE_USER] ❌ Step 1 FAILED: User with username '{username}' or email '{email}' already exists")
            raise ValueError(f"User with username '{username}' or email '{email}' already exists")
        logger.info(f"[CREATE_USER] ✅ Step 1 SUCCESS: Username and email are available")

        # Step 2: Hash password
        logger.info(f"[CREATE_USER] Step 2: Hashing password...")
        try:
            password_hash = await _hash_password_async(password)
            logger.info(f"[CREATE_USER] ✅ Step 2 SUCCESS: Password hashed")
            logger.debug(f"[CREATE_USER]   Password hash: {password_hash[:30]}...")
        except Exception as e:
            logger.error(f"[CREATE_USER] ❌ Step 2 FAILED: Password hashing error: {type(e).__name__}: {str(e)}", exc_info=True)
            raise ValueError(f"Failed to hash password: {str(e)}")

        # Step 3: Create user object
        logger.info(f"[CREATE_USER] Step 3: Creating user object...")
        user = User(
            username=username,
            email=email,
            password_hash=password_hash,
            full_name=full_name,
            role=role,
            can_use_chatbot=can_use_chatbot,
            is_active=True,
            # An admin typed this password, so the admin knows it. It is a
            # hand-over credential, not the user's own — the account cannot do
            # anything until the owner replaces it at first sign-in.
            must_change_password=True,
        )
        db.add(user)
        logger.info(f"[CREATE_USER] ✅ Step 3 SUCCESS: User object created and added to session")
        logger.debug(f"[CREATE_USER]   User data: username={user.username}, email={user.email}, role={user.role}, is_active={user.is_active}")

        # Step 4: Flush to get user ID
        logger.info(f"[CREATE_USER] Step 4: Flushing to get user ID...")
        try:
            await db.flush()
            logger.info(f"[CREATE_USER] ✅ Step 4 SUCCESS: User ID obtained: {user.id}")
        except Exception as e:
            logger.error(f"[CREATE_USER] ❌ Step 4 FAILED: Flush error: {type(e).__name__}: {str(e)}", exc_info=True)
            await db.rollback()
            raise

        # Step 5: Grant pipeline access
        if pipeline_ids:
            logger.info(f"[CREATE_USER] Step 5: Granting pipeline access...")
            for pipeline_id in pipeline_ids:
                # Verify pipeline exists
                pipeline_result = await db.execute(
                    select(Pipeline).where(Pipeline.pipeline_id == pipeline_id)
                )
                pipeline = pipeline_result.scalar_one_or_none()
                if pipeline:
                    access = UserPipelineAccess(
                        user_id=user.id,
                        pipeline_id=pipeline_id
                    )
                    db.add(access)
            logger.info(f"[CREATE_USER] ✅ Step 5 SUCCESS: Pipeline access granted ({len(pipeline_ids)} pipelines)")
        else:
            logger.debug(f"[CREATE_USER] Step 5: No pipeline access to grant")

        # Note: Commit will be handled by session context manager
        logger.info(f"[CREATE_USER]   Note: Commit will be handled by session context manager")
        
        # Step 6: Verify password works (before commit)
        logger.info(f"[CREATE_USER] Step 6: Verifying password hash...")
        try:
            from backend.auth.password import verify_password
            is_valid = verify_password(password, password_hash)
            if is_valid:
                logger.info(f"[CREATE_USER] ✅ Step 6 SUCCESS: Password hash verified - password will work for login")
            else:
                logger.error(f"[CREATE_USER] ❌ Step 6 FAILED: Password hash verification failed - CRITICAL ERROR!")
        except Exception as e:
            logger.warning(f"[CREATE_USER] ⚠️  Step 6 WARNING: Verification error (non-critical): {type(e).__name__}: {str(e)}")
        
        logger.info(f"[CREATE_USER] ✅✅✅ USER CREATION COMPLETE: {username} (ID: {user.id}, Email: {email}, Role: {role})")
        return user

    @staticmethod
    async def _current_pipeline_ids(user_id: int, db: AsyncSession) -> List[str]:
        """Pipeline ids currently granted to a user, sorted for stable diffing."""
        result = await db.execute(
            select(UserPipelineAccess.pipeline_id)
            .where(UserPipelineAccess.user_id == user_id)
        )
        return sorted(row[0] for row in result.all())

    @staticmethod
    def _invalidate_sql_agent_cache(user_id: int, reason: str) -> None:
        """Drop the user's cached SQL-agent instance after an authorization change.

        The agent caches the user's scope (pipelines, features, conversation
        memory), so leaving it in place would let a user keep querying through an
        agent built under permissions they no longer hold.

        Imported lazily: `sql_agent` imports from `backend`, so a module-level
        import here would close an import cycle. Best-effort by design — the
        authoritative guarantee is the permissions_version check in
        `_get_or_create_user_agent`, which rebuilds the agent on the next request
        whatever happens here. This must never be able to fail the write it
        accompanies.
        """
        try:
            from sql_agent.api.routes import invalidate_user_sql_agent
            invalidate_user_sql_agent(user_id, reason=reason)
        except Exception as exc:
            logger.debug(
                "[USER_SERVICE] SQL-agent cache invalidation skipped for user_id=%s: %s",
                user_id, type(exc).__name__,
            )

    @staticmethod
    def _record_authorization_audit(*, db: AsyncSession, user: User,
                                    old: dict, new: dict, action: str,
                                    actor=None, context: Optional[dict] = None) -> None:
        """Add an audit row to the CURRENT transaction.

        Deliberately synchronous and `db.add`-only: it must commit or roll back
        with the change it describes. Never records a password, token or cookie
        — only the authorization fields and request metadata.
        """
        from db_models import UserAuthorizationAuditLog

        context = context or {}
        db.add(UserAuthorizationAuditLog(
            target_user_id=user.id,
            target_username=user.username,
            changed_by_user_id=getattr(actor, "id", None),
            changed_by_username=getattr(actor, "username", None),
            action=action,
            old_role=old.get("role"),
            new_role=new.get("role"),
            old_can_use_chatbot=old.get("can_use_chatbot"),
            new_can_use_chatbot=new.get("can_use_chatbot"),
            old_is_active=old.get("is_active"),
            new_is_active=new.get("is_active"),
            old_pipeline_ids=old.get("pipeline_ids"),
            new_pipeline_ids=new.get("pipeline_ids"),
            permissions_version=user.permissions_version,
            request_id=context.get("request_id"),
            ip_address=context.get("ip_address"),
            user_agent=context.get("user_agent"),
            change_reason=context.get("reason"),
        ))

    @staticmethod
    async def update_user(
        user_id: int,
        email: Optional[str] = None,
        password: Optional[str] = None,  # Add password parameter
        full_name: Optional[str] = None,
        role: Optional[str] = None,
        can_use_chatbot: Optional[bool] = None,
        is_active: Optional[bool] = None,
        pipeline_ids: Optional[List[str]] = None,
        db: AsyncSession = None,
        actor: Optional[User] = None,
        context: Optional[dict] = None,
        force_rotation: bool = True,
    ) -> User:
        """Update user information.

        `force_rotation` applies only when a password is supplied: an
        admin-assigned password is a hand-over credential, so the owner is made
        to replace it at next sign-in. Callers pass False when the admin is
        changing their OWN password, which would otherwise lock them into
        changing it again immediately.

        `actor` and `context` are optional so existing callers keep working,
        but the route passes both so the audit row records who made the change
        and from where.
        """
        logger.info(f"[UPDATE_USER] ✏️  Starting user update for user ID: {user_id}")
        
        # Step 1: Find user
        logger.debug(f"[UPDATE_USER] Step 1: Querying database for user ID {user_id}...")
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            logger.error(f"[UPDATE_USER] ❌ Step 1 FAILED: User with ID {user_id} not found")
            raise ValueError(f"User with ID {user_id} not found")
        
        logger.info(f"[UPDATE_USER] ✅ Step 1 SUCCESS: User found - {user.username} (Email: {user.email}, Role: {user.role})")

        # Step 2: Update email if provided
        if email is not None:
            logger.info(f"[UPDATE_USER] Step 2: Updating email...")
            # Check if email is already taken by another user
            result = await db.execute(
                select(User).where(User.email == email, User.id != user_id)
            )
            if result.scalar_one_or_none():
                logger.error(f"[UPDATE_USER] ❌ Step 2 FAILED: Email '{email}' is already taken")
                raise ValueError(f"Email '{email}' is already taken")
            user.email = email
            logger.info(f"[UPDATE_USER] ✅ Step 2 SUCCESS: Email updated to {email}")

        # Step 3: Update password if provided
        if password is not None and password.strip() != "":
            logger.info(f"[UPDATE_USER] Step 3: Updating password...")
            logger.debug(f"[UPDATE_USER]   New password length: {len(password)}")
            try:
                new_password_hash = await _hash_password_async(password)
                old_hash = user.password_hash
                user.password_hash = new_password_hash
                # Stamped on every password write: it is what invalidates the
                # user's other live sessions (see the freshness check in
                # backend/auth/auth_service.py).
                user.password_changed_at = datetime.utcnow()
                user.must_change_password = force_rotation
                logger.info(f"[UPDATE_USER] ✅ Step 3 SUCCESS: Password hashed and updated "
                            f"(rotation_required={force_rotation})")
                logger.debug(f"[UPDATE_USER]   Old hash: {old_hash[:30] if old_hash else 'None'}...")
                logger.debug(f"[UPDATE_USER]   New hash: {user.password_hash[:30]}...")
            except Exception as e:
                logger.error(f"[UPDATE_USER] ❌ Step 3 FAILED: Password hashing error: {type(e).__name__}: {str(e)}", exc_info=True)
                raise ValueError(f"Failed to hash password: {str(e)}")
        elif password is not None and password.strip() == "":
            logger.debug(f"[UPDATE_USER] Step 3: Password field provided but empty - keeping current password")

        # Step 4: Update other fields
        #
        # Prior authorization state is captured BEFORE any mutation — the
        # attributes are overwritten in place below, so reading them afterwards
        # would record the new value as the old one.
        from backend.auth.capabilities import canonical_role

        old_authz = {
            "role": user.role,
            "can_use_chatbot": user.can_use_chatbot,
            "is_active": user.is_active,
            "pipeline_ids": await UserService._current_pipeline_ids(user_id, db),
        }

        logger.info(f"[UPDATE_USER] Step 4: Updating other fields...")
        if full_name is not None:
            user.full_name = full_name
            logger.debug(f"[UPDATE_USER]   Full name updated: {full_name}")
        if role is not None:
            # Stored canonically so role comparisons cannot miss on casing.
            user.role = canonical_role(role).value
            logger.debug(f"[UPDATE_USER]   Role updated: {user.role}")
        if can_use_chatbot is not None:
            user.can_use_chatbot = can_use_chatbot
            logger.debug(f"[UPDATE_USER]   Can use chatbot updated: {can_use_chatbot}")
        if is_active is not None:
            user.is_active = is_active
            logger.debug(f"[UPDATE_USER]   Is active updated: {is_active}")
            # Reactivating clears the block markers. Without this, an admin who
            # re-enables a blocked user through the edit form leaves
            # blocked_reason/blocked_at set, and the users table keeps showing
            # a BLOCKED badge for an active account (the badge is derived from
            # those columns, not from is_active).
            #
            # The reverse is deliberately NOT done: un-ticking "active" in the
            # ordinary edit form is a deactivation, not a block. blocked_at
            # stays backend-controlled and is set only by block_user(), which
            # records a reason — see block_user/unblock_user.
            if is_active and (user.blocked_reason or user.blocked_at):
                logger.info(
                    "[UPDATE_USER] user_id=%s reactivated - clearing stale block markers",
                    user.id,
                )
                user.blocked_reason = None
                user.blocked_at = None
        logger.info(f"[UPDATE_USER] ✅ Step 4 SUCCESS: Other fields updated")

        # Step 5: Update pipeline access
        if pipeline_ids is not None:
            logger.info(f"[UPDATE_USER] Step 5: Updating pipeline access...")
            # Remove existing access
            await db.execute(
                delete(UserPipelineAccess).where(UserPipelineAccess.user_id == user_id)
            )
            # Add new access. Unknown pipeline ids are rejected rather than
            # silently dropped: an admin who mistypes an id should be told, not
            # left believing access was granted.
            unknown = []
            for pipeline_id in pipeline_ids:
                pipeline_result = await db.execute(
                    select(Pipeline).where(Pipeline.pipeline_id == pipeline_id)
                )
                pipeline = pipeline_result.scalar_one_or_none()
                if pipeline:
                    access = UserPipelineAccess(
                        user_id=user_id,
                        pipeline_id=pipeline_id
                    )
                    db.add(access)
                else:
                    unknown.append(pipeline_id)
            if unknown:
                await db.rollback()
                raise ValueError(
                    f"Unknown pipeline id(s): {', '.join(sorted(unknown))}"
                )
            logger.info(f"[UPDATE_USER] ✅ Step 5 SUCCESS: Pipeline access updated ({len(pipeline_ids)} pipelines)")

        # Step 5b: Authorization version + audit, in THIS transaction.
        #
        # The version bump is what lets an already-open WebSocket or SSE stream
        # notice the change; the audit row is what makes the change
        # reconstructable afterwards. Both must share the transaction with the
        # field writes, or a rollback can leave them disagreeing.
        new_authz = {
            "role": user.role,
            "can_use_chatbot": user.can_use_chatbot,
            "is_active": user.is_active,
            "pipeline_ids": sorted(pipeline_ids) if pipeline_ids is not None
                            else old_authz["pipeline_ids"],
        }
        if new_authz != old_authz:
            user.permissions_version = int(user.permissions_version or 1) + 1
            logger.info(
                "[UPDATE_USER] authorization changed -> permissions_version=%s",
                user.permissions_version,
            )
            UserService._record_authorization_audit(
                db=db, user=user, old=old_authz, new=new_authz,
                action="authorization_updated", actor=actor, context=context,
            )
            UserService._invalidate_sql_agent_cache(user.id, "authorization_updated")
        else:
            logger.debug("[UPDATE_USER] no authorization change; version unchanged")

        # Step 6: Ensure user is tracked by session and flush
        logger.info(f"[UPDATE_USER] Step 6: Ensuring user is tracked by session...")
        db.add(user)  # Ensure object is tracked
        logger.debug(f"[UPDATE_USER]   User object added to session (or already tracked)")
        
        logger.info(f"[UPDATE_USER] Step 7: Committing changes to database...")
        try:
            # COMMIT HERE, not in the session dependency's cleanup.
            #
            # This is the reported bug. FastAPI runs dependency cleanup AFTER the
            # response has been sent, so leaving the commit to the session
            # context manager meant PUT /api/users/{id} returned 200 while its
            # transaction was still open. The admin UI reported success, and a
            # read issued immediately afterwards — exactly what the UI does when
            # it refreshes the user list — could beat the commit and show the old
            # role. Worse, a commit that failed after the response was sent left
            # the administrator told the change had been saved when it had not.
            #
            # Reproduced live before the fix: two sequential role changes, each
            # returning 200 with the new role in the body, while the database
            # still held the previous value.
            #
            # block_user and unblock_user already committed explicitly; this path
            # was the odd one out. The version bump and the audit row are written
            # in the SAME transaction, so an audit row cannot survive a rolled
            # back change, nor a change happen without its audit row.
            await db.commit()
            await db.refresh(user)
            logger.info(f"[UPDATE_USER] ✅ Step 7 SUCCESS: Changes committed")
        except Exception as e:
            logger.error(f"[UPDATE_USER] ❌ Step 7 FAILED: Commit error: {type(e).__name__}: {str(e)}", exc_info=True)
            await db.rollback()
            raise

        logger.info(f"[UPDATE_USER] ✅✅✅ USER UPDATE COMPLETE for user: {user.username} (ID: {user.id})")
        return user

    @staticmethod
    async def reset_password(user_id: int, new_password: str, db: AsyncSession,
                             force_rotation: bool = True) -> User:
        """Reset user password (admin only).

        `force_rotation` defaults to True because an admin-chosen password is
        known to the admin: the owner must replace it at next sign-in. Callers
        pass False when an admin resets their own password, which would
        otherwise force them straight into a change-password screen.
        """
        logger.info(f"[PASSWORD_RESET] 🔐 Starting password reset for user ID: {user_id}")
        
        # Step 1: Find user
        logger.debug(f"[PASSWORD_RESET] Step 1: Querying database for user ID {user_id}...")
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        
        if not user:
            logger.error(f"[PASSWORD_RESET] ❌ Step 1 FAILED: User with ID {user_id} not found")
            raise ValueError(f"User with ID {user_id} not found")
        
        logger.info(f"[PASSWORD_RESET] ✅ Step 1 SUCCESS: User found - {user.username} (Email: {user.email}, Role: {user.role})")
        logger.debug(f"[PASSWORD_RESET]   Current password hash: {user.password_hash[:30] if user.password_hash else 'None'}...")
        
        # Step 2: Hash new password
        logger.info(f"[PASSWORD_RESET] Step 2: Hashing new password...")
        logger.debug(f"[PASSWORD_RESET]   New password length: {len(new_password)}")
        
        try:
            new_password_hash = await _hash_password_async(new_password)
            logger.info(f"[PASSWORD_RESET] ✅ Step 2 SUCCESS: Password hashed")
            logger.debug(f"[PASSWORD_RESET]   New password hash: {new_password_hash[:30]}...")
        except Exception as e:
            logger.error(f"[PASSWORD_RESET] ❌ Step 2 FAILED: Password hashing error: {type(e).__name__}: {str(e)}", exc_info=True)
            raise
        
        # Step 3: Update user object
        logger.info(f"[PASSWORD_RESET] Step 3: Updating user object in session...")
        old_hash = user.password_hash
        user.password_hash = new_password_hash
        # Stamped on every password write: it is what invalidates this user's
        # other live sessions (see backend/auth/auth_service.py). A reset is
        # often a response to a compromise, so ending those sessions matters.
        user.password_changed_at = datetime.utcnow()
        user.must_change_password = force_rotation
        logger.debug(f"[PASSWORD_RESET]   Old hash: {old_hash[:30] if old_hash else 'None'}...")
        logger.debug(f"[PASSWORD_RESET]   New hash: {user.password_hash[:30]}...")
        logger.info(f"[PASSWORD_RESET]   rotation_required={force_rotation}")
        
        # Step 4: Ensure user is attached to session and flush changes
        logger.info(f"[PASSWORD_RESET] Step 4: Ensuring user is tracked by session...")
        # The user is already attached to the session from the query, but let's make sure
        # SQLAlchemy tracks the change by accessing the object
        db.add(user)  # This ensures the object is tracked (idempotent if already tracked)
        logger.debug(f"[PASSWORD_RESET]   User object added to session (or already tracked)")
        
        # Flush to ensure changes are sent to database (but don't commit yet - let context manager do it)
        logger.info(f"[PASSWORD_RESET] Step 5: Flushing changes to database...")
        try:
            await db.flush()
            logger.info(f"[PASSWORD_RESET] ✅ Step 5 SUCCESS: Changes flushed to database")
            logger.debug(f"[PASSWORD_RESET]   Password hash after flush: {user.password_hash[:30]}...")
        except Exception as e:
            logger.error(f"[PASSWORD_RESET] ❌ Step 5 FAILED: Flush error: {type(e).__name__}: {str(e)}", exc_info=True)
            await db.rollback()
            raise
        
        # Note: We don't commit here - the session context manager will commit when the route completes
        # This ensures proper transaction handling and rollback on errors
        logger.info(f"[PASSWORD_RESET]   Note: Commit will be handled by session context manager")
        
        # Step 7: Verify the new password works
        logger.info(f"[PASSWORD_RESET] Step 7: Verifying new password...")
        try:
            from backend.auth.password import verify_password
            is_valid = verify_password(new_password, user.password_hash)
            if is_valid:
                logger.info(f"[PASSWORD_RESET] ✅ Step 7 SUCCESS: New password verified successfully")
            else:
                logger.error(f"[PASSWORD_RESET] ❌ Step 7 FAILED: New password verification failed - this is a critical error!")
        except Exception as e:
            logger.warning(f"[PASSWORD_RESET] ⚠️  Step 7 WARNING: Verification error (non-critical): {type(e).__name__}: {str(e)}")
        
        logger.info(f"[PASSWORD_RESET] ✅✅✅ PASSWORD RESET COMPLETE for user: {user.username} (ID: {user.id})")
        return user

    @staticmethod
    async def get_all_users(db: AsyncSession) -> List[User]:
        """Get all users"""
        result = await db.execute(select(User).order_by(User.created_at.desc()))
        return list(result.scalars().all())

    @staticmethod
    async def get_user_by_id(user_id: int, db: AsyncSession) -> Optional[User]:
        """Get user by ID"""
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def delete_user(user_id: int, db: AsyncSession,
                          actor=None,
                          reassign_admin_to: "Optional[int]" = None) -> bool:
        """Permanently delete a HUMAN user, preserving history.

        One transaction, one commit; any failure rolls back everything —
        no partially promoted successors, no tombstone without a deletion,
        no deletion without its audit row.

        What happens, in order:
          1. refuse the `system` principal and the last platform administrator;
          2. workspaces where this user is the only admin need a successor
             (member of every affected workspace) promoted IN this transaction;
          3. preserved rows get their historical_* attribution stamped BEFORE
             the FKs null — after the DELETE the numeric id is unrecoverable;
          4. tombstone + `user_deleted` audit row;
          5. DELETE FROM users — PostgreSQL cascades the account-bound tables
             (workspace_members, sessions, memory, feedback, pipeline access,
             pending enrollments) and SET-NULLs the preserved ones. The ORM
             relationships are passive (`passive_deletes="all"`), so SQLAlchemy
             never emits the `UPDATE ... SET user_id = NULL` that used to make
             deletion impossible;
          6. only AFTER commit: evict the user's cached SQL agent. A rolled-back
             deletion must not evict a live user's agent.
        """
        from sqlalchemy import text as sa_text

        from backend.services.system_principal import is_system_principal, SYSTEM_USERNAME
        from backend.services.user_policy import (
            ensure_not_last_platform_administrator,
            validate_workspace_successor,
            workspaces_needing_admin_successor,
        )
        from db_models import DeletedUser, WorkspaceMember

        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            return False

        # Defence in depth: the route guards this too, but a guard that lives
        # only in the HTTP layer protects only the HTTP layer.
        if is_system_principal(user):
            logger.warning(
                "[DELETE_USER] ❌ Refused deletion of the protected '%s' principal (ID: %s)",
                SYSTEM_USERNAME, user_id)
            raise ValueError(
                f"The '{SYSTEM_USERNAME}' account is a protected audit principal "
                f"and cannot be deleted.")

        # Raises LastAdministratorError / WorkspaceSuccessorRequired — domain
        # exceptions the route translates to 409. Deliberately NOT caught by
        # the blanket handler below: a policy refusal is not a failure.
        await ensure_not_last_platform_administrator(
            db, target_user_id=user_id, deleting=True)
        affected = await workspaces_needing_admin_successor(db, user_id=user_id)
        await validate_workspace_successor(
            db, affected=affected, successor_id=reassign_admin_to,
            deleting_user_id=user_id)

        username = user.username
        old_authz = {
            "role": user.role,
            "can_use_chatbot": user.can_use_chatbot,
            "is_active": user.is_active,
            "pipeline_ids": await UserService._current_pipeline_ids(user_id, db),
        }

        try:
            # Successor promotion, same transaction as the deletion.
            if affected and reassign_admin_to is not None:
                for workspace in affected:
                    await db.execute(sa_text("""
                        UPDATE workspace_members SET role = 'admin'
                        WHERE workspace_id = CAST(:w AS uuid) AND user_id = :s
                    """), {"w": workspace["id"], "s": reassign_admin_to})
                logger.info(
                    "[DELETE_USER] promoted user_id=%s to admin of %d workspace(s) "
                    "vacated by user_id=%s",
                    reassign_admin_to, len(affected), user_id)

            # Stamp historical attribution while the id still exists. The
            # migration backfilled existing rows; this covers rows written
            # since, and author_username for this user's conversations.
            for table, hist_col, src_col in (
                ("conversations", "historical_user_id", "user_id"),
                ("user_query_history", "historical_user_id", "user_id"),
                ("search_history", "historical_user_id", "user_id"),
                ("chatbot_audit_log", "historical_user_id", "user_id"),
                ("identity_audit_log", "historical_user_id", "user_id"),
                ("identity_merges", "historical_merged_by", "merged_by"),
                ("live_search_alerts", "historical_created_by", "created_by"),
            ):
                await db.execute(sa_text(
                    f"UPDATE {table} SET {hist_col} = {src_col} "
                    f"WHERE {src_col} = :u AND {hist_col} IS NULL"), {"u": user_id})
            await db.execute(sa_text("""
                UPDATE conversations SET author_username = :n
                WHERE user_id = :u AND author_username IS NULL
            """), {"n": username, "u": user_id})

            # Tombstone: the id -> username record every historical_* resolves
            # against. Plain INSERT is safe: it commits atomically with the
            # DELETE, ids are never reused, and a failed attempt rolls the
            # tombstone back with everything else.
            db.add(DeletedUser(
                user_id=user_id,
                username=username,
                deleted_by_user_id=getattr(actor, "id", None),
                deleted_by_username=getattr(actor, "username", None),
            ))

            # The audit row commits or rolls back WITH the deletion.
            UserService._record_authorization_audit(
                db=db, user=user, old=old_authz,
                new={"role": None, "can_use_chatbot": False,
                     "is_active": False, "pipeline_ids": []},
                action="user_deleted", actor=actor, context=None,
            )

            # The DELETE. PostgreSQL owns everything downstream.
            await db.delete(user)
            await db.commit()
            logger.info(f"Deleted user: {username} (ID: {user_id})")
        except Exception as e:
            await db.rollback()
            logger.error(f"Error deleting user {user_id}: {str(e)}", exc_info=True)
            raise ValueError(f"Failed to delete user: {str(e)}")

        # After commit only.
        UserService._invalidate_sql_agent_cache(user_id, "user_deleted")
        return True

    @staticmethod
    async def block_user(
        user_id: int,
        reason: str,
        db: AsyncSession
    ) -> User:
        """Block a user from using the system (sets is_active=False, can_use_chatbot=False)"""
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise ValueError(f"User with ID {user_id} not found")

        old_authz = {
            "role": user.role,
            "can_use_chatbot": user.can_use_chatbot,
            "is_active": user.is_active,
            "pipeline_ids": await UserService._current_pipeline_ids(user_id, db),
        }

        user.is_active = False
        user.can_use_chatbot = False
        user.blocked_reason = reason
        user.blocked_at = datetime.utcnow()
        # Bumping the version is what closes the blocked user's OPEN
        # connections. Without it, the SQL-agent WebSocket that triggered the
        # block keeps working — the socket was authorized at handshake and
        # never re-checked.
        user.permissions_version = int(user.permissions_version or 1) + 1

        UserService._record_authorization_audit(
            db=db, user=user, old=old_authz,
            new={"role": user.role, "can_use_chatbot": False,
                 "is_active": False, "pipeline_ids": old_authz["pipeline_ids"]},
            action="user_blocked", actor=None, context={"reason": reason},
        )

        await db.commit()
        await db.refresh(user)
        # After commit: a rolled-back block must not evict a still-valid agent.
        UserService._invalidate_sql_agent_cache(user.id, "user_blocked")
        logger.warning(f"Blocked user: {user.username} - Reason: {reason}")
        return user

    @staticmethod
    async def unblock_user(
        user_id: int,
        db: AsyncSession
    ) -> User:
        """Unblock a user (admin only) - restores access"""
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise ValueError(f"User with ID {user_id} not found")

        old_authz = {
            "role": user.role,
            "can_use_chatbot": user.can_use_chatbot,
            "is_active": user.is_active,
            "pipeline_ids": await UserService._current_pipeline_ids(user_id, db),
        }

        user.is_active = True
        user.blocked_reason = None
        user.blocked_at = None
        # Note: can_use_chatbot is not automatically restored - admin must set it explicitly
        user.permissions_version = int(user.permissions_version or 1) + 1

        UserService._record_authorization_audit(
            db=db, user=user, old=old_authz,
            new={"role": user.role, "can_use_chatbot": user.can_use_chatbot,
                 "is_active": True, "pipeline_ids": old_authz["pipeline_ids"]},
            action="user_unblocked", actor=None, context=None,
        )

        await db.commit()
        await db.refresh(user)
        UserService._invalidate_sql_agent_cache(user.id, "user_unblocked")
        logger.info(f"Unblocked user: {user.username}")
        return user

