"""Stage 6 contracts — declared now, implemented in milestone 2.

Laid down so the stage-4 output shape is already the shape publishers need, and adding
Ghost/X/YouTube later touches nothing upstream.

Note on lead times: TikTok's Content Posting API requires an audited app (until audited,
uploads land as private drafts) and Instagram Reels requires a Business account plus Meta
app review. Both take weeks — start those applications independently of this code.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PublishPayload:
    topic_id: int
    article: dict
    script: dict
    metadata: dict
    hero_image: Path
    video: Path
    canonical_url: str = ""


@dataclass
class PublishResult:
    platform: str
    ok: bool
    remote_id: str = ""
    url: str = ""
    error: str = ""


class Publisher(ABC):
    platform: str

    @abstractmethod
    def publish(self, payload: PublishPayload) -> PublishResult:
        """Publish and return the remote id/url. Must be idempotent per topic_id."""
        raise NotImplementedError
