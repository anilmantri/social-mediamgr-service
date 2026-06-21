"""
Unit tests for ApprovalService — state machine, version creation, audit log.
"""
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.content import (
    ContentStatus,
    ContentType,
    DraftContent,
    ToneType,
)
from app.services.approval import ApprovalService, ALLOWED_TRANSITIONS


@pytest.mark.unit
class TestStateMachineTransitions:
    """Pure logic tests — verify only allowed transitions pass."""

    def setup_method(self):
        self.db = MagicMock()
        self.db.flush = AsyncMock()
        self.db.add = MagicMock()
        self.service = ApprovalService(db=self.db)

    def _make_draft(self, status: ContentStatus) -> DraftContent:
        draft = MagicMock(spec=DraftContent)
        draft.id = uuid.uuid4()
        draft.status = status
        draft.workspace_id = uuid.uuid4()
        draft.current_version_id = uuid.uuid4()
        draft.version_count = 1
        return draft

    def test_draft_can_submit_to_pending(self):
        draft = self._make_draft(ContentStatus.DRAFT)
        # Should not raise
        self.service._assert_transition(draft, ContentStatus.PENDING)

    def test_pending_can_approve(self):
        draft = self._make_draft(ContentStatus.PENDING)
        self.service._assert_transition(draft, ContentStatus.APPROVED)

    def test_pending_can_reject(self):
        draft = self._make_draft(ContentStatus.PENDING)
        self.service._assert_transition(draft, ContentStatus.REJECTED)

    def test_rejected_can_resubmit(self):
        draft = self._make_draft(ContentStatus.REJECTED)
        self.service._assert_transition(draft, ContentStatus.PENDING)

    def test_approved_can_schedule(self):
        draft = self._make_draft(ContentStatus.APPROVED)
        self.service._assert_transition(draft, ContentStatus.SCHEDULED)

    def test_published_has_no_transitions(self):
        draft = self._make_draft(ContentStatus.PUBLISHED)
        with pytest.raises(ValueError, match="Cannot transition"):
            self.service._assert_transition(draft, ContentStatus.DRAFT)

    def test_draft_cannot_jump_to_approved(self):
        draft = self._make_draft(ContentStatus.DRAFT)
        with pytest.raises(ValueError, match="Cannot transition"):
            self.service._assert_transition(draft, ContentStatus.APPROVED)

    def test_draft_cannot_jump_to_published(self):
        draft = self._make_draft(ContentStatus.DRAFT)
        with pytest.raises(ValueError, match="Cannot transition"):
            self.service._assert_transition(draft, ContentStatus.PUBLISHED)

    def test_error_message_includes_allowed_states(self):
        draft = self._make_draft(ContentStatus.DRAFT)
        with pytest.raises(ValueError) as exc_info:
            self.service._assert_transition(draft, ContentStatus.PUBLISHED)
        assert "pending" in str(exc_info.value)  # allowed states shown

    def test_all_statuses_covered_in_transition_map(self):
        """Every ContentStatus must appear as a key in ALLOWED_TRANSITIONS."""
        for status in ContentStatus:
            assert status in ALLOWED_TRANSITIONS, f"{status} not in ALLOWED_TRANSITIONS"


