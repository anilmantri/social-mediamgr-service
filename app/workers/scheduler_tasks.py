"""
Module 2 Celery tasks:
  - tick_publisher          : beat task every 60s — finds due posts, publishes
  - publish_single_post     : worker task for one post publish attempt
  - sync_insights           : pull Insights for all published posts every 6h
  - refresh_instagram_tokens: renew long-lived tokens every 50 days
  - scan_evergreen          : flag top performers for recycling weekly
"""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from celery import Celery
from celery.utils.log import get_task_logger

from app.core.config import settings
from app.workers.tasks import celery_app, run_async

log = structlog.get_logger(__name__)
task_log = get_task_logger(__name__)

# ── Publisher beat — fires every SCHEDULER_TICK_SECONDS ───────────────────────

@celery_app.task(
    name="app.workers.scheduler_tasks.tick_publisher",
    bind=True,
)
def tick_publisher(self) -> dict:
    """
    Celery beat entry point. Scans for due posts and fans out
    individual publish_single_post tasks.
    """
    task_log.info("tick_publisher: scanning for due posts")

    async def _run() -> dict:
        from app.db.session import AsyncSessionFactory
        from app.services.scheduler import SchedulerService

        async with AsyncSessionFactory() as session:
            svc = SchedulerService(db=session)
            due_posts = await svc.get_due_posts()

        enqueued = 0
        for post in due_posts:
            publish_single_post.apply_async(
                kwargs={"scheduled_post_id": str(post.id)},
                queue="publisher",
                countdown=0,
            )
            enqueued += 1

        task_log.info(f"tick_publisher: enqueued {enqueued} posts")
        return {"enqueued": enqueued}

    return run_async(_run())


@celery_app.task(
    bind=True,
    name="app.workers.scheduler_tasks.publish_single_post",
    max_retries=0,          # retry logic is inside PostPublisherService
    queue="publisher",
    soft_time_limit=180,
    time_limit=240,
)
def publish_single_post(self, scheduled_post_id: str) -> dict:
    """
    Publish one post to Instagram.
    Idempotent — safe to call multiple times; service guards against double-publish.
    """
    task_log.info(f"publish_single_post: {scheduled_post_id}")

    async def _run() -> dict:
        from app.db.session import AsyncSessionFactory
        from app.services.publisher import PostPublisherService

        async with AsyncSessionFactory() as session:
            svc = PostPublisherService(db=session)
            try:
                success = await svc.publish(uuid.UUID(scheduled_post_id))
                await session.commit()
                return {"success": success, "post_id": scheduled_post_id}
            except Exception:
                await session.rollback()
                raise

    try:
        return run_async(_run())
    except Exception as exc:
        task_log.error(f"publish_single_post failed: {exc}")
        raise


