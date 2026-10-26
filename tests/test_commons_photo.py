"""Wikimedia Commons file photos: who the story is about, which article is meant, which licences are allowed, and how
the picture is credited. Fake Wikipedia/Commons only: no network. The most important tests are about being WRONG
(a different person with the same name, a quoted expert, a logo, a licence that forbids this use)."""
import io
import json
import random
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.assets import pipeline as assets
from app.providers import commons_photo as cp
from app.providers import source_photo as sp

SEED = random.Random(11)


def photo(w=1800, h=1200, mime="JPEG") -> bytes:
    img = Image.new("RGB", (w, h), (200, 200, 200))
    for x in range(0, w, 100):
        for y in range(0, h, 100):
            img.paste((SEED.randrange(256), SEED.randrange(256), SEED.randrange(256)), (x, y, x + 100, y + 100))
    buf = io.BytesIO(); img.save(buf, mime); return buf.getvalue()


class Wiki:
    """A fake Wikipedia + Commons. articles: search text -> list of pages; files: file name -> facts."""
    def __init__(self, articles=None, files=None, image=None):
        self.articles, self.files, self.image, self.calls = articles or {}, files or {}, image or photo(), []

    def client(self):
        def handler(request: httpx.Request):
            url, q = str(request.url), dict(request.url.params)
            self.calls.append(url)
            if request.url.host == "en.wikipedia.org":
                pages = {str(i): {**p, "index": i + 1} for i, p in enumerate(self.articles.get(q["gsrsearch"], []))}
                return httpx.Response(200, json={"query": {"pages": pages}})
            if request.url.host == "commons.wikimedia.org":
                name = q["titles"].removeprefix("File:")
                f = self.files.get(name)
                if not f:
                    return httpx.Response(200, json={"query": {"pages": {"-1": {"title": q["titles"], "missing": ""}}}})
                meta = {"LicenseShortName": {"value": f.get("licence", "CC BY 4.0")}, "Artist": {"value": f.get("artist", "Jane Doe")},
                        "NonFree": {"value": "true" if f.get("nonfree") else ""}}
                info = {"url": f"https://upload.example/full/{name}", "thumburl": f"https://upload.example/thumb/{name}",
                        "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}", "width": f.get("w", 1800), "height": f.get("h", 1200),
                        "mime": f.get("mime", "image/jpeg"), "extmetadata": meta}
                return httpx.Response(200, json={"query": {"pages": {"1": {"title": q["titles"], "imageinfo": [info]}}}})
            if request.url.host == "upload.example":
                return httpx.Response(200, content=self.image, headers={"content-type": "image/jpeg"})
            return httpx.Response(404)
        return httpx.Client(transport=httpx.MockTransport(handler))


def find(monkeypatch, tmp_path, wiki, headline="Elon Musk wins award", body="Elon Musk won an award on Monday.", keywords=(), entity="Elon Musk", **kw):
    monkeypatch.setattr(cp, "_http_client", wiki.client())
    return cp.find_commons_photo(headline, body, list(keywords), entity, tmp_path / "photo.jpg", **kw)


def musk(file="Elon Musk 2015.jpg", extract="Elon Musk is a businessman."):
    return {"Elon Musk": [{"title": "Elon Musk", "pageimage": file, "extract": extract, "description": "businessman"}]}


# ---------- the credit ----------

def test_credits_the_author_licence_and_commons_and_links_the_file_page(monkeypatch, tmp_path):
    p = find(monkeypatch, tmp_path, Wiki(musk(), {"Elon Musk 2015.jpg": {"artist": "Steve Jurvetson", "licence": "CC BY 2.0"}}))
    assert p.credit == "File photo. Photo: Steve Jurvetson / Wikimedia Commons (CC BY 2.0), cropped"
    assert p.label == "File photo · Steve Jurvetson / Wikimedia Commons"
    assert p.page_url == "https://commons.wikimedia.org/wiki/File:Elon Musk 2015.jpg"
    assert p.subject == "Elon Musk" and Image.open(p.path).size == (1600, 900)


def test_a_long_author_name_is_shortened_in_the_label_not_lost_in_the_credit(monkeypatch, tmp_path):
    p = find(monkeypatch, tmp_path, Wiki(musk(), {"Elon Musk 2015.jpg": {"artist": "A Very Long Photographer Name Indeed Esq."}}))
    assert "Indeed Esq." in p.credit and len(p.label) < 60 and p.label.endswith("/ Wikimedia Commons")


