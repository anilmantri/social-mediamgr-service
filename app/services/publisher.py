"""
PostPublisherService — executes a single publish attempt.

Called by the Celery publisher task. Handles:
  - Pre-flight checks (token valid, image reachable)
  - Calling InstagramGraphClient.publish_image_post
  - Writing PostPublishLog (append-only, success or failure)
  - Updating ScheduledPost and DraftContent status
  - Calculating next retry time on failure
"""
import time
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.content import ContentStatus, DraftContent
from app.models.scheduler import (
    InstagramAccount,
    PostPublishLog,
    PublishErrorCode,
    PublishStatus,
    ScheduledPost,
)
from app.services.instagram_client import (
    InstagramAPIError,
    InstagramGraphClient,
    RateLimitError,
    TokenExpiredError,
    get_ig_client,
)

log = structlog.get_logger(__name__)


class PostPublisherService:
    def __init__(
        self,
        db: AsyncSession,
        ig_client: InstagramGraphClient | None = None,
    ) -> None:
        self._db = db
        self._ig = ig_client or get_ig_client()

    async def publish(self, scheduled_post_id: uuid.UUID) -> bool:
        """
        Attempt to publish a scheduled post.
        Returns True on success, False on failure.
        Always writes a PostPublishLog entry.
        """
        sp = await self._load_post(scheduled_post_id)
        if not sp:
            log.error("publisher_post_not_found", id=str(scheduled_post_id))
            return False

        # Guard: already published or being published
        if sp.publish_status in (PublishStatus.PUBLISHED, PublishStatus.PUBLISHING):
            log.warning("publisher_skip_already_done", status=sp.publish_status)
            return sp.publish_status == PublishStatus.PUBLISHED

        account = await self._load_account(sp.account_id)
        if not account:
            await self._write_log(sp, False, "account_not_found", "Instagram account not found", None, 0)
            return False

        # Mark as in-flight
        sp.publish_status = PublishStatus.PUBLISHING
        sp.attempt_count += 1
        sp.last_attempt_at = datetime.now(timezone.utc)
        await self._db.flush()

        start_ms = int(time.monotonic() * 1000)
        try:
            result = await self._ig.publish_image_post(
                ig_user_id=account.instagram_user_id,
                access_token=account.access_token,
                image_url=sp.image_url_snapshot,
                caption=sp.caption_snapshot,
                hashtags=sp.hashtags_snapshot,
            )
            duration_ms = int(time.monotonic() * 1000) - start_ms

            # ── Success ────────────────────────────────────────────────────────
            sp.publish_status = PublishStatus.PUBLISHED
            sp.ig_media_id = result["id"]
            sp.ig_permalink = result.get("permalink")
            sp.published_at = datetime.now(timezone.utc)

            await self._update_draft_published(sp.draft_content_id, result["id"])
            await self._write_log(
                sp, True, None, None,
                ig_media_id=result["id"],
                duration_ms=duration_ms,
                response_body=result,
            )
            await self._db.flush()

            log.info(
                "post_published",
                media_id=result["id"],
                permalink=result.get("permalink"),
            )
            return True

        except TokenExpiredError as exc:
            await self._handle_failure(
                sp, exc, PublishErrorCode.TOKEN_EXPIRED,
                int(time.monotonic() * 1000) - start_ms,
                fatal=True,  # don't retry until token is refreshed
            )
        except RateLimitError as exc:
            await self._handle_failure(
                sp, exc, PublishErrorCode.RATE_LIMITED,
                int(time.monotonic() * 1000) - start_ms,
                retry_delay_seconds=3600,  # back off for 1 hour
            )
        except InstagramAPIError as exc:
            code_map = {
                "media_upload_fail": PublishErrorCode.MEDIA_UPLOAD_FAIL,
                "container_fail": PublishErrorCode.CONTAINER_FAIL,
                "publish_fail": PublishErrorCode.PUBLISH_FAIL,
            }
            err_code = code_map.get(exc.code, PublishErrorCode.UNKNOWN)
            await self._handle_failure(
                sp, exc, err_code,
                int(time.monotonic() * 1000) - start_ms,
            )
        except Exception as exc:
            await self._handle_failure(
                sp, exc, PublishErrorCode.UNKNOWN,
                int(time.monotonic() * 1000) - start_ms,
            )

        return False

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _handle_failure(
        self,
        sp: ScheduledPost,
        exc: Exception,
        error_code: PublishErrorCode,
        duration_ms: int,
        fatal: bool = False,
        retry_delay_seconds: int | None = None,
    ) -> None:
        log.error(
            "publish_failed",
            scheduled_post_id=str(sp.id),
            attempt=sp.attempt_count,
            error=str(exc),
            code=error_code,
        )

        sp.publish_status = PublishStatus.FAILED

        # Schedule retry if attempts remaining and not fatal
        if not fatal and sp.attempt_count < settings.PUBLISHER_MAX_RETRIES:
            delay = retry_delay_seconds or (
                settings.PUBLISHER_RETRY_BACKOFF * (2 ** (sp.attempt_count - 1))
            )
            sp.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            sp.publish_status = PublishStatus.SCHEDULED  # will be picked up again

        await self._write_log(
            sp,
            success=False,
            error_code=error_code.value,
            error_message=str(exc),
            ig_media_id=None,
            duration_ms=duration_ms,
        )

        # Update draft to FAILED if out of retries
        if sp.publish_status == PublishStatus.FAILED:
            await self._update_draft_failed(sp.draft_content_id)

        await self._db.flush()

    async def _write_log(
        self,
        sp: ScheduledPost,
        success: bool,
        error_code: str | None,
        error_message: str | None,
        ig_media_id: str | None,
        duration_ms: int,
        response_body: dict | None = None,
        http_status: int | None = None,
    ) -> None:
        log_entry = PostPublishLog(
            scheduled_post_id=sp.id,
            attempt_number=sp.attempt_count,
            success=success,
            api_endpoint=f"/{sp.account_id}/media_publish",
            request_payload={
                "image_url": sp.image_url_snapshot,
                "caption_length": len(sp.caption_snapshot),
                "hashtag_count": len(sp.hashtags_snapshot),
            },
            http_status=http_status or (200 if success else 0),
            response_body=response_body,
            ig_media_id=ig_media_id,
            error_code=PublishErrorCode(error_code) if error_code else None,
            error_message=error_message,
            duration_ms=duration_ms,
        )
        self._db.add(log_entry)

    async def _update_draft_published(
        self, draft_id: uuid.UUID, ig_media_id: str
    ) -> None:
        result = await self._db.execute(
            select(DraftContent).where(DraftContent.id == draft_id)
        )
        draft = result.scalar_one_or_none()
        if draft:
            draft.status = ContentStatus.PUBLISHED
            draft.published_at = datetime.now(timezone.utc)
            draft.instagram_post_id = ig_media_id

    async def _update_draft_failed(self, draft_id: uuid.UUID) -> None:
        result = await self._db.execute(
            select(DraftContent).where(DraftContent.id == draft_id)
        )
        draft = result.scalar_one_or_none()
        if draft:
            draft.status = ContentStatus.FAILED

    async def _load_post(self, post_id: uuid.UUID) -> ScheduledPost | None:
        result = await self._db.execute(
            select(ScheduledPost).where(ScheduledPost.id == post_id)
        )
        return result.scalar_one_or_none()

    async def _load_account(self, account_id: uuid.UUID) -> InstagramAccount | None:
        result = await self._db.execute(
            select(InstagramAccount).where(InstagramAccount.id == account_id)
        )
        return result.scalar_one_or_none()
