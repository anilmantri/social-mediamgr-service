"""
SchedulerService — manages the post scheduling lifecycle.

Responsibilities:
  - Validate draft is approved before scheduling
  - Snapshot content at scheduling time (so later edits don't affect live post)
  - Manage ScheduledPost records
  - Build calendar view (month/week)
  - Trigger optimal-time suggestions
  - Unschedule / reschedule
"""
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

import structlog
from sqlalchemy import and_, extract, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.content import ContentStatus, ContentVersion, DraftContent
from app.models.scheduler import InstagramAccount, PublishStatus, ScheduledPost
from app.schemas.scheduler import (
    CalendarDayRead,
    CalendarMonthRead,
    ScheduledPostRead,
)

log = structlog.get_logger(__name__)


class SchedulerService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Schedule ───────────────────────────────────────────────────────────────

    async def schedule_post(
            self,
            workspace_id: uuid.UUID,
            draft_content_id: uuid.UUID,
            scheduled_at: datetime,
            scheduled_by_id: uuid.UUID,
            use_optimal_time: bool = False,
            ai_time_confidence: float | None = None,
    ) -> ScheduledPost:
        """
        Validate, snapshot, and create a ScheduledPost record.
        Raises ValueError on any preflight failure.
        """
        draft = await self._load_approved_draft(draft_content_id, workspace_id)
        account = await self._load_active_account(workspace_id)
        current_version = await self._load_current_version(draft)

        # Guard: not already scheduled
        existing = await self._db.execute(
            select(ScheduledPost).where(
                ScheduledPost.draft_content_id == draft_content_id,
                ScheduledPost.publish_status.in_([
                    PublishStatus.SCHEDULED,
                    PublishStatus.PUBLISHING,
                ]),
                )
        )
        if existing.scalar_one_or_none():
            raise ValueError("This post is already scheduled. Unschedule it first.")

        # Resolve optimal time if requested
        final_time = scheduled_at
        if use_optimal_time:
            from app.services.optimal_time import OptimalTimeService
            ot_svc = OptimalTimeService(self._db)
            suggestion = await ot_svc.get_suggestion(workspace_id)
            if suggestion.next_suggested_at:
                final_time = suggestion.next_suggested_at
                ai_time_confidence = (
                    suggestion.suggested_slots[0].confidence_score
                    if suggestion.suggested_slots else None
                )

        scheduled_post = ScheduledPost(
            workspace_id=workspace_id,
            draft_content_id=draft_content_id,
            account_id=account.id,
            scheduled_by_id=scheduled_by_id,
            scheduled_at=final_time,
            publish_status=PublishStatus.SCHEDULED,
            is_ai_optimised_time=use_optimal_time,
            ai_time_confidence=ai_time_confidence,
            # Content snapshot — frozen at scheduling time
            caption_snapshot=current_version.caption,
            hashtags_snapshot=current_version.hashtags,
            image_url_snapshot=current_version.image_url,
        )
        self._db.add(scheduled_post)

        # Update draft status
        draft.status = ContentStatus.SCHEDULED
        draft.scheduled_at = final_time
        await self._db.flush()

        log.info(
            "post_scheduled",
            draft_id=str(draft_content_id),
            scheduled_at=final_time.isoformat(),
            optimal=use_optimal_time,
        )
        return scheduled_post

    async def unschedule_post(
            self, scheduled_post_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> ScheduledPost:
        """Cancel a scheduled post. Returns it to APPROVED status."""
        sp = await self._load_scheduled_post(scheduled_post_id, workspace_id)

        if sp.publish_status == PublishStatus.PUBLISHING:
            raise ValueError("Cannot unschedule a post that is currently being published.")
        if sp.publish_status == PublishStatus.PUBLISHED:
            raise ValueError("Cannot unschedule a post that has already been published.")

        sp.publish_status = PublishStatus.UNSCHEDULED

        # Revert draft status
        draft_result = await self._db.execute(
            select(DraftContent).where(DraftContent.id == sp.draft_content_id)
        )
        draft = draft_result.scalar_one_or_none()
        if draft:
            draft.status = ContentStatus.APPROVED
            draft.scheduled_at = None

        await self._db.flush()
        log.info("post_unscheduled", scheduled_post_id=str(scheduled_post_id))
        return sp

    async def reschedule_post(
            self,
            scheduled_post_id: uuid.UUID,
            workspace_id: uuid.UUID,
            new_scheduled_at: datetime,
    ) -> ScheduledPost:
        """Move a scheduled post to a new time."""
        sp = await self._load_scheduled_post(scheduled_post_id, workspace_id)

        if sp.publish_status not in (PublishStatus.SCHEDULED, PublishStatus.FAILED):
            raise ValueError(
                f"Cannot reschedule a post with status '{sp.publish_status.value}'."
            )

        sp.scheduled_at = new_scheduled_at
        sp.publish_status = PublishStatus.SCHEDULED
        sp.next_retry_at = None

        draft_result = await self._db.execute(
            select(DraftContent).where(DraftContent.id == sp.draft_content_id)
        )
        draft = draft_result.scalar_one_or_none()
        if draft:
            draft.scheduled_at = new_scheduled_at
            draft.status = ContentStatus.SCHEDULED

        await self._db.flush()
        log.info(
            "post_rescheduled",
            scheduled_post_id=str(scheduled_post_id),
            new_time=new_scheduled_at.isoformat(),
        )
        return sp

    # ── Queries ────────────────────────────────────────────────────────────────

    async def list_scheduled_posts(
            self,
            workspace_id: uuid.UUID,
            status: PublishStatus | None = None,
            from_dt: datetime | None = None,
            to_dt: datetime | None = None,
            page: int = 1,
            page_size: int = 20,
    ) -> tuple[list[ScheduledPost], int]:
        from sqlalchemy import func

        query = select(ScheduledPost).where(
            ScheduledPost.workspace_id == workspace_id
        )
        if status:
            query = query.where(ScheduledPost.publish_status == status)
        if from_dt:
            query = query.where(ScheduledPost.scheduled_at >= from_dt)
        if to_dt:
            query = query.where(ScheduledPost.scheduled_at <= to_dt)

        count_result = await self._db.execute(
            select(func.count()).select_from(query.subquery())
        )
        total = count_result.scalar_one()

        query = (
            query.order_by(ScheduledPost.scheduled_at)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self._db.execute(query)
        return result.scalars().all(), total

    async def get_calendar_month(
            self, workspace_id: uuid.UUID, year: int, month: int
    ) -> CalendarMonthRead:
        """Build a full month calendar view with scheduled posts per day."""
        from datetime import date as date_cls
        days_in_month = monthrange(year, month)[1]
        month_start = datetime(year, month, 1, tzinfo=timezone.utc)
        month_end = datetime(year, month, days_in_month, 23, 59, 59, tzinfo=timezone.utc)

        posts, total = await self.list_scheduled_posts(
            workspace_id=workspace_id,
            from_dt=month_start,
            to_dt=month_end,
            page=1,
            page_size=200,
        )

        # Group by day
        posts_by_day: dict[str, list[ScheduledPost]] = {}
        for p in posts:
            day_key = p.scheduled_at.strftime("%Y-%m-%d")
            posts_by_day.setdefault(day_key, []).append(p)

        # Count approved (unscheduled) drafts this month
        draft_count_result = await self._db.execute(
            select(DraftContent).where(
                DraftContent.workspace_id == workspace_id,
                DraftContent.status == ContentStatus.APPROVED,
                )
        )
        approved_drafts = len(draft_count_result.scalars().all())

        # Identify top-5 engagement days from OptimalTimeSlot
        from app.services.optimal_time import OptimalTimeService
        ot_svc = OptimalTimeService(self._db)
        top_days: set[int] = set()
        try:
            suggestion = await ot_svc.get_suggestion(workspace_id)
            top_days = {s.day_of_week for s in suggestion.suggested_slots[:5]}
        except Exception:
            pass

        calendar_days = []
        for day_num in range(1, days_in_month + 1):
            day_dt = date_cls(year, month, day_num)
            day_key = day_dt.strftime("%Y-%m-%d")
            day_posts = posts_by_day.get(day_key, [])
            calendar_days.append(
                CalendarDayRead(
                    date=day_key,
                    scheduled_posts=[
                        ScheduledPostRead.model_validate(p) for p in day_posts
                    ],
                    draft_count=approved_drafts if day_num == 1 else 0,
                    is_optimal_day=day_dt.weekday() in top_days,
                )
            )

        published = sum(1 for p in posts if p.publish_status == PublishStatus.PUBLISHED)
        failed = sum(1 for p in posts if p.publish_status == PublishStatus.FAILED)

        return CalendarMonthRead(
            workspace_id=workspace_id,
            year=year,
            month=month,
            days=calendar_days,
            total_scheduled=total,
            total_published=published,
            total_failed=failed,
        )

    # ── Due posts (called by Celery beat) ─────────────────────────────────────

    async def get_due_posts(self) -> list[ScheduledPost]:
        """
        Return all posts that are due within the next tick window.
        Called every SCHEDULER_TICK_SECONDS by the beat task.
        """
        now = datetime.now(timezone.utc)
        tick_window = now + timedelta(seconds=settings.SCHEDULER_TICK_SECONDS)

        result = await self._db.execute(
            select(ScheduledPost)
            .where(
                ScheduledPost.publish_status == PublishStatus.SCHEDULED,
                ScheduledPost.scheduled_at <= tick_window,
                )
            .options(selectinload(ScheduledPost.account))
        )
        return result.scalars().all()

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _load_approved_draft(
            self, draft_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> DraftContent:
        result = await self._db.execute(
            select(DraftContent).where(
                DraftContent.id == draft_id,
                DraftContent.workspace_id == workspace_id,
                )
        )
        draft = result.scalar_one_or_none()
        if not draft:
            raise ValueError(f"Draft {draft_id} not found in workspace {workspace_id}")
        if draft.status != ContentStatus.APPROVED:
            raise ValueError(
                f"Draft must be APPROVED to schedule. Current status: {draft.status.value}"
            )
        return draft

    async def _load_active_account(
            self, workspace_id: uuid.UUID
    ) -> InstagramAccount:
        result = await self._db.execute(
            select(InstagramAccount).where(
                InstagramAccount.workspace_id == workspace_id,
                InstagramAccount.is_active == True,
                )
        )
        account = result.scalar_one_or_none()
        if not account:
            raise ValueError(
                "No active Instagram account connected to this workspace. "
                "Connect via GET /api/v1/instagram/connect"
            )
        return account

    async def _load_current_version(self, draft: DraftContent) -> ContentVersion:
        if not draft.current_version_id:
            raise ValueError("Draft has no content version. Generate content first.")
        result = await self._db.execute(
            select(ContentVersion).where(
                ContentVersion.id == draft.current_version_id
            )
        )
        version = result.scalar_one_or_none()
        if not version:
            raise ValueError("Current content version not found.")
        if not version.image_url:
            raise ValueError(
                "Draft has no image. Generate or upload an image before scheduling."
            )
        return version

    async def _load_scheduled_post(
            self, scheduled_post_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> ScheduledPost:
        result = await self._db.execute(
            select(ScheduledPost).where(
                ScheduledPost.id == scheduled_post_id,
                ScheduledPost.workspace_id == workspace_id,
                )
        )
        sp = result.scalar_one_or_none()
        if not sp:
            raise ValueError(f"Scheduled post {scheduled_post_id} not found")
        return sp