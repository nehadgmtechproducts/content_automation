"""Stage 4 orchestration — turn a Draft into a publishable asset bundle."""
from __future__ import annotations

import json
import logging
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.assets.align import align
from app.assets.render import Segment, beat_spans, plan_segments, probe, render
from app.assets.subtitles import write_ass
from app.providers.base import BrollRequest, ImageRequest, VoiceRequest, load_providers
from app.providers.image_nvidia import build_prompt, generate_art, generic_prompt
from app.providers.commons_photo import find_commons_photo
from app.providers.source_photo import find_source_photo
from app.providers.image_pillow import generate_cover
from app.settings import get_settings
from app.assets.safety import footage_context, safe_shot
from app.categories import normalize_category

log = logging.getLogger(__name__)

AI_LABEL = "AI-generated illustration"
COVER_SOURCE_FILE = "cover.source"   # "source" | "commons" | "ai" | "gradient": read by the web exporter
COVER_CREDIT_FILE = "cover.credit.json"   # the outlet and page a "source" picture came from


@dataclass
class AssetResult:
    directory: Path
    hero_image: Path
    hero_vertical: Path | None   # None in articles-only mode (it is the video's opening card)
    voiceover: Path | None
    subtitles: Path | None
    video: Path | None
    duration: float
    providers_used: dict
    cover_image: Path | None = None   # text-free website cover (see generate_cover)


def _word_count(text: str) -> int:
    return len([t for t in re.findall(r"[\w'’\-]+", text) if t])


DEFAULT_TARGET_SECONDS = 45.0
# Camera variety for the 2nd, 3rd... clip of a long beat, so it doesn't repeat one shot.
ANGLE_VARIANTS = ("close-up detail shot", "wide establishing shot", "slow tracking shot")
# Beyond this relative error, re-synthesize once at a corrected pace.
DURATION_TOLERANCE = 0.12


def _synthesize_to_target(tts, narration: str, out_path: Path, target: float) -> Path:
    """Synthesize, measure, and correct the pace once if the result misses the target.

    A 45-second script is only 45 seconds if the voice cooperates. Providers vary
    (and macOS `say` differs from Piper), so rather than trusting a configured rate,
    measure the result and adjust. One correction pass — chasing it further costs time
    for diminishing returns.
    """
    from app.assets.align import audio_duration

    voice_path = tts.synthesize(VoiceRequest(narration, out_path, target_seconds=target))
    actual = audio_duration(voice_path)
    if actual <= 0 or target <= 0:
        return voice_path

    error = abs(actual - target) / target
    if error <= DURATION_TOLERANCE:
        log.info("voiceover %.1fs vs %.1fs target — within tolerance", actual, target)
        return voice_path

    scale = actual / target
    log.info("voiceover %.1fs vs %.1fs target — re-synthesizing at %.2fx pace",
             actual, target, scale)
    corrected = tts.synthesize(
        VoiceRequest(narration, out_path, target_seconds=target, speed_scale=scale)
    )
    log.info("corrected voiceover: %.1fs", audio_duration(corrected))
    return corrected


def narration_text(script: dict) -> str:
    return " ".join(b.get("narration", "").strip() for b in script.get("beats", [])).strip()


def _write_articles_only_package(topic_id: int, out_dir: Path, article: dict, script: dict,
                                 metadata: dict, hero: Path, cover: Path,
                                 providers_used: dict, art: Path | None = None,
                                 art_kind: str | None = None) -> AssetResult:
    """Articles-only finish: no voiceover, subtitles, b-roll or MP4 — images only."""
    package = {
        "topic_id": topic_id, "article": article, "script": script, "metadata": metadata,
        "assets": {"hero": hero.name, "cover": cover.name, "art": art.name if art else None, "art_kind": art_kind},
        "providers_used": providers_used,
    }
    (out_dir / "package.json").write_text(json.dumps(package, indent=2), encoding="utf-8")
    log.info("topic %s: articles-only assets (video generation is off)", topic_id)
    return AssetResult(
        directory=out_dir, hero_image=hero, hero_vertical=None, voiceover=None,
        subtitles=None, video=None, duration=0.0, providers_used=providers_used,
        cover_image=cover,
    )


