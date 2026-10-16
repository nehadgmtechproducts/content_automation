"""export_topic against an in-memory database and temp folders: the real I/O path, with ffprobe
(needs a real mp4) stubbed. Proves the JSON, the hash-named files and the cleanup agree."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db as db
from app.models import Base, Draft, ScrapedArticle, Topic, TopicStatus
from app.publishers import web

ARTICLE = {
    "slug": "apple-disk", "title": "Apple tightens disk access", "dek": "Dek.", "meta_description": "Short.",
    "sections": [{"heading": "H", "body_markdown": "Body [1]."}], "key_takeaways": ["k"], "why_it_matters": "w [2].",
}
META = {"category": "Technology", "target_keywords": ["Apple"], "hashtags": ["#a"]}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    monkeypatch.setattr(db, "_SessionLocal", sessionmaker(bind=eng, expire_on_commit=False))
    monkeypatch.setattr(web, "_probe_video", lambda p: (1080, 1920, 41.5))

    with db.session_scope() as s:
        t = Topic(title="T", primary_entity="Apple", status=TopicStatus.approved)
        s.add(t); s.flush()
        s.add(Draft(topic_id=t.id, article=ARTICLE, script={}, meta=META,
                    created_at=datetime(2026, 10, 5, 12, tzinfo=timezone.utc)))
        for i in (1, 2):
            s.add(ScrapedArticle(topic_id=t.id, source_index=i, url=f"https://s{i}.com/a", domain=f"s{i}.com",
                                 title=f"Src {i}", markdown="m", scraper="x"))
        tid = t.id

    media = tmp_path / "media" / str(tid); media.mkdir(parents=True)
    Image.new("RGB", (1200, 675), (10, 20, 30)).save(media / "hero.jpg")
    Image.new("RGB", (1080, 1920), (30, 20, 10)).save(media / "hero_vertical.jpg")
    (media / "final.mp4").write_bytes(b"video-v1")
    web_dir = tmp_path / "web"; (web_dir / "content").mkdir(parents=True)
    return {"tid": tid, "media_root": tmp_path / "media", "web": web_dir, "media": media}


def run(env, **kw):
    return web.export_topic(env["tid"], env["web"], env["media_root"], **kw)


def test_export_writes_json_and_hash_named_files_that_match(env):
    result = run(env)
    doc = json.loads((env["web"] / "content/articles/apple-disk.json").read_text())
    published = env["web"] / "public/media/apple-disk"

    for key, path in (("heroImage", doc["heroImage"]["src"]), ("socialImage", doc["socialImage"]["src"]),
                      ("poster", doc["video"]["poster"]), ("video", doc["video"]["src"])):
        assert (published / Path(path).name).exists(), f"{key} -> {path} is not on disk"
        assert Path(path).name.count("-") == 1                      # <stem>-<hash8>.<ext>
    assert (doc["heroImage"]["width"], doc["heroImage"]["height"]) == (1600, 900)
    assert (doc["socialImage"]["width"], doc["socialImage"]["height"]) == (1200, 675)
    assert result["cover"] == Path(doc["heroImage"]["src"]).name


def test_missing_cover_is_generated_and_kept_in_the_backend_media_folder(env):
    assert not (env["media"] / "cover.jpg").exists()
    run(env)
    assert Image.open(env["media"] / "cover.jpg").size == (1600, 900)


def test_re_export_is_stable_and_a_re_render_gets_a_new_url_and_drops_the_old_file(env):
    run(env)
    first = json.loads((env["web"] / "content/articles/apple-disk.json").read_text())
    run(env)                                                         # nothing changed
    again = json.loads((env["web"] / "content/articles/apple-disk.json").read_text())
    assert again == first

    (env["media"] / "final.mp4").write_bytes(b"video-v2-rerendered")
    result = run(env)
    after = json.loads((env["web"] / "content/articles/apple-disk.json").read_text())
    assert after["video"]["src"] != first["video"]["src"]            # new URL => no stale cache
    assert Path(first["video"]["src"]).name in result["pruned"]
    assert not (env["web"] / "public/media/apple-disk" / Path(first["video"]["src"]).name).exists()
    assert (env["web"] / "public/media/apple-disk" / Path(after["video"]["src"]).name).exists()


def test_legacy_unhashed_files_are_cleaned_up(env):
    published = env["web"] / "public/media/apple-disk"; published.mkdir(parents=True)
    for n in ("hero.jpg", "cover.jpg", "social.jpg", "poster.jpg", "video.mp4"):
        (published / n).write_bytes(b"old")
    run(env)
    left = sorted(p.name for p in published.iterdir())
    assert "cover.jpg" not in left and "video.mp4" not in left and "poster.jpg" not in left
    assert "hero.jpg" in left          # not one of our managed names: never touched
    assert len(left) == 5              # hero.jpg + cover/social/poster/video (hashed)


def test_unapproved_topic_is_still_refused(env):
    with db.session_scope() as s:
        s.get(Topic, env["tid"]).status = TopicStatus.synthesized
    with pytest.raises(web.ExportError, match="not approved"):
        run(env)
    run(env, allow_unapproved=True)
