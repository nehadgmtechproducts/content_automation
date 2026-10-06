"""Stage 4 orchestration — turn a Draft into a publishable asset bundle."""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from app.assets.align import align
from app.assets.render import Segment, beat_spans, plan_segments, probe, render
from app.assets.subtitles import write_ass
from app.providers.base import BrollRequest, ImageRequest, VoiceRequest, load_providers
from app.settings import get_settings

log = logging.getLogger(__name__)


@dataclass
class AssetResult:
    directory: Path
    hero_image: Path
    hero_vertical: Path
    voiceover: Path
    subtitles: Path
    video: Path
    duration: float
    providers_used: dict


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


def produce_assets(topic_id: int, article: dict, script: dict, metadata: dict,
                   entity: str = "", music: Path | None = None) -> AssetResult:
    settings = get_settings()
    out_dir = settings.media_path / str(topic_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    providers = load_providers()

    headline = article.get("title") or "Untitled"
    dek = article.get("dek", "")

    # --- hero images: 16:9 for the blog, 9:16 as the video's opening card ---
    hero = providers.image.generate(
        ImageRequest(headline=headline, subtitle=dek, entity=entity, width=1200, height=675),
        out_dir / "hero.jpg",
    )
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
            cap = providers.broll.max_clip_seconds
            n = max(1, math.ceil(span / cap)) if cap else 1
            for j in range(n):
                q = query if j == 0 else f"{query}, {ANGLE_VARIANTS[(j - 1) % len(ANGLE_VARIANTS)]}"
                fetched = providers.broll.fetch(
                    BrollRequest(query=q, seconds=span / n, index=clip_no, context=headline), out_dir)
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
        duration=actual, providers_used=providers.used,
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
