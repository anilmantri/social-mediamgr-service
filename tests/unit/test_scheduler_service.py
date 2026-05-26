"""
Unit tests for Module 2 — SchedulerService and OptimalTimeService.
No real DB or Instagram API calls.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.content import ContentStatus
from app.models.scheduler import PublishStatus
from app.services.scheduler import SchedulerService


# ── Helpers ────────────────────────────────────────────────────────────────────

def utc_future(minutes: int = 30) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def utc_past(minutes: int = 30) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def mock_draft(status=ContentStatus.APPROVED):
    d = MagicMock()
    d.id = uuid.uuid4()
    d.workspace_id = uuid.uuid4()
    d.status = status
    d.scheduled_at = None
    d.current_version_id = uuid.uuid4()
    return d


def mock_account():
    a = MagicMock()
    a.id = uuid.uuid4()
    a.instagram_user_id = "123456"
    a.access_token = "EAAtest"
    return a


def mock_version(has_image=True):
    v = MagicMock()
    v.caption = "Test caption"
    v.hashtags = ["test", "caption"]
    v.image_url = "https://cdn.example.com/img.png" if has_image else None
    return v


# ── SchedulerService unit tests ────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
class TestSchedulerServicePreflight:

    @pytest.fixture
    def db(self):
        db = MagicMock()
        db.flush = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock()
        return db

    async def test_raises_when_draft_not_approved(self, db):
        svc = SchedulerService(db=db)
        draft = mock_draft(status=ContentStatus.DRAFT)

        with patch.object(svc, "_load_approved_draft", AsyncMock(
            side_effect=ValueError("Draft must be APPROVED to schedule")
        )):
            with pytest.raises(ValueError, match="APPROVED"):
                await svc.schedule_post(
                    workspace_id=draft.workspace_id,
                    draft_content_id=draft.id,
                    scheduled_at=utc_future(),
                    scheduled_by_id=uuid.uuid4(),
                )

    async def test_raises_when_no_instagram_account(self, db):
        svc = SchedulerService(db=db)
        draft = mock_draft()

        with patch.object(svc, "_load_approved_draft", AsyncMock(return_value=draft)), \
             patch.object(svc, "_load_active_account", AsyncMock(
                 side_effect=ValueError("No active Instagram account")
             )):
            with pytest.raises(ValueError, match="Instagram account"):
                await svc.schedule_post(
                    workspace_id=draft.workspace_id,
                    draft_content_id=draft.id,
                    scheduled_at=utc_future(),
                    scheduled_by_id=uuid.uuid4(),
                )

    async def test_raises_when_no_image(self, db):
        svc = SchedulerService(db=db)
        draft = mock_draft()

        with patch.object(svc, "_load_approved_draft", AsyncMock(return_value=draft)), \
             patch.object(svc, "_load_active_account", AsyncMock(return_value=mock_account())), \
             patch.object(svc, "_load_current_version", AsyncMock(
                 side_effect=ValueError("Draft has no image")
             )):
            with pytest.raises(ValueError, match="image"):
                await svc.schedule_post(
                    workspace_id=draft.workspace_id,
                    draft_content_id=draft.id,
                    scheduled_at=utc_future(),
                    scheduled_by_id=uuid.uuid4(),
                )

    async def test_raises_when_already_scheduled(self, db):
        svc = SchedulerService(db=db)
        draft = mock_draft()
        existing_sp = MagicMock()

        # Simulate existing scheduled post found
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=existing_sp)
        db.execute = AsyncMock(return_value=mock_result)

        with patch.object(svc, "_load_approved_draft", AsyncMock(return_value=draft)), \
             patch.object(svc, "_load_active_account", AsyncMock(return_value=mock_account())), \
             patch.object(svc, "_load_current_version", AsyncMock(return_value=mock_version())):
            with pytest.raises(ValueError, match="already scheduled"):
                await svc.schedule_post(
                    workspace_id=draft.workspace_id,
                    draft_content_id=draft.id,
                    scheduled_at=utc_future(),
                    scheduled_by_id=uuid.uuid4(),
                )

    async def test_schedule_updates_draft_status(self, db):
        svc = SchedulerService(db=db)
        draft = mock_draft()
        account = mock_account()
        version = mock_version()
        scheduled_at = utc_future(minutes=60)

        # No existing scheduled post
        no_existing = MagicMock()
        no_existing.scalar_one_or_none = MagicMock(return_value=None)
        db.execute = AsyncMock(return_value=no_existing)

        with patch.object(svc, "_load_approved_draft", AsyncMock(return_value=draft)), \
             patch.object(svc, "_load_active_account", AsyncMock(return_value=account)), \
             patch.object(svc, "_load_current_version", AsyncMock(return_value=version)):
            result = await svc.schedule_post(
                workspace_id=draft.workspace_id,
                draft_content_id=draft.id,
                scheduled_at=scheduled_at,
                scheduled_by_id=uuid.uuid4(),
            )

        assert draft.status == ContentStatus.SCHEDULED
        assert draft.scheduled_at == scheduled_at
        assert result.publish_status == PublishStatus.SCHEDULED
        assert result.caption_snapshot == version.caption
        assert result.hashtags_snapshot == version.hashtags
        assert result.image_url_snapshot == version.image_url


@pytest.mark.unit
@pytest.mark.asyncio
class TestUnschedulePost:

    @pytest.fixture
    def db(self):
        db = MagicMock()
        db.flush = AsyncMock()
        db.execute = AsyncMock()
        return db

    async def test_cannot_unschedule_publishing(self, db):
        svc = SchedulerService(db=db)
        sp = MagicMock()
        sp.publish_status = PublishStatus.PUBLISHING

        with patch.object(svc, "_load_scheduled_post", AsyncMock(return_value=sp)):
            with pytest.raises(ValueError, match="currently being published"):
                await svc.unschedule_post(uuid.uuid4(), uuid.uuid4())

    async def test_cannot_unschedule_published(self, db):
        svc = SchedulerService(db=db)
        sp = MagicMock()
        sp.publish_status = PublishStatus.PUBLISHED

        with patch.object(svc, "_load_scheduled_post", AsyncMock(return_value=sp)):
            with pytest.raises(ValueError, match="already been published"):
                await svc.unschedule_post(uuid.uuid4(), uuid.uuid4())

    async def test_unschedule_reverts_draft_to_approved(self, db):
        svc = SchedulerService(db=db)
        sp = MagicMock()
        sp.id = uuid.uuid4()
        sp.publish_status = PublishStatus.SCHEDULED
        sp.draft_content_id = uuid.uuid4()

        draft = mock_draft(status=ContentStatus.SCHEDULED)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=draft)
        db.execute = AsyncMock(return_value=mock_result)

        with patch.object(svc, "_load_scheduled_post", AsyncMock(return_value=sp)):
            await svc.unschedule_post(sp.id, uuid.uuid4())

        assert sp.publish_status == PublishStatus.UNSCHEDULED
        assert draft.status == ContentStatus.APPROVED
        assert draft.scheduled_at is None


@pytest.mark.unit
@pytest.mark.asyncio
class TestReschedulePost:

    @pytest.fixture
    def db(self):
        db = MagicMock()
        db.flush = AsyncMock()
        db.execute = AsyncMock()
        return db

    async def test_reschedule_updates_time_and_resets_status(self, db):
        svc = SchedulerService(db=db)
        sp = MagicMock()
        sp.id = uuid.uuid4()
        sp.publish_status = PublishStatus.FAILED
        sp.draft_content_id = uuid.uuid4()
        sp.next_retry_at = utc_past()

        new_time = utc_future(minutes=120)

        draft = mock_draft(status=ContentStatus.FAILED)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=draft)
        db.execute = AsyncMock(return_value=mock_result)

        with patch.object(svc, "_load_scheduled_post", AsyncMock(return_value=sp)):
            result = await svc.reschedule_post(sp.id, uuid.uuid4(), new_time)

        assert sp.scheduled_at == new_time
        assert sp.publish_status == PublishStatus.SCHEDULED
        assert sp.next_retry_at is None

    async def test_cannot_reschedule_published_post(self, db):
        svc = SchedulerService(db=db)
        sp = MagicMock()
        sp.publish_status = PublishStatus.PUBLISHED

        with patch.object(svc, "_load_scheduled_post", AsyncMock(return_value=sp)):
            with pytest.raises(ValueError, match="Cannot reschedule"):
                await svc.reschedule_post(uuid.uuid4(), uuid.uuid4(), utc_future())


# ── Schema validation tests ────────────────────────────────────────────────────

@pytest.mark.unit
class TestSchedulePostRequestValidation:

    def test_rejects_past_datetime(self):
        from app.schemas.scheduler import SchedulePostRequest
        with pytest.raises(Exception):
            SchedulePostRequest(
                draft_content_id=uuid.uuid4(),
                workspace_id=uuid.uuid4(),
                scheduled_at=utc_past(),
            )

    def test_rejects_naive_datetime(self):
        from app.schemas.scheduler import SchedulePostRequest
        with pytest.raises(Exception):
            SchedulePostRequest(
                draft_content_id=uuid.uuid4(),
                workspace_id=uuid.uuid4(),
                scheduled_at=datetime.now(),  # no tzinfo
            )

    def test_accepts_future_utc_datetime(self):
        from app.schemas.scheduler import SchedulePostRequest
        req = SchedulePostRequest(
            draft_content_id=uuid.uuid4(),
            workspace_id=uuid.uuid4(),
            scheduled_at=utc_future(60),
        )
        assert req.scheduled_at > datetime.now(timezone.utc)


# ── OptimalTimeService unit tests ──────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
class TestOptimalTimeService:

    async def test_default_suggestion_when_no_account(self):
        from app.services.optimal_time import OptimalTimeService
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None)
        ))
        svc = OptimalTimeService(db=db)
        result = await svc.get_suggestion(uuid.uuid4())
        assert result.is_reliable is False
        assert len(result.suggested_slots) > 0  # returns industry defaults

    async def test_next_occurrence_is_always_future(self):
        from app.services.optimal_time import OptimalTimeService
        svc = OptimalTimeService(db=MagicMock())

        for day in range(7):
            for hour in range(24):
                slot = MagicMock()
                slot.day_of_week = day
                slot.hour_of_day = hour
                next_dt = svc._next_occurrence(slot)
                assert next_dt > datetime.now(timezone.utc), \
                    f"next_occurrence for day={day} hour={hour} is in the past"

    async def test_is_reliable_false_below_min_posts(self):
        from app.services.optimal_time import OptimalTimeService
        from app.core.config import settings

        db = MagicMock()

        # Account found, but slots empty and post count < min
        account = MagicMock()
        no_slots = MagicMock()
        no_slots.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        count_result = MagicMock()
        count_result.scalar_one = MagicMock(return_value=3)  # below min

        db.execute = AsyncMock(side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=account)),
            no_slots,
            count_result,
        ])

        svc = OptimalTimeService(db=db)
        result = await svc.get_suggestion(uuid.uuid4())
        assert result.is_reliable is False
        assert result.based_on_posts == 3
