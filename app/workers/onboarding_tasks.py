"""
Module 12 Celery tasks:
  - send_day3_checkin       : daily — sends Day 3 email to users who signed up 3 days ago
  - send_day12_warning      : daily — sends upgrade nudge to users on day 12
  - send_first_pub_email    : triggered by publisher after first successful post
"""
from celery.utils.log import get_task_logger
from app.workers.tasks import celery_app, run_async

task_log = get_task_logger(__name__)


@celery_app.task(name="app.workers.onboarding_tasks.send_day3_checkin")
def send_day3_checkin() -> dict:
    """Daily — find users who signed up exactly 3 days ago and send check-in email."""
    task_log.info("send_day3_checkin: starting")

    async def _run() -> dict:
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select
        from app.db.session import AsyncSessionFactory
        from app.models.auth import User
        from app.models.content import Workspace
        from app.services.onboarding_service import OnboardingService

        sent = 0
        now = datetime.now(timezone.utc)
        three_days_ago_start = now - timedelta(days=3, hours=12)
        three_days_ago_end   = now - timedelta(days=2, hours=12)

        async with AsyncSessionFactory() as db:
            result = await db.execute(
                select(User).where(
                    User.created_at >= three_days_ago_start,
                    User.created_at <= three_days_ago_end,
                    User.is_active == True,
                )
            )
            users = result.scalars().all()

            for user in users:
                try:
                    ws_result = await db.execute(
                        select(Workspace).where(
                            Workspace.owner_id == user.id,
                            Workspace.is_active == True,
                        ).limit(1)
                    )
                    ws = ws_result.scalar_one_or_none()
                    if not ws:
                        continue

                    svc = OnboardingService(db=db)
                    await svc.send_day3_email(user, ws.id)
                    sent += 1
                except Exception as e:
                    task_log.warning(f"day3_email_failed user={user.id}: {e}")

        task_log.info(f"send_day3_checkin: sent={sent}")
        return {"sent": sent}

    return run_async(_run())


@celery_app.task(name="app.workers.onboarding_tasks.send_day12_warning")
def send_day12_warning() -> dict:
    """Daily — find users on day 12, send upgrade nudge."""
    task_log.info("send_day12_warning: starting")

    async def _run() -> dict:
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import select
        from app.db.session import AsyncSessionFactory
        from app.models.auth import User, Subscription, SubscriptionStatus, PlanTier
        from app.services.onboarding_service import OnboardingService

        sent = 0
        now = datetime.now(timezone.utc)
        day12_start = now - timedelta(days=12, hours=12)
        day12_end   = now - timedelta(days=11, hours=12)

        async with AsyncSessionFactory() as db:
            result = await db.execute(
                select(User).where(
                    User.created_at >= day12_start,
                    User.created_at <= day12_end,
                    User.is_active == True,
                )
            )
            users = result.scalars().all()

            for user in users:
                try:
                    # Only send to users still on free plan
                    sub_result = await db.execute(
                        select(Subscription).where(
                            Subscription.user_id == user.id,
                            Subscription.status == SubscriptionStatus.FREEMIUM,
                        )
                    )
                    if not sub_result.scalar_one_or_none():
                        continue

                    svc = OnboardingService(db=db)
                    await svc.send_day12_warning(user)
                    sent += 1
                except Exception as e:
                    task_log.warning(f"day12_email_failed user={user.id}: {e}")

        return {"sent": sent}

    return run_async(_run())


@celery_app.task(
    name="app.workers.onboarding_tasks.send_first_publish_celebration",
    bind=True,
)
def send_first_publish_celebration(self, user_id: str) -> dict:
    """Triggered by publisher after first successful post."""
    task_log.info(f"send_first_publish_celebration: user={user_id}")

    async def _run() -> dict:
        import uuid
        from sqlalchemy import select, func
        from app.db.session import AsyncSessionFactory
        from app.models.auth import User
        from app.models.content import DraftContent, ContentStatus, Workspace
        from app.services.onboarding_service import OnboardingService

        async with AsyncSessionFactory() as db:
            user_result = await db.execute(
                select(User).where(User.id == uuid.UUID(user_id))
            )
            user = user_result.scalar_one_or_none()
            if not user:
                return {"sent": False}

            # Only send if this is truly the first published post
            ws_result = await db.execute(
                select(Workspace).where(
                    Workspace.owner_id == user.id
                ).limit(1)
            )
            ws = ws_result.scalar_one_or_none()
            if not ws:
                return {"sent": False}

            count_result = await db.execute(
                select(func.count(DraftContent.id)).where(
                    DraftContent.workspace_id == ws.id,
                    DraftContent.status == ContentStatus.PUBLISHED,
                )
            )
            total_published = count_result.scalar_one()

            if total_published == 1:  # exactly first post
                svc = OnboardingService(db=db)
                await svc.send_first_publish_celebration(user)
                return {"sent": True}

        return {"sent": False}

    return run_async(_run())


# ── Beat schedule ─────────────────────────────────────────────────────────────
from celery.schedules import crontab  # noqa

celery_app.conf.beat_schedule.update({
    "onboarding-day3-checkin": {
        "task": "app.workers.onboarding_tasks.send_day3_checkin",
        "schedule": crontab(hour=10, minute=0),
        "options": {"queue": "beat"},
    },
    "onboarding-day12-warning": {
        "task": "app.workers.onboarding_tasks.send_day12_warning",
        "schedule": crontab(hour=10, minute=30),
        "options": {"queue": "beat"},
    },
})
