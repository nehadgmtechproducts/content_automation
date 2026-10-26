"""Provider adapter contracts.

Every external capability has a free local default and an optional paid implementation.
Which one runs is chosen in config/providers.yaml — no code change to swap.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ImageRequest:
    headline: str
    subtitle: str = ""
    entity: str = ""
    prompt: str = ""
    width: int = 1200
    height: int = 675
    background: str = ""   # optional picture to put the title over (path)
    label: str = ""        # a small label drawn ON the picture, e.g. "AI-generated illustration"


@dataclass
class VoiceRequest:
    text: str
    out_path: Path
    # The script is written to a 45s target, but actual length is set by how fast the
    # voice speaks. Providers that can control pace use these to hit the target.
    target_seconds: float | None = None
    speed_scale: float = 1.0        # >1 = faster speech

    @property
    def word_count(self) -> int:
        import re

        return len(re.findall(r"[\w'\u2019\-]+", self.text))


@dataclass
class BrollRequest:
    query: str
    seconds: float
    index: int
    context: str = ""        # the story headline, so footage can show the actual subject
    # Used only by the text-card provider (typographic videos with no generated footage):
    text: str = ""           # the beat's on-screen line
    label: str = ""          # category name shown on the card
    step: int = 0            # 1-based beat number
    steps: int = 0           # total beats


@dataclass
class BrollClip:
    path: Path
    is_video: bool
    query: str
    attribution: str = ""


class ImageProvider(ABC):
    name: str = "image"

    @abstractmethod
    def generate(self, req: ImageRequest, out_path: Path) -> Path: ...


class TTSProvider(ABC):
    name: str = "tts"

    @abstractmethod
    def synthesize(self, req: VoiceRequest) -> Path: ...


class BrollProvider(ABC):
    name: str = "broll"
    # Longest single clip this provider can return. A beat longer than this gets several
    # clips instead of one clip frozen on its last frame. None = no cap (stock footage is
    # picked to be at least as long as the beat).
    max_clip_seconds: float | None = None

    @abstractmethod
    def fetch(self, req: BrollRequest, out_dir: Path) -> BrollClip | None: ...


@dataclass
class ProviderSet:
    image: ImageProvider
    tts: TTSProvider
    broll: BrollProvider | None = None
    used: dict = field(default_factory=dict)


def load_providers() -> ProviderSet:
    """Instantiate the adapters named in config/providers.yaml.

    Falls back to the free local adapter whenever a paid one is selected but its key is
    missing, so a half-configured .env degrades instead of crashing.
    """
    import logging

    from app.settings import get_settings, providers_config

    log = logging.getLogger(__name__)
    cfg = providers_config()
    settings = get_settings()
    used: dict[str, str] = {}

    # --- image ---
    from app.providers.image_pillow import PillowEditorialCard

    image_choice = cfg["image"].get("active", "pillow_card")
    if image_choice == "replicate_flux" and settings.replicate_api_token:
        from app.providers.image_replicate import ReplicateFlux

        image: ImageProvider = ReplicateFlux(cfg["image"]["replicate_flux"])
    else:
        if image_choice == "replicate_flux":
            log.warning("replicate_flux selected but REPLICATE_API_TOKEN unset — using pillow_card")
        image = PillowEditorialCard()
    used["image"] = image.name

    # --- tts ---
    tts_choice = cfg["tts"].get("active", "piper")
    if tts_choice == "elevenlabs" and settings.elevenlabs_api_key:
        from app.providers.tts_elevenlabs import ElevenLabsTTS

        tts: TTSProvider = ElevenLabsTTS(cfg["tts"]["elevenlabs"])
    else:
        if tts_choice == "elevenlabs":
            log.warning("elevenlabs selected but ELEVENLABS_API_KEY unset — using piper")
        from app.providers.tts_piper import PiperTTS

        tts = PiperTTS(cfg["tts"]["piper"])
    used["tts"] = tts.name

    # --- b-roll ---
    broll: BrollProvider | None = None
    broll_choice = cfg["broll"].get("active")
    if broll_choice == "fal" and settings.fal_key:
        from app.providers.broll_fal import FalBroll

        broll = FalBroll(cfg["broll"].get("fal", {}))
        used["broll"] = broll.name
    elif broll_choice == "text_cards":
        from app.providers.broll_text import TextCardBroll

        broll = TextCardBroll(cfg["broll"].get("text_cards") or {})
        used["broll"] = broll.name
    elif broll_choice == "pexels" and settings.pexels_api_key:
        from app.providers.broll_pexels import PexelsBroll

        broll = PexelsBroll(cfg["broll"]["pexels"])
        used["broll"] = broll.name
    else:
        if broll_choice in ("fal", "pexels"):
            log.warning("%s selected for B-roll but its key is unset — using stills_only",
                        broll_choice)
        used["broll"] = "stills_only"

    return ProviderSet(image=image, tts=tts, broll=broll, used=used)
