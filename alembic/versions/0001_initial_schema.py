"""Initial schema — Module 1 tables

Revision ID: 0001_initial_schema
Revises: 
Create Date: 2025-01-01 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Enable pgcrypto for gen_random_uuid() ──────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ── workspaces ─────────────────────────────────────────────────────────────
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("instagram_account_id", sa.String(64), nullable=True),
        sa.Column("instagram_username", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("settings", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
        sa.UniqueConstraint("instagram_account_id"),
    )
    op.create_index("ix_workspaces_slug", "workspaces", ["slug"])

    # ── workspace_members ──────────────────────────────────────────────────────
    op.create_table(
        "workspace_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Enum("owner", "admin", "creator", "reviewer", "viewer", name="workspacerole"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "user_id"),
    )
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])

    # ── brand_profiles ─────────────────────────────────────────────────────────
    op.create_table(
        "brand_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("niche", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("default_tone", sa.Enum("witty", "professional", "inspiring", "casual", "educational", "promotional", name="tonetype"), nullable=False, server_default="professional"),
        sa.Column("tone_description", sa.Text(), nullable=True),
        sa.Column("topic_blocklist", postgresql.ARRAY(sa.String()), server_default=sa.text("ARRAY[]::text[]"), nullable=False),
        sa.Column("must_include_keywords", postgresql.ARRAY(sa.String()), server_default=sa.text("ARRAY[]::text[]"), nullable=False),
        sa.Column("avoid_keywords", postgresql.ARRAY(sa.String()), server_default=sa.text("ARRAY[]::text[]"), nullable=False),
        sa.Column("brand_hashtags", postgresql.ARRAY(sa.String()), server_default=sa.text("ARRAY[]::text[]"), nullable=False),
        sa.Column("niche_hashtags", postgresql.ARRAY(sa.String()), server_default=sa.text("ARRAY[]::text[]"), nullable=False),
        sa.Column("max_hashtags", sa.Integer(), server_default="25", nullable=False),
        sa.Column("voice_embedding", postgresql.ARRAY(sa.Float()), nullable=True),
        sa.Column("embedding_sample_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("embedding_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id"),
    )
    op.create_index("ix_brand_profiles_workspace_id", "brand_profiles", ["workspace_id"])

    # ── draft_contents ─────────────────────────────────────────────────────────
    op.create_table(
        "draft_contents",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_reviewer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("content_type", sa.Enum("feed_image", "feed_carousel", "story", "reel", name="contenttype"), nullable=False),
        sa.Column("status", sa.Enum("draft", "pending", "approved", "rejected", "scheduled", "published", "failed", name="contentstatus"), nullable=False, server_default="draft"),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("instagram_post_id", sa.String(64), nullable=True),
        sa.Column("generation_prompt", sa.Text(), nullable=True),
        sa.Column("calendar_slot", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_calendar_post", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("brand_voice_score", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_draft_contents_workspace_id", "draft_contents", ["workspace_id"])
    op.create_index("ix_draft_contents_status", "draft_contents", ["status"])
    op.create_index("ix_draft_contents_scheduled_at", "draft_contents", ["scheduled_at"])
    op.create_index("ix_draft_contents_calendar_slot", "draft_contents", ["calendar_slot"])

    # ── content_versions ───────────────────────────────────────────────────────
    op.create_table(
        "content_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("draft_content_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("caption", sa.Text(), nullable=False),
        sa.Column("hashtags", postgresql.ARRAY(sa.String()), server_default=sa.text("ARRAY[]::text[]"), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("image_prompt", sa.Text(), nullable=True),
        sa.Column("image_alt_text", sa.String(256), nullable=True),
        sa.Column("cta", sa.String(256), nullable=True),
        sa.Column("tone", sa.Enum("witty", "professional", "inspiring", "casual", "educational", "promotional", name="tonetype"), nullable=True),
        sa.Column("ai_model_used", sa.String(80), nullable=True),
        sa.Column("generation_params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("change_summary", sa.String(512), nullable=True),
        sa.Column("is_ai_generated", sa.Boolean(), server_default="true", nullable=False),
        sa.ForeignKeyConstraint(["draft_content_id"], ["draft_contents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_content_versions_draft_content_id", "content_versions", ["draft_content_id"])
    op.create_index(
        "uq_content_versions_draft_version",
        "content_versions",
        ["draft_content_id", "version_number"],
        unique=True,
    )

    # ── approval_actions ───────────────────────────────────────────────────────
    op.create_table(
        "approval_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("draft_content_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.Enum(
            "submitted", "approved", "rejected", "commented",
            "revision_requested", "edited", "regenerated",
            name="approvalactiontype",
        ), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(["draft_content_id"], ["draft_contents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approval_actions_draft_content_id", "approval_actions", ["draft_content_id"])
    op.create_index("ix_approval_actions_created_at", "approval_actions", ["created_at"])

    # ── generation_jobs ────────────────────────────────────────────────────────
    op.create_table(
        "generation_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("initiated_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.String(64), nullable=False),
        sa.Column("status", sa.Enum("queued", "running", "success", "failed", "retrying", name="jobstatus"), nullable=False, server_default="queued"),
        sa.Column("celery_task_id", sa.String(255), nullable=True),
        sa.Column("input_params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("celery_task_id"),
    )
    op.create_index("ix_generation_jobs_workspace_id", "generation_jobs", ["workspace_id"])
    op.create_index("ix_generation_jobs_status", "generation_jobs", ["status"])

    # ── updated_at trigger ─────────────────────────────────────────────────────
    op.execute("""
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ language 'plpgsql';
    """)

    for table in [
        "workspaces", "workspace_members", "brand_profiles",
        "draft_contents", "content_versions", "approval_actions", "generation_jobs",
    ]:
        op.execute(f"""
            CREATE TRIGGER trigger_{table}_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """)


def downgrade() -> None:
    for table in [
        "generation_jobs", "approval_actions", "content_versions",
        "draft_contents", "brand_profiles", "workspace_members", "workspaces",
    ]:
        op.execute(f"DROP TRIGGER IF EXISTS trigger_{table}_updated_at ON {table}")

    op.execute("DROP FUNCTION IF EXISTS update_updated_at_column()")

    op.drop_table("generation_jobs")
    op.drop_table("approval_actions")
    op.drop_table("content_versions")
    op.drop_table("draft_contents")
    op.drop_table("brand_profiles")
    op.drop_table("workspace_members")
    op.drop_table("workspaces")

    op.execute("DROP TYPE IF EXISTS jobstatus")
    op.execute("DROP TYPE IF EXISTS approvalactiontype")
    op.execute("DROP TYPE IF EXISTS contentstatus")
    op.execute("DROP TYPE IF EXISTS contenttype")
    op.execute("DROP TYPE IF EXISTS tonetype")
    op.execute("DROP TYPE IF EXISTS workspacerole")
