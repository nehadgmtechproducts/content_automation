"""The fixed content taxonomy — shared vocabulary between synthesis, safety rules, the
exporter and the website.

Why a closed list instead of free text: the model used to invent labels ("Technology /
Privacy", "Artificial Intelligence"), which fragments category pages and can't drive any
rule. A fixed list lets us (a) constrain the model's output to exactly these names,
(b) apply category-specific editorial and visual-safety rules, and (c) map 1:1 onto the
website's navigation.

The web project (`news_automation_web/src/core/categories.ts`) must mirror NAMES; the
exporter refuses a topic whose category can't be normalized into this list rather than
publishing it under a made-up heading.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Category:
    name: str
    slug: str
    covers: str                       # shown to the model so it can classify
    synonyms: tuple[str, ...] = ()    # legacy / near-miss labels that normalize to this one
    sensitive_visuals: bool = False   # True -> no photoreal generated footage (see safety.py)
    # People-free replacement shots for a sensitive category's risky shot descriptions. Empty means
    # "use the default (political) set" — see app/assets/safety.py.
    symbolic_shots: tuple[str, ...] = ()


CATEGORIES: tuple[Category, ...] = (
    Category(
        "Technology", "technology",
        "AI, software, internet platforms, chips, cybersecurity, privacy, startups and "
        "the technology industry.",
        ("tech", "ai", "artificial intelligence", "software", "cybersecurity", "privacy",
         "science", "internet", "semiconductors", "technology / privacy"),
    ),
    Category(
        "Politics", "politics",
        "Government, elections, legislation, courts, diplomacy, war and conflict, "
        "protests and public-policy news.",
        ("politics & world", "world", "world news", "war", "conflict", "election", "elections",
         "protest", "protests", "government", "policy", "geopolitics", "law"),
        sensitive_visuals=True,
    ),
    Category(
        "Finance", "finance",
        "Stock markets, companies' earnings, economy, central banks, currencies, "
        "commodities and crypto markets.",
        ("business", "business & finance", "markets", "stocks", "stock market", "economy",
         "money", "investing", "crypto"),
    ),
    Category(
        "Entertainment", "entertainment",
        "Movies, TV series and streaming, music, celebrities, awards, and sports "
        "(results, matches, transfers, tournaments).",
        ("sports", "sport", "movies", "film", "tv", "television", "music", "celebrity",
         "culture", "gaming", "games"),
    ),
    Category(
        "Launches", "launches",
        "New product launches and unveilings: cars, motorcycles and bikes, phones, "
        "gadgets, devices and consumer products.",
        ("new launches", "launch", "product launches", "cars", "automotive", "auto", "bikes",
         "motorcycles", "gadgets", "devices", "mobile", "smartphones", "ev", "evs"),
    ),
    Category(
        "Healthcare", "healthcare",
        "Medicine and public health: diseases and outbreaks, drugs and vaccines, medical "
        "research and clinical trials, regulators (FDA, WHO, NHS), hospitals and health "
        "systems, health insurance and policy, mental health, nutrition and fitness studies.",
        ("health", "health care", "medical", "medicine", "public health", "pharma",
         "pharmaceutical", "pharmaceuticals", "biotech", "biotechnology", "wellness", "vaccine",
         "vaccines", "disease", "diseases", "outbreak", "hospital", "hospitals", "nutrition",
         "fitness", "mental health", "clinical trial", "clinical trials", "drug", "drugs"),
        sensitive_visuals=True,
        symbolic_shots=(
            "Slow push-in on a stethoscope resting on a clean white desk, shallow depth of field",
            "Close-up of laboratory glassware and a microscope in soft window light, camera glides sideways",
            "Slow orbit around a translucent DNA double-helix model on a lab bench, cool side lighting",
            "Bright hospital corridor with a polished floor and soft ceiling lights, slow dolly forward",
        ),
    ),
)

NAMES: tuple[str, ...] = tuple(c.name for c in CATEGORIES)
CategoryName = Literal["Technology", "Politics", "Finance", "Entertainment", "Launches", "Healthcare"]

_BY_KEY: dict[str, Category] = {}
for _c in CATEGORIES:
    for _k in (_c.name, _c.slug, *_c.synonyms):
        _BY_KEY[_k.lower()] = _c


def normalize_category(raw: str | None) -> str | None:
    """Map any label onto the taxonomy, or None if it can't be placed.

    Tries the whole string, then each part of 'A / B' or 'A, B' (first match wins), so
    legacy values like 'Technology / Privacy' still resolve. Returns None rather than
    guessing: the caller decides whether that blocks publishing.
    """
    if not raw:
        return None
    text = raw.strip().lower()
    if text in _BY_KEY:
        return _BY_KEY[text].name
    for part in text.replace(",", "/").replace("&", "/").split("/"):
        key = part.strip()
        if key in _BY_KEY:
            return _BY_KEY[key].name
    return None


def get_category(name: str) -> Category | None:
    return _BY_KEY.get((name or "").lower())


def prompt_listing() -> str:
    """The category block injected into the drafting prompt."""
    return "\n".join(f"- {c.name}: {c.covers}" for c in CATEGORIES)
