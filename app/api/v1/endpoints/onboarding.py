"""
Module 12 — Onboarding API

Endpoints:
  GET  /onboarding/{workspace_id}/status   → checklist progress
  POST /onboarding/{workspace_id}/complete → mark onboarding done (skip remaining)
  POST /onboarding/welcome-email           → resend welcome email
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import CurrentAuth, get_current_auth
from app.core.workspace_deps import WorkspaceAny
from app.db.session import get_db
from app.services.onboarding_service import OnboardingService

router = APIRouter(prefix="/api/v1/onboarding", tags=["Onboarding"])

DB = Annotated[AsyncSession, Depends(get_db)]


@router.get(
    "/{workspace_id}/status",
    summary="Get onboarding checklist status",
)
async def get_onboarding_status(
    ws: WorkspaceAny,
    auth: CurrentAuth,
    db: DB,
) -> dict:
    """
    Returns the user's onboarding progress derived from real data.
    No separate flags stored — computed live from DB state.
    """
    svc = OnboardingService(db=db)
    return await svc.get_status(user=auth.user, workspace_id=ws.id)


@router.post(
    "/welcome-email",
    summary="Resend welcome email",
)
async def resend_welcome_email(
    auth: CurrentAuth,
    db: DB,
) -> dict:
    svc = OnboardingService(db=db)
    await svc.send_welcome_email(user=auth.user)
    return {"sent": True, "to": auth.user.email}
