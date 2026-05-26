"""
Groq API client — caption and hashtag generation.
Handles retries, structured output parsing, and prompt templating.
"""
import json
import re
from typing import Any

import structlog
from groq import AsyncGroq, RateLimitError, APIStatusError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from app.core.config import settings
from app.models.content import ToneType

log = structlog.get_logger(__name__)

# ── Prompt templates ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert Instagram content strategist and copywriter.
You create high-performing Instagram captions that drive engagement, saves, and shares.

Rules you ALWAYS follow:
1. Captions feel authentic and human — never corporate or robotic.
2. First line (hook) must stop the scroll in under 10 words.
3. Use line breaks generously for readability.
4. Hashtags are always provided separately, never in the caption body.
5. CTAs are specific and actionable, not generic ("Drop a 🔥 below" not "Engage with us").
6. You always respond with valid JSON — nothing else.
"""

CAPTION_PROMPT_TEMPLATE = """Generate an Instagram caption for the following:

BRAND NICHE: {niche}
TOPIC: {topic}
TONE: {tone}
{tone_description_block}
CONTENT TYPE: {content_type}
LANGUAGE: {language}
{extra_context_block}
{keywords_block}
{blocklist_block}

Respond ONLY with a JSON object in this exact format:
{{
    "caption": "Full caption text with emojis and line breaks. No hashtags here.",
    "hashtags": ["hashtag1", "hashtag2", ... (aim for {target_hashtags} total, mix of broad and niche)],
    "cta": "The specific call-to-action line",
    "hook": "The first line of the caption (for validation)",
    "reasoning": "Brief explanation of tone/strategy choices (for internal use)"
}}

