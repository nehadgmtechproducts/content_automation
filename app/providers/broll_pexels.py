"""B-roll from Pexels. Free key, instant signup — https://www.pexels.com/api/"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.providers.base import BrollClip, BrollProvider, BrollRequest
from app.settings import get_settings

log = logging.getLogger(__name__)
VIDEO_SEARCH = "https://api.pexels.com/videos/search"
PHOTO_SEARCH = "https://api.pexels.com/v1/search"


class PexelsBroll(BrollProvider):
    name = "pexels"

    def __init__(self, cfg: dict):
        self.orientation = cfg.get("orientation", "portrait")

    def _headers(self) -> dict:
        return {"Authorization": get_settings().pexels_api_key}

    def _download(self, url: str, target: Path) -> Path | None:
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
                r.raise_for_status()
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
            return target
        except httpx.HTTPError as exc:
            log.warning("pexels download failed: %s", exc)
            return None

    def fetch(self, req: BrollRequest, out_dir: Path) -> BrollClip | None:
        try:
            resp = httpx.get(
                VIDEO_SEARCH,
                params={"query": req.query, "orientation": self.orientation,
                        "per_page": 5, "size": "medium"},
                headers=self._headers(), timeout=45,
            )
            resp.raise_for_status()
            videos = resp.json().get("videos", [])
        except httpx.HTTPError as exc:
            log.warning("pexels video search failed for %r: %s", req.query, exc)
            videos = []

        for video in videos:
            if video.get("duration", 0) < req.seconds:
                continue
            files = sorted(
                (f for f in video.get("video_files", []) if f.get("width")),
                key=lambda f: abs(f["width"] - 1080),
            )
            if not files:
                continue
            target = out_dir / f"broll_{req.index}.mp4"
            if self._download(files[0]["link"], target):
                return BrollClip(target, True, req.query,
                                 attribution=video.get("user", {}).get("name", "Pexels"))

        # No usable clip — fall back to a still, which the renderer Ken Burns-es.
        try:
            resp = httpx.get(
                PHOTO_SEARCH,
                params={"query": req.query, "orientation": self.orientation, "per_page": 3},
                headers=self._headers(), timeout=45,
            )
            resp.raise_for_status()
            photos = resp.json().get("photos", [])
        except httpx.HTTPError:
            return None

        for photo in photos:
            link = photo.get("src", {}).get("large2x") or photo.get("src", {}).get("large")
            if not link:
                continue
            target = out_dir / f"broll_{req.index}.jpg"
            if self._download(link, target):
                return BrollClip(target, False, req.query,
                                 attribution=photo.get("photographer", "Pexels"))
        return None
