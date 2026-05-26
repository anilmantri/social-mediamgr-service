"""
Instagram Graph API client.

Handles the full lifecycle:
  1. OAuth — exchange code for long-lived token
  2. Media publish — 3-step: upload → container → publish
  3. Insights — pull reach/engagement per post
  4. Token refresh — renew before 60-day expiry

All methods are async. Retries are handled with tenacity.
Rate-limit state is tracked in Redis (200 calls/hr per account).
"""
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.cache import get_cache_client

log = structlog.get_logger(__name__)

# Instagram Graph API field sets
ACCOUNT_FIELDS = "id,username,followers_count,media_count,profile_picture_url,biography"
INSIGHTS_FIELDS = "reach,impressions,likes,comments_count,saved,shares,profile_visits,follows"
MEDIA_FIELDS = "id,permalink,timestamp,media_type,like_count,comments_count"


class InstagramAPIError(Exception):
    def __init__(self, message: str, code: str = "unknown", http_status: int = 0):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class RateLimitError(InstagramAPIError):
    pass


class TokenExpiredError(InstagramAPIError):
    pass


class InstagramGraphClient:
    """
    Stateless async client. Pass the account's access_token per call.
    One instance can serve all accounts.
    """

    def __init__(self) -> None:
        self._base = settings.INSTAGRAM_BASE_URL
        self._version = settings.INSTAGRAM_API_VERSION
        self._app_id = settings.INSTAGRAM_APP_ID
        self._app_secret = settings.INSTAGRAM_APP_SECRET
        self._cache = get_cache_client()

    # ── OAuth ──────────────────────────────────────────────────────────────────

    async def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        """
        Exchange a short-lived auth code for a long-lived user access token.
        Returns: {access_token, token_type, expires_in (seconds)}
        """
        # Step 1: short-lived token
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base}/{self._version}/oauth/access_token",
                data={
                    "client_id": self._app_id,
                    "client_secret": self._app_secret,
                    "redirect_uri": settings.INSTAGRAM_REDIRECT_URI,
                    "code": code,
                },
            )
        short = self._raise_for_error(resp)

        # Step 2: exchange for long-lived (60-day) token
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._base}/{self._version}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": self._app_id,
                    "client_secret": self._app_secret,
                    "fb_exchange_token": short["access_token"],
                },
            )
        long_lived = self._raise_for_error(resp)
        log.info("instagram_token_exchanged")
        return long_lived

    async def refresh_long_lived_token(self, access_token: str) -> dict[str, Any]:
        """Refresh a long-lived token before it expires (valid for up to 60 days)."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._base}/{self._version}/oauth/access_token",
                params={
                    "grant_type": "ig_refresh_token",
                    "access_token": access_token,
                },
            )
        return self._raise_for_error(resp)

    # ── Account info ───────────────────────────────────────────────────────────

    async def get_account_info(self, ig_user_id: str, access_token: str) -> dict[str, Any]:
        return await self._get(
            f"/{ig_user_id}",
            params={"fields": ACCOUNT_FIELDS},
            token=access_token,
        )

    # ── Media publish (3-step) ─────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, InstagramAPIError)),
        stop=stop_after_attempt(settings.PUBLISHER_MAX_RETRIES),
        wait=wait_exponential(multiplier=2, min=5, max=60),
        reraise=True,
    )
    async def publish_image_post(
        self,
        ig_user_id: str,
        access_token: str,
        image_url: str,
        caption: str,
        hashtags: list[str],
    ) -> dict[str, Any]:
        """
        Full 3-step publish flow:
          1. Create media container (upload)
          2. Poll until container is FINISHED
          3. Publish container → live post

        Returns: {id, permalink}
        """
        await self._check_rate_limit(ig_user_id)

        full_caption = self._build_caption(caption, hashtags)

        # Step 1: Upload container
        log.info("ig_upload_container", user=ig_user_id)
        container = await self._post(
            f"/{ig_user_id}/media",
            data={
                "image_url": image_url,
                "caption": full_caption,
                "access_token": access_token,
            },
        )
        container_id = container["id"]
        log.info("ig_container_created", container_id=container_id)

        # Step 2: Poll until FINISHED (up to 60s)
        await self._wait_for_container(container_id, access_token)

        # Step 3: Publish
        log.info("ig_publishing", container_id=container_id)
        result = await self._post(
            f"/{ig_user_id}/media_publish",
            data={
                "creation_id": container_id,
                "access_token": access_token,
            },
        )
        media_id = result["id"]

        # Fetch permalink
        media_info = await self._get(
            f"/{media_id}",
            params={"fields": "id,permalink"},
            token=access_token,
        )
        log.info("ig_published", media_id=media_id, permalink=media_info.get("permalink"))
        return {"id": media_id, "permalink": media_info.get("permalink")}

    async def _wait_for_container(
        self, container_id: str, access_token: str, timeout_s: int = 60
    ) -> None:
        """Poll container status until FINISHED or ERROR."""
        deadline = time.monotonic() + timeout_s
        import asyncio
        while time.monotonic() < deadline:
            status_data = await self._get(
                f"/{container_id}",
                params={"fields": "status_code"},
                token=access_token,
            )
            status = status_data.get("status_code", "")
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise InstagramAPIError(
                    "Media container processing failed",
                    code="container_fail",
                    http_status=0,
                )
            await asyncio.sleep(3)
        raise InstagramAPIError(
            f"Container {container_id} did not finish within {timeout_s}s",
            code="container_timeout",
        )

    # ── Insights ───────────────────────────────────────────────────────────────

    async def get_post_insights(
        self, media_id: str, access_token: str
    ) -> dict[str, Any]:
        """
        Fetch Insights for a published post.
        Returns normalised dict with all engagement fields.
        """
        data = await self._get(
            f"/{media_id}/insights",
            params={
                "metric": "reach,impressions,saved,shares,profile_visits,follows",
                "period": "lifetime",
            },
            token=access_token,
        )
        # Also fetch like/comment counts from the media object
        media = await self._get(
            f"/{media_id}",
            params={"fields": "like_count,comments_count,timestamp"},
            token=access_token,
        )

        # Normalise the insights array [{name, values[{value}]}] into flat dict
        flat: dict[str, int] = {}
        for item in data.get("data", []):
            name = item["name"]
            values = item.get("values", [{}])
            flat[name] = values[0].get("value", 0) if values else 0

        return {
            "reach": flat.get("reach", 0),
            "impressions": flat.get("impressions", 0),
            "likes": media.get("like_count", 0),
            "comments": media.get("comments_count", 0),
            "saves": flat.get("saved", 0),
            "shares": flat.get("shares", 0),
            "profile_visits": flat.get("profile_visits", 0),
            "follows": flat.get("follows", 0),
            "published_at": media.get("timestamp"),
            "raw": data,
        }

    async def get_recent_media(
        self, ig_user_id: str, access_token: str, limit: int = 25
    ) -> list[dict]:
        """Fetch recent posts for seeding OptimalTimeSlot data."""
        data = await self._get(
            f"/{ig_user_id}/media",
            params={"fields": MEDIA_FIELDS, "limit": limit},
            token=access_token,
        )
        return data.get("data", [])

    # ── Rate limit tracking ────────────────────────────────────────────────────

    async def _check_rate_limit(self, ig_user_id: str) -> None:
        """
        Enforce Meta's 200 calls/hour limit per IG user.
        Uses Redis sliding window. Raises RateLimitError if over.
        """
        key = f"ig_rate:{ig_user_id}"
        window = 3600
        max_calls = settings.INSTAGRAM_RATE_LIMIT_PER_HOUR

        now = time.time()
        window_start = now - window

        cache = self._cache._redis
        pipe = cache.pipeline()
        pipe.zremrangebyscore(key, "-inf", window_start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, window + 1)
        results = await pipe.execute()
        count = results[1]

        if count >= max_calls:
            log.warning("ig_rate_limit_hit", user=ig_user_id, count=count)
            raise RateLimitError(
                f"Instagram rate limit reached ({count}/{max_calls} calls/hr)",
                code="rate_limited",
                http_status=429,
            )

    # ── HTTP helpers ───────────────────────────────────────────────────────────

    async def _get(
        self, path: str, params: dict | None = None, token: str | None = None
    ) -> dict:
        p = dict(params or {})
        if token:
            p["access_token"] = token
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{self._base}/{self._version}{path}", params=p)
        return self._raise_for_error(resp)

    async def _post(self, path: str, data: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._base}/{self._version}{path}", data=data or {}
            )
        return self._raise_for_error(resp)

    def _raise_for_error(self, resp: httpx.Response) -> dict:
        try:
            body = resp.json()
        except Exception:
            body = {}

        if resp.status_code == 401:
            raise TokenExpiredError(
                "Instagram access token expired or invalid",
                code="token_expired",
                http_status=401,
            )
        if resp.status_code == 429:
            raise RateLimitError(
                "Instagram API rate limit exceeded",
                code="rate_limited",
                http_status=429,
            )
        if not resp.is_success:
            err = body.get("error", {})
            raise InstagramAPIError(
                err.get("message", f"HTTP {resp.status_code}"),
                code=str(err.get("code", "unknown")),
                http_status=resp.status_code,
            )
        return body

    @staticmethod
    def _build_caption(caption: str, hashtags: list[str]) -> str:
        """Combine caption and hashtags into Instagram-formatted string."""
        if not hashtags:
            return caption
        tag_str = " ".join(f"#{t.lstrip('#')}" for t in hashtags)
        return f"{caption}\n\n{tag_str}"


# ── Singleton ─────────────────────────────────────────────────────────────────
_ig_client: InstagramGraphClient | None = None


def get_ig_client() -> InstagramGraphClient:
    global _ig_client
    if _ig_client is None:
        _ig_client = InstagramGraphClient()
    return _ig_client
