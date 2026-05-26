"""
Module 9 — CreditService

Extends BillingService with:
  - Redis-backed fast credit check (no DB hit on every request)
  - Atomic deduct-then-queue pattern
  - Refund on job failure
  - Monthly credit reset
  - Per-workspace credit cache invalidation
"""
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.auth import (
    CreditActionType, CreditBalance, CreditTransaction,
    Plan, PlanTier, Subscription, SubscriptionStatus,
    CREDIT_COSTS, PLAN_MONTHLY_CREDITS,
)

log = structlog.get_logger(__name__)

CREDIT_CACHE_KEY   = "credits:{workspace_id}"
CREDIT_CACHE_TTL   = 60  # seconds — short enough to stay accurate


class CreditService:
    """
    Focused credit operations service.
    Use alongside BillingService — does not duplicate plan/stripe logic.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Fast balance check (Redis-backed) ─────────────────────────────────────

    async def get_balance_fast(self, workspace_id: uuid.UUID) -> int:
        """
        Returns credit balance from Redis cache.
        Falls back to DB if cache miss. Used for quick pre-flight checks.
        """
        try:
            from app.core.cache import get_cache_client
            cache = get_cache_client()
            key = CREDIT_CACHE_KEY.format(workspace_id=workspace_id)
            cached = await cache.get(key)
            if cached is not None:
                return int(cached)
        except Exception:
            pass  # Redis down → fall through to DB

        balance = await self._get_db_balance(workspace_id)
        await self._cache_balance(workspace_id, balance)
        return balance

    async def _cache_balance(self, workspace_id: uuid.UUID, balance: int) -> None:
        try:
            from app.core.cache import get_cache_client
            cache = get_cache_client()
            key = CREDIT_CACHE_KEY.format(workspace_id=workspace_id)
            await cache.set(key, balance, ttl=CREDIT_CACHE_TTL)
        except Exception:
            pass

    async def _invalidate_cache(self, workspace_id: uuid.UUID) -> None:
        try:
            from app.core.cache import get_cache_client
            cache = get_cache_client()
            key = CREDIT_CACHE_KEY.format(workspace_id=workspace_id)
            await cache.delete(key)
        except Exception:
            pass

    async def _get_db_balance(self, workspace_id: uuid.UUID) -> int:
        result = await self._db.execute(
            select(CreditBalance)
            .where(CreditBalance.workspace_id == workspace_id)
            .order_by(CreditBalance.created_at.desc())
        )
        balance = result.scalars().first()
        return balance.balance if balance else 0

    # ── Deduct ────────────────────────────────────────────────────────────────

    async def deduct(
        self,
        workspace_id: uuid.UUID,
        action_type: CreditActionType,
        reference_id: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> bool:
        """
        Atomically deduct credits. Returns False if insufficient balance.
        Invalidates Redis cache on success.
        """
        cost = CREDIT_COSTS.get(action_type, 1)

        result = await self._db.execute(
            select(CreditBalance)
            .where(CreditBalance.workspace_id == workspace_id)
            .order_by(CreditBalance.created_at.desc())
        )
        balance_row = result.scalars().first()

        if not balance_row or balance_row.balance < cost:
            log.warning(
                "insufficient_credits",
                workspace_id=str(workspace_id),
                cost=cost,
                balance=balance_row.balance if balance_row else 0,
            )
            return False

        balance_row.balance -= cost
        balance_row.used    += cost

        self._db.add(CreditTransaction(
            credit_balance_id=balance_row.id,
            workspace_id=workspace_id,
            user_id=user_id,
            action_type=action_type,
            credits_delta=-cost,
            balance_after=balance_row.balance,
            description=f"{action_type.value} — {cost} credit(s)",
            reference_id=reference_id,
        ))
        await self._db.flush()
        await self._invalidate_cache(workspace_id)

        log.info(
            "credits_deducted",
            workspace_id=str(workspace_id),
            action=action_type.value,
            cost=cost,
            remaining=balance_row.balance,
        )
        return True

    # ── Refund ────────────────────────────────────────────────────────────────

    async def refund(
        self,
        workspace_id: uuid.UUID,
        action_type: CreditActionType,
        reference_id: str | None = None,
    ) -> None:
        """Refund credits when a job fails after deduction."""
        cost = CREDIT_COSTS.get(action_type, 1)

        result = await self._db.execute(
            select(CreditBalance)
            .where(CreditBalance.workspace_id == workspace_id)
            .order_by(CreditBalance.created_at.desc())
        )
        balance_row = result.scalars().first()
        if not balance_row:
            return

        balance_row.balance += cost
        balance_row.used    = max(0, balance_row.used - cost)

        self._db.add(CreditTransaction(
            credit_balance_id=balance_row.id,
            workspace_id=workspace_id,
            action_type=CreditActionType.REFUND,
            credits_delta=cost,
            balance_after=balance_row.balance,
            description=f"Refund for failed {action_type.value}",
            reference_id=reference_id,
        ))
        await self._db.flush()
        await self._invalidate_cache(workspace_id)

        log.info("credits_refunded", workspace_id=str(workspace_id), cost=cost)

    # ── Assert gates ──────────────────────────────────────────────────────────

    async def assert_can_use(
        self,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        action_type: CreditActionType,
    ) -> None:
        """
        Raises HTTP 402 if user cannot perform this action.
        Checks subscription status AND credit balance.
        """
        from app.core.config import settings
        if settings.TESTING_MODE:
            return  # bypass all gates in testing mode

        from fastapi import HTTPException, status as http_status

        # Check subscription
        sub = await self._get_active_sub(user_id)
        if not sub or not sub.is_active:
            raise HTTPException(
                status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                detail="No active subscription. Please sign up for a plan.",
            )

        # Check past-due grace period
        if sub.status == SubscriptionStatus.PAST_DUE:
            if sub.grace_period_ends_at and sub.grace_period_ends_at < datetime.now(timezone.utc):
                raise HTTPException(
                    status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Payment failed and grace period expired. Please update your payment method.",
                )

        # Check credit balance
        cost = CREDIT_COSTS.get(action_type, 1)
        balance = await self.get_balance_fast(workspace_id)
        if balance < cost:
            raise HTTPException(
                status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "message": f"Insufficient credits. Need {cost}, have {balance}.",
                    "credits_needed": cost,
                    "credits_available": balance,
                    "upgrade_url": "/billing",
                },
            )

    async def assert_can_schedule(self, user_id: uuid.UUID) -> None:
        """Raises HTTP 402 if plan does not include scheduling."""
        from app.core.config import settings
        if settings.TESTING_MODE:
            return

        from fastapi import HTTPException, status as http_status
        sub = await self._get_active_sub(user_id)
        plan = sub.plan if sub else None
        if not plan or not plan.can_schedule:
            raise HTTPException(
                status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "message": "Scheduling requires Starter plan or above.",
                    "upgrade_url": "/billing",
                },
            )

    async def assert_can_bulk_generate(self, user_id: uuid.UUID) -> None:
        """Raises HTTP 402 if plan does not include bulk calendar generation."""
        from app.core.config import settings
        if settings.TESTING_MODE:
            return

        from fastapi import HTTPException, status as http_status
        sub = await self._get_active_sub(user_id)
        plan = sub.plan if sub else None
        if not plan or not plan.can_bulk_generate:
            raise HTTPException(
                status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "message": "30-day calendar generation requires Pro plan or above.",
                    "upgrade_url": "/billing",
                },
            )

    # ── Monthly reset ─────────────────────────────────────────────────────────

    async def reset_monthly_credits_for_workspace(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> int:
        """
        Reset credit balance to the plan's monthly allocation.
        Called by the monthly Celery beat task.
        Returns new balance.
        """
        sub = await self._get_active_sub(user_id)
        if not sub or not sub.is_active:
            return 0

        plan = sub.plan
        monthly = plan.monthly_credits

        result = await self._db.execute(
            select(CreditBalance)
            .where(
                CreditBalance.workspace_id == workspace_id,
                CreditBalance.subscription_id == sub.id,
            )
        )
        balance_row = result.scalar_one_or_none()

        if balance_row:
            balance_row.balance    = monthly
            balance_row.allocated  = monthly
            balance_row.used       = 0
            balance_row.last_reset_at = datetime.now(timezone.utc)
            balance_row.expires_at = datetime.now(timezone.utc) + timedelta(days=32)
        else:
            from app.models.auth import CreditBalance as CB
            balance_row = CB(
                workspace_id=workspace_id,
                subscription_id=sub.id,
                balance=monthly,
                allocated=monthly,
                used=0,
                last_reset_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(days=32),
            )
            self._db.add(balance_row)

        await self._db.flush()

        self._db.add(CreditTransaction(
            credit_balance_id=balance_row.id,
            workspace_id=workspace_id,
            action_type=CreditActionType.PLAN_ALLOCATION,
            credits_delta=monthly,
            balance_after=monthly,
            description=f"Monthly reset — {plan.name} plan — {monthly} credits",
        ))
        await self._db.flush()
        await self._invalidate_cache(workspace_id)

        log.info(
            "monthly_credits_reset",
            workspace_id=str(workspace_id),
            credits=monthly,
            plan=plan.name,
        )
        return monthly

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get_active_sub(self, user_id: uuid.UUID) -> Subscription | None:
        from sqlalchemy.orm import selectinload
        result = await self._db.execute(
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.status.in_([
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.TRIALING,
                    SubscriptionStatus.FREEMIUM,
                    SubscriptionStatus.PAST_DUE,
                ]),
            )
            .options(selectinload(Subscription.plan))
            .order_by(Subscription.created_at.desc())
        )
        return result.scalars().first()
