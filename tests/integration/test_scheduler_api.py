"""
Integration tests for Module 2 — Scheduler API endpoints.
Uses in-memory SQLite, mocks Instagram client and Celery.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from app.models.content import ContentStatus, ContentType
from app.models.scheduler import InstagramAccount, PublishStatus, ScheduledPost


def utc_future(minutes: int = 60) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
async def instagram_account(db_session, workspace):
    account = InstagramAccount(
        workspace_id=workspace.id,
        instagram_user_id="12345678",
        instagram_username="testbrand_ig",
        access_token="EAA_test_token",
        followers_count=1500,
        media_count=42,
        is_active=True,
    )
    db_session.add(account)
    await db_session.flush()
    return account


@pytest.fixture
async def approved_draft_with_version(db_session, workspace):
    """An approved DraftContent with a ContentVersion containing an image."""
    from app.models.content import ContentVersion, DraftContent, ToneType

    draft = DraftContent(
        workspace_id=workspace.id,
        created_by_id=uuid.uuid4(),
        content_type=ContentType.FEED_IMAGE,
        status=ContentStatus.APPROVED,
        generation_prompt="Morning fitness motivation",
    )
    db_session.add(draft)
    await db_session.flush()

    version = ContentVersion(
        draft_content_id=draft.id,
        version_number=1,
        created_by_id=uuid.uuid4(),
        caption="Rise and grind! 💪",
        hashtags=["fitness", "morning"],
        image_url="https://cdn.example.com/generated/test.png",
        is_ai_generated=True,
    )
    db_session.add(version)
    await db_session.flush()
    draft.current_version_id = version.id
    return draft


@pytest.fixture
async def scheduled_post(db_session, workspace, instagram_account, approved_draft_with_version):
    sp = ScheduledPost(
        workspace_id=workspace.id,
        draft_content_id=approved_draft_with_version.id,
        account_id=instagram_account.id,
        scheduled_by_id=uuid.uuid4(),
        scheduled_at=utc_future(120),
        publish_status=PublishStatus.SCHEDULED,
        caption_snapshot="Rise and grind! 💪",
        hashtags_snapshot=["fitness", "morning"],
        image_url_snapshot="https://cdn.example.com/generated/test.png",
    )
    db_session.add(sp)
    await db_session.flush()
    approved_draft_with_version.status = ContentStatus.SCHEDULED
    return sp


# ── Instagram account tests ────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
class TestInstagramAccountEndpoints:

    async def test_get_account_404_when_none_connected(
        self, async_client: AsyncClient, workspace
    ):
        response = await async_client.get(
            f"/api/v1/instagram/account/{workspace.id}"
        )
        assert response.status_code == 404

    async def test_get_account_returns_connected_account(
        self, async_client: AsyncClient, workspace, instagram_account
    ):
        response = await async_client.get(
            f"/api/v1/instagram/account/{workspace.id}"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["instagram_username"] == "testbrand_ig"
        assert data["followers_count"] == 1500

    async def test_disconnect_account(
        self, async_client: AsyncClient, workspace, instagram_account
    ):
        response = await async_client.delete(
            f"/api/v1/instagram/account/{workspace.id}"
        )
        assert response.status_code == 204

        # Confirm it's now inactive
        get_resp = await async_client.get(
            f"/api/v1/instagram/account/{workspace.id}"
        )
        assert get_resp.status_code == 404

    async def test_get_auth_url_returns_url(
        self, async_client: AsyncClient, workspace
    ):
        response = await async_client.get(
            "/api/v1/instagram/auth-url",
            params={"workspace_id": str(workspace.id)},
        )
        assert response.status_code == 200
        data = response.json()
        assert "auth_url" in data
        assert "facebook.com/dialog/oauth" in data["auth_url"]


# ── Schedule / Unschedule tests ────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
class TestScheduleEndpoints:

    async def test_schedule_approved_post(
        self,
        async_client: AsyncClient,
        workspace,
        instagram_account,
        approved_draft_with_version,
    ):
        response = await async_client.post(
            "/api/v1/schedule",
            json={
                "workspace_id": str(workspace.id),
                "draft_content_id": str(approved_draft_with_version.id),
                "scheduled_at": utc_future(120).isoformat(),
                "use_optimal_time": False,
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["publish_status"] == "scheduled"
        assert data["caption_snapshot"] == "Rise and grind! 💪"

    async def test_cannot_schedule_non_approved_draft(
        self,
        async_client: AsyncClient,
        workspace,
        instagram_account,
        draft_content,   # fixture from conftest — status is DRAFT
    ):
        response = await async_client.post(
            "/api/v1/schedule",
            json={
                "workspace_id": str(workspace.id),
                "draft_content_id": str(draft_content.id),
                "scheduled_at": utc_future(60).isoformat(),
            },
        )
        assert response.status_code == 400
        assert "APPROVED" in response.json()["detail"]

    async def test_schedule_requires_future_datetime(
        self,
        async_client: AsyncClient,
        workspace,
        approved_draft_with_version,
    ):
        past_dt = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        response = await async_client.post(
            "/api/v1/schedule",
            json={
                "workspace_id": str(workspace.id),
                "draft_content_id": str(approved_draft_with_version.id),
                "scheduled_at": past_dt,
            },
        )
        assert response.status_code == 422

    async def test_unschedule_post(
        self,
        async_client: AsyncClient,
        workspace,
        scheduled_post,
    ):
        response = await async_client.delete(
            f"/api/v1/schedule/{scheduled_post.id}",
            params={"workspace_id": str(workspace.id)},
        )
        assert response.status_code == 204

    async def test_reschedule_post(
        self,
        async_client: AsyncClient,
        workspace,
        scheduled_post,
    ):
        new_time = utc_future(240).isoformat()
        response = await async_client.patch(
            f"/api/v1/schedule/{scheduled_post.id}/reschedule",
            params={"workspace_id": str(workspace.id)},
            json={"new_scheduled_at": new_time},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["publish_status"] == "scheduled"

    async def test_list_scheduled_posts(
        self, async_client: AsyncClient, workspace, scheduled_post
    ):
        response = await async_client.get(
            "/api/v1/schedule",
            params={"workspace_id": str(workspace.id)},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 1
        assert any(p["id"] == str(scheduled_post.id) for p in data["items"])

    async def test_list_filter_by_status(
        self, async_client: AsyncClient, workspace, scheduled_post
    ):
        response = await async_client.get(
            "/api/v1/schedule",
            params={
                "workspace_id": str(workspace.id),
                "publish_status": "published",  # none are published
            },
        )
        data = response.json()
        assert data["total"] == 0


# ── Calendar tests ────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
class TestCalendarEndpoints:

    async def test_calendar_month_returns_correct_days(
        self, async_client: AsyncClient, workspace
    ):
        response = await async_client.get(
            f"/api/v1/calendar/{workspace.id}/2025/6"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["year"] == 2025
        assert data["month"] == 6
        assert len(data["days"]) == 30  # June has 30 days

    async def test_calendar_invalid_month(
        self, async_client: AsyncClient, workspace
    ):
        response = await async_client.get(
            f"/api/v1/calendar/{workspace.id}/2025/13"
        )
        assert response.status_code == 400


# ── Optimal time tests ────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
class TestOptimalTimeEndpoints:

    async def test_optimal_time_returns_defaults_when_no_account(
        self, async_client: AsyncClient, workspace
    ):
        response = await async_client.get(
            f"/api/v1/optimal-time/{workspace.id}"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["is_reliable"] is False
        assert len(data["suggested_slots"]) > 0

    async def test_recompute_optimal_time(
        self, async_client: AsyncClient, workspace
    ):
        response = await async_client.post(
            f"/api/v1/optimal-time/{workspace.id}/recompute"
        )
        assert response.status_code == 200
        assert "slots_updated" in response.json()


# ── Evergreen tests ────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
class TestEvergreenEndpoints:

    async def test_list_evergreen_empty_initially(
        self, async_client: AsyncClient, workspace
    ):
        response = await async_client.get(
            f"/api/v1/evergreen/{workspace.id}"
        )
        assert response.status_code == 200
        assert response.json() == []
