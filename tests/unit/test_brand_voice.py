"""
Unit tests for BrandVoiceService — embedding math, scoring, centroid update.
"""
import pytest
import numpy as np
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.content import BrandProfile, ToneType
from app.services.openai_client import OpenAIContentClient


@pytest.mark.unit
class TestCosineSimilarity:
    """Pure math tests — no mocking needed."""

    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        score = OpenAIContentClient.cosine_similarity(v, v)
        assert abs(score - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        score = OpenAIContentClient.cosine_similarity(a, b)
        assert abs(score - 0.0) < 1e-6

    def test_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        score = OpenAIContentClient.cosine_similarity(a, b)
        assert abs(score - (-1.0)) < 1e-6

    def test_zero_vector_returns_zero(self):
        zero = [0.0, 0.0]
        v = [1.0, 0.0]
        assert OpenAIContentClient.cosine_similarity(zero, v) == 0.0
        assert OpenAIContentClient.cosine_similarity(v, zero) == 0.0

    def test_high_dimensional_vectors(self):
        a = [0.1] * 1536
        b = [0.1] * 1536
        score = OpenAIContentClient.cosine_similarity(a, b)
        assert abs(score - 1.0) < 1e-4

    def test_similar_vectors_high_score(self):
        rng = np.random.default_rng(42)
        base = rng.random(128).tolist()
        noise = (np.array(base) + rng.normal(0, 0.01, 128)).tolist()
        score = OpenAIContentClient.cosine_similarity(base, noise)
        assert score > 0.99


@pytest.mark.unit
class TestCentroidComputation:
    def test_single_embedding(self):
        v = [1.0, 0.0, 0.0]
        centroid = OpenAIContentClient.compute_centroid([v])
        # Should be normalised unit vector
        norm = np.linalg.norm(centroid)
        assert abs(norm - 1.0) < 1e-6

    def test_two_identical_embeddings(self):
        v = [3.0, 4.0]
        centroid = OpenAIContentClient.compute_centroid([v, v])
        # centroid should point in same direction as v
        score = OpenAIContentClient.cosine_similarity(centroid, v)
        assert score > 0.999

    def test_raises_on_empty(self):
        with pytest.raises(ValueError):
            OpenAIContentClient.compute_centroid([])

    def test_centroid_is_unit_vector(self):
        rng = np.random.default_rng(0)
        embeddings = rng.random((10, 64)).tolist()
        centroid = OpenAIContentClient.compute_centroid(embeddings)
        norm = np.linalg.norm(centroid)
        assert abs(norm - 1.0) < 1e-6

    def test_centroid_dimension_matches_input(self):
        embeddings = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
        centroid = OpenAIContentClient.compute_centroid(embeddings)
        assert len(centroid) == 3


@pytest.mark.unit
class TestScoreLabel:
    def test_excellent_threshold(self):
        assert OpenAIContentClient.score_label(0.85) == "Excellent"
        assert OpenAIContentClient.score_label(1.0) == "Excellent"

    def test_good_threshold(self):
        assert OpenAIContentClient.score_label(0.70) == "Good"
        assert OpenAIContentClient.score_label(0.84) == "Good"

    def test_fair_threshold(self):
        assert OpenAIContentClient.score_label(0.55) == "Fair"
        assert OpenAIContentClient.score_label(0.69) == "Fair"

    def test_poor_threshold(self):
        assert OpenAIContentClient.score_label(0.0) == "Poor"
        assert OpenAIContentClient.score_label(0.54) == "Poor"


@pytest.mark.unit
@pytest.mark.asyncio
class TestBrandVoiceService:

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.flush = AsyncMock()
        return db

    @pytest.fixture
    def mock_openai(self):
        client = MagicMock(spec=OpenAIContentClient)
        client.embed_text = AsyncMock(return_value=[0.5] * 1536)
        client.cosine_similarity = OpenAIContentClient.cosine_similarity.__func__(
            OpenAIContentClient
        ) if False else MagicMock(return_value=0.9)
        client.score_label = MagicMock(return_value="Excellent")
        return client

    async def test_score_returns_unscored_when_no_embedding(self, mock_db, mock_openai):
        from app.services.brand_voice import BrandVoiceService
        from unittest.mock import MagicMock
        import uuid

        service = BrandVoiceService(db=mock_db, openai=mock_openai)

        # Profile with no embedding
        profile = MagicMock(spec=BrandProfile)
        profile.voice_embedding = None
        profile.embedding_sample_count = 0

        with patch.object(service, "get_or_create_profile", AsyncMock(return_value=profile)):
            result = await service.score_content(
                workspace_id=str(uuid.uuid4()),
                caption="Test caption",
                hashtags=["test"],
            )

        assert result.score == 0.0
        assert result.label == "Unscored"

    async def test_incremental_embedding_update(self, mock_db, mock_openai):
        """Verify incremental centroid converges correctly."""
        from app.services.brand_voice import BrandVoiceService
        import uuid

        service = BrandVoiceService(db=mock_db, openai=mock_openai)

        profile = MagicMock(spec=BrandProfile)
        profile.voice_embedding = None
        profile.embedding_sample_count = 0

        with patch.object(service, "get_or_create_profile", AsyncMock(return_value=profile)):
            await service.update_voice_embedding(
                workspace_id=str(uuid.uuid4()),
                approved_caption="First approved post",
                approved_hashtags=["tag1"],
            )

        # After first update, embedding should be set
        assert profile.voice_embedding is not None
        assert profile.embedding_sample_count == 1

    async def test_sample_count_increments_on_each_approval(self, mock_db, mock_openai):
        from app.services.brand_voice import BrandVoiceService
        import uuid

        service = BrandVoiceService(db=mock_db, openai=mock_openai)

        profile = MagicMock(spec=BrandProfile)
        profile.voice_embedding = [0.1] * 1536
        profile.embedding_sample_count = 5

        with patch.object(service, "get_or_create_profile", AsyncMock(return_value=profile)):
            await service.update_voice_embedding(
                workspace_id=str(uuid.uuid4()),
                approved_caption="Another approved post",
                approved_hashtags=["tag2"],
            )

        assert profile.embedding_sample_count == 6
