"""
Module 10 — Workspace Management API

Endpoints:
  GET    /workspaces                    → list user's workspaces
  POST   /workspaces                    → create new workspace
  GET    /workspaces/{id}               → get workspace detail
  PATCH  /workspaces/{id}               → update workspace
  DELETE /workspaces/{id}               → delete workspace (owner only)

  GET    /workspaces/{id}/members       → list members
  POST   /workspaces/{id}/members       → invite member
  PATCH  /workspaces/{id}/members/{uid} → update member role
  DELETE /workspaces/{id}/members/{uid} → remove member

  POST   /workspaces/{id}/transfer      → transfer ownership
  GET    /workspaces/{id}/usage         → workspace credit usage
"""
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import CurrentAuth, get_current_auth
from app.core.workspace_deps import (
    WorkspaceAccess, WorkspaceAdmin, WorkspaceAny,
    WorkspaceCreator, WorkspaceOwner, get_workspace_or_403,
)
from app.db.session import get_db
from app.models.auth import PlanTier
from app.models.content import Workspace, WorkspaceMember, WorkspaceRole

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/workspaces", tags=["Workspaces"])

DB = Annotated[AsyncSession, Depends(get_db)]


# ── Schemas ───────────────────────────────────────────────────────────────────

class WorkspaceCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)
    slug: str = Field(..., min_length=2, max_length=80, pattern=r"^[a-z0-9-]+$")


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=80)
    instagram_username: str | None = None


class WorkspaceRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    instagram_username: str | None
    is_active: bool
    owner_id: uuid.UUID | None
    role: str  # current user's role

    model_config = {"from_attributes": True}


class MemberRead(BaseModel):
    user_id: uuid.UUID
    role: WorkspaceRole
    email: str | None = None
    name: str | None = None

    model_config = {"from_attributes": True}


class InviteMemberRequest(BaseModel):
    email: str
    role: WorkspaceRole = WorkspaceRole.CREATOR


class UpdateRoleRequest(BaseModel):
    role: WorkspaceRole


class TransferOwnershipRequest(BaseModel):
    new_owner_id: uuid.UUID


# ── Workspace CRUD ────────────────────────────────────────────────────────────

@router.get("", response_model=list[WorkspaceRead], summary="List all workspaces for current user")
async def list_workspaces(
    auth: CurrentAuth,
    db: DB,
) -> list[WorkspaceRead]:
    """List all workspaces the current user owns or is a member of."""
    # Owned workspaces
    owned_result = await db.execute(
        select(Workspace).where(
            Workspace.owner_id == auth.user_id,
            Workspace.is_active == True,
        )
    )
    owned = owned_result.scalars().all()

    # Member workspaces
    member_result = await db.execute(
        select(WorkspaceMember, Workspace)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .where(
            WorkspaceMember.user_id == auth.user_id,
            Workspace.is_active == True,
        )
    )
    member_rows = member_result.all()

    result = []
    seen = set()

    for ws in owned:
        if ws.id not in seen:
            result.append(WorkspaceRead(
                id=ws.id, name=ws.name, slug=ws.slug,
                instagram_username=ws.instagram_username,
                is_active=ws.is_active, owner_id=ws.owner_id,
                role=WorkspaceRole.OWNER.value,
            ))
            seen.add(ws.id)

    for member, ws in member_rows:
        if ws.id not in seen:
            result.append(WorkspaceRead(
                id=ws.id, name=ws.name, slug=ws.slug,
                instagram_username=ws.instagram_username,
                is_active=ws.is_active, owner_id=ws.owner_id,
                role=member.role.value,
            ))
            seen.add(ws.id)

    return result


