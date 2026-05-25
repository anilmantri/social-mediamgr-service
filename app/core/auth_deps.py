"""
Real auth dependencies — replaces the stub in content.py and scheduler.py.
Import these instead of the local stubs.
"""
import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import decode_token
from app.db.session import get_db
from app.models.auth import Subscription, SubscriptionStatus, User

_bearer = HTTPBearer(auto_error=True)


class AuthenticatedUser:
    """Resolved from JWT — attached to every authenticated request."""
    def __init__(self, user: User, subscription: Subscription | None) -> None:
        self.user         = user
        self.user_id      = user.id
        self.subscription = subscription
        self.plan         = subscription.plan if subscription else None

    @property
    def is_subscribed(self) -> bool:
        return self.subscription is not None and self.subscription.is_active

    @property
    def can_schedule(self) -> bool:
        return self.plan.can_schedule if self.plan else False

    @property
    def can_use_analytics(self) -> bool:
        return self.plan.can_use_analytics if self.plan else False


async def get_current_auth(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthenticatedUser:
    """Full auth dependency — validates JWT, loads user + subscription."""
    token_payload = decode_token(credentials.credentials)

    result = await db.execute(
        select(User)
        .where(User.id == token_payload.user_id, User.is_active == True)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    # Load active subscription with plan
    sub_result = await db.execute(
        select(Subscription)
        .where(
            Subscription.user_id == user.id,
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
    subscription = sub_result.scalars().first()

    return AuthenticatedUser(user=user, subscription=subscription)


async def get_current_user_id(
    auth: Annotated[AuthenticatedUser, Depends(get_current_auth)],
) -> uuid.UUID:
    """Drop-in replacement for the old stub — returns user UUID."""
    return auth.user_id


async def get_current_user(
    auth: Annotated[AuthenticatedUser, Depends(get_current_auth)],
) -> User:
    return auth.user


# ── Typed aliases ─────────────────────────────────────────────────────────────
CurrentAuth = Annotated[AuthenticatedUser, Depends(get_current_auth)]
CurrentUserId = Annotated[uuid.UUID, Depends(get_current_user_id)]