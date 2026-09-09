"""Local neural TTS via Piper — the zero-key default voiceover.

Piper voice models aren't bundled with the wheel, so the first run downloads one
(~60MB) from the Rhasspy voice repository into ./models/piper.

On macOS, if Piper is unavailable for any reason, this falls back to the built-in
`say` binary. That keeps the pipeline runnable on your laptop today; the container
always has Piper proper.
"""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import wave
from pathlib import Path

import httpx

from app.providers.base import TTSProvider, VoiceRequest
from app.settings import ROOT

log = logging.getLogger(__name__)

VOICE_REPO = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
VOICE_PATHS = {
    "en_US-lessac-medium": "en/en_US/lessac/medium/en_US-lessac-medium.onnx",
    "en_US-amy-medium": "en/en_US/amy/medium/en_US-amy-medium.onnx",
    "en_GB-alba-medium": "en/en_GB/alba/medium/en_GB-alba-medium.onnx",
}
MODEL_DIR = ROOT / "models" / "piper"


def _download_voice(voice: str) -> Path | None:
    rel = VOICE_PATHS.get(voice)
    if rel is None:
        log.warning("unknown piper voice %s", voice)
        return None
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    onnx = MODEL_DIR / f"{voice}.onnx"
    cfg = MODEL_DIR / f"{voice}.onnx.json"
    try:
        for target, url in ((onnx, f"{VOICE_REPO}/{rel}"), (cfg, f"{VOICE_REPO}/{rel}.json")):
            if target.exists():
                continue
            log.info("downloading piper asset %s", target.name)
            with httpx.stream("GET", url, follow_redirects=True, timeout=180) as r:
                r.raise_for_status()
                with open(target, "wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
    except httpx.HTTPError as exc:
        log.warning("piper voice download failed: %s", exc)
        return None
    return onnx if onnx.exists() else None


class PiperTTS(TTSProvider):
    name = "piper"

    def __init__(self, cfg: dict):
        self.voice = cfg.get("voice", "en_US-lessac-medium")
        self.length_scale = float(cfg.get("length_scale", 1.0))

    def _synthesize_binary(self, req: VoiceRequest) -> bool:
        """Preferred path: the standalone `piper` binary.

        Used in preference to the pip package because piper-phonemize ships no macOS
        arm64 wheel, so `pip install piper-tts` cannot work on this machine at all.
        The render image installs the binary from Piper's GitHub releases.
        """
        binary = shutil.which("piper")
        if not binary:
            return False
        model = MODEL_DIR / f"{self.voice}.onnx"
        if not model.exists():
            model = _download_voice(self.voice)
        if model is None:
            return False
        try:
            subprocess.run(
                [binary, "--model", str(model), "--output_file", str(req.out_path),
                 # length_scale stretches time, so a faster target means a smaller value.
                 "--length_scale", f"{self.length_scale / max(req.speed_scale, 0.1):.3f}"],
                input=req.text, text=True, capture_output=True, check=True, timeout=600,
            )
            return req.out_path.exists() and req.out_path.stat().st_size > 1024
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            log.warning("piper binary failed: %s", exc)
            return False

    def _synthesize_package(self, req: VoiceRequest) -> bool:
        """Fallback path for environments where the pip package did install."""
        try:
            from piper.voice import PiperVoice
        except ImportError:
            return False

        model = MODEL_DIR / f"{self.voice}.onnx"
        if not model.exists():
            model = _download_voice(self.voice)
        if model is None:
            return False

        try:
            voice = PiperVoice.load(str(model))
            with wave.open(str(req.out_path), "wb") as wav:
                # The API gained synthesize_wav in later releases; support both.
                if hasattr(voice, "synthesize_wav"):
                    voice.synthesize_wav(req.text, wav)
                else:
                    voice.synthesize(req.text, wav)
            return req.out_path.exists() and req.out_path.stat().st_size > 1024
        except Exception as exc:  # noqa: BLE001 - any failure means fall back
            log.warning("piper package synthesis failed: %s", exc)
            return False

    def _synthesize_say(self, req: VoiceRequest) -> bool:
        """macOS fallback. Real speech, no download, good enough for local iteration."""
        if platform.system() != "Darwin" or not shutil.which("say"):
            return False
        aiff = req.out_path.with_suffix(".aiff")
        # Derive the rate from the target length rather than hardcoding it: at a fixed
        # 165 wpm a 113-word script lands ~10s short of the 45s the script was written for.
        if req.target_seconds and req.word_count:
            wpm = req.word_count / (req.target_seconds / 60.0) * req.speed_scale
        else:
            wpm = 165 * req.speed_scale
        wpm = int(max(120, min(280, wpm)))
        try:
            subprocess.run(
                ["say", "-v", "Samantha", "-r", str(wpm), "-o", str(aiff), req.text],
                check=True, capture_output=True, timeout=300,
            )
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(aiff), "-ar", "24000", "-ac", "1", str(req.out_path)],
                check=True, capture_output=True, timeout=120,
            )
            return req.out_path.exists()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            log.warning("macOS say fallback failed: %s", exc)
            return False
        finally:
            aiff.unlink(missing_ok=True)

    def synthesize(self, req: VoiceRequest) -> Path:
        req.out_path.parent.mkdir(parents=True, exist_ok=True)
        if self._synthesize_binary(req) or self._synthesize_package(req):
            return req.out_path
        log.info("piper unavailable; falling back to macOS `say` for voiceover")
        if self._synthesize_say(req):
            self.name = "macos_say"
            return req.out_path
        raise RuntimeError(
            "No TTS backend available. Install piper-tts and let it download a voice, "
            "or run inside the render container."
        )