@router.post("", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED, summary="Create a new workspace")
async def create_workspace(
    body: WorkspaceCreate,
    auth: CurrentAuth,
    db: DB,
) -> WorkspaceRead:
    """Create a new workspace. Checks plan limit on max_workspaces."""
    from app.services.billing import BillingService

    billing = BillingService(db=db)
    sub = await billing._get_active_subscription(auth.user_id)
    plan = sub.plan if sub else None

    # Count existing workspaces
    count_result = await db.execute(
        select(Workspace).where(
            Workspace.owner_id == auth.user_id,
            Workspace.is_active == True,
        )
    )
    existing = len(count_result.scalars().all())
    max_ws = plan.max_workspaces if plan else 1

    if existing >= max_ws:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "message": f"Your plan allows {max_ws} workspace(s). Upgrade to create more.",
                "upgrade_url": "/billing",
            },
        )

    # Check slug uniqueness
    slug_check = await db.execute(
        select(Workspace).where(Workspace.slug == body.slug)
    )
    if slug_check.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Slug already taken. Choose a different one.")

    ws = Workspace(
        name=body.name,
        slug=body.slug,
        owner_id=auth.user_id,
        is_active=True,
        settings={},
    )
    db.add(ws)
    await db.flush()

    # Allocate credits for new workspace
    if sub:
        from app.services.credit_service import CreditService
        from app.models.auth import CreditActionType
        credit_svc = CreditService(db=db)
        from app.models.auth import PLAN_MONTHLY_CREDITS
        plan_credits = plan.monthly_credits if plan else 10
        # Share existing balance — just register the balance row
        from app.models.auth import CreditBalance
        from datetime import datetime, timezone, timedelta
        bal = CreditBalance(
            workspace_id=ws.id,
            subscription_id=sub.id,
            balance=plan_credits,
            allocated=plan_credits,
            used=0,
            expires_at=datetime.now(timezone.utc) + timedelta(days=32),
            last_reset_at=datetime.now(timezone.utc),
        )
        db.add(bal)

    log.info("workspace_created", workspace_id=str(ws.id), user_id=str(auth.user_id))
    return WorkspaceRead(
        id=ws.id, name=ws.name, slug=ws.slug,
        instagram_username=ws.instagram_username,
        is_active=ws.is_active, owner_id=ws.owner_id,
        role=WorkspaceRole.OWNER.value,
    )


@router.get("/{workspace_id}", response_model=WorkspaceRead, summary="Get workspace detail")
async def get_workspace(
    ws: WorkspaceAny,
) -> WorkspaceRead:
    return WorkspaceRead(
        id=ws.id, name=ws.name, slug=ws.workspace.slug,
        instagram_username=ws.workspace.instagram_username,
        is_active=ws.workspace.is_active, owner_id=ws.workspace.owner_id,
        role=ws.role.value,
    )


@router.patch("/{workspace_id}", response_model=WorkspaceRead, summary="Update workspace name or Instagram username")
async def update_workspace(
    body: WorkspaceUpdate,
    ws: WorkspaceAdmin,
    db: DB,
) -> WorkspaceRead:
    if body.name:
        ws.workspace.name = body.name
    if body.instagram_username is not None:
        ws.workspace.instagram_username = body.instagram_username
    await db.flush()
    return WorkspaceRead(
        id=ws.id, name=ws.workspace.name, slug=ws.workspace.slug,
        instagram_username=ws.workspace.instagram_username,
        is_active=ws.workspace.is_active, owner_id=ws.workspace.owner_id,
        role=ws.role.value,
    )


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Soft delete workspace")
async def delete_workspace(
    ws: WorkspaceOwner,
    db: DB,
) -> None:
    """Soft delete — marks workspace inactive, retains data for 30 days."""
    ws.workspace.is_active = False
    await db.flush()
    log.info("workspace_deleted", workspace_id=str(ws.id))


# ── Team members ──────────────────────────────────────────────────────────────

@router.get("/{workspace_id}/members", response_model=list[MemberRead], summary="List workspace members")
async def list_members(
    ws: WorkspaceAny,
    db: DB,
) -> list[MemberRead]:
    from app.models.auth import User
    result = await db.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == ws.id)
    )
    rows = result.all()
    return [
        MemberRead(user_id=m.user_id, role=m.role, email=u.email, name=u.name)
        for m, u in rows
    ]


