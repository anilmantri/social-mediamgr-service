"""Schemas for Module 7 (Auth) and Module 8 (Billing)."""
import uuid
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.auth import BillingInterval, PlanTier, SubscriptionStatus


# ── Auth ──────────────────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleCallbackRequest(BaseModel):
    code: str
    state: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RefreshRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8)


class VerifyEmailRequest(BaseModel):
    token: str


class UserRead(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    avatar_url: str | None
    email_verified: bool
    auth_provider: str
    is_admin: bool
    timezone: str
    created_at: datetime

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=100)
    timezone: str | None = None
    avatar_url: str | None = None


# ── Plans & Billing ────────────────────────────────────────────────────────────

class PlanRead(BaseModel):
    id: uuid.UUID
    tier: PlanTier
    name: str
    description: str | None
    monthly_price_usd: float
    annual_price_usd: float
    monthly_credits: int
    max_workspaces: int
    max_team_members: int
    max_instagram_accounts: int
    can_schedule: bool
    can_use_analytics: bool
    can_bulk_generate: bool
    can_export: bool

    model_config = {"from_attributes": True}


class SubscriptionRead(BaseModel):
    id: uuid.UUID
    status: SubscriptionStatus
    billing_interval: BillingInterval
    started_at: datetime | None
    ends_at: datetime | None
    trial_ends_at: datetime | None
    cancelled_at: datetime | None
    plan: PlanRead

    model_config = {"from_attributes": True}


class CreateCheckoutRequest(BaseModel):
    plan_tier: PlanTier
    billing_interval: BillingInterval = BillingInterval.MONTHLY
    workspace_id: uuid.UUID


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str


class CreditBalanceRead(BaseModel):
    workspace_id: uuid.UUID
    balance: int
    allocated: int
    used: int
    expires_at: datetime | None

    model_config = {"from_attributes": True}


class CreditTransactionRead(BaseModel):
    id: uuid.UUID
    action_type: str
    credits_delta: int
    balance_after: int
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class UsageSummary(BaseModel):
    workspace_id: uuid.UUID
    plan_tier: PlanTier
    plan_name: str
    credits_balance: int
    credits_used: int
    credits_allocated: int
    credits_pct_used: float
    subscription_status: SubscriptionStatus
    subscription_ends_at: datetime | None
    can_generate: bool
    can_schedule: bool
    can_use_analytics: bool
