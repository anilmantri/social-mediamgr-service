"""
Module 2 API Router — Calendar & Scheduler

Endpoints:
  GET  /instagram/connect              → OAuth redirect URL
  GET  /instagram/callback             → OAuth callback, exchange code
  GET  /instagram/account/{ws_id}      → connected account info
  DELETE /instagram/account/{ws_id}    → disconnect account

  POST /schedule                       → schedule an approved post
  DELETE /schedule/{id}                → unschedule
  PATCH /schedule/{id}/reschedule      → move to new time
  GET  /schedule                       → list scheduled posts
  GET  /schedule/{id}                  → detail + publish logs
  GET  /schedule/{id}/metrics          → post performance metrics

  GET  /calendar/{ws_id}/{year}/{month} → full month calendar view

  GET  /optimal-time/{ws_id}           → best time suggestions
  POST /optimal-time/{ws_id}/recompute → force recompute from metrics

  GET  /evergreen/{ws_id}              → list evergreen candidates
  POST /evergreen/recycle              → schedule a recycled post
"""
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import get_current_user_id as _real_get_current_user_id
from app.core.config import settings
from app.db.session import get_db
from app.models.scheduler import (
    EvergreenCandidate,
    InstagramAccount,
    PostMetrics,
    PostPublishLog,
    PublishStatus,
    ScheduledPost,
)
from app.schemas.scheduler import (
    CalendarMonthRead,
    EvergreenCandidateRead,
    InstagramAccountRead,
    InstagramConnectRequest,
    OptimalTimeSuggestion,
    PostMetricsRead,
    PublishLogRead,
    RecycleRequest,
    RescheduleRequest,
    SchedulePostRequest,
    ScheduledPostList,
    ScheduledPostRead,
)
from app.services.instagram_client import get_ig_client
from app.services.optimal_time import OptimalTimeService
from app.services.scheduler import SchedulerService
from app.services.credit_service import CreditService
from app.core.workspace_deps import get_workspace_or_403, require_can_schedule

log = structlog.get_logger(__name__)

CurrentUser = Annotated[uuid.UUID, Depends(_real_get_current_user_id)]
DB = Annotated[AsyncSession, Depends(get_db)]

router = APIRouter(prefix="/api/v1", tags=["Calendar & Scheduler"])


# ── Instagram OAuth ────────────────────────────────────────────────────────────

@router.get(
    "/instagram/auth-url",
    summary="Get Instagram OAuth redirect URL",
)
async def get_auth_url(
    workspace_id: uuid.UUID = Query(...,
)) -> dict:
    """Returns the URL to redirect the user to for Instagram authorisation."""
    url = (
        f"https://www.facebook.com/dialog/oauth"
        f"?client_id={settings.INSTAGRAM_APP_ID}"
        f"&redirect_uri={settings.INSTAGRAM_REDIRECT_URI}"
        f"&scope=instagram_basic,instagram_content_publish,instagram_manage_insights,pages_read_engagement"
        f"&state={workspace_id}"
        f"&response_type=code"
    )
    return {"auth_url": url, "workspace_id": str(workspace_id)}