# ---------- the right person ----------

CTX = dict(body="Michael Gray, who played Billy Batson in the Shazam! series, has died aged 75.", headline="Michael Gray, Star of 'Shazam!', Dies at 75")
PAGES = {"Michael Gray": [
    {"title": "Michael Gray (footballer)", "pageimage": "Footballer.jpg", "extract": "Michael Gray is an English football coach and former defender.", "description": "English footballer"},
    {"title": "Michael Gray (actor)", "pageimage": "Actor.jpg", "extract": "Michael Gray is an American former child actor known for playing Billy Batson in the television series Shazam!", "description": "American actor"},
    {"title": "Michael Gray (singer)", "pageimage": "Singer.jpg", "extract": "Michael Gray is an English disco singer.", "description": "English singer"}]}
FILES = {n: {"artist": "Someone"} for n in ("Footballer.jpg", "Actor.jpg", "Singer.jpg")}


def test_picks_between_people_with_the_same_name_by_the_stories_own_words(monkeypatch, tmp_path):
    p = find(monkeypatch, tmp_path, Wiki(PAGES, FILES), **CTX)
    assert p.subject == "Michael Gray (actor)"


def test_with_several_same_named_people_and_no_clue_it_does_not_guess(monkeypatch, tmp_path):
    p = find(monkeypatch, tmp_path, Wiki(PAGES, FILES), headline="Michael Gray dies at 75", body="Michael Gray has died aged 75.")
    assert p is None


def test_a_disambiguation_page_is_never_used(monkeypatch, tmp_path):
    pages = {"Elon Musk": [{"title": "Elon Musk (disambiguation)", "pageimage": "x.jpg", "extract": "Elon Musk may refer to:", "description": ""}]}
    assert find(monkeypatch, tmp_path, Wiki(pages, {"x.jpg": {}})) is None


def test_an_article_that_merely_contains_the_name_is_not_the_subject(monkeypatch, tmp_path):
    """Seen live: 'Orders Trump' matched 'List of executive orders in the second Trump presidency'."""
    pages = {"Donald Trump": [{"title": "List of executive orders in the second Trump presidency", "pageimage": "List.jpg", "extract": "x", "description": ""}]}
    assert find(monkeypatch, tmp_path, Wiki(pages, {"List.jpg": {}}), headline="Trump signs order", body="Donald Trump signed an order.") is None


# ---------- licences ----------

@pytest.mark.parametrize("licence,artist,ok", [
    ("CC BY 4.0", "Jane Doe", True), ("CC BY 2.0", "Jane Doe", True), ("CC0", "", True), ("Public domain", "", True),
    ("CC BY-SA 4.0", "Jane Doe", False), ("CC BY-NC 2.0", "Jane Doe", False), ("CC BY-ND 2.0", "Jane Doe", False),
    ("CC BY-NC-SA 3.0", "Jane Doe", False), ("GFDL", "Jane Doe", False), ("Fair use", "Jane Doe", False),
    ("CC BY 4.0", "", False),                # attribution is the condition of the licence: no author, no use
])
def test_only_licences_that_allow_this_use_are_accepted(monkeypatch, tmp_path, licence, artist, ok):
    p = find(monkeypatch, tmp_path, Wiki(musk(), {"Elon Musk 2015.jpg": {"licence": licence, "artist": artist}}))
    assert bool(p) is ok, licence


def test_share_alike_is_allowed_only_when_switched_on(monkeypatch, tmp_path):
    files = {"Elon Musk 2015.jpg": {"licence": "CC BY-SA 3.0"}}
    assert find(monkeypatch, tmp_path, Wiki(musk(), files)) is None
    assert find(monkeypatch, tmp_path, Wiki(musk(), files), allow_sharealike=True) is not None


def test_non_free_and_missing_files_are_refused(monkeypatch, tmp_path):
    assert find(monkeypatch, tmp_path, Wiki(musk(), {"Elon Musk 2015.jpg": {"nonfree": True}})) is None
    assert find(monkeypatch, tmp_path, Wiki(musk(), {})) is None          # lives only on Wikipedia (fair use), not on Commons


# ---------- pictures that are not a photo of the subject ----------

