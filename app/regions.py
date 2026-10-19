"""The fixed list of website editions (regions) — same idea as app/categories.py.

Every story carries exactly one region, chosen by the drafting step from this closed list so
the website's Edition bar (Global / Asia / Europe / Americas / Middle East / Africa) can filter
on it. The web project (`news_automation_web/src/core/regions.ts`) must mirror NAMES.

Meaning of the values:
  * Global      — matters worldwide or isn't tied to a place (most product launches, AI and tech
                  releases, global markets, multi-region events).
  * the others  — where the story happens or whom it mainly affects.
A regional edition on the website shows its own stories PLUS the Global ones, so it is never empty
and never hides worldwide news; the Global edition shows everything.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Region:
    name: str
    slug: str
    covers: str
    synonyms: tuple[str, ...] = ()


REGIONS: tuple[Region, ...] = (
    Region(
        "Global", "global",
        "Matters worldwide or is not tied to a place: most product launches, AI and technology "
        "releases, global markets, and events spanning several regions.",
        ("worldwide", "international", "world", "multi-region"),
    ),
    Region(
        "Asia", "asia",
        "Asia and the Pacific: China, India, Japan, Korea, Southeast Asia, Pakistan, "
        "Australia and New Zealand.",
        ("asia-pacific", "apac", "china", "india", "japan", "south asia", "east asia",
         "southeast asia", "oceania", "australia"),
    ),
    Region(
        "Europe", "europe",
        "Europe: the EU, the UK, Russia and Ukraine.",
        ("eu", "uk", "britain", "united kingdom", "russia", "ukraine", "european union"),
    ),
    Region(
        "Americas", "americas",
        "North, Central and South America and the Caribbean: the US, Canada and Latin America.",
        ("us", "usa", "united states", "north america", "south america", "latin america",
         "canada", "america"),
    ),
    Region(
        "Middle East", "middle-east",
        "The Middle East: Israel and the Palestinian territories, Iran, Iraq, Syria, Lebanon, "
        "Jordan and the Gulf states.",
        ("mena", "gulf", "israel", "iran", "middle-east", "mideast"),
    ),
    Region(
        "Africa", "africa",
        "The African continent.",
        ("sub-saharan africa", "north africa"),
    ),
)

NAMES: tuple[str, ...] = tuple(r.name for r in REGIONS)
RegionName = Literal["Global", "Asia", "Europe", "Americas", "Middle East", "Africa"]
DEFAULT_REGION = "Global"

_BY_KEY: dict[str, Region] = {}
for _r in REGIONS:
    for _k in (_r.name, _r.slug, *_r.synonyms):
        _BY_KEY[_k.lower()] = _r


def normalize_region(raw: str | None) -> str | None:
    """Map a label onto the list, or None if it can't be placed (never guesses)."""
    if not raw:
        return None
    hit = _BY_KEY.get(raw.strip().lower())
    return hit.name if hit else None


def prompt_listing() -> str:
    return "\n".join(f"- {r.name}: {r.covers}" for r in REGIONS)
