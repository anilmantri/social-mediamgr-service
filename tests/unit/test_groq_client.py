"""
Unit tests for GroqContentClient.
No real API calls — tests prompt building and response parsing logic.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models.content import ToneType
from app.services.groq_client import GroqContentClient, TONE_DESCRIPTIONS


@pytest.mark.unit
class TestGroqClientParsing:
    """Tests for _parse_caption_response — pure logic, no IO."""

    def setup_method(self):
        with patch("app.services.groq_client.AsyncGroq"):
            self.client = GroqContentClient()

    def test_parses_valid_response(self):
        raw = json.dumps({
            "caption": "This is the caption 🔥",
            "hashtags": ["fitness", "#health", "Wellness"],
            "cta": "Drop a comment below!",
            "hook": "This is the caption",
            "reasoning": "Used inspiring tone",
        })
        result = self.client._parse_caption_response(raw, tone=ToneType.INSPIRING, brand_hashtags=[])
        assert result["caption"] == "This is the caption 🔥"
        assert result["cta"] == "Drop a comment below!"
        # hashtags: stripped of #, lowercased, deduplicated
        assert "fitness" in result["hashtags"]
        assert "health" in result["hashtags"]
        assert "wellness" in result["hashtags"]
        # No # prefix in stored hashtags
        assert all(not t.startswith("#") for t in result["hashtags"])

    def test_strips_markdown_fences(self):
        raw = "```json\n{\"caption\": \"Hello\", \"hashtags\": [], \"cta\": \"Click!\"}\n```"
        result = self.client._parse_caption_response(raw, tone=ToneType.CASUAL, brand_hashtags=[])
        assert result["caption"] == "Hello"

    def test_deduplicates_hashtags(self):
        raw = json.dumps({
            "caption": "Test",
            "hashtags": ["fitness", "fitness", "FITNESS", "#fitness"],
            "cta": "Go!",
        })
        result = self.client._parse_caption_response(raw, tone=ToneType.CASUAL, brand_hashtags=[])
        assert result["hashtags"].count("fitness") == 1

    def test_injects_brand_hashtags_if_missing(self):
        raw = json.dumps({
            "caption": "Test",
            "hashtags": ["generic"],
            "cta": None,
        })
        result = self.client._parse_caption_response(
            raw, tone=ToneType.PROFESSIONAL, brand_hashtags=["mybrand", "officialpage"]
        )
        assert "mybrand" in result["hashtags"]
        assert "officialpage" in result["hashtags"]

    def test_does_not_duplicate_brand_hashtags_already_present(self):
        raw = json.dumps({
            "caption": "Test",
            "hashtags": ["mybrand", "other"],
            "cta": None,
        })
        result = self.client._parse_caption_response(
            raw, tone=ToneType.PROFESSIONAL, brand_hashtags=["mybrand"]
        )
        assert result["hashtags"].count("mybrand") == 1

    def test_raises_on_empty_response(self):
        with pytest.raises(ValueError, match="empty response"):
            self.client._parse_caption_response(None, tone=ToneType.CASUAL, brand_hashtags=[])

    def test_raises_on_invalid_json(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            self.client._parse_caption_response(
                "this is not json", tone=ToneType.CASUAL, brand_hashtags=[]
            )

    def test_raises_on_missing_required_fields(self):
        raw = json.dumps({"caption": "Only caption here"})
        with pytest.raises(ValueError, match="missing fields"):
            self.client._parse_caption_response(raw, tone=ToneType.CASUAL, brand_hashtags=[])

    def test_tone_preserved_in_result(self):
        raw = json.dumps({"caption": "X", "hashtags": [], "cta": "Go"})
        result = self.client._parse_caption_response(raw, tone=ToneType.WITTY, brand_hashtags=[])
        assert result["tone_used"] == ToneType.WITTY

    def test_null_cta_returns_none(self):
        raw = json.dumps({"caption": "X", "hashtags": [], "cta": None})
        result = self.client._parse_caption_response(raw, tone=ToneType.CASUAL, brand_hashtags=[])
        assert result["cta"] is None

    def test_empty_string_cta_returns_none(self):
        raw = json.dumps({"caption": "X", "hashtags": [], "cta": "   "})
        result = self.client._parse_caption_response(raw, tone=ToneType.CASUAL, brand_hashtags=[])
        assert result["cta"] is None


@pytest.mark.unit
class TestGroqToneDescriptions:
    def test_all_tones_have_descriptions(self):
        for tone in ToneType:
            assert tone in TONE_DESCRIPTIONS, f"Missing description for tone: {tone}"
            assert len(TONE_DESCRIPTIONS[tone]) > 10

    def test_descriptions_are_distinct(self):
        descriptions = list(TONE_DESCRIPTIONS.values())
        assert len(descriptions) == len(set(descriptions)), "Duplicate tone descriptions found"


@pytest.mark.unit
@pytest.mark.asyncio
class TestGroqClientAsync:

    @pytest.fixture
    def client_with_mock_groq(self):
        """Client where the underlying Groq SDK call is mocked."""
        with patch("app.services.groq_client.AsyncGroq") as MockGroq:
            mock_groq_instance = MagicMock()
            MockGroq.return_value = mock_groq_instance

            client = GroqContentClient()

            # Mock the chat.completions.create call
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = json.dumps({
                "caption": "🔥 Push your limits every single day!\n\nYour body can do it — it's your mind you need to convince.",
                "hashtags": ["motivation", "fitness", "mindset"],
                "cta": "Tag someone who needs this today!",
                "hook": "Push your limits every single day!",
                "reasoning": "Inspiring tone with personal challenge",
            })
            mock_groq_instance.chat = MagicMock()
            mock_groq_instance.chat.completions = MagicMock()
            mock_groq_instance.chat.completions.create = AsyncMock(return_value=mock_response)
            yield client, mock_groq_instance

    async def test_generate_caption_returns_structured_result(self, client_with_mock_groq):
        client, _ = client_with_mock_groq
        result = await client.generate_caption(
            topic="Morning workout motivation",
            niche="Fitness",
            tone=ToneType.INSPIRING,
            content_type="feed_image",
        )
        assert "caption" in result
        assert "hashtags" in result
        assert isinstance(result["hashtags"], list)
        assert result["tone_used"] == ToneType.INSPIRING

    async def test_generate_caption_passes_correct_model(self, client_with_mock_groq):
        from app.core.config import settings
        client, mock_groq_instance = client_with_mock_groq
        await client.generate_caption(
            topic="Test topic",
            niche="Tech",
            tone=ToneType.PROFESSIONAL,
            content_type="feed_image",
        )
        call_kwargs = mock_groq_instance.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == settings.GROQ_MODEL

    async def test_generate_caption_uses_json_response_format(self, client_with_mock_groq):
        client, mock_groq_instance = client_with_mock_groq
        await client.generate_caption(
            topic="Test", niche="Food", tone=ToneType.CASUAL, content_type="feed_image"
        )
        call_kwargs = mock_groq_instance.chat.completions.create.call_args.kwargs
        assert call_kwargs.get("response_format") == {"type": "json_object"}

    async def test_blocklist_injected_into_prompt(self, client_with_mock_groq):
        client, mock_groq_instance = client_with_mock_groq
        await client.generate_caption(
            topic="Test",
            niche="Food",
            tone=ToneType.CASUAL,
            content_type="feed_image",
            avoid_keywords=["junk food", "sugar"],
        )
        call_kwargs = mock_groq_instance.chat.completions.create.call_args.kwargs
        user_message = call_kwargs["messages"][1]["content"]
        assert "junk food" in user_message
        assert "sugar" in user_message