@pytest.mark.parametrize("name", ["Starlink Logo 2024.svg", "Flag of Tibet.png", "Map of Ukraine.png", "Seal of Meta.png", "Coat of arms.jpg", "Diagram.png", "Chart.png"])
def test_logos_flags_maps_and_diagrams_are_not_used(monkeypatch, tmp_path, name):
    assert find(monkeypatch, tmp_path, Wiki(musk(name), {name: {}})) is None


@pytest.mark.parametrize("w,h", [(500, 400), (900, 500), (500, 900), (3000, 400)])
def test_small_or_oddly_shaped_images_are_refused(monkeypatch, tmp_path, w, h):
    wiki = Wiki(musk(), {"Elon Musk 2015.jpg": {"w": w, "h": h}}, image=photo(w, h))
    assert find(monkeypatch, tmp_path, wiki) is None


def test_a_portrait_is_shown_whole_over_a_blurred_backdrop_not_cropped_through_the_face():
    tall = Image.new("RGB", (800, 1200), (250, 0, 0))                      # solid red portrait
    out = cp.to_cover(tall)
    assert out.size == (1600, 900)
    assert out.getpixel((800, 450))[0] > 200 and out.getpixel((800, 5))[0] > 200     # the whole portrait is there, top to bottom
    left = out.getpixel((60, 450))
    assert left[0] < 160                                                     # the sides are the darkened backdrop, not the photo


def test_a_landscape_photo_is_centre_cropped_to_16_by_9():
    wide = Image.new("RGB", (2000, 1000), (0, 200, 0))
    out = cp.to_cover(wide)
    assert out.size == (1600, 900) and out.getpixel((10, 10))[1] > 150       # fills the frame edge to edge


# ---------- who is the story about ----------

def subjects(headline, body, entity=""):
    return [(s.name, s.label) for s in cp.subjects_of(headline, body, entity)]


def test_the_subject_is_whoever_the_headline_names_not_whoever_the_story_quotes():
    body = "Donald Trump said Elon Musk had restored Starlink in Ukraine. Premier Li Qiang ordered rescue teams. Dr Tom Solomon told reporters it was serious."
    names = [n for n, _ in subjects("Trump Says Musk Restored Starlink", body)]
    assert "Donald Trump" in names and "Elon Musk" in names
    assert "Li Qiang" not in names and "Tom Solomon" not in names


def test_a_lone_surname_in_the_headline_becomes_the_full_name_from_the_text():
    assert ("Donald Trump", "PERSON") in subjects("Trump signs the bill", "Donald Trump signed the bill on Friday. Donald Trump said it was great.")


def test_a_surname_that_could_mean_two_people_is_not_guessed():
    body = "Donald Trump spoke on Friday. Melania Trump also spoke. Donald Trump left."
    assert [n for n, _ in subjects("Trump speaks on Friday", body) if "Trump" in n] == []
    assert ("Melania Trump", "PERSON") in subjects("Melania Trump speaks", body)


def test_title_case_headline_fragments_are_never_read_as_names():
    names = [n for n, _ in subjects("Judge Orders Trump to Sit for Deposition", "Donald Trump must sit for a deposition, a judge ordered.")]
    assert "Orders Trump" not in names and "Donald Trump" in names


def test_countries_and_continents_are_too_generic_to_illustrate_an_event():
    assert subjects("First Human Case of Virus Detected in France", "A patient in France died. France confirmed the case.") == []


def test_people_come_before_organisations_and_places():
    body = "Meta said Mark Zuckerberg would sign. Mark Zuckerberg leads Meta, based in Menlo Park."
    assert [l for _n, l in subjects("Meta and Zuckerberg announce plan", body)][:2] == ["PERSON", "ORG"]


# ---------- the pipeline ----------

A = "https://news-a.test/story"


class FakeImage:
    def __init__(self): self.requests = []
    def generate(self, req, out_path):
        self.requests.append(req); Image.new("RGB", (req.width, req.height), (10, 20, 30)).save(out_path); return Path(out_path)


class NoTTS:
    name = "x"
    def synthesize(self, *a, **k): raise AssertionError("no voice")


