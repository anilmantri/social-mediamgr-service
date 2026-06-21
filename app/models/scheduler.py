"""
Module 2 models:
  - InstagramAccount  — OAuth token store per workspace
  - ScheduledPost     — one-to-one with DraftContent once approved+scheduled
  - PostPublishLog    — immutable record of every publish attempt
  - PostMetrics       — Insights API data per published post
  - OptimalTimeSlot   — learned best posting times per workspace
  - EvergreenCandidate — top performers flagged for recycling
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


# ── Enums ──────────────────────────────────────────────────────────────────────

class PublishStatus(str, enum.Enum):
    SCHEDULED   = "scheduled"
    PUBLISHING  = "publishing"   # in-flight right now
    PUBLISHED   = "published"
    FAILED      = "failed"
    CANCELLED   = "cancelled"
    UNSCHEDULED = "unscheduled"  # manually pulled back


class PublishErrorCode(str, enum.Enum):
    TOKEN_EXPIRED      = "token_expired"
    RATE_LIMITED       = "rate_limited"
    MEDIA_UPLOAD_FAIL  = "media_upload_fail"
    CONTAINER_FAIL     = "container_fail"
    PUBLISH_FAIL       = "publish_fail"
    VALIDATION_FAIL    = "validation_fail"
    NETWORK_ERROR      = "network_error"
    UNKNOWN            = "unknown"


class DayOfWeek(int, enum.Enum):
    MONDAY    = 0
    TUESDAY   = 1
    WEDNESDAY = 2
    THURSDAY  = 3
    FRIDAY    = 4
    SATURDAY  = 5
    SUNDAY    = 6


# ── Models ─────────────────────────────────────────────────────────────────────

class InstagramAccount(Base):
    """
    Stores OAuth tokens for a connected Instagram Business account.
    One per workspace (enforced by unique constraint).
    Tokens are long-lived (60 days) and auto-refreshed by TokenRefresh task.
    """
    __tablename__ = "instagram_accounts"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    instagram_user_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    instagram_username: Mapped[str] = mapped_column(String(64), nullable=False)
    instagram_page_id: Mapped[str | None] = mapped_column(String(64))  # FB Page linked to IG

    # Tokens (encrypted at rest in production via DB-level encryption)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_type: Mapped[str] = mapped_column(String(32), default="bearer")
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Account metadata cached from API
    followers_count: Mapped[int] = mapped_column(Integer, default=0)
    media_count: Mapped[int] = mapped_column(Integer, default=0)
    profile_picture_url: Mapped[str | None] = mapped_column(Text)
    biography: Mapped[str | None] = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_api_error: Mapped[str | None] = mapped_column(Text)
    api_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    scheduled_posts: Mapped[list["ScheduledPost"]] = relationship(back_populates="account")
    post_metrics: Mapped[list["PostMetrics"]] = relationship(back_populates="account")
    optimal_time_slots: Mapped[list["OptimalTimeSlot"]] = relationship(back_populates="account")


class ScheduledPost(Base):
    """
    Represents a single approved post queued for publication.
    Linked 1:1 to DraftContent once it reaches SCHEDULED status.
    The Celery beat task queries this table every minute.
    """
    __tablename__ = "scheduled_posts"
    __table_args__ = (UniqueConstraint("draft_content_id"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    draft_content_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("draft_contents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    scheduled_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # Scheduling
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    publish_status: Mapped[PublishStatus] = mapped_column(
        Enum(PublishStatus, values_callable=lambda x: [e.value for e in x]), default=PublishStatus.SCHEDULED, index=True
    )

    # AI-suggested vs manually set time
    is_ai_optimised_time: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_time_confidence: Mapped[float | None] = mapped_column(Float)  # 0-1

    # Content snapshot at scheduling time (denormalised for reliability)
    caption_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    hashtags_snapshot: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    image_url_snapshot: Mapped[str | None] = mapped_column(Text)

    # Publish result
    ig_media_id: Mapped[str | None] = mapped_column(String(64))     # returned by Instagram
    ig_permalink: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relationships
    account: Mapped["InstagramAccount"] = relationship(back_populates="scheduled_posts")
    publish_logs: Mapped[list["PostPublishLog"]] = relationship(
        back_populates="scheduled_post", order_by="PostPublishLog.created_at"
    )
    metrics: Mapped["PostMetrics | None"] = relationship(
        back_populates="scheduled_post", uselist=False
    )


class PostPublishLog(Base):
    """
    Append-only record of every publish attempt with full request/response.
    Critical for debugging failed posts and rate-limit analysis.
    """
    __tablename__ = "post_publish_logs"

    scheduled_post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scheduled_posts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Request details
    api_endpoint: Mapped[str | None] = mapped_column(String(256))
    request_payload: Mapped[dict | None] = mapped_column(JSONB)

    # Response details
    http_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    ig_media_id: Mapped[str | None] = mapped_column(String(64))

    # Error details
    error_code: Mapped[PublishErrorCode | None] = mapped_column(Enum(PublishErrorCode, values_callable=lambda x: [e.value for e in x]))
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    scheduled_post: Mapped["ScheduledPost"] = relationship(back_populates="publish_logs")


class PostMetrics(Base):
    """
    Cached Insights API data per published post.
    Populated by InsightsSync task every 6 hours.
    Used by OptimalTimeService to learn best posting windows.
    """
    __tablename__ = "post_metrics"
    __table_args__ = (UniqueConstraint("scheduled_post_id"),)

    scheduled_post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scheduled_posts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )

    # Instagram Insights fields
    reach: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    saves: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    profile_visits: Mapped[int] = mapped_column(Integer, default=0)
    follows: Mapped[int] = mapped_column(Integer, default=0)

    # Computed engagement rate = (likes+comments+saves+shares) / reach
    engagement_rate: Mapped[float | None] = mapped_column(Float)

    # When the post was published (denorm for time-slot analysis)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    day_of_week: Mapped[int | None] = mapped_column(Integer)   # 0=Mon, 6=Sun
    hour_of_day: Mapped[int | None] = mapped_column(Integer)   # 0-23 UTC

    metrics_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_insights: Mapped[dict] = mapped_column(JSONB, default=dict)

    account: Mapped["InstagramAccount"] = relationship(back_populates="post_metrics")
    scheduled_post: Mapped["ScheduledPost"] = relationship(back_populates="metrics")


class OptimalTimeSlot(Base):
    """
    Learned best posting windows per workspace, per day of week.
    Updated by OptimalTimeService after each InsightsSync run.
    Groq uses this data plus audience heatmap to recommend posting times.
    """
    __tablename__ = "optimal_time_slots"
    __table_args__ = (
        UniqueConstraint("account_id", "day_of_week", "hour_of_day"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)  # 0-6
    hour_of_day: Mapped[int] = mapped_column(Integer, nullable=False)  # 0-23

    # Aggregated performance of posts in this slot
    avg_engagement_rate: Mapped[float] = mapped_column(Float, default=0.0)
    avg_reach: Mapped[int] = mapped_column(Integer, default=0)
    post_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)  # 0-1, based on post_count

    # Slot ranking within this account (1 = best)
    rank: Mapped[int | None] = mapped_column(Integer)

    last_computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped["InstagramAccount"] = relationship(back_populates="optimal_time_slots")


class EvergreenCandidate(Base):
    """
    Top-performing posts flagged for recycling.
    Created by the EvergreenRecycler task.
    """
    __tablename__ = "evergreen_candidates"

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    scheduled_post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scheduled_posts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    engagement_score: Mapped[float] = mapped_column(Float, nullable=False)  # normalised 0-1
    last_recycled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recycle_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    recycle_window_days: Mapped[int] = mapped_column(Integer, default=90)