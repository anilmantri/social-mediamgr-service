"""
Module 1 database models:
  - Workspace       — one per Instagram account / team
  - WorkspaceMember — RBAC: role per user per workspace
  - BrandProfile    — brand voice, rules, embedding cache
  - DraftContent    — the core post entity (multi-version)
  - ContentVersion  — immutable version snapshots (git-like)
  - ApprovalAction  — audit log of every approve/reject/comment
  - GenerationJob   — async AI job tracking
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


# ── Enums ──────────────────────────────────────────────────────────────────────

class WorkspaceRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    CREATOR = "creator"    # can generate, cannot approve own content
    REVIEWER = "reviewer"  # can approve/reject, cannot generate
    VIEWER = "viewer"      # read-only


class ContentStatus(str, enum.Enum):
    DRAFT = "draft"           # AI generated, not yet reviewed
    PENDING = "pending"       # submitted for approval
    APPROVED = "approved"     # approved, ready to schedule
    REJECTED = "rejected"     # rejected, back to creator
    SCHEDULED = "scheduled"   # approved + scheduled time set
    PUBLISHED = "published"   # successfully posted to Instagram
    FAILED = "failed"         # publish attempt failed


class ContentType(str, enum.Enum):
    FEED_IMAGE = "feed_image"
    FEED_CAROUSEL = "feed_carousel"
    STORY = "story"
    REEL = "reel"


class ApprovalActionType(str, enum.Enum):
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMMENTED = "commented"
    REVISION_REQUESTED = "revision_requested"
    EDITED = "edited"
    REGENERATED = "regenerated"


class ToneType(str, enum.Enum):
    WITTY = "witty"
    PROFESSIONAL = "professional"
    INSPIRING = "inspiring"
    CASUAL = "casual"
    EDUCATIONAL = "educational"
    PROMOTIONAL = "promotional"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"


# ── Models ─────────────────────────────────────────────────────────────────────

class Workspace(Base):
    """Isolated account per Instagram account or team."""

    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    instagram_account_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    instagram_username: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")  # timezone, default_tone, etc.

    # Relationships
    members: Mapped[list["WorkspaceMember"]] = relationship(back_populates="workspace")
    brand_profile: Mapped["BrandProfile | None"] = relationship(
        back_populates="workspace", uselist=False
    )
    draft_contents: Mapped[list["DraftContent"]] = relationship(back_populates="workspace")
    generation_jobs: Mapped[list["GenerationJob"]] = relationship(back_populates="workspace")


class WorkspaceMember(Base):
    """User ↔ workspace with role."""

    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    role: Mapped[WorkspaceRole] = mapped_column(
        Enum(WorkspaceRole), default=WorkspaceRole.CREATOR
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    workspace: Mapped["Workspace"] = relationship(back_populates="members")


class BrandProfile(Base):
    """
    Brand voice profile per workspace.
    Stores rules, example sentences, and the cached centroid embedding
    so we can score new content for brand-voice alignment without
    recomputing all examples every time.
    """

    __tablename__ = "brand_profiles"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    niche: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Voice configuration
    default_tone: Mapped[ToneType] = mapped_column(
        Enum(ToneType), default=ToneType.PROFESSIONAL
    )
    tone_description: Mapped[str | None] = mapped_column(Text)
    topic_blocklist: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    must_include_keywords: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    avoid_keywords: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)

    # Hashtag strategy
    brand_hashtags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    niche_hashtags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    max_hashtags: Mapped[int] = mapped_column(Integer, default=25)

    # Embedding centroid (average of approved content embeddings)
    voice_embedding: Mapped[list[float] | None] = mapped_column(ARRAY(Float))
    embedding_sample_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workspace: Mapped["Workspace"] = relationship(back_populates="brand_profile")


class DraftContent(Base):
    """
    Core post entity. Each post is immutable once a version is created —
    edits produce new ContentVersion rows. Status tracks lifecycle.
    """

    __tablename__ = "draft_contents"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    assigned_reviewer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    content_type: Mapped[ContentType] = mapped_column(
        Enum(ContentType), default=ContentType.FEED_IMAGE
    )
    status: Mapped[ContentStatus] = mapped_column(
        Enum(ContentStatus), default=ContentStatus.DRAFT, index=True
    )

    # Active version pointer (denormalised for fast reads)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    version_count: Mapped[int] = mapped_column(Integer, default=1)

    # Scheduling
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    instagram_post_id: Mapped[str | None] = mapped_column(String(64))

    # Generation metadata
    generation_prompt: Mapped[str | None] = mapped_column(Text)
    calendar_slot: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_calendar_post: Mapped[bool] = mapped_column(Boolean, default=False)

    # Brand voice score (0-1, set after scoring on each generation)
    brand_voice_score: Mapped[float | None] = mapped_column(Float)

    # Relationships
    workspace: Mapped["Workspace"] = relationship(back_populates="draft_contents")
    versions: Mapped[list["ContentVersion"]] = relationship(
        back_populates="draft_content",
        order_by="ContentVersion.version_number",
    )
    approval_actions: Mapped[list["ApprovalAction"]] = relationship(
        back_populates="draft_content",
        order_by="ApprovalAction.created_at",
    )


class ContentVersion(Base):
    """
    Immutable snapshot of a draft at a point in time.
    Every edit (by human or AI regeneration) creates a new version.
    """

    __tablename__ = "content_versions"

    draft_content_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("draft_contents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # Content payload
    caption: Mapped[str] = mapped_column(Text, nullable=False)
    hashtags: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    image_url: Mapped[str | None] = mapped_column(Text)          # final S3/CDN URL
    image_prompt: Mapped[str | None] = mapped_column(Text)       # original AI prompt
    image_alt_text: Mapped[str | None] = mapped_column(String(256))
    cta: Mapped[str | None] = mapped_column(String(256))         # call-to-action
    tone: Mapped[ToneType | None] = mapped_column(Enum(ToneType))

    # Metadata
    ai_model_used: Mapped[str | None] = mapped_column(String(80))
    generation_params: Mapped[dict] = mapped_column(JSONB, default=dict)
    change_summary: Mapped[str | None] = mapped_column(String(512))  # "Edited caption tone"
    is_ai_generated: Mapped[bool] = mapped_column(Boolean, default=True)

    draft_content: Mapped["DraftContent"] = relationship(back_populates="versions")


class ApprovalAction(Base):
    """
    Append-only audit log for every action in the approval workflow.
    Never updated, only inserted.
    """

    __tablename__ = "approval_actions"

    draft_content_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("draft_contents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[ApprovalActionType] = mapped_column(
        Enum(ApprovalActionType), nullable=False
    )
    comment: Mapped[str | None] = mapped_column(Text)
    version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)  # additional context

    draft_content: Mapped["DraftContent"] = relationship(back_populates="approval_actions")


class GenerationJob(Base):
    """
    Tracks async AI generation jobs (caption, image, calendar batch).
    Polled by the frontend via WebSocket or GET /jobs/:id.
    """

    __tablename__ = "generation_jobs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    initiated_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    job_type: Mapped[str] = mapped_column(String(64), nullable=False)  # "caption", "image", "calendar"
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.QUEUED, index=True
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(255), unique=True)

    # Input / output
    input_params: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)

    # Retry tracking
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workspace: Mapped["Workspace"] = relationship(back_populates="generation_jobs")
