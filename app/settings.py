"""Runtime configuration. Env vars come from .env; YAML holds the tunables."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- credentials ---
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    # fal.ai — one key for both Stage 3 scripting (OpenAI-compatible LLM router) and
    # Stage 4 video (Gemini Omni). Read from the FAL_KEY env var.
    fal_key: str = ""
    # NVIDIA hosted API (build.nvidia.com): an `nvapi-...` key. Used when SYNTHESIS_PROVIDER=nvidia.
    nvidia_api_key: str = ""
    firecrawl_api_key: str = ""
    pexels_api_key: str = ""
    elevenlabs_api_key: str = ""
    replicate_api_token: str = ""

    # --- infrastructure ---
    database_url: str = "postgresql+psycopg://engine:engine@localhost:5432/content_engine"
    redis_url: str = "redis://localhost:6379/0"
    media_root: Path = ROOT / "media"
    media_retention_days: int = 14

    # --- models ---
    # Which provider Stage 3 actually calls. "anthropic" needs anthropic_api_key +
    # synthesis_model; "openai" needs openai_api_key + openai_model. Switching is a
    # config change only — app/pipeline.py branches on this, nothing else does.
    # "anthropic" | "openai" | "fal" | "nvidia" — app/pipeline.py branches on this.
    synthesis_provider: str = "fal"
    synthesis_model: str = "claude-opus-5"
    openai_model: str = "gpt-4o"
    fal_llm_model: str = "openai/gpt-6.1-sol"
    nvidia_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    nvidia_thinking: bool = False
    # AI-generated cover illustration (FLUX on NVIDIA's API); falls back to the gradient on failure.
    # Where a story's picture comes from. "source": a photo from one of the story's own sources (credited, linked,
    # agency/stock pictures screened out, never on a sexual-offence/child story), falling back to the AI
    # illustration, then the gradient. "ai": AI illustration only. "gradient": neither.
    cover_image_mode: str = "source"
    # Commons photos under a share-alike licence (CC BY-SA) are off: our share card is an adaptation of the photo, and
    # share-alike would then bind it. Turn on only if your lawyer is happy with that.
    commons_allow_sharealike: bool = False
    ai_images: bool = True
    nvidia_image_model: str = "black-forest-labs/flux.2-klein-4b"

    # --- stage 5: Telegram approval (optional — blank means "not configured") ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- stage 6: web export (path to the news_automation_web checkout) ---
    web_project_dir: str = ""

    # --- behaviour ---
    fixture_mode: bool = False
    # The host ffmpeg here has no libass, so rendering is delegated to a container.
    # The media directory is mounted at the SAME absolute path inside it, which means
    # no path rewriting and the generated command runs unchanged.
    render_in_docker: bool = False
    # Articles-only mode: when False (the default) Stage 4 makes just the article's images
    # (titled hero card + text-free cover) — no voiceover, subtitles, b-roll or MP4.
    generate_video: bool = False
    render_docker_image: str = "content-engine-ffmpeg"

    @property
    def media_path(self) -> Path:
        p = Path(self.media_root)
        return p if p.is_absolute() else (ROOT / p).resolve()


def _load_yaml(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name) as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def feeds_config() -> dict[str, Any]:
    return _load_yaml("feeds.yaml")


@lru_cache(maxsize=1)
def providers_config() -> dict[str, Any]:
    return _load_yaml("providers.yaml")


@lru_cache(maxsize=1)
def detect_config() -> dict[str, Any]:
    return _load_yaml("detect.yaml")
