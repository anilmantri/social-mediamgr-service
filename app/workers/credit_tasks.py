"""
Module 9 Celery tasks:
  - monthly_credit_reset   : runs on 1st of every month — resets all workspaces
  - check_expired_subs     : daily — marks expired subscriptions, downgrades to free
  - send_low_credit_alerts : daily — emails users below 20% credit threshold
"""
import uuid
from datetime import datetime, timezone

from celery.utils.log import get_task_logger

from app.workers.tasks import celery_app, run_async

task_log = get_task_logger(__name__)


@celery_app.task(
    name="app.workers.credit_tasks.monthly_credit_reset",
    bind=True,
    max_retries=3,
)
def monthly_credit_reset(self) -> dict:
    """
    Runs on the 1st of every month.
    Resets credit balance for every active workspace to their plan allocation.
    """
    task_log.info("monthly_credit_reset: starting")

    async def _run() -> dict:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from app.db.session import AsyncSessionFactory
        from app.models.auth import CreditBalance, Subscription, SubscriptionStatus
        from app.models.content import Workspace, WorkspaceMember
        from app.services.credit_service import CreditService

        reset_count = 0
        error_count = 0

        async with AsyncSessionFactory() as db:
            # Get all active subscriptions
            result = await db.execute(
                select(Subscription)
                .where(
                    Subscription.status.in_([
                        SubscriptionStatus.ACTIVE,
                        SubscriptionStatus.TRIALING,
                        SubscriptionStatus.FREEMIUM,
                    ])
                )
                .options(selectinload(Subscription.plan))
            )
            subscriptions = result.scalars().all()

            for sub in subscriptions:
                try:
                    # Get workspaces for this user
                    ws_result = await db.execute(
                        select(Workspace).where(
                            Workspace.owner_id == sub.user_id,
                            Workspace.is_active == True,
                        )
                    )
                    workspaces = ws_result.scalars().all()

                    svc = CreditService(db=db)
                    for ws in workspaces:
                        await svc.reset_monthly_credits_for_workspace(
                            workspace_id=ws.id,
                            user_id=sub.user_id,
                        )
                        reset_count += 1

                    await db.commit()
                except Exception as exc:
                    task_log.error(f"reset_error sub={sub.id}: {exc}")
                    await db.rollback()
                    error_count += 1

        task_log.info(f"monthly_credit_reset: reset={reset_count} errors={error_count}")
        return {"reset": reset_count, "errors": error_count}

    try:
        return run_async(_run())
    except Exception as exc:
        raise self.retry(exc=exc)


@celery_app.task(
    name="app.workers.credit_tasks.check_expired_subs",
    bind=True,
)
def check_expired_subs(self) -> dict:
    """
    Daily task — marks subscriptions past their end date as expired
    and downgrades workspace to free plan credits.
    """
    task_log.info("check_expired_subs: starting")

    async def _run() -> dict:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from app.db.session import AsyncSessionFactory
        from app.models.auth import (
            CreditBalance, Plan, PlanTier,
            Subscription, SubscriptionStatus,
        )
        from app.models.content import Workspace

        expired_count = 0
        now = datetime.now(timezone.utc)

        async with AsyncSessionFactory() as db:
            # Find subscriptions past ends_at that are still active
            result = await db.execute(
                select(Subscription)
                .where(
                    Subscription.status == SubscriptionStatus.ACTIVE,
                    Subscription.ends_at != None,
                    Subscription.ends_at < now,
                )
            )
            subs = result.scalars().all()

            # Get free plan
            free_plan_result = await db.execute(
                select(Plan).where(Plan.tier == PlanTier.FREE)
            )
            free_plan = free_plan_result.scalar_one_or_none()

            for sub in subs:
                try:
                    sub.status = SubscriptionStatus.EXPIRED

                    # Downgrade credit balance to free plan
                    if free_plan:
                        ws_result = await db.execute(
                            select(Workspace).where(
                                Workspace.owner_id == sub.user_id,
                                Workspace.is_active == True,
                            )
                        )
                        for ws in ws_result.scalars().all():
                            bal_result = await db.execute(
                                select(CreditBalance).where(
                                    CreditBalance.workspace_id == ws.id
                                ).order_by(CreditBalance.created_at.desc())
                            )
                            bal = bal_result.scalars().first()
                            if bal:
                                # Cap at free plan limit
                                bal.balance   = min(bal.balance, free_plan.monthly_credits)
                                bal.allocated = free_plan.monthly_credits

                    expired_count += 1
                    await db.commit()
                    task_log.info(f"subscription_expired: sub={sub.id}")
                except Exception as exc:
                    task_log.error(f"expiry_error sub={sub.id}: {exc}")
                    await db.rollback()

        return {"expired": expired_count}

    return run_async(_run())


