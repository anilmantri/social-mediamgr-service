"""
ApprovalService — manages the full review lifecycle.

Transitions:
  DRAFT → PENDING (creator submits)
  PENDING → APPROVED (reviewer approves)
  PENDING → REJECTED (reviewer rejects)
  REJECTED → PENDING (creator re-submits after edit)
  APPROVED → SCHEDULED (scheduler sets time)
  SCHEDULED → PUBLISHED (poster confirms)
"""
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.content import (
    ApprovalAction,
    ApprovalActionType,
    ContentStatus,
    ContentVersion,
    DraftContent,
    ToneType,
)
from app.schemas.content import (
    ApprovalActionResponse,
    EditRequest,
    RegenerateRequest,
)
from app.services.brand_voice import BrandVoiceService
from app.services.groq_client import get_groq_client
from app.services.openai_client import get_openai_client

log = structlog.get_logger(__name__)

# Legal status transitions
ALLOWED_TRANSITIONS: dict[ContentStatus, list[ContentStatus]] = {
    ContentStatus.DRAFT:     [ContentStatus.PENDING],
    ContentStatus.PENDING:   [ContentStatus.APPROVED, ContentStatus.REJECTED],
    ContentStatus.REJECTED:  [ContentStatus.PENDING],
    ContentStatus.APPROVED:  [ContentStatus.SCHEDULED],
    ContentStatus.SCHEDULED: [ContentStatus.PUBLISHED, ContentStatus.APPROVED],  # APPROVED = unschedule
    ContentStatus.PUBLISHED: [],
    ContentStatus.FAILED:    [ContentStatus.SCHEDULED],
}