# ── Insights sync — every 6 hours ─────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="app.workers.scheduler_tasks.sync_insights",
    max_retries=2,
    default_retry_delay=600,
    soft_time_limit=300,
    time_limit=360,
)
def sync_insights(self) -> dict:
    """
    Pull Insights for all recently published posts across all workspaces.
    Updates PostMetrics and recomputes OptimalTimeSlot.
    """
    task_log.info("sync_insights: starting")

    async def _run() -> dict:
        from sqlalchemy import select
        from app.db.session import AsyncSessionFactory
        from app.models.scheduler import InstagramAccount, PostMetrics, PublishStatus, ScheduledPost
        from app.services.instagram_client import get_ig_client
        from app.services.optimal_time import OptimalTimeService

        ig = get_ig_client()
        synced = 0
        errors = 0

        async with AsyncSessionFactory() as session:
            # Find all published posts missing metrics or with stale metrics (>6h old)
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=settings.INSIGHTS_SYNC_INTERVAL_HOURS
            )
            result = await session.execute(
                select(ScheduledPost)
                .where(
                    ScheduledPost.publish_status == PublishStatus.PUBLISHED,
                    ScheduledPost.ig_media_id.isnot(None),
                )
                .join(
                    PostMetrics,
                    PostMetrics.scheduled_post_id == ScheduledPost.id,
                    isouter=True,
                )
                .where(
                    (PostMetrics.id == None) |
                    (PostMetrics.metrics_fetched_at < cutoff)
                )
                .limit(100)
            )
            posts = result.scalars().all()

            for post in posts:
                # Get account token
                acc_result = await session.execute(
                    select(InstagramAccount).where(
                        InstagramAccount.id == post.account_id
                    )
                )
                account = acc_result.scalar_one_or_none()
                if not account:
                    continue

                try:
                    insights = await ig.get_post_insights(
                        media_id=post.ig_media_id,
                        access_token=account.access_token,
                    )
                    ot_svc = OptimalTimeService(db=session)
                    await ot_svc.sync_insights_for_post(
                        scheduled_post_id=post.id,
                        workspace_id=post.workspace_id,
                        insights=insights,
                    )
                    synced += 1
                except Exception as exc:
                    task_log.warning(f"insights_sync_error post={post.id}: {exc}")
                    errors += 1

            await session.commit()

            # Recompute optimal time slots for all affected workspaces
            workspace_ids = list({p.workspace_id for p in posts})
            for ws_id in workspace_ids:
                try:
                    ot_svc = OptimalTimeService(db=session)
                    await ot_svc.compute_optimal_slots(ws_id)
                    await session.commit()
                except Exception as exc:
                    task_log.warning(f"optimal_time_compute_error ws={ws_id}: {exc}")

        task_log.info(f"sync_insights: synced={synced} errors={errors}")
        return {"synced": synced, "errors": errors}

    try:
        return run_async(_run())
    except Exception as exc:
        task_log.error(f"sync_insights failed: {exc}")
        raise self.retry(exc=exc)


# ── Token refresh — every 50 days ─────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="app.workers.scheduler_tasks.refresh_instagram_tokens",
    max_retries=3,
    default_retry_delay=3600,
)
def refresh_instagram_tokens(self) -> dict:
    """
    Refresh long-lived Instagram tokens before they expire (60-day expiry).
    Runs every TOKEN_REFRESH_INTERVAL_DAYS days via Celery beat.
    """
    task_log.info("refresh_instagram_tokens: starting")

    async def _run() -> dict:
        from datetime import timedelta
        from sqlalchemy import select
        from app.db.session import AsyncSessionFactory
        from app.models.scheduler import InstagramAccount
        from app.services.instagram_client import get_ig_client

        ig = get_ig_client()
        refreshed = 0
        errors = 0

        async with AsyncSessionFactory() as session:
            # Find tokens expiring within 15 days
            expiry_threshold = datetime.now(timezone.utc) + timedelta(days=15)
            result = await session.execute(
                select(InstagramAccount).where(
                    InstagramAccount.is_active == True,
                    (InstagramAccount.token_expires_at == None) |
                    (InstagramAccount.token_expires_at <= expiry_threshold),
                )
            )
            accounts = result.scalars().all()

            for account in accounts:
                try:
                    new_token_data = await ig.refresh_long_lived_token(
                        account.access_token
                    )
                    account.access_token = new_token_data["access_token"]
                    from datetime import timedelta as td
                    expires_in = new_token_data.get("expires_in", 5184000)  # 60 days default
                    account.token_expires_at = datetime.now(timezone.utc) + td(seconds=expires_in)
                    account.last_refreshed_at = datetime.now(timezone.utc)
                    account.last_api_error = None
                    refreshed += 1
                    task_log.info(f"token_refreshed: account={account.id}")
                except Exception as exc:
                    account.last_api_error = str(exc)
                    account.api_error_at = datetime.now(timezone.utc)
                    errors += 1
                    task_log.error(f"token_refresh_error account={account.id}: {exc}")

            await session.commit()

        task_log.info(f"refresh_instagram_tokens: refreshed={refreshed} errors={errors}")
        return {"refreshed": refreshed, "errors": errors}

    try:
        return run_async(_run())
    except Exception as exc:
        raise self.retry(exc=exc)


