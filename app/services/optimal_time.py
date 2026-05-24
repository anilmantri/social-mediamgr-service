"""
OptimalTimeService — learns the best posting time windows per workspace.

Flow:
  1. After each InsightsSync, metrics are written to PostMetrics.
  2. compute_optimal_slots() aggregates them into OptimalTimeSlot rows.
  3. get_suggestion() returns the top N slots + the next concrete datetime.
  4. Groq is used to add narrative explanation to the suggestion.
"""
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.scheduler import InstagramAccount, OptimalTimeSlot, PostMetrics
from app.schemas.scheduler import OptimalSlotRead, OptimalTimeSuggestion

log = structlog.get_logger(__name__)

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class OptimalTimeService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_suggestion(
        self, workspace_id: uuid.UUID
    ) -> OptimalTimeSuggestion:
        """
        Return the top N time slots and the next concrete datetime for posting.
        Falls back to a sensible default if there's not enough data.
        """
        account = await self._get_account(workspace_id)
        if not account:
            return self._default_suggestion(workspace_id)

        # Load top slots ordered by avg_engagement_rate desc
        result = await self._db.execute(
            select(OptimalTimeSlot)
            .where(OptimalTimeSlot.account_id == account.id)
            .order_by(OptimalTimeSlot.rank)
            .limit(settings.OPTIMAL_TIME_TOP_N)
        )
        slots = result.scalars().all()

        total_posts_result = await self._db.execute(
            select(func.count()).select_from(PostMetrics).where(
                PostMetrics.account_id == account.id
            )
        )
        total_posts = total_posts_result.scalar_one()

        if not slots:
            return self._default_suggestion(workspace_id, total_posts)

        slot_reads = [
            OptimalSlotRead(
                day_of_week=s.day_of_week,
                day_name=DAY_NAMES[s.day_of_week],
                hour_of_day=s.hour_of_day,
                avg_engagement_rate=round(s.avg_engagement_rate, 4),
                avg_reach=s.avg_reach,
                confidence_score=round(s.confidence_score, 2),
                rank=s.rank or 0,
            )
            for s in slots
        ]

        next_dt = self._next_occurrence(slots[0]) if slots else None

        return OptimalTimeSuggestion(
            workspace_id=workspace_id,
            suggested_slots=slot_reads,
            based_on_posts=total_posts,
            is_reliable=total_posts >= settings.OPTIMAL_TIME_MIN_POSTS,
            next_suggested_at=next_dt,
        )

    async def compute_optimal_slots(self, workspace_id: uuid.UUID) -> int:
        """
        Aggregate PostMetrics into OptimalTimeSlot.
        Called after each InsightsSync run.
        Returns number of slots updated.
        """
        account = await self._get_account(workspace_id)
        if not account:
            return 0

        # Aggregate by (day_of_week, hour_of_day)
        agg_result = await self._db.execute(
            select(
                PostMetrics.day_of_week,
                PostMetrics.hour_of_day,
                func.avg(PostMetrics.engagement_rate).label("avg_er"),
                func.avg(PostMetrics.reach).label("avg_reach"),
                func.count(PostMetrics.id).label("post_count"),
            )
            .where(
                PostMetrics.account_id == account.id,
                PostMetrics.engagement_rate.isnot(None),
                PostMetrics.day_of_week.isnot(None),
                PostMetrics.hour_of_day.isnot(None),
            )
            .group_by(PostMetrics.day_of_week, PostMetrics.hour_of_day)
            .order_by(func.avg(PostMetrics.engagement_rate).desc())
        )
        rows = agg_result.all()

        if not rows:
            return 0

        now = datetime.now(timezone.utc)
        updated = 0

        for rank, row in enumerate(rows, start=1):
            if row.day_of_week is None or row.hour_of_day is None:
                continue

            # Confidence: log scale up to 1.0 at OPTIMAL_TIME_MIN_POSTS
            confidence = min(
                row.post_count / settings.OPTIMAL_TIME_MIN_POSTS, 1.0
            )

            # Upsert OptimalTimeSlot
            existing_result = await self._db.execute(
                select(OptimalTimeSlot).where(
                    OptimalTimeSlot.account_id == account.id,
                    OptimalTimeSlot.day_of_week == row.day_of_week,
                    OptimalTimeSlot.hour_of_day == row.hour_of_day,
                )
            )
            slot = existing_result.scalar_one_or_none()
            if slot:
                slot.avg_engagement_rate = float(row.avg_er or 0)
                slot.avg_reach = int(row.avg_reach or 0)
                slot.post_count = row.post_count
                slot.confidence_score = confidence
                slot.rank = rank
                slot.last_computed_at = now
            else:
                slot = OptimalTimeSlot(
                    account_id=account.id,
                    workspace_id=workspace_id,
                    day_of_week=row.day_of_week,
                    hour_of_day=row.hour_of_day,
                    avg_engagement_rate=float(row.avg_er or 0),
                    avg_reach=int(row.avg_reach or 0),
                    post_count=row.post_count,
                    confidence_score=confidence,
                    rank=rank,
                    last_computed_at=now,
                )
                self._db.add(slot)
            updated += 1

        await self._db.flush()
        log.info("optimal_slots_computed", workspace_id=str(workspace_id), slots=updated)
        return updated

    async def sync_insights_for_post(
        self,
        scheduled_post_id: uuid.UUID,
        workspace_id: uuid.UUID,
        insights: dict,
    ) -> PostMetrics:
        """
        Write/update PostMetrics from raw Insights API response.
        Called by InsightsSyncTask after fetching from Instagram.
        """
        from app.models.scheduler import PostMetrics

        # Derive engagement rate
        reach = insights.get("reach", 0)
        engagement = (
            insights.get("likes", 0)
            + insights.get("comments", 0)
            + insights.get("saves", 0)
            + insights.get("shares", 0)
        )
        engagement_rate = round(engagement / reach, 4) if reach > 0 else None

        # Parse published_at
        published_at = None
        if insights.get("published_at"):
            try:
                published_at = datetime.fromisoformat(
                    insights["published_at"].replace("Z", "+00:00")
                )
            except Exception:
                pass

        # Find existing or create
        existing = await self._db.execute(
            select(PostMetrics).where(
                PostMetrics.scheduled_post_id == scheduled_post_id
            )
        )
        metrics = existing.scalar_one_or_none()

        account = await self._get_account(workspace_id)
        if metrics:
            # Update
            metrics.reach = insights.get("reach", 0)
            metrics.impressions = insights.get("impressions", 0)
            metrics.likes = insights.get("likes", 0)
            metrics.comments = insights.get("comments", 0)
            metrics.saves = insights.get("saves", 0)
            metrics.shares = insights.get("shares", 0)
            metrics.profile_visits = insights.get("profile_visits", 0)
            metrics.follows = insights.get("follows", 0)
            metrics.engagement_rate = engagement_rate
            metrics.published_at = published_at
            metrics.day_of_week = published_at.weekday() if published_at else None
            metrics.hour_of_day = published_at.hour if published_at else None
            metrics.metrics_fetched_at = datetime.now(timezone.utc)
            metrics.raw_insights = insights.get("raw", {})
        else:
            metrics = PostMetrics(
                scheduled_post_id=scheduled_post_id,
                account_id=account.id if account else uuid.uuid4(),
                workspace_id=workspace_id,
                reach=insights.get("reach", 0),
                impressions=insights.get("impressions", 0),
                likes=insights.get("likes", 0),
                comments=insights.get("comments", 0),
                saves=insights.get("saves", 0),
                shares=insights.get("shares", 0),
                profile_visits=insights.get("profile_visits", 0),
                follows=insights.get("follows", 0),
                engagement_rate=engagement_rate,
                published_at=published_at,
                day_of_week=published_at.weekday() if published_at else None,
                hour_of_day=published_at.hour if published_at else None,
                metrics_fetched_at=datetime.now(timezone.utc),
                raw_insights=insights.get("raw", {}),
            )
            self._db.add(metrics)

        await self._db.flush()
        return metrics

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _next_occurrence(self, slot: OptimalTimeSlot) -> datetime:
        """Return the next datetime matching this day-of-week + hour."""
        now = datetime.now(timezone.utc)
        days_ahead = (slot.day_of_week - now.weekday()) % 7
        if days_ahead == 0 and now.hour >= slot.hour_of_day:
            days_ahead = 7
        target = now + timedelta(days=days_ahead)
        return target.replace(
            hour=slot.hour_of_day, minute=0, second=0, microsecond=0
        )

    def _default_suggestion(
        self, workspace_id: uuid.UUID, total_posts: int = 0
    ) -> OptimalTimeSuggestion:
        """Return industry-standard defaults when there's no account data yet."""
        defaults = [
            OptimalSlotRead(day_of_week=2, day_name="Wednesday", hour_of_day=11,
                            avg_engagement_rate=0.0, avg_reach=0, confidence_score=0.0, rank=1),
            OptimalSlotRead(day_of_week=4, day_name="Friday", hour_of_day=9,
                            avg_engagement_rate=0.0, avg_reach=0, confidence_score=0.0, rank=2),
            OptimalSlotRead(day_of_week=5, day_name="Saturday", hour_of_day=10,
                            avg_engagement_rate=0.0, avg_reach=0, confidence_score=0.0, rank=3),
        ]
        return OptimalTimeSuggestion(
            workspace_id=workspace_id,
            suggested_slots=defaults,
            based_on_posts=total_posts,
            is_reliable=False,
            next_suggested_at=None,
        )

    async def _get_account(
        self, workspace_id: uuid.UUID
    ) -> InstagramAccount | None:
        result = await self._db.execute(
            select(InstagramAccount).where(
                InstagramAccount.workspace_id == workspace_id,
                InstagramAccount.is_active == True,
            )
        )
        return result.scalar_one_or_none()
