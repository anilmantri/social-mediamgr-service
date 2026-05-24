"""
AuthService — handles the full auth lifecycle:
  - Email/password signup with verification email
  - Login with JWT + refresh token
  - Google OAuth 2.0
  - Token refresh and revocation
  - Password reset flow
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token, create_refresh_token,
    hash_password, verify_password, decode_token,
)
from app.models.auth import AuthProvider, User, UserSession
from app.schemas.auth import SignupRequest, LoginRequest, TokenResponse
from app.services.billing import BillingService

log = structlog.get_logger(__name__)

GOOGLE_TOKEN_URL   = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_AUTH_URL    = "https://accounts.google.com/o/oauth2/v2/auth"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Signup ────────────────────────────────────────────────────────────────

    async def signup(self, data: SignupRequest) -> TokenResponse:
        # Check if email already exists
        existing = await self._get_user_by_email(data.email)
        if existing:
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists",
            )

        verify_token = secrets.token_urlsafe(32)

        user = User(
            email=data.email.lower(),
            name=data.name,
            hashed_password=hash_password(data.password),
            auth_provider=AuthProvider.EMAIL,
            email_verified=False,
            email_verify_token=_hash_token(verify_token),
        )
        self._db.add(user)
        await self._db.flush()

        # Provision free plan
        billing = BillingService(self._db)
        await billing.provision_free_plan(user.id)

        # Send verification email (non-blocking)
        await self._send_verification_email(user.email, user.name, verify_token)

        log.info("user_signed_up", user_id=str(user.id), email=user.email)
        return await self._create_session(user, device_info="signup")

    # ── Email verification ────────────────────────────────────────────────────

    async def verify_email(self, token: str) -> bool:
        hashed = _hash_token(token)
        result = await self._db.execute(
            select(User).where(User.email_verify_token == hashed)
        )
        user = result.scalar_one_or_none()
        if not user:
            return False
        user.email_verified = True
        user.email_verify_token = None
        await self._db.flush()
        log.info("email_verified", user_id=str(user.id))
        return True

    # ── Login ─────────────────────────────────────────────────────────────────

    async def login(
        self, data: LoginRequest, device_info: str | None = None
    ) -> TokenResponse:
        from fastapi import HTTPException, status as http_status
        user = await self._get_user_by_email(data.email)
        if not user or not user.hashed_password:
            raise HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )
        if not verify_password(data.password, user.hashed_password):
            raise HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )
        if not user.is_active:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="Account is disabled",
            )

        user.last_login_at = datetime.now(timezone.utc)
        await self._db.flush()
        log.info("user_logged_in", user_id=str(user.id))
        return await self._create_session(user, device_info=device_info)

    # ── Google OAuth ──────────────────────────────────────────────────────────

    def get_google_auth_url(self, state: str | None = None) -> str:
        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "select_account",
        }
        if state:
            params["state"] = state
        from urllib.parse import urlencode
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    async def google_callback(self, code: str) -> TokenResponse:
        # Exchange code for tokens
        async with httpx.AsyncClient(timeout=30) as client:
            token_resp = await client.post(GOOGLE_TOKEN_URL, data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            })
            token_resp.raise_for_status()
            tokens = token_resp.json()

            # Get user info
            user_resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            user_resp.raise_for_status()
            info = user_resp.json()

        email      = info["email"].lower()
        google_id  = info["sub"]
        name       = info.get("name", email.split("@")[0])
        avatar_url = info.get("picture")

        # Find or create user
        user = await self._get_user_by_google_id(google_id)
        if not user:
            user = await self._get_user_by_email(email)

        if user:
            # Update google linking
            user.google_id  = google_id
            user.avatar_url = avatar_url
            user.email_verified = True
            if user.auth_provider == AuthProvider.EMAIL:
                user.auth_provider = AuthProvider.GOOGLE
        else:
            user = User(
                email=email,
                name=name,
                avatar_url=avatar_url,
                google_id=google_id,
                auth_provider=AuthProvider.GOOGLE,
                email_verified=True,
            )
            self._db.add(user)
            await self._db.flush()

            billing = BillingService(self._db)
            await billing.provision_free_plan(user.id)

        user.last_login_at = datetime.now(timezone.utc)
        await self._db.flush()
        log.info("google_login", user_id=str(user.id))
        return await self._create_session(user, device_info="google-oauth")

    # ── Token refresh ─────────────────────────────────────────────────────────

    async def refresh_tokens(self, refresh_token: str) -> TokenResponse:
        from fastapi import HTTPException, status as http_status
        hashed = _hash_token(refresh_token)
        result = await self._db.execute(
            select(UserSession)
            .where(
                UserSession.refresh_token_hash == hashed,
                UserSession.revoked == False,
                UserSession.expires_at > datetime.now(timezone.utc),
            )
        )
        session = result.scalar_one_or_none()
        if not session:
            raise HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token",
            )

        # Rotate refresh token
        session.revoked = True
        user_result = await self._db.execute(
            select(User).where(User.id == session.user_id)
        )
        user = user_result.scalar_one()
        return await self._create_session(user)

    async def logout(self, refresh_token: str) -> None:
        hashed = _hash_token(refresh_token)
        result = await self._db.execute(
            select(UserSession).where(UserSession.refresh_token_hash == hashed)
        )
        session = result.scalar_one_or_none()
        if session:
            session.revoked = True
            await self._db.flush()

    # ── Password reset ────────────────────────────────────────────────────────

    async def forgot_password(self, email: str) -> None:
        """Always returns success — never reveals if email exists."""
        user = await self._get_user_by_email(email)
        if not user or user.auth_provider == AuthProvider.GOOGLE:
            return  # silent

        token = secrets.token_urlsafe(32)
        user.password_reset_token   = _hash_token(token)
        user.password_reset_expires = datetime.now(timezone.utc) + timedelta(hours=2)
        await self._db.flush()
        await self._send_password_reset_email(user.email, user.name, token)

    async def reset_password(self, token: str, new_password: str) -> bool:
        hashed = _hash_token(token)
        result = await self._db.execute(
            select(User).where(
                User.password_reset_token == hashed,
                User.password_reset_expires > datetime.now(timezone.utc),
            )
        )
        user = result.scalar_one_or_none()
        if not user:
            return False
        user.hashed_password      = hash_password(new_password)
        user.password_reset_token = None
        user.password_reset_expires = None
        await self._db.flush()
        return True

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _create_session(
        self, user: User, device_info: str | None = None, ip: str | None = None
    ) -> TokenResponse:
        refresh_token = secrets.token_urlsafe(48)

        session = UserSession(
            user_id=user.id,
            refresh_token_hash=_hash_token(refresh_token),
            device_info=device_info,
            ip_address=ip,
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            last_used_at=datetime.now(timezone.utc),
        )
        self._db.add(session)
        await self._db.flush()

        access_token = create_access_token(user_id=user.id)
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    async def _get_user_by_email(self, email: str) -> User | None:
        result = await self._db.execute(
            select(User).where(User.email == email.lower())
        )
        return result.scalar_one_or_none()

    async def _get_user_by_google_id(self, google_id: str) -> User | None:
        result = await self._db.execute(
            select(User).where(User.google_id == google_id)
        )
        return result.scalar_one_or_none()

    async def _send_verification_email(
        self, email: str, name: str, token: str
    ) -> None:
        """Send email verification link. Uses SMTP configured in settings."""
        verify_url = f"{settings.FRONTEND_URL}/verify-email?token={token}"
        await self._send_email(
            to=email,
            subject="Verify your email — Social Media Manager",
            body=f"""Hi {name},

