"""fal.ai integration — logic and request shapes against FAKE transports.

These prove the code builds the right requests, sends fal's `Authorization: Key` header,
parses responses, and fails soft. They do NOT prove fal's live service behaves this way:
the real account had no balance when this was written, so no live generation has run.
"""
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.assets.render import Segment, _video_filter
from app.providers import broll_fal
from app.providers.base import BrollRequest
from app.providers.broll_fal import FalBroll, build_prompt, clamp_duration
from app.synth import engine_fal
from app.synth.engine import SourceDoc, SynthesisError
from app.synth.schemas import FactSheetOut

EP = "google/gemini-omni-flash/v1.1/text-to-video"
LOCKED = {"detail": "User is locked. Reason: Exhausted balance. Top up your balance at fal.ai/dashboard/billing."}


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    s = SimpleNamespace(fal_key="test:key", fal_llm_model="openai/test-model")
    monkeypatch.setattr(broll_fal, "get_settings", lambda: s)
    monkeypatch.setattr(engine_fal, "get_settings", lambda: s)


# ------------------------------------------------------------------ video: pure logic

@pytest.mark.parametrize("seconds,expected", [(0.5, 3), (3, 3), (3.2, 4), (9.1, 10), (14, 10)])
def test_clamp_duration_stays_in_the_endpoints_3_to_10_range(seconds, expected):
    assert clamp_duration(seconds) == expected


def test_prompt_keeps_the_query_and_forbids_text_stray_logos_and_real_people():
    p = build_prompt("data center servers.")
    assert p.startswith("data center servers.")
    assert "no on-screen text" in p and "brand logos" in p and "real people" in p


def test_prompt_carries_the_story_headline_so_footage_shows_the_actual_subject():
    """Regression: with only a generic query the first video showed an unrelated PC
    motherboard (with an ASUS logo) for an Apple story."""
    p = build_prompt("laptop screen showing a permission dialog", "Apple tightens macOS Full Disk Access")
    assert 'news story titled "Apple tightens macOS Full Disk Access"' in p
    assert "news story" not in build_prompt("x")      # no context -> no dangling clause


def test_short_clips_freeze_on_the_last_frame_instead_of_shortening_the_video():
    """Generated clips cap at 10s but a beat can run ~15s. Without tpad the segment came
    out short and every later caption drifted from the voice."""
    f = _video_filter(0, Segment(Path("x.mp4"), True, 6.0))
    assert "tpad=stop_mode=clone" in f
    assert f.index("tpad") < f.index("trim=")


# -------------------------------------------------------- video: queue flow, fake fal

def make_broll(handler, **cfg):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return FalBroll(cfg, client=client, sleep=lambda s: None)


