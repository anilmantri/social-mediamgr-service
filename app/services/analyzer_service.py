"""
Module 4 — AnalyzerService

Deep analytics beyond the overview:
  - Per-post performance breakdown ranked by engagement
  - Content type performance comparison (image vs carousel vs reel)
  - Hashtag effectiveness scoring
  - Posting time heatmap (day × hour engagement matrix)
  - Follower growth rate
  - AI improvement suggestions (Groq analyses your metrics)
  - Best and worst performing posts
  - Caption length vs engagement correlation
"""
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import ContentType, DraftContent
from app.models.scheduler import PostMetrics, ScheduledPost, PublishStatus

log = structlog.get_logger(__name__)


class AnalyzerService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Full report ───────────────────────────────────────────────────────────

    async def get_full_report(
        self,
        workspace_id: uuid.UUID,
        days: int = 30,
    ) -> dict:
        """Single call returning all analyzer data."""
        import asyncio
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        metrics = await self._load_metrics(workspace_id, cutoff)

        if not metrics:
            return {
                "workspace_id": str(workspace_id),
                "period_days": days,
                "has_data": False,
                "message": "No published posts with metrics yet. Publish posts and wait for the insights sync (runs every 6 hours).",
            }

        (
            top_posts,
            content_type_breakdown,
            hashtag_analysis,
            time_heatmap,
            caption_analysis,
            summary,
        ) = await asyncio.gather(
            self._top_posts(metrics),
            self._content_type_breakdown(workspace_id, metrics, cutoff),
            self._hashtag_analysis(metrics, workspace_id, cutoff),
            self._time_heatmap(metrics),
            self._caption_analysis(metrics, workspace_id, cutoff),
            self._summary_stats(metrics),
        )

        suggestions = await self._ai_suggestions(
            workspace_id=workspace_id,
            summary=summary,
            top_posts=top_posts,
            hashtag_analysis=hashtag_analysis,
        )

        return {
            "workspace_id": str(workspace_id),
            "period_days": days,
            "has_data": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "top_posts": top_posts,
            "worst_posts": await self._worst_posts(metrics),
            "content_type_breakdown": content_type_breakdown,
            "hashtag_analysis": hashtag_analysis,
            "time_heatmap": time_heatmap,
            "caption_analysis": caption_analysis,
            "ai_suggestions": suggestions,
        }

    # ── Summary stats ─────────────────────────────────────────────────────────

    async def _summary_stats(self, metrics: list) -> dict:
        if not metrics:
            return {}
        total = len(metrics)
        avg_er   = sum(m.engagement_rate or 0 for m in metrics) / total
        avg_reach = sum(m.reach for m in metrics) / total
        avg_likes = sum(m.likes for m in metrics) / total
        avg_saves = sum(m.saves for m in metrics) / total
        total_reach = sum(m.reach for m in metrics)
        total_impressions = sum(m.impressions for m in metrics)

        # Week-over-week change
        half = total // 2
        if half > 0:
            recent_er  = sum(m.engagement_rate or 0 for m in metrics[:half]) / half
            older_er   = sum(m.engagement_rate or 0 for m in metrics[half:]) / half
            er_change  = round((recent_er - older_er) / max(older_er, 0.001) * 100, 1)
        else:
            er_change = 0.0

        return {
            "total_posts": total,
            "avg_engagement_rate": round(avg_er * 100, 2),
            "avg_reach": int(avg_reach),
            "avg_likes": int(avg_likes),
            "avg_saves": int(avg_saves),
            "total_reach": total_reach,
            "total_impressions": total_impressions,
            "er_change_pct": er_change,
            "er_trend": "up" if er_change > 0 else "down" if er_change < 0 else "stable",
        }

    # ── Top posts ─────────────────────────────────────────────────────────────

    async def _top_posts(self, metrics: list, limit: int = 5) -> list[dict]:
        sorted_m = sorted(
            metrics,
            key=lambda m: m.engagement_rate or 0,
            reverse=True,
        )
        return [self._metric_to_dict(m) for m in sorted_m[:limit]]

    async def _worst_posts(self, metrics: list, limit: int = 3) -> list[dict]:
        sorted_m = sorted(metrics, key=lambda m: m.engagement_rate or 0)
        return [self._metric_to_dict(m) for m in sorted_m[:limit]]

    def _metric_to_dict(self, m) -> dict:
        return {
            "scheduled_post_id": str(m.scheduled_post_id),
            "reach": m.reach,
            "impressions": m.impressions,
            "likes": m.likes,
            "comments": m.comments,
            "saves": m.saves,
            "shares": m.shares,
            "engagement_rate": round((m.engagement_rate or 0) * 100, 2),
            "published_at": m.published_at.isoformat() if m.published_at else None,
            "day_of_week": m.day_of_week,
            "hour_of_day": m.hour_of_day,
        }

    # ── Content type breakdown ────────────────────────────────────────────────

    async def _content_type_breakdown(
        self, workspace_id: uuid.UUID, metrics: list, cutoff: datetime
    ) -> list[dict]:
        """Compare performance across feed images, carousels, reels, stories."""
        # Get content_type for each scheduled post
        post_ids = [m.scheduled_post_id for m in metrics]
        if not post_ids:
            return []

        result = await self._db.execute(
            select(ScheduledPost.id, DraftContent.content_type)
            .join(DraftContent, DraftContent.id == ScheduledPost.draft_content_id)
            .where(ScheduledPost.id.in_(post_ids))
        )
        type_map = {str(row.id): row.content_type for row in result.all()}

        # Aggregate by type
        by_type: dict[str, list] = defaultdict(list)
        for m in metrics:
            ct = type_map.get(str(m.scheduled_post_id))
            if ct:
                by_type[ct.value].append(m)

        out = []
        for ct, ms in by_type.items():
            avg_er    = sum(m.engagement_rate or 0 for m in ms) / len(ms)
            avg_reach = sum(m.reach for m in ms) / len(ms)
            out.append({
                "content_type": ct,
                "post_count": len(ms),
                "avg_engagement_rate": round(avg_er * 100, 2),
                "avg_reach": int(avg_reach),
                "avg_likes": int(sum(m.likes for m in ms) / len(ms)),
                "avg_saves": int(sum(m.saves for m in ms) / len(ms)),
            })

        return sorted(out, key=lambda x: x["avg_engagement_rate"], reverse=True)

    # ── Hashtag analysis ──────────────────────────────────────────────────────

    async def _hashtag_analysis(
        self, metrics: list, workspace_id: uuid.UUID, cutoff: datetime
    ) -> list[dict]:
        """Score each hashtag by the avg ER of posts that used it."""
        post_ids = [m.scheduled_post_id for m in metrics]
        if not post_ids:
            return []

        result = await self._db.execute(
            select(ScheduledPost.id, ScheduledPost.hashtags_snapshot)
            .where(ScheduledPost.id.in_(post_ids))
        )
        hashtag_map = {str(row.id): row.hashtags_snapshot for row in result.all()}

        er_map = {str(m.scheduled_post_id): m.engagement_rate or 0 for m in metrics}

        tag_scores: dict[str, list[float]] = defaultdict(list)
        for post_id, tags in hashtag_map.items():
            er = er_map.get(post_id, 0)
            for tag in (tags or []):
                tag_scores[tag.lstrip("#").lower()].append(er)

        out = []
        for tag, ers in tag_scores.items():
            if len(ers) < 2:
                continue
            avg_er = sum(ers) / len(ers)
            out.append({
                "hashtag": f"#{tag}",
                "post_count": len(ers),
                "avg_engagement_rate": round(avg_er * 100, 2),
                "performance": (
                    "excellent" if avg_er >= 0.04
                    else "good"  if avg_er >= 0.02
                    else "average" if avg_er >= 0.01
                    else "poor"
                ),
            })

        return sorted(out, key=lambda x: x["avg_engagement_rate"], reverse=True)[:20]

    # ── Time heatmap ──────────────────────────────────────────────────────────

    async def _time_heatmap(self, metrics: list) -> list[dict]:
        """
        Build a 7×24 engagement heatmap (day of week × hour of day).
        Returns flat list of {day, hour, avg_er, post_count}.
        """
        cell: dict[tuple[int, int], list[float]] = defaultdict(list)

        for m in metrics:
            if m.day_of_week is not None and m.hour_of_day is not None:
                cell[(m.day_of_week, m.hour_of_day)].append(m.engagement_rate or 0)

        out = []
        for (day, hour), ers in cell.items():
            out.append({
                "day_of_week": day,
                "hour_of_day": hour,
                "post_count":  len(ers),
                "avg_er":      round(sum(ers) / len(ers) * 100, 2),
            })

        return sorted(out, key=lambda x: x["avg_er"], reverse=True)

    # ── Caption length analysis ───────────────────────────────────────────────

    async def _caption_analysis(
        self, metrics: list, workspace_id: uuid.UUID, cutoff: datetime
    ) -> dict:
        """Correlate caption length with engagement rate."""
        post_ids = [m.scheduled_post_id for m in metrics]
        if not post_ids:
            return {}

        result = await self._db.execute(
            select(ScheduledPost.id, ScheduledPost.caption_snapshot)
            .where(ScheduledPost.id.in_(post_ids))
        )
        caption_map = {str(row.id): row.caption_snapshot for row in result.all()}
        er_map = {str(m.scheduled_post_id): m.engagement_rate or 0 for m in metrics}

        buckets = {
            "short (< 100 chars)":   [],
            "medium (100-300 chars)": [],
            "long (300+ chars)":      [],
        }

        for post_id, caption in caption_map.items():
            er = er_map.get(post_id, 0)
            length = len(caption)
            if length < 100:
                buckets["short (< 100 chars)"].append(er)
            elif length < 300:
                buckets["medium (100-300 chars)"].append(er)
            else:
                buckets["long (300+ chars)"].append(er)

        result_out = {}
        for bucket, ers in buckets.items():
            if ers:
                result_out[bucket] = {
                    "post_count": len(ers),
                    "avg_engagement_rate": round(sum(ers) / len(ers) * 100, 2),
                }

        best_bucket = max(result_out.items(), key=lambda x: x[1]["avg_engagement_rate"])[0] if result_out else None

        return {
            "buckets": result_out,
            "best_length": best_bucket,
            "recommendation": f"Posts with {best_bucket} tend to perform best for your audience." if best_bucket else None,
        }

    # ── AI suggestions ────────────────────────────────────────────────────────

    async def _ai_suggestions(
        self,
        workspace_id: uuid.UUID,
        summary: dict,
        top_posts: list,
        hashtag_analysis: list,
    ) -> list[dict]:
        """
        Uses Groq to generate 3-5 specific, actionable improvement suggestions
        based on the workspace's actual performance data.
        """
        try:
            from app.services.groq_client import get_groq_client
            client = get_groq_client()

            top_hashtags = [h["hashtag"] for h in hashtag_analysis[:5]]
            poor_hashtags = [h["hashtag"] for h in hashtag_analysis if h["performance"] == "poor"][:3]

            prompt = f"""You are a social media analyst. Analyse this Instagram account's performance data and provide exactly 5 specific, actionable improvement suggestions.

PERFORMANCE DATA:
- Average engagement rate: {summary.get('avg_engagement_rate', 0)}%
- Engagement trend: {summary.get('er_trend', 'unknown')} ({summary.get('er_change_pct', 0):+.1f}% vs previous period)
- Average reach per post: {summary.get('avg_reach', 0)}
- Total posts analysed: {summary.get('total_posts', 0)}
- Best hashtags: {', '.join(top_hashtags) if top_hashtags else 'no data'}
- Underperforming hashtags: {', '.join(poor_hashtags) if poor_hashtags else 'none'}

Respond ONLY with a JSON array of exactly 5 objects. Each object must have:
- "title": short action title (max 8 words)
- "description": specific recommendation (max 30 words)
- "impact": "high" | "medium" | "low"
- "category": "content" | "timing" | "hashtags" | "engagement" | "growth"

Example format:
[{{"title": "Post more carousels", "description": "Your carousels get 2x more saves than single images. Aim for 3 carousels per week.", "impact": "high", "category": "content"}}]"""

            import json
            # Use Groq API directly for a simple completion
            import groq as _groq
            from app.core.config import settings as _cfg
            _groq_client = _groq.AsyncGroq(api_key=_cfg.GROQ_API_KEY)
            _response = await _groq_client.chat.completions.create(
                model=_cfg.GROQ_MODEL,
                messages=[
                    {"role": "system", "content": "You are a social media analytics expert. Respond only with valid JSON."},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.4,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            raw = (_response.choices[0].message.content or "").strip()
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            suggestions = json.loads(raw)
            if isinstance(suggestions, list):
                return suggestions[:5]
            return []
        except Exception as e:
            log.warning("ai_suggestions_failed", error=str(e))
            return self._default_suggestions(summary)

    def _default_suggestions(self, summary: dict) -> list[dict]:
        """Fallback suggestions when Groq is unavailable."""
        er = summary.get("avg_engagement_rate", 0)
        suggestions = []
        if er < 1:
            suggestions.append({
                "title": "Increase posting frequency",
                "description": "Post at least 4-5 times per week to improve reach and algorithmic visibility.",
                "impact": "high",
                "category": "growth",
            })
        suggestions.extend([
            {
                "title": "Use AI optimal posting times",
                "description": "Enable AI time scheduling to publish when your audience is most active.",
                "impact": "medium",
                "category": "timing",
            },
            {
                "title": "Add strong CTAs to captions",
                "description": "End every caption with a question or call-to-action to boost comment rate.",
                "impact": "medium",
                "category": "content",
            },
            {
                "title": "Mix content formats",
                "description": "Alternate between single images, carousels, and reels for broader reach.",
                "impact": "high",
                "category": "content",
            },
            {
                "title": "Engage within first 30 minutes",
                "description": "Reply to comments quickly after posting to boost early engagement signals.",
                "impact": "high",
                "category": "engagement",
            },
        ])
        return suggestions[:5]

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _load_metrics(
        self, workspace_id: uuid.UUID, cutoff: datetime
    ) -> list:
        result = await self._db.execute(
            select(PostMetrics)
            .where(
                PostMetrics.workspace_id == workspace_id,
                PostMetrics.published_at >= cutoff,
            )
            .order_by(PostMetrics.published_at.desc())
        )
        return result.scalars().all()
