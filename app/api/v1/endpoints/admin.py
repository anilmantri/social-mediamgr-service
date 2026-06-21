"""
Module 11 — Admin Dashboard API

All endpoints require is_admin=True on the User model.
Never expose these to regular users.

Endpoints:
  GET  /admin/users                     → paginated user list
  GET  /admin/users/{id}                → user detail
  POST /admin/users/{id}/plan           → override plan
  POST /admin/users/{id}/toggle         → enable/disable account
  POST /admin/workspaces/{id}/credits   → grant credits
  GET  /admin/metrics/revenue           → MRR, ARR, conversion
  GET  /admin/metrics/health            → system health
  GET  /admin/metrics/overview          → combined admin overview
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import CurrentAuth, get_current_auth
from app.db.session import get_db
from app.models.auth import PlanTier
from app.services.admin_service import AdminService

router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])

DB = Annotated[AsyncSession, Depends(get_db)]


# ── Admin gate dependency ──────────────────────────────────────────────────────

async def require_admin(auth: CurrentAuth) -> CurrentAuth:
    if not auth.user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return auth


AdminAuth = Annotated[CurrentAuth, Depends(require_admin)]


# ── Schemas ────────────────────────────────────────────────────────────────────

class OverridePlanRequest(BaseModel):
    plan_tier: PlanTier


class GrantCreditsRequest(BaseModel):
    amount: int
    reason: str


class ToggleUserRequest(BaseModel):
    active: bool


# ── Users ─────────────────────────────────────────────────────────────────────

@router.get("/users", summary="List all users")
async def list_users(
    auth: AdminAuth,
    db: DB,
    page:      int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search:    str | None = Query(default=None),
    plan_tier: PlanTier | None = Query(default=None),
) -> dict:
    svc = AdminService(db=db)
    return await svc.list_users(
        page=page, page_size=page_size,
        search=search, plan_tier=plan_tier,
    )


@router.get("/users/{user_id}", summary="User detail with subscriptions and workspaces")
async def get_user(
    user_id: uuid.UUID,
    auth: AdminAuth,
    db: DB,
) -> dict:
    svc = AdminService(db=db)
    detail = await svc.get_user_detail(user_id)
    if not detail:
        raise HTTPException(status_code=404, detail="User not found")
    return detail


@router.post("/users/{user_id}/plan", summary="Override user plan")
async def override_plan(
    user_id: uuid.UUID,
    body: OverridePlanRequest,
    auth: AdminAuth,
    db: DB,
) -> dict:
    svc = AdminService(db=db)
    try:
        return await svc.override_plan(
            user_id=user_id,
            plan_tier=body.plan_tier,
            admin_user_id=auth.user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/users/{user_id}/toggle", summary="Enable or disable user account")
async def toggle_user(
    user_id: uuid.UUID,
    body: ToggleUserRequest,
    auth: AdminAuth,
    db: DB,
) -> dict:
    if user_id == auth.user_id:
        raise HTTPException(status_code=400, detail="Cannot disable your own account.")
    svc = AdminService(db=db)
    try:
        return await svc.toggle_user_active(
            user_id=user_id,
            active=body.active,
            admin_user_id=auth.user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Credits ────────────────────────────────────────────────────────────────────

@router.post("/workspaces/{workspace_id}/credits", summary="Grant credits to workspace")
async def grant_credits(
    workspace_id: uuid.UUID,
    body: GrantCreditsRequest,
    auth: AdminAuth,
    db: DB,
) -> dict:
    if body.amount <= 0 or body.amount > 10000:
        raise HTTPException(status_code=400, detail="Amount must be between 1 and 10,000")
    svc = AdminService(db=db)
    try:
        return await svc.grant_credits(
            workspace_id=workspace_id,
            amount=body.amount,
            reason=body.reason,
            admin_user_id=auth.user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Metrics ────────────────────────────────────────────────────────────────────

@router.get("/metrics/revenue", summary="Revenue metrics — MRR, ARR, conversion")
async def get_revenue_metrics(auth: AdminAuth, db: DB) -> dict:
    svc = AdminService(db=db)
    return await svc.get_revenue_metrics()


@router.get("/metrics/health", summary="System health — DB, Redis, queue")
async def get_system_health(auth: AdminAuth, db: DB) -> dict:
    svc = AdminService(db=db)
    return await svc.get_system_health()


@router.get("/metrics/overview", summary="Combined admin overview — one call")
async def get_admin_overview(auth: AdminAuth, db: DB) -> dict:
    import asyncio
    svc = AdminService(db=db)
    revenue, health = await asyncio.gather(
        svc.get_revenue_metrics(),
        svc.get_system_health(),
    )
    # Recent users
    users = await svc.list_users(page=1, page_size=10)
    return {
        "revenue": revenue,
        "health":  health,
        "recent_users": users["users"],
        "total_users": users["total"],
    }
