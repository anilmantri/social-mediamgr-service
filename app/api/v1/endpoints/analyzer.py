"""
Module 4 — Analyzer Dashboard API

Endpoints:
  GET /analyzer/{workspace_id}/report          → full analytics report
  GET /analyzer/{workspace_id}/top-posts       → top performing posts
  GET /analyzer/{workspace_id}/hashtags        → hashtag effectiveness
  GET /analyzer/{workspace_id}/heatmap         → posting time heatmap
  GET /analyzer/{workspace_id}/content-types   → content type breakdown
  GET /analyzer/{workspace_id}/suggestions     → AI improvement suggestions
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import CurrentAuth, get_current_auth
from app.core.workspace_deps import WorkspaceAny, require_can_analytics
from app.db.session import get_db
from app.services.analyzer_service import AnalyzerService

router = APIRouter(prefix="/api/v1/analyzer", tags=["Analyzer"])

DB = Annotated[AsyncSession, Depends(get_db)]


@router.get(
    "/{workspace_id}/report",
    summary="Full analyzer report — all analytics in one call",
    dependencies=[Depends(require_can_analytics)],
)
async def get_full_report(
    ws: WorkspaceAny,
    auth: CurrentAuth,
    db: DB,
    days: int = Query(default=30, ge=7, le=90, description="Lookback period in days"),
) -> dict:
    """
    Deep analytics report. Requires Starter plan or above.
    Includes: top/worst posts, content type comparison,
    hashtag scores, time heatmap, caption analysis, AI suggestions.
    """
    svc = AnalyzerService(db=db)
    return await svc.get_full_report(workspace_id=ws.id, days=days)


@router.get(
    "/{workspace_id}/top-posts",
    summary="Top performing posts by engagement rate",
    dependencies=[Depends(require_can_analytics)],
)
async def get_top_posts(
    ws: WorkspaceAny,
    db: DB,
    days: int = Query(default=30, ge=7, le=90),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    from datetime import datetime, timedelta, timezone
    svc = AnalyzerService(db=db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    metrics = await svc._load_metrics(ws.id, cutoff)
    top = await svc._top_posts(metrics, limit=limit)
    worst = await svc._worst_posts(metrics, limit=3)
    return {"top": top, "worst": worst, "total_posts": len(metrics)}


@router.get(
    "/{workspace_id}/hashtags",
    summary="Hashtag effectiveness ranking",
    dependencies=[Depends(require_can_analytics)],
)
async def get_hashtag_analysis(
    ws: WorkspaceAny,
    db: DB,
    days: int = Query(default=30, ge=7, le=90),
) -> dict:
    from datetime import datetime, timedelta, timezone
    svc = AnalyzerService(db=db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    metrics = await svc._load_metrics(ws.id, cutoff)
    hashtags = await svc._hashtag_analysis(metrics, ws.id, cutoff)
    return {
        "hashtags": hashtags,
        "total_unique": len(hashtags),
        "excellent": [h for h in hashtags if h["performance"] == "excellent"],
        "poor":      [h for h in hashtags if h["performance"] == "poor"],
    }


@router.get(
    "/{workspace_id}/heatmap",
    summary="Posting time heatmap — day × hour engagement matrix",
    dependencies=[Depends(require_can_analytics)],
)
async def get_time_heatmap(
    ws: WorkspaceAny,
    db: DB,
    days: int = Query(default=60, ge=7, le=90),
) -> dict:
    from datetime import datetime, timedelta, timezone
    svc = AnalyzerService(db=db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    metrics = await svc._load_metrics(ws.id, cutoff)
    heatmap = await svc._time_heatmap(metrics)
    return {
        "heatmap": heatmap,
        "best_slot": heatmap[0] if heatmap else None,
        "total_data_points": len(heatmap),
    }


@router.get(
    "/{workspace_id}/content-types",
    summary="Performance breakdown by content format",
    dependencies=[Depends(require_can_analytics)],
)
async def get_content_type_breakdown(
    ws: WorkspaceAny,
    db: DB,
    days: int = Query(default=30, ge=7, le=90),
) -> dict:
    from datetime import datetime, timedelta, timezone
    svc = AnalyzerService(db=db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    metrics = await svc._load_metrics(ws.id, cutoff)
    breakdown = await svc._content_type_breakdown(ws.id, metrics, cutoff)
    return {
        "breakdown": breakdown,
        "best_type": breakdown[0]["content_type"] if breakdown else None,
    }


@router.get(
    "/{workspace_id}/suggestions",
    summary="AI-powered improvement suggestions",
    dependencies=[Depends(require_can_analytics)],
)
async def get_suggestions(
    ws: WorkspaceAny,
    auth: CurrentAuth,
    db: DB,
    days: int = Query(default=30, ge=7, le=90),
) -> dict:
    from datetime import datetime, timedelta, timezone
    svc = AnalyzerService(db=db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    metrics = await svc._load_metrics(ws.id, cutoff)
    if not metrics:
        return {"suggestions": [], "has_data": False}
    summary    = await svc._summary_stats(metrics)
    top_posts  = await svc._top_posts(metrics)
    hashtags   = await svc._hashtag_analysis(metrics, ws.id, cutoff)
    suggestions = await svc._ai_suggestions(ws.id, summary, top_posts, hashtags)
    return {"suggestions": suggestions, "has_data": True, "based_on_posts": len(metrics)}
