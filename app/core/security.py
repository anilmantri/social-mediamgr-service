"""
Security utilities — JWT creation/verification, password hashing, RBAC.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.core.config import settings
from app.models.content import WorkspaceRole


# ── Password hashing ──────────────────────────────────────────────────────────
# Using bcrypt directly — passlib 1.7.4 is incompatible with bcrypt 4.x
# bcrypt has a 72-byte limit so we truncate the encoded password before hashing

def hash_password(plain: str) -> str:
    """Hash a plain text password using bcrypt."""
    # Encode to bytes then truncate at 72 bytes (bcrypt hard limit)
    password_bytes = plain.encode("utf-8")[:72]
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password_bytes, salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plain text password against a bcrypt hash."""
    password_bytes = plain.encode("utf-8")[:72]
    hashed_bytes   = hashed.encode("utf-8")
    try:
        return bcrypt.checkpw(password_bytes, hashed_bytes)
    except Exception:
        return False


# ── JWT ───────────────────────────────────────────────────────────────────────

class TokenPayload:
    def __init__(self, sub: str, workspace_id: str | None, role: str | None) -> None:
        self.user_id      = uuid.UUID(sub)
        self.workspace_id = uuid.UUID(workspace_id) if workspace_id else None
        self.role         = WorkspaceRole(role) if role else None


def create_access_token(
    user_id: uuid.UUID,
    workspace_id: uuid.UUID | None = None,
    role: WorkspaceRole | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "type": "access",
    }
    if workspace_id:
        payload["workspace_id"] = str(workspace_id)
    if role:
        payload["role"] = role.value
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(user_id: uuid.UUID) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        "type": "refresh",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> TokenPayload:
    try:
        data = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        return TokenPayload(
            sub=data["sub"],
            workspace_id=data.get("workspace_id"),
            role=data.get("role"),
        )
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


# ── FastAPI dependencies ───────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=True)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> TokenPayload:
    return decode_token(credentials.credentials)


async def get_current_user_id(
    token: Annotated[TokenPayload, Depends(get_current_user)],
) -> uuid.UUID:
    return token.user_id


def require_role(*allowed_roles: WorkspaceRole):
    async def _check(
        token: Annotated[TokenPayload, Depends(get_current_user)]
    ) -> TokenPayload:
        if token.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{token.role}' not permitted. Required: {[r.value for r in allowed_roles]}",
            )
        return token
    return _check
