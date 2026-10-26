"""Source photos (COVER_IMAGE_MODE=source): what the finder accepts, what it refuses, and how the pipeline uses it.

Fake pages and fake images only: no network. The safety rules matter most here (agency/stock pictures, logos,
sensitive stories), so each has its own test."""
import io
import json
import random
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.assets import pipeline as assets
from app.providers import image_nvidia as nv
from app.providers import source_photo as sp

SEED = random.Random(7)


def photo_bytes(w=1600, h=900, flat=False, fmt="JPEG") -> bytes:
    img = Image.new("RGB", (w, h), (120, 120, 120))
    if not flat:
        # large blocks of colour, like the large-scale structure of a real photo (per-pixel noise would average to grey)
        for x in range(0, w, 100):
            for y in range(0, h, 100):
                img.paste((SEED.randrange(256), SEED.randrange(256), SEED.randrange(256)), (x, y, x + 100, y + 100))
    buf = io.BytesIO(); img.save(buf, fmt); return buf.getvalue()


def page(images=("https://img.example.test/photos/storm-damage.jpg",), site="The Example", extra=""):
    metas = "".join(f'<meta property="og:image" content="{u}">' for u in images)
    return f'<html><head><meta property="og:site_name" content="{site}">{metas}</head><body>{extra}</body></html>'


class Site:
    """A fake web: page URL -> (status, html); image URL -> bytes."""
    def __init__(self, pages: dict, images: dict | None = None):
        self.pages, self.images, self.fetched = pages, images or {}, []

    def client(self):
        def handler(request: httpx.Request):
            url = str(request.url); self.fetched.append(url)
            if url in self.pages:
                status, html = self.pages[url]
                return httpx.Response(status, text=html, headers={"content-type": "text/html"})
            if url in self.images:
                return httpx.Response(200, content=self.images[url], headers={"content-type": "image/jpeg"})
            return httpx.Response(404)
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def find(monkeypatch, tmp_path, site, sources):
    monkeypatch.setattr(sp, "_http_client", site.client())
    return sp.find_source_photo([{"url": u} for u in sources], tmp_path / "photo.jpg")


A = "https://news-a.test/story"
B = "https://news-b.test/story"
GOOD = "https://img.example.test/photos/storm-damage.jpg"


# ---------- what it accepts ----------

def test_takes_the_lead_photo_credits_the_outlet_and_links_the_page(monkeypatch, tmp_path):
    p = find(monkeypatch, tmp_path, Site({A: (200, page())}, {GOOD: photo_bytes()}), [A])
    assert p and p.credit == "Image: The Example" and p.page_url == A and p.image_url == GOOD
    assert Image.open(p.path).size == (1600, 900)                      # centre-cropped to the cover's size


def test_falls_back_to_the_domain_when_the_page_names_no_outlet(monkeypatch, tmp_path):
    html = page(site="").replace('<meta property="og:site_name" content="">', "")
    p = find(monkeypatch, tmp_path, Site({"https://www.news-a.test/story": (200, html)}, {GOOD: photo_bytes()}), ["https://www.news-a.test/story"])
    assert p.outlet == "news-a.test"


def test_tries_the_next_source_when_one_fails(monkeypatch, tmp_path):
    site = Site({A: (403, "no"), B: (200, page(site="Outlet B"))}, {GOOD: photo_bytes()})
    assert find(monkeypatch, tmp_path, site, [A, B]).outlet == "Outlet B"


def test_uses_the_best_source_first_and_stops_there(monkeypatch, tmp_path):
    site = Site({A: (200, page(site="Outlet A")), B: (200, page(site="Outlet B"))}, {GOOD: photo_bytes()})
    assert find(monkeypatch, tmp_path, site, [A, B]).outlet == "Outlet A"
    assert B not in site.fetched


# ---------- what it refuses: agency and stock pictures ----------

@pytest.mark.parametrize("image_url", [
    "https://image.example.test/api/v1/image/108433320-1792429463943-gettyimages-2300.jpg",
    "https://www.example.test/wp-content/uploads/2026/10/GettyImages-1326039.jpg",
    "https://www.example.test/wp-content/uploads/2026/10/afp_6adab0544c84-1792716884.jpg",
    "https://www.example.test/wp-content/uploads/2026/10/ap_6ada610b9ae68-1792696587.jpg",
    "https://www.example.test/wp-content/uploads/2026/10/reuters_6adb61bd-1792762301.jpg",
    "https://www.example.test/img/shutterstock_2345.jpg",
    "https://cdn.example.test/alamy/abc.jpg",
    "https://cdn.example.test/epa/photo.jpg",
])
def test_agency_and_stock_pictures_are_never_used(monkeypatch, tmp_path, image_url):
    assert find(monkeypatch, tmp_path, Site({A: (200, page([image_url]))}, {image_url: photo_bytes()}), [A]) is None


