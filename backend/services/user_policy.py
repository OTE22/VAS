"""Domain rules for user lifecycle changes, independent of any HTTP layer.

Why this module exists: `_guard_last_administrator` lived in
`backend/routes/users.py` and raised HTTPException, so the DELETE route could
not share it and the service layer could not enforce it — a service importing
from a route inverts the dependency direction, and HTTPException does not
belong in domain code. The rules live here as plain exceptions; routes
translate them to status codes.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.capabilities import Role, canonical_role
from db_models import User, Workspace, WorkspaceMember

logger = logging.getLogger(__name__)


class LastAdministratorError(Exception):
    """The change would leave the platform with no active administrator."""

    def __init__(self, action: str):
        self.action = action
        super().__init__(
            f"Cannot {action} the only remaining administrator. "
            f"Promote another account to administrator first.")


@dataclass
class WorkspaceSuccessorRequired(Exception):
    """The user is the only workspace admin somewhere and no successor works.

    Carries the affected workspaces so the route can return a structured 409
    that tells the caller exactly what to fix.
    """
    workspaces: List[dict] = field(default_factory=list)
    reason: str = "successor_required"

    def __post_init__(self):
        names = ", ".join(w["name"] for w in self.workspaces) or "?"
        super().__init__(
            f"User is the last workspace admin of: {names}. "
            f"Pass reassign_admin_to=<user_id> naming a member of every "
            f"affected workspace.")


async def ensure_not_last_platform_administrator(
        db: AsyncSession, *, target_user_id: int,
        new_role=None, new_is_active=None, deleting: bool = False) -> None:
    """Refuse a change that would remove the final ACTIVE administrator.

    Demotion, deactivation and deletion all count: an inactive or absent admin
    resolves to zero capabilities and is equally unable to administer. Counts
    only active admins — an already-disabled admin account is not a usable
    escape hatch.
    """
    demoting = (new_role is not None
                and canonical_role(new_role) is not Role.ADMIN)
    deactivating = new_is_active is False
    if not (demoting or deactivating or deleting):
        return

    target = (await db.execute(
        select(User).where(User.id == target_user_id)
    )).scalar_one_or_none()
    if target is None or canonical_role(target.role) is not Role.ADMIN:
        return  # not an admin; nothing to protect

    remaining = (await db.execute(
        select(func.count()).select_from(User).where(
            User.role == Role.ADMIN.value,
            User.is_active.is_(True),
            User.id != target_user_id,
        )
    )).scalar_one()

    if remaining == 0:
        action = ("delete" if deleting else
                  "demote" if demoting else "deactivate")
        logger.warning(
            "[USER_POLICY] ❌ Blocked attempt to %s the last administrator (user_id=%s)",
            action, target_user_id)
        raise LastAdministratorError(action)


async def workspaces_needing_admin_successor(
        db: AsyncSession, *, user_id: int) -> List[dict]:
    """Workspaces where `user_id` is the ONLY member with role='admin'.

    Deleting the user would leave each of these with no workspace admin —
    nobody able to manage membership or read orphaned history there.
    """
    solo = []
    memberships = (await db.execute(
        select(WorkspaceMember.workspace_id, Workspace.name)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .where(WorkspaceMember.user_id == user_id,
               WorkspaceMember.role == "admin")
    )).all()
    for workspace_id, name in memberships:
        others = (await db.execute(
            select(func.count()).select_from(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.role == "admin",
                WorkspaceMember.user_id != user_id,
            )
        )).scalar_one()
        if others == 0:
            solo.append({"id": str(workspace_id), "name": name})
    return solo


async def validate_workspace_successor(
        db: AsyncSession, *, affected: List[dict],
        successor_id: Optional[int], deleting_user_id: int) -> None:
    """The successor must be a real, live, different user who is already a
    member of EVERY affected workspace. Anything less is a structured refusal —
    never a partial promotion.
    """
    if not affected:
        return
    if successor_id is None:
        raise WorkspaceSuccessorRequired(workspaces=affected)
    if successor_id == deleting_user_id:
        raise WorkspaceSuccessorRequired(workspaces=affected,
                                         reason="successor_is_target")

    successor = (await db.execute(
        select(User).where(User.id == successor_id)
    )).scalar_one_or_none()
    if successor is None or not successor.is_active:
        raise WorkspaceSuccessorRequired(workspaces=affected,
                                         reason="successor_not_active")

    for workspace in affected:
        member = (await db.execute(
            select(func.count()).select_from(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace["id"],
                WorkspaceMember.user_id == successor_id,
            )
        )).scalar_one()
        if member == 0:
            raise WorkspaceSuccessorRequired(
                workspaces=affected, reason="successor_not_member")
