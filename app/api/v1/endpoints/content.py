"""
API Router — Module 1: AI Content Engine

Endpoints:
  POST   /content/generate             → initiate generation job
  GET    /content/{id}                 → get draft detail
  GET    /content                      → list drafts (filterable)
  POST   /content/{id}/submit          → submit for review
  POST   /content/{id}/approve         → approve
  POST   /content/{id}/reject          → reject
  PATCH  /content/{id}/edit            → edit content (creates new version)
  POST   /content/{id}/regenerate      → re-generate caption/image
  POST   /content/{id}/comment         → add comment
  POST   /content/calendar/plan        → initiate 30-day calendar generation
  GET    /jobs/{id}                    → poll job status
  PUT    /brand-profile/{workspace_id} → upsert brand profile
  GET    /brand-profile/{workspace_id} → get brand profile
  POST   /brand-profile/{workspace_id}/score → score content against brand voice
"""
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.content import ContentStatus, GenerationJob, JobStatus
from app.schemas.content import (
    ApprovalActionResponse,
    ApproveRequest,
    BrandProfileUpsert,
    BrandVoiceScoreResponse,
    CalendarPlanRequest,
    CalendarPlanResponse,
    ContentGenerationRequest,
    ContentGenerationResponse,
    ContentGenerationResult,
    DraftContentList,
    DraftContentRead,
    EditRequest,
    JobStatusResponse,
    RejectRequest,
    RegenerateRequest,
)
from app.services.approval import ApprovalService
from app.services.brand_voice import BrandVoiceService
from app.services.content_generation import ContentGenerationService

from app.core.auth_deps import get_current_user_id as _real_get_current_user_id
from app.services.billing import BillingService
from app.models.auth import CreditActionType

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Content Engine"])

