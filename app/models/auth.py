"""
Module 7 & 8 models:
  User            — core identity (email + google oauth)
  UserSession     — refresh token store
  Plan            — Free / Starter / Pro / Agency
  Subscription    — user's active plan + stripe data
  CreditBalance   — current credits per workspace
  CreditTransaction — append-only usage log
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Float,
    ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


# ── Enums ─────────────────────────────────────────────────────────────────────

class AuthProvider(str, enum.Enum):
    EMAIL  = "email"
    GOOGLE = "google"


class SubscriptionStatus(str, enum.Enum):
    TRIALING  = "trialing"
    ACTIVE    = "active"
    PAST_DUE  = "past_due"
    CANCELLED = "cancelled"
    EXPIRED   = "expired"
    FREEMIUM  = "freemium"


class PlanTier(str, enum.Enum):
    FREE    = "free"
    STARTER = "starter"
    PRO     = "pro"
    AGENCY  = "agency"


class BillingInterval(str, enum.Enum):
    MONTHLY = "monthly"
    ANNUAL  = "annual"


class CreditActionType(str, enum.Enum):
    CAPTION_GENERATE    = "caption_generate"
    IMAGE_GENERATE      = "image_generate"
    CONTENT_GENERATE    = "content_generate"   # caption + image together
    CALENDAR_PLAN       = "calendar_plan"
    PLAN_ALLOCATION     = "plan_allocation"    # monthly top-up
    TOPUP_PURCHASE      = "topup_purchase"     # manual purchase
    ADMIN_GRANT         = "admin_grant"        # manual grant by admin
    REFUND              = "refund"


# Credit cost per action
CREDIT_COSTS: dict[CreditActionType, int] = {
    CreditActionType.CAPTION_GENERATE: 1,
    CreditActionType.IMAGE_GENERATE:   5,
    CreditActionType.CONTENT_GENERATE: 6,
    CreditActionType.CALENDAR_PLAN:    10,
}

# Credits per plan per month
PLAN_MONTHLY_CREDITS: dict[PlanTier, int] = {
    PlanTier.FREE:    10,
    PlanTier.STARTER: 200,
    PlanTier.PRO:     1000,
    PlanTier.AGENCY:  999999,  # effectively unlimited
}


# ── Models ────────────────────────────────────────────────────────────────────

class User(Base):
    """Core identity. Supports both email+password and Google OAuth."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    name: Mapped[str]  = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text)

    # Auth
    hashed_password: Mapped[str | None] = mapped_column(Text)  # null for Google-only users
    auth_provider: Mapped[AuthProvider] = mapped_column(
        Enum(AuthProvider), default=AuthProvider.EMAIL
    )
    google_id: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)

    # Verification
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verify_token: Mapped[str | None] = mapped_column(String(128))
    password_reset_token: Mapped[str | None] = mapped_column(String(128))
    password_reset_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool]  = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")

    # Relationships
    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def active_subscription(self) -> "Subscription | None":
        active = [s for s in self.subscriptions if s.status in (
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.TRIALING,
            SubscriptionStatus.FREEMIUM,
        )]
        return active[0] if active else None


class UserSession(Base):
    """Refresh token store — one row per device/session."""

    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    device_info: Mapped[str | None] = mapped_column(String(512))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="sessions")


class Plan(Base):
    """Static plan definitions — seeded at startup."""

    __tablename__ = "plans"

    tier: Mapped[PlanTier] = mapped_column(Enum(PlanTier), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Pricing
    monthly_price_usd: Mapped[float] = mapped_column(Float, default=0.0)
    annual_price_usd: Mapped[float]  = mapped_column(Float, default=0.0)

    # Stripe price IDs
    stripe_monthly_price_id: Mapped[str | None] = mapped_column(String(128))
    stripe_annual_price_id: Mapped[str | None]  = mapped_column(String(128))

    # Limits
    monthly_credits: Mapped[int] = mapped_column(Integer, nullable=False)
    max_workspaces: Mapped[int]  = mapped_column(Integer, default=1)
    max_team_members: Mapped[int] = mapped_column(Integer, default=1)
    max_instagram_accounts: Mapped[int] = mapped_column(Integer, default=1)

    # Feature flags
    can_schedule: Mapped[bool]    = mapped_column(Boolean, default=False)
    can_use_analytics: Mapped[bool] = mapped_column(Boolean, default=False)
    can_bulk_generate: Mapped[bool] = mapped_column(Boolean, default=False)
    can_export: Mapped[bool]      = mapped_column(Boolean, default=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="plan")


class Subscription(Base):
    """User's current plan. One active subscription per user at a time."""

    __tablename__ = "subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id"), nullable=False
    )

    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus), nullable=False, default=SubscriptionStatus.FREEMIUM, index=True
    )
    billing_interval: Mapped[BillingInterval] = mapped_column(
        Enum(BillingInterval), default=BillingInterval.MONTHLY
    )

    # Dates
    started_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None]     = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True))

    # Stripe
    stripe_customer_id: Mapped[str | None]      = mapped_column(String(128), index=True)
    stripe_subscription_id: Mapped[str | None]  = mapped_column(String(128), unique=True)
    stripe_payment_method_id: Mapped[str | None] = mapped_column(String(128))

    # Grace period on failed payment
    grace_period_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    metadata: Mapped[dict] = mapped_column(JSONB, default=dict)

    user: Mapped["User"]  = relationship(back_populates="subscriptions")
    plan: Mapped["Plan"]  = relationship(back_populates="subscriptions")
    credit_balances: Mapped[list["CreditBalance"]] = relationship(back_populates="subscription")

    @property
    def is_active(self) -> bool:
        return self.status in (
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.TRIALING,
            SubscriptionStatus.FREEMIUM,
        )


class CreditBalance(Base):
    """Current credit balance per workspace per subscription cycle."""

    __tablename__ = "credit_balances"
    __table_args__ = (UniqueConstraint("workspace_id", "subscription_id"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )

    balance: Mapped[int]    = mapped_column(Integer, nullable=False, default=0)
    allocated: Mapped[int]  = mapped_column(Integer, nullable=False, default=0)  # total given this cycle
    used: Mapped[int]       = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    subscription: Mapped["Subscription"] = relationship(back_populates="credit_balances")
    transactions: Mapped[list["CreditTransaction"]] = relationship(back_populates="credit_balance")


class CreditTransaction(Base):
    """Append-only ledger of every credit movement."""

    __tablename__ = "credit_transactions"

    credit_balance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("credit_balances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None]  = mapped_column(UUID(as_uuid=True))

    action_type: Mapped[CreditActionType] = mapped_column(Enum(CreditActionType), nullable=False)
    credits_delta: Mapped[int] = mapped_column(Integer, nullable=False)  # negative = deduction
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))
    reference_id: Mapped[str | None] = mapped_column(String(128))  # job_id, invoice_id etc.

    credit_balance: Mapped["CreditBalance"] = relationship(back_populates="transactions")