@pytest.mark.parametrize("name", ["map_view.jpg", "spa-day.jpg", "capital-city.jpg", "apple-store.jpg", "pa.jpg"[:0] + "paper.jpg"])
def test_ordinary_names_that_merely_contain_ap_or_pa_are_not_mistaken_for_agencies(name):
    assert not sp.is_agency_or_stock(f"https://img.example.test/{name}", name)


@pytest.mark.parametrize("caption", ["Photograph: Jane Doe/Reuters", "(AP Photo/Evan Vucci)", "Image: Getty Images", "Photo by AFP", "Shutterstock"])
def test_a_page_whose_lead_caption_names_an_agency_is_skipped(monkeypatch, tmp_path, caption):
    html = page(extra=f"<figure><img src='x'><figcaption>{caption}</figcaption></figure>")
    assert find(monkeypatch, tmp_path, Site({A: (200, html)}, {GOOD: photo_bytes()}), [A]) is None


def test_a_credit_in_the_structured_data_counts_too(monkeypatch, tmp_path):
    ld = json.dumps({"@type": "NewsArticle", "image": {"@type": "ImageObject", "url": GOOD, "creditText": "Getty Images"}})
    html = page(images=[]) .replace("</head>", f'<script type="application/ld+json">{ld}</script></head>')
    assert find(monkeypatch, tmp_path, Site({A: (200, html)}, {GOOD: photo_bytes()}), [A]) is None


def test_one_agency_rendition_condemns_the_whole_page(monkeypatch, tmp_path):
    """Seen live: CNBC offered several renditions of one Getty photo and only some file names said so."""
    clean, tagged = "https://img.example.test/a/108200611-lead-photo.jpg", "https://img.example.test/a/108200611-gettyimages-77.jpg"
    site = Site({A: (200, page([tagged, clean]))}, {clean: photo_bytes(), tagged: photo_bytes()})
    assert find(monkeypatch, tmp_path, site, [A]) is None
    assert clean not in site.fetched                                      # it never even downloaded the lookalike


def test_after_skipping_an_agency_page_it_moves_on_to_the_next_source(monkeypatch, tmp_path):
    bad = "https://img.example.test/ap_1234.jpg"
    site = Site({A: (200, page([bad], site="Agency page")), B: (200, page(site="Clean outlet"))}, {bad: photo_bytes(), GOOD: photo_bytes()})
    assert find(monkeypatch, tmp_path, site, [A, B]).outlet == "Clean outlet"


# ---------- what it refuses: not really a picture of the story ----------

@pytest.mark.parametrize("image_url", [
    "https://img.example.test/assets/site-logo.png", "https://img.example.test/default-share.jpg",
    "https://ichef.example.test/ace/branded_news/1200/cps/abc.jpg", "https://img.example.test/placeholder.jpg",
])
def test_logos_placeholders_and_branded_frames_are_skipped(monkeypatch, tmp_path, image_url):
    assert find(monkeypatch, tmp_path, Site({A: (200, page([image_url]))}, {image_url: photo_bytes()}), [A]) is None


@pytest.mark.parametrize("w,h,flat,why", [(500, 300, False, "too small"), (2400, 400, False, "a banner"), (1600, 900, True, "a flat colour")])
def test_small_strip_and_flat_images_are_rejected(monkeypatch, tmp_path, w, h, flat, why):
    assert find(monkeypatch, tmp_path, Site({A: (200, page())}, {GOOD: photo_bytes(w, h, flat)}), [A]) is None, why


def test_broken_downloads_and_non_images_do_not_crash_it(monkeypatch, tmp_path):
    site = Site({A: (200, page())}, {GOOD: b"<html>not an image</html>"})
    assert find(monkeypatch, tmp_path, site, [A]) is None


# ---------- the pipeline ----------

class FakeImage:
    def __init__(self): self.requests = []
    def generate(self, req, out_path):
        self.requests.append(req); Image.new("RGB", (req.width, req.height), (10, 20, 30)).save(out_path); return Path(out_path)