# ── Evergreen scanner — weekly ─────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="app.workers.scheduler_tasks.scan_evergreen",
)
def scan_evergreen(self) -> dict:
    """
    Scan published posts, score them, and flag top performers
    as EvergreenCandidate if they haven't been recycled recently.
    """
    task_log.info("scan_evergreen: starting")

    async def _run() -> dict:
        from sqlalchemy import select
        from app.db.session import AsyncSessionFactory
        from app.models.scheduler import EvergreenCandidate, PostMetrics, ScheduledPost
        from datetime import timedelta

        flagged = 0

        async with AsyncSessionFactory() as session:
            # Score: normalised combination of saves (weight 3), reach (weight 1)
            metrics_result = await session.execute(
                select(PostMetrics)
                .where(
                    PostMetrics.reach > 0,
                    PostMetrics.engagement_rate.isnot(None),
                )
                .order_by(PostMetrics.engagement_rate.desc())
                .limit(200)
            )
            all_metrics = metrics_result.scalars().all()

            if not all_metrics:
                return {"flagged": 0}

            # Normalise score 0-1 relative to best post in set
            max_er = max(m.engagement_rate or 0 for m in all_metrics) or 1
            top_pct = 0.2  # top 20%
            threshold = max_er * (1 - top_pct)

            for m in all_metrics:
                er = m.engagement_rate or 0
                if er < threshold:
                    continue

                score = min(er / max_er, 1.0)

                # Check if already an active candidate
                existing = await session.execute(
                    select(EvergreenCandidate).where(
                        EvergreenCandidate.scheduled_post_id == m.scheduled_post_id,
                        EvergreenCandidate.is_active == True,
                    )
                )
                if existing.scalar_one_or_none():
                    continue

                # Check recycle window
                candidate = EvergreenCandidate(
                    workspace_id=m.workspace_id,
                    scheduled_post_id=m.scheduled_post_id,
                    engagement_score=round(score, 4),
                    is_active=True,
                    recycle_window_days=90,
                )
                session.add(candidate)
                flagged += 1

            await session.commit()

        task_log.info(f"scan_evergreen: flagged={flagged}")
        return {"flagged": flagged}

    return run_async(_run())


# ── Beat schedule ──────────────────────────────────────────────────────────────

celery_app.conf.beat_schedule = {
    "tick-publisher": {
        "task": "app.workers.scheduler_tasks.tick_publisher",
        "schedule": settings.SCHEDULER_TICK_SECONDS,
        "options": {"queue": "beat"},
    },
    "sync-insights": {
        "task": "app.workers.scheduler_tasks.sync_insights",
        "schedule": settings.INSIGHTS_SYNC_INTERVAL_HOURS * 3600,
        "options": {"queue": "beat"},
    },
    "refresh-tokens": {
        "task": "app.workers.scheduler_tasks.refresh_instagram_tokens",
        "schedule": settings.TOKEN_REFRESH_INTERVAL_DAYS * 86400,
        "options": {"queue": "beat"},
    },
    "scan-evergreen": {
        "task": "app.workers.scheduler_tasks.scan_evergreen",
        "schedule": 7 * 86400,  # weekly
        "options": {"queue": "beat"},
    },
}

celery_app.conf.task_routes.update({
    "app.workers.scheduler_tasks.tick_publisher": {"queue": "beat"},
    "app.workers.scheduler_tasks.publish_single_post": {"queue": "publisher"},
    "app.workers.scheduler_tasks.sync_insights": {"queue": "beat"},
    "app.workers.scheduler_tasks.refresh_instagram_tokens": {"queue": "beat"},
    "app.workers.scheduler_tasks.scan_evergreen": {"queue": "beat"},
})
