"""
Module 7 & 8 API endpoints

Auth:
  POST /auth/signup                    → email signup
  POST /auth/login                     → email login
  POST /auth/logout                    → revoke refresh token
  POST /auth/refresh                   → get new access token
  GET  /auth/google                    → get Google OAuth URL
  GET  /auth/google/callback           → OAuth callback
  POST /auth/verify-email              → verify email token
  POST /auth/forgot-password           → send reset email
  POST /auth/reset-password            → reset password with token
  GET  /auth/me                        → current user profile
  PATCH /auth/me                       → update profile

Billing:
  GET  /billing/plans                  → list all plans
  GET  /billing/subscription           → current subscription
  GET  /billing/usage/{workspace_id}   → credit balance + usage
  GET  /billing/transactions/{ws_id}   → credit transaction history
  POST /billing/checkout               → create Stripe checkout session
  POST /billing/webhook                → Stripe webhook handler
  GET  /billing/portal                 → Stripe customer portal URL
"""
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth_deps import CurrentAuth, get_current_auth, get_current_user_id
from app.db.session import get_db
from app.models.auth import CreditTransaction, Plan, Subscription
from app.schemas.auth import (
    CheckoutResponse, CreateCheckoutRequest,
    CreditBalanceRead, CreditTransactionRead,
    ForgotPasswordRequest, LoginRequest,
    PlanRead, RefreshRequest, ResetPasswordRequest,
    SignupRequest, SubscriptionRead, TokenResponse,
    UsageSummary, UserRead, UserUpdate, VerifyEmailRequest,
)
from app.services.auth_service import AuthService
from app.services.billing import BillingService

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["Auth & Billing"])

DB = Annotated[AsyncSession, Depends(get_db)]


# ── Auth ───────────────────────────────────────────────────────────────────────

@router.post("/auth/signup", response_model=TokenResponse, status_code=201,
    summary="Sign up with email and password")
async def signup(
    body: SignupRequest, db: DB,
) -> TokenResponse:
    svc = AuthService(db=db)
    return await svc.signup(body)


@router.post("/auth/login", response_model=TokenResponse,
    summary="Login with email and password")
async def login(
    body: LoginRequest, request: Request, db: DB,
) -> TokenResponse:
    svc = AuthService(db=db)
    return await svc.login(body, device_info=request.headers.get("user-agent", "")[:200])


@router.post("/auth/logout", status_code=204, summary="Logout and revoke refresh token")
async def logout(
    body: RefreshRequest, db: DB,
) -> None:
    svc = AuthService(db=db)
    await svc.logout(body.refresh_token)


@router.post("/auth/refresh", response_model=TokenResponse, summary="Refresh access token")
async def refresh(
    body: RefreshRequest, db: DB,
) -> TokenResponse:
    svc = AuthService(db=db)
    return await svc.refresh_tokens(body.refresh_token)


@router.get("/auth/google", summary="Get Google OAuth redirect URL")
async def google_auth_url(
    redirect_to: str = Query(default="/dashboard",
),
) -> dict:
    url = AuthService.build_google_auth_url(state=redirect_to)
    return {"auth_url": url}


@router.get("/auth/google/callback", include_in_schema=False)
async def google_callback(
    db: DB,
    code: str = Query(...,
),
    state: str = Query(default="/dashboard"),
) -> RedirectResponse:
    svc = AuthService(db=db)
    try:
        tokens = await svc.google_callback(code=code)
        # Redirect to frontend with tokens in query params
        # In production use httpOnly cookies instead
        from app.core.config import settings
        redirect_url = (
            f"{settings.FRONTEND_URL}/auth/callback"
            f"?access_token={tokens.access_token}"
            f"&refresh_token={tokens.refresh_token}"
            f"&redirect={state}"
        )
        return RedirectResponse(url=redirect_url)
    except Exception as e:
        from app.core.config import settings
        return RedirectResponse(url=f"{settings.FRONTEND_URL}/login?error=oauth_failed")


@router.post("/auth/verify-email", summary="Verify email address")
async def verify_email(
    body: VerifyEmailRequest, db: DB,
) -> dict:
    svc = AuthService(db=db)
    ok = await svc.verify_email(body.token)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")
    return {"message": "Email verified successfully"}


@router.post("/auth/forgot-password", status_code=202, summary="Send password reset email")
async def forgot_password(
    body: ForgotPasswordRequest, db: DB,
) -> dict:
    svc = AuthService(db=db)
    await svc.forgot_password(body.email)
    return {"message": "If that email exists, a reset link has been sent"}


