"""FFmpeg composition of the vertical video.

Requires an FFmpeg built with libass — your Homebrew binary is not (no `ass`, no
`drawtext`, no freetype), which is exactly why the render worker is containerized.
`check_ffmpeg()` fails loudly rather than producing a silent subtitle-less video.

Design notes:
- Hard cuts between beats, not crossfades. xfade needs exact offset arithmetic across a
  variable input count and breaks noisily when a beat is short; hard cuts are also the
  correct editorial choice for 45-second vertical content.
- Beat visuals are timed from the *aligned word timestamps*, so the picture cuts on the
  narration rather than on a nominal script estimate.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.assets.align import Word

log = logging.getLogger(__name__)

WIDTH, HEIGHT, FPS = 1080, 1920, 30
HERO_SECONDS = 1.5
MIN_SEGMENT = 0.8


class RenderError(RuntimeError):
    pass


@dataclass
class Segment:
    path: Path
    is_video: bool
    duration: float


def _docker_prefix() -> list[str]:
    """docker run ... with the media tree mounted at its own absolute path."""
    from app.settings import get_settings

    settings = get_settings()
    media = str(settings.media_path)
    return [
        "docker", "run", "--rm", "--platform", "linux/arm64",
        "-v", f"{media}:{media}",
        settings.render_docker_image,
    ]


def _use_docker() -> bool:
    from app.settings import get_settings

    return get_settings().render_in_docker


def check_ffmpeg() -> None:
    """Verify the binary can actually burn subtitles before we spend time rendering."""
    if _use_docker():
        probe_cmd = _docker_prefix() + ["ffmpeg", "-hide_banner", "-filters"]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RenderError(
                f"render container '{get_render_image()}' is not usable: "
                f"{result.stderr.strip()[:300]}\n"
                f"Build it with: docker build -t {get_render_image()} "
                f"-f docker/ffmpeg.Dockerfile ."
            )
        out = result.stdout
    else:
        if not shutil.which("ffmpeg"):
            raise RenderError("ffmpeg not found on PATH")
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=60).stdout
    missing = [f for f in ("ass", "zoompan", "loudnorm") if f" {f} " not in out]
    if missing:
        raise RenderError(
            f"this ffmpeg lacks required filter(s): {', '.join(missing)}. "
            f"Subtitles need a libass build. Set RENDER_IN_DOCKER=1 and build the "
            f"render image:  docker build -t {get_render_image()} "
            f"-f docker/ffmpeg.Dockerfile ."
        )


def get_render_image() -> str:
    from app.settings import get_settings

    return get_settings().render_docker_image


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    return json.loads(out.stdout)


def beat_spans(words: list[Word], beat_word_counts: list[int]) -> list[tuple[float, float]]:
    """Map each script beat onto a real time span using the aligned words."""
    spans: list[tuple[float, float]] = []
    cursor = 0
    for count in beat_word_counts:
        if cursor >= len(words) or count <= 0:
            spans.append((words[-1].end if words else 0.0, words[-1].end if words else 0.0))
            continue
        chunk = words[cursor:cursor + count]
        spans.append((chunk[0].start, chunk[-1].end))
        cursor += count
    return spans


def plan_segments(hero: Path, beat_clips: list,
                  spans: list[tuple[float, float]], total_duration: float,
                  hero_seconds: float = HERO_SECONDS) -> list[Segment]:
    """Build the visual timeline. Guarantees the segments sum to the audio duration.

    Each entry of `beat_clips` is one clip (or None) or a LIST of clips for that beat; a
    beat with several clips splits its time evenly between them.
    """
    segments: list[Segment] = [Segment(hero, False, hero_seconds)]

    for clips, (start, end) in zip(beat_clips, spans):
        visual_start = max(start, hero_seconds)
        duration = end - visual_start
        if duration < MIN_SEGMENT:
            continue
        pieces = clips if isinstance(clips, list) and clips else [clips]
        each = float(duration) / len(pieces)
        for clip in pieces:
            source = clip if clip is not None else hero
            segments.append(Segment(source, source.suffix.lower() == ".mp4", each))

    if len(segments) == 1:
        segments.append(Segment(hero, False, max(total_duration - hero_seconds, MIN_SEGMENT)))

    # Absorb any rounding drift into the last segment so video length == audio length.
    drift = float(total_duration) - sum(s.duration for s in segments)
    segments[-1].duration = float(max(segments[-1].duration + drift, MIN_SEGMENT))
    return segments


def _video_filter(idx: int, seg: Segment) -> str:
    frames = max(int(round(seg.duration * FPS)), 1)
    if seg.is_video:
        return (
            # tpad first: a source shorter than its beat (generated clips cap at 10s, a beat
            # can run ~15s) would otherwise yield a SHORT segment and shove every later
            # caption out of sync with the voice. Cloning the last frame fills the gap.
            f"[{idx}:v]setpts=PTS-STARTPTS,"
            f"tpad=stop_mode=clone:stop_duration={seg.duration:.3f},"
            f"trim=start=0:duration={seg.duration:.3f},setpts=PTS-STARTPTS,"
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1,format=yuv420p[v{idx}]"
        )
    # Ken Burns on a still: upscale first so the zoom doesn't reveal interpolation jitter.
    return (
        f"[{idx}:v]scale={WIDTH * 2}:{HEIGHT * 2}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH * 2}:{HEIGHT * 2},"
        f"zoompan=z='min(zoom+0.0012,1.30)':d={frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={WIDTH}x{HEIGHT}:fps={FPS},"
        f"setsar=1,format=yuv420p[v{idx}]"
    )


def build_command(segments: list[Segment], audio: Path, ass: Path, out: Path,
                  music: Path | None = None) -> list[str]:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]

    for seg in segments:
        if seg.is_video:
            cmd += ["-i", str(seg.path)]
        else:
            # A single still + zoompan d=<frames> generates the whole segment; no -loop.
            cmd += ["-i", str(seg.path)]

    audio_idx = len(segments)
    cmd += ["-i", str(audio)]
    music_idx = None
    if music is not None:
        music_idx = audio_idx + 1
        cmd += ["-stream_loop", "-1", "-i", str(music)]

    filters = [_video_filter(i, seg) for i, seg in enumerate(segments)]
    concat_inputs = "".join(f"[v{i}]" for i in range(len(segments)))
    filters.append(f"{concat_inputs}concat=n={len(segments)}:v=1:a=0[vcat]")
    # Escape the subtitle path for the filtergraph parser (':' and '\' are special).
    escaped = str(ass).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    filters.append(f"[vcat]ass='{escaped}'[vout]")

    total = sum(s.duration for s in segments)
    if music_idx is not None:
        filters.append(f"[{audio_idx}:a]aresample=48000[voice]")
        filters.append(
            f"[{music_idx}:a]aresample=48000,atrim=duration={total:.3f},"
            f"volume=0.10,afade=t=out:st={max(total - 1.5, 0):.3f}:d=1.5[music]"
        )
        filters.append(
            "[voice][music]amix=inputs=2:duration=first:dropout_transition=0,"
            "loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
        )
    else:
        filters.append(
            f"[{audio_idx}:a]aresample=48000,loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
        )

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-r", str(FPS), "-g", str(FPS * 2),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        "-t", f"{total:.3f}",
        str(out),
    ]
    return cmd


def render(segments: list[Segment], audio: Path, ass: Path, out: Path,
           music: Path | None = None) -> Path:
    check_ffmpeg()
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_command(segments, audio, ass, out, music)
    if _use_docker():
        cmd = _docker_prefix() + cmd
    log.info("rendering %s (%d segments, %.1fs)%s", out.name, len(segments),
             sum(s.duration for s in segments), " [docker]" if _use_docker() else "")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise RenderError(f"ffmpeg failed:\n{result.stderr[-4000:]}")
    if not out.exists() or out.stat().st_size < 10_000:
        raise RenderError("ffmpeg produced no usable output")
    return out
