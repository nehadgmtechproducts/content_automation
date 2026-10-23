"""config/feeds.yaml sanity: unique ids, valid types and tiers, and every category has real tier-1 sources
(a story needs one original-reporting source before it can fire at all)."""
from pathlib import Path

import yaml

from app.settings import CONFIG_DIR

SOURCES = yaml.safe_load((Path(CONFIG_DIR) / "feeds.yaml").read_text())["sources"]
HEALTH_FEEDS = {"bbc_health", "guardian_health", "npr_health", "statnews", "abc_health", "fda_press", "who_news"}


def test_ids_are_unique_and_tiers_and_types_valid():
    ids = [s["id"] for s in SOURCES]
    assert len(ids) == len(set(ids))
    assert {s["tier"] for s in SOURCES} <= {1, 2, 3}
    assert {s["type"] for s in SOURCES} <= {"rss", "google_news", "hackernews", "reddit"}


def test_rss_sources_have_urls_and_google_news_sources_have_queries():
    for s in SOURCES:
        if s["type"] == "rss":
            assert s["url"].startswith("https://"), s["id"]
        if s["type"] == "google_news":
            assert s["query"].strip(), s["id"]


def test_healthcare_has_original_reporting_sources_and_topical_queries():
    by_id = {s["id"]: s for s in SOURCES}
    assert HEALTH_FEEDS <= set(by_id)
    assert all(by_id[i]["tier"] == 1 and by_id[i]["type"] == "rss" for i in HEALTH_FEEDS)
    assert sum(1 for s in SOURCES if s["id"].startswith("gnews_health")) >= 3
