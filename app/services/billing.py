"""
BillingService — plan management, credit allocation, Stripe integration,
and feature gating.
"""
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.auth import (
    BillingInterval, CreditActionType, CreditBalance,
    CreditTransaction, Plan, PlanTier, Subscription,
    SubscriptionStatus, CREDIT_COSTS, PLAN_MONTHLY_CREDITS,
)
from app.models.content import Workspace
from app.schemas.auth import (
    CheckoutResponse, CreditBalanceRead, UsageSummary,
)

log = structlog.get_logger(__name__)

# ── Plan definitions (seeded to DB at startup) ────────────────────────────────
PLAN_DEFINITIONS = [
    {
        "tier": PlanTier.FREE,
        "name": "Free",
        "description": "10 AI credits per month. Perfect to get started.",
        "monthly_price_usd": 0.0,
        "annual_price_usd":  0.0,
        "monthly_credits": 10,
        "max_workspaces": 1,
        "max_team_members": 1,
        "max_instagram_accounts": 1,
        "can_schedule": False,
        "can_use_analytics": False,
        "can_bulk_generate": False,
        "can_export": False,
    },
    {
        "tier": PlanTier.STARTER,
        "name": "Starter",
        "description": "200 credits/month. Scheduling + 1 Instagram account.",
        "monthly_price_usd": 19.0,
        "annual_price_usd":  190.0,
        "monthly_credits": 200,
        "max_workspaces": 1,
        "max_team_members": 2,
        "max_instagram_accounts": 1,
        "can_schedule": True,
        "can_use_analytics": True,
        "can_bulk_generate": False,
        "can_export": False,
    },
    {
        "tier": PlanTier.PRO,
        "name": "Pro",
        "description": "1000 credits/month. 3 accounts, bulk calendar, exports.",
        "monthly_price_usd": 49.0,
        "annual_price_usd":  490.0,
        "monthly_credits": 1000,
        "max_workspaces": 3,
        "max_team_members": 5,
        "max_instagram_accounts": 3,
        "can_schedule": True,
        "can_use_analytics": True,
        "can_bulk_generate": True,
        "can_export": True,
    },
    {
        "tier": PlanTier.AGENCY,
        "name": "Agency",
        "description": "Unlimited credits. 10 accounts, unlimited team.",
        "monthly_price_usd": 149.0,
        "annual_price_usd":  1490.0,
        "monthly_credits": 999999,
        "max_workspaces": 10,
        "max_team_members": 999,
        "max_instagram_accounts": 10,
        "can_schedule": True,
        "can_use_analytics": True,
        "can_bulk_generate": True,
        "can_export": True,
    },
]


