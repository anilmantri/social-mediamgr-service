"""
Shared pytest fixtures.
- async_client: FastAPI test client
- db_session: isolated async DB session per test (rolls back after each test)
- mock_groq / mock_openai: patch AI clients so tests never hit real APIs
"""
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import Base, get_db
from app.main import app
from app.models.content import (
    BrandProfile,
    ContentStatus,
    ContentType,
    DraftContent,
    ToneType,
    Workspace,
)

# ── Test DB (SQLite in-memory for speed) ──────────────────────────────────────
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionFactory = async_sessionmaker(
    bind=test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_tables():
    """Create all tables once per test session."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Each test gets a fresh DB session that is rolled back on teardown.
    This keeps tests isolated without re-creating the schema every time.
    """
    async with TestSessionFactory() as session:
        async with session.begin():
            yield session
            await session.rollback()


@pytest_asyncio.fixture
async def async_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """FastAPI async test client with DB override."""
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
    app.dependency_overrides.clear()


# ── Factory fixtures ──────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def workspace(db_session: AsyncSession) -> Workspace:
    ws = Workspace(
        name="Test Brand",
        slug="test-brand",
        instagram_username="testbrand",
        is_active=True,
    )
    db_session.add(ws)
    await db_session.flush()
    return ws


@pytest_asyncio.fixture
async def brand_profile(db_session: AsyncSession, workspace: Workspace) -> BrandProfile:
    profile = BrandProfile(
        workspace_id=workspace.id,
        niche="Fitness & Wellness",
        default_tone=ToneType.INSPIRING,
        brand_hashtags=["fitlife", "wellness"],
        niche_hashtags=["fitness", "workout", "health"],
        max_hashtags=20,
    )
    db_session.add(profile)
    await db_session.flush()
    return profile


@pytest_asyncio.fixture
async def draft_content(db_session: AsyncSession, workspace: Workspace) -> DraftContent:
    draft = DraftContent(
        workspace_id=workspace.id,
        created_by_id=uuid.uuid4(),
        content_type=ContentType.FEED_IMAGE,
        status=ContentStatus.DRAFT,
        generation_prompt="Morning workout motivation",
    )
    db_session.add(draft)
    await db_session.flush()
    return draft


@pytest_asyncio.fixture
async def pending_draft(db_session: AsyncSession, workspace: Workspace) -> DraftContent:
    draft = DraftContent(
        workspace_id=workspace.id,
        created_by_id=uuid.uuid4(),
        content_type=ContentType.FEED_IMAGE,
        status=ContentStatus.PENDING,
        generation_prompt="Healthy meal prep tips",
    )
    db_session.add(draft)
    await db_session.flush()
    return draft


# ── AI mock fixtures ───────────────────────────────────────────────────────────

MOCK_CAPTION_RESPONSE = {
    "caption": "Rise and grind! 💪 Your morning workout sets the tone for the entire day.\n\nShow up for yourself — even when it's hard.\n\nThe results will speak for themselves. 🔥",
    "hashtags": ["morningworkout", "fitlife", "wellness", "fitness", "workout", "health", "motivation", "gym"],
    "cta": "Drop a 💪 if you crushed your workout today!",
    "hook": "Rise and grind! 💪",
    "reasoning": "Inspiring tone with strong hook, social proof CTA",
    "tone_used": ToneType.INSPIRING,
}

MOCK_IMAGE_RESPONSE = {
    "url": "https://cdn.example.com/generated/test_image_abc123.png",
    "prompt_used": "Create a high-quality Instagram feed image for: Fitness & Wellness. Morning workout motivation.",
    "revised_prompt": None,
    "alt_text": "Instagram feed image for Fitness & Wellness: Morning workout motivation",
}

MOCK_EMBEDDING = [0.1] * 1536  # text-embedding-3-small dimension


@pytest.fixture
def mock_groq_client():
    """Patches GroqContentClient so no real API calls are made."""
    with patch("app.services.content_generation.get_groq_client") as mock_factory:
        client = MagicMock()
        client.generate_caption = AsyncMock(return_value=MOCK_CAPTION_RESPONSE)
        client.generate_calendar_plan = AsyncMock(return_value=[
            {"day": i, "topic": f"Topic {i}", "content_type": "feed_image",
             "tone": "inspiring", "theme_category": "educational"}
            for i in range(1, 6)
        ])
        client.refresh_hashtags = AsyncMock(return_value=["newtag1", "newtag2"])
        mock_factory.return_value = client
        yield client


@pytest.fixture
def mock_openai_client():
    """Patches OpenAIContentClient so no real API calls are made."""
    with patch("app.services.content_generation.get_openai_client") as mock_factory:
        client = MagicMock()
        client.generate_image = AsyncMock(return_value=MOCK_IMAGE_RESPONSE)
        client.embed_text = AsyncMock(return_value=MOCK_EMBEDDING)
        client.embed_batch = AsyncMock(return_value=[MOCK_EMBEDDING])
        client.cosine_similarity = MagicMock(return_value=0.88)
        client.score_label = MagicMock(return_value="Excellent")
        mock_factory.return_value = client
        yield client


@pytest.fixture
def mock_celery(monkeypatch):
    """Prevents Celery tasks from actually being enqueued in unit tests."""
    mock_task = MagicMock()
    mock_task.id = str(uuid.uuid4())
    mock_apply = MagicMock(return_value=mock_task)

    monkeypatch.setattr(
        "app.workers.tasks.run_content_generation.apply_async", mock_apply
    )
    monkeypatch.setattr(
        "app.workers.tasks.run_calendar_generation.apply_async", mock_apply
    )
    return mock_apply
