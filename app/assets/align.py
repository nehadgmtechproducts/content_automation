"""Word-level timestamps for the voiceover.

Run against the *generated audio* regardless of which TTS provider made it, so subtitle
timing is identical whether you're on free Piper or paid ElevenLabs. That uniformity is
the reason this is a separate stage instead of relying on a provider's own timings.
"""
from __future__ import annotations

import functools
import logging
import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from app.settings import providers_config

log = logging.getLogger(__name__)


@dataclass
class Word:
    text: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0.0)


def audio_duration(path: Path) -> float:
    """Duration in seconds. Reads the wav header directly, ffprobe otherwise."""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError, FileNotFoundError):
        pass
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True, timeout=60,
        )
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        return 0.0


@functools.lru_cache(maxsize=1)
def _whisper(model_size: str, compute_type: str):
    from faster_whisper import WhisperModel

    return WhisperModel(model_size, device="cpu", compute_type=compute_type)


def estimate_words(text: str, duration: float) -> list[Word]:
    """Proportional fallback timing when Whisper isn't available.

    Allocates each word a share of the duration weighted by its length, plus a small
    fixed cost per word for the gap between them. Not as tight as real alignment, but
    it keeps the pipeline runnable offline and the drift stays under a syllable.
    """
    tokens = [t for t in re.findall(r"[\w'’\-]+[.,!?;:]?", text) if t]
    if not tokens or duration <= 0:
        return []
    weights = [len(re.sub(r"\W", "", t)) + 2.5 for t in tokens]
    total = sum(weights)
    words, cursor = [], 0.0
    for token, weight in zip(tokens, weights):
        span = duration * (weight / total)
        words.append(Word(token, round(cursor, 3), round(cursor + span, 3)))
        cursor += span
    return words


def _alignment_is_sane(words: list[Word], script_text: str, duration: float) -> tuple[bool, str]:
    """Reject an alignment that clearly doesn't cover the narration.

    Bad alignment is worse than estimated timing: it silently produces a video whose
    subtitles are out of sync with the voice, which looks broken to a viewer but raises
    no error anywhere in the pipeline.
    """
    expected = len(re.findall(r"[\w'\u2019\-]+", script_text))
    if expected and len(words) < 0.6 * expected:
        return False, f"only {len(words)} of ~{expected} words recovered"
    if duration > 0:
        if words[0].start > 0.15 * duration:
            return False, f"first word at {words[0].start:.1f}s of {duration:.1f}s audio"
        if words[-1].end < 0.6 * duration:
            return False, f"last word at {words[-1].end:.1f}s of {duration:.1f}s audio"
    return True, ""


def align(audio_path: Path, script_text: str) -> tuple[list[Word], float]:
    """Return (words, duration). Falls back to estimation if Whisper can't run or
    produces an alignment that doesn't cover the audio."""
    duration = audio_duration(audio_path)
    cfg = providers_config().get("alignment", {})
    try:
        model = _whisper(cfg.get("model_size", "small"), cfg.get("compute_type", "int8"))
        # No initial_prompt: seeding Whisper with the very text it is transcribing makes
        # it treat that text as already emitted and resume partway through the audio.
        segments, _info = model.transcribe(
            str(audio_path), word_timestamps=True, vad_filter=False,
            condition_on_previous_text=False,
        )
        words: list[Word] = []
        for segment in segments:
            for w in (segment.words or []):
                token = w.word.strip()
                if token:
                    words.append(Word(token, round(w.start, 3), round(w.end, 3)))

        if words:
            ok, why = _alignment_is_sane(words, script_text, duration)
            if ok:
                log.info("aligned %d words with whisper", len(words))
                return words, duration or words[-1].end
            log.warning("whisper alignment rejected (%s); estimating instead", why)
        else:
            log.warning("whisper returned no words; estimating instead")
    except Exception as exc:  # noqa: BLE001 - model missing, no disk, etc.
        log.warning("whisper alignment unavailable (%s); estimating instead", exc)

    return estimate_words(script_text, duration), duration
