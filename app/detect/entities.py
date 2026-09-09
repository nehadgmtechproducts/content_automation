"""Entity extraction for headlines.

spaCy NER + a curated alias table. Deliberately NOT an LLM call: this runs over every
polled headline (thousands/day) and an LLM here would be pure waste with no quality gain
on proper nouns.
"""
from __future__ import annotations

import functools
import re

# Canonical name -> aliases that should collapse onto it.
ALIASES: dict[str, set[str]] = {
    "OpenAI": {"open ai", "openai inc", "openai llc"},
    "Anthropic": {"anthropic pbc"},
    "Google DeepMind": {"deepmind", "google deep mind"},
    "Google": {"alphabet", "alphabet inc", "google llc"},
    "Meta": {"facebook", "meta platforms", "meta ai"},
    "Microsoft": {"msft", "microsoft corp"},
    "Nvidia": {"nvda", "nvidia corp"},
    "Apple": {"aapl", "apple inc"},
    "Amazon": {"aws", "amazon web services", "amzn"},
    "Tesla": {"tsla"},
    "xAI": {"x.ai", "x ai"},
    "Mistral AI": {"mistral"},
    "Hugging Face": {"huggingface"},
    "Stability AI": {"stability"},
    "TSMC": {"taiwan semiconductor", "taiwan semiconductor manufacturing"},
    "AMD": {"advanced micro devices"},
    "Intel": {"intc"},
    "ChatGPT": {"chat gpt"},
    "Claude": set(),
    "Gemini": set(),
    "Llama": {"llama 3", "llama 4"},
    "Sam Altman": {"altman"},
    "Elon Musk": {"musk"},
    "Sundar Pichai": {"pichai"},
    "Satya Nadella": {"nadella"},
    "Jensen Huang": {"huang"},
    "Dario Amodei": {"amodei"},
}

_ALIAS_LOOKUP: dict[str, str] = {}
for canonical, aliases in ALIASES.items():
    _ALIAS_LOOKUP[canonical.lower()] = canonical
    for alias in aliases:
        _ALIAS_LOOKUP[alias.lower()] = canonical

# Entities that carry no signal — they appear in everything.
STOP_ENTITIES = {
    "ai", "artificial intelligence", "the", "us", "u.s.", "usa", "eu", "uk", "china",
    "reuters", "bloomberg", "techcrunch", "the verge", "cnbc", "wired", "engadget",
    "today", "this week", "reddit", "twitter", "x",
}

_KEEP_LABELS = {"ORG", "PERSON", "PRODUCT", "GPE", "EVENT", "WORK_OF_ART"}
_PUNCT = re.compile(r"^[\W_]+|[\W_]+$")


def canonicalize_entity(text: str) -> str | None:
    """Map a surface form to its canonical entity name, or None if it's noise."""
    cleaned = _PUNCT.sub("", (text or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"(?i)\b(inc|corp|corporation|ltd|llc|plc|co)\.?$", "", cleaned).strip()
    cleaned = re.sub(r"(?i)^the\s+", "", cleaned).strip()
    if len(cleaned) < 2:
        return None
    lowered = cleaned.lower()
    if lowered in STOP_ENTITIES:
        return None
    if lowered in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[lowered]
    # Reject all-lowercase multi-word fragments — NER noise, not proper nouns.
    if cleaned.islower() and " " in cleaned:
        return None
    return cleaned


@functools.lru_cache(maxsize=1)
def _nlp():
    import spacy

    try:
        return spacy.load("en_core_web_sm", disable=["lemmatizer", "textcat"])
    except OSError as exc:  # pragma: no cover - environment problem, not logic
        raise RuntimeError(
            "spaCy model missing. Run: python -m spacy download en_core_web_sm"
        ) from exc


def extract_entities(text: str) -> list[str]:
    """Return canonical entity names found in `text`, preserving first-seen order."""
    if not text or not text.strip():
        return []
    doc = _nlp()(text[:2000])
    out: list[str] = []
    for ent in doc.ents:
        if ent.label_ not in _KEEP_LABELS:
            continue
        canonical = canonicalize_entity(ent.text)
        if canonical and canonical not in out:
            out.append(canonical)

    # NER misses known names in headline-case text often enough to be worth a
    # direct alias sweep as well.
    lowered = text.lower()
    for alias, canonical in _ALIAS_LOOKUP.items():
        if canonical in out:
            continue
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            out.append(canonical)
    return out


def primary_entity(entities: list[str]) -> str | None:
    """The entity a cluster is keyed on. First extracted wins — NER order tracks
    headline order, and headlines lead with their subject."""
    return entities[0] if entities else None
