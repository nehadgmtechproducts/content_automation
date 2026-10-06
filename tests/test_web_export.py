from datetime import datetime, timezone

from app.publishers.web import MediaFacts, build_web_article, split_category, trim_description

ARTICLE = {
    "slug": "apple-full-disk-access", "title": "Apple Tightens Disk Access", "dek": "A dek.",
    "meta_description": "Short and fine.",
    "sections": [{"heading": "H1", "body_markdown": "Body [1]."}],
    "key_takeaways": ["One"], "why_it_matters": "Because [2].",
}
META = {"category": "Technology / Privacy", "target_keywords": ["Apple", "macOS"], "hashtags": ["#Apple"]}
SOURCES = [
    {"index": 1, "title": "A", "domain": "a.com", "url": "https://a.com/x",
     "published_at": datetime(2026, 10, 2, tzinfo=timezone.utc)},
    {"index": 2, "title": "B", "domain": "b.com", "url": "https://b.com/y", "published_at": None},
]
MEDIA = MediaFacts(1200, 675, 1080, 1920, 42.766)
WHEN = datetime(2026, 10, 5, 12, 12, 52, tzinfo=timezone.utc)


def build(**over):
    kw = dict(topic_id=6, entity="Apple", article=ARTICLE, meta=META, sources=SOURCES,
              published_at=WHEN, media=MEDIA)
    kw.update(over)
    return build_web_article(**kw)


def test_category_is_split_into_one_category_plus_keyword():
    assert split_category("Technology / Privacy") == ("Technology", ["Privacy"])
    assert split_category("") == ("Technology", [])
    doc = build()
    assert doc["category"] == "Technology"
    assert doc["keywords"] == ["Apple", "macOS", "Privacy"]


def test_media_paths_and_dimensions_use_slug_and_probed_values():
    doc = build()
    assert doc["heroImage"]["src"] == "/media/apple-full-disk-access/hero.jpg"
    assert doc["video"]["poster"] == "/media/apple-full-disk-access/poster.jpg"
    assert doc["video"]["durationSeconds"] == 42.77
    assert (doc["video"]["width"], doc["video"]["height"]) == (1080, 1920)


def test_timestamps_are_utc_iso_and_source_dates_are_plain_dates():
    doc = build()
    assert doc["publishedAt"] == "2026-10-05T12:12:52Z"
    assert doc["sources"][0]["publishedAt"] == "2026-10-02"
    assert doc["sources"][1]["publishedAt"] is None


def test_sections_are_renamed_to_the_web_shape():
    doc = build()
    assert doc["sections"] == [{"heading": "H1", "body": "Body [1]."}]
    assert doc["schemaVersion"] == 1 and doc["id"] == "topic-6"


def test_long_meta_description_is_cut_at_a_sentence_not_mid_word():
    long = ("Nvidia's new platform combines open-source OpenShell with hardware-based Sentry "
            "to keep AI agents out of unauthorized systems. It follows breaches by agents "
            "from several large AI labs.")
    out = trim_description(long)
    assert len(out) <= 160
    assert out.endswith("systems.")


def test_short_first_sentence_prefers_the_fuller_word_boundary_cut():
    long = "Short first sentence here. " + "more words follow " * 15
    out = trim_description(long)
    assert len(out) <= 161 and out.endswith("…")


def test_long_description_without_sentence_break_cuts_on_a_word():
    out = trim_description("word " * 80)
    assert len(out) <= 161 and out.endswith("…") and "wor…" not in out
