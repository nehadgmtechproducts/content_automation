"""Stage 3 via fal.ai — OpenAI-compatible LLM router, billed on the single FAL_KEY.

Same two-call design as engine.py: extract facts corroborated by >=2 sources, write
from that fact sheet alone, then validate the draft states no figure the fact sheet
never contained. Prompts, schemas, source rendering and the validator are imported
from engine.py, so switching provider can never change what "grounded" means.

What is verified vs not (as of writing):
  * VERIFIED: the key authenticates against fal's catalog (a wrong key gets 401), the
    endpoint openrouter/router/openai/v1/chat/completions exists, and the OpenAI SDK
    sends fal's non-standard `Authorization: Key <key>` header (checked offline).
  * NOT VERIFIED: any real completion. The account's balance was exhausted — every run
    returns 403 "User is locked ... Top up your balance at fal.ai/dashboard/billing" —
    so structured output (response_format), the model id, and whether the routed model
    spends hidden reasoning tokens (which burned a whole token budget on an earlier
    provider) are all untested. Re-verify with one real call once the balance is funded.
"""
from __future__ import annotations

import logging

import openai
from openai import OpenAI

from app.settings import get_settings
from app.synth.engine import (  # provider-agnostic — reused, not reimplemented
    DRAFT_SYSTEM, FACTS_SYSTEM, CallResult, SourceDoc, SynthesisError,
    _render_sources, validate_grounding,
)
from app.synth.schemas import DraftOut, FactSheetOut

log = logging.getLogger(__name__)

FAL_LLM_BASE_URL = "https://fal.run/openrouter/router/openai/v1"
# Generous on purpose: unverified whether the routed model reasons before answering.
FACTS_MAX_TOKENS = 16000
DRAFT_MAX_TOKENS = 24000


_http_client = None  # test hook: an httpx.Client with a fake transport; None in production


def _client() -> OpenAI:
    key = get_settings().fal_key
    if not key:
        raise SynthesisError("FAL_KEY is not set")
    # fal authenticates with `Authorization: Key <key>`, not `Bearer`. default_headers
    # take precedence over the SDK's own header (confirmed offline), api_key is a dummy.
    return OpenAI(api_key="unused", base_url=FAL_LLM_BASE_URL,
                  default_headers={"Authorization": f"Key {key}"},
                  http_client=_http_client)


def _explain(exc: openai.APIStatusError, stage: str) -> SynthesisError:
    detail = exc.body if exc.body else exc.message
    text = f"{stage}: fal returned HTTP {exc.status_code}: {detail}"
    if "locked" in str(detail).lower() or "balance" in str(detail).lower():
        text += " — the fal account has no balance; top up at fal.ai/dashboard/billing"
    return SynthesisError(text)


def _parse_or_raise(response, stage: str):
    choice = response.choices[0]
    if choice.message.parsed is None:
        reason = choice.finish_reason
        hint = " — ran out of token budget before producing output" if reason == "length" else ""
        raise SynthesisError(f"{stage}: no parseable output (finish_reason={reason}){hint}")
    return choice.message.parsed


def extract_facts(docs: list[SourceDoc]) -> CallResult:
    if len(docs) < 2:
        raise SynthesisError(f"need >= 2 sources to establish consensus, got {len(docs)}")
    client = _client()
    try:
        response = client.chat.completions.parse(
            model=get_settings().fal_llm_model,
            messages=[
                {"role": "system", "content": FACTS_SYSTEM},
                {"role": "user", "content": f"{_render_sources(docs)}\n\nExtract the fact sheet for this event."},
            ],
            response_format=FactSheetOut,
            max_tokens=FACTS_MAX_TOKENS,
        )
    except openai.APIStatusError as exc:
        raise _explain(exc, "fact extraction") from exc
    parsed: FactSheetOut = _parse_or_raise(response, "fact extraction")

    kept = [f for f in parsed.consensus_facts if len(set(f.supporting_source_indices)) >= 2]
    if len(kept) != len(parsed.consensus_facts):
        log.info("dropped %d single-sourced 'consensus' fact(s)",
                 len(parsed.consensus_facts) - len(kept))
    parsed.consensus_facts = kept
    if not kept:
        raise SynthesisError("no fact was corroborated by two or more sources")

    return CallResult(payload=parsed.model_dump(), model=response.model,
                      input_tokens=response.usage.prompt_tokens,
                      output_tokens=response.usage.completion_tokens)


def generate_draft(fact_sheet: dict, docs: list[SourceDoc]) -> CallResult:
    import json

    client = _client()
    source_list = "\n".join(f"[{d.index}] {d.title} — {d.domain} ({d.url})" for d in docs)
    try:
        response = client.chat.completions.parse(
            model=get_settings().fal_llm_model,
            messages=[
                {"role": "system", "content": DRAFT_SYSTEM},
                {"role": "user", "content": (
                    f"SOURCE LIST (use these indices for citations):\n{source_list}\n\n"
                    f"FACT SHEET:\n{json.dumps(fact_sheet, indent=2)}\n\n"
                    f"Write the article, the 45-second vertical video script, and the "
                    f"distribution metadata."
                )},
            ],
            response_format=DraftOut,
            max_tokens=DRAFT_MAX_TOKENS,
        )
    except openai.APIStatusError as exc:
        raise _explain(exc, "draft generation") from exc
    parsed: DraftOut = _parse_or_raise(response, "draft generation")

    return CallResult(payload=parsed.model_dump(), model=response.model,
                      input_tokens=response.usage.prompt_tokens,
                      output_tokens=response.usage.completion_tokens)


def synthesize(docs: list[SourceDoc]) -> tuple[CallResult, CallResult, str]:
    """Full stage 3. Mirrors engine.synthesize(), including the one-retry-on-ungrounded
    figures behaviour — a second failure is reported, never hidden."""
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
