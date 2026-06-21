"""
Request / response schemas for Module 1 — AI Content Engine.
Pydantic v2 with strict validation.
"""
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator, model_validator

from app.models.content import (
    ApprovalActionType,
    ContentStatus,
    ContentType,
    JobStatus,
    ToneType,
)


# ── Shared primitives ──────────────────────────────────────────────────────────

class TimestampMixin(BaseModel):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Generation request/response ────────────────────────────────────────────────

class ContentGenerationRequest(BaseModel):
    """Body for POST /api/v1/content/generate"""

    workspace_id: uuid.UUID
    topic: Annotated[str, Field(min_length=5, max_length=500)]
    content_type: ContentType = ContentType.FEED_IMAGE
    tone: ToneType = ToneType.PROFESSIONAL
    tone_override_description: str | None = Field(
        None,
        max_length=300,
        description="Free-text description that overrides the tone enum",
    )
    include_image: bool = True
    image_style: str | None = Field(
        None,
        max_length=200,
        examples=["photorealistic", "minimalist flat design", "watercolour illustration"],
    )
    cta: str | None = Field(None, max_length=120, description="Optional CTA hint")
    extra_context: str | None = Field(
        None,
        max_length=1000,
        description="Additional context injected into the generation prompt",
    )
    language: str = Field("en", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")

    @field_validator("topic")
    @classmethod
    def strip_topic(cls, v: str) -> str:
        return v.strip()


class GeneratedCaption(BaseModel):
    text: str
    hashtags: list[str]
    cta: str | None
    tone_used: ToneType
    word_count: int
    hashtag_count: int
    brand_voice_score: float | None = Field(
        None, description="0-1 similarity to brand voice centroid"
    )


class GeneratedImage(BaseModel):
    url: AnyHttpUrl
    prompt_used: str
    revised_prompt: str | None  # DALL-E 3 sometimes revises the prompt
    alt_text: str


class ContentGenerationResponse(BaseModel):
    """Immediate response — job has been queued."""

    job_id: uuid.UUID
    draft_content_id: uuid.UUID
    status: JobStatus = JobStatus.QUEUED
    estimated_seconds: int = Field(default=15)
    poll_url: str


class ContentGenerationResult(BaseModel):
    """Returned when job completes (via poll or WebSocket)."""

    job_id: uuid.UUID
    draft_content_id: uuid.UUID
    version_id: uuid.UUID
    status: JobStatus
    caption: GeneratedCaption | None = None
    image: GeneratedImage | None = None
    error: str | None = None


# ── Content CRUD schemas ───────────────────────────────────────────────────────

class ContentVersionRead(TimestampMixin):
    version_number: int
    caption: str
    hashtags: list[str]
    image_url: str | None
    image_prompt: str | None
    image_alt_text: str | None
    cta: str | None
    tone: ToneType | None
    ai_model_used: str | None
    is_ai_generated: bool
    change_summary: str | None


class ApprovalActionRead(TimestampMixin):
    actor_id: uuid.UUID
    action: ApprovalActionType
    comment: str | None
    version_id: uuid.UUID | None
    action_metadata: dict = {}


class DraftContentRead(TimestampMixin):
    workspace_id: uuid.UUID
    created_by_id: uuid.UUID
    content_type: ContentType
    status: ContentStatus
    version_count: int
    scheduled_at: datetime | None
    published_at: datetime | None
    instagram_post_id: str | None
    brand_voice_score: float | None
    is_calendar_post: bool
    generation_prompt: str | None = None
    current_version: ContentVersionRead | None = None
    approval_actions: list[ApprovalActionRead] = []


class DraftContentList(BaseModel):
    items: list[DraftContentRead]
    total: int
    page: int
    page_size: int
    has_next: bool


# ── Approval workflow schemas ──────────────────────────────────────────────────

class ApproveRequest(BaseModel):
    comment: str | None = Field(None, max_length=1000)


class RejectRequest(BaseModel):
    reason: Annotated[str, Field(min_length=10, max_length=1000)]
    request_revision: bool = True


class EditRequest(BaseModel):
    caption: str | None = Field(None, max_length=2200)
    hashtags: list[str] | None = Field(None, max_items=30)
    cta: str | None = Field(None, max_length=120)
    image_url: str | None = None
    change_summary: Annotated[str, Field(min_length=5, max_length=512)] = "Manual edit"

    @model_validator(mode="after")
    def at_least_one_field(self) -> "EditRequest":
        fields = [self.caption, self.hashtags, self.cta, self.image_url]
        if all(f is None for f in fields):
            raise ValueError("At least one field must be provided for editing.")
        return self


class RegenerateRequest(BaseModel):
    regenerate_caption: bool = True
    regenerate_image: bool = False
    new_tone: ToneType | None = None
    additional_instructions: str | None = Field(None, max_length=500)


class ApprovalActionResponse(TimestampMixin):
    draft_content_id: uuid.UUID
    action: ApprovalActionType
    comment: str | None
    new_status: ContentStatus


# ── Calendar schemas ───────────────────────────────────────────────────────────

class CalendarSlot(BaseModel):
    date: datetime
    topic: str = Field(..., min_length=3, max_length=200)
    content_type: ContentType = ContentType.FEED_IMAGE
    tone: ToneType | None = None


class CalendarPlanRequest(BaseModel):
    workspace_id: uuid.UUID
    month: int = Field(..., ge=1, le=12)
    year: int = Field(..., ge=2024, le=2030)
    posts_per_week: int = Field(default=4, ge=1, le=7)
    content_mix: dict[ContentType, float] = Field(
        default={ContentType.FEED_IMAGE: 0.7, ContentType.STORY: 0.3},
        description="Fraction of each content type. Must sum to 1.0",
    )
    theme_hints: list[str] = Field(
        default=[],
        max_length=10,
        description="High-level monthly themes to guide AI (e.g. 'summer sale', 'product launch')",
    )
    custom_slots: list[CalendarSlot] = Field(
        default=[],
        description="Pre-defined slots that override AI suggestions",
    )

    @model_validator(mode="after")
    def validate_content_mix(self) -> "CalendarPlanRequest":
        total = sum(self.content_mix.values())
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"content_mix must sum to 1.0, got {total:.2f}")
        return self


