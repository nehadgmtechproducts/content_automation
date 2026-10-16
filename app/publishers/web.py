"""Stage 6 (web) — export an approved topic to the news site's content contract.

The site (`news_automation_web`) reads `content/articles/<slug>.json` plus
`public/media/<slug>/{hero.jpg,poster.jpg,video.mp4}` and validates every file against
a zod schema (schemaVersion 1). This module is the producer side of that contract.

Split in two on purpose: `build_web_article` is a pure function (dicts in, dict out) so
the mapping is unit-tested without a database or ffmpeg; `export_topic` does the I/O.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.categories import NAMES, normalize_category
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
    cover_width: int
    cover_height: int
    social_width: int
    social_height: int
    video_width: int
    video_height: int
    video_seconds: float


@dataclass
class MediaPaths:
    """Public URL paths of the published files, e.g. "/media/<slug>/cover-3fa91c2b.jpg"."""
    cover: str
    social: str
    poster: str
    video: str


# Filenames carry a short content hash. Next.js caches resized images by URL, and browsers/CDNs
# cache videos by URL, so overwriting a file at the same URL keeps serving the OLD one. A new
# content hash is a new URL, so a regenerated cover or re-rendered video can never go stale.
_ASSET_NAME = re.compile(r"^(cover|social|poster|video)(-[0-9a-f]{8})?\.(jpg|mp4)$")


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def publish_asset(src: Path, dest_dir: Path, stem: str) -> str:
    """Copy `src` into `dest_dir` as `<stem>-<hash><ext>` and return the filename.

    Idempotent: same content, same name, no second copy. Older versions are NOT removed here
    (the live page may still reference them until the new JSON is written) — see prune_assets().
    """
    name = f"{stem}-{file_hash(src)}{src.suffix.lower()}"
    target = dest_dir / name
    if not target.exists():
        shutil.copy2(src, target)
    return name


def prune_assets(dest_dir: Path, keep: set[str]) -> list[str]:
    """Delete superseded asset versions (including the legacy unhashed names)."""
    removed = []
    for f in dest_dir.iterdir():
        if f.is_file() and _ASSET_NAME.match(f.name) and f.name not in keep:
            f.unlink()
            removed.append(f.name)
    return sorted(removed)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_category(raw: str) -> tuple[str, list[str]]:
    """Map the draft's category onto the fixed taxonomy; other parts become keywords.

    'Technology / Privacy' -> ('Technology', ['Privacy']). The first part that matches the
    taxonomy decides the category. Raises ExportError if nothing matches: publishing under
    an invented heading would fragment the site's category pages, so a human must fix it.
    """
    category = normalize_category(raw)
    if category is None:
        raise ExportError(
            f"category {raw!r} is not in the taxonomy ({', '.join(NAMES)}). "
            f"Fix the draft's category and re-export.")
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    decider = next((i for i, p in enumerate(parts) if normalize_category(p) == category), None)
    extras = [p for i, p in enumerate(parts) if i != decider]
    return category, extras


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
                      media: MediaFacts, paths: MediaPaths) -> dict:
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
        # Text-free cover: the website draws the headline over it.
        "heroImage": {
            "src": paths.cover,
            "width": media.cover_width, "height": media.cover_height,
            "alt": f"Abstract cover image for the story: {article['title']}",
        },
        # Titled card: used only for link previews (Open Graph / Twitter).
        "socialImage": {
            "src": paths.social,
            "width": media.social_width, "height": media.social_height,
            "alt": f"Story card: {article['title']}",
        },
        "video": {
            "src": paths.video,
            "poster": paths.poster,
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
    # (stem, source file). The cover is optional: stories rendered before it existed get one
    # generated from their entity, exactly as the asset pipeline would have.
    required = {"social": src / "hero.jpg", "poster": src / "hero_vertical.jpg", "video": src / "final.mp4"}
    missing = [str(p) for p in required.values() if not p.exists()]
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
    cover_src = src / "cover.jpg"
    if not cover_src.exists():
        from app.providers.image_pillow import generate_cover
        generate_cover(entity or article["title"], cover_src)

    dest = web_dir / "public" / "media" / slug
    dest.mkdir(parents=True, exist_ok=True)
    names = {stem: publish_asset(path, dest, stem) for stem, path in
             {"cover": cover_src, **required}.items()}

    cw, ch = _image_size(dest / names["cover"])
    sw, sh = _image_size(dest / names["social"])
    vw, vh, secs = _probe_video(dest / names["video"])
    base = f"/media/{slug}"
    doc = build_web_article(
        topic_id=topic_id, entity=entity, article=article, meta=meta, sources=sources,
        published_at=published_at,
        media=MediaFacts(cw, ch, sw, sh, vw, vh, secs),
        paths=MediaPaths(cover=f"{base}/{names['cover']}", social=f"{base}/{names['social']}",
                         poster=f"{base}/{names['poster']}", video=f"{base}/{names['video']}"),
    )
    out = web_dir / "content" / "articles" / f"{slug}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Only now, with the new JSON in place, remove the superseded files.
    pruned = prune_assets(dest, keep=set(names.values()))
    return {"topic_id": topic_id, "slug": slug, "json": str(out), "sources": len(sources),
            "video": f"{vw}x{vh} {secs:.1f}s", "cover": names["cover"], "pruned": pruned}


def publish_topic(topic_id: int) -> dict:
    """Approval -> website. Exports the topic, then marks it `published`.

    Never raises: this runs inside the Telegram poller, and a failed export must not
    wedge the poll loop. On failure the topic stays `approved` (so it can be retried
    with `export-web`) and the error is returned for the caller to surface.
    """
    from pathlib import Path

    from app.db import session_scope
    from app.models import Topic
    from app.settings import get_settings

    settings = get_settings()
    if not settings.web_project_dir:
        return {"published": False, "reason": "WEB_PROJECT_DIR is not set"}
    try:
        result = export_topic(topic_id, Path(settings.web_project_dir).expanduser(),
                              settings.media_path)
    except Exception as exc:  # noqa: BLE001
        return {"published": False, "reason": str(exc)}

    with session_scope() as s:
        topic = s.get(Topic, topic_id)
        if topic is not None:
            topic.status = TopicStatus.published
    return {"published": True, "slug": result["slug"], "path": f"/articles/{result['slug']}"}
