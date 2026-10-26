"""Backstop for identity protection: flag stories where a child or a survivor of a sexual offence could be identified.

The drafting prompt tells the model never to name or identify such a person, but a prompt is a request, not a
guarantee: in testing the model still wrote the school and neighbourhood of a child victim, and (before the rule
was strengthened) her name and her mother's. The law is not optional (BNS ss. 72-73, POCSO s. 23), so code
adds what a prompt cannot:

  1. detect stories that involve a sexual offence, or a child in a crime (deterministic, no model);
  2. list every person and place the draft names, so the reviewer sees exactly what could identify someone;
  3. the Telegram card then carries a loud warning and an Approve button that makes the reviewer affirm that
     nobody is identifiable (see app/approval/cards.py).

It never publishes or blocks anything by itself: it makes sure a human is looking at the right thing.
Deliberately over-inclusive (a war report mentioning children killed is flagged too): a false alarm costs a
second look, a miss costs a legal problem.
"""
from __future__ import annotations

import re

_SEXUAL_OFFENCE = re.compile(
    r"\b(rape[sd]?|raping|gang-?rap\w*|sexual(?:ly)?\s+(?:assault\w*|abus\w*|harass\w*|violen\w*|offen[cs]\w*|exploit\w*)"
    r"|molest\w*|pocso|indecent\s+assault|child\s+(?:sex(?:ual)?\s+)?abuse|sex\s+crimes?|child\s+porn\w*"
    r"|sexual\s+abuse\s+images?)\b",
    re.IGNORECASE,
)
_MINOR = re.compile(
    r"\b(?:(?:[1-9]|1[0-7])[- ]years?[- ]old|aged\s+(?:[1-9]|1[0-7])\b|minors?|juveniles?|schoolgirls?|schoolboys?"
    r"|school\s?child(?:ren)?|teen(?:ager)?s?|toddlers?|infants?|babies|baby|underage|under[- ]18|children|child)\b",
    re.IGNORECASE,
)
_HARM = re.compile(
    r"\b(assault\w*|abus\w*|rape\w*|kidnap\w*|abduct\w*|murder\w*|kill\w*|stabb\w*|shot|traffick\w*|missing|molest\w*|beaten|slain)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_NAME_LABELS = {"PERSON", "ORG", "GPE", "LOC", "FAC"}
MAX_NAMES = 30


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text) if s.strip()]


def sensitive_reasons(text: str) -> list[str]:
    """Why this text needs the extra identity check ([] = it does not)."""
    reasons: list[str] = []
    if _SEXUAL_OFFENCE.search(text):
        reasons.append("a sexual offence is reported")
    if any(_MINOR.search(s) and _HARM.search(s) for s in _sentences(text)):
        reasons.append("a child is involved in a crime or harm")
    return reasons


def draft_text(payload: dict) -> str:
    """Everything the model wrote that a reader (or the video) could see."""
    a, m, script = payload.get("article", {}), payload.get("metadata", {}), payload.get("script", {})
    parts = [a.get("title", ""), a.get("dek", ""), a.get("meta_description", ""), a.get("why_it_matters", ""),
             *a.get("key_takeaways", []), *[f"{s.get('heading', '')}. {s.get('body_markdown', '')}" for s in a.get("sections", [])],
             *m.get("title_variants", []), m.get("story_label", ""), m.get("thumbnail_text", ""),
             *m.get("target_keywords", []), *m.get("hashtags", []),
             *[b.get("narration", "") for b in script.get("beats", [])]]
    return "\n".join(p for p in parts if p)


def named_entities(text: str) -> list[str]:
    """Every person, organisation and place named in `text`, first-seen order. spaCy's small model mislabels
    some names (it calls people organisations), so all of those labels are listed rather than classified."""
    from app.detect.entities import _nlp

    out: list[str] = []
    doc = _nlp()(text[:20000])
    for ent in doc.ents:
        if ent.label_ not in _NAME_LABELS:
            continue
        name = re.sub(r"\s+", " ", ent.text).strip(" ,.;:'\"()")
        if ent.label_ == "PERSON":
            name = re.sub(r"[’']s$", "", name)          # "Priya Nair's mother" -> "Priya Nair"
        if len(name) > 2 and name.lower() not in {n.lower() for n in out}:
            out.append(name)
    return out[:MAX_NAMES]


def assess(payload: dict, source_texts: list[str] | None = None) -> dict | None:
    """None when nothing sensitive; otherwise {"reasons": [...], "names": [...]} to store with the draft."""
    written = draft_text(payload)
    reasons = sensitive_reasons(written)
    for reason in sensitive_reasons("\n".join(source_texts or [])):
        if reason not in reasons:
            reasons.append(reason)
    if not reasons:
        return None
    return {"reasons": reasons, "names": named_entities(written)}