CurrentUser = Annotated[uuid.UUID, Depends(_real_get_current_user_id)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ── Content Generation ─────────────────────────────────────────────────────────

@router.post(
    "/content/generate",
    response_model=ContentGenerationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Initiate AI content generation",
    description=(
            "Queues an async job to generate caption and optionally an image. "
            "Returns immediately with a job_id. Poll /jobs/{job_id} for completion."
    ),
)
async def generate_content(
        db: DB,
        current_user: CurrentUser,
        request: ContentGenerationRequest,
) -> ContentGenerationResponse:
    # Credit gate — check balance before queuing AI job
    action = (
        CreditActionType.CONTENT_GENERATE if request.include_image
        else CreditActionType.CAPTION_GENERATE
    )
    billing = BillingService(db=db)
    await billing.assert_can_generate(
        user_id=current_user,
        workspace_id=request.workspace_id,
        action_type=action,
    )
    service = ContentGenerationService(db=db)
    try:
        return await service.initiate_generation(
            request=request, initiated_by_id=current_user
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        log.exception("generate_content_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initiate content generation",
        )


@router.get(
    "/content",
    response_model=DraftContentList,
    summary="List draft content",
)
async def list_content(
        db: DB,
        current_user: CurrentUser,
        workspace_id: uuid.UUID = Query(...,
                                        ),
        content_status: ContentStatus | None = Query(None, alias="status"),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
) -> DraftContentList:
    service = ApprovalService(db=db)
    drafts, total = await service.list_drafts(
        workspace_id=workspace_id,
        status=content_status,
        page=page,
        page_size=page_size,
    )

    items = []
    for draft in drafts:
        current_ver = None
        if draft.current_version_id and draft.versions:
            current_ver = next(
                (v for v in draft.versions if v.id == draft.current_version_id), None
            )
        items.append(
            DraftContentRead(
                **draft.to_dict(),
                current_version=current_ver,
                approval_actions=draft.approval_actions,
            )
        )

    return DraftContentList(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_next=(page * page_size) < total,
    )


@router.get(
    "/content/{draft_id}",
    response_model=DraftContentRead,
    summary="Get draft content detail with all versions",
)
async def get_content(
        db: DB,
        current_user: CurrentUser,
        draft_id: uuid.UUID,
) -> DraftContentRead:
    service = ApprovalService(db=db)
    try:
        draft = await service.get_draft_detail(draft_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found")

    current_ver = None
    if draft.current_version_id:
        current_ver = next(
            (v for v in draft.versions if v.id == draft.current_version_id), None
        )

    return DraftContentRead(
        **draft.to_dict(),
        current_version=current_ver,
        approval_actions=draft.approval_actions,
    )


# ── Approval Workflow ──────────────────────────────────────────────────────────

@router.post(
    "/content/{draft_id}/submit",
    response_model=ApprovalActionResponse,
    summary="Submit draft for review",
)
async def submit_for_review(
        db: DB,
        current_user: CurrentUser,
        draft_id: uuid.UUID,
        body: ApproveRequest = Body(default=ApproveRequest()),
) -> ApprovalActionResponse:
    service = ApprovalService(db=db)
    try:
        return await service.submit_for_review(
            draft_id=draft_id, actor_id=current_user, comment=body.comment
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post(
    "/content/{draft_id}/approve",
    response_model=ApprovalActionResponse,
    summary="Approve content for scheduling",
)
async def approve_content(
        db: DB,
        current_user: CurrentUser,
        draft_id: uuid.UUID,
        body: ApproveRequest = Body(default=ApproveRequest()),
) -> ApprovalActionResponse:
    service = ApprovalService(db=db)
    try:
        return await service.approve(
            draft_id=draft_id, actor_id=current_user, comment=body.comment
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post(
    "/content/{draft_id}/reject",
    response_model=ApprovalActionResponse,
    summary="Reject content with reason",
)
async def reject_content(
        db: DB,
        current_user: CurrentUser,
        draft_id: uuid.UUID,
        body: RejectRequest,
) -> ApprovalActionResponse:
    service = ApprovalService(db=db)
    try:
        return await service.reject(
            draft_id=draft_id,
            actor_id=current_user,
            reason=body.reason,
            request_revision=body.request_revision,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.patch(
    "/content/{draft_id}/edit",
    response_model=ApprovalActionResponse,
    summary="Edit content — creates new version, returns to pending",
)
async def edit_content(
        db: DB,
        current_user: CurrentUser,
        draft_id: uuid.UUID,
        body: EditRequest,
) -> ApprovalActionResponse:
    service = ApprovalService(db=db)
    try:
        return await service.edit_content(
            draft_id=draft_id, actor_id=current_user, edit=body
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post(
    "/content/{draft_id}/regenerate",
    response_model=ContentGenerationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Regenerate caption and/or image",
)
async def regenerate_content(
        db: DB,
        current_user: CurrentUser,
        draft_id: uuid.UUID,
        body: RegenerateRequest,
) -> dict:
    service = ApprovalService(db=db)
    try:
        return await service.request_regeneration(
            draft_id=draft_id, actor_id=current_user, regen_request=body
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post(
    "/content/{draft_id}/comment",
    response_model=ApprovalActionResponse,
    summary="Add a comment to a draft",
)
async def add_comment(
        db: DB,
        current_user: CurrentUser,
        draft_id: uuid.UUID,
        comment: Annotated[str, Body(embed=True, min_length=1, max_length=1000,
                                     )],
) -> ApprovalActionResponse:
    service = ApprovalService(db=db)
    try:
        return await service.add_comment(
            draft_id=draft_id, actor_id=current_user, comment=comment
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


# ── Calendar ───────────────────────────────────────────────────────────────────

@router.post(
    "/content/calendar/plan",
    response_model=CalendarPlanResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate a 30-day content calendar",
    description="Queues batch generation of an entire month's content plan.",
)
async def plan_calendar(
        db: DB,
        current_user: CurrentUser,
        request: CalendarPlanRequest,
) -> CalendarPlanResponse:
    service = ContentGenerationService(db=db)
    try:
        result = await service.initiate_calendar_plan(
            request=request, initiated_by_id=current_user
        )
        return CalendarPlanResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


# ── Job polling ────────────────────────────────────────────────────────────────

@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    summary="Poll generation job status",
)
async def get_job_status(
        db: DB,
        current_user: CurrentUser,
        job_id: uuid.UUID,
) -> JobStatusResponse:
    from sqlalchemy import select
    result = await db.execute(
        select(GenerationJob).where(GenerationJob.id == job_id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    progress = None
    if job.status == JobStatus.RUNNING:
        progress = 50
    elif job.status == JobStatus.SUCCESS:
        progress = 100

    return JobStatusResponse(
        job_id=job.id,
        job_type=job.job_type,
        status=job.status,
        attempt_count=job.attempt_count,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        result=job.result,
        error_message=job.error_message,
        progress_pct=progress,
    )


# ── Brand Voice ────────────────────────────────────────────────────────────────

@router.put(
    "/brand-profile/{workspace_id}",
    summary="Create or update brand voice profile",
)
async def upsert_brand_profile(
        db: DB,
        current_user: CurrentUser,
        workspace_id: uuid.UUID,
        body: BrandProfileUpsert,
) -> dict:
    service = BrandVoiceService(db=db)
    profile = await service.upsert_profile(str(workspace_id), body)
    return {"id": str(profile.id), "workspace_id": str(workspace_id), "niche": profile.niche}


class BrandVoiceScoreRequest(BaseModel):
    caption: str
    hashtags: list[str] = []


@router.post(
    "/brand-profile/{workspace_id}/score",
    response_model=BrandVoiceScoreResponse,
    summary="Score content against brand voice",
)
async def score_brand_voice(
        db: DB,
        current_user: CurrentUser,
        workspace_id: uuid.UUID,
        body: BrandVoiceScoreRequest,
) -> BrandVoiceScoreResponse:
    service = BrandVoiceService(db=db)
    return await service.score_content(
        workspace_id=str(workspace_id),
        caption=body.caption,
        hashtags=body.hashtags,
    )