"""
Module 12 — OnboardingService

Tracks each user's onboarding progress and triggers:
  - Welcome email sequence (Day 0, Day 3, Day 12)
  - Step completion tracking
  - First post celebration
  - Trial expiry warnings (Day 10, Day 13, Day 15)
  - Upgrade prompt triggers

Onboarding steps:
  1. complete_profile  — set name, timezone
  2. connect_instagram — link Instagram Business account
  3. set_brand_voice   — fill in niche and tone
  4. first_generate    — generate first AI post
  5. first_approve     — approve a post
  6. first_schedule    — schedule a post (requires Starter+)
  7. first_publish     — first post published to Instagram
"""
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User, Subscription, SubscriptionStatus
from app.models.content import DraftContent, ContentStatus, Workspace

log = structlog.get_logger(__name__)

ONBOARDING_STEPS = [
    "complete_profile",
    "connect_instagram",
    "set_brand_voice",
    "first_generate",
    "first_approve",
    "first_schedule",
    "first_publish",
]


class OnboardingService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Onboarding status ─────────────────────────────────────────────────────

    async def get_status(
        self, user: User, workspace_id: uuid.UUID
    ) -> dict:
        """
        Returns the user's current onboarding state by
        checking actual data rather than storing separate flags.
        """
        steps = await self._compute_steps(user, workspace_id)
        completed = [s for s in steps if s["completed"]]
        next_step = next((s for s in steps if not s["completed"]), None)
        pct = round(len(completed) / len(steps) * 100)
        is_complete = len(completed) == len(steps)

        return {
            "user_id": str(user.id),
            "workspace_id": str(workspace_id),
            "is_complete": is_complete,
            "progress_pct": pct,
            "completed_count": len(completed),
            "total_steps": len(steps),
            "steps": steps,
            "next_step": next_step,
            "show_checklist": not is_complete,
        }

    async def _compute_steps(
        self, user: User, workspace_id: uuid.UUID
    ) -> list[dict]:
        """Derive step completion from actual DB state — no separate flags."""

        # Step 1: profile complete — has name and timezone set
        profile_done = bool(
            user.name and len(user.name) > 1 and user.timezone and user.timezone != "UTC"
        )

        # Step 2: Instagram connected
        from app.models.scheduler import InstagramAccount
        ig_result = await self._db.execute(
            select(InstagramAccount).where(
                InstagramAccount.workspace_id == workspace_id,
                InstagramAccount.is_active == True,
            )
        )
        ig_done = ig_result.scalar_one_or_none() is not None

        # Step 3: brand voice set
        from app.models.content import BrandProfile
        bp_result = await self._db.execute(
            select(BrandProfile).where(BrandProfile.workspace_id == workspace_id)
        )
        bp = bp_result.scalar_one_or_none()
        brand_done = bool(bp and bp.niche)

        # Step 4: first generate
        gen_result = await self._db.execute(
            select(DraftContent).where(
                DraftContent.workspace_id == workspace_id,
            ).limit(1)
        )
        gen_done = gen_result.scalar_one_or_none() is not None

        # Step 5: first approve
        approve_result = await self._db.execute(
            select(DraftContent).where(
                DraftContent.workspace_id == workspace_id,
                DraftContent.status.in_([
                    ContentStatus.APPROVED,
                    ContentStatus.SCHEDULED,
                    ContentStatus.PUBLISHED,
                ]),
            ).limit(1)
        )
        approve_done = approve_result.scalar_one_or_none() is not None

        # Step 6: first schedule
        from app.models.scheduler import ScheduledPost
        sched_result = await self._db.execute(
            select(ScheduledPost).where(
                ScheduledPost.workspace_id == workspace_id,
            ).limit(1)
        )
        sched_done = sched_result.scalar_one_or_none() is not None

        # Step 7: first publish
        pub_result = await self._db.execute(
            select(DraftContent).where(
                DraftContent.workspace_id == workspace_id,
                DraftContent.status == ContentStatus.PUBLISHED,
            ).limit(1)
        )
        pub_done = pub_result.scalar_one_or_none() is not None

        return [
            {
                "key": "complete_profile",
                "title": "Complete your profile",
                "description": "Set your name and timezone",
                "completed": profile_done,
                "action_url": "/settings",
                "action_label": "Go to Settings",
                "icon": "user",
            },
            {
                "key": "connect_instagram",
                "title": "Connect Instagram",
                "description": "Link your Instagram Business account",
                "completed": ig_done,
                "action_url": "/settings",
                "action_label": "Connect account",
                "icon": "instagram",
            },
            {
                "key": "set_brand_voice",
                "title": "Set your brand voice",
                "description": "Tell the AI about your niche and tone",
                "completed": brand_done,
                "action_url": "/settings",
                "action_label": "Set brand voice",
                "icon": "sparkles",
            },
            {
                "key": "first_generate",
                "title": "Generate your first post",
                "description": "Let AI write a caption and create an image",
                "completed": gen_done,
                "action_url": "/content",
                "action_label": "Generate content",
                "icon": "zap",
            },
            {
                "key": "first_approve",
                "title": "Approve a post",
                "description": "Review and approve AI-generated content",
                "completed": approve_done,
                "action_url": "/content?status=pending",
                "action_label": "Review content",
                "icon": "check",
            },
            {
                "key": "first_schedule",
                "title": "Schedule a post",
                "description": "Pick a time and schedule your first post",
                "completed": sched_done,
                "action_url": "/calendar",
                "action_label": "Open calendar",
                "icon": "calendar",
            },
            {
                "key": "first_publish",
                "title": "Publish to Instagram",
                "description": "Your first post goes live automatically",
                "completed": pub_done,
                "action_url": "/analytics",
                "action_label": "View analytics",
                "icon": "rocket",
            },
        ]

    # ── Email sequences ───────────────────────────────────────────────────────

    async def send_welcome_email(self, user: User) -> None:
        """Day 0 — sent immediately after signup."""
        from app.core.config import settings
        body = f"""Hi {user.name},

Welcome to Social Media Manager! 🎉

You now have 10 free AI credits to get started. Here's how to use them:

1. Go to Content Studio and click "Generate"
2. Enter a topic for your post
3. Let AI write your caption and create an image
4. Approve it, then schedule it

Your first post is just minutes away.

Get started: {settings.FRONTEND_URL}

— Social Media Manager Team

P.S. Need help? Just reply to this email."""

        await self._send(
            to=user.email,
            subject="Welcome! Your 10 free AI credits are ready 🚀",
            body=body,
        )
        log.info("welcome_email_sent", user_id=str(user.id))

    async def send_day3_email(self, user: User, workspace_id: uuid.UUID) -> None:
        """Day 3 — check if they've generated content yet."""
        from app.core.config import settings
        status = await self.get_status(user, workspace_id)
        gen_done = any(s["key"] == "first_generate" and s["completed"] for s in status["steps"])

        if gen_done:
            subject = "How's your first post going? 📸"
            body = f"""Hi {user.name},

Great job generating your first post!

Here's what to do next:
→ Approve it and schedule it to go live
→ Try generating 3-4 more posts to build a content queue
→ Use the Calendar to plan your posting schedule

{settings.FRONTEND_URL}/content

— Social Media Manager Team"""
        else:
            subject = "Your AI credits are waiting for you ✨"
            body = f"""Hi {user.name},

You signed up 3 days ago but haven't generated any content yet.

It only takes 30 seconds:
1. Click Generate
2. Type your topic (e.g. "Monday motivation for fitness fans")
3. AI does the rest

Try it now: {settings.FRONTEND_URL}/content

— Social Media Manager Team"""

        await self._send(to=user.email, subject=subject, body=body)

    async def send_day12_warning(self, user: User) -> None:
        """Day 12 — trial/free credits almost up."""
        from app.core.config import settings
        body = f"""Hi {user.name},

You've been using Social Media Manager for 12 days now.

Your free credits are running low. Upgrade to Starter ($19/month) to get:
  ✓ 200 AI credits per month
  ✓ Post scheduling
  ✓ Analytics dashboard
  ✓ 1 Instagram account

Upgrade now: {settings.FRONTEND_URL}/billing

— Social Media Manager Team"""

        await self._send(
            to=user.email,
            subject="Running low on AI credits — upgrade to keep posting",
            body=body,
        )

    # ── Celebration emails ────────────────────────────────────────────────────

    async def send_first_publish_celebration(self, user: User) -> None:
        """Sent when the first post is successfully published to Instagram."""
        from app.core.config import settings
        body = f"""Hi {user.name},

🎉 Your first post just went live on Instagram!

Check your Instagram to see it, then come back to:
→ View your post analytics (available in 6 hours)
→ Schedule your next post
→ Generate a 30-day content calendar

View analytics: {settings.FRONTEND_URL}/analytics

— Social Media Manager Team"""

        await self._send(
            to=user.email,
            subject="🎉 Your first post is live on Instagram!",
            body=body,
        )

    # ── Helper ────────────────────────────────────────────────────────────────

    async def _send(self, to: str, subject: str, body: str) -> None:
        import asyncio
        import smtplib
        from email.mime.text import MIMEText
        from app.core.config import settings

        def _do_send():
            try:
                msg = MIMEText(body)
                msg["Subject"] = subject
                msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
                msg["To"] = to
                with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as smtp:
                    if settings.SMTP_USER:
                        smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                    smtp.send_message(msg)
                log.info("email_sent", to=to, subject=subject)
            except Exception as e:
                log.warning("email_failed", to=to, error=str(e))

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_send)
