"""NVIDIA hosted-API synthesis path — against a FAKE transport (no network, no real key).

Proves the request shape (base URL, Bearer nvapi key, schema in the prompt), JSON parsing
including fenced replies, the single repair retry, and error mapping. It does NOT prove
NVIDIA's live service accepts the configured model id; that needs one real call.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.settings import ROOT
from app.synth import engine_nvidia
from app.synth.engine import BillingError, SourceDoc, SynthesisError
from app.synth.schemas import DraftOut, FactSheetOut

FACTS = {"headline_summary": "Apple restricts disk access.", "consensus_facts": [
    {"statement": "Apple restricted access.", "supporting_source_indices": [1, 2]},
    {"statement": "Only one source says this.", "supporting_source_indices": [1]}],
    "disputed_claims": [], "key_entities": ["Apple"], "timeline": [], "numbers": [],
    "editorial_framing_removed": []}
_fx = json.loads((Path(ROOT) / "fixtures" / "draft.json").read_text())
_fx["metadata"].update(category="Technology", region="Global", story_label="Apple disk access")
DRAFT = {k: _fx[k] for k in ("article", "script", "metadata")}
DOCS = [SourceDoc(index=i, url=f"https://s{i}.com/a", domain=f"s{i}.com", title=f"T{i}", markdown="body")
        for i in (1, 2)]


def reply(content, status=200, finish="stop"):
    body = {"id": "x", "object": "chat.completion", "created": 0, "model": "meta/test",
            "choices": [{"index": 0, "finish_reason": finish,
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    return httpx.Response(status, json=body)


class Wire(list):
    """Requests received so far; `.queue` holds the canned responses to hand out in order."""
    def __init__(self):
        super().__init__()
        self.queue = []


@pytest.fixture()
def wire(monkeypatch):
    """Install a fake transport; returns the list of requests it received."""
    seen = Wire()
    queue = seen.queue

    def handler(request):
        seen.append(request)
        item = queue.pop(0)
        return item(request) if callable(item) else item

    monkeypatch.setattr(engine_nvidia, "_http_client", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(engine_nvidia, "get_settings", lambda: SimpleNamespace(
        nvidia_api_key="nvapi-test", nvidia_model="meta/test", nvidia_thinking=False))
    return seen


def test_request_goes_to_nvidia_with_the_bearer_key_and_the_schema(wire):
    wire.queue.append(reply(json.dumps(FACTS)))
    engine_nvidia.extract_facts(DOCS)
    req = wire[0]
    assert str(req.url) == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer nvapi-test"
    body = json.loads(req.content)
    assert body["model"] == "meta/test"
    assert "consensus_facts" in body["messages"][0]["content"]      # the schema is in the prompt
    assert body["chat_template_kwargs"] == {"enable_thinking": False}   # hidden reasoning stays off


def test_single_sourced_facts_are_dropped_and_tokens_counted(wire):
    wire.queue.append(reply(json.dumps(FACTS)))
    out = engine_nvidia.extract_facts(DOCS)
    assert [f["statement"] for f in out.payload["consensus_facts"]] == ["Apple restricted access."]
    assert (out.input_tokens, out.output_tokens) == (10, 5)


def test_fenced_json_with_chatter_is_accepted(wire):
    wire.queue.append(reply("Sure! Here you go:\n```json\n" + json.dumps(DRAFT) + "\n```\nHope it helps."))
    out = engine_nvidia.generate_draft({"x": 1}, DOCS)
    assert out.payload["article"]["title"] == DRAFT["article"]["title"]


def test_a_bad_reply_is_retried_once_with_the_error_fed_back(wire):
    wire.queue += [reply("not json at all"), reply(json.dumps(DRAFT))]
    out = engine_nvidia.generate_draft({"x": 1}, DOCS)
    assert out.input_tokens == 20                                   # both calls counted
    second = json.loads(wire[1].content)["messages"]
    assert second[-2]["content"] == "not json at all" and "did not validate" in second[-1]["content"]


def test_two_bad_replies_fail_loudly_not_silently(wire):
    wire.queue += [reply("nope"), reply("still nope", finish="length")]
    with pytest.raises(SynthesisError, match="ran out of token budget"):
        engine_nvidia.generate_draft({"x": 1}, DOCS)


def test_out_of_credits_is_a_billing_error_so_production_pauses(wire):
    wire.queue.append(httpx.Response(402, json={"detail": "Payment required: out of credits"}))
    with pytest.raises(BillingError):
        engine_nvidia.extract_facts(DOCS)


def test_bad_key_names_the_variable_to_fix(wire):
    wire.queue.append(httpx.Response(401, json={"detail": "Unauthorized"}))
    with pytest.raises(SynthesisError, match="NVIDIA_API_KEY") as e:
        engine_nvidia.extract_facts(DOCS)
    assert not isinstance(e.value, BillingError)


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(engine_nvidia, "get_settings", lambda: SimpleNamespace(nvidia_api_key="", nvidia_model="m"))
    with pytest.raises(SynthesisError, match="NVIDIA_API_KEY is not set"):
        engine_nvidia.extract_facts(DOCS)


def test_probe_is_false_on_failure_and_true_on_success(wire):
    wire.queue += [httpx.Response(402, json={"detail": "x"}), reply("ok")]
    assert engine_nvidia.probe() is False
    assert engine_nvidia.probe() is True


def test_schemas_used_are_the_shared_ones():
    FactSheetOut.model_validate(FACTS); DraftOut.model_validate(DRAFT)


def _long_draft():
    d = json.loads(json.dumps(DRAFT))
    d["article"]["sections"] = [{"heading": "H", "body_markdown": " ".join(["word"] * 200)}] * 3
    return d


def test_a_short_article_is_regenerated_once_with_the_length_feedback(wire):
    wire.queue += [reply(json.dumps(FACTS)), reply(json.dumps(DRAFT)), reply(json.dumps(_long_draft()))]
    facts, draft, notes = engine_nvidia.synthesize(DOCS)
    assert engine_nvidia.body_words(draft.payload) == 600 and "for length" in notes
    assert "only" in json.loads(wire[2].content)["messages"][1]["content"]   # the retry names the shortfall


def test_a_long_enough_article_is_not_regenerated(wire):
    wire.queue += [reply(json.dumps(FACTS)), reply(json.dumps(_long_draft()))]
    _f, _d, notes = engine_nvidia.synthesize(DOCS)
    assert len(wire) == 2 and notes == ""


def test_still_short_after_the_retry_is_reported_not_hidden(wire):
    wire.queue += [reply(json.dumps(FACTS)), reply(json.dumps(DRAFT)), reply(json.dumps(DRAFT))]
    _f, _d, notes = engine_nvidia.synthesize(DOCS)
    assert notes.startswith("SHORT ARTICLE AFTER RETRY")
