"""
Module 10 — Workspace Dependencies & Tenant Isolation

Every endpoint that takes a workspace_id uses one of these dependencies:
  - get_workspace_or_403     → user must be owner OR member
  - get_workspace_as_owner   → user must be owner only
  - get_workspace_with_role  → user must have specific role

Plan-level feature gates:
  - require_can_schedule
  - require_can_analytics
  - require_can_bulk_generate
  - require_can_export
"""
import uuid
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth_deps import CurrentAuth, get_current_auth
from app.db.session import get_db
from app.models.content import Workspace, WorkspaceMember, WorkspaceRole

log = structlog.get_logger(__name__)


class WorkspaceAccess:
    """Resolved workspace with user's role inside it."""

    def __init__(
        self,
        workspace: Workspace,
        role: WorkspaceRole,
        is_owner: bool,
    ) -> None:
        self.workspace  = workspace
        self.id         = workspace.id
        self.name       = workspace.name
        self.role       = role
        self.is_owner   = is_owner

    # ── Role checks ───────────────────────────────────────────────────────────

    @property
    def can_generate(self) -> bool:
        return self.role in (
            WorkspaceRole.OWNER,
            WorkspaceRole.ADMIN,
            WorkspaceRole.CREATOR,
        )

    @property
    def can_approve(self) -> bool:
        return self.role in (
            WorkspaceRole.OWNER,
            WorkspaceRole.ADMIN,
            WorkspaceRole.REVIEWER,
        )

    @property
    def can_manage(self) -> bool:
        """Full management — settings, team, billing."""
        return self.role in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN)

    def assert_can_generate(self) -> None:
        if not self.can_generate:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your role does not have permission to generate content in this workspace.",
            )

    def assert_can_approve(self) -> None:
        if not self.can_approve:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your role does not have permission to approve content in this workspace.",
            )

    def assert_can_manage(self) -> None:
        if not self.can_manage:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only workspace owners and admins can manage workspace settings.",
            )


# ── Core dependency factory ────────────────────────────────────────────────────

async def _resolve_workspace(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession,
    require_owner: bool = False,
    min_role: WorkspaceRole | None = None,
) -> WorkspaceAccess:
    """
    Core resolution logic:
    1. Load workspace — 404 if not found or inactive
    2. Check if user is owner → grant OWNER role
    3. Check WorkspaceMember → grant their role
    4. If neither → 403
    5. If require_owner and not owner → 403
    6. If min_role and role is insufficient → 403
    """
    # Load workspace
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.is_active == True,
        )
    )
    workspace = result.scalar_one_or_none()
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found.",
        )

    user_id   = auth.user_id
    is_owner  = workspace.owner_id == user_id

    if is_owner:
        role = WorkspaceRole.OWNER
    else:
        # Check membership
        member_result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
        )
        member = member_result.scalar_one_or_none()

        if not member:
            log.warning(
                "workspace_access_denied",
                user_id=str(user_id),
                workspace_id=str(workspace_id),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this workspace.",
            )
        role = member.role

    if require_owner and not is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workspace owner can perform this action.",
        )

    # Role hierarchy check
    ROLE_HIERARCHY = [
        WorkspaceRole.VIEWER,
        WorkspaceRole.REVIEWER,
        WorkspaceRole.CREATOR,
        WorkspaceRole.ADMIN,
        WorkspaceRole.OWNER,
    ]
    if min_role and ROLE_HIERARCHY.index(role) < ROLE_HIERARCHY.index(min_role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This action requires at least {min_role.value} role.",
        )

    return WorkspaceAccess(workspace=workspace, role=role, is_owner=is_owner)


# ── Public dependency functions ────────────────────────────────────────────────

async def get_workspace_or_403(
    workspace_id: uuid.UUID,
    auth: Annotated[object, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkspaceAccess:
    """Any member or owner of the workspace."""
    return await _resolve_workspace(workspace_id, auth, db)


async def get_workspace_as_owner(
    workspace_id: uuid.UUID,
    auth: Annotated[object, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkspaceAccess:
    """Only the workspace owner."""
    return await _resolve_workspace(workspace_id, auth, db, require_owner=True)


async def get_workspace_as_admin(
    workspace_id: uuid.UUID,
    auth: Annotated[object, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkspaceAccess:
    """Owner or admin role."""
    return await _resolve_workspace(
        workspace_id, auth, db, min_role=WorkspaceRole.ADMIN
    )


async def get_workspace_as_creator(
    workspace_id: uuid.UUID,
    auth: Annotated[object, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkspaceAccess:
    """Creator role or above."""
    return await _resolve_workspace(
        workspace_id, auth, db, min_role=WorkspaceRole.CREATOR
    )


# ── Typed aliases ─────────────────────────────────────────────────────────────
WorkspaceAny     = Annotated[WorkspaceAccess, Depends(get_workspace_or_403)]
WorkspaceOwner   = Annotated[WorkspaceAccess, Depends(get_workspace_as_owner)]
WorkspaceAdmin   = Annotated[WorkspaceAccess, Depends(get_workspace_as_admin)]
WorkspaceCreator = Annotated[WorkspaceAccess, Depends(get_workspace_as_creator)]


# ── Plan feature gate dependencies ────────────────────────────────────────────

async def require_can_schedule(
    auth: Annotated[object, Depends(get_current_auth)],
) -> None:
    """Raises 402 if plan does not include scheduling."""
    from app.core.config import settings as _s
    if _s.TESTING_MODE:
        return

    from fastapi import HTTPException, status as s
    plan = auth.plan if hasattr(auth, "plan") else None
    if not plan or not plan.can_schedule:
        raise HTTPException(
            status_code=s.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "message": "Scheduling requires Starter plan or above.",
                "upgrade_url": "/billing",
            },
        )


async def require_can_analytics(
    auth: Annotated[object, Depends(get_current_auth)],
) -> None:
    from app.core.config import settings as _s
    if _s.TESTING_MODE:
        return

    plan = auth.plan if hasattr(auth, "plan") else None
    if not plan or not plan.can_use_analytics:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "message": "Analytics requires Starter plan or above.",
                "upgrade_url": "/billing",
            },
        )


async def require_can_bulk_generate(
    auth: Annotated[object, Depends(get_current_auth)],
) -> None:
    from app.core.config import settings as _s
    if _s.TESTING_MODE:
        return

    plan = auth.plan if hasattr(auth, "plan") else None
    if not plan or not plan.can_bulk_generate:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "message": "30-day calendar generation requires Pro plan or above.",
                "upgrade_url": "/billing",
            },
        )


async def require_can_export(
    auth: Annotated[object, Depends(get_current_auth)],
) -> None:
    from app.core.config import settings as _s
    if _s.TESTING_MODE:
        return

    plan = auth.plan if hasattr(auth, "plan") else None
    if not plan or not plan.can_export:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "message": "Export requires Pro plan or above.",
                "upgrade_url": "/billing",
            },
        )
