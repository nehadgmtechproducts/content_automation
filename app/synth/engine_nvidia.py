"""Stage 3 via NVIDIA's hosted API (build.nvidia.com) — OpenAI-compatible chat completions,
authenticated with an `nvapi-...` key (NVIDIA_API_KEY).

Same two-call design as engine.py: extract facts corroborated by >=2 sources, write from that
fact sheet alone, then validate the draft states no figure the fact sheet never contained.
Prompts, schemas, source rendering and the validator are imported from engine.py, so switching
provider can never change what "grounded" means.

Unlike the fal/OpenAI paths this does not rely on provider-side structured output (support
differs per hosted model). The JSON schema is put in the prompt, the reply is validated with
pydantic, and one failed parse is retried once with the validation error fed back.
"""
from __future__ import annotations

import json
import logging
import re

import openai
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from app.settings import get_settings
from app.synth.engine import (  # provider-agnostic — reused, not reimplemented
    DRAFT_SYSTEM, FACTS_SYSTEM, BillingError, CallResult, SourceDoc, SynthesisError,
    _render_sources, validate_grounding,
)
from app.synth.schemas import DraftOut, FactSheetOut

log = logging.getLogger(__name__)

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
FACTS_MAX_TOKENS = 8000
DRAFT_MAX_TOKENS = 12000
# The prompt asks for 600 words (+/- 60) but this model tends to write ~200-300. Below this, ask
# once for a fuller version built from the same fact sheet.
MIN_BODY_WORDS = 480
_http_client = None  # test hook: an httpx.Client with a fake transport; None in production


def _client() -> OpenAI:
    key = get_settings().nvidia_api_key
    if not key:
        raise SynthesisError("NVIDIA_API_KEY is not set")
    return OpenAI(api_key=key, base_url=NVIDIA_BASE_URL, http_client=_http_client, timeout=300)


def _explain(exc: openai.APIStatusError, stage: str) -> SynthesisError:
    detail = exc.body if exc.body else exc.message
    text = f"{stage}: NVIDIA returned HTTP {exc.status_code}: {detail}"
    if exc.status_code in (401, 403):
        return SynthesisError(text + " — check NVIDIA_API_KEY (it must start with nvapi-)")
    if exc.status_code == 402 or "credit" in str(detail).lower() or "quota" in str(detail).lower():
        return BillingError(text + " — the NVIDIA account is out of credits")
    return SynthesisError(text)


def probe() -> bool:
    """Cheap 'is production possible again?' check, used only while production is paused."""
    try:
        _client().chat.completions.create(
            model=get_settings().nvidia_model,
            messages=[{"role": "user", "content": "Reply: ok"}], max_tokens=5)
        return True
    except Exception as exc:  # noqa: BLE001 - any failure means "not yet"
        log.info("production probe: still unavailable (%s)", type(exc).__name__)
        return False