@router.post("/{workspace_id}/members", status_code=status.HTTP_201_CREATED, summary="Invite a member by email")
async def invite_member(
    body: InviteMemberRequest,
    ws: WorkspaceAdmin,
    auth: CurrentAuth,
    db: DB,
) -> dict:
    """Invite a user by email. User must already have an account."""
    from app.models.auth import User, Plan
    from app.services.billing import BillingService

    # Check plan limit
    billing = BillingService(db=db)
    sub = await billing._get_active_subscription(auth.user_id)
    plan = sub.plan if sub else None
    max_members = plan.max_team_members if plan else 1

    count_result = await db.execute(
        select(WorkspaceMember).where(WorkspaceMember.workspace_id == ws.id)
    )
    current_count = len(count_result.scalars().all())
    if current_count >= max_members:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "message": f"Your plan allows {max_members} team member(s). Upgrade for more.",
                "upgrade_url": "/billing",
            },
        )

    # Find user by email
    user_result = await db.execute(
        select(User).where(User.email == body.email.lower())
    )
    target_user = user_result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(
            status_code=404,
            detail="No account found with that email. They need to sign up first.",
        )

    # Check not already a member
    existing = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == target_user.id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="User is already a member.")

    member = WorkspaceMember(
        workspace_id=ws.id,
        user_id=target_user.id,
        role=body.role,
    )
    db.add(member)
    await db.flush()
    log.info("member_invited", workspace_id=str(ws.id), invited_user=str(target_user.id))
    return {"message": f"{target_user.name} added as {body.role.value}"}


@router.patch("/{workspace_id}/members/{user_id}", response_model=MemberRead, summary="Update member role")
async def update_member_role(
    user_id: uuid.UUID,
    body: UpdateRoleRequest,
    ws: WorkspaceAdmin,
    db: DB,
) -> MemberRead:
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found.")
    if body.role == WorkspaceRole.OWNER:
        raise HTTPException(status_code=400, detail="Use the transfer ownership endpoint to change owner.")
    member.role = body.role
    await db.flush()
    return MemberRead(user_id=member.user_id, role=member.role)


@router.delete("/{workspace_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a member")
async def remove_member(
    user_id: uuid.UUID,
    ws: WorkspaceAdmin,
    auth: CurrentAuth,
    db: DB,
) -> None:
    if user_id == auth.user_id:
        raise HTTPException(status_code=400, detail="Cannot remove yourself. Use leave workspace instead.")
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found.")
    await db.delete(member)
    await db.flush()


@router.post("/{workspace_id}/transfer", status_code=status.HTTP_200_OK, summary="Transfer workspace ownership")
async def transfer_ownership(
    body: TransferOwnershipRequest,
    ws: WorkspaceOwner,
    auth: CurrentAuth,
    db: DB,
) -> dict:
    """Transfer workspace ownership to another member."""
    # Verify new owner is a member
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == body.new_owner_id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="New owner must be an existing workspace member.")

    # Transfer
    ws.workspace.owner_id = body.new_owner_id
    member.role = WorkspaceRole.OWNER

    # Demote previous owner to admin
    prev_owner = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == auth.user_id,
        )
    )
    prev = prev_owner.scalar_one_or_none()
    if prev:
        prev.role = WorkspaceRole.ADMIN
    else:
        db.add(WorkspaceMember(
            workspace_id=ws.id,
            user_id=auth.user_id,
            role=WorkspaceRole.ADMIN,
        ))

    await db.flush()
    log.info("ownership_transferred", workspace_id=str(ws.id), new_owner=str(body.new_owner_id))
    return {"message": "Ownership transferred successfully."}


@router.get("/{workspace_id}/usage", summary="Get workspace credit usage and plan limits")
async def get_workspace_usage(
    ws: WorkspaceAny,
    auth: CurrentAuth,
    db: DB,
) -> dict:
    """Credit usage and plan limits for this workspace."""
    from app.services.billing import BillingService
    billing = BillingService(db=db)
    usage = await billing.get_usage_summary(auth.user_id, ws.id)
    return {
        "workspace_id": str(ws.id),
        "workspace_name": ws.name,
        "your_role": ws.role.value,
        "plan": usage.plan_name,
        "credits_balance": usage.credits_balance,
        "credits_used": usage.credits_used,
        "credits_allocated": usage.credits_allocated,
        "credits_pct_used": usage.credits_pct_used,
        "can_generate": usage.can_generate,
        "can_schedule": usage.can_schedule,
        "can_use_analytics": usage.can_use_analytics,
    }