class ApprovalService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._brand_voice = BrandVoiceService(db=db)

    # ── Workflow actions ───────────────────────────────────────────────────────

    async def submit_for_review(
            self, draft_id: uuid.UUID, actor_id: uuid.UUID, comment: str | None = None
    ) -> ApprovalActionResponse:
        draft = await self._load_draft(draft_id)
        self._assert_transition(draft, ContentStatus.PENDING)
        draft.status = ContentStatus.PENDING
        await self._log_action(
            draft=draft,
            actor_id=actor_id,
            action=ApprovalActionType.SUBMITTED,
            comment=comment,
        )
        await self._db.flush()
        log.info("draft_submitted", draft_id=str(draft_id), actor=str(actor_id))
        return self._build_response(draft, ApprovalActionType.SUBMITTED, comment)

    async def approve(
            self, draft_id: uuid.UUID, actor_id: uuid.UUID, comment: str | None = None
    ) -> ApprovalActionResponse:
        draft = await self._load_draft(draft_id)
        self._assert_transition(draft, ContentStatus.APPROVED)
        draft.status = ContentStatus.APPROVED
        await self._log_action(
            draft=draft,
            actor_id=actor_id,
            action=ApprovalActionType.APPROVED,
            comment=comment,
            version_id=draft.current_version_id,
        )
        await self._db.flush()

        # Update brand voice embedding with this approved content
        current_version = await self._get_current_version(draft)
        if current_version:
            await self._brand_voice.update_voice_embedding(
                workspace_id=str(draft.workspace_id),
                approved_caption=current_version.caption,
                approved_hashtags=current_version.hashtags,
            )

        log.info("draft_approved", draft_id=str(draft_id), actor=str(actor_id))
        return self._build_response(draft, ApprovalActionType.APPROVED, comment)

    async def reject(
            self,
            draft_id: uuid.UUID,
            actor_id: uuid.UUID,
            reason: str,
            request_revision: bool = True,
    ) -> ApprovalActionResponse:
        draft = await self._load_draft(draft_id)
        self._assert_transition(draft, ContentStatus.REJECTED)
        draft.status = ContentStatus.REJECTED
        await self._log_action(
            draft=draft,
            actor_id=actor_id,
            action=ApprovalActionType.REJECTED,
            comment=reason,
            metadata={"request_revision": request_revision},
        )
        await self._db.flush()
        log.info("draft_rejected", draft_id=str(draft_id), actor=str(actor_id))
        return self._build_response(draft, ApprovalActionType.REJECTED, reason)

    async def add_comment(
            self, draft_id: uuid.UUID, actor_id: uuid.UUID, comment: str
    ) -> ApprovalActionResponse:
        draft = await self._load_draft(draft_id)
        await self._log_action(
            draft=draft,
            actor_id=actor_id,
            action=ApprovalActionType.COMMENTED,
            comment=comment,
        )
        await self._db.flush()
        return self._build_response(draft, ApprovalActionType.COMMENTED, comment)

    async def edit_content(
            self, draft_id: uuid.UUID, actor_id: uuid.UUID, edit: EditRequest
    ) -> ApprovalActionResponse:
        """
        Create a new ContentVersion with the edited fields.
        Rolls back status to DRAFT so content goes through approval again
        (unless the editor IS the approver, which is a workspace policy decision).
        """
        draft = await self._load_draft(draft_id, load_versions=True)
        current = await self._get_current_version(draft)

        if not current:
            raise ValueError(f"Draft {draft_id} has no current version to edit")

        new_version = ContentVersion(
            draft_content_id=draft.id,
            version_number=draft.version_count + 1,
            created_by_id=actor_id,
            caption=edit.caption if edit.caption is not None else current.caption,
            hashtags=edit.hashtags if edit.hashtags is not None else current.hashtags,
            image_url=edit.image_url if edit.image_url is not None else current.image_url,
            image_prompt=current.image_prompt,
            image_alt_text=current.image_alt_text,
            cta=edit.cta if edit.cta is not None else current.cta,
            tone=current.tone,
            ai_model_used=current.ai_model_used,
            generation_params=current.generation_params,
            change_summary=edit.change_summary,
            is_ai_generated=False,
        )
        self._db.add(new_version)
        await self._db.flush()

        draft.current_version_id = new_version.id
        draft.version_count += 1
        # Edited content needs re-approval
        if draft.status == ContentStatus.APPROVED:
            draft.status = ContentStatus.PENDING

        await self._log_action(
            draft=draft,
            actor_id=actor_id,
            action=ApprovalActionType.EDITED,
            comment=edit.change_summary,
            version_id=new_version.id,
        )
        await self._db.flush()

        log.info(
            "draft_edited",
            draft_id=str(draft_id),
            new_version=draft.version_count,
            actor=str(actor_id),
        )
        return self._build_response(draft, ApprovalActionType.EDITED, edit.change_summary)

    async def request_regeneration(
            self,
            draft_id: uuid.UUID,
            actor_id: uuid.UUID,
            regen_request: RegenerateRequest,
    ) -> dict:
        """
        Trigger selective regeneration of caption and/or image.
        Returns a new GenerationJob for polling.
        """
        draft = await self._load_draft(draft_id)
        current = await self._get_current_version(draft)

        if not current:
            raise ValueError("No current version to regenerate from")

        await self._log_action(
            draft=draft,
            actor_id=actor_id,
            action=ApprovalActionType.REGENERATED,
            comment=regen_request.additional_instructions,
            metadata=regen_request.model_dump(mode="json"),
        )

        # Build partial generation request
        from app.schemas.content import ContentGenerationRequest
        regen_gen_request = ContentGenerationRequest(
            workspace_id=draft.workspace_id,
            topic=draft.generation_prompt or "Regenerate content",
            content_type=draft.content_type,
            tone=regen_request.new_tone or current.tone or ToneType.PROFESSIONAL,
            include_image=regen_request.regenerate_image,
            extra_context=regen_request.additional_instructions,
        )

        from app.services.content_generation import ContentGenerationService
        gen_service = ContentGenerationService(db=self._db)
        response = await gen_service.initiate_generation(
            request=regen_gen_request, initiated_by_id=actor_id
        )

        await self._db.flush()
        return response.model_dump(mode="json")

    # ── Queries ────────────────────────────────────────────────────────────────

    async def list_drafts(
            self,
            workspace_id: uuid.UUID,
            status: ContentStatus | None = None,
            page: int = 1,
            page_size: int = 20,
    ) -> tuple[list[DraftContent], int]:
        query = (
            select(DraftContent)
            .where(DraftContent.workspace_id == workspace_id)
            .options(
                selectinload(DraftContent.versions),
                selectinload(DraftContent.approval_actions),
            )
            .order_by(DraftContent.created_at.desc())
        )
        if status:
            query = query.where(DraftContent.status == status)

        # Count
        from sqlalchemy import func
        count_q = select(func.count()).select_from(
            query.subquery()
        )
        total_result = await self._db.execute(count_q)
        total = total_result.scalar_one()

        # Paginate
        query = query.offset((page - 1) * page_size).limit(page_size)
        result = await self._db.execute(query)
        return result.scalars().all(), total

    async def get_draft_detail(self, draft_id: uuid.UUID) -> DraftContent:
        return await self._load_draft(draft_id, load_versions=True)

    # ── Private helpers ────────────────────────────────────────────────────────

    async def _load_draft(
            self, draft_id: uuid.UUID, load_versions: bool = False
    ) -> DraftContent:
        query = select(DraftContent).where(DraftContent.id == draft_id)
        if load_versions:
            query = query.options(
                selectinload(DraftContent.versions),
                selectinload(DraftContent.approval_actions),
            )
        result = await self._db.execute(query)
        draft = result.scalar_one_or_none()
        if not draft:
            raise ValueError(f"DraftContent {draft_id} not found")
        return draft

    async def _get_current_version(self, draft: DraftContent) -> ContentVersion | None:
        if not draft.current_version_id:
            return None
        result = await self._db.execute(
            select(ContentVersion).where(ContentVersion.id == draft.current_version_id)
        )
        return result.scalar_one_or_none()

    def _assert_transition(self, draft: DraftContent, target: ContentStatus) -> None:
        allowed = ALLOWED_TRANSITIONS.get(draft.status, [])
        if target not in allowed:
            raise ValueError(
                f"Cannot transition from {draft.status.value!r} to {target.value!r}. "
                f"Allowed: {[s.value for s in allowed]}"
            )

    async def _log_action(
            self,
            draft: DraftContent,
            actor_id: uuid.UUID,
            action: ApprovalActionType,
            comment: str | None = None,
            version_id: uuid.UUID | None = None,
            metadata: dict | None = None,
    ) -> ApprovalAction:
        log_entry = ApprovalAction(
            draft_content_id=draft.id,
            actor_id=actor_id,
            action=action,
            comment=comment,
            version_id=version_id,
            action_metadata=metadata or {},
        )
        self._db.add(log_entry)
        return log_entry

    def _build_response(
            self,
            draft: DraftContent,
            action: ApprovalActionType,
            comment: str | None,
    ) -> ApprovalActionResponse:
        return ApprovalActionResponse(
            id=draft.id,  # re-using draft id for response id
            draft_content_id=draft.id,
            action=action,
            comment=comment,
            new_status=draft.status,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
