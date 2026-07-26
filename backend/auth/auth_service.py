"""
Authentication Service
=====================
Handles user authentication, JWT tokens, and authorization.
"""

import os
import sys
import jwt
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from db_connection import get_db
from db_models import User, UserPipelineAccess
from backend.auth.password import hash_password, verify_password

logger = logging.getLogger(__name__)

# JWT Configuration - Load from central config
SECRET_KEY = settings.JWT_SECRET_KEY
ALGORITHM = settings.JWT_ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES

# Make security optional to allow cookie-based authentication (auto_error=False)
security = HTTPBearer(auto_error=False)


class AuthService:
    """Authentication and authorization service"""

    @staticmethod
    def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
        """Create a JWT access token.

        Every token carries a unique `jti` (enabling revocation on logout and
        acting as session rotation — a new login never reuses a previous
        session identifier), plus `iat`, `iss` and `aud` so tokens minted for
        another issuer/audience are rejected.

        Token payloads are NEVER logged.
        """
        try:
            to_encode = data.copy()
            now = datetime.utcnow()
            expire = now + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))

            to_encode.update({
                "exp": expire,
                "iat": now,
                "jti": uuid.uuid4().hex,
                "iss": getattr(settings, "JWT_ISSUER", "face-recognition-service"),
                "aud": getattr(settings, "JWT_AUDIENCE", "face-recognition-api"),
            })

            encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
            logger.debug("[TOKEN] Access token created")
            return encoded_jwt
        except Exception as e:
            logger.error(f"[TOKEN] Token creation error: {type(e).__name__}", exc_info=True)
            raise

    @staticmethod
    def decode_token(token: str, verify_revocation: bool = True) -> Optional[dict]:
        """Decode and verify a JWT token.

        Tokens issued before issuer/audience claims existed are still accepted
        (claims are verified only when present) so an in-flight deployment does
        not log every user out.

        `verify_revocation` is checked by the async caller — this method stays
        synchronous for backward compatibility.
        """
        if not token:
            return None

        try:
            token = token.strip()
            unverified_claims = {}
            try:
                unverified_claims = jwt.decode(token, options={"verify_signature": False}) or {}
            except Exception:
                pass

            decode_kwargs = {"algorithms": [ALGORITHM]}
            if unverified_claims.get("aud"):
                decode_kwargs["audience"] = getattr(settings, "JWT_AUDIENCE", "face-recognition-api")
            if unverified_claims.get("iss"):
                decode_kwargs["issuer"] = getattr(settings, "JWT_ISSUER", "face-recognition-service")

            payload = jwt.decode(token, SECRET_KEY, **decode_kwargs)
            return payload
        except jwt.ExpiredSignatureError:
            logger.debug("[TOKEN] Rejected: expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.debug("[TOKEN] Rejected: %s", type(e).__name__)
            return None
        except Exception as e:
            logger.error("[TOKEN] Decode error: %s", type(e).__name__)
            return None

    @staticmethod
    async def authenticate_user_detailed(username: str, password: str,
                                         db: AsyncSession):
        """Authenticate and report WHY it failed (for internal use only).

        Returns (user_or_None, reason). Reasons: `ok`, `unknown_user`,
        `account_disabled`, `bad_password`, `error`.

        The reason is **never** surfaced to the client — every failure yields
        the same generic message. It exists so the caller knows whether a
        bcrypt verification actually ran: paths that skip bcrypt (unknown
        user, disabled account) must be timing-equalized, and paths that ran
        it must NOT be, or the equalization itself becomes a timing oracle.

        Logs carry pseudonymized identifiers only — never usernames, emails,
        password hashes or credentials.
        """
        from backend.auth.auth_security import pseudonymize
        actor = pseudonymize(username)

        try:
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()

            if not user:
                logger.debug("[AUTH] Authentication failed actor=%s reason=unknown_user", actor)
                return None, "unknown_user"

            if not user.is_active:
                # Disabled/blocked accounts fail identically to bad credentials
                logger.info("[AUTH] Authentication denied user_id=%s reason=account_disabled", user.id)
                return None, "account_disabled"

            # bcrypt is ~200-300ms of pure CPU — run it off the event loop
            import asyncio
            loop = asyncio.get_running_loop()
            password_valid = await loop.run_in_executor(
                None, verify_password, password, user.password_hash
            )

            if not password_valid:
                logger.debug("[AUTH] Authentication failed user_id=%s reason=bad_password", user.id)
                return None, "bad_password"

            user.last_login = datetime.utcnow()
            await db.commit()
            logger.debug("[AUTH] Authentication succeeded user_id=%s", user.id)
            return user, "ok"

        except Exception as e:
            logger.error("[AUTH] Authentication error actor=%s error=%s",
                         actor, type(e).__name__, exc_info=True)
            return None, "error"

    @staticmethod
    async def authenticate_user(username: str, password: str, db: AsyncSession) -> Optional[User]:
        """Authenticate a user by username and password.

        Backward-compatible wrapper: returns the user or None with no reason.
        """
        user, _reason = await AuthService.authenticate_user_detailed(username, password, db)
        return user

    @staticmethod
    async def get_user_by_id(user_id: int, db: AsyncSession) -> Optional[User]:
        """Get user by ID"""
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_user_pipelines(user_id: int, db: AsyncSession) -> List[str]:
        """
        Get list of pipeline IDs the user can access.
        
        This is the MAIN function that determines if a user can access unknown faces.
        Access is granted through the user_pipeline_access table, which is managed
        via the Admin → Users page when assigning pipelines to users.
        
        Returns:
            List[str]: List of pipeline IDs the user can access (empty list if none)
        """
        logger.debug(f"[GET_USER_PIPELINES] Fetching pipelines for user_id: {user_id}")
        result = await db.execute(
            select(UserPipelineAccess.pipeline_id)
            .where(UserPipelineAccess.user_id == user_id)
        )
        pipelines = [row[0] for row in result.all()]
        logger.info(f"[GET_USER_PIPELINES] User {user_id} has access to {len(pipelines)} pipelines: {pipelines}")
        return pipelines


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> User:
    """
    Get the current authenticated user from JWT token.
    
    Checks both:
    1. Authorization header (Bearer token) - for API calls
    2. HttpOnly cookie (access_token) - for browser-based authentication
    """
    token = None

    # NOTE: cookie *values* must never be logged — they are the credential.
    # Only the presence of a token is ever recorded.

    # First, try to get token from Authorization header
    if credentials and credentials.credentials:
        token = credentials.credentials

    # If no token in header, try the HttpOnly cookie (either name)
    if not token:
        token = request.cookies.get("__Host-access_token") or request.cookies.get("access_token")

    # If still no token, authentication failed
    if not token:
        logger.debug("[AUTH] No authentication token present path=%s method=%s",
                     request.url.path, request.method)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Decode and verify token
    payload = AuthService.decode_token(token)

    if payload is None:
        logger.debug("[AUTH] Token rejected (invalid or expired)")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Revoked (logged-out) tokens must not authenticate anyone
    try:
        from backend.auth.auth_security import is_token_revoked
        if await is_token_revoked(payload.get("jti")):
            logger.debug("[AUTH] Token rejected (revoked)")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has ended. Please log in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("[AUTH] Revocation check unavailable: %s", type(e).__name__)
    
    # JWT 'sub' claim is a string, convert to int for user lookup
    user_id_str = payload.get("sub")
    if user_id_str is None:
        logger.warning("[AUTH] Token missing 'sub' claim")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )
    
    try:
        user_id = int(user_id_str)
    except (ValueError, TypeError):
        logger.warning(f"[AUTH] Invalid user ID in token: {user_id_str}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )
    
    user = await AuthService.get_user_by_id(user_id, db)
    if user is None:
        logger.warning(f"[AUTH] User not found for ID: {user_id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    
    if not user.is_active:
        logger.warning(f"[AUTH] User {user.username} (ID: {user_id}) is inactive")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive",
        )
    
    logger.debug(f"[AUTH] ✅ User authenticated: {user.username} (ID: {user.id}, Role: {user.role})")
    return user


def require_role(allowed_roles: List[str]):
    """Dependency to require specific role(s)"""
    async def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required role: {', '.join(allowed_roles)}"
            )
        return current_user
    return role_checker


