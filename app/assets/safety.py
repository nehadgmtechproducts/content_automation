"""Visual-safety guard for generated footage.

The b-roll model produces photoreal video. For politics, war, protests and elections that
is a misinformation hazard: a generated "crowd at a protest" or "soldiers on a street" is
indistinguishable from news footage, and nothing in the pipeline would mark it as fake.
The drafting prompt already tells the model to avoid such shots, but a prompt is a request,
not a guarantee — so this is the deterministic backstop applied to every shot.

For categories flagged `sensitive_visuals`, any shot description that mentions people,
forces, crowds, weapons, flags, violence or real-event imagery is replaced with a symbolic,
people-free shot. Other categories pass through unchanged.
"""
from __future__ import annotations

import itertools
import re

from app.categories import get_category

_RISKY = re.compile(
    r"\b(soldier|soldiers|troops?|army|military|police|officers?|protest(?:er|ers|s)?|"
    r"crowd|crowds|rally|rallies|riot|riots|mob|demonstrat\w*|march(?:ers|ing)?|"
    r"president|prime minister|minister|senator|candidate|politician|leader|"
    r"flag|flags|weapon|weapons|gun|guns|rifle|missile|missiles|bomb|bombs|explosion|"
    r"blast|tank|tanks|drone strike|fighter jet|warplane|"
    r"blood|injur\w*|wounded|casualt\w*|corpse|dead|victim|victims|refugee|refugees|"
    r"face|faces|portrait|person|people|man|woman|men|women|child|children)\b",
    re.IGNORECASE,
)

# People-free, non-event, clearly symbolic. Rotated so beats don't all look alike.
_SYMBOLIC = (
    "Slow push-in on a wooden gavel resting on a dark desk, shallow depth of field",
    "Close-up of a sealed ballot box on a table with soft window light, camera glides sideways",
    "Overhead shot of a paper map on a table with a compass resting on it, slow drift",
    "Slow orbit around a brass set of scales on a marble surface, dramatic side lighting",
)
_rotation = itertools.count()


def is_risky_shot(category: str | None, query: str) -> bool:
    cat = get_category(category or "")
    return bool(cat and cat.sensitive_visuals and _RISKY.search(query or ""))


def safe_shot(category: str | None, query: str) -> tuple[str, bool]:
    """Return (shot, was_replaced). Safe or non-sensitive queries come back unchanged."""
    if is_risky_shot(category, query):
        return _SYMBOLIC[next(_rotation) % len(_SYMBOLIC)], True
    return query, False


def footage_context(category: str | None, headline: str) -> str:
    """The headline is appended to the footage prompt ("Footage for a news story titled…").
    For sensitive categories it would name real people and events right back into the
    generator, undoing safe_shot — so it is withheld there."""
    cat = get_category(category or "")
    return "" if cat and cat.sensitive_visuals else headline
