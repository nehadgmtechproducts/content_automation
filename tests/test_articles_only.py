"""Articles-only mode (GENERATE_VIDEO off, the default): the pipeline makes images and a
reviewable Telegram card, and never touches voice / b-roll / ffmpeg.

All Telegram traffic goes to a fake client — nothing here sends a real message.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db as db
from app import pipeline
from app.approval import service
from app.approval.cards import build_article_messages
from app.assets import pipeline as assets
from app.models import (
    ApprovalCard, AssetBundle, Base, Draft, ScrapedArticle, Topic, TopicStatus,
)
from app.publishers import web
from app.settings import Settings

ARTICLE = {
    "slug": "apple-disk", "title": "Apple tightens disk access", "dek": "A short dek.",
    "meta_description": "Short.",
    "sections": [
        {"heading": "What changed", "body_markdown": "Apple **restricted** access [1].\n\nSecond paragraph [2]."},
        {"heading": "Reaction", "body_markdown": "See [the docs](https://example.com/docs)."},
    ],
    "key_takeaways": ["First", "Second"], "why_it_matters": "It matters [1].",
}
META = {"category": "Technology", "target_keywords": ["Apple"], "hashtags": ["#a"]}
CHAT = "-100123"


# ---------- settings ----------

def test_video_generation_is_off_by_default():
    assert Settings.model_fields["generate_video"].default is False


# ---------- produce_assets ----------

class FakeImage:
    def generate(self, req, out_path):
        Image.new("RGB", (req.width, req.height), (10, 20, 30)).save(out_path)
        return Path(out_path)


class ExplodingTTS:
    name = "boom"

    def synthesize(self, *_a, **_k):
        raise AssertionError("TTS must not run in articles-only mode")


@pytest.fixture()
def media(monkeypatch, tmp_path):
    providers = SimpleNamespace(image=FakeImage(), tts=ExplodingTTS(), broll=None, used={"image": "fake"})
    monkeypatch.setattr(assets, "load_providers", lambda: providers)
    return tmp_path


def settings_for(tmp_path, generate_video):
    return lambda: SimpleNamespace(media_path=tmp_path, generate_video=generate_video)


def test_articles_only_makes_images_and_nothing_else(monkeypatch, media):
    monkeypatch.setattr(assets, "get_settings", settings_for(media, False))
    result = assets.produce_assets(7, ARTICLE, {"beats": [{"narration": "x"}]}, META, entity="Apple")

    assert sorted(p.name for p in (media / "7").iterdir()) == ["cover.jpg", "cover.source", "hero.jpg", "package.json"]
    assert result.video is None and result.voiceover is None and result.subtitles is None
    assert result.hero_vertical is None and result.duration == 0.0
    assert result.hero_image.name == "hero.jpg" and result.cover_image.name == "cover.jpg"
    package = json.loads((media / "7" / "package.json").read_text())
    assert package["assets"] == {"hero": "hero.jpg", "cover": "cover.jpg", "art": None, "art_kind": None}
    assert "video_info" not in package


def test_video_mode_still_reaches_the_voiceover_step(monkeypatch, media):
    monkeypatch.setattr(assets, "get_settings", settings_for(media, True))
    with pytest.raises(AssertionError, match="TTS must not run"):
        assets.produce_assets(8, ARTICLE, {"beats": [{"narration": "hello there"}]}, META, entity="Apple")
    assert (media / "8" / "hero_vertical.jpg").exists()   # the video's opening card is still made


# ---------- database helpers ----------

@pytest.fixture()
def database(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "_SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))


def add_topic(status, video=""):
    with db.session_scope() as s:
        t = Topic(title="T", primary_entity="Apple", status=status)
        s.add(t); s.flush()
        s.add(Draft(topic_id=t.id, article=ARTICLE, script={}, meta=META))
        s.add(AssetBundle(topic_id=t.id, directory="/m", hero_image="/m/hero.jpg", video=video))
        for i in (1, 2):
            s.add(ScrapedArticle(topic_id=t.id, source_index=i, url=f"https://s{i}.com/a",
                                 domain=f"s{i}.com", title=f"Src {i}", markdown="m", scraper="x"))
        return t.id


# ---------- run_assets ----------

def test_run_assets_stores_empty_paths_when_there_is_no_video(database, monkeypatch):
    with db.session_scope() as s:
        t = Topic(title="T", primary_entity="E", status=TopicStatus.synthesized)
        s.add(t); s.flush(); tid = t.id
        s.add(Draft(topic_id=tid, article={"title": "A"}, script={}, meta={}))
    p = Path("/tmp/x")
    monkeypatch.setattr(pipeline, "produce_assets", lambda *a, **k: SimpleNamespace(
        directory=p, hero_image=p, hero_vertical=None, voiceover=None, subtitles=None,
        video=None, duration=0.0, providers_used={}))
    monkeypatch.setattr(pipeline.approval_service, "is_configured", lambda: False)

    out = pipeline.run_assets(tid)

    assert out["ok"] is True and out["video"] is None
    with db.session_scope() as s:
        bundle = s.query(AssetBundle).filter_by(topic_id=tid).one()
        assert (bundle.video, bundle.voiceover, bundle.subtitles) == ("", "", "")
        assert s.get(Topic, tid).status == TopicStatus.assets_ready


# ---------- the Telegram review card ----------

def test_article_messages_hold_the_whole_article_within_the_limit():
    messages = build_article_messages(ARTICLE, [{"index": 1, "title": "Src 1", "url": "https://s1.com/a"}])
    joined = "\n".join(messages)
    for needle in ("Apple tightens disk access", "A short dek.", "What changed", "Second paragraph",
                   "First", "Why it matters", "https://s1.com/a"):
        assert needle in joined
    assert "**" not in joined                                   # markdown markers removed
    assert "the docs (https://example.com/docs)" in joined      # links keep their URL
    assert all(len(m) <= 4096 for m in messages)


def test_long_articles_are_split_without_losing_a_word():
    long = {"title": "T", "sections": [{"heading": "H", "body_markdown": "\n\n".join(
        " ".join(f"w{i}x{j}" for j in range(120)) for i in range(60))}]}
    messages = build_article_messages(long, limit=1000)
    assert len(messages) > 1 and all(len(m) <= 1000 for m in messages)
    sent = " ".join(" ".join(messages).split())
    expected = [f"w{i}x{j}" for i in range(60) for j in range(120)]
    assert [w for w in sent.split() if w.startswith("w")] == expected


def test_an_overlong_unbroken_token_is_still_cut_to_the_limit():
    messages = build_article_messages({"title": "T", "dek": "z" * 2500}, limit=1000)
    assert all(len(m) <= 1000 for m in messages)
    assert sum(m.count("z") for m in messages) == 2500


class RecordingClient:
    def __init__(self):
        self.sent = []

    def send_message(self, chat, text, reply_markup=None):
        self.sent.append(("message", text, reply_markup)); return {"message_id": len(self.sent)}

    def send_photo(self, chat, path, caption="", reply_markup=None):
        self.sent.append(("photo", caption, reply_markup)); return {"message_id": len(self.sent)}

    def send_video(self, chat, path, caption="", reply_markup=None):
        self.sent.append(("video", caption, reply_markup)); return {"message_id": len(self.sent)}


@pytest.fixture()
def telegram(monkeypatch):
    monkeypatch.setattr(service, "is_configured", lambda: True)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(telegram_chat_id=CHAT))


def test_card_without_video_is_article_then_metadata_then_photo_with_buttons(database, telegram):
    tid = add_topic(TopicStatus.assets_ready, video="")
    client = RecordingClient()

    out = service.push_for_approval(tid, client)

    kinds = [k for k, _t, _m in client.sent]
    assert kinds[-1] == "photo" and "video" not in kinds and kinds[0] == "message"
    assert "Apple tightens disk access" in client.sent[0][1]
    assert client.sent[-1][2] is not None                       # buttons are on the photo
    assert all(m is None for _k, _t, m in client.sent[:-1])     # and nowhere else
    assert out == {"sent": True, "message_id": len(client.sent)}
    with db.session_scope() as s:
        card = s.query(ApprovalCard).filter_by(topic_id=tid).one()
        assert card.message_id == len(client.sent)              # the message later edited on a decision
        assert s.get(Topic, tid).status == TopicStatus.pending_approval


def test_card_with_a_video_keeps_the_original_layout(database, telegram, tmp_path):
    video = tmp_path / "final.mp4"; video.write_bytes(b"v")
    tid = add_topic(TopicStatus.assets_ready, video=str(video))
    client = RecordingClient()

    service.push_for_approval(tid, client)

    assert [k for k, _t, _m in client.sent] == ["photo", "video", "message"]
    assert client.sent[1][2] is not None


# ---------- exporter ----------

def test_export_without_video_files_omits_video(database, tmp_path):
    tid = add_topic(TopicStatus.approved)
    media = tmp_path / "media" / str(tid); media.mkdir(parents=True)
    Image.new("RGB", (1200, 675), (10, 20, 30)).save(media / "hero.jpg")
    (tmp_path / "web" / "content").mkdir(parents=True)

    result = web.export_topic(tid, tmp_path / "web", tmp_path / "media")

    doc = json.loads((tmp_path / "web/content/articles/apple-disk.json").read_text())
    assert "video" not in doc and result["video"] is None
    published = tmp_path / "web/public/media/apple-disk"
    assert (published / Path(doc["heroImage"]["src"]).name).exists()
    assert (published / Path(doc["socialImage"]["src"]).name).exists()
    assert not list(published.glob("video-*")) and not list(published.glob("poster-*"))


def test_export_credits_an_ai_cover_and_not_a_gradient_one(database, tmp_path):
    tid = add_topic(TopicStatus.approved)
    media = tmp_path / "media" / str(tid); media.mkdir(parents=True)
    Image.new("RGB", (1200, 675), (10, 20, 30)).save(media / "hero.jpg")
    (tmp_path / "web" / "content").mkdir(parents=True)

    def exported(source):
        (media / "cover.source").write_text(source)
        web.export_topic(tid, tmp_path / "web", tmp_path / "media")
        return json.loads((tmp_path / "web/content/articles/apple-disk.json").read_text())["heroImage"]

    ai = exported("ai")
    assert ai["credit"] == "Illustration generated with AI" and ai["alt"].startswith("AI-generated illustration")
    plain = exported("gradient")
    assert "credit" not in plain and plain["alt"].startswith("Abstract cover")


def _export_with(tmp_path, kind, credit_file=None):
    tid = add_topic(TopicStatus.approved)
    media = tmp_path / "media" / str(tid); media.mkdir(parents=True)
    Image.new("RGB", (1200, 675), (10, 20, 30)).save(media / "hero.jpg")
    (media / "cover.source").write_text(kind)
    if credit_file is not None:
        (media / "cover.credit.json").write_text(json.dumps(credit_file))
    (tmp_path / "web" / "content").mkdir(parents=True, exist_ok=True)
    web.export_topic(tid, tmp_path / "web", tmp_path / "media")
    return json.loads((tmp_path / "web/content/articles/apple-disk.json").read_text())["heroImage"]


def test_a_source_photo_is_exported_with_its_credit_and_a_link_and_is_not_marked_ai(database, tmp_path):
    hero = _export_with(tmp_path, "source", {"credit": "Image: The Example", "outlet": "The Example", "url": "https://example.test/story"})
    assert hero["credit"] == "Image: The Example" and hero["creditUrl"] == "https://example.test/story"
    assert hero["alt"].startswith("Photo for the story") and "aiGenerated" not in hero


def test_an_ai_cover_is_exported_flagged_as_ai(database, tmp_path):
    hero = _export_with(tmp_path, "ai")
    assert hero["aiGenerated"] is True and hero["credit"] == "Illustration generated with AI" and "creditUrl" not in hero


def test_a_gradient_cover_has_no_credit_and_no_flag(database, tmp_path):
    hero = _export_with(tmp_path, "gradient")
    assert "credit" not in hero and "aiGenerated" not in hero and hero["alt"].startswith("Abstract cover")


def test_a_source_photo_without_its_credit_is_never_published(database, tmp_path):
    """A photograph we cannot credit must not go out: the export refuses instead of guessing."""
    with pytest.raises(web.ExportError, match="cover.credit.json"):
        _export_with(tmp_path, "source", credit_file=None)


def test_a_commons_file_photo_is_exported_with_credit_link_and_a_file_photo_alt(database, tmp_path):
    credit = "File photo. Photo: Steve Jurvetson / Wikimedia Commons (CC BY 2.0), cropped"
    hero = _export_with(tmp_path, "commons", {"credit": credit, "outlet": "Wikimedia Commons",
                                              "url": "https://commons.wikimedia.org/wiki/File:Elon_Musk_2015.jpg", "licence": "CC BY 2.0"})
    assert hero["credit"] == credit and hero["creditUrl"].startswith("https://commons.wikimedia.org/wiki/File:")
    assert hero["alt"].startswith("File photo for the story") and "aiGenerated" not in hero


def test_a_commons_photo_without_its_credit_is_never_published(database, tmp_path):
    with pytest.raises(web.ExportError, match="cover.credit.json"):
        _export_with(tmp_path, "commons", credit_file=None)