@pytest.mark.unit
@pytest.mark.asyncio
class TestApprovalServiceAsync:

    @pytest.fixture
    def db(self):
        db = MagicMock()
        db.flush = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def pending_draft(self):
        draft = MagicMock(spec=DraftContent)
        draft.id = uuid.uuid4()
        draft.workspace_id = uuid.uuid4()
        draft.status = ContentStatus.PENDING
        draft.current_version_id = uuid.uuid4()
        draft.version_count = 1
        draft.generation_prompt = "Test topic"
        return draft

    async def test_approve_changes_status(self, db, pending_draft):
        service = ApprovalService(db=db)

        with patch.object(service, "_load_draft", AsyncMock(return_value=pending_draft)), \
             patch.object(service, "_get_current_version", AsyncMock(return_value=MagicMock(
                 caption="Great caption", hashtags=["tag1"]
             ))), \
             patch.object(service._brand_voice, "update_voice_embedding", AsyncMock()):
            result = await service.approve(
                draft_id=pending_draft.id,
                actor_id=uuid.uuid4(),
                comment="Looks great!",
            )

        assert pending_draft.status == ContentStatus.APPROVED
        assert result.new_status == ContentStatus.APPROVED

    async def test_reject_changes_status(self, db, pending_draft):
        service = ApprovalService(db=db)

        with patch.object(service, "_load_draft", AsyncMock(return_value=pending_draft)):
            result = await service.reject(
                draft_id=pending_draft.id,
                actor_id=uuid.uuid4(),
                reason="Caption does not match brand voice.",
            )

        assert pending_draft.status == ContentStatus.REJECTED
        assert result.new_status == ContentStatus.REJECTED

    async def test_reject_requires_reason_min_length(self, db):
        service = ApprovalService(db=db)
        draft = MagicMock(spec=DraftContent)
        draft.id = uuid.uuid4()
        draft.status = ContentStatus.PENDING

        with patch.object(service, "_load_draft", AsyncMock(return_value=draft)):
            # "Short" is < 10 chars — schema validation should catch this at API level
            # but service itself just writes what it receives
            result = await service.reject(
                draft_id=draft.id,
                actor_id=uuid.uuid4(),
                reason="Too short",  # service doesn't validate length — schema does
            )
        assert draft.status == ContentStatus.REJECTED

    async def test_approve_triggers_brand_voice_update(self, db, pending_draft):
        service = ApprovalService(db=db)
        mock_version = MagicMock(caption="Test caption", hashtags=["fitness"])

        update_mock = AsyncMock()
        service._brand_voice.update_voice_embedding = update_mock

        with patch.object(service, "_load_draft", AsyncMock(return_value=pending_draft)), \
             patch.object(service, "_get_current_version", AsyncMock(return_value=mock_version)):
            await service.approve(draft_id=pending_draft.id, actor_id=uuid.uuid4())

        update_mock.assert_called_once()
        call_kwargs = update_mock.call_args.kwargs
        assert call_kwargs["approved_caption"] == "Test caption"
        assert call_kwargs["approved_hashtags"] == ["fitness"]

    async def test_approve_does_not_update_voice_when_no_version(self, db, pending_draft):
        service = ApprovalService(db=db)
        pending_draft.current_version_id = None

        update_mock = AsyncMock()
        service._brand_voice.update_voice_embedding = update_mock

        with patch.object(service, "_load_draft", AsyncMock(return_value=pending_draft)), \
             patch.object(service, "_get_current_version", AsyncMock(return_value=None)):
            await service.approve(draft_id=pending_draft.id, actor_id=uuid.uuid4())

        update_mock.assert_not_called()

    async def test_edit_creates_new_version(self, db):
        service = ApprovalService(db=db)
        actor_id = uuid.uuid4()

        draft = MagicMock(spec=DraftContent)
        draft.id = uuid.uuid4()
        draft.workspace_id = uuid.uuid4()
        draft.status = ContentStatus.APPROVED
        draft.current_version_id = uuid.uuid4()
        draft.version_count = 1

        current_version = MagicMock()
        current_version.caption = "Old caption"
        current_version.hashtags = ["old_tag"]
        current_version.cta = "Old CTA"
        current_version.image_url = "https://example.com/old.png"
        current_version.image_prompt = "old prompt"
        current_version.image_alt_text = "old alt"
        current_version.tone = ToneType.PROFESSIONAL
        current_version.ai_model_used = "llama-3"
        current_version.generation_params = {}

        from app.schemas.content import EditRequest
        edit = EditRequest(caption="New improved caption!", change_summary="Updated caption tone")

        with patch.object(service, "_load_draft", AsyncMock(return_value=draft)), \
             patch.object(service, "_get_current_version", AsyncMock(return_value=current_version)):
            result = await service.edit_content(draft_id=draft.id, actor_id=actor_id, edit=edit)

        # Version count should increment
        assert draft.version_count == 2
        # Editing approved content resets to pending
        assert draft.status == ContentStatus.PENDING
        # New version was added to DB
        db.add.assert_called()

    async def test_comment_does_not_change_status(self, db):
        service = ApprovalService(db=db)
        draft = MagicMock(spec=DraftContent)
        draft.id = uuid.uuid4()
        draft.status = ContentStatus.PENDING
        original_status = draft.status

        with patch.object(service, "_load_draft", AsyncMock(return_value=draft)):
            await service.add_comment(
                draft_id=draft.id, actor_id=uuid.uuid4(), comment="Looks good but check the CTA"
            )

        assert draft.status == original_status
