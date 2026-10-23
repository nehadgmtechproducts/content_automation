"""Stage 3 — the synthesis engine.

Two calls, deliberately:
  1. fact extraction  -> an auditable FactSheet you can inspect and re-run against
  2. generation       -> article + 45s script + distribution metadata, from the FactSheet

Splitting them means a bad article can be regenerated without re-paying for extraction,
and the fact sheet is a reviewable artifact when someone asks "where did this claim come
from?".
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import anthropic

from app.settings import get_settings
from app.synth.schemas import DraftOut, FactSheetOut
from app.categories import prompt_listing
from app.regions import prompt_listing as region_listing

log = logging.getLogger(__name__)

# Stable prefix -> cacheable. Nothing volatile (no timestamps, no ids) may enter these.
FACTS_SYSTEM = """You are a wire-service fact extractor. You are given several independent \
news articles about the same event. Your job is to separate what is established from what \
is asserted.

Rules:
- A statement belongs in consensus_facts ONLY if at least two distinct sources assert it. \
Record every supporting source index.
- Anything asserted by exactly one source goes in disputed_claims, no matter how plausible.
- Strip editorial framing. "Slammed" becomes "criticized". "Bombshell report" becomes \
"report". Record what you stripped in editorial_framing_removed.
- Never introduce a fact, number, name, or date that does not appear in the supplied \
articles. If the sources do not say it, it does not exist.
- Copy figures exactly as reported, with their units and their qualifiers ("about", \
"more than").
- Prefer specific attributions ("according to the filing") over vague ones ("reportedly")."""

DRAFT_SYSTEM = """You are a senior news editor writing for a general audience that is \
intelligent but time-poor. You write from a supplied fact sheet and nothing else.

Choose exactly ONE category for the story:
""" + prompt_listing() + """

Give the story a short story_label: the NAME of the ongoing subject (2-6 words, a noun \
phrase such as "OpenAI real-time voice model" or "Quebec election"), not a headline and not \
today's development. Follow-up reports on the same subject will share this label.

Choose exactly ONE region (the website edition the story belongs to):
""" + region_listing() + """
Pick Global unless the story is clearly about, or mainly affects, one region. A product or \
AI model released by a global company is Global even if the company is based in one country; \
an election, protest, court ruling, match or local market is the region where it happens.

Category rules (apply the rules of the category you chose, in addition to the hard rules):
- Politics: attribute every claim to a named person, body or outlet. State what happened \
and what each side says only when the sources do. No predictions, no speculation about \
motives, no loaded labels, no endorsement of any party or candidate. For war, violence \
or protests: no graphic detail; casualty or damage figures must carry the source that \
reported them.
- Finance: report prices, percentages and figures exactly as sourced, with the date or \
session they refer to. Never recommend buying, selling or holding anything, and never \
forecast prices. Do not imply a stock will rise or fall.
- Entertainment (including sports): scores, results, dates, names and titles exactly as \
sourced. Anything a single outlet reports as a rumour or leak goes in disputed_claims.
- Launches: specifications, prices and availability are the manufacturer's claims — \
attribute them ("the company says") and keep the currency and region as sourced.
- Healthcare: attribute every medical claim to the study, journal, regulator, health body or \
company that made it, and say what kind of evidence it is when the sources do (a small trial, \
animal or lab study, observational study, regulator decision). Never say something cures, \
prevents or treats a condition unless the sources attribute that to a regulator or the study \
authors. Do not confuse correlation with causation. Give no medical advice, dosages or \
recommendations to readers. No graphic detail; case, death and hospitalisation figures must \
carry the body that reported them.
- Technology: performance and benchmark claims belong to the company that made them.

Hard rules:
- Every factual claim in the article must trace to a consensus fact in the supplied sheet. \
You may not add outside knowledge, context, or history that is not in the sheet.
- Cite with bracketed indices [1], [2] matching the source list you are given.
- If something is in disputed_claims, either omit it or attribute it explicitly \
("X reported, though other outlets have not confirmed").
- No hype. No "game-changing", "revolutionary", "in a stunning move". No rhetorical \
questions as section headings.
- The article body targets 600 words (+/- 60), in 3-4 H2 sections.

For the video script:
- Exactly four beats in order: hook, conflict, resolution, cta.
- Total narration 105-125 words. That lands at ~45 seconds at natural pace. Count them.
- The hook must state the actual news in its first 8 words. No "You won't believe".
- The cta is one short line pointing to the full article.
- broll_query describes ONE concrete, filmable shot in 6-12 words that shows the story's \
actual subject — name the product, company or object ("Apple MacBook showing a privacy \
permission dialog", not "person typing on a laptop"). A shot that could illustrate any story \
in the category is not acceptable. Each beat's shot must look different from the others. \
Do not ask for readable text, and do not name any brand other than the story's own subject.
- Politics stories: the shot must NOT show real or identifiable people, soldiers, police, \
crowds, weapons, flags, injuries or any depiction of a real event — generated footage of \
those can be mistaken for real footage. Use symbolic objects instead (a gavel, a ballot \
box, a sealed envelope, scales, a map on a table)."""


@dataclass
class SourceDoc:
    index: int
    url: str
    domain: str
    title: str
    markdown: str


@dataclass
class CallResult:
    payload: dict
    model: str
    input_tokens: int
    output_tokens: int


class SynthesisError(RuntimeError):
    pass


class BillingError(SynthesisError):
    """The provider account has no balance. Not about the story: every topic fails the same
    way until someone tops up, so the scheduler pauses production instead of burning
    stories (see app/production.py)."""


def _client() -> anthropic.Anthropic:
    key = get_settings().anthropic_api_key
    if key:
        return anthropic.Anthropic(api_key=key)
    # No explicit key: the SDK still resolves ANTHROPIC_AUTH_TOKEN or an `ant auth login`
    # profile, so let it try rather than failing here.
    return anthropic.Anthropic()


def _render_sources(docs: list[SourceDoc]) -> str:
    parts = []
    for d in docs:
        parts.append(
            f"<source index=\"{d.index}\" domain=\"{d.domain}\" url=\"{d.url}\">\n"
            f"# {d.title}\n\n{d.markdown}\n</source>"
        )
    return "\n\n".join(parts)


def extract_facts(docs: list[SourceDoc]) -> CallResult:
    if len(docs) < 2:
        raise SynthesisError(
            f"need >= 2 sources to establish consensus, got {len(docs)}"
        )

    client = _client()
    settings = get_settings()
    response = client.messages.parse(
        model=settings.synthesis_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        cache_control={"type": "ephemeral"},
        system=FACTS_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"{_render_sources(docs)}\n\n"
                       f"Extract the fact sheet for this event.",
        }],
        output_format=FactSheetOut,
    )
    parsed: FactSheetOut = response.parsed_output

    # Enforce the >=2 sources rule in code — the prompt asks for it, this guarantees it.
    kept = [f for f in parsed.consensus_facts if len(set(f.supporting_source_indices)) >= 2]
    dropped = len(parsed.consensus_facts) - len(kept)
    if dropped:
        log.info("dropped %d single-sourced 'consensus' fact(s)", dropped)
    parsed.consensus_facts = kept

    if not kept:
        raise SynthesisError("no fact was corroborated by two or more sources")

    return CallResult(
        payload=parsed.model_dump(),
        model=response.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def generate_draft(fact_sheet: dict, docs: list[SourceDoc]) -> CallResult:
    client = _client()
    settings = get_settings()

    source_list = "\n".join(f"[{d.index}] {d.title} — {d.domain} ({d.url})" for d in docs)
    import json

    response = client.messages.parse(
        model=settings.synthesis_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        cache_control={"type": "ephemeral"},
        system=DRAFT_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"SOURCE LIST (use these indices for citations):\n{source_list}\n\n"
                f"FACT SHEET:\n{json.dumps(fact_sheet, indent=2)}\n\n"
                f"Write the article, the 45-second vertical video script, and the "
                f"distribution metadata."
            ),
        }],
        output_format=DraftOut,
    )
    parsed: DraftOut = response.parsed_output
    return CallResult(
        payload=parsed.model_dump(),
        model=response.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


# ------------------------------------------------------------------- validation

_NUMBER = re.compile(
    r"(?<![\w.])\$?\s?(\d[\d,]*(?:\.\d+)?)\s*"
    r"(trillion|billion|million|thousand|percent|tn|bn|mn|m|k|%)?\b",
    re.IGNORECASE,
)
_MAGNITUDES = {
    "trillion": 10 ** 12, "tn": 10 ** 12,
    "billion": 10 ** 9, "bn": 10 ** 9,
    "million": 10 ** 6, "mn": 10 ** 6, "m": 10 ** 6,
    "thousand": 10 ** 3, "k": 10 ** 3,
}
_YEAR = re.compile(r"^(1[89]|20)\d{2}$")
# Bare integers this small are list counts, ordinals and durations ("top 3", "2 days"),
# not the kind of figure worth auditing. Decimals are always audited.
_SMALL_COUNT_MAX = 12


def _numbers_in(text: str) -> set[str]:
    """Every auditable figure in `text`, normalized so formatting differences match.

    "$4.2 billion", "4.2bn" and "$4,200,000,000" all yield the same tokens, so the
    validator compares meaning rather than punctuation.
    """
    found: set[str] = set()
    for raw, unit in _NUMBER.findall(text):
        token = raw.replace(",", "").rstrip(".")
        if not token:
            continue
        if _YEAR.match(token):
            continue
        try:
            value = float(token)
        except ValueError:
            continue
        # Bare small integers carry no audit risk; a decimal or a unit means it does.
        if "." not in token and not unit and value <= _SMALL_COUNT_MAX:
            continue

        found.add(token)
        multiplier = _MAGNITUDES.get(unit.lower()) if unit else None
        if multiplier:
            scaled = value * multiplier
            found.add(str(int(scaled)) if scaled.is_integer() else str(scaled))
    return found


def validate_grounding(draft: dict, fact_sheet: dict) -> list[str]:
    """Return a list of ungrounded figures found in the draft.

    This guards the one failure mode that actually matters in automated news: a
    confidently invented number. Everything the article states numerically must appear
    somewhere in the fact sheet.
    """
    article = draft.get("article", {})
    body = " ".join(
        [article.get("dek", ""), article.get("why_it_matters", "")]
        + [s.get("body_markdown", "") for s in article.get("sections", [])]
        + list(article.get("key_takeaways", []))
    )
    script_text = " ".join(b.get("narration", "") for b in draft.get("script", {}).get("beats", []))

    sheet_text = " ".join(
        [fact_sheet.get("headline_summary", "")]
        + [f.get("statement", "") for f in fact_sheet.get("consensus_facts", [])]
        + [f"{n.get('value','')} {n.get('describes','')}" for n in fact_sheet.get("numbers", [])]
        + [t.get("what", "") + " " + t.get("when", "") for t in fact_sheet.get("timeline", [])]
        + [d.get("statement", "") for d in fact_sheet.get("disputed_claims", [])]
    )

    grounded = _numbers_in(sheet_text)
    problems = []
    for number in sorted(_numbers_in(body) | _numbers_in(script_text)):
        if number not in grounded:
            problems.append(number)
    return problems


def synthesize(docs: list[SourceDoc]) -> tuple[CallResult, CallResult, str]:
    """Full stage 3. Returns (facts, draft, validation_notes).

    One retry on ungrounded figures — a second failure is reported rather than hidden,
    because silently shipping an invented number is worse than a failed topic.
    """
    facts = extract_facts(docs)
    draft = generate_draft(facts.payload, docs)

    problems = validate_grounding(draft.payload, facts.payload)
    notes = ""
    if problems:
        log.warning("ungrounded figures %s — regenerating once", problems)
        draft = generate_draft(facts.payload, docs)
        problems = validate_grounding(draft.payload, facts.payload)
        if problems:
            notes = f"UNGROUNDED FIGURES AFTER RETRY: {', '.join(problems)}"
            log.error(notes)
        else:
            notes = "regenerated once to clear ungrounded figures"
    return facts, draft, notes
