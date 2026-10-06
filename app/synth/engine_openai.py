"""Stage 3, OpenAI path.

Same two-call design as engine.py (Anthropic): extract facts corroborated by >=2
sources, then write from that fact sheet alone, then validate the draft doesn't state
a figure the fact sheet never mentioned. The prompts, schemas, source rendering, and
the grounding validator are shared with the Anthropic path via import — only the
actual API call differs, so switching providers can never silently change what
"grounded" means between the two.
"""
from __future__ import annotations

import logging

from openai import OpenAI

from app.settings import get_settings
from app.synth.engine import (  # provider-agnostic — reused, not reimplemented
    DRAFT_SYSTEM, FACTS_SYSTEM, CallResult, SourceDoc, SynthesisError,
    _render_sources, validate_grounding,
)
from app.synth.schemas import DraftOut, FactSheetOut

log = logging.getLogger(__name__)


def _client() -> OpenAI:
    return OpenAI(api_key=get_settings().openai_api_key)


def extract_facts(docs: list[SourceDoc]) -> CallResult:
    if len(docs) < 2:
        raise SynthesisError(f"need >= 2 sources to establish consensus, got {len(docs)}")

    client = _client()
    settings = get_settings()
    response = client.responses.parse(
        model=settings.openai_model,
        instructions=FACTS_SYSTEM,
        input=f"{_render_sources(docs)}\n\nExtract the fact sheet for this event.",
        text_format=FactSheetOut,
    )
    parsed: FactSheetOut = response.output_parsed
    if parsed is None:
        raise SynthesisError("model refused or returned no parseable fact sheet")

    kept = [f for f in parsed.consensus_facts if len(set(f.supporting_source_indices)) >= 2]
    dropped = len(parsed.consensus_facts) - len(kept)
    if dropped:
        log.info("dropped %d single-sourced 'consensus' fact(s)", dropped)
    parsed.consensus_facts = kept
    if not kept:
        raise SynthesisError("no fact was corroborated by two or more sources")

    return CallResult(
        payload=parsed.model_dump(), model=response.model,
        input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens,
    )


def generate_draft(fact_sheet: dict, docs: list[SourceDoc]) -> CallResult:
    import json

    client = _client()
    settings = get_settings()
    source_list = "\n".join(f"[{d.index}] {d.title} — {d.domain} ({d.url})" for d in docs)

    response = client.responses.parse(
        model=settings.openai_model,
        instructions=DRAFT_SYSTEM,
        input=(
            f"SOURCE LIST (use these indices for citations):\n{source_list}\n\n"
            f"FACT SHEET:\n{json.dumps(fact_sheet, indent=2)}\n\n"
            f"Write the article, the 45-second vertical video script, and the "
            f"distribution metadata."
        ),
        text_format=DraftOut,
    )
    parsed: DraftOut = response.output_parsed
    if parsed is None:
        raise SynthesisError("model refused or returned no parseable draft")

    return CallResult(
        payload=parsed.model_dump(), model=response.model,
        input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens,
    )


def synthesize(docs: list[SourceDoc]) -> tuple[CallResult, CallResult, str]:
    """Full stage 3 via OpenAI. Mirrors engine.synthesize() exactly, including the
    one-retry-on-ungrounded-figures behavior."""
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