class NoTTS:
    name = "x"
    def synthesize(self, *a, **k): raise AssertionError("no voice")


def produce(monkeypatch, tmp_path, *, mode="source", site=None, metadata=None, art_ok=False, sources=(A,)):
    image = FakeImage()
    monkeypatch.setattr(assets, "load_providers", lambda: SimpleNamespace(image=image, tts=NoTTS(), broll=None, used={}))
    monkeypatch.setattr(assets, "get_settings", lambda: SimpleNamespace(media_path=tmp_path, generate_video=False, cover_image_mode=mode))
    if site:
        monkeypatch.setattr(sp, "_http_client", site.client())
    ai_calls = []
    def fake_art(prompt, out, *a, **k):
        ai_calls.append(prompt)
        if not art_ok: return None
        Image.new("RGB", (1600, 900), (200, 80, 20)).save(out); return out
    monkeypatch.setattr(assets, "generate_art", fake_art)
    result = assets.produce_assets(9, {"title": "T", "dek": "d"}, {"beats": []}, metadata or {"category": "Politics"}, entity="E",
                                   sources=[{"url": u} for u in sources])
    out = tmp_path / "9"
    return result, image, ai_calls, out


def test_a_source_photo_becomes_the_cover_with_its_credit_recorded(monkeypatch, tmp_path):
    site = Site({A: (200, page(site="The Example"))}, {GOOD: photo_bytes()})
    result, image, ai_calls, out = produce(monkeypatch, tmp_path, site=site)
    assert (out / "cover.source").read_text() == "source" and ai_calls == []        # no AI picture was even requested
    credit = json.loads((out / "cover.credit.json").read_text())
    assert credit == {"credit": "Image: The Example", "outlet": "The Example", "url": A}
    assert image.requests[0].label == "Image: The Example"                          # the share card names the outlet on the picture
    assert image.requests[0].background.endswith("photo.jpg")


def test_with_no_safe_photo_it_falls_back_to_the_ai_illustration(monkeypatch, tmp_path):
    site = Site({A: (200, page(["https://img.example.test/ap_1.jpg"]))}, {"https://img.example.test/ap_1.jpg": photo_bytes()})
    result, image, ai_calls, out = produce(monkeypatch, tmp_path, site=site, art_ok=True)
    assert (out / "cover.source").read_text() == "ai" and len(ai_calls) == 1
    assert image.requests[0].label == "AI-generated illustration"
    assert not (out / "cover.credit.json").exists()


def test_with_neither_it_uses_the_gradient(monkeypatch, tmp_path):
    result, image, ai_calls, out = produce(monkeypatch, tmp_path, site=Site({A: (404, "")}), art_ok=False)
    assert (out / "cover.source").read_text() == "gradient" and image.requests[0].label == ""


def test_a_sensitive_story_never_uses_a_source_photo(monkeypatch, tmp_path):
    """A photograph of a real child or survivor is what the law forbids us to show."""
    site = Site({A: (200, page())}, {GOOD: photo_bytes()})
    result, image, ai_calls, out = produce(monkeypatch, tmp_path, site=site, metadata={"category": "Politics", "sensitive": {"reasons": ["x"], "names": []}}, art_ok=True)
    assert A not in site.fetched and (out / "cover.source").read_text() == "ai"


def test_ai_mode_ignores_the_sources_and_gradient_mode_uses_no_picture(monkeypatch, tmp_path):
    site = Site({A: (200, page())}, {GOOD: photo_bytes()})
    _r, _i, _calls, out = produce(monkeypatch, tmp_path, mode="ai", site=site, art_ok=True)
    assert A not in site.fetched and (out / "cover.source").read_text() == "ai"
    site2 = Site({A: (200, page())}, {GOOD: photo_bytes()})
    _r, _i, calls, out2 = produce(monkeypatch, tmp_path / "g", mode="gradient", site=site2, art_ok=True)
    assert A not in site2.fetched and calls == [] and (out2 / "cover.source").read_text() == "gradient"


def test_a_rerun_does_not_leave_a_stale_credit_behind(monkeypatch, tmp_path):
    produce(monkeypatch, tmp_path, site=Site({A: (200, page())}, {GOOD: photo_bytes()}))
    _r, _i, _c, out = produce(monkeypatch, tmp_path, site=Site({A: (404, "")}), art_ok=True)
    assert (out / "cover.source").read_text() == "ai" and not (out / "cover.credit.json").exists()
