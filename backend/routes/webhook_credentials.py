"""Admin API for issued ingest credentials.

Mounted under /api/admin/, NOT under /api/webhook/: nginx has a dedicated
`location ~ ^/(api/)?webhook/` block shaped for ingest (its own body limit and
short proxy timeouts), and credential management is an ordinary admin surface.

The raw token exists in exactly one response, once, and is never recoverable
afterwards. Everything else here returns a fingerprint instead.
"""

import logging
import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.auth_service import require_role
from backend.routes.upload import require_upload_csrf
from config import settings
from db_connection import get_db
from db_models import SettingsAuditLog, User, WebhookCredential

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/webhook-credentials", tags=["Ingest Credentials"])

_NO_STORE = "no-store, no-cache, must-revalidate"


def _normalize_name(raw: str) -> str:
    """Collapse internal whitespace and trim. The display form."""
    return " ".join(str(raw or "").split())


def _name_key(name: str) -> str:
    """The uniqueness key: casefolded display form.

    So "Acme VMS", "acme  vms" and " ACME VMS " cannot all exist and leave a log
    line ambiguous about which credential authenticated a frame.
    """
    return _normalize_name(name).casefold()


class CreateCredentialRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100,
                      description="Who this credential is for, e.g. 'Acme VMS'")


class WebhookCredentialOut(BaseModel):
    """The listable shape. Declaring this as the response_model is what makes
    token leakage structurally impossible rather than a matter of reviewer
    discipline: FastAPI serializes these fields, not the ORM row."""
    id: int
    name: str
    fingerprint: str
    created_at: Optional[str] = None
    created_by_username: Optional[str] = None
    last_used_at: Optional[str] = None


class WebhookCredentialCreated(WebhookCredentialOut):
    token: str
    warning: str


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() + "Z" if value else None


def _fingerprint(token_hash: str) -> str:
    """First 8 hex chars of the SHA-256. Derived, never stored as a column.

    Non-reversible, and it lets someone holding a token identify its row
    (sha256(token)[:8]). Publishing 32 bits of the digest of a 256-bit
    secrets.token_urlsafe(32) value enables nothing.
    """
    return (token_hash or "")[:8]


def _render(row: WebhookCredential) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "fingerprint": _fingerprint(row.token_hash),
        "created_at": _iso(row.created_at),
        "created_by_username": row.created_by_username,
        "last_used_at": _iso(row.last_used_at),
    }


async def _audit(db: AsyncSession, request: Request, current_user: User,
                 action: str, name: str) -> None:
    """Record issuance/revocation. The NAME only — never the token, never the hash."""
    try:
        from backend.utils.identity_audit import get_client_info
        ip_address, user_agent = get_client_info(request)
        entry = SettingsAuditLog(
            setting_key="WEBHOOK_CREDENTIALS",
            old_value=None,
            new_value=name,
            value_type="credential",
            changed_by_user_id=current_user.id,
            changed_by_username=current_user.username,
            # `action` is already "credential_issued"/"credential_revoked";
            # prefixing it verbatim read "ingest credential credential_revoked".
            change_reason=f"ingest credential {action.replace('credential_', '')}",
            ip_address=ip_address,
            user_agent=user_agent,
        )
        if hasattr(entry, "action"):
            entry.action = action
        db.add(entry)
        await db.commit()
    except Exception as e:                                     # noqa: BLE001
        logger.warning("[WEBHOOK] Failed to audit credential %s: %s", action, e)


@router.post("", status_code=201, response_model=WebhookCredentialCreated)
async def create_credential(
    payload: CreateCredentialRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(require_upload_csrf),
):
    """Mint a credential. The token is in THIS response and nowhere else, ever."""
    response.headers["Cache-Control"] = _NO_STORE
    response.headers["Pragma"] = "no-cache"

    name = _normalize_name(payload.name)
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Name must be at least 2 characters")

    from backend.core.enrollment_service import hash_upload_token

    raw_token = secrets.token_urlsafe(32)
    row = WebhookCredential(
        token_hash=hash_upload_token(raw_token),
        name=name,
        name_key=_name_key(name),
        created_by_user_id=current_user.id,
        created_by_username=current_user.username,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # Caught, never pre-checked with a SELECT: a pre-check is a TOCTOU race
        # that lets two concurrent mints both pass and turns one into a 500.
        await db.rollback()
        raise HTTPException(status_code=409, detail={
            "error": {"code": "CREDENTIAL_NAME_TAKEN",
                      "message": f"A credential named '{name}' already exists."}})
    await db.refresh(row)

    from backend.security import webhook_credentials as cred_cache
    cred_cache.invalidate()

    await _audit(db, request, current_user, "credential_issued", name)
    logger.info("[WEBHOOK] ingest credential issued: name=%r by=%s",
                name, current_user.username)

    return {
        **_render(row),
        "token": raw_token,
        "warning": ("This token is shown once and cannot be recovered. Store it "
                    "now; if it is lost, revoke this credential and issue a new one."),
    }


@router.get("")
async def list_credentials(
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
):
    """Every issued credential. Never a token, never a full hash."""
    response.headers["Cache-Control"] = _NO_STORE

    rows = (await db.execute(
        select(WebhookCredential).order_by(WebhookCredential.created_at.desc())
    )).scalars().all()

    from backend.security import webhook_auth
    from backend.security import webhook_credentials as cred_cache

    ttl = cred_cache.ttl_seconds()

    return {
        "credentials": [WebhookCredentialOut(**_render(r)).model_dump() for r in rows],
        "count": len(rows),
        # A boolean, never a value or a digest count: it only tells the page
        # whether the break-glass path is armed.
        "env_keys_configured": webhook_auth.keys_configured(settings),
        # So the page renders the revocation-latency note from the real number
        # instead of a hardcoded one that can drift.
        "cache_ttl_seconds": ttl,
    }


@router.delete("/{credential_id}")
async def revoke_credential(
    credential_id: int,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(["admin"])),
    _csrf: None = Depends(require_upload_csrf),
):
    """Revoke by deleting the row. There is no revoked state to distinguish."""
    response.headers["Cache-Control"] = _NO_STORE

    row = (await db.execute(
        select(WebhookCredential).where(WebhookCredential.id == credential_id)
    )).scalars().first()
    if row is None:
        # Identical to "already deleted" — the two are the same state.
        raise HTTPException(status_code=404, detail="Credential not found")

    name = row.name
    await db.delete(row)
    await db.commit()

    from backend.security import webhook_credentials as cred_cache
    cred_cache.invalidate()

    ttl = cred_cache.ttl_seconds()

    await _audit(db, request, current_user, "credential_revoked", name)
    logger.info("[WEBHOOK] ingest credential revoked: name=%r by=%s",
                name, current_user.username)

    return {
        "deleted": True,
        "id": credential_id,
        "name": name,
        "effective_within_seconds": ttl,
        # The honest guarantee. invalidate() above only clears THIS worker's
        # cache; gunicorn runs several, so the other workers pick the deletion
        # up on their own refresh.
        "message": (f"Revoked. Cached verifiers refresh within {ttl}s; a frame "
                    "presented in that window may still be accepted."),
    }
