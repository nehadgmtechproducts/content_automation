"""Flux Schnell via Replicate. Drop-in for PillowEditorialCard once you set a token."""
from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from app.providers.base import ImageProvider, ImageRequest
from app.settings import get_settings

log = logging.getLogger(__name__)
PREDICTIONS = "https://api.replicate.com/v1/predictions"


class ReplicateFlux(ImageProvider):
    name = "replicate_flux"

    def __init__(self, cfg: dict):
        self.model = cfg.get("model", "black-forest-labs/flux-schnell")
        self.aspect_ratio = cfg.get("aspect_ratio", "9:16")

    def _prompt(self, req: ImageRequest) -> str:
        if req.prompt:
            return req.prompt
        # Editorial illustration, deliberately non-photoreal: a photoreal render of a real
        # news event is a fabricated photograph, which is not something to publish.
        return (
            f"Editorial magazine illustration, abstract conceptual, about: {req.headline}. "
            f"Bold geometric shapes, limited duotone palette, dramatic lighting, "
            f"no text, no logos, no recognizable faces, high detail, print quality."
        )

    def generate(self, req: ImageRequest, out_path: Path) -> Path:
        token = get_settings().replicate_api_token
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {
            "input": {
                "prompt": self._prompt(req),
                "aspect_ratio": self.aspect_ratio,
                "output_format": "jpg",
                "num_outputs": 1,
            }
        }
        resp = httpx.post(
            f"https://api.replicate.com/v1/models/{self.model}/predictions",
            json=body, headers=headers, timeout=60,
        )
        resp.raise_for_status()
        prediction = resp.json()

        deadline = time.time() + 120
        while prediction.get("status") not in ("succeeded", "failed", "canceled"):
            if time.time() > deadline:
                raise TimeoutError("replicate prediction timed out")
            time.sleep(1.5)
            prediction = httpx.get(
                f"{PREDICTIONS}/{prediction['id']}", headers=headers, timeout=30
            ).json()

        if prediction.get("status") != "succeeded":
            raise RuntimeError(f"replicate failed: {prediction.get('error')}")

        output = prediction["output"]
        url = output[0] if isinstance(output, list) else output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(httpx.get(url, timeout=60).content)
        return out_path