class BillingService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Plan seeding ──────────────────────────────────────────────────────────

    async def seed_plans(self) -> None:
        """Called on startup — ensures plan rows exist."""
        for plan_data in PLAN_DEFINITIONS:
            result = await self._db.execute(
                select(Plan).where(Plan.tier == plan_data["tier"])
            )
            plan = result.scalar_one_or_none()
            if not plan:
                plan = Plan(**plan_data)
                self._db.add(plan)
            else:
                # Update pricing/limits if changed
                for k, v in plan_data.items():
                    if k != "tier":
                        setattr(plan, k, v)
        await self._db.flush()
        log.info("plans_seeded")

    # ── Free plan provision ───────────────────────────────────────────────────

    async def provision_free_plan(self, user_id: uuid.UUID) -> Subscription:
        """Create freemium subscription for a new user."""
        free_plan = await self._get_plan_by_tier(PlanTier.FREE)

        sub = Subscription(
            user_id=user_id,
            plan_id=free_plan.id,
            status=SubscriptionStatus.FREEMIUM,
            started_at=datetime.now(timezone.utc),
        )
        self._db.add(sub)
        await self._db.flush()

        # Create default workspace for new user
        workspace = Workspace(
            name="My Brand",
            slug=f"workspace-{str(user_id)[:8]}",
            is_active=True,
        )
        self._db.add(workspace)
        await self._db.flush()

        # Allocate free credits for this workspace
        await self._allocate_credits(
            subscription_id=sub.id,
            workspace_id=workspace.id,
            amount=free_plan.monthly_credits,
            action_type=CreditActionType.PLAN_ALLOCATION,
            description=f"Free plan monthly allocation — {free_plan.monthly_credits} credits",
        )

        log.info("free_plan_provisioned", user_id=str(user_id), workspace_id=str(workspace.id))
        return sub

    # ── Credit management ─────────────────────────────────────────────────────

    async def get_credit_balance(self, workspace_id: uuid.UUID) -> CreditBalance | None:
        result = await self._db.execute(
            select(CreditBalance)
            .where(CreditBalance.workspace_id == workspace_id)
            .order_by(CreditBalance.created_at.desc())
        )
        return result.scalars().first()

    async def deduct_credits(
        self,
        workspace_id: uuid.UUID,
        action_type: CreditActionType,
        reference_id: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> bool:
        """
        Deduct credits for an AI action. Returns False if insufficient balance.
        This is called BEFORE the AI job is queued.
        """
        cost = CREDIT_COSTS.get(action_type, 1)
        balance = await self.get_credit_balance(workspace_id)

        if not balance or balance.balance < cost:
            log.warning("insufficient_credits", workspace_id=str(workspace_id), cost=cost, balance=balance.balance if balance else 0)
            return False

        balance.balance -= cost
        balance.used    += cost

        tx = CreditTransaction(
            credit_balance_id=balance.id,
            workspace_id=workspace_id,
            user_id=user_id,
            action_type=action_type,
            credits_delta=-cost,
            balance_after=balance.balance,
            description=f"Used {cost} credit(s) for {action_type.value}",
            reference_id=reference_id,
        )
        self._db.add(tx)
        await self._db.flush()

        log.info("credits_deducted", workspace_id=str(workspace_id), cost=cost, remaining=balance.balance)
        return True

    async def refund_credits(
        self,
        workspace_id: uuid.UUID,
        action_type: CreditActionType,
        reference_id: str | None = None,
    ) -> None:
        """Refund credits when an AI job fails after deduction."""
        cost    = CREDIT_COSTS.get(action_type, 1)
        balance = await self.get_credit_balance(workspace_id)
        if not balance:
            return

        balance.balance += cost
        balance.used    -= cost

        tx = CreditTransaction(
            credit_balance_id=balance.id,
            workspace_id=workspace_id,
            action_type=CreditActionType.REFUND,
            credits_delta=cost,
            balance_after=balance.balance,
            description=f"Refund for failed {action_type.value}",
            reference_id=reference_id,
        )
        self._db.add(tx)
        await self._db.flush()

    async def _allocate_credits(
        self,
        subscription_id: uuid.UUID,
        workspace_id: uuid.UUID,
        amount: int,
        action_type: CreditActionType,
        description: str,
    ) -> CreditBalance:
        # Find or create balance for this workspace+subscription
        result = await self._db.execute(
            select(CreditBalance).where(
                CreditBalance.workspace_id == workspace_id,
                CreditBalance.subscription_id == subscription_id,
            )
        )
        balance = result.scalar_one_or_none()

        if balance:
            balance.balance   += amount
            balance.allocated += amount
        else:
            from datetime import timedelta
            balance = CreditBalance(
                workspace_id=workspace_id,
                subscription_id=subscription_id,
                balance=amount,
                allocated=amount,
                used=0,
                expires_at=datetime.now(timezone.utc) + timedelta(days=32),
                last_reset_at=datetime.now(timezone.utc),
            )
            self._db.add(balance)

        await self._db.flush()

        tx = CreditTransaction(
            credit_balance_id=balance.id,
            workspace_id=workspace_id,
            action_type=action_type,
            credits_delta=amount,
            balance_after=balance.balance,
            description=description,
        )
        self._db.add(tx)
        await self._db.flush()
        return balance

    # ── Usage summary ─────────────────────────────────────────────────────────

    async def get_usage_summary(
        self, user_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> UsageSummary:
        sub = await self._get_active_subscription(user_id)
        balance = await self.get_credit_balance(workspace_id)

        plan = sub.plan if sub else await self._get_plan_by_tier(PlanTier.FREE)
        status = sub.status if sub else SubscriptionStatus.FREEMIUM
        bal = balance.balance if balance else 0
        used = balance.used if balance else 0
        allocated = balance.allocated if balance else plan.monthly_credits
        pct = round(used / max(allocated, 1) * 100, 1)

        return UsageSummary(
            workspace_id=workspace_id,
            plan_tier=plan.tier,
            plan_name=plan.name,
            credits_balance=bal,
            credits_used=used,
            credits_allocated=allocated,
            credits_pct_used=pct,
            subscription_status=status,
            subscription_ends_at=sub.ends_at if sub else None,
            can_generate=bal > 0,
            can_schedule=plan.can_schedule,
            can_use_analytics=plan.can_use_analytics,
        )

    # ── Feature gates ─────────────────────────────────────────────────────────

    async def assert_can_generate(
        self, user_id: uuid.UUID, workspace_id: uuid.UUID,
        action_type: CreditActionType = CreditActionType.CONTENT_GENERATE
    ) -> None:
        from fastapi import HTTPException, status as http_status
        sub = await self._get_active_subscription(user_id)
        if not sub or not sub.is_active:
            raise HTTPException(
                status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                detail="No active subscription. Please upgrade to continue.",
            )
        balance = await self.get_credit_balance(workspace_id)
        cost = CREDIT_COSTS.get(action_type, 1)
        if not balance or balance.balance < cost:
            raise HTTPException(
                status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Insufficient credits. Need {cost}, have {balance.balance if balance else 0}. Please upgrade your plan.",
            )

    async def assert_can_schedule(self, user_id: uuid.UUID) -> None:
        from fastapi import HTTPException, status as http_status
        sub = await self._get_active_subscription(user_id)
        plan = sub.plan if sub else await self._get_plan_by_tier(PlanTier.FREE)
        if not plan.can_schedule:
            raise HTTPException(
                status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                detail="Scheduling requires Starter plan or above.",
            )

    # ── Stripe ────────────────────────────────────────────────────────────────

    async def create_checkout_session(
        self,
        user_id: uuid.UUID,
        plan_tier: PlanTier,
        billing_interval: BillingInterval,
        workspace_id: uuid.UUID,
    ) -> CheckoutResponse:
        if not settings.STRIPE_SECRET_KEY:
            from fastapi import HTTPException, status as http_status
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Payment processing not configured",
            )

        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY

        price_id = self._get_stripe_price_id(plan_tier, billing_interval)
        plan = await self._get_plan_by_tier(plan_tier)

        # Get or create Stripe customer
        sub = await self._get_active_subscription(user_id)
        customer_id = sub.stripe_customer_id if sub else None

        if not customer_id:
            user_result = await self._db.execute(
                select("users").where(1 == 1)  # simplified
            )
            # Create customer in Stripe
            from app.models.auth import User
            user_q = await self._db.execute(select(User).where(User.id == user_id))
            user = user_q.scalar_one()
            customer = stripe.Customer.create(email=user.email, name=user.name)
            customer_id = customer.id

        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=f"{settings.FRONTEND_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{settings.FRONTEND_URL}/billing/cancelled",
            metadata={
                "user_id": str(user_id),
                "workspace_id": str(workspace_id),
                "plan_tier": plan_tier.value,
            },
            subscription_data={
                "metadata": {
                    "user_id": str(user_id),
                    "workspace_id": str(workspace_id),
                }
            },
        )

        return CheckoutResponse(checkout_url=session.url, session_id=session.id)

    async def handle_stripe_webhook(self, payload: bytes, sig_header: str) -> None:
        """Process Stripe webhook events."""
        if not settings.STRIPE_SECRET_KEY:
            return

        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
        except Exception as e:
            log.error("stripe_webhook_error", error=str(e))
            raise

        event_type = event["type"]
        data = event["data"]["object"]

        log.info("stripe_webhook", event_type=event_type)

        if event_type == "checkout.session.completed":
            await self._handle_checkout_completed(data)
        elif event_type == "invoice.payment_succeeded":
            await self._handle_payment_succeeded(data)
        elif event_type == "invoice.payment_failed":
            await self._handle_payment_failed(data)
        elif event_type == "customer.subscription.deleted":
            await self._handle_subscription_cancelled(data)

    async def _handle_checkout_completed(self, session: dict) -> None:
        meta = session.get("metadata", {})
        user_id      = uuid.UUID(meta["user_id"])
        workspace_id = uuid.UUID(meta["workspace_id"])
        plan_tier    = PlanTier(meta["plan_tier"])
        stripe_sub_id     = session.get("subscription")
        stripe_customer_id = session.get("customer")

        plan = await self._get_plan_by_tier(plan_tier)

        # Deactivate old subscription
        old_sub = await self._get_active_subscription(user_id)
        if old_sub:
            old_sub.status = SubscriptionStatus.CANCELLED
            old_sub.cancelled_at = datetime.now(timezone.utc)

        # Create new active subscription
        new_sub = Subscription(
            user_id=user_id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_sub_id,
            started_at=datetime.now(timezone.utc),
        )
        self._db.add(new_sub)
        await self._db.flush()

        # Allocate credits
        await self._allocate_credits(
            subscription_id=new_sub.id,
            workspace_id=workspace_id,
            amount=plan.monthly_credits,
            action_type=CreditActionType.PLAN_ALLOCATION,
            description=f"{plan.name} plan activated — {plan.monthly_credits} credits",
        )
        log.info("subscription_activated", user_id=str(user_id), plan=plan_tier.value)

    async def _handle_payment_succeeded(self, invoice: dict) -> None:
        stripe_sub_id = invoice.get("subscription")
        if not stripe_sub_id:
            return
        result = await self._db.execute(
            select(Subscription)
            .where(Subscription.stripe_subscription_id == stripe_sub_id)
            .options(selectinload(Subscription.plan))
        )
        sub = result.scalar_one_or_none()
        if not sub:
            return
        sub.status = SubscriptionStatus.ACTIVE
        sub.grace_period_ends_at = None
        await self._db.flush()
        log.info("payment_succeeded", subscription_id=str(sub.id))

    async def _handle_payment_failed(self, invoice: dict) -> None:
        from datetime import timedelta
        stripe_sub_id = invoice.get("subscription")
        if not stripe_sub_id:
            return
        result = await self._db.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id)
        )
        sub = result.scalar_one_or_none()
        if not sub:
            return
        sub.status = SubscriptionStatus.PAST_DUE
        sub.grace_period_ends_at = datetime.now(timezone.utc) + timedelta(days=3)
        await self._db.flush()
        log.warning("payment_failed", subscription_id=str(sub.id))

    async def _handle_subscription_cancelled(self, stripe_sub: dict) -> None:
        stripe_sub_id = stripe_sub.get("id")
        result = await self._db.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id)
        )
        sub = result.scalar_one_or_none()
        if not sub:
            return
        sub.status = SubscriptionStatus.CANCELLED
        sub.cancelled_at = datetime.now(timezone.utc)
        await self._db.flush()
        log.info("subscription_cancelled", subscription_id=str(sub.id))

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get_plan_by_tier(self, tier: PlanTier) -> Plan:
        result = await self._db.execute(select(Plan).where(Plan.tier == tier))
        plan = result.scalar_one_or_none()
        if not plan:
            raise RuntimeError(f"Plan {tier} not found — run seed_plans() first")
        return plan

    async def get_all_plans(self) -> list[Plan]:
        result = await self._db.execute(
            select(Plan).where(Plan.is_active == True).order_by(Plan.monthly_price_usd)
        )
        return result.scalars().all()

    async def _get_active_subscription(self, user_id: uuid.UUID) -> Subscription | None:
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

    def _get_stripe_price_id(
        self, tier: PlanTier, interval: BillingInterval
    ) -> str:
        mapping = {
            (PlanTier.STARTER, BillingInterval.MONTHLY): settings.STRIPE_STARTER_MONTHLY_PRICE_ID,
            (PlanTier.STARTER, BillingInterval.ANNUAL):  settings.STRIPE_STARTER_ANNUAL_PRICE_ID,
            (PlanTier.PRO,     BillingInterval.MONTHLY): settings.STRIPE_PRO_MONTHLY_PRICE_ID,
            (PlanTier.PRO,     BillingInterval.ANNUAL):  settings.STRIPE_PRO_ANNUAL_PRICE_ID,
            (PlanTier.AGENCY,  BillingInterval.MONTHLY): settings.STRIPE_AGENCY_MONTHLY_PRICE_ID,
            (PlanTier.AGENCY,  BillingInterval.ANNUAL):  settings.STRIPE_AGENCY_ANNUAL_PRICE_ID,
        }
        price_id = mapping.get((tier, interval))
        if not price_id:
            raise ValueError(f"No Stripe price ID configured for {tier}/{interval}")
        return price_id