def produce_assets(topic_id: int, article: dict, script: dict, metadata: dict,
                   entity: str = "", music: Path | None = None,
                   sources: list[dict] | None = None) -> AssetResult:
    settings = get_settings()
    out_dir = settings.media_path / str(topic_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    providers = load_providers()

    headline = article.get("title") or "Untitled"
    dek = article.get("dek", "")

    # --- the story's picture. Order: a photo from one of its own sources (COVER_IMAGE_MODE=source), else an AI
    # illustration, else nothing (the gradient). Neither step raises: a story is never held up by its picture. ---
    art, art_kind, photo, commons = None, None, None, None
    mode = getattr(settings, "cover_image_mode", "source")
    if mode == "source":
        if metadata.get("sensitive"):
            # a photograph of a real child or survivor is what the law forbids us to show (see identity_guard.py)
            log.info("topic %s is flagged sensitive: no real photograph at all", topic_id)
        else:
            if sources:
                photo = find_source_photo(sources, out_dir / "photo.jpg")
                if photo:
                    art, art_kind = photo.path, "source"
            if art is None:      # no safe photo from the sources: a licence-safe FILE photo of the story's subject
                body = " ".join([dek, *[s.get("body_markdown", "") for s in article.get("sections", [])]])
                commons = find_commons_photo(headline, body, metadata.get("target_keywords", []), entity, out_dir / "photo.jpg",
                                             allow_sharealike=getattr(settings, "commons_allow_sharealike", False))
                if commons:
                    art, art_kind = commons.path, "commons"
    if art is None and mode in ("source", "ai"):
        art = generate_art(build_prompt(metadata.get("category"), headline, entity, script),
                           out_dir / "art.jpg", fallback_prompt=generic_prompt(metadata.get("category")))
        art_kind = "ai" if art else None

    # --- hero image: the titled 16:9 card (link previews + Telegram), over the picture if we have one ---
    hero = providers.image.generate(
        ImageRequest(headline=headline, subtitle=dek, entity=entity, width=1200, height=675,
                     background=str(art) if art else "",
                     # the card is shared in link previews and Telegram, away from the website's badge, so the
                     # label is painted onto the picture itself
                     label=photo.credit if photo else (commons.label if commons else (AI_LABEL if art else ""))),
        out_dir / "hero.jpg",
    )

    # --- website cover: text-free, the website draws the headline over it. The AI picture when we
    # have one; otherwise the deterministic gradient from the local renderer. The source is written
    # next to it so the exporter can credit an AI picture honestly. ---
    if art:
        cover = out_dir / "cover.jpg"
        shutil.copyfile(art, cover)
    else:
        cover = generate_cover(entity or headline, out_dir / "cover.jpg")
    (out_dir / COVER_SOURCE_FILE).write_text(art_kind or "gradient", encoding="utf-8")
    credit_file = out_dir / COVER_CREDIT_FILE
    if photo:
        credit_file.write_text(json.dumps({"credit": photo.credit, "outlet": photo.outlet, "url": photo.page_url}), encoding="utf-8")
    elif commons:
        credit_file.write_text(json.dumps({"credit": commons.credit, "outlet": "Wikimedia Commons", "url": commons.page_url,
                                           "subject": commons.subject, "licence": commons.licence}), encoding="utf-8")
    else:
        credit_file.unlink(missing_ok=True)       # a re-run must not leave a stale credit behind

    if not settings.generate_video:
        return _write_articles_only_package(
            topic_id, out_dir, article, script, metadata, hero, cover, providers.used, art, art_kind)

    hero_vertical = providers.image.generate(
        ImageRequest(
            headline=metadata.get("thumbnail_text") or headline,
            subtitle=entity, entity=entity, width=1080, height=1920,
        ),
        out_dir / "hero_vertical.jpg",
    )

    # --- voiceover, paced to the script's target length ---
    narration = narration_text(script)
    if not narration:
        raise ValueError("script has no narration to voice")
    target = float(script.get("estimated_seconds") or 0) or sum(
        float(b.get("target_seconds") or 0) for b in script.get("beats", [])
    ) or DEFAULT_TARGET_SECONDS

    voice_path = _synthesize_to_target(providers.tts, narration, out_dir / "voice.wav", target)
    providers.used["tts"] = providers.tts.name  # may have fallen back at call time

    # --- word timestamps -> kinetic subtitles ---
    words, duration = align(voice_path, narration)
    if not words:
        raise RuntimeError("alignment produced no words; cannot build subtitles")
    duration = duration or words[-1].end
    ass_path = write_ass(words, out_dir / "subs.ass")

    # --- b-roll, one clip per beat ---
    beats = script.get("beats", [])
    spans = beat_spans(words, [_word_count(b.get("narration", "")) for b in beats])
    clips: list[list[Path | None]] = []
    clip_no = 0
    for i, (beat, (start, end)) in enumerate(zip(beats, spans)):
        beat_clips: list[Path | None] = []
        span = max(end - start, 1.0)
        if providers.broll is None:
            beat_clips.append(None)
        else:
            query = beat.get("broll_query") or entity or headline
            query, replaced = safe_shot(metadata.get("category"), query)
            if replaced:
                log.warning("beat %s: risky shot for a %s story replaced with a symbolic one",
                            i, metadata.get("category"))
            cap = providers.broll.max_clip_seconds
            n = max(1, math.ceil(span / cap)) if cap else 1
            for j in range(n):
                q = query if j == 0 else f"{query}, {ANGLE_VARIANTS[(j - 1) % len(ANGLE_VARIANTS)]}"
                fetched = providers.broll.fetch(
                    BrollRequest(query=q, seconds=span / n, index=clip_no,
                                 context=footage_context(metadata.get("category"), headline),
                                 text=beat.get("on_screen_text") or "",
                                 label=normalize_category(metadata.get("category")) or metadata.get("category") or "",
                                 step=i + 1, steps=len(beats)), out_dir)
                clip_no += 1
                if fetched:
                    beat_clips.append(fetched.path)
                else:
                    log.info("no b-roll for beat %d clip %d (%r); using hero card", i, j, q)
                    beat_clips.append(None)
        clips.append(beat_clips)

    # --- render ---
    segments = plan_segments(hero_vertical, clips, spans, duration)
    video = render(segments, voice_path, ass_path, out_dir / "final.mp4", music=music)

    info = probe(video)
    v_stream = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    actual = float(info["format"]["duration"])

    package = {
        "topic_id": topic_id,
        "article": article,
        "script": script,
        "metadata": metadata,
        "assets": {
            "hero": hero.name,
            "hero_vertical": hero_vertical.name,
            "voiceover": voice_path.name,
            "subtitles": ass_path.name,
            "video": video.name,
        },
        "video_info": {
            "width": v_stream.get("width"),
            "height": v_stream.get("height"),
            "duration_seconds": round(actual, 2),
        },
        "providers_used": providers.used,
    }
    (out_dir / "package.json").write_text(json.dumps(package, indent=2), encoding="utf-8")

    return AssetResult(
        directory=out_dir, hero_image=hero, hero_vertical=hero_vertical,
        voiceover=voice_path, subtitles=ass_path, video=video,
        duration=actual, providers_used=providers.used, cover_image=cover,
    )


def prune_old_media(days: int | None = None) -> int:
    """Delete asset directories older than the retention window.

    Not optional on this machine — the disk is 91% full.
    """
    import shutil
    import time

    settings = get_settings()
    days = days if days is not None else settings.media_retention_days
    cutoff = time.time() - days * 86400
    removed = 0
    for child in settings.media_path.iterdir():
        if not child.is_dir() or not child.name.isdigit():
            continue
        if child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed
