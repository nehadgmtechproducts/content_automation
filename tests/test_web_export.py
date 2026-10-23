from datetime import datetime, timezone

import pytest

from app.publishers.web import ExportError, MediaFacts, MediaPaths, build_web_article, split_category, trim_description

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
MEDIA = MediaFacts(cover_width=1600, cover_height=900, social_width=1200, social_height=675,
                   video_width=1080, video_height=1920, video_seconds=42.766)
PATHS = MediaPaths(cover="/media/apple-full-disk-access/cover-aaaaaaaa.jpg",
                   social="/media/apple-full-disk-access/social-bbbbbbbb.jpg",
                   poster="/media/apple-full-disk-access/poster-cccccccc.jpg",
                   video="/media/apple-full-disk-access/video-dddddddd.mp4")
WHEN = datetime(2026, 10, 5, 12, 12, 52, tzinfo=timezone.utc)


def build(**over):
    kw = dict(topic_id=6, entity="Apple", article=ARTICLE, meta=META, sources=SOURCES,
              published_at=WHEN, media=MEDIA, paths=PATHS)
    kw.update(over)
    return build_web_article(**kw)


def test_category_is_split_into_one_category_plus_keyword():
    assert split_category("Technology / Privacy") == ("Technology", ["Privacy"])
    with pytest.raises(ExportError):          # no silent default: an empty label blocks export
        split_category("")
    doc = build()
    assert doc["category"] == "Technology"
    assert doc["keywords"] == ["Apple", "macOS", "Privacy"]


def test_cover_is_the_hero_and_the_titled_card_is_the_social_image():
    doc = build()
    assert doc["heroImage"]["src"] == PATHS.cover
    assert (doc["heroImage"]["width"], doc["heroImage"]["height"]) == (1600, 900)
    assert doc["socialImage"]["src"] == PATHS.social
    assert (doc["socialImage"]["width"], doc["socialImage"]["height"]) == (1200, 675)
    assert doc["video"]["poster"] == PATHS.poster and doc["video"]["src"] == PATHS.video
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


# --- category taxonomy -----------------------------------------------------------
import pytest

from app.publishers.web import ExportError


def test_legacy_labels_normalize_to_the_taxonomy_in_the_export():
    doc = build(meta={**META, "category": "Artificial Intelligence"})
    assert doc["category"] == "Technology"


def test_new_categories_export_as_themselves():
    assert build(meta={**META, "category": "Launches"})["category"] == "Launches"
    assert build(meta={**META, "category": "Healthcare"})["category"] == "Healthcare"
    assert build(meta={**META, "category": "Health"})["category"] == "Healthcare"     # near-miss label


def test_unknown_category_blocks_the_export_instead_of_inventing_a_heading():
    with pytest.raises(ExportError, match="taxonomy"):
        build(meta={**META, "category": "Knitting"})


def test_region_is_exported_and_old_drafts_default_to_global():
    assert build(meta={**META, "region": "Asia"})["region"] == "Asia"
    assert build()["region"] == "Global"          # META has no region (a pre-regions draft)


def test_story_is_exported_and_old_drafts_form_a_story_of_their_own():
    own = build()["story"]
    assert own == {"key": "apple-full-disk-access", "label": "Apple Tightens Disk Access"}   # slug + title fallback
    grouped = build(meta={**META, "story_key": "voice", "story_label": "OpenAI voice model"})["story"]
    assert grouped == {"key": "voice", "label": "OpenAI voice model"}