def require_admin():
    """Dependency factory to require admin role. Returns user dict for compatibility."""
    async def admin_checker(current_user: User = Depends(get_current_user)) -> dict:
        if current_user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied. Admin role required."
            )
        # Return as dict for compatibility with routes expecting dict
        return {
            "id": current_user.id,
            "username": current_user.username,
            "email": current_user.email,
            "role": current_user.role,
            "is_active": current_user.is_active
        }
    return admin_checker


def require_pipeline_access(pipeline_id: str):
    """Dependency to require access to a specific pipeline"""
    async def pipeline_checker(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
    ) -> User:
        # Admin has access to all pipelines
        if current_user.role == "admin":
            return current_user
        
        # Check if user has access to this pipeline
        pipelines = await AuthService.get_user_pipelines(current_user.id, db)
        if pipeline_id not in pipelines:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to pipeline: {pipeline_id}"
            )
        return current_user
    return pipeline_checker


async def check_identity_access(
    identity_id: str,
    current_user: User,
    db: AsyncSession
) -> bool:
    """
    Check if user has access to an identity based on its pipeline_ids.
    Returns True if user has access, False otherwise.
    Admin always has access.
    """
    from db_models import Identity, IdentityAppearance, IdentityEmbedding, Face, Detection
    from sqlalchemy import select
    
    # Admin has access to all identities
    if current_user.role == "admin":
        return True
    
    # Get user's accessible pipelines
    user_pipelines = await AuthService.get_user_pipelines(current_user.id, db)
    if not user_pipelines:
        return False
    
    # NOTE: We use identity_id as a string directly - SQLAlchemy handles UUID conversion automatically
    # PostgreSQL UUID columns accept string comparisons - no need to convert to UUID object
    # This avoids import dependencies and is simpler
    
    # Get identity's pipeline IDs from multiple sources
    pipeline_ids_set = set()
    
    # 1. From IdentityAppearance
    appearance_result = await db.execute(
        select(IdentityAppearance.pipeline_id).where(
            IdentityAppearance.identity_id == identity_id
        ).distinct()
    )
    for row in appearance_result:
        if row[0]:
            pipeline_ids_set.add(row[0])
    
    # 2. From IdentityEmbedding (fallback if no appearances)
    if not pipeline_ids_set:
        embedding_result = await db.execute(
            select(IdentityEmbedding.pipeline_id).where(
                IdentityEmbedding.identity_id == identity_id
            ).distinct()
        )
        for row in embedding_result:
            if row[0]:
                pipeline_ids_set.add(row[0])
    
    # 3. From Face->Detection (fallback if no embeddings)
    if not pipeline_ids_set:
        face_result = await db.execute(
            select(Detection.pipeline_id).join(
                Face, Face.detection_id == Detection.id
            ).where(
                Face.identity_id == identity_id
            ).distinct()
        )
        for row in face_result:
            if row[0]:
                pipeline_ids_set.add(row[0])
    
    # Check if any of the identity's pipelines are in user's accessible pipelines
    return bool(pipeline_ids_set & set(user_pipelines))


