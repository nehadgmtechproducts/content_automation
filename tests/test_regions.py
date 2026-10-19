import pytest

from app.publishers.web import ExportError, resolve_region
from app.regions import DEFAULT_REGION, NAMES, REGIONS, normalize_region, prompt_listing
from app.synth.schemas import MetadataOut


def test_the_six_editions_in_the_order_the_website_shows_them():
    assert NAMES == ("Global", "Asia", "Europe", "Americas", "Middle East", "Africa")


@pytest.mark.parametrize("raw,expected", [
    ("Global", "Global"), ("global", "Global"), ("worldwide", "Global"),
    ("Asia", "Asia"), ("APAC", "Asia"), ("India", "Asia"),
    ("UK", "Europe"), ("Russia", "Europe"),
    ("US", "Americas"), ("Latin America", "Americas"),
    ("middle-east", "Middle East"), ("Gulf", "Middle East"),
    ("Africa", "Africa"),
])
def test_labels_normalize_into_the_list(raw, expected):
    assert normalize_region(raw) == expected


@pytest.mark.parametrize("raw", ["Atlantis", "", None, "  "])
def test_unknown_labels_are_not_guessed(raw):
    assert normalize_region(raw) is None


def test_no_synonym_is_ambiguous():
    seen = {}
    for r in REGIONS:
        for key in (r.name, r.slug, *r.synonyms):
            assert seen.setdefault(key.lower(), r.name) == r.name, f"{key!r} is ambiguous"


def test_the_model_cannot_invent_a_region():
    base = dict(title_variants=["a"], target_keywords=[], hashtags=[], thumbnail_text="t",
                music_mood="m", category="Finance", story_label="A story")
    assert MetadataOut(region="Asia", **base).region == "Asia"
    with pytest.raises(Exception):
        MetadataOut(region="Eastern Europe", **base)
    with pytest.raises(Exception):
        MetadataOut(**base)                     # region is required in new drafts


def test_prompt_lists_every_region():
    assert all(n in prompt_listing() for n in NAMES)


# --- exporter -------------------------------------------------------------------------
def test_old_drafts_without_a_region_default_to_global_so_they_are_never_hidden():
    assert resolve_region(None) == resolve_region("") == DEFAULT_REGION == "Global"


def test_a_valid_region_is_normalized_and_an_invalid_one_blocks_the_export():
    assert resolve_region("APAC") == "Asia"
    with pytest.raises(ExportError, match="not in the list"):
        resolve_region("Atlantis")
