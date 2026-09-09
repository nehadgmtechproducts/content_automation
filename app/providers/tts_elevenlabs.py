"""ElevenLabs voiceover. Drop-in for PiperTTS once ELEVENLABS_API_KEY is set."""
from __future__ import annotations

import subprocess
from pathlib import Path

import httpx

from app.providers.base import TTSProvider, VoiceRequest
from app.settings import get_settings


class ElevenLabsTTS(TTSProvider):
    name = "elevenlabs"

    def __init__(self, cfg: dict):
        self.voice_id = cfg.get("voice_id", "21m00Tcm4TlvDq8ikWAM")
        self.model_id = cfg.get("model_id", "eleven_turbo_v2_5")

    def synthesize(self, req: VoiceRequest) -> Path:
        key = get_settings().elevenlabs_api_key
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
        resp = httpx.post(
            url,
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            json={
                "text": req.text,
                "model_id": self.model_id,
                "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
            },
            timeout=180,
        )
        resp.raise_for_status()

        req.out_path.parent.mkdir(parents=True, exist_ok=True)
        mp3 = req.out_path.with_suffix(".mp3")
        mp3.write_bytes(resp.content)
        # Normalize to the same wav shape Piper emits so downstream stages don't branch.
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp3), "-ar", "24000", "-ac", "1", str(req.out_path)],
            check=True, capture_output=True,
        )
        mp3.unlink(missing_ok=True)
        return req.out_path