Welcome to Social Media Manager! Please verify your email address to get started.

Click here to verify: {verify_url}

This link expires in 24 hours.

If you didn't create an account, you can safely ignore this email.

— Social Media Manager Team""",
        )

    async def _send_password_reset_email(
        self, email: str, name: str, token: str
    ) -> None:
        reset_url = f"{settings.FRONTEND_URL}/reset-password?token={token}"
        await self._send_email(
            to=email,
            subject="Reset your password — Social Media Manager",
            body=f"""Hi {name},

We received a request to reset your password.

Click here to reset: {reset_url}

This link expires in 2 hours. If you didn't request a reset, ignore this email.

— Social Media Manager Team""",
        )

    async def _send_email(self, to: str, subject: str, body: str) -> None:
        """Send plain-text email via SMTP (Mailhog locally, SendGrid/SES in prod)."""
        import smtplib
        from email.mime.text import MIMEText
        import asyncio

        def _send():
            try:
                msg = MIMEText(body)
                msg["Subject"] = subject
                msg["From"]    = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
                msg["To"]      = to
                with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as smtp:
                    if settings.SMTP_USER:
                        smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                    smtp.send_message(msg)
                log.info("email_sent", to=to, subject=subject)
            except Exception as e:
                log.warning("email_send_failed", to=to, error=str(e))

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _send)
