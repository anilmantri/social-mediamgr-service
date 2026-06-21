"""
Module 11 — AdminService

Internal dashboard for platform operators:
  - User list with subscription, usage, last login
  - Revenue metrics (MRR, ARR, churn, trial conversion)
  - Per-user credit usage and AI cost tracking
  - Manual plan overrides
  - System health (queue depth, error rate, DB stats)
  - Feature flag management
"""
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.auth import (
    CreditBalance, CreditTransaction, Plan, PlanTier,
    Subscription, SubscriptionStatus, User,
)
from app.models.content import DraftContent, Workspace
from app.models.scheduler import ScheduledPost, PublishStatus

log = structlog.get_logger(__name__)


class AdminService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Users ─────────────────────────────────────────────────────────────────

    async def list_users(
        self,
        page: int = 1,
        page_size: int = 50,
        search: str | None = None,
        plan_tier: PlanTier | None = None,
    ) -> dict:
        query = (
            select(User)
            .options(selectinload(User.subscriptions).selectinload(Subscription.plan))
            .order_by(User.created_at.desc())
        )
        if search:
            query = query.where(
                User.email.ilike(f"%{search}%") | User.name.ilike(f"%{search}%")
            )

        count_q = select(func.count()).select_from(query.subquery())
        total = (await self._db.execute(count_q)).scalar_one()

        query = query.offset((page - 1) * page_size).limit(page_size)
        result = await self._db.execute(query)
        users = result.scalars().all()

        rows = []
        for u in users:
            active_sub = next(
                (s for s in u.subscriptions if s.status in (
                    SubscriptionStatus.ACTIVE, SubscriptionStatus.FREEMIUM,
                    SubscriptionStatus.TRIALING, SubscriptionStatus.PAST_DUE,
                )),
                None,
            )

            if plan_tier and (not active_sub or active_sub.plan.tier != plan_tier):
                continue

            # Credit balance
            bal_result = await self._db.execute(
                select(CreditBalance)
                .join(Workspace, Workspace.id == CreditBalance.workspace_id)
                .where(Workspace.owner_id == u.id)
                .order_by(CreditBalance.created_at.desc())
            )
            bal = bal_result.scalars().first()

            rows.append({
                "id": str(u.id),
                "email": u.email,
                "name": u.name,
                "email_verified": u.email_verified,
                "auth_provider": u.auth_provider.value,
                "is_admin": u.is_admin,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat(),
                "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
                "plan": active_sub.plan.name if active_sub else "None",
                "plan_tier": active_sub.plan.tier.value if active_sub else "none",
                "subscription_status": active_sub.status.value if active_sub else "none",
                "credits_balance": bal.balance if bal else 0,
                "credits_used": bal.used if bal else 0,
            })

        return {
            "users": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": (page * page_size) < total,
        }

    async def get_user_detail(self, user_id: uuid.UUID) -> dict:
        result = await self._db.execute(
            select(User)
            .where(User.id == user_id)
            .options(
                selectinload(User.subscriptions).selectinload(Subscription.plan),
                selectinload(User.sessions),
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            return {}

        # Workspaces
        ws_result = await self._db.execute(
            select(Workspace).where(Workspace.owner_id == user_id)
        )
        workspaces = ws_result.scalars().all()

        # Content stats per workspace
        ws_data = []
        for ws in workspaces:
            content_result = await self._db.execute(
                select(func.count(DraftContent.id)).where(
                    DraftContent.workspace_id == ws.id
                )
            )
            pub_result = await self._db.execute(
                select(func.count(DraftContent.id)).where(
                    DraftContent.workspace_id == ws.id,
                    DraftContent.status == "published",
                )
            )
            bal_result = await self._db.execute(
                select(CreditBalance).where(
                    CreditBalance.workspace_id == ws.id
                ).order_by(CreditBalance.created_at.desc())
            )
            bal = bal_result.scalars().first()

            ws_data.append({
                "id": str(ws.id),
                "name": ws.name,
                "total_posts": content_result.scalar_one(),
                "published_posts": pub_result.scalar_one(),
                "credits_balance": bal.balance if bal else 0,
                "credits_used": bal.used if bal else 0,
            })

        return {
            "id": str(user.id),
            "email": user.email,
            "name": user.name,
            "created_at": user.created_at.isoformat(),
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            "is_admin": user.is_admin,
            "is_active": user.is_active,
            "email_verified": user.email_verified,
            "subscriptions": [
                {
                    "id": str(s.id),
                    "plan": s.plan.name,
                    "tier": s.plan.tier.value,
                    "status": s.status.value,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "ends_at": s.ends_at.isoformat() if s.ends_at else None,
                }
                for s in user.subscriptions
            ],
            "workspaces": ws_data,
            "active_sessions": sum(1 for s in user.sessions if not s.revoked),
        }

    async def override_plan(
        self, user_id: uuid.UUID, plan_tier: PlanTier, admin_user_id: uuid.UUID
    ) -> dict:
        """Manually assign a plan to a user (support override)."""
        plan_result = await self._db.execute(
            select(Plan).where(Plan.tier == plan_tier)
        )
        plan = plan_result.scalar_one_or_none()
        if not plan:
            raise ValueError(f"Plan {plan_tier} not found")

        # Deactivate current subscription
        sub_result = await self._db.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.status.in_([
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.FREEMIUM,
                    SubscriptionStatus.TRIALING,
                ]),
            )
        )
        for sub in sub_result.scalars().all():
            sub.status = SubscriptionStatus.CANCELLED
            sub.cancelled_at = datetime.now(timezone.utc)

        # Create new active subscription
        new_sub = Subscription(
            user_id=user_id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            started_at=datetime.now(timezone.utc),
        )
        self._db.add(new_sub)
        await self._db.flush()

        log.info("admin_plan_override", user_id=str(user_id), plan=plan_tier, admin=str(admin_user_id))
        return {"user_id": str(user_id), "new_plan": plan.name, "status": "active"}

    async def grant_credits(
        self,
        workspace_id: uuid.UUID,
        amount: int,
        reason: str,
        admin_user_id: uuid.UUID,
    ) -> dict:
        """Manually grant credits to a workspace."""
        from app.models.auth import CreditActionType
        bal_result = await self._db.execute(
            select(CreditBalance).where(
                CreditBalance.workspace_id == workspace_id
            ).order_by(CreditBalance.created_at.desc())
        )
        bal = bal_result.scalars().first()
        if not bal:
            raise ValueError("No credit balance found for this workspace")

        bal.balance += amount
        bal.allocated += amount

        self._db.add(CreditTransaction(
            credit_balance_id=bal.id,
            workspace_id=workspace_id,
            user_id=admin_user_id,
            action_type=CreditActionType.ADMIN_GRANT,
            credits_delta=amount,
            balance_after=bal.balance,
            description=f"Admin grant: {reason}",
        ))
        await self._db.flush()
        log.info("admin_credit_grant", workspace_id=str(workspace_id), amount=amount, admin=str(admin_user_id))
        return {"workspace_id": str(workspace_id), "granted": amount, "new_balance": bal.balance}

    # ── Revenue metrics ───────────────────────────────────────────────────────

    async def get_revenue_metrics(self) -> dict:
        """MRR, ARR, user counts by plan, trial conversion rate."""
        result = await self._db.execute(
            select(Subscription, Plan)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(
                Subscription.status.in_([
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.TRIALING,
                    SubscriptionStatus.FREEMIUM,
                ])
            )
            .options(selectinload(Subscription.plan))
        )
        subs = result.all()

        mrr = 0.0
        by_plan: dict[str, int] = {}
        for sub, plan in subs:
            by_plan[plan.tier.value] = by_plan.get(plan.tier.value, 0) + 1
            if sub.status == SubscriptionStatus.ACTIVE:
                mrr += plan.monthly_price_usd

        # Total users
        total_users = (await self._db.execute(select(func.count(User.id)))).scalar_one()
        free_users  = by_plan.get("free", 0) + by_plan.get("freemium", 0)
        paid_users  = sum(v for k, v in by_plan.items() if k not in ("free", "freemium"))
        conversion  = round(paid_users / max(total_users, 1) * 100, 1)

        # New users last 30 days
        thirty_ago = datetime.now(timezone.utc) - timedelta(days=30)
        new_result = await self._db.execute(
            select(func.count(User.id)).where(User.created_at >= thirty_ago)
        )
        new_users_30d = new_result.scalar_one()

        # Published posts last 30 days
        pub_result = await self._db.execute(
            select(func.count(ScheduledPost.id)).where(
                ScheduledPost.publish_status == PublishStatus.PUBLISHED,
                ScheduledPost.published_at >= thirty_ago,
            )
        )
        published_30d = pub_result.scalar_one()

        return {
            "mrr_usd": round(mrr, 2),
            "arr_usd": round(mrr * 12, 2),
            "total_users": total_users,
            "paid_users": paid_users,
            "free_users": free_users,
            "conversion_rate_pct": conversion,
            "new_users_30d": new_users_30d,
            "published_posts_30d": published_30d,
            "by_plan": by_plan,
        }

    # ── System health ─────────────────────────────────────────────────────────

    async def get_system_health(self) -> dict:
        """DB stats, Redis ping, pending jobs count."""
        now = datetime.now(timezone.utc)

        # Failed posts in last 24h
        failed_result = await self._db.execute(
            select(func.count(ScheduledPost.id)).where(
                ScheduledPost.publish_status == PublishStatus.FAILED,
                ScheduledPost.last_attempt_at >= now - timedelta(hours=24),
            )
        )
        failed_24h = failed_result.scalar_one()

        # Pending scheduled posts
        pending_result = await self._db.execute(
            select(func.count(ScheduledPost.id)).where(
                ScheduledPost.publish_status == PublishStatus.SCHEDULED,
            )
        )
        pending_scheduled = pending_result.scalar_one()

        # Total content in DB
        content_result = await self._db.execute(
            select(func.count(DraftContent.id))
        )
        total_content = content_result.scalar_one()

        # Redis health
        redis_ok = False
        try:
            from app.core.cache import get_cache_client
            cache = get_cache_client()
            await cache.ping()
            redis_ok = True
        except Exception:
            pass

        return {
            "db_status": "ok",
            "redis_status": "ok" if redis_ok else "down",
            "failed_posts_24h": failed_24h,
            "pending_scheduled_posts": pending_scheduled,
            "total_content_records": total_content,
            "checked_at": now.isoformat(),
        }

    async def toggle_user_active(
        self, user_id: uuid.UUID, active: bool, admin_user_id: uuid.UUID
    ) -> dict:
        """Enable or disable a user account."""
        result = await self._db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise ValueError("User not found")
        user.is_active = active
        await self._db.flush()
        log.info("admin_user_toggle", user_id=str(user_id), active=active, admin=str(admin_user_id))
        return {"user_id": str(user_id), "is_active": active}
