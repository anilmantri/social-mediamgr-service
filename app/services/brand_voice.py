"""
BrandVoiceService — manages brand voice profiles and scores generated content
against the brand's established voice using embedding cosine similarity.

Flow:
  1. User sets up BrandProfile with niche, tone, rules.
  2. As content is approved, we call update_voice_embedding() to update the centroid.
  3. On each new generation, score_content() returns a 0-1 similarity score.
"""
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import BrandProfile, ContentVersion, DraftContent, ContentStatus
from app.schemas.content import BrandProfileUpsert, BrandVoiceScoreResponse
from app.services.openai_client import OpenAIContentClient, get_openai_client
from app.core.config import settings

log = structlog.get_logger(__name__)


class BrandVoiceService:
    def __init__(self, db: AsyncSession, openai: OpenAIContentClient | None = None) -> None:
        self._db = db
        self._openai = openai or get_openai_client()

    async def get_or_create_profile(self, workspace_id: str) -> BrandProfile:
        result = await self._db.execute(
            select(BrandProfile).where(BrandProfile.workspace_id == workspace_id)
        )
        profile = result.scalar_one_or_none()
        if not profile:
            profile = BrandProfile(
                workspace_id=workspace_id,
                niche="General",
                default_tone="professional",
            )
            self._db.add(profile)
            await self._db.flush()
        return profile

    async def upsert_profile(
        self, workspace_id: str, data: BrandProfileUpsert
    ) -> BrandProfile:
        profile = await self.get_or_create_profile(workspace_id)
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(profile, field, value)
        await self._db.flush()
        log.info("brand_profile_updated", workspace_id=str(workspace_id))
        return profile

    async def score_content(
        self,
        workspace_id: str,
        caption: str,
        hashtags: list[str],
    ) -> BrandVoiceScoreResponse:
        """
        Score new content against the workspace brand voice centroid.
        Returns score 0-1 and a human-readable label.
        """
        profile = await self.get_or_create_profile(workspace_id)

        if not profile.voice_embedding or profile.embedding_sample_count < settings.BRAND_VOICE_MIN_SAMPLES:
            return BrandVoiceScoreResponse(
                score=0.0,
                label="Unscored",
                sample_count=profile.embedding_sample_count,
                min_samples_required=settings.BRAND_VOICE_MIN_SAMPLES,
                explanation=(
                    f"Need at least {settings.BRAND_VOICE_MIN_SAMPLES} approved posts "
                    f"to build a brand voice profile. "
                    f"Currently have {profile.embedding_sample_count}."
                ),
            )

        content_text = f"{caption}\n{' '.join('#' + h for h in hashtags)}"
        content_embedding = await self._openai.embed_text(content_text)

        score = self._openai.cosine_similarity(content_embedding, profile.voice_embedding)
        # Scale: cosine similarity for similar text is typically 0.7-1.0
        # Normalise to 0-1 range using min threshold of 0.5
        normalised = max(0.0, (score - 0.5) / 0.5)
        label = self._openai.score_label(normalised)

        return BrandVoiceScoreResponse(
            score=round(normalised, 3),
            label=label,
            sample_count=profile.embedding_sample_count,
            min_samples_required=settings.BRAND_VOICE_MIN_SAMPLES,
            explanation=f"Brand voice similarity: {label} ({normalised:.0%}). "
                        f"Based on {profile.embedding_sample_count} approved posts.",
        )

    async def update_voice_embedding(
        self, workspace_id: str, approved_caption: str, approved_hashtags: list[str]
    ) -> None:
        """
        Update brand voice centroid with newly approved content.
        Uses online incremental update: new_centroid = (n-1/n)*old + (1/n)*new
        This avoids re-embedding all historical posts on every approval.
        """
        profile = await self.get_or_create_profile(workspace_id)

        content_text = f"{approved_caption}\n{' '.join('#' + h for h in approved_hashtags)}"
        new_embedding = await self._openai.embed_text(content_text)

        n = profile.embedding_sample_count + 1

        if profile.voice_embedding is None or profile.embedding_sample_count == 0:
            profile.voice_embedding = new_embedding
        else:
            # Weighted incremental average
            import numpy as np
            old = np.array(profile.voice_embedding)
            new = np.array(new_embedding)
            updated = ((n - 1) / n) * old + (1 / n) * new
            # Re-normalise to unit vector
            norm = np.linalg.norm(updated)
            if norm > 0:
                updated = updated / norm
            profile.voice_embedding = updated.tolist()

        profile.embedding_sample_count = n
        profile.embedding_updated_at = datetime.now(timezone.utc)
        await self._db.flush()

        log.info(
            "brand_voice_embedding_updated",
            workspace_id=str(workspace_id),
            sample_count=n,
        )
