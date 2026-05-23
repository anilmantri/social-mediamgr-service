"""
ContentGenerationService — the main orchestrator for Module 1.

Responsibilities:
  - Orchestrate caption generation (Groq) + image generation (DALL-E)
  - Score generated content against brand voice
  - Persist DraftContent + ContentVersion to DB
  - Emit GenerationJob status updates
  - Handle calendar batch generation
"""
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.content import (
    BrandProfile,
    ContentStatus,
    ContentType,
    ContentVersion,
    DraftContent,
    GenerationJob,
    JobStatus,
    ToneType,
    Workspace,
)
from app.schemas.content import (
    CalendarPlanRequest,
    ContentGenerationRequest,
    ContentGenerationResponse,
    ContentGenerationResult,
    GeneratedCaption,
    GeneratedImage,
)
from app.services.brand_voice import BrandVoiceService
from app.services.groq_client import GroqContentClient, get_groq_client
from app.services.openai_client import OpenAIContentClient, get_openai_client

log = structlog.get_logger(__name__)


class ContentGenerationService:
    def __init__(
        self,
        db: AsyncSession,
        groq: GroqContentClient | None = None,
        openai: OpenAIContentClient | None = None,
    ) -> None:
        self._db = db
        self._groq = groq or get_groq_client()
        self._openai = openai or get_openai_client()
        self._brand_voice = BrandVoiceService(db=db, openai=self._openai)

    # ── Public API ─────────────────────────────────────────────────────────────

    async def initiate_generation(
        self, request: ContentGenerationRequest, initiated_by_id: uuid.UUID
    ) -> ContentGenerationResponse:
        """
        Validate the request, create DB records, enqueue async job.
        Returns immediately with job_id for polling.
        """
        workspace = await self._get_workspace(request.workspace_id)
        brand_profile = await self._brand_voice.get_or_create_profile(
            str(request.workspace_id)
        )

        # Create DraftContent shell
        draft = DraftContent(
            workspace_id=request.workspace_id,
            created_by_id=initiated_by_id,
            content_type=request.content_type,
            status=ContentStatus.DRAFT,
            generation_prompt=request.topic,
        )
        self._db.add(draft)
        await self._db.flush()

        # Create GenerationJob
        job = GenerationJob(
            workspace_id=request.workspace_id,
            initiated_by_id=initiated_by_id,
            job_type="content_generation",
            status=JobStatus.QUEUED,
            input_params=request.model_dump(mode="json"),
        )
        self._db.add(job)
        await self._db.flush()

        log.info(
            "generation_initiated",
            draft_id=str(draft.id),
            job_id=str(job.id),
            topic=request.topic[:50],
        )

        # Enqueue Celery task
        from app.workers.tasks import run_content_generation
        celery_task = run_content_generation.apply_async(
            kwargs={
                "job_id": str(job.id),
                "draft_id": str(draft.id),
                "request_data": request.model_dump(mode="json"),
                "initiated_by_id": str(initiated_by_id),
            },
            countdown=0,
        )

        # Save Celery task id back to job
        job.celery_task_id = celery_task.id
        await self._db.flush()

        return ContentGenerationResponse(
            job_id=job.id,
            draft_content_id=draft.id,
            status=JobStatus.QUEUED,
            estimated_seconds=20 if request.include_image else 8,
            poll_url=f"/api/v1/jobs/{job.id}",
        )

    async def execute_generation(
        self,
        job_id: uuid.UUID,
        draft_id: uuid.UUID,
        request: ContentGenerationRequest,
        initiated_by_id: uuid.UUID,
    ) -> ContentGenerationResult:
        """
        Called by Celery worker. Does the actual AI calls synchronously.
        Updates job + draft in DB throughout.
        """
        job = await self._get_job(job_id)
        draft = await self._get_draft(draft_id)
        brand_profile = await self._brand_voice.get_or_create_profile(
            str(request.workspace_id)
        )

        await self._update_job_status(job, JobStatus.RUNNING)

        try:
            # ── Step 1: Generate caption ──────────────────────────────────────
            log.info("generating_caption", job_id=str(job_id), topic=request.topic[:50])
            caption_data = await self._groq.generate_caption(
                topic=request.topic,
                niche=brand_profile.niche,
                tone=request.tone,
                content_type=request.content_type.value,
                language=request.language,
                tone_override=request.tone_override_description,
                extra_context=request.extra_context,
                must_include_keywords=brand_profile.must_include_keywords,
                avoid_keywords=brand_profile.avoid_keywords,
                brand_hashtags=brand_profile.brand_hashtags,
                niche_hashtags=brand_profile.niche_hashtags,
                max_hashtags=brand_profile.max_hashtags,
                cta_hint=request.cta,
            )

            # ── Step 2: Score against brand voice ─────────────────────────────
            voice_score_resp = await self._brand_voice.score_content(
                workspace_id=str(request.workspace_id),
                caption=caption_data["caption"],
                hashtags=caption_data["hashtags"],
            )
            brand_voice_score = voice_score_resp.score if voice_score_resp.score > 0 else None

            generated_caption = GeneratedCaption(
                text=caption_data["caption"],
                hashtags=caption_data["hashtags"],
                cta=caption_data.get("cta"),
                tone_used=request.tone,
                word_count=len(caption_data["caption"].split()),
                hashtag_count=len(caption_data["hashtags"]),
                brand_voice_score=brand_voice_score,
            )

            # ── Step 3: Generate image (optional) ────────────────────────────
            generated_image: GeneratedImage | None = None
            if request.include_image:
                log.info("generating_image", job_id=str(job_id))
                image_data = await self._openai.generate_image(
                    topic=request.topic,
                    niche=brand_profile.niche,
                    content_type=request.content_type.value,
                    tone=request.tone.value,
                    style=request.image_style,
                    workspace_id=str(request.workspace_id),
                )
                generated_image = GeneratedImage(
                    url=image_data["url"],
                    prompt_used=image_data["prompt_used"],
                    revised_prompt=image_data.get("revised_prompt"),
                    alt_text=image_data["alt_text"],
                )

            # ── Step 4: Persist ContentVersion ────────────────────────────────
            version = ContentVersion(
                draft_content_id=draft.id,
                version_number=1,
                created_by_id=initiated_by_id,
                caption=generated_caption.text,
                hashtags=generated_caption.hashtags,
                image_url=str(generated_image.url) if generated_image else None,
                image_prompt=generated_image.prompt_used if generated_image else None,
                image_alt_text=generated_image.alt_text if generated_image else None,
                cta=generated_caption.cta,
                tone=request.tone,
                ai_model_used=settings.GROQ_MODEL,
                generation_params={
                    "topic": request.topic,
                    "language": request.language,
                    "image_style": request.image_style,
                },
                is_ai_generated=True,
            )
            self._db.add(version)
            await self._db.flush()

            # Update draft with active version
            draft.current_version_id = version.id
            draft.brand_voice_score = brand_voice_score
            await self._db.flush()

            # Update job success
            result_data = {
                "caption": generated_caption.model_dump(mode="json"),
                "image": generated_image.model_dump(mode="json") if generated_image else None,
                "version_id": str(version.id),
            }
            await self._update_job_status(
                job, JobStatus.SUCCESS, result=result_data
            )

            log.info(
                "generation_complete",
                job_id=str(job_id),
                draft_id=str(draft_id),
                version_id=str(version.id),
                brand_voice_score=brand_voice_score,
            )

            return ContentGenerationResult(
                job_id=job_id,
                draft_content_id=draft_id,
                version_id=version.id,
                status=JobStatus.SUCCESS,
                caption=generated_caption,
                image=generated_image,
            )

        except Exception as exc:
            log.exception("generation_failed", job_id=str(job_id), error=str(exc))
            await self._update_job_status(
                job, JobStatus.FAILED, error_message=str(exc)
            )
            raise

    # ── Calendar generation ────────────────────────────────────────────────────

    async def initiate_calendar_plan(
        self, request: CalendarPlanRequest, initiated_by_id: uuid.UUID
    ) -> dict:
        """Enqueue 30-day batch generation job."""
        import calendar
        # Calculate total posts
        import calendar as cal_module
        days_in_month = cal_module.monthrange(request.year, request.month)[1]
        weeks = days_in_month / 7
        total_posts = min(int(weeks * request.posts_per_week), settings.MAX_BATCH_SIZE)

        job = GenerationJob(
            workspace_id=request.workspace_id,
            initiated_by_id=initiated_by_id,
            job_type="calendar_plan",
            status=JobStatus.QUEUED,
            input_params={**request.model_dump(mode="json"), "total_posts": total_posts},
        )
        self._db.add(job)
        await self._db.flush()

        from app.workers.tasks import run_calendar_generation
        celery_task = run_calendar_generation.apply_async(
            kwargs={
                "job_id": str(job.id),
                "request_data": request.model_dump(mode="json"),
                "initiated_by_id": str(initiated_by_id),
                "total_posts": total_posts,
            }
        )
        job.celery_task_id = celery_task.id
        await self._db.flush()

        return {
            "job_id": job.id,
            "workspace_id": request.workspace_id,
            "month": request.month,
            "year": request.year,
            "total_posts": total_posts,
            "status": JobStatus.QUEUED,
            "poll_url": f"/api/v1/jobs/{job.id}",
        }

    async def execute_calendar_generation(
        self,
        job_id: uuid.UUID,
        request: CalendarPlanRequest,
        initiated_by_id: uuid.UUID,
        total_posts: int,
    ) -> list[dict]:
        """
        Called by Celery worker. Generates all calendar post ideas,
        then kicks off individual generation jobs for each slot.
        """
        job = await self._get_job(job_id)
        await self._update_job_status(job, JobStatus.RUNNING)

        brand_profile = await self._brand_voice.get_or_create_profile(
            str(request.workspace_id)
        )

        try:
            content_mix_str = {k.value: v for k, v in request.content_mix.items()}
            slots = await self._groq.generate_calendar_plan(
                niche=brand_profile.niche,
                month=request.month,
                year=request.year,
                posts_per_week=request.posts_per_week,
                theme_hints=request.theme_hints,
                content_mix=content_mix_str,
                total_posts=total_posts,
            )

            # Create DraftContent shells for each slot
            draft_ids = []
            for slot in slots[:total_posts]:
                draft = DraftContent(
                    workspace_id=request.workspace_id,
                    created_by_id=initiated_by_id,
                    content_type=ContentType(slot.get("content_type", "feed_image")),
                    status=ContentStatus.DRAFT,
                    generation_prompt=slot.get("topic", ""),
                    is_calendar_post=True,
                    calendar_slot=datetime(
                        request.year, request.month, max(1, slot.get("day", 1))
                    ).replace(tzinfo=timezone.utc),
                )
                self._db.add(draft)
                draft_ids.append(draft)

            await self._db.flush()

            # Enqueue individual generation jobs for each slot
            from app.workers.tasks import run_content_generation
            for draft, slot in zip(draft_ids, slots[:total_posts]):
                sub_request = ContentGenerationRequest(
                    workspace_id=request.workspace_id,
                    topic=slot.get("topic", ""),
                    content_type=ContentType(slot.get("content_type", "feed_image")),
                    tone=ToneType(slot.get("tone", brand_profile.default_tone.value)),
                    include_image=True,
                )
                sub_job = GenerationJob(
                    workspace_id=request.workspace_id,
                    initiated_by_id=initiated_by_id,
                    job_type="content_generation",
                    status=JobStatus.QUEUED,
                    input_params=sub_request.model_dump(mode="json"),
                )
                self._db.add(sub_job)
                await self._db.flush()

                run_content_generation.apply_async(
                    kwargs={
                        "job_id": str(sub_job.id),
                        "draft_id": str(draft.id),
                        "request_data": sub_request.model_dump(mode="json"),
                        "initiated_by_id": str(initiated_by_id),
                    },
                    countdown=0,
                )

            await self._update_job_status(
                job, JobStatus.SUCCESS,
                result={"slots_created": len(draft_ids), "draft_ids": [str(d.id) for d in draft_ids]},
            )

            log.info("calendar_generation_complete", job_id=str(job_id), slots=len(draft_ids))
            return slots

        except Exception as exc:
            log.exception("calendar_generation_failed", job_id=str(job_id))
            await self._update_job_status(job, JobStatus.FAILED, error_message=str(exc))
            raise

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _get_workspace(self, workspace_id: uuid.UUID) -> Workspace:
        result = await self._db.execute(
            select(Workspace).where(Workspace.id == workspace_id, Workspace.is_active == True)
        )
        workspace = result.scalar_one_or_none()
        if not workspace:
            raise ValueError(f"Workspace {workspace_id} not found or inactive")
        return workspace

    async def _get_draft(self, draft_id: uuid.UUID) -> DraftContent:
        result = await self._db.execute(
            select(DraftContent)
            .where(DraftContent.id == draft_id)
            .options(selectinload(DraftContent.versions))
        )
        draft = result.scalar_one_or_none()
        if not draft:
            raise ValueError(f"DraftContent {draft_id} not found")
        return draft

    async def _get_job(self, job_id: uuid.UUID) -> GenerationJob:
        result = await self._db.execute(
            select(GenerationJob).where(GenerationJob.id == job_id)
        )
        job = result.scalar_one_or_none()
        if not job:
            raise ValueError(f"GenerationJob {job_id} not found")
        return job

    async def _update_job_status(
        self,
        job: GenerationJob,
        status: JobStatus,
        result: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        job.status = status
        job.attempt_count += 1
        if status == JobStatus.RUNNING:
            job.started_at = datetime.now(timezone.utc)
        elif status in (JobStatus.SUCCESS, JobStatus.FAILED):
            job.completed_at = datetime.now(timezone.utc)
            if result:
                job.result = result
            if error_message:
                job.error_message = error_message
        await self._db.flush()