The caption should be {word_count_hint} words. No markdown, no code fences. Just the JSON object."""

HASHTAG_REFRESH_PROMPT = """Given these hashtags that need refreshing (they've lost reach):
{stale_hashtags}

For the niche: {niche}
Suggest {count} replacement hashtags that are currently performing well.

Respond ONLY with a JSON array: ["hashtag1", "hashtag2", ...]
No # prefix, no markdown, just the JSON array."""

CALENDAR_THEME_PROMPT = """You are planning an Instagram content calendar for:

BRAND NICHE: {niche}
MONTH: {month_name} {year}
POSTS PER WEEK: {posts_per_week}
MONTHLY THEMES: {theme_hints}
CONTENT MIX: {content_mix}

Generate exactly {total_posts} post ideas for this month.
Distribute them across the month thoughtfully (not all on Mondays).
Consider holidays and events in {month_name}.

Respond ONLY with a JSON array of post objects:
[
  {{
    "day": 1,
    "topic": "Specific post topic (not generic)",
    "content_type": "feed_image|story|reel",
    "tone": "professional|witty|inspiring|casual|educational|promotional",
    "theme_category": "educational|entertaining|promotional|behind_scenes|user_generated"
  }},
  ...
]
Produce exactly {total_posts} items. No markdown, just the JSON array."""


# ── Tone descriptions ──────────────────────────────────────────────────────────

TONE_DESCRIPTIONS = {
    ToneType.WITTY: "Clever, playful, uses wordplay and humour. Light-hearted but still on-brand.",
    ToneType.PROFESSIONAL: "Polished, authoritative, and trustworthy. Confident but approachable.",
    ToneType.INSPIRING: "Motivational and uplifting. Creates emotional resonance. Uses storytelling.",
    ToneType.CASUAL: "Conversational, like texting a friend. Relaxed, uses contractions and colloquialisms.",
    ToneType.EDUCATIONAL: "Informative and clear. Teaches something valuable. Uses simple language.",
    ToneType.PROMOTIONAL: "Persuasive and benefit-focused. Creates urgency. Highlights value clearly.",
}

WORD_COUNT_HINTS = {
    ToneType.WITTY: "50-100",
    ToneType.PROFESSIONAL: "80-150",
    ToneType.INSPIRING: "100-200",
    ToneType.CASUAL: "40-80",
    ToneType.EDUCATIONAL: "150-300",
    ToneType.PROMOTIONAL: "60-120",
}


# ── Client ────────────────────────────────────────────────────────────────────

class GroqContentClient:
    """
    Async Groq client for caption generation.
    Thread-safe — create once, reuse across requests.
    """

    def __init__(self) -> None:
        self._client = AsyncGroq(api_key=settings.GROQ_API_KEY)

    @retry(
        retry=retry_if_exception_type((RateLimitError, TimeoutError)),
        stop=stop_after_attempt(settings.MAX_CAPTION_RETRIES),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before_sleep=before_sleep_log(log, "warning"),
    )
    async def generate_caption(
        self,
        *,
        topic: str,
        niche: str,
        tone: ToneType,
        content_type: str,
        language: str = "en",
        tone_override: str | None = None,
        extra_context: str | None = None,
        must_include_keywords: list[str] | None = None,
        avoid_keywords: list[str] | None = None,
        brand_hashtags: list[str] | None = None,
        niche_hashtags: list[str] | None = None,
        max_hashtags: int = 25,
        cta_hint: str | None = None,
    ) -> dict[str, Any]:
        """
        Generate a structured caption response from Groq.
        Returns parsed dict with keys: caption, hashtags, cta, hook, reasoning.
        """
        tone_desc = tone_override or TONE_DESCRIPTIONS.get(tone, "")
        target_hashtags = min(max_hashtags, 25)
        existing_hashtags = (brand_hashtags or []) + (niche_hashtags or [])

        prompt = CAPTION_PROMPT_TEMPLATE.format(
            niche=niche,
            topic=topic,
            tone=tone.value.upper(),
            tone_description_block=f"TONE DESCRIPTION: {tone_desc}\n" if tone_desc else "",
            content_type=content_type.replace("_", " ").title(),
            language=language,
            extra_context_block=f"ADDITIONAL CONTEXT: {extra_context}\n" if extra_context else "",
            keywords_block=(
                f"MUST INCLUDE KEYWORDS: {', '.join(must_include_keywords)}\n"
                if must_include_keywords else ""
            ),
            blocklist_block=(
                f"AVOID THESE TOPICS/WORDS: {', '.join(avoid_keywords)}\n"
                if avoid_keywords else ""
            ),
            target_hashtags=target_hashtags,
            word_count_hint=WORD_COUNT_HINTS.get(tone, "80-150"),
        )

        if cta_hint:
            prompt += f"\nCTA HINT: {cta_hint}"
        if existing_hashtags:
            prompt += f"\nALWAYS INCLUDE THESE BRAND HASHTAGS: {', '.join(existing_hashtags[:15])}"

        log.info("groq_caption_request", topic=topic[:50], tone=tone, model=settings.GROQ_MODEL)

        response = await self._client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=settings.GROQ_MAX_TOKENS,
            temperature=settings.GROQ_TEMPERATURE,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content
        log.debug("groq_raw_response", length=len(raw or ""))

        return self._parse_caption_response(raw, tone=tone, brand_hashtags=brand_hashtags or [])

    def _parse_caption_response(
        self, raw: str | None, tone: ToneType, brand_hashtags: list[str]
    ) -> dict[str, Any]:
        if not raw:
            raise ValueError("Groq returned empty response")

        # Strip any accidental markdown fences
        clean = re.sub(r"```(?:json)?|```", "", raw).strip()

        try:
            data = json.loads(clean)
        except json.JSONDecodeError as e:
            log.error("groq_json_parse_error", error=str(e), raw=raw[:200])
            raise ValueError(f"Groq response is not valid JSON: {e}") from e

        # Validate required fields
        required = {"caption", "hashtags", "cta"}
        missing = required - data.keys()
        if missing:
            raise ValueError(f"Groq response missing fields: {missing}")

        # Sanitise hashtags — strip #, lowercase, deduplicate
        hashtags: list[str] = []
        seen: set[str] = set()
        for tag in data.get("hashtags", []):
            clean_tag = tag.lstrip("#").lower().strip()
            if clean_tag and clean_tag not in seen:
                hashtags.append(clean_tag)
                seen.add(clean_tag)

        # Ensure brand hashtags are present
        for brand_tag in brand_hashtags:
            clean_brand = brand_tag.lstrip("#").lower().strip()
            if clean_brand not in seen:
                hashtags.append(clean_brand)
                seen.add(clean_brand)

        return {
            "caption": data["caption"].strip(),
            "hashtags": hashtags,
            "cta": data.get("cta", "").strip() or None,
            "hook": data.get("hook", "").strip(),
            "reasoning": data.get("reasoning", ""),
            "tone_used": tone,
        }

    @retry(
        retry=retry_if_exception_type((RateLimitError, TimeoutError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=20),
    )
    async def generate_calendar_plan(
        self,
        *,
        niche: str,
        month: int,
        year: int,
        posts_per_week: int,
        theme_hints: list[str],
        content_mix: dict[str, float],
        total_posts: int,
    ) -> list[dict[str, Any]]:
        """Generate a month's worth of post ideas."""
        import calendar
        month_name = calendar.month_name[month]

        mix_description = ", ".join(
            f"{int(v*100)}% {k.replace('_', ' ')}" for k, v in content_mix.items()
        )

        prompt = CALENDAR_THEME_PROMPT.format(
            niche=niche,
            month_name=month_name,
            year=year,
            posts_per_week=posts_per_week,
            theme_hints=", ".join(theme_hints) if theme_hints else "none specified",
            content_mix=mix_description,
            total_posts=total_posts,
        )

        log.info("groq_calendar_request", month=month, year=year, total=total_posts)

        response = await self._client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=settings.GROQ_MAX_TOKENS * 2,
            temperature=0.85,  # slightly more creative for planning
            response_format={"type": "json_object"},
        )

        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            raise ValueError("Groq returned empty response for calendar plan")
        # Calendar prompt returns array wrapped in object
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
            # Sometimes model wraps in {"posts": [...]}
            for key in ("posts", "calendar", "items", "plan"):
                if key in parsed and isinstance(parsed[key], list):
                    return parsed[key]
            raise ValueError(f"Unexpected calendar response shape: {list(parsed.keys())}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Calendar plan JSON parse error: {e}") from e

    async def refresh_hashtags(
        self, stale_hashtags: list[str], niche: str, count: int = 15
    ) -> list[str]:
        """Suggest replacement hashtags for stale ones."""
        prompt = HASHTAG_REFRESH_PROMPT.format(
            stale_hashtags=", ".join(stale_hashtags),
            niche=niche,
            count=count,
        )
        response = await self._client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=0.5,
        )
        raw = response.choices[0].message.content or "[]"
        try:
            tags = json.loads(raw)
            return [t.lstrip("#").lower() for t in tags if isinstance(t, str)]
        except json.JSONDecodeError:
            return []


# ── Singleton ─────────────────────────────────────────────────────────────────
_groq_client: GroqContentClient | None = None


def get_groq_client() -> GroqContentClient:
    global _groq_client
    if _groq_client is None:
        _groq_client = GroqContentClient()
    return _groq_client
