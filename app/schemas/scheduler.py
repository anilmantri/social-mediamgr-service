"""
Module 2 schemas — Calendar & Scheduler
"""
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

from app.models.scheduler import PublishStatus


# ── Shared base ────────────────────────────────────────────────────────────────
class TimestampMixin(BaseModel):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


# ── Instagram Account ─────────────────────────────────────────────────────────

class InstagramConnectRequest(BaseModel):
    """Body for POST /instagram/connect — exchanges short-lived code for token."""
    code: str = Field(..., min_length=10)
    workspace_id: uuid.UUID


class InstagramAccountRead(TimestampMixin):
    workspace_id: uuid.UUID
    instagram_user_id: str
    instagram_username: str
    followers_count: int
    media_count: int
    profile_picture_url: str | None
    is_active: bool
    token_expires_at: datetime | None
    last_refreshed_at: datetime | None


# ── Scheduling ────────────────────────────────────────────────────────────────

class SchedulePostRequest(BaseModel):
    """Body for POST /schedule — schedule an approved draft."""
    draft_content_id: uuid.UUID
    workspace_id: uuid.UUID
    scheduled_at: datetime = Field(
        ..., description="UTC datetime when the post should go live"
    )
    use_optimal_time: bool = Field(
        default=False,
        description="Ignore scheduled_at and let AI pick the best time window",
    )

    @field_validator("scheduled_at")
    @classmethod
    def must_be_future(cls, v: datetime) -> datetime:
        from datetime import timezone
        now = datetime.now(timezone.utc)
        if v.tzinfo is None:
            raise ValueError("scheduled_at must be timezone-aware (UTC)")
        if v <= now:
            raise ValueError("scheduled_at must be in the future")
        return v


class RescheduleRequest(BaseModel):
    new_scheduled_at: datetime
    reason: str | None = Field(None, max_length=300)

    @field_validator("new_scheduled_at")
    @classmethod
    def must_be_future(cls, v: datetime) -> datetime:
        from datetime import timezone
        if v.tzinfo is None:
            raise ValueError("new_scheduled_at must be timezone-aware (UTC)")
        if v <= datetime.now(timezone.utc):
            raise ValueError("new_scheduled_at must be in the future")
        return v


class ScheduledPostRead(TimestampMixin):
    workspace_id: uuid.UUID
    draft_content_id: uuid.UUID
    scheduled_at: datetime
    publish_status: PublishStatus
    is_ai_optimised_time: bool
    ai_time_confidence: float | None
    ig_media_id: str | None
    ig_permalink: str | None
    published_at: datetime | None
    attempt_count: int
    caption_snapshot: str
    hashtags_snapshot: list[str]
    image_url_snapshot: str | None


class ScheduledPostList(BaseModel):
    items: list[ScheduledPostRead]
    total: int
    page: int
    page_size: int
    has_next: bool


class PublishResultRead(BaseModel):
    scheduled_post_id: uuid.UUID
    success: bool
    ig_media_id: str | None
    ig_permalink: str | None
    error_code: str | None
    error_message: str | None
    published_at: datetime | None


# ── Calendar view ──────────────────────────────────────────────────────────────

class CalendarDayRead(BaseModel):
    date: str                          # "YYYY-MM-DD"
    scheduled_posts: list[ScheduledPostRead]
    draft_count: int                   # approved but not yet scheduled
    is_optimal_day: bool               # one of top-5 engagement days this week


class CalendarMonthRead(BaseModel):
    workspace_id: uuid.UUID
    year: int
    month: int
    days: list[CalendarDayRead]
    total_scheduled: int
    total_published: int
    total_failed: int


# ── Optimal time ──────────────────────────────────────────────────────────────

class OptimalSlotRead(BaseModel):
    day_of_week: int                   # 0=Mon, 6=Sun
    day_name: str
    hour_of_day: int
    avg_engagement_rate: float
    avg_reach: int
    confidence_score: float
    rank: int


class OptimalTimeSuggestion(BaseModel):
    workspace_id: uuid.UUID
    suggested_slots: list[OptimalSlotRead]
    based_on_posts: int
    is_reliable: bool                  # True when based_on_posts >= OPTIMAL_TIME_MIN_POSTS
    next_suggested_at: datetime | None  # best upcoming datetime slot


# ── Post metrics ──────────────────────────────────────────────────────────────

class PostMetricsRead(TimestampMixin):
    scheduled_post_id: uuid.UUID
    reach: int
    impressions: int
    likes: int
    comments: int
    saves: int
    shares: int
    engagement_rate: float | None
    published_at: datetime | None


# ── Evergreen ──────────────────────────────────────────────────────────────────

class EvergreenCandidateRead(TimestampMixin):
    workspace_id: uuid.UUID
    scheduled_post_id: uuid.UUID
    engagement_score: float
    last_recycled_at: datetime | None
    recycle_count: int
    is_active: bool


class RecycleRequest(BaseModel):
    candidate_id: uuid.UUID
    scheduled_at: datetime

    @field_validator("scheduled_at")
    @classmethod
    def must_be_future(cls, v: datetime) -> datetime:
        from datetime import timezone
        if v <= datetime.now(timezone.utc):
            raise ValueError("scheduled_at must be in the future")
        return v


# ── Publish log ────────────────────────────────────────────────────────────────

class PublishLogRead(TimestampMixin):
    scheduled_post_id: uuid.UUID
    attempt_number: int
    success: bool
    http_status: int | None
    error_code: str | None
    error_message: str | None
    duration_ms: int | None
    ig_media_id: str | None
