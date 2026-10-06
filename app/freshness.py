"""Guards against spending a scrape, a synthesis call, or a render on a topic that
has aged past being worth calling "breaking."

Under normal automatic operation this should almost never trigger — Celery chains
scrape -> synthesis -> assets within seconds of a topic firing (see tasks.py). It exists
for exactly what actually happened once already in this project: a topic left sitting
for days (an API key not being configured yet, a paused worker, a manual CLI run
against something detected a week ago) getting processed as if it were still current.

Deliberately does NOT gate Stage 5 (Telegram approval) — once assets exist and a topic
is pending_approval, whether a delayed human review is still worth approving is a
judgment call for the reviewer, not something to auto-refuse.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Protocol, TypeVar


@dataclass
class FreshnessCheck:
    fresh: bool
    age_hours: float
    reason: str


def check_freshness(detected_at: datetime, max_age_hours: float,
                    now: datetime | None = None) -> FreshnessCheck:
    """Pure function: is a topic detected at `detected_at` still worth acting on?"""
    now = now or datetime.now(timezone.utc)
    if detected_at.tzinfo is None:
        detected_at = detected_at.replace(tzinfo=timezone.utc)

    age_hours = (now - detected_at).total_seconds() / 3600.0

    if age_hours < 0:
        # A clock skew or a backfilled row — don't refuse on a negative age, but don't
        # pretend it's normal either.
        return FreshnessCheck(True, round(age_hours, 1),
                              f"detected_at is in the future ({age_hours:.1f}h) — allowing")

    if age_hours > max_age_hours:
        return FreshnessCheck(
            False, round(age_hours, 1),
            f"stale: topic is {age_hours:.1f}h old, over the {max_age_hours:.0f}h "
            f"freshness limit — refusing to spend a scrape/synthesis/render on it"
        )

    return FreshnessCheck(True, round(age_hours, 1),
                          f"{age_hours:.1f}h old, within the {max_age_hours:.0f}h limit")


class _Timed(Protocol):
    published_at: datetime | None
    fetched_at: datetime


T = TypeVar("T", bound=_Timed)


def event_time(item: _Timed) -> datetime:
    """When the outlet published the item — falling back to when we first saw it only
    if the feed gave no publish date. Ingest time alone is the wrong clock: a 3-day-old
    article we happened to fetch today would look brand new."""
    return item.published_at or item.fetched_at


def split_fresh(items: Iterable[T], max_age_hours: float,
                now: datetime | None = None) -> tuple[list[T], list[T]]:
    """Partition items into (fresh, stale) by PUBLISH time, for detection.

    Stale items must not feed the velocity trigger: three outlets all covering a story
    from three days ago is a recap, not breaking news.
    """
    fresh: list[T] = []
    stale: list[T] = []
    for item in items:
        ok = check_freshness(event_time(item), max_age_hours, now).fresh
        (fresh if ok else stale).append(item)
    return fresh, stale
