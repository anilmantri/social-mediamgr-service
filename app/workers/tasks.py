"""
Celery worker configuration and task definitions.

Tasks run in a separate process from the FastAPI app.
Each task creates its own DB session.
"""
import asyncio
import uuid
from typing import Any

import structlog
from celery import Celery
from celery.utils.log import get_task_logger

from app.core.config import settings

# Import scheduler_tasks and credit_tasks so Celery registers beat schedule
import app.workers.scheduler_tasks  # noqa: F401
import app.workers.credit_tasks      # noqa: F401
import app.workers.onboarding_tasks  # noqa: F401

log = structlog.get_logger(__name__)
task_log = get_task_logger(__name__)

# ── Celery app ─────────────────────────────────────────────────────────────────
celery_app = Celery(
    "social_mediamgr",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
    task_acks_late=True,           # ack only after task completes (safer)
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # one task at a time per worker
    task_routes={
        "app.workers.tasks.run_content_generation": {"queue": "generation"},
        "app.workers.tasks.run_calendar_generation": {"queue": "calendar"},
    },
)


def run_async(coro) -> Any:
    """Run an async function inside a Celery (sync) task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Tasks ──────────────────────────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="app.workers.tasks.run_content_generation",
    max_retries=3,
    default_retry_delay=10,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
)
def run_content_generation(
    self,
    job_id: str,
    draft_id: str,
    request_data: dict,
    initiated_by_id: str,
) -> dict:
    """
    Worker task: runs the full AI content generation pipeline.
    Creates its own DB session, calls ContentGenerationService.execute_generation().
    """
    task_log.info(f"Starting content generation job={job_id}")

    async def _run():
        from app.db.session import AsyncSessionFactory
        from app.schemas.content import ContentGenerationRequest
        from app.services.content_generation import ContentGenerationService
        from app.services.credit_service import CreditService
        from app.models.auth import CreditActionType

        request = ContentGenerationRequest(**request_data)

        # Determine action type for credit tracking
        action = (
            CreditActionType.CONTENT_GENERATE if request.include_image
            else CreditActionType.CAPTION_GENERATE
        )

        async with AsyncSessionFactory() as session:
            service = ContentGenerationService(db=session)
            credit_svc = CreditService(db=session)
            try:
                result = await service.execute_generation(
                    job_id=uuid.UUID(job_id),
                    draft_id=uuid.UUID(draft_id),
                    request=request,
                    initiated_by_id=uuid.UUID(initiated_by_id),
                )
                # Deduct credits on success
                await credit_svc.deduct(
                    workspace_id=request.workspace_id,
                    action_type=action,
                    reference_id=job_id,
                    user_id=uuid.UUID(initiated_by_id),
                )
                await session.commit()
                return result.model_dump(mode="json")
            except Exception:
                # Refund credits on failure
                try:
                    await credit_svc.refund(
                        workspace_id=request.workspace_id,
                        action_type=action,
                        reference_id=job_id,
                    )
                    await session.commit()
                except Exception:
                    pass
                await session.rollback()
                raise

    try:
        return run_async(_run())
    except Exception as exc:
        task_log.error(f"Content generation failed job={job_id}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    name="app.workers.tasks.run_calendar_generation",
    max_retries=2,
    default_retry_delay=30,
    soft_time_limit=300,
    time_limit=360,
)
def run_calendar_generation(
    self,
    job_id: str,
    request_data: dict,
    initiated_by_id: str,
    total_posts: int,
) -> dict:
    """
    Worker task: generates a full calendar month of post ideas,
    then enqueues individual generation tasks for each slot.
    """
    task_log.info(f"Starting calendar generation job={job_id} total={total_posts}")

    async def _run():
        from app.db.session import AsyncSessionFactory
        from app.schemas.content import CalendarPlanRequest
        from app.services.content_generation import ContentGenerationService

        request = CalendarPlanRequest(**request_data)

        async with AsyncSessionFactory() as session:
            service = ContentGenerationService(db=session)
            try:
                slots = await service.execute_calendar_generation(
                    job_id=uuid.UUID(job_id),
                    request=request,
                    initiated_by_id=uuid.UUID(initiated_by_id),
                    total_posts=total_posts,
                )
                await session.commit()
                return {"slots_created": len(slots)}
            except Exception:
                await session.rollback()
                raise

    try:
        return run_async(_run())
    except Exception as exc:
        task_log.error(f"Calendar generation failed job={job_id}: {exc}")
        raise self.retry(exc=exc)
