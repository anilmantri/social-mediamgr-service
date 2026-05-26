"""
OpenAI client for:
  1. Image generation (DALL-E 3)
  2. Text embeddings (text-embedding-3-small) for brand voice scoring
"""
import hashlib
import io
import re
from typing import Any

import httpx
import numpy as np
import structlog
from openai import AsyncOpenAI, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.services.storage import StorageService, get_storage_service

log = structlog.get_logger(__name__)


IMAGE_PROMPT_TEMPLATE = """Create a high-quality Instagram {content_type} image for:

BRAND NICHE: {niche}
TOPIC: {topic}
VISUAL STYLE: {style}
MOOD/TONE: {tone}

The image should be:
- Visually striking and scroll-stopping
- Suitable for a professional Instagram feed
- {aspect_ratio_hint}
- No text overlays (text will be added separately)
- Photorealistic unless style specifies otherwise

{extra_prompt}"""

CONTENT_TYPE_ASPECT_HINTS = {
    "feed_image": "Square or portrait orientation (1:1 or 4:5 aspect ratio)",
    "feed_carousel": "Square orientation (1:1 aspect ratio) for consistent carousel look",
    "story": "Portrait orientation filling a 9:16 screen",
    "reel": "Portrait orientation optimised for vertical video thumbnail",
}

DEFAULT_STYLES = {
    "professional": "Clean, minimalist, high-end photography style",
    "witty": "Bright, colourful, fun composition with visual humour elements",
    "inspiring": "Dramatic lighting, aspirational lifestyle photography",
    "casual": "Candid, authentic, natural lighting — lifestyle photography",
    "educational": "Clean infographic-friendly composition, clear focal point",
    "promotional": "Product-focused, high-contrast, commercial photography style",
}


class OpenAIContentClient:
    """
    Async OpenAI client for image generation and embeddings.
    Designed to be instantiated once and reused.
    """

    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        self._storage: StorageService = get_storage_service()

    @retry(
        retry=retry_if_exception_type((RateLimitError, TimeoutError)),
        stop=stop_after_attempt(settings.MAX_IMAGE_RETRIES),
        wait=wait_exponential(multiplier=2, min=5, max=60),
    )
    async def generate_image(
        self,
        *,
        topic: str,
        niche: str,
        content_type: str = "feed_image",
        tone: str = "professional",
        style: str | None = None,
        extra_prompt: str | None = None,
        workspace_id: str = "default",
    ) -> dict[str, Any]:
        """
        Generate an image with DALL-E 3, upload to S3/R2, and return metadata.
        Returns: {url, prompt_used, revised_prompt, alt_text}
        """
        resolved_style = style or DEFAULT_STYLES.get(tone, DEFAULT_STYLES["professional"])
        aspect_hint = CONTENT_TYPE_ASPECT_HINTS.get(content_type, CONTENT_TYPE_ASPECT_HINTS["feed_image"])

        prompt = IMAGE_PROMPT_TEMPLATE.format(
            content_type=content_type.replace("_", " "),
            niche=niche,
            topic=topic,
            style=resolved_style,
            tone=tone,
            aspect_ratio_hint=aspect_hint,
            extra_prompt=extra_prompt or "",
        ).strip()

        log.info("dalle_image_request", topic=topic[:50], content_type=content_type)

        response = await self._client.images.generate(
            model=settings.OPENAI_IMAGE_MODEL,
            prompt=prompt,
            n=1,
            size=settings.OPENAI_IMAGE_SIZE,
            quality=settings.OPENAI_IMAGE_QUALITY,
            response_format="url",
        )

        image_data = response.data[0]
        openai_url = image_data.url
        revised_prompt = image_data.revised_prompt

        log.debug("dalle_image_generated", url=openai_url[:80] if openai_url else None)

        # Download from OpenAI's temporary URL and upload to our permanent storage
        permanent_url = await self._upload_from_url(
            url=openai_url,
            workspace_id=workspace_id,
            topic=topic,
        )

        alt_text = self._generate_alt_text(topic=topic, niche=niche, content_type=content_type)

        return {
            "url": permanent_url,
            "prompt_used": prompt,
            "revised_prompt": revised_prompt,
            "alt_text": alt_text,
        }

    async def _upload_from_url(
        self, url: str, workspace_id: str, topic: str
    ) -> str:
        """Download image from OpenAI URL and upload to permanent storage."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            image_bytes = resp.content

        # Deterministic filename based on content hash
        content_hash = hashlib.sha256(image_bytes).hexdigest()[:16]
        safe_topic = re.sub(r"[^a-z0-9]+", "_", topic.lower())[:30]
        filename = f"generated/{workspace_id}/{safe_topic}_{content_hash}.png"

        permanent_url = await self._storage.upload_bytes(
            data=image_bytes,
            key=filename,
            content_type="image/png",
        )

        log.info("image_uploaded_to_storage", key=filename, size_kb=len(image_bytes) // 1024)
        return permanent_url

    def _generate_alt_text(self, topic: str, niche: str, content_type: str) -> str:
        type_label = content_type.replace("_", " ")
        return f"Instagram {type_label} for {niche}: {topic[:80]}"

    # ── Embeddings ─────────────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type((RateLimitError, TimeoutError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=20),
    )
    async def embed_text(self, text: str) -> list[float]:
        """
        Embed a single text string.
        For brand voice scoring, pass the full caption + hashtags joined.
        """
        # text-embedding-3-small max is 8191 tokens; truncate to ~6000 chars
        truncated = text[:6000]

        response = await self._client.embeddings.create(
            model=settings.OPENAI_EMBEDDING_MODEL,
            input=truncated,
            encoding_format="float",
        )
        return response.data[0].embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in a single API call (up to 100)."""
        if not texts:
            return []
        if len(texts) > 100:
            raise ValueError("embed_batch: max 100 texts at once")

        truncated = [t[:6000] for t in texts]
        response = await self._client.embeddings.create(
            model=settings.OPENAI_EMBEDDING_MODEL,
            input=truncated,
            encoding_format="float",
        )
        # Preserve original order
        ordered = sorted(response.data, key=lambda d: d.index)
        return [d.embedding for d in ordered]

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two embedding vectors."""
        va, vb = np.array(a), np.array(b)
        norm_a, norm_b = np.linalg.norm(va), np.linalg.norm(vb)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(va, vb) / (norm_a * norm_b))

    @staticmethod
    def compute_centroid(embeddings: list[list[float]]) -> list[float]:
        """Compute the mean embedding (brand voice centroid)."""
        if not embeddings:
            raise ValueError("Cannot compute centroid of empty list")
        matrix = np.array(embeddings)
        mean = matrix.mean(axis=0)
        # Normalise to unit vector
        norm = np.linalg.norm(mean)
        if norm > 0:
            mean = mean / norm
        return mean.tolist()

    @staticmethod
    def score_label(score: float) -> str:
        if score >= 0.85:
            return "Excellent"
        elif score >= 0.70:
            return "Good"
        elif score >= 0.55:
            return "Fair"
        return "Poor"


# ── Singleton ─────────────────────────────────────────────────────────────────
_openai_client: OpenAIContentClient | None = None


def get_openai_client() -> OpenAIContentClient:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAIContentClient()
    return _openai_client