def produce(monkeypatch, tmp_path, wiki, *, metadata=None, source_page=None, art_ok=False, allow=False):
    image = FakeImage()
    monkeypatch.setattr(assets, "load_providers", lambda: SimpleNamespace(image=image, tts=NoTTS(), broll=None, used={}))
    monkeypatch.setattr(assets, "get_settings", lambda: SimpleNamespace(media_path=tmp_path, generate_video=False, cover_image_mode="source", commons_allow_sharealike=allow))
    monkeypatch.setattr(cp, "_http_client", wiki.client())
    pages = {A: (200, source_page)} if source_page else {}
    def src_handler(request):
        url = str(request.url)
        if url in pages: return httpx.Response(200, text=pages[url][1], headers={"content-type": "text/html"})
        if url.endswith("lead.jpg"): return httpx.Response(200, content=photo(1600, 900), headers={"content-type": "image/jpeg"})
        return httpx.Response(404)
    monkeypatch.setattr(sp, "_http_client", httpx.Client(transport=httpx.MockTransport(src_handler)))
    ai_calls = []
    def fake_art(prompt, out, *a, **k):
        ai_calls.append(prompt)
        if not art_ok: return None
        Image.new("RGB", (1600, 900), (200, 80, 20)).save(out); return out
    monkeypatch.setattr(assets, "generate_art", fake_art)
    article = {"title": "Elon Musk wins award", "dek": "Elon Musk won an award on Monday.", "sections": [{"heading": "h", "body_markdown": "Elon Musk won an award."}]}
    result = assets.produce_assets(5, article, {"beats": []}, metadata or {"category": "Technology", "target_keywords": []}, entity="Elon Musk", sources=[{"url": A}])
    return image, ai_calls, tmp_path / "5"


def test_with_no_safe_source_photo_a_file_photo_of_the_subject_is_used_and_credited(monkeypatch, tmp_path):
    image, ai_calls, out = produce(monkeypatch, tmp_path, Wiki(musk(), {"Elon Musk 2015.jpg": {"artist": "Steve Jurvetson", "licence": "CC BY 2.0"}}))
    assert (out / "cover.source").read_text() == "commons" and ai_calls == []
    info = json.loads((out / "cover.credit.json").read_text())
    assert info["credit"] == "File photo. Photo: Steve Jurvetson / Wikimedia Commons (CC BY 2.0), cropped"
    assert info["url"].startswith("https://commons.wikimedia.org/wiki/File:") and info["licence"] == "CC BY 2.0" and info["subject"] == "Elon Musk"
    assert image.requests[0].label == "File photo · Steve Jurvetson / Wikimedia Commons"


def test_a_good_source_photo_beats_a_file_photo(monkeypatch, tmp_path):
    page = '<html><head><meta property="og:site_name" content="The Example"><meta property="og:image" content="https://img.example.test/lead.jpg"></head></html>'
    wiki = Wiki(musk(), {"Elon Musk 2015.jpg": {}})
    image, _calls, out = produce(monkeypatch, tmp_path, wiki, source_page=page)
    assert (out / "cover.source").read_text() == "source"
    assert not any("wikipedia" in c for c in wiki.calls)                  # Commons was never even asked


def test_with_neither_the_ai_illustration_is_the_fallback(monkeypatch, tmp_path):
    image, ai_calls, out = produce(monkeypatch, tmp_path, Wiki({}, {}), art_ok=True)
    assert (out / "cover.source").read_text() == "ai" and len(ai_calls) == 1 and not (out / "cover.credit.json").exists()


def test_a_sensitive_story_never_gets_a_real_photograph_of_anyone(monkeypatch, tmp_path):
    wiki = Wiki(musk(), {"Elon Musk 2015.jpg": {}})
    image, ai_calls, out = produce(monkeypatch, tmp_path, wiki, art_ok=True, metadata={"category": "Politics", "sensitive": {"reasons": ["x"], "names": []}})
    assert wiki.calls == [] and (out / "cover.source").read_text() == "ai"


def test_the_share_alike_setting_reaches_the_finder(monkeypatch, tmp_path):
    files = {"Elon Musk 2015.jpg": {"licence": "CC BY-SA 4.0"}}
    assert (produce(monkeypatch, tmp_path / "a", Wiki(musk(), files), art_ok=True)[2] / "cover.source").read_text() == "ai"
    assert (produce(monkeypatch, tmp_path / "b", Wiki(musk(), files), allow=True)[2] / "cover.source").read_text() == "commons"
