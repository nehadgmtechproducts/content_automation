"""Stage 6 (web) — export an approved topic to the news site's content contract.

The site (`news_automation_web`) reads `content/articles/<slug>.json` plus
`public/media/<slug>/{hero.jpg,poster.jpg,video.mp4}` and validates every file against
a zod schema (schemaVersion 1). This module is the producer side of that contract.

Split in two on purpose: `build_web_article` is a pure function (dicts in, dict out) so
the mapping is unit-tested without a database or ffmpeg; `export_topic` does the I/O.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.models import TopicStatus

SCHEMA_VERSION = 1
META_DESCRIPTION_MAX = 160          # search engines truncate around here
# Only reviewed topics may reach the public site: /about tells readers a person
# reviews every story, so the exporter enforces it.
EXPORTABLE = {TopicStatus.approved, TopicStatus.published}


class ExportError(RuntimeError):
    pass


@dataclass
class MediaFacts:
    hero_width: int
    hero_height: int
    video_width: int
    video_height: int
    video_seconds: float


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_category(raw: str) -> tuple[str, list[str]]:
    """'Technology / Privacy' -> ('Technology', ['Privacy']).

    The site groups by ONE category; the extra parts are kept as keywords so no signal
    from the model's free-text label is lost.
    """
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    return (parts[0] if parts else "Technology"), parts[1:]


def trim_description(text: str, limit: int = META_DESCRIPTION_MAX) -> str:
    """Cut at the last sentence end, else last word, under `limit` — never mid-word."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sentence = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sentence >= limit * 0.5:
        return cut[: sentence + 1]
    return cut[: cut.rfind(" ")].rstrip(" ,;:-") + "…"


def build_web_article(*, topic_id: int, entity: str, article: dict, meta: dict,
                      sources: list[dict], published_at: datetime,
                      media: MediaFacts) -> dict:
    """Pure mapping from backend rows to the web contract."""
    slug = article["slug"]
    category, extra = split_category(meta.get("category", ""))
    keywords = list(dict.fromkeys([*meta.get("target_keywords", []), *extra]))
    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": f"topic-{topic_id}",
        "slug": slug,
        "title": article["title"],
        "dek": article["dek"],
        "metaDescription": trim_description(article["meta_description"]),
        "category": category,
        "entity": entity,
        "publishedAt": _iso(published_at),
        "updatedAt": _iso(published_at),
        "heroImage": {
            "src": f"/media/{slug}/hero.jpg",
            "width": media.hero_width, "height": media.hero_height,
            "alt": f"Editorial card for the story: {article['title']}",
        },
        "video": {
            "src": f"/media/{slug}/video.mp4",
            "poster": f"/media/{slug}/poster.jpg",
            "width": media.video_width, "height": media.video_height,
            "durationSeconds": round(media.video_seconds, 2),
        },
        "sections": [{"heading": s["heading"], "body": s["body_markdown"]}
                     for s in article["sections"]],
        "keyTakeaways": article["key_takeaways"],
        "whyItMatters": article["why_it_matters"],
        "sources": [{
            "index": s["index"], "title": s["title"], "domain": s["domain"], "url": s["url"],
            "publishedAt": s["published_at"].strftime("%Y-%m-%d") if s.get("published_at") else None,
        } for s in sources],
        "keywords": keywords,
        "hashtags": meta.get("hashtags", []),
    }


def _probe_video(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    return j["streams"][0]["width"], j["streams"][0]["height"], float(j["format"]["duration"])


def _image_size(path: Path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def export_topic(topic_id: int, web_dir: Path, media_root: Path,
                 allow_unapproved: bool = False) -> dict:
    """Write the JSON + copy the media into the web project. Returns a summary."""
    from app.db import session_scope
    from app.models import Draft, ScrapedArticle, Topic

    if not (web_dir / "content").is_dir():
        raise ExportError(f"{web_dir} does not look like the web project (no content/ dir)")

    src = media_root / str(topic_id)
    needed = {"hero.jpg": src / "hero.jpg", "poster.jpg": src / "hero_vertical.jpg",
              "video.mp4": src / "final.mp4"}
    missing = [str(p) for p in needed.values() if not p.exists()]
    if missing:
        raise ExportError(f"missing rendered assets for topic {topic_id}: {', '.join(missing)}")

    with session_scope() as s:
        topic = s.get(Topic, topic_id)
        if topic is None:
            raise ExportError(f"topic {topic_id} not found")
        if topic.status not in EXPORTABLE and not allow_unapproved:
            raise ExportError(
                f"topic {topic_id} is '{topic.status.value}', not approved. Approve it in "
                f"Telegram first (the site promises human review), or pass --allow-unapproved "
                f"for a sample/demo export.")
        draft = s.query(Draft).filter_by(topic_id=topic_id).one_or_none()
        if draft is None:
            raise ExportError(f"topic {topic_id} has no draft — run synthesis first")
        rows = (s.query(ScrapedArticle).filter_by(topic_id=topic_id)
                .order_by(ScrapedArticle.source_index).all())
        sources = [{"index": r.source_index, "title": r.title, "domain": r.domain,
                    "url": r.url, "published_at": r.published_at} for r in rows]
        article, meta = draft.article, draft.meta
        entity, published_at = topic.primary_entity, draft.created_at

    slug = article["slug"]
    dest = web_dir / "public" / "media" / slug
    dest.mkdir(parents=True, exist_ok=True)
    for name, path in needed.items():
        shutil.copy(path, dest / name)

    hw, hh = _image_size(dest / "hero.jpg")
    vw, vh, secs = _probe_video(dest / "video.mp4")
    doc = build_web_article(
        topic_id=topic_id, entity=entity, article=article, meta=meta, sources=sources,
        published_at=published_at,
        media=MediaFacts(hw, hh, vw, vh, secs),
    )
    out = web_dir / "content" / "articles" / f"{slug}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"topic_id": topic_id, "slug": slug, "json": str(out), "sources": len(sources),
            "video": f"{vw}x{vh} {secs:.1f}s"}