@router.post("/auth/reset-password", summary="Reset password with token")
async def reset_password(
    body: ResetPasswordRequest, db: DB,
) -> dict:
    svc = AuthService(db=db)
    ok = await svc.reset_password(body.token, body.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    return {"message": "Password updated successfully"}


@router.get("/auth/me", response_model=UserRead, summary="Get current user profile")
async def get_me(
    auth: CurrentAuth,
) -> UserRead:
    return UserRead.model_validate(auth.user)


@router.patch("/auth/me", response_model=UserRead, summary="Update profile")
async def update_me(
    auth: CurrentAuth,
    db: DB,
    body: UserUpdate,
) -> UserRead:
    user = auth.user
    if body.name is not None:
        user.name = body.name
    if body.timezone is not None:
        user.timezone = body.timezone
    if body.avatar_url is not None:
        user.avatar_url = body.avatar_url
    await db.flush()
    return UserRead.model_validate(user)


# ── Billing ────────────────────────────────────────────────────────────────────

@router.get("/billing/plans", response_model=list[PlanRead], summary="List all plans")
async def list_plans(
    db: DB,
) -> list[PlanRead]:
    billing = BillingService(db=db)
    plans = await billing.get_all_plans()
    return [PlanRead.model_validate(p) for p in plans]


@router.get("/billing/subscription", response_model=SubscriptionRead,
    summary="Get current subscription")
async def get_subscription(
    auth: CurrentAuth, db: DB,
) -> SubscriptionRead:
    billing = BillingService(db=db)
    sub = await billing._get_active_subscription(auth.user_id)
    if not sub:
        raise HTTPException(status_code=404, detail="No active subscription found")
    return SubscriptionRead.model_validate(sub)


@router.get("/billing/usage/{workspace_id}", response_model=UsageSummary,
    summary="Get credit balance and usage for a workspace")
async def get_usage(
    auth: CurrentAuth,
    db: DB,
    workspace_id: uuid.UUID,
) -> UsageSummary:
    billing = BillingService(db=db)
    return await billing.get_usage_summary(auth.user_id, workspace_id)


@router.get("/billing/transactions/{workspace_id}",
    response_model=list[CreditTransactionRead],
    summary="Credit transaction history")
async def get_transactions(
    auth: CurrentAuth,
    db: DB,
    workspace_id: uuid.UUID,
    limit: int = Query(default=50, le=200,
),
) -> list[CreditTransactionRead]:
    result = await db.execute(
        select(CreditTransaction)
        .where(CreditTransaction.workspace_id == workspace_id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(limit)
    )
    txs = result.scalars().all()
    return [CreditTransactionRead.model_validate(t) for t in txs]


@router.post("/billing/checkout", response_model=CheckoutResponse,
    summary="Create Stripe checkout session to upgrade plan")
async def create_checkout(
    auth: CurrentAuth,
    db: DB,
    body: CreateCheckoutRequest,
) -> CheckoutResponse:
    billing = BillingService(db=db)
    return await billing.create_checkout_session(
        user_id=auth.user_id,
        plan_tier=body.plan_tier,
        billing_interval=body.billing_interval,
        workspace_id=body.workspace_id,
    )


@router.post("/billing/webhook", include_in_schema=False)
async def stripe_webhook(
    db: DB,
    request: Request,
    stripe_signature: str = Header(alias="stripe-signature", default="",
),
) -> dict:
    payload = await request.body()
    billing = BillingService(db=db)
    try:
        await billing.handle_stripe_webhook(payload, stripe_signature)
        return {"received": True}
    except Exception as e:
        log.error("stripe_webhook_error", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/billing/portal", summary="Get Stripe customer portal URL")
async def get_portal_url(
    auth: CurrentAuth, db: DB,
) -> dict:
    if not auth.subscription or not auth.subscription.stripe_customer_id:
        raise HTTPException(
            status_code=400,
            detail="No active paid subscription found",
        )
    from app.core.config import settings
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    session = stripe.billing_portal.Session.create(
        customer=auth.subscription.stripe_customer_id,
        return_url=f"{settings.FRONTEND_URL}/billing",
    )
    return {"portal_url": session.url}


# ── Credits ────────────────────────────────────────────────────────────────────

@router.get(
    "/credits/{workspace_id}",
    summary="Get real-time credit balance for a workspace",
    tags=["Credits"],
)
async def get_credit_balance(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
) -> dict:
    from app.services.credit_service import CreditService
    svc = CreditService(db=db)
    balance = await svc.get_balance_fast(workspace_id)
    usage   = await svc.get_balance_fast(workspace_id)
    return {
        "workspace_id": str(workspace_id),
        "balance": balance,
        "can_generate_caption": balance >= 1,
        "can_generate_with_image": balance >= 6,
        "can_plan_calendar": balance >= 10,
    }


@router.post(
    "/credits/{workspace_id}/check",
    summary="Check if workspace can afford a specific action",
    tags=["Credits"],
)
async def check_credit_action(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
    action: str = "content_generate",
) -> dict:
    from app.services.credit_service import CreditService
    from app.models.auth import CreditActionType, CREDIT_COSTS
    try:
        action_type = CreditActionType(action)
    except ValueError:
        action_type = CreditActionType.CONTENT_GENERATE

    svc     = CreditService(db=db)
    balance = await svc.get_balance_fast(workspace_id)
    cost    = CREDIT_COSTS.get(action_type, 1)

    return {
        "workspace_id":  str(workspace_id),
        "action":        action_type.value,
        "cost":          cost,
        "balance":       balance,
        "can_afford":    balance >= cost,
        "shortfall":     max(0, cost - balance),
    }
