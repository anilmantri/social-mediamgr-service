"""
Integration tests for Module 1 API endpoints.
Uses real async DB (SQLite in-memory), mocks AI clients and Celery.
"""
import uuid
import pytest
from httpx import AsyncClient

from app.models.content import ContentStatus, ContentType, ToneType


@pytest.mark.integration
@pytest.mark.asyncio
class TestGenerateEndpoint:

    async def test_generate_returns_202_and_job_id(
        self, async_client: AsyncClient, workspace, mock_groq_client, mock_openai_client, mock_celery
    ):
        response = await async_client.post(
            "/api/v1/content/generate",
            json={
                "workspace_id": str(workspace.id),
                "topic": "5 tips for better morning workouts",
                "content_type": "feed_image",
                "tone": "inspiring",
                "include_image": True,
            },
        )
        assert response.status_code == 202
        data = response.json()
        assert "job_id" in data
        assert "draft_content_id" in data
        assert data["status"] == "queued"
        assert "/api/v1/jobs/" in data["poll_url"]

    async def test_generate_validates_topic_min_length(
        self, async_client: AsyncClient, workspace
    ):
        response = await async_client.post(
            "/api/v1/content/generate",
            json={
                "workspace_id": str(workspace.id),
                "topic": "Hi",  # too short — min 5 chars
                "content_type": "feed_image",
                "tone": "professional",
            },
        )
        assert response.status_code == 422

    async def test_generate_with_invalid_workspace_returns_400(
        self, async_client: AsyncClient, mock_celery
    ):
        response = await async_client.post(
            "/api/v1/content/generate",
            json={
                "workspace_id": str(uuid.uuid4()),  # non-existent workspace
                "topic": "Valid topic for testing",
                "content_type": "feed_image",
                "tone": "professional",
            },
        )
        assert response.status_code == 400

    async def test_generate_enqueues_celery_task(
        self, async_client: AsyncClient, workspace, mock_groq_client, mock_openai_client, mock_celery
    ):
        await async_client.post(
            "/api/v1/content/generate",
            json={
                "workspace_id": str(workspace.id),
                "topic": "Testing celery task enqueue",
                "content_type": "feed_image",
                "tone": "casual",
                "include_image": False,
            },
        )
        mock_celery.assert_called_once()