def require_chatbot_access():
    """Dependency to require chatbot access"""
    async def chatbot_checker(current_user: User = Depends(get_current_user)) -> User:
        if not current_user.can_use_chatbot:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Chatbot access denied"
            )
        return current_user
    return chatbot_checker


def require_unknown_faces_access():
    """
    Dependency factory to require Unknown Faces access.
    Allows admin users OR regular users with pipeline access.
    Returns user dict for compatibility.
    """
    async def unknown_faces_checker(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
    ) -> dict:
        # Admin always has access
        if current_user.role == "admin":
            return {
                "id": current_user.id,
                "username": current_user.username,
                "email": current_user.email,
                "role": current_user.role,
                "is_active": current_user.is_active
            }
        
        # Regular users: check if they have pipeline access
        user_pipelines = await AuthService.get_user_pipelines(current_user.id, db)
        if not user_pipelines:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied. You need pipeline access to use this feature. Contact an administrator."
            )
        
        # User has pipeline access - allow
        return {
            "id": current_user.id,
            "username": current_user.username,
            "email": current_user.email,
            "role": current_user.role,
            "is_active": current_user.is_active
        }
    return unknown_faces_checker


def require_strict_access(allowed_roles: Optional[List[str]] = None, require_pipeline_access: bool = False):
    """
    STRICT AUTHORIZATION LAYER - Validates user access based on admin-defined permissions.
    
    This is the STRICTEST authorization check - users can ONLY access what admin has explicitly allowed.
    
    Args:
        allowed_roles: List of roles allowed (e.g., ["admin"]). If None, checks pipeline access.
        require_pipeline_access: If True, regular users MUST have at least one pipeline assigned.
    
    Returns:
        User object if authorized, raises HTTPException if not.
    """
    async def strict_checker(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
    ) -> User:
        # Step 1: Check if user is active
        if not current_user.is_active:
            logger.warning(f"[STRICT_AUTH] User {current_user.username} (ID: {current_user.id}) is inactive - ACCESS DENIED")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied. Your account is inactive. Contact an administrator."
            )
        
        # Step 2: If role-based access is specified, check role
        if allowed_roles:
            if current_user.role not in allowed_roles:
                logger.warning(f"[STRICT_AUTH] User {current_user.username} (ID: {current_user.id}, Role: {current_user.role}) attempted access requiring roles {allowed_roles} - ACCESS DENIED")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied. Required role: {', '.join(allowed_roles)}"
                )
        
        # Step 3: If pipeline access is required for non-admins, verify they have it
        if require_pipeline_access and current_user.role != "admin":
            user_pipelines = await AuthService.get_user_pipelines(current_user.id, db)
            if not user_pipelines or len(user_pipelines) == 0:
                logger.warning(f"[STRICT_AUTH] User {current_user.username} (ID: {current_user.id}) has no pipeline access - ACCESS DENIED")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied. You need pipeline access to use this feature. Contact an administrator to assign you to a pipeline."
                )
            logger.info(f"[STRICT_AUTH] User {current_user.username} (ID: {current_user.id}) authorized - has access to {len(user_pipelines)} pipelines")
        
        # Step 4: All checks passed - user is authorized
        logger.debug(f"[STRICT_AUTH] User {current_user.username} (ID: {current_user.id}, Role: {current_user.role}) - ACCESS GRANTED")
        return current_user
    return strict_checker