@celery_app.task(
    name="app.workers.credit_tasks.send_low_credit_alerts",
)
def send_low_credit_alerts() -> dict:
    """
    Daily task — sends email to users with less than 20% credits remaining.
    """
    task_log.info("send_low_credit_alerts: starting")

    async def _run() -> dict:
        from sqlalchemy import select
        from app.db.session import AsyncSessionFactory
        from app.models.auth import CreditBalance, User
        from app.models.content import Workspace

        alerted = 0

        async with AsyncSessionFactory() as db:
            result = await db.execute(
                select(CreditBalance).where(
                    CreditBalance.allocated > 0,
                )
            )
            balances = result.scalars().all()

            for bal in balances:
                pct_remaining = bal.balance / max(bal.allocated, 1)
                if pct_remaining > 0.2:
                    continue

                # Get workspace owner
                ws_result = await db.execute(
                    select(Workspace).where(Workspace.id == bal.workspace_id)
                )
                ws = ws_result.scalar_one_or_none()
                if not ws or not ws.owner_id:
                    continue

                user_result = await db.execute(
                    select(User).where(User.id == ws.owner_id)
                )
                user = user_result.scalar_one_or_none()
                if not user:
                    continue

                try:
                    await _send_low_credit_email(
                        email=user.email,
                        name=user.name,
                        balance=bal.balance,
                        allocated=bal.allocated,
                        pct=int(pct_remaining * 100),
                    )
                    alerted += 1
                except Exception as exc:
                    task_log.warning(f"alert_email_failed user={user.id}: {exc}")

        task_log.info(f"send_low_credit_alerts: alerted={alerted}")
        return {"alerted": alerted}

    return run_async(_run())


async def _send_low_credit_email(
    email: str, name: str, balance: int, allocated: int, pct: int
) -> None:
    import smtplib
    from email.mime.text import MIMEText
    import asyncio
    from app.core.config import settings

    body = f"""Hi {name},

You have used {100 - pct}% of your monthly AI credits.

Remaining: {balance} / {allocated} credits

To keep generating content without interruption, upgrade your plan at:
{settings.FRONTEND_URL}/billing

— Social Media Manager Team"""

    def _send():
        msg = MIMEText(body)
        msg["Subject"] = f"You have {balance} AI credits remaining — Social Media Manager"
        msg["From"]    = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"]      = email
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as smtp:
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(msg)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send)


# ── Beat schedule entries ─────────────────────────────────────────────────────
from app.workers.tasks import celery_app  # noqa

celery_app.conf.beat_schedule.update({
    "monthly-credit-reset": {
        "task": "app.workers.credit_tasks.monthly_credit_reset",
        # Run at midnight on the 1st of every month
        "schedule": celery_app.conf.beat_schedule.get(
            "monthly-credit-reset", {}
        ).get("schedule") or __import__("celery.schedules", fromlist=["crontab"]).crontab(
            hour=0, minute=0, day_of_month=1
        ),
        "options": {"queue": "beat"},
    },
    "daily-expiry-check": {
        "task": "app.workers.credit_tasks.check_expired_subs",
        "schedule": __import__("celery.schedules", fromlist=["crontab"]).crontab(
            hour=2, minute=0
        ),
        "options": {"queue": "beat"},
    },
    "daily-low-credit-alerts": {
        "task": "app.workers.credit_tasks.send_low_credit_alerts",
        "schedule": __import__("celery.schedules", fromlist=["crontab"]).crontab(
            hour=9, minute=0
        ),
        "options": {"queue": "beat"},
    },
})
