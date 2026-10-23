"""Story illustration from NVIDIA's hosted FLUX model — the website cover's picture.

One photo per story, written from the story's own first shot description (the script's
`broll_query`, already screened by app/assets/safety.py for sensitive categories). It is an
AI-generated illustration, not a photograph of the event, and the website says so (the
exporter records the source and the page shows a credit).

Never raises: any failure (no key, NVIDIA down, bad response) returns None and the caller falls
back to the local gradient cover, so a story is never held up by image generation.
"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

import httpx
from PIL import Image

from app.assets.safety import footage_context, safe_shot
from app.settings import get_settings

log = logging.getLogger(__name__)

IMAGE_URL = "https://ai.api.nvidia.com/v1/genai/{model}"
GEN_WIDTH, GEN_HEIGHT = 1344, 768           # a size the model accepts; ~16:9
CREDIT = "Illustration generated with AI"
_http_client: httpx.Client | None = None    # test hook: a client with a fake transport

_RULES = ("Strictly no text, no letters, no numbers, no logos, no brand marks, no watermarks, "
          "no recognisable real people and no clear faces.")


def build_prompt(category: str | None, headline: str, entity: str, script: dict) -> str:
    """The text sent to the model. Sensitive categories (Politics) swap risky shots for symbolic
    ones and withhold the headline, exactly as the video footage prompt does."""
    beats = (script or {}).get("beats") or []
    shot = (beats[0].get("broll_query") if beats else "") or entity or headline
    shot, _replaced = safe_shot(category, shot)
    context = footage_context(category, headline)
    parts = [f"Photorealistic editorial news photograph: {shot}."]
    if context:
        parts.append(f'It illustrates a news story titled "{context}".')
    parts.append(f"Natural cinematic lighting, wide 16:9 composition, shallow depth of field. {_RULES}")
    return " ".join(parts)


# Used when the model's content filter rejects the story-specific prompt (it tends to refuse brand
# and logo wording). Generic on purpose: nothing from the story, only the kind of subject.
_GENERIC_SCENES = {
    "Technology": "a dark modern data center aisle with glowing server racks and cool blue light",
    "Politics": "an empty stately government chamber with flags, soft daylight, wide shot",
    "Finance": "blurred trading screens with rising line charts in a dim financial office",
    "Entertainment": "an empty theatre stage with warm spotlights and red velvet seats",
    "Launches": "a sleek product on a minimalist studio pedestal with dramatic soft lighting",
    "Healthcare": "a bright modern laboratory bench with a microscope and glassware, soft light, no people",
}
_DEFAULT_SCENE = "an abstract modern newsroom with glowing screens and soft light"


def generic_prompt(category: str | None) -> str:
    scene = _GENERIC_SCENES.get(category or "", _DEFAULT_SCENE)
    return (f"Photorealistic editorial news photograph: {scene}. Natural cinematic lighting, wide 16:9 "
            f"composition, shallow depth of field. {_RULES}")


def fit_cover(image: Image.Image, width: int, height: int) -> Image.Image:
    """Scale to cover the target box, then centre-crop (no stretching)."""
    scale = max(width / image.width, height / image.height)
    resized = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    left, top = (resized.width - width) // 2, (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def generate_art(prompt: str, out_path: Path, width: int = 1600, height: int = 900,
                 fallback_prompt: str = "") -> Path | None:
    """Generate the illustration and save it as a `width`x`height` JPEG; None on any failure.

    `fallback_prompt` is tried once more if the first prompt is refused by the content filter."""
    settings = get_settings()
    if not settings.nvidia_api_key or not settings.ai_images:
        return None
    client = _http_client or httpx.Client(timeout=90)
    prompts = [prompt] + ([fallback_prompt] if fallback_prompt and fallback_prompt != prompt else [])
    last = ""
    for text in prompts:
        for _try in (1, 2):                  # a second try only helps for transient errors
            try:
                resp = client.post(
                    IMAGE_URL.format(model=settings.nvidia_image_model),
                    headers={"Authorization": f"Bearer {settings.nvidia_api_key}",
                             "Accept": "application/json", "NVCF-POLL-SECONDS": "45"},
                    json={"prompt": text, "width": GEN_WIDTH, "height": GEN_HEIGHT,
                          "seed": 0, "steps": 4},
                )
                if resp.status_code != 200:
                    last = f"HTTP {resp.status_code}"
                    continue
                artifact = (resp.json().get("artifacts") or [{}])[0]
                if artifact.get("finishReason") not in (None, "SUCCESS"):
                    last = f"finishReason={artifact.get('finishReason')}"   # e.g. CONTENT_FILTERED
                    break                    # refused: repeating the same prompt will not help
                raw = base64.b64decode(artifact["base64"])
                image = fit_cover(Image.open(io.BytesIO(raw)).convert("RGB"), width, height)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                image.save(out_path, quality=90)
                return out_path
            except Exception as exc:  # noqa: BLE001 - never block a story on its picture
                last = type(exc).__name__
    log.warning("AI illustration failed (%s); using the gradient cover", last)
    return None
