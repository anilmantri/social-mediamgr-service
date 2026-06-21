"""Module 2 — Calendar & Scheduler tables

Revision ID: 0002_scheduler_tables
Revises: 0001_initial_schema
Create Date: 2025-01-01 00:00:01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_scheduler_tables"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── instagram_accounts ────────────────────────────────────────────────────
    op.create_table(
        "instagram_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instagram_user_id", sa.String(64), nullable=False),
        sa.Column("instagram_username", sa.String(64), nullable=False),
        sa.Column("instagram_page_id", sa.String(64), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("token_type", sa.String(32), server_default="bearer", nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("followers_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("media_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("profile_picture_url", sa.Text(), nullable=True),
        sa.Column("biography", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_api_error", sa.Text(), nullable=True),
        sa.Column("api_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id"),
        sa.UniqueConstraint("instagram_user_id"),
    )
    op.create_index("ix_instagram_accounts_workspace_id", "instagram_accounts", ["workspace_id"])

    # ── scheduled_posts ────────────────────────────────────────────────────────
    op.create_table(
        "scheduled_posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_content_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scheduled_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("publish_status", sa.Enum(
            "scheduled", "publishing", "published", "failed", "cancelled", "unscheduled",
            name="publishstatus"
        ), server_default="scheduled", nullable=False),
        sa.Column("is_ai_optimised_time", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("ai_time_confidence", sa.Float(), nullable=True),
        sa.Column("caption_snapshot", sa.Text(), nullable=False),
        sa.Column("hashtags_snapshot", postgresql.ARRAY(sa.String()), server_default=sa.text("ARRAY[]::text[]"), nullable=False),
        sa.Column("image_url_snapshot", sa.Text(), nullable=True),
        sa.Column("ig_media_id", sa.String(64), nullable=True),
        sa.Column("ig_permalink", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["draft_content_id"], ["draft_contents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["instagram_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_content_id"),
    )
    op.create_index("ix_scheduled_posts_workspace_id", "scheduled_posts", ["workspace_id"])
    op.create_index("ix_scheduled_posts_publish_status", "scheduled_posts", ["publish_status"])
    op.create_index("ix_scheduled_posts_scheduled_at", "scheduled_posts", ["scheduled_at"])

    # ── post_publish_logs ─────────────────────────────────────────────────────
    op.create_table(
        "post_publish_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("scheduled_post_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("api_endpoint", sa.String(256), nullable=True),
        sa.Column("request_payload", postgresql.JSONB(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column("ig_media_id", sa.String(64), nullable=True),
        sa.Column("error_code", sa.Enum(
            "token_expired", "rate_limited", "media_upload_fail", "container_fail",
            "publish_fail", "validation_fail", "network_error", "unknown",
            name="publisherrorcode"
        ), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["scheduled_post_id"], ["scheduled_posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_post_publish_logs_scheduled_post_id", "post_publish_logs", ["scheduled_post_id"])
    op.create_index("ix_post_publish_logs_created_at", "post_publish_logs", ["created_at"])

    # ── post_metrics ──────────────────────────────────────────────────────────
    op.create_table(
        "post_metrics",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("scheduled_post_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reach", sa.Integer(), server_default="0", nullable=False),
        sa.Column("impressions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("likes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("comments", sa.Integer(), server_default="0", nullable=False),
        sa.Column("saves", sa.Integer(), server_default="0", nullable=False),
        sa.Column("shares", sa.Integer(), server_default="0", nullable=False),
        sa.Column("profile_visits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("follows", sa.Integer(), server_default="0", nullable=False),
        sa.Column("engagement_rate", sa.Float(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("day_of_week", sa.Integer(), nullable=True),
        sa.Column("hour_of_day", sa.Integer(), nullable=True),
        sa.Column("metrics_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_insights", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(["scheduled_post_id"], ["scheduled_posts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["instagram_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scheduled_post_id"),
    )
    op.create_index("ix_post_metrics_workspace_id", "post_metrics", ["workspace_id"])
    op.create_index("ix_post_metrics_account_id", "post_metrics", ["account_id"])
    op.create_index("ix_post_metrics_published_at", "post_metrics", ["published_at"])

    # ── optimal_time_slots ────────────────────────────────────────────────────
    op.create_table(
        "optimal_time_slots",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("hour_of_day", sa.Integer(), nullable=False),
        sa.Column("avg_engagement_rate", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("avg_reach", sa.Integer(), server_default="0", nullable=False),
        sa.Column("post_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("confidence_score", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("last_computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["instagram_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "day_of_week", "hour_of_day"),
    )
    op.create_index("ix_optimal_time_slots_account_id", "optimal_time_slots", ["account_id"])

    # ── evergreen_candidates ───────────────────────────────────────────────────
    op.create_table(
        "evergreen_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scheduled_post_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("engagement_score", sa.Float(), nullable=False),
        sa.Column("last_recycled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recycle_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("recycle_window_days", sa.Integer(), server_default="90", nullable=False),
        sa.ForeignKeyConstraint(["scheduled_post_id"], ["scheduled_posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evergreen_candidates_workspace_id", "evergreen_candidates", ["workspace_id"])
    op.create_index("ix_evergreen_candidates_scheduled_post_id", "evergreen_candidates", ["scheduled_post_id"])

    # ── updated_at triggers for new tables ────────────────────────────────────
    for table in [
        "instagram_accounts", "scheduled_posts", "post_publish_logs",
        "post_metrics", "optimal_time_slots", "evergreen_candidates",
    ]:
        op.execute(f"""
            CREATE TRIGGER trigger_{table}_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """)


def downgrade() -> None:
    for table in [
        "evergreen_candidates", "optimal_time_slots", "post_metrics",
        "post_publish_logs", "scheduled_posts", "instagram_accounts",
    ]:
        op.execute(f"DROP TRIGGER IF EXISTS trigger_{table}_updated_at ON {table}")
        op.drop_table(table)

    op.execute("DROP TYPE IF EXISTS publisherrorcode")
    op.execute("DROP TYPE IF EXISTS publishstatus")
