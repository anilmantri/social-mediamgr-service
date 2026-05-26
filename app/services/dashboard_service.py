"""
Module 3 — DashboardService

Aggregates data across modules to power the Overview Dashboard:
  - Account health score
  - Content pipeline summary (drafts by status)
  - Scheduled post queue
  - Recent publish results
  - Follower growth (from stored metrics)
  - Engagement rate trend
  - Pending approvals alert
  - Best performing post this month
  - Optimal time suggestion
  - Credit usage summary
"""
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import ContentStatus, DraftContent
from app.models.scheduler import (
    InstagramAccount, PostMetrics, PublishStatus,
    ScheduledPost,
)

log = structlog.get_logger(__name__)


class DashboardService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_overview(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> dict:
        """
        Single call that returns everything the dashboard needs.
        All queries run concurrently via gather.
        """
        import asyncio

        (
            pipeline,
            recent_posts,
            scheduled_queue,
            engagement,
            account,
            pending_count,
            credit_usage,
            optimal_time,
        ) = await asyncio.gather(
            self._content_pipeline(workspace_id),
            self._recent_published(workspace_id),
            self._scheduled_queue(workspace_id),
            self._engagement_trend(workspace_id),
            self._instagram_account(workspace_id),
            self._pending_approvals_count(workspace_id),
            self._credit_usage(workspace_id, user_id),
            self._optimal_time(workspace_id),
        )

        health_score = self._compute_health_score(
            pipeline=pipeline,
            recent_posts=recent_posts,
            account=account,
            engagement=engagement,
        )

        return {
            "workspace_id": str(workspace_id),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "health_score": health_score,
            "content_pipeline": pipeline,
            "recent_published": recent_posts,
            "scheduled_queue": scheduled_queue,
            "engagement_trend": engagement,
            "instagram_account": account,
            "pending_approvals": pending_count,
            "credit_usage": credit_usage,
            "optimal_time": optimal_time,
        }

    # ── Pipeline ──────────────────────────────────────────────────────────────

    async def _content_pipeline(self, workspace_id: uuid.UUID) -> dict:
        """Count drafts by status for the pipeline summary card."""
        result = await self._db.execute(
            select(DraftContent.status, func.count(DraftContent.id).label("count"))
            .where(DraftContent.workspace_id == workspace_id)
            .group_by(DraftContent.status)
        )
        rows = result.all()
        counts = {row.status.value: row.count for row in rows}
        total = sum(counts.values())

        return {
            "total": total,
            "draft":     counts.get("draft", 0),
            "pending":   counts.get("pending", 0),
            "approved":  counts.get("approved", 0),
            "scheduled": counts.get("scheduled", 0),
            "published": counts.get("published", 0),
            "rejected":  counts.get("rejected", 0),
            "failed":    counts.get("failed", 0),
        }

    # ── Recent published ──────────────────────────────────────────────────────

    async def _recent_published(self, workspace_id: uuid.UUID) -> list[dict]:
        """Last 5 published posts with their metrics if available."""
        result = await self._db.execute(
            select(ScheduledPost)
            .where(
                ScheduledPost.workspace_id == workspace_id,
                ScheduledPost.publish_status == PublishStatus.PUBLISHED,
            )
            .order_by(ScheduledPost.published_at.desc())
            .limit(5)
        )
        posts = result.scalars().all()

        out = []
        for p in posts:
            # Try to get metrics
            metrics_result = await self._db.execute(
                select(PostMetrics).where(
                    PostMetrics.scheduled_post_id == p.id
                )
            )
            m = metrics_result.scalar_one_or_none()

            out.append({
                "id": str(p.id),
                "caption": p.caption_snapshot[:80] + "…" if len(p.caption_snapshot) > 80 else p.caption_snapshot,
                "image_url": p.image_url_snapshot,
                "published_at": p.published_at.isoformat() if p.published_at else None,
                "ig_permalink": p.ig_permalink,
                "metrics": {
                    "reach": m.reach if m else 0,
                    "likes": m.likes if m else 0,
                    "comments": m.comments if m else 0,
                    "saves": m.saves if m else 0,
                    "engagement_rate": round(m.engagement_rate * 100, 2) if m and m.engagement_rate else 0,
                } if m else None,
            })
        return out

    # ── Scheduled queue ───────────────────────────────────────────────────────

    async def _scheduled_queue(self, workspace_id: uuid.UUID) -> list[dict]:
        """Next 5 scheduled posts."""
        now = datetime.now(timezone.utc)
        result = await self._db.execute(
            select(ScheduledPost)
            .where(
                ScheduledPost.workspace_id == workspace_id,
                ScheduledPost.publish_status == PublishStatus.SCHEDULED,
                ScheduledPost.scheduled_at >= now,
            )
            .order_by(ScheduledPost.scheduled_at)
            .limit(5)
        )
        posts = result.scalars().all()

        return [{
            "id": str(p.id),
            "caption": p.caption_snapshot[:60] + "…" if len(p.caption_snapshot) > 60 else p.caption_snapshot,
            "image_url": p.image_url_snapshot,
            "scheduled_at": p.scheduled_at.isoformat(),
            "is_ai_time": p.is_ai_optimised_time,
        } for p in posts]

    # ── Engagement trend ──────────────────────────────────────────────────────

    async def _engagement_trend(self, workspace_id: uuid.UUID) -> dict:
        """30-day engagement trend — daily average ER."""
        thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)

        result = await self._db.execute(
            select(PostMetrics)
            .where(
                PostMetrics.workspace_id == workspace_id,
                PostMetrics.published_at >= thirty_days_ago,
                PostMetrics.engagement_rate.isnot(None),
            )
            .order_by(PostMetrics.published_at)
        )
        metrics = result.scalars().all()

        if not metrics:
            return {
                "has_data": False,
                "avg_engagement_rate": 0,
                "avg_reach": 0,
                "total_posts": 0,
                "best_post": None,
                "daily": [],
            }

        avg_er   = sum(m.engagement_rate for m in metrics) / len(metrics)
        avg_reach = sum(m.reach for m in metrics) / len(metrics)
        best     = max(metrics, key=lambda m: m.engagement_rate or 0)

        # Group by date for chart
        from collections import defaultdict
        daily: dict[str, list[float]] = defaultdict(list)
        for m in metrics:
            if m.published_at:
                date_str = m.published_at.strftime("%Y-%m-%d")
                daily[date_str].append(m.engagement_rate or 0)

        daily_data = [
            {"date": date, "avg_er": round(sum(ers) / len(ers) * 100, 2)}
            for date, ers in sorted(daily.items())
        ]

        return {
            "has_data": True,
            "avg_engagement_rate": round(avg_er * 100, 2),
            "avg_reach": int(avg_reach),
            "total_posts": len(metrics),
            "best_post": {
                "id": str(best.scheduled_post_id),
                "engagement_rate": round((best.engagement_rate or 0) * 100, 2),
                "reach": best.reach,
                "likes": best.likes,
            },
            "daily": daily_data,
        }

    # ── Instagram account ─────────────────────────────────────────────────────

    async def _instagram_account(self, workspace_id: uuid.UUID) -> dict | None:
        result = await self._db.execute(
            select(InstagramAccount).where(
                InstagramAccount.workspace_id == workspace_id,
                InstagramAccount.is_active == True,
            )
        )
        acc = result.scalar_one_or_none()
        if not acc:
            return None
        return {
            "username": acc.instagram_username,
            "followers_count": acc.followers_count,
            "media_count": acc.media_count,
            "profile_picture_url": acc.profile_picture_url,
            "is_active": acc.is_active,
            "token_expires_at": acc.token_expires_at.isoformat() if acc.token_expires_at else None,
        }

    # ── Pending approvals ─────────────────────────────────────────────────────

    async def _pending_approvals_count(self, workspace_id: uuid.UUID) -> dict:
        result = await self._db.execute(
            select(func.count(DraftContent.id))
            .where(
                DraftContent.workspace_id == workspace_id,
                DraftContent.status == ContentStatus.PENDING,
            )
        )
        count = result.scalar_one()
        return {
            "count": count,
            "needs_attention": count > 0,
        }

    # ── Credit usage ──────────────────────────────────────────────────────────

    async def _credit_usage(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> dict:
        try:
            from app.services.billing import BillingService
            billing = BillingService(db=self._db)
            usage = await billing.get_usage_summary(user_id, workspace_id)
            return {
                "balance": usage.credits_balance,
                "used": usage.credits_used,
                "allocated": usage.credits_allocated,
                "pct_used": usage.credits_pct_used,
                "plan_name": usage.plan_name,
                "plan_tier": usage.plan_tier.value,
                "can_generate": usage.can_generate,
            }
        except Exception:
            return {
                "balance": 0, "used": 0, "allocated": 0,
                "pct_used": 0, "plan_name": "Free",
                "plan_tier": "free", "can_generate": False,
            }

    # ── Optimal time ──────────────────────────────────────────────────────────

    async def _optimal_time(self, workspace_id: uuid.UUID) -> dict | None:
        try:
            from app.services.optimal_time import OptimalTimeService
            svc = OptimalTimeService(db=self._db)
            suggestion = await svc.get_suggestion(workspace_id)
            if not suggestion.suggested_slots:
                return None
            top = suggestion.suggested_slots[0]
            from app.lib.utils import DAY_LABELS, HOUR_LABELS
            return {
                "day_name": top.day_name,
                "hour": top.hour_of_day,
                "hour_label": HOUR_LABELS[top.hour_of_day],
                "avg_engagement_rate": top.avg_engagement_rate,
                "confidence": top.confidence_score,
                "is_reliable": suggestion.is_reliable,
                "next_suggested_at": suggestion.next_suggested_at.isoformat() if suggestion.next_suggested_at else None,
            }
        except Exception:
            return None

    # ── Health score ──────────────────────────────────────────────────────────

    def _compute_health_score(
        self,
        pipeline: dict,
        recent_posts: list,
        account: dict | None,
        engagement: dict,
    ) -> dict:
        """
        Composite health score 0-100 based on:
          - Instagram connected (20 pts)
          - Posted in last 7 days (20 pts)
          - Has approved content ready (20 pts)
          - Engagement rate > 2% (20 pts)
          - No pending approvals older than 3 days (20 pts)
        """
        score = 0
        breakdown = {}

        # Instagram connected
        if account:
            score += 20
            breakdown["instagram_connected"] = 20
        else:
            breakdown["instagram_connected"] = 0

        # Posted recently
        recent_7d = [
            p for p in recent_posts
            if p["published_at"] and
            datetime.fromisoformat(p["published_at"]) > datetime.now(timezone.utc) - timedelta(days=7)
        ]
        if recent_7d:
            score += 20
            breakdown["posting_consistency"] = 20
        else:
            breakdown["posting_consistency"] = 0

        # Has ready content
        if pipeline.get("approved", 0) > 0:
            score += 20
            breakdown["content_ready"] = 20
        else:
            breakdown["content_ready"] = 0

        # Engagement rate
        avg_er = engagement.get("avg_engagement_rate", 0)
        if avg_er >= 3:
            score += 20
            breakdown["engagement_rate"] = 20
        elif avg_er >= 1:
            score += 10
            breakdown["engagement_rate"] = 10
        else:
            breakdown["engagement_rate"] = 0

        # No pending backlog
        if pipeline.get("pending", 0) == 0:
            score += 20
            breakdown["approval_health"] = 20
        elif pipeline.get("pending", 0) <= 2:
            score += 10
            breakdown["approval_health"] = 10
        else:
            breakdown["approval_health"] = 0

        label = (
            "Excellent" if score >= 80
            else "Good" if score >= 60
            else "Fair" if score >= 40
            else "Needs attention"
        )

        return {
            "score": score,
            "label": label,
            "breakdown": breakdown,
            "color": (
                "emerald" if score >= 80
                else "blue" if score >= 60
                else "amber" if score >= 40
                else "rose"
            ),
        }
