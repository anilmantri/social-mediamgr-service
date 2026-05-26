"""
Unit tests for PostPublisherService — publish flow, retries, error codes.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.scheduler import PublishErrorCode, PublishStatus
from app.services.instagram_client import (
    InstagramAPIError,
    RateLimitError,
    TokenExpiredError,
)


def make_scheduled_post(status=PublishStatus.SCHEDULED):
    sp = MagicMock()
    sp.id = uuid.uuid4()
    sp.account_id = uuid.uuid4()
    sp.draft_content_id = uuid.uuid4()
    sp.workspace_id = uuid.uuid4()
    sp.publish_status = status
    sp.attempt_count = 0
    sp.caption_snapshot = "Great caption 🔥"
    sp.hashtags_snapshot = ["fitness", "health"]
    sp.image_url_snapshot = "https://cdn.example.com/image.png"
    sp.ig_media_id = None
    sp.ig_permalink = None
    sp.published_at = None
    sp.last_attempt_at = None
    sp.next_retry_at = None
    return sp


def make_account():
    a = MagicMock()
    a.id = uuid.uuid4()
    a.instagram_user_id = "987654"
    a.access_token = "EAAtest_token"
    return a


@pytest.mark.unit
@pytest.mark.asyncio
class TestPostPublisherService:

    @pytest.fixture
    def db(self):
        db = MagicMock()
        db.flush = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def mock_ig(self):
        client = MagicMock()
        client.publish_image_post = AsyncMock(return_value={
            "id": "17896129349180552",
            "permalink": "https://www.instagram.com/p/ABC123/",
        })
        return client

    async def test_successful_publish_updates_status(self, db, mock_ig):
        from app.services.publisher import PostPublisherService

        sp = make_scheduled_post()
        account = make_account()

        svc = PostPublisherService(db=db, ig_client=mock_ig)
        with patch.object(svc, "_load_post", AsyncMock(return_value=sp)), \
             patch.object(svc, "_load_account", AsyncMock(return_value=account)), \
             patch.object(svc, "_update_draft_published", AsyncMock()):
            result = await svc.publish(sp.id)

        assert result is True
        assert sp.publish_status == PublishStatus.PUBLISHED
        assert sp.ig_media_id == "17896129349180552"
        assert sp.published_at is not None

    async def test_successful_publish_writes_log(self, db, mock_ig):
        from app.services.publisher import PostPublisherService

        sp = make_scheduled_post()
        account = make_account()

        svc = PostPublisherService(db=db, ig_client=mock_ig)
        with patch.object(svc, "_load_post", AsyncMock(return_value=sp)), \
             patch.object(svc, "_load_account", AsyncMock(return_value=account)), \
             patch.object(svc, "_update_draft_published", AsyncMock()):
            await svc.publish(sp.id)

        db.add.assert_called()  # PostPublishLog added

    async def test_token_expired_marks_failed_no_retry(self, db):
        from app.services.publisher import PostPublisherService
        from app.core.config import settings

        sp = make_scheduled_post()
        sp.attempt_count = 0
        account = make_account()

        mock_ig = MagicMock()
        mock_ig.publish_image_post = AsyncMock(
            side_effect=TokenExpiredError("Token expired", code="token_expired", http_status=401)
        )

        svc = PostPublisherService(db=db, ig_client=mock_ig)
        with patch.object(svc, "_load_post", AsyncMock(return_value=sp)), \
             patch.object(svc, "_load_account", AsyncMock(return_value=account)), \
             patch.object(svc, "_update_draft_failed", AsyncMock()):
            result = await svc.publish(sp.id)

        assert result is False
        # Token expired is fatal — should stay FAILED, not get rescheduled
        assert sp.publish_status == PublishStatus.FAILED
        assert sp.next_retry_at is None

    async def test_rate_limit_reschedules_for_one_hour(self, db):
        from app.services.publisher import PostPublisherService
        from datetime import timedelta

        sp = make_scheduled_post()
        sp.attempt_count = 0
        account = make_account()

        mock_ig = MagicMock()
        mock_ig.publish_image_post = AsyncMock(
            side_effect=RateLimitError("Rate limited", code="rate_limited", http_status=429)
        )

        svc = PostPublisherService(db=db, ig_client=mock_ig)
        with patch.object(svc, "_load_post", AsyncMock(return_value=sp)), \
             patch.object(svc, "_load_account", AsyncMock(return_value=account)):
            result = await svc.publish(sp.id)

        assert result is False
        # Rate limited: should be rescheduled 1 hour from now
        assert sp.publish_status == PublishStatus.SCHEDULED
        assert sp.next_retry_at is not None
        gap = sp.next_retry_at - datetime.now(timezone.utc)
        assert timedelta(minutes=50) < gap < timedelta(minutes=70)

    async def test_retries_exhaust_to_failed(self, db):
        from app.services.publisher import PostPublisherService
        from app.core.config import settings

        sp = make_scheduled_post()
        sp.attempt_count = settings.PUBLISHER_MAX_RETRIES  # already at max
        account = make_account()

        mock_ig = MagicMock()
        mock_ig.publish_image_post = AsyncMock(
            side_effect=InstagramAPIError("Server error", code="unknown", http_status=500)
        )

        svc = PostPublisherService(db=db, ig_client=mock_ig)
        with patch.object(svc, "_load_post", AsyncMock(return_value=sp)), \
             patch.object(svc, "_load_account", AsyncMock(return_value=account)), \
             patch.object(svc, "_update_draft_failed", AsyncMock()):
            result = await svc.publish(sp.id)

        assert result is False
        assert sp.publish_status == PublishStatus.FAILED

    async def test_skips_already_published_post(self, db, mock_ig):
        from app.services.publisher import PostPublisherService

        sp = make_scheduled_post(status=PublishStatus.PUBLISHED)

        svc = PostPublisherService(db=db, ig_client=mock_ig)
        with patch.object(svc, "_load_post", AsyncMock(return_value=sp)):
            result = await svc.publish(sp.id)

        assert result is True
        mock_ig.publish_image_post.assert_not_called()

    async def test_returns_false_for_missing_post(self, db, mock_ig):
        from app.services.publisher import PostPublisherService

        svc = PostPublisherService(db=db, ig_client=mock_ig)
        with patch.object(svc, "_load_post", AsyncMock(return_value=None)):
            result = await svc.publish(uuid.uuid4())

        assert result is False


@pytest.mark.unit
class TestInstagramClientCaptionBuilder:

    def test_builds_caption_with_hashtags(self):
        from app.services.instagram_client import InstagramGraphClient
        result = InstagramGraphClient._build_caption(
            "Great post!", ["fitness", "#health", "gym"]
        )
        assert "Great post!" in result
        assert "#fitness" in result
        assert "#health" in result
        assert "#gym" in result

    def test_no_double_hash(self):
        from app.services.instagram_client import InstagramGraphClient
        result = InstagramGraphClient._build_caption("Post", ["#tag"])
        assert "##tag" not in result
        assert "#tag" in result

    def test_empty_hashtags_returns_caption_only(self):
        from app.services.instagram_client import InstagramGraphClient
        result = InstagramGraphClient._build_caption("Just a caption", [])
        assert result == "Just a caption"