def _extract_json(text: str) -> str:
    """The JSON object in a model reply, tolerating ```json fences and chatter around it."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        return fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


def _complete(system: str, user: str, schema: type[BaseModel], stage: str,
              max_tokens: int):
    """One chat completion, parsed into `schema`; a parse failure is retried once."""
    client = _client()
    settings = get_settings()
    model = settings.nvidia_model
    system = (f"{system}\n\nReply with ONE JSON object and nothing else — no prose, no code "
              f"fences. It must validate against this JSON Schema:\n"
              f"{json.dumps(schema.model_json_schema())}")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    in_tok = out_tok = 0
    for attempt in (1, 2):
        try:
            response = client.chat.completions.create(
                model=model, messages=messages, max_tokens=max_tokens, temperature=0.2,
                # Nemotron models "think" first and the hidden reasoning eats the whole token
                # budget, leaving an empty answer (seen live). Off by default; NVIDIA_THINKING=1
                # turns it back on.
                extra_body={"chat_template_kwargs": {"enable_thinking": settings.nvidia_thinking}})
        except openai.APIStatusError as exc:
            raise _explain(exc, stage) from exc
        if response.usage:
            in_tok += response.usage.prompt_tokens
            out_tok += response.usage.completion_tokens
        content = response.choices[0].message.content or ""
        try:
            parsed = schema.model_validate_json(_extract_json(content))
            return parsed, response.model or model, in_tok, out_tok
        except ValidationError as exc:
            reason = response.choices[0].finish_reason
            log.warning("%s: reply did not match the schema (attempt %d, finish=%s)", stage, attempt, reason)
            if attempt == 2:
                hint = " — ran out of token budget" if reason == "length" else ""
                raise SynthesisError(f"{stage}: no parseable output{hint}: {str(exc)[:300]}") from exc
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content":
                          f"That did not validate: {str(exc)[:1500]}\nReturn the corrected JSON object only."}]


def extract_facts(docs: list[SourceDoc]) -> CallResult:
    if len(docs) < 2:
        raise SynthesisError(f"need >= 2 sources to establish consensus, got {len(docs)}")
    parsed, model, i, o = _complete(
        FACTS_SYSTEM, f"{_render_sources(docs)}\n\nExtract the fact sheet for this event.",
        FactSheetOut, "fact extraction", FACTS_MAX_TOKENS)

    kept = [f for f in parsed.consensus_facts if len(set(f.supporting_source_indices)) >= 2]
    if len(kept) != len(parsed.consensus_facts):
        log.info("dropped %d single-sourced 'consensus' fact(s)",
                 len(parsed.consensus_facts) - len(kept))
    parsed.consensus_facts = kept
    if not kept:
        raise SynthesisError("no fact was corroborated by two or more sources")
    return CallResult(payload=parsed.model_dump(), model=model, input_tokens=i, output_tokens=o)


def body_words(draft_payload: dict) -> int:
    return sum(len(s.get("body_markdown", "").split())
               for s in draft_payload.get("article", {}).get("sections", []))


def generate_draft(fact_sheet: dict, docs: list[SourceDoc], too_short: int = 0) -> CallResult:
    source_list = "\n".join(f"[{d.index}] {d.title} — {d.domain} ({d.url})" for d in docs)
    parsed, model, i, o = _complete(
        DRAFT_SYSTEM,
        f"SOURCE LIST (use these indices for citations):\n{source_list}\n\n"
        f"FACT SHEET:\n{json.dumps(fact_sheet, indent=2)}\n\n"
        f"Write the article, the 45-second vertical video script, and the distribution metadata."
        + (f"\n\nYour previous attempt had only {too_short} words in the article body. The body "
           f"must be 600 words (+/- 60) across 3-4 sections. Reach that length by covering EVERY "
           f"fact in the fact sheet in depth — who said or did what, the sequence, the numbers, "
           f"what is disputed — never by adding anything that is not in the fact sheet."
           if too_short else ""),
        DraftOut, "draft generation", DRAFT_MAX_TOKENS)
    return CallResult(payload=parsed.model_dump(), model=model, input_tokens=i, output_tokens=o)


def synthesize(docs: list[SourceDoc]) -> tuple[CallResult, CallResult, str]:
    """Full stage 3, mirroring engine_fal.synthesize(), including the one-retry-on-ungrounded
    figures behaviour — a second failure is reported, never hidden."""
    facts = extract_facts(docs)
    draft = generate_draft(facts.payload, docs)
    notes = ""
    words = body_words(draft.payload)
    if words < MIN_BODY_WORDS:
        log.warning("article body is %d words (target 600) — asking for a fuller version", words)
        draft = generate_draft(facts.payload, docs, too_short=words)
        words = body_words(draft.payload)
        notes = (f"regenerated once for length ({words} words)" if words >= MIN_BODY_WORDS
                 else f"SHORT ARTICLE AFTER RETRY: {words} words")

    problems = validate_grounding(draft.payload, facts.payload)
    if problems:
        log.warning("ungrounded figures %s — regenerating once", problems)
        draft = generate_draft(facts.payload, docs)
        problems = validate_grounding(draft.payload, facts.payload)
        if problems:
            notes = f"{notes}; UNGROUNDED FIGURES AFTER RETRY: {', '.join(problems)}".lstrip("; ")
            log.error(notes)
        else:
            notes = f"{notes}; regenerated once to clear ungrounded figures".lstrip("; ")
    return facts, draft, notes