@router.get(
    "/instagram/callback",
    summary="OAuth callback — exchange code for token",
    include_in_schema=False,  # not user-facing, called by Meta redirect
)
async def instagram_callback(
    code: str,
    state: uuid.UUID,      # workspace_id passed via state param
    db: DB,
) -> dict:
    """
    Receives the OAuth code from Meta, exchanges for long-lived token,
    fetches account info, and upserts InstagramAccount.
    """
    ig = get_ig_client()
    try:
        token_data = await ig.exchange_code_for_token(code)
        access_token = token_data["access_token"]

        # Get user's IG account linked to this token
        # We need the IG user id — fetch via /me with accounts
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            me_resp = await client.get(
                f"{settings.INSTAGRAM_BASE_URL}/{settings.INSTAGRAM_API_VERSION}/me/accounts",
                params={"access_token": access_token, "fields": "instagram_business_account,name"},
            )
        pages = me_resp.json().get("data", [])
        if not pages:
            raise HTTPException(status_code=400, detail="No Facebook Page linked to this account.")

        # Use first page's Instagram Business Account
        ig_biz = pages[0].get("instagram_business_account", {})
        ig_user_id = ig_biz.get("id")
        if not ig_user_id:
            raise HTTPException(status_code=400, detail="No Instagram Business Account found on this Page.")

        account_info = await ig.get_account_info(ig_user_id, access_token)

        from datetime import timedelta
        expires_in = token_data.get("expires_in", 5184000)
        from datetime import datetime, timezone
        token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        # Upsert InstagramAccount
        existing = await db.execute(
            select(InstagramAccount).where(
                InstagramAccount.workspace_id == state
            )
        )
        account = existing.scalar_one_or_none()
        if account:
            account.access_token = access_token
            account.token_expires_at = token_expires_at
            account.instagram_user_id = ig_user_id
            account.instagram_username = account_info.get("username", "")
            account.followers_count = account_info.get("followers_count", 0)
            account.media_count = account_info.get("media_count", 0)
            account.profile_picture_url = account_info.get("profile_picture_url")
            account.biography = account_info.get("biography")
            account.is_active = True
            account.last_api_error = None
        else:
            account = InstagramAccount(
                workspace_id=state,
                instagram_user_id=ig_user_id,
                instagram_username=account_info.get("username", ""),
                access_token=access_token,
                token_expires_at=token_expires_at,
                followers_count=account_info.get("followers_count", 0),
                media_count=account_info.get("media_count", 0),
                profile_picture_url=account_info.get("profile_picture_url"),
                biography=account_info.get("biography"),
                is_active=True,
            )
            db.add(account)
        await db.flush()

        log.info("instagram_connected", workspace_id=str(state), ig_user=ig_user_id)
        return {
            "status": "connected",
            "instagram_username": account.instagram_username,
            "workspace_id": str(state),
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("instagram_callback_error", error=str(exc))
        raise HTTPException(status_code=400, detail=f"Instagram connection failed: {exc}")


@router.get(
    "/instagram/account/{workspace_id}",
    response_model=InstagramAccountRead,
    summary="Get connected Instagram account",
)
async def get_instagram_account(
    db: DB,
    current_user: CurrentUser,
    workspace_id: uuid.UUID,
) -> InstagramAccountRead:
    result = await db.execute(
        select(InstagramAccount).where(
            InstagramAccount.workspace_id == workspace_id,
            InstagramAccount.is_active == True,
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="No connected Instagram account found.")
    return InstagramAccountRead.model_validate(account)


@router.delete(
    "/instagram/account/{workspace_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disconnect Instagram account",
)
async def disconnect_instagram_account(
    db: DB,
    current_user: CurrentUser,
    workspace_id: uuid.UUID,
) -> None:
    result = await db.execute(
        select(InstagramAccount).where(
            InstagramAccount.workspace_id == workspace_id
        )
    )
    account = result.scalar_one_or_none()
    if account:
        account.is_active = False


# ── Scheduling ────────────────────────────────────────────────────────────────

@router.post(
    "/schedule",
    response_model=ScheduledPostRead,
    status_code=status.HTTP_201_CREATED,
    summary="Schedule an approved post",
)
async def schedule_post(
    db: DB,
    current_user: CurrentUser,
    body: SchedulePostRequest,
) -> ScheduledPostRead:
    svc = SchedulerService(db=db)
    _credit_svc = CreditService(db=db)
    await _credit_svc.assert_can_schedule(user_id=current_user)
    try:
        sp = await svc.schedule_post(
            workspace_id=body.workspace_id,
            draft_content_id=body.draft_content_id,
            scheduled_at=body.scheduled_at,
            scheduled_by_id=current_user,
            use_optimal_time=body.use_optimal_time,
        )
        return ScheduledPostRead.model_validate(sp)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete(
    "/schedule/{scheduled_post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unschedule a post",
)
async def unschedule_post(
    db: DB,
    current_user: CurrentUser,
    scheduled_post_id: uuid.UUID,
    workspace_id: uuid.UUID = Query(...,
),
) -> None:
    svc = SchedulerService(db=db)
    try:
        await svc.unschedule_post(scheduled_post_id, workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch(
    "/schedule/{scheduled_post_id}/reschedule",
    response_model=ScheduledPostRead,
    summary="Move a post to a new time",
)
async def reschedule_post(
    db: DB,
    current_user: CurrentUser,
    scheduled_post_id: uuid.UUID,
    body: RescheduleRequest,
    workspace_id: uuid.UUID = Query(...,
),
) -> ScheduledPostRead:
    svc = SchedulerService(db=db)
    try:
        sp = await svc.reschedule_post(scheduled_post_id, workspace_id, body.new_scheduled_at)
        return ScheduledPostRead.model_validate(sp)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/schedule",
    response_model=ScheduledPostList,
    summary="List scheduled posts",
)
async def list_scheduled_posts(
    db: DB,
    current_user: CurrentUser,
    workspace_id: uuid.UUID = Query(...,
),
    publish_status: PublishStatus | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> ScheduledPostList:
    svc = SchedulerService(db=db)
    posts, total = await svc.list_scheduled_posts(
        workspace_id=workspace_id,
        status=publish_status,
        page=page,
        page_size=page_size,
    )
    return ScheduledPostList(
        items=[ScheduledPostRead.model_validate(p) for p in posts],
        total=total,
        page=page,
        page_size=page_size,
        has_next=(page * page_size) < total,
    )


@router.get(
    "/schedule/{scheduled_post_id}",
    response_model=ScheduledPostRead,
    summary="Get scheduled post detail",
)
async def get_scheduled_post(
    db: DB,
    current_user: CurrentUser,
    scheduled_post_id: uuid.UUID,
) -> ScheduledPostRead:
    result = await db.execute(
        select(ScheduledPost).where(ScheduledPost.id == scheduled_post_id)
    )
    sp = result.scalar_one_or_none()
    if not sp:
        raise HTTPException(status_code=404, detail="Scheduled post not found")
    return ScheduledPostRead.model_validate(sp)


@router.get(
    "/schedule/{scheduled_post_id}/metrics",
    response_model=PostMetricsRead,
    summary="Get post performance metrics",
)
async def get_post_metrics(
    db: DB,
    current_user: CurrentUser,
    scheduled_post_id: uuid.UUID,
) -> PostMetricsRead:
    result = await db.execute(
        select(PostMetrics).where(
            PostMetrics.scheduled_post_id == scheduled_post_id
        )
    )
    metrics = result.scalar_one_or_none()
    if not metrics:
        raise HTTPException(
            status_code=404,
            detail="No metrics yet. Metrics are synced every 6 hours after publishing.",
        )
    return PostMetricsRead.model_validate(metrics)


@router.get(
    "/schedule/{scheduled_post_id}/logs",
    response_model=list[PublishLogRead],
    summary="Get publish attempt logs",
)
async def get_publish_logs(
    db: DB,
    current_user: CurrentUser,
    scheduled_post_id: uuid.UUID,
) -> list[PublishLogRead]:
    result = await db.execute(
        select(PostPublishLog)
        .where(PostPublishLog.scheduled_post_id == scheduled_post_id)
        .order_by(PostPublishLog.created_at)
    )
    logs = result.scalars().all()
    return [PublishLogRead.model_validate(l) for l in logs]


# ── Calendar ───────────────────────────────────────────────────────────────────

@router.get(
    "/calendar/{workspace_id}/{year}/{month}",
    response_model=CalendarMonthRead,
    summary="Get full month calendar view",
)
async def get_calendar_month(
    db: DB,
    current_user: CurrentUser,
    workspace_id: uuid.UUID,
    year: int,
    month: int,
) -> CalendarMonthRead:
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="month must be 1-12")
    if not (2024 <= year <= 2030):
        raise HTTPException(status_code=400, detail="year must be 2024-2030")
    svc = SchedulerService(db=db)
    return await svc.get_calendar_month(workspace_id, year, month)


# ── Optimal time ──────────────────────────────────────────────────────────────

@router.get(
    "/optimal-time/{workspace_id}",
    response_model=OptimalTimeSuggestion,
    summary="Get best posting time suggestions",
)
async def get_optimal_time(
    db: DB,
    current_user: CurrentUser,
    workspace_id: uuid.UUID,
) -> OptimalTimeSuggestion:
    svc = OptimalTimeService(db=db)
    return await svc.get_suggestion(workspace_id)


@router.post(
    "/optimal-time/{workspace_id}/recompute",
    summary="Force recompute optimal time slots from existing metrics",
)
async def recompute_optimal_time(
    db: DB,
    current_user: CurrentUser,
    workspace_id: uuid.UUID,
) -> dict:
    svc = OptimalTimeService(db=db)
    updated = await svc.compute_optimal_slots(workspace_id)
    return {"slots_updated": updated}


# ── Evergreen ──────────────────────────────────────────────────────────────────

@router.get(
    "/evergreen/{workspace_id}",
    response_model=list[EvergreenCandidateRead],
    summary="List evergreen candidates (top performing posts for recycling)",
)
async def list_evergreen(
    db: DB,
    current_user: CurrentUser,
    workspace_id: uuid.UUID,
) -> list[EvergreenCandidateRead]:
    result = await db.execute(
        select(EvergreenCandidate)
        .where(
            EvergreenCandidate.workspace_id == workspace_id,
            EvergreenCandidate.is_active == True,
        )
        .order_by(EvergreenCandidate.engagement_score.desc())
    )
    candidates = result.scalars().all()
    return [EvergreenCandidateRead.model_validate(c) for c in candidates]


@router.post(
    "/evergreen/recycle",
    response_model=ScheduledPostRead,
    status_code=status.HTTP_201_CREATED,
    summary="Recycle an evergreen post — schedule a refreshed copy",
)
async def recycle_post(
    db: DB,
    current_user: CurrentUser,
    body: RecycleRequest,
    workspace_id: uuid.UUID = Query(...,
),
) -> ScheduledPostRead:
    """
    _credit_svc = CreditService(db=db)
    await _credit_svc.assert_can_schedule(user_id=current_user)
    Re-schedules an evergreen post's original content at the new time.
    Increments recycle_count and records last_recycled_at.
    """
    # Load candidate
    result = await db.execute(
        select(EvergreenCandidate).where(EvergreenCandidate.id == body.candidate_id)
    )
    candidate = result.scalar_one_or_none()
    if not candidate:
        raise HTTPException(status_code=404, detail="Evergreen candidate not found")

    # Load original scheduled post
    orig_result = await db.execute(
        select(ScheduledPost).where(ScheduledPost.id == candidate.scheduled_post_id)
    )
    original = orig_result.scalar_one_or_none()
    if not original:
        raise HTTPException(status_code=404, detail="Original post not found")

    # Create a fresh ScheduledPost with the same content snapshot
    from app.models.scheduler import PublishStatus as PS
    recycled = ScheduledPost(
        workspace_id=workspace_id,
        draft_content_id=original.draft_content_id,
        account_id=original.account_id,
        scheduled_by_id=current_user,
        scheduled_at=body.scheduled_at,
        publish_status=PS.SCHEDULED,
        is_ai_optimised_time=False,
        caption_snapshot=original.caption_snapshot,
        hashtags_snapshot=original.hashtags_snapshot,
        image_url_snapshot=original.image_url_snapshot,
    )
    db.add(recycled)

    from datetime import datetime, timezone
    candidate.last_recycled_at = datetime.now(timezone.utc)
    candidate.recycle_count += 1
    await db.flush()

    log.info("evergreen_recycled", candidate_id=str(body.candidate_id))
    return ScheduledPostRead.model_validate(recycled)