@pytest.mark.integration
@pytest.mark.asyncio
class TestListContentEndpoint:

    async def test_list_returns_empty_for_new_workspace(
        self, async_client: AsyncClient, workspace
    ):
        response = await async_client.get(
            "/api/v1/content", params={"workspace_id": str(workspace.id)}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    async def test_list_returns_draft_content(
        self, async_client: AsyncClient, workspace, draft_content
    ):
        response = await async_client.get(
            "/api/v1/content", params={"workspace_id": str(workspace.id)}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["id"] == str(draft_content.id)

    async def test_list_filters_by_status(
        self, async_client: AsyncClient, workspace, draft_content, pending_draft
    ):
        response = await async_client.get(
            "/api/v1/content",
            params={"workspace_id": str(workspace.id), "status": "pending"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["status"] == "pending"

    async def test_list_pagination(
        self, async_client: AsyncClient, workspace, db_session
    ):
        # Create 5 drafts
        from app.models.content import DraftContent
        for i in range(5):
            draft = DraftContent(
                workspace_id=workspace.id,
                created_by_id=uuid.uuid4(),
                content_type=ContentType.FEED_IMAGE,
                status=ContentStatus.DRAFT,
                generation_prompt=f"Topic {i}",
            )
            db_session.add(draft)
        await db_session.flush()

        response = await async_client.get(
            "/api/v1/content",
            params={"workspace_id": str(workspace.id), "page": 1, "page_size": 3},
        )
        data = response.json()
        assert len(data["items"]) == 3
        assert data["has_next"] is True


@pytest.mark.integration
@pytest.mark.asyncio
class TestApprovalWorkflowEndpoints:

    async def test_submit_draft_for_review(
        self, async_client: AsyncClient, draft_content
    ):
        response = await async_client.post(
            f"/api/v1/content/{draft_content.id}/submit",
            json={"comment": "Ready for review"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["new_status"] == "pending"
        assert data["action"] == "submitted"

    async def test_approve_pending_content(
        self, async_client: AsyncClient, pending_draft
    ):
        response = await async_client.post(
            f"/api/v1/content/{pending_draft.id}/approve",
            json={"comment": "Looks great!"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["new_status"] == "approved"

    async def test_reject_pending_content(
        self, async_client: AsyncClient, pending_draft
    ):
        response = await async_client.post(
            f"/api/v1/content/{pending_draft.id}/reject",
            json={
                "reason": "Caption does not align with our brand voice guidelines.",
                "request_revision": True,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["new_status"] == "rejected"
        assert data["action"] == "rejected"

    async def test_cannot_approve_draft_directly(
        self, async_client: AsyncClient, draft_content
    ):
        """DRAFT → APPROVED is an illegal transition."""
        response = await async_client.post(
            f"/api/v1/content/{draft_content.id}/approve",
        )
        assert response.status_code == 400
        assert "Cannot transition" in response.json()["detail"]

    async def test_edit_creates_new_version_and_returns_to_pending(
        self, async_client: AsyncClient, db_session, pending_draft
    ):
        # Add a content version to the pending draft
        from app.models.content import ContentVersion
        version = ContentVersion(
            draft_content_id=pending_draft.id,
            version_number=1,
            created_by_id=uuid.uuid4(),
            caption="Original caption text here",
            hashtags=["original"],
            is_ai_generated=True,
        )
        db_session.add(version)
        await db_session.flush()
        pending_draft.current_version_id = version.id

        response = await async_client.patch(
            f"/api/v1/content/{pending_draft.id}/edit",
            json={
                "caption": "Revised caption with improved wording for better engagement",
                "change_summary": "Improved caption clarity",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["action"] == "edited"

    async def test_edit_requires_at_least_one_field(
        self, async_client: AsyncClient, pending_draft
    ):
        response = await async_client.patch(
            f"/api/v1/content/{pending_draft.id}/edit",
            json={"change_summary": "No actual changes"},
        )
        assert response.status_code == 422

    async def test_add_comment_does_not_change_status(
        self, async_client: AsyncClient, pending_draft
    ):
        response = await async_client.post(
            f"/api/v1/content/{pending_draft.id}/comment",
            json={"comment": "Please review the hashtag selection."},
        )
        assert response.status_code == 200
        # Fetch draft and confirm status unchanged
        get_resp = await async_client.get(f"/api/v1/content/{pending_draft.id}")
        assert get_resp.json()["status"] == "pending"

    async def test_404_on_nonexistent_draft(self, async_client: AsyncClient):
        response = await async_client.post(
            f"/api/v1/content/{uuid.uuid4()}/approve",
        )
        assert response.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
class TestJobPollingEndpoint:

    async def test_poll_queued_job(
        self, async_client: AsyncClient, workspace, db_session
    ):
        from app.models.content import GenerationJob, JobStatus
        job = GenerationJob(
            workspace_id=workspace.id,
            initiated_by_id=uuid.uuid4(),
            job_type="content_generation",
            status=JobStatus.QUEUED,
            input_params={"topic": "test"},
        )
        db_session.add(job)
        await db_session.flush()

        response = await async_client.get(f"/api/v1/jobs/{job.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "queued"
        assert data["progress_pct"] is None

    async def test_poll_running_job_returns_50_pct(
        self, async_client: AsyncClient, workspace, db_session
    ):
        from app.models.content import GenerationJob, JobStatus
        job = GenerationJob(
            workspace_id=workspace.id,
            initiated_by_id=uuid.uuid4(),
            job_type="content_generation",
            status=JobStatus.RUNNING,
            input_params={},
        )
        db_session.add(job)
        await db_session.flush()

        response = await async_client.get(f"/api/v1/jobs/{job.id}")
        assert response.json()["progress_pct"] == 50

    async def test_poll_success_job_returns_100_pct(
        self, async_client: AsyncClient, workspace, db_session
    ):
        from app.models.content import GenerationJob, JobStatus
        job = GenerationJob(
            workspace_id=workspace.id,
            initiated_by_id=uuid.uuid4(),
            job_type="content_generation",
            status=JobStatus.SUCCESS,
            input_params={},
            result={"caption": {}, "image": None},
        )
        db_session.add(job)
        await db_session.flush()

        response = await async_client.get(f"/api/v1/jobs/{job.id}")
        assert response.json()["progress_pct"] == 100

    async def test_poll_nonexistent_job_returns_404(self, async_client: AsyncClient):
        response = await async_client.get(f"/api/v1/jobs/{uuid.uuid4()}")
        assert response.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
class TestBrandProfileEndpoint:

    async def test_upsert_brand_profile(
        self, async_client: AsyncClient, workspace
    ):
        response = await async_client.put(
            f"/api/v1/brand-profile/{workspace.id}",
            json={
                "niche": "Fitness & Wellness",
                "default_tone": "inspiring",
                "brand_hashtags": ["fitlife", "wellness"],
                "niche_hashtags": ["fitness", "gym", "health"],
                "max_hashtags": 20,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["niche"] == "Fitness & Wellness"

    async def test_score_content_without_enough_samples(
        self, async_client: AsyncClient, workspace, brand_profile
    ):
        response = await async_client.post(
            f"/api/v1/brand-profile/{workspace.id}/score",
            json={
                "caption": "Morning workout tips for a better you! 💪",
                "hashtags": ["fitness", "morning"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        # brand_profile fixture has 0 samples — should return Unscored
        assert data["label"] == "Unscored"
        assert data["score"] == 0.0


@pytest.mark.integration
@pytest.mark.asyncio
class TestHealthEndpoint:

    async def test_health_check_returns_ok(self, async_client: AsyncClient):
        response = await async_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "version" in data