def happy_handler(log):
    state = {"polls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        log.append((req.method, str(req.url), req.headers.get("authorization"), req.content))
        url = str(req.url)
        if req.method == "POST" and url.endswith(EP):
            return httpx.Response(200, json={"request_id": "r1"})
        if url.endswith("/requests/r1/status"):
            state["polls"] += 1
            return httpx.Response(200, json={"status": "IN_PROGRESS" if state["polls"] < 2 else "COMPLETED"})
        if url.endswith("/requests/r1"):
            return httpx.Response(200, json={"video": {"url": "https://cdn.example/x.mp4"}})
        if url == "https://cdn.example/x.mp4":
            return httpx.Response(200, content=b"MP4DATA")
        raise AssertionError(f"unexpected request {req.method} {url}")
    return handler


def test_generates_a_clip_end_to_end_against_a_fake_queue(tmp_path):
    import json
    log = []
    clip = make_broll(happy_handler(log)).fetch(BrollRequest("data center servers", 4.2, 2), tmp_path)

    assert clip is not None and clip.is_video
    assert clip.path.read_bytes() == b"MP4DATA"
    assert clip.path.parent.name == "fal_cache" and clip.path.name.endswith("_5s.mp4")
    method, url, auth, body = log[0]
    assert auth == "Key test:key"                      # fal's scheme, not Bearer
    sent = json.loads(body)
    assert sent["aspect_ratio"] == "9:16"               # our vertical format
    assert sent["duration"] == 5                        # ceil(4.2), within 3-10
    assert "data center servers" in sent["prompt"]
    assert sum(1 for m, *_ in log if m == "POST") == 1  # exactly one paid submission


def test_locked_account_fails_soft_and_stops_spending_attempts(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(403, json=LOCKED)

    b = make_broll(handler)
    assert b.fetch(BrollRequest("a", 5, 0), tmp_path) is None
    assert b.fetch(BrollRequest("b", 5, 1), tmp_path) is None
    assert len(calls) == 1, "later beats must not keep hitting a locked account"


def test_job_that_never_finishes_gives_up_instead_of_hanging(tmp_path):
    def handler(req):
        if req.method == "POST":
            return httpx.Response(200, json={"request_id": "r1"})
        return httpx.Response(200, json={"status": "IN_PROGRESS"})

    assert make_broll(handler, max_wait_seconds=0).fetch(BrollRequest("a", 5, 0), tmp_path) is None


def test_missing_key_makes_no_request(monkeypatch, tmp_path):
    monkeypatch.setattr(broll_fal, "get_settings", lambda: SimpleNamespace(fal_key=""))

    def handler(req):
        raise AssertionError("must not call fal without a key")

    assert make_broll(handler).fetch(BrollRequest("a", 5, 0), tmp_path) is None


def test_provider_loader_uses_fal_only_when_a_key_exists(monkeypatch):
    import app.settings as settings_mod
    from app.providers.base import load_providers

    def with_key(key):
        s = SimpleNamespace(fal_key=key, pexels_api_key="", replicate_api_token="", elevenlabs_api_key="")
        monkeypatch.setattr(settings_mod, "get_settings", lambda: s)
        return load_providers().used["broll"]

    assert with_key("a:b") == "fal_gemini_omni"
    assert with_key("") == "stills_only"       # degrades instead of crashing


# ------------------------------------------------------------ scripting, fake fal LLM

DOCS = [SourceDoc(1, "https://a.com/x", "a.com", "T1", "text one"),
        SourceDoc(2, "https://b.com/y", "b.com", "T2", "text two")]


def chat_response(content: str) -> dict:
    return {"id": "c1", "object": "chat.completion", "created": 0, "model": "openai/test-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}


def fact_sheet(*fact_sources):
    return FactSheetOut(
        headline_summary="h",
        consensus_facts=[{"statement": f"fact {i}", "supporting_source_indices": s}
                         for i, s in enumerate(fact_sources)],
        disputed_claims=[], key_entities=[], timeline=[], numbers=[],
        editorial_framing_removed=[]).model_dump_json()


def use_fake_llm(monkeypatch, handler):
    monkeypatch.setattr(engine_fal, "_http_client", httpx.Client(transport=httpx.MockTransport(handler)))


def test_fact_extraction_sends_fals_auth_header_and_a_json_schema(monkeypatch):
    import json
    seen = {}

    def handler(req):
        seen["url"], seen["auth"] = str(req.url), req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=chat_response(fact_sheet([1, 2])))

    use_fake_llm(monkeypatch, handler)
    result = engine_fal.extract_facts(DOCS)

    assert seen["url"] == "https://fal.run/openrouter/router/openai/v1/chat/completions"
    assert seen["auth"] == "Key test:key"
    assert seen["body"]["model"] == "openai/test-model"
    assert seen["body"]["response_format"]["type"] == "json_schema"
    assert result.payload["consensus_facts"][0]["statement"] == "fact 0"
    assert (result.input_tokens, result.output_tokens) == (11, 7)


def test_single_sourced_claims_are_dropped_even_if_the_model_calls_them_consensus(monkeypatch):
    use_fake_llm(monkeypatch, lambda req: httpx.Response(200, json=chat_response(fact_sheet([1, 2], [1]))))
    facts = engine_fal.extract_facts(DOCS).payload["consensus_facts"]
    assert [f["statement"] for f in facts] == ["fact 0"]


def test_empty_balance_is_reported_with_the_fix_not_a_raw_stack_trace(monkeypatch):
    use_fake_llm(monkeypatch, lambda req: httpx.Response(403, json=LOCKED))
    with pytest.raises(SynthesisError, match="top up at fal.ai/dashboard/billing"):
        engine_fal.extract_facts(DOCS)


def test_needs_two_sources_before_calling_the_model(monkeypatch):
    use_fake_llm(monkeypatch, lambda req: (_ for _ in ()).throw(AssertionError("no call expected")))
    with pytest.raises(SynthesisError, match="need >= 2 sources"):
        engine_fal.extract_facts(DOCS[:1])


def test_missing_key_fails_before_any_request(monkeypatch):
    monkeypatch.setattr(engine_fal, "get_settings", lambda: SimpleNamespace(fal_key="", fal_llm_model="m"))
    with pytest.raises(SynthesisError, match="FAL_KEY is not set"):
        engine_fal.extract_facts(DOCS)


# ------------------------------------------------------------------- clip cache

def test_a_repeat_request_reuses_the_clip_and_is_not_billed_again(tmp_path):
    log = []
    b = make_broll(happy_handler(log))
    first = b.fetch(BrollRequest("data center servers", 4.2, 0, "Story"), tmp_path)
    posts_after_first = sum(1 for m, *_ in log if m == "POST")
    second = b.fetch(BrollRequest("data center servers", 4.2, 7, "Story"), tmp_path)
    assert second is not None and second.path == first.path
    assert sum(1 for m, *_ in log if m == "POST") == posts_after_first == 1


def test_a_longer_cached_clip_serves_a_shorter_request(tmp_path):
    """A re-render with a slightly faster voice needs a shorter clip — reuse, don't pay."""
    log = []
    b = make_broll(happy_handler(log))
    b.fetch(BrollRequest("shot", 9.0, 0, "Story"), tmp_path)          # generates a 9s clip
    again = b.fetch(BrollRequest("shot", 6.0, 1, "Story"), tmp_path)  # needs only 6s
    assert again is not None and sum(1 for m, *_ in log if m == "POST") == 1


def test_a_cached_clip_that_is_too_short_is_regenerated(tmp_path):
    log = []
    b = make_broll(happy_handler(log))
    b.fetch(BrollRequest("shot", 4.0, 0, "Story"), tmp_path)          # 4s clip cached
    b.fetch(BrollRequest("shot", 9.0, 1, "Story"), tmp_path)          # needs 9s -> must pay
    assert sum(1 for m, *_ in log if m == "POST") == 2


def test_a_different_story_context_is_not_a_cache_hit(tmp_path):
    log = []
    b = make_broll(happy_handler(log))
    b.fetch(BrollRequest("shot", 5.0, 0, "Story A"), tmp_path)
    b.fetch(BrollRequest("shot", 5.0, 1, "Story B"), tmp_path)
    assert sum(1 for m, *_ in log if m == "POST") == 2