class CalendarPlanResponse(BaseModel):
    job_id: uuid.UUID
    workspace_id: uuid.UUID
    month: int
    year: int
    total_posts: int
    status: JobStatus
    poll_url: str


# ── Brand voice schemas ────────────────────────────────────────────────────────

class BrandProfileUpsert(BaseModel):
    niche: Annotated[str, Field(min_length=3, max_length=120)]
    description: str | None = Field(None, max_length=1000)
    default_tone: ToneType = ToneType.PROFESSIONAL
    tone_description: str | None = Field(None, max_length=500)
    topic_blocklist: list[str] = Field(default=[], max_length=50)
    must_include_keywords: list[str] = Field(default=[], max_length=50)
    avoid_keywords: list[str] = Field(default=[], max_length=50)
    brand_hashtags: list[str] = Field(default=[], max_length=10)
    niche_hashtags: list[str] = Field(default=[], max_length=20)
    max_hashtags: int = Field(default=25, ge=1, le=30)


class BrandVoiceScoreResponse(BaseModel):
    score: float = Field(..., ge=0, le=1)
    label: str  # "Excellent", "Good", "Fair", "Poor"
    sample_count: int
    min_samples_required: int
    explanation: str


# ── Job status schema ──────────────────────────────────────────────────────────

class JobStatusResponse(BaseModel):
    job_id: uuid.UUID
    job_type: str
    status: JobStatus
    attempt_count: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    result: dict | None
    error_message: str | None
    progress_pct: int | None = None  # 0-100 if available
