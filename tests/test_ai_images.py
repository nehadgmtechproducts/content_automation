"""AI cover illustration — fake transport only (the conftest blocks real image requests).

The live endpoint was verified by hand: FLUX.2-klein returned a 1344x768 photo in ~3s."""
import base64
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.assets import pipeline as assets
from app.providers import image_nvidia as nv
from app.providers.image_pillow import PillowEditorialCard
from app.providers.base import ImageRequest

SCRIPT = {"beats": [{"broll_query": "SpaceX rocket on a launch pad"}]}
SETTINGS = SimpleNamespace(nvidia_api_key="nvapi-test", ai_images=True,
                           nvidia_image_model="black-forest-labs/flux.2-klein-4b")


def png_b64(w=1344, h=768, color=(200, 80, 20)):
    buf = io.BytesIO(); Image.new("RGB", (w, h), color).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def install(monkeypatch, handler, settings=SETTINGS):
    monkeypatch.setattr(nv, "_http_client", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(nv, "get_settings", lambda: settings)


def ok(request):
    return httpx.Response(200, json={"artifacts": [{"base64": png_b64(), "finishReason": "SUCCESS", "seed": 0}]})


# ---------- prompt ----------

def test_prompt_uses_the_stories_own_shot_and_forbids_text_logos_and_faces():
    p = nv.build_prompt("Finance", "Musk pledges SpaceX share", "Elon Musk", SCRIPT)
    assert "SpaceX rocket on a launch pad" in p and 'titled "Musk pledges SpaceX share"' in p
    assert "no text" in p and "no logos" in p and "no clear faces" in p


def test_politics_prompts_withhold_the_headline_and_swap_risky_shots():
    risky = {"beats": [{"broll_query": "Donald Trump speaking at a rally crowd"}]}
    p = nv.build_prompt("Politics", "Trump signs the bill", "Donald Trump", risky)
    assert "Trump" not in p                       # neither the headline nor the risky shot reaches the model


def test_prompt_falls_back_to_the_entity_when_there_is_no_script():
    assert "Apple" in nv.build_prompt("Technology", "H", "Apple", {})


# ---------- generation ----------

def test_success_saves_a_cropped_16x9_jpeg_and_sends_the_key(monkeypatch, tmp_path):
    seen = []
    def handler(request):
        seen.append(request); return ok(request)
    install(monkeypatch, handler)
    out = nv.generate_art("a rocket", tmp_path / "art.jpg", 1600, 900)
    assert out == tmp_path / "art.jpg" and Image.open(out).size == (1600, 900)
    assert seen[0].headers["authorization"] == "Bearer nvapi-test"
    assert seen[0].url.path.endswith("/flux.2-klein-4b")
    assert json.loads(seen[0].content)["prompt"] == "a rocket"


@pytest.mark.parametrize("response,expected_calls", [
    (httpx.Response(500, text="boom"), 2),
    (httpx.Response(504, text="errored"), 2),
    (httpx.Response(200, text="not json"), 2),
    # A content-filter refusal is final for that prompt: repeating it cannot help.
    (httpx.Response(200, json={"artifacts": [{"base64": "", "finishReason": "CONTENT_FILTERED"}]}), 1),
])
def test_any_failure_returns_none(monkeypatch, tmp_path, response, expected_calls):
    calls = []
    install(monkeypatch, lambda r: calls.append(r) or response)
    assert nv.generate_art("x", tmp_path / "art.jpg") is None
    assert len(calls) == expected_calls and not (tmp_path / "art.jpg").exists()


def test_a_network_error_returns_none(monkeypatch, tmp_path):
    def boom(request): raise httpx.ConnectError("down")
    install(monkeypatch, boom)
    assert nv.generate_art("x", tmp_path / "art.jpg") is None


def test_no_key_or_switched_off_makes_no_request(monkeypatch, tmp_path):
    def never(request): raise AssertionError("must not call the API")
    for settings in (SimpleNamespace(nvidia_api_key="", ai_images=True, nvidia_image_model="m"),
                     SimpleNamespace(nvidia_api_key="k", ai_images=False, nvidia_image_model="m")):
        install(monkeypatch, never, settings)
        assert nv.generate_art("x", tmp_path / "art.jpg") is None


def test_fit_cover_crops_instead_of_stretching():
    wide = Image.new("RGB", (2000, 500), (255, 0, 0))
    assert nv.fit_cover(wide, 1600, 900).size == (1600, 900)


# ---------- titled card over a picture ----------

def test_card_draws_over_a_background_picture_and_keeps_text_readable(tmp_path):
    bg = tmp_path / "bg.jpg"; Image.new("RGB", (1600, 900), (255, 255, 255)).save(bg)   # worst case: white
    out = PillowEditorialCard().generate(
        ImageRequest(headline="A headline", entity="Ent", background=str(bg)), tmp_path / "hero.jpg")
    img = Image.open(out)
    assert img.size == (1200, 675)
    assert sum(img.getpixel((60, 600))) < 3 * 128      # left/lower area was darkened under the title


# ---------- pipeline ----------

class FakeImage:
    def __init__(self): self.backgrounds = []
    def generate(self, req, out_path):
        self.backgrounds.append(req.background)
        Image.new("RGB", (req.width, req.height), (10, 20, 30)).save(out_path); return out_path


class NoTTS:
    name = "x"
    def synthesize(self, *a, **k): raise AssertionError("no voice in articles-only mode")


def run_pipeline(monkeypatch, tmp_path, art_ok):
    image = FakeImage()
    monkeypatch.setattr(assets, "load_providers", lambda: SimpleNamespace(image=image, tts=NoTTS(), broll=None, used={}))
    monkeypatch.setattr(assets, "get_settings", lambda: SimpleNamespace(media_path=tmp_path, generate_video=False))
    if art_ok:
        install(monkeypatch, ok)
    result = assets.produce_assets(5, {"title": "T", "dek": "d"}, SCRIPT, {"category": "Technology"}, entity="Apple")
    return result, image, tmp_path / "5"


def test_with_ai_art_the_cover_is_the_picture_and_the_source_is_recorded(monkeypatch, tmp_path):
    result, image, d = run_pipeline(monkeypatch, tmp_path, art_ok=True)
    assert (d / "cover.source").read_text() == "ai"
    assert image.backgrounds == [str(d / "art.jpg")]                 # the titled card sits on the picture
    assert Image.open(d / "cover.jpg").getpixel((800, 450))[0] > 150  # the orange test picture, not a gradient


def test_when_art_fails_the_pipeline_still_finishes_with_the_gradient(monkeypatch, tmp_path):
    install(monkeypatch, lambda r: httpx.Response(500))
    result, image, d = run_pipeline(monkeypatch, tmp_path, art_ok=False)
    assert (d / "cover.source").read_text() == "gradient"
    assert image.backgrounds == [""] and result.cover_image.exists() and result.hero_image.exists()


def test_a_content_filter_refusal_falls_back_to_the_generic_scene(monkeypatch, tmp_path):
    prompts = []
    def handler(request):
        prompts.append(json.loads(request.content)["prompt"])
        if "Nvidia" in prompts[-1]:
            return httpx.Response(200, json={"artifacts": [{"base64": "", "finishReason": "CONTENT_FILTERED"}]})
        return ok(request)
    install(monkeypatch, handler)
    out = nv.generate_art("Nvidia logo", tmp_path / "art.jpg", fallback_prompt=nv.generic_prompt("Technology"))
    assert out is not None and len(prompts) == 2           # the refusal is NOT retried; the fallback is used
    assert "data center" in prompts[1] and "Nvidia" not in prompts[1]


def test_generic_prompts_exist_for_every_category_and_name_nothing_from_the_story():
    from app.categories import NAMES
    for name in NAMES:
        p = nv.generic_prompt(name)
        assert name in nv._GENERIC_SCENES and "no logos" in p


# ---------- the label painted onto the picture ----------

def _render(tmp_path, label, name):
    bg = tmp_path / "bg.jpg"; Image.new("RGB", (1600, 900), (230, 230, 230)).save(bg)          # a pale picture: worst case for a label
    out = PillowEditorialCard().generate(
        ImageRequest(headline="A headline", entity="Ent", background=str(bg), label=label), tmp_path / name)
    return Image.open(out).convert("RGB")


def test_the_label_is_painted_in_the_top_right_corner_and_only_when_asked(tmp_path):
    with_label, without = _render(tmp_path, "AI-generated illustration", "a.jpg"), _render(tmp_path, "", "b.jpg")
    w, h = with_label.size
    corner = (int(w * 0.62), 0, w, int(h * 0.14))
    assert with_label.crop(corner).tobytes() != without.crop(corner).tobytes()               # something was drawn there
    dark = [p for p in with_label.crop(corner).getdata() if sum(p) < 60]
    white = [p for p in with_label.crop(corner).getdata() if sum(p) > 740]
    assert len(dark) > 500 and len(white) > 60                                                # a dark pill with white text
    rest = (0, int(h * 0.20), w, h)                                                           # nothing else changed
    assert with_label.crop(rest).tobytes() == without.crop(rest).tobytes()


def test_the_pipeline_labels_the_card_when_it_sits_on_an_ai_picture(monkeypatch, tmp_path):
    seen = []
    class Spy(FakeImage):
        def generate(self, req, out_path):
            seen.append((req.background, req.label)); return super().generate(req, out_path)
    providers = SimpleNamespace(image=Spy(), tts=NoTTS(), broll=None, used={})
    monkeypatch.setattr(assets, "load_providers", lambda: providers)
    monkeypatch.setattr(assets, "get_settings", lambda: SimpleNamespace(media_path=tmp_path, generate_video=False))
    install(monkeypatch, ok)
    assets.produce_assets(6, {"title": "T", "dek": "d"}, SCRIPT, {"category": "Technology"}, entity="Apple")
    assert seen[0][1] == "AI-generated illustration" and seen[0][0].endswith("art.jpg")

    seen.clear()
    install(monkeypatch, lambda r: httpx.Response(500))                                       # art fails -> gradient card
    assets.produce_assets(7, {"title": "T", "dek": "d"}, SCRIPT, {"category": "Technology"}, entity="Apple")
    assert seen[0] == ("", "")                                                                # no picture to label, so no label
