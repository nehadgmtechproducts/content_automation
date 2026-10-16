"""Circuit breaker for automatic production.

Why it exists: the scheduler turns every fired story into a topic and immediately tries to
write and render it. If the LLM account is out of balance, that fails for EVERY story —
and each failure still creates a topic (counting toward the daily cap) and suppresses that
story for 24 hours. Left alone, a locked account silently eats a day's worth of real news
without producing anything.

So a billing failure writes a small marker file; while it exists the scheduler keeps
ingesting (free) but does not fire topics. Recovery is automatic where possible: with the
fal provider, each poll sends one 5-token probe and clears the marker as soon as it works.
Manual runs (`produce --topic N`) ignore the marker.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.settings import get_settings

log = logging.getLogger(__name__)
PAUSE_FILE = ".production_paused"
FALLBACK_EXPIRY = timedelta(minutes=60)   # providers with no probe just retry after this


def _path():
    return get_settings().media_path / PAUSE_FILE


def status() -> dict | None:
    try:
        return json.loads(_path().read_text())
    except (FileNotFoundError, ValueError):
        return None


def pause(reason: str) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"reason": reason[:500], "since": datetime.now(timezone.utc).isoformat()}))
    log.warning("automatic production PAUSED: %s", reason)


def resume() -> bool:
    try:
        _path().unlink()
        log.info("automatic production resumed")
        return True
    except FileNotFoundError:
        return False


def allowed(probe: Callable[[], bool] | None = None, now: datetime | None = None) -> bool:
    """True if the scheduler may fire topics now. Clears the marker when it can recover."""
    st = status()
    if st is None:
        return True
    if probe is not None:
        if probe():
            resume()
            return True
        return False
    since = datetime.fromisoformat(st["since"])
    if (now or datetime.now(timezone.utc)) - since >= FALLBACK_EXPIRY:
        resume()
        return True
    return False
