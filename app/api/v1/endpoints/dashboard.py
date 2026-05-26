"""
Module 3 — Overview Dashboard API

Endpoints:
  GET /dashboard/{workspace_id}/overview   → full dashboard data in one call
  GET /dashboard/{workspace_id}/pipeline   → content pipeline counts only
  GET /dashboard/{workspace_id}/published  → recent published posts + metrics
  GET /dashboard/{workspace_id}/scheduled  → upcoming scheduled queue
  GET /dashboard/{workspace_id}/health     → account health score
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import CurrentAuth, get_current_auth
from app.core.workspace_deps import WorkspaceAny, get_workspace_or_403
from app.db.session import get_db
from app.services.dashboard_service import DashboardService

router = APIRouter(prefix="/api/v1/dashboard", tags=["Dashboard"])

DB = Annotated[AsyncSession, Depends(get_db)]


@router.get(
    "/{workspace_id}/overview",
    summary="Full dashboard overview — all cards in one call",
)
async def get_overview(
    ws: WorkspaceAny,
    auth: CurrentAuth,
    db: DB,
) -> dict:
    """
    Returns everything the Overview Dashboard needs in a single request.
    Includes: health score, pipeline, recent posts, scheduled queue,
    engagement trend, Instagram account, pending approvals, credits, optimal time.
    """
    svc = DashboardService(db=db)
    return await svc.get_overview(
        workspace_id=ws.id,
        user_id=auth.user_id,
    )


@router.get(
    "/{workspace_id}/pipeline",
    summary="Content pipeline counts by status",
)
async def get_pipeline(
    ws: WorkspaceAny,
    db: DB,
) -> dict:
    svc = DashboardService(db=db)
    return await svc._content_pipeline(ws.id)


@router.get(
    "/{workspace_id}/published",
    summary="Recent published posts with metrics",
)
async def get_published(
    ws: WorkspaceAny,
    db: DB,
) -> dict:
    svc = DashboardService(db=db)
    posts = await svc._recent_published(ws.id)
    engagement = await svc._engagement_trend(ws.id)
    return {
        "recent_posts": posts,
        "engagement_trend": engagement,
    }


@router.get(
    "/{workspace_id}/scheduled",
    summary="Upcoming scheduled post queue",
)
async def get_scheduled_queue(
    ws: WorkspaceAny,
    db: DB,
) -> dict:
    svc = DashboardService(db=db)
    queue = await svc._scheduled_queue(ws.id)
    return {"queue": queue, "count": len(queue)}


@router.get(
    "/{workspace_id}/health",
    summary="Account health score breakdown",
)
async def get_health(
    ws: WorkspaceAny,
    auth: CurrentAuth,
    db: DB,
) -> dict:
    svc = DashboardService(db=db)
    pipeline    = await svc._content_pipeline(ws.id)
    recent      = await svc._recent_published(ws.id)
    account     = await svc._instagram_account(ws.id)
    engagement  = await svc._engagement_trend(ws.id)
    return svc._compute_health_score(
        pipeline=pipeline,
        recent_posts=recent,
        account=account,
        engagement=engagement,
    )
