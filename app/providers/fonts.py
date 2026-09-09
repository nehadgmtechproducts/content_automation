"""Locate a usable TTF. The container ships DejaVu; macOS has its own set."""
from __future__ import annotations

from pathlib import Path

CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
]
CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _first_existing(paths: list[str]) -> str:
    for p in paths:
        if Path(p).exists():
            return p
    raise RuntimeError(
        "No usable TTF font found. Install fonts-dejavu-core (Linux) or run in the "
        "render container."
    )


def bold_font() -> str:
    return _first_existing(CANDIDATES_BOLD)


def regular_font() -> str:
    return _first_existing(CANDIDATES_REGULAR)


def subtitle_font_name() -> str:
    """Family name written into the ASS style.

    Deliberately NOT resolved from the local filesystem: subtitles are generated
    wherever the pipeline runs but burned inside the render container, so the name has
    to be one that container guarantees (it installs fonts-dejavu-core). Resolving the
    host's fonts here would emit "Arial" on macOS and silently font-substitute at render
    time. Override via providers.yaml -> subtitles.font_family.
    """
    from app.settings import providers_config

    return providers_config().get("subtitles", {}).get("font_family", "DejaVu Sans")
